import streamlit as st
import httpx
import numpy as np
import pandas as pd
import plotly.graph_objects as go
from datetime import datetime, timezone
import time
import json
from supabase import create_client, Client

st.set_page_config(page_title="Shark Alpha Cockpit (Cloud 2)", layout="wide")

OKX_API = "https://www.okx.com"

@st.cache_resource
def init_supabase() -> Client:
    url = st.secrets["SUPABASE_URL"].strip().rstrip("/")
    key = st.secrets["SUPABASE_KEY"].strip()
    return create_client(url, key)

supabase = init_supabase()

def symbol_to_okx_swap(sym: str) -> str:
    if not sym:
        return "BTC-USDT-SWAP"
    if sym.endswith("USDT"):
        base = sym[:-4]
        return f"{base}-USDT-SWAP"
    return sym

# =========================================================================
# BLOK 1: ZAKEŠOVANÉ DÁTOVÉ POMOCNÍKY (bez duplicitného sťahovania)
# =========================================================================
@st.cache_data(ttl=15)
def fetch_cached_vault_state() -> dict:
    """state_json z bot_vault_state (id == 1) – cache 3 s, aby 1s sidebar nezahlcoval Supabase."""
    try:
        vs_res = supabase.table("bot_vault_state").select("state_json").eq("id", 1).execute()
        if vs_res.data:
            raw = vs_res.data[0].get("state_json", {})
            if isinstance(raw, str):
                return json.loads(raw) or {}
            if isinstance(raw, dict):
                return raw
    except Exception:
        pass
    return {}


@st.cache_data(ttl=25)
def fetch_cached_trade_history() -> pd.DataFrame:
    try:
        res = supabase.table("trade_history").select(
            "id,window_id,symbol,direction,regime,leverage,entry_price,exit_price,margin_usd,pnl_usd,pnl_percent,exit_reason,closed_at,open_at,dca1_filled,dca2_filled,dca3_filled,tp1_hit,tp2_hit"
        ).order("id", desc=True).limit(300).execute()
        df = pd.DataFrame(res.data) if res.data else pd.DataFrame()
        if not df.empty and "id" in df.columns:
            df = df.sort_values("id", ascending=True).reset_index(drop=True)
        return df
    except Exception:
        return pd.DataFrame()


@st.cache_data(ttl=25)
def fetch_cached_mmr_matrix() -> pd.DataFrame:
    try:
        res = supabase.table("ai_mmr_matrix").select("*").order("total_trades", desc=True).execute()
        return pd.DataFrame(res.data) if res.data else pd.DataFrame()
    except Exception:
        return pd.DataFrame()


@st.cache_data(ttl=60)
def fetch_cached_ai_model_info() -> dict:
    try:
        res = supabase.table("ai_models").select("model_name,accuracy,trained_at").eq("id", 1).execute()
        return res.data[0] if res.data else {}
    except Exception:
        return {}


def write_remote_control_flag(updates: dict) -> bool:
    allowed = {"auto_refresh_enabled", "run_pretrain_requested", "force_clear_window_id"}
    if not isinstance(updates, dict) or not updates or not set(updates).issubset(allowed):
        return False
    try:
        res = supabase.table("bot_vault_state").select("state_json").eq("id", 1).execute()
        if res.data:
            raw = res.data[0].get("state_json") or {}
            state = json.loads(raw) if isinstance(raw, str) else dict(raw) if isinstance(raw, dict) else {}
            state.update(updates)
            supabase.table("bot_vault_state").update({
                "state_json": state,
                "updated_at": datetime.now(timezone.utc).isoformat()
            }).eq("id", 1).execute()
        else:
            state = dict(updates)
            supabase.table("bot_vault_state").insert({
                "id": 1, "state_json": state,
                "updated_at": datetime.now(timezone.utc).isoformat()
            }).execute()
        fetch_cached_vault_state.clear()
        return True
    except Exception:
        return False


