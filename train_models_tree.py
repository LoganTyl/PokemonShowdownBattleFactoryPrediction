"""
Train Gen 9 Battle Factory (singles) next-action models from dbo.TrainingExamples.

Types of models used:
- Tree-based sklearn models first (for feature importance)
- Two-stage setup:
    A) action_type: move vs switch
    B) move_id: which move (conditional on action_type=move)
    C) switch_to_id: which switch target (conditional on action_type=switch)
- Candidate masking at inference time will use: revealed moves + remaining SetCandidates-derived move pool.

This training script focuses on building *baseline* models that work end-to-end.
It saves artifacts (vectorizer + model + label encoder) to disk via joblib.

Later iteration ideas (kept here as a reminder):
- Linear baseline: FeatureHasher + LogisticRegression (fast, very debuggable with weights).
- Neural baseline: small MLP with embeddings for categorical tokens + numeric stats; later can add
    history / sequence context.

Requirements:
    pip install scikit-learn joblib pyodbc

Usage:
    python train_models_tree.py

Notes:
- Uses GroupShuffleSplit by battleId to reduce leakage.
- Uses sample weights from TrainingExamples.Weight when present.
- Uses a RandomForestClassifier by default to expose feature_importances_ easily.
    If it becomes too slow/large, you can switch to ExtraTreesClassifier or HistGradientBoostingClassifier
    (then use permutation importance instead).
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import joblib
import pyodbc
import numpy as np
from sklearn.feature_extraction import DictVectorizer
from sklearn.metrics import accuracy_score, top_k_accuracy_score
from sklearn.model_selection import GroupShuffleSplit
from sklearn.preprocessing import LabelEncoder
from sklearn.ensemble import RandomForestClassifier

from app_config import CONN_STR

# Where to save model artifacts
BASE_ARTIFACT_DIR = "model_artifacts"
RUN_ID = datetime.now(timezone.utc).strftime("run_%Y%m%d_%H%M%S")
ARTIFACT_DIR = os.path.join(BASE_ARTIFACT_DIR, RUN_ID)


# If you want smaller/denser features, tweak these:
MAX_REVEALED_MOVES = 8   # cap token explosion
MAX_CAND_MOVE_POOL = 30  # cap token explosion


# -------------------------
# Helpers
# -------------------------
def to_id(name: str) -> str:
    """Showdown-style id: lowercase alphanumeric only."""
    if not name:
        return ""
    return re.sub(r"[^a-z0-9]+", "", name.lower())


def bucket_hp(hp_pct: Optional[float]) -> str:
    """Bucket HP% into coarse bins for robustness."""
    if hp_pct is None:
        return "unk"
    v = float(hp_pct)
    # Normalize if looks like 0..1
    if v <= 1.01:
        v *= 100.0
    if v <= 0:
        return "0"
    if v < 25:
        return "lt25"
    if v < 50:
        return "25_49"
    if v < 75:
        return "50_74"
    if v < 90:
        return "75_89"
    return "90_100"


def safe_get(d: Dict[str, Any], path: List[str], default=None):
    cur: Any = d
    for p in path:
        if not isinstance(cur, dict) or p not in cur:
            return default
        cur = cur[p]
    return cur


def parse_features(state: Dict[str, Any]) -> Tuple[Dict[str, Any], Optional[str]]:
    """
    Turn your compact StateJson into a flat feature dict.
    Returns (feature_dict, battle_id).
    """
    feats: Dict[str, Any] = {}

    meta = state.get("meta") or {}
    battle_id = meta.get("battleId")

    # Meta context
    feats["phase=" + str(meta.get("phase"))] = 1
    feats["actorSide=" + str(meta.get("actorSide"))] = 1
    feats["requestType=" + str(meta.get("requestType"))] = 1

    # Tera used flags
    tera = state.get("teraUsedBySide") or {}
    if "p1" in tera:
        feats["teraUsed_p1=" + str(bool(tera["p1"]))] = 1
    if "p2" in tera:
        feats["teraUsed_p2=" + str(bool(tera["p2"]))] = 1

    # Actor / Opponent active
    a = state.get("actorActive") or {}
    o = state.get("opponentActive") or {}

    a_species = to_id(a.get("species") or a.get("battleName") or "")
    o_species = to_id(o.get("species") or o.get("battleName") or "")
    if a_species:
        feats["a_species=" + a_species] = 1
    if o_species:
        feats["o_species=" + o_species] = 1

    feats["a_hp_bucket=" + bucket_hp(a.get("hpPct"))] = 1
    feats["o_hp_bucket=" + bucket_hp(o.get("hpPct"))] = 1

    # Types (dex-enriched)
    a_types = safe_get(a, ["dex", "types"], []) or []
    o_types = safe_get(o, ["dex", "types"], []) or []
    for t in a_types[:2]:
        feats["a_type=" + str(t)] = 1
    for t in o_types[:2]:
        feats["o_type=" + str(t)] = 1

    # Base stats (numeric)
    a_bs = safe_get(a, ["dex", "baseStats"], {}) or {}
    o_bs = safe_get(o, ["dex", "baseStats"], {}) or {}
    for k in ("atk", "spa", "spe", "def", "spd", "hp", "bst"):
        if k in a_bs and a_bs[k] is not None:
            feats[f"a_bs_{k}"] = float(a_bs[k])
        if k in o_bs and o_bs[k] is not None:
            feats[f"o_bs_{k}"] = float(o_bs[k])

    # Revealed moves as tokens
    a_moves = safe_get(a, ["reveals", "moves"], []) or []
    a_moves = [m for m in a_moves if m]
    for mid in list(dict.fromkeys([to_id(m) for m in a_moves]))[:MAX_REVEALED_MOVES]:
        if mid:
            feats["a_move=" + mid] = 1

    o_moves = safe_get(o, ["reveals", "moves"], []) or []
    o_moves = [m for m in o_moves if m]
    for mid in list(dict.fromkeys([to_id(m) for m in o_moves]))[:MAX_REVEALED_MOVES]:
        if mid:
            feats["o_move=" + mid] = 1

    # MoveMeta buckets for actor (type/category/eff)
    mm = safe_get(a, ["reveals", "moveMeta"], []) or []
    for item in mm[:MAX_REVEALED_MOVES]:
        name = to_id(item.get("name") or "")
        if not name:
            continue
        mtype = item.get("type")
        cat = item.get("category")
        if mtype:
            feats[f"a_moveType[{name}]=" + str(mtype)] = 1
        if cat:
            feats[f"a_moveCat[{name}]=" + str(cat)] = 1
        eff = item.get("effVsOpponent")
        if eff is not None:
            try:
                effv = float(eff)
                if effv <= 0.0:
                    b = "0"
                elif effv < 1.0:
                    b = "lt1"
                elif effv == 1.0:
                    b = "1"
                else:
                    b = "gt1"
                feats[f"a_moveEff[{name}]=" + b] = 1
            except Exception:
                pass

    # Candidate summaries (already in your StateJson)
    a_cand = a.get("candidates") or {}
    o_cand = o.get("candidates") or {}

    feats["a_cand_status=" + str(a_cand.get("status"))] = 1
    feats["o_cand_status=" + str(o_cand.get("status"))] = 1

    for prefix, c in (("a", a_cand), ("o", o_cand)):
        if "hasCandidates" in c:
            feats[f"{prefix}_hasCand=" + str(bool(c["hasCandidates"]))] = 1
        if c.get("totalCount") is not None:
            feats[f"{prefix}_cand_total"] = float(c["totalCount"])
        if c.get("remainingCount") is not None:
            feats[f"{prefix}_cand_remaining"] = float(c["remainingCount"])

    # Coarse known/unknown flags
    feats["a_item_known=" + str(safe_get(a, ["reveals", "item"]) is not None)] = 1
    feats["a_ability_known=" + str(safe_get(a, ["reveals", "ability"]) is not None)] = 1
    feats["o_item_known=" + str(safe_get(o, ["reveals", "item"]) is not None)] = 1
    feats["o_ability_known=" + str(safe_get(o, ["reveals", "ability"]) is not None)] = 1
    feats["a_tera_known=" + str(safe_get(a, ["reveals", "teraType"]) is not None)] = 1
    feats["o_tera_known=" + str(safe_get(o, ["reveals", "teraType"]) is not None)] = 1

    return feats, battle_id

def topk_acc_from_proba(y_true, proba, k: int = 3) -> float:
    k = min(k, proba.shape[1])
    topk = np.argsort(proba, axis=1)[:, -k:]
    # y_true is shape (n,)
    return float(np.mean([y_true[i] in topk[i] for i in range(len(y_true))]))

@dataclass
class Dataset:
    X: Any
    y: Any
    groups: List[str]
    weights: List[float]
    label_encoder: LabelEncoder
    vectorizer: DictVectorizer
    feature_schema_version: str


def load_training_examples(cur: pyodbc.Cursor, label_type: str) -> List[Dict[str, Any]]:
    cur.execute(
        """
        SELECT ExampleId, DecisionPointId, FeatureSchemaVersion, FeaturesJson, LabelType, LabelValue, Weight
        FROM dbo.TrainingExamples
        WHERE LabelType = ?
        """,
        label_type,
    )
    cols = [d[0] for d in cur.description]
    return [{cols[i]: r[i] for i in range(len(cols))} for r in cur.fetchall()]


def build_dataset(rows: List[Dict[str, Any]], normalize_label_to_id: bool) -> Dataset:
    feats_list: List[Dict[str, Any]] = []
    labels: List[str] = []
    groups: List[str] = []
    weights: List[float] = []

    fsv = (rows[0].get("FeatureSchemaVersion") if rows else None) or "unknown"

    for row in rows:
        raw = row.get("FeaturesJson")
        if not raw:
            continue
        try:
            state = json.loads(raw)
        except Exception:
            continue

        feats, battle_id = parse_features(state)
        if not battle_id:
            battle_id = f"dp_{row.get('DecisionPointId')}"

        feats_list.append(feats)

        lv = (row.get("LabelValue") or "").strip()
        if normalize_label_to_id:
            lv = to_id(lv)
        labels.append(lv)

        w = row.get("Weight")
        weights.append(float(w) if w is not None else 1.0)
        groups.append(str(battle_id))

    vec = DictVectorizer(sparse=True)
    X = vec.fit_transform(feats_list)

    le = LabelEncoder()
    y = le.fit_transform(labels)

    return Dataset(X=X, y=y, groups=groups, weights=weights, label_encoder=le, vectorizer=vec, feature_schema_version=str(fsv))


def train_rf(dataset: Dataset, seed: int = 7) -> Tuple[RandomForestClassifier, Dict[str, Any]]:
    gss = GroupShuffleSplit(n_splits=1, test_size=0.2, random_state=seed)
    idx_train, idx_test = next(gss.split(dataset.X, dataset.y, groups=dataset.groups))

    Xtr = dataset.X[idx_train]
    ytr = dataset.y[idx_train]
    wtr = [dataset.weights[i] for i in idx_train]

    Xte = dataset.X[idx_test]
    yte = dataset.y[idx_test]
    wte = [dataset.weights[i] for i in idx_test]

    clf = RandomForestClassifier(
        n_estimators=400,
        max_depth=None,
        min_samples_leaf=2,
        n_jobs=-1,
        random_state=seed,
    )
    clf.fit(Xtr, ytr, sample_weight=wtr)

    proba = clf.predict_proba(Xte)
    pred = proba.argmax(axis=1)

    metrics: Dict[str, Any] = {
        "n_train": int(Xtr.shape[0]),
        "n_test": int(Xte.shape[0]),
        "accuracy": float(accuracy_score(yte, pred, sample_weight=wte)),
    }
    try:
        # k3 = min(3, proba.shape[1])
        # metrics["top3"] = float(top_k_accuracy_score(yte, proba, k=k3, labels=list(range(proba.shape[1]))))
        metrics["top3"] = topk_acc_from_proba(yte, proba, k=3)
        metrics["top5"] = topk_acc_from_proba(yte, proba, k=5)
    except Exception:
        pass

    return clf, metrics


def save_artifact(label_type: str, dataset: Dataset, model: RandomForestClassifier, metrics: Dict[str, Any]) -> str:
    os.makedirs(ARTIFACT_DIR, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    path = os.path.join(ARTIFACT_DIR, f"{label_type}_{ts}.joblib")

    bundle = {
        "label_type": label_type,
        "feature_schema_version": dataset.feature_schema_version,
        "vectorizer": dataset.vectorizer,
        "label_encoder": dataset.label_encoder,
        "model": model,
        "metrics": metrics,
        "created_at_utc": ts,
        "notes": {
            "masking": "At inference: mask to revealed moves + remaining SetCandidates-derived move pool; switches to unfainted bench.",
            "alt_linear": "FeatureHasher + LogisticRegression for fast baseline and interpretable weights.",
            "alt_neural": "MLP with embeddings for tokens; later add history/sequence context.",
        },
    }
    joblib.dump(bundle, path)
    return path


def top_feature_importances(dataset: Dataset, model: RandomForestClassifier, top_n: int = 25) -> List[Tuple[str, float]]:
    if not hasattr(model, "feature_importances_"):
        return []
    names = dataset.vectorizer.get_feature_names_out()
    imps = model.feature_importances_
    pairs = list(zip(names, imps))
    pairs.sort(key=lambda x: x[1], reverse=True)
    return [(n, float(v)) for n, v in pairs[:top_n]]


def main() -> int:
    with pyodbc.connect(CONN_STR) as conn:
        cur = conn.cursor()

        # 1) action_type
        rows_action = load_training_examples(cur, "action_type")
        if not rows_action:
            print("No TrainingExamples for LabelType='action_type'.")
            return 1
        ds_action = build_dataset(rows_action, normalize_label_to_id=False)
        m_action, met_action = train_rf(ds_action)
        p_action = save_artifact("action_type", ds_action, m_action, met_action)

        # 2) move_id
        rows_move = load_training_examples(cur, "move_name")
        p_move = None
        if rows_move:
            ds_move = build_dataset(rows_move, normalize_label_to_id=True)
            m_move, met_move = train_rf(ds_move)
            p_move = save_artifact("move_id", ds_move, m_move, met_move)

        # 3) switch_to_id
        rows_sw = load_training_examples(cur, "switch_to")
        p_sw = None
        if rows_sw:
            ds_sw = build_dataset(rows_sw, normalize_label_to_id=True)
            m_sw, met_sw = train_rf(ds_sw)
            p_sw = save_artifact("switch_to_id", ds_sw, m_sw, met_sw)

    print("\nSaved artifacts:")
    print("  action_type:", p_action)
    if p_move:
        print("  move_id:", p_move)
    if p_sw:
        print("  switch_to_id:", p_sw)

    print("\nTop feature importances (action_type):")
    for name, val in top_feature_importances(ds_action, m_action, top_n=25):
        print(f"  {val:0.6f}  {name}")

    print("\nMetrics:")
    print("  action_type:", met_action)
    if rows_move:
        print("  move_id:", met_move)
    if rows_sw:
        print("  switch_to_id:", met_sw)
        
    
    manifest = {
    "run_id": RUN_ID,
    "created_at_utc": datetime.now(timezone.utc).isoformat(),
    "artifacts": {
        "action_type": p_action,
        "move_id": p_move,
        "switch_to_id": p_sw,
        },
    }
    
    with open(os.path.join(ARTIFACT_DIR, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
