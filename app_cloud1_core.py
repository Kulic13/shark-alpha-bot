ÚLOHA: Oprava 5 logických konfliktov v manažmente otvorenej pozície (vyhladený 3s EMA Spoofing Escape + oprava smeru jeho učenia, striktná hierarchia Shadow OBI pod DCA2, okamžitá Break-Even ochrana po TP1 bez 300s slepoty, čerstvý Real Tape CVD pri TP3/Front-Run a oprava učenia Wall Front-Run) v priloženom súbore `cloud1_core_v2_closed_loop_Gq27jr.txt`.

VÝSTUPNÝ FORMÁT (POVINNÉ):
- Upravený kód NEVYPISUJ do chatovacieho okna. Vygeneruj a poskytni ho výhradne ako hotový `.txt` súbor na stiahnutie (`cloud1_core_v3_stable.txt`).

KRITICKÉ PRAVIDLÁ PROTI SKRACOVANIU:
1. Vygenerovaný `.txt` súbor musí obsahovať KOMPLETNÝ, 100 % spustiteľný kód celého skriptu od prvého po posledný riadok.
2. PRÍSNY ZÁKAZ SKRACOVANIA: Nikde v súbore nepouži komentáre typu `# ... existing code ...` ani vynechávky. Všetky funkcie vrátane `run_core_cycle`, `process_global_market_scanning`, `run_historical_pretrain` a `main_ui` musia ostať v plnom znení.
3. ZÁKAZ ZMENY DB SCHÉMY: Nemeň názvy tabuliek ani stĺpcov v Supabase.

ZADANIE ÚPRAV (5 PRESNE OHRANIČENÝCH BODOV):

### BOD 1: Oprava smeru učenia `spoof_sensitivity` a pridanie `wall_frontrun_used` v `update_filter_weights_from_outcome`
- Vo funkcii `update_filter_weights_from_outcome(pos: dict, is_win: bool, reason: str, vault: dict)`:
  - **Oprava Spoofing učenia (Problém 4):** Ak `"SPOOF"` v `reason_u`: predčasný únik v strate bol falošný poplach (príliš vysoká citlivosť), preto citlivosť **ZNIŽUJ**:
    `mw["spoof_sensitivity"] = float(np.clip(float(mw.get("spoof_sensitivity", 1.0)) - 0.05, 0.70, 1.40))`
  - **Oprava Wall Front-Run učenia (Problém 5):** Namiesto `if "WALL_FRONTRUN" in reason_u and is_win:` kontroluj príznak na pozícii `if pos.get("wall_frontrun_used") or "WALL_FRONTRUN" in reason_u:`:
    - Ak `is_win`: `mw["wall_frontrun_mult"] = float(np.clip(float(mw.get("wall_frontrun_mult", 3.5)) - 0.05, 2.2, 5.0))`
    - Ak `not is_win`: `mw["wall_frontrun_mult"] = float(np.clip(float(mw.get("wall_frontrun_mult", 3.5)) + 0.05, 2.2, 5.0))`

### BOD 2: Vyhladený 3s EMA Spoofing Detector bez falošných poplachov (sekcia A2 v `run_core_cycle`)
- Vo funkcii `run_core_cycle`, v sekcii `A2. SPOOFING / LIQUIDITY PULL ESCAPE`:
  - Nahraď surový 1-sekundový skok `prev_wob - curr_wob` vyhladeným EMA trendom knihy objednávok:
    ```python
    prev_wob_ema = float(pos.get("wob_ema", curr_wob))
    wob_ema = 0.65 * prev_wob_ema + 0.35 * float(curr_wob)
    pos["wob_ema"] = wob_ema
    pos["last_wob_imbalance"] = float(curr_wob)
    spoof_drop_thr = 0.65 / max(0.5, float(mw.get("spoof_sensitivity", 1.0)))
    spoof_suspect = (
        (is_long and (prev_wob_ema - curr_wob) > spoof_drop_thr and curr_wob < -0.25)
        or ((not is_long) and (curr_wob - prev_wob_ema) > spoof_drop_thr and curr_wob > 0.25)
    )
    ```
  - Zvýš minimálnu stratu pre aktiváciu Spoofing Escape zo `0.20 %` na `max(0.35, 0.35 * float(natr))` (aby šum spreadu nevyhadzoval zdravé obchody):
    `min_spoof_loss_pct = max(0.35, 0.35 * float(natr or 1.5))`
    `in_loss_spoof = (((is_long and curr_price < entry_px) or ((not is_long) and curr_price > entry_px)) and loss_pct > min_spoof_loss_pct)`
  - Ponechaj potvrdenie cez `fetch_real_tape_cvd` (`real_flow < 0.80` pre LONG, `> 1.20` pre SHORT).

