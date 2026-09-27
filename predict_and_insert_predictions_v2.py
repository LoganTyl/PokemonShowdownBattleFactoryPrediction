"""
predict_and_insert_predictions_v2.py

Enhancements over v0:
- "latest DecisionPoints" mode for live usage: predicts only the most recent DecisionPoints per side.
- v1 masking:
  * Move mask uses revealed moves + remaining SetCandidates move pool (BF_SetMoveOption)
  * Switch mask uses unfainted bench options derived from BattleEvents 'faint' up to DecisionPoint.LineNum
- Still supports batch/replay mode over all DecisionPoints.

Assumptions / Notes:
- DecisionPoints.StateJson includes:
    state["meta"]["battleId"], state["meta"]["lineNum"], state["meta"]["actorSide"]
    state["actorActive"]["battlePokemonId"], state["actorActive"]["side"], state["actorActive"]["slot"]
- SetCandidates schema includes:
    BattlePokemonId, BF_SetId, IsEliminated
- BF_SetMoveOption schema includes:
    SetId, MoveName

Usage:
  # batch over all decisionpoints
  python predict_and_insert_predictions_v2.py --mode skip --limit all

  # only newest decisionpoints (per side) for one live battle
  python predict_and_insert_predictions_v2.py --battle-id gen9battlefactory-... --latest-only --mode replace

Model selection:
- By default uses newest folder model_artifacts/run_*
- You can pass --model-run model_artifacts/run_YYYYMMDD_HHMMSS
"""

from __future__ import annotations

import argparse
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple, Set

import pyodbc
import joblib

from app_config import CONN_STR

DEFAULT_CONN_STR = CONN_STR

# -------------------------
# Helpers
# -------------------------
_TO_ID_RE = re.compile(r"[^a-z0-9]+")

def to_id(s: str) -> str:
    if s is None:
        return ""
    return _TO_ID_RE.sub("", str(s).lower())

def get_predict_side(cur: pyodbc.Cursor, battle_id: str, predict_for_player_name: str) -> str:
    cur.execute("SELECT Player1Name, Player2Name FROM dbo.Battles WHERE BattleId = ?;", battle_id)
    r = cur.fetchone()
    if not r:
        raise ValueError(f"No battle row for BattleId={battle_id}")
    p1, p2 = str(r[0]), str(r[1])
    if predict_for_player_name == p1:
        return "p1"
    if predict_for_player_name == p2:
        return "p2"
    raise ValueError(f"predict_for_player_name '{predict_for_player_name}' not in battle players: '{p1}', '{p2}'")

# -------------------------
# Model artifact discovery
# -------------------------
def newest_run_dir(base_dir: str = "model_artifacts") -> Optional[str]:
    if not os.path.isdir(base_dir):
        return None
    run_dirs = [
        os.path.join(base_dir, name)
        for name in os.listdir(base_dir)
        if os.path.isdir(os.path.join(base_dir, name)) and name.lower().startswith("run_")
    ]
    if not run_dirs:
        return None
    run_dirs.sort(key=lambda p: os.path.getmtime(p), reverse=True)
    return run_dirs[0]


def newest_joblib_with_prefix(search_dir: str, prefix: str) -> Optional[str]:
    if not os.path.isdir(search_dir):
        return None
    cands = []
    for name in os.listdir(search_dir):
        if not name.lower().endswith(".joblib"):
            continue
        if not name.lower().startswith(prefix.lower()):
            continue
        cands.append(os.path.join(search_dir, name))
    if not cands:
        return None
    cands.sort(key=lambda p: os.path.getmtime(p), reverse=True)
    return cands[0]


@dataclass
class ModelBundle:
    model: Any
    classes: Optional[List[str]]
    feature_columns: Optional[List[str]]
    model_version: str
    vectorizer: Any = None
    label_encoder: Any = None


