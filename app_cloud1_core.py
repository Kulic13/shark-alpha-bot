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
# BLOK 1: INFRAŠTRUKTÚRA, CLOUD TREZOR (SUPABASE) & ZÁLOŽNÁ CACHE
# =========================================================================
VAULT_FILE = "vault_state_backup.json"
OKX_API = "https://www.okx.com"

get_open = operator.itemgetter(1)
get_high = operator.itemgetter(2)
get_low = operator.itemgetter(3)
get_close = operator.itemgetter(4)
get_vol = operator.itemgetter(5)

@st.cache_resource
def init_supabase() -> Client:
    url = st.secrets["SUPABASE_URL"].strip().rstrip("/")
    if url.endswith("/rest/v1"):
        url = url[:-8]
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
        "last_main_scan": 0,
        "last_db_sync": 0,
        "last_btc_veto": {
            "veto_long": False,
            "veto_short": False,
            "btc_1h_pct": 0.0,
            "btc_15m_pct": 0.0
        },
        "pending_supabase_sync": []
    }

def load_vault() -> dict:
    # V 1s režime preferujeme rýchlu lokálnu cache, aby sme nezaťažovali Supabase čítaním každú sekundu
    if os.path.exists(VAULT_FILE):
        try:
            with open(VAULT_FILE, "r", encoding="utf-8") as f:
                state = json.load(f)
                if "windows" in state and "balance" in state:
                    state.setdefault("last_main_scan", 0)
                    state.setdefault("last_db_sync", 0)
                    state.setdefault("pending_supabase_sync", [])
                    state.setdefault("last_btc_veto", {
                        "veto_long": False,
                        "veto_short": False,
                        "btc_1h_pct": 0.0,
                        "btc_15m_pct": 0.0
                    })
                    return state
        except Exception:
            pass

    try:
        res = supabase.table("bot_vault_state").select("state_json").eq("id", 1).execute()
        if res.data and len(res.data) > 0 and res.data[0].get("state_json"):
            state = res.data[0]["state_json"]
            state.setdefault("last_main_scan", 0)
            state.setdefault("last_db_sync", 0)
            state.setdefault("pending_supabase_sync", [])
            state.setdefault("last_btc_veto", {
                "veto_long": False,
                "veto_short": False,
                "btc_1h_pct": 0.0,
                "btc_15m_pct": 0.0
            })
            save_vault_local(state)
            return state
    except Exception as e:
        st.warning(f"Upozornenie: Čítanie trezoru zo Supabase zlyhalo ({e}), inicializujem predvolený trezor.")

    return get_default_vault()

def save_vault_local(state: dict):
    try:
        tmp_file = f"{VAULT_FILE}.tmp"
        with open(tmp_file, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
        os.replace(tmp_file, VAULT_FILE)
    except Exception:
        pass

def save_vault(state: dict, sync_cloud: bool = True):
    save_vault_local(state)
    if sync_cloud:
        try:
            supabase.table("bot_vault_state").upsert({
                "id": 1,
                "state_json": state,
                "updated_at": datetime.now(timezone.utc).isoformat()
            }).execute()
            state["last_db_sync"] = time.time()
        except Exception as e:
            st.error(f"Chyba ukladania trezoru do Supabase: {e}")

def safe_supabase_upsert(table: str, data: dict, vault: dict):
    try:
        supabase.table(table).upsert(data).execute()
    except Exception:
        vault["pending_supabase_sync"].append({
            "table": table,
            "op": "upsert",
            "data": data
        })

def flush_pending_sync(vault: dict):
    if not vault.get("pending_supabase_sync"):
        return
    remaining = []
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
        "long_futures_wr": 52.0,
        "short_futures_wr": 52.0,
        "spot_regime_wr": 52.0,
        "funding_reversal_wr": 52.0,
        "avg_win_r": 1.5,
        "avg_loss_r": 1.0,
        "sl_modifier": 1.00,
        "tp_modifier": 1.00,
        "total_trades": 0
    }
    try:
        res = supabase.table("ai_learning_matrix").select("*").eq("symbol", symbol).execute()
        if res.data and len(res.data) > 0:
            return res.data[0]
        supabase.table("ai_learning_matrix").insert(default_card).execute()
    except Exception:
        pass
    return default_card

def update_ai_learning(
    symbol: str,
    direction: str,
    regime: str,
    is_funding_trap: bool,
    is_win: bool,
    r_multiple: float,
    vault: dict
):
    card = get_ai_card(symbol)
    decay = 0.85
    outcome = 100.0 if is_win else 0.0

    if is_funding_trap:
        card["funding_reversal_wr"] = (card["funding_reversal_wr"] * decay) + (outcome * (1.0 - decay))
    elif regime == "SPOT":
        card["spot_regime_wr"] = (card["spot_regime_wr"] * decay) + (outcome * (1.0 - decay))
    elif direction == "LONG":
        card["long_futures_wr"] = (card["long_futures_wr"] * decay) + (outcome * (1.0 - decay))
    else:
        card["short_futures_wr"] = (card["short_futures_wr"] * decay) + (outcome * (1.0 - decay))

    if is_win:
        card["avg_win_r"] = (card["avg_win_r"] * decay) + (abs(r_multiple) * (1.0 - decay))
        card["sl_modifier"] = float(np.clip(card["sl_modifier"] * 0.97, 0.75, 1.45))
        card["tp_modifier"] = float(np.clip(card["tp_modifier"] * 1.02, 0.75, 1.45))
    else:
        card["avg_loss_r"] = (card["avg_loss_r"] * decay) + (abs(r_multiple) * (1.0 - decay))
        card["sl_modifier"] = float(np.clip(card["sl_modifier"] * 1.06, 0.75, 1.45))
        card["tp_modifier"] = float(np.clip(card["tp_modifier"] * 0.98, 0.75, 1.45))

    card["total_trades"] = int(card.get("total_trades", 0)) + 1
    card["last_update"] = datetime.now(timezone.utc).isoformat()
    safe_supabase_upsert("ai_learning_matrix", card, vault)

