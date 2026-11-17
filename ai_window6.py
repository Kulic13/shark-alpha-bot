import time
import base64
import pickle
import asyncio
import httpx
import numpy as np
import pandas as pd
from supabase import Client

OKX_API = "https://okx.com"

FEATURES = [
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
]

_MODEL_CACHE = None
_FEATURES_CACHE = None
_MODEL_CACHE_TS = 0.0
_MODEL_CACHE_TTL = 1800.0  # Obnoviť model zo Supabase každých 30 minút


def _load_xgboost_v3(supabase: Client):
    """Načíta a kešuje natrénovaný XGBoost V3 model a jeho zoznam príznakov zo Supabase."""
    global _MODEL_CACHE, _FEATURES_CACHE, _MODEL_CACHE_TS
    now = time.time()
    if _MODEL_CACHE is not None and (now - _MODEL_CACHE_TS) < _MODEL_CACHE_TTL:
        return _MODEL_CACHE, _FEATURES_CACHE

    try:
        res = supabase.table("ai_models").select("model_base64, features_list").eq("id", 1).execute()
        if res.data and len(res.data) > 0 and res.data[0].get("model_base64"):
            row = res.data[0]
            raw_bytes = base64.b64decode(row["model_base64"])
            model = pickle.loads(raw_bytes)
            feats = row.get("features_list") or FEATURES
            _MODEL_CACHE = model
            _FEATURES_CACHE = list(feats)
            _MODEL_CACHE_TS = now
            return model, _FEATURES_CACHE
    except Exception:
        pass
    return _MODEL_CACHE, _FEATURES_CACHE


def _compute_market_regime_metrics(tickers_list: list) -> tuple[float, float]:
    """Vypočíta market_breadth (podiel rastúcich mincí) a market_dispersion (rozptyl zmien)."""
    changes = []
    for t in tickers_list:
        inst_id = t.get("instId", "")
        if not inst_id.endswith("-USDT-SWAP"):
            continue
        last_px = float(t.get("last") or 0.0)
        open_24h = float(t.get("open24h") or 0.0)
        if last_px > 0 and open_24h > 0:
            pct = ((last_px - open_24h) / open_24h) * 100.0
            changes.append(pct)

    if not changes:
        return 0.5, 2.0

    arr = np.asarray(changes, dtype=float)
    breadth = float(np.mean(arr > 0.0))
    dispersion = float(np.std(arr))
    return breadth, dispersion


def _compute_us_vwap_zscore(df: pd.DataFrame) -> float:
    """
    Vypočíta Z-skóre aktuálnej ceny voči objemovo váženému priemeru (VWAP)
    od začiatku aktuálnej/poslednej US obchodnej seansy (14:30 UTC).
    """
    times_utc = pd.to_datetime(df["ts"].astype(np.int64), unit="ms", utc=True)
    now_utc = times_utc.iloc[-1]

    us_open_today = now_utc.replace(hour=14, minute=30, second=0, microsecond=0)
    if now_utc < us_open_today:
        us_start = us_open_today - pd.Timedelta(days=1)
    else:
        us_start = us_open_today

    session_df = df[times_utc >= us_start]
    if len(session_df) < 3:
        session_df = df.tail(20)

    typical_px = (session_df["h"] + session_df["l"] + session_df["c"]) / 3.0
    vol = session_df["v"].replace(0, 1e-9)
    cum_vol = vol.cumsum()
    vwap_series = (typical_px * vol).cumsum() / cum_vol

    current_vwap = float(vwap_series.iloc[-1])
    std_dev = float((typical_px - vwap_series).std())
    if std_dev <= 1e-9 or np.isnan(std_dev):
        return 0.0

    z_score = (float(df["c"].iloc[-1]) - current_vwap) / std_dev
    return float(np.clip(z_score, -5.0, 5.0))