def load_bundle(path: str, model_version: str) -> ModelBundle:
    obj = joblib.load(path)

    if isinstance(obj, dict) and "model" in obj:
        mdl = obj["model"]
        cols = obj.get("feature_columns")
        cls = obj.get("classes")
        vec = obj.get("vectorizer") or obj.get("dict_vectorizer") or obj.get("dv") or obj.get("preprocessor")
        le = obj.get("label_encoder")

        if cls is None and "label_encoder" in obj:
            le = obj["label_encoder"]
            try:
                cls = [str(x) for x in list(le.classes_)]
            except Exception:
                cls = None

        if cls is None:
            try:
                cls = [str(x) for x in list(getattr(mdl, "classes_"))]
            except Exception:
                cls = None

        return ModelBundle(
            model=mdl,
            classes=cls,
            feature_columns=cols,
            model_version=model_version,
            vectorizer=vec,
            label_encoder=le,
        )

    mdl = obj
    cls = None
    try:
        cls = [str(x) for x in list(getattr(mdl, "classes_"))]
    except Exception:
        pass
    return ModelBundle(
        model=mdl,
        classes=cls,
        feature_columns=cols,
        model_version=model_version,
        vectorizer=vec,
        label_encoder=le,
    )


def load_models(model_run: Optional[str]) -> Tuple[ModelBundle, Optional[ModelBundle], Optional[ModelBundle]]:
    if model_run is None:
        model_run = newest_run_dir("model_artifacts")
    if model_run is None:
        raise RuntimeError("No model artifacts found. Train models first (model_artifacts/run_*).")

    search_dir = model_run if os.path.isdir(model_run) else os.path.dirname(model_run)
    model_version = os.path.basename(search_dir)

    p_action = newest_joblib_with_prefix(search_dir, "action_type") or newest_joblib_with_prefix(search_dir, "action")
    if not p_action:
        raise RuntimeError(f"Could not find action_type*.joblib in {search_dir}")

    p_move = newest_joblib_with_prefix(search_dir, "move_id") or newest_joblib_with_prefix(search_dir, "move")
    p_sw = newest_joblib_with_prefix(search_dir, "switch_to_id") or newest_joblib_with_prefix(search_dir, "switch")

    action = load_bundle(p_action, model_version=model_version)
    move = load_bundle(p_move, model_version=model_version) if p_move else None
    sw = load_bundle(p_sw, model_version=model_version) if p_sw else None
    return action, move, sw


# -------------------------
# Feature extraction
# -------------------------
def try_import_training_featurizer() -> Optional[Any]:
    try:
        import train_models_tree  # type: ignore
        return train_models_tree
    except Exception:
        return None


def baseline_parse_features(state: Dict[str, Any]) -> Dict[str, Any]:
    f: Dict[str, Any] = {}
    meta = state.get("meta") or {}
    f["turn"] = meta.get("turnNumber") or 0
    f["actorSide"] = meta.get("actorSide") or ""
    f["phase"] = meta.get("phase") or ""

    a = state.get("actorActive") or {}
    o = state.get("opponentActive") or {}

    def add_mon(prefix: str, mon: Dict[str, Any]) -> None:
        f[prefix + "_species"] = mon.get("species") or ""
        f[prefix + "_hpPct"] = float(mon.get("hpPct") or 0.0)
        dex = mon.get("dex") or {}
        types = dex.get("types") or []
        f[prefix + "_type1"] = types[0] if len(types) >= 1 else ""
        f[prefix + "_type2"] = types[1] if len(types) >= 2 else ""
        bs = dex.get("baseStats") or {}
        for k in ("hp", "atk", "def", "spa", "spd", "spe", "bst"):
            if k in bs:
                f[f"{prefix}_bs_{k}"] = int(bs.get(k) or 0)

        rev = mon.get("reveals") or {}
        moves = rev.get("moves") or []
        moves = sorted([m for m in moves if m])[:4]
        for i in range(4):
            f[f"{prefix}_move_{i+1}"] = moves[i] if i < len(moves) else ""
        f[prefix + "_item_known"] = 1 if rev.get("item") not in (None, "", "UNKNOWN") else 0
        f[prefix + "_ability_known"] = 1 if rev.get("ability") not in (None, "", "UNKNOWN") else 0
        f[prefix + "_tera_known"] = 1 if rev.get("teraType") not in (None, "", "UNKNOWN") else 0

        cand = mon.get("candidates") or {}
        f[prefix + "_cand_total"] = int(cand.get("totalCount") or 0)
        f[prefix + "_cand_remaining"] = int(cand.get("remainingCount") or 0)

    add_mon("a", a)
    add_mon("o", o)

    tera = state.get("teraUsedBySide") or {}
    f["p1_tera_used"] = 1 if tera.get("p1") else 0
    f["p2_tera_used"] = 1 if tera.get("p2") else 0
    return f


