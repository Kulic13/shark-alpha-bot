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
        "balance": 1000.0,
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
        "vol_prah_low": 1.5,
        "vol_prah_mid": 2.5,
        "shield_mode_active": False,
        "daily_pnl_usd": 0.0,
        "daily_pnl_date": "",
        "day_start_balance": 1000.0,
        # SYSTEM A/B meta-controller & adaptive funnel  (RESET + RELAXED)
        "strictness_long": 0.90,
        "strictness_short": 0.90,
        "last_trade_opened_ts": 0.0,
        "filter_weights": {
            "macro": 0.90, "liq": 0.90, "ofd": 0.90,
            "shield": 0.90, "micro": 0.90, "risk": 0.90,
        },
        "reject_stats": {
            "macro": 0, "liq": 0, "ofd": 0,
            "shield": 0, "micro": 0, "risk": 0,
        },
        "shadow_pool": [],  # unified incubator (max 100); replaces shadow_trades
        "pending_supabase_sync": [],
        "cell_strictness": 0.85
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
                    state.setdefault("vol_prah_low", 1.5)
                    state.setdefault("vol_prah_mid", 2.5)
                    state.setdefault("shield_mode_active", False)
                    state.setdefault("daily_pnl_usd", 0.0)
                    state.setdefault("daily_pnl_date", "")
                    state.setdefault("day_start_balance", float(state.get("balance", 1000.0)))
                    state.setdefault("strictness_long", 0.90)
                    state.setdefault("strictness_short", 0.90)
                    state.setdefault("last_trade_opened_ts", 0.0)
                    state.setdefault("filter_weights", {
                        "macro": 0.90, "liq": 0.90, "ofd": 0.90,
                        "shield": 0.90, "micro": 0.90, "risk": 0.90,
                    })
                    state.setdefault("reject_stats", {
                        "macro": 0, "liq": 0, "ofd": 0,
                        "shield": 0, "micro": 0, "risk": 0,
                    })
                    state.setdefault("shadow_pool", list(state.get("shadow_trades") or []))  # migrate old key
                    state.setdefault("shadow_trades", [])  # legacy no-op list
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
            state.setdefault("vol_prah_low", 1.5)
            state.setdefault("vol_prah_mid", 2.5)
            state.setdefault("shield_mode_active", False)
            state.setdefault("daily_pnl_usd", 0.0)
            state.setdefault("daily_pnl_date", "")
            state.setdefault("day_start_balance", float(state.get("balance", 1000.0)))
            state.setdefault("strictness_long", 0.90)
            state.setdefault("strictness_short", 0.90)
            state.setdefault("last_trade_opened_ts", 0.0)
            state.setdefault("filter_weights", {
                "macro": 0.90, "liq": 0.90, "ofd": 0.90,
                "shield": 0.90, "micro": 0.90, "risk": 0.90,
            })
            state.setdefault("reject_stats", {
                "macro": 0, "liq": 0, "ofd": 0,
                "shield": 0, "micro": 0, "risk": 0,
            })
            state.setdefault("shadow_pool", list(state.get("shadow_trades") or []))  # migrate old key
            state.setdefault("shadow_trades", [])  # legacy no-op list
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
    Pre active_positions preferujeme UPDATE podľa window_id (spoľahlivejšie než čistý upsert).
    """
    try:
        if table == "ai_mmr_matrix":
            supabase.table(table).upsert(data, on_conflict="regime_x,regime_y").execute()
        elif table == "active_positions":
            wid = data.get("window_id")
            if wid is not None:
                # 1) skús update existujúceho riadku
                res = supabase.table(table).update(data).eq("window_id", int(wid)).execute()
                # 2) ak nič neaktualizovalo (riadok neexistuje), urob upsert
                if not res.data:
                    supabase.table(table).upsert(data, on_conflict="window_id").execute()
            else:
                supabase.table(table).upsert(data, on_conflict="window_id").execute()
        else:
            supabase.table(table).upsert(data).execute()
    except Exception as e:
        # neschovávať chybu úplne – aspoň do pending + varovanie
        try:
            st.warning(f"Supabase upsert zlyhal ({table}): {e}")
        except Exception:
            pass
        vault.setdefault("pending_supabase_sync", []).append({
            "table": table,
            "op": "upsert",
            "data": data
        })


def force_clear_active_position(window_id: int, vault: dict):
    """
    Tvrdý clear jedného okna v active_positions.
    Najprv UPDATE podľa window_id, pri zlyhaní do pending.
    Zabraňuje situácii, keď cockpit stále vidí starú ACTIVE pozíciu po close.
    """
    now_iso = datetime.now(timezone.utc).isoformat()
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
        "updated_at": now_iso,
        "okx_entry_order_id": None,
        "okx_sl_order_id": None,
        "okx_tp1_order_id": None,
        "okx_tp2_order_id": None,
        "okx_tp3_order_id": None,
    }
    try:
        # Priorita: priamy UPDATE – necháva 1 riadok, žiadne duplicity
        res = supabase.table("active_positions").update(row).eq("window_id", int(window_id)).execute()
        if not res.data:
            # Riadok neexistoval → insert/upsert
            supabase.table("active_positions").upsert(row, on_conflict="window_id").execute()
    except Exception as e:
        try:
            st.warning(f"Clear active_positions window {window_id} zlyhal: {e}")
        except Exception:
            pass
        vault.setdefault("pending_supabase_sync", []).append({
            "table": "active_positions",
            "op": "upsert",
            "data": row
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
                    wid = data.get("window_id")
                    if wid is not None:
                        res = supabase.table(tbl).update(data).eq("window_id", int(wid)).execute()
                        if not res.data:
                            supabase.table(tbl).upsert(data, on_conflict="window_id").execute()
                    else:
                        supabase.table(tbl).upsert(data, on_conflict="window_id").execute()
                else:
                    supabase.table(tbl).upsert(data).execute()
            elif item["op"] == "insert":
                supabase.table(item["table"]).insert(item["data"]).execute()
        except Exception:
            remaining.append(item)
    vault["pending_supabase_sync"] = remaining


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

async def fetch_hloubkova_mikrostruktura(
    client: httpx.AsyncClient,
    symbol: str,
    klines_15m: list | None = None,
    cache: dict | None = None,
) -> dict:
    """
    Simulace a výpočet asynchronního hloubkového vnímání knihy řádu (Depth 20) z OKX veřejného REST.
    - Vážený Order Book (WOB): Exponenciální útlum váhy podle vzdálenosti od středu (1/k).
    - OBI Imbalance: Výsledný tlak pasivních zdí v knize.
    - CVD a VPIN: Výpočet čisté agresivity trhu a toxicity toku objednávek.
    - best_bid / best_ask / spread_pct / wob_imbalance pre verify_execution bez duplicitného GET.
    - klines_15m / cache: ak volajúci už má 15m sviečky, nevolá sa znova fetch_okx_klines.
    """
    inst_id = to_okx_inst_id(symbol)
    res = {
        "obi_hluboke": 1.0,
        "neblizsi_stena_sl": 0.0,
        "cvd_agresivita": 0.0,
        "vpin_toxicita": 0.20,
        "best_bid": 0.0,
        "best_ask": 0.0,
        "spread_pct": 999.0,
        "wob_imbalance": 0.0,
        "bids": [],
        "asks": [],
        "ok": False,
    }
    try:
        ob_res = await client.get(f"{OKX_API}/api/v5/market/books", params={"instId": inst_id, "sz": "20"})
        if ob_res.status_code == 200:
            ob_data = ob_res.json()
            if ob_data.get("code") == "0" and ob_data.get("data"):
                book = ob_data["data"][0]
                bids = book.get("bids", [])
                asks = book.get("asks", [])
                if not bids or not asks:
                    return res
                best_bid = float(bids[0][0])
                best_ask = float(asks[0][0])
                if best_bid <= 0 or best_ask <= 0:
                    return res
                res["best_bid"] = best_bid
                res["best_ask"] = best_ask
                res["spread_pct"] = ((best_ask - best_bid) / best_bid) * 100.0
                res["bids"] = bids[:20]
                res["asks"] = asks[:20]

                w_bid = sum(float(b[1]) * (1.0 / (k + 1)) for k, b in enumerate(bids[:20]))
                w_ask = sum(float(a[1]) * (1.0 / (k + 1)) for k, a in enumerate(asks[:20]))
                res["obi_hluboke"] = w_bid / max(1e-9, w_ask)
                # Signed WOB imbalance ∈ [-1, 1] (kladné = bid pressure)
                denom = w_bid + w_ask
                res["wob_imbalance"] = ((w_bid - w_ask) / denom) if denom > 1e-9 else 0.0
                # Detekce nejsilnější pasivní zdi pro Shadow OBI Stop Loss Barrier
                if w_bid > w_ask:
                    max_bid_idx = int(np.argmax([float(b[1]) for b in bids[:20]]))
                    res["neblizsi_stena_sl"] = float(bids[max_bid_idx][0])
                else:
                    max_ask_idx = int(np.argmax([float(a[1]) for a in asks[:20]]))
                    res["neblizsi_stena_sl"] = float(asks[max_ask_idx][0])
                # CVD (Cumulative Volume Delta) za posledních 5 svíček
                # Preferuj klines od volajúceho (verify_*), inak jeden fetch (s cache)
                klines = klines_15m
                if not klines or len(klines) < 5:
                    klines = await fetch_okx_klines(client, symbol, "15m", 6, cache)
                if klines and len(klines) >= 5:
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
                    # VPIN aproximace přes absolutní delty
                    res["vpin_toxicita"] = abs(delta_suma) / max(1e-9, total_v)
                res["ok"] = True
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


def compute_ofd_from_klines(klines: list) -> float:
    """
    Order-Flow Delta aproximácia z 15m sviečok.
    Body (open→close) určuje smer agresivity; knôty (wicks) sa delia 50/50,
    aby pin-bary a dlhé knôty neprevrátili interpretáciu objemu.
    """
    if not klines:
        return 1.0
    recent = klines[-5:] if len(klines) >= 5 else klines
    buy_vol = 0.0
    sell_vol = 0.0
    for k in recent:
        h = float(get_high(k))
        l = float(get_low(k))
        o = float(get_open(k))
        c = float(get_close(k))
        v = float(get_vol(k))
        rng = max(1e-12, h - l)
        body = abs(c - o)
        body_frac = min(1.0, body / rng)
        # Smer z tela sviečky; zvyšok (knôty) neutrálne 50/50
        if c >= o:
            buyer_ratio = 0.5 + 0.5 * body_frac
        else:
            buyer_ratio = 0.5 - 0.5 * body_frac
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
    if btc_1h_pct < -1.80 or btc_15m_pct < -0.90:
        result["veto_long"] = True
    if btc_1h_pct > 1.80 or btc_15m_pct > 0.90:
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


def _get_filter_ctx(vault: dict | None, direction: str = "LONG") -> dict:
    """
    Lightweight adaptive funnel context (SYSTEM A/B + closed-loop System 2).
    Combines meta strictness_long/short with regime cell_strictness from MMR matrix.
    """
    vault = vault or {}
    weights = vault.get("filter_weights") or {}
    base = float(
        vault.get("strictness_long", 1.0) if direction == "LONG"
        else vault.get("strictness_short", 1.0)
    )
    cell_s = float(vault.get("cell_strictness", 1.0))
    strictness = float(np.clip(base * cell_s, 0.80, 1.30))
    w = lambda k, d=1.0: float(weights.get(k, d))
    return {
        "strictness": strictness,
        "cell_strictness": cell_s,
        "w_macro": w("macro"),
        "w_liq": w("liq"),
        "w_ofd": w("ofd"),
        "w_shield": w("shield"),
        "w_micro": w("micro"),
        "w_risk": w("risk"),
        "ofd_long_thr": 1.30 * strictness * w("ofd"),
        "ofd_short_thr": 0.70 / max(1e-9, strictness * w("ofd")),
        "rsi_high": 78.0 - (strictness * w("shield") - 1.0) * 15.0,
        "rsi_low": 22.0 + (strictness * w("shield") - 1.0) * 15.0,
        "vpin_max": 0.80 / max(1e-9, strictness * w("micro")),
    }


def _bump_reject_stat(vault: dict, key: str):
    """Increment bottleneck counter for filter stage (vault-only, no DB columns)."""
    stats = vault.setdefault("reject_stats", {
        "macro": 0, "liq": 0, "ofd": 0, "shield": 0, "micro": 0, "risk": 0,
    })
    if key in stats:
        stats[key] = int(stats.get(key, 0)) + 1


def _map_failed_filter_key(failed_filter: str) -> str:
    """Normalize failed_filter labels to filter_weights / reject_stats keys."""
    u = (failed_filter or "").upper()
    if "MACRO" in u or "VETO" in u or "BTC" in u:
        return "macro"
    if "LIQ" in u or "VOL" in u:
        return "liq"
    if "OFD" in u or "ORDERFLOW" in u:
        return "ofd"
    if "SHIELD" in u or "EXTREME" in u or "RSI" in u:
        return "shield"
    if "MICRO" in u or "VERIFY" in u or "FILTER_5" in u or "VPIN" in u or "WOB" in u:
        return "micro"
    if "RISK" in u or "MMR" in u or "FILTER_6" in u or "KELLY" in u:
        return "risk"
    return "risk"


def record_shadow_trade(vault: dict, cand: dict, failed_filter: str, tp1_price: float = 0.0, sl_price: float = 0.0):
    """
    Unified Shadow Pool (max 100): candidate passed liquidity but blocked by a later filter.
    Backward-compatible name; writes to vault["shadow_pool"].
    """
    entry = float(cand.get("exec_price") or cand.get("reject_price") or 0.0)
    if entry <= 0 and cand.get("klines"):
        try:
            entry = float(get_close(cand["klines"][-1]))
        except Exception:
            entry = 0.0
    if entry <= 0:
        return
    filter_key = _map_failed_filter_key(failed_filter)
    _bump_reject_stat(vault, filter_key)

    if tp1_price <= 0 or sl_price <= 0:
        natr = float(cand.get("natr") or 2.0)
        dir_mult = 1.0 if cand.get("direction") == "LONG" else -1.0
        if tp1_price <= 0:
            tp1_price = entry * (1.0 + dir_mult * (natr * 1.0 / 100.0))
        if sl_price <= 0:
            sl_price = entry * (1.0 - dir_mult * (natr * 2.0 / 100.0))

    item = {
        "symbol": cand.get("symbol"),
        "direction": cand.get("direction"),
        "reject_price": entry,
        "blocked_by": filter_key,
        "virtual_tp1": float(tp1_price),
        "virtual_sl": float(sl_price),
        "ts": time.time(),
        # legacy aliases for older process paths
        "entry_price": entry,
        "tp1_price": float(tp1_price),
        "sl_price": float(sl_price),
        "failed_filter": failed_filter,
        "opened_ts": time.time(),
        "status": "ACTIVE",
    }
    pool = list(vault.get("shadow_pool") or vault.get("shadow_trades") or [])
    pool.append(item)
    vault["shadow_pool"] = pool[-100:]


def process_shadow_pool(vault: dict, tickers: dict, now_ts: float):
    """
    Evaluate unified shadow_pool during 3-min scan using already-fetched tickers (0 extra API).
    Age window: [900s, 3600s]. TP1 first → loosen filter; SL first → tighten.
    """
    pool = list(vault.get("shadow_pool") or [])
    if not pool:
        return
    weights = vault.setdefault("filter_weights", {
        "macro": 1.0, "liq": 1.0, "ofd": 1.0, "shield": 1.0, "micro": 1.0, "risk": 1.0,
    })
    remaining = []
    for sh in pool:
        ts0 = float(sh.get("ts") or sh.get("opened_ts") or 0.0)
        age = now_ts - ts0
        if age > 3600:
            continue  # expired
        if age < 900:
            remaining.append(sh)  # too young
            continue
        sym = sh.get("symbol")
        if not sym or not tickers or sym not in tickers:
            remaining.append(sh)
            continue
        try:
            t = tickers[sym]
            px = float(t.get("last") if isinstance(t, dict) else t)
        except Exception:
            remaining.append(sh)
            continue
        is_long = sh.get("direction") == "LONG"
        tp1 = float(sh.get("virtual_tp1") or sh.get("tp1_price") or 0.0)
        sl = float(sh.get("virtual_sl") or sh.get("sl_price") or 0.0)
        key = sh.get("blocked_by") or _map_failed_filter_key(sh.get("failed_filter", "risk"))
        hit_tp = (is_long and px >= tp1) or (not is_long and px <= tp1)
        hit_sl = (is_long and px <= sl) or (not is_long and px >= sl)
        if hit_sl and not hit_tp:
            w = float(weights.get(key, 1.0))
            weights[key] = float(np.clip(w + 0.01, 0.80, 1.25))
            continue  # evaluated out
        if hit_tp and not hit_sl:
            w = float(weights.get(key, 1.0))
            weights[key] = float(np.clip(w - 0.015, 0.80, 1.25))
            if is_long:
                vault["strictness_long"] = float(np.clip(float(vault.get("strictness_long", 1.0)) - 0.01, 0.80, 1.30))
            else:
                vault["strictness_short"] = float(np.clip(float(vault.get("strictness_short", 1.0)) - 0.01, 0.80, 1.30))
            continue  # evaluated out
        remaining.append(sh)
    vault["filter_weights"] = weights
    vault["shadow_pool"] = remaining[-100:]


# Backward-compatible alias used by older call sites
def process_shadow_trades(vault: dict, price_tickers: dict, now_ts: float):
    process_shadow_pool(vault, price_tickers, now_ts)




def update_meta_controller(vault: dict, now_ts: float | None = None):
    """SYSTEM B – Autonomous Meta-Controller for strictness_long/short."""
    if now_ts is None:
        now_ts = time.time()
    s_long = float(vault.get("strictness_long", 1.0))
    s_short = float(vault.get("strictness_short", 1.0))

    if vault.get("shield_mode_active"):
        s_long += 0.10
        s_short += 0.10

    last_open = float(vault.get("last_trade_opened_ts") or 0.0)
    ny_lock = bool(vault.get("ny_open_lock", False))
    if last_open > 0 and (now_ts - last_open) > 14400 and not ny_lock:
        # Anti-Deadlock Time Decay: 2% toward 1.0
        s_long = s_long * 0.98 + 1.0 * 0.02
        s_short = s_short * 0.98 + 1.0 * 0.02

    vault["strictness_long"] = float(np.clip(s_long, 0.80, 1.30))
    vault["strictness_short"] = float(np.clip(s_short, 0.80, 1.30))


def simulate_depth20_vwap(levels: list, notional_usd: float, fallback_px: float) -> float:
    """
    SYSTEM C – walk Depth-20 book levels [price, size, ...] to estimate realistic VWAP fill.
    levels: bids (for sell/exit SHORT cover) or asks (for buy/LONG entry).
    size is in contracts; without ctVal we treat size as notional-fraction proxy via price*size.
    """
    if not levels or notional_usd <= 0 or fallback_px <= 0:
        return float(fallback_px)
    remaining = float(notional_usd)
    cost = 0.0
    filled = 0.0
    for lvl in levels[:20]:
        try:
            px = float(lvl[0])
            sz = float(lvl[1])
        except Exception:
            continue
        if px <= 0 or sz <= 0:
            continue
        level_notional = px * sz  # proxy notional
        take = min(remaining, level_notional)
        cost += take
        filled += take / px
        remaining -= take
        if remaining <= 1e-9:
            break
    if filled <= 1e-12:
        return float(fallback_px)
    return cost / filled


def update_filter_weights_from_outcome(pos: dict, is_win: bool, reason: str, vault: dict):
    """
    Closed-loop System 1 <-> System 2 credit assignment.
    Uses entry_fingerprint on real trades + failed_filter on shadow trades.
    Weights always clamped to [0.80, 1.25].
    """
    weights = vault.setdefault("filter_weights", {
        "macro": 1.0, "liq": 1.0, "ofd": 1.0,
        "shield": 1.0, "micro": 1.0, "risk": 1.0,
    })
    reason_u = (reason or "").upper()
    failed = (pos.get("failed_filter") or "").upper() if pos else ""
    fp = (pos.get("entry_fingerprint") or {}) if pos else {}
    direction = (pos.get("direction") or "").upper() if pos else ""

    def adj(key: str, delta: float):
        weights[key] = float(np.clip(float(weights.get(key, 1.0)) + delta, 0.80, 1.25))

    def clamp_all():
        for k in list(weights.keys()):
            weights[k] = float(np.clip(float(weights.get(k, 1.0)), 0.80, 1.25))
        vault["filter_weights"] = weights

    # --- Shadow incubator feedback ---
    if "SHADOW_WIN" in reason_u:
        # Overly restrictive filter that blocked a winner → loosen it
        if "MICRO" in reason_u or "VERIFY" in reason_u or "FILTER_5" in failed or "MICRO" in failed:
            adj("micro", -0.015)
        if "RISK" in reason_u or "MMR" in reason_u or "FILTER_6" in failed or "RISK" in failed:
            adj("risk", -0.015)
        if "SHIELD" in reason_u or "SHIELD" in failed:
            adj("shield", -0.015)
        # Slightly reduce directional strictness
        if direction == "SHORT":
            vault["strictness_short"] = float(np.clip(float(vault.get("strictness_short", 1.0)) - 0.02, 0.80, 1.30))
        else:
            vault["strictness_long"] = float(np.clip(float(vault.get("strictness_long", 1.0)) - 0.02, 0.80, 1.30))
        clamp_all()
        return

    if "SHADOW_LOSS" in reason_u:
        if "MICRO" in reason_u or "VERIFY" in reason_u or "FILTER_5" in failed:
            adj("micro", +0.02)
        if "RISK" in reason_u or "FILTER_6" in failed:
            adj("risk", +0.02)
        clamp_all()
        return

    # --- Real trade: WIN → gently relax all weights toward 1.0 ---
    if is_win:
        for k in list(weights.keys()):
            w0 = float(weights.get(k, 1.0))
            weights[k] = float(np.clip(w0 * 0.98 + 1.0 * 0.02, 0.80, 1.25))
        clamp_all()
        return

    # --- Real trade: LOSS → credit assignment via entry_fingerprint ---
    if reason_u == "VPIN_EMERGENCY_ESCAPE" or float(fp.get("micro_vpin", 0.0) or 0.0) > 0.65:
        adj("micro", +0.03)
    if direction == "LONG" and float(fp.get("shield_rsi", 50.0) or 50.0) > 70.0:
        adj("shield", +0.03)
    if direction == "SHORT" and float(fp.get("shield_rsi", 50.0) or 50.0) < 30.0:
        adj("shield", +0.03)
    if direction == "LONG" and float(fp.get("ofd_val", 1.3) or 1.3) < 1.38:
        adj("ofd", +0.03)
    if direction == "SHORT" and float(fp.get("ofd_val", 0.7) or 0.7) > 0.65:
        adj("ofd", +0.03)
    wob = float(fp.get("micro_wob", 0.0) or 0.0)
    # Against trade direction: LONG with negative WOB (ask pressure) or SHORT with positive WOB
    if direction == "LONG" and wob < -0.08:
        adj("micro", +0.02)
    if direction == "SHORT" and wob > 0.08:
        adj("micro", +0.02)
    if "LIQUIDATION" in reason_u:
        adj("risk", +0.04)
    if "ORDERFLOW" in reason_u:
        adj("ofd", +0.02)
    if "HARD_SL" in reason_u or reason_u == "HARD_SL_HIT":
        adj("shield", +0.02)

    clamp_all()


def check_anti_extreme_shield(
    klines: list,
    direction: str,
    is_pd: bool = False,
    is_btc: bool = False,
    filter_ctx: dict | None = None,
) -> bool:
    if len(klines) < 20:
        return False
    ctx = filter_ctx or {}
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
    # SYSTEM A: adaptive RSI from filter_ctx (strictness × w_shield)
    ctx_rsi_high = float(ctx.get("rsi_high", 78.0))
    ctx_rsi_low = float(ctx.get("rsi_low", 22.0))
    if is_pd:
        max_atr_dist = 2.2
        rsi_high, rsi_low = max(ctx_rsi_high, 78.0), min(ctx_rsi_low, 22.0)
        skip_bb = True
    elif is_btc:
        max_atr_dist = 1.8
        rsi_high, rsi_low = min(75.0, ctx_rsi_high), max(25.0, ctx_rsi_low)
        skip_bb = True
    else:
        # Štandardný režim: BB filter vypnutý; ATR + adaptive RSI
        max_atr_dist = 2.0
        rsi_high, rsi_low = ctx_rsi_high, ctx_rsi_low
        skip_bb = True
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
    is_pd_mode: bool = False,
    vault: dict | None = None,
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
                            "exec_price": exec_price,
                            "entry_fingerprint": {
                                "macro_btc_1h": float(btc_veto.get("btc_1h_pct", 0.0)),
                                "liq_vol_24h": float(vol),
                                "ofd_val": float(ofd),
                                "shield_rsi": float(calculate_rsi_14(pd.Series([float(get_close(k)) for k in klines]))) if len(klines) >= 15 else 50.0,
                                "micro_wob": 0.0,
                                "micro_vpin": float(calculate_vpin_toxicity(klines)) if len(klines) >= 10 else 0.0,
                                "risk_wr": 52.0,
                            },
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
                                "exec_price": exec_price,
                                "entry_fingerprint": {
                                    "macro_btc_1h": float(btc_veto.get("btc_1h_pct", 0.0)),
                                    "liq_vol_24h": float(vol),
                                    "ofd_val": float(ofd),
                                    "shield_rsi": float(calculate_rsi_14(pd.Series([float(get_close(k)) for k in klines]))) if len(klines) >= 15 else 50.0,
                                    "micro_wob": 0.0,
                                    "micro_vpin": float(calculate_vpin_toxicity(klines)) if len(klines) >= 10 else 0.0,
                                    "risk_wr": 52.0,
                                },
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
                            "exec_price": exec_price,
                            "entry_fingerprint": {
                                "macro_btc_1h": float(btc_veto.get("btc_1h_pct", 0.0)),
                                "liq_vol_24h": float(vol),
                                "ofd_val": float(ofd),
                                "shield_rsi": float(calculate_rsi_14(pd.Series([float(get_close(k)) for k in klines]))) if len(klines) >= 15 else 50.0,
                                "micro_wob": 0.0,
                                "micro_vpin": float(calculate_vpin_toxicity(klines)) if len(klines) >= 10 else 0.0,
                                "risk_wr": 52.0,
                            },
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
                            "exec_price": exec_price,
                            "entry_fingerprint": {
                                "macro_btc_1h": float(btc_veto.get("btc_1h_pct", 0.0)),
                                "liq_vol_24h": float(vol),
                                "ofd_val": float(ofd),
                                "shield_rsi": float(calculate_rsi_14(pd.Series([float(get_close(k)) for k in klines]))) if len(klines) >= 15 else 50.0,
                                "micro_wob": 0.0,
                                "micro_vpin": float(calculate_vpin_toxicity(klines)) if len(klines) >= 10 else 0.0,
                                "risk_wr": 52.0,
                            },
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
                fctx_long = _get_filter_ctx(vault, "LONG")
                fctx_short = _get_filter_ctx(vault, "SHORT")
                ofd_long_thr = float(fctx_long.get("ofd_long_thr", 1.30))
                ofd_short_thr = float(fctx_short.get("ofd_short_thr", 0.70))
                if direction is None:
                    if ofd > ofd_long_thr and is_15m_green:
                        direction = "LONG"
                    elif ofd < ofd_short_thr and not is_15m_green:
                        direction = "SHORT"
                if direction:
                    if direction == "LONG" and btc_veto.get("veto_long"):
                        if vault is not None:
                            _bump_reject_stat(vault, "macro")
                            record_shadow_trade(
                                vault,
                                {"symbol": sym, "direction": direction,
                                 "exec_price": float(get_close(klines[-1])),
                                 "klines": klines, "natr": calculate_natr_14(klines)},
                                failed_filter="MACRO_BTC_VETO",
                            )
                        return None
                    if direction == "SHORT" and btc_veto.get("veto_short"):
                        if vault is not None:
                            _bump_reject_stat(vault, "macro")
                            record_shadow_trade(
                                vault,
                                {"symbol": sym, "direction": direction,
                                 "exec_price": float(get_close(klines[-1])),
                                 "klines": klines, "natr": calculate_natr_14(klines)},
                                failed_filter="MACRO_BTC_VETO",
                            )
                        return None
                    fctx = fctx_long if direction == "LONG" else fctx_short
                    if not check_anti_extreme_shield(klines, direction, is_pd=False, filter_ctx=fctx):
                        if vault is not None:
                            record_shadow_trade(
                                vault,
                                {"symbol": sym, "direction": direction,
                                 "exec_price": float(get_close(klines[-1])),
                                 "klines": klines, "natr": calculate_natr_14(klines)},
                                failed_filter="SHIELD_ANTI_EXTREME",
                            )
                        return None
                    ok_exec, exec_price = await verify_execution_confirmation(
                        client, sym, direction, klines, ofd, False, klines_cache, filter_ctx=fctx
                    )
                    if not ok_exec:
                        if vault is not None:
                            record_shadow_trade(
                                vault,
                                {"symbol": sym, "direction": direction,
                                 "exec_price": float(get_close(klines[-1])),
                                 "klines": klines, "natr": calculate_natr_14(klines)},
                                failed_filter="FILTER_5_MICRO",
                            )
                        return None
                    vol_norm = np.log1p(vol)
                    sas_score = (abs(price_change_pct) + 1.0) * (ofd if direction == "LONG" else (1.0 / max(0.1, ofd))) * (10.0 / vol_norm)
                    # Entry fingerprint for closed-loop credit assignment
                    # micro_* musia byť reálne z verify (inak credit assignment skresľuje MMR/filtre)
                    try:
                        rsi_fp = float(calculate_rsi_14(pd.Series([float(get_close(k)) for k in klines])))
                    except Exception:
                        rsi_fp = 50.0
                    micro_wob = float((fctx or {}).get("last_wob", 0.0) or 0.0)
                    micro_vpin = float((fctx or {}).get("last_vpin", 0.0) or 0.0)
                    # Fallback: ak verify nevystavil VPIN, spočítaj z klines (bez extra API)
                    if micro_vpin == 0.0 and klines:
                        try:
                            micro_vpin = float(calculate_vpin_toxicity(klines))
                        except Exception:
                            pass
                    entry_fingerprint = {
                        "macro_btc_1h": float(btc_veto.get("btc_1h_pct", 0.0)),
                        "liq_vol_24h": float(vol),
                        "ofd_val": float(ofd),
                        "shield_rsi": float(rsi_fp),
                        "micro_wob": micro_wob,
                        "micro_vpin": micro_vpin,
                        "risk_wr": 52.0,  # filled in build_trade_setup from MMR cell
                    }
                    return {
                        "symbol": sym,
                        "direction": direction,
                        "score": sas_score,
                        "is_funding_trap": is_trap,
                        "is_pd": False,
                        "sector": sec,
                        "ofd": ofd,
                        "klines": klines,
                        "exec_price": exec_price,
                        "entry_fingerprint": entry_fingerprint,
                        "book_levels": list((fctx or {}).get("book_levels") or []),
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

def exit_cost_rate(window_id: int, cost_rate: float | None = None) -> float:
    """
    Exit leg only: taker 0.0005 + half of non-fee slippage.
    Entry fees are already booked at open / limit-fill — do not double-count.
    cost_rate = exchange_fees(0.0010) + slippage; so slippage = cost_rate - 0.0010.
    """
    if cost_rate is None:
        cost_rate = compute_roundtrip_cost_rate(window_id)
    slippage = max(0.0, float(cost_rate) - 0.0010)
    return 0.0005 + 0.5 * slippage


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
    Osa X (Trend): preferuje regresný sklon z posledných 10×15m BTC sviečok
    (uložený pri scane ako last_btc_trend_slope); fallback na btc_1h_pct.
    Osa Y (Volatilita): invariantní 30denní percentily.
    """
    # Osa X: Trendová klasifikace (BEAR = 1, SIDE = 2, BULL = 3)
    # Preferuj polyfit sklon (menej citlivý na jednorazový 1h výkyv)
    norm_slope = vault.get("last_btc_trend_slope", None)
    if norm_slope is not None:
        if norm_slope < -0.33:
            regime_x = 1
        elif norm_slope > 0.33:
            regime_x = 3
        else:
            regime_x = 2
    else:
        btc_veto = vault.get("last_btc_veto", {})
        btc_1h = btc_veto.get("btc_1h_pct", 0.0)
        if btc_1h < -0.33:
            regime_x = 1
        elif btc_1h > 0.33:
            regime_x = 3
        else:
            regime_x = 2

    # Osa Y: Volatilitní invariantní percentily (LOW = 1, MID = 2, HIGH = 3)
    # Preferuj natr_history_30d (skutočný TR%), fallback na vol_prah_* z posledného scanu
    hist = vault.get("natr_history_30d", [])
    if len(hist) >= 15:
        p_low = float(np.percentile(hist, 33))
        p_mid = float(np.percentile(hist, 66))
    else:
        p_low = float(vault.get("vol_prah_low", 1.5))
        p_mid = float(vault.get("vol_prah_mid", 2.5))

    if natr <= p_low:
        regime_y = 1
    elif natr <= p_mid:
        regime_y = 2
    else:
        regime_y = 3
        
    return regime_x, regime_y

