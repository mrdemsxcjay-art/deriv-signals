"""
Contexte synthétique (§5) — remplace le fondamental, calculé sur données réelles.

Détecteurs (causaux, immuables — voir preuve anti-repaint dans test_synth.py) :
- SPIKE (BOOM1000, M5) : bougie haussière dont le range > `multiplier` × médiane
  des 100 ranges précédents. **Calibré par mesure** : 3,0 (intervalle moyen 36 min
  ≈ cible documentée 30-35 min ; le 4,0 proposé donnait 48 min ⇒ rejeté).
- JUMP (JD10, TICKS) : |Δtick| > `threshold` pts. **Calibré par mesure** : 10 pts
  (séparation nette : corps p99,9 = 7,2 pts, plus petit jump = 11,2 pts ;
  taux mesuré 2,76/h ≈ 3/h officiels Deriv). L'approche « gap M5 » du §5 initial
  est ABANDONNÉE (mesuré : 5 gaps/17 j — les jumps tombent intra-bougie).

Références temporelles : les « temps depuis » sont relatifs à la DERNIÈRE
donnée chargée (clôture dernière bougie / dernier tick), pas à l'horloge murale
⇒ déterministe et testable (écart ≤ 5 min en production, négligeable).
"""
from __future__ import annotations

import statistics
from typing import List, Optional, Sequence


# ---------------------------------------------------------------- spikes ---

def detect_spikes(
    candles: Sequence[dict],
    window: int = 100,
    multiplier: float = 3.0,
) -> List[dict]:
    """Spikes [{"index", "epoch", "amplitude", "median_amp", "ratio"}] (M5, haussiers)."""
    if window < 1:
        raise ValueError("window >= 1 requis")
    ranges = [c["high"] - c["low"] for c in candles]
    out: List[dict] = []
    for i in range(window, len(candles)):
        med = statistics.median(ranges[i - window:i])
        amp = ranges[i]
        if med > 0 and candles[i]["close"] > candles[i]["open"] and amp > multiplier * med:
            out.append({"index": i, "epoch": candles[i]["epoch"],
                        "amplitude": amp, "median_amp": med, "ratio": amp / med})
    return out


# ---------------------------------------------------------------- jumps ---

def detect_jumps(
    ticks: Sequence[dict],
    threshold: float = 10.0,
) -> List[dict]:
    """Jumps [{"index", "epoch", "price", "size", "direction"}] — direction UP|DN."""
    out: List[dict] = []
    for k in range(1, len(ticks)):
        d = ticks[k]["price"] - ticks[k - 1]["price"]
        if abs(d) > threshold:
            out.append({"index": k, "epoch": ticks[k]["epoch"], "price": ticks[k]["price"],
                        "size": abs(d), "direction": "UP" if d > 0 else "DN"})
    return out


# --------------------------------------------------------------- helpers ---

def _median(xs: Sequence[float]) -> Optional[float]:
    return statistics.median(xs) if xs else None


def _quantile(xs: Sequence[float], q: float) -> Optional[float]:
    if not xs:
        return None
    s = sorted(xs)
    return s[min(int(len(s) * q), len(s) - 1)]


def _intervals_min(epochs: Sequence[int]) -> List[float]:
    return [(b - a) / 60.0 for a, b in zip(epochs, epochs[1:])]


# ------------------------------------------------------- contextes §5 ---

def vol_stats(m15: Sequence[dict], atr_period: int = 14,
              percentile_days: int = 30) -> dict:
    """Régime de volatilité universel : ATR M15 + percentile 30 j + régime."""
    from ..analysis.candles import atr_series
    atrs = [a for a in atr_series(m15, atr_period) if a is not None]
    need = percentile_days * 24 * 4  # M15 : 96/jour
    hist = atrs[-need:]
    cur = atrs[-1] if atrs else None
    pct = (sum(1 for a in hist if a <= cur) / len(hist) * 100.0
           if cur is not None and len(hist) >= 100 else None)
    regime = ("calme" if pct < 33 else "normal" if pct < 66 else "tendu") \
        if pct is not None else None
    return {"atr_m15": cur, "percentile": pct, "regime": regime, "n_m15": len(m15)}


def boom_context(m5: Sequence[dict], h1: Sequence[dict], granularity_m5: int = 300,
                 window: int = 100, multiplier: float = 3.0, stats_window: int = 50,
                 spikes: Optional[List[dict]] = None) -> dict:
    """BOOM1000 : dernier spike, intervalle moyen (50 derniers), dérive/h, n° spike.

    `spikes` pré-calculés (replay Étape 4) : si fourni, la détection est sautée.
    """
    if spikes is None:
        spikes = detect_spikes(m5, window, multiplier)
    ref_close = m5[-1]["epoch"] + granularity_m5 if m5 else None
    last = spikes[-1] if spikes else None
    last_bar = m5[last["index"]] if last else None
    ivs = _intervals_min([s["epoch"] for s in spikes])[-stats_window:]
    amps = [s["amplitude"] for s in spikes[-stats_window:]]
    # Dérive = MÉDIANE (pas moyenne) des variations H1 : mesuré Étape 2, la moyenne
    # capte les spikes (signe instable : +2,4/-1,2/-0,8/-4,0/+1,7 sur 5×100 h) tandis
    # que la médiane est systématiquement baissière (-4,0/-10,2/-6,2/-6,1/-0,7).
    drift = None
    diffs = [h1[i]["close"] - h1[i - 1]["close"] for i in range(1, len(h1))][-500:]
    if diffs:
        drift = statistics.median(diffs)
    return {"n_spikes": len(spikes),
            "last_spike_num": len(spikes),  # n° = rang dans la fenêtre chargée
            "last_spike_epoch": last["epoch"] if last else None,
            "last_spike_high": last_bar["high"] if last_bar else None,
            "last_spike_low": last_bar["low"] if last_bar else None,
            "time_since_spike_min": ((ref_close - last["epoch"]) / 60.0
                                     if ref_close is not None and last is not None else None),
            "avg_interval_min": (sum(ivs) / len(ivs)) if ivs else None,
            "median_interval_min": _median(ivs),
            "drift_pts_per_hour": drift,
            "amplitude_med": _median(amps),
            "amplitude_p90": _quantile(amps, 0.9),
            "amplitude_max": max(amps) if amps else None}


def jump_context(ticks: Sequence[dict], threshold: float = 10.0,
                 stats_window: int = 20) -> dict:
    """JD10 : taux/h, intervalle moyen, taille médiane (20 derniers), dernier jump."""
    jumps = detect_jumps(ticks, threshold)
    span_h = (ticks[-1]["epoch"] - ticks[0]["epoch"]) / 3600.0 if len(ticks) >= 2 else 0.0
    last = jumps[-1] if jumps else None
    recent = jumps[-stats_window:]
    ivs = _intervals_min([j["epoch"] for j in jumps])[-stats_window:]
    sizes = [j["size"] for j in recent]
    return {"n_jumps": len(jumps),
            "rate_per_hour": (len(jumps) / span_h) if span_h > 0 else None,
            "avg_interval_min": (sum(ivs) / len(ivs)) if ivs else None,
            "median_interval_min": _median(ivs),
            "median_size": _median(sizes),
            "p90_size": _quantile(sizes, 0.9),
            "max_size": max(sizes) if sizes else None,
            "last_jump_epoch": last["epoch"] if last else None,
            "last_jump_direction": last["direction"] if last else None,
            "time_since_jump_min": ((ticks[-1]["epoch"] - last["epoch"]) / 60.0
                                    if ticks and last else None)}
