import streamlit as st
import httpx
import asyncio
import json
import os
import time
import operator
import math
import random
import numpy as np
import pandas as pd
from datetime import datetime, timezone, timedelta
from supabase import create_client, Client
from ai_window6 import scan_ai_champion_window6

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
        "cell_strictness": 0.85,
        "pending_setups": {},
        "ai_w6_feedback": [],
        "last_market_breadth": 0.5,
        "last_market_dispersion": 2.0,
        "last_btc_2h_pct": 0.0,
        "auto_refresh_enabled": False,
        "micro_weights": {
            "spoof_sensitivity": 1.0,
            "wall_frontrun_mult": 3.5,
            "dca_wob_floor": 0.10,
        },
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
                    state.setdefault("pending_setups", {})
                    state.setdefault("ai_w6_feedback", [])
                    state.setdefault("last_market_breadth", 0.5)
                    state.setdefault("last_market_dispersion", 2.0)
                    state.setdefault("last_btc_2h_pct", 0.0)
                    state.setdefault("auto_refresh_enabled", False)
                    state.setdefault("micro_weights", {
                        "spoof_sensitivity": 1.0,
                        "wall_frontrun_mult": 3.5,
                        "dca_wob_floor": 0.10,
                    })
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
            state.setdefault("pending_setups", {})
            state.setdefault("ai_w6_feedback", [])
            state.setdefault("auto_refresh_enabled", False)
            state.setdefault("micro_weights", {
                "spoof_sensitivity": 1.0,
                "wall_frontrun_mult": 3.5,
                "dca_wob_floor": 0.10,
            })
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
    _raw_pnl_usd = pos.get("live_pnl_usd") if pos.get("live_pnl_usd") is not None else pos.get("pnl_usd")
    _raw_pnl_pct = pos.get("live_pnl_pct") if pos.get("live_pnl_pct") is not None else pos.get("pnl_percent")
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
        "pnl_usd": float(_raw_pnl_usd or 0.0),
        "pnl_percent": float(_raw_pnl_pct or 0.0),
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

