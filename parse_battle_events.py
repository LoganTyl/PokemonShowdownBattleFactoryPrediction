import json
import re
from dataclasses import dataclass, asdict
from typing import Any, Dict, List, Optional, Tuple

import pyodbc

from app_config import CONN_STR

# --------------------------
# Helpers
# --------------------------

IDENT_RE = re.compile(r"^(p[12][a-z]):\s*(.+)$")  # e.g. "p1a: Pikachu"
HP_PCT_RE = re.compile(r"^(\d+)(?:\.\d+)?/(\d+)(?:\s+.*)?$")  # "75/100", "0 fnt", etc.
PCT_RE = re.compile(r"(\d+(?:\.\d+)?)%")

def parse_ident(s: str) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """
    Returns (ident, side, name_or_nick).
    - ident: 'p1a'
    - side: 'p1' or 'p2'
    - name: remainder after 'p1a: '
    """
    if not s:
        return None, None, None
    m = IDENT_RE.match(s.strip())
    if not m:
        return None, None, s.strip()
    ident = m.group(1)  # p1a
    side = ident[:2]    # p1
    name = m.group(2).strip()
    return ident, side, name

def parse_hp_percent(token: str) -> Optional[float]:
    """
    Tries to infer percent from tokens like:
    - '75/100'
    - '150/300'
    - '50%'
    - '0 fnt'
    Returns percent 0..100, else None.
    """
    if not token:
        return None
    t = token.strip()

    # Direct percent e.g. "62%"
    m_pct = PCT_RE.search(t)
    if m_pct:
        try:
            return float(m_pct.group(1))
        except ValueError:
            pass

    # Ratio e.g. "75/100"
    m = HP_PCT_RE.match(t)
    if m:
        try:
            cur = float(m.group(1))
            mx = float(m.group(2))
            if mx > 0:
                return round((cur / mx) * 100.0, 2)
        except ValueError:
            pass

    # "0 fnt" sometimes appears; treat as 0
    if t.startswith("0") and "fnt" in t:
        return 0.0

    return None

def safe_json(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))

# --------------------------
# Event model
# --------------------------

@dataclass
class BattleEvent:
    BattleId: str
    LineNum: int
    TurnNumber: Optional[int]

    EventType: str
    SubType: Optional[str] = None

    SourceSide: Optional[str] = None
    SourceSlot: Optional[int] = None
    SourceBattlePokemonId: Optional[int] = None
    SourceIdent: Optional[str] = None

    TargetSide: Optional[str] = None
    TargetSlot: Optional[int] = None
    TargetBattlePokemonId: Optional[int] = None
    TargetIdent: Optional[str] = None

    MoveName: Optional[str] = None
    ItemName: Optional[str] = None
    AbilityName: Optional[str] = None
    SpeciesName: Optional[str] = None

    HpPctBefore: Optional[float] = None
    HpPctAfter: Optional[float] = None
    DamagePct: Optional[float] = None

    EventJson: Optional[str] = None
    EventSchemaVersion: str = "v1"

# --------------------------
# Line parsing
# --------------------------

def split_protocol(line_text: str) -> List[str]:
    # Protocol lines look like: |type|arg1|arg2|...
    # Leading '|' creates empty first element when splitting.
    parts = line_text.split("|")
    if parts and parts[0] == "":
        parts = parts[1:]
    return parts

