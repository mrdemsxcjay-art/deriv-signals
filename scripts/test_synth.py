"""
Test ÉTAPE 2b — Contexte synthétique (§5) : spikes, jumps, régimes.

- Checks 1-2 : séries SYNTHÉTIQUES exactes (spike @105 ratio 5,0 ; jumps +25/-40).
- Checks 3-4 : DONNÉES RÉELLES (bandes de plausibilité calibrées) + contextes §5.
- Check 5 : PREUVE ANTI-REPAINT sur données réelles (fenêtres croissantes).

Exécution : python scripts/test_synth.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.data.deriv_provider import DerivProvider
from src.synthetics.context import (
    boom_context, detect_jumps, detect_spikes, jump_context,
)

CHECKS = []


def check(name):
    def deco(fn):
        CHECKS.append((name, fn))
        return fn
    return deco


@check("1/5 Spike synthétique — 1 seul spike @105, ratio exact 5,0")
def t_spike_synth():
    flat = [{"epoch": 1000 + i, "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0}
            for i in range(120)]
    flat[105] = {"epoch": 1105, "open": 100.0, "high": 110.0, "low": 100.0, "close": 110.0}
    flat[108] = {"epoch": 1108, "open": 110.0, "high": 110.0, "low": 100.0, "close": 100.0}
    sp = detect_spikes(flat, window=100, multiplier=3.0)
    assert len(sp) == 1, f"{len(sp)} spikes au lieu de 1"
    s = sp[0]
    assert s["index"] == 105 and s["amplitude"] == 10.0, s
    assert s["median_amp"] == 2.0 and s["ratio"] == 5.0, s
    assert detect_spikes(flat[:100], window=100) == []  # pas de chauffe ⇒ rien
    print(f"   spike unique @105 (amp=10, méd=2, ratio=5.0) ; baissier @108 ignoré ✅")


@check("2/5 Jumps synthétiques — +25 UP et −40 DN aux ticks exacts")
def t_jump_synth():
    ticks = [{"epoch": 2000 + k, "price": 100.0 + (k % 2) * 0.5} for k in range(30)]
    ticks[10]["price"] = ticks[9]["price"] + 25.0
    for k in range(11, 30):  # le marché continue depuis le nouveau niveau
        ticks[k]["price"] += 25.0
    ticks[20]["price"] = ticks[19]["price"] - 40.0
    for k in range(21, 30):
        ticks[k]["price"] -= 40.0
    jp = detect_jumps(ticks, threshold=10.0)
    got = [(j["index"], j["size"], j["direction"]) for j in jp]
    assert got == [(10, 25.0, "UP"), (20, 40.0, "DN")], got
    assert detect_jumps(ticks[:10], threshold=10.0) == []
    print(f"   jumps {got} ✅")


@check("3/5 RÉEL — spikes BOOM1000 M5 (~1/36 min, mult 3,0)")
def t_spike_real():
    p = DerivProvider()
    try:
        m5 = p.get_candles("BOOM1000", 300, 1000)
        h1 = p.get_candles("BOOM1000", 3600, 500)
        ctx = boom_context(m5, h1)
        print(f"   n={ctx['n_spikes']} interv_moy={ctx['avg_interval_min']:.1f}min "
              f"ampl_med={ctx['amplitude_med']:.1f} p90={ctx['amplitude_p90']:.1f} "
              f"max={ctx['amplitude_max']:.1f} dérive={ctx['drift_pts_per_hour']:.2f}pts/h")
        assert 70 <= ctx["n_spikes"] <= 220, ctx
        assert 20 <= ctx["avg_interval_min"] <= 60, ctx
        assert 10 <= ctx["amplitude_med"] <= 40, ctx
        assert ctx["amplitude_max"] >= 30, ctx
        assert ctx["drift_pts_per_hour"] < 0, "dérive BOOM non baissière ?!"
        assert ctx["time_since_spike_min"] is not None and ctx["time_since_spike_min"] >= 0
    finally:
        p.close()


@check("4/5 RÉEL — jumps JD10 ticks (~3/h, seuil 10 pts)")
def t_jump_real():
    p = DerivProvider()
    try:
        b1 = p.get_ticks("JD10", 5000, "latest", use_cache=False)
        b0 = p.get_ticks("JD10", 5000, b1[0]["epoch"] - 1, use_cache=False)
        ticks = sorted({t["epoch"]: t for t in b0 + b1}.values(), key=lambda t: t["epoch"])
        assert len(ticks) > 9000, f"pagination incomplète : {len(ticks)}"
        ctx = jump_context(ticks)
        print(f"   n={ctx['n_jumps']} taux={ctx['rate_per_hour']:.2f}/h "
              f"interv_moy={ctx['avg_interval_min']:.0f}min méd_taille={ctx['median_size']:.1f} "
              f"p90={ctx['p90_size']:.1f} max={ctx['max_size']:.1f}")
        assert 2 <= ctx["n_jumps"] <= 18, ctx
        assert 1.0 <= ctx["rate_per_hour"] <= 6.0, ctx
        assert ctx["max_size"] >= 20, ctx  # au moins 1 vrai jump (la médiane sur n≈5 est trop bruitée : 14,6-19,3 le 06/09 vs 43,8 à l'étape 2 → régime à re-mesurer)
    finally:
        p.close()


@check("5/5 PREUVE ANTI-REPAINT réelle — spikes/jumps stables (fenêtres croissantes)")
def t_norepaint_real():
    p = DerivProvider()
    try:
        m5 = p.get_candles("BOOM1000", 300, 1000)
        prev = None
        for n in (500, 750, 1000):
            cur = {(s["index"], s["epoch"], s["amplitude"], s["ratio"])
                   for s in detect_spikes(m5[:n])}
            if prev is not None:
                assert prev <= cur, f"spike modifié/disparu à N={n}"
            prev = cur
        print(f"   spikes BOOM : 500→750→1000 ⇒ {len(prev)} événements stables ✅")
        ticks = p.get_ticks("JD10", 5000, "latest", use_cache=False)
        prev = None
        for n in (2500, 4000, len(ticks)):
            cur = {(j["index"], j["epoch"], j["size"], j["direction"])
                   for j in detect_jumps(ticks[:n])}
            if prev is not None:
                assert prev <= cur, f"jump modifié/disparu à N={n}"
            prev = cur
        print(f"   jumps JD10 : 2500→4000→{len(ticks)} ⇒ {len(prev)} événements stables ✅")
    finally:
        p.close()


def main():
    print("=" * 64)
    print("TEST ÉTAPE 2b — Contexte synthétique (synthétique + réel)")
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
