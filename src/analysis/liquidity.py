"""
Liquidité : doubles sommets/creux (EQH/EQL) et balayages (sweeps).

- EQH/EQL : swings de même côté regroupés par prix (trié, coupe si écart >
  tolérance). Groupes de ≥ 2 swings uniquement. Tolérance par défaut =
  0,1 × ATR courant (calibrage fin à l'étape 4).
- Sweep : mèche AU-DELÀ d'un swing confirmé + clôture revenue EN-DEÇÀ
  (ratage = prise de liquidité). Ex. sweep haussier des lows : low < swingL
  et close > swingL. Causal et immuable une fois la bougie clôturée.
"""
from __future__ import annotations

from typing import List, Optional, Sequence

from .candles import atr_value, find_swings


def equal_levels(
    candles: Sequence[dict],
    strength: int = 2,
    tolerance_pts: Optional[float] = None,
    atr_mult: float = 0.1,
    atr_period: int = 14,
) -> List[dict]:
    """Niveaux [{"kind", "price", "indices", "epochs", "count"}] — kind EQH|EQL."""
    if tolerance_pts is None:
        atr = atr_value(candles, atr_period)
        tolerance_pts = atr_mult * atr if atr else 0.0
    swings = find_swings(candles, strength)
    out: List[dict] = []
    for kind, label in (("H", "EQH"), ("L", "EQL")):
        grp = sorted((s for s in swings if s["kind"] == kind), key=lambda s: s["price"])
        cluster: List[dict] = []
        for s in grp:
            if cluster and s["price"] - cluster[-1]["price"] > tolerance_pts:
                if len(cluster) >= 2:
                    out.append(_level(label, cluster))
                cluster = []
            cluster.append(s)
        if len(cluster) >= 2:
            out.append(_level(label, cluster))
    return out


def _level(kind: str, cluster: Sequence[dict]) -> dict:
    idx = sorted(s["index"] for s in cluster)
    return {"kind": kind,
            "price": sum(s["price"] for s in cluster) / len(cluster),
            "indices": idx,
            "epochs": [s["epoch"] for s in sorted(cluster, key=lambda s: s["index"])],
            "count": len(cluster)}


def detect_sweeps(candles: Sequence[dict], strength: int = 2) -> List[dict]:
    """Sweeps [{"direction", "index", "epoch", "level", "ref_index", "ref_epoch"}].

    direction "bullish" = balayage des lows (mèche sous le swing, clôture au-dessus) ;
    "bearish" = miroir sur les highs. Référence = dernier swing CONFIRMÉ avant la barre.
    """
    swings = find_swings(candles, strength)
    highs = [s for s in swings if s["kind"] == "H"]
    lows = [s for s in swings if s["kind"] == "L"]
    out: List[dict] = []
    for i, c in enumerate(candles):
        rl = next((s for s in reversed(lows) if s["index"] < i - strength), None)
        rh = next((s for s in reversed(highs) if s["index"] < i - strength), None)
        if rl is not None and c["low"] < rl["price"] < c["close"]:
            out.append({"direction": "bullish", "index": i, "epoch": c["epoch"],
                        "level": rl["price"], "ref_index": rl["index"],
                        "ref_epoch": rl["epoch"]})
        if rh is not None and c["close"] < rh["price"] < c["high"]:
            out.append({"direction": "bearish", "index": i, "epoch": c["epoch"],
                        "level": rh["price"], "ref_index": rh["index"],
                        "ref_epoch": rh["epoch"]})
    return out
