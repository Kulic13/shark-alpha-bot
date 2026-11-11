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
            "4": {"status": "WAITING", "symbol": None, "mandate_until": 0},
            "5": {"status": "WAITING", "symbol": None, "mandate_until": 0},
            "6": {"status": "WAITING", "symbol": None, "mandate_until": 0}
        },
        "last_main_scan": 0,
        "last_db_sync": 0,
        "last_heartbeat": 0,
        "last_btc_veto": {
            "veto_long": False,
            "veto_short": False,
            "btc_1h_pct": 0.0,
            "btc_15m_pct": 0.0
        },
        "natr_history_30d": [],  # Plovoucí 30denní historie pro výpočet invariantních percentilů
        "pending_supabase_sync": []
    }

def load_vault() -> dict:
    if os.path.exists(VAULT_FILE):
        try:
            with open(VAULT_FILE, "r", encoding="utf-8") as f:
                state = json.load(f)
                if "windows" in state and "balance" in state:
                    state.setdefault("last_main_scan", 0)
                    state.setdefault("last_db_sync", 0)
                    state.setdefault("last_heartbeat", 0)
                    state.setdefault("pending_supabase_sync", [])
                    state.setdefault("natr_history_30d", [])
                    state.setdefault("last_btc_veto", {
                        "veto_long": False,
                        "veto_short": False,
                        "btc_1h_pct": 0.0,
                        "btc_15m_pct": 0.0
                    })
                    # Zajištění 6okenní zpětné kompatibility lokální cache
                    for w_key in ["1", "2", "3", "4", "5", "6"]:
                        if w_key not in state["windows"]:
                            state["windows"][w_key] = {"status": "WAITING", "symbol": "BTCUSDT" if w_key == "1" else None, "mandate_until": 0}
                    return state
        except Exception:
            pass

    try:
        res = supabase.table("bot_vault_state").select("state_json").eq("id", 1).execute()
        if res.data and len(res.data) > 0 and res.data[0].get("state_json"):
            state = res.data[0]["state_json"]
            state.setdefault("last_main_scan", 0)
            state.setdefault("last_db_sync", 0)
            state.setdefault("last_heartbeat", 0)
            state.setdefault("pending_supabase_sync", [])
            state.setdefault("natr_history_30d", [])
            state.setdefault("last_btc_veto", {
                "veto_long": False,
                "veto_short": False,
                "btc_1h_pct": 0.0,
                "btc_15m_pct": 0.0
            })
            for w_key in ["1", "2", "3", "4", "5", "6"]:
                if w_key not in state["windows"]:
                    state["windows"][w_key] = {"status": "WAITING", "symbol": "BTCUSDT" if w_key == "1" else None, "mandate_until": 0}
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
    """
    Upsert do Supabase.
    Pre ai_mmr_matrix musí ísť conflict na (regime_x, regime_y),
    inak vznikajú duplicitné riadky a 'obrovská matica'.
    """
    try:
        if table == "ai_mmr_matrix":
            supabase.table(table).upsert(data, on_conflict="regime_x,regime_y").execute()
        elif table == "active_positions":
            supabase.table(table).upsert(data, on_conflict="window_id").execute()
        else:
            supabase.table(table).upsert(data).execute()
    except Exception:
        vault.setdefault("pending_supabase_sync", []).append({
            "table": table,
            "op": "upsert",
            "data": data
        })


def push_active_position_row(pos: dict, vault: dict, extra: dict | None = None):
    """Okamžitý zápis ACTIVE pozície do Supabase vrátane margin_usd / active_margin (Kelly)."""
    row = {
        "window_id": int(pos["window_id"]),
        "status": "ACTIVE",
        "symbol": pos.get("symbol"),
        "direction": pos.get("direction"),
        "regime": pos.get("regime"),
        "leverage": pos.get("leverage"),
        "entry_price": pos.get("entry_price"),
        "margin_usd": float(pos.get("margin_usd") or 0.0),
        "active_margin": float(pos.get("active_margin") or 0.0),
        "dca1_price": pos.get("dca1_price", 0.0),
        "dca2_price": pos.get("dca2_price", 0.0),
        "dca3_price": pos.get("dca3_price", 0.0),
        "dca1_filled": bool(pos.get("dca1_filled", False)),
        "dca2_filled": bool(pos.get("dca2_filled", False)),
        "dca3_filled": bool(pos.get("dca3_filled", False)),
        "tp1_price": pos.get("tp1_price", 0.0),
        "tp2_price": pos.get("tp2_price", 0.0),
        "tp3_price": pos.get("tp3_price", 0.0),
        "tp1_hit": bool(pos.get("tp1_hit", False)),
        "tp2_hit": bool(pos.get("tp2_hit", False)),
        "sl_tsl_price": pos.get("sl_tsl_price", 0.0),
        "tsl_active": bool(pos.get("tsl_active", False)),
        "pnl_usd": float(pos.get("live_pnl_usd") or pos.get("pnl_usd") or 0.0),
        "pnl_percent": float(pos.get("live_pnl_pct") or pos.get("pnl_percent") or 0.0),
        "mandate_expires_at": pos.get("mandate_expires_at"),
        "opened_at": pos.get("opened_at"),
        "updated_at": datetime.now(timezone.utc).isoformat(),
        # Verzia 2.0 Infrastruktúrne priradenie klientských ID pre AMEND protokoly
        "okx_entry_order_id": pos.get("okx_entry_order_id"),
        "okx_sl_order_id": pos.get("okx_sl_order_id"),
        "okx_tp1_order_id": pos.get("okx_tp1_order_id"),
        "okx_tp2_order_id": pos.get("okx_tp2_order_id"),
        "okx_tp3_order_id": pos.get("okx_tp3_order_id")
    }
    if extra:
        row.update(extra)
    safe_supabase_upsert("active_positions", row, vault)


def push_waiting_window_row(window_id: int, vault: dict):
    """Nastaví okno v active_positions na WAITING (vyčistené)."""
    row = {
        "window_id": int(window_id),
        "status": "WAITING",
        "symbol": "BTCUSDT" if int(window_id) == 1 else None,
        "direction": None,
        "regime": None,
        "leverage": None,
        "entry_price": None,
        "margin_usd": 0.0,
        "active_margin": 0.0,
        "dca1_price": 0.0,
        "dca2_price": 0.0,
        "dca3_price": 0.0,
        "dca1_filled": False,
        "dca2_filled": False,
        "dca3_filled": False,
        "tp1_price": 0.0,
        "tp2_price": 0.0,
        "tp3_price": 0.0,
        "tp1_hit": False,
        "tp2_hit": False,
        "sl_tsl_price": 0.0,
        "tsl_active": False,
        "pnl_usd": 0.0,
        "pnl_percent": 0.0,
        "mandate_expires_at": None,
        "opened_at": None,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "okx_entry_order_id": None,
        "okx_sl_order_id": None,
        "okx_tp1_order_id": None,
        "okx_tp2_order_id": None,
        "okx_tp3_order_id": None,
    }
    safe_supabase_upsert("active_positions", row, vault)


def sync_all_windows_to_db(vault: dict):
    """
    Kompletná synchronizácia 6 okien vault → active_positions.
    Volá sa na konci každého cyklu, aby cockpit vždy videl rovnaký stav ako core1.
    """
    windows = vault.get("windows", {})
    for key in ("1", "2", "3", "4", "5", "6"):
        pos = windows.get(key) or {}
        status = pos.get("status", "WAITING")
        wid = int(key)
        if status == "ACTIVE" and pos.get("symbol"):
            # doplň window_id ak chýba
            if "window_id" not in pos:
                pos = dict(pos)
                pos["window_id"] = wid
            push_active_position_row(pos, vault)
        else:
            push_waiting_window_row(wid, vault)


def flush_pending_sync(vault: dict):
    if not vault.get("pending_supabase_sync"):
        return
    remaining = []
    for item in vault["pending_supabase_sync"]:
        try:
            if item["op"] == "upsert":
                tbl = item["table"]
                data = item["data"]
                if tbl == "ai_mmr_matrix":
                    supabase.table(tbl).upsert(data, on_conflict="regime_x,regime_y").execute()
                elif tbl == "active_positions":
                    supabase.table(tbl).upsert(data, on_conflict="window_id").execute()
                else:
                    supabase.table(tbl).upsert(data).execute()
            elif item["op"] == "insert":
                supabase.table(item["table"]).insert(item["data"]).execute()
        except Exception:
            remaining.append(item)
    vault["pending_supabase_sync"] = remaining


# =========================================================================
# BLOK 5: INVARIANTNÁ AI MATICA REŽIMOV (MMR) & KNN PROSTOROVÉ VYHLAZOVÁNÍ
# =========================================================================
MAPA_SUSEDNOSTI = {
    (1, 1): [(1, 2), (2, 1)],
    (1, 2): [(1, 1), (1, 3), (2, 2)],
    (1, 3): [(1, 2), (2, 3)],
    (2, 1): [(1, 1), (2, 2), (3, 1)],
    (2, 2): [(1, 2), (2, 1), (2, 3), (3, 2)],
    (2, 3): [(1, 3), (2, 2), (3, 3)],
    (3, 1): [(2, 1), (3, 2)],
    (3, 2): [(2, 2), (3, 1), (3, 3)],
    (3, 3): [(3, 2), (2, 3)]
}

def urci_trend_x(btc_klines: list) -> int:
    """Osa X (Trendový režim): Diskretizace sklonu regresní přímky do stavů 1 (BEAR), 2 (SIDE), 3 (BULL)"""
    if len(btc_klines) < 10:
        return 2
    closes = [float(get_close(k)) for k in btc_klines[-10:]]
    x = np.arange(len(closes))
    slope = np.polyfit(x, closes, 1)[0]
    norm_slope = slope / closes[0] * 100.0
    if norm_slope < -0.33:
        return 1  # BEAR
    elif norm_slope > 0.33:
        return 3  # BULL
    return 2  # SIDE