async def fetch_okx_instrument_spec(symbol: str, client: httpx.AsyncClient | None = None) -> dict | None:
    global _INSTRUMENT_CACHE, _INSTRUMENT_CACHE_TS
    now = time.time()
    if (now - _INSTRUMENT_CACHE_TS) > _INSTRUMENT_CACHE_TTL:
        _INSTRUMENT_CACHE = {}
        _INSTRUMENT_CACHE_TS = now
    if symbol in _INSTRUMENT_CACHE:
        return _INSTRUMENT_CACHE[symbol]
    inst_id = to_okx_inst_id(symbol)
    try:
        async def _do_get(c: httpx.AsyncClient):
            res = await c.get(
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

        if client is not None:
            return await _do_get(client)
        async with httpx.AsyncClient(timeout=6.0) as local_client:
            return await _do_get(local_client)
    except Exception:
        return None

async def okx_min_notional_usd(symbol: str, price: float, client: httpx.AsyncClient | None = None) -> float:
    if price <= 0:
        return 5.2
    spec = await fetch_okx_instrument_spec(symbol, client=client)
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

    unique_syms = list(dict.fromkeys(symbols))
    requested_symbols = set(unique_syms)
    tickers: dict = {}

    # Primárne načítanie všetkých SWAP tickerov jedným hromadným requestom.
    try:
        res = await client.get(
            f"{OKX_API}/api/v5/market/tickers",
            params={"instType": "SWAP"},
        )
        res.raise_for_status()
        payload = res.json()
        if payload.get("code") != "0":
            raise RuntimeError(f"OKX bulk tickers chyba: {payload.get('msg')}")

        for item in payload.get("data", []):
            sym = from_okx_inst_id(str(item.get("instId") or ""))
            if sym in requested_symbols:
                tickers[sym] = item
    except Exception:
        # Pri zlyhaní bulk endpointu pokračuje fallback pre všetky požadované symboly.
        tickers = {}

    # Individuálny fallback sa použije iba pre symboly, ktoré bulk odpoveď nepokryla.
    missing_symbols = [sym for sym in unique_syms if sym not in tickers]
    if missing_symbols:
        results = await asyncio.gather(
            *(fetch_single_ticker(client, sym) for sym in missing_symbols)
        )
        for sym, ticker in zip(missing_symbols, results):
            if ticker is not None:
                tickers[sym] = ticker

    return tickers

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


async def fetch_real_tape_cvd(client: httpx.AsyncClient, symbol: str, limit: int = 100) -> dict:
    """
    Real Tape CVD – zrealizované nákupné a predajné príkazy z OKX public trades.
    Vráti tape_ofd (buy/sell volume ratio), whale_ratio a ok flag.
    """
    fallback = {
        "ok": False,
        "tape_ofd": 1.0,
        "buy_vol": 0.0,
        "sell_vol": 0.0,
        "whale_ratio": 0.0,
    }
    inst_id = to_okx_inst_id(symbol)
    try:
        res = await client.get(
            f"{OKX_API}/api/v5/market/trades",
            params={"instId": inst_id, "limit": str(limit)},
        )
        if res.status_code != 200:
            return fallback
        data = res.json()
        if data.get("code") != "0" or not data.get("data"):
            return fallback
        trades = data.get("data", [])
        if not trades:
            return fallback
        sizes = []
        buy_vol = 0.0
        sell_vol = 0.0
        for t in trades:
            try:
                sz = float(t.get("sz", 0.0) or 0.0)
            except Exception:
                sz = 0.0
            if sz <= 0:
                continue
            sizes.append(sz)
            side = str(t.get("side", "")).lower()
            if side == "buy":
                buy_vol += sz
            elif side == "sell":
                sell_vol += sz
        if not sizes or (buy_vol + sell_vol) <= 0:
            return fallback
        avg_sz = float(sum(sizes) / max(1, len(sizes)))
        whale_vol = 0.0
        for t in trades:
            try:
                sz = float(t.get("sz", 0.0) or 0.0)
            except Exception:
                sz = 0.0
            if sz > 3.0 * avg_sz:
                whale_vol += sz
        tape_ofd = buy_vol / max(1e-9, sell_vol)
        whale_ratio = whale_vol / max(1e-9, buy_vol + sell_vol)
        return {
            "ok": True,
            "tape_ofd": float(tape_ofd),
            "buy_vol": float(buy_vol),
            "sell_vol": float(sell_vol),
            "whale_ratio": float(whale_ratio),
        }
    except Exception:
        return fallback


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



def compute_theta_timers(natr: float) -> tuple[float, float]:
    safe_natr = max(0.5, float(natr or 1.5))
    sniper_ttl_sec = float(np.clip(720.0 / safe_natr, 240.0, 720.0))
    max_hold_sec = float(np.clip(2400.0 / safe_natr, 900.0, 2400.0))
    return sniper_ttl_sec, max_hold_sec


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

def load_mmr_cache() -> dict[tuple[int, int], dict]:
    """One-shot load of entire ai_mmr_matrix into {(regime_x, regime_y): row}."""
    try:
        res = supabase.table("ai_mmr_matrix").select("*").execute()
        if not res.data:
            return {}
        return {(int(r["regime_x"]), int(r["regime_y"])): r for r in res.data}
    except Exception:
        return {}

def update_24h_oi_metrics(tickers: dict, oi_map: dict, existing_metrics: dict | None = None):
    if not oi_map:
        return
    existing = existing_metrics if existing_metrics is not None else get_24h_metrics()
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
    # cell_strictness riadi veľkosť stávky (Kelly), nie prahy filtrov
    strictness = float(np.clip(base, 0.75, 1.30))
    w = lambda k, d=1.0: float(weights.get(k, d))
    raw_ofd_long = 1.30 * strictness * w("ofd")
    raw_ofd_short = 0.70 / max(1e-9, strictness * w("ofd"))
    return {
        "strictness": strictness,
        "cell_strictness": cell_s,
        "w_macro": w("macro"),
        "w_liq": w("liq"),
        "w_ofd": w("ofd"),
        "w_shield": w("shield"),
        "w_micro": w("micro"),
        "w_risk": w("risk"),
        "ofd_long_thr": max(1.18, raw_ofd_long),
        "ofd_short_thr": min(0.82, raw_ofd_short),
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
    if len(cand.get("klines") or []) >= 25:
        v5_feats = _extract_v5_features_from_klines(
            cand["klines"], vault, float(vault.get("last_btc_2h_pct", 0.0))
        )
        if v5_feats:
            item["v5_features"] = dict(v5_feats)
    pool = list(vault.get("shadow_pool") or vault.get("shadow_trades") or [])
    pool.append(item)
    vault["shadow_pool"] = pool[-100:]


def process_shadow_pool(vault: dict, tickers: dict, now_ts: float):
    """
    Evaluate unified shadow_pool during 3-min scan using already-fetched tickers (0 extra API).
    Dynamický pool od age >= 0 do age <= 3600. TP1 first → loosen filter; SL first → tighten.
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
        # Dynamický pool: vyhodnocuj okamžite od age >= 0 (žiadny 900s lock)
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
            if sh.get("v5_features"):
                feedback = list(vault.get("ai_w6_feedback") or [])
                feedback.append({
                    "source": "SHADOW_POOL",
                    "is_win": False,
                    "direction": sh.get("direction"),
                    "r_multiple": -1.0,
                    "features": dict(sh["v5_features"]),
                    "closed_ts": float(now_ts),
                })
                vault["ai_w6_feedback"] = feedback[-600:]
            w = float(weights.get(key, 1.0))
            weights[key] = float(np.clip(w + 0.02, 0.70, 1.40))
            continue  # evaluated out
        if hit_tp and not hit_sl:
            if sh.get("v5_features"):
                feedback = list(vault.get("ai_w6_feedback") or [])
                feedback.append({
                    "source": "SHADOW_POOL",
                    "is_win": True,
                    "direction": sh.get("direction"),
                    "r_multiple": 1.0,
                    "features": dict(sh["v5_features"]),
                    "closed_ts": float(now_ts),
                })
                vault["ai_w6_feedback"] = feedback[-600:]
            w = float(weights.get(key, 1.0))
            weights[key] = float(np.clip(w - 0.025, 0.70, 1.40))
            if is_long:
                vault["strictness_long"] = float(np.clip(float(vault.get("strictness_long", 1.0)) - 0.015, 0.75, 1.30))
            else:
                vault["strictness_short"] = float(np.clip(float(vault.get("strictness_short", 1.0)) - 0.015, 0.75, 1.30))
            continue  # evaluated out
        remaining.append(sh)
    vault["filter_weights"] = weights
    vault["shadow_pool"] = remaining[-100:]



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
    Weights always clamped to [0.70, 1.40].
    """
    weights = vault.setdefault("filter_weights", {
        "macro": 1.0, "liq": 1.0, "ofd": 1.0,
        "shield": 1.0, "micro": 1.0, "risk": 1.0,
    })
    mw = vault.setdefault("micro_weights", {
        "spoof_sensitivity": 1.0,
        "wall_frontrun_mult": 3.5,
        "dca_wob_floor": 0.10,
    })
    reason_u = (reason or "").upper()
    failed = (pos.get("failed_filter") or "").upper() if pos else ""
    fp = (pos.get("entry_fingerprint") or {}) if pos else {}
    direction = (pos.get("direction") or "").upper() if pos else ""

    def adj(key: str, delta: float):
        weights[key] = float(np.clip(float(weights.get(key, 1.0)) + delta, 0.70, 1.40))

    def clamp_all():
        for k in list(weights.keys()):
            weights[k] = float(np.clip(float(weights.get(k, 1.0)), 0.70, 1.40))
        vault["filter_weights"] = weights

    # --- micro_weights adaptive learning (spoof / wall / DCA absorption) ---
    if "SPOOF" in reason_u:
        # Predčasný únik v strate bol falošný poplach (príliš vysoká citlivosť) → citlivosť ZNIŽUJ
        mw["spoof_sensitivity"] = float(np.clip(
            float(mw.get("spoof_sensitivity", 1.0)) - 0.05,
            0.70, 1.40,
        ))
    if pos.get("wall_frontrun_used") or "WALL_FRONTRUN" in reason_u:
        if is_win:
            mw["wall_frontrun_mult"] = float(np.clip(
                float(mw.get("wall_frontrun_mult", 3.5)) - 0.05,
                2.2, 5.0,
            ))
        else:
            mw["wall_frontrun_mult"] = float(np.clip(
                float(mw.get("wall_frontrun_mult", 3.5)) + 0.05,
                2.2, 5.0,
            ))
    if not is_win and (bool(pos.get("dca1_filled")) or bool(pos.get("dca2_filled"))):
        mw["dca_wob_floor"] = float(np.clip(
            float(mw.get("dca_wob_floor", 0.10)) + 0.03,
            -0.10, 0.35,
        ))
    if is_win:
        mw["dca_wob_floor"] = float(np.clip(
            float(mw.get("dca_wob_floor", 0.10)) - 0.01,
            -0.10, 0.35,
        ))
    vault["micro_weights"] = mw

    # --- Shadow incubator feedback ---
    if "SHADOW_WIN" in reason_u:
        # Overly restrictive filter that blocked a winner → loosen it
        if "MICRO" in reason_u or "VERIFY" in reason_u or "FILTER_5" in failed or "MICRO" in failed:
            adj("micro", -0.025)
        if "RISK" in reason_u or "MMR" in reason_u or "FILTER_6" in failed or "RISK" in failed:
            adj("risk", -0.025)
        if "SHIELD" in reason_u or "SHIELD" in failed:
            adj("shield", -0.025)
        # Slightly reduce directional strictness
        if direction == "SHORT":
            vault["strictness_short"] = float(np.clip(float(vault.get("strictness_short", 1.0)) - 0.02, 0.75, 1.30))
        else:
            vault["strictness_long"] = float(np.clip(float(vault.get("strictness_long", 1.0)) - 0.02, 0.75, 1.30))
        clamp_all()
        return

    if "SHADOW_LOSS" in reason_u:
        if "MICRO" in reason_u or "VERIFY" in reason_u or "FILTER_5" in failed:
            adj("micro", +0.025)
        if "RISK" in reason_u or "FILTER_6" in failed:
            adj("risk", +0.025)
        clamp_all()
        return

    # --- Real trade: WIN → gently relax all weights toward 1.0 ---
    # THETA_DECAY_PROFIT_EXIT is a profit-taking exit, equivalent to TP1_WALL_FRONTRUN.
    if is_win or reason_u in {"THETA_DECAY_PROFIT_EXIT", "TP1_WALL_FRONTRUN"}:
        for k in list(weights.keys()):
            w0 = float(weights.get(k, 1.0))
            weights[k] = float(np.clip(w0 * 0.98 + 1.0 * 0.02, 0.70, 1.40))
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
    """
    Hard Safety Gates only (2-layer filter).
    Impulse size and EMA20 distance are left to 1m Sniper timing.
    Keeps: wick > 0.50, structural side of EMA20, extreme RSI 14 (>82 / <18).
    """
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
        # Hard wick gate (symmetrical)
        if direction == "LONG" and (upper_wick / candle_range) > 0.50:
            return False
        if direction == "SHORT" and (lower_wick / candle_range) > 0.50:
            return False
    ema_20 = float(df["c"].ewm(span=20, adjust=False).mean().iloc[-1])
    # Structural trend: no LONG under EMA20 / no SHORT above EMA20
    if direction == "LONG" and last_c <= ema_20:
        return False
    if direction == "SHORT" and last_c >= ema_20:
        return False
    # Extreme RSI hard caps (independent of adaptive soft thresholds)
    rsi_14 = calculate_rsi_14(df["c"])
    if not np.isnan(rsi_14):
        if direction == "LONG" and rsi_14 > 82.0:
            return False
        if direction == "SHORT" and rsi_14 < 18.0:
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
    sectors_map: dict | None = None,
    metrics_24h: dict | None = None,
):
    if sectors_map is None:
        sectors_map = get_sectors_map()
    if metrics_24h is None:
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
                    soft_m = float((fctx or {}).get("soft_mult", 1.0))
                    sas_score = (abs(price_change_pct) + 1.0) * (ofd if direction == "LONG" else (1.0 / max(0.1, ofd))) * (10.0 / vol_norm) * soft_m
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


FEATURES_V5 = [
    "natr", "rsi", "ofd", "vpin", "vol_ratio", "ema_dist_atr",
    "z_us_vwap", "rel_btc_2h", "market_breadth", "market_dispersion",
    "regime_x", "regime_y",
]


def _extract_v5_features_from_klines(
    klines: list,
    vault: dict,
    btc_2h_pct: float = 0.0,
) -> dict | None:
    """Vypočíta kanonický 12-prvkový V5 vektor z 15m OHLCV sviečok."""
    if not isinstance(klines, list) or len(klines) < 25:
        return None
    try:
        df = pd.DataFrame(
            klines,
            columns=["ts", "o", "h", "l", "c", "v", "vc", "vq", "cf"],
        )
        for col in ("o", "h", "l", "c", "v"):
            df[col] = pd.to_numeric(df[col], errors="coerce")
        df = df.dropna(subset=["o", "h", "l", "c", "v"]).reset_index(drop=True)
        if len(df) < 25:
            return None
        prev_c = df["c"].shift(1)
        tr = pd.concat(
            [df["h"] - df["l"], (df["h"] - prev_c).abs(), (df["l"] - prev_c).abs()],
            axis=1,
        ).max(axis=1)
        atr = tr.rolling(14).mean()
        natr_s = (atr / df["c"].replace(0, np.nan)) * 100.0
        delta = df["c"].diff()
        gain = delta.clip(lower=0).rolling(14).mean()
        loss = (-delta.clip(upper=0)).rolling(14).mean()
        rs = gain / loss.replace(0, 1e-9)
        rsi_s = 100.0 - (100.0 / (1.0 + rs))
        rng = (df["h"] - df["l"]).replace(0, 1e-9)
        body_frac = (df["c"] - df["o"]).abs() / rng
        buyer_ratio = np.where(
            df["c"] >= df["o"], 0.5 + 0.5 * body_frac, 0.5 - 0.5 * body_frac
        )
        buy_vol = (df["v"] * buyer_ratio).rolling(5).sum()
        sell_vol = (df["v"] * (1.0 - buyer_ratio)).rolling(5).sum().replace(0, 1e-9)
        ofd_s = buy_vol / sell_vol
        c_ratio = (df["c"] - df["l"]) / rng
        b_v = df["v"] * c_ratio
        s_v = df["v"] * (1.0 - c_ratio)
        vpin_s = (b_v - s_v).abs().rolling(10).sum() / (
            (b_v + s_v).rolling(10).sum().replace(0, 1e-9)
        )
        vol_ratio_s = df["v"] / df["v"].rolling(10).mean().replace(0, 1e-9)
        ema = df["c"].ewm(span=20, adjust=False).mean()
        ema_dist_s = (df["c"] - ema) / atr.replace(0, 1e-9)

        times = pd.to_datetime(df["ts"].astype(np.int64), unit="ms", utc=True)
        now_utc = times.iloc[-1]
        us_open = now_utc.replace(hour=14, minute=30, second=0, microsecond=0)
        us_start = us_open - pd.Timedelta(days=1) if now_utc < us_open else us_open
        session = df[times >= us_start]
        if len(session) < 3:
            session = df.tail(20)
        typical = (session["h"] + session["l"] + session["c"]) / 3.0
        volume = session["v"].replace(0, 1e-9)
        vwap = (typical * volume).cumsum() / volume.cumsum()
        std_dev = float((typical - vwap).std())
        z_us_vwap = 0.0 if not np.isfinite(std_dev) or std_dev <= 1e-9 else float(
            np.clip((float(df["c"].iloc[-1]) - float(vwap.iloc[-1])) / std_dev, -5.0, 5.0)
        )
        c_now = float(df["c"].iloc[-1])
        c_2h = float(df["c"].iloc[-9])
        coin_2h_pct = ((c_now - c_2h) / max(1e-9, c_2h)) * 100.0
        natr = float(natr_s.iloc[-1])
        rx, ry = get_mmr_coordinates(natr, vault if isinstance(vault, dict) else {})
        result = {
            "natr": natr,
            "rsi": float(rsi_s.iloc[-1]),
            "ofd": float(ofd_s.iloc[-1]),
            "vpin": float(vpin_s.iloc[-1]),
            "vol_ratio": float(vol_ratio_s.iloc[-1]),
            "ema_dist_atr": float(ema_dist_s.iloc[-1]),
            "z_us_vwap": float(z_us_vwap),
            "rel_btc_2h": float(coin_2h_pct - float(btc_2h_pct or 0.0)),
            "market_breadth": float((vault or {}).get("last_market_breadth", 0.5)),
            "market_dispersion": float((vault or {}).get("last_market_dispersion", 2.0)),
            "regime_x": float(rx),
            "regime_y": float(ry),
        }
        if any(not np.isfinite(v) for v in result.values()):
            return None
        return {key: float(result[key]) for key in FEATURES_V5}
    except Exception:
        return None


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


async def build_trade_setup(cand: dict, balance: float, window_id: int, vault: dict, client: httpx.AsyncClient | None = None, mmr_cache: dict | None = None) -> dict | None:
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
    max_natr = 9.0 if is_pd else 5.0
    if natr > max_natr or natr < 0.12 or np.isnan(natr):
        return None

    # 2. Určení souřadnic v MMR matici
    rx, ry = get_mmr_coordinates(natr, vault)
    
    # 3. Načtení AI paměťové buňky z MMR matice (Abstrakce od symbolů)
    mmr_cell = get_mmr_cell(rx, ry, mmr_cache=mmr_cache)
    cell_wr = float(mmr_cell["win_rate"]) / 100.0
    trades_n = int(mmr_cell["total_trades"])
    
    # Bayesovské vyhlazení pravděpodobnosti (Prior síla = 10 vzorků, Prior WR = 52%)
    prior_strength = 6.0
    prior_wr = 0.48
    wr = ((cell_wr * trades_n) + (prior_wr * prior_strength)) / (trades_n + prior_strength)

    # 4. DRAWDOWN CONTROL & SHIELD MODE INTERCEPTOR
    # Pokud denní ztráta překročí 3 %, aktivuje se krizový štít
    is_shield_active = vault.get("shield_mode_active", False)
    veto_threshold = 0.34 if is_shield_active else 0.30
    
    # Continuous Scout Mode: nízke WR neblokuje vstup – len zmenší stávku (nižšie)
    continuous_scout = bool(wr < veto_threshold)


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

    # Continuous Scout: nikdy neskôr slepý – minimálny prieskumný zlomok
    cell_trades = int(mmr_cell.get("total_trades", 0) or 0)
    if continuous_scout or full_kelly <= 0:
        kelly_fraction = 0.08
        full_kelly = max(float(full_kelly), 0.15)
    elif cell_trades < 5:
        kelly_fraction = 0.20  # Scout Mode – uvoľnené
    elif is_shield_active:
        kelly_fraction = 0.20
    else:
        kelly_fraction = 0.40
    max_risk_pct = 0.015 if is_pd else 0.025
    # cell_strictness z vaultu škáluje veľkosť stávky (nie filter prahy)
    cell_s = float(vault.get("cell_strictness", 1.0) or 1.0)
    kelly_margin = min(
        balance * full_kelly * (kelly_fraction / max(0.75, cell_s)),
        balance * max_risk_pct,
    )

    # Ověření reálných limitů OKX burzy
    # Ak Kellyho marža nestačí na min. notional, obchod NEOTVÁRAME
    # (žiadne násilné navýšenie nad riziko z Kelly kritéria).
    min_notional = await okx_min_notional_usd(sym, price, client=client)
    min_margin = min_notional / max(1, leverage)

    hard_cap_margin = balance * max_risk_pct
    if kelly_margin < min_margin:
        if min_margin <= hard_cap_margin and balance >= min_margin * 1.5:
            margin_usd = min_margin
        else:
            return None
    else:
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
        # P&D: zúžený SL, hard floor 1.2 %
        sl_pct = max(0.012, (natr * 1.8 * sl_mod) / 100.0)
        tp1 = price * (1.0 + dir_mult * ((natr * 1.0 * tp_mod) / 100.0 + cost_rate))
        tp2 = price * (1.0 + dir_mult * ((natr * 1.8 * tp_mod) / 100.0 + cost_rate))
    else:
        # Max 2 DCA úrovne (DCA3 vypnuté)
        dca1 = price * (1.0 - dir_mult * ((0.6 * natr) / 100.0))
        dca2 = price * (1.0 - dir_mult * ((1.2 * natr) / 100.0))
        dca3 = 0.0

        # Zúžený SL: ~2.0 × NATR, hard floor 1.0 %
        raw_sl_mult = max(1.8, 2.0 * sl_mod)
        sl_pct = max(0.010, (raw_sl_mult * natr) / 100.0)

        tp1 = price * (1.0 + dir_mult * ((1.0 * natr * tp_mod) / 100.0 + cost_rate))
        tp2 = price * (1.0 + dir_mult * ((1.8 * natr * tp_mod) / 100.0 + cost_rate))

    sl_jitter = random.uniform(0.0005, 0.0018)
    sl_price = price * (1.0 - dir_mult * (sl_pct + sl_jitter))

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

    # Jednotný 100 % vstup na VWAP cene (bez 40/60 TWAP pending leg)
    # Celá marža ide hneď do active_margin; pending limit vypnutý.
    if margin_usd * max(1, leverage) < min_notional:
        return None  # celý setup pod minimom OKX
    active_margin = margin_usd
    pending_limit_margin = 0.0
    pending_limit_price = 0.0

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
        "max_hold_sec": compute_theta_timers(natr)[1],
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
        "dca3_price": 0.0, "dca3_filled": False,
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
        "ai_features": dict(cand.get("ai_features") or {}),
        "v5_features": (
            dict(cand.get("ai_features") or {})
            if len(cand.get("ai_features") or {}) == 12
            else _extract_v5_features_from_klines(
                klines, vault, float(vault.get("last_btc_2h_pct", 0.0))
            )
        ),
        "ai_confidence": float(cand.get("ai_confidence", 0.0)),
        "source": cand.get("source", ""),
    }


