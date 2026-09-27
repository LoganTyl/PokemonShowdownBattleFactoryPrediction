#!/usr/bin/env python3
"""Populate dbo.DecisionPoints and dbo.ObservedActions from dbo.BattleEvents.

Gen 9 Battle Factory singles.

This script uses SourceIdent/TargetIdent (p1a/p2a) to identify the active slot.
It does not rely on SourceSlot/TargetSlot (which are NULL in your BattleEvents).

You already altered your DecisionPoints/ObservedActions tables to add explicit
columns (ActorBattlePokemonId, OpponentBattlePokemonId, RequestType, MoveName,
SwitchToBattlePokemonId, SwitchToSpecies, SourceLineNum, IsSuspicious,
SuspicionReason, ActorBattlePokemonId). This script detects which columns exist
and inserts accordingly.

Database settings are loaded from .env through app_config.py.
"""

from __future__ import annotations

import argparse
from datetime import datetime, UTC
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

import pyodbc

from app_config import CONN_STR


STATE_SCHEMA_VERSION = "dp_v1"
PARSER_VERSION = "populate_decisionpoints_actions_v1"
ACTIVE_IDENT_BY_SIDE = {"p1": "p1a", "p2": "p2a"}


def now_utc() -> datetime:
    return datetime.now(UTC)


def safe_lower(x: Optional[str]) -> Optional[str]:
    return x.lower() if isinstance(x, str) else None


def is_active_ident(ident: Optional[str]) -> bool:
    return bool(ident) and ident.strip().lower() in ("p1a", "p2a")

def load_turn_boundaries(cur, battle_id: str) -> List[Tuple[int, int]]:
    """
    Returns [(TurnNumber, StartLineNum), ...] from dbo.Turns.
    """
    cur.execute("""
        SELECT TurnNumber, StartLineNum
        FROM dbo.Turns
        WHERE BattleId = ?
        ORDER BY TurnNumber;
    """, battle_id)
    return [(int(r[0]), int(r[1])) for r in cur.fetchall()]


def compute_actives_before_line(events: List[EventRow], line_num: int) -> Dict[str, Optional[int]]:
    """
    Active mons immediately before the given line number.
    Uses switch events only.
    """
    active: Dict[str, Optional[int]] = {"p1": None, "p2": None}

    for ev in events:
        if ev.line_num >= line_num:
            break

        if ev.event_type == "switch":
            if ev.source_side in ("p1", "p2") and is_active_ident(ev.source_ident) and ev.source_bp_id is not None:
                active[ev.source_side] = ev.source_bp_id

    return active

@dataclass
class EventRow:
    battle_id: str
    line_num: int
    turn_num: int
    event_type: str
    source_side: Optional[str]
    source_ident: Optional[str]
    source_bp_id: Optional[int]
    target_side: Optional[str]
    target_ident: Optional[str]
    target_bp_id: Optional[int]
    move_name: Optional[str]
    species_name: Optional[str]
    is_suspicious: bool
    suspicion_reason: Optional[str]


def table_has_column(cur, table: str, col: str) -> bool:
    cur.execute(
        """
        SELECT 1
        FROM sys.columns
        WHERE object_id = OBJECT_ID(?)
          AND name = ?;
        """,
        table,
        col,
    )
    return cur.fetchone() is not None


def load_events(cur, battle_id: str) -> List[EventRow]:
    cur.execute(
        """
        SELECT
            BattleId, LineNum, TurnNumber,
            EventType,
            SourceSide, SourceIdent, SourceBattlePokemonId,
            TargetSide, TargetIdent, TargetBattlePokemonId,
            MoveName, SpeciesName,
            IsSuspicious, SuspicionReason
        FROM dbo.BattleEvents
        WHERE BattleId = ?
        ORDER BY LineNum;
        """,
        battle_id,
    )

    out: List[EventRow] = []
    for (
        bid, line_num, turn_num,
        et,
        ss, si, sbp,
        ts, ti, tbp,
        move, species,
        is_susp, susp_reason,
    ) in cur.fetchall():
        out.append(
            EventRow(
                battle_id=str(bid),
                line_num=int(line_num),
                turn_num=int(turn_num) if turn_num is not None else 0,
                event_type=str(et),
                source_side=safe_lower(ss),
                source_ident=safe_lower(si),
                source_bp_id=int(sbp) if sbp is not None else None,
                target_side=safe_lower(ts),
                target_ident=safe_lower(ti),
                target_bp_id=int(tbp) if tbp is not None else None,
                move_name=str(move) if move is not None else None,
                species_name=str(species) if species is not None else None,
                is_suspicious=bool(is_susp),
                suspicion_reason=str(susp_reason) if susp_reason is not None else None,
            )
        )
    return out


