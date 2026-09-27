# Pokémon Showdown Future Sight – Developer Handoff
Generated: 2026-03-14 UTC

---

## 1. Project Overview

This project is a real-time AI assistant for **Pokémon Showdown Gen 9 Battle Factory Singles**.

Its purpose is to help players, especially newer competitive players, understand what the opponent is likely running and what the opponent is likely to do next.

The system has two major halves:

### A. Deterministic state / candidate engine
This side of the project:
- ingests replay logs or live spectator websocket lines
- parses them into structured battle events
- builds battle entities and turn structure
- tracks revealed information
- initializes and eliminates possible opponent sets using Battle Factory snapshot data and SQL procedures

### B. Machine-learning prediction engine
This side of the project:
- creates structured decision points from the live/replay state
- generates a compact `StateJson`
- predicts:
  - **action_type**: move vs switch
  - **next_move**
  - **next_switch**
- stores predictions in SQL
- displays them in the Tkinter UI

The current project state is **working and presentable**:
- replay pipeline works
- live spectator pipeline works
- live UI works
- predictions update during battle
- forced-switch and post-KO timing issues that previously caused one-turn lag are fixed

---

## 2. Current Working State

### Confirmed working
- Replay ingestion and downstream prediction pipeline
- Live spectator websocket ingestion
- `battle-` prefix normalization for live battle IDs
- Live parsing of:
  - player names
  - format display name
  - Battle Factory tier broadcast
  - teampreview roster
- Battle entity population for live battles
- Set candidate initialization for live battles
- Reveal population for live battles
- Turn reconstruction
- Decision point generation
- State JSON generation
- Live prediction generation and insertion
- UI display of:
  - team rosters
  - active markers
  - set candidates
  - eliminated sets greyed out
  - predictions
  - combined action ranking
  - switch-only forced replacement states
  - battle log
- Reconnect resilience improvements:
  - SQL cursor/connection recovery in live ingest
  - `Turns` dedupe on reconnect to same battle
  - websocket reconnect hardening work started / improved

### Notable improvements completed during this cycle
- Fixed live/replay battle ID mismatch
- Fixed live teampreview population into `BattlePokemon`
- Restored `BattlePokemon.Species` to use DisplayName-style values rather than lowercase ids
- Fixed `BattlePokemon.BattleName` assignment to bind to the correct species row instead of first-empty-slot order
- Fixed switch masking so it only predicts legal team members
- Fixed prediction-side wiring so the selected opponent is actually the side being predicted
- Fixed `next_switch` class-coverage issue by adding more training examples
- Fixed turn-lag in normal move decision points by building turn-start decision points from `dbo.Turns`
- Fixed forced-switch lag by checkpointing on `faint`
- Fixed latest-prediction selection logic to use battle-flow order (`LineNum`) rather than just `DecisionPointId`
- Fixed UI sticking on stale forced-switch predictions
- Fixed active-marker display so both sides can be marked active
- Fixed UI team auto-follow behavior

---

## 3. Main Python Files and Responsibilities

### Ingestion / parsing / state building

#### `live_ingest_v2.py`
Primary live spectator ingestion file.
Responsibilities:
- maintain live SQL connection/cursor
- normalize battle IDs
- insert `BattleLogLines`
- update battle header info from live lines
- collect teampreview roster
- trigger checkpoints on:
  - `turn`
  - `faint`
  - `win`
  - `tie`
- finalize `RawLog` / `InputLog`
- call live turn pipeline
- call prediction pipeline

#### `parse_battle_events.py`
Converts raw battle log lines into normalized `BattleEvents`.

#### `live_turn_pipeline.py`
Checkpoint pipeline for live battles.
Responsibilities:
- rebuild `BattleEvents`
- rebuild `Turns`
- populate entities/reveals
- initialize candidate sets
- run SQL procedures
- create decision points and state JSON

#### `populate_battle_entities_v2.py`
Builds battle entities from parsed events.
Responsibilities:
- populate / update `BattlePokemon`
- assign `BattleName`
- backfill event Pokémon IDs
- populate `BattlePokemonReveals`
- upsert `BattleSides`

Important note:
- this file was updated so live "skip" mode still updates reveals/backfills instead of exiting too early.

#### `init_set_candidates.py`
Initializes `SetCandidates` for a battle using the Battle Factory snapshot data.

#### `populate_decisionpoints_actions.py`
Builds:
- `DecisionPoints`
- `ObservedActions`