# =========================================================================
# BLOK 5: GLOBÁLNÍ 3x3 MMR MATICE UČENÍ & KNN SPATIAL SMOOTHING
# =========================================================================

def get_mmr_cell(regime_x: int, regime_y: int, mmr_cache: dict | None = None) -> dict:
    """
    Abstrakce od symbolů. Načítá statistické parametry úspěšnosti z globální
    3x3 MMR matice na základě trendových (X) a volatilitních (Y) souřadnic.
    Preferuje in-memory mmr_cache ak je dostupný.
    """
    key = (int(regime_x), int(regime_y))
    if mmr_cache is not None and key in mmr_cache:
        return mmr_cache[key]
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
            cell = res.data[0]
            if mmr_cache is not None:
                mmr_cache[key] = cell
            return cell
        # Pokud buňka v DB chybí (např. při prvním spuštění), inicializujeme ji
        supabase.table("ai_mmr_matrix").insert(default_cell).execute()
        if mmr_cache is not None:
            mmr_cache[key] = dict(default_cell)
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

def update_mmr_cell_values(rx: int, ry: int, outcome: float, r_multiple: float, weight: float, vault: dict, mmr_cache: dict | None = None, exit_reason: str = ""):
    """
    Matematické jádro učení buňky – responzívne 24/7 EMA s rozlíšením exit_reason.
    base_alpha je konštantné (učenie nikdy nezamrzne), reason_mult upravuje intenzitu.
    """
    cell = get_mmr_cell(rx, ry, mmr_cache=mmr_cache)
    trades_n = int(cell.get("total_trades", 0))
    reason_u = (exit_reason or "").upper()

    # Konštantná rýchlosť učenia (nikdy nezamrzne pri vysokom total_trades)
    base_alpha = 0.12
    alpha = float(base_alpha * weight)  # 0.12 cieľ / 0.024 sused (≈0.025)

    if outcome == 0.0:  # LOSS
        if "LIQUIDATION" in reason_u:
            reason_mult = 1.50
            sl_delta = +0.05 * weight
            tp_delta = -0.02 * weight
        elif "VPIN" in reason_u or "ORDERFLOW" in reason_u:
            reason_mult = 0.45
            sl_delta = +0.015 * weight
            tp_delta = 0.0
        elif "TIME_STOP" in reason_u:
            reason_mult = 0.50
            sl_delta = +0.01 * weight
            tp_delta = -0.015 * weight
        else:
            reason_mult = 1.00
            sl_delta = +0.035 * weight
            tp_delta = -0.015 * weight
        eff_alpha = float(np.clip(alpha * reason_mult, 0.0, 0.25))
        cell["win_rate"] = float(np.clip((cell["win_rate"] * (1.0 - eff_alpha)) + (0.0 * eff_alpha), 25.0, 85.0))
        cell["avg_loss_r"] = float((cell["avg_loss_r"] * (1.0 - alpha)) + (abs(r_multiple) * alpha))
        cell["sl_modifier"] = float(np.clip(cell["sl_modifier"] * (1.0 + sl_delta), 0.70, 1.50))
        cell["tp_modifier"] = float(np.clip(cell["tp_modifier"] * (1.0 + tp_delta), 0.70, 1.50))
    else:  # WIN (outcome == 100.0)
        if "CLIMAX" in reason_u or "TP2" in reason_u or "TRAILING" in reason_u:
            reason_mult = 1.25
            sl_delta = -0.02 * weight
            tp_delta = +0.025 * weight
        else:
            reason_mult = 0.80
            sl_delta = -0.01 * weight
            tp_delta = +0.01 * weight
        eff_alpha = float(np.clip(alpha * reason_mult, 0.0, 0.25))
        cell["win_rate"] = float(np.clip((cell["win_rate"] * (1.0 - eff_alpha)) + (100.0 * eff_alpha), 25.0, 85.0))
        cell["avg_win_r"] = float((cell["avg_win_r"] * (1.0 - alpha)) + (abs(r_multiple) * alpha))
        cell["sl_modifier"] = float(np.clip(cell["sl_modifier"] * (1.0 + sl_delta), 0.70, 1.50))
        cell["tp_modifier"] = float(np.clip(cell["tp_modifier"] * (1.0 + tp_delta), 0.70, 1.50))

    # Počet obchodů zvyšujeme pouze v cílové buňce, kde reálně k exekuci došlo
    if weight == 1.0:
        cell["total_trades"] = trades_n + 1

    cell["updated_at"] = datetime.now(timezone.utc).isoformat()
    safe_supabase_upsert("ai_mmr_matrix", cell, vault)
    if mmr_cache is not None:
        mmr_cache[(int(rx), int(ry))] = dict(cell)

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
    mmr_cache: dict | None = None,
    exit_reason: str = "",
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
    update_mmr_cell_values(rx, ry, outcome, r_multiple, 1.0, vault, mmr_cache=mmr_cache, exit_reason=exit_reason)
    
    # 2. PROSTOROVÉ PŘELÉVÁNÍ INFORMACÍ (KNN Spatial Smoothing - Sousední buňky = 20 %)
    neighbors = get_mmr_neighbors(rx, ry)
    for nx, ny in neighbors:
        update_mmr_cell_values(nx, ny, outcome, r_multiple, 0.20, vault, mmr_cache=mmr_cache, exit_reason=exit_reason)


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
    VPIN Engine (Toxický štít) – Bulk Volume Classification (BVC).
    Rozdeľuje barový objem podľa CDF normalizovaných log-výnosov (math.erf).
    Vrací hodnotu od 0.0 (bez toxicity) do 1.0 (extrémní riziko flash crashe).
    volume_bucket_size zachované pre spätnú kompatibilitu podpisu (BVC ho nepoužíva).
    """
    if len(klines_15m) < 10:
        return 0.5

    closes = np.array([float(get_close(k)) for k in klines_15m], dtype=float)
    vols = np.array([float(get_vol(k)) for k in klines_15m], dtype=float)

    log_rets = np.diff(np.log(np.maximum(closes, 1e-12)))
    bar_vols = vols[1:]

    sigma = float(np.std(log_rets))
    # Poistka proti nulovej volatilite
    if sigma <= 1e-12 or np.isnan(sigma):
        return 0.25

    z = log_rets / sigma
    phi = np.array(
        [0.5 * (1.0 + math.erf(float(zi) / math.sqrt(2.0))) for zi in z],
        dtype=float,
    )

    buy_vols = bar_vols * phi
    sell_vols = bar_vols * (1.0 - phi)

    total_vol = float(np.sum(bar_vols))
    if total_vol <= 1e-9:
        return 0.0

    return float(np.sum(np.abs(buy_vols - sell_vols)) / total_vol)


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
    2-vrstvový filter: Hard Safety Gates + Soft Scoring multipliers.
    Hard gates return False; soft factors only scale soft_mult into filter_ctx.
    """
    if len(klines_15m) < 12:
        return False, 0.0

    soft_mult = 1.0

    # --- Soft: 15m volume impulse ---
    vols = [float(get_vol(k)) for k in klines_15m[-11:]]
    avg_prev_vol = float(np.mean(vols[:-1])) if len(vols) > 1 else 0.0
    curr_vol = vols[-1]
    if avg_prev_vol > 0 and curr_vol >= avg_prev_vol * 1.10:
        soft_mult *= 1.20
    else:
        soft_mult *= 0.90

    # --- Soft: 1H EMA21 alignment ---
    klines_1h = await fetch_okx_klines(client, symbol, "1H", 25, cache)
    if len(klines_1h) >= 21:
        closes_1h = pd.Series([float(get_close(k)) for k in klines_1h])
        ema21_1h = float(closes_1h.ewm(span=21, adjust=False).mean().iloc[-1])
        last_1h_close = float(closes_1h.iloc[-1])
        aligned = (
            (direction == "LONG" and last_1h_close > ema21_1h)
            or (direction == "SHORT" and last_1h_close < ema21_1h)
        )
        if aligned:
            soft_mult *= 1.25
        else:
            soft_mult *= 0.85

    # --- Hard + Soft microstructure ---
    try:
        micro = await fetch_hloubkova_mikrostruktura(client, symbol, klines_15m=klines_15m, cache=cache)
        if not micro.get("ok"):
            return False, 0.0

        best_ask = float(micro["best_ask"])
        best_bid = float(micro["best_bid"])
        spread_pct = float(micro["spread_pct"])
        # Hard gate: spread
        max_spread = 0.32 if is_pd else 0.22
        if spread_pct > max_spread:
            return False, 0.0

        # Hard gate: BVC VPIN
        vpin_score = calculate_vpin_toxicity(klines_15m)
        vpin_max = float((filter_ctx or {}).get("vpin_max", 0.80))
        if vpin_score > max(0.85, vpin_max):
            return False, 0.0

        # Hard gate: extreme book imbalance only
        wob_imbalance = float(micro.get("wob_imbalance", 0.0))
        if direction == "LONG" and wob_imbalance < -0.35:
            return False, 0.0
        if direction == "SHORT" and wob_imbalance > 0.35:
            return False, 0.0

        # Soft: Wall-to-TP1 (symmetrical – opposing side walls)
        opp_levels = (micro.get("asks") or []) if direction == "LONG" else (micro.get("bids") or [])
        if opp_levels and len(opp_levels) >= 5:
            try:
                sizes = [float(lvl[1]) for lvl in opp_levels[:15] if float(lvl[1]) > 0]
                avg_sz = float(np.mean(sizes)) if sizes else 0.0
                mid_px = (best_ask + best_bid) / 2.0 if (best_ask > 0 and best_bid > 0) else 0.0
                wall_near = False
                if avg_sz > 0 and mid_px > 0:
                    for lvl in opp_levels[:15]:
                        px = float(lvl[0])
                        sz = float(lvl[1])
                        dist_pct = abs(px - mid_px) / mid_px * 100.0
                        if sz > 4.5 * avg_sz and dist_pct <= 1.2:
                            wall_near = True
                            break
                if wall_near:
                    soft_mult *= 0.70
                else:
                    soft_mult *= 1.10
            except Exception:
                soft_mult *= 1.0

        # SYSTEM C – Depth-20 VWAP estimate for entry
        levels = (micro.get("asks") or []) if direction == "LONG" else (micro.get("bids") or [])
        fallback = best_ask if direction == "LONG" else best_bid
        exec_price = simulate_depth20_vwap(levels, notional_usd=50.0, fallback_px=fallback)
        if filter_ctx is not None:
            filter_ctx["last_wob"] = float(wob_imbalance)
            filter_ctx["last_vpin"] = float(vpin_score)
            filter_ctx["book_levels"] = levels
            filter_ctx["soft_mult"] = float(soft_mult)
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
        if direction == "LONG" and last_1h_close < ema21_1h and ofd < 1.10:
            return False, 0.0
        if direction == "SHORT" and last_1h_close > ema21_1h and ofd > 0.90:
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
        if vpin_score > 0.85:
            return False, 0.0

        wob_imbalance = float(micro.get("wob_imbalance", 0.0))
        if direction == "LONG" and wob_imbalance < -0.30:
            return False, 0.0
        if direction == "SHORT" and wob_imbalance > 0.30:
            return False, 0.0

        levels = (micro.get("asks") or []) if direction == "LONG" else (micro.get("bids") or [])
        fallback = best_ask if direction == "LONG" else best_bid
        exec_price = simulate_depth20_vwap(levels, notional_usd=100.0, fallback_px=fallback)
        return True, exec_price
    except Exception:
        return False, 0.0