# =========================================================================
# BLOK 3A: OKX API KONEKTOR & ODĽAHČENÝ 1s ZBER DÁT
# =========================================================================
def to_okx_inst_id(symbol: str) -> str:
    base = symbol.replace("USDT", "")
    return f"{base}-USDT-SWAP"

def from_okx_inst_id(inst_id: str) -> str:
    parts = inst_id.split("-")
    if len(parts) >= 2 and parts[1] == "USDT":
        return f"{parts[0]}USDT"
    return inst_id

async def fetch_single_ticker(client: httpx.AsyncClient, symbol: str) -> dict | None:
    """Odľahčené stiahnutie iba 1 konkrétneho tickeru (~0.5 kB) pre 1-sekundový TSL cyklus."""
    inst_id = to_okx_inst_id(symbol)
    try:
        res = await client.get(f"{OKX_API}/api/v5/market/ticker", params={"instId": inst_id})
        if res.status_code == 200:
            data = res.json()
            if data.get("code") == "0" and data.get("data"):
                return data["data"][0]
    except Exception:
        pass
    return None

async def fetch_active_tickers_only(client: httpx.AsyncClient, symbols: list[str]) -> dict:
    """Paralelne stiahne len tickery aktívne otvorených pozícií (šetří 99 % dát oproti celému trhu)."""
    if not symbols:
        return {}
    unique_syms = list(set(symbols))
    tasks = [fetch_single_ticker(client, s) for s in unique_syms]
    results = await asyncio.gather(*tasks)
    return {sym: res for sym, res in zip(unique_syms, results) if res is not None}

async def fetch_market_snapshot(client: httpx.AsyncClient):
    """Ťažký zber celého trhu - spúšťa sa iba raz za 3 minúty pri hlavnom skene."""
    t_res, oi_res = await asyncio.gather(
        client.get(f"{OKX_API}/api/v5/market/tickers", params={"instType": "SWAP"}),
        client.get(f"{OKX_API}/api/v5/public/open-interest", params={"instType": "SWAP"})
    )
    t_res.raise_for_status()
    t_json = t_res.json()
    if t_json.get("code") != "0":
        raise RuntimeError(f"OKX Tickers API chyba: {t_json.get('msg')}")

    tickers = {}
    for item in t_json.get("data", []):
        inst_id = item.get("instId", "")
        if inst_id.endswith("-USDT-SWAP"):
            sym = from_okx_inst_id(inst_id)
            tickers[sym] = item

    oi_map = {}
    if oi_res.status_code == 200:
        oi_json = oi_res.json()
        if oi_json.get("code") == "0":
            for item in oi_json.get("data", []):
                inst_id = item.get("instId", "")
                if inst_id.endswith("-USDT-SWAP"):
                    sym = from_okx_inst_id(inst_id)
                    oi_map[sym] = float(item.get("oiCcy", 0.0))

    return tickers, oi_map

async def fetch_okx_klines(
    client: httpx.AsyncClient,
    symbol: str,
    bar: str = "15m",
    limit: int = 25,
    cache: dict = None
) -> list:
    cache_key = f"{symbol}_{bar}_{limit}"
    if cache is not None and cache_key in cache:
        return cache[cache_key]

    inst_id = to_okx_inst_id(symbol)
    res = await client.get(
        f"{OKX_API}/api/v5/market/candles",
        params={"instId": inst_id, "bar": bar, "limit": str(limit)}
    )
    if res.status_code != 200:
        return []
    data = res.json()
    if data.get("code") != "0":
        return []
    klines = list(reversed(data.get("data", [])))
    if cache is not None:
        cache[cache_key] = klines
    return klines

async def fetch_okx_funding(client: httpx.AsyncClient, symbol: str) -> float:
    inst_id = to_okx_inst_id(symbol)
    try:
        res = await client.get(
            f"{OKX_API}/api/v5/public/funding-rate",
            params={"instId": inst_id}
        )
        if res.status_code == 200:
            data = res.json()
            if data.get("code") == "0" and data.get("data"):
                return float(data["data"][0].get("fundingRate", 0.0))
    except Exception:
        pass
    return 0.0

# =========================================================================
# BLOK 3B: ANTI-BLIND EXEKÚCIA, ANTI-EXTREME ŠTÍT, BTC VETO & SKENER
# =========================================================================
async def verify_execution_confirmation(
    client: httpx.AsyncClient,
    symbol: str,
    direction: str,
    klines_15m: list,
    ofd: float,
    is_pd: bool,
    cache: dict
) -> tuple[bool, float]:
    if len(klines_15m) < 12:
        return False, 0.0

    vols = [float(get_vol(k)) for k in klines_15m[-11:]]
    avg_prev_vol = float(np.mean(vols[:-1])) if len(vols) > 1 else 0.0
    curr_vol = vols[-1]
    if avg_prev_vol > 0 and (curr_vol / avg_prev_vol) < 1.10:
        return False, 0.0

    klines_1h = await fetch_okx_klines(client, symbol, "1H", 25, cache)
    if len(klines_1h) >= 21:
        closes_1h = pd.Series([float(get_close(k)) for k in klines_1h])
        ema21_1h = float(closes_1h.ewm(span=21, adjust=False).mean().iloc[-1])
        last_1h_close = float(closes_1h.iloc[-1])
        if direction == "LONG" and last_1h_close < ema21_1h and ofd < 1.50:
            return False, 0.0
        if direction == "SHORT" and last_1h_close > ema21_1h and ofd > 0.65:
            return False, 0.0

    inst_id = to_okx_inst_id(symbol)
    try:
        ob_res = await client.get(
            f"{OKX_API}/api/v5/market/books",
            params={"instId": inst_id, "sz": "5"}
        )
        if ob_res.status_code != 200:
            return False, 0.0
        ob_json = ob_res.json()
        if ob_json.get("code") != "0" or not ob_json.get("data"):
            return False, 0.0

        book = ob_json["data"][0]
        asks = book.get("asks", [])
        bids = book.get("bids", [])
        if not asks or not bids:
            return False, 0.0

        best_ask = float(asks[0][0])
        best_bid = float(bids[0][0])
        if best_bid <= 0 or best_ask <= 0:
            return False, 0.0

        spread_pct = ((best_ask - best_bid) / best_bid) * 100.0
        max_spread = 0.30 if is_pd else 0.18
        if spread_pct > max_spread:
            return False, 0.0

        bid_depth = sum(float(b[1]) for b in bids[:5])
        ask_depth = sum(float(a[1]) for a in asks[:5])
        obi = bid_depth / max(1e-9, ask_depth)

        if direction == "LONG" and obi < 0.85:
            return False, 0.0
        if direction == "SHORT" and obi > 1.18:
            return False, 0.0

        exec_price = best_ask if direction == "LONG" else best_bid
        return True, exec_price
    except Exception:
        return False, 0.0


