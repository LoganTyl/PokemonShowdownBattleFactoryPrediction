#!/usr/bin/env python3
"""
populate_decisionpoint_state.py

Populates dbo.DecisionPoints.StateJson (and optionally LegalActionsJson) by reconstructing
a lightweight per-battle game state from dbo.BattleEvents and dbo.BattlePokemonReveals.

Design goals:
- Works for Gen9 Battle Factory singles (p1a/p2a active idents)
- No attempt to fully simulate mechanics (trapping, encore, choice-lock, etc.)
- Produces consistent, ML-friendly snapshots at each DecisionPoint.

Usage:
  python populate_decisionpoint_state.py skip 50
  python populate_decisionpoint_state.py replace 50
  python populate_decisionpoint_state.py skip all

Modes:
  skip    : only fills DecisionPoints where StateJson is empty / default placeholder
  replace : overwrites StateJson (and LegalActionsJson if enabled)

Database settings are loaded from .env through app_config.py.
"""

from __future__ import annotations

import json
import sys
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, UTC
from typing import Any, Dict, List, Optional, Tuple

import pyodbc

from app_config import CONN_STR


# ----------------------------
# Configure these as you like
# ----------------------------

STATE_SCHEMA_VERSION = "state_v3_compact_dex_candidates"
PARSER_VERSION = "dp_state_v3_compact_dex_candidates"

# Set True to also populate DecisionPoints.LegalActionsJson with a very rough approximation.
# This does NOT account for trapping, encore, choice-lock, taunt, etc.
POPULATE_LEGAL_ACTIONS = False

# If True, LegalActionsJson "moves" are derived from remaining SetCandidates move options.
# If False, "moves" are derived only from revealed moves (more conservative but sparse).
LEGAL_MOVES_FROM_CANDIDATES = True

# If True, include a small candidate summary for opponent active in StateJson.
# This uses lightweight counts, not full distributions.
INCLUDE_CANDIDATE_SUMMARY = True



# ----------------------------
# Helpers
# ----------------------------

