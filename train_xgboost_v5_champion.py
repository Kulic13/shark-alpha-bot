# =============================================================================
# train_xgboost_v4_champion.py
# Google Colab / GitHub Actions – XGBoost US Champion V5 (12 features)
# =============================================================================
# !pip install supabase xgboost scikit-learn httpx pandas numpy

import time
import os
import json
import pickle
import base64
import httpx
import numpy as np
import pandas as pd
import xgboost as xgb
from datetime import datetime, timezone
from supabase import create_client
from sklearn.metrics import accuracy_score, classification_report
from sklearn.utils.class_weight import compute_sample_weight

# =========================================================================
# BEZPEČNÉ NAČÍTANIE KĽÚČOV Z GOOGLE COLAB SECRETS
# =========================================================================
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
if not SUPABASE_URL or not SUPABASE_KEY:
    try:
        from google.colab import userdata
        SUPABASE_URL = SUPABASE_URL or userdata.get("SUPABASE_URL")
        SUPABASE_KEY = SUPABASE_KEY or userdata.get("SUPABASE_KEY")
    except Exception:
        pass

if not SUPABASE_URL or not SUPABASE_KEY:
    raise ValueError(
        "Chýbajú kľúče SUPABASE_URL alebo SUPABASE_KEY v Colab Secrets (ikona kľúča vľavo)!"
    )

supabase = create_client(SUPABASE_URL.strip(), SUPABASE_KEY.strip())

OKX_API = "https://www.okx.com"
FEATURES_V5 = [
    "natr",
    "rsi",
    "ofd",
    "vpin",
    "vol_ratio",
    "ema_dist_atr",
    "z_us_vwap",
    "rel_btc_2h",
    "market_breadth",
    "market_dispersion",
    "regime_x",
    "regime_y",
]
FEATURES = FEATURES_V5

SYMBOLS = [
    "BTC-USDT-SWAP",
    "ETH-USDT-SWAP",
    "SOL-USDT-SWAP",
    "XRP-USDT-SWAP",
    "DOGE-USDT-SWAP",
    "SUI-USDT-SWAP",
    "AVAX-USDT-SWAP",
    "LINK-USDT-SWAP",
    "ADA-USDT-SWAP",
    "BNB-USDT-SWAP",
    "PEPE-USDT-SWAP",
    "NEAR-USDT-SWAP",
    "TON-USDT-SWAP",
    "APT-USDT-SWAP",
    "ARB-USDT-SWAP",
    "OP-USDT-SWAP",
]

# =========================================================================
# 1. HISTORICKÉ DÁTA Z OKX (15m, paginácia, dedupe)
# =========================================================================
def fetch_okx_history(symbol: str = "BTC-USDT-SWAP", total_candles: int = 3500) -> pd.DataFrame:
    print(f"Sťahujem {total_candles} sviečok pre {symbol}...")
    all_data = []
    after_ts = None
    headers = {"User-Agent": "Mozilla/5.0"}
    url = f"{OKX_API}/api/v5/market/candles"

    with httpx.Client(headers=headers, timeout=20.0, follow_redirects=True) as client:
        while len(all_data) < total_candles:
            params = {"instId": symbol, "bar": "15m", "limit": "100"}
            if after_ts:
                params["after"] = str(after_ts)

            try:
                res = client.get(url, params=params)
                if res.status_code != 200 or not res.text:
                    print(f"  API {res.status_code}, retry 2s...")
                    time.sleep(2.0)
                    continue
                payload = res.json()
            except Exception as e:
                print(f"  Chyba odpovede: {e}, retry 2s...")
                time.sleep(2.0)
                continue

            data = payload.get("data", [])
            if not data:
                break

            all_data.extend(data)
            after_ts = data[-1][0]  # oldest in this chunk (OKX newest-first)
            time.sleep(0.25)

    # chronological oldest -> newest, dedupe by ts
    rows = list(reversed(all_data))
    seen = set()
    uniq = []
    for row in rows:
        ts = row[0]
        if ts in seen:
            continue
        seen.add(ts)
        uniq.append(row)

    uniq = uniq[-total_candles:]
    df = pd.DataFrame(uniq, columns=["ts", "o", "h", "l", "c", "v", "vc", "vq", "cf"])
    for col in ["o", "h", "l", "c", "v"]:
        df[col] = df[col].astype(float)
    df["ts"] = df["ts"].astype(np.int64)
    print(f"  → {len(df)} sviečok pre {symbol}")
    return df