def _compute_features_v3(
    klines: list,
    btc_2h_pct: float,
    market_breadth: float,
    market_dispersion: float,
) -> dict | None:
    """Vypočíta všetkých 10 príznakov pre model xgboost_us_champion_v3."""
    if len(klines) < 25:
        return None

    df = pd.DataFrame(klines, columns=["ts", "o", "h", "l", "c", "v", "vc", "vq", "cf"])
    for col in ["o", "h", "l", "c", "v"]:
        df[col] = df[col].astype(float)

    prev_c = df["c"].shift(1)
    tr = pd.concat([df["h"] - df["l"], (df["h"] - prev_c).abs(), (df["l"] - prev_c).abs()], axis=1).max(axis=1)
    atr_14 = tr.rolling(14).mean()
    df["natr"] = (atr_14 / df["c"]) * 100.0

    delta = df["c"].diff()
    gain = delta.clip(lower=0).rolling(14).mean()
    loss = (-delta.clip(upper=0)).rolling(14).mean()
    rs = gain / loss.replace(0, 1e-9)
    df["rsi"] = 100.0 - (100.0 / (1.0 + rs))

    rng = (df["h"] - df["l"]).replace(0, 1e-9)
    body_frac = (df["c"] - df["o"]).abs() / rng
    buyer_ratio = np.where(df["c"] >= df["o"], 0.5 + 0.5 * body_frac, 0.5 - 0.5 * body_frac)
    buy_vol = (df["v"] * buyer_ratio).rolling(5).sum()
    sell_vol = (df["v"] * (1.0 - buyer_ratio)).rolling(5).sum().replace(0, 1e-9)
    df["ofd"] = buy_vol / sell_vol

    c_ratio = (df["c"] - df["l"]) / rng
    b_v = df["v"] * c_ratio
    s_v = df["v"] * (1.0 - c_ratio)
    df["vpin"] = (b_v - s_v).abs().rolling(10).sum() / (b_v + s_v).rolling(10).sum().replace(0, 1e-9)

    df["vol_ratio"] = df["v"] / df["v"].rolling(10).mean().replace(0, 1e-9)

    ema_20 = df["c"].ewm(span=20, adjust=False).mean()
    df["ema_dist_atr"] = (df["c"] - ema_20) / atr_14.replace(0, 1e-9)

    # 2-hodinová relatívna výkonnosť voči BTC (8 × 15m sviečok)
    c_now = float(df["c"].iloc[-1])
    c_2h_ago = float(df["c"].iloc[-9]) if len(df) >= 9 else float(df["c"].iloc[0])
    coin_2h_pct = ((c_now - c_2h_ago) / max(1e-9, c_2h_ago)) * 100.0
    rel_btc_2h = coin_2h_pct - btc_2h_pct

    z_us_vwap = _compute_us_vwap_zscore(df)

    last = df.iloc[-1]
    result = {
        "natr": float(last["natr"]),
        "rsi": float(last["rsi"]),
        "ofd": float(last["ofd"]),
        "vpin": float(last["vpin"]),
        "vol_ratio": float(last["vol_ratio"]),
        "ema_dist_atr": float(last["ema_dist_atr"]),
        "z_us_vwap": float(z_us_vwap),
        "rel_btc_2h": float(rel_btc_2h),
        "market_breadth": float(market_breadth),
        "market_dispersion": float(market_dispersion),
    }

    if any(np.isnan(v) or np.isinf(v) for v in result.values()):
        return None
    return result