Important note:
- turn-start DPs were changed to use `dbo.Turns` boundaries instead of relying only on events already present in the current turn.
- forced-switch DPs are important and now get created in time when checkpointing on `faint`.

#### `populate_decisionpoint_state_v3_dex_candidates.py`
Builds compact `StateJson` per decision point, including:
- actor active
- opponent active
- candidate counts
- reveals
- tera state
- dex metadata

### Training / inference

#### `populate_training_examples.py`
Creates training examples from decision points / observed actions.

#### `train_models_tree.py`
Trains the model artifacts used for inference.

#### `predict_and_insert_predictions_v2.py`
Loads model artifacts and inserts predictions into SQL.
Key logic:
- injects `predictSide` based on the user-selected opponent name
- predicts only for the intended side
- masks illegal options
- respects request type:
  - move request: generate move + switch distributions
  - forced-switch/switch request: generate switch-only distribution
- latest DP selection now uses `LineNum DESC, DecisionPointId DESC` semantics rather than just `DecisionPointId`

### UI

#### `ps_ui_test_live_ui_v4.py`
Current Tkinter live UI.
Responsibilities:
- connect/disconnect to spectator websocket
- choose predicted player in modal
- display team rosters
- auto-follow predicted side's active Pokémon until user manually selects something else
- show set candidates
- show combined predictions / move distribution / switch distribution
- show switch-only UI for forced-switch states
- show battle log
- refresh from SQL on a timer
- keep UI DB connection alive/reopen if closed

---

## 4. Replay Pipeline

Replay flow is the more mature and deterministic path.

Typical replay flow:
1. replay text import
2. raw lines into `BattleLogLines`
3. parse into `BattleEvents`
4. populate battle entities / reveals
5. initialize `SetCandidates`
6. run candidate elimination procedures
7. build `DecisionPoints` and `ObservedActions`
8. build `StateJson`
9. build `TrainingExamples`
10. train models
11. run inference / insert predictions

Replay processing is currently considered stable.

---

## 5. Live Pipeline

Current live flow:
1. connect to Showdown as spectator
2. receive raw websocket lines
3. insert `BattleLogLines`
4. checkpoint on:
   - `turn`
   - `faint`
   - `win`
   - `tie`
5. rebuild `BattleEvents`
6. rebuild `Turns`
7. populate entities/reveals
8. initialize/eliminate set candidates
9. build DPs / state
10. insert predictions
11. refresh UI from SQL

Important design note:
- the project does **not** rely on player request JSON because it runs as a spectator.
- therefore timing is inferred from battle lines and turn/faint boundaries.

---

## 6. Machine Learning Architecture

### Models
Three separate classifiers are used:

#### `action_type`
Predicts whether the selected opponent is more likely to:
- move
- switch

#### `next_move`
Predicts the likely move **if** the opponent takes a move action.

#### `next_switch`
Predicts the likely switch target **if** the opponent switches.

### Combination logic
The UI also builds a combined ranking:
- move score = `P(move) * P(selected move | move)`
- switch score = `P(switch) * P(selected switch | switch)`

### Constraint / masking logic
This is important:
- move options are constrained by revealed info + candidate move pool for the predicted side
- switch options are constrained to legal team members only
- the active Pokémon is excluded as a switch target
- forced-switch states do **not** produce move distributions anymore

### Current modeling limitation
The switch model is a closed-label multiclass model:
- a switch target cannot be predicted unless it appears in training labels
- this caused missing labels earlier (for example `rotomwash`) when the training set was too small
- adding more replay-derived training examples fixed this in practice

---

## 7. Database Summary

### Canonical reconstruction source
A full SQL export exists and should be treated as the authoritative reconstruction source:

**`PokemonShowdownFutureSightSQLReconstruction.sql`**

This should live in a dedicated SQL / reconstruction folder.

### Core battle tables

#### `Battles`
Battle-level metadata.
Tracks:
- battle id
- player names
- format / tier
- source
- raw/input logs
- timestamps / result fields depending on schema version

#### `BattleLogLines`
Raw ordered lines from replay import or live websocket feed.

#### `BattleEvents`
Normalized battle events parsed from log lines.
Examples:
- switch
- move
- faint
- weather
- upkeep
- turn markers
- teampreview events

#### `BattlePokemon`
Battle-specific Pokémon roster rows.
Tracks:
- side
- slot
- species
- battle name
- lead flag
- IDs used by state generation and reveals

