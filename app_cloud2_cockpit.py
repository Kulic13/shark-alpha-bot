import streamlit as st
import httpx
import numpy as np
import pandas as pd
import plotly.graph_objects as go
from datetime import datetime, timezone
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
    ai_res = supabase.table("ai_learning_matrix").select("*").order("total_trades", desc=True).execute()
    ai_df = pd.DataFrame(ai_res.data) if ai_res.data else pd.DataFrame()
except Exception as e:
    st.error(f"Chyba načítania ai_learning_matrix: {e}")
    ai_df = pd.DataFrame()

# -------------------------------------------------------------------------
# 0. NY OPEN LOCK – vizuálny status (14:00–15:30 UTC = 15:00–16:30 SEČ)
# -------------------------------------------------------------------------
_now_utc = datetime.now(timezone.utc)
_ny_mins = _now_utc.hour * 60 + _now_utc.minute
_ny_lock_active = (14 * 60) <= _ny_mins < (15 * 60 + 30)
if _ny_lock_active:
    st.error(
        "🛑 NY OPEN LOCK AKTIVNÍ: Skener nových obchodů je dočasně uzamčen pro ochranu kapitálu. "
        "Běžící pozice jsou chráněny rozšířeným SL-Shieldem pod/nad extrémními knoty."
    )
else:
    st.caption("🟢 Core status: Skener trhu pracuje v běžném režimu (Volný trh).")

# -------------------------------------------------------------------------
# 1. HORNÁ ČASŤ OBRAZOVKY: DYNAMICKÝ EQUITY DASHBOARD & AI MATICA VÝKONU
# -------------------------------------------------------------------------
top_col1, top_col2 = st.columns([1.2, 1.0])