async def verify_execution_confirmation_btc(
    client: httpx.AsyncClient,
    symbol: str,
    direction: str,
    klines_15m: list,
    ofd: float,
    cache: dict
) -> tuple[bool, float]:
    """Uvoľnená verzia pre BTC – volume filter 1.05x namiesto 1.10x (BTC má stabilnejší volume)."""
    if len(klines_15m) < 12:
        return False, 0.0

    vols = [float(get_vol(k)) for k in klines_15m[-11:]]
    avg_prev_vol = float(np.mean(vols[:-1])) if len(vols) > 1 else 0.0
    curr_vol = vols[-1]
    # Uvoľnené pre BTC (1.05 namiesto 1.10)
    if avg_prev_vol > 0 and (curr_vol / avg_prev_vol) < 1.05:
        return False, 0.0

    klines_1h = await fetch_okx_klines(client, symbol, "1H", 25, cache)
    if len(klines_1h) >= 21:
        closes_1h = pd.Series([float(get_close(k)) for k in klines_1h])
        ema21_1h = float(closes_1h.ewm(span=21, adjust=False).mean().iloc[-1])
        last_1h_close = float(closes_1h.iloc[-1])
        if direction == "LONG" and last_1h_close < ema21_1h and ofd < 1.50:
            return False, 0.0
        if direction == "SHORT" and last_1h_close > ema21_1h and ofd > 0.65:
            return False, 0.0

    inst_id = to_okx_inst_id(symbol)
    try:
        ob_res = await client.get(
            f"{OKX_API}/api/v5/market/books",
            params={"instId": inst_id, "sz": "5"}
        )
        if ob_res.status_code != 200:
            return False, 0.0
        ob_json = ob_res.json()
        if ob_json.get("code") != "0" or not ob_json.get("data"):
            return False, 0.0

        book = ob_json["data"][0]
        asks = book.get("asks", [])
        bids = book.get("bids", [])
        if not asks or not bids:
            return False, 0.0

        best_ask = float(asks[0][0])
        best_bid = float(bids[0][0])
        if best_bid <= 0 or best_ask <= 0:
            return False, 0.0

        spread_pct = ((best_ask - best_bid) / best_bid) * 100.0
        # BTC má veľmi úzky spread – necháme 0.18
        if spread_pct > 0.18:
            return False, 0.0

        bid_depth = sum(float(b[1]) for b in bids[:5])
        ask_depth = sum(float(a[1]) for a in asks[:5])
        obi = bid_depth / max(1e-9, ask_depth)

        if direction == "LONG" and obi < 0.85:
            return False, 0.0
        if direction == "SHORT" and obi > 1.18:
            return False, 0.0

        exec_price = best_ask if direction == "LONG" else best_bid
        return True, exec_price
    except Exception:
        return False, 0.0


def compute_ofd_from_klines(klines: list) -> float:
    if not klines:
        return 1.0
    recent = klines[-5:] if len(klines) >= 5 else klines
    buy_vol = 0.0
    sell_vol = 0.0
    for k in recent:
        h = float(get_high(k))
        l = float(get_low(k))
        c = float(get_close(k))
        v = float(get_vol(k))
        rng = max(1e-12, h - l)
        buyer_ratio = (c - l) / rng
        buy_vol += v * buyer_ratio
        sell_vol += v * (1.0 - buyer_ratio)
    return buy_vol / max(1e-9, sell_vol)

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

def update_24h_oi_metrics(tickers: dict, oi_map: dict):
    if not oi_map:
        return
    existing = get_24h_metrics()
    alpha = 0.015
    now_iso = datetime.now(timezone.utc).isoformat()
    batch = []

    for sym, oi_coins in oi_map.items():
        if sym not in tickers:
            continue
        price = float(tickers[sym].get("last", 0.0))
        vol = float(tickers[sym].get("volCcy24h", 0.0))
        if price <= 0 or vol < 500_000:
            continue

        oi_usd = oi_coins * price
        if oi_usd <= 0:
            continue

        prev_avg = float(existing.get(sym, {}).get("oi_avg_24h", 0.0))
        new_avg = oi_usd if prev_avg <= 0 else (prev_avg * (1.0 - alpha)) + (oi_usd * alpha)
        batch.append({
            "symbol": sym,
            "oi_avg_24h": round(new_avg, 2),
            "updated_at": now_iso
        })

    if batch:
        try:
            for i in range(0, len(batch), 100):
                supabase.table("market_metrics_24h").upsert(batch[i:i + 100]).execute()
        except Exception:
            pass

