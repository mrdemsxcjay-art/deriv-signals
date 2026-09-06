"""
Fair Value Gaps (FVG) : imbalance sur 3 bougies.

- FVG haussier centré en `i` : low[i+1] > high[i-1] ⇒ zone [high[i-1], low[i+1]],
  confirmé à la clôture de i+1 (3 bougies clôturées : causal, sans repaint).
- FVG baissier : high[i+1] < low[i-1] ⇒ zone [high[i+1], low[i-1]].
- Mitigation : 1ʳᵉ clôture postérieure AU-DELÀ du bord extrême
  (FVG haussier comblé/invalidé si close < bottom).
- `min_size` (points) : filtre optionnel, 0 = tout garder (filtrage ATR à l'étape 3+).
"""
from __future__ import annotations

from typing import List, Sequence


def detect_fvg(candles: Sequence[dict], min_size: float = 0.0) -> List[dict]:
    """FVGs [{"direction", "top", "bottom", "index", "epoch",
    "mitigated", "mitigated_index", "mitigated_epoch"}]."""
    n = len(candles)
    out: List[dict] = []
    for i in range(1, n - 1):
        a, b, d = candles[i - 1], candles[i], candles[i + 1]
        if d["low"] > a["high"]:
            top, bottom = d["low"], a["high"]
            if top - bottom >= min_size:
                mit = next((m for m in range(i + 2, n)
                            if candles[m]["close"] < bottom), None)
                out.append({"direction": "bullish", "top": top, "bottom": bottom,
                            "index": i, "epoch": b["epoch"],
                            "mitigated": mit is not None, "mitigated_index": mit,
                            "mitigated_epoch": candles[mit]["epoch"] if mit is not None else None})
        elif d["high"] < a["low"]:
            top, bottom = a["low"], d["high"]
            if top - bottom >= min_size:
                mit = next((m for m in range(i + 2, n)
                            if candles[m]["close"] > top), None)
                out.append({"direction": "bearish", "top": top, "bottom": bottom,
                            "index": i, "epoch": b["epoch"],
                            "mitigated": mit is not None, "mitigated_index": mit,
                            "mitigated_epoch": candles[mit]["epoch"] if mit is not None else None})
    return out


def active_fvgs(fvgs: Sequence[dict]) -> List[dict]:
    """Sous-ensemble non mitigé (imbalances encore valides)."""
    return [f for f in fvgs if not f["mitigated"]]
