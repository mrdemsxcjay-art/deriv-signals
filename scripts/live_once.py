"""
Cycle live unique — point d'entrée CI (GitHub Actions) et runs manuels.

- Exécute run_cycle(notify=True) : analyse → signaux → Telegram → tracker.
- Affiche un résumé. Exit 1 si erreurs (échec visible, jamais silencieux).

Usage :
  export TELEGRAM_BOT_TOKEN=... TELEGRAM_CHAT_ID=...
  python scripts/live_once.py [--no-notify] [--db data/signals.db]
"""
import argparse
import os
import sys
import traceback

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import yaml  # noqa: E402

from src.signals.engine import run_cycle  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description="Cycle live unique (Deriv → Telegram).")
    ap.add_argument("--settings", default="config/settings.yaml")
    ap.add_argument("--db", default=None, help="défaut : storage.path du yaml")
    ap.add_argument("--no-notify", action="store_true",
                    help="n'envoie rien sur Telegram (dry-run)")
    args = ap.parse_args()

    with open(args.settings, "r", encoding="utf-8") as f:
        settings = yaml.safe_load(f)
    if args.db:
        settings.setdefault("storage", {})["path"] = args.db

    try:
        res = run_cycle(settings, db_path=args.db, notify=not args.no_notify)
    except Exception:
        traceback.print_exc()
        print("❌ CYCLE CRASHÉ (voir traceback ci-dessus)")
        sys.exit(1)

    print("=" * 64)
    print(f"CYCLE LIVE — signaux émis : {len(res.signals)}"
          + (" (dry-run, 0 envoi)" if args.no_notify else ""))
    for inst, line in res.logs.items():
        print(f"  {inst:>9} : {line}")
    if res.tracker:
        for t in res.tracker:
            print(f"  tracker : {t}")
    if res.errors:
        print("ERREURS :")
        for e in res.errors:
            print(f"  ❌ {e}")
    print("=" * 64)
    sys.exit(1 if res.errors else 0)


if __name__ == "__main__":
    main()
