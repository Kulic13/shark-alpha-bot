import streamlit as st
import httpx
import numpy as np
import pandas as pd
import plotly.graph_objects as go
from datetime import datetime, timezone
from supabase import create_client, Client

st.set_page_config(page_title="Shark Alpha Cockpit (Cloud 2)", layout="wide")

BINANCE_FAPI = "https://binance.com"
BINANCE_EAPI = "https://binance.com"

@st.cache_resource
def init_supabase() -> Client:
    return create_client(st.secrets["SUPABASE_URL"], st.secrets["SUPABASE_KEY"])

supabase = init_supabase()

# =========================================================================
# BLOK 2: OPČNÉ STENY A MAX PAIN (12h Cache + Piatkový Hard-Reset 00:01 UTC)
# =========================================================================
@st.cache_data(ttl=43200)
def fetch_btc_options_walls(day_epoch_key: str) -> dict:
    try:
        with httpx.Client(timeout=8.0) as client:
            res = client.get(f"{BINANCE_EAPI}/eapi/v1/ticker")
            data = res.json()
        calls, puts = {}, {}
        for item in data:
            sym = item.get("symbol", "")
            if not sym.startswith("BTC-"):
                continue
            parts = sym.split("-")
            if len(parts) < 4:
                continue
            strike = float(parts[2])
            opt_type = parts[3]
            oi = float(item.get("openInterest", 0))
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
# KANÁL 2: ODĽAHČENÉ SŤAHOVANIE SVIEČOK PRE VIZUALIZÁCIU (Max 24 / 30)
# =========================================================================
@st.cache_data(ttl=10)
def fetch_visual_klines(symbol: str, interval: str) -> pd.DataFrame:
    limit = 24 if interval == "15m" else 30
    try:
        with httpx.Client(timeout=5.0) as client:
            res = client.get(f"{BINANCE_FAPI}/fapi/v1/klines", params={"symbol": symbol, "interval": interval, "limit": limit})
            raw = res.json()
        df = pd.DataFrame(raw, columns=["ts", "o", "h", "l", "c", "v", "ct", "qv", "tr", "tb", "tq", "ig"])
        df["time"] = pd.to_datetime(df["ts"], unit="ms")
        for col in ["o", "h", "l", "c"]:
            df[col] = df[col].astype(float)
        return df
    except Exception:
        return pd.DataFrame()

# =========================================================================
# BLOK 7: VIZUÁLNY KOKPIT (HORNÝ DASHBOARD, SIDEBAR & 4 VEĽKÉ GRAFY)
# =========================================================================
st.title("🦈 Shark Alpha Institutional Cockpit (Cloud 2)")

# Načítanie dát zo Supabase
pos_res = supabase.table("active_positions").select("*").order("window_id").execute()
active_windows = {r["window_id"]: r for r in pos_res.data} if pos_res.data else {}

hist_res = supabase.table("trade_history").select("*").order("id", desc=False).execute()
history_df = pd.DataFrame(hist_res.data) if hist_res.data else pd.DataFrame()

ai_res = supabase.table("ai_learning_matrix").select("*").order("total_trades", desc=True).execute()
ai_df = pd.DataFrame(ai_res.data) if ai_res.data else pd.DataFrame()

# -------------------------------------------------------------------------
# 1. HORNÁ ČASŤ OBRAZOVKY: DYNAMICKÝ EQUITY DASHBOARD & AI MATICA VÝKONU
# -------------------------------------------------------------------------
top_col1, top_col2 = st.columns([1.2, 1.0])

with top_col1:
    st.subheader("📈 Dynamický Equity Dashboard")
    if not history_df.empty:
        curr_bal = float(history_df["current_balance"].iloc[-1])
        total_pnl = curr_bal - 100.0
        win_trades = len(history_df[history_df["pnl_usd"] > 0])
        wr_total = (win_trades / len(history_df)) * 100.0

        m1, m2, m3 = st.columns(3)
        m1.metric("Aktuálny Kapitál", f"${curr_bal:.2f}", f"{total_pnl:+.2f} USD")
        m2.metric("Celková Úspešnosť", f"{wr_total:.1f} %", f"{len(history_df)} obchodov")
        m3.metric("Nerealizované PnL", f"${sum(w.get('pnl_usd', 0) for w in active_windows.values()):+.2f}")

        fig_eq = go.Figure()
        fig_eq.add_trace(go.Scatter(
            x=history_df["closed_at"], y=history_df["current_balance"],
            mode="lines+markers", name="Equity ($)", line=dict(color="#00ffcc", width=3)
        ))
        fig_eq.update_layout(height=260, margin=dict(l=10, r=10, t=10, b=10), template="plotly_dark")
        st.plotly_chart(fig_eq, use_container_width=True)
    else:
        st.metric("Aktuálny Kapitál", "$100.00", "Čaká na prvý uzavretý obchod")
        st.info("Zatiaľ neboli uzavreté žiadne obchody. Equity krivka sa vykreslí automaticky po prvom trejde.")

with top_col2:
    st.subheader("🧠 Matrica Výkonu a Učenia AI")
    if not ai_df.empty:
        display_cols = ["symbol", "long_futures_wr", "short_futures_wr", "spot_regime_wr", "funding_reversal_wr", "sl_modifier", "tp_modifier", "total_trades"]
        st.dataframe(ai_df[display_cols], height=310, use_container_width=True, hide_index=True)
    else:
        st.info("AI Matica (`ai_learning_matrix`) je pripravená a čaká na prvé dáta z Cloudu 1.")
