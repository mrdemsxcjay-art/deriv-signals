"""
Câblage paper au cycle live — miroir + résolution + snapshot baseline.

Appelé UNIQUEMENT par run_cycle (jamais par le replay) : les positions paper
naissent exactement au même moment que les signaux live (même cycle, mêmes
bougies clôturées). Toute erreur paper remonte en res.errors (jamais
silencieuse) et ne bloque JAMAIS le live (try/except côté moteur).
"""
from __future__ import annotations

import time
from typing import Any, Dict, List

from ..storage import database as db
from . import models as PM
from . import store as PStore

SYM = {"JD10": "JD10", "BOOM1000": "BOOM1000"}


def check_frozen(paper_db: str) -> None:
    """Garde de gel (règle 6) : init + empreinte, ou ValueError si dérive."""
    PStore.init_db(paper_db)
    fp = PM.config_fingerprint()
    old = PStore.get_meta(paper_db, "fingerprint")
    if old is None:
        PStore.set_meta(paper_db, "fingerprint", fp)
        PStore.set_meta(paper_db, "protocol",
                        "H-SL-ADAPTIVE paper v1 (règles 1-8, 07/09/2026)")
        return
    if old != fp:
        raise ValueError("empreinte paper différente — paramètres modifiés "
                         "pendant l'observation (interdit, règle 6)")


def _sig_dict(sig: Any) -> Dict[str, Any]:
    return sig if isinstance(sig, dict) else dict(sig.__dict__)


def mirror_signal(paper_db: str, sig: Any, tf: Dict[str, list],
                  cursor_epoch: int, now: int) -> str:
    """Position paper miroir d'un signal live frais. Retourne un fragment de log.

    Enregistre (règle 4) : entrée, SL fixe, SL adaptatif, TP, score, régime,
    timestamp, latence de calcul. NO_TRADE paper ⇒ ligne conservée quand même
    (baseline suivie, règle 8).
    """
    s = _sig_dict(sig)
    if (s["instrument"], s["direction"]) not in PM.PAPER_PAIRS:
        return ""
    t0 = time.perf_counter()
    pos = PM.paper_position(
        s, {"M15": tf.get("M15", []), "M5": tf.get("M5", [])}, cursor_epoch)
    ms = round((time.perf_counter() - t0) * 1000, 1)
    dist, bucket = PM.regime_of(tf.get("D1", []), s["entry_epoch"], s["entry"])
    base = {"live_id": s["id"], "instrument": s["instrument"],
            "direction": s["direction"], "entry": s["entry"],
            "entry_epoch": s["entry_epoch"], "sl_fixed_pts": s["sl_pts"],
            "tp_fixed_pts": s["tp_pts"], "score": s.get("confidence"),
            "grade": s.get("grade"), "regime": bucket, "d1_dist_atr": dist,
            "live_created_epoch": s.get("created_epoch", now), "compute_ms": ms}
    if "no_trade" in pos:
        row = dict(base, id=f"{s['id']}:NOTRADE",
                   no_trade_reason=pos["no_trade"])
        PStore.save_no_trade(paper_db, row, now)
        return f" 📝paper NO_TRADE ({pos['no_trade']})"
    fresh = PStore.save_signal(paper_db, dict(base, **pos), now)
    return (f" 📝paper {pos['pair']}@TP{pos['m']:g} SL{pos['sl_pts']:.1f} "
            f"(fixe {s['sl_pts']:.0f})" + ("" if fresh else " (déjà)"))


def update_paper(paper_db: str, provider: Any, expiry_bars: int = 96) -> List[dict]:
    """Résout les positions paper ouvertes (1 requête M15 par instrument)."""
    closed: List[dict] = []
    by_sym: Dict[str, List[dict]] = {}
    for p in PStore.get_open(paper_db):
        by_sym.setdefault(SYM[p["instrument"]], []).append(p)
    for symbol, poss in by_sym.items():
        m15 = provider.get_candles(symbol, 900, 300)
        for p in poss:
            o = PM.resolve_paper(p, m15, expiry_bars)
            if o is None:
                continue
            mae, mfe = PM.mae_mfe(p["direction"], p["entry"], m15,
                                  p["entry_epoch"], o["closed_epoch"])
            o["mae_pts"] = round(mae, 2)
            o["mfe_pts"] = round(mfe, 2)
            o["_fresh"] = (PStore.close_signal(paper_db, o) == "inserted")
            if o["_fresh"]:
                closed.append(o)
    return closed


def snapshot_live_closes(paper_db: str, live_db: str, provider: Any,
                         live_outcomes: List[dict]) -> int:
    """Snapshot baseline (règle 4-5) : résultat + R + MAE/MFE du SL fixe.

    N'agit que sur les clôtures live FRAÎCHES ayant une ligne paper.
    Retourne le nombre de snapshots.
    """
    fresh = [o for o in (live_outcomes or [])
             if o.get("result") in ("TP", "SL", "EXPIRE") and o.get("_fresh", True)]
    ids = [o["signal_id"] for o in fresh if PStore.has_live(paper_db, o["signal_id"])]
    if not ids:
        return 0
    sigs = {i: db.get_signal(live_db, i) for i in ids}
    by_sym: Dict[str, List[dict]] = {}
    for i in ids:
        if sigs[i] is not None:
            by_sym.setdefault(sigs[i]["symbol"], []).append(sigs[i])
    omap = {o["signal_id"]: o for o in fresh}
    n = 0
    for symbol, rows in by_sym.items():
        m15 = provider.get_candles(symbol, 900, 300)
        for s in rows:
            o = omap[s["id"]]
            mae, mfe = PM.mae_mfe(s["direction"], s["entry"], m15,
                                  s["entry_epoch"], o["closed_epoch"])
            PStore.snapshot_live_close(paper_db, s["id"], o["result"], o["r"],
                                       o["bars_held"], o["closed_epoch"],
                                       round(mae, 2), round(mfe, 2))
            n += 1
    return n