def urci_volatilitu_y(natr: float, vault: dict) -> int:
    """Osa Y (Volatilitní index): Dynamické dělení pomocí plovoucích 30denních percentilů (1=LOW, 2=MID, 3=HIGH)"""
    hist = vault.get("natr_history_30d", [])
    if len(hist) < 15:
        # Fallback pokud není naplněna plovoucí historie
        if natr <= 1.5: return 1
        elif natr <= 3.0: return 2
        return 3
    p33 = np.percentile(hist, 33)
    p66 = np.percentile(hist, 66)
    if natr <= p33:
        return 1  # LOW
    elif natr <= p66:
        return 2  # MID
    return 3  # HIGH

def get_ai_mmr_cell(rx: int, ry: int) -> dict:
    """Načtení globální statistické buňky z tabulky ai_mmr_matrix namísto separátních mincí"""
    default_cell = {
        "regime_x": rx, "regime_y": ry,
        "win_rate": 52.0, "total_trades": 0,
        "avg_win_r": 1.5, "avg_loss_r": 1.0,
        "sl_modifier": 1.00, "tp_modifier": 1.00
    }
    try:
        res = supabase.table("ai_mmr_matrix").select("*").eq("regime_x", rx).eq("regime_y", ry).execute()
        if res.data and len(res.data) > 0:
            return res.data[0]
    except Exception:
        pass
    return default_cell

def update_ai_learning_mmr(rx: int, ry: int, is_win: bool, r_multiple: float, vault: dict):
    """KNN Spatial Smoothing: Výsledek obchodu se zapíše do cílové buňky a s váhou 20% prosákne do sousedů"""
    bunky_k_aktualizaci = [(rx, ry, 1.0)]  # (rx, ry, vaha_prostory)
    if (rx, ry) in MAPA_SUSEDNOSTI:
        for s_rx, s_ry in MAPA_SUSEDNOSTI[(rx, ry)]:
            bunky_k_aktualizaci.append((s_rx, s_ry, 0.20))
    for target_x, target_y, vaha_prostory in bunky_k_aktualizaci:
        cell = get_ai_mmr_cell(target_x, target_y)
        n = int(cell.get("total_trades", 0))
        # Adaptivní decay faktor podle zaplnění buňky: rychlé učení na začátku, stabilita v saturaci
        gamma = 0.05
        alfa_baze = 1.0 / (1.0 + gamma * n)
        alfa = alfa_baze * vaha_prostory
        outcome_wr = 100.0 if is_win else 0.0
        cell["win_rate"] = float((cell["win_rate"] * (1.0 - alfa)) + (outcome_wr * alfa))
        if is_win:
            cell["avg_win_r"] = float((cell["avg_win_r"] * (1.0 - alfa)) + (abs(r_multiple) * alfa))
            cell["sl_modifier"] = float(np.clip(cell["sl_modifier"] * (1.0 - 0.03 * vaha_prostory), 0.75, 1.45))
            cell["tp_modifier"] = float(np.clip(cell["tp_modifier"] * (1.0 + 0.02 * vaha_prostory), 0.75, 1.45))
        else:
            cell["avg_loss_r"] = float((cell["avg_loss_r"] * (1.0 - alfa)) + (abs(r_multiple) * alfa))
            cell["sl_modifier"] = float(np.clip(cell["sl_modifier"] * (1.0 + 0.06 * vaha_prostory), 0.75, 1.45))
            cell["tp_modifier"] = float(np.clip(cell["tp_modifier"] * (1.0 - 0.02 * vaha_prostory), 0.75, 1.45))
        if vaha_prostory == 1.0:
            cell["total_trades"] = n + 1
        cell["updated_at"] = datetime.now(timezone.utc).isoformat()
        safe_supabase_upsert("ai_mmr_matrix", cell, vault)


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

_INSTRUMENT_CACHE: dict = {}
_INSTRUMENT_CACHE_TS: float = 0.0
_INSTRUMENT_CACHE_TTL: float = 3600.0

def fetch_okx_instrument_spec(symbol: str) -> dict | None:
    global _INSTRUMENT_CACHE, _INSTRUMENT_CACHE_TS
    now = time.time()
    if (now - _INSTRUMENT_CACHE_TS) > _INSTRUMENT_CACHE_TTL:
        _INSTRUMENT_CACHE = {}
        _INSTRUMENT_CACHE_TS = now
    if symbol in _INSTRUMENT_CACHE:
        return _INSTRUMENT_CACHE[symbol]
    inst_id = to_okx_inst_id(symbol)
    try:
        with httpx.Client(timeout=6.0) as client:
            res = client.get(
                f"{OKX_API}/api/v5/public/instruments",
                params={"instType": "SWAP", "instId": inst_id}
            )
            if res.status_code != 200:
                return None
            data = res.json()
            if data.get("code") != "0" or not data.get("data"):
                return None
            item = data["data"][0]
            spec = {
                "min_sz": float(item.get("minSz") or 0),
                "lot_sz": float(item.get("lotSz") or 0),
                "ct_val": float(item.get("ctVal") or 0),
                "ct_mult": float(item.get("ctMult") or 1),
                "tick_sz": float(item.get("tickSz") or 0),
            }
            _INSTRUMENT_CACHE[symbol] = spec
            return spec
    except Exception:
        return None

def okx_min_notional_usd(symbol: str, price: float) -> float:
    if price <= 0:
        return 5.2
    spec = fetch_okx_instrument_spec(symbol)
    if not spec or spec["min_sz"] <= 0 or spec["ct_val"] <= 0:
        return 5.2
    min_notional = spec["min_sz"] * spec["ct_val"] * spec.get("ct_mult", 1.0) * price
    return max(float(min_notional), 1.0)

async def fetch_single_ticker(client: httpx.AsyncClient, symbol: str) -> dict | None:
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
    if not symbols:
        return {}
    unique_syms = list(set(symbols))
    tasks = [fetch_single_ticker(client, s) for s in unique_syms]
    results = await asyncio.gather(*tasks)
    return {sym: res for sym, res in zip(unique_syms, results) if res is not None}

async def fetch_market_snapshot(client: httpx.AsyncClient):
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
    cache_key = f"{symbol}{bar}{limit}"
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
# BLOK 3B / 4: HLOUBKOVÁ MIKROSTRUKTÚRA OKX REST-EMULÁCIA (Knihy Depth-20, CVD-5, VPIN)
# =========================================================================

async def fetch_hloubkova_mikrostruktura(client: httpx.AsyncClient, symbol: str) -> dict:
    """
    Simulace a výpočet asynchronního hloubkového vnímání knihy řádu (Depth 20) z OKX veřejného REST.
    - Vážený Order Book (WOB): Exponenciální útlum váhy podle vzdálenosti od středu (1/k).
    - OBI Imbalance: Výsledný tlak pasivních zdí v knize.
    - CVD a VPIN: Výpočet čisté agresivity trhu a toxicity toku objednávek.
    """
    inst_id = to_okx_inst_id(symbol)
    res = {
        "obi_hluboke": 1.0,
        "neblizsi_stena_sl": 0.0,
        "cvd_agresivita": 0.0,
        "vpin_toxicita": 0.20
    }
    try:
        ob_res = await client.get(f"{OKX_API}/api/v5/market/books", params={"instId": inst_id, "sz": "20"})
        if ob_res.status_code == 200:
            ob_data = ob_res.json()
            if ob_data.get("code") == "0" and ob_data.get("data"):
                book = ob_data["data"][0]
                bids = book.get("bids", [])
                asks = book.get("asks", [])
                w_bid = sum(float(b[1]) * (1.0 / (k + 1)) for k, b in enumerate(bids[:20]))
                w_ask = sum(float(a[1]) * (1.0 / (k + 1)) for k, a in enumerate(asks[:20]))
                res["obi_hluboke"] = w_bid / max(1e-9, w_ask)
                # Detekce nejsilnější pasivní zdi pro Shadow OBI Stop Loss Barrier
                if bids and asks:
                    if w_bid > w_ask:
                        max_bid_idx = np.argmax([float(b[1]) for b in bids[:20]])
                        res["neblizsi_stena_sl"] = float(bids[max_bid_idx][0])
                    else:
                        max_ask_idx = np.argmax([float(a[1]) for a in asks[:20]])
                        res["neblizsi_stena_sl"] = float(asks[max_ask_idx][0])
                # CVD (Cumulative Volume Delta) za posledních 5 svíček
                klines = await fetch_okx_klines(client, symbol, "15m", 6)
                if len(klines) >= 5:
                    delta_suma = 0.0
                    total_v = 0.0
                    for k in klines[-5:]:
                        h = float(get_high(k))
                        l = float(get_low(k))
                        c = float(get_close(k))
                        v = float(get_vol(k))
                        rng = max(1e-12, h - l)
                        buyer_ratio = (c - l) / rng
                        delta_suma += v * buyer_ratio - v * (1.0 - buyer_ratio)
                        total_v += v
                    res["cvd_agresivita"] = delta_suma
                    # VPIN (Volume-Synchronized Probability of Toxicity) aproximace přes absolutní delty
                    res["vpin_toxicita"] = abs(delta_suma) / max(1e-9, total_v)
    except Exception:
        pass
    return res


def calculate_natr_14(klines: list) -> float:
    """
    Skutočný Normalized ATR (NATR) z 15m sviečok.
    True Range = max(high-low, abs(high-prev_close), abs(low-prev_close))
    ATR_14 = SMA(TR, 14)
    NATR = (ATR_14 / close) * 100
    """
    if len(klines) < 15:
        return 2.0  # bezpečný fallback
    trs = []
    for i in range(1, len(klines)):
        h = float(get_high(klines[i]))
        l = float(get_low(klines[i]))
        prev_c = float(get_close(klines[i - 1]))
        tr = max(h - l, abs(h - prev_c), abs(l - prev_c))
        trs.append(tr)
    if len(trs) < 14:
        return 2.0
    atr_14 = float(np.mean(trs[-14:]))
    last_close = float(get_close(klines[-1]))
    if last_close <= 0:
        return 2.0
    natr = (atr_14 / last_close) * 100.0
    return float(natr)


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
    # Verzia 2.0 Inkorporace hluboké mikrostruktury do exekuce
    micro = await fetch_hloubkova_mikrostruktura(client, symbol)
    obi = micro["obi_hluboke"]
    if direction == "LONG" and obi < 0.85:
        return False, 0.0
    if direction == "SHORT" and obi > 1.18:
        return False, 0.0
    ticker = await fetch_single_ticker(client, symbol)
    if not ticker:
        return False, 0.0
    exec_price = float(ticker["askPx"]) if direction == "LONG" else float(ticker["bidPx"])
    return True, exec_price