async def check_1m_sniper_trigger(
    client: httpx.AsyncClient,
    symbol: str,
    direction: str,
    natr: float,
    cache: dict | None = None,
    age_sec: float = 0.0,
) -> tuple[bool, float, bool]:
    """
    Symetrický 1m Sniper: MM Stop-Run Reclaim, Bull/Bear Trap invalidácia,
    Slow-Grind EMA9, Real Tape CVD potvrdenie.
    Returns (triggered, trigger_price, invalidated).
    """
    try:
        # Fresh data – do not rely solely on shared cache for sniper timing
        klines_1m = await fetch_okx_klines(client, symbol, "1m", 30, None)
        klines_15m = await fetch_okx_klines(client, symbol, "15m", 22, None)
        if not klines_1m or len(klines_1m) < 15:
            return False, 0.0, False

        # 30m Rolling VWAP
        tp_sum = 0.0
        vol_sum = 0.0
        tp_list = []
        closes_1m = []
        for k in klines_1m:
            h = float(get_high(k))
            l = float(get_low(k))
            c = float(get_close(k))
            v = float(get_vol(k))
            tp = (h + l + c) / 3.0
            tp_list.append(tp)
            closes_1m.append(c)
            tp_sum += tp * v
            vol_sum += v
        if vol_sum <= 1e-9:
            vwap_30m = float(np.mean(tp_list)) if tp_list else 0.0
        else:
            vwap_30m = tp_sum / vol_sum
        if vwap_30m <= 0:
            return False, 0.0, False

        # EMA9 on 1m closes
        s_close = pd.Series(closes_1m, dtype=float)
        ema9_1m = float(s_close.ewm(span=9, adjust=False).mean().iloc[-1])

        # Avg volume last 5 completed 1m bars (exclude current forming if possible)
        vols_1m = [float(get_vol(k)) for k in klines_1m]
        avg_vol_5m = float(np.mean(vols_1m[-6:-1])) if len(vols_1m) >= 6 else float(np.mean(vols_1m[:-1] or vols_1m))
        curr_vol = float(get_vol(klines_1m[-1]))

        # EMA20 on 15m
        ema20_15m = None
        if klines_15m and len(klines_15m) >= 20:
            c15 = pd.Series([float(get_close(k)) for k in klines_15m], dtype=float)
            ema20_15m = float(c15.ewm(span=20, adjust=False).mean().iloc[-1])

        prev_bar = klines_1m[-2]
        curr_bar = klines_1m[-1]
        prev_h = float(get_high(prev_bar))
        prev_l = float(get_low(prev_bar))
        curr_o = float(get_open(curr_bar))
        curr_c = float(get_close(curr_bar))

        max_vwap_dev = max(0.0020, (0.55 * float(natr or 1.5)) / 100.0)

        # --- Immediate symmetrical Bull / Bear Trap invalidation ---
        if direction == "LONG":
            if curr_c < vwap_30m * (1.0 - max_vwap_dev):
                return False, 0.0, True
            if ema20_15m is not None and curr_c <= ema20_15m:
                return False, 0.0, True
        elif direction == "SHORT":
            if curr_c > vwap_30m * (1.0 + max_vwap_dev):
                return False, 0.0, True
            if ema20_15m is not None and curr_c >= ema20_15m:
                return False, 0.0, True

        # --- 3 technical paths ---
        tech_ok = False
        # Path A: VWAP Pullback + Micro-Break
        if direction == "LONG":
            if (
                curr_c <= vwap_30m * (1.0 + max_vwap_dev)
                and curr_c > curr_o
                and curr_c > prev_h
                and curr_vol >= avg_vol_5m * 0.90
            ):
                tech_ok = True
        else:
            if (
                curr_c >= vwap_30m * (1.0 - max_vwap_dev)
                and curr_c < curr_o
                and curr_c < prev_l
                and curr_vol >= avg_vol_5m * 0.90
            ):
                tech_ok = True

        # Path B: MM Stop-Run Reclaim
        if not tech_ok:
            recent_5 = klines_1m[-6:-1] if len(klines_1m) >= 6 else klines_1m[:-1]
            if direction == "LONG":
                dipped = any(float(get_low(k)) < vwap_30m for k in recent_5)
                if (
                    dipped
                    and curr_c >= vwap_30m
                    and curr_c > prev_h
                    and curr_vol >= avg_vol_5m * 1.15
                ):
                    tech_ok = True
            else:
                spiked = any(float(get_high(k)) > vwap_30m for k in recent_5)
                if (
                    spiked
                    and curr_c <= vwap_30m
                    and curr_c < prev_l
                    and curr_vol >= avg_vol_5m * 1.15
                ):
                    tech_ok = True

        # Path C: Slow-Grind EMA9 Continuation (after age_sec >= 360)
        if not tech_ok and float(age_sec or 0.0) >= 360.0:
            if direction == "LONG":
                if (
                    curr_c >= vwap_30m
                    and curr_c > ema9_1m
                    and curr_c > curr_o
                    and curr_c > prev_h
                ):
                    tech_ok = True
            else:
                if (
                    curr_c <= vwap_30m
                    and curr_c < ema9_1m
                    and curr_c < curr_o
                    and curr_c < prev_l
                ):
                    tech_ok = True

        if not tech_ok:
            return False, 0.0, False

        # --- Real Tape CVD confirmation ---
        tape = await fetch_real_tape_cvd(client, symbol, limit=100)
        if tape.get("ok"):
            tape_ofd = float(tape.get("tape_ofd", 1.0))
            if direction == "LONG" and tape_ofd < 1.12:
                return False, 0.0, False
            if direction == "SHORT" and tape_ofd > 0.88:
                return False, 0.0, False

        return True, curr_c, False
    except Exception:
        return False, 0.0, False



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