def compute_turn_start_actives(events: List[EventRow]) -> Dict[int, Dict[str, Optional[int]]]:
    """Return {turn: {'p1': bp_id, 'p2': bp_id}} for the start of each turn."""
    active: Dict[str, Optional[int]] = {"p1": None, "p2": None}
    snapshot: Dict[int, Dict[str, Optional[int]]] = {}

    for ev in events:
        t = ev.turn_num
        if t > 0 and t not in snapshot:
            snapshot[t] = {"p1": active["p1"], "p2": active["p2"]}

        if ev.event_type == "switch":
            if ev.source_side in ("p1", "p2") and is_active_ident(ev.source_ident) and ev.source_bp_id is not None:
                active[ev.source_side] = ev.source_bp_id

    return snapshot


def build_turn_start_decisionpoints(cur, battle_id: str, events: List[EventRow]) -> List[Dict]:
    """
    Build turn-start decision points from dbo.Turns, not just BattleEvents.
    This allows turn 1 DP creation at the |turn|1 checkpoint even before any turn-1 move events exist.
    """
    turn_bounds = load_turn_boundaries(cur, battle_id)
    if not turn_bounds:
        return []

    dps: List[Dict] = []

    for turn_num, start_line_num in turn_bounds:
        actives = compute_actives_before_line(events, start_line_num)

        for side in ("p1", "p2"):
            opp = "p2" if side == "p1" else "p1"
            dps.append(
                {
                    "BattleId": battle_id,
                    "TurnNumber": turn_num,
                    "ActorSide": side,
                    "Phase": "turn_start",
                    "LineNum": start_line_num,
                    "ActivePokemonSlot": 1,
                    "StateJson": "{}",
                    "StateSchemaVersion": STATE_SCHEMA_VERSION,
                    "LegalActionsJson": None,
                    "ParserVersion": PARSER_VERSION,
                    "CreatedAt": now_utc(),
                    "RequestType": "move",
                    "ActorBattlePokemonId": actives.get(side),
                    "OpponentBattlePokemonId": actives.get(opp),
                }
            )

    return dps


def build_forced_switch_decisionpoints(battle_id: str, events: List[EventRow]) -> List[Dict]:
    dps: List[Dict] = []

    active: Dict[str, Optional[int]] = {"p1": None, "p2": None}
    for ev in events:
        if ev.event_type == "switch":
            if ev.source_side in ("p1", "p2") and is_active_ident(ev.source_ident) and ev.source_bp_id is not None:
                active[ev.source_side] = ev.source_bp_id

        if ev.event_type == "faint":
            if ev.target_side in ("p1", "p2") and is_active_ident(ev.target_ident):
                fainted_side = ev.target_side
                opp = "p2" if fainted_side == "p1" else "p1"

                dps.append(
                    {
                        "BattleId": battle_id,
                        "TurnNumber": ev.turn_num or 0,
                        "ActorSide": fainted_side,
                        "Phase": "forced_switch",
                        "LineNum": ev.line_num,
                        "ActivePokemonSlot": 1,
                        "StateJson": "{}",
                        "StateSchemaVersion": STATE_SCHEMA_VERSION,
                        "LegalActionsJson": None,
                        "ParserVersion": PARSER_VERSION,
                        "CreatedAt": now_utc(),
                        "RequestType": "switch",
                        "ActorBattlePokemonId": None,
                        "OpponentBattlePokemonId": active.get(opp),
                    }
                )
                active[fainted_side] = None

    return dps