def vectorize_features(feature_dict: Dict[str, Any], feature_columns: Optional[List[str]]) -> List[Any]:
    cols = feature_columns if feature_columns else sorted(feature_dict.keys())
    vals: List[Any] = []
    for c in cols:
        v = feature_dict.get(c)
        if isinstance(v, bool):
            v = int(v)
        vals.append(v if v is not None else 0)
    return vals


# -------------------------
# DB queries for masks
# -------------------------
def get_candidate_move_pool(cur: pyodbc.Cursor, battle_pokemon_id: int) -> Set[str]:
    """
    Union of all MoveName across remaining candidate BF sets for this battle pokemon.
    """
    cur.execute("""
        SELECT DISTINCT MO.MoveName
        FROM dbo.SetCandidates SC
        JOIN dbo.BF_SetMoveOption MO
          ON MO.SetId = SC.BF_SetId
        WHERE SC.BattlePokemonId = ?
          AND SC.IsEliminated = 0
          AND MO.MoveName IS NOT NULL
          AND MO.MoveName <> '';
    """, battle_pokemon_id)
    return {str(r[0]) for r in cur.fetchall()}


def get_unfainted_bench(cur: pyodbc.Cursor, battle_id: str, side: str, dp_line_num: int, active_bpid: Optional[int]) -> List[Tuple[int, str, Optional[str], int, Optional[str]]]:
    """
    Returns list of (BattlePokemonId, Species, BattleName, Slot, DisplayName)
    that are NOT fainted as of dp_line_num and are not the active mon.
    """
    cur.execute("""
        SELECT BP.BattlePokemonId, BP.Species, BP.BattleName, BP.Slot, DP.DisplayName
        FROM dbo.BattlePokemon BP
        LEFT JOIN dbo.DexPokemon DP
          ON DP.DisplayName = BP.Species
        WHERE BP.BattleId = ?
          AND BP.Side = ?
          AND (? IS NULL OR BP.BattlePokemonId <> ?)
          AND NOT EXISTS (
            SELECT 1
            FROM dbo.BattleEvents E
            WHERE E.BattleId = BP.BattleId
              AND E.EventType = 'faint'
              AND E.TargetBattlePokemonId = BP.BattlePokemonId
              AND E.LineNum <= ?
          )
        ORDER BY BP.Slot;
    """, battle_id, side, active_bpid, active_bpid, dp_line_num)

    out = []
    for r in cur.fetchall():
        out.append((
            int(r[0]),
            str(r[1]),
            (str(r[2]) if r[2] is not None else None),
            int(r[3]),
            (str(r[4]) if r[4] is not None else None),
        ))
    return out


# -------------------------
# Masking utilities
# -------------------------
def allowed_moves_v1(cur: pyodbc.Cursor, state: Dict[str, Any]) -> Optional[Set[str]]:
    """
    Allowed moves = revealed moves + candidate move pool (names + ids)
    """
    a = state.get("actorActive") or {}
    bpid = a.get("battlePokemonId")
    if not bpid:
        return None

    rev = a.get("reveals") or {}
    revealed = {m for m in (rev.get("moves") or []) if isinstance(m, str) and m.strip()}

    cand_moves = set()
    try:
        cand_moves = get_candidate_move_pool(cur, int(bpid))
    except Exception:
        cand_moves = set()

    names = set(revealed) | set(cand_moves)
    if not names:
        return None

    allowed: Set[str] = set()
    for n in names:
        allowed.add(n)
        allowed.add(to_id(n))
    return allowed


