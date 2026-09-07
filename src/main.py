"""
Point d'entrée du robot — Étape 1 : vérification données Deriv.

Usage :
    python -m src.main --once              # 1 cycle : télécharge + valide tout, affiche le résumé
    python -m src.main --once --fast       # idem avec 100 bougies/TF (rapide, pour CI)
    python -m src.main --symbol JD10       # un seul instrument
"""
from __future__ import annotations

import argparse
import logging
import os
import sys

import yaml

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.data.deriv_provider import DerivProvider
from src.signals.engine import run_cycle
from src.storage import database as db

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("main")


def load_settings(path: str = "config/settings.yaml") -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def run_once(settings: dict, only_symbol: str | None = None, fast: bool = False) -> bool:
    cfg = settings["deriv"]
    provider = DerivProvider(
        ws_url=cfg["ws_url"],
        cache_dir=settings["cache"]["dir"],
        timeout=cfg["timeout"],
        throttle_seconds=cfg["throttle_seconds"],
        max_retries=cfg["max_retries"],
        backoff_base=cfg["backoff_base"],
        use_cache=settings["cache"]["enabled"] and not fast,
    )
    counts = {tf: (100 if fast else n) for tf, n in settings["counts"].items()}
    all_ok = True
    try:
        latency = provider.ping()
        print(f"\n📡 Deriv ping : {latency * 1000:.0f} ms ({cfg['ws_url']})\n")
        for name, inst in settings["instruments"].items():
            symbol = inst["symbol"]
            if only_symbol and symbol != only_symbol:
                continue
            print(f"━━━ {name} ({symbol}) — {inst['label']} ━━━")
            for tf, gran in settings["timeframes"].items():
                try:
                    candles = provider.get_timeframe(symbol, tf, counts[tf])
                    rep = provider.validate(candles, gran)
                    status = "✅" if rep["ok"] else "❌"
                    age = rep["age_seconds"]
                    age_str = f"{age // 60} min" if age and age >= 60 else f"{age} s"
                    print(
                        f"  {status} {tf:>4} ({gran:>5}s) : {rep['count']:>4} bougies | "
                        f"clôture {rep['last_close_utc']} (il y a {age_str}) | "
                        f"trous={rep['gaps']} dupl={rep['duplicates']} ohlc_err={rep['ohlc_errors']}"
                    )
                    if not rep["ok"]:
                        all_ok = False
                except Exception as exc:
                    print(f"  ❌ {tf:>4} : ÉCHEC — {exc}")
                    all_ok = False
            print()
    finally:
        provider.close()
    print("RÉSULTAT :", "✅ DONNÉES OK" if all_ok else "❌ ANOMALIES DÉTECTÉES (voir lignes ❌)")
    return all_ok


def run_cycle_cmd(settings: dict) -> None:
    """Cycle d'analyse complet : stratégie + scoring + anti-spam + tracker."""
    res = run_cycle(settings)
    print("\n━━━ CYCLE D'ANALYSE ━━━")
    for inst, line in res.logs.items():
        print(f"  {inst:>9} : {line}")
    for s in res.signals:
        print(f"\n  🟢 {s.id} — {s.side_label} {s.instrument} @ {s.entry:.2f} "
              f"| SL {s.sl_pts:.1f} pts | TP {s.tp_pts:.1f} pts "
              f"| {s.confidence}% {s.grade}")
        for c in s.confluences:
            print(f"      ✨ {c}")
    for t in res.tracker:
        if t["result"] != "ERROR":
            print(f"\n  🔒 {t['signal_id']} → {t['result']} ({t['r']:+.2f}R, "
                  f"{t['points']:+.1f} pts, {t['bars_held']} barres)")
    for e in res.errors:
        print(f"  ⚠️ {e}")
    stats = db.get_stats(settings.get("storage", {}).get("path", "data/signals.db"))
    print(f"\n  📊 stats : {stats['n']} clôturés ({stats['TP']} TP / {stats['SL']} SL / "
          f"{stats['EXPIRE']} EXPIRE), R total {stats['r_total']:+.2f}, "
          f"{stats['open']} ouvert(s)")


def main() -> None:
    ap = argparse.ArgumentParser(description="Robot signaux Deriv")
    ap.add_argument("--once", action="store_true", help="Contrôle données unique puis quitte")
    ap.add_argument("--cycle", action="store_true", help="Cycle d'analyse complet (Étape 3+)")
    ap.add_argument("--symbol", default=None, help="Un seul symbole API (ex : JD10)")
    ap.add_argument("--fast", action="store_true", help="100 bougies/TF (rapide, pour CI)")
    ap.add_argument("--settings", default="config/settings.yaml")
    args = ap.parse_args()
    settings = load_settings(args.settings)
    if args.cycle:
        run_cycle_cmd(settings)
        sys.exit(0)
    ok = run_once(settings, only_symbol=args.symbol, fast=args.fast)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
