# import joblib

# obj = joblib.load(r"model_artifacts\run_20260220_054711\action_type_20260220_054712.joblib")
# print(type(obj))
# if isinstance(obj, dict):
#     print(obj.keys())
#     for k in obj.keys():
#         v = obj[k]
#         print(k, type(v))
# else:
#     # pipeline/estimator
#     print("has steps:", hasattr(obj, "named_steps"))
#     if hasattr(obj, "named_steps"):
#         print(obj.named_steps.keys())


# from live_ingest_v2 import LiveBattleIngestor
# from live_turn_pipeline import CONN_STR

# ing = LiveBattleIngestor(conn_str=CONN_STR)
# ing.on_line("|turn|1")
# print("ok")

# from predict_and_insert_predictions_v2 import load_models, newest_joblib_with_prefix, newest_run_dir
# run = newest_run_dir("model_artifacts")
# print("model_run:", run)
# from predict_and_insert_predictions_v2 import load_bundle
# action_path = newest_joblib_with_prefix(run, "action_type") or newest_joblib_with_prefix(run,"action")
# move_path = newest_joblib_with_prefix(run, "move_id") or newest_joblib_with_prefix(run,"move")
# sw_path = newest_joblib_with_prefix(run, "switch_to_id") or newest_joblib_with_prefix(run,"switch")
# print("action_path:", action_path)
# print("move_path:", move_path)
# print("switch_path:", sw_path)
# a,m,s = load_models(run)
# print("action.classes:", len(a.classes) if a and a.classes else a.classes)
# print("move.classes:", None if m is None else (len(m.classes) if m.classes else "no classes, label_encoder="+str(bool(m.label_encoder))))
# print("switch.classes:", None if s is None else (len(s.classes) if s.classes else "no classes, label_encoder="+str(bool(s.label_encoder))))

# model_check_debug.py
import argparse, json, os
from predict_and_insert_predictions_v2 import (
    DEFAULT_CONN_STR, load_models, try_import_training_featurizer,
    baseline_parse_features, vectorize_features,
    allowed_moves_for_side, allowed_switches_for_side, mask_and_renorm
)
import pyodbc

def get_state_for_dp(cur, dp_id):
    cur.execute("SELECT StateJson FROM dbo.DecisionPoints WHERE DecisionPointId = ?;", dp_id)
    r = cur.fetchone()
    if not r:
        raise SystemExit("No DecisionPoint row for id="+str(dp_id))
    return json.loads(r[0])

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--conn-str", default=os.environ.get("PS_CONN_STR") or DEFAULT_CONN_STR)
    ap.add_argument("--dp", type=int, help="DecisionPointId to inspect", required=True)
    ap.add_argument("--model-run", default=None)
    args = ap.parse_args()

    action_b, move_b, sw_b = load_models(args.model_run)
    featurizer = try_import_training_featurizer()

    conn = pyodbc.connect(args.conn_str, timeout=60)
    cur = conn.cursor()
    state = get_state_for_dp(cur, args.dp)
    meta = state.get("meta", {})
    predict_side = (meta.get("predictSide") or "").lower()
    if predict_side not in ("p1","p2"):
        print("state.meta.predictSide not set; trying to infer or set manually.")
    feats = None
    if featurizer and hasattr(featurizer, "parse_features"):
        res = featurizer.parse_features(state)
        feats = res[0] if isinstance(res, tuple) else res
    if feats is None:
        feats = baseline_parse_features(state)

    def inspect_bundle(name, bundle, allowed_fn=None):
        if bundle is None:
            print(f"{name}: bundle is None")
            return
        # labels
        if bundle.classes:
            labels = bundle.classes
        elif bundle.label_encoder is not None:
            labels = [str(x) for x in list(bundle.label_encoder.classes_)]
        else:
            labels = None
        print(f"\n{name}: labels_count={len(labels) if labels is not None else 'None'}")
        # produce proba
        if bundle.vectorizer is not None:
            X = bundle.vectorizer.transform([feats])
            proba = bundle.model.predict_proba(X)[0]
        else:
            X = [vectorize_features(feats, bundle.feature_columns)]
            proba = bundle.model.predict_proba(X)[0]
        print(f"{name}: proba_len={len(proba)}")
        if labels is not None:
            print(f"{name}: labels_len={len(labels)}")
        # allowed
        allowed = None
        if allowed_fn:
            allowed = allowed_fn(cur, state, predict_side)
            print(f"{name}: allowed_count={(len(allowed) if allowed is not None else 'None')}")
            if allowed:
                sample = list(sorted(allowed))[:10]
                print(f"{name}: allowed_sample={sample}")
        # mask & renorm
        labs = labels if labels is not None else [str(i) for i in range(len(proba))]
        pairs, mask_info = mask_and_renorm([float(x) for x in proba], labs, allowed)
        print(f"{name}: mask_info={mask_info}")
        print(f"{name}: post_mask_count={len(pairs)}")
        print(f"{name}: top_before_mask={sorted(zip(labs, [float(x) for x in proba]), key=lambda x:-x[1])[:10]}")
        print(f"{name}: top_after_mask={pairs[:10]}")

    inspect_bundle("action_type", action_b, None)
    inspect_bundle("move", move_b, allowed_moves_for_side)
    inspect_bundle("switch", sw_b, allowed_switches_for_side)

if __name__ == '__main__':
    main()