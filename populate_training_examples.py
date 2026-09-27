"""
Populate dbo.TrainingExamples from DecisionPoints + ObservedActions.

Generates three supervised datasets (two-stage setup):
  1) LabelType='action_type'  -> LabelValue in {'move','switch'}
  2) LabelType='move_name'    -> LabelValue = MoveName (only when action_type=move)
  3) LabelType='switch_to'    -> LabelValue = SwitchToSpecies (only when action_type=switch)

FeaturesJson:
  - By default, stores the full DecisionPoints.StateJson (already compact + dex-enriched).
  - Optionally, can add a small wrapper with meta fields.

Safeguards:
  - Skips DecisionPoints with no ObservedActions.
  - Skips suspicious actions if ObservedActions.IsSuspicious exists and is 1.
  - Supports skip/replace modes.
  - Avoids duplicate inserts by checking existing (DecisionPointId, LabelType) pairs.

Usage:
  python populate_training_examples.py skip all
  python populate_training_examples.py replace 100
"""

from __future__ import annotations

import json
import sys
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

import pyodbc

from app_config import CONN_STR

# Connection/login timeout (opening the connection)
CONNECT_TIMEOUT_S = 120

# Command execution timeout (running queries)
QUERY_TIMEOUT_S = 300

# If True, skip ObservedActions marked suspicious (recommended)
SKIP_SUSPICIOUS = True

# If True, wrap features as {"state": <StateJson>, "dp": {...}}; else FeaturesJson = StateJson as-is
WRAP_FEATURES = False

# LabelType constants (keep short to avoid varchar truncation)
LABEL_ACTION_TYPE = "action_type"
LABEL_MOVE_NAME = "move_name"
LABEL_SWITCH_TO = "switch_to"


def connect_with_retry(conn_str: str, timeout: int = CONNECT_TIMEOUT_S, retries: int = 5, sleep_s: float = 2.0) -> pyodbc.Connection:
    last_err = None
    for attempt in range(1, retries + 1):
        try:
            return pyodbc.connect(conn_str, timeout=timeout)
        except pyodbc.Error as e:
            last_err = e
            if attempt < retries:
                time.sleep(sleep_s * attempt)
            else:
                raise
    raise last_err  # pragma: no cover


def table_has_column(cur: pyodbc.Cursor, table: str, col: str) -> bool:
    cur.execute(
        """
        SELECT 1
        FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_SCHEMA='dbo' AND TABLE_NAME=? AND COLUMN_NAME=?
        """,
        table,
        col,
    )
    return cur.fetchone() is not None


def fetch_decisionpoints(cur: pyodbc.Cursor, limit: Optional[int] = None) -> List[Dict[str, Any]]:
    """Pull DecisionPoints with their StateJson + schema/version fields."""
    top = f"TOP ({int(limit)})" if limit else ""
    cur.execute(
        f"""
        SELECT {top}
          DecisionPointId,
          BattleId,
          TurnNumber,
          ActorSide,
          Phase,
          LineNum,
          StateJson,
          StateSchemaVersion,
          ParserVersion,
          RequestType
        FROM dbo.DecisionPoints
        ORDER BY DecisionPointId
        """
    )
    rows = []
    cols = [d[0] for d in cur.description]
    for r in cur.fetchall():
        rows.append({cols[i]: r[i] for i in range(len(cols))})
    return rows


def fetch_observed_actions_map(cur: pyodbc.Cursor) -> Dict[int, Dict[str, Any]]:
    """Map DecisionPointId -> ObservedActions row."""
    has_move = table_has_column(cur, "ObservedActions", "MoveName")
    has_sw_to_species = table_has_column(cur, "ObservedActions", "SwitchToSpecies")
    has_susp = table_has_column(cur, "ObservedActions", "IsSuspicious")
    has_susp_reason = table_has_column(cur, "ObservedActions", "SuspicionReason")

    select_cols = [
        "DecisionPointId",
        "ActionType",
        "ActionValue",
        "WasForced",
    ]
    if has_move:
        select_cols.append("MoveName")
    if has_sw_to_species:
        select_cols.append("SwitchToSpecies")
    if has_susp:
        select_cols.append("IsSuspicious")
    if has_susp_reason:
        select_cols.append("SuspicionReason")

    cur.execute(f"SELECT {', '.join(select_cols)} FROM dbo.ObservedActions")
    cols = [d[0] for d in cur.description]
    mp: Dict[int, Dict[str, Any]] = {}
    for r in cur.fetchall():
        row = {cols[i]: r[i] for i in range(len(cols))}
        dp_id = int(row["DecisionPointId"])
        mp[dp_id] = row
    return mp


def existing_example_keys(cur: pyodbc.Cursor) -> set[Tuple[int, str]]:
    """Return set of (DecisionPointId, LabelType) already in TrainingExamples."""
    cur.execute("SELECT DecisionPointId, LabelType FROM dbo.TrainingExamples")
    out = set()
    for dp_id, lt in cur.fetchall():
        out.add((int(dp_id), str(lt)))
    return out


def delete_examples_for_decisionpoints(cur: pyodbc.Cursor, dp_ids: Sequence[int]) -> None:
    """Delete TrainingExamples for provided DecisionPointIds."""
    if not dp_ids:
        return
    CHUNK = 1000
    for i in range(0, len(dp_ids), CHUNK):
        chunk = dp_ids[i : i + CHUNK]
        placeholders = ",".join("?" for _ in chunk)
        cur.execute(f"DELETE FROM dbo.TrainingExamples WHERE DecisionPointId IN ({placeholders})", chunk)