def close_position(pos: dict, exit_price: float, reason: str, vault: dict, mmr_cache: dict | None = None):
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
    if int(pos.get("window_id", 0)) != 6:
        if pos.get("v5_features"):
            try:
                feedback = list(vault.get("ai_w6_feedback") or [])
                feedback.append({
                    "source": "W15_REAL",
                    "is_win": bool(is_win),
                    "direction": pos.get("direction"),
                    "r_multiple": round(float(r_mult), 4),
                    "features": dict(pos["v5_features"]),
                    "closed_ts": time.time(),
                })
                vault["ai_w6_feedback"] = feedback[-600:]
            except Exception:
                pass
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
                mmr_cache=mmr_cache,
                exit_reason=reason,
            )
        except Exception:
            pass
        try:
            update_filter_weights_from_outcome(pos, is_win, reason, vault)
        except Exception:
            pass
    else:
        # Okno 6: reálna exekúcia je najvyššie vážený feedback zdroj.
        feedback_features = pos.get("v5_features") or pos.get("ai_features")
        if feedback_features:
            try:
                fb_list = list(vault.get("ai_w6_feedback") or [])
                fb_list.append({
                    "source": "W6_REAL",
                    "symbol": pos.get("symbol"),
                    "direction": pos.get("direction"),
                    "is_win": bool(is_win),
                    "r_multiple": round(float(r_mult), 4),
                    "exit_reason": str(reason),
                    "ai_confidence": float(pos.get("ai_confidence", 0.0)),
                    "features": dict(feedback_features),
                    "closed_ts": time.time(),
                })
                vault["ai_w6_feedback"] = fb_list[-600:]
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
        "closed_at": now_dt.isoformat(),
        # DCA / TP execution flags (pre cockpit breakdown)
        "dca1_filled": bool(pos.get("dca1_filled", False)),
        "dca2_filled": bool(pos.get("dca2_filled", False)),
        "dca3_filled": bool(pos.get("dca3_filled", False)),
        "tp1_hit": bool(pos.get("tp1_hit", False)),
        "tp2_hit": bool(pos.get("tp2_hit", False)),
        "dca1_price": float(pos.get("dca1_price") or 0.0),
        "dca2_price": float(pos.get("dca2_price") or 0.0),
        "dca3_price": float(pos.get("dca3_price") or 0.0),
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
        "closed_at": now_dt.isoformat(),
        "dca1_filled": bool(pos.get("dca1_filled", False)),
        "dca2_filled": bool(pos.get("dca2_filled", False)),
        "dca3_filled": bool(pos.get("dca3_filled", False)),
        "tp1_hit": bool(pos.get("tp1_hit", False)),
        "tp2_hit": bool(pos.get("tp2_hit", False)),
        "dca1_price": float(pos.get("dca1_price") or 0.0),
        "dca2_price": float(pos.get("dca2_price") or 0.0),
        "dca3_price": float(pos.get("dca3_price") or 0.0),
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
STALE_POSITION_MAX_SEC = 3600  # 60 minút – krátky reštart obnoví obchody, dlhý výpadok (>1h) ich čisto zmaže


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
    vault["pending_setups"] = {}
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

    pending_setups = vault.setdefault("pending_setups", {})
    for _pk in list(pending_setups.keys()):
        _pending = pending_setups.get(_pk) or {}
        _queued_ts = float(_pending.get("created_at", now_ts) or now_ts)
        _pending_natr = float(_pending.get("natr", 1.5) or 1.5)
        _ttl_default = compute_theta_timers(_pending_natr)[0]
        _ttl_limit = float(_pending.get("sniper_ttl_sec") or _ttl_default)
        if (now_ts - _queued_ts) > _ttl_limit:
            pending_setups.pop(_pk, None)

    active_windows = [
        (win_key, pos) for win_key, pos in vault["windows"].items()
        if pos["status"] == "ACTIVE" and pos.get("symbol")
    ]
    last_main_scan = vault.get("last_main_scan", 0)
    is_scan_due = (now_ts - last_main_scan) >= 180

    # ----- IDLE PATH: žiadne ACTIVE okná, žiadne pending, scan nie je due -----
    if not active_windows and not pending_setups and not is_scan_due:
        save_vault_local(vault)
        last_sync = float(vault.get("last_db_sync", 0) or 0)
        has_pending = bool(vault.get("pending_supabase_sync"))
        if (now_ts - last_sync) >= 60 or (has_pending and (now_ts - last_sync) >= 10):
            flush_pending_sync(vault)
            save_vault(vault, sync_cloud=True)
        return

    # ----- ACTIVE / SCAN PATH: zdieľaný client + mmr_cache -----
    async with httpx.AsyncClient(http2=True, timeout=8.0) as client:
        mmr_cache = load_mmr_cache()
        klines_cache: dict = {}
        tick_micro_cache: dict = {}  # per-symbol mid-freq cache (klines / book / vpin / ofd)

        # ===== 1m Sniper: vyhodnotenie pending_setups =====
        pending_setups = vault.setdefault("pending_setups", {})
        if pending_setups and not ny_lock:
            for w_idx, item in list(pending_setups.items()):
                try:
                    queued_ts = float((item or {}).get("created_at", now_ts) or now_ts)
                    item_natr = float((item or {}).get("natr", 1.5) or 1.5)
                    ttl_defaults = compute_theta_timers(item_natr)
                    ttl_limit = float((item or {}).get("sniper_ttl_sec") or ttl_defaults[0])
                    if (now_ts - queued_ts) > ttl_limit:
                        pending_setups.pop(w_idx, None)
                        continue
                    win_state = (vault.get("windows") or {}).get(w_idx) or {}
                    if win_state.get("status") != "WAITING":
                        pending_setups.pop(w_idx, None)
                        continue
                    cand = (item or {}).get("cand") or {}
                    if not cand.get("symbol") or not cand.get("direction"):
                        pending_setups.pop(w_idx, None)
                        continue
                    age_sec = now_ts - float(item.get("created_at", now_ts) or now_ts)
                    ok_trig, trig_px, invalidated = await check_1m_sniper_trigger(
                        client,
                        cand["symbol"],
                        cand["direction"],
                        float(item.get("natr", 2.0) or 2.0),
                        cache=klines_cache,
                        age_sec=age_sec,
                    )
                    if invalidated:
                        pending_setups.pop(w_idx, None)
                        continue
                    if not ok_trig:
                        continue
                    cand = dict(cand)
                    cand["exec_price"] = float(trig_px)
                    setup = await build_trade_setup(
                        cand, vault["balance"], int(w_idx), vault,
                        client=client, mmr_cache=mmr_cache,
                    )
                    if setup:
                        entry_natr = float(setup.get("natr") or item_natr)
                        setup["max_hold_sec"] = compute_theta_timers(entry_natr)[1]
                        vault["windows"][w_idx] = setup
                        vault["last_trade_opened_ts"] = time.time()
                        mkt_notional = float(setup.get("active_margin") or 0.0) * float(setup.get("leverage") or 1)
                        vault["balance"] = round(float(vault["balance"]) - mkt_notional * 0.0005, 4)
                        push_active_position_row(setup, vault)
                    else:
                        record_shadow_trade(
                            vault,
                            {**cand, "natr": float(item.get("natr", 2.0) or 2.0)},
                            failed_filter=cand.get("_failed_filter", "FILTER_6_RISK_MMR"),
                        )
                    pending_setups.pop(w_idx, None)
                except Exception:
                    pass
            # Refresh active list po prípadnom otvorení z čakárne
            active_windows = [
                (win_key, pos) for win_key, pos in vault["windows"].items()
                if pos.get("status") == "ACTIVE" and pos.get("symbol")
            ]

        # ===== 1-sekundová Dual-Speed slučka pre ACTIVE pozície (max 15 tikov) =====
        if active_windows:
            for tick in range(15):
                tick_ts = time.time()
                tick_dt = datetime.now(timezone.utc)
                vault["last_heartbeat"] = tick_ts

                # Refresh active list (pozícia sa mohla uzavrieť v predchádzajúcom tiku)
                active_windows = [
                    (win_key, pos) for win_key, pos in vault["windows"].items()
                    if pos.get("status") == "ACTIVE" and pos.get("symbol")
                ]
                if not active_windows:
                    break

                active_syms_list = [pos["symbol"] for _, pos in active_windows]
                active_tickers = await fetch_active_tickers_only(client, active_syms_list) if active_syms_list else {}
                position_closed_in_tick = False

                # --- Stredná frekvencia: sviečky + mikroštruktúra iba na tick == 0 ---
                if tick == 0:
                    tick_micro_cache = {}
                    for win_key, pos in active_windows:
                        sym = pos["symbol"]
                        if sym in tick_micro_cache:
                            continue
                        klines = await fetch_okx_klines(client, sym, "15m", 15, klines_cache)
                        vpin_score = 0.0
                        ofd = float(pos.get("cached_ofd", 1.0))
                        if klines:
                            ofd = compute_ofd_from_klines(klines)
                            pos["cached_ofd"] = ofd
                            vpin_score = calculate_vpin_toxicity(klines)
                        tick_micro_cache[sym] = {
                            "klines": klines,
                            "vpin_score": vpin_score,
                            "ofd": ofd,
                        }

                # 1s Order Book radar – výhradne pre unikátne symboly aktívnych pozícií
                unique_syms = list({pos["symbol"] for _, pos in active_windows if pos.get("symbol")})
                book_by_sym: dict = {}
                if unique_syms:
                    async def _fetch_ob_1s(sym: str):
                        kl = (tick_micro_cache.get(sym) or {}).get("klines") or []
                        try:
                            micro = await fetch_hloubkova_mikrostruktura(
                                client, sym, klines_15m=kl, cache=klines_cache
                            )
                            if micro.get("ok"):
                                return sym, {
                                    "bids": micro.get("bids") or [],
                                    "asks": micro.get("asks") or [],
                                    "wob": float(micro.get("wob_imbalance", 0.0)),
                                }
                        except Exception:
                            pass
                        return sym, {"bids": [], "asks": [], "wob": 0.0}

                    ob_results = await asyncio.gather(*(_fetch_ob_1s(s) for s in unique_syms))
                    for sym, pack in ob_results:
                        book_by_sym[sym] = pack

                # --- Rýchla frekvencia: kompletná správa pozície každú 1 s ---
                for win_key, pos in list(active_windows):
                    pos_changed_in_tick = False  # zmena stavu (SL / DCA / TP1 / TP2) → okamžitý zápis do Supabase
                    # Mohla sa medzitým uzavrieť
                    if pos.get("status") != "ACTIVE" or not pos.get("symbol"):
                        continue
                    win_id = int(win_key)
                    sym = pos["symbol"]
                    if sym not in active_tickers:
                        pos["ticker_miss_count"] = int(pos.get("ticker_miss_count", 0)) + 1
                        if pos["ticker_miss_count"] >= 5:
                            single_t = await fetch_single_ticker(client, sym)
                            if single_t is not None:
                                active_tickers[sym] = single_t
                            else:
                                pos["last_ticker_error"] = f"Missing ticker ({pos['ticker_miss_count']}x)"
                                continue
                        else:
                            continue

                    pos["ticker_miss_count"] = 0
                    pos["last_ticker_error"] = ""
                    curr_price = float(active_tickers[sym]["last"])
                    is_long = pos["direction"] == "LONG"
                    dir_mult = 1.0 if is_long else -1.0
                    natr = pos["natr"]
                    rx = int(pos.get("regime_x", 2))
                    ry = int(pos.get("regime_y", 2))

                    micro_pack = tick_micro_cache.get(sym) or {}
                    klines = micro_pack.get("klines") or []
                    ob_pack = book_by_sym.get(sym) or {}
                    book_data = {"bids": ob_pack.get("bids") or [], "asks": ob_pack.get("asks") or []}
                    curr_wob = float(ob_pack.get("wob", 0.0))
                    vpin_score = float(micro_pack.get("vpin_score") or 0.0)
                    ofd = float(micro_pack.get("ofd") if micro_pack.get("ofd") is not None else pos.get("cached_ofd", 1.0))
                    mw = vault.get("micro_weights") or {
                        "spoof_sensitivity": 1.0,
                        "wall_frontrun_mult": 3.5,
                        "dca_wob_floor": 0.10,
                    }

                    # SYSTEM D – intrabar wick levels z aktuálnej 15m sviečky
                    # DÔLEŽITÉ: wick pred opened_at sa NESMIE použiť na Hard SL / Liquidation
                    candle_high = curr_price
                    candle_low = curr_price
                    if klines and len(klines) > 0:
                        try:
                            candle_high = float(get_high(klines[-1]))
                            candle_low = float(get_low(klines[-1]))
                        except Exception:
                            pass

                    # Čas otvorenia pozície (pre filtrovanie pre-entry wickov)
                    opened_ts = 0.0
                    try:
                        oa = pos.get("opened_at")
                        if oa:
                            opened_ts = datetime.fromisoformat(str(oa).replace("Z", "+00:00")).timestamp()
                    except Exception:
                        opened_ts = 0.0
                    age_sec = (tick_ts - opened_ts) if opened_ts > 0 else 9999.0

                    # Pre-entry wick guard:
                    # Ak sme v pozícii kratšie než 1×15m bar (900s), candle_low/high môžu obsahovať
                    # pohyb PRED vstupom → pre Hard SL a Liquidation použiť len curr_price.
                    use_wick_for_exit = age_sec >= 900.0
                    eff_low = candle_low if use_wick_for_exit else curr_price
                    eff_high = candle_high if use_wick_for_exit else curr_price

                    # SYSTEM D – FUTURES liquidation price (90% of margin at leverage)
                    if pos.get("regime") == "FUTURES" and int(pos.get("leverage") or 1) > 1:
                        lev = max(1, int(pos.get("leverage", 1)))
                        entry_px = float(pos.get("entry_price") or curr_price)
                        liq_price = entry_px * (1.0 - dir_mult * (0.90 / lev))
                        hit_liq = (
                            (is_long and (curr_price <= liq_price or eff_low <= liq_price))
                            or (not is_long and (curr_price >= liq_price or eff_high >= liq_price))
                        )
                        if hit_liq:
                            pos["_force_full_margin_loss"] = True
                            close_position(pos, liq_price, "LIQUIDATION_HIT", vault, mmr_cache=mmr_cache)
                            db_dirty = True
                            position_closed_in_tick = True
                            continue

                    # SYSTEM D – Funding settlement at 00/08/16 UTC
                    if tick_dt.hour in (0, 8, 16) and int(pos.get("last_funding_hour", -1)) != tick_dt.hour:
                        try:
                            funding_rate = await fetch_okx_funding(client, sym)
                            # LONG pays when funding > 0; SHORT receives (sign via dir_mult)
                            notional = float(pos.get("active_margin") or 0.0) * float(pos.get("leverage") or 1)
                            funding_fee_usd = notional * float(funding_rate) * dir_mult
                            vault["balance"] = round(float(vault.get("balance", 0.0)) - funding_fee_usd, 4)
                            pos["realized_pnl_usd"] = float(pos.get("realized_pnl_usd", 0.0)) - funding_fee_usd
                            pos["last_funding_hour"] = tick_dt.hour
                            db_dirty = True
                        except Exception:
                            pass

                    # A. VPIN EMERGENCY ESCAPE – len ak je pozícia už v strate (LONG aj SHORT)
                    # Nevyhadzuje ziskové / break-even pozície pri toxickom flow
                    if vpin_score > 0.85:
                        entry_px = float(pos.get("entry_price") or curr_price)
                        in_loss = (
                            (is_long and curr_price < entry_px * 0.998)
                            or ((not is_long) and curr_price > entry_px * 1.002)
                        )
                        if in_loss:
                            close_position(pos, curr_price, "VPIN_EMERGENCY_ESCAPE", vault, mmr_cache=mmr_cache)
                            db_dirty = True
                            position_closed_in_tick = True
                            continue

                    # A2. SPOOFING / LIQUIDITY PULL ESCAPE – vyhladený 3s EMA trend knihy + Real Tape CVD
                    prev_wob_ema = float(pos.get("wob_ema", curr_wob))
                    wob_ema = 0.65 * prev_wob_ema + 0.35 * float(curr_wob)
                    pos["wob_ema"] = wob_ema
                    pos["last_wob_imbalance"] = float(curr_wob)
                    spoof_drop_thr = 0.65 / max(0.5, float(mw.get("spoof_sensitivity", 1.0)))
                    spoof_suspect = (
                        (is_long and (prev_wob_ema - curr_wob) > spoof_drop_thr and curr_wob < -0.25)
                        or ((not is_long) and (curr_wob - prev_wob_ema) > spoof_drop_thr and curr_wob > 0.25)
                    )
                    if spoof_suspect:
                        entry_px = float(pos.get("entry_price") or curr_price)
                        loss_pct = abs(curr_price - entry_px) / max(1e-9, entry_px) * 100.0
                        min_spoof_loss_pct = max(0.35, 0.35 * float(natr or 1.5))
                        in_loss_spoof = (
                            ((is_long and curr_price < entry_px) or ((not is_long) and curr_price > entry_px))
                            and loss_pct > min_spoof_loss_pct
                        )
                        if in_loss_spoof:
                            try:
                                tape = await fetch_real_tape_cvd(client, sym, limit=100)
                            except Exception:
                                tape = {"ok": False}
                            real_flow = float(tape["tape_ofd"]) if tape.get("ok") else float(ofd)
                            if (is_long and real_flow < 0.80) or ((not is_long) and real_flow > 1.20):
                                close_position(pos, curr_price, "SPOOF_COLLAPSE_ESCAPE", vault, mmr_cache=mmr_cache)
                                db_dirty = True
                                position_closed_in_tick = True
                                continue

                    # B. SHADOW OBI BARRIER (PAPER) – LONG aj SHORT
                    # SL smie posunúť len smerom k zisku a max na 40 % vzdialenosti k aktuálnej cene
                    # (zabraňuje utiahnutiu SL tesne pod/nad cenu → okamžitý wick-out)
                    if book_data and not pos.get("tp1_hit", False):
                        bids = book_data.get("bids", [])
                        asks = book_data.get("asks", [])
                        cur_sl = float(pos.get("sl_tsl_price") or 0.0)
                        entry_px = float(pos.get("entry_price") or curr_price)

                        if is_long and bids and cur_sl > 0:
                            best_wall_px = float(bids[0][0])
                            max_wall_vol = 0.0
                            for b in bids[:20]:
                                if float(b[1]) > max_wall_vol:
                                    max_wall_vol = float(b[1])
                                    best_wall_px = float(b[0])
                            shadow_sl = best_wall_px - 0.0001
                            # max povolený SL = 40 % cesty od pôvodného SL k cene
                            max_allowed_sl = cur_sl + 0.40 * (curr_price - cur_sl)
                            # Hierarchia DCA2 > Shadow OBI > Hard SL: nikdy nad nevyplnenú DCA úroveň
                            if not pos.get("dca2_filled") and float(pos.get("dca2_price") or 0.0) > 0:
                                dca_floor_px = float(pos["dca2_price"]) * 0.998
                            elif not pos.get("dca1_filled") and float(pos.get("dca1_price") or 0.0) > 0:
                                dca_floor_px = float(pos["dca1_price"]) * 0.998
                            else:
                                dca_floor_px = entry_px
                            if (shadow_sl > cur_sl and shadow_sl < curr_price
                                    and shadow_sl <= max_allowed_sl
                                    and shadow_sl < entry_px  # SL musí ostať pod entry pri LONGu
                                    and shadow_sl < dca_floor_px):
                                paper_amend_sl(pos, shadow_sl, "SHADOW_OBI_LONG")
                                pos_changed_in_tick = True
                                db_dirty = True

                        elif (not is_long) and asks and cur_sl > 0:
                            best_wall_px = float(asks[0][0])
                            max_wall_vol = 0.0
                            for a in asks[:20]:
                                if float(a[1]) > max_wall_vol:
                                    max_wall_vol = float(a[1])
                                    best_wall_px = float(a[0])
                            shadow_sl = best_wall_px + 0.0001
                            max_allowed_sl = cur_sl - 0.40 * (cur_sl - curr_price)
                            # Hierarchia DCA2 > Shadow OBI > Hard SL: nikdy pod nevyplnenú DCA úroveň
                            if not pos.get("dca2_filled") and float(pos.get("dca2_price") or 0.0) > 0:
                                dca_ceil_px = float(pos["dca2_price"]) * 1.002
                            elif not pos.get("dca1_filled") and float(pos.get("dca1_price") or 0.0) > 0:
                                dca_ceil_px = float(pos["dca1_price"]) * 1.002
                            else:
                                dca_ceil_px = entry_px
                            if (shadow_sl < cur_sl and shadow_sl > curr_price
                                    and shadow_sl >= max_allowed_sl
                                    and shadow_sl > entry_px  # SL musí ostať nad entry pri SHORTe
                                    and shadow_sl > dca_ceil_px):
                                paper_amend_sl(pos, shadow_sl, "SHADOW_OBI_SHORT")
                                pos_changed_in_tick = True
                                db_dirty = True

                    # C. RISK-CAPPED DCA (max 2 úrovne) s OFD + WOB + Real Tape CVD
                    if not pos["is_pd"]:
                        cost_rate = pos.get("cost_rate", compute_roundtrip_cost_rate(win_id))
                        tp_mod = pos.get("tp_mod", 1.0)
                        for idx, pct in ((1, 0.25), (2, 0.35)):
                            if pos.get(f"dca{idx}_filled"):
                                continue
                            dca_p = float(pos.get(f"dca{idx}_price") or 0.0)
                            if dca_p <= 0:
                                continue
                            if not ((is_long and curr_price <= dca_p) or (not is_long and curr_price >= dca_p)):
                                continue
                            # OFD filter spomalenia tlaku – nerieď v extrémnom protitlaku
                            if (is_long and ofd < 0.70) or (not is_long and ofd > 1.30):
                                continue
                            # Pasívna opora v 1s knihe (DCA absorption floor)
                            wob_floor = float(mw.get("dca_wob_floor", 0.10))
                            if (is_long and curr_wob < wob_floor) or ((not is_long) and curr_wob > -wob_floor):
                                continue
                            # Real Tape CVD – výplach sa už musí zastaviť
                            try:
                                tape_dca = await fetch_real_tape_cvd(client, sym, limit=100)
                            except Exception:
                                tape_dca = {"ok": False}
                            real_flow_dca = float(tape_dca["tape_ofd"]) if tape_dca.get("ok") else float(ofd)
                            if (is_long and real_flow_dca < 0.78) or ((not is_long) and real_flow_dca > 1.22):
                                continue

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
                            pos_changed_in_tick = True
                            db_dirty = True

                            # Dynamický SL: 1.6 × NATR od novej priemernej ceny
                            new_sl_pct = max(0.010, (1.6 * float(natr)) / 100.0)
                            new_sl = pos["entry_price"] * (1.0 - dir_mult * new_sl_pct)
                            # Likvidačná poistka
                            if pos.get("regime") == "FUTURES" and int(pos.get("leverage") or 1) > 1:
                                lev = max(1, int(pos.get("leverage", 1)))
                                new_liq = pos["entry_price"] * (1.0 - dir_mult * (0.90 / lev))
                                safe_buffer = max(0.003, (0.4 * float(natr)) / 100.0)
                                if is_long:
                                    if new_sl <= new_liq * (1.0 + safe_buffer):
                                        new_sl = new_liq * (1.0 + safe_buffer)
                                else:
                                    if new_sl >= new_liq * (1.0 - safe_buffer):
                                        new_sl = new_liq * (1.0 - safe_buffer)
                            paper_amend_sl(pos, new_sl, f"RISK_CAPPED_DCA{idx}")
                            pos_changed_in_tick = True

                            pos["tp1_price"] = pos["entry_price"] * (1.0 + dir_mult * ((1.0 * natr * tp_mod) / 100.0 + cost_rate))
                            pos["tp2_price"] = pos["entry_price"] * (1.0 + dir_mult * ((1.8 * natr * tp_mod) / 100.0 + cost_rate))

                    # D. FÁZOVANÝ SCALE-OUT (TP1 & TP2) S REŽIMOVÝM TSL + WALL FRONT-RUN
                    # ZMENA 1+3: same-bar + širší BE + 5min BE freeze / trail delay + voľnejší post-TP1 trail
                    _tp1_just_hit = False
                    frontrun_mult = float(mw.get("wall_frontrun_mult", 3.5))

                    def _has_massive_wall(book: dict, is_long_side: bool, px_lo: float, px_hi: float, mult: float) -> bool:
                        levels = (book.get("asks") or []) if is_long_side else (book.get("bids") or [])
                        if not levels:
                            return False
                        try:
                            sizes = [float(lvl[1]) for lvl in levels[:20] if float(lvl[1]) > 0]
                            if not sizes:
                                return False
                            avg_sz = float(sum(sizes) / len(sizes))
                            lo, hi = (min(px_lo, px_hi), max(px_lo, px_hi))
                            for lvl in levels[:20]:
                                px = float(lvl[0])
                                sz = float(lvl[1])
                                if lo <= px <= hi and sz > mult * avg_sz:
                                    return True
                        except Exception:
                            return False
                        return False

                    if not pos.get("tp1_hit", False):
                        entry_px = float(pos.get("entry_price") or curr_price)
                        tp1_px = float(pos.get("tp1_price") or 0.0)
                        # Wall Front-Run pred TP1 (≥ 85 % cesty k TP1)
                        if tp1_px > 0:
                            path_tp1 = abs(tp1_px - entry_px)
                            prog_tp1 = abs(curr_price - entry_px)
                            if path_tp1 > 1e-12 and (prog_tp1 / path_tp1) >= 0.85:
                                flow_weak = (is_long and ofd < 1.10) or ((not is_long) and ofd > 0.90)
                                if flow_weak and _has_massive_wall(book_data, is_long, curr_price, tp1_px, frontrun_mult):
                                    execute_scale_out(pos, curr_price, 0.50, "TP1_WALL_FRONTRUN", vault, tick_dt)
                                    pos["wall_frontrun_used"] = True
                                    pos["tp1_hit"] = True
                                    pos_changed_in_tick = True
                                    pos["tsl_active"] = True
                                    pos["extreme_price"] = curr_price
                                    pos["tp1_hit_ts"] = float(tick_ts)
                                    _tp1_just_hit = True
                                    cost_rate = pos.get("cost_rate", compute_roundtrip_cost_rate(win_id))
                                    be_pad = float(cost_rate) + 0.0008
                                    be_lock = pos["entry_price"] * (1.0 + dir_mult * be_pad)
                                    paper_amend_sl(pos, be_lock, "BE_LOCK_AFTER_TP1")
                                    pos_changed_in_tick = True
                                    db_dirty = True

                        if not pos.get("tp1_hit", False):
                            if (is_long and (curr_price >= pos["tp1_price"] or candle_high >= pos["tp1_price"])) or (not is_long and (curr_price <= pos["tp1_price"] or candle_low <= pos["tp1_price"])):
                                # Odprodej 50 % pozice tržním scale-outem
                                execute_scale_out(pos, curr_price, 0.50, "TP1_SCALE_OUT", vault, tick_dt)
                                pos["tp1_hit"] = True
                                pos_changed_in_tick = True
                                pos["tsl_active"] = True
                                pos["extreme_price"] = curr_price
                                pos["tp1_hit_ts"] = float(tick_ts)
                                _tp1_just_hit = True

                                # Čistý Break-Even po TP1: len krytie nákladov (cost_rate + 0.08 %)
                                cost_rate = pos.get("cost_rate", compute_roundtrip_cost_rate(win_id))
                                be_pad = float(cost_rate) + 0.0008
                                be_lock = pos["entry_price"] * (1.0 + dir_mult * be_pad)
                                paper_amend_sl(pos, be_lock, "BE_LOCK_AFTER_TP1")
                                pos_changed_in_tick = True
                                db_dirty = True

                    if pos.get("tp1_hit", False) and not pos.get("tp2_hit", False):
                        # Wall Front-Run pred TP2 (≥ 85 % cesty k TP2)
                        entry_px = float(pos.get("entry_price") or curr_price)
                        tp2_px = float(pos.get("tp2_price") or 0.0)
                        if tp2_px > 0:
                            path_tp2 = abs(tp2_px - entry_px)
                            prog_tp2 = abs(curr_price - entry_px)
                            if path_tp2 > 1e-12 and (prog_tp2 / path_tp2) >= 0.85:
                                flow_weak2 = (is_long and ofd < 1.10) or ((not is_long) and ofd > 0.90)
                                if flow_weak2 and _has_massive_wall(book_data, is_long, curr_price, tp2_px, frontrun_mult):
                                    execute_scale_out(pos, curr_price, 0.60, "TP2_WALL_FRONTRUN", vault, tick_dt)
                                    pos["wall_frontrun_used"] = True
                                    pos["tp2_hit"] = True
                                    pos_changed_in_tick = True
                                    pos["extreme_price"] = curr_price
                                    db_dirty = True
                        if is_long and curr_price > pos["extreme_price"]:
                            pos["extreme_price"] = curr_price
                        elif not is_long and curr_price < pos["extreme_price"]:
                            pos["extreme_price"] = curr_price

                        # ZMENA 3: po TP1 drž BE fixný 5 min (trail ešte neťahaj), potom voľnejší krok
                        # → viac priestoru dobehnúť TP2 namiesto skorého TRAILING_SL_PROFIT
                        tp1_ts_for_trail = float(pos.get("tp1_hit_ts") or 0.0)
                        sec_since_tp1_trail = (float(tick_ts) - tp1_ts_for_trail) if tp1_ts_for_trail > 0 else 1e9
                        post_tp1_trail_delay_sec = 300.0  # 5 minút BE freeze

                        if sec_since_tp1_trail >= post_tp1_trail_delay_sec:
                            # REŽIMOVÝ TSL ENGINE — voľnejšie kroky (Zmena 3B)
                            if ry == 3:    # HIGH_VOL
                                step_pct = (1.10 * natr) / 100.0
                            elif ry == 1:  # LOW_VOL
                                step_pct = (0.90 * natr) / 100.0
                            else:          # MID_VOL
                                step_pct = (1.00 * natr) / 100.0

                            new_tsl = pos["extreme_price"] * (1.0 - dir_mult * step_pct)
                            if (is_long and new_tsl > pos["sl_tsl_price"]) or (not is_long and new_tsl < pos["sl_tsl_price"]):
                                paper_amend_sl(pos, new_tsl, "REGIME_TSL")
                                pos_changed_in_tick = True
                                db_dirty = True
                        # else: BE lock ostáva, trail ešte neaktívny

                        if (is_long and (curr_price >= pos["tp2_price"] or candle_high >= pos["tp2_price"])) or (not is_long and (curr_price <= pos["tp2_price"] or candle_low <= pos["tp2_price"])):
                            execute_scale_out(pos, curr_price, 0.60, "TP2_SCALE_OUT", vault, tick_dt)
                            pos["tp2_hit"] = True
                            pos_changed_in_tick = True
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
                                pos["tp3_zone_entered_at"] = tick_ts

                            time_in_zone = tick_ts - float(pos.get("tp3_zone_entered_at") or tick_ts)
                            min_time_in_zone = 3.0  # minimálne 45 sekúnd v radarovej zóne pred climax exitom

                            # Detekce likvidačního šílenství – čerstvý Real Tape CVD (nie staré sviečkové OFD)
                            try:
                                tape_tp3 = await fetch_real_tape_cvd(client, sym, limit=100)
                            except Exception:
                                tape_tp3 = {"ok": False}
                            live_flow_tp3 = float(tape_tp3["tape_ofd"]) if tape_tp3.get("ok") else float(ofd)
                            liquidation_climax = (is_long and live_flow_tp3 > 1.55) or (not is_long and live_flow_tp3 < 0.45)

                            hard_tp3_hit = (is_long and curr_price >= pos["tp3_price"]) or (not is_long and curr_price <= pos["tp3_price"])

                            if (liquidation_climax and time_in_zone >= min_time_in_zone) or hard_tp3_hit:
                                close_position(pos, curr_price, "LIQUIDATION_CLIMAX_HIT", vault, mmr_cache=mmr_cache)
                                db_dirty = True
                                position_closed_in_tick = True
                                continue
                        else:
                            # ak vyšiel zo zóny, resetuj timer
                            pos["tp3_zone_entered_at"] = None
                            step_pct = (0.45 * natr) / 100.0
                            new_tsl = pos["extreme_price"] * (1.0 - dir_mult * step_pct)
                            if (is_long and new_tsl > pos["sl_tsl_price"]) or (not is_long and new_tsl < pos["sl_tsl_price"]):
                                paper_amend_sl(pos, new_tsl, "POST_TP2_TSL")
                                pos_changed_in_tick = True
                                db_dirty = True

                    # E2. VOLATILITY-ADAPTIVE THETA TIME-STOP
                    # Všetky výpočty výnosu používajú dir_mult, takže LONG/SHORT sú symetrické.
                    max_hold_defaults = compute_theta_timers(natr)
                    max_hold = float(pos.get("max_hold_sec") or max_hold_defaults[1])
                    if not pos.get("tp1_hit", False):
                        entry_px_ts = float(pos.get("entry_price") or curr_price)
                        raw_pnl_pct = (
                            dir_mult
                            * ((curr_price - entry_px_ts) / max(1e-9, entry_px_ts))
                            * 100.0
                        )
                        cost_pct = float(
                            pos.get("cost_rate", compute_roundtrip_cost_rate(win_id))
                        ) * 100.0

                        # Theta kompresia: od 55 % životnosti vyber čistý zisk nad nákladmi.
                        if age_sec >= (0.55 * max_hold):
                            if raw_pnl_pct >= (cost_pct + 0.10):
                                close_position(
                                    pos,
                                    curr_price,
                                    "THETA_DECAY_PROFIT_EXIT",
                                    vault,
                                    mmr_cache=mmr_cache,
                                )
                                db_dirty = True
                                position_closed_in_tick = True
                                continue

                        # Dynamický chop stop: od 100 % max_hold, najneskôr pri 120 %.
                        if age_sec >= max_hold:
                            move_pct = abs(curr_price - entry_px_ts) / max(1e-9, entry_px_ts) * 100.0
                            if (
                                move_pct < 0.45 * float(natr or 1.5)
                                or age_sec >= (1.20 * max_hold)
                            ):
                                close_position(
                                    pos,
                                    curr_price,
                                    "TIME_STOP_CHOP",
                                    vault,
                                    mmr_cache=mmr_cache,
                                )
                                db_dirty = True
                                position_closed_in_tick = True
                                continue

                    # Stagnácia po TP1 zostáva zachovaná (4 hodiny).
                    if pos.get("tp1_hit", False) and age_sec >= 14400.0:
                        close_position(pos, curr_price, "TIME_STOP_STAGNANT", vault, mmr_cache=mmr_cache)
                        db_dirty = True
                        position_closed_in_tick = True
                        continue

                    # F. VYHODNOCENÍ ZÁSAHU TVRDÉHO SL / TSL NA BURZE (paper)
                    # LONG:  hit ak curr_price/eff_low  <= SL
                    # SHORT: hit ak curr_price/eff_high >= SL
                    # eff_* ignoruje pre-entry wick prvé 15 min; min_hold 120s pre počiatočný Hard SL
                    # Same-tick skip po TP1; od nasledujúcej sekundy BE lock plne chráni kapitál
                    # (300s freeze platí len pre posun TSL v sekcii D, nie pre zásah BE stopu)
                    sl_px = float(pos["sl_tsl_price"])
                    min_hold_sec = 120.0

                    hit_sl_tsl = (
                        (is_long and (curr_price <= sl_px or eff_low <= sl_px))
                        or (not is_long and (curr_price >= sl_px or eff_high >= sl_px))
                    )
                    if hit_sl_tsl:
                        if pos.get("tsl_active"):
                            # Same-bar ako TP1: knôt by inak okamžite zabil zvyšok na BE
                            if _tp1_just_hit:
                                pass
                            else:
                                tp1_ts = float(pos.get("tp1_hit_ts") or 0.0)
                                sec_since_tp1 = (float(tick_ts) - tp1_ts) if tp1_ts > 0 else 1e9
                                # Rozlíš tesný BE stop vs bežný trailing profit
                                entry_px = float(pos.get("entry_price") or 0.0)
                                be_band = abs(sl_px - entry_px) / max(1e-9, entry_px)
                                cost_r = float(pos.get("cost_rate", compute_roundtrip_cost_rate(win_id)))
                                if be_band <= (cost_r + 0.0020) and sec_since_tp1 < 600.0:
                                    reason = "BE_STOP_AFTER_TP1"
                                else:
                                    reason = "TRAILING_SL_PROFIT"
                                exit_px = sl_px
                                close_position(pos, exit_px, reason, vault, mmr_cache=mmr_cache)
                                db_dirty = True
                                position_closed_in_tick = True
                                continue
                        elif age_sec >= min_hold_sec:
                            reason = "HARD_SL_HIT"
                            exit_px = sl_px
                            close_position(pos, exit_px, reason, vault, mmr_cache=mmr_cache)
                            db_dirty = True
                            position_closed_in_tick = True
                            continue
                        # else: ochranná doba – ignoruj, pozícia beží ďalej

                    # UI synchronizační vrstva pre pamäť Streamlitu
                    update_live_pnl_metrics(pos, curr_price, dir_mult, win_id)

                    # Okamžitý zápis pri zmene stavu + priebežný Live PnL každé 2 s pre cockpit sidebar
                    if pos_changed_in_tick or (tick % 2 == 0):
                        try:
                            push_active_position_row(pos, vault)
                            save_vault_local(vault)
                        except Exception:
                            pass

                # Po spracovaní všetkých pozícií v tiku: ak niečo zatvorené → break pre UI redraw
                if position_closed_in_tick:
                    break

                # Čakanie do ďalšej sekundy (okrem posledného tiku)
                if tick < 14:
                    elapsed = time.time() - tick_ts
                    if elapsed < 1.0:
                        await asyncio.sleep(1.0 - elapsed)

        # 2. Globálny 3-minútový skener (po dual-speed slučke alebo keď je scan due)
        scan_now = time.time()
        if is_scan_due or (scan_now - float(vault.get("last_main_scan", 0) or 0)) >= 180:
            await process_global_market_scanning(
                client, vault, scan_now, ny_lock, klines_cache, mmr_cache=mmr_cache
            )

        # VŽDY zosúlaď active_positions s vault-om (cockpit musí vidieť rovnaký počet obchodov)
        try:
            sync_all_windows_to_db(vault)
        except Exception:
            pass

        # SYSTEM B – meta-controller strictness update
        try:
            update_meta_controller(vault, time.time())
        except Exception:
            pass

        flush_pending_sync(vault)
        save_vault(vault, sync_cloud=True)


