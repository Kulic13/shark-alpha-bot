import time
import base64
import pickle
import asyncio
import httpx
import numpy as np
import pandas as pd
from supabase import Client

from us_market_engine import AI_FEATURES, compute_live_coin_features, is_us_trading_window
from us_market_engine import compute_single_coin_features

OKX_API = "https://okx.com"

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
            feats = row.get("features_list") or list(AI_FEATURES)
            _MODEL_CACHE = model
            _FEATURES_CACHE = list(feats)
            _MODEL_CACHE_TS = now
            return model, _FEATURES_CACHE
    except Exception:
        pass
    return _MODEL_CACHE, _FEATURES_CACHE


def _btc_return_2h(btc_klines: list | None) -> float:
    """2h výnos BTC (v %) z 15m sviečok (8 × 15m); pri nedostatku dát vráti 0.0."""
    if not btc_klines or len(btc_klines) < 9:
        return 0.0
    try:
        b_now = float(btc_klines[-1][4])
        b_2h = float(btc_klines[-9][4])
        return ((b_now - b_2h) / max(1e-9, b_2h)) * 100.0
    except Exception:
        return 0.0


def _preliminary_cross_section(klines: list | None) -> tuple[float, float] | None:
    """
    Predbežný z_us_vwap a ret_2h jedného coinu cez compute_single_coin_features
    z us_market_engine (rovnaký pipeline ako v trénerovi). Berie posledný riadok.
    """
    if not klines or len(klines) < 25:
        return None
    try:
        df = pd.DataFrame(klines, columns=["ts", "o", "h", "l", "c", "v", "vc", "vq", "cf"])
        feat_df = compute_single_coin_features(df[["ts", "o", "h", "l", "c", "v"]])
        last = feat_df.iloc[-1]
        z = float(last["z_us_vwap"])
        r = float(last["ret_2h"])
    except Exception:
        return None
    if np.isnan(z) or np.isinf(z) or np.isnan(r) or np.isinf(r):
        return None
    return z, r


async def scan_ai_champion_window6(
    supabase: Client,
    tickers_list: list,
    excluded_symbols: set,
    min_confidence: float = 0.55,
) -> dict | None:
    """
    Asynchrónny AI skener pre Okno 6 (XGBoost V3).
    Vyhodnotí top 20 likvidných párov s využitím US VWAP a korelácie voči BTC.
    Obchoduje výhradne počas US seansy (is_us_trading_window).
    """
    if not is_us_trading_window():
        return None

    model, feature_order = _load_xgboost_v3(supabase)
    if model is None or not feature_order:
        return None

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

    async with httpx.AsyncClient(http2=True, timeout=8.0) as client:

        async def fetch_klines(inst_id: str) -> list | None:
            async with sem:
                try:
                    res = await client.get(
                        f"{OKX_API}/api/v5/market/candles",
                        params={"instId": inst_id, "bar": "15m", "limit": "60"},
                    )
                    if res.status_code != 200:
                        return None
                    payload = res.json()
                    if payload.get("code") != "0" or not payload.get("data"):
                        return None
                    return list(reversed(payload["data"]))
                except Exception:
                    return None

        # FÁZA 1: 15m sviečky (limit=60) pre BTCUSDT + všetkých kandidátov naraz
        fetch_ids = ["BTC-USDT-SWAP"] + [c[1] for c in top_candidates]
        fetched = await asyncio.gather(*(fetch_klines(i) for i in fetch_ids))
        btc_klines = fetched[0]
        coin_klines = fetched[1:]

        btc_ret_2h = _btc_return_2h(btc_klines)

        # FÁZA 2: prierezové metriky zhodné s trénerom (attach_institutional_context)
        z_list: list[float] = []
        ret_2h_list: list[float] = []
        for klines in coin_klines:
            pre = _preliminary_cross_section(klines)
            if pre is None:
                continue
            z_list.append(pre[0])
            ret_2h_list.append(pre[1])

        if not z_list:
            return None

        market_breadth = float(np.mean([z > 0 for z in z_list]))
        # ddof=1 = rovnaká definícia ako pandas .std() v trénerovi
        market_dispersion = float(np.std(ret_2h_list, ddof=1)) if len(ret_2h_list) > 1 else 0.0

        # FÁZA 3: inferencia pre každého kandidáta
        results = []
        for item, klines in zip(top_candidates, coin_klines):
            sym, inst_id, vol, price = item
            if not klines:
                continue
            try:
                feats = compute_live_coin_features(klines, btc_ret_2h, market_breadth, market_dispersion)
                if feats is None:
                    continue

                X_row = pd.DataFrame([[feats.get(k, 0.0) for k in feature_order]], columns=feature_order)

                # Predikcia pravdepodobností: [0 = WAIT, 1 = LONG, 2 = SHORT]
                probs = model.predict_proba(X_row)[0]
                prob_long = float(probs[1])
                prob_short = float(probs[2])

                direction = None
                conf = 0.0
                if prob_long >= min_confidence and prob_long > prob_short:
                    direction = "LONG"
                    conf = prob_long
                elif prob_short >= min_confidence and prob_short > prob_long:
                    direction = "SHORT"
                    conf = prob_short

                if direction:
                    results.append({
                        "symbol": sym,
                        "direction": direction,
                        "price": price,
                        "ai_confidence": round(conf, 4),
                        "ofd": feats["ofd"],
                        "rsi": feats["rsi"],
                        "vpin": feats["vpin"],
                        "source": "AI_CHAMPION_US_V3",
                        "ai_features": dict(feats),
                    })
            except Exception:
                continue

        if not results:
            return None
        results.sort(key=lambda x: x["ai_confidence"], reverse=True)
        return results[0]