def insert_training_examples(cur: pyodbc.Cursor, rows: List[Dict[str, Any]]) -> int:
    if not rows:
        return 0

    cols = [
        "DecisionPointId",
        "FeatureSchemaVersion",
        "FeaturesJson",
        "LabelType",
        "LabelValue",
        "Weight",
    ]
    cols = [c for c in cols if table_has_column(cur, "TrainingExamples", c)]
    sql = f"INSERT INTO dbo.TrainingExamples ({', '.join(cols)}) VALUES ({', '.join(['?']*len(cols))});"
    data = [tuple(r.get(c) for c in cols) for r in rows]

    # Disable fast_executemany to avoid buffer sizing problems with JSON strings
    cur.fast_executemany = False
    cur.executemany(sql, data)
    return len(rows)


def make_feature_json(dp: Dict[str, Any]) -> str:
    """Use existing StateJson as the core feature payload."""
    state_json = dp.get("StateJson")
    if state_json is None:
        return json.dumps({"state": None}, ensure_ascii=False)

    try:
        state_obj = json.loads(state_json)
    except Exception:
        state_obj = {"raw_state": state_json}

    if not WRAP_FEATURES:
        return json.dumps(state_obj, ensure_ascii=False)

    wrapper = {
        "state": state_obj,
        "dp": {
            "battleId": dp.get("BattleId"),
            "turnNumber": dp.get("TurnNumber"),
            "actorSide": dp.get("ActorSide"),
            "phase": dp.get("Phase"),
            "lineNum": dp.get("LineNum"),
            "requestType": dp.get("RequestType"),
        },
    }
    return json.dumps(wrapper, ensure_ascii=False)


def main(argv: List[str]) -> int:
    if len(argv) < 3:
        print("Usage: python populate_training_examples.py skip|replace <N|all>")
        return 2

    mode = argv[1].lower()
    n_str = argv[2].lower()
    if mode not in ("skip", "replace"):
        print("Mode must be skip or replace")
        return 2

    limit = None if n_str == "all" else int(n_str)

    with connect_with_retry(CONN_STR) as conn:
        conn.autocommit = False
        cur = conn.cursor()

        dps = fetch_decisionpoints(cur, limit=limit)
        if not dps:
            print("No DecisionPoints found.")
            return 0

        oa_map = fetch_observed_actions_map(cur)
        existing = existing_example_keys(cur) if mode == "skip" else set()

        dp_ids = [int(dp["DecisionPointId"]) for dp in dps]
        if mode == "replace":
            delete_examples_for_decisionpoints(cur, dp_ids)
            existing = set()

        to_insert: List[Dict[str, Any]] = []
        skipped_no_action = 0
        skipped_suspicious = 0
        skipped_duplicates = 0

        for dp in dps:
            dp_id = int(dp["DecisionPointId"])
            oa = oa_map.get(dp_id)
            if not oa:
                skipped_no_action += 1
                continue

            weight = 1.0
            if oa.get("WasForced") in (1, True):
                weight = min(weight, 0.25)
            if oa.get("IsSuspicious") in (1, True):
                weight = min(weight, 0.10)
            
            if SKIP_SUSPICIOUS and oa.get("IsSuspicious") in (1, True):
                skipped_suspicious += 1
                continue

            feat_schema = dp.get("StateSchemaVersion") or "state_unknown"
            features_json = make_feature_json(dp)

            action_type = (oa.get("ActionType") or "").strip().lower()
            if action_type not in ("move", "switch"):
                skipped_no_action += 1
                continue

            # 1) action_type
            k1 = (dp_id, LABEL_ACTION_TYPE)
            if k1 in existing:
                skipped_duplicates += 1
            else:
                to_insert.append({
                    "DecisionPointId": dp_id,
                    "FeatureSchemaVersion": str(feat_schema),
                    "FeaturesJson": features_json,
                    "LabelType": LABEL_ACTION_TYPE,
                    "LabelValue": action_type,
                    "Weight": weight,
                })
                existing.add(k1)

            # 2) conditional label
            if action_type == "move":
                move = (oa.get("MoveName") or oa.get("ActionValue") or "").strip()
                if move:
                    k2 = (dp_id, LABEL_MOVE_NAME)
                    if k2 in existing:
                        skipped_duplicates += 1
                    else:
                        to_insert.append({
                            "DecisionPointId": dp_id,
                            "FeatureSchemaVersion": str(feat_schema),
                            "FeaturesJson": features_json,
                            "LabelType": LABEL_MOVE_NAME,
                            "LabelValue": move,
                            "Weight": weight,
                        })
                        existing.add(k2)
            else:
                sw = (oa.get("SwitchToSpecies") or oa.get("Target") or oa.get("ActionValue") or "").strip()
                if sw:
                    k3 = (dp_id, LABEL_SWITCH_TO)
                    if k3 in existing:
                        skipped_duplicates += 1
                    else:
                        to_insert.append({
                            "DecisionPointId": dp_id,
                            "FeatureSchemaVersion": str(feat_schema),
                            "FeaturesJson": features_json,
                            "LabelType": LABEL_SWITCH_TO,
                            "LabelValue": sw,
                            "Weight": weight,
                        })
                        existing.add(k3)

        inserted = insert_training_examples(cur, to_insert)
        conn.commit()

        print(f"DecisionPoints scanned: {len(dps)}")
        print(f"Examples inserted: {inserted}")
        print(f"Skipped (no observed action): {skipped_no_action}")
        print(f"Skipped (suspicious): {skipped_suspicious}")
        print(f"Skipped (duplicates): {skipped_duplicates}")
        return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
