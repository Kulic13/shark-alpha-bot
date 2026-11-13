import pickle
import base64
import time
import numpy as np
import pandas as pd
from datetime import datetime, timezone

# Zoznam vstupných premenných (Features) – prísne zhodný pre tréning aj živú inferenciu
AI_FEATURES = [
    "natr",             # 14-period Normalized ATR (%)
    "rsi",              # 14-period RSI
    "ofd",              # Order-Flow Delta (agresivita tela sviečky)
    "vpin",             # Volume-Synchronized Probability of Toxicity
    "vol_ratio",        # Aktuálny objem voči 10-bar priemeru
    "ema_dist_atr",     # Vzdialenosť od EMA20 v násobkoch ATR
    "z_us_vwap",        # Normalizovaná vzdialenosť od US Session VWAP (od 14:30 UTC)
    "rel_btc_2h",       # Relatívna výkonnosť voči BTC za posledné 2 hodiny (%)
    "market_breadth",   # Podiel elitných mincí nad svojím US VWAP (0.0 až 1.0)
    "market_dispersion" # Cross-Sectional Dispersion (rozptyl výnosov trhu)
]

# Lokálna pamäťová cache pre AI model v Streamlite (aby sa nesťahoval každé 3 minúty)
_MODEL_CACHE = {
    "model": None,
    "trained_at": None,
    "threshold": 0.65,
    "last_fetch_ts": 0.0
}


def is_us_trading_window(now_dt: datetime | None = None) -> bool:
    """
    Zlaté okno US seansy pre Okno 6: 15:00 – 20:30 UTC.
    Rešpektuje NY Open Lock (do 15:00 UTC) a zatvára nové vstupy pred ázijským útlmom.
    """
    if now_dt is None:
        now_dt = datetime.now(timezone.utc)
    # Víkendová ochrana (inštitucionálny US tok je len Po–Pia)
    if now_dt.weekday() >= 5:
        return False
    minutes_utc = now_dt.hour * 60 + now_dt.minute
    start_min = 15 * 60        # 15:00 UTC
    end_min = 20 * 60 + 30     # 20:30 UTC
    return start_min <= minutes_utc <= end_min


def compute_single_coin_features(df: pd.DataFrame) -> pd.DataFrame:
    """
    Vektorizovaný výpočet indikátorov pre jednu mincu (používa sa v tréningu aj naživo).
    Očakáva DataFrame so stĺpcami: ['ts', 'o', 'h', 'l', 'c', 'v'] zoradený od najstaršej sviečky.
    """
    data = df.copy()
    for col in ("o", "h", "l", "c", "v"):
        data[col] = data[col].astype(float)
    data["ts"] = data["ts"].astype(np.int64)

    # 1. NATR (14)
    prev_c = data["c"].shift(1)
    tr = pd.concat([
        data["h"] - data["l"],
        (data["h"] - prev_c).abs(),
        (data["l"] - prev_c).abs()
    ], axis=1).max(axis=1)
    atr_14 = tr.rolling(14).mean()
    data["natr"] = (atr_14 / data["c"].replace(0, np.nan)) * 100.0

    # 2. RSI (14)
    delta = data["c"].diff()
    gain = delta.clip(lower=0).rolling(14).mean()
    loss = (-delta.clip(upper=0)).rolling(14).mean()
    rs = gain / loss.replace(0, 1e-9)
    data["rsi"] = 100.0 - (100.0 / (1.0 + rs))

    # 3. OFD (Order-Flow Delta aproximácia z 5 sviečok)
    rng = (data["h"] - data["l"]).replace(0, 1e-9)
    body_frac = ((data["c"] - data["o"]).abs() / rng).clip(0.0, 1.0)
    buyer_ratio = np.where(data["c"] >= data["o"], 0.5 + 0.5 * body_frac, 0.5 - 0.5 * body_frac)
    buy_vol = (data["v"] * buyer_ratio).rolling(5).sum()
    sell_vol = (data["v"] * (1.0 - buyer_ratio)).rolling(5).sum().replace(0, 1e-9)
    data["ofd"] = buy_vol / sell_vol

    # 4. VPIN (Toxicita toku za 10 sviečok)
    c_ratio = ((data["c"] - data["l"]) / rng).clip(0.0, 1.0)
    b_v = data["v"] * c_ratio
    s_v = data["v"] * (1.0 - c_ratio)
    total_v = (b_v + s_v).rolling(10).sum().replace(0, 1e-9)
    data["vpin"] = (b_v - s_v).abs().rolling(10).sum() / total_v

    # 5. Volume Ratio
    avg_vol = data["v"].rolling(10).mean().replace(0, 1e-9)
    data["vol_ratio"] = data["v"] / avg_vol

    # 6. EMA20 Distance v násobkoch ATR
    ema_20 = data["c"].ewm(span=20, adjust=False).mean()
    data["ema_dist_atr"] = (data["c"] - ema_20) / atr_14.replace(0, 1e-9)

    # 7. US Session VWAP Anchor (reset každý deň o 14:30 UTC)
    dt_utc = pd.to_datetime(data["ts"], unit="ms", utc=True)
    # Posunieme čas o 14h 30m dozadu, aby sa "session_day" lámal presne o 14:30 UTC
    session_day = (dt_utc - pd.Timedelta(hours=14, minutes=30)).dt.date
    typ_price = (data["h"] + data["l"] + data["c"]) / 3.0
    pv = typ_price * data["v"]

    cum_pv = pv.groupby(session_day).cumsum()
    cum_vol = data["v"].groupby(session_day).cumsum().replace(0, 1e-9)
    us_vwap = cum_pv / cum_vol

    vwap_pct_diff = ((data["c"] - us_vwap) / us_vwap.replace(0, 1e-9)) * 100.0
    data["z_us_vwap"] = vwap_pct_diff / data["natr"].replace(0, 1e-9)

    # 8. 2-hodinový výnos (8 × 15m sviečok) pre výpočet relatívnej sily a rozptylu
    data["ret_2h"] = data["c"].pct_change(8) * 100.0

    return data


