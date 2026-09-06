"""
Tracker — suivi des signaux ouverts sur bougies M15 clôturées.

- SELL : SL touché si high ≥ sl_price ; TP si low ≤ tp_price (miroir pour BUY).
- Même bougie touchant SL ET TP ⇒ SL (hypothèse prudente, notée dans l'issue).
- EXPIRE après `expiry_bars` M15 (défaut 96 = 24 h, provisoire — replay étape 4) :
  R = clamp(R latent, -1, +3). Bornes R [-1, +3] garanties par construction.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from ..storage import database as db


def _clamp_r(r: float) -> float:
    return max(-1.0, min(3.0, r))


def resolve_signal(sig: Dict[str, Any], m15: List[dict],
                   expiry_bars: int = 96) -> Optional[Dict[str, Any]]:
    """Résout un signal ouvert. Retourne l'outcome dict ou None (toujours ouvert)."""
    bars = [c for c in m15 if c["epoch"] > sig["entry_epoch"]]
    if not bars:
        return None
    bear = sig["direction"] == "bearish"
    entry, sl_p, tp_p = sig["entry"], sig["sl_price"], sig["tp_price"]
    sl_pts = sig["sl_pts"]
    for k, c in enumerate(bars, start=1):
        sl_hit = c["high"] >= sl_p if bear else c["low"] <= sl_p
        tp_hit = c["low"] <= tp_p if bear else c["high"] >= tp_p
        if sl_hit and tp_hit:
            pts = -sl_pts
            return {"signal_id": sig["id"], "closed_epoch": c["epoch"], "result": "SL",
                    "r": -1.0, "points": pts, "bars_held": k, "exit_price": sl_p,
                    "note": "SL avant TP (même bougie, hypothèse prudente)"}
        if sl_hit:
            return {"signal_id": sig["id"], "closed_epoch": c["epoch"], "result": "SL",
                    "r": -1.0, "points": -sl_pts, "bars_held": k, "exit_price": sl_p,
                    "note": ""}
        if tp_hit:
            return {"signal_id": sig["id"], "closed_epoch": c["epoch"], "result": "TP",
                    "r": 3.0, "points": 3.0 * sl_pts, "bars_held": k, "exit_price": tp_p,
                    "note": ""}
        if k >= expiry_bars:
            signed = (entry - c["close"]) if bear else (c["close"] - entry)
            return {"signal_id": sig["id"], "closed_epoch": c["epoch"], "result": "EXPIRE",
                    "r": _clamp_r(signed / sl_pts), "points": signed, "bars_held": k,
                    "exit_price": c["close"],
                    "note": f"expiré après {expiry_bars} M15 sans TP/SL"}
    return None


def update_all(db_path: str, provider: Any, expiry_bars: int = 96,
               m15_count: int = 300) -> List[Dict[str, Any]]:
    """Résout tous les signaux ouverts (1 requête M15 par instrument concerné)."""
    outcomes: List[Dict[str, Any]] = []
    opens = db.get_open_signals(db_path)
    by_symbol: Dict[str, List[Dict[str, Any]]] = {}
    for s in opens:
        by_symbol.setdefault(s["symbol"], []).append(s)
    for symbol, sigs in by_symbol.items():
        try:
            m15 = provider.get_candles(symbol, 900, m15_count)
        except Exception as exc:  # noqa: BLE001 — un instrument en panne ne bloque pas
            outcomes.append({"signal_id": "*", "note": f"tracker {symbol} : {exc}",
                             "result": "ERROR", "r": 0.0, "points": 0.0,
                             "bars_held": 0, "exit_price": 0.0,
                             "closed_epoch": 0})
            continue
        for s in sigs:
            out = resolve_signal(s, m15, expiry_bars)
            if out is not None:
                db.close_signal(db_path, out)
                outcomes.append(out)
    return [o for o in outcomes if o["result"] != "ERROR"] + \
           [o for o in outcomes if o["result"] == "ERROR"]
