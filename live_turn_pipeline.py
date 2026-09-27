"""
Run the "replay pipeline" steps for ONE battle_id, suitable for calling mid-match at turn boundaries.

This module is intentionally thin and reuses existing code from:
- populate_battle_entities_v2.populate_entities_for_battle
- init_set_candidates.init_candidates_for_battle
- SQL procedures for eliminations + suspicious marking

Why a separate module?
- Keeps live_ingest.py focused on writing BattleLogLines and rebuilding BattleEvents.
- Avoids duplicating the replay pipeline logic for live battles.

Recommended usage (from live_ingest.py turn checkpoint):
    from live_turn_pipeline import run_turn_checkpoint
    run_turn_checkpoint(battle_id, mode="skip", run_sql=True)

SQL Caution:
    If dbo.ApplySetCandidateEliminations_All and dbo.MarkSuspicious_All are run in SSMS, it will affect
    all battles, which could possibly be overkill if run every turn during live play.

This module will TRY to run per-battle procedures first (if they exist), and fall back
to the *_All procedures if not found.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import pyodbc

from app_config import CONN_STR
import populate_battle_entities_v2
import init_set_candidates
import populate_decisionpoints_actions
import populate_decisionpoint_state_v3_dex_candidates as dp_state

DB_COMMAND_TIMEOUT_S = 180


# --------------------------
# Stored procedure names
# --------------------------
# Preferred per-battle procedures (if you create them later).
PER_BATTLE_ELIM_PROC = "dbo.ApplySetCandidateEliminations"
PER_BATTLE_SUSP_PROC = "dbo.MarkSuspicious_ForBattle"

# Current all-battles procedures you said you have now.
ALL_ELIM_PROC = "dbo.ApplySetCandidateEliminations_All"
ALL_SUSP_PROC = "dbo.MarkSuspicious_All"


@dataclass
class TurnCheckpointResult:
    battle_id: str
    entities_ran: bool
    candidates_ran: bool
    sql_elim_ran: str  # proc name or ""
    sql_susp_ran: str  # proc name or ""


def _try_exec_proc(cur: pyodbc.Cursor, proc_name: str, battle_id: Optional[str]) -> bool:
    """
    Try to execute a stored procedure.
    Returns True if it executed successfully, False if the proc does not exist.
    Raises for other SQL errors (so you notice genuine problems).
    """
    try:
        if battle_id is None:
            cur.execute(f"EXEC {proc_name};")
        else:
            cur.execute(f"EXEC {proc_name} @BattleId = ?;", battle_id)
        return True
    except pyodbc.ProgrammingError as e:
        # If it's "could not find stored procedure", treat as non-fatal fallback.
        msg = str(e).lower()
        if "could not find stored procedure" in msg or "is not a recognized built-in function name" in msg:
            return False
        raise


def run_turn_checkpoint(
    battle_id: str,
    mode: str = "skip",
    conn_str: str = CONN_STR,
    run_sql: bool = True,
) -> TurnCheckpointResult:
    """
    Run per-battle pipeline steps on an existing DB connection.

    mode:
        - "skip": don't rebuild if already populated (fast for live)
        - "replace": overwrite for this battle (useful when debugging)

    run_sql:
        - True: run eliminations + suspicious marking (preferred for UI accuracy)
        - False: skip SQL procs (faster; useful if you do them manually in SSMS)

    Returns a small summary to log.
    """
    if not battle_id:
        raise ValueError("battle_id is required")

    if mode not in ("skip", "replace"):
        raise ValueError("mode must be 'skip' or 'replace'")

    conn = pyodbc.connect(conn_str, timeout=120)
    try:
        conn.autocommit = False
        cur = conn.cursor()
        try:
            cur.timeout = DB_COMMAND_TIMEOUT_S
        except Exception:
            pass

        # Update TierName from the "Battle Factory Tier: <tier>" broadcast line (RU/PU/Uber/etc.)
        bf_tier = init_set_candidates.extract_bf_tier_name(cur, battle_id)
        if bf_tier:
            # Fill TierName from TierId using BF_Tier (authoritative)
            cur.execute("""
                UPDATE b
                SET b.TierName = t.TierName
                FROM dbo.Battles b
                JOIN dbo.BF_Tier t ON t.TierId = b.TierId
                WHERE b.BattleId = ?
                AND b.TierName IS NULL;
            """, battle_id)

        # 1) Entities / reveals
        populate_battle_entities_v2.populate_entities_for_battle(cur, battle_id, mode=mode)
        conn.commit()

        # 2) Init set candidates
        init_set_candidates.init_candidates_for_battle(cur, battle_id, mode=mode)
        conn.commit()

        elim_ran = ""
        susp_ran = ""

        # 3) SQL procs (optional)
        if run_sql:
            # Prefer per-battle procs if present; else fall back to *_All.
            if _try_exec_proc(cur, PER_BATTLE_ELIM_PROC, battle_id=battle_id):
                elim_ran = PER_BATTLE_ELIM_PROC
            else:
                _try_exec_proc(cur, ALL_ELIM_PROC, battle_id=None)
                elim_ran = ALL_ELIM_PROC

            if _try_exec_proc(cur, PER_BATTLE_SUSP_PROC, battle_id=battle_id):
                susp_ran = PER_BATTLE_SUSP_PROC
            else:
                _try_exec_proc(cur, ALL_SUSP_PROC, battle_id=None)
                susp_ran = ALL_SUSP_PROC

            conn.commit()
            
        # 4) Turns (used by some UI/debug queries)
        rebuild_turns_for_battle(cur, battle_id)
        conn.commit()

        # 5) DecisionPoints + ObservedActions
        # For live, we want this rebuilt every checkpoint so new turns/actions appear.
        populate_decisionpoints_actions.process_battle(cur, battle_id, mode="replace")
        conn.commit()

        # 6) StateJson for DecisionPoints (v3 dex + candidates)
        populate_state_for_battle(cur, battle_id, mode="skip")
        conn.commit()
        
        return TurnCheckpointResult(
            battle_id=battle_id,
            entities_ran=True,
            candidates_ran=True,
            sql_elim_ran=elim_ran,
            sql_susp_ran=susp_ran,
        )
    finally:
        try:
            conn.close()
        except Exception:
            pass

def rebuild_turns_for_battle(cur, battle_id: str) -> int:
    """
    Populate dbo.Turns from dbo.BattleLogLines.
    Turns table expected columns: (BattleId, TurnNumber, StartLineNum, EndLineNum)
    """
    cur.execute(
        """
        SELECT LineNum, TurnNumber, LineType
        FROM dbo.BattleLogLines
        WHERE BattleId = ?
        ORDER BY LineNum;
        """,
        battle_id,
    )
    rows = cur.fetchall()
    if not rows:
        return 0

    # Identify turn start lines
    turn_start_map = {}
    max_ln = 0

    for ln, tn, lt in rows:
        ln = int(ln)
        max_ln = max(max_ln, ln)

        if lt == "turn" and tn is not None:
            tn = int(tn)
            # Keep the earliest line number for each turn number
            if tn not in turn_start_map:
                turn_start_map[tn] = ln
            else:
                turn_start_map[tn] = min(turn_start_map[tn], ln)

    if not turn_start_map:
        return 0

    # Sort by turn number, not raw line duplicates
    starts = sorted(turn_start_map.items(), key=lambda x: x[0])  # [(turn_num, start_line)]

    bounds = []
    for i, (tn, start_ln) in enumerate(starts):
        end_ln = (starts[i + 1][1] - 1) if i + 1 < len(starts) else max_ln
        bounds.append((tn, start_ln, end_ln))

    cur.execute("DELETE FROM dbo.Turns WHERE BattleId = ?;", battle_id)

    cur.fast_executemany = True
    cur.executemany(
        """
        INSERT INTO dbo.Turns (BattleId, TurnNumber, StartLineNum, EndLineNum)
        VALUES (?, ?, ?, ?);
        """,
        [(battle_id, tn, s, e) for (tn, s, e) in bounds],
    )

    return len(bounds)

def populate_state_for_battle(cur, battle_id: str, mode: str = "skip") -> int:
    type_map = dp_state.load_type_effectiveness_map(cur)

    dps, existing_state = dp_state.load_decision_points(cur, battle_id)
    if not dps:
        return 0

    bp_lookup = dp_state.load_battlepokemon_lookup(cur, battle_id)
    events = dp_state.load_events(cur, battle_id)
    reveals = dp_state.load_reveals(cur, battle_id)

    dex_pokemon = dp_state.load_dex_pokemon_map(cur, battle_id)
    dex_moves = dp_state.load_dex_move_map(cur, battle_id)

    from collections import defaultdict
    dp_by_linenum = defaultdict(list)
    for dp in dps:
        dp_by_linenum[dp.LineNum].append(dp)

    event_by_linenum = defaultdict(list)
    for e in events:
        event_by_linenum[e.LineNum].append(e)

    all_linenums = sorted(set([e.LineNum for e in events] + list(dp_by_linenum.keys())))

    state = dp_state.BattleState()
    reveal_idx = 0
    updates = []

    for ln in all_linenums:
        while reveal_idx < len(reveals) and reveals[reveal_idx].LineNum <= ln:
            state.apply_reveal(reveals[reveal_idx])
            reveal_idx += 1

        for e in event_by_linenum.get(ln, []):
            state.apply_event(e)

        for dp in dp_by_linenum.get(ln, []):
            existing = existing_state.get(dp.DecisionPointId)

            if mode == "skip" and existing is not None and not dp_state.is_blank_state(existing):
                continue

            snap = dp_state.snapshot_for_decision_point(cur, dp, state, bp_lookup, dex_pokemon, dex_moves, type_map)
            state_json = dp_state.safe_json(snap)

            tup = [state_json, dp_state.STATE_SCHEMA_VERSION]

            if (
                dp_state.POPULATE_LEGAL_ACTIONS
                and dp_state.table_has_column(cur, "DecisionPoints", "LegalActionsJson")
            ):
                la = dp_state.build_legal_actions_json(cur, dp, state)
                tup.append(dp_state.safe_json(la) if la is not None else None)

            if dp_state.table_has_column(cur, "DecisionPoints", "ParserVersion"):
                tup.append(dp_state.PARSER_VERSION)

            tup.append(dp.DecisionPointId)
            updates.append(tuple(tup))

    if not updates:
        return 0

    return dp_state.update_decision_points(cur, updates)
