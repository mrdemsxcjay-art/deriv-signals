"""
Test ÉTAPE 6 — Page statut : génération sur base réaliste / vide / absente.

Exécution : python scripts/test_status.py
"""
import os
import shutil
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import yaml  # noqa: E402

from make_status import build, read_db  # noqa: E402

CHECKS = []


def check(name):
    def deco(fn):
        CHECKS.append((name, fn))
        return fn
    return deco


def settings():
    with open("config/settings.yaml", "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


@check("1/3 Base réaliste (93 signaux C3) — stats + tableau exacts")
def t_real():
    tmp = os.path.join(tempfile.mkdtemp(), "s.db")
    shutil.copy("data/replay_final.db", tmp)
    rows, stats = read_db(tmp)
    assert stats["n"] == 93 and len(rows) == 20, stats
    assert abs(stats["r_total"] - 23.0) < 1e-9, stats
    assert stats["open"] == 0 and stats["by_inst"] == {"JD10": 37, "BOOM1000": 56}
    m = build(settings(), rows, stats)
    for needle in ("🤖 Robot signaux Deriv — statut", "En ligne",
                   "🟢 JD10", "🟢 BOOM1000", "⏸️ V10", "Seuil 65",
                   "+23.0R", "93</b><span>signaux au total",
                   "ACHAT", "VENTE", "✅ TP", "🛑 SL",
                   'http-equiv="refresh"', "aucun ordre exécuté"):
        assert needle in m, needle
    print("   93 signaux, +23.0R, chips actifs/pause ✅")


@check("2/3 Base vide — page valide, zéros, mention explicite")
def t_empty():
    tmp = os.path.join(tempfile.mkdtemp(), "e.db")
    con = sqlite3.connect(tmp)
    con.execute("CREATE TABLE signals (id TEXT, instrument TEXT, direction TEXT, "
                "confidence INT, grade TEXT, created_epoch INT, entry REAL, "
                "sl_pts REAL, tp_pts REAL, status TEXT)")
    con.execute("CREATE TABLE outcomes (signal_id TEXT, result TEXT, r REAL)")
    con.commit()
    con.close()
    rows, stats = read_db(tmp)
    assert rows == [] and stats["n"] == 0 and stats["r_total"] == 0.0
    m = build(settings(), rows, stats)
    assert "Aucun signal pour le moment" in m and "+0.0R" in m
    print("   fallback vide ✅")


@check("3/3 Base absente — jamais de crash")
def t_missing():
    rows, stats = read_db(os.path.join(tempfile.mkdtemp(), "nope.db"))
    assert rows == [] and stats["n"] == 0
    m = build(settings(), rows, stats)
    assert "Aucun signal pour le moment" in m
    print("   fallback absent ✅")


def main():
    print("=" * 64)
    print("TEST ÉTAPE 6 — Page statut (génération)")
    print("=" * 64)
    passed = 0
    for name, fn in CHECKS:
        print(f"\n▶ {name}")
        try:
            fn()
            print(f"  → {name} : PASS ✅")
            passed += 1
        except AssertionError as e:
            print(f"  → {name} : FAIL ❌ — {e}")
        except Exception as e:  # noqa: BLE001
            print(f"  → {name} : ERREUR ❌ — {type(e).__name__}: {e}")
    print("\n" + "=" * 64)
    print(f"RÉSULTAT : {passed}/{len(CHECKS)} checks passés")
    print("=" * 64)
    sys.exit(0 if passed == len(CHECKS) else 1)


if __name__ == "__main__":
    main()
