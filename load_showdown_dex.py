"""
Load lightweight dex metadata into SQL Server for feature enrichment.

What it does
- Downloads (or reads locally) Pokémon Showdown client data:
    https://play.pokemonshowdown.com/data/pokedex.json
    https://play.pokemonshowdown.com/data/moves.json
- Upserts into:
    dbo.DexPokemon, dbo.DexMove
- Populates dbo.DexTypeEffectiveness with the standard modern (Gen 6+) type chart.

Notes
- This script uses the JSON endpoints (not the JS) because they're easier to parse.
- Type effectiveness here is only the classic 18-type damage multiplier chart.
  (The extra logic in Showdown's typechart.js about weather/status isn't required for type effectiveness.)
"""

from __future__ import annotations

import json
import re
import sys
from typing import Any, Dict, List, Tuple

import pyodbc
import urllib.request

from app_config import CONN_STR

POKEDEX_JSON_URL = "https://play.pokemonshowdown.com/data/pokedex.json"
MOVES_JSON_URL = "https://play.pokemonshowdown.com/data/moves.json"


def fetch_json(url: str) -> Dict[str, Any]:
    with urllib.request.urlopen(url) as resp:
        raw = resp.read().decode("utf-8")
    return json.loads(raw)


def read_json_file(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


# -------------------------
# Type effectiveness chart
# -------------------------
_TYPES = [
    "Normal","Fire","Water","Electric","Grass","Ice","Fighting","Poison","Ground","Flying",
    "Psychic","Bug","Rock","Ghost","Dragon","Dark","Steel","Fairy",
]

# Unspecified pairs default to 1.0
_TYPE_MULT: Dict[Tuple[str, str], float] = {
    ("Normal","Rock"):0.5, ("Normal","Ghost"):0.0, ("Normal","Steel"):0.5,

    ("Fire","Fire"):0.5, ("Fire","Water"):0.5, ("Fire","Grass"):2.0, ("Fire","Ice"):2.0,
    ("Fire","Bug"):2.0, ("Fire","Rock"):0.5, ("Fire","Dragon"):0.5, ("Fire","Steel"):2.0,

    ("Water","Fire"):2.0, ("Water","Water"):0.5, ("Water","Grass"):0.5, ("Water","Ground"):2.0,
    ("Water","Rock"):2.0, ("Water","Dragon"):0.5,

    ("Electric","Water"):2.0, ("Electric","Electric"):0.5, ("Electric","Grass"):0.5, ("Electric","Ground"):0.0,
    ("Electric","Flying"):2.0, ("Electric","Dragon"):0.5,

    ("Grass","Fire"):0.5, ("Grass","Water"):2.0, ("Grass","Grass"):0.5, ("Grass","Poison"):0.5,
    ("Grass","Ground"):2.0, ("Grass","Flying"):0.5, ("Grass","Bug"):0.5, ("Grass","Rock"):2.0,
    ("Grass","Dragon"):0.5, ("Grass","Steel"):0.5,

    ("Ice","Fire"):0.5, ("Ice","Water"):0.5, ("Ice","Grass"):2.0, ("Ice","Ice"):0.5,
    ("Ice","Ground"):2.0, ("Ice","Flying"):2.0, ("Ice","Dragon"):2.0, ("Ice","Steel"):0.5,

    ("Fighting","Normal"):2.0, ("Fighting","Ice"):2.0, ("Fighting","Rock"):2.0, ("Fighting","Dark"):2.0, ("Fighting","Steel"):2.0,
    ("Fighting","Poison"):0.5, ("Fighting","Flying"):0.5, ("Fighting","Psychic"):0.5, ("Fighting","Bug"):0.5,
    ("Fighting","Ghost"):0.0, ("Fighting","Fairy"):0.5,

    ("Poison","Grass"):2.0, ("Poison","Fairy"):2.0,
    ("Poison","Poison"):0.5, ("Poison","Ground"):0.5, ("Poison","Rock"):0.5, ("Poison","Ghost"):0.5,
    ("Poison","Steel"):0.0,

    ("Ground","Fire"):2.0, ("Ground","Electric"):2.0, ("Ground","Grass"):0.5, ("Ground","Poison"):2.0,
    ("Ground","Flying"):0.0, ("Ground","Bug"):0.5, ("Ground","Rock"):2.0, ("Ground","Steel"):2.0,

    ("Flying","Grass"):2.0, ("Flying","Fighting"):2.0, ("Flying","Bug"):2.0,
    ("Flying","Electric"):0.5, ("Flying","Rock"):0.5, ("Flying","Steel"):0.5,

    ("Psychic","Fighting"):2.0, ("Psychic","Poison"):2.0,
    ("Psychic","Psychic"):0.5, ("Psychic","Steel"):0.5, ("Psychic","Dark"):0.0,

    ("Bug","Grass"):2.0, ("Bug","Psychic"):2.0, ("Bug","Dark"):2.0,
    ("Bug","Fire"):0.5, ("Bug","Fighting"):0.5, ("Bug","Poison"):0.5, ("Bug","Flying"):0.5,
    ("Bug","Ghost"):0.5, ("Bug","Steel"):0.5, ("Bug","Fairy"):0.5,

    ("Rock","Fire"):2.0, ("Rock","Ice"):2.0, ("Rock","Flying"):2.0, ("Rock","Bug"):2.0,
    ("Rock","Fighting"):0.5, ("Rock","Ground"):0.5, ("Rock","Steel"):0.5,

    ("Ghost","Psychic"):2.0, ("Ghost","Ghost"):2.0,
    ("Ghost","Normal"):0.0, ("Ghost","Dark"):0.5,

    ("Dragon","Dragon"):2.0, ("Dragon","Steel"):0.5, ("Dragon","Fairy"):0.0,

    ("Dark","Psychic"):2.0, ("Dark","Ghost"):2.0,
    ("Dark","Fighting"):0.5, ("Dark","Dark"):0.5, ("Dark","Fairy"):0.5,

    ("Steel","Ice"):2.0, ("Steel","Rock"):2.0, ("Steel","Fairy"):2.0,
    ("Steel","Fire"):0.5, ("Steel","Water"):0.5, ("Steel","Electric"):0.5, ("Steel","Steel"):0.5,

    ("Fairy","Fighting"):2.0, ("Fairy","Dragon"):2.0, ("Fairy","Dark"):2.0,
    ("Fairy","Fire"):0.5, ("Fairy","Poison"):0.5, ("Fairy","Steel"):0.5,
}


def build_type_rows() -> List[Tuple[str, str, float]]:
    out: List[Tuple[str, str, float]] = []
    for atk in _TYPES:
        for dfn in _TYPES:
            out.append((atk, dfn, float(_TYPE_MULT.get((atk, dfn), 1.0))))
    return out


# -------------------------
# SQL helpers
# -------------------------
def table_columns(cur, table: str) -> set[str]:
    cur.execute(
        """
        SELECT COLUMN_NAME
        FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_SCHEMA='dbo' AND TABLE_NAME=?
        """,
        table,
    )
    return {r[0] for r in cur.fetchall()}


def upsert_dexpokemon(cur, pokedex: Dict[str, Any]) -> int:
    cols = table_columns(cur, "DexPokemon")
    if not {"PokemonId", "DisplayName", "Type1"}.issubset(cols):
        raise RuntimeError("dbo.DexPokemon missing required columns; run dex_schema.sql first.")

    rows: List[Tuple] = []
    # Build a quick lookup from showdown id -> entry (already is pokedex),
    # and allow inheritance from baseSpecies when formes omit fields.
    for pid, p in pokedex.items():
        display = p.get("name") or pid

        types = p.get("types")
        bs = p.get("baseStats")

        # Inheritance: many cosmetic formes omit types/baseStats and rely on baseSpecies
        if (not types) or (not bs):
            base_name = p.get("baseSpecies")
            if base_name:
                base_id = re.sub(r"[^a-z0-9]+", "", base_name.lower())  # showdown toID
                base_p = pokedex.get(base_id)
                if base_p:
                    if not types:
                        types = base_p.get("types")
                    if not bs:
                        bs = base_p.get("baseStats")

        # If still missing types, skip (avoids NOT NULL failure)
        if not types or not types[0]:
            continue

        t1 = types[0]
        t2 = types[1] if len(types) > 1 else None

        bs = bs or {}
        hp, atk, df, spa, spd, spe = (bs.get(k) for k in ("hp","atk","def","spa","spd","spe"))

        bst = None
        if all(isinstance(x, (int, float)) for x in (hp, atk, df, spa, spd, spe)):
            bst = int(hp + atk + df + spa + spd + spe)

        weight = p.get("weightkg")
        rows.append((pid, display, t1, t2, hp, atk, df, spa, spd, spe, bst, weight))

    cur.execute("IF OBJECT_ID('tempdb..#DexPokemonSrc') IS NOT NULL DROP TABLE #DexPokemonSrc;")
    cur.execute(
        """
        CREATE TABLE #DexPokemonSrc(
          PokemonId VARCHAR(64) NOT NULL,
          DisplayName NVARCHAR(96) NOT NULL,
          Type1 VARCHAR(16) NOT NULL,
          Type2 VARCHAR(16) NULL,
          BaseHP SMALLINT NULL,
          BaseAtk SMALLINT NULL,
          BaseDef SMALLINT NULL,
          BaseSpA SMALLINT NULL,
          BaseSpD SMALLINT NULL,
          BaseSpe SMALLINT NULL,
          BST SMALLINT NULL,
          WeightKg DECIMAL(8,2) NULL
        );
        """
    )

    cur.fast_executemany = True
    cur.executemany(
        """
        INSERT INTO #DexPokemonSrc
        (PokemonId,DisplayName,Type1,Type2,BaseHP,BaseAtk,BaseDef,BaseSpA,BaseSpD,BaseSpe,BST,WeightKg)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        rows,
    )

    cur.execute(
        """
        MERGE dbo.DexPokemon AS T
        USING #DexPokemonSrc AS S
          ON T.PokemonId = S.PokemonId
        WHEN MATCHED THEN UPDATE SET
          DisplayName=S.DisplayName, Type1=S.Type1, Type2=S.Type2,
          BaseHP=S.BaseHP, BaseAtk=S.BaseAtk, BaseDef=S.BaseDef,
          BaseSpA=S.BaseSpA, BaseSpD=S.BaseSpD, BaseSpe=S.BaseSpe,
          BST=S.BST, WeightKg=S.WeightKg
        WHEN NOT MATCHED THEN
          INSERT (PokemonId,DisplayName,Type1,Type2,BaseHP,BaseAtk,BaseDef,BaseSpA,BaseSpD,BaseSpe,BST,WeightKg)
          VALUES (S.PokemonId,S.DisplayName,S.Type1,S.Type2,S.BaseHP,S.BaseAtk,S.BaseDef,S.BaseSpA,S.BaseSpD,S.BaseSpe,S.BST,S.WeightKg);
        """
    )
    return len(rows)


def upsert_dexmove(cur, moves: Dict[str, Any]) -> int:
    cols = table_columns(cur, "DexMove")
    if not {"MoveId", "MoveName"}.issubset(cols):
        raise RuntimeError("dbo.DexMove missing required columns; run dex_schema.sql first.")

    rows: List[Tuple] = []
    for mid, m in moves.items():
        name = m.get("name") or mid
        t = m.get("type")
        cat = m.get("category")
        bp = m.get("basePower")
        acc = m.get("accuracy")
        prio = m.get("priority")
        target = m.get("target")
        flags = m.get("flags") or {}
        # accuracy can be True; store null for non-numeric
        acc_num = acc if isinstance(acc, (int, float)) else None
        rows.append((mid, name, t, cat, bp, acc_num, prio, target, json.dumps(flags, ensure_ascii=False)))

    cur.execute("IF OBJECT_ID('tempdb..#DexMoveSrc') IS NOT NULL DROP TABLE #DexMoveSrc;")
    cur.execute(
        """
        CREATE TABLE #DexMoveSrc(
          MoveId VARCHAR(64) NOT NULL,
          MoveName NVARCHAR(96) NOT NULL,
          TypeName VARCHAR(16) NULL,
          Category VARCHAR(16) NULL,
          BasePower SMALLINT NULL,
          Accuracy SMALLINT NULL,
          Priority SMALLINT NULL,
          Target VARCHAR(32) NULL,
          FlagsJson NVARCHAR(MAX) NULL
        );
        """
    )

    cur.fast_executemany = True
    cur.executemany(
        """
        INSERT INTO #DexMoveSrc
        (MoveId,MoveName,TypeName,Category,BasePower,Accuracy,Priority,Target,FlagsJson)
        VALUES (?,?,?,?,?,?,?,?,?)
        """,
        rows,
    )

    cur.execute(
        """
        MERGE dbo.DexMove AS T
        USING #DexMoveSrc AS S
          ON T.MoveId = S.MoveId
        WHEN MATCHED THEN UPDATE SET
          MoveName=S.MoveName, TypeName=S.TypeName, Category=S.Category,
          BasePower=S.BasePower, Accuracy=S.Accuracy, Priority=S.Priority,
          Target=S.Target, FlagsJson=S.FlagsJson
        WHEN NOT MATCHED THEN
          INSERT (MoveId,MoveName,TypeName,Category,BasePower,Accuracy,Priority,Target,FlagsJson)
          VALUES (S.MoveId,S.MoveName,S.TypeName,S.Category,S.BasePower,S.Accuracy,S.Priority,S.Target,S.FlagsJson);
        """
    )
    return len(rows)


def load_type_effectiveness(cur) -> int:
    rows = build_type_rows()
    cur.execute("TRUNCATE TABLE dbo.DexTypeEffectiveness;")
    cur.fast_executemany = True
    cur.executemany(
        """
        INSERT INTO dbo.DexTypeEffectiveness (AttackingType, DefendingType, Multiplier)
        VALUES (?, ?, ?)
        """,
        rows,
    )
    return len(rows)


def main(argv: List[str]) -> int:
    # Usage:
    #   python load_showdown_dex.py online
    #   python load_showdown_dex.py offline path/to/pokedex.json path/to/moves.json
    if len(argv) < 2:
        print("Usage: python load_showdown_dex.py online | offline <pokedex.json> <moves.json>")
        return 2

    mode = argv[1].lower()
    if mode == "online":
        pokedex = fetch_json(POKEDEX_JSON_URL)
        moves = fetch_json(MOVES_JSON_URL)
    elif mode == "offline":
        if len(argv) < 4:
            print("Usage: python load_showdown_dex.py offline <pokedex.json> <moves.json>")
            return 2
        pokedex = read_json_file(argv[2])
        moves = read_json_file(argv[3])
    else:
        print("Unknown mode:", mode)
        return 2

    with pyodbc.connect(CONN_STR, timeout=60) as conn:
        conn.autocommit = False
        cur = conn.cursor()

        print("Upserting DexPokemon...")
        n1 = upsert_dexpokemon(cur, pokedex)
        print("  rows:", n1)

        print("Upserting DexMove...")
        n2 = upsert_dexmove(cur, moves)
        print("  rows:", n2)

        print("Loading DexTypeEffectiveness...")
        n3 = load_type_effectiveness(cur)
        print("  rows:", n3)

        conn.commit()

    print("Done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
