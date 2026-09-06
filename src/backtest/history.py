"""
Chargement des historiques profonds pour le replay (Étape 4).

Profondeurs (replay N jours + warmup + lookback 30 j pour le percentile ATR) :
- M5 : N×288 + 300 · M15 : min(5000, N×96 + 3000) · M30 : N×48 + 120
- H1 : 1000 · H4 : 800 · D1 : 365 (plafonds settings/Deriv).
- Ticks JD10 : ~40 000 (~11 h, ≈33 jumps) — stats gelées car stationnaires.

Cache disque `data/backtest/` : le téléchargement (~15 requêtes) n'a lieu qu'avec
--refresh ; les runs de calibration relisent le cache (rapide, déterministe).
"""
from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Dict, List

log = logging.getLogger("backtest.history")


def fetch_deep(provider: Any, symbol: str, granularity: int, total: int,
               batch: int = 5000) -> List[dict]:
    """Historique profond par pagination end=<epoch> (trié, dédupliqué)."""
    chunks: List[list] = []
    have, end = 0, "latest"
    while have < total:
        want = min(batch, total - have + (1 if end == "latest" else 0))
        chunk = provider.get_candles(symbol, granularity, want, use_cache=False, end=end)
        if not chunk:
            break
        chunks.append(chunk)
        have += len(chunk)
        end = chunk[0]["epoch"] - 1
        if len(chunk) < want:
            break  # bout d'historique Deriv atteint
        time.sleep(0.2)
    by_epoch: Dict[int, dict] = {}
    for ch in chunks:
        for c in ch:
            by_epoch[c["epoch"]] = c
    return [by_epoch[e] for e in sorted(by_epoch)]


def fetch_ticks_sample(provider: Any, symbol: str, total: int = 40000,
                       batch: int = 5000) -> List[dict]:
    """Échantillon ticks profond (pagination) pour stats jumps gelées."""
    chunks: List[list] = []
    have, end = 0, "latest"
    while have < total:
        chunk = provider.get_ticks(symbol, min(batch, total - have),
                                   end, use_cache=False)
        if not chunk:
            break
        chunks.append(chunk)
        have += len(chunk)
        end = chunk[0]["epoch"] - 1
        if len(chunk) < min(batch, total - have + len(chunk)):
            break
        time.sleep(0.2)
    by_epoch: Dict[int, dict] = {}
    for ch in chunks:
        for t in ch:
            by_epoch[t["epoch"]] = t
    return [by_epoch[e] for e in sorted(by_epoch)]


def depths_for_days(days: float) -> Dict[str, int]:
    d = int(days)
    return {"M5": d * 288 + 300,
            "M15": min(5000, d * 96 + 3000),
            "M30": d * 48 + 120,
            "H1": 1000, "H4": 800, "D1": 365}


def load_histories(provider: Any, instruments: Dict[str, dict],
                   timeframes: Dict[str, int], days: int = 21) -> Dict[str, Dict[str, list]]:
    """Télécharge les historiques profonds. {symbole: {TF: bougies}}."""
    need = depths_for_days(days)
    out: Dict[str, Dict[str, list]] = {}
    for name, inst in instruments.items():
        sym = inst["symbol"]
        out[sym] = {}
        for tf, gran in timeframes.items():
            t0 = time.time()
            candles = fetch_deep(provider, sym, gran, need[tf])
            rep = provider.validate(candles, gran)
            log.info("%s %s : %d bougies (%.1fs) ok=%s trous=%d",
                     sym, tf, len(candles), time.time() - t0, rep["ok"], rep["gaps"])
            out[sym][tf] = candles
    return out


def save_cache(path: str, payload: dict) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f)
    os.replace(tmp, path)


def load_cache(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)