def parse_line_to_event(battle_id: str, line_num: int, turn_num: Optional[int], line_text: str) -> Optional[BattleEvent]:
    parts = split_protocol(line_text)
    if not parts:
        return None

    ltype = parts[0].strip().lower()  # 'switch', 'move', '-damage', etc.

    # --- switch / drag ---
    if ltype in ("switch", "drag"):
        # |switch|p1a: Name|Species, M|100/100
        src_ident, src_side, src_name = parse_ident(parts[1] if len(parts) > 1 else "")
        species_part = parts[2] if len(parts) > 2 else None
        # species_part often "Kyogre, M" -> take species before comma
        species = None
        if species_part:
            species = species_part.split(",")[0].strip()

        hp_after = parse_hp_percent(parts[3] if len(parts) > 3 else "")

        payload = {"raw": line_text, "parts": parts}
        return BattleEvent(
            BattleId=battle_id,
            LineNum=line_num,
            TurnNumber=turn_num,
            EventType="switch",
            SubType=ltype,  # 'switch' or 'drag'
            SourceSide=src_side,
            SourceIdent=src_ident,
            SpeciesName=species,
            HpPctAfter=hp_after,
            EventJson=safe_json(payload),
        )

    # --- move ---
    if ltype == "move":
        # |move|p1a: Name|Move|p2a: Target
        src_ident, src_side, _ = parse_ident(parts[1] if len(parts) > 1 else "")
        move = parts[2].strip() if len(parts) > 2 else None
        tgt_ident, tgt_side, _ = parse_ident(parts[3] if len(parts) > 3 else "")

        payload = {"raw": line_text, "parts": parts}
        return BattleEvent(
            BattleId=battle_id,
            LineNum=line_num,
            TurnNumber=turn_num,
            EventType="move",
            SourceSide=src_side,
            SourceIdent=src_ident,
            TargetSide=tgt_side,
            TargetIdent=tgt_ident,
            MoveName=move,
            EventJson=safe_json(payload),
        )
        
    # --- terastallize ---
    if ltype == "-terastallize":
        # |-terastallize|p1a: Name|Water
        tgt_ident, tgt_side, _ = parse_ident(parts[1] if len(parts) > 1 else "")
        tera_type = parts[2].strip() if len(parts) > 2 else None

        payload = {"raw": line_text, "parts": parts}
        return BattleEvent(
            BattleId=battle_id,
            LineNum=line_num,
            TurnNumber=turn_num,
            EventType="terastallize",
            SubType=tera_type,          # store Tera type here
            TargetSide=tgt_side,
            TargetIdent=tgt_ident,
            EventJson=safe_json(payload),
        )


    # --- faint ---
    if ltype == "faint":
        # |faint|p2a: Name
        tgt_ident, tgt_side, _ = parse_ident(parts[1] if len(parts) > 1 else "")
        payload = {"raw": line_text, "parts": parts}
        return BattleEvent(
            BattleId=battle_id,
            LineNum=line_num,
            TurnNumber=turn_num,
            EventType="faint",
            TargetSide=tgt_side,
            TargetIdent=tgt_ident,
            HpPctAfter=0.0,
            EventJson=safe_json(payload),
        )

    # --- hp changes ---
    if ltype in ("-damage", "-heal"):
        # |-damage|p2a: Name|75/100|[from] ...
        tgt_ident, tgt_side, _ = parse_ident(parts[1] if len(parts) > 1 else "")
        hp_after = parse_hp_percent(parts[2] if len(parts) > 2 else "")
        payload = {"raw": line_text, "parts": parts}

        return BattleEvent(
            BattleId=battle_id,
            LineNum=line_num,
            TurnNumber=turn_num,
            EventType="hp_change",
            SubType="damage" if ltype == "-damage" else "heal",
            TargetSide=tgt_side,
            TargetIdent=tgt_ident,
            HpPctAfter=hp_after,
            EventJson=safe_json(payload),
        )

    # --- item reveal / set ---
    if ltype == "-item":
        # |-item|p1a: Name|Leftovers|[from] ability: Frisk
        tgt_ident, tgt_side, _ = parse_ident(parts[1] if len(parts) > 1 else "")
        item = parts[2].strip() if len(parts) > 2 else None
        payload = {"raw": line_text, "parts": parts}

        return BattleEvent(
            BattleId=battle_id,
            LineNum=line_num,
            TurnNumber=turn_num,
            EventType="item_reveal",
            TargetSide=tgt_side,
            TargetIdent=tgt_ident,
            ItemName=item,
            EventJson=safe_json(payload),
        )

    # --- item end/consume/remove ---
    if ltype == "-enditem":
        # |-enditem|p1a: Name|Sitrus Berry|[eat]
        tgt_ident, tgt_side, _ = parse_ident(parts[1] if len(parts) > 1 else "")
        item = parts[2].strip() if len(parts) > 2 else None
        reason = parts[3].strip() if len(parts) > 3 else None  # e.g. "[eat]" or "[from] ..."
        payload = {"raw": line_text, "parts": parts}

        # Default: treat as consume unless it clearly indicates removal by something else
        etype = "item_consume"
        sub = None
        if reason:
            if "knock off" in reason.lower() or "trick" in reason.lower() or "removed" in reason.lower():
                etype = "item_remove"
            sub = reason

        return BattleEvent(
            BattleId=battle_id,
            LineNum=line_num,
            TurnNumber=turn_num,
            EventType=etype,
            SubType=sub,
            TargetSide=tgt_side,
            TargetIdent=tgt_ident,
            ItemName=item,
            EventJson=safe_json(payload),
        )

    # --- activate (can be item or ability) ---
    if ltype == "-activate":
        # |-activate|p1a: Name|ability: Intimidate|...
        # |-activate|p1a: Name|item: Leftovers|...
        tgt_ident, tgt_side, _ = parse_ident(parts[1] if len(parts) > 1 else "")
        token = parts[2].strip() if len(parts) > 2 else ""
        payload = {"raw": line_text, "parts": parts}

        lower = token.lower()
        if lower.startswith("item:"):
            item = token.split(":", 1)[1].strip()
            return BattleEvent(
                BattleId=battle_id,
                LineNum=line_num,
                TurnNumber=turn_num,
                EventType="item_activate",
                TargetSide=tgt_side,
                TargetIdent=tgt_ident,
                ItemName=item,
                EventJson=safe_json(payload),
            )
        if lower.startswith("ability:"):
            ability = token.split(":", 1)[1].strip()
            return BattleEvent(
                BattleId=battle_id,
                LineNum=line_num,
                TurnNumber=turn_num,
                EventType="ability_activate",
                TargetSide=tgt_side,
                TargetIdent=tgt_ident,
                AbilityName=ability,
                EventJson=safe_json(payload),
            )

        # Unknown activate token -> keep as generic activate
        return BattleEvent(
            BattleId=battle_id,
            LineNum=line_num,
            TurnNumber=turn_num,
            EventType="activate",
            TargetSide=tgt_side,
            TargetIdent=tgt_ident,
            EventJson=safe_json(payload),
        )

    # --- ability reveal / explicit ---
    if ltype == "-ability":
        # |-ability|p2a: Name|Levitate|[from] ability: Trace
        tgt_ident, tgt_side, _ = parse_ident(parts[1] if len(parts) > 1 else "")
        ability = parts[2].strip() if len(parts) > 2 else None
        payload = {"raw": line_text, "parts": parts}
        return BattleEvent(
            BattleId=battle_id,
            LineNum=line_num,
            TurnNumber=turn_num,
            EventType="ability_reveal",
            TargetSide=tgt_side,
            TargetIdent=tgt_ident,
            AbilityName=ability,
            EventJson=safe_json(payload),
        )

    # Other event types can be added incrementally without schema changes.
    return None

