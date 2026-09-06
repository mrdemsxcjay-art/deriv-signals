"""
Bougies : ATR et swings (fractales) — base de toute la SMC.

- ATR de Wilder (amorce = SMA des `period` premiers True Ranges).
- Swing = fractale de force `strength` : plus haut/bas strict sur
  [i-strength, i+strength]. Un swing en `i` est CONFIRMÉ à la clôture de
  `i+strength` (délai structurel documenté, pas du repaint : une fois
  confirmé, il est immuable).
"""
from __future__ import annotations

from typing import List, Optional, Sequence


def true_range(c: dict, prev_close: Optional[float]) -> float:
    """True Range : max(H-L, |H-prevC|, |L-prevC|). Sans prev : H-L."""
    hl = c["high"] - c["low"]
    if prev_close is None:
        return hl
    return max(hl, abs(c["high"] - prev_close), abs(c["low"] - prev_close))


def atr_series(candles: Sequence[dict], period: int = 14) -> List[Optional[float]]:
    """ATR de Wilder, aligné sur les bougies (None pendant la chauffe)."""
    if period < 1:
        raise ValueError("period >= 1 requis")
    out: List[Optional[float]] = [None] * len(candles)
    if len(candles) < period:
        return out
    trs = [true_range(c, candles[i - 1]["close"] if i else None)
           for i, c in enumerate(candles)]
    atr = sum(trs[:period]) / period
    out[period - 1] = atr
    for i in range(period, len(candles)):
        atr = (atr * (period - 1) + trs[i]) / period
        out[i] = atr
    return out


def atr_value(candles: Sequence[dict], period: int = 14) -> Optional[float]:
    """Dernière valeur d'ATR (None si pas assez d'historique)."""
    s = atr_series(candles, period)
    return s[-1] if s else None


def find_swings(candles: Sequence[dict], strength: int = 2) -> List[dict]:
    """Swings fractales : [{"index", "epoch", "price", "kind"}] triés par index.

    kind = "H" (plus haut strict des 2×strength voisins) ou "L" (plus bas strict).
    Une même barre peut porter un H et un L. Égalités ⇒ pas de swing (déterministe).
    """
    if strength < 1:
        raise ValueError("strength >= 1 requis")
    n = len(candles)
    swings: List[dict] = []
    for i in range(strength, n - strength):
        h, lo = candles[i]["high"], candles[i]["low"]
        neigh_h = [candles[j]["high"] for j in range(i - strength, i + strength + 1) if j != i]
        neigh_l = [candles[j]["low"] for j in range(i - strength, i + strength + 1) if j != i]
        if all(h > x for x in neigh_h):
            swings.append({"index": i, "epoch": candles[i]["epoch"],
                           "price": h, "kind": "H"})
        if all(lo < x for x in neigh_l):
            swings.append({"index": i, "epoch": candles[i]["epoch"],
                           "price": lo, "kind": "L"})
    swings.sort(key=lambda s: (s["index"], s["kind"]))
    return swings