async def verify_execution_confirmation_btc(
    client: httpx.AsyncClient,
    symbol: str,
    direction: str,
    klines_15m: list,
    ofd: float,
    cache: dict
) -> tuple[bool, float]:
    if len(klines_15m) < 12:
        return False, 0.0
    vols = [float(get_vol(k)) for k in klines_15m[-11:]]
    avg_prev_vol = float(np.mean(vols[:-1])) if len(vols) > 1 else 0.0
    curr_vol = vols[-1]
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
    micro = await fetch_hloubkova_mikrostruktura(client, symbol)
    obi = micro["obi_hluboke"]
    if direction == "LONG" and obi <= 1.15:
        return False, 0.0
    if direction == "SHORT" and obi >= 0.85:
        return False, 0.0
    ticker = await fetch_single_ticker(client, symbol)
    if not ticker:
        return False, 0.0
    exec_price = float(ticker["askPx"]) if direction == "LONG" else float(ticker["bidPx"])
    return True, exec_price

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

def check_anti_extreme_shield(klines: list, direction: str, is_pd: bool = False, is_btc: bool = False) -> bool:
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
    if is_pd:
        max_atr_dist = 2.2
        rsi_high, rsi_low = 78.0, 22.0
        skip_bb = True
    elif is_btc:
        max_atr_dist = 1.8
        rsi_high, rsi_low = 75.0, 25.0
        skip_bb = True
    else:
        max_atr_dist = 1.3
        rsi_high, rsi_low = 68.0, 32.0
        skip_bb = False
    bb_upper = ema_20 + (2.0 * std_20)
    bb_lower = ema_20 - (2.0 * std_20)
    if direction == "LONG":
        if (last_c - ema_20) > (max_atr_dist * atr_14):
            return False
        if not skip_bb and last_c > bb_upper:
            return False
    else:
        if (ema_20 - last_c) > (max_atr_dist * atr_14):
            return False
        if not skip_bb and last_c < bb_lower:
            return False
    rsi_14 = calculate_rsi_14(df["c"])
    if not np.isnan(rsi_14):
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
                    # UVOĽNENÝ PUMP & DUMP režim (platí pre všetko okrem BTC)
                    # Pôvodné prahy boli príliš prísne → okno 6 takmer nikdy nevstupovalo.
                    oi_curr = oi_map.get(sym, 0.0) * price
                    oi_avg = float(metrics_24h.get(sym, {}).get("oi_avg_24h", 0.0))
                    if oi_avg <= 0:
                        oi_avg = oi_curr
                    oi_ratio = oi_curr / max(1.0, oi_avg)

                    # LONG: pump / OI expand + agresívny buy flow (uvoľnené)
                    if oi_ratio > 1.25 and ofd > 1.20 and is_15m_green:
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

                    # SHORT: dump / funding trap (uvoľnené – nižší move + nižší funding práh)
                    if price_change_pct > 8.0 and ofd < 0.90 and not is_15m_green:
                        if btc_veto.get("veto_short"):
                            return None
                        funding_rate = await fetch_okx_funding(client, sym)
                        # Funding trap ALEBO čistý dump orderflow
                        if (funding_rate > 0.0010 or ofd < 0.70) and check_anti_extreme_shield(klines, "SHORT", is_pd=True):
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
                                "is_funding_trap": funding_rate > 0.0010,
                                "is_pd": True,
                                "sector": sec,
                                "ofd": ofd,
                                "klines": klines,
                                "exec_price": exec_price
                            }

                    # Ďalší uvoľnený vstup: silný OFD bez extrémneho OI (stále PD okno)
                    if ofd > 1.45 and is_15m_green and price_change_pct > 3.0:
                        if btc_veto.get("veto_long"):
                            return None
                        if not check_anti_extreme_shield(klines, "LONG", is_pd=True):
                            return None
                        ok_exec, exec_price = await verify_execution_confirmation(
                            client, sym, "LONG", klines, ofd, True, klines_cache
                        )
                        if not ok_exec:
                            return None
                        sas_score = ofd * (abs(price_change_pct) + 1.0) * 40.0
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
                    if ofd < 0.60 and not is_15m_green and price_change_pct < -3.0:
                        if btc_veto.get("veto_short"):
                            return None
                        if not check_anti_extreme_shield(klines, "SHORT", is_pd=True):
                            return None
                        ok_exec, exec_price = await verify_execution_confirmation(
                            client, sym, "SHORT", klines, ofd, True, klines_cache
                        )
                        if not ok_exec:
                            return None
                        sas_score = (1.0 / max(0.1, ofd)) * (abs(price_change_pct) + 1.0) * 40.0
                        return {
                            "symbol": sym,
                            "direction": "SHORT",
                            "score": sas_score,
                            "is_funding_trap": False,
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
# BLOK 4: ADAPTÍVNY RISK ENGINE, VETO FILTR, DRAWDOWN CONTROL & TWAP ENTRY
# =========================================================================

def compute_roundtrip_cost_rate(window_id: int) -> float:
    exchange_fees = 0.0010
    # Diferencované ocenění slippage podle strukturální likvidity oken 2.0
    if window_id in (1, 2, 3):
        slippage = 0.0005  # Vysoká likvidita
    elif window_id in (4, 5):
        slippage = 0.0012  # Stredne-nízka likvidita (5M - 9.9M)
    else:
        slippage = 0.0020  # Nízka likvidita / Pump-Dump (Okno 6)
    return exchange_fees + slippage

def get_mmr_coordinates(natr: float, vault: dict) -> tuple[int, int]:
    """
    Dynamická klasifikace tržního režimu do 3x3 matice (MMR).
    Osa X (Trend) je určena makro-filtrem z BTC.
    Osa Y (Volatilita) je počítána přes invariantní 30denní percentily.
    """
    btc_veto = vault.get("last_btc_veto", {})
    btc_1h = btc_veto.get("btc_1h_pct", 0.0)
    
    # Osa X: Trendová klasifikace (BEAR = 1, SIDE = 2, BULL = 3)
    if btc_1h < -0.33:
        regime_x = 1
    elif btc_1h > 0.33:
        regime_x = 3
    else:
        regime_x = 2

    # Osa Y: Volatilitní invariantní percentily (LOW = 1, MID = 2, HIGH = 3)
    p_low = vault.get("vol_prah_low", 1.5)
    p_mid = vault.get("vol_prah_mid", 2.5)
    
    if natr <= p_low:
        regime_y = 1
    elif natr <= p_mid:
        regime_y = 2
    else:
        regime_y = 3
        
    return regime_x, regime_y

def build_trade_setup(cand: dict, balance: float, window_id: int, vault: dict) -> dict | None:
    """
    Pokročilý institucionální Risk Engine 2.0 s bayesovským Kellym,
    MMR Veto filtrem, Drawdown Control (Shield Mode) a TWAP Twin Entry přípravou.
    """
    sym = cand["symbol"]
    direction = cand["direction"]
    is_pd = cand["is_pd"]
    klines = cand["klines"]
    last_candle = klines[-1]
    price = float(cand.get("exec_price") or get_close(last_candle))
    
    # 1. Výpočet NATR a kontrola extrémních limitů šumu  (OPRAVENÉ: skutočný ATR/NATR z 15m)
    natr = calculate_natr_14(klines)
    max_natr = 9.0 if is_pd else 5.5
    if natr > max_natr or natr < 0.3 or np.isnan(natr):
        return None

    # 2. Určení souřadnic v MMR matici
    rx, ry = get_mmr_coordinates(natr, vault)
    
    # 3. Načtení AI paměťové buňky z MMR matice (Abstrakce od symbolů)
    mmr_cell = get_mmr_cell(rx, ry)
    cell_wr = float(mmr_cell["win_rate"]) / 100.0
    trades_n = int(mmr_cell["total_trades"])
    
    # Bayesovské vyhlazení pravděpodobnosti (Prior síla = 10 vzorků, Prior WR = 52%)
    prior_strength = 10.0
    prior_wr = 0.52
    wr = ((cell_wr * trades_n) + (prior_wr * prior_strength)) / (trades_n + prior_strength)

    # 4. DRAWDOWN CONTROL & SHIELD MODE INTERCEPTOR
    # Pokud denní ztráta překročí 3 %, aktivuje se krizový štít
    is_shield_active = vault.get("shield_mode_active", False)
    veto_threshold = 0.50 if is_shield_active else 0.435
    
    # MATICOVÉ VETO: Pokud je buňka pod limitem úspěšnosti, Risk Engine příkaz zablokuje
    if wr < veto_threshold:
        return None

    # 5. DYNAMICKÁ VOLBA REGIMU A LEVERAGE PODLE OSY Y
    if is_pd:
        regime = "FUTURES"
        leverage = int(np.clip(round(12.0 / max(natr, 0.5)), 2, 5))
    elif ry == 1:    # LOW_VOL (Klidný trh -> vyšší páka)
        regime = "FUTURES"
        leverage = int(np.clip(round(15.0 / max(natr, 0.5)), 6, 10))
    elif ry == 2:  # MID_VOL (Střední volatilita)
        regime = "FUTURES"
        leverage = int(np.clip(round(14.0 / max(natr, 0.5)), 4, 8))
    else:          # HIGH_VOL (Vysoká volatilita -> přechod na SPOT / bez páky)
        regime = "SPOT"
        leverage = 1

    leverage = int(min(leverage, 10))  # Absolutní hard cap x10

    # 6. BAYESOVSKÝ KELLY CRITERION & PENĚŽNÍ ALOKACE
    b = float(mmr_cell["avg_win_r"]) / max(0.1, float(mmr_cell["avg_loss_r"]))
    q = 1.0 - wr
    full_kelly = (wr * b - q) / max(0.1, b)
    if full_kelly <= 0:
        return None

    # Adaptivní sázkový zlomek: V Shield Mode snižujeme alokaci na polovinu
    kelly_fraction = 0.175 if is_shield_active else 0.35
    max_risk_pct = 0.015 if is_pd else 0.025
    kelly_margin = min(balance * full_kelly * kelly_fraction, balance * max_risk_pct)

    # Ověření reálných limitů OKX burzy
    min_notional = okx_min_notional_usd(sym, price)
    min_margin = min_notional / max(1, leverage)

    if kelly_margin < min_margin:
        if min_margin <= balance * 0.08:
            margin_usd = min_margin
        else:
            return None
    else:
        margin_usd = kelly_margin

    # 7. EXEKUČNÍ DEKOMPOZICE (TWAP TWIN ENTRY VELIKOST)
    # Rozdělení na 60 % pasivní limit (maker) a 40 % aktivní krytí
    if is_pd:
        active_margin = margin_usd
    else:
        active_margin = margin_usd * 0.30
        if (active_margin * max(1, leverage)) < min_notional:
            active_margin = min(min_margin, margin_usd)

    # Sestavení cenových hladin risk managementu
    dir_mult = 1.0 if direction == "LONG" else -1.0
    sl_mod = float(mmr_cell["sl_modifier"])
    tp_mod = float(mmr_cell["tp_modifier"])
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
        
        raw_sl_mult = max(2.25, 2.5 * sl_mod)
        sl_pct = (raw_sl_mult * natr) / 100.0
        
        tp1 = price * (1.0 + dir_mult * ((1.0 * natr * tp_mod) / 100.0 + cost_rate))
        tp2 = price * (1.0 + dir_mult * ((1.8 * natr * tp_mod) / 100.0 + cost_rate))

    sl_price = price * (1.0 - dir_mult * sl_pct)

    # 8. OPRAVA ASYMETRIE LIKVIDAČNÍ ZÓNY (TP3)
    recent_klines = klines[-20:]
    highs = [float(get_high(k)) for k in recent_klines]
    lows = [float(get_low(k)) for k in recent_klines]
    mmr_buffer = 0.004
    min_tp3_gap = (0.5 * natr) / 100.0

    if direction == "LONG":
        liq_ref = min(lows) * (1.0 - (1.0 / 20.0) + mmr_buffer)
        raw_tp3 = liq_ref * 1.005
        tp3 = max(raw_tp3, tp2 * (1.0 + min_tp3_gap))
    else:
        liq_ref = max(highs) * (1.0 + (1.0 / 20.0) - mmr_buffer)
        raw_tp3 = liq_ref * 0.995
        tp3 = min(raw_tp3, tp2 * (1.0 - min_tp3_gap))

    # Generování unikátních klientských ID (clOrdId) pro bleskové AMEND operace
    tx_id = f"sk_{int(time.time()*1000)}"
    initial_ofd = compute_ofd_from_klines(klines)

    return {
        "window_id": window_id,
        "status": "ACTIVE",
        "symbol": sym,
        "direction": direction,
        "regime": regime,
        "regime_x": rx,
        "regime_y": ry,
        "leverage": leverage,
        "is_pd": is_pd,
        "is_funding_trap": cand["is_funding_trap"],
        "natr": natr,
        "cost_rate": cost_rate,
        "tp_mod": tp_mod,
        "margin_usd": margin_usd,
        "active_margin": active_margin,
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
        "tp3_zone_entered_at": None,  # pre min. čas v zóne pred Liquidation Climax
        "opened_at": datetime.now(timezone.utc).isoformat(),  # čas otvorenia pozície (pre cockpit OPEN v-line)
        "mandate_expires_at": (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat(),
        # Injektáž unikátních klientských ID pro atomické změny bez rušení příkazů
        "okx_entry_order_id": f"{tx_id}_ent_{window_id}",
        "okx_sl_order_id": f"{tx_id}_sl_{window_id}",
        "okx_tp1_order_id": f"{tx_id}_tp1_{window_id}",
        "okx_tp2_order_id": f"{tx_id}_tp2_{window_id}",
        "okx_tp3_order_id": f"{tx_id}_tp3_{window_id}"
    }


# =========================================================================
# BLOK 5: GLOBÁLNÍ 3x3 MMR MATICE UČENÍ & KNN SPATIAL SMOOTHING
# =========================================================================

def get_mmr_cell(regime_x: int, regime_y: int) -> dict:
    """
    Abstrakce od symbolů. Načítá statistické parametry úspěšnosti z globální
    3x3 MMR matice na základě trendových (X) a volatilitních (Y) souřadnic.
    """
    default_cell = {
        "regime_x": regime_x,
        "regime_y": regime_y,
        "win_rate": 52.0,
        "avg_win_r": 1.5,
        "avg_loss_r": 1.0,
        "sl_modifier": 1.00,
        "tp_modifier": 1.00,
        "total_trades": 0
    }
    try:
        res = supabase.table("ai_mmr_matrix").select("*").eq("regime_x", regime_x).eq("regime_y", regime_y).execute()
        if res.data and len(res.data) > 0:
            return res.data[0]
        # Pokud buňka v DB chybí (např. při prvním spuštění), inicializujeme ji
        supabase.table("ai_mmr_matrix").insert(default_cell).execute()
    except Exception:
        pass
    return default_cell

def get_mmr_neighbors(rx: int, ry: int) -> list[tuple[int, int]]:
    """
    Definuje mapu sousednosti v rámci 3x3 tenzoru (Čebyševova vzdálenost = 1).
    Vrací seznam souřadnic všech bezprostředně sousedících buněk.
    """
    neighbors = []
    for dx in (-1, 0, 1):
        for dy in (-1, 0, 1):
            if dx == 0 and dy == 0:
                continue  # Vynecháme cílovou buňku samotnou
            nx, ny = rx + dx, ry + dy
            # Kontrola hranic matice 3x3
            if 1 <= nx <= 3 and 1 <= ny <= 3:
                neighbors.append((nx, ny))
    return neighbors

def update_mmr_cell_values(rx: int, ry: int, outcome: float, r_multiple: float, weight: float, vault: dict):
    """
    Matematické jádro učení buňky s adaptivním decay faktorem a prostorovou vahou.
    """
    cell = get_mmr_cell(rx, ry)
    trades_n = int(cell.get("total_trades", 0))
    
    # Adaptivní rychlost učení (alpha) s koeficientem sytosti gamma = 0.05
    # Prázdná buňka se učí skokově, plná dat reaguje stabilně a filtruje šum
    gamma = 0.05
    base_alpha = 1.0 / (1.0 + gamma * trades_n)
    
    # Finální rychlost učení modifikovaná prostorovou vahou (1.0 pro cíl, 0.20 pro sousedy)
    alpha = base_alpha * weight
    
    # Výpočet inverzního decay (retence paměti)
    decay = 1.0 - alpha
    
    # 1. Update Win Rate
    cell["win_rate"] = float((cell["win_rate"] * decay) + (outcome * alpha))
    
    # 2. Update R-Multiples a asymetrických modifikátorů řízení rizik
    if outcome == 100.0:  # WIN
        cell["avg_win_r"] = float((cell["avg_win_r"] * decay) + (abs(r_multiple) * alpha))
        # Zvýšení ziskovosti utažením SL a mírným roztažením TP
        cell["sl_modifier"] = float(np.clip(cell["sl_modifier"] * (1.0 - 0.03 * weight), 0.75, 1.45))
        cell["tp_modifier"] = float(np.clip(cell["tp_modifier"] * (1.0 + 0.02 * weight), 0.75, 1.45))
    else:  # LOSS
        cell["avg_loss_r"] = float((cell["avg_loss_r"] * decay) + (abs(r_multiple) * alpha))
        # Preventivní utažení risk managementu při inkasování ztráty
        cell["sl_modifier"] = float(np.clip(cell["sl_modifier"] * (1.0 + 0.06 * weight), 0.75, 1.45))
        cell["tp_modifier"] = float(np.clip(cell["tp_modifier"] * (1.0 - 0.02 * weight), 0.75, 1.45))
        
    # Počet obchodů zvyšujeme pouze v cílové buňce, kde reálně k exekuci došlo
    if weight == 1.0:
        cell["total_trades"] = trades_n + 1
        
    cell["last_update"] = datetime.now(timezone.utc).isoformat()
    safe_supabase_upsert("ai_mmr_matrix", cell, vault)

def update_ai_learning(
    symbol: str,       # Zachováno pro logování/zpětnou kompatibilitu, učení je však vázáno na MMR
    direction: str,    # Směr pozice
    regime: str,       # Typ exekuce (SPOT/FUTURES)
    is_funding_trap: bool,
    is_win: bool,
    r_multiple: float,
    vault: dict
):
    """
    Hlavní post-trade modul učení 2.0. Kompletně odstraňuje izolované učení mincí.
    Implementuje prostorové přelévání informací (KNN Spatial Smoothing) do okolních režimů.
    """
    # Získání souřadnic MMR, ve kterých byl obchod exekuován
    # Tyto souřadnice si pozice nese ve svém stavu od momentu vstupu
    active_windows = vault.get("windows", {})
    
    # Vyhledáme souřadnice z běžícího setupu (případně fallback na aktuální tržní stav)
    rx = vault.get("last_active_rx", 2)
    ry = vault.get("last_active_ry", 2)
    
    for win_data in active_windows.values():
        if win_data.get("symbol") == symbol and "regime_x" in win_data:
            rx = int(win_data["regime_x"])
            ry = int(win_data["regime_y"])
            break

    outcome = 100.0 if is_win else 0.0

    # 1. ZÁPIS DO CÍLOVÉ BUŇKY TRŽNÍHO REŽIMU (Plná váha = 100 %)
    update_mmr_cell_values(rx, ry, outcome, r_multiple, 1.0, vault)
    
    # 2. PROSTOROVÉ PŘELÉVÁNÍ INFORMACÍ (KNN Spatial Smoothing - Sousední buňky = 20 %)
    # Ztráta/Zisk v jednom režimu preventivně ovlivní chování bota v příbuzných tržních strukturách
    neighbors = get_mmr_neighbors(rx, ry)
    for nx, ny in neighbors:
        update_mmr_cell_values(nx, ny, outcome, r_multiple, 0.20, vault)


# =========================================================================
# BLOK 3A: POKROČILÁ MIKROSTRUKTURA, VÁŽENÝ ORDER BOOK & CVD ENGINE
# =========================================================================

def calculate_weighted_order_book_imbalance(book_data: dict) -> float:
    """
    Matematický popis Váženého Order Booku (WOB) z hloubky 20.
    Přiřazuje váhu w_k = 1 / k každé úrovni knihy objednávek.
    Detekuje reálný pasivní tlak institucionálních zdí blízko spreadu.
    Vrací Imbalance I_OB v intervalu [-1.0, 1.0].
    """
    asks = book_data.get("asks", [])  # [cena, množství, ...]
    bids = book_data.get("bids", [])
    
    if not asks or not bids:
        return 0.0
        
    wob_ask = 0.0
    wob_bid = 0.0
    
    # Sledování hloubky do maximální úrovně 20
    max_depth = min(20, len(asks), len(bids))
    
    for k in range(1, max_depth + 1):
        weight = 1.0 / k  # Lineární útlum váhy s rostoucí vzdáleností od ceny
        
        # Asks (Nabídka / Prodejní stěny)
        ask_vol = float(asks[k-1][1])
        wob_ask += ask_vol * weight
        
        # Bids (Poptávka / Nákupní stěny)
        bid_vol = float(bids[k-1][1])
        wob_bid += bid_vol * weight
        
    denominator = wob_bid + wob_ask
    if denominator <= 1e-9:
        return 0.0
        
    # Výsledná imbalance: kladná = převaha nákupních zdí, záporná = převaha prodejních zdí
    return (wob_bid - wob_ask) / denominator

def compute_real_cvd_5(klines_15m: list) -> float:
    """
    Měří čistou agresivitu tržních nákupů a prodejů (Cumulative Volume Delta)
    za posledních 5 svíček. Využívá objemové toky a exaktní Close-to-Range polohu.
    """
    if not klines_15m:
        return 0.0
        
    recent_bars = klines_15m[-5:] if len(klines_15m) >= 5 else klines_15m
    total_cvd = 0.0
    
    for bar in recent_bars:
        h = float(get_high(bar))
        l = float(get_low(bar))
        c = float(get_close(bar))
        v = float(get_vol(bar))
        
        candle_range = h - l
        if candle_range <= 1e-12:
            buyer_ratio = 0.5
        else:
            buyer_ratio = (c - l) / candle_range
            
        market_buys = v * buyer_ratio
        market_sells = v * (1.0 - buyer_ratio)
        
        # Delta konkrétní svíčky
        total_cvd += (market_buys - market_sells)
        
    return total_cvd

def calculate_vpin_toxicity(klines_15m: list, volume_bucket_size: float = 10000.0) -> float:
    """
    VPIN Engine (Toxický štít): Počítá pravděpodobnost toxického toku objednávek
    (Volume-Synchronized Probability of Toxicity) synchronizovanou na objemové koše.
    Vrací hodnotu od 0.0 (bez toxicity) do 1.0 (extrémní riziko flash crashe).
    """
    if len(klines_15m) < 10:
        return 0.5
        
    buy_volumes = []
    sell_volumes = []
    
    # Rozpouštění svíček do standardizovaných objemových košů o velikosti V
    for bar in klines_15m:
        h = float(get_high(bar))
        l = float(get_low(bar))
        c = float(get_close(bar))
        v = float(get_vol(bar))
        
        candle_range = max(1e-12, h - l)
        b_ratio = (c - l) / candle_range
        
        buy_volumes.append(v * b_ratio)
        sell_volumes.append(v * (1.0 - b_ratio))
        
    # Výpočet sumy absolutních rozdílů mezi nákupním a prodejním tlakem napříč koši
    absolute_infow_imbalance = 0.0
    total_volume_processed = sum(buy_volumes) + sum(sell_volumes)
    
    for b, s in zip(buy_volumes, sell_volumes):
        absolute_infow_imbalance += abs(b - s)
        
    if total_volume_processed <= 1e-9:
        return 0.0
        
    # Finální VPIN koeficient informovaného/toxického flow
    return absolute_infow_imbalance / total_volume_processed


# =========================================================================
# BLOK 3B: ANTI-BLIND EXEKUCE & CHYTRÉ POTVRZOVÁNÍ SIGNÁLŮ
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
    """
    Verifikace exekuce 2.0: Spojuje asynchronní datové mikroskopy z OKX hloubky 20.
    Filtruje slippage, kontroluje WOB Imbalanci a aktivuje toxický VPIN štít.
    """
    if len(klines_15m) < 12:
        return False, 0.0

    # 1. Objemový filtr aktivity (Ochrana proti šumu a nelikvidním fázím)
    vols = [float(get_vol(k)) for k in klines_15m[-11:]]
    avg_prev_vol = float(np.mean(vols[:-1])) if len(vols) > 1 else 0.0
    curr_vol = vols[-1]
    if avg_prev_vol > 0 and (curr_vol / avg_prev_vol) < 1.10:
        return False, 0.0

    # 2. Makro trendový filtr (1H EMA21)
    klines_1h = await fetch_okx_klines(client, symbol, "1H", 25, cache)
    if len(klines_1h) >= 21:
        closes_1h = pd.Series([float(get_close(k)) for k in klines_1h])
        ema21_1h = float(closes_1h.ewm(span=21, adjust=False).mean().iloc[-1])
        last_1h_close = float(closes_1h.iloc[-1])
        if direction == "LONG" and last_1h_close < ema21_1h and ofd < 1.50:
            return False, 0.0
        if direction == "SHORT" and last_1h_close > ema21_1h and ofd > 0.65:
            return False, 0.0

    # 3. Stažení hloubkové mikrostruktury z OKX (Hloubka 20)
    inst_id = to_okx_inst_id(symbol)
    try:
        ob_res = await client.get(
            f"{OKX_API}/api/v5/market/books",
            params={"instId": inst_id, "sz": "20"}
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

        # 4. Kontrola maximálního povoleného spreadu
        spread_pct = ((best_ask - best_bid) / best_bid) * 100.0
        max_spread = 0.30 if is_pd else 0.18
        if spread_pct > max_spread:
            return False, 0.0

        # 5. Výpočet Váženého Order Booku z hloubky 20 (WOB)
        wob_imbalance = calculate_weighted_order_book_imbalance(book)
        
        # 6. Výpočet toxicity toku příkazů přes VPIN Engine
        vpin_score = calculate_vpin_toxicity(klines_15m)
        if vpin_score > 0.80:  # TOXICKÝ ŠTÍT: Blokování vstupu do vysoce toxického orderflow
            return False, 0.0

        # Verifikace směrového tlaku pomocí mikrostruktury zdí (WOB)
        if direction == "LONG" and wob_imbalance < -0.15:  # Prodejní zdi jsou příliš silné
            return False, 0.0
        if direction == "SHORT" and wob_imbalance > 0.15:  # Nákupní stěny blokují propad ceny
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
    """
    Přísná institucionální verze pro BTC (Makrokotva).
    Vyžaduje hloubkovou shodu WOB imbalance z 20 úrovní knihy a nízký VPIN.
    """
    if len(klines_15m) < 12:
        return False, 0.0

    vols = [float(get_vol(k)) for k in klines_15m[-11:]]
    avg_prev_vol = float(np.mean(vols[:-1])) if len(vols) > 1 else 0.0
    curr_vol = vols[-1]
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
            params={"instId": inst_id, "sz": "20"}
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
        if spread_pct > 0.12:  # Přísnější spread limit pro makrokotvu BTC
            return False, 0.0

        wob_imbalance = calculate_weighted_order_book_imbalance(book)
        vpin_score = calculate_vpin_toxicity(klines_15m)
        
        if vpin_score > 0.70:  # Pro BTC je toxický práh stažen ještě níže
            return False, 0.0

        # Přísné hloubkové ověření pasivního flow pro BTC
        if direction == "LONG" and wob_imbalance <= 0.10:  # Vyžaduje čistou nákupní převahu v knize
            return False, 0.0
        if direction == "SHORT" and wob_imbalance >= -0.10: # Vyžaduje čistou prodejní převahu v knize
            return False, 0.0

        exec_price = best_ask if direction == "LONG" else best_bid
        return True, exec_price
    except Exception:
        return False, 0.0


# =========================================================================
# PAPER TRADE HELPERS (žiadne reálne private API volania)
# =========================================================================

def paper_amend_sl(pos: dict, new_px: float, reason: str = ""):
    """Paper-trade: len aktualizuje lokálny stav, žiadny real AMEND na OKX."""
    pos["sl_tsl_price"] = new_px
    # voliteľne logovanie pre debug
    # print(f"[PAPER AMEND SL] {pos.get('symbol')} -> {new_px} ({reason})")


def close_position(pos: dict, exit_price: float, reason: str, vault: dict):
    """
    Paper-trade zatvorenie pozície.
    Počíta PnL, aktualizuje balance, zapisuje do trade_history a uvoľní okno.
    """
    now_dt = datetime.now(timezone.utc)
    dir_mult = 1.0 if pos["direction"] == "LONG" else -1.0
    cost_rate = pos.get("cost_rate", compute_roundtrip_cost_rate(pos["window_id"]))
    leverage = float(pos.get("leverage", 1))
    active_margin = float(pos.get("active_margin", 0.0))
    entry = float(pos.get("entry_price", exit_price))

    gross_pnl_pct = ((exit_price - entry) / entry) * dir_mult * leverage * 100.0
    net_pnl_pct = gross_pnl_pct - (cost_rate * leverage * 100.0)
    pnl_usd = active_margin * (net_pnl_pct / 100.0)

    vault["balance"] = round(float(vault.get("balance", 100.0)) + pnl_usd, 4)

    # R-multiple pre učenie (hrubý odhad)
    sl_dist = abs(entry - float(pos.get("sl_tsl_price", entry))) / entry
    r_mult = (net_pnl_pct / 100.0) / max(sl_dist, 1e-6) if sl_dist > 0 else 0.0

    is_win = pnl_usd > 0
    try:
        update_ai_learning(
            symbol=pos.get("symbol", ""),
            direction=pos.get("direction", ""),
            regime=pos.get("regime", "FUTURES"),
            is_funding_trap=bool(pos.get("is_funding_trap", False)),
            is_win=is_win,
            r_multiple=r_mult,
            vault=vault
        )
    except Exception:
        pass

    hist_row = {
        "window_id": pos["window_id"],
        "symbol": pos["symbol"],
        "direction": pos["direction"],
        "regime": pos.get("regime"),
        "leverage": pos.get("leverage"),
        "entry_price": entry,
        "exit_price": exit_price,
        "margin_usd": round(active_margin, 4),
        "pnl_usd": round(pnl_usd, 4),
        "pnl_percent": round(net_pnl_pct, 2),
        "exit_reason": reason,
        "current_balance": vault["balance"],
        "closed_at": now_dt.isoformat()
    }
    try:
        supabase.table("trade_history").insert(hist_row).execute()
    except Exception:
        vault.setdefault("pending_supabase_sync", []).append({
            "table": "trade_history", "op": "insert", "data": hist_row
        })

    # uvoľnenie okna v lokálnom vault-e
    win_key = str(pos["window_id"])
    vault["windows"][win_key] = {
        "status": "WAITING",
        "symbol": "BTCUSDT" if win_key == "1" else None,
        "mandate_until": 0
    }

    # DÔLEŽITÉ: uvoľniť aj riadok v Supabase active_positions
    # (inak Cloud 2 / cockpit stále vidí starú ACTIVE pozíciu)
    try:
        closed_row = {
            "window_id": int(pos["window_id"]),
            "status": "WAITING",
            "symbol": "BTCUSDT" if win_key == "1" else None,
            "direction": None,
            "regime": None,
            "leverage": None,
            "entry_price": None,
            "margin_usd": 0.0,
            "active_margin": 0.0,
            "dca1_price": 0.0,
            "dca2_price": 0.0,
            "dca3_price": 0.0,
            "dca1_filled": False,
            "dca2_filled": False,
            "dca3_filled": False,
            "tp1_price": 0.0,
            "tp2_price": 0.0,
            "tp3_price": 0.0,
            "tp1_hit": False,
            "tp2_hit": False,
            "sl_tsl_price": 0.0,
            "tsl_active": False,
            "pnl_usd": 0.0,
            "pnl_percent": 0.0,
            "mandate_expires_at": None,
            "opened_at": None,
            "updated_at": now_dt.isoformat(),
            "okx_entry_order_id": None,
            "okx_sl_order_id": None,
            "okx_tp1_order_id": None,
            "okx_tp2_order_id": None,
            "okx_tp3_order_id": None,
        }
        safe_supabase_upsert("active_positions", closed_row, vault)
    except Exception:
        pass


def is_ny_open_lock(now_dt: datetime) -> bool:
    """
    NY Open Lock: blokuje nové vstupy 30 min pred a 30 min po otvorení NY (14:30 UTC približne).
    Paper-trade verzia – jednoduchý časový filter.
    """
    # NYSE open ~ 14:30 UTC (zimný čas) / 13:30 UTC (letný)
    # Používame 14:00 – 15:00 UTC ako ochranné okno
    hour = now_dt.hour
    minute = now_dt.minute
    if hour == 14 and minute >= 0:
        return True
    if hour == 15 and minute < 0:  # prakticky len 14:xx
        return True
    return False


def execute_scale_out(pos: dict, price: float, pct: float, reason: str, vault: dict, now_dt: datetime):
    """Pomocná metoda pro kalkulaci zisku při částečném scale-outu (paper)."""
    dir_mult = 1.0 if pos["direction"] == "LONG" else -1.0
    cost_rate = pos.get("cost_rate", compute_roundtrip_cost_rate(pos["window_id"]))
    gross_pnl = ((price - pos["entry_price"]) / pos["entry_price"]) * dir_mult * pos["leverage"] * 100.0
    net_pnl_pct = gross_pnl - (cost_rate * pos["leverage"] * 100.0)
    closed_margin = pos["active_margin"] * pct
    partial_pnl_usd = closed_margin * (net_pnl_pct / 100.0)
    vault["balance"] = round(vault["balance"] + partial_pnl_usd, 4)
    pos["active_margin"] -= closed_margin
    hist_row = {
        "window_id": pos["window_id"], "symbol": pos["symbol"], "direction": pos["direction"],
        "regime": pos["regime"], "leverage": pos["leverage"], "entry_price": pos["entry_price"],
        "exit_price": price, "margin_usd": round(closed_margin, 4), "pnl_usd": round(partial_pnl_usd, 4),
        "pnl_percent": round(net_pnl_pct, 2), "exit_reason": reason, "current_balance": vault["balance"],
        "closed_at": now_dt.isoformat()
    }
    try:
        supabase.table("trade_history").insert(hist_row).execute()
    except Exception:
        vault.setdefault("pending_supabase_sync", []).append({
            "table": "trade_history", "op": "insert", "data": hist_row
        })


def update_live_pnl_metrics(pos: dict, curr_price: float, dir_mult: float, win_id: int):
    cost_rate = pos.get("cost_rate", compute_roundtrip_cost_rate(win_id))
    gross = ((curr_price - pos["entry_price"]) / pos["entry_price"]) * dir_mult * pos["leverage"] * 100.0
    net = gross - (cost_rate * pos["leverage"] * 100.0)
    pos["live_pnl_usd"] = round(pos["active_margin"] * (net / 100.0), 4)
    pos["live_pnl_pct"] = round(net, 2)
    pos["last_price"] = curr_price


# =========================================================================
# INTEGRACE LOGIKY 2.0: MANAGEMENT POZIC (SHADOW OBI, AMEND, REŽIMOVÝ TSL)
# =========================================================================


# Maximálny čas offline (sekundy). Ak appka spí/offline dlhšie, pozície sa mažú.
STALE_POSITION_MAX_SEC = 600  # 10 minút


def purge_stale_open_positions(vault: dict, reason: str = "APP_OFFLINE_GT_10MIN") -> int:
    """
    Ak od posledného heartbeat-u ubehlo > 10 minút, všetky ACTIVE pozície sa
    vymažú z vault-u aj z active_positions. Nezapisujú sa do trade_history
    (nie je to riadne zatvorenie obchodu, ale výpadok appky).
    Vracia počet zmazaných pozícií.
    """
    now_ts = time.time()
    last_hb = float(vault.get("last_heartbeat") or 0)
    # Ak heartbeat ešte nikdy nebol, nastav teraz a nič nemaž
    if last_hb <= 0:
        vault["last_heartbeat"] = now_ts
        return 0
    if (now_ts - last_hb) <= STALE_POSITION_MAX_SEC:
        return 0

    purged = 0
    windows = vault.get("windows", {})
    for key in ("1", "2", "3", "4", "5", "6"):
        pos = windows.get(key) or {}
        if pos.get("status") == "ACTIVE" and pos.get("symbol"):
            purged += 1
        windows[key] = {
            "status": "WAITING",
            "symbol": "BTCUSDT" if key == "1" else None,
            "mandate_until": 0,
        }
    vault["windows"] = windows
    vault["last_heartbeat"] = now_ts

    # Vyčisti DB active_positions
    try:
        sync_all_windows_to_db(vault)
    except Exception:
        for key in ("1", "2", "3", "4", "5", "6"):
            try:
                push_waiting_window_row(int(key), vault)
            except Exception:
                pass

    try:
        save_vault(vault, sync_cloud=True)
    except Exception:
        save_vault_local(vault)

    return purged


async def run_core_cycle(vault: dict):
    now_ts = time.time()
    now_dt = datetime.now(timezone.utc)
    db_dirty = False

    # Ak appka bola offline > 10 min, zmaž otvorené pozície (bez zápisu do histórie)
    purged = purge_stale_open_positions(vault)
    if purged > 0:
        db_dirty = True

    # Heartbeat – doklad, že core1 práve beží
    vault["last_heartbeat"] = now_ts

    ny_lock = is_ny_open_lock(now_dt)
    vault["ny_open_lock"] = ny_lock

    active_windows = [
        (win_key, pos) for win_key, pos in vault["windows"].items()
        if pos["status"] == "ACTIVE" and pos.get("symbol")
    ]
    last_main_scan = vault.get("last_main_scan", 0)
    is_scan_due = (now_ts - last_main_scan) >= 180

    if not active_windows and not is_scan_due:
        if vault.get("pending_supabase_sync") and (now_ts - vault.get("last_db_sync", 0)) >= 10:
            flush_pending_sync(vault)
            save_vault(vault, sync_cloud=True)
        return

    async with httpx.AsyncClient(http2=True, timeout=8.0) as client:
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
            rx = int(pos.get("regime_x", 2))
            ry = int(pos.get("regime_y", 2))

            # 1. 15s Sběr a vyhodnocení mikrostruktury knihy (Hloubka 20)
            klines = await fetch_okx_klines(client, sym, "15m", 25, klines_cache)
            book_data = {}
            vpin_score = 0.0
            if klines:
                pos["cached_ofd"] = compute_ofd_from_klines(klines)
                vpin_score = calculate_vpin_toxicity(klines)
                
                # Asynchronní dotaz na hloubku 20 pre Shadow OBI a Likvidace
                inst_id = to_okx_inst_id(sym)
                try:
                    ob_res = await client.get(f"{OKX_API}/api/v5/market/books", params={"instId": inst_id, "sz": "20"})
                    if ob_res.status_code == 200:
                        ob_json = ob_res.json()
                        if ob_json.get("code") == "0" and ob_json.get("data"):
                            book_data = ob_json["data"][0]
                except Exception:
                    pass

            ofd = pos.get("cached_ofd", 1.0)

            # A. VPIN EMERGENCY ESCAPE (Toxický štít v protisměru)
            if vpin_score > 0.85:
                close_position(pos, curr_price, "VPIN_EMERGENCY_ESCAPE", vault)
                db_dirty = True
                continue

            # B. SHADOW OBI BARRIER (Dynamické schovávání SL za stěny hloubky 20) - PAPER
            if book_data and not pos.get("tp1_hit", False):
                bids = book_data.get("bids", [])
                asks = book_data.get("asks", [])
                
                # Hledání nejsilnější pasivní zdi v knize
                if is_long and bids:
                    best_wall_px = float(bids[0][0])
                    max_wall_vol = 0.0
                    for b in bids[:20]:
                        if float(b[1]) > max_wall_vol:
                            max_wall_vol = float(b[1])
                            best_wall_px = float(b[0])
                    
                    # Stop Loss schováme přesně 1 tick pod nalezenou zeď
                    shadow_sl = best_wall_px - 0.0001
                    if shadow_sl > pos["sl_tsl_price"] and shadow_sl < curr_price:
                        paper_amend_sl(pos, shadow_sl, "SHADOW_OBI_LONG")
                        db_dirty = True
                
                elif not is_long and asks:
                    best_wall_px = float(asks[0][0])
                    max_wall_vol = 0.0
                    for a in asks[:20]:
                        if float(a[1]) > max_wall_vol:
                            max_wall_vol = float(a[1])
                            best_wall_px = float(a[0])
                            
                    shadow_sl = best_wall_px + 0.0001
                    if shadow_sl < pos["sl_tsl_price"] and shadow_sl > curr_price:
                        paper_amend_sl(pos, shadow_sl, "SHADOW_OBI_SHORT")
                        db_dirty = True

            # C. FILTROVANÉ DCA DOKUPY S EXEKUČNÍM OBRANNÝM VETEM
            if not pos["is_pd"]:
                cost_rate = pos.get("cost_rate", compute_roundtrip_cost_rate(win_id))
                tp_mod = pos.get("tp_mod", 1.0)
                for idx, pct in ((1, 0.10), (2, 0.30), (3, 0.30)):
                    if not pos[f"dca{idx}_filled"]:
                        dca_p = pos[f"dca{idx}_price"]
                        if (is_long and curr_price <= dca_p) or (not is_long and curr_price >= dca_p):
                            
                            # Pokud jsme u DCA3 a flow je extrémně toxické, aktivujeme Veto a neriskujeme ředění
                            if idx == 3 and ((is_long and ofd < 0.40) or (not is_long and ofd > 1.60)):
                                close_position(pos, curr_price, "EARLY_ORDERFLOW_ESCAPE", vault)
                                db_dirty = True
                                break
                                
                            added_m = pos["margin_usd"] * pct
                            old_qty = (pos["active_margin"] * pos["leverage"]) / pos["entry_price"]
                            new_qty = (added_m * pos["leverage"]) / dca_p
                            pos["entry_price"] = ((old_qty * pos["entry_price"]) + (new_qty * dca_p)) / (old_qty + new_qty)
                            pos["active_margin"] += added_m
                            pos[f"dca{idx}_filled"] = True
                            db_dirty = True

                            pos["tp1_price"] = pos["entry_price"] * (1.0 + dir_mult * ((1.0 * natr * tp_mod) / 100.0 + cost_rate))
                            pos["tp2_price"] = pos["entry_price"] * (1.0 + dir_mult * ((1.8 * natr * tp_mod) / 100.0 + cost_rate))

            # D. FÁZOVANÝ SCALE-OUT (TP1 & TP2) S REŽIMOVÝM TSL
            if not pos.get("tp1_hit", False):
                if (is_long and curr_price >= pos["tp1_price"]) or (not is_long and curr_price <= pos["tp1_price"]):
                    # Odprodej 50 % pozice tržním scale-outem
                    execute_scale_out(pos, curr_price, 0.50, "TP1_SCALE_OUT", vault, now_dt)
                    pos["tp1_hit"] = True
                    pos["tsl_active"] = True
                    pos["extreme_price"] = curr_price
                    
                    # OKAMŽITÝ POSUN SL NA BE+ (paper)
                    be_lock = pos["entry_price"] * (1.0 + dir_mult * (cost_rate + 0.0010))
                    paper_amend_sl(pos, be_lock, "BE_LOCK_AFTER_TP1")
                    db_dirty = True

            if pos.get("tp1_hit", False) and not pos.get("tp2_hit", False):
                if is_long and curr_price > pos["extreme_price"]:
                    pos["extreme_price"] = curr_price
                elif not is_long and curr_price < pos["extreme_price"]:
                    pos["extreme_price"] = curr_price

                # REŽIMOVÝ TSL ENGINE: Krok trailingu se adaptuje podle osy Y (volatility) z MMR matice
                if ry == 3:    # HIGH_VOL (Volnější krok pro šum velkých svíček)
                    step_pct = (0.85 * natr) / 100.0
                elif ry == 1:  # LOW_VOL (Agresivní utažení pro okamžité uzamčení zisku)
                    step_pct = (0.35 * natr) / 100.0
                else:          # MID_VOL
                    step_pct = (0.65 * natr) / 100.0

                new_tsl = pos["extreme_price"] * (1.0 - dir_mult * step_pct)
                if (is_long and new_tsl > pos["sl_tsl_price"]) or (not is_long and new_tsl < pos["sl_tsl_price"]):
                    paper_amend_sl(pos, new_tsl, "REGIME_TSL")
                    db_dirty = True

                if (is_long and curr_price >= pos["tp2_price"]) or (not is_long and curr_price <= pos["tp2_price"]):
                    execute_scale_out(pos, curr_price, 0.60, "TP2_SCALE_OUT", vault, now_dt)
                    pos["tp2_hit"] = True
                    pos["extreme_price"] = curr_price
                    db_dirty = True

            # E. LIQUIDATION CLIMAX (Exekuce finálního TP3) – s minimálnym časom v zóne
            if pos.get("tp2_hit", False):
                if is_long and curr_price > pos["extreme_price"]:
                    pos["extreme_price"] = curr_price
                elif not is_long and curr_price < pos["extreme_price"]:
                    pos["extreme_price"] = curr_price

                radar_dist = pos["tp3_price"] * ((0.25 * natr) / 100.0)
                in_radar = (is_long and curr_price >= pos["tp3_price"] - radar_dist) or (not is_long and curr_price <= pos["tp3_price"] + radar_dist)

                if in_radar:
                    # zaznamenaj čas vstupu do zóny (ak ešte nie je)
                    if pos.get("tp3_zone_entered_at") is None:
                        pos["tp3_zone_entered_at"] = now_ts

                    time_in_zone = now_ts - float(pos.get("tp3_zone_entered_at") or now_ts)
                    min_time_in_zone = 45.0  # minimálne 45 sekúnd v radarovej zóne pred climax exitom

                    # Detekce likvidačního šílenství (silné OFD vyvolané nucenými příkazy protistrany)
                    liquidation_climax = (is_long and ofd > 1.65) or (not is_long and ofd < 0.35)

                    hard_tp3_hit = (is_long and curr_price >= pos["tp3_price"]) or (not is_long and curr_price <= pos["tp3_price"])

                    if (liquidation_climax and time_in_zone >= min_time_in_zone) or hard_tp3_hit:
                        close_position(pos, curr_price, "LIQUIDATION_CLIMAX_HIT", vault)
                        db_dirty = True
                        continue
                else:
                    # ak vyšiel zo zóny, resetuj timer
                    pos["tp3_zone_entered_at"] = None
                    step_pct = (0.45 * natr) / 100.0
                    new_tsl = pos["extreme_price"] * (1.0 - dir_mult * step_pct)
                    if (is_long and new_tsl > pos["sl_tsl_price"]) or (not is_long and new_tsl < pos["sl_tsl_price"]):
                        paper_amend_sl(pos, new_tsl, "POST_TP2_TSL")
                        db_dirty = True

            # F. VYHODNOCENÍ ZÁSAHU TVRDÉHO SL / TSL NA BURZE (paper)
            hit_sl_tsl = (is_long and curr_price <= pos["sl_tsl_price"]) or (not is_long and curr_price >= pos["sl_tsl_price"])
            if hit_sl_tsl:
                reason = "TRAILING_SL_PROFIT" if pos["tsl_active"] else "HARD_SL_HIT"
                close_position(pos, curr_price, reason, vault)
                db_dirty = True
                continue

            # UI synchronizační vrstva pro paměť Streamlitu
            update_live_pnl_metrics(pos, curr_price, dir_mult, win_id)

        # 2. Asynchronní 3minutový globální skener trhu a správa 6 oken
        if is_scan_due:
            await process_global_market_scanning(client, vault, now_ts, ny_lock, klines_cache)

        # VŽDY zosúlaď active_positions s vault-om (cockpit musí vidieť rovnaký počet obchodov)
        try:
            sync_all_windows_to_db(vault)
        except Exception:
            pass

        need_cloud_sync = True  # po sync vždy ulož vault
        flush_pending_sync(vault)
        save_vault(vault, sync_cloud=need_cloud_sync)


async def process_global_market_scanning(client: httpx.AsyncClient, vault: dict, now_ts: float, ny_lock: bool, klines_cache: dict):
    """
    Finální propojovací blok skeneru 2.0.
    1. Počítá plovoucí 30denní percentily volatility pro osu Y.
    2. Provádí ortogonální de-duplikaci aktiv a sektorů.
    3. Alokuje signály napříč 6 nezávislými okny podle strukturální likvidity.
    """
    vault["last_main_scan"] = now_ts
    
    # A. VÝPOČET PLOVOUCÍCH PERCENTILŮ VOLATILITY (Osa Y)
    # Systém si stahováním BTC/USDT historie udržuje invariantní přehled o makrocyklu trhu
    try:
        btc_hist_klines = await fetch_okx_klines(client, "BTCUSDT", "1D", 30, klines_cache)
        if len(btc_hist_klines) >= 15:
            natr_history = []
            for bar in btc_hist_klines:
                h, l, c = float(get_high(bar)), float(get_low(bar)), float(get_close(bar))
                if c > 0:
                    natr_history.append(((h - l) / c) * 100.0)
            
            # Seřazení historických hodnot pro určení empirické distribuční funkce (ECDF)
            natr_history.sort()
            n_samples = len(natr_history)
            
            # Stanovení dynamických prahů (spodních 33 % = LOW, horních 34 % = HIGH)
            vault["vol_prah_low"] = float(natr_history[int(n_samples * 0.33)])
            vault["vol_prah_mid"] = float(natr_history[int(n_samples * 0.66)])
    except Exception:
        # Bezpečný fallback, pokud by API selhalo, aby bot nepřestal fungovat
        vault["vol_prah_low"] = vault.get("vol_prah_low", 1.5)
        vault["vol_prah_mid"] = vault.get("vol_prah_mid", 2.5)

    if ny_lock:
        return  # Pokud je aktivní NY Open Lock, nové obchody neskenujeme

    # B. SBĚR MARKET DATA A VETO FILTRACE
    tickers, oi_map = await fetch_market_snapshot(client)
    update_24h_oi_metrics(tickers, oi_map)

    btc_klines = await fetch_okx_klines(client, "BTCUSDT", "15m", 25, klines_cache)
    btc_veto = evaluate_btc_veto(btc_klines)
    vault["last_btc_veto"] = btc_veto

    # Sběr aktuálně obsazených mincí a sektorů pro zamezení nežádoucí korelace rizika
    active_syms = {p["symbol"] for p in vault["windows"].values() if p["status"] == "ACTIVE" and p["symbol"]}
    sectors_map = get_sectors_map()
    active_secs = {sectors_map.get(s, "UNKNOWN") for s in active_syms}

    # ===== OKNO 1: BTCUSDT (MAKROKOTVA) =====
    if vault["windows"]["1"]["status"] == "WAITING" and len(btc_klines) >= 20:
        last_btc = btc_klines[-1]
        is_15m_green = float(get_close(last_btc)) > float(get_open(last_btc))
        btc_ofd = compute_ofd_from_klines(btc_klines)
        direction, is_trap = None, False

        if not is_15m_green and btc_ofd < 1.00:
            funding_rate = await fetch_okx_funding(client, "BTCUSDT")
            if funding_rate > 0.0008:
                direction, is_trap = "SHORT", True
        if direction is None and is_15m_green and btc_ofd > 1.05:
            funding_rate = await fetch_okx_funding(client, "BTCUSDT")
            if funding_rate < -0.0015:
                direction, is_trap = "LONG", True

        if direction is None:
            if btc_ofd >= 1.08 and is_15m_green:
                direction = "LONG"
            elif btc_ofd <= 0.92 and not is_15m_green:
                direction = "SHORT"

        if direction is None:
            if btc_ofd >= 1.20:
                direction = "LONG"
            elif btc_ofd <= 0.80:
                direction = "SHORT"

        if direction and check_anti_extreme_shield(btc_klines, direction, is_pd=False, is_btc=True):
            ok_exec, exec_price = await verify_execution_confirmation_btc(client, "BTCUSDT", direction, btc_klines, btc_ofd, klines_cache)
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
                    vault["balance"], 1, vault
                )
                if setup:
                    vault["windows"]["1"] = setup
                    push_active_position_row(setup, vault)
                    active_syms.add("BTCUSDT")

    # ===== OKNA 2 & 3: ELITNÍ ŠAMPIONI (Objem >= 10M USD) =====
    for w_idx in ("2", "3"):
        if vault["windows"][w_idx]["status"] == "WAITING":
            cands = await evaluate_candidates(client, tickers, oi_map, active_syms, active_secs, 10_000_000.0, 1e12, btc_veto, klines_cache, is_pd_mode=False)
            for c in cands:
                setup = build_trade_setup(c, vault["balance"], int(w_idx), vault)
                if setup:
                    vault["windows"][w_idx] = setup
                    push_active_position_row(setup, vault)
                    active_syms.add(c["symbol"])
                    active_secs.add(c["sector"])
                    break

    # ===== OKNA 4 & 5: ŠAMPIONI NÍZKÉ LIKVIDITY (Objem 5M – 9.99M USD) =====
    for w_idx in ("4", "5"):
        if vault["windows"][w_idx]["status"] == "WAITING":
            cands = await evaluate_candidates(client, tickers, oi_map, active_syms, active_secs, 5_000_000.0, 9_999_999.0, btc_veto, klines_cache, is_pd_mode=False)
            for c in cands:
                setup = build_trade_setup(c, vault["balance"], int(w_idx), vault)
                if setup:
                    vault["windows"][w_idx] = setup
                    push_active_position_row(setup, vault)
                    active_syms.add(c["symbol"])
                    active_secs.add(c["sector"])
                    break

    # ===== OKNO 6: PUMP & DUMP REŽIM (uvoľnené, všetko okrem BTC, objem 0.5M – 8M USD) =====
    if vault["windows"]["6"]["status"] == "WAITING":
        cands_pd = await evaluate_candidates(client, tickers, oi_map, active_syms, active_secs, 500_000.0, 8_000_000.0, btc_veto, klines_cache, is_pd_mode=True)
        for c in cands_pd:
            setup = build_trade_setup(c, vault["balance"], 6, vault)
            if setup:
                vault["windows"]["6"] = setup
                push_active_position_row(setup, vault)
                break


# =========================================================================
# STREAMLIT UI (základné rozhranie – nezasahuje do core logiky vyššie)
# =========================================================================

def render_window_card(win_key: str, pos: dict):
    """Jednoduchá karta pre jedno okno."""
    status = pos.get("status", "WAITING")
    symbol = pos.get("symbol") or "—"
    direction = pos.get("direction", "")
    leverage = pos.get("leverage", "—")
    entry = pos.get("entry_price")
    live_pnl_usd = pos.get("live_pnl_usd")
    live_pnl_pct = pos.get("live_pnl_pct")
    regime = pos.get("regime", "")
    natr = pos.get("natr")

    with st.container(border=True):
        st.markdown(f"**Okno {win_key}**  |  `{status}`")
        if status == "ACTIVE" and symbol:
            col1, col2, col3 = st.columns(3)
            with col1:
                st.write(f"**{symbol}**")
                st.caption(f"{direction} ×{leverage}  |  {regime}")
            with col2:
                if entry is not None:
                    st.metric("Entry", f"{entry:.6g}")
                if natr is not None:
                    st.caption(f"NATR: {natr:.2f}%")
            with col3:
                if live_pnl_usd is not None:
                    st.metric("Live PnL", f"{live_pnl_usd:+.2f} $", f"{live_pnl_pct:+.2f}%")
        else:
            st.caption("Čaká na signál…")


def main_ui():
    st.title("Shark Alpha Core – Paper Trade")
    st.caption("Cloud 1 · OKX public data · Fiktívne obchody")

    # Načítanie trezoru
    if "vault" not in st.session_state:
        st.session_state.vault = load_vault()

    vault = st.session_state.vault

    # Pri otvorení stránky: ak offline > 10 min, zmaž visuté pozície
    purged = purge_stale_open_positions(vault)
    if purged > 0:
        st.warning(
            f"Appka bola offline viac ako 10 minút. "
            f"Zmazaných otvorených pozícií: **{purged}** (neuložené do histórie)."
        )
        st.session_state.vault = vault

    # Horný panel – balance + ovládanie
    top1, top2, top3 = st.columns([2, 1, 1])
    with top1:
        st.metric("Balance (paper)", f"{vault.get('balance', 0):.2f} USDT")
    with top2:
        last_scan = vault.get("last_main_scan", 0)
        if last_scan:
            ago = int(time.time() - last_scan)
            st.metric("Posledný scan", f"pred {ago}s")
        else:
            st.metric("Posledný scan", "—")
    with top3:
        ny_lock = vault.get("ny_open_lock", False)
        st.metric("NY Lock", "AKTÍVNY" if ny_lock else "OFF")

    st.divider()

    # Tlačidlá
    c1, c2, c3 = st.columns(3)
    with c1:
        run_once = st.button("▶ Spustiť 1 cyklus", type="primary", use_container_width=True)
    with c2:
        auto_refresh = st.toggle("Auto-refresh (15s)", value=False)
    with c3:
        if st.button("🔄 Reload vault", use_container_width=True):
            st.session_state.vault = load_vault()
            st.rerun()

    # Spustenie jadra
    if run_once or auto_refresh:
        with st.spinner("Beží core cyklus…"):
            try:
                asyncio.run(run_core_cycle(st.session_state.vault))
                save_vault(st.session_state.vault, sync_cloud=True)
            except Exception as e:
                st.error(f"Chyba v core cykle: {e}")

    st.divider()

    # 6 okien
    st.subheader("Obchodné okná")
    cols = st.columns(3)
    windows = vault.get("windows", {})
    for i, win_key in enumerate(["1", "2", "3", "4", "5", "6"]):
        with cols[i % 3]:
            pos = windows.get(win_key, {"status": "WAITING"})
            render_window_card(win_key, pos)

    # BTC Veto info
    st.divider()
    btc_veto = vault.get("last_btc_veto", {})
    v1, v2, v3, v4 = st.columns(4)
    with v1:
        st.metric("BTC 15m %", f"{btc_veto.get('btc_15m_pct', 0):+.3f}%")
    with v2:
        st.metric("BTC 1h %", f"{btc_veto.get('btc_1h_pct', 0):+.3f}%")
    with v3:
        st.metric("Veto LONG", "ÁNO" if btc_veto.get("veto_long") else "nie")
    with v4:
        st.metric("Veto SHORT", "ÁNO" if btc_veto.get("veto_short") else "nie")

    # Auto-refresh
    if auto_refresh:
        time.sleep(15)
        st.rerun()


# Vstupný bod Streamlit
if __name__ == "__main__" or True:  # Streamlit vždy spúšťa modul
    main_ui()
