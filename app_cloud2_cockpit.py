import streamlit as st
import httpx
import numpy as np
import pandas as pd
import plotly.graph_objects as go
from datetime import datetime, timezone
import time
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
# BLOK 2: OPČNÉ STENY A MAX PAIN (OKX Options API + Piatkový Hard-Reset)
# =========================================================================
@st.cache_data(ttl=43200)
def fetch_btc_options_walls(day_epoch_key: str) -> dict:
    try:
        with httpx.Client(timeout=10.0) as client:
            res = client.get(
                f"{OKX_API}/api/v5/public/open-interest",
                params={"instType": "OPTION", "uly": "BTC-USD"}
            )
            res.raise_for_status()
            payload = res.json()

        data = payload.get("data", [])
        calls, puts = {}, {}

        for item in data:
            inst_id = item.get("instId", "")
            # Formát OKX opcie: BTC-USD-261108-70000-C
            parts = inst_id.split("-")
            if len(parts) < 5:
                continue
            try:
                strike = float(parts[3])
            except ValueError:
                continue
            opt_type = parts[4]
            # Preferujeme oiCcy ak existuje, inak oi (počet kontraktov)
            oi = float(item.get("oiCcy") or item.get("oi") or 0)
            if oi <= 0:
                continue

            if opt_type == "C":
                calls[strike] = calls.get(strike, 0.0) + oi
            elif opt_type == "P":
                puts[strike] = puts.get(strike, 0.0) + oi

        if not calls or not puts:
            return {}

        call_wall = max(calls, key=calls.get)
        put_wall = max(puts, key=puts.get)

        strikes = sorted(list(set(list(calls.keys()) + list(puts.keys()))))
        s_arr = np.array(strikes)
        c_oi = np.array([calls.get(s, 0.0) for s in strikes])
        p_oi = np.array([puts.get(s, 0.0) for s in strikes])

        # Vektorový výpočet Max Pain
        pain_matrix = np.maximum(0, s_arr[:, None] - s_arr[None, :]) * c_oi[None, :] + \
                      np.maximum(0, s_arr[None, :] - s_arr[:, None]) * p_oi[None, :]
        max_pain = float(s_arr[np.argmin(pain_matrix.sum(axis=1))])

        return {"call_wall": call_wall, "put_wall": put_wall, "max_pain": max_pain}
    except Exception:
        return {}

def get_btc_options_with_friday_reset() -> dict:
    now_utc = datetime.now(timezone.utc)
    # Piatok (weekday == 4) po 00:01 UTC zmaže hladiny v deň expirácie
    if now_utc.weekday() == 4 and (now_utc.hour > 0 or now_utc.minute >= 1):
        return {}
    # Unikátny kľúč pre 12h blok a týždeň
    half_day = "AM" if now_utc.hour < 12 else "PM"
    epoch_key = f"{now_utc.strftime('%Y-%W-%d')}-{half_day}"
    return fetch_btc_options_walls(epoch_key)

# =========================================================================
# KANÁL 2: ODĽAHČENÉ SŤAHOVANIE SVIEČOK PRE VIZUALIZÁCIU (OKX API)
# =========================================================================
@st.cache_data(ttl=10)
def fetch_visual_klines(symbol: str, interval: str) -> pd.DataFrame:
    # Viac histórie pre lepší prehľad (pôvodne 24/30)
    limit = 60 if interval == "15m" else 90
    inst_id = symbol_to_okx_swap(symbol)
    try:
        with httpx.Client(timeout=8.0) as client:
            res = client.get(
                f"{OKX_API}/api/v5/market/candles",
                params={"instId": inst_id, "bar": interval, "limit": str(limit)}
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
        }
        reason_counts = reasons_all.replace("", "UNKNOWN").value_counts()
        for reason, cnt in reason_counts.items():
            label = reason_labels.get(str(reason), str(reason))
            pct = (cnt / n_all) * 100.0
            st.write(f"{label}: **{cnt}** ({pct:.0f}%)")


