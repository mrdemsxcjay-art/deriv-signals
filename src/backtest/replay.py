"""
Moteur de replay : rejoue N jours à pas fixe avec le CODE DE PRODUCTION
(mêmes portes, scoring, anti-spam via engine.process_decision, tracker).

- Données : historiques profonds chargés une fois (history.py) + détecteurs
  pré-calculés (views.py). Chaque cycle ne filtre que le curseur (rapide, exact).
- Jumps JD10 : contexte GELÉ (échantillon ~40k ticks) — distribution stationnaire.
- Résolutions TP/SL/EXPIRE via tracker.resolve_signal sur les vraies bougies futures.
- Rapport : débit/jour, winrate, R total/moyen, drawdown max, tenue moyenne,
  distribution des scores, portes bloquantes — par instrument + total.
"""
from __future__ import annotations

import logging
import os
import sqlite3
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from ..agents.strategy_agent import evaluate_instrument
from ..signals.engine import active_instruments, process_decision
from ..signals.tracker import resolve_signal
from ..storage import database as db
from ..synthetics.context import boom_context, jump_context, vol_stats
from .views import build_symbol_views, count_closed, filter_prep, filter_spikes

log = logging.getLogger("backtest.replay")


@dataclass
class ReplayReport:
    days: float
    cycles: int
    params: dict
    per_instrument: Dict[str, dict] = field(default_factory=dict)
    total: dict = field(default_factory=dict)
    cycles_log: List[dict] = field(default_factory=list)  # 1 ligne / cycle / instrument


class ReplayProvider:
    """Façade minimale pour le tracker : tranches M15 au curseur T."""

    def __init__(self, histories: Dict[str, Dict[str, list]]) -> None:
        self.histories = histories
        self.T = 0

    def set_cursor(self, T: int) -> None:
        self.T = T

    def get_candles(self, symbol: str, granularity: int, count: int) -> List[dict]:
        tf = {300: "M5", 900: "M15", 1800: "M30", 3600: "H1",
              14400: "H4", 86400: "D1"}[granularity]
        bars = [c for c in self.histories[symbol][tf]
                if c["epoch"] + granularity <= self.T]
        return bars[-count:]

    def close(self) -> None:
        pass


def _day_start_utc_ts(now: int) -> int:
    from datetime import datetime, timezone
    return int(datetime.fromtimestamp(now, tz=timezone.utc
                                      ).replace(hour=0, minute=0, second=0).timestamp())


def _max_drawdown(equity: List[float]) -> float:
    peak, dd = 0.0, 0.0
    for v in equity:
        peak = max(peak, v)
        dd = max(dd, peak - v)
    return dd


