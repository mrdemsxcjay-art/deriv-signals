"""
Moteur — un cycle : données → stratégie → scoring → anti-spam → SQLite → tracker.

- 1 décision max par instrument et par cycle (jamais 2 signaux opposés).
- Anti-spam : seuil 65 + cooldown 180 min + max 4/jour/instrument (§6).
- Transparence : chaque instrument loggue « bloqué par : … » ou le signal émis.
- Robuste : un instrument en panne n'arrête pas le cycle (erreur loggée).
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
    logs: Dict[str, str] = field(default_factory=dict)
    tracker: List[dict] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)


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


def process_decision(dec: Any, inst_name: str, symbol: str, db_path: str, now: int,
                     threshold: int, cooldown: int, max_day: int,
                     stake: float) -> tuple:
    """Seuil + anti-spam + sauvegarde. Partagée live (run_cycle) et replay.

    Retourne (Signal|None, ligne de log). `now` = horloge du cycle (murale en
    live, curseur en replay). `cooldown` en secondes.
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
        id=f"{inst_name}-{dec.direction}-{now}", instrument=inst_name,
        symbol=symbol, direction=dec.direction, created_epoch=now,
        entry_epoch=dec.plan["entry_epoch"], entry=dec.plan["entry"],
        sl_pts=dec.plan["sl_pts"], tp_pts=dec.plan["tp_pts"],
        sl_price=dec.plan["sl_price"], tp_price=dec.plan["tp_price"],
        stake_usd=stake, confidence=dec.score, grade=dec.grade or "B",
        gates=[g.__dict__ for g in dec.gates],
        confluences=dec.confluences, context=dec.context_snapshot)
    db.save_signal(db_path, sig)
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

        for inst_name, inst in active_instruments(settings).items():
            symbol = inst["symbol"]
            try:
                tf = fetch_tf_data(provider, symbol, settings["timeframes"], settings["counts"])
                ticks = (ensure_ticks(provider, symbol,
                                      P["synthetics_params"].get("jump_threshold_pts", 10.0))
                         if inst_name == "JD10" else None)
                ctx = build_contexts(inst_name, tf, ticks, P)
                dec = evaluate_instrument(inst_name, tf, ctx, P, floors)
                if not dec.passed:
                    res.logs[inst_name] = f"🔴 bloqué par : {dec.blocked_by}"
                    continue
                sig, line = process_decision(dec, inst_name, symbol, db_path, now,
                                             threshold, cooldown, max_day, stake)
                res.logs[inst_name] = line
                if sig is not None:
                    res.signals.append(sig)
            except Exception as exc:  # noqa: BLE001
                res.logs[inst_name] = f"❌ erreur données/stratégie : {exc}"
                res.errors.append(f"{inst_name}: {exc}")
        try:
            res.tracker = tracker_update(
                db_path, provider,
                expiry_bars=P.get("expiry_bars_m15", 96))
        except Exception as exc:  # noqa: BLE001
            res.errors.append(f"tracker: {exc}")
        if notify:  # étape 5+ : envois Telegram entrée + clôtures (jamais bloquants)
            try:
                from ..notify.telegram import notify_closes, notify_signals
                for r in notify_signals(res.signals):
                    if r.get("status") == "sent" and r.get("instrument") in res.logs:
                        res.logs[r["instrument"]] += " 📩"
                    elif r.get("status") != "sent":
                        res.errors.append(
                            f"telegram {r.get('signal_id')}: "
                            f"{r.get('reason', r.get('error'))}")
                closes = [o for o in (res.tracker or [])
                          if o.get("result") in ("TP", "SL", "EXPIRE")]
                if closes:
                    stats = db.get_stats(db_path)
                    items = [(sg, o) for o in closes
                             for sg in [db.get_signal(db_path, o.get("signal_id"))]
                             if sg is not None]
                    for r in notify_closes(items, stats):
                        if r.get("status") == "sent" and r.get("instrument") in res.logs:
                            res.logs[r["instrument"]] += " 📪"
                        elif r.get("status") != "sent":
                            res.errors.append(
                                f"telegram clôture {r.get('signal_id')}: "
                                f"{r.get('reason', r.get('error'))}")
            except Exception as exc:  # noqa: BLE001
                res.errors.append(f"telegram: {exc}")
    finally:
        if own_provider:
            provider.close()
    return res