with top_col1:
    st.subheader("📈 Dynamický Equity Dashboard")
    if not history_df.empty:
        curr_bal = safe_float(history_df["current_balance"].iloc[-1], 100.0)
        total_pnl = curr_bal - 100.0
        # Scale-out riadky (TP1/TP2) nie sú samostatné obchody – filtrujeme ich z počítania WR
        if "exit_reason" in history_df.columns:
            final_mask = ~history_df["exit_reason"].isin(["TP1_SCALE_OUT", "TP2_SCALE_OUT"])
            final_df = history_df[final_mask]
        else:
            final_df = history_df
        total_trades = len(final_df)
        win_trades = len(final_df[final_df["pnl_usd"] > 0]) if total_trades > 0 else 0
        fail_trades = len(final_df[final_df["pnl_usd"] <= 0]) if total_trades > 0 else 0
        wr_total = (win_trades / total_trades) * 100.0 if total_trades > 0 else 0.0

        # Nerealizované PnL – IBA aktívne pozície
        unrealized = sum(
            safe_float(w.get("pnl_usd"), 0.0)
            for w in active_windows.values()
            if w.get("status") == "ACTIVE"
        )

        m1, m2, m3 = st.columns(3)
        m1.metric("Aktuálny Kapitál", f"${curr_bal:.2f}", f"{total_pnl:+.2f} USD")
        m2.metric("Celková Úspešnosť", f"{wr_total:.1f} %", f"{total_trades} obchodov")
        m2.caption(f"✅ Win: **{win_trades}**  |  ❌ Fail: **{fail_trades}**")
        m3.metric("Nerealizované PnL", f"${unrealized:+.2f}")

        # ----- SUMÁR ŠTATISTIKY: TP / DCA / EXIT REASON -----
        # Hit/DCA počty berieme z finálnych uzavretí (bez scale-out riadkov), aby sa nenafukovali
        st.markdown("##### 📊 Sumár štatistiky obchodov")
        s1, s2, s3 = st.columns(3)

        with s1:
            st.markdown("**Take Profit hits**")
            tp1_n = int(final_df["tp1_hit"].fillna(False).astype(bool).sum()) if "tp1_hit" in final_df.columns else 0
            tp2_n = int(final_df["tp2_hit"].fillna(False).astype(bool).sum()) if "tp2_hit" in final_df.columns else 0
            tp3_n = int((final_df["exit_reason"] == "TP3_LIQUIDATION_HIT").sum()) if "exit_reason" in final_df.columns else 0
            st.write(f"TP1 executed: **{tp1_n}** / {total_trades}")
            st.write(f"TP2 executed: **{tp2_n}** / {total_trades}")
            st.write(f"TP3 hit (close): **{tp3_n}** / {total_trades}")

        with s2:
            st.markdown("**DCA fills**")
            dca1_n = int(final_df["dca1_filled"].fillna(False).astype(bool).sum()) if "dca1_filled" in final_df.columns else 0
            dca2_n = int(final_df["dca2_filled"].fillna(False).astype(bool).sum()) if "dca2_filled" in final_df.columns else 0
            dca3_n = int(final_df["dca3_filled"].fillna(False).astype(bool).sum()) if "dca3_filled" in final_df.columns else 0
            st.write(f"DCA1 filled: **{dca1_n}** / {total_trades}")
            st.write(f"DCA2 filled: **{dca2_n}** / {total_trades}")
            st.write(f"DCA3 filled: **{dca3_n}** / {total_trades}")

        with s3:
            st.markdown("**Dôvod zatvorenia**")
            if "exit_reason" in history_df.columns:
                # Zobraziť všetky dôvody vrátane scale-outov (informácie o priebežných výberoch)
                reason_counts = history_df["exit_reason"].fillna("UNKNOWN").value_counts()
                reason_labels = {
                    "HARD_SL_HIT": "🔴 Hard SL",
                    "TRAILING_SL_PROFIT": "🟢 Trailing SL (zisk)",
                    "TP3_LIQUIDATION_HIT": "🎯 TP3",
                    "EARLY_ORDERFLOW_ESCAPE": "⚠️ Early OF escape",
                    "TP1_SCALE_OUT": "🟦 TP1 Scale-out 50%",
                    "TP2_SCALE_OUT": "🟦 TP2 Scale-out 30%",
                }
                hist_n = max(1, len(history_df))
                for reason, cnt in reason_counts.items():
                    label = reason_labels.get(str(reason), str(reason))
                    pct = (cnt / hist_n) * 100.0
                    st.write(f"{label}: **{cnt}** ({pct:.0f}%)")
            else:
                st.caption("exit_reason ešte nie je v histórii")

        # Equity krivka – KAŽDÝ bod vrátane TP1/TP2 scale-outov (zubovitý rast kapitálu)
        # Preferujeme closed_at, potom updated_at, inak id
        if "closed_at" in history_df.columns and history_df["closed_at"].notna().any():
            x_col = "closed_at"
        elif "updated_at" in history_df.columns and history_df["updated_at"].notna().any():
            x_col = "updated_at"
        else:
            x_col = "id"

        fig_eq = go.Figure()
        fig_eq.add_trace(go.Scatter(
            x=history_df[x_col], y=history_df["current_balance"],
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
        display_cols = ["symbol", "long_futures_wr", "short_futures_wr", "spot_regime_wr",
                        "funding_reversal_wr", "sl_modifier", "tp_modifier", "total_trades"]
        available_cols = [c for c in display_cols if c in ai_df.columns]
        st.dataframe(ai_df[available_cols], height=310, use_container_width=True, hide_index=True)
    else:
        st.info("AI Matica (`ai_learning_matrix`) je pripravená a čaká na prvé dáta z Cloudu 1.")

# -------------------------------------------------------------------------
# 2. ĽAVÝ SIDEBAR: OVLÁDANIE + REAKTÍVNE KARTY (1s fragment)
# -------------------------------------------------------------------------
st.sidebar.header("🎛️ Ovládanie & Živé Pozície")
timeframe = st.sidebar.radio("Časový rámec grafov:", ["15m", "1m"], horizontal=True)
if st.sidebar.button("🔄 Obnoviť dáta Kokpitu"):
    st.cache_data.clear()
    for _k in list(st.session_state.keys()):
        if str(_k).startswith("kline_cache_") or str(_k).startswith("kline_ts_"):
            del st.session_state[_k]
    st.rerun()

st.sidebar.divider()

window_titles = {
    1: "1️⃣ Makrokotva BTC",
    2: "2️⃣ Šampión A (Elitný ≥10M)",
    3: "3️⃣ Šampión B (Nízko-likv. 5-9M)",
    4: "4️⃣ Šampión C (Pump & Dump)"
}


@st.fragment(run_every="1s")
def render_live_position_cards():
    """Live načítanie active_positions každú 1s – TSL / marža / PnL bez refreshu grafov."""
    try:
        pos_res_live = supabase.table("active_positions").select("*").order("window_id").execute()
        live_windows = {r["window_id"]: r for r in pos_res_live.data} if pos_res_live.data else {}
    except Exception:
        live_windows = {}

    for wid in (1, 2, 3, 4):
        w = live_windows.get(wid, {})
        status = w.get("status", "WAITING")
        # Window 1 defaultne BTCUSDT aj keď WAITING
        sym = w.get("symbol") or ("BTCUSDT" if wid == 1 else "HĽADÁM...")

        with st.sidebar.expander(f"{window_titles[wid]} | {sym}", expanded=True):
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

                # Fáza obchodu (odvodená) – Fáza 1.5 = plynulý TSL medzi TP1 a TP2
                if tp2_hit:
                    phase = "🎯 Fáza 3 – Trailing po TP2"
                elif tp1_hit:
                    phase = "🔒 Fáza 1.5 – Plynulý TSL (medzi TP1 a TP2)"
                else:
                    phase = "⏳ Fáza 1 – Čaká na TP1"
                st.caption(phase)

                # Live PnL
                pnl_u = safe_float(w.get("pnl_usd"))
                pnl_p = safe_float(w.get("pnl_percent"))
                st.metric("Live PnL", f"${pnl_u:+.3f} USD", f"{pnl_p:+.2f} %")

                # Počiatočný Kelly vklad (nemenný margin_usd z otvorenia)
                kelly_m = safe_float(w.get("margin_usd"))
                act_m = safe_float(w.get("active_margin"))
                if kelly_m > 0:
                    st.markdown(f"**Počiatočný vklad (Kelly):** ${kelly_m:.2f} USD")
                else:
                    st.markdown("**Počiatočný vklad (Kelly):** _nedostupné_")
                    st.caption(
                        "Chýba stĺpec `margin_usd` v `active_positions` (alebo pozícia pred patchom). "
                        "V Supabase pridaj stĺpce a nasad' aktualizovaný Core."
                    )
                if act_m > 0:
                    st.caption(f"Aktívna marža (po scale-out): ${act_m:.2f} USD")

                # Updated_at ak existuje
                updated = w.get("updated_at")
                if updated:
                    st.caption(f"Posledný update: {updated}")
            else:
                st.caption("Stav: Čaká na vhodný signál z Cloudu 1...")


render_live_position_cards()

# -------------------------------------------------------------------------
# 3. HLAVNÁ PLOCHA: 4 VEĽKÉ SVIEČKOVÉ GRAFY (riadený refresh podľa timeframe)
# -------------------------------------------------------------------------


@st.fragment(run_every="1s")
def render_market_charts():
    """Grafy sa prekresľujú každú 1s (hladiny/TSL), ale OKX sviečky len raz za 15m / 1m."""
    import time as _time

    st.subheader("📊 Živé Trhové Grafy & Exekučné Hladiny")
    btc_options = get_btc_options_with_friday_reset()

    # Live symboly a hladiny z active_positions
    try:
        pos_res_charts = supabase.table("active_positions").select("*").order("window_id").execute()
        chart_windows = {r["window_id"]: r for r in pos_res_charts.data} if pos_res_charts.data else {}
    except Exception:
        chart_windows = active_windows

    tf = timeframe  # z outer scope (sidebar radio)
    refresh_sec = 900 if tf == "15m" else 60  # 15 min vs 1 min
    now_ts = _time.time()

    for wid in (1, 2, 3, 4):
        w = chart_windows.get(wid, {})
        status = w.get("status", "WAITING")
        sym = w.get("symbol") or ("BTCUSDT" if wid == 1 else None)

        st.markdown(f"### {window_titles[wid]} — `{sym if sym else 'SKENUJEM TRH...'}`")
        if not sym:
            st.info("Skener v Cloude 1 momentálne hľadá najlepšiu príležitosť pre toto okno.")
            continue

        cache_key = f"kline_cache_{wid}_{sym}_{tf}"
        ts_key = f"kline_ts_{wid}_{sym}_{tf}"
        last_ts = float(st.session_state.get(ts_key, 0.0))
        need_fetch = (now_ts - last_ts) >= refresh_sec or cache_key not in st.session_state

        if need_fetch:
            df_k = fetch_visual_klines(sym, tf)
            if not df_k.empty:
                st.session_state[cache_key] = df_k
                st.session_state[ts_key] = now_ts
            else:
                df_k = st.session_state.get(cache_key, pd.DataFrame())
        else:
            df_k = st.session_state.get(cache_key, pd.DataFrame())

        if df_k is None or (isinstance(df_k, pd.DataFrame) and df_k.empty):
            st.warning(f"Nepodarilo sa načítať sviečky pre {sym} z OKX API.")
            continue

        fig = go.Figure(data=[go.Candlestick(
            x=df_k["time"], open=df_k["o"], high=df_k["h"], low=df_k["l"], close=df_k["c"],
            name=sym
        )])

        # --- Y-rozsah: prioritne okolo ceny (sviečky + entry/TP/SL/DCA) ---
        price_low = float(df_k["l"].min())
        price_high = float(df_k["h"].max())

        # Vykreslenie opčných stien pre BTC (Okno 1)
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

            fig.add_hline(y=entry, line_color="white", line_width=2, annotation_text="ENTRY")

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

            level_prices = [p for p in [entry, tp1, tp2, tp3, sl, dca1_p,
                                        safe_float(w.get("dca2_price")),
                                        safe_float(w.get("dca3_price"))] if p > 0]
            if level_prices:
                price_low = min(price_low, min(level_prices))
                price_high = max(price_high, max(level_prices))

            entry_ts = None
            for key in ("opened_at", "entry_time", "created_at", "updated_at"):
                raw_ts = w.get(key)
                if raw_ts:
                    try:
                        entry_ts = pd.to_datetime(raw_ts, utc=True)
                        break
                    except Exception:
                        continue
            if entry_ts is None and not df_k.empty:
                entry_ts = df_k["time"].iloc[0]

            if entry_ts is not None:
                fig.add_vline(
                    x=entry_ts,
                    line_color="white",
                    line_width=2,
                    line_dash="solid",
                    annotation_text="OPEN",
                    annotation_position="top"
                )

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
                autorange=False,
                fixedrange=False
            )
        )
        st.plotly_chart(fig, use_container_width=True)

    next_in = max(0, int(refresh_sec - (now_ts - float(st.session_state.get(
        f"kline_ts_1_{(chart_windows.get(1, {}).get('symbol') or 'BTCUSDT')}_{tf}", now_ts
    )))))
    st.caption(
        f"Sviečky: refresh každých {refresh_sec // 60} min ({tf}) • "
        f"Hladiny/TSL: live 1s • OKX rate-limit ochrana aktívna"
    )


render_market_charts()

st.divider()
st.caption(
    "Shark Alpha Cockpit Cloud 2 • Scale-out 50/30/20 • Fáza 1.5 plynulý TSL • "
    "1s sidebar fragment • riadený refresh grafov podľa timeframe"
)