def first_action_after(events: List[EventRow], side: str, start_line_num: int) -> Optional[EventRow]:
    active_ident = ACTIVE_IDENT_BY_SIDE[side]

    for ev in events:
        if ev.line_num < start_line_num:
            continue

        if ev.event_type == "move" and ev.source_side == side and ev.source_ident == active_ident:
            return ev

        if ev.event_type == "switch" and ev.source_side == side and ev.source_ident == active_ident:
            return ev

    return None


def label_observed_action(dp: Dict, events: List[EventRow]) -> Optional[Dict]:
    side = str(dp["ActorSide"]).lower()
    start_line = int(dp["LineNum"])

    ev = first_action_after(events, side, start_line)
    if ev is None:
        return None

    row: Dict = {
        "DecisionPointId": dp["DecisionPointId"],
        "ActionType": "",
        "ActionValue": "",
        "Target": None,
        "WasForced": 1 if dp.get("Phase") == "forced_switch" else 0,
        "CreatedAt": now_utc(),
        "MoveName": None,
        "SwitchToBattlePokemonId": None,
        "SwitchToSpecies": None,
        "SourceLineNum": ev.line_num,
        "IsSuspicious": 1 if ev.is_suspicious else 0,
        "SuspicionReason": ev.suspicion_reason,
        "ActorBattlePokemonId": dp.get("ActorBattlePokemonId"),
    }

    if ev.event_type == "move" and ev.move_name:
        row["ActionType"] = "move"
        row["ActionValue"] = ev.move_name
        row["MoveName"] = ev.move_name
        row["Target"] = ev.target_ident
        return row

    if ev.event_type == "switch":
        row["ActionType"] = "switch"
        if ev.species_name:
            row["ActionValue"] = ev.species_name
            row["SwitchToSpecies"] = ev.species_name
        if ev.source_bp_id is not None:
            row["SwitchToBattlePokemonId"] = ev.source_bp_id
        return row

    return None


def delete_existing_for_battle(cur, battle_id: str) -> None:
    # 1) Predictions -> DecisionPoints
    cur.execute("""
        DELETE P
        FROM dbo.Predictions P
        JOIN dbo.DecisionPoints DP
            ON DP.DecisionPointId = P.DecisionPointId
        WHERE DP.BattleId = ?;
    """, battle_id)

    # 2) TrainingExamples -> DecisionPoints
    cur.execute("""
        DELETE TE
        FROM dbo.TrainingExamples TE
        JOIN dbo.DecisionPoints DP
            ON DP.DecisionPointId = TE.DecisionPointId
        WHERE DP.BattleId = ?;
    """, battle_id)

    # 3) ObservedActions -> DecisionPoints
    cur.execute("""
        DELETE OA
        FROM dbo.ObservedActions OA
        JOIN dbo.DecisionPoints DP
            ON DP.DecisionPointId = OA.DecisionPointId
        WHERE DP.BattleId = ?;
    """, battle_id)

    # 4) Now safe to delete DecisionPoints
    cur.execute("DELETE FROM dbo.DecisionPoints WHERE BattleId = ?;", battle_id)


def load_existing_decisionpoint_ids(cur, battle_id: str) -> Dict[Tuple[int, str, str, int], int]:
    cur.execute(
        """
        SELECT DecisionPointId, TurnNumber, ActorSide, Phase, LineNum
        FROM dbo.DecisionPoints
        WHERE BattleId = ?;
        """,
        battle_id,
    )
    mp: Dict[Tuple[int, str, str, int], int] = {}
    for dp_id, t, side, phase, line in cur.fetchall():
        mp[(int(t), str(side).lower(), str(phase), int(line))] = int(dp_id)
    return mp