def evaluate_btc_veto(btc_klines: list) -> dict:
    result = {
        "veto_long": False,
        "veto_short": False,
        "btc_1h_pct": 0.0,
        "btc_15m_pct": 0.0
    }
    if len(btc_klines) < 5:
        return result

    curr_close = float(get_close(btc_klines[-1]))
    curr_open = float(get_open(btc_klines[-1]))
    open_1h_ago = float(get_open(btc_klines[-4]))

    btc_15m_pct = ((curr_close - curr_open) / max(1e-9, curr_open)) * 100.0
    btc_1h_pct = ((curr_close - open_1h_ago) / max(1e-9, open_1h_ago)) * 100.0

    result["btc_15m_pct"] = round(btc_15m_pct, 3)
    result["btc_1h_pct"] = round(btc_1h_pct, 3)

    if btc_1h_pct < -0.80 or btc_15m_pct < -0.45:
        result["veto_long"] = True
    if btc_1h_pct > 0.80 or btc_15m_pct > 0.45:
        result["veto_short"] = True

    return result

def calculate_rsi_14(closes: pd.Series) -> float:
    delta = closes.diff()
    gain = delta.clip(lower=0).rolling(14).mean()
    loss = (-delta.clip(upper=0)).rolling(14).mean()
    last_loss = float(loss.iloc[-1])
    if last_loss == 0:
        return 100.0 if float(gain.iloc[-1]) > 0 else 50.0
    rs = float(gain.iloc[-1]) / last_loss
    return float(100.0 - (100.0 / (1.0 + rs)))

def check_anti_extreme_shield(klines: list, direction: str, is_pd: bool = False) -> bool:
    if len(klines) < 20:
        return False

    df = pd.DataFrame(klines, columns=["ts", "o", "h", "l", "c", "v", "vc", "vq", "cf"])
    for col in ("o", "h", "l", "c"):
        df[col] = df[col].astype(float)

    last_o = df["o"].iloc[-1]
    last_h = df["h"].iloc[-1]
    last_l = df["l"].iloc[-1]
    last_c = df["c"].iloc[-1]

    candle_range = last_h - last_l
    if candle_range > 0:
        upper_wick = last_h - max(last_o, last_c)
        lower_wick = min(last_o, last_c) - last_l
        if direction == "LONG" and (upper_wick / candle_range) > 0.45:
            return False
        if direction == "SHORT" and (lower_wick / candle_range) > 0.45:
            return False

    prev_c = df["c"].shift(1)
    tr = pd.concat((df["h"] - df["l"], (df["h"] - prev_c).abs(), (df["l"] - prev_c).abs()), axis=1).max(axis=1)
    atr_14 = float(tr.rolling(14).mean().iloc[-1])
    ema_20 = float(df["c"].ewm(span=20, adjust=False).mean().iloc[-1])
    std_20 = float(df["c"].rolling(20).std().iloc[-1])

    if atr_14 <= 0 or np.isnan(atr_14) or np.isnan(std_20):
        return False

    max_atr_dist = 2.2 if is_pd else 1.3
    bb_upper = ema_20 + (2.0 * std_20)
    bb_lower = ema_20 - (2.0 * std_20)

    if direction == "LONG":
        if (last_c - ema_20) > (max_atr_dist * atr_14):
            return False
        if not is_pd and last_c > bb_upper:
            return False
    else:
        if (ema_20 - last_c) > (max_atr_dist * atr_14):
            return False
        if not is_pd and last_c < bb_lower:
            return False

    rsi_14 = calculate_rsi_14(df["c"])
    if not np.isnan(rsi_14):
        rsi_high = 78.0 if is_pd else 68.0
        rsi_low = 22.0 if is_pd else 32.0
        if direction == "LONG" and rsi_14 > rsi_high:
            return False
        if direction == "SHORT" and rsi_14 < rsi_low:
            return False

    return True