def allowed_switches_v1(cur: pyodbc.Cursor, state: Dict[str, Any]) -> Optional[Set[str]]:
    """
    Allowed switches = unfainted bench (BattlePokemon rows not fainted by dp_line_num),
    encoded as several representations to match whatever label scheme your model uses.
    """
    meta = state.get("meta") or {}
    battle_id = meta.get("battleId")
    line_num = meta.get("lineNum")
    actor_side = meta.get("actorSide")

    if not battle_id or line_num is None or not actor_side:
        return None

    a = state.get("actorActive") or {}
    active_bpid = a.get("battlePokemonId")

    bench = get_unfainted_bench(cur, str(battle_id), str(actor_side), int(line_num), int(active_bpid) if active_bpid else None)
    if not bench:
        return None

    allowed: Set[str] = set()
    for bpid, species, bname, slot, display in bench:
        # numeric id
        allowed.add(str(bpid))

        # species id stored in BP.Species (often pokemonid like 'swampert')
        allowed.add(species)
        allowed.add(to_id(species))

        # display name variants (often what the model was trained on)
        if display:
            allowed.add(display)          # 'Swampert'
            allowed.add(to_id(display))   # 'swampert'

        # battle name
        if bname:
            allowed.add(bname)
            allowed.add(to_id(bname))

        # slot variants
        allowed.add(str(slot))
        allowed.add(f"slot{slot}")
    
    # Extra safety: ensure the currently-active mon cannot be a switch target.
    # This handles cases where active_bpid is missing/mismatched and bench query doesn't exclude it.
    active_species = a.get("species") or a.get("speciesName") or a.get("displayName") or a.get("pokemon")
    if active_species:
        allowed.discard(str(active_species))
        allowed.discard(to_id(str(active_species)))

    # If actorActive includes BattleName (e.g., "Kyurem-Black" shown as "Kyurem"), also discard it
    active_bname = a.get("battleName") or a.get("name")
    if active_bname:
        allowed.discard(str(active_bname))
        allowed.discard(to_id(str(active_bname)))

    # And if we do have an active battlepokemonid, discard that too
    if active_bpid:
        allowed.discard(str(active_bpid))

    return allowed

def allowed_switches_for_side(cur: pyodbc.Cursor, state: Dict[str, Any], side: str) -> Optional[Set[str]]:
    meta = state.get("meta") or {}
    battle_id = meta.get("battleId")
    line_num = meta.get("lineNum")
    if not battle_id or line_num is None or side not in ("p1", "p2"):
        return None

    # Active for THAT side (not necessarily actorActive)
    # Try dedicated fields first; fallback to actor/foe depending on side.
    active = None
    if (state.get("p1Active") or state.get("p2Active")):
        active = state.get("p1Active") if side == "p1" else state.get("p2Active")

    if active is None:
        actor_side = (meta.get("actorSide") or "").lower()
        if actor_side == side:
            active = state.get("actorActive") or {}
        else:
            active = state.get("opponentActive") or {}  # if your schema has it

    active_bpid = (active or {}).get("battlePokemonId")

    bench = get_unfainted_bench(
        cur, str(battle_id), side, int(line_num),
        int(active_bpid) if active_bpid else None
    )
    if not bench:
        return None

    allowed: Set[str] = set()
    for bpid, species, bname, slot, display in bench:
        allowed.add(str(bpid))
        allowed.add(species)
        allowed.add(to_id(species))
        if display:
            allowed.add(display)
            allowed.add(to_id(display))
        if bname:
            allowed.add(bname)
            allowed.add(to_id(bname))
        allowed.add(str(slot))
        allowed.add(f"slot{slot}")

    # Explicitly exclude the active mon for this side (by species/name/id)
    active_species = (active or {}).get("species") or (active or {}).get("speciesName") or (active or {}).get("displayName") or (active or {}).get("pokemon")
    if active_species:
        allowed.discard(str(active_species))
        allowed.discard(to_id(str(active_species)))
    active_bname = (active or {}).get("battleName") or (active or {}).get("name")
    if active_bname:
        allowed.discard(str(active_bname))
        allowed.discard(to_id(str(active_bname)))
    if active_bpid:
        allowed.discard(str(active_bpid))

    return allowed