def _build_entry_fingerprint(cand: dict, vault: dict, mmr_cell: dict, initial_ofd: float) -> dict:
    fp = dict(cand.get("entry_fingerprint") or {})
    fp.setdefault("macro_btc_1h", float((vault.get("last_btc_veto") or {}).get("btc_1h_pct", 0.0)))
    fp.setdefault("liq_vol_24h", 0.0)
    fp["ofd_val"] = float(fp.get("ofd_val") or initial_ofd)
    fp.setdefault("shield_rsi", 50.0)
    fp.setdefault("micro_wob", 0.0)
    fp.setdefault("micro_vpin", 0.0)
    fp["risk_wr"] = float(mmr_cell.get("win_rate", 52.0))
    return fp


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
    max_natr = 9.0 if is_pd else 6.5
    if natr > max_natr or natr < 0.12 or np.isnan(natr):
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
    veto_threshold = 0.42 if is_shield_active else 0.38
    
    # MATICOVÉ VETO: Pokud je buňka pod limitem úspěšnosti, Risk Engine příkaz zablokuje
    if wr < veto_threshold:
        cand["_failed_filter"] = "FILTER_6_RISK_MMR"
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

    # Adaptivní sázkový zlomek: Shield → polovica; Scout Mode (cell trades < 5) → 0.15
    cell_trades = int(mmr_cell.get("total_trades", 0) or 0)
    if cell_trades < 5:
        kelly_fraction = 0.20  # Scout Mode – uvoľnené
    elif is_shield_active:
        kelly_fraction = 0.20
    else:
        kelly_fraction = 0.40
    max_risk_pct = 0.015 if is_pd else 0.025
    kelly_margin = min(balance * full_kelly * kelly_fraction, balance * max_risk_pct)

    # Ověření reálných limitů OKX burzy
    # Ak Kellyho marža nestačí na min. notional, obchod NEOTVÁRAME
    # (žiadne násilné navýšenie nad riziko z Kelly kritéria).
    min_notional = okx_min_notional_usd(sym, price)
    min_margin = min_notional / max(1, leverage)

    if kelly_margin < min_margin:
        return None
    margin_usd = kelly_margin

    # Exact size-dependent VWAP from Depth-20 levels captured at verify (0 extra API)
    book_levels = cand.get("book_levels") or []
    if book_levels and margin_usd > 0 and leverage > 0:
        notional = float(margin_usd) * float(leverage)
        price = float(simulate_depth20_vwap(book_levels, notional, price))
        # refresh exec on cand for fingerprint consistency
        cand["exec_price"] = price

    # 7. EXEKUČNÍ DEKOMPOZICE – placeholder; finálne active_margin nastaví SYSTEM C TWAP 40/60 nižšie
    # (zámerne tu neprepisujeme percentá, aby nevznikol konflikt 30% vs 40%)
    active_margin = margin_usd  # dočasne plná; SYSTEM C upraví non-PD na 40% market leg

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

    # SYSTEM C – TWAP 40/60 (non-P&D): 40% market now (taker), 60% pending limit (maker)
    # Jediný zdroj pravdy pre active_margin pri vstupe (žiadny starý 30% split).
    pending_limit_margin = 0.0
    pending_limit_price = 0.0
    if not is_pd:
        market_frac = 0.40
        filled_margin = margin_usd * market_frac
        # Ak by 40% leg neprešiel min notional, daj celú maržu marketom (bez pending)
        if (filled_margin * max(1, leverage)) < min_notional:
            if margin_usd * max(1, leverage) < min_notional:
                return None  # celý setup pod minimom OKX
            active_margin = margin_usd
            pending_limit_margin = 0.0
            pending_limit_price = 0.0
        else:
            active_margin = filled_margin
            pending_limit_margin = margin_usd * (1.0 - market_frac)
            pending_limit_price = price
    else:
        active_margin = margin_usd

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
        "pending_limit_margin": float(pending_limit_margin),
        "pending_limit_price": float(pending_limit_price),
        "last_funding_hour": -1,
        "entry_price": price,
        "dca1_price": dca1, "dca1_filled": False,
        "dca2_price": dca2, "dca2_filled": False,
        "dca3_price": dca3, "dca3_filled": False,
        "tp1_price": tp1, "tp1_hit": False,
        "tp2_price": tp2, "tp2_hit": False,
        "tp3_price": tp3,
        "sl_tsl_price": sl_price,
        "initial_sl_price": sl_price,  # pôvodný SL pre správny R-multiple celého obchodu
        "initial_margin_usd": float(margin_usd),  # celková plánovaná marža pri vstupe
        "max_deployed_margin": float(margin_usd),  # plánovaný plný kapitál; rastie len ak DCA > plán
        "initial_entry_price": float(price),  # fixný vstup pre R-multiple (DCA nemení risk base)
        "realized_pnl_usd": 0.0,  # kumulovaný PnL z TP1/TP2 scale-outov
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
        "okx_tp3_order_id": f"{tx_id}_tp3_{window_id}",
        "entry_fingerprint": _build_entry_fingerprint(cand, vault, mmr_cell, initial_ofd),
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
        
    cell["updated_at"] = datetime.now(timezone.utc).isoformat()
    safe_supabase_upsert("ai_mmr_matrix", cell, vault)

