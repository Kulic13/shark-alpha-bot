import os
import time
import pickle
import base64
import httpx
import numpy as np
import pandas as pd
import xgboost as xgb
from datetime import datetime, timezone
from supabase import create_client
from sklearn.metrics import accuracy_score
from us_market_engine import (
    AI_FEATURES,
    attach_institutional_context,
    is_us_trading_window
)

OKX_API = "https://www.okx.com"
MIN_CANDLES_HISTORY = 1400  # Anti-listing filter (~15.6 dňa histórie)
TARGET_CANDLES = 2880       # 30 dní 15m sviečok (30 * 96)
MIN_VOL_24H_USD = 10_000_000.0


def init_db():
    url = os.environ.get("SUPABASE_URL", "").strip().rstrip("/")
    if url.endswith("/rest/v1"):
        url = url[:-8]
    key = os.environ.get("SUPABASE_KEY", "").strip()
    if not url or not key:
        raise RuntimeError("Chýbajú environment premenné SUPABASE_URL alebo SUPABASE_KEY.")
    return create_client(url, key)


def get_top_liquid_symbols(client: httpx.Client, max_coins: int = 25) -> list[str]:
    """Vyberie najlikvidnejšie USDT-SWAP páry na OKX (mimo BTC)."""
    res = client.get(f"{OKX_API}/api/v5/market/tickers", params={"instType": "SWAP"})
    res.raise_for_status()
    items = res.json().get("data", [])
    candidates = []
    for it in items:
        inst_id = it.get("instId", "")
        if not inst_id.endswith("-USDT-SWAP") or inst_id == "BTC-USDT-SWAP":
            continue
        vol = float(it.get("volCcy24h") or 0.0)
        if vol >= MIN_VOL_24H_USD:
            sym = inst_id.replace("-USDT-SWAP", "USDT")
            candidates.append((sym, vol))
    candidates.sort(key=lambda x: x[1], reverse=True)
    return [c[0] for c in candidates[:max_coins]]


def fetch_history_30d(client: httpx.Client, symbol: str) -> pd.DataFrame | None:
    """
    Stiahne až 30 dní (2880 sviečok) 15m histórie z OKX.
    Ak minca nemá aspoň 1500 uzavretých sviečok, vráti None (ochrana proti novým minciam).
    """
    inst_id = symbol.replace("USDT", "-USDT-SWAP")
    all_rows = []
    after_ts = None

    while len(all_rows) < TARGET_CANDLES:
        params = {"instId": inst_id, "bar": "15m", "limit": "300"}
        if after_ts:
            params["after"] = str(after_ts)
        try:
            res = client.get(f"{OKX_API}/api/v5/market/candles", params=params)
            if res.status_code != 200 or not res.text:
                time.sleep(1.5)
                continue
            payload = res.json()
            data = payload.get("data", [])
            if not data:
                break
            all_rows.extend(data)
            oldest_row = data[-1]
            after_ts = oldest_row[0]
            time.sleep(0.25)
        except Exception:
            time.sleep(1.5)
            continue

    # Deduplikácia a zoradenie od najstaršej po najnovšiu
    seen = set()
    unique_rows = []
    for r in reversed(all_rows):
        ts_val = r[0]
        if ts_val not in seen:
            seen.add(ts_val)
            unique_rows.append(r)

    if len(unique_rows) < MIN_CANDLES_HISTORY:
        print(f"[SKIP] {symbol}: iba {len(unique_rows)} sviečok (limit je {MIN_CANDLES_HISTORY}).")
        return None

    unique_rows = unique_rows[-TARGET_CANDLES:]
    df = pd.DataFrame(unique_rows, columns=["ts", "o", "h", "l", "c", "v", "vc", "vq", "cf"])
    return df[["ts", "o", "h", "l", "c", "v"]]


