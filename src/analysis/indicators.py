"""
Indicateurs techniques — fonctions pures, déterministes, sans repaint.

Conventions (valables pour tout le module `analysis`) :
- entrée bougies : dicts {epoch, open, high, low, close} (format DerivProvider) ;
- les séries retournées sont alignées sur l'entrée (`None` pendant la chauffe) ;
- aucun accès réseau, aucun état global : mêmes entrées ⇒ mêmes sorties.
"""
from __future__ import annotations

from typing import List, Optional, Sequence


def ema_series(values: Sequence[float], period: int) -> List[Optional[float]]:
    """EMA (moyenne mobile exponentielle). Amorce = SMA des `period` 1ères valeurs."""
    if period < 1:
        raise ValueError("period >= 1 requis")
    out: List[Optional[float]] = [None] * len(values)
    if len(values) < period:
        return out
    k = 2.0 / (period + 1)
    ema = sum(values[:period]) / period
    out[period - 1] = ema
    for i in range(period, len(values)):
        ema = values[i] * k + ema * (1 - k)
        out[i] = ema
    return out


def ema_value(values: Sequence[float], period: int) -> Optional[float]:
    """Dernière valeur d'EMA (None si pas assez d'historique)."""
    s = ema_series(values, period)
    return s[-1] if s else None


def rsi_series(closes: Sequence[float], period: int = 14) -> List[Optional[float]]:
    """RSI de Wilder. Amorce = moyennes simples des gains/pertes sur `period`.

    Conventions de bord : 100 si que des hausses, 0 si que des baisses,
    50 si parfaitement plat (neutre).
    """
    if period < 1:
        raise ValueError("period >= 1 requis")
    out: List[Optional[float]] = [None] * len(closes)
    if len(closes) <= period:
        return out
    gains = [max(0.0, closes[i] - closes[i - 1]) for i in range(1, len(closes))]
    losses = [max(0.0, closes[i - 1] - closes[i]) for i in range(1, len(closes))]
    avg_g = sum(gains[:period]) / period
    avg_l = sum(losses[:period]) / period

    def _rsi(g: float, sl: float) -> float:
        if sl == 0:
            return 50.0 if g == 0 else 100.0
        rs = g / sl
        return 100.0 - 100.0 / (1.0 + rs)

    out[period] = _rsi(avg_g, avg_l)
    for i in range(period + 1, len(closes)):
        avg_g = (avg_g * (period - 1) + gains[i - 1]) / period
        avg_l = (avg_l * (period - 1) + losses[i - 1]) / period
        out[i] = _rsi(avg_g, avg_l)
    return out


def rsi_value(closes: Sequence[float], period: int = 14) -> Optional[float]:
    """Dernière valeur de RSI (None si pas assez d'historique)."""
    s = rsi_series(closes, period)
    return s[-1] if s else None


def body_ratio(c: dict) -> float:
    """|corps| / range dans [0, 1] (0 si bougie plate)."""
    rng = c["high"] - c["low"]
    if rng <= 0:
        return 0.0
    return abs(c["close"] - c["open"]) / rng


def close_position(c: dict) -> float:
    """Position de la clôture dans le range : 0 (plus bas) → 1 (plus haut)."""
    rng = c["high"] - c["low"]
    if rng <= 0:
        return 0.5
    return (c["close"] - c["low"]) / rng


def is_bullish_engulfing(prev: dict, cur: dict) -> bool:
    """Englobante haussière classique : prev baissière, corps cur avale corps prev."""
    if not (cur["close"] > cur["open"] and prev["close"] < prev["open"]):
        return False
    return cur["open"] <= prev["close"] and cur["close"] >= prev["open"]


def is_bearish_engulfing(prev: dict, cur: dict) -> bool:
    """Englobante baissière classique (miroir)."""
    if not (cur["close"] < cur["open"] and prev["close"] > prev["open"]):
        return False
    return cur["open"] >= prev["close"] and cur["close"] <= prev["open"]


def pin_bar(c: dict, body_max: float = 1 / 3, wick_mult: float = 2.0) -> Optional[str]:
    """Pin bar : petit corps (≤ 1/3 du range) + une mèche ≥ 2× le corps.

    Retourne "bullish" (longue mèche basse), "bearish" (longue mèche haute) ou None.
    """
    rng = c["high"] - c["low"]
    if rng <= 0:
        return None
    body = abs(c["close"] - c["open"])
    if body > body_max * rng:
        return None
    upper = c["high"] - max(c["open"], c["close"])
    lower = min(c["open"], c["close"]) - c["low"]
    if lower >= wick_mult * body and lower > upper:
        return "bullish"
    if upper >= wick_mult * body and upper > lower:
        return "bearish"
    return None
