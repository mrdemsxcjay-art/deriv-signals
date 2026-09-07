"""
Paper trading H-SL-ADAPTIVE — calculs déterministes des paires suivies.

Paires ACCEPTÉES (conditionnelles, verdicts OOS 2026-09-07) suivies en paper,
live strictement inchangé :
- JD10 bearish : A2-ob-b0 @ TP4 (SL = extrême zone OB supply, marge 0)
- BOOM1000 bullish : C-spk-P50 @ TP3 (SL = P50 amplitudes des spikes M5)

Formules = portage EXACT de scripts/sl_models.py (recherche validée OOS) :
mêmes fonctions src.analysis / src.synthetics, mêmes paramètres, mêmes
rails §9. Toute divergence est un bug bloquant (parité testée dans
scripts/test_paper.py). Données insuffisantes ⇒ NO_TRADE, jamais SL inventé.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from ..analysis.candles import atr_value
from ..analysis.order_blocks import active_zones, detect_order_blocks
from ..analysis.structure import detect_structure
from ..signals.tracker import resolve_signal
from ..synthetics.context import detect_spikes

# (instrument, direction) -> (nom variante recherche, multiple TP).
PAPER_PAIRS: Dict[Tuple[str, str], Tuple[str, float]] = {
    ("JD10", "bearish"): ("A2-ob-b0", 4.0),
    ("BOOM1000", "bullish"): ("C-spk-P50", 3.0),
}
# Rails §9 (copie exacte recherche — parité testée, ne pas tuner ici).
RAILS = {"BOOM1000": (10.0, 75.0), "JD10": (20.0, 300.0), "V10": (10.0, 75.0)}
ATR_CAP = 3.0
EXPIRY_BARS = 96


def pct_rank(sorted_vals: List[float], p: float) -> Optional[float]:
    """Percentile déterministe nearest-rank (copie exacte recherche)."""
    if not sorted_vals:
        return None
    idx = min(len(sorted_vals) - 1, max(0, -(-p * len(sorted_vals) // 100) - 1))
    return sorted_vals[idx]


def paper_features(instrument: str, direction: str, entry: float,
                   entry_epoch: int, m15_all: List[dict], m5_all: List[dict],
                   cursor_epoch: int) -> Optional[Dict[str, Any]]:
    """Features causales à l'entrée (None si insuffisant).

    Découpage identique à la recherche : M15 closes ≤ entry (500 max, 60 min),
    M5 closes ≤ curseur (bougies clôturées uniquement). En recherche le curseur
    vaut entry_epoch + 900 (= clôture du M15 d'évaluation) ; en live, passer
    l'epoch de clôture du M15 évalué.
    """
    m15 = [b for b in m15_all if b["epoch"] <= entry_epoch][-500:]
    if len(m15) < 60:
        return None
    m5 = [b for b in m5_all if b["epoch"] + 300 <= cursor_epoch]
    atr15 = atr_value(m15[-200:], 14)
    if atr15 is None or atr15 <= 0:
        return None
    struct = detect_structure(m15, 2)
    obs = active_zones(detect_order_blocks(m15, struct))
    want = "demand" if direction == "bullish" else "supply"
    rel = [o for o in obs if o["direction"] == want]
    ob = max(rel, key=lambda o: o["index"]) if rel else None
    spikes = detect_spikes(m5, 100, 3.0) if len(m5) >= 101 else []
    amps = sorted(s["amplitude"] for s in spikes[-50:])
    return {"atr15": atr15, "ob": ob, "amps": amps, "entry": entry,
            "direction": direction, "instrument": instrument}


def pair_sl_raw(pair: str, feat: Optional[Dict[str, Any]]) -> Optional[float]:
    """Distance SL brute (None = NO_TRADE)."""
    if feat is None:
        return None
    e, bull = feat["entry"], feat["direction"] == "bullish"
    if pair == "A2-ob-b0":
        ob = feat["ob"]
        if ob is None:
            return None
        dist = (e - ob["bottom"]) if bull else (ob["top"] - e)
        return dist if dist > 0 else None
    if pair == "C-spk-P50":
        if len(feat["amps"]) < 10:
            return None
        return pct_rank(feat["amps"], 50)
    raise ValueError(f"paire paper inconnue : {pair}")


def apply_rails(inst: str, raw: float, atr15: float):
    """Rails §9 (copie exacte recherche)."""
    lo, hi = RAILS[inst]
    cap = ATR_CAP * atr15
    v, hit = raw, None
    if v < lo:
        v, hit = lo, "min"
    if v > min(hi, cap):
        v, hit = min(hi, cap), ("max" if hi <= cap else "atr_cap")
    return v, hit


def paper_position(live_sig: Dict[str, Any], tf: Dict[str, List[dict]],
                   cursor_epoch: int) -> Dict[str, Any]:
    """Position paper miroir d'un signal live, ou {'no_trade': motif}.

    live_sig : clés instrument/direction/entry/entry_epoch/id.
    tf : {'M15': [...], 'M5': [...]} bougies clôturées.
    N'écrit rien, ne touche jamais au live.
    """
    key = (live_sig["instrument"], live_sig["direction"])
    if key not in PAPER_PAIRS:
        return {"no_trade": "paire non suivie en paper"}
    pair, m = PAPER_PAIRS[key]
    feat = paper_features(live_sig["instrument"], live_sig["direction"],
                          live_sig["entry"], live_sig["entry_epoch"],
                          tf.get("M15", []), tf.get("M5", []), cursor_epoch)
    raw = pair_sl_raw(pair, feat)
    if raw is None or feat is None:
        return {"no_trade": "données insuffisantes (OB/spikes/ATR)"}
    sl, hit = apply_rails(live_sig["instrument"], raw, feat["atr15"])
    bull = live_sig["direction"] == "bullish"
    e = live_sig["entry"]
    return {"id": f"{live_sig['id']}:{pair}@TP{m:g}", "live_id": live_sig["id"],
            "instrument": live_sig["instrument"], "direction": live_sig["direction"],
            "entry": e, "entry_epoch": live_sig["entry_epoch"],
            "pair": pair, "m": m, "sl_pts": sl, "tp_pts": m * sl,
            "sl_price": (e - sl) if bull else (e + sl),
            "tp_price": (e + m * sl) if bull else (e - m * sl),
            "rail_hit": hit, "raw_sl": raw}


def resolve_paper(paper_sig: Dict[str, Any], m15: List[dict],
                  expiry_bars: int = EXPIRY_BARS) -> Optional[Dict[str, Any]]:
    """Résout une position paper (miroir exact de recherche simulate()).

    VRAI resolve_signal de production + recrédit TP=+m (C12-style, paper seul).
    EXPIRE : clamp [-1, +3] du tracker, identique à la recherche validée OOS.
    """
    m = paper_sig["m"]
    o = resolve_signal(paper_sig, m15, expiry_bars)
    if o is None:
        return None
    o = dict(o)
    if o["result"] == "TP":
        o["r"] = float(m)
        o["points"] = float(paper_sig["tp_pts"])
    elif o["result"] == "SL":
        o["r"] = -1.0
        o["points"] = -float(paper_sig["sl_pts"])
    return o