# --------------------------
# DB functions
# --------------------------

def battle_events_exist(cur, battle_id: str) -> bool:
    cur.execute("SELECT TOP 1 1 FROM dbo.BattleEvents WHERE BattleId = ?;", battle_id)
    return cur.fetchone() is not None

def get_battles_to_parse(cur, limit: Optional[int] = None) -> List[str]:
    sql = "SELECT BattleId FROM dbo.Battles ORDER BY CreatedAt DESC"
    if limit is not None:
        sql = f"SELECT TOP ({int(limit)}) BattleId FROM dbo.Battles ORDER BY CreatedAt DESC"
    cur.execute(sql)
    return [r[0] for r in cur.fetchall()]

def load_log_lines(cur, battle_id: str) -> List[Tuple[int, Optional[int], str]]:
    cur.execute("""
        SELECT LineNum, TurnNumber, LineText
        FROM dbo.BattleLogLines
        WHERE BattleId = ?
        ORDER BY LineNum;
    """, battle_id)
    return [(int(r[0]), r[1], r[2]) for r in cur.fetchall()]

def insert_battle_events(cur, events: List[BattleEvent]) -> None:
    if not events:
        return

    rows = []
    for e in events:
        rows.append((
            e.BattleId, e.LineNum, e.TurnNumber,
            e.EventType, e.SubType,
            e.SourceSide, e.SourceSlot, e.SourceBattlePokemonId, e.SourceIdent,
            e.TargetSide, e.TargetSlot, e.TargetBattlePokemonId, e.TargetIdent,
            e.MoveName, e.ItemName, e.AbilityName, e.SpeciesName,
            e.HpPctBefore, e.HpPctAfter, e.DamagePct,
            e.EventJson, e.EventSchemaVersion
        ))

    cur.fast_executemany = True
    cur.executemany("""
        INSERT INTO dbo.BattleEvents
        (
            BattleId, LineNum, TurnNumber,
            EventType, SubType,
            SourceSide, SourceSlot, SourceBattlePokemonId, SourceIdent,
            TargetSide, TargetSlot, TargetBattlePokemonId, TargetIdent,
            MoveName, ItemName, AbilityName, SpeciesName,
            HpPctBefore, HpPctAfter, DamagePct,
            EventJson, EventSchemaVersion
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
    """, rows)