# =========================================================================
# BLOK 7: VIZUÁLNY KOKPIT (HORNÝ DASHBOARD, SIDEBAR & 4 VEĽKÉ GRAFY)
# =========================================================================
st.title("🦈 Shark Alpha Institutional Cockpit (Cloud 2)")

# Načítanie dát zo Supabase s error handlingom
try:
    pos_res = supabase.table("active_positions").select("*").order("window_id").execute()
    active_windows = {r["window_id"]: r for r in pos_res.data} if pos_res.data else {}
except Exception as e:
    st.error(f"Chyba načítania active_positions: {e}")
    active_windows = {}

try:
    hist_res = supabase.table("trade_history").select("*").order("id", desc=False).execute()
    history_df = pd.DataFrame(hist_res.data) if hist_res.data else pd.DataFrame()
except Exception as e:
    st.error(f"Chyba načítania trade_history: {e}")
    history_df = pd.DataFrame()

try:
    ai_res = supabase.table("ai_mmr_matrix").select("*").order("total_trades", desc=True).execute()
    ai_df = pd.DataFrame(ai_res.data) if ai_res.data else pd.DataFrame()
except Exception as e:
    st.error(f"Chyba načítania ai_mmr_matrix: {e}")
    ai_df = pd.DataFrame()

# Live vault JSONB (System F telemetry source – no extra columns)
vault_state = {}
try:
    vs_res = supabase.table("bot_vault_state").select("state_json").eq("id", 1).execute()
    if vs_res.data:
        raw = vs_res.data[0].get("state_json", {})
        if isinstance(raw, str):
            import json
            vault_state = json.loads(raw) or {}
        elif isinstance(raw, dict):
            vault_state = raw
except Exception as e:
    st.caption(f"bot_vault_state nedostupné: {e}")
    vault_state = {}

# -------------------------------------------------------------------------
# 1. HORNÁ ČASŤ OBRAZOVKY: DYNAMICKÝ EQUITY DASHBOARD & AI MATICA VÝKONU
# -------------------------------------------------------------------------
top_col1, top_col2 = st.columns([1.2, 1.0])

