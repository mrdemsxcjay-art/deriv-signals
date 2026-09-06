"""
Test ÉTAPE 6 — Page statut : génération sur base réaliste / vide / absente.

Exécution : python scripts/test_status.py
"""
import os
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


@check("1/3 Base réaliste construite (93 signaux, +23R) — stats + tableau exacts")
def t_real():
    from src.storage import database as db
    tmp = os.path.join(tempfile.mkdtemp(), "s.db")
    db.init_db(tmp)
    base = 1_700_000_000
    # Miroir C3 : 37 JD10 (12 TP / 25 SL → +11R) + 56 BOOM (17 TP / 39 SL → +12R)
    tp_jd, tp_bo = set(range(0, 37, 3)[:12]), set(range(0, 56, 3)[:17])  # TP répartis (visibles dans les 20 affichés)
    assert len(tp_jd) == 12 and len(tp_bo) == 17
    jd = [("JD10", "bearish", i, i in tp_jd) for i in range(37)]
    bo = [("BOOM1000", "bullish", i, i in tp_bo) for i in range(56)]
    tail = [x for pair in zip(jd[27:], bo[46:]) for x in pair]  # 20 alternés
    specs = jd[:27] + bo[:46] + tail  # les 20 affichés mixent ACHAT/VENTE
    assert len(specs) == 93
    for k, (inst, direction, i, is_tp) in enumerate(specs, start=1):
            sid = f"{inst}-{direction}-{base + k}"
            db.save_signal(tmp, {"id": sid, "instrument": inst, "symbol": inst,
                                 "direction": direction, "created_epoch": base + k,
                                 "entry_epoch": base, "entry": 100.0,
                                 "sl_pts": 25.0, "tp_pts": 75.0,
                                 "sl_price": 125.0, "tp_price": 25.0,
                                 "stake_usd": 1.0, "confidence": 75, "grade": "A"})
            db.close_signal(tmp, {"signal_id": sid, "closed_epoch": base + k + 900,
                                  "result": "TP" if is_tp else "SL",
                                  "r": 3.0 if is_tp else -1.0,
                                  "points": 75.0 if is_tp else -25.0,
                                  "bars_held": 5, "exit_price": 25.0, "note": ""})
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
