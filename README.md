# Pokémon Showdown AI Battle Assistant (FutureSight)

FutureSight is a capstone project for ingesting Pokémon Showdown Gen 9 Battle
Factory matches, maintaining structured battle state in SQL Server, and using
tree-based machine-learning models to predict an opponent's next action.

## Main components

- `ps_ui_test_live_ui_v4.py`: Tkinter live-match interface.
- `live_ingest_v2.py`: websocket ingestion and live battle persistence.
- `live_turn_pipeline.py`: per-turn entity, candidate, and prediction pipeline.
- `import_replays.py`: imports public replay logs for training data.
- `train_models_tree.py`: trains action, move, and switch models.
- `predict_and_insert_predictions_v2.py`: creates and stores predictions.
- `Data JSONs/`: Pokémon Showdown dex data used for metadata and features.

Generated model files are written to `model_artifacts/`. That directory is
ignored by Git because trained artifacts can be hundreds of megabytes and can
be reproduced by running the training pipeline.

## Prerequisites

- Python 3.10, 3.11, or 3.12
- Microsoft SQL Server
- Microsoft ODBC Driver 17 for SQL Server, or another compatible driver
- An existing project database with the tables and stored procedures described
  in `DOCS.md`

## Setup

Create and activate a virtual environment, then install dependencies:

```powershell
python -m venv .venv
.\.venv\Scripts\activate
python -m pip install -r requirements.txt
```

Copy the example configuration and enter your own SQL Server values:

```powershell
Copy-Item .env.example .env
```

The application loads database settings from `.env`. The real `.env` file is
ignored by Git and must never be committed. You can configure either:

- `PS_CONN_STR` as one complete pyodbc connection string; or
- the individual `DB_DRIVER`, `DB_SERVER`, `DB_DATABASE`, and authentication
  variables shown in `.env.example`.

For Windows authentication, leave `DB_TRUSTED_CONNECTION=yes`. For SQL Server
authentication, set it to `no` and fill in `DB_USER` and `DB_PASSWORD`.

## Running the live UI

```powershell
python ps_ui_test_live_ui_v4.py
```

Paste a live Gen 9 Battle Factory URL, connect, and choose the player whose next
action should be predicted.

## Importing replays and training models

Add public replay JSON URLs to `replays.txt`, one per line. Blank lines and
lines beginning with `#` are ignored. Then run the full pipeline:

```powershell
python run_pipeline_batch.py --replays-file replays.txt --mode replace --limit all --train
```

Individual pipeline scripts can also be run directly. See `DOCS.md` and each
script's module documentation for details.

## Public-repository safety

The repository intentionally excludes:

- `.env` and other local environment files
- Git history from the original private working copy
- trained `model_artifacts/`
- Python caches, virtual environments, logs, and editor-local settings

Before publishing your own fork, run a secret scanner and review staged files:

```powershell
git status
git diff --cached
```

If a real password, token, or other credential was ever committed elsewhere,
rotate it. Removing a credential from source code does not invalidate it.

## Additional documentation

See `DOCS.md` for architecture, database expectations, pipeline behavior, and
troubleshooting notes.