def _compute_market_regime_metrics(tickers_list: list) -> tuple[float, float]:
    changes = []
    for ticker in tickers_list:
        try:
            last_px = float(ticker.get("last") or 0.0)
            open_24h = float(ticker.get("open24h") or 0.0)
            if last_px > 0.0 and open_24h > 0.0:
                changes.append(((last_px - open_24h) / open_24h) * 100.0)
        except (AttributeError, TypeError, ValueError):
            continue
    if not changes:
        return 0.5, 2.0
    arr = np.asarray(changes, dtype=float)
    return float(np.mean(arr > 0.0)), float(np.std(arr))


async def process_global_market_scanning(client: httpx.AsyncClient, vault: dict, now_ts: float, ny_lock: bool, klines_cache: dict, mmr_cache: dict | None = None):
    """
    Finální propojovací blok skeneru 2.0.
    1. Počítá plovoucí 30denní percentily volatility pro osu Y.
    2. Provádí ortogonální de-duplikaci aktiv a sektorů.
    3. Alokuje signály napříč 6 nezávislými okny podle strukturální likvidity.
    """
    if mmr_cache is None:
        mmr_cache = load_mmr_cache()
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
    try:
        market_breadth, market_dispersion = _compute_market_regime_metrics(list(tickers.values()))
        vault["last_market_breadth"] = float(market_breadth)
        vault["last_market_dispersion"] = float(market_dispersion)
    except Exception:
        vault.setdefault("last_market_breadth", 0.5)
        vault.setdefault("last_market_dispersion", 2.0)
    sectors_map = get_sectors_map()
    metrics_24h = get_24h_metrics()
    update_24h_oi_metrics(tickers, oi_map, existing_metrics=metrics_24h)
    # Unified shadow pool MTM on already-fetched tickers (zero extra API)
    try:
        process_shadow_pool(vault, tickers, now_ts)
    except Exception:
        pass

    btc_klines = await fetch_okx_klines(client, "BTCUSDT", "15m", 25, klines_cache)
    try:
        if len(btc_klines) >= 9:
            btc_last = float(get_close(btc_klines[-1]))
            btc_2h_ago = float(get_close(btc_klines[-9]))
            vault["last_btc_2h_pct"] = ((btc_last - btc_2h_ago) / max(1e-9, btc_2h_ago)) * 100.0
    except Exception:
        vault.setdefault("last_btc_2h_pct", 0.0)
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
        mmr_cell = get_mmr_cell(curr_rx, curr_ry, mmr_cache=mmr_cache)
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
    pending_setups = vault.setdefault("pending_setups", {})
    # Vyčisti expirované pending setups
    for _pk in list(pending_setups.keys()):
        if now_ts >= float((pending_setups.get(_pk) or {}).get("expires_at", 0) or 0):
            pending_setups.pop(_pk, None)
    active_syms = {p["symbol"] for p in vault["windows"].values() if p["status"] == "ACTIVE" and p["symbol"]}
    for _ps in pending_setups.values():
        _c = (_ps or {}).get("cand") or {}
        if _c.get("symbol"):
            active_syms.add(_c["symbol"])
    active_secs = {sectors_map.get(s, "UNKNOWN") for s in active_syms}
    for _ps in pending_setups.values():
        _c = (_ps or {}).get("cand") or {}
        _sec = _c.get("sector")
        if _sec:
            active_secs.add(_sec)

    # ===== OKNO 1: BTCUSDT (MAKROKOTVA) =====
    if vault["windows"]["1"]["status"] == "WAITING" and "1" not in pending_setups and len(btc_klines) >= 20:
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
                # 1m Sniper čakáreň pre Okno 1 (30 min) – otvorenie až po VWAP/Tape spúšťači
                btc_cand = {
                    "symbol": "BTCUSDT",
                    "direction": direction,
                    "is_pd": False,
                    "is_funding_trap": is_trap,
                    "sector": sectors_map.get("BTCUSDT", "BTC_MACRO"),
                    "ofd": float(btc_ofd),
                    "klines": btc_klines,
                    "exec_price": exec_price,
                    "entry_fingerprint": {
                        "macro_btc_1h": float(btc_veto.get("btc_1h_pct", 0.0)),
                        "liq_vol_24h": float((tickers.get("BTCUSDT") or {}).get("volCcy24h", 0.0)),
                        "ofd_val": float(btc_ofd),
                        "shield_rsi": btc_rsi_fp,
                        "micro_wob": 0.0,
                        "micro_vpin": float(calculate_vpin_toxicity(btc_klines)) if len(btc_klines) >= 10 else 0.0,
                        "risk_wr": 52.0,
                    },
                }
                btc_natr = float(calculate_natr_14(btc_klines))
                sniper_ttl_sec, max_hold_sec = compute_theta_timers(btc_natr)
                pending_setups["1"] = {
                    "window_id": 1,
                    "cand": btc_cand,
                    "natr": btc_natr,
                    "sniper_ttl_sec": sniper_ttl_sec,
                    "max_hold_sec": max_hold_sec,
                    "created_at": now_ts,
                    "expires_at": now_ts + sniper_ttl_sec,
                }
                active_syms.add("BTCUSDT")

    # ===== OKNA 2 & 3: ELITNÍ ŠAMPIONI (Objem >= 10M USD) – 1m Sniper čakáreň =====
    for w_idx in ("2", "3"):
        if vault["windows"][w_idx]["status"] == "WAITING" and w_idx not in pending_setups:
            cands = await evaluate_candidates(client, tickers, oi_map, active_syms, active_secs, 7_000_000.0, 1e12, btc_veto, klines_cache, is_pd_mode=False, vault=vault, sectors_map=sectors_map, metrics_24h=metrics_24h)
            if cands:
                c = cands[0]
                cand_natr = float(calculate_natr_14(c["klines"])) if c.get("klines") else 2.0
                sniper_ttl_sec, max_hold_sec = compute_theta_timers(cand_natr)
                pending_setups[w_idx] = {
                    "window_id": int(w_idx),
                    "cand": c,
                    "natr": cand_natr,
                    "sniper_ttl_sec": sniper_ttl_sec,
                    "max_hold_sec": max_hold_sec,
                    "created_at": now_ts,
                    "expires_at": now_ts + sniper_ttl_sec,
                }
                active_syms.add(c["symbol"])
                active_secs.add(c.get("sector") or "UNKNOWN")

    # ===== OKNA 4 & 5: ŠAMPIONI NÍZKÉ LIKVIDITY (Objem 5M – 9.99M USD) – 1m Sniper čakáreň =====
    for w_idx in ("4", "5"):
        if vault["windows"][w_idx]["status"] == "WAITING" and w_idx not in pending_setups:
            cands = await evaluate_candidates(client, tickers, oi_map, active_syms, active_secs, 3_500_000.0, 9_999_999.0, btc_veto, klines_cache, is_pd_mode=False, vault=vault, sectors_map=sectors_map, metrics_24h=metrics_24h)
            if cands:
                c = cands[0]
                cand_natr = float(calculate_natr_14(c["klines"])) if c.get("klines") else 2.0
                sniper_ttl_sec, max_hold_sec = compute_theta_timers(cand_natr)
                pending_setups[w_idx] = {
                    "window_id": int(w_idx),
                    "cand": c,
                    "natr": cand_natr,
                    "sniper_ttl_sec": sniper_ttl_sec,
                    "max_hold_sec": max_hold_sec,
                    "created_at": now_ts,
                    "expires_at": now_ts + sniper_ttl_sec,
                }
                active_syms.add(c["symbol"])
                active_secs.add(c.get("sector") or "UNKNOWN")

    # ===== OKNO 6: AI LOVEC ŠAMPIÓNOV (US SESSION - XGBOOST V3) – 1m Sniper čakáreň =====
    if vault["windows"]["6"]["status"] == "WAITING" and "6" not in pending_setups:
        tickers_list = list(tickers.values()) if isinstance(tickers, dict) else list(tickers)
        ai_cand = await scan_ai_champion_window6(supabase, tickers_list, active_syms, vault=vault)
        if ai_cand:
            sym_6 = ai_cand["symbol"]
            klines_6 = await fetch_okx_klines(client, sym_6, "15m", 25, klines_cache)
            if len(klines_6) >= 20:
                cand_payload = {
                    "symbol": sym_6,
                    "direction": ai_cand["direction"],
                    "is_pd": False,
                    "is_funding_trap": False,
                    "sector": sectors_map.get(sym_6, "UNKNOWN"),
                    "ofd": float(ai_cand.get("ofd", 1.0)),
                    "klines": klines_6,
                    "exec_price": float(ai_cand["price"]),
                    "ai_features": dict(ai_cand.get("ai_features") or {}),
                    "v5_features": dict(ai_cand.get("ai_features") or {}),
                    "ai_confidence": float(ai_cand.get("ai_confidence", 0.0)),
                    "source": ai_cand.get("source", "AI_CHAMPION_US_V3"),
                    "entry_fingerprint": {
                        "macro_btc_1h": float(btc_veto.get("btc_1h_pct", 0.0)),
                        "liq_vol_24h": float((tickers.get(sym_6) or {}).get("volCcy24h", 0.0)),
                        "ofd_val": float(ai_cand.get("ofd", 1.0)),
                        "shield_rsi": float(ai_cand.get("rsi", 50.0)),
                        "micro_wob": 0.0,
                        "micro_vpin": float(ai_cand.get("vpin", 0.0)),
                        "risk_wr": 52.0,
                    },
                    "book_levels": [],
                }
                cand_natr = float(calculate_natr_14(klines_6))
                sniper_ttl_sec, max_hold_sec = compute_theta_timers(cand_natr)
                pending_setups["6"] = {
                    "window_id": 6,
                    "cand": cand_payload,
                    "natr": cand_natr,
                    "sniper_ttl_sec": sniper_ttl_sec,
                    "max_hold_sec": max_hold_sec,
                    "created_at": now_ts,
                    "expires_at": now_ts + sniper_ttl_sec,
                }
                active_syms.add(sym_6)

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
        ticker_error = str(pos.get("last_ticker_error") or "").strip()
        if ticker_error:
            st.caption(f":red[⚠️ {ticker_error}]")
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
    Uses BVC VPIN, OFD floors zosúladené so živými filtrami (1.18 / 0.82).
    """
    if len(klines_15m) < 12:
        return False, 0.0
    vols = [float(get_vol(k)) for k in klines_15m[-11:]]
    avg_prev = float(np.mean(vols[:-1])) if len(vols) > 1 else 0.0
    curr_vol = vols[-1]
    if avg_prev > 0 and (curr_vol / avg_prev) < 1.10:
        return False, 0.0
    vpin = float(calculate_vpin_toxicity(klines_15m))
    # Adaptívny strop zosúladený s default vpin_max (~0.80 pri strictness=1)
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
    # OFD floors zosúladené s _get_filter_ctx (max 1.18 / min 0.82)
    if direction == "LONG" and ofd < 1.18:
        return False, 0.0
    if direction == "SHORT" and ofd > 0.82:
        return False, 0.0
    px = float(get_close(klines_15m[-1]))
    return True, px


def _sandbox_close(pos: dict, exit_price: float, reason: str, sandbox: dict, vault: dict, mmr_cache: dict | None = None):
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
            mmr_cache=mmr_cache,
            exit_reason=reason,
        )
    except Exception:
        pass
    try:
        update_filter_weights_from_outcome(pos, is_win, reason, vault)
    except Exception:
        pass


def _sandbox_manage(pos: dict, bar: list, sandbox: dict, vault: dict, mmr_cache: dict | None = None) -> bool:
    """
    Manage one sandbox position on a historical bar.
    Zosúladené s Oblasťou 2: Risk-Capped DCA1/2, čistý BE po TP1, rozlíšenie exit_reason.
    Returns True if position still open, False if closed.
    """
    hi = float(get_high(bar))
    lo = float(get_low(bar))
    cl = float(get_close(bar))
    is_long = pos["direction"] == "LONG"
    dir_mult = 1.0 if is_long else -1.0
    entry = float(pos["entry_price"])
    lev = max(1, int(pos.get("leverage") or 1))
    natr = float(pos.get("natr") or 2.0)
    cost_rate = float(pos.get("cost_rate", 0.0015))

    # --- Risk-Capped DCA1 / DCA2 (pred SL/TP, iba non-P&D a pred TP1) ---
    if not pos.get("is_pd") and not pos.get("tp1_hit"):
        for idx_dca, pct in ((1, 0.25), (2, 0.35)):
            if pos.get(f"dca{idx_dca}_filled"):
                continue
            dca_p = float(pos.get(f"dca{idx_dca}_price") or 0.0)
            if dca_p <= 0:
                continue
            if not ((is_long and lo <= dca_p) or (not is_long and hi >= dca_p)):
                continue
            added_m = float(pos.get("margin_usd") or 0.0) * pct
            old_qty = (float(pos["active_margin"]) * lev) / max(1e-12, float(pos["entry_price"]))
            new_qty = (added_m * lev) / dca_p
            if old_qty + new_qty > 0:
                pos["entry_price"] = (
                    (old_qty * float(pos["entry_price"])) + (new_qty * dca_p)
                ) / (old_qty + new_qty)
            pos["active_margin"] = float(pos["active_margin"]) + added_m
            pos["max_deployed_margin"] = max(
                float(pos.get("max_deployed_margin", pos["active_margin"])),
                float(pos["active_margin"]),
            )
            pos[f"dca{idx_dca}_filled"] = True
            entry = float(pos["entry_price"])
            # Dynamický SL: 1.6 × NATR od novej priemernej ceny + likvidačná poistka
            new_sl_pct = max(0.010, (1.6 * natr) / 100.0)
            new_sl = entry * (1.0 - dir_mult * new_sl_pct)
            if pos.get("regime") == "FUTURES" and lev > 1:
                new_liq = entry * (1.0 - dir_mult * (0.90 / lev))
                safe_buffer = max(0.003, (0.4 * natr) / 100.0)
                if is_long:
                    if new_sl <= new_liq * (1.0 + safe_buffer):
                        new_sl = new_liq * (1.0 + safe_buffer)
                else:
                    if new_sl >= new_liq * (1.0 - safe_buffer):
                        new_sl = new_liq * (1.0 - safe_buffer)
            pos["sl_tsl_price"] = new_sl
            tp_mod = float(pos.get("tp_mod", 1.0))
            pos["tp1_price"] = entry * (1.0 + dir_mult * ((1.0 * natr * tp_mod) / 100.0 + cost_rate))
            pos["tp2_price"] = entry * (1.0 + dir_mult * ((1.8 * natr * tp_mod) / 100.0 + cost_rate))

    # Liquidation
    if pos.get("regime") == "FUTURES" and lev > 1:
        liq = entry * (1.0 - dir_mult * (0.90 / lev))
        if (is_long and lo <= liq) or (not is_long and hi >= liq):
            pos["_force_full_margin_loss"] = True
            _sandbox_close(pos, liq, "LIQUIDATION_HIT", sandbox, vault, mmr_cache=mmr_cache)
            return False

    # SL / TSL s rozlíšením dôvodu
    sl = float(pos.get("sl_tsl_price") or 0.0)
    if sl > 0:
        hit_sl = (is_long and lo <= sl) or (not is_long and hi >= sl)
        if hit_sl:
            if pos.get("tsl_active"):
                be_band = abs(sl - entry) / max(1e-9, entry)
                if be_band <= 0.012:
                    reason = "BE_STOP_AFTER_TP1"
                else:
                    reason = "TRAILING_SL_PROFIT"
            else:
                reason = "HARD_SL_HIT"
            _sandbox_close(pos, sl, reason, sandbox, vault, mmr_cache=mmr_cache)
            return False

    # TP1 scale-out 50% + čistý BE lock
    if not pos.get("tp1_hit"):
        tp1 = float(pos.get("tp1_price") or 0.0)
        hit_tp1 = (is_long and hi >= tp1) or (not is_long and lo <= tp1)
        if hit_tp1 and tp1 > 0:
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
            # Čistý Break-Even: cost_rate + 0.08 %
            be = entry * (1.0 + dir_mult * (cost_rate + 0.0008))
            pos["sl_tsl_price"] = be

    # TP2 close remainder
    if pos.get("tp1_hit") and not pos.get("tp2_hit"):
        tp2 = float(pos.get("tp2_price") or 0.0)
        hit_tp2 = (is_long and hi >= tp2) or (not is_long and lo <= tp2)
        if hit_tp2 and tp2 > 0:
            _sandbox_close(pos, tp2, "TP2_SCALE_OUT", sandbox, vault, mmr_cache=mmr_cache)
            return False
        # trail extreme
        if is_long:
            pos["extreme_price"] = max(float(pos.get("extreme_price", cl)), hi)
        else:
            pos["extreme_price"] = min(float(pos.get("extreme_price", cl)), lo)
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
    mmr_cache = load_mmr_cache()
    # 25 % exponenciálne omladenie (Memory Decay) – stará história neprebíja 7-dňový Pre-Train
    for _cell in list(mmr_cache.values()):
        try:
            _cell["win_rate"] = float(_cell.get("win_rate", 52.0)) * 0.75 + 50.0 * 0.25
            _cell["sl_modifier"] = float(_cell.get("sl_modifier", 1.0)) * 0.75 + 1.0 * 0.25
            _cell["tp_modifier"] = float(_cell.get("tp_modifier", 1.0)) * 0.75 + 1.0 * 0.25
        except Exception:
            pass
    # Jemné omladenie filter_weights o 20 % smerom k 1.0
    _fw = vault.setdefault("filter_weights", {
        "macro": 1.0, "liq": 1.0, "ofd": 1.0, "shield": 1.0, "micro": 1.0, "risk": 1.0,
    })
    for _k in list(_fw.keys()):
        try:
            _fw[_k] = float(np.clip(float(_fw.get(_k, 1.0)) * 0.80 + 1.0 * 0.20, 0.70, 1.40))
        except Exception:
            pass
    vault["filter_weights"] = _fw
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
                if _sandbox_manage(pos, bar, sandbox, vault, mmr_cache=mmr_cache):
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
                setup = await build_trade_setup(cand, float(sandbox["balance"]), window_id=1, vault=vault, client=client, mmr_cache=mmr_cache)
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
        default_ar = bool(vault.get("auto_refresh_enabled", False))
        auto_refresh = st.toggle("Auto-refresh (15s)", value=default_ar)
        if auto_refresh != default_ar:
            vault["auto_refresh_enabled"] = bool(auto_refresh)
            save_vault(vault, sync_cloud=True)
    with c3:
        if st.button("🔄 Reload vault", use_container_width=True):
            st.session_state.vault = load_vault()
            st.rerun()

    # Spustenie jadra (zámok proti súbehu + ukladanie riadi run_core_cycle)
    st.session_state.setdefault("is_running", False)
    had_active_at_start = any(
        (w or {}).get("status") == "ACTIVE"
        for w in (vault.get("windows") or {}).values()
    )
    if (run_once or auto_refresh) and not st.session_state.is_running:
        st.session_state.is_running = True
        with st.spinner("Beží core cyklus…"):
            try:
                asyncio.run(run_core_cycle(st.session_state.vault))
            except Exception as e:
                st.error(f"Chyba v core cykle: {e}")
            finally:
                st.session_state.is_running = False

    st.divider()

    # 6 okien
    st.subheader("Obchodné okná")
    cols = st.columns(3)
    windows = vault.get("windows", {})
    for i, win_key in enumerate(["1", "2", "3", "4", "5", "6"]):
        with cols[i % 3]:
            pos = windows.get(win_key, {"status": "WAITING"})
            render_window_card(win_key, pos)

    with st.expander("🛠️ Núdzové uvoľnenie okna (pri zaseknutí)", expanded=False):
        sel_win = st.selectbox(
            "Číslo okna",
            options=[1, 2, 3, 4, 5, 6],
            format_func=lambda value: f"Okno {value}",
            key="emergency_clear_window",
        )
        if st.button("Uvoľniť vybrané okno (WAITING)", use_container_width=True):
            force_clear_active_position(int(sel_win), vault)
            vault.setdefault("windows", {})[str(sel_win)] = {"status": "WAITING"}
            save_vault(vault, sync_cloud=True)
            st.success(f"Okno {int(sel_win)} bolo nastavené na WAITING.")
            st.rerun()

    # 1m Sniper čakáreň + lievik (reject_stats)
    st.divider()
    st.subheader("1m Sniper čakáreň & Lievik")
    pending = vault.get("pending_setups") or {}
    now_ui = time.time()
    if not pending:
        st.caption("1m Sniper čakáreň: prázdna")
    else:
        for pk, item in sorted(pending.items(), key=lambda x: str(x[0])):
            cand = (item or {}).get("cand") or {}
            sym_p = cand.get("symbol") or "?"
            dir_p = cand.get("direction") or "?"
            exp_at = float((item or {}).get("expires_at", 0) or 0)
            left = max(0, int(exp_at - now_ui))
            st.write(
                f"Okno {pk}: **{sym_p}** ({dir_p}) – čaká na 1m VWAP/Tape trigger, "
                f"vyprší za **{left}s**"
            )
    rs = vault.get("reject_stats") or {}
    st.caption(
        "Lievik zamietnutí — "
        f"Macro: {int(rs.get('macro', 0))} · "
        f"Liq: {int(rs.get('liq', 0))} · "
        f"OFD: {int(rs.get('ofd', 0))} · "
        f"Shield: {int(rs.get('shield', 0))} · "
        f"Micro: {int(rs.get('micro', 0))} · "
        f"Risk: {int(rs.get('risk', 0))}"
    )

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

    # Auto-refresh: po dual-speed 15s slučke len krátka pauza, inak 15s idle
    if auto_refresh:
        still_active = any(
            (w or {}).get("status") == "ACTIVE"
            for w in (st.session_state.vault.get("windows") or {}).values()
        )
        if had_active_at_start or still_active:
            time.sleep(0.5)
        else:
            time.sleep(15)
        st.rerun()


# Vstupný bod Streamlit
if __name__ == "__main__" or True:  # Streamlit vždy spúšťa modul
    main_ui()
