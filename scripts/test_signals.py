"""
Test ÉTAPE 3 — Pipeline 5 portes + scoring + SL/risque + tracker + moteur.

- Checks 1-7 : SYNTHÉTIQUES déterministes, valeurs exactes (portes, scoring,
  SL JD10, tracker TP/SL/EXPIRE, SQLite, anti-spam moteur via provider factice).
- Check 8 : RÉEL — cycle complet + déterminisme (2 évaluations ⇒ identiques).

Exécution : python scripts/test_signals.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import src.agents.strategy_agent as SA
from src.agents.strategy_agent import (
    context_aligned, decide_boom_buy, evaluate_instrument, gate_d1, gate_h4,
    gate_h1, gate_m15, jump_risk_label, stop_points, timing_bonus,
)
from src.signals import engine as ENG
from src.signals.engine import build_contexts, run_cycle
from src.signals.models import Signal
from src.signals.scoring import compute_score, grade_of
from src.signals.tracker import resolve_signal
from src.storage import database as db

CHECKS = []


def check(name):
    def deco(fn):
        CHECKS.append((name, fn))
        return fn
    return deco


def mk(H, L, O, C, base=1000):
    return [{"epoch": base + i, "open": o, "high": h, "low": lo, "close": c}
            for i, (o, h, lo, c) in enumerate(zip(O, H, L, C))]


A = mk(  # série A (cf. test_analysis) : BOS@6, BOS@9, CHoCH bear@12
    [10.0, 10.5, 11.0, 10.8, 10.6, 11.2, 11.8, 11.5, 11.3, 12.0, 11.7, 11.4, 10.5],
    [9.0, 9.5, 10.0, 10.2, 9.9, 10.6, 11.0, 11.1, 10.9, 11.2, 11.3, 11.0, 10.0],
    [9.2, 9.8, 10.3, 10.7, 10.5, 10.7, 11.1, 11.4, 11.2, 11.3, 11.7, 11.3, 10.4],
    [9.8, 10.3, 10.8, 10.5, 10.4, 11.0, 11.5, 11.3, 11.1, 11.9, 11.5, 11.2, 10.2])
U = mk(  # M15 haussière : BOS bull @6 (niveau 10,8), clôture 11,3
    [10.0, 10.4, 10.8, 10.6, 10.5, 11.0, 11.4, 11.2, 11.6],
    [9.6, 9.9, 10.2, 10.3, 10.2, 10.5, 10.8, 10.9, 11.0],
    [9.7, 10.0, 10.3, 10.5, 10.4, 10.6, 10.9, 11.1, 11.1],
    [10.0, 10.3, 10.6, 10.4, 10.3, 10.8, 11.2, 11.0, 11.3], base=2000)

FLOORS = {"V10": 25.0, "JD10": 40.0, "BOOM1000": 50.0}


@check("1/8 Portes D1/H1/M15 — verdicts et valeurs exacts (série A)")
def t_gates():
    g = gate_d1(A, "bearish", ema_period=5, strength=2)
    assert g.passed and abs(g.data["ema"] - 10.9502) < 1e-3, g.detail
    assert g.data["break"]["type"] == "CHoCH" and g.data["break"]["index"] == 12
    assert gate_d1(A, "bullish", ema_period=5).passed is False
    assert gate_d1(A[:3], "bearish", ema_period=200).detail.count("indisponible") == 1
    h = gate_h1(A, "bullish", ema_period=5)
    assert h.detail == "H1 : clôture 10.20 ≤ EMA5 10.95 (direction haussier interdite)", h.detail
    assert gate_h1(A, "bearish", ema_period=5).passed is True
    m = gate_m15(A, "bearish", atr_period=5)
    assert m.passed and m.data["level"] == 10.9 and m.data["age"] == 0, m.detail
    assert abs(m.data["atr"] - 0.7677) < 1e-4 and abs(m.data["dist"] - 0.3) < 1e-9
    assert m.data["in_zone"] is False
    mb = gate_m15(A, "bullish", atr_period=5)
    assert mb.detail == "M15 : dernière cassure baissier @12 (attendue haussier)", mb.detail
    print("   D1/H1/M15 : pass/block + EMA5 10.9502 + ATR5 0.7677 + dist 0.3 ✅")


@check("2/8 Portes H4/timing — pass exact + bonus non bloquant")
def t_gate_h4_timing():
    g = gate_h4(A, "bearish", ema_fast=3, ema_slow=5, strength=2)
    assert g.detail == ("H4 baissier : EMA3 10.78 vs EMA5 10.95 + 1 OB / 1 FVG actifs "
                        "(ex. OB 11.20-12.00)"), g.detail
    assert gate_h4(A, "bearish", 5, 10).detail == "H4 : EMA5 10.95 vs EMA10 10.87 (pas baissier)"
    assert gate_h4(A, "bullish", 5, 10).detail == "H4 : clôture 10.20 ≤ EMA5 10.95"
    flat = {"epoch": 0, "open": 10.0, "high": 10.1, "low": 9.9, "close": 10.0}
    pin = {"epoch": 1, "open": 10.0, "high": 10.3, "low": 9.0, "close": 10.1}
    assert timing_bonus([flat, pin], [flat, flat], "bullish") == (True, "timing : pin bar haussier M5")
    ok, txt = timing_bonus([flat, pin], [flat, flat], "bearish")
    assert ok is False and "non bloquant" in txt
    print("   H4 pass/block exacts + timing bonus/non-bloquant ✅")


@check("3/8 Scoring — 100/A+, 85/A+, 70/B, 50/—, bornes des grades")
def t_scoring():
    s, gr, det = compute_score(True, True, True, True, True, True)
    assert (s, gr) == (100, "A+") and sum(det.values()) == 100, (s, gr)
    assert compute_score(True, True, False, True, False, True)[:2] == (85, "A+")
    assert compute_score(True, False, False, True, False, False)[:2] == (70, "B")
    assert compute_score(False, False, False, False, False, False)[:2] == (50, "—")
    assert [grade_of(x) for x in (64, 65, 74, 75, 84, 85, 100)] == \
        ["—", "B", "B", "A", "A", "A+", "A+"]
    print("   100/A+ · 85/A+ · 70/B · 50/— · bornes grades ✅")


@check("4/8 SL JD10 + risque JUMP + contextes — maths exactes")
def t_sl_risk():
    sl, note = stop_points("JD10", {"median_size": 43.8}, FLOORS)
    assert abs(sl - 65.7) < 1e-9 and "65.7" in note, (sl, note)
    assert stop_points("JD10", {"median_size": 10.0}, FLOORS)[0] == 40.0  # plancher
    assert stop_points("JD10", {}, FLOORS)[0] == 40.0                     # stats ? → plancher
    assert stop_points("V10", {}, FLOORS) == (25.0, "SL plancher V10 = 25.0 pts")
    assert jump_risk_label(30, 43.8, 86.9)[0] == "ÉLEVÉ"
    assert jump_risk_label(65.7, 43.8, 86.9)[0] == "MOYEN"
    assert jump_risk_label(90, 43.8, 86.9)[0] == "FAIBLE"
    assert jump_risk_label(40, None, None)[0] == "INCONNU"
    assert context_aligned("V10", "bullish", {"vol": {"percentile": 50}}, 25, {})[0] is True
    assert context_aligned("V10", "bullish", {"vol": {"percentile": 5}}, 25, {})[0] is False
    assert context_aligned("JD10", "bullish", {"jump": {"p90_size": 86.9}}, 90, {})[0] is True
    assert context_aligned("JD10", "bullish", {"jump": {"p90_size": 86.9}}, 65.7, {})[0] is False
    b = {"boom": {"time_since_spike_min": 30, "avg_interval_min": 36}}
    assert context_aligned("BOOM1000", "bearish", b, 50, {})[0] is True
    b["boom"]["time_since_spike_min"] = 100
    assert context_aligned("BOOM1000", "bearish", b, 50, {})[0] is False
    print("   SL 65.7/40/25 · risque É/M/F/I · contextes V10/JD10/BOOM ✅")


@check("5/8 BOOM BUY — bloqué sans spike récent, 80/A avec spike + pin M5")
def t_boom_buy():
    tf = {"M15": U,
          "M5": [{"epoch": 0, "open": 10.0, "high": 10.1, "low": 9.9, "close": 10.0},
                 {"epoch": 1, "open": 10.0, "high": 10.3, "low": 9.0, "close": 10.1}],
          "M30": U[:2], "H1": U[:3]}
    ctx_old = {"boom": {"time_since_spike_min": 120, "avg_interval_min": 36},
               "vol": {"percentile": 50}}
    d = decide_boom_buy(tf, ctx_old, {}, FLOORS)
    assert d.passed is False and "120" in (d.blocked_by or "") and "45" in (d.blocked_by or "")
    ctx = {"boom": {"time_since_spike_min": 12, "avg_interval_min": 36,
                    "last_spike_high": 11.6, "drift_pts_per_hour": -5.0},
           "vol": {"percentile": 50, "regime": "normal", "atr_m15": 1.0}}
    d = decide_boom_buy(tf, ctx, {"atr_period": 3}, FLOORS)
    assert d.passed, d.blocked_by
    assert d.plan["entry"] == 11.3 and d.plan["sl_pts"] == 50.0 and d.plan["tp_pts"] == 150.0
    assert (d.score, d.grade) == (80, "A"), (d.score, d.breakdown)  # 50+0+10+10+10+0+0
    assert any("capitalisation" in c for c in d.confluences)
    print(f"   bloqué@120min ✅ · spike@12min + pin M5 ⇒ 80/A, TP {d.plan['tp_pts']} = 3×SL ✅")


@check("6/8 Tracker — TP +3R / SL −1R / prudence même bougie / EXPIRE / bornes R")
def t_tracker():
    base = {"id": "t", "direction": "bearish", "entry": 100.0, "sl_pts": 5.0,
            "sl_price": 105.0, "tp_price": 85.0, "entry_epoch": 1000}

    def bar(ep, o, h, lo, c):
        return {"epoch": ep, "open": o, "high": h, "low": lo, "close": c}

    tp = resolve_signal(base, [bar(1001, 100, 101, 99, 100), bar(1002, 100, 100, 84, 86)])
    assert (tp["result"], tp["r"], tp["points"], tp["bars_held"], tp["exit_price"]) == \
        ("TP", 3.0, 15.0, 2, 85.0), tp
    sl = resolve_signal(base, [bar(1001, 100, 106, 104, 105)])
    assert (sl["result"], sl["r"], sl["points"], sl["bars_held"]) == ("SL", -1.0, -5.0, 1), sl
    both = resolve_signal(base, [bar(1001, 100, 106, 84, 95)])
    assert both["result"] == "SL" and "prudente" in both["note"], both
    flat = [bar(1001 + k, 99, 99.5, 98.5, 99.0) for k in range(96)]
    ex = resolve_signal(base, flat, expiry_bars=96)
    assert ex["result"] == "EXPIRE" and abs(ex["r"] - 0.2) < 1e-9, ex
    assert (ex["points"], ex["bars_held"], ex["exit_price"]) == (1.0, 96, 99.0), ex
    assert resolve_signal(base, flat[:3], expiry_bars=96) is None
    for o in (tp, sl, both, ex):
        assert -1.0 <= o["r"] <= 3.0, o  # bornes R inviolables
    print("   TP +3R · SL −1R · même-bougie→SL · EXPIRE +0.2R@96 · bornes R ✅")


@check("7/8 SQLite + anti-spam moteur (provider factice, 0 réseau)")
def t_db_antispam():
    tmp = os.path.join(tempfile.mkdtemp(), "t.db")
    db.init_db(tmp)
    sig = Signal(id="V10-bearish-100", instrument="V10", symbol="R_10", direction="bearish",
                 created_epoch=100, entry_epoch=90, entry=10.0, sl_pts=25.0, tp_pts=75.0,
                 sl_price=35.0, tp_price=-65.0, stake_usd=1.0, confidence=70, grade="B")
    sig2 = Signal(id="V10-bullish-200", instrument="V10", symbol="R_10", direction="bullish",
                  created_epoch=200, entry_epoch=190, entry=10.0, sl_pts=25.0, tp_pts=75.0,
                  sl_price=-15.0, tp_price=85.0, stake_usd=1.0, confidence=80, grade="A")
    db.save_signal(tmp, sig)
    db.save_signal(tmp, sig2)
    assert len(db.get_open_signals(tmp)) == 2
    assert db.last_created(tmp, "V10") == 200 and db.count_since(tmp, "V10", 150) == 1
    db.close_signal(tmp, {"signal_id": "V10-bearish-100", "closed_epoch": 300, "result": "TP",
                          "r": 3.0, "points": 75.0, "bars_held": 5, "exit_price": -65.0, "note": ""})
    st = db.get_stats(tmp)
    assert (st["n"], st["TP"], st["winrate"], st["r_total"], st["open"]) == (1, 1, 1.0, 3.0, 1), st
    print("   SQLite round-trip + stats (1 TP, R+3) ✅")

    # --- moteur : fake provider TEMPORELLEMENT COHÉRENT (C1 : la garde
    # fraîcheur exige des barres fraîches) + décision factice ancrée sur la barre.
    GRANS = {"M5": 300, "M15": 900, "M30": 1800, "H1": 3600, "H4": 14400, "D1": 86400}

    def fresh_bars(now, gran, n=3, price=10.0):
        last_epoch = (now - 60 - gran) // gran * gran
        return [{"epoch": last_epoch - (n - 1 - i) * gran, "open": price,
                 "high": price + 0.1, "low": price - 0.1, "close": price + 0.05}
                for i in range(n)]

    class FakeProvider:
        def __init__(self, now):
            self._now = now

        def get_timeframe(self, symbol, tf, count):
            return fresh_bars(self._now, GRANS[tf])

        def get_candles(self, symbol, gran, count):
            return fresh_bars(self._now, gran)

        def get_ticks(self, symbol, count=2000, end="latest", use_cache=None, max_cache=40000):
            return [{"epoch": self._now - 60 + i, "price": 100.0} for i in range(25)]

        def close(self):
            pass

    settings = {"instruments": {"V10": {"symbol": "R_10"}, "JD10": {"symbol": "JD10"},
                                "BOOM1000": {"symbol": "BOOM1000"}},
                "timeframes": {"M5": 300, "M15": 900, "M30": 1800, "H1": 3600,
                               "H4": 14400, "D1": 86400},
                "counts": {"M5": 3, "M15": 3, "M30": 3, "H1": 3, "H4": 3, "D1": 3},
                "strategy": {}, "synthetics": {}, "stops": dict(FLOORS),
                "scoring": {"threshold": 65, "cooldown_minutes": 180,
                            "max_per_day_per_instrument": 4},
                "account": {"stake_usd": 1.0}}
    NOON = (1_700_000_000 // 86400) * 86400 + 12 * 3600  # midi UTC (même jour +400 min)
    from src.agents.strategy_agent import Decision, GateResult
    state = {"score": 78}

    def fake_eval(instrument, tf, ctx, P, floors, prep=None):
        return Decision(instrument, "bearish", True,
                        gates=[GateResult("D1", "bearish", True, "porte factice")],
                        score=state["score"], grade="A", breakdown={"base": 50},
                        plan={"entry": 10.0, "sl_pts": 25.0, "tp_pts": 75.0,
                              "sl_price": 35.0, "tp_price": -65.0,
                              "entry_epoch": tf["M15"][-1]["epoch"]},
                        confluences=["factice"], context_snapshot={})

    real_eval = ENG.evaluate_instrument
    ENG.evaluate_instrument = fake_eval
    try:
        tmp2 = os.path.join(tempfile.mkdtemp(), "m.db")
        r1 = run_cycle(settings, db_path=tmp2, provider=FakeProvider(NOON), now_epoch=NOON)
        assert len(r1.signals) == 3, [s.id for s in r1.signals]
        r2 = run_cycle(settings, db_path=tmp2, provider=FakeProvider(NOON + 600),
                       now_epoch=NOON + 600)
        assert len(r2.signals) == 0 and all("cooldown" in v for v in r2.logs.values()), r2.logs
        for i in range(4):  # quota V10 (4/jour déjà émis dans run1+prefill → bloque)
            db.save_signal(tmp2, {"id": f"pre-{i}", "instrument": "V10", "symbol": "R_10",
                                  "direction": "bearish", "created_epoch": NOON + 100,
                                  "entry_epoch": 1, "entry": 1.0, "sl_pts": 1.0, "tp_pts": 3.0,
                                  "sl_price": 2.0, "tp_price": -2.0, "stake_usd": 1.0,
                                  "confidence": 70, "grade": "B"})
        r3 = run_cycle(settings, db_path=tmp2, provider=FakeProvider(NOON + 200 * 60),
                       now_epoch=NOON + 200 * 60)
        assert "quota" in r3.logs["V10"] and len(r3.signals) == 2, r3.logs
        state["score"] = 60
        r4 = run_cycle(settings, db_path=tmp2, provider=FakeProvider(NOON + 400 * 60),
                       now_epoch=NOON + 400 * 60)
        assert len(r4.signals) == 0 and all("score 60 < 65" in v for v in r4.logs.values())
    finally:
        ENG.evaluate_instrument = real_eval
    print("   moteur : 3 émis → cooldown → quota V10 → seuil 60<65 ✅")


@check("8/8 RÉEL — cycle complet + déterminisme (2 évaluations identiques)")
def t_real_cycle():
    import yaml
    with open("config/settings.yaml", "r", encoding="utf-8") as f:
        settings = yaml.safe_load(f)
    tmp = os.path.join(tempfile.mkdtemp(), "live.db")
    res = run_cycle(settings, db_path=tmp)
    expected = {n for n, i in settings["instruments"].items() if i.get("enabled", True)}
    assert set(res.logs) == expected, res.logs
    assert "V10" not in res.logs  # pause V10 (étape 4 validée)
    for inst, line in res.logs.items():
        print(f"   {inst:>9} : {line}")
        assert line, "log vide !"
    for s in res.signals:
        assert abs(s.tp_pts - 3 * s.sl_pts) < 1e-9 and s.grade in ("B", "A", "A+")
        assert 65 <= s.confidence <= 100
    # déterminisme : mêmes données ⇒ même décision
    from src.data.deriv_provider import DerivProvider
    p = DerivProvider()
    try:
        tf = {t: p.get_timeframe("R_10", t, settings["counts"][t])
              for t in settings["timeframes"]}
        P = dict(settings.get("strategy", {}))
        P["synthetics_params"] = settings.get("synthetics", {})
        ctx = build_contexts("V10", tf, None, P)
        d1 = evaluate_instrument("V10", tf, ctx, P, settings["stops"])
        d2 = evaluate_instrument("V10", tf, ctx, P, settings["stops"])
        assert (d1.passed, d1.blocked_by, d1.score, d1.grade) == \
               (d2.passed, d2.blocked_by, d2.score, d2.grade)
        assert d1.plan == d2.plan
        print(f"   déterminisme : 2× Mars ({d1.blocked_by or d1.score}) identiques ✅")
    finally:
        p.close()


def main():
    print("=" * 64)
    print("TEST ÉTAPE 3 — Pipeline + scoring + tracker + moteur")
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