def allowed_moves_for_side(cur: pyodbc.Cursor, state: Dict[str, Any], side: str) -> Optional[Set[str]]:
    # If the side you want equals actorSide, reuse existing behavior
    meta = state.get("meta") or {}
    actor = (meta.get("actorSide") or "").lower()
    if side == actor:
        return allowed_moves_v1(cur, state)

    # Otherwise, flip the perspective by swapping actor/opponent blocks in a shallow copy
    st = dict(state)
    st["actorActive"], st["opponentActive"] = state.get("opponentActive"), state.get("actorActive")
    st_meta = dict(meta)
    st_meta["actorSide"] = side
    st["meta"] = st_meta
    return allowed_moves_v1(cur, st)

def mask_and_renorm(proba: List[float], labels: List[str], allowed: Optional[Set[str]]) -> Tuple[List[Tuple[str, float]], Dict[str, Any]]:
    if not labels:
        return [], {"maskApplied": False, "maskReason": "no_labels"}

    if allowed is None:
        pairs = list(zip(labels, proba))
        pairs.sort(key=lambda x: x[1], reverse=True)
        return pairs, {"maskApplied": False, "maskReason": "no_mask"}

    filtered = [(lab, p) for lab, p in zip(labels, proba) if lab in allowed]
    if not filtered:
        pairs = list(zip(labels, proba))
        pairs.sort(key=lambda x: x[1], reverse=True)
        return pairs, {"maskApplied": False, "maskReason": "mask_empty_fallback"}

    s = sum(p for _, p in filtered)
    if s <= 0:
        filtered.sort(key=lambda x: x[1], reverse=True)
        return filtered, {"maskApplied": True, "maskReason": "zero_sum_no_renorm"}

    ren = [(lab, p / s) for lab, p in filtered]
    ren.sort(key=lambda x: x[1], reverse=True)
    return ren, {"maskApplied": True, "maskReason": "derived_mask_v1", "allowedCount": len(allowed)}


# -------------------------
# DecisionPoint selection
# -------------------------
def fetch_decisionpoints_batch(cur: pyodbc.Cursor, battle_id: Optional[str], limit: Optional[int]) -> List[Tuple[int, str]]:
    """
    Returns (DecisionPointId, StateJson)
    """
    sql = """
        SELECT DP.DecisionPointId, DP.StateJson
        FROM dbo.DecisionPoints DP
        WHERE DP.StateJson IS NOT NULL
    """
    params: List[Any] = []
    if battle_id:
        sql += " AND DP.BattleId = ?"
        params.append(battle_id)

    sql += " ORDER BY DP.DecisionPointId ASC"
    if limit is not None:
        sql = f"SELECT TOP ({int(limit)}) * FROM ({sql}) AS X ORDER BY X.DecisionPointId ASC"

    cur.execute(sql, params)
    return [(int(r[0]), str(r[1])) for r in cur.fetchall()]


def fetch_latest_decisionpoints_per_side(cur: pyodbc.Cursor, battle_id: str, n_per_side: int = 1) -> List[Tuple[int, str]]:
    """
    Returns up to n_per_side newest DecisionPoints per ActorSide for a battle, with StateJson.
    Newest is determined by battle flow order (LineNum), not DecisionPointId.
    """
    cur.execute("""
        WITH Ranked AS (
          SELECT
            DP.DecisionPointId,
            DP.StateJson,
            DP.ActorSide,
            DP.LineNum,
            ROW_NUMBER() OVER (
                PARTITION BY DP.ActorSide
                ORDER BY DP.LineNum DESC, DP.DecisionPointId DESC
            ) AS rn
          FROM dbo.DecisionPoints DP
          WHERE DP.BattleId = ?
            AND DP.StateJson IS NOT NULL
        )
        SELECT DecisionPointId, StateJson
        FROM Ranked
        WHERE rn <= ?
        ORDER BY LineNum ASC, DecisionPointId ASC;
    """, battle_id, int(n_per_side))
    return [(int(r[0]), str(r[1])) for r in cur.fetchall()]