# =========================================================================
# 2. FEATURE ENGINEERING (100% parity s ai_window6 / _compute_features_v3)
# =========================================================================
def _compute_us_vwap_zscore(df: pd.DataFrame) -> pd.Series:
    """
    Z-skóre close vs kumulatívny VWAP od začiatku US seansy (14:30 UTC).
    Pre každý riadok: session od posledného 14:30 UTC <= ts, inak predchádzajúci deň.
    """
    times_utc = pd.to_datetime(df["ts"], unit="ms", utc=True)
    typical_px = (df["h"] + df["l"] + df["c"]) / 3.0
    vol = df["v"].replace(0, 1e-9)

    z_scores = np.zeros(len(df), dtype=float)
    for i in range(len(df)):
        now_utc = times_utc.iloc[i]
        us_open_today = now_utc.replace(hour=14, minute=30, second=0, microsecond=0)
        if now_utc < us_open_today:
            us_start = us_open_today - pd.Timedelta(days=1)
        else:
            us_start = us_open_today

        mask = times_utc.iloc[: i + 1] >= us_start
        session_idx = np.where(mask.values)[0]
        if len(session_idx) < 3:
            # fallback: last 20 bars up to i
            start = max(0, i - 19)
            session_idx = np.arange(start, i + 1)

        tp = typical_px.iloc[session_idx]
        vv = vol.iloc[session_idx]
        cum_vol = vv.cumsum()
        vwap_series = (tp * vv).cumsum() / cum_vol
        current_vwap = float(vwap_series.iloc[-1])
        std_dev = float((tp - vwap_series).std())
        if std_dev <= 1e-9 or np.isnan(std_dev):
            z_scores[i] = 0.0
        else:
            z = (float(df["c"].iloc[i]) - current_vwap) / std_dev
            z_scores[i] = float(np.clip(z, -5.0, 5.0))

    return pd.Series(z_scores, index=df.index)


def compute_base_features(df: pd.DataFrame) -> pd.DataFrame:
    """Lokálne featuri na jednom symbole (bez cross-asset)."""
    data = df.copy()

    prev_c = data["c"].shift(1)
    tr = pd.concat(
        [data["h"] - data["l"], (data["h"] - prev_c).abs(), (data["l"] - prev_c).abs()],
        axis=1,
    ).max(axis=1)
    atr_14 = tr.rolling(14).mean()
    data["natr"] = (atr_14 / data["c"]) * 100.0
    data["_atr_14"] = atr_14

    delta = data["c"].diff()
    gain = delta.clip(lower=0).rolling(14).mean()
    loss = (-delta.clip(upper=0)).rolling(14).mean()
    rs = gain / loss.replace(0, 1e-9)
    data["rsi"] = 100.0 - (100.0 / (1.0 + rs))

    rng = (data["h"] - data["l"]).replace(0, 1e-9)
    body_frac = (data["c"] - data["o"]).abs() / rng
    buyer_ratio = np.where(
        data["c"] >= data["o"],
        0.5 + 0.5 * body_frac,
        0.5 - 0.5 * body_frac,
    )
    buy_vol = (data["v"] * buyer_ratio).rolling(5).sum()
    sell_vol = (data["v"] * (1.0 - buyer_ratio)).rolling(5).sum().replace(0, 1e-9)
    data["ofd"] = buy_vol / sell_vol

    c_ratio = (data["c"] - data["l"]) / rng
    b_v = data["v"] * c_ratio
    s_v = data["v"] * (1.0 - c_ratio)
    data["vpin"] = (b_v - s_v).abs().rolling(10).sum() / (b_v + s_v).rolling(10).sum().replace(0, 1e-9)

    data["vol_ratio"] = data["v"] / data["v"].rolling(10).mean().replace(0, 1e-9)

    ema_20 = data["c"].ewm(span=20, adjust=False).mean()
    data["ema_dist_atr"] = (data["c"] - ema_20) / atr_14.replace(0, 1e-9)

    # 2h return (8 × 15m) pre neskôr rel_btc_2h
    data["ret_2h"] = (data["c"] / data["c"].shift(8) - 1.0) * 100.0
    # 24h return (96 × 15m) pre breadth / dispersion
    data["ret_24h"] = (data["c"] / data["c"].shift(96) - 1.0) * 100.0

    data["z_us_vwap"] = _compute_us_vwap_zscore(data)
    return data


