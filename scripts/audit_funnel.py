"""
AUDIT entonnoir — rejoue les 100 derniers cycles avec le CODE DE PRODUCTION
et les SETTINGS LIVE, par instrument × direction, sans RIEN modifier.

Usage :
  python scripts/audit_funnel.py --settings /tmp/live/settings.yaml --out /tmp/funnel.json

Lecture seule (écrit uniquement --out). Zéro impact live / zéro Telegram.
"""
import argparse
import copy
import json
import os
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import yaml

from src.agents.strategy_agent import decide, decide_boom_buy, evaluate_instrument
from src.backtest.history import load_cache
from src.backtest.views import (
    build_symbol_views, count_closed, filter_prep, filter_spikes,
)
from src.signals.engine import active_instruments
from src.synthetics.context import boom_context, jump_context, vol_stats

N_CYCLES = 100


def classify(blocked_by):
    """Taxonomie d'audit <- détail de blocage production."""
    b = blocked_by or ""
    if "aucune structure" in b or "aucune cassure" in b:
        return "absence BOS/CHoCH"
    if "pas de retest" in b:
        return "absence retest"
    if b.startswith("D1 :") or b.startswith("H4 :"):
        return "structure invalide"
    if b.startswith("H1 :"):
        return "structure invalide"
    if b.startswith("M15 :"):
        return "structure invalide"
    if "BOOM BUY" in b:
        return "filtre spike/jump"
    if "ambigu" in b:
        return "autre (ambiguïté)"
    return "autre"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--settings", default="config/settings.yaml")
    ap.add_argument("--histories", default="data/backtest/histories_52d.json")
    ap.add_argument("--ticks", default="data/backtest/JD10_ticks_deep.json")
    ap.add_argument("--out", default="/tmp/funnel.json")
    ap.add_argument("--offset-cycles", type=int, default=0)
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    settings = yaml.safe_load(open(args.settings, encoding="utf-8"))
    live = yaml.safe_load(open("/tmp/live/settings.yaml", encoding="utf-8"))
    for sec in ("strategy", "stops", "scoring", "synthetics", "timeframes", "instruments"):
        assert settings.get(sec) == live.get(sec), f"section {sec} DIVERGE du live !"
    print("settings : sections utiles IDENTIQUES au live")

    hist = load_cache(args.histories)["histories"]
    ticks = load_cache(args.ticks)["ticks"]
    P = dict(settings.get("strategy", {}))
    P["synthetics_params"] = settings.get("synthetics", {})
    floors = settings["stops"]
    threshold = settings["scoring"]["threshold"]
    cooldown = settings["scoring"]["cooldown_minutes"] * 60
    max_day = settings["scoring"]["max_per_day_per_instrument"]
    gran = settings["timeframes"]

    views = {}
    for name, inst in active_instruments(settings).items():
        h = hist[inst["symbol"]]
        views[inst["symbol"]] = build_symbol_views(
            h["M15"], h["H4"], h["D1"], h["M5"], strength=P.get("swing_strength", 2),
            spike_window=settings["synthetics"].get("spike_window", 100),
            spike_mult=settings["synthetics"].get("spike_multiplier", 3.0))
    jump_frozen = jump_context(
        ticks, threshold=settings["synthetics"].get("jump_threshold_pts", 10.0))
    print(f"jumps gelés : n={jump_frozen['n_jumps']} méd={jump_frozen.get('median_size')} "
          f"p90={jump_frozen.get('p90_size')}")

    ref = hist[next(iter(active_instruments(settings).values()))["symbol"]]["M15"]
    closes = [c["epoch"] + 900 for c in ref]
    off = args.offset_cycles
    end = len(closes) - off
    cursors = closes[end - N_CYCLES:end]
    assert len(cursors) == N_CYCLES, (off, len(cursors))
    print(f"fenêtre : {len(cursors)} cycles, M15 {cursors[0]} -> {cursors[-1]}")

    # Anti-spam simulé (miroir process_decision) ; amorcé avec le signal live réel.
    # Anti-spam amorce avec le signal live reel s'il precede la fenetre.
    seed = [1788729785] if cursors[-1] >= 1788729785 else []
    emitted = {"BOOM1000": list(seed), "JD10": []}  # 06/09 21:23 BOOM SELL live
    rows = []
    for T in cursors:
        for name, inst in active_instruments(settings).items():
            sym = inst["symbol"]
            h, v = hist[sym], views[sym]
            n = {tf: count_closed([c["epoch"] for c in h[tf]], gran[tf], T) for tf in gran}
            data_ok = min(n["D1"], n["H4"], n["H1"], n["M15"]) >= 5 and n["M5"] >= 2
            tf = {t: h[t][:n[t]] for t in gran}
            prep = {"D1": filter_prep(v["D1"], n["D1"], False),
                    "H4": filter_prep(v["H4"], n["H4"], True),
                    "M15": filter_prep(v["M15"], n["M15"], True)}
            ctx = {"vol": vol_stats(tf["M15"])}
            if name == "BOOM1000":
                ctx["boom"] = boom_context(
                    tf["M5"], tf["H1"],
                    multiplier=settings["synthetics"].get("spike_multiplier", 3.0),
                    spikes=filter_spikes(v["spikes"], n["M5"]))
            else:
                ctx["jump"] = jump_frozen
            dirs = [("bearish", lambda d: decide(name, d, tf, ctx, P, floors, prep)),
                    ("bullish", lambda d: decide_boom_buy(tf, ctx, P, floors, prep)
                     if name == "BOOM1000" else decide(name, d, tf, ctx, P, floors, prep))]
            for direction, fn in dirs:
                r = {"T": T, "instrument": name, "direction": direction,
                     "data_ok": data_ok, "gates": {}, "passed": False,
                     "score": None, "breakdown": None, "would_emit": False,
                     "reject": None}
                if not data_ok:
                    r["reject"] = "données manquantes"
                    rows.append(r)
                    continue
                try:
                    dec = fn(direction)
                except Exception as exc:
                    r["reject"] = f"erreur: {exc}"
                    rows.append(r)
                    continue
                for g in dec.gates:
                    r["gates"][g.name] = {"pass": g.passed, "detail": g.detail}
                if not dec.passed:
                    r["reject"] = classify(dec.blocked_by)
                    r["blocked_by"] = dec.blocked_by
                    rows.append(r)
                    continue
                r["passed"] = True
                r["score"] = dec.score
                r["breakdown"] = dec.breakdown
                if dec.score < threshold:
                    r["reject"] = "score insuffisant"
                    rows.append(r)
                    continue
                day0 = T - (T % 86400)
                last = max(emitted[name]) if emitted[name] else None
                if last is not None and T - last < cooldown:
                    r["reject"] = "signal déjà envoyé (cooldown)"
                elif sum(1 for e in emitted[name] if e >= day0) >= max_day:
                    r["reject"] = "signal déjà envoyé (quota)"
                else:
                    r["would_emit"] = True
                    emitted[name].append(T)
                rows.append(r)
            # Arbitre réel (1 décision / instrument / cycle).
            dec = evaluate_instrument(name, tf, ctx, P, floors, prep)
            rows.append({"T": T, "instrument": name, "direction": "*arbiter*",
                         "passed": dec.passed, "score": dec.score,
                         "blocked": dec.blocked_by})

    if args.quiet:
        import datetime
        endd = datetime.datetime.fromtimestamp(cursors[-1], datetime.timezone.utc).strftime("%d/%m")
        sm = {}
        for inst, d in (("BOOM1000", "bullish"), ("BOOM1000", "bearish"),
                        ("JD10", "bullish"), ("JD10", "bearish")):
            rs = [r for r in rows if r.get("instrument") == inst and r.get("direction") == d]
            sm[f"{inst[:4]}{d[:4]}"] = (sum(1 for r in rs if r["passed"]),
                                        sum(1 for r in rs if r["would_emit"]))
        print(f"off={off:>5} fin={endd} " + " ".join(f"{k}p{p}e{e}" for k, (p, e) in sm.items()),
              flush=True)
    json.dump({"settings": args.settings, "cursors": cursors, "rows": rows,
               "jump_frozen": {k: jump_frozen.get(k) for k in
                               ("n_jumps", "median_size", "p90_size")}},
              open(args.out, "w", encoding="utf-8"))
    print(f"-> {args.out} ({len(rows)} lignes)")

    # --- matrice + stats ---
    keys = [("BOOM1000", "bullish"), ("BOOM1000", "bearish"),
            ("JD10", "bullish"), ("JD10", "bearish")]
    print("\n== MATRICE (100 cycles) ==")
    for inst, d in keys:
        rs = [r for r in rows if r.get("instrument") == inst and r.get("direction") == d]
        g = lambda name: sum(1 for r in rs if r["gates"].get(name, {}).get("pass"))
        passed = sum(1 for r in rs if r["passed"])
        emitted_n = sum(1 for r in rs if r["would_emit"])
        scores = sorted(r["score"] for r in rs if r["score"] is not None)
        med = scores[len(scores) // 2] if scores else None
        avg = round(sum(scores) / len(scores), 1) if scores else None
        print(f"{inst:>9} {d:>8} : data={sum(1 for r in rs if r['data_ok']):>3} "
              f"D1={g('D1'):>3} H4={g('H4'):>3} H1={g('H1'):>3} M15={g('M15'):>3} "
              f"SPIKE={g('SPIKE'):>3} passed={passed:>3} score~{avg}/{med} "
              f"seuil65+={sum(1 for s in scores if s >= 65):>3} emits={emitted_n:>3}")
    print("\n== REJETS par raison ==")
    for inst, d in keys:
        rs = [r for r in rows if r.get("instrument") == inst and r.get("direction") == d
              and not r["would_emit"]]
        print(f"  {inst:>9} {d:>8} : {dict(Counter(r['reject'] for r in rs).most_common())}")
    print("\n== ARBITRE (décision live réelle / instrument) ==")
    for inst in ("BOOM1000", "JD10"):
        ar = [r for r in rows if r.get("direction") == "*arbiter*"
              and r["instrument"] == inst]
        p = [r for r in ar if r["passed"]]
        print(f"  {inst:>9} : passed={len(p)}/100 scores={sorted(r['score'] for r in p)}")
        print(f"             bloqué={dict(Counter((r['blocked'] or '').split(':')[0] for r in ar if not r['passed']).most_common(4))}")


if __name__ == "__main__":
    main()
