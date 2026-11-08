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

st.set_page_config(page_title="Shark Alpha Core (Cloud 1)", layout="centered")

# =========================================================================
# BLOK 1: INFRAŠTRUKTÚRA, CLOUD TREZOR (SUPABASE) & ZÁLOŽNÁ CACHE
# =========================================================================
VAULT_FILE = "vault_state_backup.json"
BINANCE_FAPI = "https://binance.com"

get_open = operator.itemgetter(1)
get_high = operator.itemgetter(2)
get_low = operator.itemgetter(3)
get_close = operator.itemgetter(4)

@st.cache_resource
def init_supabase() -> Client:
    url = st.secrets["SUPABASE_URL"]
    key = st.secrets["SUPABASE_KEY"]
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
        "last_pd_scan": 0,
        "last_30m_slot": "",
        "pending_supabase_sync": []
    }

def load_vault() -> dict:
    # 1. Primárne načítanie z cloudovej tabuľky bot_vault_state
    try:
        res = supabase.table("bot_vault_state").select("state_json").eq("id", 1).execute()
        if res.data and res.data[0].get("state_json"):
            state = res.data[0]["state_json"]
            if "last_30m_slot" not in state:
                state["last_30m_slot"] = ""
            if "pending_supabase_sync" not in state:
                state["pending_supabase_sync"] = []
            return state
    except Exception as e:
        st.warning(f"Upozornenie: Čítanie trezoru zo Supabase zlyhalo ({e}), skúšam lokálnu zálohu.")

    # 2. Záložné načítanie z lokálneho disku (ak vypadla sieť)
    if os.path.exists(VAULT_FILE):
        try:
            with open(VAULT_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass

    return get_default_vault()

def save_vault(state: dict):
    # 1. Lokálna poistka
    try:
        tmp_file = f"{VAULT_FILE}.tmp"
        with open(tmp_file, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
        os.replace(tmp_file, VAULT_FILE)
    except Exception:
        pass

    # 2. Primárny zápis do Supabase (prežije reštart Streamlit kontajnera)
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
# BLOK 3: 4-OKNOVÝ SAS SKENER, FUNDING PASCE, ORDER FLOW & P&D RADAR
# =========================================================================
async def fetch_market_snapshot(client: httpx.AsyncClient):
    t_res, f_res = await asyncio.gather(
        client.get(f"{BINANCE_FAPI}/fapi/v1/ticker/24hr"),
        client.get(f"{BINANCE_FAPI}/fapi/v1/premiumIndex")
    )
    if t_res.status_code == 451 or f_res.status_code == 451:
        raise RuntimeError("Binance API vrátilo HTTP 451 (Geoblokácia US servera Streamlit Cloudu).")
    t_res.raise_for_status()
    f_res.raise_for_status()

    tickers = {x["symbol"]: x for x in t_res.json() if x["symbol"].endswith("USDT")}
    fundings = {x["symbol"]: x for x in f_res.json() if x["symbol"].endswith("USDT")}
    return tickers, fundings

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
    fundings: dict,
    excluded_symbols: set,
    excluded_sectors: set,
    min_vol: float,
    max_vol: float,
    is_pd_mode: bool = False
):
    sectors_map = get_sectors_map()
    metrics_24h = get_24h_metrics()
    pre_filtered = []

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

        total_base_vol = float(t.get("volume", 0))
        taker_buy_base = float(t.get("takerBuyBaseVolume", total_base_vol * 0.5))
        taker_sell_base = max(1e-9, total_base_vol - taker_buy_base)
        ofd = taker_buy_base / taker_sell_base

        f_data = fundings.get(sym, {})
        funding_rate = float(f_data.get("lastFundingRate", 0.0))
        price_change_pct = float(t["priceChangePercent"])

        pre_filtered.append((sym, vol, sec, price, ofd, funding_rate, price_change_pct))

    # Paralelné sťahovanie sviečok pre predfiltrované coiny (max 10 naraz)
    sem = asyncio.Semaphore(10)

    async def process_symbol(item):
        sym, vol, sec, price, ofd, funding_rate, price_change_pct = item
        async with sem:
            try:
                k_res = await client.get(f"{BINANCE_FAPI}/fapi/v1/klines", params={"symbol": sym, "interval": "15m", "limit": 20})
                if k_res.status_code != 200:
                    return None
                klines = k_res.json()
                if len(klines) < 15:
                    return None

                last_candle = klines[-1]
                last_open = float(get_open(last_candle))
                last_close = float(get_close(last_candle))
                is_15m_green = last_close > last_open

                if is_pd_mode:
                    oi_res = await client.get(f"{BINANCE_FAPI}/fapi/v1/openInterest", params={"symbol": sym})
                    if oi_res.status_code != 200:
                        return None
                    oi_curr = float(oi_res.json().get("openInterest", 0)) * price
                    oi_avg = metrics_24h.get(sym, {}).get("oi_avg_24h", oi_curr)
                    oi_ratio = oi_curr / max(1.0, oi_avg)

                    if oi_ratio > 1.80 and ofd > 1.60 and is_15m_green: sas_score = oi_ratio * ofd * 100
return {"symbol": sym, "direction": "LONG", "score": sas_score, "is_funding_trap": False, "is_pd": True, "sector": sec, "ofd": ofd, "klines": klines}
elif price_change_pct > 15.0 and funding_rate > 0.0020 and ofd < 0.80 and not is_15m_green:
sas_score = abs(price_change_pct) * (1 / max(0.1, ofd)) * 50
return {"symbol": sym, "direction": "SHORT", "score": sas_score, "is_funding_trap": True, "is_pd": True, "sector": sec, "ofd": ofd, "klines": klines}
return None
direction = None
is_trap = False
if funding_rate > 0.0020 and not is_15m_green and ofd < 0.90:
direction = "SHORT"
is_trap = True
elif funding_rate < -0.0050 and is_15m_green and ofd > 1.15:
direction = "LONG"
is_trap = True
elif ofd > 1.30 and is_15m_green:
direction = "LONG"
elif ofd < 0.70 and not is_15m_green:
direction = "SHORT"
if direction:
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

=========================================================================


BLOK 4: INTEGROVANÝ RISK ENGINE, KELLY, NATR ŠTÍT & ORDER FLOW TSL


=========================================================================

def calculate_natr_14(klines: list) -> float:
df = pd.DataFrame(klines, columns=["ts", "o", "h", "l", "c", "v", "ct", "qv", "tr", "tb", "tq", "ig"])
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

=========================================================================


HLAVNÁ AUTONÓMNA SLUČKA JADRA (CLOUD 1)


=========================================================================

async def run_core_cycle(vault: dict):
flush_pending_sync(vault)
now_ts = time.time()
now_dt = datetime.now(timezone.utc)
async with httpx.AsyncClient(http2=True, timeout=10.0) as client:
tickers, fundings = await fetch_market_snapshot(client)
for win_key, pos in vault["windows"].items():
win_id = int(win_key)
if pos["status"] != "ACTIVE":
continue
sym = pos["symbol"]
if sym not in tickers:
continue
curr_price = float(tickers[sym]["lastPrice"])
t_data = tickers[sym]
tot_v = float(t_data.get("volume", 1.0))
tb_v = float(t_data.get("takerBuyBaseVolume", tot_v * 0.5))
ofd = tb_v / max(1e-9, tot_v - tb_v)
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
# Spoľahlivý 30-minútový slot (nepremešká okno pri vyššej latencii API)
slot_half = "00" if now_dt.minute < 30 else "30"
current_30m_slot = f"{now_dt.strftime('%Y-%m-%d-%H')}:{slot_half}"
is_new_30m_slot = current_30m_slot != vault.get("last_30m_slot", "")
active_syms = {p["symbol"] for p in vault["windows"].values() if p["status"] == "ACTIVE" and p["symbol"]}
sectors_map = get_sectors_map()
active_secs = {sectors_map.get(s, "UNKNOWN") for s in active_syms}
if is_new_30m_slot:
vault["last_30m_slot"] = current_30m_slot
if vault["windows"]["1"]["status"] == "WAITING":
k_btc = await client.get(f"{BINANCE_FAPI}/fapi/v1/klines", params={"symbol": "BTCUSDT", "interval": "15m", "limit": 20})
if k_btc.status_code == 200:
btc_klines = k_btc.json()
last_btc = btc_klines[-1]
btc_dir = "LONG" if float(get_close(last_btc)) > float(get_open(last_btc)) else "SHORT"
setup = build_trade_setup({"symbol": "BTCUSDT", "direction": btc_dir, "is_pd": False, "is_funding_trap": False, "klines": btc_klines}, vault["balance"], 1)
if setup:
vault["windows"]["1"] = setup
active_syms.add("BTCUSDT")
if vault["windows"]["2"]["status"] == "WAITING":
cands_a = await evaluate_candidates(client, tickers, fundings, active_syms, active_secs, 10_000_000, 1e12, False)
for c in cands_a:
setup = build_trade_setup(c, vault["balance"], 2)
if setup:
vault["windows"]["2"] = setup
active_syms.add(c["symbol"])
active_secs.add(c["sector"])
break
if vault["windows"]["3"]["status"] == "WAITING":
cands_b = await evaluate_candidates(client, tickers, fundings, active_syms, active_secs, 5_000_000, 9_999_999, False)
for c in cands_b:
setup = build_trade_setup(c, vault["balance"], 3)
if setup:
vault["windows"]["3"] = setup
active_syms.add(c["symbol"])
break
if vault["windows"]["4"]["status"] == "WAITING" and (now_ts - vault.get("last_pd_scan", 0) >= 300):
vault["last_pd_scan"] = now_ts
cands_c = await evaluate_candidates(client, tickers, fundings, active_syms, active_secs, 1_000_000, 4_999_999, True)
for c in cands_c:
setup = build_trade_setup(c, vault["balance"], 4)
if setup:
vault["windows"]["4"] = setup
break
save_vault(vault)
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

=========================================================================


SPUSTENIE NEPRETRŽITÉHO JADRA


=========================================================================

st.title("🦈 Shark Alpha Core (Cloud 1 - Active Engine)")
vault_state = load_vault()
col1, col2 = st.columns(2)
col1.metric("Live Fiktívny Kapitál", f"${vault_state['balance']:.2f} USD")
col2.metric("Posledný 30m Slot (UTC)", vault_state.get("last_30m_slot") or "Čaká na prvý cyklus")
try:
asyncio.run(run_core_cycle(vault_state))
st.success(f"✅ Cyklus dokončený ({datetime.now(timezone.utc).strftime('%H:%M:%S')} UTC)")
except Exception as e:
st.error(f"⚠️ Chyba cyklu: {e}")
st.json(vault_state["windows"])
time.sleep(5)
st.rerun()
