"""
Order Blocks : dernière bougie opposée avant une cassure (origine de l'impulsion).

- Cassure haussière (BOS/CHoCH) ⇒ OB "demand" = range [low, high] de la dernière
  bougie baissière dans les `lookback` barres précédentes (conservateur : plein range).
- Cassure baissière ⇒ OB "supply" (miroir, dernière bougie haussière).
- Mitigation (consommation) : 1ʳᵉ clôture AU-DELÀ du bord extrême
  (demand invalidé si close < bottom ; les mèches ne comptent pas).
- Immuabilité : l'émission (zone, index) ne change jamais ; seule la mitigation
  peut passer actif→mitigé quand de nouvelles bougies arrivent (info nouvelle,
  pas du repaint). Le filtrage par âge se fait dans les sélecteurs (étape 3),
  jamais ici.
"""
from __future__ import annotations

from typing import List, Sequence


def detect_order_blocks(
    candles: Sequence[dict],
    structure_events: Sequence[dict],
    lookback: int = 5,
) -> List[dict]:
    """OBs [{"direction", "top", "bottom", "index", "epoch",
    "break_index", "break_epoch", "mitigated", "mitigated_index", "mitigated_epoch"}]."""
    n = len(candles)
    obs: List[dict] = []
    for ev in structure_events:
        j = ev["index"]
        if ev["direction"] == "bullish":
            ks = [k for k in range(max(0, j - lookback), j)
                  if candles[k]["close"] < candles[k]["open"]]
            if not ks:
                continue
            k = ks[-1]
            top, bottom = candles[k]["high"], candles[k]["low"]
            mit = next((m for m in range(j + 1, n)
                        if candles[m]["close"] < bottom), None)
            obs.append({"direction": "demand", "top": top, "bottom": bottom,
                        "index": k, "epoch": candles[k]["epoch"],
                        "break_index": j, "break_epoch": ev["epoch"],
                        "mitigated": mit is not None,
                        "mitigated_index": mit,
                        "mitigated_epoch": candles[mit]["epoch"] if mit is not None else None})
        else:
            ks = [k for k in range(max(0, j - lookback), j)
                  if candles[k]["close"] > candles[k]["open"]]
            if not ks:
                continue
            k = ks[-1]
            top, bottom = candles[k]["high"], candles[k]["low"]
            mit = next((m for m in range(j + 1, n)
                        if candles[m]["close"] > top), None)
            obs.append({"direction": "supply", "top": top, "bottom": bottom,
                        "index": k, "epoch": candles[k]["epoch"],
                        "break_index": j, "break_epoch": ev["epoch"],
                        "mitigated": mit is not None,
                        "mitigated_index": mit,
                        "mitigated_epoch": candles[mit]["epoch"] if mit is not None else None})
    return obs


def active_zones(obs: Sequence[dict]) -> List[dict]:
    """Sous-ensemble non mitigé (zones d'intérêt utilisables)."""
    return [o for o in obs if not o["mitigated"]]