def parse_and_insert_for_battle(cur, battle_id: str, mode: str = "skip") -> int:
    """
    mode:
      - 'skip': if any events exist for the battle, do nothing
      - 'replace': delete existing events and rebuild
    Returns: number of inserted events
    """
    if mode == "skip" and battle_events_exist(cur, battle_id):
        return 0

    if mode == "replace":
        cur.execute("DELETE FROM dbo.BattleEvents WHERE BattleId = ?;", battle_id)

    lines = load_log_lines(cur, battle_id)
    events: List[BattleEvent] = []

    for line_num, turn_num, line_text in lines:
        ev = parse_line_to_event(battle_id, line_num, turn_num, line_text)
        if ev is not None:
            events.append(ev)

    insert_battle_events(cur, events)
    return len(events)

# --------------------------
# CLI entry
# --------------------------

def main(parse_mode: str = "skip", limit: Optional[int] = None) -> None:
    cn = pyodbc.connect(CONN_STR)
    cn.autocommit = False
    try:
        cur = cn.cursor()
        battle_ids = get_battles_to_parse(cur, limit=limit)

        for i, battle_id in enumerate(battle_ids, start=1):
            try:
                n = parse_and_insert_for_battle(cur, battle_id, mode=parse_mode)
                cn.commit()
                if n == 0 and parse_mode == "skip":
                    print(f"[{i}/{len(battle_ids)}] Skipped {battle_id} (already parsed)")
                else:
                    print(f"[{i}/{len(battle_ids)}] Parsed {battle_id}: inserted {n} events")
            except Exception as e:
                cn.rollback()
                print(f"[{i}/{len(battle_ids)}] FAILED {battle_id}: {e}")
    finally:
        cn.close()

if __name__ == "__main__":
    import sys
    mode = "skip"
    lim = None

    # Usage:
    #   python parse_battle_events.py
    #   python parse_battle_events.py replace
    #   python parse_battle_events.py skip 20
    if len(sys.argv) >= 2:
        mode = sys.argv[1].strip().lower()
    if len(sys.argv) >= 3:
        lim = int(sys.argv[2])

    if mode not in ("skip", "replace"):
        print("Mode must be 'skip' or 'replace'")
        raise SystemExit(2)

    main(parse_mode=mode, limit=lim)