def run_replay(histories: Dict[str, Dict[str, list]], ticks_sample: List[dict],
               settings: Dict[str, Any], days: float = 21.0, step_min: int = 15,
               db_path: str = "data/replay.db",
               overrides: Optional[Dict[str, Any]] = None,
               verbose: bool = True, end_offset_days: float = 0.0) -> ReplayReport:
    t_start = time.time()
    if os.path.exists(db_path):
        os.remove(db_path)
    db.init_db(db_path)

    # --- paramètres (overrides pointés : "scoring.threshold", "stops.JD10", ...) ---
    import copy
    settings = copy.deepcopy(settings)
    for dotted, val in (overrides or {}).items():
        node = settings
        *path, key = dotted.split(".")
        for p in path:
            if not isinstance(node, dict) or p not in node:
                raise KeyError(f"override inconnu : {dotted!r} (section {p!r} absente)")
            node = node[p]
        if not isinstance(node, dict) or key not in node:
            raise KeyError(f"override inconnu : {dotted!r} (clé {key!r} absente) — "
                           f"clés valides : {sorted(node) if isinstance(node, dict) else '?'}")
        node[key] = val
    P = dict(settings.get("strategy", {}))
    P["synthetics_params"] = settings.get("synthetics", {})
    floors = settings["stops"]
    scoring, account = settings.get("scoring", {}), settings.get("account", {})
    threshold = scoring.get("threshold", 65)
    cooldown = scoring.get("cooldown_minutes", 180) * 60
    max_day = scoring.get("max_per_day_per_instrument", 4)
    stake = account.get("stake_usd", 1.0)
    expiry = P.get("expiry_bars_m15", 96)

    # --- vues pré-calculées + contexte jumps gelé ---
    views = {}
    for name, inst in active_instruments(settings).items():
        h = histories[inst["symbol"]]
        views[inst["symbol"]] = build_symbol_views(
            h["M15"], h["H4"], h["D1"], h["M5"],
            strength=P.get("swing_strength", 2),
            spike_window=settings["synthetics"].get("spike_window", 100),
            spike_mult=settings["synthetics"].get("spike_multiplier", 3.0))
    jump_frozen = jump_context(ticks_sample,
                               threshold=settings["synthetics"].get("jump_threshold_pts", 10.0))
    if verbose:
        log.info("vues pré-calculées + jumps gelés (n=%d, méd=%s)",
                 jump_frozen["n_jumps"], jump_frozen["median_size"])

    # --- curseurs : clôtures M15 sur la fenêtre (pas step_min) ---
    _act = active_instruments(settings)
    assert _act, "aucun instrument actif (tous enabled: false ?)"
    ref = histories[next(iter(_act.values()))["symbol"]]["M15"]
    T_end = ref[-1]["epoch"] + 900 - int(end_offset_days * 86400)
    T0 = T_end - int(days * 86400)
    closes = [c["epoch"] + 900 for c in ref if T0 <= c["epoch"] + 900 <= T_end]
    step = max(1, step_min // 15)
    cursors = closes[::step]
    provider = ReplayProvider(histories)
    gran = settings["timeframes"]
    report = ReplayReport(days=days, cycles=len(cursors), params=overrides or {})

    for k, T in enumerate(cursors):
        if verbose and (k % 400 == 0 or k == len(cursors) - 1):
            log.info("replay %d/%d (%s%%)", k + 1, len(cursors),
                     round(100 * (k + 1) / len(cursors)))
        provider.set_cursor(T)
        for name, inst in active_instruments(settings).items():
            sym = inst["symbol"]
            h, v = histories[sym], views[sym]
            n = {tf: count_closed([c["epoch"] for c in h[tf]], gran[tf], T)
                 for tf in gran}
            if min(n["D1"], n["H4"], n["H1"], n["M15"]) < 5:
                continue  # warmup insuffisant en début de fenêtre
            tf = {t: h[t][:n[t]] for t in gran}
            prep = {"D1": filter_prep(v["D1"], n["D1"], False),
                    "H4": filter_prep(v["H4"], n["H4"], True),
                    "M15": filter_prep(v["M15"], n["M15"], True)}
            ctx: Dict[str, Any] = {"vol": vol_stats(tf["M15"])}
            if name == "BOOM1000":
                ctx["boom"] = boom_context(
                    tf["M5"], tf["H1"],
                    multiplier=settings["synthetics"].get("spike_multiplier", 3.0),
                    spikes=filter_spikes(v["spikes"], n["M5"]))
            else:
                ctx["jump"] = jump_frozen
            dec = evaluate_instrument(name, tf, ctx, P, floors, prep)
            row: Dict[str, Any] = {"T": T, "instrument": name, "passed": dec.passed,
                                   "score": dec.score, "grade": dec.grade,
                                   "blocked": (dec.blocked_by or "").split(":")[0]
                                   if not dec.passed else None}
            if not dec.passed:
                report.cycles_log.append(row)
                continue
            sig, line = process_decision(dec, name, sym, db_path, T,
                                         threshold, cooldown, max_day, stake)
            row["emitted"] = sig is not None
            row["emit_note"] = line
            report.cycles_log.append(row)
        # tracker : résout les signaux ouverts avec les bougies ≤ T
        for s in db.get_open_signals(db_path):
            out = resolve_signal(s, provider.get_candles(s["symbol"], 900, 400),
                                 expiry_bars=expiry)
            if out is not None:
                db.close_signal(db_path, out)

    # --- rapport ---
    with sqlite3.connect(db_path) as con:
        cur = con.execute("""SELECT s.instrument, s.id, s.confidence, s.grade, s.direction,
                                    s.created_epoch, o.result, o.r, o.points, o.bars_held,
                                    o.closed_epoch
                             FROM signals s LEFT JOIN outcomes o ON o.signal_id = s.id
                             ORDER BY s.created_epoch""")
        rows = cur.fetchall()
    by_inst: Dict[str, list] = {}
    for r in rows:
        by_inst.setdefault(r[0], []).append(r)
    for name in active_instruments(settings):
        inst_rows = by_inst.get(name, [])
        closed = [r for r in inst_rows if r[6] is not None]
        equity, run = [], 0.0
        for r in sorted(closed, key=lambda x: x[10]):
            run += float(r[7])
            equity.append(run)
        tps = sum(1 for r in closed if r[6] == "TP")
        sls = sum(1 for r in closed if r[6] == "SL")
        exs = sum(1 for r in closed if r[6] == "EXPIRE")
        scores = [r[2] for r in inst_rows]
        passed_scores = [c["score"] for c in report.cycles_log
                         if c["instrument"] == name and c["passed"]]
        blocks = Counter(c["blocked"] for c in report.cycles_log
                         if c["instrument"] == name and not c["passed"])
        report.per_instrument[name] = {
            "n": len(inst_rows), "per_day": round(len(inst_rows) / days, 2),
            "TP": tps, "SL": sls, "EXPIRE": exs,
            "winrate": round(tps / len(closed), 3) if closed else None,
            "r_total": round(run, 2), "r_avg": round(run / len(closed), 2) if closed else None,
            "max_dd": round(_max_drawdown(equity), 2),
            "avg_bars": round(sum(r[9] for r in closed) / len(closed), 1) if closed else None,
            "open_left": len(inst_rows) - len(closed),
            "score_min": min(scores) if scores else None,
            "score_avg": round(sum(scores) / len(scores), 1) if scores else None,
            "n_passed": len(passed_scores),
            "blocked_top": dict(blocks.most_common(5)),
        }
    all_closed = [r for rows_ in by_inst.values() for r in rows_ if r[6] is not None]
    equity, run = [], 0.0
    for r in sorted(all_closed, key=lambda x: x[10]):
        run += float(r[7])
        equity.append(run)
    report.total = {
        "n": len(rows), "per_day": round(len(rows) / days, 2),
        "TP": sum(1 for r in all_closed if r[6] == "TP"),
        "SL": sum(1 for r in all_closed if r[6] == "SL"),
        "EXPIRE": sum(1 for r in all_closed if r[6] == "EXPIRE"),
        "winrate": round(sum(1 for r in all_closed if r[6] == "TP") / len(all_closed), 3)
        if all_closed else None,
        "r_total": round(run, 2),
        "r_avg": round(run / len(all_closed), 2) if all_closed else None,
        "max_dd": round(_max_drawdown(equity), 2),
        "open_left": len(rows) - len(all_closed),
        "minutes": round((time.time() - t_start) / 60, 1),
    }
    if verbose:
        log.info("replay terminé : %d signaux (%.2f/j), R %+.2f, %s min",
                 report.total["n"], report.total["per_day"],
                 report.total["r_total"], report.total["minutes"])
    return report
