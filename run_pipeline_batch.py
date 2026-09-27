"""
Run the full replay -> DB -> features -> training -> model pipeline in the correct order.

This is a thin orchestrator that shells out to your existing scripts, plus optionally runs your
SQL Server stored procedures via pyodbc.

Default behavior is as follows:
- import_replays: uses --replays-file
- parse_battle_events / populate_battle_entities_v2 / init_set_candidates / populate_decisionpoints_actions:
    replace or skip
- populate_decisionpoint_state_v3_dex_candidates / populate_training_examples:
    <skip|replace> <N|all>  (defaults to replace all)
- train_models_tree: no args

Usage examples:
  python run_pipeline_batch.py --replays-file replays.txt --mode replace --limit all --train
  python run_pipeline_batch.py --mode skip --limit 200 --no-train
  python run_pipeline_batch.py --run-sql --conn-str "DRIVER=...;SERVER=...;DATABASE=...;Trusted_Connection=yes;" --no-train

Notes:
- This script does not require modifying DB schema.
- If resumability by stage is desired later on, another table can be made (PipelineRuns, for example) to mark stage completion.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from typing import List

from app_config import CONN_STR

# -------------------------
# Project script filenames
# -------------------------
IMPORT_REPLAYS = "import_replays.py"
PARSE_EVENTS = "parse_battle_events.py"
POP_ENTITIES = "populate_battle_entities_v2.py"
INIT_CANDS = "init_set_candidates.py"
POP_DP_ACTIONS = "populate_decisionpoints_actions.py"
POP_DP_STATE = "populate_decisionpoint_state_v3_dex_candidates.py"
POP_TRAIN_EX = "populate_training_examples.py"
TRAIN_MODELS = "train_models_tree.py"

# SQL procedures (no args, as you described)
PROC_ELIM_ALL = "dbo.ApplySetCandidateEliminations_All"
PROC_SUSP_ALL = "dbo.MarkSuspicious_All"


def run_cmd(cmd: List[str], cwd: str, step_name: str) -> None:
    """Run a command and stream output; raise on failure."""
    print(f"\n=== {step_name} ===")
    print(" ".join(cmd))
    t0 = time.time()
    proc = subprocess.run(cmd, cwd=cwd)
    dt = time.time() - t0
    if proc.returncode != 0:
        raise RuntimeError(f"Step failed ({step_name}) with exit code {proc.returncode}")
    print(f"--- {step_name} completed in {dt:0.1f}s ---")


def run_sql_procs(conn_str: str) -> None:
    """Run the two _All stored procedures via pyodbc."""
    try:
        import pyodbc  # type: ignore
    except Exception as e:
        raise RuntimeError("pyodbc is required for --run-sql. Install it or run the procs in SSMS.") from e

    print("\n=== SQL Procedures ===")
    print(f"Executing: {PROC_ELIM_ALL}")
    print(f"Executing: {PROC_SUSP_ALL}")

    conn = pyodbc.connect(conn_str, timeout=120)
    try:
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute(f"EXEC {PROC_ELIM_ALL};")
        cur.execute(f"EXEC {PROC_SUSP_ALL};")
    finally:
        conn.close()

    print("--- SQL Procedures completed ---")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--replays-file", default="replays.txt",
                    help="Path to the text file containing replay URLs (one per line).")
    ap.add_argument("--mode", choices=["replace", "skip"], default="replace",
                    help="Mode for scripts that support skip/replace.")
    ap.add_argument("--limit", default="all",
                    help="For scripts that support <N|all>. Default: all.")
    ap.add_argument("--no-train", action="store_true",
                    help="Skip training step.")
    ap.add_argument("--run-sql", action="store_true",
                    help="Run stored procedures via pyodbc after candidates are initialized.")
    ap.add_argument("--conn-str", default=CONN_STR,
                    help="ODBC connection string for --run-sql.")
    ap.add_argument("--project-dir", default=".",
                    help="Directory containing the scripts (default: current directory).")

    args = ap.parse_args()
    project_dir = os.path.abspath(args.project_dir)

    py = sys.executable

    required = [IMPORT_REPLAYS, PARSE_EVENTS, POP_ENTITIES, INIT_CANDS, POP_DP_ACTIONS, POP_DP_STATE, POP_TRAIN_EX]
    if not args.no_train:
        required.append(TRAIN_MODELS)

    missing = [f for f in required if not os.path.exists(os.path.join(project_dir, f))]
    if missing:
        print("Missing required files in project-dir:")
        for f in missing:
            print("  -", f)
        return 2

    # 1) Import replays
    run_cmd([py, IMPORT_REPLAYS, args.replays_file], cwd=project_dir, step_name="Import replays")

    # 2) Parse events
    run_cmd([py, PARSE_EVENTS, args.mode], cwd=project_dir, step_name="Parse battle events")

    # 3) Populate entities
    run_cmd([py, POP_ENTITIES, args.mode], cwd=project_dir, step_name="Populate battle entities")

    # 4) Initialize set candidates
    run_cmd([py, INIT_CANDS, args.mode], cwd=project_dir, step_name="Initialize set candidates")

    # 5) SQL procedures (optional)
    if args.run_sql:
        if not args.conn_str.strip():
            print("\nERROR: --run-sql was provided but --conn-str is empty.")
            print("Provide your full ODBC connection string via --conn-str.")
            return 2
        run_sql_procs(args.conn_str)

    # 6) Populate decisionpoints/actions
    run_cmd([py, POP_DP_ACTIONS, args.mode], cwd=project_dir, step_name="Populate DecisionPoints + ObservedActions")

    # 7) Populate decisionpoint state
    run_cmd([py, POP_DP_STATE, args.mode, str(args.limit)], cwd=project_dir, step_name="Populate DecisionPoint StateJson")

    # 8) Populate training examples
    run_cmd([py, POP_TRAIN_EX, args.mode, str(args.limit)], cwd=project_dir, step_name="Populate TrainingExamples")

    # 9) Train models (optional)
    if not args.no_train:
        run_cmd([py, TRAIN_MODELS], cwd=project_dir, step_name="Train models (tree)")

    print("\nPipeline finished successfully.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