with top_col1:
    st.subheader("📈 Dynamický Equity Dashboard")
    # --- Kapitál: po SQL wipe nespoliehať sa len na vault.balance (býva starý/seknutý) ---
    # Zdroj pravdy v cockpite = EQUITY_BASELINE + Σ realized pnl z trade_history + unrealized
    vault_bal = safe_float(vault_state.get("balance"), EQUITY_BASELINE)

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

    if not history_df.empty:
        # Win/fail len z finálnych close (nie TP1/TP2 scale-out partial riadky)
        if "exit_reason" in history_df.columns:
            _reasons = history_df["exit_reason"].fillna("").astype(str)
            _final = ~_reasons.isin(["TP1_SCALE_OUT", "TP2_SCALE_OUT"])
            _pdf = history_df.loc[_final]
        else:
            _pdf = history_df
        if "pnl_usd" in _pdf.columns and len(_pdf) > 0:
            win_trades = int((_pdf["pnl_usd"] > 0).sum())
            fail_trades = int((_pdf["pnl_usd"] <= 0).sum())
            total_trades = len(_pdf)
        else:
            win_trades = len(history_df[history_df.get("pnl_usd", pd.Series(dtype=float)) > 0]) if "pnl_usd" in history_df.columns else 0
            fail_trades = len(history_df[history_df.get("pnl_usd", pd.Series(dtype=float)) <= 0]) if "pnl_usd" in history_df.columns else 0
            total_trades = len(history_df)
        wr_total = (win_trades / total_trades) * 100.0 if total_trades > 0 else 0.0

        m1, m2, m3 = st.columns(3)
        m1.metric("Aktuálny Kapitál", f"${curr_bal:.2f}", f"{total_pnl:+.2f} USD")
        m1.caption(
            f"baseline {EQUITY_BASELINE:.0f} + realized {realized_pnl:+.2f} + unrealized {unrealized:+.2f}"
            + (f"  | vault: ${vault_bal:.2f}" if abs(vault_bal - curr_bal) > 0.5 else "")
        )
        m2.metric("Celková Úspešnosť", f"{wr_total:.1f} %", f"{total_trades} obchodov")
        m2.caption(f"✅ Win: **{win_trades}**  |  ❌ Fail: **{fail_trades}**")
        m3.metric("Nerealizované PnL", f"${unrealized:+.2f}")

        # ----- ROZDELENÉ ŠTATISTIKY: Okná 1–5 vs AI Okno 6 -----
        if "window_id" in history_df.columns:
            df_w15 = history_df[history_df["window_id"].astype(int) != 6].copy()
            df_w6 = history_df[history_df["window_id"].astype(int) == 6].copy()
        else:
            df_w15 = history_df.copy()
            df_w6 = history_df.iloc[0:0].copy()

        tab_w15, tab_w6 = st.tabs([
            "📊 Okná 1–5 (Adaptívne MMR)",
            "🤖 AI Okno 6 (XGBoost V3)",
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
                wins15 = int((df_w15["pnl_usd"] > 0).sum()) if "pnl_usd" in df_w15.columns else 0
                fails15 = n15 - wins15
                wr15 = (wins15 / n15) * 100.0
                pnl15 = float(df_w15["pnl_usd"].sum()) if "pnl_usd" in df_w15.columns else 0.0
                a1, a2, a3 = st.columns(3)
                a1.metric("Čistý PnL ($)", f"${pnl15:+.2f}")
                a2.metric("Win / Fail Ratio (%)", f"{wr15:.1f} %", f"{wins15} / {n15}")
                a3.metric("Počet záznamov", f"{n15}")

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
                    st.plotly_chart(fig15, use_container_width=True)
                    st.caption(f"Equity od baseline **{EQUITY_BASELINE:.0f} $** + Σ pnl (aktuálne dáta, bez starej SQL histórie balance).")
                else:
                    st.info("Okná 1–5: žiadne platné pnl_usd na vykreslenie equity.")
            else:
                st.info("Okná 1–5: Zatiaľ žiadne uzavreté obchody.")

            st.markdown("##### 📊 Sumár štatistiky — Okná 1–5")
            render_execution_breakdown(df_w15, "Okná 1–5")

        with tab_w6:
            # AI model status (id=1)
            try:
                model_res = supabase.table("ai_models").select("model_name, accuracy, trained_at").eq("id", 1).execute()
                if model_res.data and len(model_res.data) > 0:
                    mrow = model_res.data[0]
                    m_name = mrow.get("model_name") or "xgboost_us_champion_v3"
                    m_acc = mrow.get("accuracy")
                    m_trained = mrow.get("trained_at") or "—"
                    acc_txt = f"{float(m_acc) * 100:.1f}%" if m_acc is not None else "—"
                    st.caption(f"Model: **{m_name}**  |  Accuracy: **{acc_txt}**  |  Trained at: **{m_trained}**")
                else:
                    st.caption("AI model (id=1) zatiaľ nie je v `ai_models`.")
            except Exception as e:
                st.caption(f"ai_models nedostupné: {e}")

            n6 = len(df_w6)
            if n6 > 0:
                wins6 = int((df_w6["pnl_usd"] > 0).sum()) if "pnl_usd" in df_w6.columns else 0
                fails6 = n6 - wins6
                wr6 = (wins6 / n6) * 100.0
                pnl6 = float(df_w6["pnl_usd"].sum()) if "pnl_usd" in df_w6.columns else 0.0
                b1, b2, b3 = st.columns(3)
                b1.metric("AI Čistý PnL ($)", f"${pnl6:+.2f}")
                b2.metric("AI Win / Fail Ratio (%)", f"{wr6:.1f} %", f"{wins6} / {n6}")
                b3.metric("Počet AI záznamov", f"{n6}")

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
                    st.plotly_chart(fig6, use_container_width=True)
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
# 2. ĽAVÝ SIDEBAR: 4 OPERAČNÉ KARTY POZÍCIÍ (BTC, ŠAMPIÓN A, B, C)
# -------------------------------------------------------------------------
st.sidebar.header("🎛️ Ovládanie & Živé Pozície")
timeframe = st.sidebar.radio("Časový rámec grafov:", ["15m", "1m"], horizontal=True)
if st.sidebar.button("🔄 Obnoviť dáta Kokpitu"):
    st.cache_data.clear()
    st.rerun()

st.sidebar.divider()

window_titles = {
    1: "1️⃣ Makrokotva BTC",
    2: "2️⃣ Elitný ≥10M (A)",
    3: "3️⃣ Elitný ≥10M (B)",
    4: "4️⃣ Nízka likvidita 5-9.9M (A)",
    5: "5️⃣ Nízka likvidita 5-9.9M (B)",
    6: "6️⃣ AI Lovec (XGBoost V3)"
}

for wid in (1, 2, 3, 4, 5, 6):
    w = active_windows.get(wid, {})
    status = w.get("status", "WAITING")
    # Symbol len ak je obchod ACTIVE; inak placeholder
    if status == "ACTIVE" and w.get("symbol"):
        sym = w.get("symbol")
    else:
        sym = "—" if status != "ACTIVE" else (w.get("symbol") or "—")

    with st.sidebar.expander(f"{window_titles[wid]} | {sym}", expanded=(status == "ACTIVE")):
        if status == "ACTIVE":
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

            # DCA stavy + executed
            dca1_p = safe_float(w.get("dca1_price"))
            if dca1_p > 0:
                dca1_f = bool(w.get("dca1_filled", False))
                dca2_f = bool(w.get("dca2_filled", False))
                dca3_f = bool(w.get("dca3_filled", False))
                dca1_txt = f"`{dca1_p:.5f}`  ✅ executed" if dca1_f else f"`{dca1_p:.5f}`"
                dca2_txt = f"`{safe_float(w.get('dca2_price')):.5f}`  ✅ executed" if dca2_f else f"`{safe_float(w.get('dca2_price')):.5f}`"
                dca3_txt = f"`{safe_float(w.get('dca3_price')):.5f}`  ✅ executed" if dca3_f else f"`{safe_float(w.get('dca3_price')):.5f}`"
                st.markdown(f"**DCA1:** {dca1_txt}")
                st.markdown(f"**DCA2:** {dca2_txt}")
                st.markdown(f"**DCA3:** {dca3_txt}")

            # Fáza obchodu (odvodená)
            if tp2_hit:
                phase = "🎯 Fáza 3 – Trailing po TP2"
            elif tp1_hit:
                phase = "🔒 Fáza 2 – Break-even lock (TP1 executed)"
            else:
                phase = "⏳ Fáza 1 – Čaká na TP1"
            st.caption(phase)

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
        else:
            st.caption("Stav: Čaká na vhodný signál z Cloudu 1...")

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
    fw = vault_state.get("filter_weights") or {
        "macro": 1.0, "liq": 1.0, "ofd": 1.0, "shield": 1.0, "micro": 1.0, "risk": 1.0,
    }
    rs = vault_state.get("reject_stats") or {
        "macro": 0, "liq": 0, "ofd": 0, "shield": 0, "micro": 0, "risk": 0,
    }
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Strictness LONG", f"{safe_float(vault_state.get('strictness_long'), 1.0):.2f}")
    c2.metric("Strictness SHORT", f"{safe_float(vault_state.get('strictness_short'), 1.0):.2f}")
    c3.metric("Daily PnL ($)", f"{safe_float(vault_state.get('daily_pnl_usd'), 0.0):+.2f}")
    shield_on = bool(vault_state.get("shield_mode_active", False))
    scout_on = bool(vault_state.get("scout_mode", False))
    c4.metric("Shield Mode", "AKTÍVNY" if shield_on else "OFF")
    c5.metric("Scout Mode", "AKTÍVNY" if scout_on else "OFF")

    keys = ["macro", "liq", "ofd", "shield", "micro", "risk"]
    weights = [safe_float(fw.get(k), 1.0) for k in keys]
    rejects = [int(rs.get(k, 0) or 0) for k in keys]
    fig_w = go.Figure()
    fig_w.add_trace(go.Bar(
        name="filter_weight",
        x=keys, y=weights,
        marker_color="#00ffcc",
        text=[f"{w:.2f}" for w in weights],
        textposition="outside",
    ))
    fig_w.add_hline(y=1.0, line_dash="dot", line_color="white", annotation_text="baseline 1.0")
    fig_w.update_layout(
        height=280, template="plotly_dark",
        margin=dict(l=10, r=10, t=30, b=10),
        yaxis=dict(range=[0.7, 1.35], title="weight"),
        title="Váhy 6-stupňového lievika",
    )
    st.plotly_chart(fig_w, use_container_width=True)
    stats_df = pd.DataFrame({"filter": keys, "weight": weights, "reject_count": rejects})
    st.dataframe(stats_df, hide_index=True, use_container_width=True)

with tab_mmr:
    if not ai_df.empty and {"regime_x", "regime_y"}.issubset(set(ai_df.columns)):
        x_map = {1: "BEAR", 2: "SIDE", 3: "BULL"}
        y_map = {1: "LOW_VOL", 2: "MID_VOL", 3: "HIGH_VOL"}
        tmp = ai_df.copy()
        tmp["rx"] = tmp["regime_x"].map(lambda v: x_map.get(int(v), str(v)))
        tmp["ry"] = tmp["regime_y"].map(lambda v: y_map.get(int(v), str(v)))
        # win_rate pivot
        if "win_rate" in tmp.columns:
            piv_wr = tmp.pivot_table(index="ry", columns="rx", values="win_rate", aggfunc="mean")
            # ordered axes
            piv_wr = piv_wr.reindex(index=["LOW_VOL", "MID_VOL", "HIGH_VOL"], columns=["BEAR", "SIDE", "BULL"])
            st.markdown("**Win rate (%)**")
            st.dataframe(piv_wr.round(1), use_container_width=True)
        if "total_trades" in tmp.columns:
            piv_n = tmp.pivot_table(index="ry", columns="rx", values="total_trades", aggfunc="sum")
            piv_n = piv_n.reindex(index=["LOW_VOL", "MID_VOL", "HIGH_VOL"], columns=["BEAR", "SIDE", "BULL"])
            st.markdown("**Total trades**")
            st.dataframe(piv_n.fillna(0).astype(int), use_container_width=True)
        mod_cols = [c for c in ("sl_modifier", "tp_modifier") if c in tmp.columns]
        if mod_cols:
            st.markdown("**SL / TP modifiers**")
            show = tmp[["rx", "ry"] + mod_cols + (["win_rate", "total_trades"] if "win_rate" in tmp.columns else [])]
            st.dataframe(show, hide_index=True, use_container_width=True)
    else:
        st.info("AI Matica ešte neobsahuje dáta pre heatmapu režimov.")

with tab_shadow:
    pool = vault_state.get("shadow_pool") or vault_state.get("shadow_trades") or []
    if not pool:
        st.info("Shadow pool je prázdny – žiadne virtuálne blokované signály.")
    else:
        now_ts = time.time()
        rows = []
        for sh in pool:
            ts0 = safe_float(sh.get("ts") or sh.get("opened_ts"), 0.0)
            age_min = round((now_ts - ts0) / 60.0, 1) if ts0 > 0 else None
            rows.append({
                "symbol": sh.get("symbol"),
                "direction": sh.get("direction"),
                "blocked_by": sh.get("blocked_by") or sh.get("failed_filter"),
                "reject_price": safe_float(sh.get("reject_price") or sh.get("entry_price")),
                "virtual_tp1": safe_float(sh.get("virtual_tp1") or sh.get("tp1_price")),
                "virtual_sl": safe_float(sh.get("virtual_sl") or sh.get("sl_price")),
                "age_min": age_min,
            })
        st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)

st.divider()

# -------------------------------------------------------------------------
# 3. HLAVNÁ PLOCHA: 4 VEĽKÉ SVIEČKOVÉ GRAFY VERTIKÁLNE POD SEBOU
# -------------------------------------------------------------------------
st.subheader("📊 Živé Trhové Grafy & Exekučné Hladiny")
btc_options = get_btc_options_with_friday_reset()

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

    df_k = fetch_visual_klines(sym, timeframe)
    if df_k.empty:
        st.warning(f"Nepodarilo sa načítať sviečky pre {sym} z OKX API.")
        continue

    fig = go.Figure(data=[go.Candlestick(
        x=df_k["time"], open=df_k["o"], high=df_k["h"], low=df_k["l"], close=df_k["c"],
        name=sym
    )])

    # --- Y-rozsah: prioritne okolo ceny (sviečky + entry/TP/SL/DCA), aby graf nebol stlačený ---
    # Opčné steny (call/put wall) môžu byť veľmi ďaleko → nechávame ich mimo default view.
    # Po odzoomovaní (zoom out) budú stále viditeľné.
    price_low = float(df_k["l"].min())
    price_high = float(df_k["h"].max())

    # Vykreslenie opčných stien pre BTC (Okno 1) – len ako referencie, mimo auto-range
    if wid == 1 and btc_options:
        fig.add_hline(y=btc_options["call_wall"], line_dash="dot", line_color="cyan",
                      annotation_text="Call Wall", annotation_position="top left")
        fig.add_hline(y=btc_options["put_wall"], line_dash="dot", line_color="magenta",
                      annotation_text="Put Wall", annotation_position="top left")
        fig.add_hline(y=btc_options["max_pain"], line_dash="dash", line_color="yellow",
                      annotation_text="Max Pain", annotation_position="top left")

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
        fig.add_hline(y=sl, line_color="#ff3355", line_width=2, annotation_text=sl_txt)

        # DCA hladiny – oranžové, filled = plná čiara + "executed"
        dca1_p = safe_float(w.get("dca1_price"))
        if dca1_p > 0:
            dca1_f = bool(w.get("dca1_filled", False))
            dca2_f = bool(w.get("dca2_filled", False))
            dca3_f = bool(w.get("dca3_filled", False))

            fig.add_hline(
                y=dca1_p,
                line_dash="solid" if dca1_f else "dot",
                line_color="orange",
                line_width=2 if dca1_f else 1,
                annotation_text="DCA1 executed" if dca1_f else "DCA1"
            )
            fig.add_hline(
                y=safe_float(w.get("dca2_price")),
                line_dash="solid" if dca2_f else "dot",
                line_color="orange",
                line_width=2 if dca2_f else 1,
                annotation_text="DCA2 executed" if dca2_f else "DCA2"
            )
            fig.add_hline(
                y=safe_float(w.get("dca3_price")),
                line_dash="solid" if dca3_f else "dot",
                line_color="orange",
                line_width=2 if dca3_f else 1,
                annotation_text="DCA3 executed" if dca3_f else "DCA3"
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
        xaxis_rangeslider_visible=False,
        template="plotly_dark",
        margin=dict(l=10, r=10, t=20, b=10),
        yaxis=dict(
            range=[y_min, y_max],
            autorange=False,          # pevný range okolo ceny – walls sú mimo, po zoom out ich uvidíš
            fixedrange=False          # používateľ môže stále zoomovať / panovať
        )
    )
    st.plotly_chart(fig, use_container_width=True)

st.divider()
st.caption("Shark Alpha Cockpit Cloud 2 • System F telemetry • fresh equity baseline 1000 USD (Σ pnl, ignore old SQL balance) • y-range okolo ceny / OPEN v-line / DCA & TP stavy")