def update_ai_learning(
    symbol: str,       # Zachováno pro logování/zpětnou kompatibilitu, učení je však vázáno na MMR
    direction: str,    # Směr pozice
    regime: str,       # Typ exekuce (SPOT/FUTURES)
    is_funding_trap: bool,
    is_win: bool,
    r_multiple: float,
    vault: dict,
    regime_x: int | None = None,
    regime_y: int | None = None,
):
    """
    Hlavní post-trade modul učení 2.0. Kompletně odstraňuje izolované učení mincí.
    Implementuje prostorové přelévání informací (KNN Spatial Smoothing) do okolních režimů.
    Súradnice (rx, ry) musia prísť priamo z uzatváranej pozície – nie spätným hľadaním podľa symbolu.
    """
    if regime_x is not None and regime_y is not None:
        rx, ry = int(regime_x), int(regime_y)
    else:
        # Fallback len ak volajúci neposlal súradnice (nemalo by nastať)
        rx = int(vault.get("last_active_rx", 2))
        ry = int(vault.get("last_active_ry", 2))

    outcome = 100.0 if is_win else 0.0

    # 1. ZÁPIS DO CÍLOVÉ BUŇKY TRŽNÍHO REŽIMU (Plná váha = 100 %)
    update_mmr_cell_values(rx, ry, outcome, r_multiple, 1.0, vault)
    
    # 2. PROSTOROVÉ PŘELÉVÁNÍ INFORMACÍ (KNN Spatial Smoothing - Sousední buňky = 20 %)
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
    cache: dict,
    filter_ctx: dict | None = None,
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

    # 3. Hĺbková mikroštruktúra (jedno API volanie cez fetch_hloubkova_mikrostruktura)
    try:
        micro = await fetch_hloubkova_mikrostruktura(client, symbol, klines_15m=klines_15m, cache=cache)
        if not micro.get("ok"):
            return False, 0.0

        best_ask = float(micro["best_ask"])
        best_bid = float(micro["best_bid"])
        spread_pct = float(micro["spread_pct"])
        max_spread = 0.30 if is_pd else 0.18
        if spread_pct > max_spread:
            return False, 0.0

        # Preferuj VPIN z klines_15m; adaptive max from filter_ctx (SYSTEM A)
        vpin_score = calculate_vpin_toxicity(klines_15m)
        vpin_max = float((filter_ctx or {}).get("vpin_max", 0.80))
        if vpin_score > vpin_max:
            return False, 0.0

        wob_imbalance = float(micro.get("wob_imbalance", 0.0))
        if direction == "LONG" and wob_imbalance < -0.15:
            return False, 0.0
        if direction == "SHORT" and wob_imbalance > 0.15:
            return False, 0.0

        # SYSTEM C – Depth-20 VWAP estimate for entry (reuse already-fetched book)
        levels = (micro.get("asks") or []) if direction == "LONG" else (micro.get("bids") or [])
        fallback = best_ask if direction == "LONG" else best_bid
        exec_price = simulate_depth20_vwap(levels, notional_usd=50.0, fallback_px=fallback)
        # Stash microstructure + raw book for exact size-dependent VWAP in build_trade_setup
        if filter_ctx is not None:
            filter_ctx["last_wob"] = float(wob_imbalance)
            filter_ctx["last_vpin"] = float(vpin_score)
            filter_ctx["book_levels"] = levels
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

    # Volume: stačí, že trh nie je mŕtvy (uvoľnené z 1.05 → 0.90)
    vols = [float(get_vol(k)) for k in klines_15m[-11:]]
    avg_prev_vol = float(np.mean(vols[:-1])) if len(vols) > 1 else 0.0
    curr_vol = vols[-1]
    if avg_prev_vol > 0 and (curr_vol / avg_prev_vol) < 0.90:
        return False, 0.0

    klines_1h = await fetch_okx_klines(client, symbol, "1H", 25, cache)
    if len(klines_1h) >= 21:
        closes_1h = pd.Series([float(get_close(k)) for k in klines_1h])
        ema21_1h = float(closes_1h.ewm(span=21, adjust=False).mean().iloc[-1])
        last_1h_close = float(closes_1h.iloc[-1])
        # Proti-trend len pri slabej OFD (mierne uvoľnené)
        if direction == "LONG" and last_1h_close < ema21_1h and ofd < 1.25:
            return False, 0.0
        if direction == "SHORT" and last_1h_close > ema21_1h and ofd > 0.80:
            return False, 0.0

    try:
        micro = await fetch_hloubkova_mikrostruktura(client, symbol, klines_15m=klines_15m, cache=cache)
        if not micro.get("ok"):
            return False, 0.0

        best_ask = float(micro["best_ask"])
        best_bid = float(micro["best_bid"])
        spread_pct = float(micro["spread_pct"])
        if spread_pct > 0.18:
            return False, 0.0

        vpin_score = calculate_vpin_toxicity(klines_15m)
        if vpin_score > 0.82:
            return False, 0.0

        wob_imbalance = float(micro.get("wob_imbalance", 0.0))
        if direction == "LONG" and wob_imbalance < -0.12:
            return False, 0.0
        if direction == "SHORT" and wob_imbalance > 0.12:
            return False, 0.0

        levels = (micro.get("asks") or []) if direction == "LONG" else (micro.get("bids") or [])
        fallback = best_ask if direction == "LONG" else best_bid
        exec_price = simulate_depth20_vwap(levels, notional_usd=100.0, fallback_px=fallback)
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