async def evaluate_candidates(
    client: httpx.AsyncClient,
    tickers: dict,
    oi_map: dict,
    excluded_symbols: set,
    excluded_sectors: set,
    min_vol: float,
    max_vol: float,
    btc_veto: dict,
    klines_cache: dict,
    is_pd_mode: bool = False
):
    sectors_map = get_sectors_map()
    metrics_24h = get_24h_metrics()
    pre_filtered = []

    for sym, t in tickers.items():
        if sym == "BTCUSDT" or sym in excluded_symbols:
            continue
        vol = float(t.get("volCcy24h", 0.0))
        if not (min_vol <= vol <= max_vol):
            continue
        sec = sectors_map.get(sym, "UNKNOWN")
        if sec != "UNKNOWN" and sec in excluded_sectors:
            continue

        price = float(t.get("last", 0.0))
        open_24h = float(t.get("open24h", price))
        if price <= 0 or open_24h <= 0:
            continue

        price_change_pct = ((price - open_24h) / open_24h) * 100.0
        pre_filtered.append((sym, vol, sec, price, price_change_pct))

    sem = asyncio.Semaphore(10)

    async def process_symbol(item):
        sym, vol, sec, price, price_change_pct = item
        async with sem:
            try:
                klines = await fetch_okx_klines(client, sym, "15m", 25, klines_cache)
                if len(klines) < 20:
                    return None

                last_candle = klines[-1]
                last_open = float(get_open(last_candle))
                last_close = float(get_close(last_candle))
                is_15m_green = last_close > last_open
                ofd = compute_ofd_from_klines(klines)

                if is_pd_mode:
                    oi_curr = oi_map.get(sym, 0.0) * price
                    oi_avg = float(metrics_24h.get(sym, {}).get("oi_avg_24h", 0.0))
                    if oi_avg <= 0:
                        oi_avg = oi_curr
                    oi_ratio = oi_curr / max(1.0, oi_avg)

                    if oi_ratio > 1.80 and ofd > 1.60 and is_15m_green:
                        if btc_veto.get("veto_long"):
                            return None
                        if not check_anti_extreme_shield(klines, "LONG", is_pd=True):
                            return None
                        ok_exec, exec_price = await verify_execution_confirmation(
                            client, sym, "LONG", klines, ofd, True, klines_cache
                        )
                        if not ok_exec:
                            return None
                        sas_score = oi_ratio * ofd * 100.0
                        return {
                            "symbol": sym,
                            "direction": "LONG",
                            "score": sas_score,
                            "is_funding_trap": False,
                            "is_pd": True,
                            "sector": sec,
                            "ofd": ofd,
                            "klines": klines,
                            "exec_price": exec_price
                        }

                    elif price_change_pct > 15.0 and ofd < 0.80 and not is_15m_green:
                        if btc_veto.get("veto_short"):
                            return None
                        funding_rate = await fetch_okx_funding(client, sym)
                        if funding_rate > 0.0020 and check_anti_extreme_shield(klines, "SHORT", is_pd=True):
                            ok_exec, exec_price = await verify_execution_confirmation(
                                client, sym, "SHORT", klines, ofd, True, klines_cache
                            )
                            if not ok_exec:
                                return None
                            sas_score = abs(price_change_pct) * (1.0 / max(0.1, ofd)) * 50.0
                            return {
                                "symbol": sym,
                                "direction": "SHORT",
                                "score": sas_score,
                                "is_funding_trap": True,
                                "is_pd": True,
                                "sector": sec,
                                "ofd": ofd,
                                "klines": klines,
                                "exec_price": exec_price
                            }
                    return None

                direction = None
                is_trap = False

                if not is_15m_green and ofd < 0.90:
                    funding_rate = await fetch_okx_funding(client, sym)
                    if funding_rate > 0.0020:
                        direction = "SHORT"
                        is_trap = True
                if direction is None and is_15m_green and ofd > 1.15:
                    funding_rate = await fetch_okx_funding(client, sym)
                    if funding_rate < -0.0050:
                        direction = "LONG"
                        is_trap = True

                if direction is None:
                    if ofd > 1.30 and is_15m_green:
                        direction = "LONG"
                    elif ofd < 0.70 and not is_15m_green:
                        direction = "SHORT"

                if direction:
                    if direction == "LONG" and btc_veto.get("veto_long"):
                        return None
                    if direction == "SHORT" and btc_veto.get("veto_short"):
                        return None
                    if not check_anti_extreme_shield(klines, direction, is_pd=False):
                        return None
                    ok_exec, exec_price = await verify_execution_confirmation(
                        client, sym, direction, klines, ofd, False, klines_cache
                    )
                    if not ok_exec:
                        return None

                    vol_norm = np.log1p(vol)
                    sas_score = (abs(price_change_pct) + 1.0) * (ofd if direction == "LONG" else (1.0 / max(0.1, ofd))) * (10.0 / vol_norm)
                    return {
                        "symbol": sym,
                        "direction": direction,
                        "score": sas_score,
                        "is_funding_trap": is_trap,
                        "is_pd": False,
                        "sector": sec,
                        "ofd": ofd,
                        "klines": klines,
                        "exec_price": exec_price
                    }
            except Exception:
                return None
        return None

    results = await asyncio.gather(*(process_symbol(item) for item in pre_filtered))
    candidates = [r for r in results if r is not None]
    candidates.sort(key=lambda x: x["score"], reverse=True)
    return candidates

# =========================================================================
# BLOK 4: INTEGROVANÝ RISK ENGINE, BAYESOVSKÝ KELLY, NATR ŠTÍT & TSL
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

def compute_roundtrip_cost_rate(window_id: int) -> float:
    exchange_fees = 0.0010
    slippage = 0.0005 if window_id in (1, 2) else 0.0020
    return exchange_fees + slippage

def build_trade_setup(cand: dict, balance: float, window_id: int) -> dict | None:
    sym = cand["symbol"]
    direction = cand["direction"]
    is_pd = cand["is_pd"]
    klines = cand["klines"]
    last_candle = klines[-1]
    price = float(cand.get("exec_price") or get_close(last_candle))
    natr = calculate_natr_14(klines)

    max_natr = 9.0 if is_pd else 5.5
    if natr > max_natr or natr < 0.3 or np.isnan(natr):
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
        raw_wr = card["funding_reversal_wr"] / 100.0
    elif regime == "SPOT":
        raw_wr = card["spot_regime_wr"] / 100.0
    elif direction == "LONG":
        raw_wr = card["long_futures_wr"] / 100.0
    else:
        raw_wr = card["short_futures_wr"] / 100.0

    trades_n = int(card.get("total_trades", 0))
    prior_strength = 10.0
    prior_wr = 0.52
    wr = ((raw_wr * trades_n) + (prior_wr * prior_strength)) / (trades_n + prior_strength)

    b = card["avg_win_r"] / max(0.1, card["avg_loss_r"])
    q = 1.0 - wr
    full_kelly = (wr * b - q) / max(0.1, b)
    if full_kelly <= 0:
        return None

    frac_kelly = full_kelly * 0.35
    max_risk_pct = 0.015 if is_pd else 0.025
    margin_usd = min(balance * frac_kelly, balance * max_risk_pct)

    if (margin_usd * leverage) < 5.2:
        min_margin = 5.2 / max(1, leverage)
        if min_margin <= balance * 0.06:
            margin_usd = min_margin
        else:
            return None

    dir_mult = 1.0 if direction == "LONG" else -1.0
    sl_mod = card["sl_modifier"]
    tp_mod = card["tp_modifier"]
    cost_rate = compute_roundtrip_cost_rate(window_id)

    if is_pd:
        dca1 = dca2 = dca3 = 0.0
        sl_pct = (natr * 1.8 * sl_mod) / 100.0
        tp1 = price * (1.0 + dir_mult * ((natr * 1.0 * tp_mod) / 100.0 + cost_rate))
        tp2 = price * (1.0 + dir_mult * ((natr * 1.8 * tp_mod) / 100.0 + cost_rate))
    else:
        dca1 = price * (1.0 - dir_mult * ((0.5 * natr) / 100.0))
        dca2 = price * (1.0 - dir_mult * ((1.2 * natr) / 100.0))
        dca3 = price * (1.0 - dir_mult * ((2.0 * natr) / 100.0))

        # Finančná poistka: Hard SL musí byť vždy za DCA3 (min. 2.25 * natr),
        # inak by nízky sl_modifier zavrel obchod pred vyplnením DCA3
        raw_sl_mult = max(2.25, 2.5 * sl_mod)
        sl_pct = (raw_sl_mult * natr) / 100.0

        tp1 = price * (1.0 + dir_mult * ((1.0 * natr * tp_mod) / 100.0 + cost_rate))
        tp2 = price * (1.0 + dir_mult * ((1.8 * natr * tp_mod) / 100.0 + cost_rate))

    sl_price = price * (1.0 - dir_mult * sl_pct)

    # Výpočet likvidačnej zóny 20x páky (TP3) s garantovanou monotónnosťou
    recent_klines = klines[-20:]
    highs = [float(get_high(k)) for k in recent_klines]
    lows = [float(get_low(k)) for k in recent_klines]
    mmr = 0.004
    min_tp3_gap = (0.5 * natr) / 100.0

    if direction == "LONG":
        liq_short_20x = max(highs) * (1.0 + (1.0 / 20.0) - mmr)
        raw_tp3 = liq_short_20x * 0.995
        tp3 = max(raw_tp3, tp2 * (1.0 + min_tp3_gap))
    else:
        liq_long_20x = min(lows) * (1.0 - (1.0 / 20.0) + mmr)
        raw_tp3 = liq_long_20x * 1.005
        tp3 = min(raw_tp3, tp2 * (1.0 - min_tp3_gap))

    initial_ofd = compute_ofd_from_klines(klines)

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
        "cost_rate": cost_rate,
        "tp_mod": tp_mod,
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
        "cached_ofd": initial_ofd,
        "last_ofd_check": time.time(),
        "mandate_expires_at": (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat()
    }