def triple_barrier_labels(df: pd.DataFrame, horizon: int = 12) -> pd.Series:
    """
    True sequential triple-barrier:
      tp_pct = 1.4 * natr/100, sl_pct = 1.2 * natr/100
      target: 0=WAIT, 1=LONG, 2=SHORT
    """
    n = len(df)
    targets = np.zeros(n, dtype=int)
    closes = df["c"].values
    highs = df["h"].values
    lows = df["l"].values
    natrs = df["natr"].values

    for i in range(n):
        natr = natrs[i]
        if np.isnan(natr) or natr <= 0:
            targets[i] = 0
            continue
        c0 = closes[i]
        tp_pct = 1.4 * (natr / 100.0)
        sl_pct = 1.2 * (natr / 100.0)
        long_tp = c0 * (1.0 + tp_pct)
        long_sl = c0 * (1.0 - sl_pct)
        short_tp = c0 * (1.0 - tp_pct)
        short_sl = c0 * (1.0 + sl_pct)

        label = 0
        end = min(n, i + horizon + 1)
        for j in range(i + 1, end):
            hi = highs[j]
            lo = lows[j]
            long_hit_tp = hi >= long_tp
            long_hit_sl = lo <= long_sl
            short_hit_tp = lo <= short_tp
            short_hit_sl = hi >= short_sl

            long_first = False
            short_first = False

            if long_hit_tp and long_hit_sl:
                # ambiguous same bar for LONG path
                pass
            elif long_hit_tp:
                long_first = True
            elif long_hit_sl:
                pass  # SL first for long → not a long win

            if short_hit_tp and short_hit_sl:
                pass
            elif short_hit_tp:
                short_first = True
            elif short_hit_sl:
                pass

            if long_first and short_first:
                label = 0  # both sides hit same bar → WAIT
                break
            if long_first:
                label = 1
                break
            if short_first:
                label = 2
                break

        targets[i] = label

    return pd.Series(targets, index=df.index, name="target")


# =========================================================================
# 3. BUILD DATASET ACROSS SYMBOLS
# =========================================================================
print("=" * 60)
print("V4 CHAMPION – fetch + features + triple-barrier")
print("=" * 60)

raw = {}
for sym in SYMBOLS:
    raw[sym] = fetch_okx_history(sym, 3500)

# Base features per symbol
featured = {}
for sym, df in raw.items():
    if len(df) < 120:
        print(f"SKIP {sym}: insufficient bars ({len(df)})")
        continue
    feat = compute_base_features(df)
    feat["symbol"] = sym
    featured[sym] = feat

if "BTC-USDT-SWAP" not in featured:
    raise RuntimeError("BTC data required for rel_btc_2h and market regime.")

btc_full = featured["BTC-USDT-SWAP"].copy()
btc = btc_full[["ts", "ret_2h", "ret_24h"]].rename(
    columns={"ret_2h": "btc_ret_2h", "ret_24h": "btc_ret_24h"}
)
# Regime X: normalizovaný 10-sviečkový lineárny sklon BTC close.
btc_close = btc_full["c"].astype(float)
btc_slope = btc_close.rolling(10, min_periods=10).apply(
    lambda values: (
        float(np.polyfit(np.arange(len(values)), values, 1)[0])
        / max(1e-9, float(values[0])) * 100.0
    ),
    raw=True,
)
btc_regime_x = np.where(btc_slope < -0.33, 1.0, np.where(btc_slope > 0.33, 3.0, 2.0))
# Regime Y: rolling 33./66. percentily BTC NATR (30d / 2880 × 15m).
btc_natr = btc_full["natr"].astype(float)
btc_p33 = btc_natr.rolling(2880, min_periods=30).quantile(0.33)
btc_p66 = btc_natr.rolling(2880, min_periods=30).quantile(0.66)
btc_regime_y = np.where(
    btc_natr <= btc_p33, 1.0,
    np.where(btc_natr <= btc_p66, 2.0, 3.0),
)
btc_regimes = pd.DataFrame({
    "ts": btc_full["ts"],
    "regime_x": pd.Series(btc_regime_x, index=btc_full.index).fillna(2.0),
    "regime_y": pd.Series(btc_regime_y, index=btc_full.index).fillna(2.0),
})

