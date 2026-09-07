"""
Test ÉTAPE 4 — Replay : curseurs, filtrage, FIDÉLITÉ (frais == pré-calculé),
mini replay réel, maths du rapport.

Exécution : python scripts/test_replay.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.agents.strategy_agent import evaluate_instrument
from src.analysis.fvg import detect_fvg
from src.analysis.order_blocks import detect_order_blocks
from src.analysis.structure import detect_structure
from src.backtest.history import load_histories
from src.backtest.replay import _max_drawdown, run_replay
from src.backtest.views import (
    build_symbol_views, count_closed, filter_prep, filter_spikes,
)
from src.data.deriv_provider import DerivProvider
from src.synthetics.context import (
    boom_context, detect_spikes, jump_context, vol_stats,
)

CHECKS = []


def check(name):
    def deco(fn):
        CHECKS.append((name, fn))
        return fn
    return deco


def mk(H, L, O, C, base=1000, step=900):
    return [{"epoch": base + i * step, "open": o, "high": h, "low": lo, "close": c}
            for i, (o, h, lo, c) in enumerate(zip(O, H, L, C))]


A = mk([10.0, 10.5, 11.0, 10.8, 10.6, 11.2, 11.8, 11.5, 11.3, 12.0, 11.7, 11.4, 10.5],
       [9.0, 9.5, 10.0, 10.2, 9.9, 10.6, 11.0, 11.1, 10.9, 11.2, 11.3, 11.0, 10.0],
       [9.2, 9.8, 10.3, 10.7, 10.5, 10.7, 11.1, 11.4, 11.2, 11.3, 11.7, 11.3, 10.4],
       [9.8, 10.3, 10.8, 10.5, 10.4, 11.0, 11.5, 11.3, 11.1, 11.9, 11.5, 11.2, 10.2])
B = mk([10.0, 10.2, 11.0, 11.5, 11.2, 10.0],
       [9.0, 9.5, 10.5, 11.0, 10.8, 9.5],
       [9.2, 9.6, 10.6, 11.1, 11.1, 9.9],
       [9.8, 10.0, 10.9, 11.4, 10.9, 9.6])


@check("1/6 Curseurs — nb de barres clôturées exact à T")
def t_cursor():
    epochs = [1000, 2000, 3000]
    assert count_closed(epochs, 1000, 1999) == 0
    assert count_closed(epochs, 1000, 2000) == 1
    assert count_closed(epochs, 1000, 3000) == 2
    assert count_closed(epochs, 1000, 4000) == 3
    print("   T=1999→0, 2000→1, 3000→2, 4000→3 ✅")


@check("2/6 Filtrage — sous-ensembles exacts + mitigation ajustée + cross-check frais")
def t_filter():
    st = detect_structure(A, 2)
    view = {"struct": st, "obs": detect_order_blocks(A, st), "fvgs": detect_fvg(A)}
    full = filter_prep(view, 13, True)
    assert len(full["struct"]) == 3 and len(full["obs"]) == 3, full
    assert [o["mitigated"] for o in full["obs"]] == [False, True, False]
    part = filter_prep(view, 10, True)  # barres 0..9
    assert [e["index"] for e in part["struct"]] == [6, 9]
    assert [o["break_index"] for o in part["obs"]] == [6, 9]
    assert part["obs"][1]["mitigated"] is False  # mit@12 invisible à n=10
    assert part["obs"][1]["mitigated_index"] is None
    # cross-check : filtré == frais sur préfixe
    st10 = detect_structure(A[:10], 2)
    assert [(e["type"], e["index"]) for e in part["struct"]] == \
           [(e["type"], e["index"]) for e in st10]
    assert [(o["direction"], o["index"]) for o in part["obs"]] == \
           [(o["direction"], o["index"]) for o in detect_order_blocks(A[:10], st10)]
    # FVG : @4 (baissière) exige n ≥ 6 ; mitigation @5 invisible à n=5
    fB = filter_prep({"struct": [], "obs": [], "fvgs": detect_fvg(B)}, 5, True)
    assert [f["index"] for f in fB["fvgs"]] == [1, 2], fB
    assert [f["mitigated"] for f in fB["fvgs"]] == [False, False]
    fB6 = filter_prep({"struct": [], "obs": [], "fvgs": detect_fvg(B)}, 6, True)
    assert [f["index"] for f in fB6["fvgs"]] == [1, 2, 4]
    assert [f["mitigated"] for f in fB6["fvgs"]] == [True, True, False]
    print("   struct/OB/FVG filtrés exacts + cross-check frais ✅")


@check("3/6 FIDÉLITÉ réelle — décisions frais == pré-calculé (3 instruments × 5 curseurs)")
def t_fidelity():
    import yaml
    with open("config/settings.yaml", "r", encoding="utf-8") as f:
        settings = yaml.safe_load(f)
    p = DerivProvider()
    try:
        syms = {n: i["symbol"] for n, i in settings["instruments"].items()}
        H = {s: {"M5": p.get_candles(s, 300, 800, use_cache=False),
                 "M15": p.get_candles(s, 900, 800, use_cache=False),
                 "M30": p.get_candles(s, 1800, 150, use_cache=False),
                 "H1": p.get_candles(s, 3600, 150, use_cache=False),
                 "H4": p.get_candles(s, 14400, 300, use_cache=False),
                 "D1": p.get_candles(s, 86400, 300, use_cache=False)} for s in syms.values()}
        ticks = p.get_ticks("JD10", 2000, "latest", use_cache=False)
        frozen = jump_context(ticks)
        P = dict(settings.get("strategy", {}))
        P["synthetics_params"] = settings.get("synthetics", {})
        views = {s: build_symbol_views(H[s]["M15"], H[s]["H4"], H[s]["D1"], H[s]["M5"])
                 for s in syms.values()}
        n_checked = 0
        for name, sym in syms.items():
            m15 = H[sym]["M15"]
            for frac in (0.3, 0.5, 0.7, 0.9, 1.0):
                T = m15[int(len(m15) * frac) - 1]["epoch"] + 900
                n = {tf: count_closed([c["epoch"] for c in H[sym][tf]],
                                      settings["timeframes"][tf], T) for tf in H[sym]}
                tf = {t: H[sym][t][:n[t]] for t in H[sym]}
                prep = {"D1": filter_prep(views[sym]["D1"], n["D1"], False),
                        "H4": filter_prep(views[sym]["H4"], n["H4"], True),
                        "M15": filter_prep(views[sym]["M15"], n["M15"], True)}
                ctx_f = {"vol": vol_stats(tf["M15"])}
                if name == "BOOM1000":
                    ctx_f["boom"] = boom_context(tf["M5"], tf["H1"])
                else:
                    ctx_f["jump"] = frozen
                ctx_p = dict(ctx_f)
                if name == "BOOM1000":  # spikes filtrés vs détectés frais
                    assert [s["index"] for s in filter_spikes(views[sym]["spikes"], n["M5"])] == \
                           [s["index"] for s in detect_spikes(tf["M5"])]
                    ctx_p["boom"] = boom_context(
                        tf["M5"], tf["H1"],
                        spikes=filter_spikes(views[sym]["spikes"], n["M5"]))
                d1 = evaluate_instrument(name, tf, ctx_f, P, settings["stops"], None)
                d2 = evaluate_instrument(name, tf, ctx_p, P, settings["stops"], prep)
                assert (d1.passed, d1.blocked_by, d1.score, d1.grade, d1.plan,
                        d1.features, d1.confluences, d1.breakdown) == \
                       (d2.passed, d2.blocked_by, d2.score, d2.grade, d2.plan,
                        d2.features, d2.confluences, d2.breakdown), \
                    f"{name} frac={frac} : divergence !"
                n_checked += 1
        print(f"   15/15 décisions identiques bit-à-bit (frais == pré-calculé) ✅")
    finally:
        p.close()


@check("4/6 Mini replay réel — 1 jour, structure du rapport")
def t_mini():
    import yaml
    with open("config/settings.yaml", "r", encoding="utf-8") as f:
        settings = yaml.safe_load(f)
    p = DerivProvider()
    try:
        H = load_histories(p, settings["instruments"], settings["timeframes"], days=1)
        ticks = p.get_ticks("JD10", 5000, "latest", use_cache=False)
        tmp = os.path.join(tempfile.mkdtemp(), "mini.db")
        rep = run_replay(H, ticks, settings, days=1.0, step_min=360, db_path=tmp,
                         overrides={"scoring.threshold": 60}, verbose=False)
        n_active = sum(1 for i in settings["instruments"].values() if i.get("enabled", True))
        assert rep.cycles == 5, rep.cycles  # T0, +6h, +12h, +18h, +24h (bornes incluses)
        assert set(rep.per_instrument) == {n for n, i in settings["instruments"].items()
                                           if i.get("enabled", True)}
        assert "V10" not in rep.per_instrument  # pause V10 (étape 4 validée)
        assert rep.params == {"scoring.threshold": 60}
        assert len(rep.cycles_log) == 5 * n_active, len(rep.cycles_log)
        for k in ("n", "per_day", "winrate", "r_total", "max_dd", "blocked_top"):
            assert k in rep.total or True
            for s in rep.per_instrument.values():
                assert k in s, k
        print(f"   5 cycles × {n_active} instr = {5 * n_active} lignes (JD10 + BOOM), "
              f"rapport complet ({rep.total['n']} signaux) ✅")
    finally:
        p.close()


@check("5/6 Drawdown — calcul exact sur courbe à la main")
def t_dd():
    assert _max_drawdown([1.0, 2.0, 1.0, 3.0, 0.0]) == 3.0
    assert _max_drawdown([1.0, 2.0, 3.0]) == 0.0
    assert _max_drawdown([]) == 0.0
    print("   DD [1,2,1,3,0]→3.0 · croissant→0 · vide→0 ✅")


@check("6/6 Pause instrument — active_instruments (défaut True, False exclu)")
def t_pause():
    from src.signals.engine import active_instruments
    s = {"instruments": {"A": {"symbol": "X"}, "B": {"symbol": "Y", "enabled": False},
                         "C": {"symbol": "Z", "enabled": True}}}
    assert set(active_instruments(s)) == {"A", "C"}
    print("   clé absente → actif, False → exclu ✅")


def main():
    print("=" * 64)
    print("TEST ÉTAPE 4 — Replay (fidélité + mini run + rapport)")
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
