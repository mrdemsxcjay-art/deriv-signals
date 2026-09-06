"""
Structure SMC : BOS / CHoCH sur clôtures de bougies clôturées.

Règles (déterministes, causales — donc sans repaint) :
- ne casse que sur le niveau d'un swing CONFIRMÉ strictement avant la barre
  (index_swing + strength < index_barre, le swing étant confirmé à +strength) ;
- clôture > dernier swing H ⇒ cassure haussière ; clôture < dernier swing L ⇒ baissière ;
- un niveau cassé est CONSOMMÉ (anti-refire : pas de 2ᵉ signal sur le même niveau) ;
- BOS = cassure dans le sens de la tendance (ou 1ʳᵉ cassure = BOS, établit la tendance) ;
- CHoCH = cassure contre la tendance (retournement).
"""
from __future__ import annotations

from typing import List, Optional, Sequence

from .candles import find_swings


def detect_structure(candles: Sequence[dict], strength: int = 2) -> List[dict]:
    """Événements [{"type", "direction", "index", "epoch", "level",
    "ref_index", "ref_epoch"}] — type BOS|CHoCH, direction bullish|bearish."""
    swings = find_swings(candles, strength)
    highs = [s for s in swings if s["kind"] == "H"]
    lows = [s for s in swings if s["kind"] == "L"]
    events: List[dict] = []
    trend: Optional[str] = None
    consumed_h, consumed_l = -1, -1
    for i, c in enumerate(candles):
        rh = next((s for s in reversed(highs)
                   if s["index"] < i - strength and s["index"] > consumed_h), None)
        rl = next((s for s in reversed(lows)
                   if s["index"] < i - strength and s["index"] > consumed_l), None)
        if rh is not None and c["close"] > rh["price"]:
            etype = "BOS" if trend in (None, "bullish") else "CHoCH"
            events.append({"type": etype, "direction": "bullish", "index": i,
                           "epoch": c["epoch"], "level": rh["price"],
                           "ref_index": rh["index"], "ref_epoch": rh["epoch"]})
            trend, consumed_h = "bullish", rh["index"]
        elif rl is not None and c["close"] < rl["price"]:
            etype = "BOS" if trend in (None, "bearish") else "CHoCH"
            events.append({"type": etype, "direction": "bearish", "index": i,
                           "epoch": c["epoch"], "level": rl["price"],
                           "ref_index": rl["index"], "ref_epoch": rl["epoch"]})
            trend, consumed_l = "bearish", rl["index"]
    return events


def current_trend(events: Sequence[dict]) -> Optional[str]:
    """Tendance = direction de la dernière cassure (None si aucune)."""
    return events[-1]["direction"] if events else None


def last_break(events: Sequence[dict], direction: Optional[str] = None) -> Optional[dict]:
    """Dernière cassure (filtrée par direction si précisée)."""
    for e in reversed(events):
        if direction is None or e["direction"] == direction:
            return e
    return None