# Cross-sectional market_breadth / market_dispersion on aligned ts
# Build panel of ret_24h for all symbols
ret_panel = None
for sym, feat in featured.items():
    s = feat[["ts", "ret_24h"]].rename(columns={"ret_24h": sym})
    if ret_panel is None:
        ret_panel = s
    else:
        ret_panel = ret_panel.merge(s, on="ts", how="outer")

ret_panel = ret_panel.sort_values("ts").reset_index(drop=True)
sym_cols = [c for c in ret_panel.columns if c != "ts"]
# breadth: fraction of coins with ret_24h > 0 (ignore NaN per row)
breadth = ret_panel[sym_cols].gt(0).sum(axis=1) / ret_panel[sym_cols].notna().sum(axis=1).replace(0, np.nan)
dispersion = ret_panel[sym_cols].std(axis=1, skipna=True)
regime = pd.DataFrame(
    {
        "ts": ret_panel["ts"],
        "market_breadth": breadth.fillna(0.5),
        "market_dispersion": dispersion.fillna(2.0),
    }
)

# Assemble training rows: all non-BTC symbols (Window 6 never trades BTC)
train_frames = []
for sym, feat in featured.items():
    if sym == "BTC-USDT-SWAP":
        continue

    merged = feat.merge(btc, on="ts", how="left")
    merged = merged.merge(regime, on="ts", how="left")
    merged = merged.merge(btc_regimes, on="ts", how="left")
    merged["regime_x"] = merged["regime_x"].fillna(2.0)
    merged["regime_y"] = merged["regime_y"].fillna(2.0)
    merged["rel_btc_2h"] = merged["ret_2h"] - merged["btc_ret_2h"]
    merged["market_breadth"] = merged["market_breadth"].fillna(0.5)
    merged["market_dispersion"] = merged["market_dispersion"].fillna(2.0)

    merged["target"] = triple_barrier_labels(merged, horizon=12)

    # drop warm-up / NaN features
    use = merged.dropna(subset=FEATURES + ["target"]).copy()
    use = use.replace([np.inf, -np.inf], np.nan).dropna(subset=FEATURES)
    use["symbol"] = sym
    train_frames.append(use)
    print(f"{sym}: {len(use)} labeled rows | class counts {use['target'].value_counts().to_dict()}")

if not train_frames:
    raise RuntimeError("No training frames built.")

# =========================================================================
# 4. LEAK-FREE PER-SYMBOL CHRONOLOGICAL SPLIT
# =========================================================================
X_train_list, y_train_list = [], []
X_test_list, y_test_list = [], []

for frame in train_frames:
    frame = frame.sort_values("ts").reset_index(drop=True)
    n = len(frame)
    split = int(n * 0.80)
    if split < 50 or (n - split) < 20:
        print(f"  skip small frame {frame['symbol'].iloc[0]} n={n}")
        continue
    train_part = frame.iloc[:split]
    test_part = frame.iloc[split:]
    X_train_list.append(train_part[FEATURES])
    y_train_list.append(train_part["target"])
    X_test_list.append(test_part[FEATURES])
    y_test_list.append(test_part["target"])

X_train = pd.concat(X_train_list, ignore_index=True)
y_train = pd.concat(y_train_list, ignore_index=True)
X_test = pd.concat(X_test_list, ignore_index=True)
y_test = pd.concat(y_test_list, ignore_index=True)

print("\n" + "=" * 60)
print(f"Train rows: {len(X_train)} | Test rows: {len(X_test)}")
print(f"Train class dist: {y_train.value_counts().to_dict()}")
print(f"Test  class dist: {y_test.value_counts().to_dict()}")

# Načítanie spätnej väzby reálnych exekúcií Okna 6 z bot_vault_state.
feedback_vectors = []
feedback_targets = []
feedback_source_weights = []
feedback_count = 0