def safe_json(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def is_blank_state(state_json: Any) -> bool:
    if state_json is None:
        return True
    s = str(state_json).strip()
    return s == "" or s == "{}" or s.lower() == "null"


def parse_ident_side(ident: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
    """ident like 'p1a' -> ('p1', 'p1a')"""
    if not ident:
        return None, None
    ident = ident.strip()
    if len(ident) >= 2 and ident[0] == "p" and ident[1] in ("1", "2"):
        return ident[:2], ident
    return None, ident


def now_utc_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds") + "Z"

def to_id(name: str) -> str:
    """
    Convert a Pokémon or move name to Showdown-style id:
    lowercase alphanumeric only.
    Example:
        "Landorus-Therian" -> "landorustherian"
        "Iron Moth" -> "ironmoth"
    """
    if not name:
        return ""
    return re.sub(r"[^a-z0-9]+", "", name.lower())

@dataclass
class DecisionPointRow:
    DecisionPointId: int
    BattleId: str
    TurnNumber: int
    ActorSide: str
    Phase: str
    LineNum: int
    ActorBattlePokemonId: Optional[int]
    OpponentBattlePokemonId: Optional[int]
    RequestType: Optional[str]


@dataclass
class BattleEventRow:
    LineNum: int
    TurnNumber: int
    EventType: str
    SubType: Optional[str]
    SourceSide: Optional[str]
    SourceIdent: Optional[str]
    SourceBattlePokemonId: Optional[int]
    TargetSide: Optional[str]
    TargetIdent: Optional[str]
    TargetBattlePokemonId: Optional[int]
    MoveName: Optional[str]
    ItemName: Optional[str]
    AbilityName: Optional[str]
    SpeciesName: Optional[str]
    HpPctAfter: Optional[float]
    DamagePct: Optional[float]
    IsSuspicious: bool
    SuspicionReason: Optional[str]


@dataclass
class RevealRow:
    BattlePokemonId: int
    RevealType: str
    RevealValue: str
    TurnNumber: Optional[int]
    LineNum: int


def table_has_column(cur, table: str, col: str) -> bool:
    cur.execute(
        """
        SELECT 1
        FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_SCHEMA='dbo'
          AND TABLE_NAME=?
          AND COLUMN_NAME=?
        """,
        table,
        col,
    )
    return cur.fetchone() is not None




# ----------------------------
# Dex lookup (optional enrichment)
# ----------------------------

def load_type_effectiveness_map(cur) -> Dict[str, Dict[str, float]]:
    """Load the 18x18 type chart into a nested dict: atk -> def -> mult."""
    m: Dict[str, Dict[str, float]] = defaultdict(dict)
    if not table_has_column(cur, "DexTypeEffectiveness", "Multiplier"):
        return m
    cur.execute("SELECT AttackingType, DefendingType, Multiplier FROM dbo.DexTypeEffectiveness;")
    for atk, dfn, mult in cur.fetchall():
        m[str(atk)][str(dfn)] = float(mult)
    return m


def compute_type_multiplier(type_map: Dict[str, Dict[str, float]], atk_type: Optional[str], def_types: List[str]) -> Optional[float]:
    if not atk_type or not def_types:
        return None
    mult = 1.0
    for dt in def_types:
        mult *= float(type_map.get(atk_type, {}).get(dt, 1.0))
    return float(mult)


def load_dex_pokemon_map(cur, battle_id: str) -> Dict[str, Dict[str, Any]]:
    """
    Returns mapping keyed by species display name (as stored in BattlePokemon.Species)
    to a compact dex dict: types + base stats.
    """
    out: Dict[str, Dict[str, Any]] = {}
    if not table_has_column(cur, "DexPokemon", "DisplayName"):
        return out

    # Collect species seen in this battle
    cur.execute("SELECT DISTINCT Species FROM dbo.BattlePokemon WHERE BattleId = ?;", battle_id)
    species_list = [r[0] for r in cur.fetchall() if r and r[0]]

    if not species_list:
        return out

    # Fetch by DisplayName first (exact match)
    placeholders = ",".join(["?"] * len(species_list))
    cur.execute(
        f"""
        SELECT DisplayName, Type1, Type2, BaseHP, BaseAtk, BaseDef, BaseSpA, BaseSpD, BaseSpe, BST
        FROM dbo.DexPokemon
        WHERE DisplayName IN ({placeholders});
        """,
        *species_list,
    )
    for row in cur.fetchall():
        (dname, t1, t2, hp, atk, df, spa, spd, spe, bst) = row
        out[str(dname)] = {
            "types": [str(t1)] + ([str(t2)] if t2 else []),
            "baseStats": {
                "hp": int(hp) if hp is not None else None,
                "atk": int(atk) if atk is not None else None,
                "def": int(df) if df is not None else None,
                "spa": int(spa) if spa is not None else None,
                "spd": int(spd) if spd is not None else None,
                "spe": int(spe) if spe is not None else None,
                "bst": int(bst) if bst is not None else None,
            },
        }

    # Fallback: any missing species - try by PokemonId (toID)
    missing = [s for s in species_list if str(s) not in out]
    if missing:
        ids = [to_id(str(s)) for s in missing]
        placeholders = ",".join(["?"] * len(ids))
        cur.execute(
            f"""
            SELECT PokemonId, DisplayName, Type1, Type2, BaseHP, BaseAtk, BaseDef, BaseSpA, BaseSpD, BaseSpe, BST
            FROM dbo.DexPokemon
            WHERE PokemonId IN ({placeholders});
            """,
            *ids,
        )
        by_id = {}
        for row in cur.fetchall():
            (pid, dname, t1, t2, hp, atk, df, spa, spd, spe, bst) = row
            by_id[str(pid)] = (dname, t1, t2, hp, atk, df, spa, spd, spe, bst)

        for s, sid in zip(missing, ids):
            rec = by_id.get(sid)
            if not rec:
                continue
            dname, t1, t2, hp, atk, df, spa, spd, spe, bst = rec
            out[str(s)] = {
                "types": [str(t1)] + ([str(t2)] if t2 else []),
                "baseStats": {
                    "hp": int(hp) if hp is not None else None,
                    "atk": int(atk) if atk is not None else None,
                    "def": int(df) if df is not None else None,
                    "spa": int(spa) if spa is not None else None,
                    "spd": int(spd) if spd is not None else None,
                    "spe": int(spe) if spe is not None else None,
                    "bst": int(bst) if bst is not None else None,
                },
            }

    return out


def load_dex_move_map(cur, battle_id: str) -> Dict[str, Dict[str, Any]]:
    """Return mapping MoveName -> compact metadata."""
    out: Dict[str, Dict[str, Any]] = {}
    if not table_has_column(cur, "DexMove", "MoveName"):
        return out

    cur.execute(
        """
        SELECT DISTINCT MoveName
        FROM dbo.BattleEvents
        WHERE BattleId = ?
          AND EventType = 'move'
          AND MoveName IS NOT NULL
          AND MoveName <> '';
        """,
        battle_id,
    )
    move_names = [r[0] for r in cur.fetchall() if r and r[0]]
    if not move_names:
        return out

    placeholders = ",".join(["?"] * len(move_names))
    cur.execute(
        f"""
        SELECT MoveName, TypeName, Category, BasePower, Priority
        FROM dbo.DexMove
        WHERE MoveName IN ({placeholders});
        """,
        *move_names,
    )
    for mn, t, cat, bp, pr in cur.fetchall():
        out[str(mn)] = {
            "type": str(t) if t is not None else None,
            "category": str(cat) if cat is not None else None,
            "basePower": int(bp) if bp is not None else None,
            "priority": int(pr) if pr is not None else None,
        }
    return out


# ----------------------------
# Loaders
# ----------------------------

def get_battle_ids(cur, limit: Optional[int]) -> List[str]:
    cur.execute(
        """
        SELECT BattleId
        FROM dbo.Battles
        WHERE BattleId LIKE 'gen9battlefactory-%'
        ORDER BY BattleId;
        """
    )
    ids = [r[0] for r in cur.fetchall()]
    if limit is None:
        return ids
    return ids[:limit]


def load_decision_points(cur, battle_id: str) -> Tuple[List[DecisionPointRow], Dict[int, Any]]:
    # Pull the DP rows we need; also grab existing StateJson so skip-mode is fast.
    select_cols = ["DecisionPointId", "BattleId", "TurnNumber", "ActorSide", "Phase", "LineNum"]
    optional_cols = ["ActorBattlePokemonId", "OpponentBattlePokemonId", "RequestType", "StateJson"]
    cols = [c for c in (select_cols + optional_cols) if table_has_column(cur, "DecisionPoints", c)]

    cur.execute(
        f"""
        SELECT {", ".join(cols)}
        FROM dbo.DecisionPoints
        WHERE BattleId = ?
        ORDER BY LineNum;
        """,
        battle_id,
    )

    dps: List[DecisionPointRow] = []
    existing_state: Dict[int, Any] = {}

    for row in cur.fetchall():
        m = dict(zip(cols, row))
        dp_id = int(m["DecisionPointId"])
        existing_state[dp_id] = m.get("StateJson")
        dps.append(
            DecisionPointRow(
                DecisionPointId=dp_id,
                BattleId=str(m["BattleId"]),
                TurnNumber=int(m["TurnNumber"]),
                ActorSide=str(m["ActorSide"]),
                Phase=str(m["Phase"]),
                LineNum=int(m["LineNum"]),
                ActorBattlePokemonId=int(m["ActorBattlePokemonId"]) if m.get("ActorBattlePokemonId") is not None else None,
                OpponentBattlePokemonId=int(m["OpponentBattlePokemonId"]) if m.get("OpponentBattlePokemonId") is not None else None,
                RequestType=str(m["RequestType"]) if m.get("RequestType") is not None else None,
            )
        )

    return dps, existing_state


def load_events(cur, battle_id: str) -> List[BattleEventRow]:
    cols = [
        "LineNum",
        "TurnNumber",
        "EventType",
        "SubType",
        "SourceSide",
        "SourceIdent",
        "SourceBattlePokemonId",
        "TargetSide",
        "TargetIdent",
        "TargetBattlePokemonId",
        "MoveName",
        "ItemName",
        "AbilityName",
        "SpeciesName",
        "HpPctAfter",
        "DamagePct",
        "IsSuspicious",
        "SuspicionReason",
    ]
    cols = [c for c in cols if table_has_column(cur, "BattleEvents", c)]

    cur.execute(
        f"""
        SELECT {", ".join(cols)}
        FROM dbo.BattleEvents
        WHERE BattleId = ?
        ORDER BY LineNum;
        """,
        battle_id,
    )

    out: List[BattleEventRow] = []
    for r in cur.fetchall():
        m = dict(zip(cols, r))
        out.append(
            BattleEventRow(
                LineNum=int(m["LineNum"]),
                TurnNumber=int(m["TurnNumber"]) if m.get("TurnNumber") is not None else 0,
                EventType=str(m["EventType"]),
                SubType=str(m["SubType"]) if m.get("SubType") is not None else None,
                SourceSide=str(m["SourceSide"]) if m.get("SourceSide") is not None else None,
                SourceIdent=str(m["SourceIdent"]) if m.get("SourceIdent") is not None else None,
                SourceBattlePokemonId=int(m["SourceBattlePokemonId"]) if m.get("SourceBattlePokemonId") is not None else None,
                TargetSide=str(m["TargetSide"]) if m.get("TargetSide") is not None else None,
                TargetIdent=str(m["TargetIdent"]) if m.get("TargetIdent") is not None else None,
                TargetBattlePokemonId=int(m["TargetBattlePokemonId"]) if m.get("TargetBattlePokemonId") is not None else None,
                MoveName=str(m["MoveName"]) if m.get("MoveName") is not None else None,
                ItemName=str(m["ItemName"]) if m.get("ItemName") is not None else None,
                AbilityName=str(m["AbilityName"]) if m.get("AbilityName") is not None else None,
                SpeciesName=str(m["SpeciesName"]) if m.get("SpeciesName") is not None else None,
                HpPctAfter=float(m["HpPctAfter"]) if m.get("HpPctAfter") is not None else None,
                DamagePct=float(m["DamagePct"]) if m.get("DamagePct") is not None else None,
                IsSuspicious=bool(m.get("IsSuspicious") or 0),
                SuspicionReason=str(m["SuspicionReason"]) if m.get("SuspicionReason") is not None else None,
            )
        )
    return out


def load_reveals(cur, battle_id: str) -> List[RevealRow]:
    cur.execute(
        """
        SELECT R.BattlePokemonId, R.RevealType, R.RevealValue, R.TurnNumber, R.LineNum
        FROM dbo.BattlePokemonReveals R
        JOIN dbo.BattlePokemon BP ON BP.BattlePokemonId = R.BattlePokemonId
        WHERE BP.BattleId = ?
        ORDER BY R.LineNum;
        """,
        battle_id,
    )

    out: List[RevealRow] = []
    for bp_id, rtype, rval, tnum, lnum in cur.fetchall():
        out.append(
            RevealRow(
                BattlePokemonId=int(bp_id),
                RevealType=str(rtype),
                RevealValue=str(rval),
                TurnNumber=int(tnum) if tnum is not None else None,
                LineNum=int(lnum),
            )
        )
    return out


def load_battlepokemon_lookup(cur, battle_id: str) -> Dict[int, Dict[str, Any]]:
    cur.execute(
        """
        SELECT BattlePokemonId, Side, Slot, Species, BattleName
        FROM dbo.BattlePokemon
        WHERE BattleId = ?;
        """,
        battle_id,
    )
    out: Dict[int, Dict[str, Any]] = {}
    for bp_id, side, slot, species, bname in cur.fetchall():
        out[int(bp_id)] = {
            "battlePokemonId": int(bp_id),
            "side": str(side) if side is not None else None,
            "slot": int(slot) if slot is not None else None,
            "species": str(species) if species is not None else None,
            "battleName": str(bname) if bname is not None else None,
        }
    return out


def get_candidate_summary(cur, battle_pokemon_id: int) -> dict:
    """Return candidate counts for a BattlePokemonId.

    Distinguishes:
      - no rows in SetCandidates (not initialized / snapshot drift / tier mismatch)
      - rows exist but all eliminated (eliminated_to_zero)
      - normal (some remaining)
    """
    cur.execute(
        """
        SELECT
            COUNT(*) AS TotalCandidates,
            SUM(CASE WHEN IsEliminated = 0 THEN 1 ELSE 0 END) AS RemainingCandidates
        FROM dbo.SetCandidates
        WHERE BattlePokemonId = ?;
        """,
        battle_pokemon_id,
    )
    row = cur.fetchone()
    total = int(row[0]) if row and row[0] is not None else 0
    # SUM over zero rows returns NULL; treat as 0.
    remaining = int(row[1]) if row and row[1] is not None else 0

    has_candidates = total > 0
    if not has_candidates:
        status = "no_candidate_data"
    elif remaining == 0:
        status = "eliminated_to_zero"
    else:
        status = "ok"

    return {
        "hasCandidates": has_candidates,
        "totalCount": total,
        "remainingCount": remaining,
        "status": status,
    }


def get_legal_moves_from_candidates(cur, battle_pokemon_id: int) -> List[str]:
    cur.execute(
        """
        SELECT DISTINCT MO.MoveName
        FROM dbo.SetCandidates SC
        JOIN dbo.BF_SetMoveOption MO ON MO.SetId = SC.BF_SetId
        WHERE SC.BattlePokemonId = ?
          AND SC.IsEliminated = 0
        ORDER BY MO.MoveName;
        """,
        battle_pokemon_id,
    )
    return [r[0] for r in cur.fetchall() if r and r[0]]


# ----------------------------
# State reconstruction
# ----------------------------

class BattleState:
    """Lightweight battle state tracker keyed by active ident 'p1a'/'p2a'."""

    def __init__(self) -> None:
        self.active_by_ident: Dict[str, Optional[int]] = {"p1a": None, "p2a": None}
        self.hp_pct_by_bp: Dict[int, Optional[float]] = {}
        self.fainted_bp: set[int] = set()
        self.tera_used_by_side: Dict[str, bool] = {"p1": False, "p2": False}

        # Reveals known so far per BattlePokemonId
        self.moves_by_bp: Dict[int, set[str]] = defaultdict(set)
        self.item_by_bp: Dict[int, Optional[str]] = defaultdict(lambda: None)
        self.ability_by_bp: Dict[int, Optional[str]] = defaultdict(lambda: None)
        self.tera_by_bp: Dict[int, Optional[str]] = defaultdict(lambda: None)

    def apply_reveal(self, r: RevealRow) -> None:
        bp = r.BattlePokemonId
        rt = r.RevealType
        rv = r.RevealValue
        if rt == "move":
            self.moves_by_bp[bp].add(rv)
        elif rt == "item":
            self.item_by_bp[bp] = rv
        elif rt == "ability":
            self.ability_by_bp[bp] = rv
        elif rt == "tera":
            self.tera_by_bp[bp] = rv

    def apply_event(self, e: BattleEventRow) -> None:
        et = e.EventType

        # Active tracking (singles): switch events update the target ident.
        if et == "switch":
            if e.TargetIdent and e.TargetBattlePokemonId:
                ident = e.TargetIdent.strip()
                if ident in self.active_by_ident:
                    self.active_by_ident[ident] = int(e.TargetBattlePokemonId)

        # HP tracking
        if et == "hp_change":
            bp_id = e.TargetBattlePokemonId or e.SourceBattlePokemonId
            if bp_id and e.HpPctAfter is not None:
                self.hp_pct_by_bp[int(bp_id)] = float(e.HpPctAfter)

        # Faint tracking
        if et == "faint":
            bp_id = e.TargetBattlePokemonId or e.SourceBattlePokemonId
            if bp_id:
                bp_id = int(bp_id)
                self.fainted_bp.add(bp_id)
                # Clear if currently active
                for ident, cur_bp in list(self.active_by_ident.items()):
                    if cur_bp == bp_id:
                        self.active_by_ident[ident] = None

        # Tera used tracking
        if et == "terastallize":
            side, _ = parse_ident_side(e.TargetIdent)
            if side in ("p1", "p2"):
                self.tera_used_by_side[side] = True
            if e.TargetBattlePokemonId and e.SubType:
                self.tera_by_bp[int(e.TargetBattlePokemonId)] = str(e.SubType)


def snapshot_for_decision_point(
    cur,
    dp: DecisionPointRow,
    state: BattleState,
    bp_lookup: Dict[int, Dict[str, Any]],
    dex_pokemon: Dict[str, Dict[str, Any]],
    dex_moves: Dict[str, Dict[str, Any]],
    type_map: Dict[str, Dict[str, float]],
) -> Dict[str, Any]:
    actor_side = dp.ActorSide
    opp_side = "p2" if actor_side == "p1" else "p1"
    actor_ident = actor_side + "a"
    opp_ident = opp_side + "a"

    actor_bp = state.active_by_ident.get(actor_ident)
    opp_bp = state.active_by_ident.get(opp_ident)

    # Prefer explicit ids stored on DP if present
    if dp.ActorBattlePokemonId is not None:
        actor_bp = dp.ActorBattlePokemonId
    if dp.OpponentBattlePokemonId is not None:
        opp_bp = dp.OpponentBattlePokemonId

    

    # Determine defending types for effectiveness (use opponent active's dex types if available).
    actor_species = bp_lookup.get(int(actor_bp), {}).get("species") if actor_bp else None
    opp_species = bp_lookup.get(int(opp_bp), {}).get("species") if opp_bp else None

    actor_types = dex_pokemon.get(str(actor_species), {}).get("types", []) if actor_species else []
    opp_types = dex_pokemon.get(str(opp_species), {}).get("types", []) if opp_species else []

    def mon_obj(bp_id: Optional[int], opp_def_types: List[str]) -> Optional[Dict[str, Any]]:
        if not bp_id:
            return None
        bp_id = int(bp_id)
        base = dict(bp_lookup.get(bp_id, {"battlePokemonId": bp_id}))
        base["hpPct"] = state.hp_pct_by_bp.get(bp_id)
        base["isFainted"] = bp_id in state.fainted_bp

        # Compact dex enrichment (types + base stats). We do NOT attempt to compute derived stats (EVs/IVs/nature).
        species_name = base.get("species") or base.get("battleName")
        dex = dex_pokemon.get(str(species_name)) if species_name else None
        if dex:
            base["dex"] = dex

        revealed_moves = sorted(state.moves_by_bp.get(bp_id, set()))
        move_meta: List[Dict[str, Any]] = []
        for mn in revealed_moves:
            meta = dict(dex_moves.get(mn, {}))
            if meta:
                eff = compute_type_multiplier(type_map, meta.get("type"), opp_def_types)
                meta["name"] = mn
                meta["effVsOpponent"] = eff
                move_meta.append(meta)
            else:
                move_meta.append({"name": mn})

        base["reveals"] = {
            "moves": revealed_moves,
            "moveMeta": move_meta,
            "item": state.item_by_bp.get(bp_id),
            "ability": state.ability_by_bp.get(bp_id),
            "teraType": state.tera_by_bp.get(bp_id),
        }

        if INCLUDE_CANDIDATE_SUMMARY:
            base["candidates"] = get_candidate_summary(cur, bp_id)
        return base

    return {
        "meta": {
            "battleId": dp.BattleId,
            "turnNumber": dp.TurnNumber,
            "phase": dp.Phase,
            "actorSide": actor_side,
            "requestType": dp.RequestType,
            "lineNum": dp.LineNum,
            "stateSchemaVersion": STATE_SCHEMA_VERSION,
            "parserVersion": PARSER_VERSION,
            "generatedAt": now_utc_iso(),
        },
        "actorActive": mon_obj(actor_bp, opp_types),
        "opponentActive": mon_obj(opp_bp, actor_types),
        "teraUsedBySide": dict(state.tera_used_by_side),
    }


def build_legal_actions_json(cur, dp: DecisionPointRow, state: BattleState) -> Optional[Dict[str, Any]]:
    """
    Rough approximation:
      - moves: revealed moves OR union of candidate moves for the active mon
      - switches: all alive bench species on that side (excluding active)
    No trapping / lockout logic.
    """
    actor_side = dp.ActorSide
    actor_ident = actor_side + "a"
    actor_bp = state.active_by_ident.get(actor_ident)
    if dp.ActorBattlePokemonId is not None:
        actor_bp = dp.ActorBattlePokemonId
    if not actor_bp:
        return None

    actor_bp = int(actor_bp)
    moves: List[str]
    if LEGAL_MOVES_FROM_CANDIDATES:
        moves = get_legal_moves_from_candidates(cur, actor_bp)
    else:
        moves = sorted(state.moves_by_bp.get(actor_bp, set()))

    cur.execute(
        """
        SELECT BattlePokemonId, Species
        FROM dbo.BattlePokemon
        WHERE BattleId = ?
          AND Side = ?;
        """,
        dp.BattleId,
        actor_side,
    )

    switches: List[str] = []
    for bp_id, species in cur.fetchall():
        bp_id = int(bp_id)
        if bp_id == actor_bp:
            continue
        if bp_id in state.fainted_bp:
            continue
        switches.append(str(species))

    return {
        "approximate": True,
        "source": "candidates" if LEGAL_MOVES_FROM_CANDIDATES else "reveals",
        "moves": moves,
        "switches": sorted(set(switches)),
        "canTera": not state.tera_used_by_side.get(actor_side, False),
    }


def update_decision_points(cur, updates: List[Tuple[Any, ...]]) -> int:
    has_legal = table_has_column(cur, "DecisionPoints", "LegalActionsJson")
    has_parser = table_has_column(cur, "DecisionPoints", "ParserVersion")

    set_parts = ["StateJson = ?", "StateSchemaVersion = ?"]
    if has_legal and POPULATE_LEGAL_ACTIONS:
        set_parts.append("LegalActionsJson = ?")
    if has_parser:
        set_parts.append("ParserVersion = ?")

    sql = f"""
        UPDATE dbo.DecisionPoints
        SET {", ".join(set_parts)}
        WHERE DecisionPointId = ?;
    """

    cur.fast_executemany = False  # safest for variable-length JSON
    cur.executemany(sql, updates)
    return len(updates)


def main() -> int:
    if len(sys.argv) < 3:
        print("Usage: python populate_decisionpoint_state.py <skip|replace> <N|all>")
        return 2

    mode = sys.argv[1].strip().lower()
    if mode not in ("skip", "replace"):
        print("First arg must be skip or replace")
        return 2

    n_arg = sys.argv[2].strip().lower()
    limit: Optional[int] = None if n_arg == "all" else int(n_arg)

    conn = pyodbc.connect(CONN_STR)
    conn.autocommit = False

    try:
        cur = conn.cursor()
        type_map = load_type_effectiveness_map(cur)


        battle_ids = get_battle_ids(cur, limit)
        print(f"[INFO] Battles to process: {len(battle_ids)}")

        total_updated = 0

        for idx, battle_id in enumerate(battle_ids, start=1):
            dps, existing_state = load_decision_points(cur, battle_id)
            if not dps:
                continue

            bp_lookup = load_battlepokemon_lookup(cur, battle_id)
            events = load_events(cur, battle_id)
            reveals = load_reveals(cur, battle_id)

            dex_pokemon = load_dex_pokemon_map(cur, battle_id)
            dex_moves = load_dex_move_map(cur, battle_id)


            # Group decision points by LineNum for quick snapshots.
            dp_by_linenum: Dict[int, List[DecisionPointRow]] = defaultdict(list)
            for dp in dps:
                dp_by_linenum[dp.LineNum].append(dp)

            # Build event bucket by line.
            event_by_linenum: Dict[int, List[BattleEventRow]] = defaultdict(list)
            for e in events:
                event_by_linenum[e.LineNum].append(e)

            # Iterate over every LineNum that has an event or a decision point.
            all_linenums = sorted(set([e.LineNum for e in events] + list(dp_by_linenum.keys())))

            state = BattleState()
            reveal_idx = 0
            updates: List[Tuple[Any, ...]] = []

            for ln in all_linenums:
                # Apply reveals up to this line
                while reveal_idx < len(reveals) and reveals[reveal_idx].LineNum <= ln:
                    state.apply_reveal(reveals[reveal_idx])
                    reveal_idx += 1

                # Apply events at this line
                for e in event_by_linenum.get(ln, []):
                    state.apply_event(e)

                # Snapshot decision points at this line
                for dp in dp_by_linenum.get(ln, []):
                    existing = existing_state.get(dp.DecisionPointId)

                    if mode == "skip" and existing is not None and not is_blank_state(existing):
                        continue

                    snap = snapshot_for_decision_point(cur, dp, state, bp_lookup, dex_pokemon, dex_moves, type_map)
                    state_json = safe_json(snap)

                    tup: List[Any] = [state_json, STATE_SCHEMA_VERSION]

                    if POPULATE_LEGAL_ACTIONS and table_has_column(cur, "DecisionPoints", "LegalActionsJson"):
                        la = build_legal_actions_json(cur, dp, state)
                        tup.append(safe_json(la) if la is not None else None)

                    if table_has_column(cur, "DecisionPoints", "ParserVersion"):
                        tup.append(PARSER_VERSION)

                    tup.append(dp.DecisionPointId)
                    updates.append(tuple(tup))

            if updates:
                updated = update_decision_points(cur, updates)
                conn.commit()
                total_updated += updated

            if idx % 10 == 0 or idx == len(battle_ids):
                print(f"[INFO] Processed {idx}/{len(battle_ids)} battles. Updated DPs so far: {total_updated}")

        print(f"[DONE] Total DecisionPoints updated: {total_updated}")
        return 0

    except Exception as e:
        conn.rollback()
        print("[ERROR]", e)
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