def update_daily_pnl_and_shield(vault: dict, pnl_usd: float, now_dt: datetime | None = None):
    """
    Sleduje denný PnL. Pri strate > 3 % day_start_balance aktivuje shield_mode.
    Pri zmene UTC dňa resetuje denný PnL a vypne štít.
    """
    if now_dt is None:
        now_dt = datetime.now(timezone.utc)
    today = now_dt.strftime("%Y-%m-%d")
    stored_date = vault.get("daily_pnl_date") or ""
    if stored_date != today:
        vault["daily_pnl_date"] = today
        vault["daily_pnl_usd"] = 0.0
        vault["day_start_balance"] = float(vault.get("balance", 1000.0))
        vault["shield_mode_active"] = False
    vault["daily_pnl_usd"] = float(vault.get("daily_pnl_usd", 0.0)) + float(pnl_usd)
    day_start = float(vault.get("day_start_balance") or vault.get("balance") or 1000.0)
    if day_start > 0 and vault["daily_pnl_usd"] <= -(0.03 * day_start):
        vault["shield_mode_active"] = True
    # Ak sa denný PnL zotavil nad -1.5 %, môžeme štít vypnúť (voliteľné uvoľnenie)
    elif day_start > 0 and vault["daily_pnl_usd"] > -(0.015 * day_start):
        vault["shield_mode_active"] = False


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

    # Liquidation: zvyšok marže je stratený celý (nie lineárny % z liq_price)
    if pos.get("_force_full_margin_loss") or reason == "LIQUIDATION_HIT":
        pnl_usd = -abs(active_margin)
        net_pnl_pct = -100.0 * leverage if active_margin > 0 else 0.0
    else:
        gross_pnl_pct = ((exit_price - entry) / max(1e-9, entry)) * dir_mult * leverage * 100.0
        # Len exit leg (entry fee už strhnutá pri otvorení / limit fill)
        xcost = exit_cost_rate(pos["window_id"], cost_rate)
        net_pnl_pct = gross_pnl_pct - (xcost * leverage * 100.0)
        pnl_usd = active_margin * (net_pnl_pct / 100.0)

    vault["balance"] = round(float(vault.get("balance", 100.0)) + pnl_usd, 4)
    update_daily_pnl_and_shield(vault, pnl_usd, now_dt)

    # Celkový PnL obchodu = scale-outy (TP1/TP2) + finálny zvyšok
    realized_prev = float(pos.get("realized_pnl_usd", 0.0))
    total_pnl_usd = realized_prev + float(pnl_usd)

    # R-multiple: risk base = max nasadená marža × |initial_entry − initial_sl| / initial_entry
    # initial_entry sa NEPREPISUJE pri DCA (priemerný entry by skreslil 1R v MMR matici)
    initial_entry = float(pos.get("initial_entry_price") or entry)
    initial_sl = float(pos.get("initial_sl_price") or pos.get("sl_tsl_price") or initial_entry)
    deployed_margin = float(
        pos.get("max_deployed_margin")
        or pos.get("initial_margin_usd")
        or pos.get("margin_usd")
        or active_margin
        or 1.0
    )
    sl_dist = abs(initial_entry - initial_sl) / max(1e-9, initial_entry)
    risk_usd = max(1e-9, deployed_margin * sl_dist * leverage)
    r_mult = total_pnl_usd / risk_usd

    is_win = total_pnl_usd > 0
    try:
        update_ai_learning(
            symbol=pos.get("symbol", ""),
            direction=pos.get("direction", ""),
            regime=pos.get("regime", "FUTURES"),
            is_funding_trap=bool(pos.get("is_funding_trap", False)),
            is_win=is_win,
            r_multiple=r_mult,
            vault=vault,
            regime_x=pos.get("regime_x"),
            regime_y=pos.get("regime_y"),
        )
    except Exception:
        pass
    try:
        update_filter_weights_from_outcome(pos, is_win, reason, vault)
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

    # DÔLEŽITÉ: okamžitý hard-clear v Supabase active_positions
    # (UPDATE podľa window_id – inak Cloud 2 / cockpit stále vidí starú ACTIVE pozíciu)
    force_clear_active_position(int(pos["window_id"]), vault)


