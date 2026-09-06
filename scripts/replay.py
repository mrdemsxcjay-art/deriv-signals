"""
CLI replay — calibration sur N jours de données réelles (Étape 4).

Usage :
  python scripts/replay.py --days 21
  python scripts/replay.py --days 21 --set scoring.threshold=70 --set stops.V10=30
  python scripts/replay.py --days 21 --refresh          # re-télécharge tout
  python scripts/replay.py --days 7 --step-min 60       # rapide (test)

Les historiques sont cachés dans data/backtest/ (runs suivants sans réseau).
"""
import argparse
import json
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import yaml

from src.backtest.history import (
    fetch_ticks_sample, load_cache, load_histories, save_cache,
)
from src.backtest.replay import run_replay
from src.data.deriv_provider import DerivProvider

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("replay-cli")


def parse_set(pairs):
    out = {}
    for p in pairs or []:
        k, v = p.split("=", 1)
        try:
            out[k] = int(v)
        except ValueError:
            try:
                out[k] = float(v)
            except ValueError:
                out[k] = v
        # booléens éventuels
        if out[k] == "true":
            out[k] = True
        elif out[k] == "false":
            out[k] = False
    return out


def main():
    ap = argparse.ArgumentParser(description="Replay de calibration (Étape 4)")
    ap.add_argument("--days", type=float, default=21.0)
    ap.add_argument("--end-offset-days", type=float, default=0.0,
                    help="fenêtre décalée (validation hors-échantillon)")
    ap.add_argument("--step-min", type=int, default=15)
    ap.add_argument("--db", default="data/replay.db")
    ap.add_argument("--report", default="data/replay_report.json")
    ap.add_argument("--cache-dir", default="data/backtest")
    ap.add_argument("--histories", default=None,
                    help="fichier d'historiques (défaut: histories_{days}d.json) — "
                         "ex: histories_32d.json pour une fenêtre décalée")
    ap.add_argument("--set", action="append", default=[], help="override ex: scoring.threshold=70")
    ap.add_argument("--refresh", action="store_true", help="re-télécharge les historiques")
    ap.add_argument("--refresh-ticks", action="store_true")
    ap.add_argument("--settings", default="config/settings.yaml")
    args = ap.parse_args()

    with open(args.settings, "r", encoding="utf-8") as f:
        settings = yaml.safe_load(f)
    hist_name = args.histories or f"histories_{args.days:g}d.json"
    hist_cache = os.path.join(args.cache_dir, hist_name)
    ticks_cache = os.path.join(args.cache_dir, "JD10_ticks_deep.json")

    provider = DerivProvider(cache_dir=settings["cache"]["dir"])
    try:
        if args.refresh or not os.path.exists(hist_cache):
            log.info("téléchargement historiques (%g j)...", args.days)
            histories = load_histories(provider, settings["instruments"],
                                       settings["timeframes"], days=args.days)
            save_cache(hist_cache, {"days": args.days, "histories": histories})
        else:
            log.info("historiques relus depuis %s", hist_cache)
            histories = load_cache(hist_cache)["histories"]
        if args.refresh_ticks or not os.path.exists(ticks_cache):
            log.info("échantillon ticks JD10 (~40k)...")
            ticks = fetch_ticks_sample(provider, "JD10", 40000)
            save_cache(ticks_cache, {"ticks": ticks})
        else:
            ticks = load_cache(ticks_cache)["ticks"]
        log.info("ticks gelés : %d (%.1f h)", len(ticks),
                 (ticks[-1]["epoch"] - ticks[0]["epoch"]) / 3600 if len(ticks) > 1 else 0)
    finally:
        provider.close()

    overrides = parse_set(args.set)
    report = run_replay(histories, ticks, settings, days=args.days,
                        step_min=args.step_min, db_path=args.db,
                        overrides=overrides, verbose=True,
                        end_offset_days=args.end_offset_days)

    # --- tableau calibration ---
    print("\n" + "=" * 96)
    print(f"REPLAY {args.days:g} j · pas {args.step_min} min · overrides {overrides or 'aucun'}")
    print("=" * 96)
    hdr = f"{'instr':>9} {'n':>4} {'/jour':>6} {'TP':>4} {'SL':>4} {'EX':>4} {'winr':>6} {'R tot':>7} {'R moy':>6} {'DDmax':>6} {'tenue':>6} {'score':>6}"
    print(hdr)
    print("-" * 96)
    for name, s in report.per_instrument.items():
        print(f"{name:>9} {s['n']:>4} {s['per_day']:>6} {s['TP']:>4} {s['SL']:>4} "
              f"{s['EXPIRE']:>4} {str(s['winrate']):>6} {s['r_total']:>+7} "
              f"{str(s['r_avg']):>6} {s['max_dd']:>6} {str(s['avg_bars']):>6} "
              f"{str(s['score_avg']):>6}")
    t = report.total
    print("-" * 96)
    print(f"{'TOTAL':>9} {t['n']:>4} {t['per_day']:>6} {t['TP']:>4} {t['SL']:>4} "
          f"{t['EXPIRE']:>4} {str(t['winrate']):>6} {t['r_total']:>+7} "
          f"{str(t['r_avg']):>6} {t['max_dd']:>6} {'':>6} {'':>6}")
    print(f"(ouverts non résolus en fin de fenêtre : {t['open_left']} · durée {t['minutes']} min)")
    print("\nPortes bloquantes (top 5 / instrument) :")
    for name, s in report.per_instrument.items():
        print(f"  {name:>9} : {s['blocked_top']}")
    print(f"  passed (portes OK, avant seuil/anti-spam) : "
          + ", ".join(f"{k}={v['n_passed']}" for k, v in report.per_instrument.items()))
    print("=" * 96)

    with open(args.report, "w", encoding="utf-8") as f:
        json.dump({"days": report.days, "cycles": report.cycles, "params": report.params,
                   "per_instrument": report.per_instrument, "total": report.total}, f, indent=1)
    print(f"rapport écrit : {args.report} (+ base {args.db})")


if __name__ == "__main__":
    main()