# -------------------------------------------------------------------------
# 2. ĽAVÝ SIDEBAR: 4 OPERAČNÉ KARTY POZÍCIÍ (BTC, ŠAMPIÓN A, B, C)
# -------------------------------------------------------------------------
st.sidebar.header("🎛️ Ovládanie & Živé Pozície")
timeframe = st.sidebar.radio("Časový rámec grafov:", ["15m", "1m"], horizontal=True)
if st.sidebar.button("🔄 Obnoviť dáta Kokpitu"):
    st.rerun()

st.sidebar.divider()

window_titles = {
    1: "1️⃣ Makrokotva BTC",
    2: "2️⃣ Šampión A (Elitný ≥10M)",
    3: "3️⃣ Šampión B (Nízko-likv. 5-9M)",
    4: "4️⃣ Šampión C (Pump & Dump)"
}

for wid in:
    w = active_windows.get(wid, {})
    status = w.get("status", "WAITING")
    sym = w.get("symbol") or ("BTCUSDT" if wid == 1 else "HĽADÁM...")
    with st.sidebar.expander(f"{window_titles[wid]} | {sym}", expanded=True):
        if status == "ACTIVE":
            dir_icon = "🟢 LONG" if w.get("direction") == "LONG" else "🔴 SHORT"
            st.markdown(f"**Smer:** {dir_icon} | **Režim:** {w.get('regime')} ({w.get('leverage')}x)")
            st.markdown(f"**Entry Price:** `{w.get('entry_price', 0):.5f}`")
            st.markdown(f"**TP1:** `{w.get('tp1_price', 0):.5f}` | **TP2:** `{w.get('tp2_price', 0):.5f}`")
            st.markdown(f"**TP3 (Liq):** `{w.get('tp3_price', 0):.5f}`")
            sl_label = "TSL (Aktívny)" if w.get("tsl_active") else "Hard SL"
            st.markdown(f"**{sl_label}:** `{w.get('sl_tsl_price', 0):.5f}`")
            pnl_u = w.get("pnl_usd", 0.0)
            pnl_p = w.get("pnl_percent", 0.0)
            st.metric("Live PnL", f"${pnl_u:+.3f} USD", f"{pnl_p:+.2f} %")
        else:
            st.caption("Stav: Čaká na vhodný signál z Cloudu 1...")

# -------------------------------------------------------------------------
# 3. HLAVNÁ PLOCHA: 4 VEĽKÉ SVIEČKOVÉ GRAFY VERTIKÁLNE POD SEBOU
# -------------------------------------------------------------------------
st.subheader("📊 Živé Trhové Grafy & Exekučné Hladiny")
btc_options = get_btc_options_with_friday_reset()

for wid in:
    w = active_windows.get(wid, {})
    status = w.get("status", "WAITING")
    sym = w.get("symbol") or ("BTCUSDT" if wid == 1 else None)

    st.markdown(f"### {window_titles[wid]} — `{sym if sym else 'SKENUJEM TRH...'}`")
    if not sym:
        st.info("Skener v Cloude 1 momentálne hľadá najlepšiu príležitosť pre toto okno.")
        continue

    df_k = fetch_visual_klines(sym, timeframe)
    if df_k.empty:
        st.warning(f"Nepodarilo sa načítať sviečky pre {sym}.")
        continue

    fig = go.Figure(data=[go.Candlestick(
        x=df_k["time"], open=df_k["o"], high=df_k["h"], low=df_k["l"], close=df_k["c"],
        name=sym
    )])

    # Vykreslenie opčných stien pre BTC (Okno 1)
    if wid == 1 and btc_options:
        fig.add_hline(y=btc_options["call_wall"], line_dash="dot", line_color="cyan", annotation_text="Call Wall")
        fig.add_hline(y=btc_options["put_wall"], line_dash="dot", line_color="magenta", annotation_text="Put Wall")
        fig.add_hline(y=btc_options["max_pain"], line_dash="dash", line_color="yellow", annotation_text="Max Pain")

    # Vykreslenie živých obchodných hladín z Cloudu 1
    if status == "ACTIVE":
        fig.add_hline(y=w["entry_price"], line_color="white", line_width=2, annotation_text="ENTRY")
        fig.add_hline(y=w["tp1_price"], line_dash="dash", line_color="#00ff88", annotation_text="TP1")
        fig.add_hline(y=w["tp2_price"], line_dash="dash", line_color="#00cc66", annotation_text="TP2")
        fig.add_hline(y=w["tp3_price"], line_dash="dash", line_color="#009944", annotation_text="TP3")
        sl_txt = "TSL" if w.get("tsl_active") else "SL"
        fig.add_hline(y=w["sl_tsl_price"], line_color="#ff3355", line_width=2, annotation_text=sl_txt)

        if w.get("dca1_price", 0) > 0:
            fig.add_hline(y=w["dca1_price"], line_dash="dot", line_color="orange", annotation_text="DCA1")
            fig.add_hline(y=w["dca2_price"], line_dash="dot", line_color="orange", annotation_text="DCA2")
            fig.add_hline(y=w["dca3_price"], line_dash="dot", line_color="orange", annotation_text="DCA3")

    fig.update_layout(
        height=520,
        xaxis_rangeslider_visible=False,
        template="plotly_dark",
        margin=dict(l=10, r=10, t=20, b=10)
    )
    st.plotly_chart(fig, use_container_width=True)


st.divider()
