import time
import asyncio
import httpx
import numpy as np
import pandas as pd
from datetime import datetime, timezone
from us_market_engine import (
    AI_FEATURES,
    is_us_trading_window,
    attach_institutional_context,
    load_cloud_ai_model
)

OKX_API = "https://okx.com"
MIN_HISTORY_BARS = 1400
MIN_VOL_24H_USD = 10_000_000.0
AI_CONFIDENCE_THRESHOLD = 0.65


async def _fetch_candles_async(client: httpx.AsyncClient, symbol: str, limit: int = 100) -> pd.DataFrame | None:
    """Rýchlo stiahne posledných 100 uzavretých 15m sviečok pre výpočet US VWAP a indikátorov."""
    inst_id = symbol.replace("USDT", "-USDT-SWAP")
    try:
        res = await client.get(
            f"{OKX_API}/api/v5/market/candles",
            params={"instId": inst_id, "bar": "15m", "limit": str(limit)}
        )
        if res.status_code != 200:
            return None
        data = res.json().get("data", [])
        if not data or len(data) < 35:
            return None
        # Zoradíme od najstaršej po najnovšiu (vynecháme poslednú neuzavretú sviečku)
        closed_rows = list(reversed(data))[:-1]
        df = pd.DataFrame(closed_rows, columns=["ts", "o", "h", "l", "c", "v", "vc", "vq", "cf"])
        return df[["ts", "o", "h", "l", "c", "v"]]
    except Exception:
        return None


async def _verify_min_history(client: httpx.AsyncClient, symbol: str) -> bool:
    """Anti-listing poistka: overí, či minca existuje na OKX aspoň ~14.6 dňa (1400 sviečok)."""
    inst_id = symbol.replace("USDT", "-USDT-SWAP")
    target_past_ms = int((time.time() - (MIN_HISTORY_BARS * 15 * 60)) * 1000)
    try:
        res = await client.get(
            f"{OKX_API}/api/v5/market/candles",
            params={"instId": inst_id, "bar": "15m", "after": str(target_past_ms), "limit": "5"}
        )
        data = res.json().get("data", [])
        return len(data) > 0
    except Exception:
        return False


async def scan_ai_champion_window6(supabase_client, tickers: list, active_syms: set) -> dict | None:
    """
    Hlavný motor pre Okno 6 (AI Lovec Šampiónov):
    1. Povolí hľadanie iba v Zlatom okne US seansy (15:00 – 20:30 UTC, Po–Pia).
    2. Načíta cloudový model zo Supabase.
    3. Vyberie top likvidné altcoiny, overí anti-listing limit a spočíta inštitucionálny kontext.
    4. Vyberie absolútneho Šampióna s AI istotou >= 65 %.
    """
    now_utc = datetime.now(timezone.utc)
    if not is_us_trading_window(now_utc):
        return None

    model, cached_thresh = load_cloud_ai_model(supabase_client)
    if model is None:
        return None

    threshold = max(AI_CONFIDENCE_THRESHOLD, float(cached_thresh))

    raw_candidates = []
    for it in tickers:
        inst_id = it.get("instId", "")
        if not inst_id.endswith("-USDT-SWAP") or inst_id == "BTC-USDT-SWAP":
            continue
        sym = inst_id.replace("-USDT-SWAP", "USDT")
        if sym in active_syms:
            continue
        vol_usd = float(it.get("volCcy24h") or 0.0)
        last_px = float(it.get("last") or 0.0)
        open_24h = float(it.get("open24h") or last_px)
        if vol_usd >= MIN_VOL_24H_USD and last_px > 0:
            pct_24h = abs((last_px - open_24h) / max(1e-9, open_24h)) * 100.0
            raw_candidates.append({
                "sym": sym,
                "vol_usd": vol_usd,
                "pct_24h": pct_24h,
                "last_px": last_px
            })

    if not raw_candidates:
        return None

    # Zoradíme podľa kombinácie likvidity a pohybu, vezmeme top 15 do hĺbkového AI skenu
    raw_candidates.sort(key=lambda x: (x["vol_usd"] * (1.0 + x["pct_24h"] / 10.0)), reverse=True)
    top_pool = raw_candidates[:15]

    headers = {"User-Agent": "SharkAlphaWindow6/3.0"}
    async with httpx.AsyncClient(headers=headers, timeout=8.0) as client:
        tasks = [_fetch_candles_async(client, "BTCUSDT", 100)]
        for item in top_pool:
            tasks.append(_fetch_candles_async(client, item["sym"], 100))

        results = await asyncio.gather(*tasks, return_exceptions=True)

        btc_df = results.pop(0)
        if isinstance(btc_df, Exception) or btc_df is None or btc_df.empty:
            return None

        coins_dict = {}
        price_map = {}
        for item, res_df in zip(top_pool, results):
            sym = item["sym"]
            if not isinstance(res_df, Exception) and res_df is not None and not res_df.empty:
                coins_dict[sym] = res_df
                price_map[sym] = item["last_px"]

        if not coins_dict:
            return None

        # Výpočet spoločného inštitucionálneho kontextu (US VWAP, Breadth, Dispersion)
        enriched_dict = attach_institutional_context(coins_dict, btc_df)
        if not enriched_dict:
            return None

        best_champion = None
        best_prob = 0.0

        for sym, df in enriched_dict.items():
            last_row = df.iloc[-1]
            if any(pd.isna(last_row[f]) for f in AI_FEATURES):
                continue

            X_live = pd.DataFrame([last_row[AI_FEATURES].to_dict()])
            probs_row = model.predict_proba(X_live).tolist().pop(0)
            # Index 0 = WAIT, Index 1 = LONG, Index 2 = SHORT
            prob_long = float(probs_row.pop(1))
            prob_short = float(probs_row.pop(1))

            if prob_long >= threshold and prob_long > best_prob:
                best_prob = prob_long
                best_champion = (sym, "LONG", prob_long, last_row, price_map[sym])
            elif prob_short >= threshold and prob_short > best_prob:
                best_prob = prob_short
                best_champion = (sym, "SHORT", prob_short, last_row, price_map[sym])

        if best_champion is None:
            return None

        champ_sym, direction, conf_prob, feat_row, live_price = best_champion

        # Finálna anti-listing kontrola iba pre víťazného Šampióna
        is_mature = await _verify_min_history(client, champ_sym)
        if not is_mature:
            return None

        return {
            "symbol": champ_sym,
            "direction": direction,
            "price": live_price,
            "natr": float(feat_row["natr"]),
            "rsi": float(feat_row["rsi"]),
            "ofd": float(feat_row["ofd"]),
            "vpin": float(feat_row["vpin"]),
            "ai_confidence": round(conf_prob * 100.0, 1),
            "source": "AI_CHAMPION_US_V3"
        }