#### `BattlePokemonReveals`
Revealed facts per battle Pokémon:
- moves
- item
- ability
- tera type
- source line/turn

#### `BattleSides`
Side-level metadata (p1/p2 and player names).

#### `Turns`
Turn boundaries:
- `BattleId`
- `TurnNumber`
- `StartLineNum`
- `EndLineNum`

### Battle Factory snapshot tables

#### `BF_Tier`
Battle Factory tiers.

#### `BF_TierPokemon`
Pokémon available in each BF tier.

#### `BF_Set`
Base definition of a set candidate.
Known columns include:
- `SetId`
- `TierPokemonId`
- `SetIndex`
- `Species`
- `SetWeight`
- `WantsTera`
- `Gender`
- EVs
- IVs

#### `BF_SetMoveOption`
Move options for a set, including support for multiple options per move slot.

#### `BF_SetAbility`
Ability information/options for a set.

#### `BF_SetItem`
Item information/options for a set.

#### `BF_SetNature`
Nature information for a set.

#### `BF_SetTeraType`
Tera type information/options for a set.

### Dex tables

#### `DexPokemon`
Pokémon dex metadata.

#### `DexMove`
Move dex metadata.

#### `DexTypeEffectiveness`
Type matchup table.

### ML / state / prediction tables

#### `DecisionPoints`
Stores the moments where the system predicts the selected side's choice.
Important fields include:
- `BattleId`
- `TurnNumber`
- `Phase`
- `RequestType`
- `ActorSide`
- `LineNum`
- `ActorBattlePokemonId`
- `OpponentBattlePokemonId`
- `StateJson`

#### `ObservedActions`
Stores the actual observed action for decision-point supervision.

#### `TrainingExamples`
Training data derived from DPs and observed actions.

#### `Models`
Model registry / metadata.

#### `Predictions`
Prediction rows linked to `DecisionPoints`.
Contains prediction JSON for:
- `action_type`
- `next_move`
- `next_switch`

#### `SetCandidates`
Battle-specific status for BF sets.
Known fields:
- `BattlePokemonId`
- `BF_SetId`
- `IsEliminated`
- `EliminatedAtTurn`
- `EliminatedLineNum`
- `EliminationReason`
- `CreatedAt`

### Stored procedures known and important

From the exported SQL, the important procedures include:
- `ApplySetCandidateEliminations`
- `ApplySetCandidateEliminations_All`
- `MarkSuspicious_All`
- `MarkSuspicious_ForBattle`
- `MarkSuspiciousMoves_All`
- `MarkSuspiciousMovesForBattle`
- `MarkSuspiciousTera_All`
- `MarkSuspiciousTeraForBattle`

High-level roles:
- eliminate impossible set candidates
- flag suspicious move / tera combinations for weighting or analysis

---

## 8. Current UI Status

The current UI is in `ps_ui_test_live_ui_v4.py`.

### What it currently shows
- connection / status
- selected predicted player
- turn / phase info
- battle log
- two team tables with active marker
- set candidates for selected Pokémon
- combined predictions
- move distribution
- switch distribution

### Current set-candidate behavior
- user can select a Pokémon in the roster table
- by default, UI auto-follows the predicted side's active Pokémon
- if the user manually selects something else, that selection is preserved
- eliminated sets are greyed out rather than hidden
- move slots are displayed in 4 columns
- multiple move options in one slot are displayed together in that slot column

### Current prediction behavior
- move DPs show combined action ranking plus move/switch distributions
- forced-switch DPs show switch-only messaging and switch distribution
- latest DP selection is battle-flow aware rather than purely ID-based

---

## 9. Known Current Rough Edges / Possible Brittle Points

These are not necessarily blocking, but are worth knowing.

### A. Reconnect to same battle still duplicates raw history
The `Turns` crash caused by reconnecting to the same battle has been fixed by deduping turn starts in `rebuild_turns_for_battle`, but reconnecting may still duplicate `BattleLogLines` history.
Current status:
- crash fixed
- raw history duplication still not fully solved at the ingestion layer

### B. Live websocket reconnect is improved but still worth monitoring
Unexpected socket drops can happen.
Some reconnect resilience has been added / discussed, but this is still an area worth retesting if the project resumes.

### C. The project is still monolithic in places
The UI file in particular is not MVC-style and contains:
- websocket coordination
- UI rendering
- DB polling
- some project logic
This is workable, but refactoring would help future maintainability.