# =========================================================================
# HLAVNÁ AUTONÓMNA SLUČKA JADRA (ODĽAHČENÝ 1-SEKUNDOVÝ REŽIM)
# =========================================================================
async def run_core_cycle(vault: dict):
    now_ts = time.time()
    now_dt = datetime.now(timezone.utc)
    db_dirty = False  # Sleduje, či nastala kritická udalosť vyžadujúca okamžitý zápis do cloudu

    active_windows = [
        (win_key, pos) for win_key, pos in vault["windows"].items()
        if pos["status"] == "ACTIVE" and pos.get("symbol")
    ]
    last_main_scan = vault.get("last_main_scan", 0)
    is_scan_due = (now_ts - last_main_scan) >= 180

    # Ak nie sú otvorené žiadne pozície a nie je čas na 3m sken, nevoláme žiadne API
    if not active_windows and not is_scan_due:
        if vault.get("pending_supabase_sync") and (now_ts - vault.get("last_db_sync", 0)) >= 10:
            flush_pending_sync(vault)
            save_vault(vault, sync_cloud=True)
        return

    async with httpx.AsyncClient(http2=True, timeout=8.0) as client:
        # 1. Rýchly 1s zber cien iba pre otvorené pozície (~0.5 kB namiesto 400 kB)
        active_syms_list = [pos["symbol"] for _, pos in active_windows]
        active_tickers = await fetch_active_tickers_only(client, active_syms_list) if active_syms_list else {}
        klines_cache = {}

        for win_key, pos in active_windows:
            win_id = int(win_key)
            sym = pos["symbol"]
            if sym not in active_tickers:
                continue

            curr_price = float(active_tickers[sym]["last"])
            is_long = pos["direction"] == "LONG"
            dir_mult = 1.0 if is_long else -1.0
            natr = pos["natr"]

            # 15m sviečky pre výpočet OFD aktualizujeme iba raz za 15 sekúnd (ochrana pred Rate Limitom)
            last_ofd_ts = pos.get("last_ofd_check", 0)
            if (now_ts - last_ofd_ts) >= 15.0 or "cached_ofd" not in pos:
                klines = await fetch_okx_klines(client, sym, "15m", 10, klines_cache)
                if klines:
                    pos["cached_ofd"] = compute_ofd_from_klines(klines)
                    pos["last_ofd_check"] = now_ts
            ofd = pos.get("cached_ofd", 1.0)

            # A. Ochranný únik pri toxickom Order Flow pred DCA3
            if not pos["is_pd"] and not pos["dca3_filled"]:
                near_dca3 = (is_long and curr_price <= pos["dca3_price"]) or (not is_long and curr_price >= pos["dca3_price"])
                toxic_of = (is_long and ofd < 0.40) or (not is_long and ofd > 1.60)
                if near_dca3 and toxic_of:
                    close_position(pos, curr_price, "EARLY_ORDERFLOW_ESCAPE", vault)
                    db_dirty = True
                    continue

            # B. Spracovanie DCA dokupov (1, 2, 3)
            if not pos["is_pd"]:
                cost_rate = pos.get("cost_rate", compute_roundtrip_cost_rate(win_id))
                tp_mod = pos.get("tp_mod", 1.0)
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
                            db_dirty = True

                            pos["tp1_price"] = pos["entry_price"] * (1.0 + dir_mult * ((1.0 * natr * tp_mod) / 100.0 + cost_rate))
                            pos["tp2_price"] = pos["entry_price"] * (1.0 + dir_mult * ((1.8 * natr * tp_mod) / 100.0 + cost_rate))

                            min_tp3_gap = (0.5 * natr) / 100.0
                            if is_long:
                                pos["tp3_price"] = max(pos["tp3_price"], pos["tp2_price"] * (1.0 + min_tp3_gap))
                            else:
                                pos["tp3_price"] = min(pos["tp3_price"], pos["tp2_price"] * (1.0 - min_tp3_gap))

            # C. DVOJSTUPŇOVÝ TSL ENGINE (Fáza 1: TP1 -> BE, Fáza 2: TP2 -> Trail)
            if not pos.get("tp1_hit", False):
                if (is_long and curr_price >= pos["tp1_price"]) or (not is_long and curr_price <= pos["tp1_price"]):
                    pos["tp1_hit"] = True
                    pos["tsl_active"] = True
                    pos["extreme_price"] = curr_price
                    cost_rate = pos.get("cost_rate", compute_roundtrip_cost_rate(win_id))
                    be_lock = pos["entry_price"] * (1.0 + dir_mult * (cost_rate + 0.0010))
                    if (is_long and be_lock > pos["sl_tsl_price"]) or (not is_long and be_lock < pos["sl_tsl_price"]):
                        pos["sl_tsl_price"] = be_lock
                    db_dirty = True

            if pos.get("tp1_hit", False) and not pos.get("tp2_hit", False):
                if (is_long and curr_price >= pos["tp2_price"]) or (not is_long and curr_price <= pos["tp2_price"]):
                    pos["tp2_hit"] = True
                    pos["extreme_price"] = curr_price
                    db_dirty = True

            if pos.get("tp2_hit", False):
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
                        db_dirty = True
                        continue
                    elif strong_pump:
                        tsl_w_pct = (natr / max(0.2, ofd)) if is_long else (natr * ofd)
                        new_tsl = pos["extreme_price"] * (1.0 - dir_mult * (tsl_w_pct / 100.0))
                        if (is_long and new_tsl > pos["sl_tsl_price"]) or (not is_long and new_tsl < pos["sl_tsl_price"]):
                            pos["sl_tsl_price"] = new_tsl
                            db_dirty = True
                else:
                    step_pct = (0.45 * natr) / 100.0
                    new_tsl = pos["extreme_price"] * (1.0 - dir_mult * step_pct)
                    if (is_long and new_tsl > pos["sl_tsl_price"]) or (not is_long and new_tsl < pos["sl_tsl_price"]):
                        pos["sl_tsl_price"] = new_tsl
                        db_dirty = True

            # D. Vyhodnotenie zásahu SL / TSL (reaguje každú 1 sekundu)
            hit_sl_tsl = (is_long and curr_price <= pos["sl_tsl_price"]) or (not is_long and curr_price >= pos["sl_tsl_price"])
            if hit_sl_tsl:
                reason = "TRAILING_SL_PROFIT" if pos["tsl_active"] else "HARD_SL_HIT"
                close_position(pos, curr_price, reason, vault)
                db_dirty = True
                continue

            # E. Dočasný výpočet PnL do pamäte pozície pre UI
            cost_rate = pos.get("cost_rate", compute_roundtrip_cost_rate(win_id))
            gross_pnl_pct = ((curr_price - pos["entry_price"]) / pos["entry_price"]) * dir_mult * pos["leverage"] * 100.0
            net_pnl_pct = gross_pnl_pct - (cost_rate * pos["leverage"] * 100.0)
            pnl_usd = pos["active_margin"] * (net_pnl_pct / 100.0)
            pos["live_pnl_usd"] = round(pnl_usd, 4)
            pos["live_pnl_pct"] = round(net_pnl_pct, 2)
            pos["last_price"] = curr_price

            # Zápis aktívnej pozície do Supabase len pri zmene stavu alebo raz za 10 sekúnd
            last_pos_sync = pos.get("last_pos_db_sync", 0)
            if db_dirty or (now_ts - last_pos_sync) >= 10.0:
                pos["last_pos_db_sync"] = now_ts
                safe_supabase_upsert("active_positions", {
                    "window_id": win_id, "status": "ACTIVE", "symbol": sym, "direction": pos["direction"],
                    "regime": pos["regime"], "leverage": pos["leverage"], "entry_price": pos["entry_price"],
                    "dca1_price": pos["dca1_price"], "dca2_price": pos["dca2_price"], "dca3_price": pos["dca3_price"],
                    "tp1_price": pos["tp1_price"], "tp2_price": pos["tp2_price"], "tp3_price": pos["tp3_price"],
                    "sl_tsl_price": pos["sl_tsl_price"], "tsl_active": pos["tsl_active"],
                    "pnl_usd": round(pnl_usd, 4), "pnl_percent": round(net_pnl_pct, 2),
                    "mandate_expires_at": pos["mandate_expires_at"], "updated_at": now_dt.isoformat()
                }, vault)

        # 2. Hlavný 3-minútový skener celého trhu (sťahuje všetky tickery a OI len raz za 180s)
        if is_scan_due:
            vault["last_main_scan"] = now_ts
            db_dirty = True

            tickers, oi_map = await fetch_market_snapshot(client)
            update_24h_oi_metrics(tickers, oi_map)

            btc_klines = await fetch_okx_klines(client, "BTCUSDT", "15m", 25, klines_cache)
            btc_veto = evaluate_btc_veto(btc_klines)
            vault["last_btc_veto"] = btc_veto

            active_syms = {p["symbol"] for p in vault["windows"].values() if p["status"] == "ACTIVE" and p["symbol"]}
            sectors_map = get_sectors_map()
            active_secs = {sectors_map.get(s, "UNKNOWN") for s in active_syms}

            # ===== BTC WINDOW 1 – OPRAVENÁ LOGIKA (OFD + funding rovnako ako altcoiny) =====
            if vault["windows"]["1"]["status"] == "WAITING" and len(btc_klines) >= 20:
                last_btc = btc_klines[-1]
                is_15m_green = float(get_close(last_btc)) > float(get_open(last_btc))
                btc_ofd = compute_ofd_from_klines(btc_klines)

                direction = None
                is_trap = False

                # Rovnaká logika smeru ako v evaluate_candidates
                if not is_15m_green and btc_ofd < 0.90:
                    funding_rate = await fetch_okx_funding(client, "BTCUSDT")
                    if funding_rate > 0.0020:
                        direction = "SHORT"
                        is_trap = True
                if direction is None and is_15m_green and btc_ofd > 1.15:
                    funding_rate = await fetch_okx_funding(client, "BTCUSDT")
                    if funding_rate < -0.0050:
                        direction = "LONG"
                        is_trap = True

                if direction is None:
                    if btc_ofd > 1.30 and is_15m_green:
                        direction = "LONG"
                    elif btc_ofd < 0.70 and not is_15m_green:
                        direction = "SHORT"

                if direction and check_anti_extreme_shield(btc_klines, direction, is_pd=False):
                    # Pre BTC používame uvoľnenú verziu (volume 1.05x)
                    ok_exec, exec_price = await verify_execution_confirmation_btc(
                        client, "BTCUSDT", direction, btc_klines, btc_ofd, klines_cache
                    )
                    if ok_exec:
                        setup = build_trade_setup(
                            {
                                "symbol": "BTCUSDT",
                                "direction": direction,
                                "is_pd": False,
                                "is_funding_trap": is_trap,
                                "klines": btc_klines,
                                "exec_price": exec_price
                            },
                            vault["balance"],
                            1
                        )
                        if setup:
                            vault["windows"]["1"] = setup
                            active_syms.add("BTCUSDT")

            if vault["windows"]["2"]["status"] == "WAITING":
                cands_a = await evaluate_candidates(
                    client, tickers, oi_map, active_syms, active_secs,
                    10_000_000, 1e12, btc_veto, klines_cache, is_pd_mode=False
                )
                for c in cands_a:
                    setup = build_trade_setup(c, vault["balance"], 2)
                    if setup:
                        vault["windows"]["2"] = setup
                        active_syms.add(c["symbol"])
                        active_secs.add(c["sector"])
                        break

            if vault["windows"]["3"]["status"] == "WAITING":
                cands_b = await evaluate_candidates(
                    client, tickers, oi_map, active_syms, active_secs,
                    5_000_000, 9_999_999, btc_veto, klines_cache, is_pd_mode=False
                )
                for c in cands_b:
                    setup = build_trade_setup(c, vault["balance"], 3)
                    if setup:
                        vault["windows"]["3"] = setup
                        active_syms.add(c["symbol"])
                        active_secs.add(c["sector"])
                        break

            if vault["windows"]["4"]["status"] == "WAITING":
                cands_c = await evaluate_candidates(
                    client, tickers, oi_map, active_syms, active_secs,
                    1_000_000, 4_999_999, btc_veto, klines_cache, is_pd_mode=True
                )
                for c in cands_c:
                    setup = build_trade_setup(c, vault["balance"], 4)
                    if setup:
                        vault["windows"]["4"] = setup
                        break

    # Uloženie trezoru: lokálne vždy (0 ms), do cloudu Supabase len pri zmene alebo raz za 10 sekúnd
    need_cloud_sync = db_dirty or (now_ts - vault.get("last_db_sync", 0)) >= 10.0
    if need_cloud_sync:
        flush_pending_sync(vault)
    save_vault(vault, sync_cloud=need_cloud_sync)