try:
    feedback_res = (
        supabase.table("bot_vault_state")
        .select("state_json")
        .eq("id", 1)
        .execute()
    )
    feedback_state = {}
    if feedback_res.data:
        feedback_state = feedback_res.data[0].get("state_json") or {}
        if isinstance(feedback_state, str):
            import json
            feedback_state = json.loads(feedback_state)

    feedback_rows = feedback_state.get("ai_w6_feedback", []) if isinstance(feedback_state, dict) else []
    if isinstance(feedback_rows, list):
        for feedback_row in feedback_rows:
            if not isinstance(feedback_row, dict):
                continue
            feedback_features = feedback_row.get("features")
            if not isinstance(feedback_features, dict) or not feedback_features:
                continue

            # Staršie feedback záznamy bez regime osí ostávajú kompatibilné.
            feedback_features = dict(feedback_features)
            feedback_features.setdefault("regime_x", 2.0)
            feedback_features.setdefault("regime_y", 2.0)
            try:
                feature_vector = [float(feedback_features[key]) for key in FEATURES]
            except (KeyError, TypeError, ValueError):
                continue

            if not np.all(np.isfinite(feature_vector)):
                continue

            is_win = feedback_row.get("is_win")
            direction = str(feedback_row.get("direction") or "").upper()
            if is_win is True and direction == "LONG":
                fb_target = 1
            elif is_win is True and direction == "SHORT":
                fb_target = 2
            elif is_win is False:
                fb_target = 0
            else:
                continue

            feedback_vectors.append(feature_vector)
            feedback_targets.append(fb_target)
            source = str(feedback_row.get("source") or "SHADOW_POOL").upper()
            if source == "W6_REAL":
                feedback_source_weights.append(3.5)
            elif source == "W15_REAL":
                feedback_source_weights.append(2.5)
            else:
                feedback_source_weights.append(1.5)
except Exception as feedback_error:
    print(f"Upozornenie: ai_w6_feedback sa nepodarilo načítať: {feedback_error}")

feedback_count = len(feedback_vectors)
print(f"ai_w6_feedback vzorky zapojené do tréningu: {feedback_count}")

if feedback_count:
    feedback_X = pd.DataFrame(feedback_vectors, columns=FEATURES)
    feedback_y = pd.Series(feedback_targets, name="target", dtype=int)
    X_train = pd.concat([X_train, feedback_X], ignore_index=True)
    y_train = pd.concat([y_train, feedback_y], ignore_index=True)

sample_weights = compute_sample_weight("balanced", y_train)
if feedback_count:
    sample_weights[-feedback_count:] = np.asarray(feedback_source_weights, dtype=float)

print("Trénujem XGBoost multi:softprob V5...")
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
print(classification_report(y_test, preds, target_names=["WAIT", "LONG", "SHORT"], digits=3))

# Trading audit: rovnaký prah 0.46 a dominancia nad WAIT ako v ai_window6.
probs = model.predict_proba(X_test)
# class order: 0=WAIT, 1=LONG, 2=SHORT
prob_wait = probs[:, 0]
prob_long = probs[:, 1]
prob_short = probs[:, 2]
y_true = y_test.values

long_mask = (prob_long >= 0.46) & (prob_long > prob_short) & (prob_long > prob_wait)
short_mask = (prob_short >= 0.46) & (prob_short > prob_long) & (prob_short > prob_wait)

n_long_sig = int(long_mask.sum())
n_short_sig = int(short_mask.sum())
long_hits = int(((y_true == 1) & long_mask).sum()) if n_long_sig else 0
short_hits = int(((y_true == 2) & short_mask).sum()) if n_short_sig else 0

long_wr = (long_hits / n_long_sig * 100.0) if n_long_sig > 0 else 0.0
short_wr = (short_hits / n_short_sig * 100.0) if n_short_sig > 0 else 0.0
n_trade_sig = n_long_sig + n_short_sig
trade_hits = long_hits + short_hits
trade_wr = (trade_hits / n_trade_sig * 100.0) if n_trade_sig > 0 else 0.0

print("\n--- Trading Audit (prob >= 0.46 and dominates WAIT) ---")
print(f"LONG  signals: {n_long_sig:4d} | hits: {long_hits:4d} | WR: {long_wr:.2f}%")
print(f"SHORT signals: {n_short_sig:4d} | hits: {short_hits:4d} | WR: {short_wr:.2f}%")
print(f"COMBINED      : {n_trade_sig:4d} | hits: {trade_hits:4d} | WR: {trade_wr:.2f}%")

# Store high-confidence WR if we have trades, else overall accuracy
accuracy_to_store = trade_wr if n_trade_sig > 0 else (acc * 100.0)

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
model_bytes = pickle.dumps(model)
model_b64 = base64.b64encode(model_bytes).decode("utf-8")

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
      f"({'high-conf trade WR' if n_trade_sig > 0 else 'overall acc'})")
print(f"features_list = {FEATURES}")
print("=" * 60)