### D. Closed-label switch model limitation remains a conceptual constraint
Even though the current larger training set fixed practical missing classes, the `switch_to_id` model still depends on label coverage in training.
A future scoring/ranking approach per legal candidate could remove this limitation.

### E. SQL connection health in live ingest was a real failure mode
It appears improved after recovery logic, but if future users see repeated "closed cursor / closed connection" errors, the first place to inspect is the live ingest SQL lifecycle.

### F. UI prediction selection logic is sensitive
The UI relies on selecting the most relevant DP for the chosen side.
This now works, but any future changes to DP generation order or selection SQL should be tested carefully around:
- KOs
- forced switches
- reconnects
- turn-start transitions

---

## 10. Recommended Next Steps

### Priority 1: Stabilization / cleanup
- test reconnect behavior more thoroughly
- consider deduping reconnect-replayed `BattleLogLines`
- consolidate SQL connection recovery patterns
- simplify / harden websocket reconnect behavior

### Priority 2: Code structure cleanup
- split UI, websocket, and DB-refresh responsibilities
- isolate query logic from Tkinter rendering
- separate presentation/UI code from project logic

### Priority 3: ML / data improvements
- continue increasing replay-derived training examples
- monitor class coverage for switch model
- improve evaluation / tracking of model quality over time
- consider more explicit class coverage diagnostics after retraining

### Priority 4: Model design improvements
- evaluate whether switch prediction should move toward legal-candidate scoring instead of global multiclass classification
- possibly improve move prediction calibration
- consider hybrid approaches if future scope expands beyond Battle Factory

### Priority 5: UX improvements
- more explanatory labels/tooltips
- better candidate detail formatting
- optional export/debug views
- optional presentation mode / compact mode

---

## 11. If Handing Off to a New Developer

A future person should know this first:

1. The project is now **working end-to-end** in both replay and live spectator modes.
2. The tricky areas were:
   - battle ID normalization
   - teampreview roster handling
   - correct side selection for predictions
   - DP timing around turn starts and KOs
   - selecting latest DPs by `LineNum`, not just `DecisionPointId`
3. The database is central. Most debugging is easiest if done by checking:
   - `BattleLogLines`
   - `BattleEvents`
   - `BattlePokemon`
   - `SetCandidates`
   - `DecisionPoints`
   - `Predictions`
4. The SQL export file should be treated as the canonical reconstruction source.
5. If predictions ever look “one turn behind” again, inspect:
   - checkpoint trigger timing
   - DP generation timing
   - latest-DP selection SQL
6. If a side is being predicted incorrectly, inspect:
   - `predict_for_player_name`
   - `predictSide`
   - request type handling
   - masking side selection

---

## 12. Recommended Quick Debug Queries

### Latest decision points for a battle
```sql
SELECT
    DecisionPointId,
    TurnNumber,
    Phase,
    RequestType,
    ActorSide,
    LineNum
FROM dbo.DecisionPoints
WHERE BattleId = 'gen9battlefactory-...'
ORDER BY LineNum DESC, DecisionPointId DESC;
```

### Prediction counts by decision point
```sql
SELECT
    DP.DecisionPointId,
    DP.TurnNumber,
    DP.Phase,
    DP.RequestType,
    DP.ActorSide,
    DP.LineNum,
    COUNT(P.PredictionId) AS PredCount
FROM dbo.DecisionPoints DP
LEFT JOIN dbo.Predictions P
    ON P.DecisionPointId = DP.DecisionPointId
WHERE DP.BattleId = 'gen9battlefactory-...'
GROUP BY
    DP.DecisionPointId,
    DP.TurnNumber,
    DP.Phase,
    DP.RequestType,
    DP.ActorSide,
    DP.LineNum
ORDER BY DP.LineNum DESC, DP.DecisionPointId DESC;
```

### Roster for a live battle
```sql
SELECT Side, Slot, Species, BattleName, BattlePokemonId
FROM dbo.BattlePokemon
WHERE BattleId = 'gen9battlefactory-...'
ORDER BY Side, Slot;
```

### Candidate sets for one battle Pokémon
```sql
SELECT *
FROM dbo.SetCandidates
WHERE BattlePokemonId = <BattlePokemonId>;
```

---

## 13. Assets / Important Files to Keep Together

Recommended handoff folder contents:
- `PokeShowdown_FutureSight_Developer_Handoff.md`  ← this document
- `PokemonShowdownFutureSightSQLReconstruction.sql`
- current live UI file
- current live ingest / pipeline files
- model artifacts folder currently in use
- any replay samples used for regression testing