def build_labels_and_weights(df: pd.DataFrame) -> pd.DataFrame:
    """
    Vytvorí cieľovú premennú (0 = WAIT, 1 = LONG, 2 = SHORT),
    vyfiltruje čisto Americkú seansu (15:00 – 20:30 UTC)
    a vypočíta exponenciálne váhy (najnovšie dni majú najvyššiu váhu).
    """
    data = df.copy()
    # Výhľad na nasledujúcich 6 sviečok (1.5 hodiny)
    future_max = data["h"].shift(-1).rolling(6, min_periods=1).max().shift(-5)
    future_min = data["l"].shift(-1).rolling(6, min_periods=1).min().shift(-5)

    # Zisk musí prekonať poplatky + aspoň 1.0 * NATR
    tp_dist = data["c"] * ((1.0 * data["natr"] / 100.0) + 0.0015)
    sl_dist = data["c"] * (1.4 * data["natr"] / 100.0)

    long_win = ((future_max - data["c"]) >= tp_dist) & ((data["c"] - future_min) < sl_dist)
    short_win = ((data["c"] - future_min) >= tp_dist) & ((future_max - data["c"]) < sl_dist)

    data["target"] = 0
    data.loc[long_win, "target"] = 1
    data.loc[short_win, "target"] = 2

    # Odstránenie koncových riadkov bez budúcnosti a NaN
    data = data.dropna(subset=AI_FEATURES + ["target"]).copy()

    # Filtrácia výhradne na US seansu (15:00 – 20:30 UTC, Po–Pia)
    dt_utc = pd.to_datetime(data["ts"], unit="ms", utc=True)
    us_mask = [is_us_trading_window(dt.to_pydatetime()) for dt in dt_utc]
    data = data[us_mask].copy()

    if data.empty:
        return data

    # Exponenciálne váhovanie (Decay): čerstvé dáta majú váhu až 2.0, staré 30-dňové 0.5
    max_ts = data["ts"].max()
    min_ts = data["ts"].min()
    span = max(1.0, float(max_ts - min_ts))
    age_norm = (data["ts"] - min_ts) / span  # 0.0 (najstaršie) -> 1.0 (najnovšie)
    data["sample_weight"] = 0.5 + 1.5 * (age_norm ** 2)

    return data


def run_daily_training():
    print("=== ŠTART DENNÉHO AI TRÉNERA (US SESSION CHAMPION) ===")
    supabase = init_db()
    headers = {"User-Agent": "SharkAlphaTrainer/3.0"}

    with httpx.Client(headers=headers, timeout=15.0) as client:
        btc_df = fetch_history_30d(client, "BTCUSDT")
        if btc_df is None or btc_df.empty:
            raise RuntimeError("Nepodarilo sa stiahnuť históriu BTCUSDT.")

        top_syms = get_top_liquid_symbols(client, max_coins=25)
        print(f"Nájdených {len(top_syms)} likvidných kandidátov. Sťahujem 30d históriu...")

        coins_dict = {}
        for sym in top_syms:
            df = fetch_history_30d(client, sym)
            if df is not None:
                coins_dict[sym] = df

    print(f"Po anti-listing filtri (>= {MIN_CANDLES_HISTORY} sviečok) ostalo {len(coins_dict)} mincí.")
    if len(coins_dict) < 5:
        raise RuntimeError("Nedostatok mincí s dostatočnou históriou na tréning.")

    # Výpočet spoločných a inštitucionálnych indikátorov
    enriched_dict = attach_institutional_context(coins_dict, btc_df)

    all_frames = []
    for sym, edf in enriched_dict.items():
        labeled = build_labels_and_weights(edf)
        if not labeled.empty:
            all_frames.append(labeled)

    full_df = pd.concat(all_frames, ignore_index=True)
    # Zoradenie striktne podľa času, aby testovacia sada bola vždy z najnovších dní
    full_df = full_df.sort_values("ts").reset_index(drop=True)

    X = full_df[AI_FEATURES]
    y = full_df["target"]
    w = full_df["sample_weight"]

    split_idx = int(len(full_df) * 0.80)
    X_train, X_test = X.iloc[:split_idx], X.iloc[split_idx:]
    y_train, y_test = y.iloc[:split_idx], y.iloc[split_idx:]
    w_train = w.iloc[:split_idx]

    print(f"Trénujem XGBoost na {len(X_train)} US-session situáciách (test na {len(X_test)})...")
    model = xgb.XGBClassifier(
        objective="multi:softprob",
        num_class=3,
        n_estimators=220,
        max_depth=4,
        learning_rate=0.035,
        subsample=0.80,
        colsample_bytree=0.75,
        reg_alpha=0.5,
        reg_lambda=1.5,
        random_state=42,
        n_jobs=-1
    )
    model.fit(X_train, y_train, sample_weight=w_train)

    preds = model.predict(X_test)
    acc = accuracy_score(y_test, preds)
    print(f"Celková presnosť na najnovších dátach: {acc * 100:.2f} %")

    # Uloženie do Supabase
    model_bytes = pickle.dumps(model)
    model_b64 = base64.b64encode(model_bytes).decode("utf-8")

    supabase.table("ai_models").upsert({
        "id": 1,
        "model_name": "xgboost_us_champion_v3",
        "model_base64": model_b64,
        "accuracy": round(float(acc) * 100.0, 2),
        "features_list": AI_FEATURES,
        "trained_at": datetime.now(timezone.utc).isoformat()
    }).execute()

    print("=== HOTOVO! Čerstvý mozog 'xgboost_us_champion_v3' uložený v Supabase. ===")


if __name__ == "__main__":
    run_daily_training()