async def scan_ai_champion_window6(
    supabase: Client,
    tickers_list: list,
    excluded_symbols: set,
    min_confidence: float = 0.46,
) -> dict | None:
    """
    Asynchrónny AI skener pre Okno 6 (XGBoost V3).
    Vyhodnotí top 20 likvidných párov s využitím US VWAP a korelácie voči BTC.
    """
    model, feature_order = _load_xgboost_v3(supabase)
    if model is None or not feature_order:
        return None

    market_breadth, market_dispersion = _compute_market_regime_metrics(tickers_list)

    candidates = []
    for t in tickers_list:
        inst_id = t.get("instId", "")
        if not inst_id.endswith("-USDT-SWAP"):
            continue
        sym = f"{inst_id.split('-')[0]}USDT"
        if sym == "BTCUSDT" or sym in excluded_symbols:
            continue
        vol = float(t.get("volCcy24h") or 0.0)
        price = float(t.get("last") or 0.0)
        if vol >= 1_000_000.0 and price > 0:
            candidates.append((sym, inst_id, vol, price))

    candidates.sort(key=lambda x: x[2], reverse=True)
    top_candidates = candidates[:20]
    if not top_candidates:
        return None

    sem = asyncio.Semaphore(6)
    best_pick = None

    async with httpx.AsyncClient(http2=True, timeout=8.0) as client:
        # Najprv stiahneme 15m sviečky BTC pre výpočet rel_btc_2h
        btc_2h_pct = 0.0
        try:
            btc_res = await client.get(
                f"{OKX_API}/api/v5/market/candles",
                params={"instId": "BTC-USDT-SWAP", "bar": "15m", "limit": "12"}
            )
            if btc_res.status_code == 200:
                b_data = btc_res.json().get("data", [])
                if len(b_data) >= 9:
                    b_klines = list(reversed(b_data))
                    b_now = float(b_klines[-1][4])
                    b_2h = float(b_klines[-9][4])
                    btc_2h_pct = ((b_now - b_2h) / max(1e-9, b_2h)) * 100.0
        except Exception:
            pass

        async def evaluate_one(item):
            sym, inst_id, vol, price = item
            async with sem:
                try:
                    res = await client.get(
                        f"{OKX_API}/api/v5/market/candles",
                        params={"instId": inst_id, "bar": "15m", "limit": "60"}
                    )
                    if res.status_code != 200:
                        return None
                    payload = res.json()
                    if payload.get("code") != "0" or not payload.get("data"):
                        return None

                    klines = list(reversed(payload["data"]))
                    feats = _compute_features_v3(klines, btc_2h_pct, market_breadth, market_dispersion)
                    if feats is None:
                        return None

                    # Symetrická bezpečnostná brána kvality trhu pre LONG aj SHORT.
                    if feats["natr"] < 0.25 or feats["natr"] > 7.5:
                        return None
                    if feats["vpin"] > 0.85:
                        return None

                    X_row = pd.DataFrame([[feats.get(k, 0.0) for k in feature_order]], columns=feature_order)

                    # Predikcia pravdepodobností: [0 = WAIT, 1 = LONG, 2 = SHORT]
                    probs = model.predict_proba(X_row)[0]
                    prob_wait = float(probs[0])
                    prob_long = float(probs[1])
                    prob_short = float(probs[2])

                    direction = None
                    conf = 0.0
                    if prob_long >= min_confidence and prob_long > prob_short and prob_long > prob_wait:
                        direction = "LONG"
                        conf = prob_long
                    elif prob_short >= min_confidence and prob_short > prob_long and prob_short > prob_wait:
                        direction = "SHORT"
                        conf = prob_short

                    if direction:
                        return {
                            "symbol": sym,
                            "direction": direction,
                            "price": price,
                            "ai_confidence": round(conf, 4),
                            "ofd": feats["ofd"],
                            "rsi": feats["rsi"],
                            "vpin": feats["vpin"],
                            "ai_features": dict(feats),
                            "source": "AI_CHAMPION_US_V3",
                        }
                except Exception:
                    return None
                return None

        results = await asyncio.gather(*(evaluate_one(c) for c in top_candidates))
        valid = [r for r in results if r is not None]
        if valid:
            valid.sort(key=lambda x: x["ai_confidence"], reverse=True)
            best_pick = valid[0]
        return best_pick
