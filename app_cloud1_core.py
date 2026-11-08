import streamlit as st
import httpx
import asyncio
import json
import os
import time
import operator
import numpy as np
import pandas as pd
from datetime import datetime, timezone, timedelta
from supabase import create_client, Client

st.set_page_config(page_title="Shark Alpha Core (Cloud 1 - OKX)", layout="centered")

# =========================================================================
# BLOK 1: INFRAŠTRUKTÚRA, CLOUD TREZOR (SUPABASE) & OKX API KONFIGURÁCIA
# =========================================================================
VAULT_FILE = "vault_state_backup.json"
OKX_API = "https://www.okx.com"

get_open = operator.itemgetter(1)
get_high = operator.itemgetter(2)
get_low = operator.itemgetter(3)
get_close = operator.itemgetter(4)
get_vol = operator.itemgetter(5)

def to_okx_inst(symbol: str) -> str:
    if symbol.endswith("-SWAP"):
        return symbol
    base = symbol.replace("USDT", "")
    return f"{base}-USDT-SWAP"

def from_okx_inst(inst_id: str) -> str:
    return inst_id.replace("-USDT-SWAP", "USDT")

@st.cache_resource
def init_supabase() -> Client:
    url = st.secrets["SUPABASE_URL"].strip().rstrip("/")
    key = st.secrets["SUPABASE_KEY"].strip()
    return create_client(url, key)

supabase = init_supabase()

def get_default_vault() -> dict:
    return {
        "balance": 100.0,
        "windows": {
            "1": {"status": "WAITING", "symbol": "BTCUSDT", "mandate_until": 0},
            "2": {"status": "WAITING", "symbol": None, "mandate_until": 0},
            "3": {"status": "WAITING", "symbol": None, "mandate_until": 0},
            "4": {"status": "WAITING", "symbol": None, "mandate_until": 0}
        },
        "last_market_scan": 0,
        "pending_supabase_sync": list()
    }