def is_ny_open_lock(now_dt: datetime) -> bool:
    """
    NY Open Lock: blokuje nové vstupy 30 min pred a 30 min po otvorení NY (14:30 UTC približne).
    Paper-trade verzia – jednoduchý časový filter.
    """
    # NYSE open ~ 14:30 UTC (zimný čas) / 13:30 UTC (letný)
    # Ochranné okno: 14:00 – 15:00 UTC (vrátane 15:00:xx do konca prvej minúty)
    hour = now_dt.hour
    minute = now_dt.minute
    if hour == 14:
        return True
    if hour == 15 and minute == 0:
        return True
    return False


def execute_scale_out(pos: dict, price: float, pct: float, reason: str, vault: dict, now_dt: datetime):
    """Pomocná metoda pro kalkulaci zisku při částečném scale-outu (paper)."""
    dir_mult = 1.0 if pos["direction"] == "LONG" else -1.0
    cost_rate = pos.get("cost_rate", compute_roundtrip_cost_rate(pos["window_id"]))
    gross_pnl = ((price - pos["entry_price"]) / pos["entry_price"]) * dir_mult * pos["leverage"] * 100.0
    # Len exit leg — entry fees už zaúčtované
    xcost = exit_cost_rate(pos["window_id"], cost_rate)
    net_pnl_pct = gross_pnl - (xcost * pos["leverage"] * 100.0)
    closed_margin = pos["active_margin"] * pct
    partial_pnl_usd = closed_margin * (net_pnl_pct / 100.0)
    vault["balance"] = round(vault["balance"] + partial_pnl_usd, 4)
    update_daily_pnl_and_shield(vault, partial_pnl_usd, now_dt)
    pos["active_margin"] -= closed_margin
    # Kumuluj realizovaný PnL pre finálny R-multiple celého obchodu
    pos["realized_pnl_usd"] = float(pos.get("realized_pnl_usd", 0.0)) + float(partial_pnl_usd)
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

    # Denný rollover + aktualizácia shield_mode (0 PnL, len dátum)
    update_daily_pnl_and_shield(vault, 0.0, now_dt)

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
        # Shadow pool sa vyhodnocuje v 3min scane (process_shadow_pool) – tu len ACTIVE tickery
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

            # SYSTEM D – intrabar wick levels from latest 15m candle
            candle_high = curr_price
            candle_low = curr_price
            if klines and len(klines) > 0:
                try:
                    candle_high = float(get_high(klines[-1]))
                    candle_low = float(get_low(klines[-1]))
                except Exception:
                    pass

            # SYSTEM D – FUTURES liquidation price (90% of margin at leverage)
            # Jeden path: close_position spočíta PnL; žiadne manuálne double-write balance/realized.
            if pos.get("regime") == "FUTURES" and int(pos.get("leverage") or 1) > 1:
                lev = max(1, int(pos.get("leverage", 1)))
                entry_px = float(pos.get("entry_price") or curr_price)
                liq_price = entry_px * (1.0 - dir_mult * (0.90 / lev))
                hit_liq = (
                    (is_long and (curr_price <= liq_price or candle_low <= liq_price))
                    or (not is_long and (curr_price >= liq_price or candle_high >= liq_price))
                )
                if hit_liq:
                    # Flag pre close_position: full remaining margin loss (nie mark-to-market %)
                    pos["_force_full_margin_loss"] = True
                    close_position(pos, liq_price, "LIQUIDATION_HIT", vault)
                    db_dirty = True
                    continue

            # SYSTEM D – Funding settlement at 00/08/16 UTC
            if now_dt.hour in (0, 8, 16) and int(pos.get("last_funding_hour", -1)) != now_dt.hour:
                try:
                    funding_rate = await fetch_okx_funding(client, sym)
                    # LONG pays when funding > 0; SHORT receives (sign via dir_mult)
                    notional = float(pos.get("active_margin") or 0.0) * float(pos.get("leverage") or 1)
                    funding_fee_usd = notional * float(funding_rate) * dir_mult
                    vault["balance"] = round(float(vault.get("balance", 0.0)) - funding_fee_usd, 4)
                    pos["realized_pnl_usd"] = float(pos.get("realized_pnl_usd", 0.0)) - funding_fee_usd
                    pos["last_funding_hour"] = now_dt.hour
                    db_dirty = True
                except Exception:
                    pass

            # SYSTEM C – TWAP pending limit fill (60% remainder)
            pending_m = float(pos.get("pending_limit_margin") or 0.0)
            pending_px = float(pos.get("pending_limit_price") or 0.0)
            if pending_m > 0 and pending_px > 0:
                touched = (
                    (is_long and curr_price <= pending_px)
                    or (not is_long and curr_price >= pending_px)
                )
                # Cancel if price ran away > 0.5 * NATR
                away = abs(curr_price - pending_px) / max(1e-9, pending_px) * 100.0
                if away > 0.5 * float(natr or 1.0):
                    pos["pending_limit_margin"] = 0.0
                    pos["pending_limit_price"] = 0.0
                    db_dirty = True
                elif touched:
                    # Maker fill 0.02% fee; blend average entry
                    old_am = float(pos.get("active_margin") or 0.0)
                    old_entry = float(pos.get("entry_price") or pending_px)
                    fill_px = pending_px  # limit price
                    new_am = old_am + pending_m
                    if new_am > 0:
                        pos["entry_price"] = (old_am * old_entry + pending_m * fill_px) / new_am
                    pos["active_margin"] = new_am
                    pos["max_deployed_margin"] = max(
                        float(pos.get("max_deployed_margin", new_am)), new_am
                    )
                    # Maker fee
                    maker_fee = pending_m * float(pos.get("leverage") or 1) * 0.0002
                    vault["balance"] = round(float(vault.get("balance", 0.0)) - maker_fee, 4)
                    pos["pending_limit_margin"] = 0.0
                    pos["pending_limit_price"] = 0.0
                    db_dirty = True

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
                            pos["max_deployed_margin"] = max(
                                float(pos.get("max_deployed_margin", pos["active_margin"])),
                                float(pos["active_margin"]),
                            )
                            pos[f"dca{idx}_filled"] = True
                            db_dirty = True

                            pos["tp1_price"] = pos["entry_price"] * (1.0 + dir_mult * ((1.0 * natr * tp_mod) / 100.0 + cost_rate))
                            pos["tp2_price"] = pos["entry_price"] * (1.0 + dir_mult * ((1.8 * natr * tp_mod) / 100.0 + cost_rate))

            # D. FÁZOVANÝ SCALE-OUT (TP1 & TP2) S REŽIMOVÝM TSL
            if not pos.get("tp1_hit", False):
                if (is_long and (curr_price >= pos["tp1_price"] or candle_high >= pos["tp1_price"])) or (not is_long and (curr_price <= pos["tp1_price"] or candle_low <= pos["tp1_price"])):
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

                if (is_long and (curr_price >= pos["tp2_price"] or candle_high >= pos["tp2_price"])) or (not is_long and (curr_price <= pos["tp2_price"] or candle_low <= pos["tp2_price"])):
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

            # F. VYHODNOCENÍ ZÁSAHU TVRDÉHO SL / TSL NA BURZE (paper + intrabar wick)
            sl_px = float(pos["sl_tsl_price"])
            hit_sl_tsl = (
                (is_long and (curr_price <= sl_px or candle_low <= sl_px))
                or (not is_long and (curr_price >= sl_px or candle_high >= sl_px))
            )
            if hit_sl_tsl:
                reason = "TRAILING_SL_PROFIT" if pos["tsl_active"] else "HARD_SL_HIT"
                # Exit at SL level (wick fill), not necessarily last trade price
                exit_px = sl_px
                close_position(pos, exit_px, reason, vault)
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

        # Shadow pool MTM: process_shadow_pool v process_global_market_scanning (3min, full tickers)
        # — žiadne extra API ani duplicitné volanie tu.

        # SYSTEM B – meta-controller strictness update
        try:
            update_meta_controller(vault, now_ts)
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
    # 15m NATR (calculate_natr_14) na každom scane → rolling ~30 dní (2880 × 15m barov).
    # Trade NATR aj percentily sú na rovnakom timeframe (15m).
    try:
        btc_15m = await fetch_okx_klines(client, "BTCUSDT", "15m", 25, klines_cache)
        if len(btc_15m) >= 15:
            natr_15m = float(calculate_natr_14(btc_15m))
            hist = list(vault.get("natr_history_30d") or [])
            hist.append(natr_15m)
            # Rolling window: 2880 × 15m ≈ 30 dní
            if len(hist) > 2880:
                hist = hist[-2880:]
            vault["natr_history_30d"] = hist
            if len(hist) >= 15:
                hist_arr = np.asarray(hist, dtype=float)
                vault["vol_prah_low"] = float(np.percentile(hist_arr, 33))
                vault["vol_prah_mid"] = float(np.percentile(hist_arr, 66))
    except Exception:
        # Bezpečný fallback, pokud by API selhalo, aby bot nepřestal fungovat
        vault["vol_prah_low"] = vault.get("vol_prah_low", 1.5)
        vault["vol_prah_mid"] = vault.get("vol_prah_mid", 2.5)

    if ny_lock:
        return  # Pokud je aktivní NY Open Lock, nové obchody neskenujeme

    # B. SBĚR MARKET DATA A VETO FILTRACE
    tickers, oi_map = await fetch_market_snapshot(client)
    update_24h_oi_metrics(tickers, oi_map)
    # Unified shadow pool MTM on already-fetched tickers (zero extra API)
    try:
        process_shadow_pool(vault, tickers, now_ts)
    except Exception:
        pass

    btc_klines = await fetch_okx_klines(client, "BTCUSDT", "15m", 25, klines_cache)
    btc_veto = evaluate_btc_veto(btc_klines)
    vault["last_btc_veto"] = btc_veto
    # Regresný sklon z posledných 10×15m sviečok → stabilnejšia Os X v MMR
    try:
        if len(btc_klines) >= 10:
            closes = [float(get_close(k)) for k in btc_klines[-10:]]
            x = np.arange(len(closes))
            slope = float(np.polyfit(x, closes, 1)[0])
            vault["last_btc_trend_slope"] = (slope / max(1e-9, closes[0])) * 100.0
        else:
            vault["last_btc_trend_slope"] = 0.0
    except Exception:
        vault["last_btc_trend_slope"] = vault.get("last_btc_trend_slope", 0.0)

    # ----- CLOSED-LOOP FORWARD PASS: MMR cell → filter strictness -----
    # Determine current regime from BTC 15m NATR and apply cell_strictness
    try:
        btc_natr = float(calculate_natr_14(btc_klines)) if len(btc_klines) >= 15 else 2.0
        curr_rx, curr_ry = get_mmr_coordinates(btc_natr, vault)
        mmr_cell = get_mmr_cell(curr_rx, curr_ry)
        cell_wr = float(mmr_cell.get("win_rate", 52.0))
        cell_n = int(mmr_cell.get("total_trades", 0) or 0)
        # Scout Mode: untrained cell (total_trades < 5) → relax ~18%, half Kelly later
        if cell_n < 5:
            cell_strictness = 0.75
            vault["scout_mode"] = True
        elif cell_n >= 3:
            vault["scout_mode"] = False
            if cell_wr > 58.0:
                cell_strictness = 0.90  # profitable regime → loosen
            elif cell_wr < 48.0:
                cell_strictness = 1.20  # struggling regime → tighten
            else:
                cell_strictness = 1.0
        else:
            vault["scout_mode"] = False
            cell_strictness = 1.0
        vault["cell_strictness"] = float(cell_strictness)
        vault["last_active_rx"] = int(curr_rx)
        vault["last_active_ry"] = int(curr_ry)
    except Exception:
        vault.setdefault("cell_strictness", 0.85)

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

        # Funding trap (mierne uvoľnené)
        if not is_15m_green and btc_ofd < 1.02:
            funding_rate = await fetch_okx_funding(client, "BTCUSDT")
            if funding_rate > 0.0005:
                direction, is_trap = "SHORT", True
        if direction is None and is_15m_green and btc_ofd > 1.02:
            funding_rate = await fetch_okx_funding(client, "BTCUSDT")
            if funding_rate < -0.0008:
                direction, is_trap = "LONG", True

        # Smer + farba sviečky (uvoľnené z 1.08/0.92)
        if direction is None:
            if btc_ofd >= 1.04 and is_15m_green:
                direction = "LONG"
            elif btc_ofd <= 0.96 and not is_15m_green:
                direction = "SHORT"

        # Čistý OFD bez farby – stále prísnejší, aby sme nelovili fake chop
        if direction is None:
            if btc_ofd >= 1.12:
                direction = "LONG"
            elif btc_ofd <= 0.88:
                direction = "SHORT"

        if direction and check_anti_extreme_shield(btc_klines, direction, is_pd=False, is_btc=True):
            ok_exec, exec_price = await verify_execution_confirmation_btc(client, "BTCUSDT", direction, btc_klines, btc_ofd, klines_cache)
            if ok_exec:
                try:
                    btc_rsi_fp = float(calculate_rsi_14(pd.Series([float(get_close(k)) for k in btc_klines])))
                except Exception:
                    btc_rsi_fp = 50.0
                setup = build_trade_setup(
                    {
                        "symbol": "BTCUSDT",
                        "direction": direction,
                        "is_pd": False,
                        "is_funding_trap": is_trap,
                        "klines": btc_klines,
                        "exec_price": exec_price,
                        "entry_fingerprint": {
                            "macro_btc_1h": float(btc_veto.get("btc_1h_pct", 0.0)),
                            "liq_vol_24h": 0.0,
                            "ofd_val": float(btc_ofd),
                            "shield_rsi": btc_rsi_fp,
                            "micro_wob": 0.0,
                            "micro_vpin": 0.0,
                            "risk_wr": 52.0,
                        },
                    },
                    vault["balance"], 1, vault
                )
                if setup:
                    vault["windows"]["1"] = setup
                    vault["last_trade_opened_ts"] = time.time()
                    # SYSTEM C: taker 0.05% on market leg notional
                    mkt_notional = float(setup.get("active_margin") or 0.0) * float(setup.get("leverage") or 1)
                    vault["balance"] = round(float(vault["balance"]) - mkt_notional * 0.0005, 4)
                    push_active_position_row(setup, vault)
                    active_syms.add("BTCUSDT")
                else:
                    # Filter 6 MMR veto shadow
                    record_shadow_trade(
                        vault,
                        {"symbol": "BTCUSDT", "direction": direction, "exec_price": exec_price,
                         "klines": btc_klines, "natr": calculate_natr_14(btc_klines)},
                        failed_filter="FILTER_6_RISK_MMR",
                    )

    # ===== OKNA 2 & 3: ELITNÍ ŠAMPIONI (Objem >= 10M USD) =====
    for w_idx in ("2", "3"):
        if vault["windows"][w_idx]["status"] == "WAITING":
            cands = await evaluate_candidates(client, tickers, oi_map, active_syms, active_secs, 7_000_000.0, 1e12, btc_veto, klines_cache, is_pd_mode=False, vault=vault)
            for c in cands:
                setup = build_trade_setup(c, vault["balance"], int(w_idx), vault)
                if setup:
                    vault["windows"][w_idx] = setup
                    vault["last_trade_opened_ts"] = time.time()
                    mkt_notional = float(setup.get("active_margin") or 0.0) * float(setup.get("leverage") or 1)
                    vault["balance"] = round(float(vault["balance"]) - mkt_notional * 0.0005, 4)
                    push_active_position_row(setup, vault)
                    active_syms.add(c["symbol"])
                    active_secs.add(c["sector"])
                    break
                else:
                    record_shadow_trade(
                        vault,
                        {**c, "natr": calculate_natr_14(c["klines"]) if c.get("klines") else 2.0},
                        failed_filter=c.get("_failed_filter", "FILTER_6_RISK_MMR"),
                    )

    # ===== OKNA 4 & 5: ŠAMPIONI NÍZKÉ LIKVIDITY (Objem 5M – 9.99M USD) =====
    for w_idx in ("4", "5"):
        if vault["windows"][w_idx]["status"] == "WAITING":
            cands = await evaluate_candidates(client, tickers, oi_map, active_syms, active_secs, 3_500_000.0, 9_999_999.0, btc_veto, klines_cache, is_pd_mode=False, vault=vault)
            for c in cands:
                setup = build_trade_setup(c, vault["balance"], int(w_idx), vault)
                if setup:
                    vault["windows"][w_idx] = setup
                    vault["last_trade_opened_ts"] = time.time()
                    mkt_notional = float(setup.get("active_margin") or 0.0) * float(setup.get("leverage") or 1)
                    vault["balance"] = round(float(vault["balance"]) - mkt_notional * 0.0005, 4)
                    push_active_position_row(setup, vault)
                    active_syms.add(c["symbol"])
                    active_secs.add(c["sector"])
                    break
                else:
                    record_shadow_trade(
                        vault,
                        {**c, "natr": calculate_natr_14(c["klines"]) if c.get("klines") else 2.0},
                        failed_filter=c.get("_failed_filter", "FILTER_6_RISK_MMR"),
                    )

    # ===== OKNO 6: PUMP & DUMP REŽIM (uvoľnené, všetko okrem BTC, objem 0.5M – 8M USD) =====
    if vault["windows"]["6"]["status"] == "WAITING":
        cands_pd = await evaluate_candidates(client, tickers, oi_map, active_syms, active_secs, 500_000.0, 8_000_000.0, btc_veto, klines_cache, is_pd_mode=True, vault=vault)
        for c in cands_pd:
            setup = build_trade_setup(c, vault["balance"], 6, vault)
            if setup:
                vault["windows"]["6"] = setup
                vault["last_trade_opened_ts"] = time.time()
                # P&D enters 100% market – taker on full active margin
                mkt_notional = float(setup.get("active_margin") or 0.0) * float(setup.get("leverage") or 1)
                vault["balance"] = round(float(vault["balance"]) - mkt_notional * 0.0005, 4)
                push_active_position_row(setup, vault)
                break
            else:
                record_shadow_trade(
                    vault,
                    {**c, "natr": calculate_natr_14(c["klines"]) if c.get("klines") else 2.0},
                    failed_filter=c.get("_failed_filter", "FILTER_6_RISK_MMR"),
                )


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