def close_position(pos: dict, exit_price: float, reason: str, vault: dict):
    win_id = pos["window_id"]
    dir_mult = 1.0 if pos["direction"] == "LONG" else -1.0
    cost_rate = pos.get("cost_rate", compute_roundtrip_cost_rate(win_id))
    gross_pnl_pct = ((exit_price - pos["entry_price"]) / pos["entry_price"]) * dir_mult * pos["leverage"] * 100.0
    net_pnl_pct = gross_pnl_pct - (cost_rate * pos["leverage"] * 100.0)
    pnl_usd = pos["active_margin"] * (net_pnl_pct / 100.0)
    vault["balance"] = round(vault["balance"] + pnl_usd, 4)
    is_win = pnl_usd > 0
    r_mult = pnl_usd / max(0.01, pos["margin_usd"])
    update_ai_learning(pos["symbol"], pos["direction"], pos["regime"], pos["is_funding_trap"], is_win, r_mult, vault)
    hist_row = {
        "window_id": win_id, "symbol": pos["symbol"], "direction": pos["direction"],
        "regime": pos["regime"], "leverage": pos["leverage"], "entry_price": pos["entry_price"],
        "exit_price": exit_price, "margin_usd": pos["active_margin"], "pnl_usd": round(pnl_usd, 4),
        "pnl_percent": round(net_pnl_pct, 2), "exit_reason": reason, "current_balance": vault["balance"]
    }
    try:
        supabase.table("trade_history").insert(hist_row).execute()
    except Exception:
        vault["pending_supabase_sync"].append({"table": "trade_history", "op": "insert", "data": hist_row})
    vault["windows"][str(win_id)] = {"status": "WAITING", "symbol": "BTCUSDT" if win_id == 1 else None, "mandate_until": 0}
    safe_supabase_upsert("active_positions", {
        "window_id": win_id, "status": "WAITING", "symbol": None, "entry_price": 0.0,
        "tp1_price": 0.0, "tp2_price": 0.0, "tp3_price": 0.0, "sl_tsl_price": 0.0,
        "pnl_usd": 0.0, "pnl_percent": 0.0, "updated_at": datetime.now(timezone.utc).isoformat()
    }, vault)
    save_vault(vault, sync_cloud=True)


# =========================================================================
# SPUSTENIE NEPRETRŽITÉHO JADRA (1-SEKUNDOVÝ ODĽAHČENÝ CYKLUS)
# =========================================================================

st.title("🦈 Shark Alpha Core (Cloud 1 - OKX 1s Turbo Engine)")
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

cycle_start = time.time()
try:
    asyncio.run(run_core_cycle(vault_state))
    elapsed_ms = int((time.time() - cycle_start) * 1000)
    st.success(f"⚡ 1s Cyklus dokončený za {elapsed_ms} ms ({datetime.now(timezone.utc).strftime('%H:%M:%S')} UTC)")
except Exception as e:
    st.error(f"⚠️ Chyba cyklu: {e}")

st.json(vault_state["windows"])

# Dynamické čakanie tak, aby celkový takt cyklu bol presne ~1.0 sekunda
sleep_time = max(0.15, 1.0 - (time.time() - cycle_start))
time.sleep(sleep_time)
st.rerun()
