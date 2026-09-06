"""
Test ÉTAPE 2a — Analyse SMC/price action sur séries SYNTHÉTIQUES déterministes.

Aucun réseau. Chaque assertion est une valeur EXACTE calculée à la main :
swings, BOS/CHoCH, Order Blocks, FVG, sweep, EQH, EMA, RSI, ATR, engulfing,
pin bar + PREUVE ANTI-REPAINT (fenêtres croissantes ⇒ événements identiques).

Exécution : python scripts/test_analysis.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.analysis.candles import atr_series, find_swings
from src.analysis.fvg import detect_fvg
from src.analysis.indicators import (
    body_ratio, ema_series, is_bearish_engulfing, is_bullish_engulfing,
    pin_bar, rsi_series,
)
from src.analysis.liquidity import detect_sweeps, equal_levels
from src.analysis.order_blocks import detect_order_blocks
from src.analysis.structure import current_trend, detect_structure

BASE = 1_700_000_000
CHECKS = []


def check(name):
    def deco(fn):
        CHECKS.append((name, fn))
        return fn
    return deco


def mk(H, L, O, C, step=900):
    return [{"epoch": BASE + i * step, "open": o, "high": h, "low": lo, "close": c}
            for i, (o, h, lo, c) in enumerate(zip(O, H, L, C))]


# Série A : tendance haussière + retournement (structure, OB, EQH)
A = mk(
    H=[10.0, 10.5, 11.0, 10.8, 10.6, 11.2, 11.8, 11.5, 11.3, 12.0, 11.7, 11.4, 10.5],
    L=[9.0, 9.5, 10.0, 10.2, 9.9, 10.6, 11.0, 11.1, 10.9, 11.2, 11.3, 11.0, 10.0],
    O=[9.2, 9.8, 10.3, 10.7, 10.5, 10.7, 11.1, 11.4, 11.2, 11.3, 11.7, 11.3, 10.4],
    C=[9.8, 10.3, 10.8, 10.5, 10.4, 11.0, 11.5, 11.3, 11.1, 11.9, 11.5, 11.2, 10.2],
)
# Série B : impulsions + gaps de juste valeur (FVG)
B = mk(
    H=[10.0, 10.2, 11.0, 11.5, 11.2, 10.0],
    L=[9.0, 9.5, 10.5, 11.0, 10.8, 9.5],
    O=[9.2, 9.6, 10.6, 11.1, 11.1, 9.9],
    C=[9.8, 10.0, 10.9, 11.4, 10.9, 9.6],
)
# Série C : creux balayé (sweep)
C = mk(
    H=[10.0, 10.1, 10.2, 10.1, 10.0, 10.1, 10.1],
    L=[9.5, 9.3, 9.0, 9.2, 9.4, 8.8, 9.1],
    O=[9.8, 9.9, 9.8, 9.5, 9.6, 9.6, 9.2],
    C=[9.9, 9.7, 9.5, 9.8, 9.7, 9.9, 9.8],
)


@check("1/9 Swings fractales (force 2) — indices et prix exacts")
def t_swings():
    got = [(s["kind"], s["index"], s["price"]) for s in find_swings(A, 2)]
    exp = [("H", 2, 11.0), ("L", 4, 9.9), ("H", 6, 11.8), ("L", 8, 10.9), ("H", 9, 12.0)]
    assert got == exp, f"{got} != {exp}"
    print(f"   {got} ✅")


@check("2/9 Structure BOS/CHoCH — 2 BOS + 1 CHoCH aux barres exactes")
def t_structure():
    ev = detect_structure(A, 2)
    got = [(e["type"], e["direction"], e["index"], e["level"], e["ref_index"]) for e in ev]
    exp = [("BOS", "bullish", 6, 11.0, 2),
           ("BOS", "bullish", 9, 11.8, 6),
           ("CHoCH", "bearish", 12, 10.9, 8)]
    assert got == exp, f"{got} != {exp}"
    assert current_trend(ev) == "bearish"
    print(f"   {got} + trend=bearish ✅")


@check("3/9 Order Blocks — 3 zones exactes + mitigation exacte @12")
def t_ob():
    obs = detect_order_blocks(A, detect_structure(A, 2), lookback=5)
    got = [(o["direction"], o["bottom"], o["top"], o["index"], o["break_index"],
            o["mitigated"], o["mitigated_index"]) for o in obs]
    exp = [("demand", 9.9, 10.6, 4, 6, False, None),
           ("demand", 10.9, 11.3, 8, 9, True, 12),
           ("supply", 11.2, 12.0, 9, 12, False, None)]
    assert got == exp, f"{got} != {exp}"
    print(f"   3 OBs (demand@4 actif, demand@8 mitigé@12, supply@9 actif) ✅")


@check("4/9 FVG — 3 gaps exacts (2 haussiers mitigés@5, 1 baissier actif)")
def t_fvg():
    fv = detect_fvg(B)
    got = [(f["direction"], f["bottom"], f["top"], f["index"],
            f["mitigated"], f["mitigated_index"]) for f in fv]
    exp = [("bullish", 10.0, 10.5, 1, True, 5),
           ("bullish", 10.2, 11.0, 2, True, 5),
           ("bearish", 10.0, 11.0, 4, False, None)]
    assert got == exp, f"{got} != {exp}"
    print(f"   3 FVGs exacts ✅")


@check("5/9 Sweep + EQH — 1 balayage haussier @5, 1 EQH {6,9}")
def t_liq():
    sw = detect_sweeps(C, 2)
    got = [(s["direction"], s["index"], s["level"], s["ref_index"]) for s in sw]
    assert got == [("bullish", 5, 9.0, 2)], got
    eq = equal_levels(A, 2, tolerance_pts=0.5)
    assert len(eq) == 1 and eq[0]["kind"] == "EQH", eq
    assert eq[0]["indices"] == [6, 9] and abs(eq[0]["price"] - 11.9) < 1e-9, eq
    print(f"   sweep={got}, EQH@11.9 [6,9] ✅")


@check("6/9 EMA / RSI / ATR — valeurs exactes calculées à la main")
def t_indic():
    assert ema_series([10, 11, 12, 13], 3) == [None, None, 11.0, 12.0]
    r = rsi_series([10, 12, 11, 13], 2)
    assert r[0] is None and r[1] is None
    assert abs(r[2] - 66.6666666667) < 1e-6, r  # Wilder : RS=2 → 66,67
    assert abs(r[3] - 85.7142857143) < 1e-6, r  # Wilder : RS=6 → 85,71
    assert rsi_series([5, 5, 5, 5], 2)[-1] == 50.0      # plat → neutre
    assert rsi_series([1, 2, 3, 4], 2)[-1] == 100.0     # que des hausses
    atr = atr_series(mk(H=[10, 11, 11], L=[9, 10, 10.5], O=[9.5, 10.5, 10.6],
                        C=[9.5, 10.5, 10.6]), 2)
    assert atr[0] is None and abs(atr[1] - 1.25) < 1e-9 and abs(atr[2] - 0.875) < 1e-9, atr
    print("   EMA [−,−,11,12] · RSI [−,−,66.67,85.71] · ATR [−,1.25,0.875] ✅")


@check("7/9 Engulfing / pin bar / corps — booléens exacts")
def t_candles():
    b = {"epoch": 0, "open": 10.0, "high": 10.1, "low": 8.9, "close": 9.0}   # baissière
    e = {"epoch": 1, "open": 8.9, "high": 10.2, "low": 8.8, "close": 10.1}  # avale b
    assert is_bullish_engulfing(b, e) is True
    u = {"epoch": 0, "open": 9.0, "high": 10.1, "low": 8.9, "close": 10.0}   # haussière
    d = {"epoch": 1, "open": 10.1, "high": 10.2, "low": 8.8, "close": 8.9}   # avale u
    assert is_bearish_engulfing(u, d) is True
    assert is_bullish_engulfing(e, b) is False and is_bearish_engulfing(b, e) is False
    pin = {"epoch": 2, "open": 10.0, "high": 10.3, "low": 9.0, "close": 10.1}
    assert pin_bar(pin) == "bullish"
    pin2 = {"epoch": 3, "open": 10.1, "high": 11.0, "low": 9.9, "close": 10.0}
    assert pin_bar(pin2) == "bearish"
    assert pin_bar(e) is None  # gros corps → pas de pin
    assert abs(body_ratio(e) - (1.2 / 1.4)) < 1e-9
    print("   engulfing×2, pin×2, body_ratio ✅")


def _frozen(candles):
    st = detect_structure(candles, 2)
    obs = detect_order_blocks(candles, st)
    fv = detect_fvg(candles)
    sw = detect_sweeps(candles, 2)
    return (
        {(e["type"], e["direction"], e["index"], e["epoch"], e["level"]) for e in st},
        {(o["direction"], o["top"], o["bottom"], o["index"], o["epoch"]) for o in obs},
        {(f["direction"], f["top"], f["bottom"], f["index"], f["epoch"]) for f in fv},
        {(s["direction"], s["index"], s["epoch"], s["level"]) for s in sw},
        ({(o["direction"], o["index"]): o["mitigated"] for o in obs} |
         {(f["direction"], f["index"]): f["mitigated"] for f in fv}),
    )


@check("8/9 PREUVE ANTI-REPAINT — fenêtres croissantes ⇒ événements identiques")
def t_norepaint():
    for name, series, cuts in (("A", A, [8, 10, 13]), ("B", B, [4, 6]), ("C", C, [5, 7])):
        prev = None
        for n in cuts:
            cur = _frozen(series[:n])
            if prev is not None:
                for k in range(4):
                    assert prev[k] <= cur[k], f"{name} N={n} : événement modifié/disparu !"
                for key, was in prev[4].items():  # mitigation : ne peut que s'activer
                    assert (not was) or cur[4][key], f"{name} N={n} : dé-mitigation !"
            prev = cur
        print(f"   série {name} : {cuts} → émissions stables, mitigation monotone ✅")


@check("9/9 Robustesse — séries vides/courtes sans exception")
def t_edge():
    assert detect_structure([], 2) == [] and detect_fvg([]) == []
    assert detect_order_blocks([], []) == [] and detect_sweeps([], 2) == []
    assert find_swings([], 2) == [] and ema_series([], 5) == []
    assert rsi_series([1.0], 14) == [None] and atr_series([], 14) == []
    assert equal_levels([], 2) == [] and current_trend([]) is None
    print("   vides/courtes → retours vides, aucune exception ✅")


def main():
    print("=" * 64)
    print("TEST ÉTAPE 2a — Analyse SMC (séries synthétiques exactes)")
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