# =========================================================================
# MODULE 4: ISOLATED HISTORICAL DRY-RUN PRE-TRAINER
# =========================================================================
# Sandbox ONLY updates ai_mmr_matrix + vault filter_weights / natr_history_30d.
# NEVER touches vault balance, windows, active_positions, or trade_history.

_PRETRAIN_SYMBOLS = [
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT",
    "BNBUSDT", "ADAUSDT", "AVAXUSDT", "LINKUSDT", "SUIUSDT", "PEPEUSDT",
]


async def _fetch_klines_paginated(
    client: httpx.AsyncClient,
    symbol: str,
    bar: str = "15m",
    target: int = 672,
    sem: asyncio.Semaphore | None = None,
) -> list:
    """Fetch up to `target` historical candles; OKX max ~300 per request via `after`."""
    all_rows: list = []
    after_ts: str | None = None
    remaining = target
    while remaining > 0:
        limit = min(300, remaining)
        params = {"instId": to_okx_inst_id(symbol), "bar": bar, "limit": str(limit)}
        if after_ts is not None:
            params["after"] = after_ts
        if sem is not None:
            async with sem:
                res = await client.get(f"{OKX_API}/api/v5/market/candles", params=params)
                await asyncio.sleep(0.15)
        else:
            res = await client.get(f"{OKX_API}/api/v5/market/candles", params=params)
            await asyncio.sleep(0.15)
        if res.status_code != 200:
            break
        data = res.json()
        if data.get("code") != "0" or not data.get("data"):
            break
        chunk = data["data"]  # newest first from OKX
        if not chunk:
            break
        all_rows.extend(chunk)
        # next page: older than oldest in this chunk (ts is index 0)
        after_ts = str(chunk[-1][0])
        remaining = target - len(all_rows)
        if len(chunk) < limit:
            break
    # chronological oldest → newest
    klines = list(reversed(all_rows))
    # dedupe by ts
    seen = set()
    uniq = []
    for k in klines:
        ts = k[0]
        if ts in seen:
            continue
        seen.add(ts)
        uniq.append(k)
    return uniq[-target:]