# -------------------------
# Predictions DB I/O
# -------------------------
def prediction_exists(cur: pyodbc.Cursor, decision_point_id: int, prediction_type: str, model_version: Optional[str]) -> bool:
    if model_version is None:
        cur.execute("SELECT 1 FROM dbo.Predictions WHERE DecisionPointId = ? AND PredictionType = ?;",
                    decision_point_id, prediction_type)
    else:
        cur.execute("SELECT 1 FROM dbo.Predictions WHERE DecisionPointId = ? AND PredictionType = ? AND ModelVersion = ?;",
                    decision_point_id, prediction_type, model_version)
    return cur.fetchone() is not None


def delete_prediction(cur: pyodbc.Cursor, decision_point_id: int, prediction_type: str, model_version: Optional[str]) -> int:
    cur.execute(
        "DELETE FROM dbo.Predictions WHERE DecisionPointId = ? AND PredictionType = ? AND ISNULL(ModelVersion,'') = ISNULL(?, '');",
        decision_point_id, prediction_type, model_version
    )
    return int(cur.rowcount or 0)


def insert_prediction(cur: pyodbc.Cursor, decision_point_id: int, model_version: Optional[str], prediction_type: str, payload: Dict[str, Any]) -> None:
    cur.execute(
        """
        INSERT INTO dbo.Predictions (DecisionPointId, ModelVersion, PredictionType, PredictionJson, CreatedAt)
        VALUES (?, ?, ?, ?, ?)
        """,
        decision_point_id,
        model_version,
        prediction_type,
        json.dumps(payload, ensure_ascii=False),
        datetime.now(timezone.utc).replace(tzinfo=None),
    )


# -------------------------
# Prediction core
# -------------------------
def predict_for_state(
    cur: pyodbc.Cursor,
    state: Dict[str, Any],
    action_bundle: ModelBundle,
    move_bundle: Optional[ModelBundle],
    switch_bundle: Optional[ModelBundle],
    featurizer_mod: Optional[Any],
) -> Dict[str, Dict[str, Any]]:
    if featurizer_mod and hasattr(featurizer_mod, "parse_features"):
        res = featurizer_mod.parse_features(state)  # type: ignore
        if isinstance(res, tuple) and len(res) >= 1:
            feats = res[0]
            battle_id_from_feats = res[1] if len(res) > 1 else None
        else:
            feats = res
            battle_id_from_feats = None
    else:
        feats = baseline_parse_features(state)
        battle_id_from_feats = None
        
    meta = state.get("meta") or {}
    request_type = str(meta.get("requestType") or "").lower()
    predict_side = (meta.get("predictSide") or "").lower()
    if predict_side not in ("p1", "p2"):
        raise ValueError("state.meta.predictSide is required for predictions")

    def proba_payload(bundle: ModelBundle, allowed: Optional[Set[str]], ptype: str) -> Dict[str, Any]:
        # Preferred: DictVectorizer path (your artifacts contain this)
        if bundle.vectorizer is not None:
            X = bundle.vectorizer.transform([feats])  # feats must be dict[str, number/bool]
            proba = bundle.model.predict_proba(X)[0]
        else:
            # fallback (only works if model was trained on dense vectors already)
            X = [vectorize_features(feats, bundle.feature_columns)]
            proba = bundle.model.predict_proba(X)[0]
        if bundle.classes:
            labels = bundle.classes
        elif bundle.label_encoder is not None:
            labels = [str(x) for x in list(bundle.label_encoder.classes_)]
        else:
            labels = [str(i) for i in range(len(proba))]
        pairs, mask_info = mask_and_renorm([float(x) for x in proba], labels, allowed)
        topk = pairs[:10]
        return {
            "meta": state.get("meta", {}),
            "modelVersion": bundle.model_version,
            "predictionType": ptype,
            "mask": mask_info,
            "topK": [{"label": lab, "prob": float(p)} for lab, p in topk],
            "generatedAtUtc": datetime.now(timezone.utc).isoformat(),
            "battleIdFromFeaturizer": battle_id_from_feats,
        }

    payloads: Dict[str, Dict[str, Any]] = {}
    payloads["action_type"] = proba_payload(action_bundle, None, "action_type")

    # Normal turn: opponent can either move or switch
    if request_type == "move":
        if move_bundle is not None:
            payloads["next_move"] = proba_payload(
                move_bundle,
                allowed_moves_for_side(cur, state, predict_side),
                "next_move"
            )

        if switch_bundle is not None:
            payloads["next_switch"] = proba_payload(
                switch_bundle,
                allowed_switches_for_side(cur, state, predict_side),
                "next_switch"
            )

    # Forced replacement / switch-only state
    elif request_type in ("switch", "forced_switch", "teampreview"):
        if switch_bundle is not None:
            payloads["next_switch"] = proba_payload(
                switch_bundle,
                allowed_switches_for_side(cur, state, predict_side),
                "next_switch"
            )

    return payloads