def insert_rows(cur, table: str, rows: List[Dict], preferred_cols: List[str]) -> int:
    if not rows:
        return 0

    cols: List[str] = [c for c in preferred_cols if table_has_column(cur, table, c)]
    col_sql = ", ".join(cols)
    val_sql = ", ".join(["?"] * len(cols))
    sql = f"INSERT INTO {table} ({col_sql}) VALUES ({val_sql});"

    data = [tuple(r.get(c) for c in cols) for r in rows]
    cur.fast_executemany = False
    cur.executemany(sql, data)
    return len(data)


def battle_ids_to_process(cur, limit: Optional[int]) -> List[str]:
    sql = "SELECT BattleId FROM dbo.Battles WHERE BattleId LIKE 'gen9battlefactory-%' ORDER BY CreatedAt DESC"
    if limit is not None:
        sql = f"SELECT TOP ({int(limit)}) BattleId FROM dbo.Battles WHERE BattleId LIKE 'gen9battlefactory-%' ORDER BY CreatedAt DESC"
    cur.execute(sql)
    return [str(r[0]) for r in cur.fetchall()]


def process_battle(cur, battle_id: str, mode: str) -> Tuple[int, int]:
    if mode == "replace":
        delete_existing_for_battle(cur, battle_id)

    if mode == "skip":
        cur.execute("SELECT TOP 1 1 FROM dbo.DecisionPoints WHERE BattleId = ?;", battle_id)
        if cur.fetchone() is not None:
            return (0, 0)

    events = load_events(cur, battle_id)
    if not events:
        return (0, 0)

    dps: List[Dict] = []
    dps.extend(build_turn_start_decisionpoints(cur, battle_id, events))
    dps.extend(build_forced_switch_decisionpoints(battle_id, events))

    dp_cols = [
        "BattleId", "TurnNumber", "ActorSide", "Phase", "LineNum",
        "ActivePokemonSlot", "StateJson", "StateSchemaVersion",
        "LegalActionsJson", "ParserVersion", "CreatedAt",
        "RequestType", "ActorBattlePokemonId", "OpponentBattlePokemonId",
    ]
    inserted_dp = insert_rows(cur, "dbo.DecisionPoints", dps, dp_cols)

    dp_ids = load_existing_decisionpoint_ids(cur, battle_id)
    for dp in dps:
        key = (int(dp["TurnNumber"]), str(dp["ActorSide"]).lower(), str(dp["Phase"]), int(dp["LineNum"]))
        dp["DecisionPointId"] = dp_ids.get(key)

    oa_rows: List[Dict] = []
    for dp in dps:
        if not dp.get("DecisionPointId"):
            continue
        oa = label_observed_action(dp, events)
        if oa is not None:
            oa_rows.append(oa)

    oa_cols = [
        "DecisionPointId", "ActionType", "ActionValue", "Target", "WasForced", "CreatedAt",
        "MoveName", "SwitchToBattlePokemonId", "SwitchToSpecies",
        "SourceLineNum", "IsSuspicious", "SuspicionReason", "ActorBattlePokemonId",
    ]
    inserted_oa = insert_rows(cur, "dbo.ObservedActions", oa_rows, oa_cols)

    return (inserted_dp, inserted_oa)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["skip", "replace"], help="skip keeps existing rows; replace rebuilds per battle")
    ap.add_argument("limit", nargs="?", type=int, default=None, help="optional number of most recent battles")
    args = ap.parse_args()

    conn = pyodbc.connect(CONN_STR)
    conn.autocommit = False
    cur = conn.cursor()

    battle_ids = battle_ids_to_process(cur, args.limit)
    total_dp = 0
    total_oa = 0

    try:
        for idx, bid in enumerate(battle_ids, start=1):
            dp_count, oa_count = process_battle(cur, bid, args.mode)
            total_dp += dp_count
            total_oa += oa_count
            conn.commit()
            print(f"[{idx}/{len(battle_ids)}] {bid}: DecisionPoints +{dp_count}, ObservedActions +{oa_count}")
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()
        conn.close()

    print(f"Done. Inserted DecisionPoints={total_dp}, ObservedActions={total_oa}")


if __name__ == "__main__":
    main()
