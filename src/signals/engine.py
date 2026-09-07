"""
Moteur — un cycle : données → stratégie → scoring → anti-spam → SQLite → tracker.

- 1 décision max par instrument et par cycle (jamais 2 signaux opposés).
- Anti-spam : seuil 65 + cooldown 180 min + max 4/jour/instrument (§6).
- Transparence : chaque instrument loggue « bloqué par : … » ou le signal émis.
- Robuste : un instrument en panne n'arrête pas le cycle (erreur loggée).
- C1 (idempotence) : IDs déterministes (barre M15) ; garde fraîcheur (NO_TRADE
  si périmé) ; garde SL/TP-déjà-touché ; notifications at-most-once par état
  (flags notified_* + re-vérification pré-envoi + rattrapage des impayés).
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from ..agents.strategy_agent import evaluate_instrument
from ..data.deriv_provider import DerivProvider
from ..storage import database as db
from ..synthetics.context import (
    boom_context, detect_jumps, jump_context, v10_context, vol_stats,
)
from .models import Signal
from .tracker import update_all as tracker_update


@dataclass
class CycleResult:
    signals: List[Signal] = field(default_factory=list)
    paper: Dict[str, Any] = field(default_factory=dict)
    logs: Dict[str, str] = field(default_factory=dict)
    tracker: List[dict] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)


# C1 — âge max (minutes) de la dernière bougie CLÔTURÉE par timeframe (≈3× TF :
# 1× cycle nominal de la bougie + 15 min de cron + marge délais GitHub).
# Mesuré le 07/09/2026 (conditions nominales) : M5 2 min, M15/M30 7 min,
# H1 37 min, H4 217 min, D1 457 min — toutes les limites ≈3× le nominal max.
DEFAULT_FRESHNESS_MIN = {"M5": 15, "M15": 45, "M30": 90, "H1": 180,
                         "H4": 720, "D1": 4320}
# JD10 : dernier tick (nominalement < 2 min ; 60 min = pathologie cache/horloge).
TICKS_FRESHNESS_MIN = 60


def ensure_ticks(provider: DerivProvider, symbol: str, threshold: float = 10.0,
                 min_jumps: int = 20, max_batches: int = 8) -> list:
    """Garantit un historique ticks suffisant (remplissage froid paginé, puis incrémental)."""
    ticks = provider.get_ticks(symbol, count=5000)
    batches = 0
    while (len(detect_jumps(ticks, threshold)) < min_jumps
           and batches < max_batches and ticks):
        oldest = ticks[0]["epoch"]
        batch = provider.get_ticks(symbol, count=5000, end=oldest - 1)
        if not batch or batch[0]["epoch"] >= oldest:
            break
        ticks = provider.get_ticks(symbol, count=1000)  # relit le cache fusionné
        batches += 1
    return ticks


def fetch_tf_data(provider: DerivProvider, symbol: str, timeframes: Dict[str, int],
                  counts: Dict[str, int]) -> Dict[str, list]:
    return {tf: provider.get_timeframe(symbol, tf, counts[tf]) for tf in timeframes}


def build_contexts(instrument: str, tf: Dict[str, list],
                   ticks: Optional[list], P: Dict[str, Any]) -> Dict[str, Any]:
    S = P.get("synthetics_params", {})
    ctx: Dict[str, Any] = {"vol": vol_stats(tf["M15"])}
    if instrument == "V10":
        ctx["v10"] = v10_context(tf["M5"], tf["M15"])
    elif instrument == "BOOM1000":
        ctx["boom"] = boom_context(tf["M5"], tf["H1"],
                                   multiplier=S.get("spike_multiplier", 3.0))
    elif instrument == "JD10":
        ctx["jump"] = jump_context(ticks or [], threshold=S.get("jump_threshold_pts", 10.0)) \
            if ticks else {}
    return ctx


def _day_start_utc(now: int) -> int:
    dt = datetime.fromtimestamp(now, tz=timezone.utc).replace(hour=0, minute=0, second=0)
    return int(dt.timestamp())


def check_freshness(tf: Dict[str, list], grans: Dict[str, int], now: int,
                    limits_min: Optional[Dict[str, int]] = None) -> List[str]:
    """Garde C1 : âge de la dernière bougie CLÔTURÉE par TF.

    Retourne la liste des problèmes (vide = frais). Une donnée périmée ⇒
    NO_TRADE (fail-closed) : un signal sur bougies obsolètes est une faute
    système, pas un signal.
    """
    lim = dict(DEFAULT_FRESHNESS_MIN)
    lim.update(limits_min or {})
    problems = []
    for name, bars in tf.items():
        gran = grans.get(name)
        if not bars:
            problems.append(f"{name} : aucune bougie")
            continue
        if gran is None:
            continue
        age = now - (bars[-1]["epoch"] + gran)
        max_age = lim.get(name, 3 * gran // 60) * 60
        if age > max_age:
            problems.append(f"{name} : dernière clôture il y a {age // 60} min "
                            f"(limite {max_age // 60})")
    return problems


def check_still_valid(entry_epoch: int, direction: str, sl_price: float,
                      tp_price: float, m5: List[dict]) -> Optional[str]:
    """Garde C1 : le SL/TP n'est pas déjà touché sur les M5 POSTÉRIEURES.

    Seules comptent les M5 clôturées APRÈS la barre signal M15 (epoch ≥
    entry+900 = information strictement plus fraîche que la décision).
    Retourne None si valide (ou si aucune M5 plus fraîche : on émet —
    limite documentée), sinon le motif d'invalidation.
    """
    newer = [c["close"] for c in m5 if c["epoch"] >= entry_epoch + 900]
    if not newer:
        return None
    if direction == "bullish":
        if min(newer) <= sl_price:
            return f"SL {sl_price:.2f} déjà traversé (M5 à {min(newer):.2f})"
        if max(newer) >= tp_price:
            return f"TP {tp_price:.2f} déjà atteint (M5 à {max(newer):.2f})"
    else:
        if max(newer) >= sl_price:
            return f"SL {sl_price:.2f} déjà traversé (M5 à {max(newer):.2f})"
        if min(newer) <= tp_price:
            return f"TP {tp_price:.2f} déjà atteint (M5 à {min(newer):.2f})"
    return None


def process_decision(dec: Any, inst_name: str, symbol: str, db_path: str, now: int,
                     threshold: int, cooldown: int, max_day: int,
                     stake: float) -> tuple:
    """Seuil + anti-spam + sauvegarde. Partagée live (run_cycle) et replay.

    Retourne (Signal|None, ligne de log). `now` = horloge du cycle (murale en
    live, curseur en replay). `cooldown` en secondes.

    C1 : l'id est DÉTERMINISTE (instrument + direction + epoch de la barre
    M15 signal). Deux runs traitant la même barre produisent la même ligne :
    le 2ᵉ reçoit (None, '♻️ doublon') et ne ré-émet RIEN.
    """
    assert dec.score is not None
    if dec.score < threshold:
        return None, (f"⏸️ {dec.direction} score {dec.score} < {threshold} "
                      f"(détail {dec.breakdown})")
    last = db.last_created(db_path, inst_name)
    if last is not None and now - last < cooldown:
        return None, (f"⏸️ cooldown : dernier signal {inst_name} il y a "
                      f"{(now - last) // 60} min (< {cooldown // 60})")
    if db.count_since(db_path, inst_name, _day_start_utc(now)) >= max_day:
        return None, f"⏸️ quota {max_day}/jour atteint pour {inst_name}"
    sig = Signal(
        id=f"{inst_name}-{dec.direction}-{dec.plan['entry_epoch']}", instrument=inst_name,
        symbol=symbol, direction=dec.direction, created_epoch=now,
        entry_epoch=dec.plan["entry_epoch"], entry=dec.plan["entry"],
        sl_pts=dec.plan["sl_pts"], tp_pts=dec.plan["tp_pts"],
        sl_price=dec.plan["sl_price"], tp_price=dec.plan["tp_price"],
        stake_usd=stake, confidence=dec.score, grade=dec.grade or "B",
        gates=[g.__dict__ for g in dec.gates],
        confluences=dec.confluences, context=dec.context_snapshot)
    if not db.save_signal(db_path, sig):
        return None, (f"♻️ doublon idempotent : {sig.id} déjà enregistré "
                      f"(0 ré-envoi, notification d'origine conservée)")
    return sig, (f"🟢 SIGNAL {dec.direction} {dec.score}% {dec.grade} — entrée "
                 f"{sig.entry:.2f}, SL {sig.sl_pts:.1f} pts, TP {sig.tp_pts:.1f} pts")


def active_instruments(settings: Dict[str, Any]) -> Dict[str, dict]:
    """Instruments tradés : `enabled: false` = pause (défaut True si clé absente)."""
    return {n: i for n, i in settings["instruments"].items() if i.get("enabled", True)}


def run_cycle(settings: Dict[str, Any], db_path: Optional[str] = None,
              provider: Optional[DerivProvider] = None,
              now_epoch: Optional[int] = None,
              notify: bool = False) -> CycleResult:
    res = CycleResult()
    now = now_epoch if now_epoch is not None else int(time.time())
    db_path = db_path or settings.get("storage", {}).get("path", "data/signals.db")
    db.init_db(db_path)
    # Paper H-SL-ADAPTIVE (miroir, live inchange - decision 07/09/2026).
    paper_cfg = settings.get("paper", {})
    paper_on = bool(paper_cfg.get("enabled", False))
    paper_db = paper_cfg.get("db_path", "data/paper.db")
    if paper_on:
        from ..paper import sync as paper_sync  # lazy : comme notify, 0 risque d'import
        try:
            paper_sync.check_frozen(paper_db)
        except Exception as exc:
            res.errors.append(f"paper GELE : {exc}")
            paper_on = False
    own_provider = provider is None
    if own_provider:
        cfg = settings["deriv"]
        provider = DerivProvider(ws_url=cfg["ws_url"], cache_dir=settings["cache"]["dir"],
                                 timeout=cfg["timeout"],
                                 throttle_seconds=cfg["throttle_seconds"],
                                 max_retries=cfg["max_retries"],
                                 backoff_base=cfg["backoff_base"])
    assert provider is not None
    try:
        P = dict(settings.get("strategy", {}))
        P["synthetics_params"] = settings.get("synthetics", {})
        floors = settings["stops"]
        scoring = settings.get("scoring", {})
        threshold = scoring.get("threshold", 65)
        cooldown = scoring.get("cooldown_minutes", 180) * 60
        max_day = scoring.get("max_per_day_per_instrument", 4)
        stake = settings.get("account", {}).get("stake_usd", 1.0)
        fresh_cfg = settings.get("freshness", {})

        for inst_name, inst in active_instruments(settings).items():
            symbol = inst["symbol"]
            try:
                tf = fetch_tf_data(provider, symbol, settings["timeframes"], settings["counts"])
                ticks = (ensure_ticks(provider, symbol,
                                      P["synthetics_params"].get("jump_threshold_pts", 10.0))
                         if inst_name == "JD10" else None)
                # C1 : garde fraîcheur — périmé ⇒ NO_TRADE (erreur visible, jamais silencieux).
                stale = check_freshness(tf, settings["timeframes"], now,
                                        fresh_cfg.get("max_age_min"))
                if inst_name == "JD10":
                    tlim = fresh_cfg.get("ticks_max_age_min", TICKS_FRESHNESS_MIN) * 60
                    if not ticks:
                        stale.append("ticks JD10 : aucun tick")
                    elif now - ticks[-1]["epoch"] > tlim:
                        stale.append(f"ticks JD10 : dernier il y a "
                                     f"{(now - ticks[-1]['epoch']) // 60} min "
                                     f"(limite {tlim // 60})")
                if stale:
                    res.logs[inst_name] = ("⛔ NO_TRADE (données périmées) : "
                                           + "; ".join(stale))
                    res.errors.append(f"{inst_name} STALE : {'; '.join(stale)}")
                    continue
                ctx = build_contexts(inst_name, tf, ticks, P)
                dec = evaluate_instrument(inst_name, tf, ctx, P, floors)
                if not dec.passed:
                    res.logs[inst_name] = f"🔴 bloqué par : {dec.blocked_by}"
                    continue
                sig, line = process_decision(dec, inst_name, symbol, db_path, now,
                                             threshold, cooldown, max_day, stake)
                if sig is not None:
                    # C1 : garde SL/TP-déjà-touché — entrée supprimée (marquée
                    # -2, jamais envoyée), suivi tracker conservé (compta R honnête).
                    invalid = check_still_valid(sig.entry_epoch, sig.direction,
                                                sig.sl_price, sig.tp_price, tf["M5"])
                    if invalid is not None:
                        db.mark_notified(db_path, sig.id, "entry", at_epoch=-2)
                        line += f" ⛔ invalidé avant envoi : {invalid} (suivi conservé)"
                        res.logs[inst_name] = line
                        continue
                    res.signals.append(sig)
                    if paper_on:
                        try:
                            line += paper_sync.mirror_signal(
                                paper_db, sig, tf, sig.entry_epoch + 900, now)
                        except Exception as exc:  # noqa: BLE001 - le paper ne bloque jamais le live
                            res.errors.append(f"paper miroir {sig.id}: {exc}")
                res.logs[inst_name] = line
            except Exception as exc:  # noqa: BLE001
                res.logs[inst_name] = f"❌ erreur données/stratégie : {exc}"
                res.errors.append(f"{inst_name}: {exc}")
        try:
            res.tracker = tracker_update(
                db_path, provider,
                expiry_bars=P.get("expiry_bars_m15", 96))
        except Exception as exc:  # noqa: BLE001
            res.errors.append(f"tracker: {exc}")
        if paper_on:
            try:
                paper_closed = paper_sync.update_paper(
                    paper_db, provider, P.get("expiry_bars_m15", 96))
                n_snap = paper_sync.snapshot_live_closes(
                    paper_db, db_path, provider, res.tracker or [])
                res.paper = {"closed": len(paper_closed),
                             "r_closed": round(sum(o["r"] for o in paper_closed), 2),
                             "live_snapshots": n_snap}
            except Exception as exc:  # noqa: BLE001 - le paper ne bloque jamais le live
                res.errors.append(f"paper: {exc}")
        if notify:  # étape 5+ : entrées + clôtures, idempotent (C1)
            try:
                from ..notify.telegram import notify_closes, notify_signals
                tw = settings.get("telegram", {})
                entry_win = int(tw.get("entry_retry_min", 120)) * 60
                close_win = int(tw.get("close_retry_hours", 24)) * 3600
                # Les impayés trop vieux sont enterrés (un rattrapage tardif
                # spammerait un prix irréaliste) AVANT toute collecte.
                db.expire_stale_pending(db_path, now, entry_win, close_win)

                def _sid(x: Any) -> str:
                    return x.get("id") if isinstance(x, dict) else getattr(x, "id", "?")

                # -- entrées : fraîches (ce cycle) puis impayées (runs
                # précédents), dédupliquées par signal_id.
                seen, entries = set(), []
                for s in list(res.signals) + db.get_pending_entries(db_path, now, entry_win):
                    if _sid(s) in seen:
                        continue
                    seen.add(_sid(s))
                    row = db.get_signal(db_path, _sid(s))  # re-vérification pré-envoi
                    if row is not None and (row.get("notified_entry_at") or 0) == 0:
                        entries.append(s)
                for r in notify_signals(entries):
                    if r.get("status") == "sent":
                        db.mark_notified(db_path, r.get("signal_id"), "entry", now)
                        if r.get("instrument") in res.logs:
                            res.logs[r["instrument"]] += " 📩"
                    else:
                        res.errors.append(
                            f"telegram {r.get('signal_id')}: "
                            f"{r.get('reason', r.get('error'))}")
                # -- clôtures : fraîches (résolues ce cycle, _fresh) puis impayées.
                closes = [o for o in (res.tracker or [])
                          if o.get("result") in ("TP", "SL", "EXPIRE") and o.get("_fresh", True)]
                items = [(sg, o) for o in closes
                         for sg in [db.get_signal(db_path, o.get("signal_id"))]
                         if sg is not None]
                have = {sg["id"] for sg, _ in items}
                items += [(s, o) for s, o in db.get_pending_closes(db_path, now, close_win)
                          if s["id"] not in have]
                to_send = []
                for sg, o in items:
                    row = db.get_signal(db_path, sg["id"])  # re-vérification pré-envoi
                    if row is not None and (row.get("notified_close_at") or 0) == 0:
                        to_send.append((sg, o))
                if to_send:
                    stats = db.get_stats(db_path)
                    for r in notify_closes(to_send, stats):
                        if r.get("status") == "sent":
                            db.mark_notified(db_path, r.get("signal_id"), "close", now)
                            if r.get("instrument") in res.logs:
                                res.logs[r["instrument"]] += " 📪"
                        else:
                            res.errors.append(
                                f"telegram clôture {r.get('signal_id')}: "
                                f"{r.get('reason', r.get('error'))}")
            except Exception as exc:  # noqa: BLE001
                res.errors.append(f"telegram: {exc}")
    finally:
        if own_provider:
            provider.close()
    return res