def _sim_verify_hist(klines_15m: list, direction: str, ofd: float) -> tuple[bool, float]:
    """
    Historical execution confirmation WITHOUT order book API.
    Uses VPIN, OFD, volume ratio; neutral WOB; fixed tight spread.
    """
    if len(klines_15m) < 12:
        return False, 0.0
    vols = [float(get_vol(k)) for k in klines_15m[-11:]]
    avg_prev = float(np.mean(vols[:-1])) if len(vols) > 1 else 0.0
    curr_vol = vols[-1]
    if avg_prev > 0 and (curr_vol / avg_prev) < 1.10:
        return False, 0.0
    vpin = float(calculate_vpin_toxicity(klines_15m))
    if vpin > 0.80:
        return False, 0.0
    # Neutral book assumption
    wob_imbalance = 0.0
    spread_pct = 0.05
    if spread_pct > 0.18:
        return False, 0.0
    if direction == "LONG" and wob_imbalance < -0.15:
        return False, 0.0
    if direction == "SHORT" and wob_imbalance > 0.15:
        return False, 0.0
    # weak OFD vs direction
    if direction == "LONG" and ofd < 1.05:
        return False, 0.0
    if direction == "SHORT" and ofd > 0.95:
        return False, 0.0
    px = float(get_close(klines_15m[-1]))
    return True, px


def _sandbox_close(pos: dict, exit_price: float, reason: str, sandbox: dict, vault: dict):
    """Close virtual position: update ai_mmr + filter_weights only (no real balance/windows)."""
    dir_mult = 1.0 if pos["direction"] == "LONG" else -1.0
    leverage = float(pos.get("leverage", 1))
    active_margin = float(pos.get("active_margin", 0.0))
    entry = float(pos.get("entry_price", exit_price))
    cost_rate = float(pos.get("cost_rate", 0.0015))
    gross = ((exit_price - entry) / max(1e-9, entry)) * dir_mult * leverage * 100.0
    xcost = 0.0005 + 0.5 * max(0.0, cost_rate - 0.0010)
    net = gross - (xcost * leverage * 100.0)
    pnl_usd = active_margin * (net / 100.0)
    sandbox["balance"] = round(float(sandbox["balance"]) + pnl_usd, 4)
    realized = float(pos.get("realized_pnl_usd", 0.0)) + pnl_usd
    initial_entry = float(pos.get("initial_entry_price") or entry)
    initial_sl = float(pos.get("initial_sl_price") or entry)
    deployed = float(pos.get("max_deployed_margin") or pos.get("margin_usd") or active_margin or 1.0)
    sl_dist = abs(initial_entry - initial_sl) / max(1e-9, initial_entry)
    risk_usd = max(1e-9, deployed * sl_dist * leverage)
    r_mult = realized / risk_usd
    is_win = realized > 0
    try:
        update_ai_learning(
            symbol=pos.get("symbol", ""),
            direction=pos.get("direction", ""),
            regime=pos.get("regime", "FUTURES"),
            is_funding_trap=bool(pos.get("is_funding_trap", False)),
            is_win=is_win,
            r_multiple=r_mult,
            vault=vault,
            regime_x=pos.get("regime_x"),
            regime_y=pos.get("regime_y"),
        )
    except Exception:
        pass
    try:
        update_filter_weights_from_outcome(pos, is_win, reason, vault)
    except Exception:
        pass