def attach_institutional_context(coins_dict: dict[str, pd.DataFrame], btc_df: pd.DataFrame) -> dict[str, pd.DataFrame]:
    """
    Spojí dáta všetkých elitných mincí s Bitcoinom a vypočíta makro-náladu trhu:
    - rel_btc_2h: Výkonnosť mince oproti Bitcoinu za posledné 2 hodiny
    - market_breadth: Šírka trhu (koľko % elitných mincí je nad svojím US VWAP)
    - market_dispersion: Prierezový rozptyl výnosov (smerodajná odchýlka)
    """
    if btc_df is None or btc_df.empty or not coins_dict:
        return {}

    btc_feat = compute_single_coin_features(btc_df)[["ts", "ret_2h"]].rename(columns={"ret_2h": "btc_ret_2h"})

    processed = {}
    vwap_above_series = []
    ret_2h_series = []

    for sym, df in coins_dict.items():
        if sym == "BTCUSDT" or df is None or len(df) < 30:
            continue
        feat_df = compute_single_coin_features(df)
        feat_df = pd.merge(feat_df, btc_feat, on="ts", how="left")
        feat_df["btc_ret_2h"] = feat_df["btc_ret_2h"].ffill().fillna(0.0)
        feat_df["rel_btc_2h"] = feat_df["ret_2h"] - feat_df["btc_ret_2h"]

        s_idx = feat_df.set_index("ts")
        vwap_above_series.append((s_idx["z_us_vwap"] > 0).astype(float))
        ret_2h_series.append(s_idx["ret_2h"])
        processed[sym] = feat_df

    if not processed:
        return {}

    # Prierezová agregácia naprieč celým trhom pre každú časovú pečiatku (ts)
    breadth_df = pd.concat(vwap_above_series, axis=1)
    market_breadth = breadth_df.mean(axis=1).rename("market_breadth")

    disp_df = pd.concat(ret_2h_series, axis=1)
    market_dispersion = disp_df.std(axis=1).fillna(0.0).rename("market_dispersion")

    ctx_df = pd.concat([market_breadth, market_dispersion], axis=1).reset_index()

    final_dict = {}
    for sym, feat_df in processed.items():
        merged = pd.merge(feat_df, ctx_df, on="ts", how="left")
        merged["market_breadth"] = merged["market_breadth"].ffill().fillna(0.5)
        merged["market_dispersion"] = merged["market_dispersion"].ffill().fillna(0.0)
        final_dict[sym] = merged

    return final_dict


def load_cloud_ai_model(supabase_client, force_refresh: bool = False):
    """
    Načíta natrénovaný XGBoost model z tabuľky 'ai_models' v Supabase.
    Používa 15-minútovú pamäťovú cache, aby nezaťažoval databázu pri každom cykle.
    """
    now = time.time()
    if not force_refresh and _MODEL_CACHE["model"] is not None and (now - _MODEL_CACHE["last_fetch_ts"]) < 900:
        return _MODEL_CACHE["model"], _MODEL_CACHE["threshold"]

    try:
        res = supabase_client.table("ai_models").select("model_base64, trained_at, accuracy").eq("id", 1).execute()
        if res.data and len(res.data) > 0:
            row = res.data[0]
            trained_at = row.get("trained_at")
            if trained_at != _MODEL_CACHE["trained_at"] or _MODEL_CACHE["model"] is None:
                raw_bytes = base64.b64decode(row["model_base64"])
                model = pickle.loads(raw_bytes)
                _MODEL_CACHE["model"] = model
                _MODEL_CACHE["trained_at"] = trained_at
            _MODEL_CACHE["last_fetch_ts"] = now
    except Exception:
        pass

    return _MODEL_CACHE["model"], _MODEL_CACHE["threshold"]