# -------------------------
# Model loader wrapper
# -------------------------
def load_models(model_run: Optional[str]) -> Tuple[ModelBundle, Optional[ModelBundle], Optional[ModelBundle]]:
    if model_run is None:
        model_run = newest_run_dir("model_artifacts")
    if model_run is None:
        raise RuntimeError("No model artifacts found under model_artifacts/run_*")

    search_dir = model_run if os.path.isdir(model_run) else os.path.dirname(model_run)
    model_version = os.path.basename(search_dir)

    p_action = newest_joblib_with_prefix(search_dir, "action_type") or newest_joblib_with_prefix(search_dir, "action")
    if not p_action:
        raise RuntimeError(f"Could not find action_type*.joblib in {search_dir}")

    p_move = newest_joblib_with_prefix(search_dir, "move_id") or newest_joblib_with_prefix(search_dir, "move")
    p_sw = newest_joblib_with_prefix(search_dir, "switch_to_id") or newest_joblib_with_prefix(search_dir, "switch")

    action = load_bundle(p_action, model_version=model_version)
    move = load_bundle(p_move, model_version=model_version) if p_move else None
    sw = load_bundle(p_sw, model_version=model_version) if p_sw else None
    return action, move, sw


# -------------------------
# CLI
# -------------------------
# -------------------------
# Public API for live integration
# -------------------------
def run_predictions_for_battle(
    *,
    conn_str: str,
    battle_id: str,
    predict_for_player_name: str,
    mode: str = "replace",
    latest_only: bool = True,
    n_per_side: int = 1,
    model_run: Optional[str] = None,
    include_move: bool = True,
    include_switch: bool = True,
) -> Dict[str, int]:
    """
    Convenience wrapper for live ingestion.

    Runs predictions and inserts into dbo.Predictions.

    Returns counts:
      {"selected": N, "inserted": X, "skipped": Y, "replaced_rows": Z}
    """
    if mode not in ("skip", "replace"):
        raise ValueError("mode must be 'skip' or 'replace'")

    action_bundle, move_bundle, switch_bundle = load_models(model_run)
    if not include_move:
        move_bundle = None
    if not include_switch:
        switch_bundle = None
        
    featurizer_mod = try_import_training_featurizer()

    conn = pyodbc.connect(conn_str, timeout=120)
    try:
        conn.autocommit = False
        cur = conn.cursor()

        if latest_only:
            dps = fetch_latest_decisionpoints_per_side(cur, battle_id, n_per_side=n_per_side)
        else:
            dps = fetch_decisionpoints_batch(cur, battle_id, limit=None)

        inserted = 0
        skipped = 0
        replaced_rows = 0

        for dp_id, state_json in dps:
            try:
                state = json.loads(state_json)
            except Exception:
                skipped += 1
                continue
            
            meta = state.setdefault("meta", {})
            bid = meta.get("battleId") or battle_id
            meta["predictSide"] = get_predict_side(cur, str(bid), predict_for_player_name)

            payloads = predict_for_state(cur, state, action_bundle, move_bundle, switch_bundle, featurizer_mod)

            for ptype, payload in payloads.items():
                mv = payload.get("modelVersion") or action_bundle.model_version

                if mode == "skip":
                    if prediction_exists(cur, dp_id, ptype, mv):
                        skipped += 1
                        continue
                    insert_prediction(cur, dp_id, mv, ptype, payload)
                    inserted += 1
                else:
                    replaced_rows += delete_prediction(cur, dp_id, ptype, mv)
                    insert_prediction(cur, dp_id, mv, ptype, payload)
                    inserted += 1

        conn.commit()
        return {"selected": len(dps), "inserted": inserted, "skipped": skipped, "replaced_rows": replaced_rows}
    finally:
        try:
            conn.close()
        except Exception:
            pass
        
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--conn-str", default=DEFAULT_CONN_STR)
    ap.add_argument("--battle-id", default=None)
    ap.add_argument("--latest-only", action="store_true", help="Only predict newest DecisionPoints per side (requires --battle-id).")
    ap.add_argument("--n-per-side", type=int, default=1, help="When --latest-only, how many newest DPs per side to predict.")
    ap.add_argument("--mode", choices=["skip", "replace"], default="skip")
    ap.add_argument("--limit", default="all")
    ap.add_argument("--model-run", default=None)
    ap.add_argument("--no-move", action="store_true")
    ap.add_argument("--no-switch", action="store_true")
    ap.add_argument("--predict-for-player", default=None, help="Exact player name (Player1Name or Player2Name) to predict.")
    args = ap.parse_args()

    limit = None if args.limit == "all" else int(args.limit)

    action_bundle, move_bundle, switch_bundle = load_models(args.model_run)
    if args.no_move:
        move_bundle = None
    if args.no_switch:
        switch_bundle = None

    featurizer_mod = try_import_training_featurizer()

    conn = pyodbc.connect(args.conn_str, timeout=120)
    try:
        conn.autocommit = False
        cur = conn.cursor()

        if args.latest_only:
            if not args.battle_id:
                raise SystemExit("--latest-only requires --battle-id")
            dps = fetch_latest_decisionpoints_per_side(cur, args.battle_id, n_per_side=args.n_per_side)
        else:
            dps = fetch_decisionpoints_batch(cur, args.battle_id, limit)

        print(f"DecisionPoints selected: {len(dps)}")
        print("Using models to create predictions...")

        inserted = 0
        skipped = 0
        replaced_rows = 0

        for dp_id, state_json in dps:
            try:
                state = json.loads(state_json)
            except Exception:
                skipped += 1
                continue
            
            if args.predict_for_player:
                meta = state.setdefault("meta", {})
                bid = meta.get("battleId") or args.battle_id
                meta["predictSide"] = get_predict_side(cur, str(bid), args.predict_for_player)

            payloads = predict_for_state(cur, state, action_bundle, move_bundle, switch_bundle, featurizer_mod)

            for ptype, payload in payloads.items():
                mv = payload.get("modelVersion") or action_bundle.model_version

                if args.mode == "skip":
                    if prediction_exists(cur, dp_id, ptype, mv):
                        skipped += 1
                        continue
                    insert_prediction(cur, dp_id, mv, ptype, payload)
                    inserted += 1
                else:
                    replaced_rows += delete_prediction(cur, dp_id, ptype, mv)
                    insert_prediction(cur, dp_id, mv, ptype, payload)
                    inserted += 1

        conn.commit()
        print(f"Inserted={inserted} Skipped={skipped} ReplacedRows={replaced_rows}")
        return 0
    finally:
        try:
            conn.close()
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