def _fetch_okx_candles_df(symbol: str, bar: str, limit: int) -> pd.DataFrame:
    """Stiahne sviečky z OKX a vráti chronologický DataFrame (ts, o, h, l, c, v, time)."""
    inst_id = symbol_to_okx_swap(symbol)
    try:
        with httpx.Client(timeout=8.0) as client:
            res = client.get(
                f"{OKX_API}/api/v5/market/candles",
                params={"instId": inst_id, "bar": bar, "limit": str(limit)}
            )
            res.raise_for_status()
            raw = res.json().get("data", [])

        if not raw:
            return pd.DataFrame()

        # OKX vracia sviečky od najnovšej po najstaršiu -> otočíme chronologicky
        raw_chronological = list(reversed(raw))
        # OKX formát: [ts, o, h, l, c, vol, volCcy, volCcyQuote, confirm]
        rows = []
        for k in raw_chronological:
            rows.append({
                "ts": int(k[0]),
                "o": float(k[1]),
                "h": float(k[2]),
                "l": float(k[3]),
                "c": float(k[4]),
                "v": float(k[5])
            })
        df = pd.DataFrame(rows)
        df["time"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
        return df
    except Exception:
        return pd.DataFrame()


@st.cache_data(ttl=4)
def fetch_visual_klines_1m(symbol: str) -> pd.DataFrame:
    return _fetch_okx_candles_df(symbol, "1m", 90)


@st.cache_data(ttl=14)
def fetch_visual_klines_15m(symbol: str) -> pd.DataFrame:
    return _fetch_okx_candles_df(symbol, "15m", 60)


def fetch_visual_klines(symbol: str, interval: str) -> pd.DataFrame:
    """Wrapper: 1m sviečky sa obnovujú každé 4 s, 15m každých 14 s."""
    if interval == "1m":
        return fetch_visual_klines_1m(symbol)
    return fetch_visual_klines_15m(symbol)


@st.cache_data(ttl=2)
def load_active_windows(quiet: bool = False) -> dict:
    """Čerstvé active_positions zo Supabase (order by window_id) ako {window_id: row}."""
    try:
        pos_res = supabase.table("active_positions").select("window_id,status,symbol,direction,regime,leverage,entry_price,margin_usd,active_margin,dca1_price,dca2_price,dca3_price,dca1_filled,dca2_filled,dca3_filled,tp1_price,tp2_price,tp3_price,tp1_hit,tp2_hit,sl_tsl_price,tsl_active,pnl_usd,pnl_percent,mandate_expires_at,opened_at,updated_at").order("window_id").execute()
        return {int(r["window_id"]): r for r in pos_res.data} if pos_res.data else {}
    except Exception as e:
        if quiet:
            st.caption(f"active_positions nedostupné: {e}")
        else:
            st.error(f"Chyba načítania active_positions: {e}")
        return {}


def safe_float(val, default=0.0) -> float:
    try:
        if val is None:
            return default
        return float(val)
    except (TypeError, ValueError):
        return default


# Čistý reštart equity: zabudnúť na staré / rozbité current_balance zo SQL
EQUITY_BASELINE = 1000.0


def build_fresh_equity(df_sub: pd.DataFrame) -> tuple[pd.Series, pd.Series]:
    """
    Equity od EQUITY_BASELINE + kumulatívny súčet pnl_usd.
    Ignoruje current_balance z DB (po mazaní SQL býva nekonzistentný).
    Triedi chronologicky; vynechá riadky bez pnl_usd.
    """
    if df_sub is None or df_sub.empty or "pnl_usd" not in df_sub.columns:
        return pd.Series(dtype=float), pd.Series(dtype=float)

    d = df_sub.copy()
    d["pnl_usd"] = pd.to_numeric(d["pnl_usd"], errors="coerce")
    d = d.dropna(subset=["pnl_usd"])
    if d.empty:
        return pd.Series(dtype=float), pd.Series(dtype=float)

    # časová os
    if "closed_at" in d.columns and d["closed_at"].notna().any():
        d["_t"] = pd.to_datetime(d["closed_at"], utc=True, errors="coerce")
    elif "updated_at" in d.columns and d["updated_at"].notna().any():
        d["_t"] = pd.to_datetime(d["updated_at"], utc=True, errors="coerce")
    elif "id" in d.columns:
        d["_t"] = d["id"]
    else:
        d["_t"] = np.arange(len(d))

    d = d.dropna(subset=["_t"]).sort_values("_t")
    if d.empty:
        return pd.Series(dtype=float), pd.Series(dtype=float)

    equity = EQUITY_BASELINE + d["pnl_usd"].cumsum()
    return d["_t"].reset_index(drop=True), equity.reset_index(drop=True)


def render_execution_breakdown(df_subset: pd.DataFrame, title_prefix: str):
    """3-stĺpcový breakdown: Take Profit hits / DCA fills / Dôvod zatvorenia pre daný subset histórie.

    trade_history obsahuje aj partial scale-out riadky (TP1_SCALE_OUT / TP2_SCALE_OUT).
    - TP hits: počítame eventy (scale-out riadky + finálne TP3 close).
    - DCA fills: počítame len na finálnych close riadkoch (flags dca*_filled z core1).
    - Dôvod zatvorenia: všetky riadky (vrátane scale-outov).
    """
    n_all = len(df_subset)
    if n_all == 0:
        st.info(f"{title_prefix}: Zatiaľ žiadne uzavreté obchody.")
        return

    if "exit_reason" in df_subset.columns:
        reasons_all = df_subset["exit_reason"].fillna("").astype(str)
    else:
        reasons_all = pd.Series([""] * n_all, index=df_subset.index)

    # Finálne close = všetko okrem partial scale-out ledger riadkov
    scale_out_reasons = {"TP1_SCALE_OUT", "TP2_SCALE_OUT"}
    is_final = ~reasons_all.isin(scale_out_reasons)
    df_final = df_subset.loc[is_final].copy()
    n_trades = len(df_final) if len(df_final) > 0 else n_all
    reasons_final = (
        df_final["exit_reason"].fillna("").astype(str)
        if "exit_reason" in df_final.columns and len(df_final) > 0
        else reasons_all
    )

    c1, c2, c3 = st.columns(3)

    with c1:
        st.markdown("**Take Profit hits**")
        # Event-based: scale-out riadky + finálne TP3/climax
        tp1_cnt = int((reasons_all == "TP1_SCALE_OUT").sum())
        tp2_cnt = int((reasons_all == "TP2_SCALE_OUT").sum())
        tp3_cnt = int(reasons_all.str.contains("LIQUIDATION_CLIMAX|TP3", regex=True, na=False).sum())
        # Fallback z boolean flags na finálnych close (ak core1 zapísal tp*_hit)
        if tp1_cnt == 0 and "tp1_hit" in df_final.columns and len(df_final) > 0:
            tp1_cnt = int(df_final["tp1_hit"].fillna(False).astype(bool).sum())
        if tp2_cnt == 0 and "tp2_hit" in df_final.columns and len(df_final) > 0:
            tp2_cnt = int(df_final["tp2_hit"].fillna(False).astype(bool).sum())
        st.write(f"TP1 executed: **{tp1_cnt}** / {n_trades}")
        st.write(f"TP2 executed: **{tp2_cnt}** / {n_trades}")
        st.write(f"TP3 hit (close): **{tp3_cnt}** / {n_trades}")

    with c2:
        st.markdown("**DCA fills**")
        # Trade-level: flags len na finálnych close riadkoch (core1 close_position)
        src = df_final if len(df_final) > 0 else df_subset
        n_dca_base = len(src) if len(src) > 0 else n_all
        if "dca1_filled" in src.columns:
            dca1_cnt = int(src["dca1_filled"].fillna(False).astype(bool).sum())
        else:
            dca1_cnt = 0
        if "dca2_filled" in src.columns:
            dca2_cnt = int(src["dca2_filled"].fillna(False).astype(bool).sum())
        else:
            dca2_cnt = 0
        if "dca3_filled" in src.columns:
            dca3_cnt = int(src["dca3_filled"].fillna(False).astype(bool).sum())
        else:
            dca3_cnt = 0
        st.write(f"DCA1 filled: **{dca1_cnt}** / {n_dca_base}")
        st.write(f"DCA2 filled: **{dca2_cnt}** / {n_dca_base}")
        st.write(f"DCA3 filled: **{dca3_cnt}** / {n_dca_base}")
        if "dca1_filled" not in df_subset.columns:
            st.caption("⚠ Stĺpce dca*_filled ešte nie sú v trade_history (deploy core1 fix).")

    with c3:
        st.markdown("**Dôvod zatvorenia**")
        reason_labels = {
            "HARD_SL_HIT": "🔴 Hard SL",
            "BE_STOP_AFTER_TP1": "🟡 BE stop po TP1",
            "TP1_SCALE_OUT": "TP1_SCALE_OUT",
            "TRAILING_SL_PROFIT": "🟢 Trailing SL (zisk)",
            "EARLY_ORDERFLOW_ESCAPE": "⚠️ Early OF escape",
            "TP2_SCALE_OUT": "TP2_SCALE_OUT",
            "LIQUIDATION_CLIMAX_HIT": "🎯 TP3 Liquidation Climax",
            "TP3_LIQUIDATION_HIT": "🎯 TP3",
            "VPIN_EMERGENCY_ESCAPE": "☠️ VPIN Emergency",
            "TP1_WALL_FRONTRUN": "🧱 TP1 Front-Run steny",
            "TP2_WALL_FRONTRUN": "🧱 TP2 Front-Run steny",
            "SPOOF_COLLAPSE_ESCAPE": "👻 Spoofing únik",
            "TIME_STOP_CHOP": "⏳ Time-Stop (Chop)",
            "TIME_STOP_STAGNANT": "⏳ Time-Stop (Stagnácia)",
            "MFE_PROFIT_LOCK": "🛡️ MFE Zámok zisku",
            "THETA_DECAY_PROFIT_EXIT": "⏱️ Theta Decay (zisk)",
        }
        reason_counts = reasons_all.replace("", "UNKNOWN").value_counts()
        for reason, cnt in reason_counts.items():
            label = reason_labels.get(str(reason), str(reason))
            pct = (cnt / n_all) * 100.0
            st.write(f"{label}: **{cnt}** ({pct:.0f}%)")


# =========================================================================
# BLOK 7: VIZUÁLNY KOKPIT (FRAGMENTY: DASHBOARD 15s, SIDEBAR 1s, GRAFY 5s/15s)
# =========================================================================
st.title("🦈 Shark Alpha Institutional Cockpit (Cloud 2)")

# -------------------------------------------------------------------------
# 2. ĽAVÝ SIDEBAR: ovládanie (mimo fragmentu) + 1s nepreblikávajúce živé pozície
# -------------------------------------------------------------------------
st.sidebar.header("🎛️ Ovládanie & Živé Pozície")
timeframe = st.sidebar.radio("Časový rámec grafov:", ["15m", "1m"], horizontal=True)
live_mode = st.sidebar.toggle("⚡ Živý režim (Auto)", value=True)
if st.sidebar.button("🔄 Obnoviť dáta Kokpitu"):
    st.cache_data.clear()
    st.rerun()

st.sidebar.markdown("### 🤖 Ovládanie VPS Enginu (`core1_version3`)")
vault_control = fetch_cached_vault_state()
engine_on = bool(vault_control.get("auto_refresh_enabled", False))
st.sidebar.markdown("🟢 **ENGINE BEŽÍ**" if engine_on else "🔴 **STANDBY (VYPNUTÝ)**")
if st.sidebar.button("⏻ " + ("Vypnúť Engine" if engine_on else "Spustiť Engine"), key="engine_master_toggle"):
    if write_remote_control_flag({"auto_refresh_enabled": not engine_on}):
        st.rerun()
    else:
        st.sidebar.error("Zápis ovládania VPS zlyhal.")

remote_active_windows = load_active_windows(quiet=True)
has_active_position = any(w.get("status") == "ACTIVE" for w in remote_active_windows.values())
pretrain_running = bool(vault_control.get("pretrain_running"))
if pretrain_running:
    st.sidebar.info("⏳ Pre-Trainer práve beží...")
else:
    if st.sidebar.button(
        "🧠 Spustiť 7d Pre-Trainer",
        disabled=engine_on or has_active_position,
        key="pretrain_trigger",
    ):
        if write_remote_control_flag({"run_pretrain_requested": True}):
            st.rerun()
        else:
            st.sidebar.error("Zápis Pre-Trainer triggeru zlyhal.")

selected_wid = st.sidebar.selectbox("Okno na vyčistenie", [1, 2, 3, 4, 5, 6], key="force_clear_window")
if st.sidebar.button("🧹 Vyčistiť okno", key="force_clear_window_button"):
    if write_remote_control_flag({"force_clear_window_id": int(selected_wid)}):
        st.rerun()
    else:
        st.sidebar.error("Zápis Force Clear požiadavky zlyhal.")

st.sidebar.divider()

window_titles = {
    1: "1️⃣ Makrokotva BTC",
    2: "2️⃣ Elitný ≥10M (A)",
    3: "3️⃣ Elitný ≥10M (B)",
    4: "4️⃣ Stredná likvidita 3.5-9.9M (A)",
    5: "5️⃣ Stredná likvidita 3.5-9.9M (B)",
    6: "6️⃣ AI Lovec (XGBoost V5)"
}


@st.fragment(run_every="3s" if live_mode else None)
def render_live_sidebar_positions():
    active_windows = load_active_windows(quiet=True)
    vault_state = fetch_cached_vault_state()
    pending_all = vault_state.get("pending_setups") or {}

    for wid in (1, 2, 3, 4, 5, 6):
        w = active_windows.get(wid, {})
        status = w.get("status", "WAITING")

        if status == "ACTIVE":
            sym = w.get("symbol") or "—"
            with st.expander(f"{window_titles[wid]} | {sym}", expanded=True):
                dir_icon = "🟢 LONG" if w.get("direction") == "LONG" else "🔴 SHORT"
                st.markdown(f"**Smer:** {dir_icon} | **Režim:** {w.get('regime')} ({w.get('leverage')}x)")
                st.markdown(f"**Entry Price:** `{safe_float(w.get('entry_price')):.5f}`")

                # TP hladiny + executed stavy
                tp1_hit = bool(w.get("tp1_hit", False))
                tp2_hit = bool(w.get("tp2_hit", False))
                tp1_txt = f"`{safe_float(w.get('tp1_price')):.5f}`  ✅ executed" if tp1_hit else f"`{safe_float(w.get('tp1_price')):.5f}`"
                tp2_txt = f"`{safe_float(w.get('tp2_price')):.5f}`  ✅ executed" if tp2_hit else f"`{safe_float(w.get('tp2_price')):.5f}`"
                st.markdown(f"**TP1:** {tp1_txt}")
                st.markdown(f"**TP2:** {tp2_txt}")
                st.markdown(f"**TP3 (Liq):** `{safe_float(w.get('tp3_price')):.5f}`")

                # SL / TSL
                tsl_active = bool(w.get("tsl_active", False))
                sl_label = "🟢 TSL (Aktívny)" if tsl_active else "🔴 Hard SL"
                st.markdown(f"**{sl_label}:** `{safe_float(w.get('sl_tsl_price')):.5f}`")

                # DCA hladiny – iba tie, ktorých cena > 0 (nulové DCA úplne skryté)
                dca1_p = safe_float(w.get("dca1_price"))
                dca2_p = safe_float(w.get("dca2_price"))
                dca3_p = safe_float(w.get("dca3_price"))
                if dca1_p > 0:
                    dca1_txt = f"`{dca1_p:.5f}`  ✅ executed" if bool(w.get("dca1_filled", False)) else f"`{dca1_p:.5f}`"
                    st.markdown(f"**DCA1:** {dca1_txt}")
                if dca2_p > 0:
                    dca2_txt = f"`{dca2_p:.5f}`  ✅ executed" if bool(w.get("dca2_filled", False)) else f"`{dca2_p:.5f}`"
                    st.markdown(f"**DCA2:** {dca2_txt}")
                if dca3_p > 0:
                    dca3_txt = f"`{dca3_p:.5f}`  ✅ executed" if bool(w.get("dca3_filled", False)) else f"`{dca3_p:.5f}`"
                    st.markdown(f"**DCA3:** {dca3_txt}")

                # Fáza obchodu (odvodená)
                if tp2_hit:
                    phase = "🎯 Fáza 3 – Trailing po TP2"
                elif tp1_hit:
                    phase = "🔒 Fáza 2 – Break-even lock (TP1 executed)"
                else:
                    phase = "⏳ Fáza 1 – Čaká na TP1"
                st.caption(phase)

                # Anti-MM defense badges (live flags from vault_state["windows"][wid])
                w_vault = (vault_state.get("windows") or {}).get(str(wid), {}) or {}
                if w_vault.get("dca_rescue_active"):
                    st.warning("🚑 DCA Rescue Exit aktívny (komprimovaný TP1)")
                if int(w_vault.get("spoof_streak") or 0) > 0:
                    st.caption(f"👁️ Spoof radar streak: {int(w_vault.get('spoof_streak'))}/3")

                # Live PnL
                pnl_u = safe_float(w.get("pnl_usd"))
                pnl_p = safe_float(w.get("pnl_percent"))
                st.metric("Live PnL", f"${pnl_u:+.3f} USD", f"{pnl_p:+.2f} %")
                # Vstupný kapitál (Marža)
                act_m = safe_float(w.get("active_margin"))
                tot_m = safe_float(w.get("margin_usd"))
                lev = max(1, int(safe_float(w.get("leverage"), 1)))
                st.markdown(f"**Vstupný kapitál:** `${act_m:.2f}` / `${tot_m:.2f} USD` *(pozícia: ${act_m * lev:.2f})*")

                # Updated_at ak existuje
                updated = w.get("updated_at")
                if updated:
                    st.caption(f"Posledný update: {updated}")
                    try:
                        updated_dt = datetime.fromisoformat(str(updated).replace("Z", "+00:00"))
                        if updated_dt.tzinfo is None:
                            updated_dt = updated_dt.replace(tzinfo=timezone.utc)
                        age_s = (datetime.now(timezone.utc) - updated_dt.astimezone(timezone.utc)).total_seconds()
                        if age_s > 20:
                            st.warning(f"⚠️ Dáta stoja {int(age_s)}s (skontroluj Cloud 1)")
                    except (TypeError, ValueError, OverflowError):
                        pass
        else:
            # Čakajúci kandidát z 1m Sniper čakárne (pending_setups)
            pend = pending_all.get(str(wid)) or {}
            cand = pend.get("cand") or {}
            p_sym = cand.get("symbol")
            exp_at = safe_float(pend.get("expires_at"), 0.0)
            if p_sym and exp_at > time.time():
                with st.expander(f"{window_titles[wid]} | 🎯 {p_sym} (1m Sniper)", expanded=False):
                    p_dir = str(cand.get("direction") or "").upper()
                    p_icon = "🟢 LONG" if p_dir == "LONG" else "🔴 SHORT"
                    st.markdown(f"**Smer:** {p_icon}")
                    st.markdown(f"**Zostávajúci čas čakárne:** `{int(exp_at - time.time())} s`")
            else:
                with st.expander(f"{window_titles[wid]} | —", expanded=False):
                    st.caption("Stav: Čaká na vhodný signál z Cloudu 1...")


@st.fragment(run_every="30s" if live_mode else None)
def render_dashboard_and_telemetry():
    # Každý fragment si stiahne iba to, čo potrebuje
    active_windows = load_active_windows()

    history_df = fetch_cached_trade_history()
    ai_df = fetch_cached_mmr_matrix()

    # Live vault JSONB (System F telemetry source – no extra columns)
    vault_state = fetch_cached_vault_state()

    # -------------------------------------------------------------------------
    # 1. HORNÁ ČASŤ OBRAZOVKY: DYNAMICKÝ EQUITY DASHBOARD & AI MATICA VÝKONU
    # -------------------------------------------------------------------------
    top_col1, top_col2 = st.columns([1.2, 1.0])

    with top_col1:
        st.subheader("📈 Dynamický Equity Dashboard")
        # --- Kapitál: po SQL wipe nespoliehať sa len na vault.balance (býva starý/seknutý) ---
        # Hlavná metrika = autoritatívny vault balance z workera; ledger (baseline + realized + unrealized) je v caption.

        realized_pnl = 0.0
        if not history_df.empty and "pnl_usd" in history_df.columns:
            realized_pnl = float(pd.to_numeric(history_df["pnl_usd"], errors="coerce").fillna(0.0).sum())

        unrealized = sum(
            safe_float(w.get("pnl_usd"), 0.0)
            for w in active_windows.values()
            if w.get("status") == "ACTIVE"
        )

        # Rekonštruovaný kapitál (fresh baseline) – to, čo má user vidieť ako „Aktuálny zostatok“
        curr_bal = EQUITY_BASELINE + realized_pnl + unrealized
        total_pnl = realized_pnl + unrealized  # delta voči baseline
        vault_bal = safe_float(vault_state.get("balance"), curr_bal)

        if not history_df.empty:
            # Win/Fail: 1 obchod = 1 finálny close (bez TP1/TP2 scale-out ledger riadkov).
            # WIN = dosiahnutý TP1/TP2/TP3 alebo TSL/BE po TP, alebo kladný pnl na finálnom close.
            WIN_REASONS = {
                "TRAILING_SL_PROFIT",
                "BE_STOP_AFTER_TP1",
                "LIQUIDATION_CLIMAX_HIT",
                "TP3_LIQUIDATION_HIT",
                "TP2_SCALE_OUT",  # ak by finálny close omylom niesol tento reason
                "TP1_WALL_FRONTRUN",
                "TP2_WALL_FRONTRUN",
                "MFE_PROFIT_LOCK",
                "THETA_DECAY_PROFIT_EXIT",
            }
            FAIL_REASONS = {
                "HARD_SL_HIT",
                "EARLY_ORDERFLOW_ESCAPE",
                "VPIN_EMERGENCY_ESCAPE",
                "LIQUIDATION_HIT",
                "SPOOF_COLLAPSE_ESCAPE",
                "TIME_STOP_CHOP",
            }
            SCALE_OUT = {"TP1_SCALE_OUT", "TP2_SCALE_OUT"}

            def _row_is_win(row, columns) -> bool:
                """Single source of truth for Win/Fail classification (top summary + tab stats)."""
                reason = str(row.get("exit_reason") or "")
                tp1 = bool(row.get("tp1_hit", False)) if "tp1_hit" in columns else False
                tp2 = bool(row.get("tp2_hit", False)) if "tp2_hit" in columns else False
                try:
                    pnl = float(row.get("pnl_usd") or 0.0)
                except (TypeError, ValueError):
                    pnl = 0.0

                if reason in WIN_REASONS:
                    return True
                elif tp1 or tp2:
                    # dosiahnutý TP → počítame ako WIN (aj keď zvyšok dotrailoval na BE)
                    return True
                elif reason == "HARD_SL_HIT":
                    # Legacy MFE-ratcheted profitable stops (pre-V5.8) are wins; only pnl <= 0 is a fail.
                    return pnl > 0
                elif reason in FAIL_REASONS:
                    return False
                return pnl > 0

            def _compute_tab_trade_stats(df_sub: pd.DataFrame) -> dict:
                """Per-tab stats: 1 closed trade = 1 row (partial scale-outs excluded from Win Rate / counts)."""
                if "exit_reason" in df_sub.columns:
                    _r = df_sub["exit_reason"].fillna("").astype(str)
                    df_closed = df_sub.loc[~_r.isin(SCALE_OUT)].copy()
                else:
                    df_closed = df_sub.copy()

                wins = 0
                losses = 0
                for _, _row in df_closed.iterrows():
                    if _row_is_win(_row, df_closed.columns):
                        wins += 1
                    else:
                        losses += 1
                closed_count = wins + losses
                win_rate = (wins / closed_count) * 100.0 if closed_count > 0 else 0.0

                if "pnl_usd" in df_sub.columns:
                    pnl_all = pd.to_numeric(df_sub["pnl_usd"], errors="coerce").fillna(0.0)
                else:
                    pnl_all = pd.Series(0.0, index=df_sub.index)
                raw_pnl_sum = float(pnl_all.sum())
                gross_profit = float(pnl_all.loc[pnl_all > 0].sum())
                gross_loss = float(abs(pnl_all.loc[pnl_all < 0].sum()))
                profit_factor = (gross_profit / gross_loss) if gross_loss > 1e-9 else (99.99 if gross_profit > 0 else 0.0)

                est_entry_fees = 0.0
                if "margin_usd" in df_closed.columns and "leverage" in df_closed.columns:
                    est_entry_fees = float(
                        (
                            pd.to_numeric(df_closed["margin_usd"], errors="coerce").fillna(0.0)
                            * pd.to_numeric(df_closed["leverage"], errors="coerce").fillna(1.0)
                            * 0.0005
                        ).sum()
                    )
                net_pnl_after_entry_fee = raw_pnl_sum - est_entry_fees

                return {
                    "wins": wins,
                    "losses": losses,
                    "closed_count": closed_count,
                    "win_rate": win_rate,
                    "raw_pnl_sum": raw_pnl_sum,
                    "gross_profit": gross_profit,
                    "gross_loss": gross_loss,
                    "profit_factor": profit_factor,
                    "est_entry_fees": est_entry_fees,
                    "net_pnl_after_entry_fee": net_pnl_after_entry_fee,
                    "ledger_count": int(len(df_sub)),
                }

            def _render_tab_stats_row(df_sub: pd.DataFrame):
                st_ = _compute_tab_trade_stats(df_sub)
                c1, c2, c3, c4 = st.columns(4)
                c1.metric("Čistý PnL ($)", f"${st_['raw_pnl_sum']:+.2f}")
                c1.caption(
                    f"Po odpočte vstup. poplatkov (~${st_['est_entry_fees']:.2f}): ${st_['net_pnl_after_entry_fee']:+.2f}"
                )
                c2.metric("Reálny Win Rate", f"{st_['win_rate']:.1f} %", f"{st_['wins']} Win / {st_['losses']} Fail")
                c3.metric("Profit Factor", f"{st_['profit_factor']:.2f}" if st_["profit_factor"] < 99.0 else "∞")
                c4.metric("Uzavreté obchody", f"{st_['closed_count']}", f"{st_['ledger_count']} ledger záznamov")

            if "exit_reason" in history_df.columns:
                _reasons = history_df["exit_reason"].fillna("").astype(str)
                _pdf = history_df.loc[~_reasons.isin(SCALE_OUT)].copy()
            else:
                _pdf = history_df.copy()

            win_trades = 0
            fail_trades = 0
            if len(_pdf) > 0:
                for _, row in _pdf.iterrows():
                    is_win = _row_is_win(row, _pdf.columns)

                    if is_win:
                        win_trades += 1
                    else:
                        fail_trades += 1

            total_trades = win_trades + fail_trades
            wr_total = (win_trades / total_trades) * 100.0 if total_trades > 0 else 0.0
            n_scale = 0
            if "exit_reason" in history_df.columns:
                n_scale = int(history_df["exit_reason"].fillna("").astype(str).isin(list(SCALE_OUT)).sum())

            m1, m2, m3 = st.columns(3)
            m1.metric("Aktuálny Kapitál (OKX Vault)", f"${vault_bal:.2f}", f"{vault_bal - EQUITY_BASELINE:+.2f} USD")
            m1.caption(
                f"Ledger (trade_history): baseline {EQUITY_BASELINE:.0f} + realized {realized_pnl:+.2f} "
                f"+ unrealized {unrealized:+.2f} = ${curr_bal:.2f}"
            )
            m2.metric("Celková Úspešnosť", f"{wr_total:.1f} %", f"{total_trades} obchodov")
            m2.caption(
                f"✅ Win: **{win_trades}**  |  ❌ Fail: **{fail_trades}**"
                + (f"  |  scale-out eventy: {n_scale} (nezapočítané do WR)" if n_scale else "")
            )
            m3.metric("Nerealizované PnL", f"${unrealized:+.2f}")

            # ----- ROZDELENÉ ŠTATISTIKY: Okná 1–5 vs AI Okno 6 -----
            if "window_id" in history_df.columns:
                w_ids = pd.to_numeric(history_df["window_id"], errors="coerce").fillna(0).astype(int)
                df_w15 = history_df[w_ids != 6].copy()
                df_w6 = history_df[w_ids == 6].copy()
            else:
                df_w15 = history_df.copy()
                df_w6 = history_df.iloc[0:0].copy()

            tab_w15, tab_w6 = st.tabs([
                "📊 Okná 1–5 (Adaptívne MMR)",
                "🤖 AI Okno 6 (XGBoost V5)",
            ])

            def _equity_xcol(df_sub: pd.DataFrame) -> str:
                if "closed_at" in df_sub.columns and df_sub["closed_at"].notna().any():
                    return "closed_at"
                if "updated_at" in df_sub.columns and df_sub["updated_at"].notna().any():
                    return "updated_at"
                return "id"

            with tab_w15:
                n15 = len(df_w15)
                if n15 > 0:
                    _render_tab_stats_row(df_w15)

                    x15, eq15 = build_fresh_equity(df_w15)
                    if len(eq15) > 0:
                        fig15 = go.Figure()
                        fig15.add_hline(y=EQUITY_BASELINE, line_dash="dot", line_color="gray",
                                        annotation_text=f"baseline {EQUITY_BASELINE:.0f}")
                        fig15.add_trace(go.Scatter(
                            x=x15, y=eq15,
                            mode="lines+markers", name="Equity W1–5 ($)",
                            line=dict(color="#00ffcc", width=3),
                        ))
                        fig15.update_layout(
                            height=260, margin=dict(l=10, r=10, t=10, b=10),
                            template="plotly_dark",
                            yaxis_title="Equity ($)",
                        )
                        st.plotly_chart(fig15, use_container_width=True, key="equity_chart_w15")
                        st.caption(f"Equity od baseline **{EQUITY_BASELINE:.0f} $** + Σ pnl (aktuálne dáta, bez starej SQL histórie balance).")
                    else:
                        st.info("Okná 1–5: žiadne platné pnl_usd na vykreslenie equity.")
                else:
                    st.info("Okná 1–5: Zatiaľ žiadne uzavreté obchody.")

                st.markdown("##### 📊 Sumár štatistiky — Okná 1–5")
                render_execution_breakdown(df_w15, "Okná 1–5")

            with tab_w6:
                # AI model status (id=1)
                mrow = fetch_cached_ai_model_info()
                if mrow:
                    m_name = mrow.get("model_name") or "xgboost_us_champion_v5"
                    m_acc = mrow.get("accuracy")
                    m_trained = mrow.get("trained_at") or "—"
                    acc_txt = f"{float(m_acc) * 100:.1f}%" if m_acc is not None else "—"
                    st.caption(f"Model: **{m_name}**  |  Accuracy: **{acc_txt}**  |  Trained at: **{m_trained}**")
                else:
                    st.caption("AI model (id=1) zatiaľ nie je v `ai_models`.")

                n6 = len(df_w6)
                if n6 > 0:
                    _render_tab_stats_row(df_w6)

                    x6, eq6 = build_fresh_equity(df_w6)
                    if len(eq6) > 0:
                        fig6 = go.Figure()
                        fig6.add_hline(y=EQUITY_BASELINE, line_dash="dot", line_color="gray",
                                       annotation_text=f"baseline {EQUITY_BASELINE:.0f}")
                        fig6.add_trace(go.Scatter(
                            x=x6, y=eq6,
                            mode="lines+markers", name="Equity AI W6 ($)",
                            line=dict(color="#00FFAA", width=3),
                        ))
                        fig6.update_layout(
                            height=260, margin=dict(l=10, r=10, t=10, b=10),
                            template="plotly_dark",
                            yaxis_title="Equity ($)",
                        )
                        st.plotly_chart(fig6, use_container_width=True, key="equity_chart_w6")
                        st.caption(f"AI Equity od baseline **{EQUITY_BASELINE:.0f} $** + Σ pnl (fresh restart).")
                    else:
                        st.info("AI Okno 6: žiadne platné pnl_usd na vykreslenie equity.")
                else:
                    st.info("AI Okno 6: Zatiaľ žiadne uzavreté obchody.")

                st.markdown("##### 📊 Sumár štatistiky — AI Okno 6")
                render_execution_breakdown(df_w6, "AI Okno 6")
        else:
            st.metric("Aktuálny Kapitál", f"${curr_bal:.2f}", f"{total_pnl:+.2f} USD")
            st.info("Zatiaľ neboli uzavreté žiadne obchody. Equity krivka sa vykreslí automaticky po prvom trejde.")

    with top_col2:
        st.subheader("🧠 Matrica Výkonu a Učenia AI")
        if not ai_df.empty:
            display_cols = ["regime_x", "regime_y", "win_rate", "avg_win_r", "avg_loss_r",
                            "sl_modifier", "tp_modifier", "total_trades"]
            available_cols = [c for c in display_cols if c in ai_df.columns]
            st.dataframe(ai_df[available_cols], height=310, use_container_width=True, hide_index=True)
        else:
            st.info("AI Matica (`ai_mmr_matrix`) je pripravená a čaká na prvé dáta z Cloudu 1.")

    # -------------------------------------------------------------------------
    # SYSTEM F: Adaptívna Telemetria & Audit Log
    # -------------------------------------------------------------------------
    st.subheader("🔬 System F: Adaptívna Telemetria & Audit Log")
    tab_meta, tab_mmr, tab_shadow = st.tabs([
        "Meta-Controller & Váhy filtrov",
        "3×3 MMR Heatmapa",
        "Shadow Incubator Audit",
    ])

    with tab_meta:
        TIER_LABELS = {
            "TIER_1_BTC": "1️⃣ Tier 1 — BTC Makrokotva",
            "TIER_2_ELITE": "2️⃣ Tier 2 — Elitné Alty (≥10M)",
            "TIER_3_LOWCAP": "3️⃣ Tier 3 — Low-Cap Alty (3.5–9.99M)",
        }
        DIRECTIONS = ["LONG", "SHORT"]
        tiers = list(TIER_LABELS.keys())
        tier_weights_root = vault_state.get("tier_filter_weights") or {}
        tier_strictness_root = vault_state.get("tier_strictness") or {}
        tier_reject_root = vault_state.get("tier_reject_stats") or {}
        tier_tensors_root = vault_state.get("mmr_tensors") or {}

        sel_tier = st.radio("Tier", tiers, horizontal=True, format_func=lambda k: TIER_LABELS[k], key="v7_tier_choice")
        sel_dir = st.radio("Smer", DIRECTIONS, horizontal=True, key="v7_direction_choice")
        strictness = safe_float(((tier_strictness_root.get(sel_tier) or {}).get(sel_dir)), 0.90)
        tw = ((tier_weights_root.get(sel_tier) or {}).get(sel_dir) or {"w_flow": 1.0, "w_book": 1.0, "w_shield": 1.0, "w_tox": 1.0})
        rs = ((tier_reject_root.get(sel_tier) or {}).get(sel_dir) or {"macro": 0, "flow": 0, "book": 0, "shield": 0, "tox": 0})
        if not isinstance(tw, dict): tw = {"w_flow": 1.0, "w_book": 1.0, "w_shield": 1.0, "w_tox": 1.0}
        if not isinstance(rs, dict): rs = {"macro": 0, "flow": 0, "book": 0, "shield": 0, "tox": 0}

        st.markdown(f"#### {TIER_LABELS[sel_tier]} · {sel_dir}")
        c1, c2, c3, c4, c5, c6 = st.columns(6)
        c1.metric(f"Strictness ({sel_tier} | {sel_dir})", f"{strictness:.3f}")
        c2.metric("Daily PnL ($)", f"{safe_float(vault_state.get('daily_pnl_usd'), 0.0):+.2f}")
        shield_on = bool(vault_state.get("shield_mode_active", False))
        scout_on = bool(vault_state.get("scout_mode", False))
        c3.metric("Shield Mode", "AKTÍVNY" if shield_on else "OFF")
        c4.metric("Scout Mode", "AKTÍVNY" if scout_on else "OFF")
        long_cnt = sum(1 for w in active_windows.values() if w.get("status") == "ACTIVE" and str(w.get("direction", "")).upper() == "LONG")
        short_cnt = sum(1 for w in active_windows.values() if w.get("status") == "ACTIVE" and str(w.get("direction", "")).upper() == "SHORT")
        c5.metric("Portfolio Exposure LONG", f"{long_cnt}/3")
        c6.metric("Portfolio Exposure SHORT", f"{short_cnt}/3")

        active_weight_keys = ["w_flow", "w_book", "w_shield", "w_tox"]
        active_weights = [safe_float(tw.get(k), 1.0) for k in active_weight_keys]
        fig_w = go.Figure(go.Bar(x=active_weight_keys, y=active_weights, text=[f"{v:.3f}" for v in active_weights], textposition="outside"))
        fig_w.add_hline(y=1.0, line_dash="dot", annotation_text="baseline 1.0")
        fig_w.update_layout(height=300, template="plotly_dark", title=f"System F weights · {sel_tier} · {sel_dir}", yaxis_title="Weight", yaxis=dict(range=[0.70, 1.40]), margin=dict(l=10, r=10, t=40, b=10))
        st.plotly_chart(fig_w, use_container_width=True, key=f"v7_weights_{sel_tier}_{sel_dir}")

        st.markdown("#### Súhrn všetkých 6 izolácií")
        summary_rows = []
        for t in tiers:
            for d in DIRECTIONS:
                wdict = ((tier_weights_root.get(t) or {}).get(d) or {})
                rdict = ((tier_reject_root.get(t) or {}).get(d) or {})
                sd = safe_float(((tier_strictness_root.get(t) or {}).get(d)), 0.90)
                summary_rows.append({"Tier": t, "Direction": d, "Strictness": sd,
                    "w_flow": safe_float(wdict.get("w_flow"), 1.0), "w_book": safe_float(wdict.get("w_book"), 1.0),
                    "w_shield": safe_float(wdict.get("w_shield"), 1.0), "w_tox": safe_float(wdict.get("w_tox"), 1.0),
                    "reject_macro": safe_float(rdict.get("macro"), 0), "reject_flow": safe_float(rdict.get("flow"), 0),
                    "reject_book": safe_float(rdict.get("book"), 0), "reject_shield": safe_float(rdict.get("shield"), 0),
                    "reject_tox": safe_float(rdict.get("tox"), 0)})
        st.dataframe(pd.DataFrame(summary_rows), hide_index=True, use_container_width=True)

        st.markdown("#### Existujúce micro_weights")
        micro_weights = vault_state.get("micro_weights") or {}
        if isinstance(micro_weights, dict) and micro_weights:
            st.dataframe(pd.DataFrame([{"metric": k, "value": v} for k, v in micro_weights.items()]), hide_index=True, use_container_width=True)
        else:
            st.caption("micro_weights v state_json zatiaľ nie sú dostupné.")
        st.caption("Read-only System F telemetria z bot_vault_state.state_json; Supabase schéma sa nemení.")

    with tab_mmr:
        TIER_LABELS_MMR = {
            "TIER_1_BTC": "Tier 1 — BTC Makrokotva",
            "TIER_2_ELITE": "Tier 2 — Elitné Alty (≥10M)",
            "TIER_3_LOWCAP": "Tier 3 — Low-Cap (3.5–9.99M)",
        }
        mmr_choices = [f"{t} | {d}" for t in TIER_LABELS_MMR for d in DIRECTIONS] + ["Agregovaný súhrn (DB)"]
        mmr_sel = st.radio("MMR izolovaný tensor", mmr_choices, horizontal=True, key="v7_mmr_selector")
        if mmr_sel == "Agregovaný súhrn (DB)":
            if not ai_df.empty and {"regime_x", "regime_y"}.issubset(set(ai_df.columns)):
                x_map = {1: "BEAR", 2: "SIDE", 3: "BULL"}; y_map = {1: "LOW_VOL", 2: "MID_VOL", 3: "HIGH_VOL"}
                tmp = ai_df.copy()
                tmp["rx"] = tmp["regime_x"].map(lambda v: x_map.get(int(v), str(v)))
                tmp["ry"] = tmp["regime_y"].map(lambda v: y_map.get(int(v), str(v)))
                if "win_rate" in tmp.columns:
                    piv = tmp.pivot_table(index="ry", columns="rx", values="win_rate", aggfunc="mean").reindex(index=["LOW_VOL", "MID_VOL", "HIGH_VOL"], columns=["BEAR", "SIDE", "BULL"])
                    st.markdown("**Win Rate (%) — DB agregát**"); st.dataframe(piv.round(1), use_container_width=True)
                if "total_trades" in tmp.columns:
                    piv = tmp.pivot_table(index="ry", columns="rx", values="total_trades", aggfunc="sum").reindex(index=["LOW_VOL", "MID_VOL", "HIGH_VOL"], columns=["BEAR", "SIDE", "BULL"])
                    st.markdown("**Total Trades — DB agregát**"); st.dataframe(piv.fillna(0).astype(int), use_container_width=True)
                st.markdown("**Detailed Cell Table — DB agregát**")
                cols = [c for c in ["rx", "ry", "win_rate", "avg_win_r", "avg_loss_r", "sl_modifier", "tp_modifier", "total_trades"] if c in tmp.columns]
                st.dataframe(tmp[cols], hide_index=True, use_container_width=True)
            else: st.info("AI Matica DB ešte nemá dostupné agregované dáta.")
        else:
            sel_tier, sel_dir = mmr_sel.split(" | ", 1)
            tensor = ((vault_state.get("mmr_tensors") or {}).get(sel_tier) or {}).get(sel_dir) or {}
            rows = []
            rx_labels = {1: "BEAR", 2: "SIDE", 3: "BULL"}; ry_labels = {1: "LOW_VOL", 2: "MID_VOL", 3: "HIGH_VOL"}
            for ry in range(1, 4):
                for rx in range(1, 4):
                    cell = tensor.get(f"{rx}_{ry}", {}) if isinstance(tensor, dict) else {}
                    if not isinstance(cell, dict): cell = {}
                    rows.append({"rx": rx_labels[rx], "ry": ry_labels[ry], "win_rate": safe_float(cell.get("win_rate"), 0.0),
                        "avg_win_r": safe_float(cell.get("avg_win_r"), 0.0), "avg_loss_r": safe_float(cell.get("avg_loss_r"), 0.0),
                        "sl_modifier": safe_float(cell.get("sl_modifier"), 1.0), "tp_modifier": safe_float(cell.get("tp_modifier"), 1.0),
                        "total_trades": int(safe_float(cell.get("total_trades"), 0))})
            cell_df = pd.DataFrame(rows)
            st.markdown("**Win Rate (%)**")
            wr_pivot = cell_df.pivot(index="ry", columns="rx", values="win_rate").reindex(index=["LOW_VOL", "MID_VOL", "HIGH_VOL"], columns=["BEAR", "SIDE", "BULL"])
            st.dataframe(wr_pivot.round(1), use_container_width=True)
            st.markdown("**Total Trades**")
            n_pivot = cell_df.pivot(index="ry", columns="rx", values="total_trades").reindex(index=["LOW_VOL", "MID_VOL", "HIGH_VOL"], columns=["BEAR", "SIDE", "BULL"])
            st.dataframe(n_pivot.astype(int), use_container_width=True)
            st.markdown("**Detailed Cell Table**")
            st.dataframe(cell_df, hide_index=True, use_container_width=True)

    with tab_shadow:
        TIER_LABELS_SHADOW = {
            "TIER_1_BTC": "Tier 1 (BTC)",
            "TIER_2_ELITE": "Tier 2 (Elitné ≥10M)",
            "TIER_3_LOWCAP": "Tier 3 (Low-Cap 3.5–9.99M)",
        }
        tier_pools = vault_state.get("tier_shadow_pools") or {}
        tier_keys = list(TIER_LABELS_SHADOW)
        def _shadow_list(obj):
            if isinstance(obj, list): return obj
            if isinstance(obj, dict):
                # Flatten direction-separated pool lists, if present.
                return [item for val in obj.values() if isinstance(val, list) for item in val]
            return []
        if not tier_pools:
            legacy = vault_state.get("shadow_pool") or []
            tier_pools = {"LEGACY": legacy}
        tier_counts = {t: len(_shadow_list(tier_pools.get(t, []))) for t in tier_keys}
        m1, m2, m3 = st.columns(3)
        m1.metric("Tier 1 (BTC)", tier_counts["TIER_1_BTC"])
        m2.metric("Tier 2 (Elitné ≥10M)", tier_counts["TIER_2_ELITE"])
        m3.metric("Tier 3 (Low-Cap 3.5–9.99M)", tier_counts["TIER_3_LOWCAP"])
        shadow_filter = st.selectbox("Tier filter", ["Všetky tiery"] + tier_keys, format_func=lambda k: "Všetky tiery" if k == "Všetky tiery" else TIER_LABELS_SHADOW[k], key="v7_shadow_filter")
        selected_tiers = tier_keys if shadow_filter == "Všetky tiery" else [shadow_filter]
        now_ts = time.time(); shadow_rows = []
        for tier in selected_tiers:
            for sh in _shadow_list(tier_pools.get(tier, [])):
                if not isinstance(sh, dict): continue
                ts0 = safe_float(sh.get("ts") or sh.get("opened_ts"), 0.0)
                shadow_rows.append({"tier": tier, "symbol": sh.get("symbol"), "direction": sh.get("direction"),
                    "blocked_by": sh.get("blocked_by") or sh.get("failed_filter"),
                    "reject_price": safe_float(sh.get("reject_price") or sh.get("entry_price")),
                    "virtual_tp1": safe_float(sh.get("virtual_tp1") or sh.get("tp1_price")),
                    "virtual_sl": safe_float(sh.get("virtual_sl") or sh.get("sl_price")),
                    "cost_rate": safe_float(sh.get("cost_rate"), 0.0),
                    "age_min": round((now_ts-ts0)/60.0, 1) if ts0 > 0 else None})
        if shadow_rows:
            st.dataframe(pd.DataFrame(shadow_rows, columns=["tier", "symbol", "direction", "blocked_by", "reject_price", "virtual_tp1", "virtual_sl", "cost_rate", "age_min"]), hide_index=True, use_container_width=True)
        else:
            st.info("Shadow pools neobsahujú kandidátov pre zvolený filter.")


render_dashboard_and_telemetry()

with st.sidebar:
    render_live_sidebar_positions()

st.divider()

# -------------------------------------------------------------------------
# 3. HLAVNÁ PLOCHA: SVIEČKOVÉ GRAFY (fragment, nepreblikáva, zachováva zoom)
# -------------------------------------------------------------------------
st.subheader("📊 Živé Trhové Grafy & Exekučné Hladiny")

chart_interval = ("10s" if timeframe == "1m" else "20s") if live_mode else None


@st.fragment(run_every=chart_interval)
def render_live_charts_section(tf: str):
    active_windows = load_active_windows()

    for wid in (1, 2, 3, 4, 5, 6):
        w = active_windows.get(wid, {})
        status = w.get("status", "WAITING")
        sym = w.get("symbol") if status == "ACTIVE" else None

        # Graf a hladiny SA KRESLIA LEN PRI AKTÍVNOM OBCHODE
        # (žiadny predvolený BTC graf, keď okno 1 čaká)
        if status != "ACTIVE" or not sym:
            with st.expander(f"{window_titles[wid]} — čaká na signál", expanded=False):
                st.caption("Žiadny aktívny obchod v tomto okne. Graf sa zobrazí po otvorení pozície z Cloudu 1.")
            continue

        st.markdown(f"### {window_titles[wid]} — `{sym}`")

        df_k = fetch_visual_klines(sym, tf)
        if df_k.empty:
            st.warning(f"Nepodarilo sa načítať sviečky pre {sym} z OKX API.")
            continue

        fig = go.Figure(data=[go.Candlestick(
            x=df_k["time"], open=df_k["o"], high=df_k["h"], low=df_k["l"], close=df_k["c"],
            name=sym
        )])

        # --- Y-rozsah: prioritne okolo ceny (sviečky + entry/TP/SL/DCA), aby graf nebol stlačený ---
        price_low = float(df_k["l"].min())
        price_high = float(df_k["h"].max())

        # Vykreslenie živých obchodných hladín z Cloudu 1
        if status == "ACTIVE":
            entry = safe_float(w.get("entry_price"))
            tp1 = safe_float(w.get("tp1_price"))
            tp2 = safe_float(w.get("tp2_price"))
            tp3 = safe_float(w.get("tp3_price"))
            sl = safe_float(w.get("sl_tsl_price"))

            # Horizontálna ENTRY (cena)
            fig.add_hline(y=entry, line_color="white", line_width=2, annotation_text="ENTRY")

            # TP1 / TP2 – zelené, ak už hitnuté tak hrubšie / iný štýl
            tp1_hit = bool(w.get("tp1_hit", False))
            tp2_hit = bool(w.get("tp2_hit", False))

            fig.add_hline(
                y=tp1, line_dash="dash", line_color="#00ff88",
                line_width=3 if tp1_hit else 1.5,
                annotation_text="TP1 executed" if tp1_hit else "TP1"
            )
            fig.add_hline(
                y=tp2, line_dash="dash", line_color="#00cc66",
                line_width=3 if tp2_hit else 1.5,
                annotation_text="TP2 executed" if tp2_hit else "TP2"
            )
            fig.add_hline(y=tp3, line_dash="dash", line_color="#009944", annotation_text="TP3")

            tsl_active = bool(w.get("tsl_active", False))
            sl_txt = "TSL" if tsl_active else "SL"
            sl_color = "#ff3355"
            fig.add_hline(y=sl, line_color=sl_color, line_width=2, annotation_text=sl_txt)

            # DCA hladiny – oranžové; kreslíme iba tie s cenou > 0 (DCA3 = 0.0 nesmie deformovať os Y)
            dca1_p = safe_float(w.get("dca1_price"))
            dca2_p = safe_float(w.get("dca2_price"))
            dca3_p = safe_float(w.get("dca3_price"))
            for _lbl, _px, _filled in (
                ("DCA1", dca1_p, bool(w.get("dca1_filled", False))),
                ("DCA2", dca2_p, bool(w.get("dca2_filled", False))),
                ("DCA3", dca3_p, bool(w.get("dca3_filled", False))),
            ):
                if _px > 0:
                    fig.add_hline(
                        y=_px,
                        line_dash="solid" if _filled else "dot",
                        line_color="orange",
                        line_width=2 if _filled else 1,
                        annotation_text=f"{_lbl} executed" if _filled else _lbl
                    )

            # Rozšírenie y-range o obchodné hladiny (entry/TP/SL/DCA), nie o vzdialené walls
            level_prices = [p for p in [entry, tp1, tp2, tp3, sl, dca1_p,
                                        safe_float(w.get("dca2_price")),
                                        safe_float(w.get("dca3_price"))] if p > 0]
            if level_prices:
                price_low = min(price_low, min(level_prices))
                price_high = max(price_high, max(level_prices))

            # Biela VERTIKÁLNA čiara = moment otvorenia obchodu (z core1)
            # Priorita: opened_at → mandate_expires_at - 30min. Žiadny updated_at / prvá sviečka.
            entry_ts = None
            source = None
            raw_opened = w.get("opened_at")

            if raw_opened:
                try:
                    entry_ts = pd.to_datetime(raw_opened, utc=True)
                    source = "opened_at"
                except Exception:
                    entry_ts = None

            if entry_ts is None:
                raw_mandate = w.get("mandate_expires_at")
                if raw_mandate:
                    try:
                        mandate_ts = pd.to_datetime(raw_mandate, utc=True)
                        entry_ts = mandate_ts - pd.Timedelta(minutes=30)
                        source = "mandate-30m"
                    except Exception:
                        entry_ts = None

            if entry_ts is not None and not df_k.empty:
                if getattr(entry_ts, "tzinfo", None) is None:
                    entry_ts = entry_ts.tz_localize("UTC")
                else:
                    entry_ts = entry_ts.tz_convert("UTC")

                # Zjednoť typ s osou X (candles sú UTC datetime)
                if str(df_k["time"].dtype).startswith("datetime64"):
                    x_val = entry_ts
                else:
                    x_val = entry_ts

                t_min = df_k["time"].min()
                t_max = df_k["time"].max()
                in_range = (entry_ts >= t_min) and (entry_ts <= t_max)

                # Spoľahlivejší vline cez shape (lepšia zhoda datetime s Plotly candlestick)
                fig.add_shape(
                    type="line",
                    x0=x_val, x1=x_val,
                    y0=0, y1=1,
                    yref="paper",
                    line=dict(color="white", width=2, dash="solid"),
                )
                fig.add_annotation(
                    x=x_val,
                    y=1,
                    yref="paper",
                    text="OPEN" if in_range else "OPEN (mimo rozsahu grafu)",
                    showarrow=False,
                    yanchor="bottom",
                    font=dict(color="white", size=12),
                )
                st.caption(
                    f"Otvorené: **{entry_ts.strftime('%Y-%m-%d %H:%M:%S')} UTC**"
                    f"  | zdroj: `{source}`"
                    + ("" if in_range else "  | ⚠ čas je mimo načítaných sviečok – priblíž/oddial časovú os")
                )
            else:
                st.caption("OPEN čiara: chýba `opened_at` v active_positions (core1 musí pri otvorení zapísať čas).")

        # Padding okolo ceny, aby sviečky neboli nalepené na okraj
        pad = max((price_high - price_low) * 0.08, price_high * 0.002)
        y_min = price_low - pad
        y_max = price_high + pad

        fig.update_layout(
            height=520,
            uirevision=f"{wid}_{sym}_{tf}",   # zachová manuálny zoom/pan pri live aktualizácii
            xaxis_rangeslider_visible=False,
            template="plotly_dark",
            margin=dict(l=10, r=10, t=20, b=10),
            yaxis=dict(
                range=[y_min, y_max],
                autorange=False,          # pevný range okolo ceny – walls sú mimo, po zoom out ich uvidíš
                fixedrange=False          # používateľ môže stále zoomovať / panovať
            )
        )
        st.plotly_chart(fig, use_container_width=True, key=f"chart_w{wid}_{tf}")


render_live_charts_section(timeframe)

st.divider()
st.caption("Shark Alpha Cockpit Cloud 2 • core1_version3 MM Engine • System F telemetry • fresh equity baseline 1000 USD")
