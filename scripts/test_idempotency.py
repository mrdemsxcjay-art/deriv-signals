"""
Test C1 — Idempotence : 1 signal / 1 clôture / 1 outcome / 1 comptabilisation R /
1 notification, même en cas de double run, retry, panne d'envoi ou restauration
périmée. Zéro réseau (providers factices à cohérence temporelle).

Exécution : python scripts/test_idempotency.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.agents.strategy_agent import Decision, GateResult
from src.notify import telegram as TG
from src.signals import engine as ENG
from src.signals.tracker import resolve_signal
from src.storage import database as db

CHECKS = []


def check(name):
    def deco(fn):
        CHECKS.append((name, fn))
        return fn
    return deco


GRANS = {"M5": 300, "M15": 900, "M30": 1800, "H1": 3600, "H4": 14400, "D1": 86400}
NOON = (1_700_000_000 // 86400) * 86400 + 12 * 3600


def fresh_bars(now, gran, n=3, price=10.0):
    last_epoch = (now - 60 - gran) // gran * gran
    return [{"epoch": last_epoch - (n - 1 - i) * gran, "open": price,
             "high": price + 0.1, "low": price - 0.1, "close": price + 0.05}
            for i in range(n)]


class FakeLiveProvider:
    """Bougies fraîches ancrées sur now() + ticks frais (zéro réseau)."""

    def __init__(self, now_fn, m5=None):
        self._now = now_fn
        self._m5 = m5

    def get_timeframe(self, symbol, tf, count):
        if tf == "M5" and self._m5 is not None:
            return self._m5
        return fresh_bars(self._now(), GRANS[tf])

    def get_candles(self, symbol, gran, count):
        return fresh_bars(self._now(), gran)

    def get_ticks(self, symbol, count=2000, end="latest", use_cache=None,
                  max_cache=40000):
        now = self._now()
        return [{"epoch": now - 60 + i, "price": 100.0} for i in range(25)]

    def close(self):
        pass


def fake_eval_factory(state):
    def _eval(instrument, tf, ctx, P, floors, prep=None):
        return Decision(
            instrument, "bearish", True,
            gates=[GateResult("D1", "bearish", True, "porte factice")],
            score=state["score"], grade="A", breakdown={"base": 50},
            plan={"entry": 10.0, "sl_pts": 25.0, "tp_pts": 75.0,
                  "sl_price": 35.0, "tp_price": -65.0,
                  "entry_epoch": tf["M15"][-1]["epoch"]},
            confluences=["factice"], context_snapshot={})
    return _eval


def base_settings(instruments=("BOOM1000",)):
    sym = {"JD10": "JD10", "BOOM1000": "BOOM1000"}
    return {"instruments": {n: {"symbol": sym[n]} for n in instruments},
            "timeframes": dict(GRANS), "counts": {k: 3 for k in GRANS},
            "strategy": {}, "synthetics": {},
            "stops": {"JD10": 40.0, "BOOM1000": 25.0},
            "scoring": {"threshold": 65, "cooldown_minutes": 180,
                        "max_per_day_per_instrument": 4},
            "account": {"stake_usd": 1.0}}


def count_rows(path, table="signals"):
    import sqlite3
    with sqlite3.connect(path) as con:
        return con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


@check("1/11 IDs déterministes : même barre rejouée ⇒ ♻️, 1 seule ligne")
def t_deterministic_ids():
    tmp = os.path.join(tempfile.mkdtemp(), "i1.db")
    db.init_db(tmp)
    e15 = (NOON - 960) // 900 * 900
    dec = Decision("BOOM1000", "bearish", True, gates=[], score=78, grade="A",
                   breakdown={}, plan={"entry": 10.0, "sl_pts": 25.0, "tp_pts": 75.0,
                                       "sl_price": 35.0, "tp_price": -65.0,
                                       "entry_epoch": e15},
                   confluences=[], context_snapshot={})
    s1, _ = ENG.process_decision(dec, "BOOM1000", "BOOM1000", tmp, NOON, 65,
                                 180 * 60, 4, 1.0)
    assert s1 is not None and s1.id == f"BOOM1000-bearish-{e15}", s1
    # Même barre retraitée après cooldown (2ᵉ run) ⇒ doublon, pas de ré-émission.
    s2, line2 = ENG.process_decision(dec, "BOOM1000", "BOOM1000", tmp,
                                     NOON + 200 * 60, 65, 180 * 60, 4, 1.0)
    assert s2 is None and "♻️" in line2, line2
    assert count_rows(tmp) == 1
    print(f"   id={s1.id} · 2ᵉ traitement → ♻️ · lignes=1 ✅")


@check("2/11 save OR IGNORE : un CLOSED ne ressuscite jamais en ACTIVE")
def t_no_resurrection():
    tmp = os.path.join(tempfile.mkdtemp(), "i2.db")
    db.init_db(tmp)
    row = {"id": "X-bearish-1", "instrument": "X", "symbol": "X", "direction": "bearish",
           "created_epoch": 100, "entry_epoch": 90, "entry": 10.0, "sl_pts": 25.0,
           "tp_pts": 75.0, "sl_price": 35.0, "tp_price": -65.0, "stake_usd": 1.0,
           "confidence": 70, "grade": "B"}
    assert db.save_signal(tmp, row) is True
    assert db.close_signal(tmp, {"signal_id": "X-bearish-1", "closed_epoch": 300,
                                 "result": "TP", "r": 3.0, "points": 75.0,
                                 "bars_held": 5, "exit_price": -65.0, "note": ""}) == "inserted"
    assert db.save_signal(tmp, dict(row, status="ACTIVE")) is False  # re-traitement
    got = db.get_signal(tmp, "X-bearish-1")
    assert got["status"] == "CLOSED" and count_rows(tmp, "outcomes") == 1, got
    print("   re-save après clôture → ignoré, statut CLOSED conservé ✅")


@check("3/11 close ×2 (SL et TP) : 1 outcome, R compté une fois, 1er gagne")
def t_close_once():
    import sqlite3
    for result, r in (("SL", -1.0), ("TP", 3.0)):
        tmp = os.path.join(tempfile.mkdtemp(), "i3.db")
        db.init_db(tmp)
        db.save_signal(tmp, {"id": "S-1", "instrument": "S", "symbol": "S",
                             "direction": "bearish", "created_epoch": 100,
                             "entry_epoch": 90, "entry": 10.0, "sl_pts": 25.0,
                             "tp_pts": 75.0, "sl_price": 35.0, "tp_price": -65.0,
                             "stake_usd": 1.0, "confidence": 70, "grade": "B"})
        out = {"signal_id": "S-1", "closed_epoch": 300, "result": result, "r": r,
               "points": r * 25.0, "bars_held": 5, "exit_price": 35.0 if r < 0 else -65.0,
               "note": ""}
        assert db.close_signal(tmp, out) == "inserted"
        assert db.close_signal(tmp, out) == "already"  # même événement reçu 2×
        # Événement contradictoire tardif (TP après SL) : le 1er gagne.
        other = dict(out, result="TP" if result == "SL" else "SL",
                     r=3.0 if result == "SL" else -1.0)
        assert db.close_signal(tmp, other) == "already"
        with sqlite3.connect(tmp) as con:
            rows = con.execute("SELECT result, r FROM outcomes").fetchall()
            total = con.execute("SELECT SUM(r) FROM outcomes").fetchone()[0]
        assert rows == [(result, r)] and total == r, (rows, total)
    print("   SL×2 et TP×2 → 1 ligne, ΣR unique, 1er événement gagne ✅")


@check("4/11 double run : 2ᵉ cycle identique ⇒ 0 nouvel envoi, 1 ligne")
def t_rerun_idempotent():
    state = {"score": 78}
    settings = base_settings()
    sent = []

    def fake_notify(sigs):
        sent.extend(sigs)
        return [{"signal_id": s.id if hasattr(s, "id") else s["id"],
                 "instrument": s.instrument if hasattr(s, "instrument") else s["instrument"],
                 "status": "sent"} for s in sigs]

    real_eval, real_ns = ENG.evaluate_instrument, TG.notify_signals
    ENG.evaluate_instrument, TG.notify_signals = fake_eval_factory(state), fake_notify
    try:
        tmp = os.path.join(tempfile.mkdtemp(), "i4.db")
        r1 = ENG.run_cycle(settings, db_path=tmp, provider=FakeLiveProvider(lambda: NOON),
                           now_epoch=NOON, notify=True)
        assert len(r1.signals) == 1 and len(sent) == 1, (r1.signals, sent)
        r2 = ENG.run_cycle(settings, db_path=tmp, provider=FakeLiveProvider(lambda: NOON),
                           now_epoch=NOON, notify=True)  # retry / double run
        assert len(r2.signals) == 0 and len(sent) == 1, (r2.signals, sent)
        assert count_rows(tmp) == 1
        row = db.get_signal(tmp, r1.signals[0].id)
        assert row["notified_entry_at"] == NOON, row
    finally:
        ENG.evaluate_instrument, TG.notify_signals = real_eval, real_ns
    print("   run1 : 1 envoi · run2 (retry) : 0 envoi · flag posé ✅")


@check("5/11 panne d'envoi puis retry : exactement 1 succès au total")
def t_failover_exactly_once():
    state = {"score": 78}
    settings = base_settings()
    calls, mode = [], {"fail": True}

    def flaky(sigs):
        calls.append(list(sigs))
        if mode["fail"]:
            return [{"signal_id": "x", "instrument": "BOOM1000",
                     "status": "error", "error": "panne simulée"} for _ in sigs]
        return [{"signal_id": (s.id if hasattr(s, "id") else s["id"]),
                 "instrument": "BOOM1000", "status": "sent"} for s in sigs]

    real_eval, real_ns = ENG.evaluate_instrument, TG.notify_signals
    ENG.evaluate_instrument, TG.notify_signals = fake_eval_factory(state), flaky
    try:
        tmp = os.path.join(tempfile.mkdtemp(), "i5.db")
        prov = lambda: FakeLiveProvider(lambda: NOON)  # noqa: E731
        r1 = ENG.run_cycle(settings, db_path=tmp, provider=prov(), now_epoch=NOON,
                           notify=True)
        assert len(r1.errors) == 1 and "panne" in r1.errors[0], r1.errors
        mode["fail"] = False
        r2 = ENG.run_cycle(settings, db_path=tmp, provider=prov(), now_epoch=NOON,
                           notify=True)
        assert r2.errors == [], r2.errors
        r3 = ENG.run_cycle(settings, db_path=tmp, provider=prov(), now_epoch=NOON,
                           notify=True)
        assert r3.errors == [], r3.errors
        successes = sum(len(c) for i, c in enumerate(calls) if i > 0 and c)
        assert successes == 1 and len(calls[0]) == 1 and calls[2] == [], calls
    finally:
        ENG.evaluate_instrument, TG.notify_signals = real_eval, real_ns
    print("   panne → erreur visible · retry → 1 succès · run3 → 0 envoi ✅")


@check("6/11 impayés : récent renvoyé, vieux enterré (-1), clôtures idem")
def t_pending_windows():
    settings = base_settings()
    now = NOON
    sent_e, sent_c = [], []

    def fake_e(sigs):
        sent_e.extend(sigs)
        return [{"signal_id": s["id"], "instrument": s["instrument"], "status": "sent"}
                for s in sigs]

    def fake_c(items, stats=None):
        sent_c.extend(items)
        return [{"signal_id": o["signal_id"], "instrument": s["instrument"], "status": "sent"}
                for s, o in items]

    real_eval, real_ns, real_nc = (ENG.evaluate_instrument, TG.notify_signals,
                                   TG.notify_closes)
    ENG.evaluate_instrument = lambda *a, **k: Decision("BOOM1000", "bearish", False,
                                                       blocked_by="test")
    TG.notify_signals, TG.notify_closes = fake_e, fake_c
    try:
        tmp = os.path.join(tempfile.mkdtemp(), "i6.db")
        db.init_db(tmp)
        mk = lambda i, created: {"id": i, "instrument": "BOOM1000", "symbol": "BOOM1000",
                                 "direction": "bearish", "created_epoch": created,
                                 "entry_epoch": created, "entry": 10.0, "sl_pts": 25.0,
                                 "tp_pts": 75.0, "sl_price": 35.0, "tp_price": -65.0,
                                 "stake_usd": 1.0, "confidence": 70, "grade": "B"}
        db.save_signal(tmp, mk("recent-e", now - 600))
        db.save_signal(tmp, mk("old-e", now - 130 * 60))
        db.save_signal(tmp, mk("recent-c", now - 3600))
        db.mark_notified(tmp, "recent-c", "entry", now - 3600)
        db.close_signal(tmp, {"signal_id": "recent-c", "closed_epoch": now - 600,
                              "result": "SL", "r": -1.0, "points": -25.0,
                              "bars_held": 2, "exit_price": 35.0, "note": ""})
        db.save_signal(tmp, mk("old-c", now - 30 * 3600))
        db.mark_notified(tmp, "old-c", "entry", now - 30 * 3600)
        db.close_signal(tmp, {"signal_id": "old-c", "closed_epoch": now - 26 * 3600,
                              "result": "TP", "r": 3.0, "points": 75.0,
                              "bars_held": 9, "exit_price": -65.0, "note": ""})
        res = ENG.run_cycle(settings, db_path=tmp, provider=FakeLiveProvider(lambda: now),
                            now_epoch=now, notify=True)
        assert [s["id"] for s in sent_e] == ["recent-e"], sent_e
        assert [s["id"] for s, _ in sent_c] == ["recent-c"], sent_c
        assert db.get_signal(tmp, "old-e")["notified_entry_at"] == -1
        assert db.get_signal(tmp, "old-c")["notified_close_at"] == -1
        assert db.get_signal(tmp, "recent-e")["notified_entry_at"] == now
        assert res.errors == [], res.errors
    finally:
        ENG.evaluate_instrument, TG.notify_signals, TG.notify_closes = \
            real_eval, real_ns, real_nc
    print("   récents renvoyés (1+1) · vieux enterrés -1 · 0 erreur ✅")


@check("7/11 restauration périmée : re-résolution ⇒ valeurs IDENTIQUES (ΣR unique)")
def t_stale_restore_deterministic():
    e0 = 5_000_000
    sig = {"id": "R-1", "instrument": "R", "symbol": "R", "direction": "bearish",
           "created_epoch": e0, "entry_epoch": e0, "entry": 100.0, "sl_pts": 25.0,
           "tp_pts": 75.0, "sl_price": 125.0, "tp_price": 25.0, "stake_usd": 1.0,
           "confidence": 70, "grade": "B"}

    def bar(i, o, h, lo, c):
        return {"epoch": e0 + i * 900, "open": o, "high": h, "low": lo, "close": c}

    full = [bar(0, 100, 101, 99, 100), bar(1, 100, 102, 99, 101),
            bar(2, 101, 103, 100, 102), bar(3, 102, 126, 101, 124),  # SL (high 126)
            bar(4, 124, 125, 20, 22)]  # TP tardif : le SL (barre 3) gagne
    trunc = full[:4]  # restauration partielle, mais l'événement est dedans
    o1 = resolve_signal(sig, full, 96)
    o2 = resolve_signal(sig, trunc, 96)
    assert o1 is not None and o1 == o2 and o1["result"] == "SL", (o1, o2)
    tmp = os.path.join(tempfile.mkdtemp(), "i7.db")
    db.init_db(tmp)
    db.save_signal(tmp, sig)
    assert db.close_signal(tmp, o1) == "inserted"
    assert db.close_signal(tmp, o2) == "already"  # restauration rejouée
    assert count_rows(tmp, "outcomes") == 1
    print(f"   SL barre 3 dans les 2 fenêtres · 2ᵉ close → already · ΣR={o1['r']} ✅")


@check("8/11 périmé ⇒ NO_TRADE (+ bornes exactes des limites fraîcheur)")
def t_staleness():
    now = NOON
    for tf, gran, lim in (("M15", 900, 45), ("H1", 3600, 180)):
        ok_bar = [{"epoch": now - gran - lim * 60, "open": 1, "high": 1, "low": 1,
                   "close": 1}]  # âge == limite pile
        ko_bar = [{"epoch": now - gran - lim * 60 - 1, "open": 1, "high": 1,
                   "low": 1, "close": 1}]  # limite + 1 s
        assert ENG.check_freshness({tf: ok_bar}, GRANS, now) == [], tf
        assert len(ENG.check_freshness({tf: ko_bar}, GRANS, now)) == 1, tf
    assert ENG.check_freshness({"M15": []}, GRANS, now) != []
    # Niveau cycle : barres de 2 h ⇒ NO_TRADE + erreur visible, 0 signal.
    settings = base_settings()

    class StaleProvider(FakeLiveProvider):
        def get_timeframe(self, symbol, tf, count):
            return fresh_bars(self._now() - 2 * 3600, GRANS[tf])

    real_eval = ENG.evaluate_instrument
    ENG.evaluate_instrument = fake_eval_factory({"score": 78})
    try:
        tmp = os.path.join(tempfile.mkdtemp(), "i8.db")
        res = ENG.run_cycle(settings, db_path=tmp, provider=StaleProvider(lambda: now),
                            now_epoch=now, notify=False)
        assert res.signals == [] and "NO_TRADE" in res.logs["BOOM1000"], res.logs
        assert len(res.errors) == 1 and "STALE" in res.errors[0], res.errors
    finally:
        ENG.evaluate_instrument = real_eval
    print("   limite pile OK · +1 s KO · cycle périmé → NO_TRADE + erreur ✅")


@check("9/11 SL/TP déjà touché ⇒ entrée supprimée (-2), suivi conservé")
def t_already_hit():
    assert ENG.check_still_valid(0, "bullish", 90.0, 130.0,
                                 [{"epoch": 900, "close": 100.0},
                                  {"epoch": 1200, "close": 101.0}]) is None
    assert ENG.check_still_valid(0, "bullish", 90.0, 130.0,
                                 [{"epoch": 900, "close": 89.0}]) is not None
    assert ENG.check_still_valid(0, "bullish", 90.0, 130.0,
                                 [{"epoch": 1200, "close": 131.0}]) is not None
    assert ENG.check_still_valid(0, "bearish", 110.0, 70.0,
                                 [{"epoch": 899, "close": 999.0}]) is None  # trop vieux : ignoré
    assert ENG.check_still_valid(0, "bearish", 110.0, 70.0,
                                 [{"epoch": 900, "close": 111.0}]) is not None  # borne incluse
    # Niveau cycle : M5 traverse le SL après la barre signal ⇒ ⛔ + -2, ligne gardée.
    e15 = (NOON - 960) // 900 * 900
    now9 = e15 + 1560
    m5 = [{"epoch": e15 + 300, "open": 10, "high": 10.2, "low": 9.9, "close": 10.05},
          {"epoch": e15 + 600, "open": 10, "high": 10.2, "low": 9.9, "close": 10.05},
          {"epoch": e15 + 900, "open": 10, "high": 10.2, "low": 9.9, "close": 10.05},
          {"epoch": e15 + 1200, "open": 10, "high": 36.5, "low": 9.9, "close": 36.0}]
    settings = base_settings()
    real_eval = ENG.evaluate_instrument
    ENG.evaluate_instrument = fake_eval_factory({"score": 78})
    try:
        tmp = os.path.join(tempfile.mkdtemp(), "i9.db")
        res = ENG.run_cycle(settings, db_path=tmp,
                            provider=FakeLiveProvider(lambda: now9, m5=m5),
                            now_epoch=now9, notify=False)
        assert res.signals == [] and "⛔" in res.logs["BOOM1000"], res.logs
        assert count_rows(tmp) == 1  # audit trail conservé
        row = db.get_signal(tmp, f"BOOM1000-bearish-{e15}")
        assert row is not None and row["notified_entry_at"] == -2, row
    finally:
        ENG.evaluate_instrument = real_eval
    print("   5 cas unitaires + cycle ⛔ SL traversé → -2, ligne conservée ✅")


@check("10/11 migration : base pré-C1 ⇒ backfill one-shot, jamais ré-envoyée")
def t_migration_backfill():
    import sqlite3
    tmp = os.path.join(tempfile.mkdtemp(), "i10.db")
    with sqlite3.connect(tmp) as con:  # schéma AVANT C1 (sans notified_*, sans meta)
        con.execute("""CREATE TABLE signals (id TEXT PRIMARY KEY, instrument TEXT,
                       symbol TEXT, direction TEXT, created_epoch INTEGER,
                       entry_epoch INTEGER, entry REAL, sl_pts REAL, tp_pts REAL,
                       sl_price REAL, tp_price REAL, stake_usd REAL,
                       confidence INTEGER, grade TEXT, gates_json TEXT,
                       confluences_json TEXT, context_json TEXT, status TEXT)""")
        con.execute("""CREATE TABLE outcomes (signal_id TEXT PRIMARY KEY,
                       closed_epoch INTEGER, result TEXT, r REAL, points REAL,
                       bars_held INTEGER, exit_price REAL, note TEXT)""")
        con.execute("INSERT INTO signals VALUES ('A-1','A','A','bearish',1000,990,10,25,75,"
                    "35,-65,1,70,'B','[]','[]','{}','ACTIVE')")
        con.execute("INSERT INTO signals VALUES ('C-1','C','C','bullish',2000,1990,10,25,75,"
                    "-15,85,1,80,'A','[]','[]','{}','CLOSED')")
        con.execute("INSERT INTO outcomes VALUES ('C-1',2900,'TP',3.0,75.0,5,-15.0,'')")
    db.init_db(tmp)
    with sqlite3.connect(tmp) as con:
        cols = {r[1] for r in con.execute("PRAGMA table_info(signals)").fetchall()}
    assert {"notified_entry_at", "notified_close_at"} <= cols, cols
    a = db.get_signal(tmp, "A-1")
    c = db.get_signal(tmp, "C-1")
    assert a["notified_entry_at"] == 1000 and a["notified_close_at"] == 0, a
    assert c["notified_entry_at"] == 2000 and c["notified_close_at"] == 2900, c
    db.init_db(tmp)  # 2ᵉ passage : no-op
    assert db.get_signal(tmp, "A-1")["notified_entry_at"] == 1000
    assert db.get_pending_entries(tmp, 1000 + 60, 7200) == []  # jamais ré-envoyé
    print("   colonnes ajoutées · backfill entrées/clôtures · 2ᵉ init no-op ✅")


@check("11/11 workflow : concurrence + sync pré-cycle + commit always + retry push")
def t_workflow_guards():
    import yaml
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, ".github", "workflows", "signals.yml"),
              encoding="utf-8") as f:
        wf = yaml.safe_load(f)
    live = wf["jobs"]["live"]
    conc = live.get("concurrency", {})
    assert conc.get("group") == "deriv-live" and conc.get("cancel-in-progress") is False, conc
    steps = live["steps"]
    idx = {}
    for i, s in enumerate(steps):
        run = s.get("run", "")
        if "merge --ff-only" in run:
            idx["sync"] = i
        if "live_once" in run:
            idx["cycle"] = i
        if "make_status" in run:
            assert s.get("if") == "always()", s
            idx["status"] = i
        if "git commit" in run:
            assert s.get("if") == "always()", s
            assert "for i in" in run and "git push" in run, run
            idx["commit"] = i
    assert set(idx) == {"sync", "cycle", "status", "commit"}, idx
    assert idx["sync"] < idx["cycle"] < idx["status"] < idx["commit"], idx
    print("   concurrence sérialisée · sync pré-cycle · statut+commit always · retry push ✅")


def main():
    ok = 0
    for name, fn in CHECKS:
        fn()
        print(f"✅ {name}")
        ok += 1
    print(f"\n{ok}/{len(CHECKS)} checks verts (idempotence C1)")


if __name__ == "__main__":
    main()
