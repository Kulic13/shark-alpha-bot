model = xgb.XGBClassifier(
    objective="multi:softprob",
    num_class=3,
    n_estimators=300,
    max_depth=4,
    learning_rate=0.03,
    subsample=0.8,
    colsample_bytree=0.8,
    reg_alpha=0.5,
    reg_lambda=1.5,
    random_state=42,
    n_jobs=-1,
)
model.fit(X_train, y_train, sample_weight=sample_weights)

preds = model.predict(X_test)
acc = float(accuracy_score(y_test, preds))
print(f"\nOverall accuracy (WAIT/LONG/SHORT): {acc * 100:.2f} %")
print("\nClassification report:")
print(classification_report(y_test, preds, labels=[0, 1, 2], target_names=["WAIT", "LONG", "SHORT"], digits=3, zero_division=0))

# Trading audit: prah 0.39 + margin +0.03 nad WAIT – parity s live ai_window6.
probs = model.predict_proba(X_test)
# class order: 0=WAIT, 1=LONG, 2=SHORT
prob_wait = probs[:, 0]
prob_long = probs[:, 1]
prob_short = probs[:, 2]
y_true = y_test.values

long_mask = (prob_long >= 0.39) & (prob_long > prob_short) & (prob_long >= (prob_wait + 0.03))
short_mask = (prob_short >= 0.39) & (prob_short > prob_long) & (prob_short >= (prob_wait + 0.03))

n_long_sig = int(long_mask.sum())
n_short_sig = int(short_mask.sum())
long_hits = int(((y_true == 1) & long_mask).sum()) if n_long_sig else 0
short_hits = int(((y_true == 2) & short_mask).sum()) if n_short_sig else 0

long_wr = (long_hits / n_long_sig * 100.0) if n_long_sig > 0 else 0.0
short_wr = (short_hits / n_short_sig * 100.0) if n_short_sig > 0 else 0.0
n_trade_sig = n_long_sig + n_short_sig
trade_hits = long_hits + short_hits
trade_wr = (trade_hits / n_trade_sig * 100.0) if n_trade_sig > 0 else 0.0

print("\n--- Trading Audit (prob >= 0.39 and >= WAIT + 0.03) ---")
print(f"LONG  signals: {n_long_sig:4d} | hits: {long_hits:4d} | WR: {long_wr:.2f}%")
print(f"SHORT signals: {n_short_sig:4d} | hits: {short_hits:4d} | WR: {short_wr:.2f}%")
print(f"COMBINED      : {n_trade_sig:4d} | hits: {trade_hits:4d} | WR: {trade_wr:.2f}%")

# Keep ai_models.accuracy semantically stable: always store overall test accuracy.
# Trading win-rate remains a separate diagnostic and is printed above.
accuracy_to_store = acc * 100.0

# Feature importance (diagnostic)
try:
    imp = pd.Series(model.feature_importances_, index=FEATURES).sort_values(ascending=False)
    print("\nFeature importance:")
    print(imp.to_string())
except Exception:
    pass

# =========================================================================
# 5. SAVE MODEL V5 → Supabase ai_models id=1
# =========================================================================
def export_verified_model(model, validation_X):
    """Export the primitive xgb_json_v1 envelope consumed by V11.3j."""
    classes = [int(c) for c in model.classes_]
    if classes != [0, 1, 2] or list(validation_X.columns) != FEATURES or validation_X.empty:
        raise ValueError("Model classes or validation features incompatible with V11.3j")
    booster = model.get_booster()
    if booster.num_features() != len(FEATURES) or booster.feature_names != FEATURES:
        raise ValueError("Booster feature count/order incompatible with V11.3j")
    booster_json = bytes(booster.save_raw(raw_format="json")).decode("utf-8")
    package = {
        "format": "xgb_json_v1",
        "booster_json": booster_json,
        "classes": classes,
        "features": list(FEATURES),
        "xgb_version": str(xgb.__version__),
    }
    # Reload using the same JSON path as the production scanner before DB write.
    reloaded = xgb.Booster()
    reloaded.load_model(bytearray(booster_json.encode("utf-8")))
    probe = validation_X.iloc[:256]
    original = np.asarray(model.predict_proba(probe), dtype=float)
    restored = np.asarray(reloaded.predict(xgb.DMatrix(probe)), dtype=float)
    if original.shape != (len(probe), 3) or restored.shape != original.shape or not np.all(np.isfinite(restored)) or not np.allclose(original, restored, rtol=1e-6, atol=1e-7):
        raise ValueError("Export validation failed: original/JSON predictions differ")
    if not np.allclose(restored.sum(axis=1), 1.0, rtol=1e-6, atol=1e-7):
        raise ValueError("Export validation failed: invalid probability sums")
    # Pickle contains only a primitive dict of strings/lists/integers, no estimator.
    return base64.b64encode(pickle.dumps(package, protocol=4)).decode("utf-8")


model_b64 = export_verified_model(model, X_test)
print("xgb_json_v1 export: verified probability parity; ready for Supabase")

supabase.table("ai_models").upsert(
    {
        "id": 1,
        "model_name": "xgboost_us_champion_v5",
        "model_base64": model_b64,
        "accuracy": round(float(accuracy_to_store) / 100.0, 4),
        "features_list": FEATURES,
        "trained_at": datetime.now(timezone.utc).isoformat(),
    }
).execute()

print("\n" + "=" * 60)
print("ÚSPECH! Model V5 (xgboost_us_champion_v5) uložený v Supabase ai_models id=1.")
print(f"accuracy field = {accuracy_to_store:.2f} % "
      f"(overall test accuracy)")
print(f"features_list = {FEATURES}")
print("=" * 60)