def _sandbox_manage(pos: dict, bar: list, sandbox: dict, vault: dict) -> bool:
    """
    Manage one sandbox position on a historical bar.
    Returns True if position still open, False if closed.
    """
    hi = float(get_high(bar))
    lo = float(get_low(bar))
    cl = float(get_close(bar))
    is_long = pos["direction"] == "LONG"
    dir_mult = 1.0 if is_long else -1.0
    entry = float(pos["entry_price"])
    lev = max(1, int(pos.get("leverage") or 1))

    # Liquidation
    if pos.get("regime") == "FUTURES" and lev > 1:
        liq = entry * (1.0 - dir_mult * (0.90 / lev))
        if (is_long and lo <= liq) or (not is_long and hi >= liq):
            pos["_force_full_margin_loss"] = True
            _sandbox_close(pos, liq, "LIQUIDATION_HIT", sandbox, vault)
            return False

    # SL / TSL
    sl = float(pos.get("sl_tsl_price") or 0.0)
    if sl > 0:
        hit_sl = (is_long and lo <= sl) or (not is_long and hi >= sl)
        if hit_sl:
            reason = "TRAILING_SL_PROFIT" if pos.get("tsl_active") else "HARD_SL_HIT"
            _sandbox_close(pos, sl, reason, sandbox, vault)
            return False

    # TP1 scale-out 50% + BE lock
    if not pos.get("tp1_hit"):
        tp1 = float(pos.get("tp1_price") or 0.0)
        hit_tp1 = (is_long and hi >= tp1) or (not is_long and lo <= tp1)
        if hit_tp1 and tp1 > 0:
            # virtual partial
            cost_rate = float(pos.get("cost_rate", 0.0015))
            gross = ((tp1 - entry) / max(1e-9, entry)) * dir_mult * lev * 100.0
            xcost = 0.0005 + 0.5 * max(0.0, cost_rate - 0.0010)
            net = gross - (xcost * lev * 100.0)
            closed_m = float(pos["active_margin"]) * 0.50
            partial = closed_m * (net / 100.0)
            sandbox["balance"] = round(float(sandbox["balance"]) + partial, 4)
            pos["active_margin"] = float(pos["active_margin"]) - closed_m
            pos["realized_pnl_usd"] = float(pos.get("realized_pnl_usd", 0.0)) + partial
            pos["tp1_hit"] = True
            pos["tsl_active"] = True
            pos["extreme_price"] = tp1
            be = entry * (1.0 + dir_mult * (cost_rate + 0.0010))
            pos["sl_tsl_price"] = be

    # TP2 close remainder
    if pos.get("tp1_hit") and not pos.get("tp2_hit"):
        tp2 = float(pos.get("tp2_price") or 0.0)
        hit_tp2 = (is_long and hi >= tp2) or (not is_long and lo <= tp2)
        if hit_tp2 and tp2 > 0:
            _sandbox_close(pos, tp2, "TP2_SCALE_OUT", sandbox, vault)
            return False
        # trail extreme
        if is_long:
            pos["extreme_price"] = max(float(pos.get("extreme_price", cl)), hi)
        else:
            pos["extreme_price"] = min(float(pos.get("extreme_price", cl)), lo)
        natr = float(pos.get("natr") or 2.0)
        step = (0.65 * natr) / 100.0
        new_tsl = float(pos["extreme_price"]) * (1.0 - dir_mult * step)
        if (is_long and new_tsl > pos["sl_tsl_price"]) or (not is_long and new_tsl < pos["sl_tsl_price"]):
            pos["sl_tsl_price"] = new_tsl

    return True


async def run_historical_pretrain(vault: dict):
    """
    7-day 15m dry-run pre-trainer.
    Sandbox balance starts at 100.0 — never writes vault balance/windows/history.
    Only trains ai_mmr_matrix + vault filter_weights (+ natr_history_30d warm-up).
    """
    sem = asyncio.Semaphore(4)
    hist: dict[str, list] = {}
    async with httpx.AsyncClient(http2=True, timeout=20.0) as client:
        async def load_one(sym: str):
            rows = await _fetch_klines_paginated(client, sym, "15m", 672, sem)
            return sym, rows

        results = await asyncio.gather(*(load_one(s) for s in _PRETRAIN_SYMBOLS))
        for sym, rows in results:
            if rows and len(rows) >= 40:
                hist[sym] = rows

    if "BTCUSDT" not in hist or len(hist["BTCUSDT"]) < 40:
        return  # insufficient data

    btc = hist["BTCUSDT"]
    n = len(btc)
    # Align other symbols by timestamp intersection when possible
    sandbox = {"balance": 100.0, "positions": []}  # max 3 concurrent
    # Warm-up NATR history from BTC (sandbox → vault natr_history only)
    natr_hist = list(vault.get("natr_history_30d") or [])

    alts = [s for s in _PRETRAIN_SYMBOLS if s != "BTCUSDT" and s in hist]

    for i in range(30, n):
        btc_window = btc[max(0, i - 24): i + 1]
        if len(btc_window) < 15:
            continue
        # NATR history + percentiles
        try:
            n15 = float(calculate_natr_14(btc_window))
            natr_hist.append(n15)
            if len(natr_hist) > 2880:
                natr_hist = natr_hist[-2880:]
            vault["natr_history_30d"] = natr_hist
            if len(natr_hist) >= 15:
                arr = np.asarray(natr_hist, dtype=float)
                vault["vol_prah_low"] = float(np.percentile(arr, 33))
                vault["vol_prah_mid"] = float(np.percentile(arr, 66))
        except Exception:
            n15 = 2.0

        btc_veto = evaluate_btc_veto(btc_window)
        try:
            if len(btc_window) >= 10:
                closes = [float(get_close(k)) for k in btc_window[-10:]]
                slope = float(np.polyfit(np.arange(len(closes)), closes, 1)[0])
                vault["last_btc_trend_slope"] = (slope / max(1e-9, closes[0])) * 100.0
        except Exception:
            pass
        vault["last_btc_veto"] = btc_veto

        # Manage existing sandbox positions on this bar (BTC bar time approx)
        still = []
        for pos in sandbox["positions"]:
            sym = pos["symbol"]
            series = hist.get(sym) or []
            # use bar at same index if lengths match, else last bar <= i
            bar = series[i] if i < len(series) else (series[-1] if series else None)
            if bar is None:
                still.append(pos)
                continue
            if _sandbox_manage(pos, bar, sandbox, vault):
                still.append(pos)
        sandbox["positions"] = still

        # Entry screening if capacity
        if len(sandbox["positions"]) >= 3:
            continue

        # Build candidate universe for this step
        for sym in (["BTCUSDT"] + alts):
            if len(sandbox["positions"]) >= 3:
                break
            if any(p.get("symbol") == sym for p in sandbox["positions"]):
                continue
            series = hist.get(sym)
            if not series or i >= len(series) or i < 20:
                continue
            klines = series[max(0, i - 24): i + 1]
            if len(klines) < 20:
                continue
            last = klines[-1]
            ofd = compute_ofd_from_klines(klines)
            is_green = float(get_close(last)) > float(get_open(last))
            fctx_long = _get_filter_ctx(vault, "LONG")
            fctx_short = _get_filter_ctx(vault, "SHORT")
            ofd_long_thr = float(fctx_long.get("ofd_long_thr", 1.30))
            ofd_short_thr = float(fctx_short.get("ofd_short_thr", 0.70))
            direction = None
            if ofd > ofd_long_thr and is_green:
                direction = "LONG"
            elif ofd < ofd_short_thr and not is_green:
                direction = "SHORT"
            if not direction:
                continue
            if direction == "LONG" and btc_veto.get("veto_long"):
                continue
            if direction == "SHORT" and btc_veto.get("veto_short"):
                continue
            fctx = fctx_long if direction == "LONG" else fctx_short
            if not check_anti_extreme_shield(klines, direction, is_pd=False, is_btc=(sym == "BTCUSDT"), filter_ctx=fctx):
                continue
            ok, px = _sim_verify_hist(klines, direction, ofd)
            if not ok:
                continue
            cand = {
                "symbol": sym,
                "direction": direction,
                "is_pd": False,
                "is_funding_trap": False,
                "klines": klines,
                "exec_price": px,
                "entry_fingerprint": {
                    "macro_btc_1h": float(btc_veto.get("btc_1h_pct", 0.0)),
                    "liq_vol_24h": 0.0,
                    "ofd_val": float(ofd),
                    "shield_rsi": 50.0,
                    "micro_wob": 0.0,
                    "micro_vpin": float(calculate_vpin_toxicity(klines)) if len(klines) >= 10 else 0.0,
                    "risk_wr": 52.0,
                },
                "book_levels": [],
            }
            # Sandbox balance only — never vault["balance"]
            setup = build_trade_setup(cand, float(sandbox["balance"]), window_id=1, vault=vault)
            if not setup:
                continue
            # Strip live-order IDs noise; keep learning fields
            setup["realized_pnl_usd"] = 0.0
            setup["tp1_hit"] = False
            setup["tp2_hit"] = False
            setup["tsl_active"] = False
            sandbox["positions"].append(setup)
            # Scout/small open fee on sandbox only
            mkt_notional = float(setup.get("active_margin") or 0.0) * float(setup.get("leverage") or 1)
            sandbox["balance"] = round(float(sandbox["balance"]) - mkt_notional * 0.0005, 4)

    # Persist trained weights + natr history (NOT sandbox balance)
    vault["natr_history_30d"] = natr_hist[-2880:]
    vault["pretrain_last_ts"] = time.time()
    vault["pretrain_sandbox_end_balance"] = float(sandbox["balance"])  # diagnostic only
    save_vault(vault, sync_cloud=True)



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

    # ----- MODULE 4: Historické učenie (izolovaný sandbox) -----
    st.divider()
    st.subheader("Kalibrácia & Historické učenie")
    has_active_trades = any(
        (w or {}).get("status") == "ACTIVE"
        for w in (vault.get("windows") or {}).values()
    )
    if has_active_trades:
        st.info(
            "Historické učenie je uzamknuté, kým prebieha aktívny obchod. "
            "Odomkne sa automaticky po uzavretí všetkých pozícií."
        )
    pretrain_btn = st.button(
        "Spustiť historické učenie (posledných 7 dní)",
        disabled=has_active_trades,
        use_container_width=True,
    )
    if pretrain_btn and not has_active_trades:
        with st.spinner("Prebieha 7-dňová historická simulácia a kalibrácia matice..."):
            try:
                asyncio.run(run_historical_pretrain(st.session_state.vault))
                st.success(
                    "Historické učenie dokončené. "
                    "Aktualizované: ai_mmr_matrix + filter_weights (+ NATR história). "
                    "Live balance a okná neboli zmenené."
                )
            except Exception as e:
                st.error(f"Historické učenie zlyhalo: {e}")
        st.rerun()

    # Auto-refresh
    if auto_refresh:
        time.sleep(15)
        st.rerun()


# Vstupný bod Streamlit
if __name__ == "__main__" or True:  # Streamlit vždy spúšťa modul
    main_ui()