def load_vault() -> dict:
    try:
        res = supabase.table("bot_vault_state").select("state_json").eq("id", 1).execute()
        if res.data and res.data[0].get("state_json"):
            state = res.data[0]["state_json"]
            if "last_market_scan" not in state:
                state["last_market_scan"] = 0
            if "pending_supabase_sync" not in state:
                state["pending_supabase_sync"] = list()
            return state
    except Exception as e:
        st.warning(f"Upozornenie: Čítanie trezoru zo Supabase zlyhalo ({e}), skúšam lokálnu zálohu.")

    if os.path.exists(VAULT_FILE):
        try:
            with open(VAULT_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass

    return get_default_vault()

def save_vault(state: dict):
    try:
        tmp_file = f"{VAULT_FILE}.tmp"
        with open(tmp_file, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
        os.replace(tmp_file, VAULT_FILE)
    except Exception:
        pass

    try:
        supabase.table("bot_vault_state").upsert({
            "id": 1,
            "state_json": state,
            "updated_at": datetime.now(timezone.utc).isoformat()
        }).execute()
    except Exception as e:
        st.error(f"Chyba ukladania trezoru do Supabase: {e}")

def safe_supabase_upsert(table: str, data: dict, vault: dict):
    try:
        supabase.table(table).upsert(data).execute()
    except Exception:
        vault["pending_supabase_sync"].append({"table": table, "op": "upsert", "data": data})

def flush_pending_sync(vault: dict):
    if not vault.get("pending_supabase_sync"):
        return
    remaining = list()
    for item in vault["pending_supabase_sync"]:
        try:
            if item["op"] == "upsert":
                supabase.table(item["table"]).upsert(item["data"]).execute()
            elif item["op"] == "insert":
                supabase.table(item["table"]).insert(item["data"]).execute()
        except Exception:
            remaining.append(item)
    vault["pending_supabase_sync"] = remaining

# =========================================================================
# BLOK 5: ADAPTÍVNA AI MATICA UČENIA (Smerové WR, Modifikátory, Decay 0.85)
# =========================================================================
def get_ai_card(symbol: str) -> dict:
    default_card = {
        "symbol": symbol,
        "long_futures_wr": 50.0,
        "short_futures_wr": 50.0,
        "spot_regime_wr": 50.0,
        "funding_reversal_wr": 50.0,
        "avg_win_r": 1.5,
        "avg_loss_r": 1.0,
        "sl_modifier": 1.00,
        "tp_modifier": 1.00,
        "total_trades": 0
    }
    try:
        res = supabase.table("ai_learning_matrix").select("*").eq("symbol", symbol).execute()
        if res.data:
            return res.data[0]
        supabase.table("ai_learning_matrix").insert(default_card).execute()
    except Exception:
        pass
    return default_card

def update_ai_learning(symbol: str, direction: str, regime: str, is_funding_trap: bool, is_win: bool, r_multiple: float, vault: dict):
    card = get_ai_card(symbol)
    decay = 0.85
    outcome = 100.0 if is_win else 0.0

    if is_funding_trap:
        card["funding_reversal_wr"] = (card["funding_reversal_wr"] * decay) + (outcome * (1 - decay))
    elif regime == "SPOT":
        card["spot_regime_wr"] = (card["spot_regime_wr"] * decay) + (outcome * (1 - decay))
    elif direction == "LONG":
        card["long_futures_wr"] = (card["long_futures_wr"] * decay) + (outcome * (1 - decay))
    else:
        card["short_futures_wr"] = (card["short_futures_wr"] * decay) + (outcome * (1 - decay))

    if is_win:
        card["avg_win_r"] = (card["avg_win_r"] * decay) + (abs(r_multiple) * (1 - decay))
        card["sl_modifier"] = float(np.clip(card["sl_modifier"] * 0.97, 0.75, 1.45))
        card["tp_modifier"] = float(np.clip(card["tp_modifier"] * 1.02, 0.75, 1.45))
    else:
        card["avg_loss_r"] = (card["avg_loss_r"] * decay) + (abs(r_multiple) * (1 - decay))
        card["sl_modifier"] = float(np.clip(card["sl_modifier"] * 1.06, 0.75, 1.45))
        card["tp_modifier"] = float(np.clip(card["tp_modifier"] * 0.98, 0.75, 1.45))

    card["total_trades"] += 1
    card["last_update"] = datetime.now(timezone.utc).isoformat()
    safe_supabase_upsert("ai_learning_matrix", card, vault)

# =========================================================================
# BLOK 3: OKX DATA FEED, OBOJSMERNÝ BTC VETO, ANTI-FOMO & 3-MIN SKENER
# =========================================================================
async def fetch_market_snapshot(client: httpx.AsyncClient):
    t_res, oi_res = await asyncio.gather(
        client.get(f"{OKX_API}/api/v5/market/tickers", params={"instType": "SWAP"}),
        client.get(f"{OKX_API}/api/v5/public/open-interest", params={"instType": "SWAP"})
    )
    t_res.raise_for_status()
    oi_res.raise_for_status()

    t_json = t_res.json().get("data", [])
    oi_json = oi_res.json().get("data", [])

    tickers = {}
    for x in t_json:
        inst_id = x.get("instId", "")
        if not inst_id.endswith("-USDT-SWAP"):
            continue
        sym = from_okx_inst(inst_id)
        last_p = float(x.get("last") or 0)
        open_24h = float(x.get("open24h") or last_p)
        vol_usdt = float(x.get("volCcy24h") or 0)
        chg_pct = ((last_p - open_24h) / open_24h * 100.0) if open_24h > 0 else 0.0

        tickers[sym] = {
            "symbol": sym,
            "instId": inst_id,
            "lastPrice": str(last_p),
            "quoteVolume": str(vol_usdt),
            "priceChangePercent": str(chg_pct)
        }

    oi_map = {}
    for x in oi_json:
        inst_id = x.get("instId", "")
        if inst_id.endswith("-USDT-SWAP"):
            sym = from_okx_inst(inst_id)
            oi_map[sym] = float(x.get("oiCcy") or x.get("oi") or 0)

    return tickers, oi_map

async def fetch_okx_klines(client: httpx.AsyncClient, symbol: str, bar: str = "15m", limit: int = 24, cache: dict | None = None) -> list:
    cache_key = f"{symbol}_{bar}_{limit}"
    if cache is not None and cache_key in cache:
        return cache[cache_key]

    inst_id = to_okx_inst(symbol)
    try:
        res = await client.get(f"{OKX_API}/api/v5/market/candles", params={"instId": inst_id, "bar": bar, "limit": str(limit)})
        if res.status_code != 200:
            return []
        raw = res.json().get("data", [])
        # OKX vracia sviečky od najnovšej po najstaršiu -> otočíme na chronologické poradie
        klines = list(reversed(raw))
        if cache is not None:
            cache[cache_key] = klines
        return klines
    except Exception:
        return []

async def fetch_okx_funding(client: httpx.AsyncClient, symbol: str) -> float:
    inst_id = to_okx_inst(symbol)
    try:
        res = await client.get(f"{OKX_API}/api/v5/public/funding-rate", params={"instId": inst_id})
        if res.status_code != 200:
            return 0.0
        data = res.json().get("data", [])
        if data:
            return float(data[0].get("fundingRate") or 0.0)
    except Exception:
        pass
    return 0.0

def compute_ofd_from_klines(klines: list) -> float:
    if not klines:
        return 1.0
    buy_vol = 0.0
    sell_vol = 0.0
    for k in klines[-5:]:
        o = float(get_open(k))
        h = float(get_high(k))
        l = float(get_low(k))
        c = float(get_close(k))
        v = float(get_vol(k))
        rng = max(1e-9, h - l)
        buy_ratio = np.clip((c - l) / rng, 0.05, 0.95)
        buy_vol += v * buy_ratio
        sell_vol += v * (1.0 - buy_ratio)
    return float(buy_vol / max(1e-9, sell_vol))

def evaluate_btc_veto(btc_klines: list) -> dict:
    """
    Obojsmerný BTC Veto Filter:
    - Blokuje LONG altcoiny pri prudkom páde BTC (1h < -0.80 % alebo 15m < -0.45 %).
    - Blokuje SHORT altcoiny pri prudkom raste BTC (1h > +0.80 % alebo 15m > +0.45 %).
    """
    status = {
        "veto_long": False,
        "veto_short": False,
        "chg_15m": 0.0,
        "chg_1h": 0.0,
        "reason": "OK"
    }
    if len(btc_klines) < 5:
        return status

    last_c = float(get_close(btc_klines[-1]))
    last_o = float(get_open(btc_klines[-1]))
    open_1h = float(get_open(btc_klines[-4]))

    chg_15m = ((last_c - last_o) / last_o * 100.0) if last_o > 0 else 0.0
    chg_1h = ((last_c - open_1h) / open_1h * 100.0) if open_1h > 0 else 0.0

    status["chg_15m"] = round(chg_15m, 3)
    status["chg_1h"] = round(chg_1h, 3)

    reasons = []
    if chg_1h < -0.80 or chg_15m < -0.45:
        status["veto_long"] = True
        reasons.append(f"BTC DUMP (1h: {chg_1h:+.2f}%, 15m: {chg_15m:+.2f}%) -> LONG BLOCKED")

    if chg_1h > 0.80 or chg_15m > 0.45:
        status["veto_short"] = True
        reasons.append(f"BTC PUMP (1h: {chg_1h:+.2f}%, 15m: {chg_15m:+.2f}%) -> SHORT BLOCKED")

    if reasons:
        status["reason"] = " | ".join(reasons)
    return status

def check_anti_fomo_ema20(klines: list, direction: str, is_pd_mode: bool = False) -> bool:
    """
    Anti-FOMO Filter (EMA-20 + ATR vzdialenosť):
    - Zabraňuje nákupu na vyčerpanom vrchole (LONG) alebo predaju na dne (SHORT).
    - Štandardný limit: max 1.5x ATR od EMA-20 (pre Pump & Dump voľnejší limit 2.5x ATR).
    """
    if len(klines) < 20:
        return False

    closes = pd.Series([float(get_close(k)) for k in klines])
    highs = pd.Series([float(get_high(k)) for k in klines])
    lows = pd.Series([float(get_low(k)) for k in klines])

    ema_20 = closes.ewm(span=20, adjust=False).mean().iloc[-1]
    prev_c = closes.shift(1)
    tr = pd.concat((highs - lows, (highs - prev_c).abs(), (lows - prev_c).abs()), axis=1).max(axis=1)
    atr_14 = tr.rolling(14).mean().iloc[-1]
    last_close = closes.iloc[-1]

    if pd.isna(atr_14) or atr_14 <= 0:
        return False

    max_stretch = 2.5 if is_pd_mode else 1.5
    if direction == "LONG":
        return last_close <= (ema_20 + max_stretch * atr_14)
    else:
        return last_close >= (ema_20 - max_stretch * atr_14)

def get_sectors_map() -> dict:
    try:
        res = supabase.table("coin_sectors").select("*").execute()
        return {r["symbol"]: r["sector"] for r in res.data}
    except Exception:
        return {}

def get_24h_metrics() -> dict:
    try:
        res = supabase.table("market_metrics_24h").select("*").execute()
        return {r["symbol"]: r for r in res.data}
    except Exception:
        return {}

async def evaluate_candidates(
    client: httpx.AsyncClient,
    tickers: dict,
    oi_map: dict,
    btc_veto: dict,
    klines_cache: dict,
    excluded_symbols: set,
    excluded_sectors: set,
    min_vol: float,
    max_vol: float,
    is_pd_mode: bool = False
):
    sectors_map = get_sectors_map()
    metrics_24h = get_24h_metrics()
    pre_filtered = list()

    for sym, t in tickers.items():
        if sym == "BTCUSDT" or sym in excluded_symbols:
            continue
        vol = float(t["quoteVolume"])
        if not (min_vol <= vol <= max_vol):
            continue
        sec = sectors_map.get(sym, "UNKNOWN")
        if sec != "UNKNOWN" and sec in excluded_sectors:
            continue

        price = float(t["lastPrice"])
        if price <= 0:
            continue

        price_change_pct = float(t["priceChangePercent"])
        pre_filtered.append((sym, vol, sec, price, price_change_pct))

    sem = asyncio.Semaphore(10)

    async def process_symbol(item):
        sym, vol, sec, price, price_change_pct = item
        async with sem:
            try:
                klines = await fetch_okx_klines(client, sym, "15m", 24, klines_cache)
                if len(klines) < 20:
                    return None

                ofd = compute_ofd_from_klines(klines)
                last_candle = klines[-1]
                last_open = float(get_open(last_candle))
                last_close = float(get_close(last_candle))
                is_15m_green = last_close > last_open

                if is_pd_mode:
                    oi_curr = oi_map.get(sym, 0.0) * price
                    oi_avg = metrics_24h.get(sym, {}).get("oi_avg_24h", 0.0)
                    if oi_avg <= 0:
                        oi_avg = oi_curr
                    oi_ratio = oi_curr / max(1.0, oi_avg)

                    if oi_ratio > 1.80 and ofd > 1.60 and is_15m_green:
                        if btc_veto["veto_long"] or not check_anti_fomo_ema20(klines, "LONG", True):
                            return None
                        sas_score = oi_ratio * ofd * 100
                        return {"symbol": sym, "direction": "LONG", "score": sas_score, "is_funding_trap": False, "is_pd": True, "sector": sec, "ofd": ofd, "klines": klines}
                    elif price_change_pct > 15.0 and ofd < 0.80 and not is_15m_green:
                        if btc_veto["veto_short"] or not check_anti_fomo_ema20(klines, "SHORT", True):
                            return None
                        funding_rate = await fetch_okx_funding(client, sym)
                        if funding_rate > 0.0020:
                            sas_score = abs(price_change_pct) * (1 / max(0.1, ofd)) * 50
                            return {"symbol": sym, "direction": "SHORT", "score": sas_score, "is_funding_trap": True, "is_pd": True, "sector": sec, "ofd": ofd, "klines": klines}
                    return None

                direction = None
                is_trap = False

                if ofd > 1.30 and is_15m_green:
                    direction = "LONG"
                elif ofd < 0.70 and not is_15m_green:
                    direction = "SHORT"
                elif not is_15m_green and ofd < 0.90:
                    funding_rate = await fetch_okx_funding(client, sym)
                    if funding_rate > 0.0020:
                        direction = "SHORT"
                        is_trap = True
                elif is_15m_green and ofd > 1.15:
                    funding_rate = await fetch_okx_funding(client, sym)
                    if funding_rate < -0.0050:
                        direction = "LONG"
                        is_trap = True

                if direction:
                    # 1. Obojsmerný BTC Veto Filter
                    if direction == "LONG" and btc_veto["veto_long"]:
                        return None
                    if direction == "SHORT" and btc_veto["veto_short"]:
                        return None

                    # 2. Anti-FOMO EMA-20 Filter
                    if not check_anti_fomo_ema20(klines, direction, False):
                        return None

                    vol_norm = np.log1p(vol)
                    sas_score = (abs(price_change_pct) + 1.0) * (ofd if direction == "LONG" else (1.0 / max(0.1, ofd))) * (10.0 / vol_norm)
                    return {"symbol": sym, "direction": direction, "score": sas_score, "is_funding_trap": is_trap, "is_pd": False, "sector": sec, "ofd": ofd, "klines": klines}
            except Exception:
                return None
        return None

    results = await asyncio.gather(*(process_symbol(item) for item in pre_filtered))
    candidates = [r for r in results if r is not None]
    candidates.sort(key=lambda x: x["score"], reverse=True)
    return candidates

# =========================================================================
# BLOK 4: INTEGROVANÝ RISK ENGINE, KELLY, NATR ŠTÍT & ORDER FLOW TSL
# =========================================================================
def calculate_natr_14(klines: list) -> float:
    df = pd.DataFrame(klines, columns=["ts", "o", "h", "l", "c", "v", "vc", "vq", "cf"])
    for col in ("h", "l", "c"):
        df[col] = df[col].astype(float)
    prev_c = df["c"].shift(1)
    tr = pd.concat((df["h"] - df["l"], (df["h"] - prev_c).abs(), (df["l"] - prev_c).abs()), axis=1).max(axis=1)
    atr_14 = tr.rolling(14).mean().iloc[-1]
    last_close = df["c"].iloc[-1]
    return float((atr_14 / last_close) * 100.0)

def build_trade_setup(cand: dict, balance: float, window_id: int) -> dict | None:
    sym = cand["symbol"]
    direction = cand["direction"]
    is_pd = cand["is_pd"]
    klines = cand["klines"]
    last_candle = klines[-1]
    price = float(get_close(last_candle))
    natr = calculate_natr_14(klines)

    max_natr = 9.0 if is_pd else 5.5
    if natr > max_natr or natr < 0.3:
        return None

    if natr <= 3.0 and not is_pd:
        regime = "FUTURES"
        leverage = int(np.clip(int(22.0 / natr), 2, 10))
    elif is_pd:
        regime = "FUTURES"
        leverage = int(np.clip(int(15.0 / natr), 2, 5))
    else:
        regime = "SPOT"
        leverage = 1

    card = get_ai_card(sym)
    if cand["is_funding_trap"]:
        wr = card["funding_reversal_wr"] / 100.0
    elif regime == "SPOT":
        wr = card["spot_regime_wr"] / 100.0
    elif direction == "LONG":
        wr = card["long_futures_wr"] / 100.0
    else:
        wr = card["short_futures_wr"] / 100.0

    b = card["avg_win_r"] / max(0.1, card["avg_loss_r"])
    q = 1.0 - wr
    full_kelly = (wr * b - q) / max(0.1, b)
    if full_kelly <= 0:
        return None

    frac_kelly = full_kelly * 0.35
    max_risk_pct = 0.01 if is_pd else 0.02
    margin_usd = min(balance * frac_kelly, balance * max_risk_pct)

    if (margin_usd * leverage) < 5.2:
        return None

    dir_mult = 1.0 if direction == "LONG" else -1.0
    sl_mod = card["sl_modifier"]
    tp_mod = card["tp_modifier"]
    fee_buffer = 0.001

    if is_pd:
        dca1 = dca2 = dca3 = 0.0
        sl_pct = (natr * 1.8 * sl_mod) / 100.0
        tp1 = price * (1.0 + dir_mult * ((natr * 1.0) / 100.0 + fee_buffer))
        tp2 = tp1 * (1.0 + dir_mult * 0.01)
    else:
        dca1 = price * (1.0 - dir_mult * ((0.5 * natr) / 100.0))
        dca2 = price * (1.0 - dir_mult * ((1.2 * natr) / 100.0))
        dca3 = price * (1.0 - dir_mult * ((2.0 * natr) / 100.0))
        sl_pct = (2.5 * natr * sl_mod) / 100.0
        tp1 = price * (1.0 + dir_mult * ((1.0 * natr * tp_mod) / 100.0 + fee_buffer))
        tp2 = price * (1.0 + dir_mult * ((1.8 * natr * tp_mod) / 100.0))

    sl_price = price * (1.0 - dir_mult * sl_pct)

    recent_klines = klines[-20:]
    highs = [float(get_high(k)) for k in recent_klines]
    lows = [float(get_low(k)) for k in recent_klines]
    mmr = 0.004
    if direction == "LONG":
        liq_short_20x = max(highs) * (1.0 + (1.0 / 20.0) - mmr)
        tp3 = liq_short_20x * 0.995
    else:
        liq_long_20x = min(lows) * (1.0 - (1.0 / 20.0) + mmr)
        tp3 = liq_long_20x * 1.005

    return {
        "window_id": window_id,
        "status": "ACTIVE",
        "symbol": sym,
        "direction": direction,
        "regime": regime,
        "leverage": leverage,
        "is_pd": is_pd,
        "is_funding_trap": cand["is_funding_trap"],
        "natr": natr,
        "margin_usd": margin_usd,
        "active_margin": margin_usd if is_pd else margin_usd * 0.30,
        "entry_price": price,
        "dca1_price": dca1, "dca1_filled": False,
        "dca2_price": dca2, "dca2_filled": False,
        "dca3_price": dca3, "dca3_filled": False,
        "tp1_price": tp1, "tp1_hit": False,
        "tp2_price": tp2, "tp2_hit": False,
        "tp3_price": tp3,
        "sl_tsl_price": sl_price,
        "tsl_active": False,
        "extreme_price": price,
        "mandate_expires_at": (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat()
    }

# =========================================================================
# HLAVNÁ AUTONÓMNA SLUČKA JADRA (CLOUD 1 - OKX ENGINE)
# =========================================================================
async def run_core_cycle(vault: dict) -> dict:
    flush_pending_sync(vault)
    now_ts = time.time()
    now_dt = datetime.now(timezone.utc)
    klines_cache = {}
    btc_veto = {"veto_long": False, "veto_short": False, "chg_15m": 0.0, "chg_1h": 0.0, "reason": "Čaká na sken"}

    async with httpx.AsyncClient(http2=True, timeout=10.0) as client:
        tickers, oi_map = await fetch_market_snapshot(client)

        # 1. Monitoring otvorených pozícií každých 5 sekúnd
        for win_key, pos in vault["windows"].items():
            win_id = int(win_key)
            if pos["status"] != "ACTIVE":
                continue

            sym = pos["symbol"]
            if sym not in tickers:
                continue

            curr_price = float(tickers[sym]["lastPrice"])
            pos_klines = await fetch_okx_klines(client, sym, "15m", 6, klines_cache)
            ofd = compute_ofd_from_klines(pos_klines)

            is_long = pos["direction"] == "LONG"
            dir_mult = 1.0 if is_long else -1.0
            natr = pos["natr"]

            if not pos["is_pd"] and not pos["dca3_filled"]:
                near_dca3 = (is_long and curr_price <= pos["dca3_price"]) or (not is_long and curr_price >= pos["dca3_price"])
                toxic_of = (is_long and ofd < 0.40) or (not is_long and ofd > 1.60)
                if near_dca3 and toxic_of:
                    close_position(pos, curr_price, "EARLY_ORDERFLOW_ESCAPE", vault)
                    continue

            if not pos["is_pd"]:
                for idx, pct in ((1, 0.10), (2, 0.30), (3, 0.30)):
                    if not pos[f"dca{idx}_filled"]:
                        dca_p = pos[f"dca{idx}_price"]
                        if (is_long and curr_price <= dca_p) or (not is_long and curr_price >= dca_p):
                            added_m = pos["margin_usd"] * pct
                            old_qty = (pos["active_margin"] * pos["leverage"]) / pos["entry_price"]
                            new_qty = (added_m * pos["leverage"]) / dca_p
                            pos["entry_price"] = ((old_qty * pos["entry_price"]) + (new_qty * dca_p)) / (old_qty + new_qty)
                            pos["active_margin"] += added_m
                            pos[f"dca{idx}_filled"] = True
                            pos["tp1_price"] = pos["entry_price"] * (1.0 + dir_mult * ((1.0 * natr) / 100.0 + 0.001))
                            pos["tp2_price"] = pos["entry_price"] * (1.0 + dir_mult * ((1.8 * natr) / 100.0))

            if not pos["tsl_active"]:
                if (is_long and curr_price >= pos["tp1_price"]) or (not is_long and curr_price <= pos["tp1_price"]):
                    pos["tsl_active"] = True
                    pos["tp1_hit"] = True
                    pos["extreme_price"] = curr_price
                    be_lock = pos["entry_price"] * (1.0 + dir_mult * 0.0015)
                    pos["sl_tsl_price"] = be_lock

            if pos["tsl_active"]:
                if is_long and curr_price > pos["extreme_price"]:
                    pos["extreme_price"] = curr_price
                elif not is_long and curr_price < pos["extreme_price"]:
                    pos["extreme_price"] = curr_price

                radar_dist = pos["tp3_price"] * ((0.25 * natr) / 100.0)
                in_radar = (is_long and curr_price >= pos["tp3_price"] - radar_dist) or (not is_long and curr_price <= pos["tp3_price"] + radar_dist)

                if in_radar:
                    strong_pump = (is_long and ofd > 1.45) or (not is_long and ofd < 0.65)
                    if not strong_pump and ((is_long and curr_price >= pos["tp3_price"]) or (not is_long and curr_price <= pos["tp3_price"])):
                        close_position(pos, curr_price, "TP3_LIQUIDATION_HIT", vault)
                        continue
                    elif strong_pump:
                        tsl_w_pct = (natr / max(0.2, ofd)) if is_long else (natr * ofd)
                        new_tsl = curr_price * (1.0 - dir_mult * (tsl_w_pct / 100.0))
                        if (is_long and new_tsl > pos["sl_tsl_price"]) or (not is_long and new_tsl < pos["sl_tsl_price"]):
                            pos["sl_tsl_price"] = new_tsl
                else:
                    step_pct = (0.45 * natr) / 100.0
                    new_tsl = pos["extreme_price"] * (1.0 - dir_mult * step_pct)
                    if (is_long and new_tsl > pos["sl_tsl_price"]) or (not is_long and new_tsl < pos["sl_tsl_price"]):
                        pos["sl_tsl_price"] = new_tsl

            hit_sl_tsl = (is_long and curr_price <= pos["sl_tsl_price"]) or (not is_long and curr_price >= pos["sl_tsl_price"])
            if hit_sl_tsl:
                reason = "TRAILING_SL_PROFIT" if pos["tsl_active"] else "HARD_SL_HIT"
                close_position(pos, curr_price, reason, vault)
                continue

            pnl_pct = ((curr_price - pos["entry_price"]) / pos["entry_price"]) * dir_mult * pos["leverage"] * 100.0
            pnl_usd = pos["active_margin"] * (pnl_pct / 100.0)
            safe_supabase_upsert("active_positions", {
                "window_id": win_id, "status": "ACTIVE", "symbol": sym, "direction": pos["direction"],
                "regime": pos["regime"], "leverage": pos["leverage"], "entry_price": pos["entry_price"],
                "dca1_price": pos["dca1_price"], "dca2_price": pos["dca2_price"], "dca3_price": pos["dca3_price"],
                "tp1_price": pos["tp1_price"], "tp2_price": pos["tp2_price"], "tp3_price": pos["tp3_price"],
                "sl_tsl_price": pos["sl_tsl_price"], "tsl_active": pos["tsl_active"],
                "pnl_usd": round(pnl_usd, 4), "pnl_percent": round(pnl_pct, 2),
                "mandate_expires_at": pos["mandate_expires_at"], "updated_at": now_dt.isoformat()
            }, vault)

        # 2. Jednotný 3-minútový skener (180 s) pre všetky 4 okná + BTC Veto + Anti-FOMO
        has_waiting_window = any(p["status"] == "WAITING" for p in vault["windows"].values())
        is_3m_scan_due = (now_ts - vault.get("last_market_scan", 0)) >= 180

        if has_waiting_window and is_3m_scan_due:
            vault["last_market_scan"] = now_ts

            btc_klines = await fetch_okx_klines(client, "BTCUSDT", "15m", 24, klines_cache)
            btc_veto = evaluate_btc_veto(btc_klines)

            active_syms = {p["symbol"] for p in vault["windows"].values() if p["status"] == "ACTIVE" and p["symbol"]}
            sectors_map = get_sectors_map()
            active_secs = {sectors_map.get(s, "UNKNOWN") for s in active_syms}

            # Okno 1: Makrokotva BTC (s Anti-FOMO EMA-20 filtrom)
            if vault["windows"]["1"]["status"] == "WAITING" and len(btc_klines) >= 20:
                last_btc = btc_klines[-1]
                btc_dir = "LONG" if float(get_close(last_btc)) > float(get_open(last_btc)) else "SHORT"
                if check_anti_fomo_ema20(btc_klines, btc_dir, False):
                    setup = build_trade_setup(
                        {"symbol": "BTCUSDT", "direction": btc_dir, "is_pd": False, "is_funding_trap": False, "klines": btc_klines},
                        vault["balance"],
                        1
                    )
                    if setup:
                        vault["windows"]["1"] = setup
                        active_syms.add("BTCUSDT")

            # Okno 2: Šampión A (Elitný >= 10M USDT)
            if vault["windows"]["2"]["status"] == "WAITING":
                cands_a = await evaluate_candidates(client, tickers, oi_map, btc_veto, klines_cache, active_syms, active_secs, 10_000_000, 1e12, False)
                for c in cands_a:
                    setup = build_trade_setup(c, vault["balance"], 2)
                    if setup:
                        vault["windows"]["2"] = setup
                        active_syms.add(c["symbol"])
                        active_secs.add(c["sector"])
                        break

            # Okno 3: Šampión B (Nízko-likvidný 5M - 9.99M USDT)
            if vault["windows"]["3"]["status"] == "WAITING":
                cands_b = await evaluate_candidates(client, tickers, oi_map, btc_veto, klines_cache, active_syms, active_secs, 5_000_000, 9_999_999, False)
                for c in cands_b:
                    setup = build_trade_setup(c, vault["balance"], 3)
                    if setup:
                        vault["windows"]["3"] = setup
                        active_syms.add(c["symbol"])
                        break

            # Okno 4: Šampión C (Pump & Dump 1M - 4.99M USDT)
            if vault["windows"]["4"]["status"] == "WAITING":
                cands_c = await evaluate_candidates(client, tickers, oi_map, btc_veto, klines_cache, active_syms, active_secs, 1_000_000, 4_999_999, True)
                for c in cands_c:
                    setup = build_trade_setup(c, vault["balance"], 4)
                    if setup:
                        vault["windows"]["4"] = setup
                        break

    save_vault(vault)
    return btc_veto

def close_position(pos: dict, exit_price: float, reason: str, vault: dict):
    dir_mult = 1.0 if pos["direction"] == "LONG" else -1.0
    pnl_pct = ((exit_price - pos["entry_price"]) / pos["entry_price"]) * dir_mult * pos["leverage"] * 100.0
    pnl_usd = pos["active_margin"] * (pnl_pct / 100.0)
    vault["balance"] = round(vault["balance"] + pnl_usd, 4)

    is_win = pnl_usd > 0
    r_mult = pnl_usd / max(0.01, pos["margin_usd"])
    update_ai_learning(pos["symbol"], pos["direction"], pos["regime"], pos["is_funding_trap"], is_win, r_mult, vault)

    hist_row = {
        "window_id": pos["window_id"], "symbol": pos["symbol"], "direction": pos["direction"],
        "regime": pos["regime"], "leverage": pos["leverage"], "entry_price": pos["entry_price"],
        "exit_price": exit_price, "margin_usd": pos["active_margin"], "pnl_usd": round(pnl_usd, 4),
        "pnl_percent": round(pnl_pct, 2), "exit_reason": reason, "current_balance": vault["balance"]
    }
    try:
        supabase.table("trade_history").insert(hist_row).execute()
    except Exception:
        vault["pending_supabase_sync"].append({"table": "trade_history", "op": "insert", "data": hist_row})

    win_id = pos["window_id"]
    vault["windows"][str(win_id)] = {"status": "WAITING", "symbol": "BTCUSDT" if win_id == 1 else None, "mandate_until": 0}
    safe_supabase_upsert("active_positions", {
        "window_id": win_id, "status": "WAITING", "symbol": None, "entry_price": 0.0,
        "tp1_price": 0.0, "tp2_price": 0.0, "tp3_price": 0.0, "sl_tsl_price": 0.0,
        "pnl_usd": 0.0, "pnl_percent": 0.0, "updated_at": datetime.now(timezone.utc).isoformat()
    }, vault)
    save_vault(vault)

# =========================================================================
# SPUSTENIE NEPRETRŽITÉHO JADRA
# =========================================================================
st.title("🦈 Shark Alpha Core (Cloud 1 - OKX Engine)")
vault_state = load_vault()

col1, col2, col3 = st.columns(3)
col1.metric("Live Fiktívny Kapitál", f"${vault_state['balance']:.2f} USD")

last_scan_ts = vault_state.get("last_main_scan", 0)
last_scan_str = datetime.fromtimestamp(last_scan_ts, tz=timezone.utc).strftime("%H:%M:%S") if last_scan_ts > 0 else "Čaká"
col2.metric("Posledný 3m Sken (UTC)", last_scan_str)

veto_info = vault_state.get("last_btc_veto", {})
veto_status = "OK (Voľný trh)"
if veto_info.get("veto_long"):
    veto_status = "🛑 VETO LONG"
elif veto_info.get("veto_short"):
    veto_status = "🛑 VETO SHORT"
col3.metric("BTC Veto Filter", veto_status)

try:
    asyncio.run(run_core_cycle(vault_state))
    st.success(f"✅ Cyklus dokončený ({datetime.now(timezone.utc).strftime('%H:%M:%S')} UTC)")
except Exception as e:
    st.error(f"⚠️ Chyba cyklu: {e}")

st.json(vault_state["windows"])

time.sleep(5)
st.rerun()


compile(code_content, file_path, "exec")
print(f"File {file_path} generated and verified successfully. Lines: {len(code_content.splitlines())}")
