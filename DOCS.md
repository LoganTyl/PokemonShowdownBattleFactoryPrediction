# Project Documentation

This document summarizes the repository: architecture, key modules, run instructions, database notes, and developer guidance.

Refer also to `README.md` for quick start and `requirements.txt` for pinned dependencies.

---

## Overview

This project implements a live ingestion and prediction pipeline for Pokémon Showdown Battle Factory matches. It provides:

- A live websocket ingest that writes raw battle lines to the DB and runs a replay-style pipeline at turn boundaries (`live_ingest_v2.py`).
- A small Tkinter UI for viewing live matches, predictions, and candidate sets (`ps_ui_test_live_ui_v4.py`).
- Utilities to populate database tables (entities, decision points, candidates), run per-battle pipeline steps, and generate model predictions.
- Model training and artifacts under `train_models_tree.py` and `model_artifacts/`.

Primary goal: allow live monitoring of matches with automated set-candidate generation and model predictions for likely next actions.

## Key files and roles

- `ps_ui_test_live_ui_v4.py` — Main UI. Features:
  - Connect to a Showdown battle websocket and ingest lines.
  - Teams view (roster), Set Candidates view, Predictions view, and Battle Log.
  - Autofollow behavior: when a predict player is configured, UI snaps to the opponent's active Pokémon at battle start and on turn changes; manual row clicks disable autofollow until selection becomes invalid or a new turn begins.
  - Programmatic selection events are suppressed so they are not treated as user clicks.
  - Candidate columns use fixed widths to preserve horizontal scrolling when columns are reordered.

- `live_ingest_v2.py` — Live ingest implementation. Writes lines into `dbo.BattleLogLines`, rebuilds `dbo.BattleEvents` on checkpoints (turns), runs the per-battle pipeline (via `live_turn_pipeline.run_turn_checkpoint`) and optionally triggers prediction insertion.

- `live_turn_pipeline.py` — Thin orchestration for running pipeline steps per-battle.
  - Calls: entities population (`populate_battle_entities_v2`), set candidate initialization (`init_set_candidates`), elimination + suspicious stored procedures, decisionpoint action population, and state generation.
  - Uses the connection string loaded by `app_config.py` from the local `.env` file.

- `predict_and_insert_predictions_v2.py` — Generates model predictions and inserts them into `dbo.Predictions` keyed to DecisionPoints.

- `init_set_candidates.py`, `populate_battle_entities_v2.py`, `populate_decisionpoints_actions.py`, `populate_decisionpoint_state_v3_dex_candidates.py` — DB population helpers used by the pipeline.

- `parse_battle_events.py` — Parses raw BattleLogLines into structured `BattleEvents` used by the pipeline.

- `train_models_tree.py` — Scripts to train models used by the prediction step and produce artifacts stored in `model_artifacts/`.

- `model_artifacts/` — Directory for trained models and run manifests.

## Database expectations

The code expects a SQL Server database with the following (non-exhaustive) tables and stored procedures:

- Tables: `dbo.Battles`, `dbo.BattleLogLines`, `dbo.BattleEvents`, `dbo.BattlePokemon`, `dbo.SetCandidates`, `dbo.BF_Set`, `dbo.DecisionPoints`, `dbo.Predictions`, `dbo.Turns`, etc.
- Stored procs (optional/per-battle):
  - `dbo.ApplySetCandidateEliminations` or fallback `dbo.ApplySetCandidateEliminations_All`
  - `dbo.MarkSuspicious_ForBattle` or fallback `dbo.MarkSuspicious_All`

Database configuration is loaded from `.env`. Copy `.env.example` to `.env` and
replace its placeholders with the local SQL Server instance and database name.

Notes:
- `pyodbc` must be configured with appropriate ODBC drivers for SQL Server on your platform.
- The live ingest writes every raw line to `dbo.BattleLogLines`; `live_turn_pipeline` rebuilds events and runs heavy steps only at checkpoint (turn) boundaries to limit load.

## Running the UI (local)

1. Create and activate a Python virtualenv and configure the local environment:

   ```powershell
   python -m venv .venv
   .\.venv\Scripts\activate
   pip install -r requirements.txt
   Copy-Item .env.example .env
   ```

2. Run the UI:

   ```powershell
   python ps_ui_test_live_ui_v4.py
   ```

3. Enter a Showdown battle URL (e.g., https://play.pokemonshowdown.com/battle-...) and click Connect. When prompted, select which player you want the predictor to model.

Behavioral notes:
- The Battle Log pane receives raw websocket lines. Autofollow-related debug messages were intentionally removed from the battle log to keep it clean.

## Running the ingest/prediction pipeline

- To run the live ingest integrated with the UI, the UI starts a websocket worker and calls `LiveBattleIngestor.on_line` for each line (see `ps_ui_test_live_ui_v4.py`).
- To run per-battle pipeline steps manually or in batch, use `run_pipeline_batch.py` or call `live_turn_pipeline.run_turn_checkpoint`.

## Development notes and conventions

- The UI uses `ttk.Treeview` widgets for tabular views. Programmatic selection triggers `<<TreeviewSelect>>` events; the code uses `_suppress_team_select_event` and timestamped programmatic-selection tracking to avoid treating those as user actions.
- Refresh cadence: the UI refresh loop ticks every 1000ms (`_refresh_interval_ms`) and calls `_render_roster`, `_render_predictions`, and `_render_candidates` as needed.
- SQL interactions are performed via `pyodbc.Cursor` objects created with `pyodbc.connect(CONN_STR)`; callers should be careful to commit when appropriate.

## Troubleshooting

- No DB connection / pyodbc errors: ensure the ODBC driver is installed and the values in `.env` point to a reachable SQL Server; confirm credentials and network access.
- UI selection highlight not visible: the code calls `team_tree.focus_set()` to give keyboard focus so the selected row is highlighted. If focus is stolen by other widgets, clicking the Team view will restore it.
- Autofollow not working: ensure `predict_player` is set (via dialog when connecting), and that DecisionPoints are present in DB for the battle. The UI resumes autofollow when a new TurnNumber is observed.

## Testing

- Unit tests: none provided by default; `pytest` is in `requirements.txt` if you add tests.
- Manual test: run the UI and connect to a live Showdown battle (or a replay URL) and verify Teams, Candidates, and Predictions populate as expected.

## Possible Improvements to be Made Later On

- Add a small settings dialog for toggling verbose autofollow/debug logging (currently silent in the Battle Log).
- Add a lightweight integration test that uses a saved sample of `BattleLogLines` to run the pipeline in-memory.
- Add per-battle stored procedures to avoid running full `*_All` SQL procs during live matches.