### BOD 3: Prísna hierarchia `DCA2 > Shadow OBI > Hard SL` (sekcia B v `run_core_cycle`)
- Vo funkcii `run_core_cycle`, v sekcii `B. SHADOW OBI BARRIER (PAPER)`:
  - Vypočítaj ochrannú hranicu nevyplnených DCA úrovní, ktorú Shadow OBI **nikdy nesmie prekročiť**:
    - Pre `is_long`:
      - Ak `not pos.get("dca2_filled") and float(pos.get("dca2_price") or 0.0) > 0`:
        `dca_floor_px = float(pos["dca2_price"]) * 0.998`
      - Elif `not pos.get("dca1_filled") and float(pos.get("dca1_price") or 0.0) > 0`:
        `dca_floor_px = float(pos["dca1_price"]) * 0.998`
      - Else:
        `dca_floor_px = entry_px`
      - Do podmienky posunu `shadow_sl` pridaj `and shadow_sl < dca_floor_px`.
    - Pre `not is_long`:
      - Ak `not pos.get("dca2_filled") and float(pos.get("dca2_price") or 0.0) > 0`:
        `dca_ceil_px = float(pos["dca2_price"]) * 1.002`
      - Elif `not pos.get("dca1_filled") and float(pos.get("dca1_price") or 0.0) > 0`:
        `dca_ceil_px = float(pos["dca1_price"]) * 1.002`
      - Else:
        `dca_ceil_px = entry_px`
      - Do podmienky posunu `shadow_sl` pridaj `and shadow_sl > dca_ceil_px`.

### BOD 4: Okamžitá ochrana Break-Even Stop-Lossu po TP1 + Značka `wall_frontrun_used` (sekcie D a F v `run_core_cycle`)
- **V sekcii D (pri `TP1_WALL_FRONTRUN` aj `TP2_WALL_FRONTRUN`):**
  - Pridaj `pos["wall_frontrun_used"] = True`, aby `close_position` vedelo, že v tomto obchode bol použitý front-running steny.
- **V sekcii F (vyhodnotenie SL / TSL):**
  - Odstráň nebezpečné 300-sekundové ignorovanie zásahu Break-Even Stop-Lossu po TP1!
  - Ponechaj iba ochranu pre ten istý 1-sekundový tik (`if _tp1_just_hit: pass`), aby sa BE lock nevykonal v tej istej milisekunde ako TP1, no **od nasledujúcej sekundy už `be_lock` plne chráni kapitál** (zatiaľ čo posúvanie TSL v sekcii D ostáva zmrazené na 300s).

### BOD 5: Čerstvý `Real Tape CVD` pri TP3 Liquidation Climax (sekcia E v `run_core_cycle`)
- Vo funkcii `run_core_cycle`, v sekcii `E. LIQUIDATION CLIMAX`:
  - Keď je cena v radarovej zóne (`if in_radar:`), stiahni čerstvý `tape_tp3 = await fetch_real_tape_cvd(client, sym, limit=100)` a použi `live_flow_tp3 = float(tape_tp3["tape_ofd"]) if tape_tp3.get("ok") else float(ofd)` na detekciu `liquidation_climax = (is_long and live_flow_tp3 > 1.55) or (not is_long and live_flow_tp3 < 0.45)`, aby sa 3-sekundový Climax nerozhodoval podľa starého sviečkového OFD.
