"""
Test PAPER H-SL-ADAPTIVE — paires suivies (A2 JD10 bear @TP4, C-spk-P50 BOOM bull @TP3).

- T1 : R-math resolve_paper (TP=+m, SL=-1, EXPIRE clamp [-1,+3] recherche).
- T2 : rails paper == recherche (grille).
- T3 : SL bruts paper == recherche sur feats factices + NO_TRADE.
- T4 : features paper == recherche sur bougies synthétiques (découpage causal).
- T5 : end-to-end déterministe + parité totale avec recherche (SL, prix, outcome).
- T6 : store (roundtrip, idempotence C1, stats).

Exécution : python scripts/test_paper.py
"""
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import sl_models as RESEARCH  # noqa: E402 — recherche validée OOS (référence)
from src.paper import models as PM  # noqa: E402
from src.paper import store as PStore  # noqa: E402


def C(epoch, o, h, low, close):
    return {"epoch": epoch, "open": o, "high": h, "low": low, "close": close}


def boom_tf():
    """M15/M5 BOOM : 20 spikes M5 déterministes (amp 20), ATR M15 > 0."""
    e0 = 1_700_000_000
    m15 = []
    for i in range(150):
        c = 5000.0 + (i % 7) * 1.5 - (i % 5)
        m15.append(C(e0 + i * 900, c - 0.5, c + 1.0, c - 1.0, c))
    m5 = []
    for i in range(250):
        c = 5000.0 + (i % 11) * 0.3
        if i >= 150 and i % 5 == 0:
            m5.append(C(e0 + i * 300, c, c + 18.0, c - 2.0, c + 15.0))
        else:
            m5.append(C(e0 + i * 300, c, c + 1.5, c - 0.5, c + 0.5))
    return m15, m5


def jd_tf():
    """M15 JD10 : swing H(60)=1062, L(70)=1035, cassure baissière barre 80,
    supply OB barre 79 [1032, 1037] actif (clôtures suivantes < 1037)."""
    e0 = 1_700_000_000
    m15 = []
    for i in range(60):
        c = 1000.0 + i
        m15.append(C(e0 + i * 900, c - 0.5, c + 0.5, c - 1.0, c))
    m15.append(C(e0 + 60 * 900, 1059.0, 1062.0, 1058.0, 1060.0))
    for i, c in zip(range(61, 70), (1058, 1056, 1054, 1052, 1051, 1049, 1047, 1045, 1044)):
        m15.append(C(e0 + i * 900, c + 0.5, c + 1.0, c - 1.0, c))
    m15.append(C(e0 + 70 * 900, 1044.0, 1047.0, 1035.0, 1046.0))
    for i, c in zip(range(71, 79), (1045, 1043, 1042, 1040, 1039, 1038, 1036, 1035.5)):
        m15.append(C(e0 + i * 900, c + 0.5, c + 1.0, c - 1.0, c))
    m15.append(C(e0 + 79 * 900, 1034.0, 1037.0, 1033.5, 1036.5))
    m15.append(C(e0 + 80 * 900, 1035.0, 1035.5, 1029.0, 1030.0))
    for i in range(81, 150):
        c = 1030.0 - (i - 80) * 0.4
        m15.append(C(e0 + i * 900, c + 0.2, c + 0.5, c - 0.5, c))
    m5 = [C(e0 + i * 300, 1000.0, 1001.0, 999.0, 1000.5) for i in range(50)]
    return m15, m5


def t1_r_math():
    base = {"id": "X", "entry": 1000.0, "entry_epoch": 100,
            "sl_pts": 10.0, "tp_pts": 30.0,
            "sl_price": 990.0, "tp_price": 1030.0, "direction": "bullish", "m": 3.0}
    fut_tp = [C(200 + i, 1000 + i, 1001 + i, 999 + i, 1000 + i) for i in range(1, 6)]
    fut_tp.append(C(300, 1029, 1031, 1028, 1030))  # TP barre 6
    o = PM.resolve_paper(base, fut_tp)
    assert (o["result"], o["r"], o["points"], o["bars_held"]) == ("TP", 3.0, 30.0, 6), o
    base4 = dict(base, m=4.0, tp_pts=40.0, tp_price=1040.0)
    fut_tp4 = [C(200 + i, 1000 + 8 * i, 1002 + 8 * i, 999 + 8 * i, 1001 + 8 * i)
               for i in range(1, 6)]
    o = PM.resolve_paper(base4, fut_tp4)  # +40 dès barre 5 -> TP +4R
    assert (o["result"], o["r"]) == ("TP", 4.0), o
    fut_sl = [C(200, 1000, 1001, 989, 995)]  # SL barre 1
    o = PM.resolve_paper(base, fut_sl)
    assert (o["result"], o["r"], o["points"]) == ("SL", -1.0, -10.0), o
    # EXPIRE : dérive +35 sans toucher TP40 (m=4) -> clamp recherche +3.
    fut_ex = [C(200 + i * 900, 1000 + i * 0.36, 1001 + i * 0.36, 999 + i * 0.36,
                1000 + i * 0.36) for i in range(1, 97)]
    o = PM.resolve_paper(base4, fut_ex)
    assert o["result"] == "EXPIRE" and o["r"] == 3.0, o
    assert PM.resolve_paper(base, []) is None
    print("T1 R-math OK (TP=+m, SL=-1, EXPIRE clamp +3)")


def t2_rails():
    for inst in ("BOOM1000", "JD10"):
        for raw in (1.0, 9.9, 10.0, 22.3, 25.0, 40.0, 75.0, 187.7, 300.0, 500.0):
            for atr in (2.0, 16.3, 60.0):
                assert PM.apply_rails(inst, raw, atr) == \
                    RESEARCH.apply_rails(inst, raw, atr), (inst, raw, atr)
    print("T2 rails OK (grille 2×10×3 == recherche)")


def t3_sl_whitebox():
    sig_bull = {"instrument": "BOOM1000", "direction": "bullish",
                "entry": 100.0, "sl_pts": 25.0}
    sig_bear = {"instrument": "JD10", "direction": "bearish",
                "entry": 100.0, "sl_pts": 40.0}
    f = {"entry": 100.0, "direction": "bullish", "atr15": 10.0, "swing": None,
         "ob": {"direction": "demand", "top": 95.0, "bottom": 90.0, "index": 3},
         "atr5": None, "atr30": None, "amps": [float(a) for a in range(5, 17)]}
    assert PM.pair_sl_raw("A2-ob-b0", f) == 10.0
    assert RESEARCH.variants_for(sig_bull, f, [])["A2-ob-b0"] == 10.0
    assert PM.pair_sl_raw("C-spk-P50", f) == \
        RESEARCH.variants_for(sig_bull, f, [])["C-spk-P50"] == 10.0
    fb = dict(f, direction="bearish",
              ob={"direction": "supply", "top": 130.0, "bottom": 120.0, "index": 3})
    assert PM.pair_sl_raw("A2-ob-b0", fb) == 30.0
    assert "A2-ob-b0" in RESEARCH.variants_for(sig_bear, fb, [])
    # NO_TRADE : pas d'OB / distance <= 0 / amps < 10 / feat None.
    assert PM.pair_sl_raw("A2-ob-b0", dict(f, ob=None)) is None
    assert "A2-ob-b0" not in RESEARCH.variants_for(sig_bull, dict(f, ob=None), [])
    bad = dict(fb, ob={"direction": "supply", "top": 90.0, "bottom": 80.0, "index": 3})
    assert PM.pair_sl_raw("A2-ob-b0", bad) is None
    assert "A2-ob-b0" not in RESEARCH.variants_for(sig_bear, bad, [])
    few = dict(f, amps=[1.0] * 9)
    assert PM.pair_sl_raw("C-spk-P50", few) is None
    assert "C-spk-P50" not in RESEARCH.variants_for(sig_bull, few, [])
    assert PM.pair_sl_raw("A2-ob-b0", None) is None
    try:
        PM.pair_sl_raw("ZZZ", f)
        raise AssertionError("paire inconnue acceptée !")
    except ValueError:
        pass
    print("T3 SL white-box OK (valeurs + NO_TRADE == recherche)")


def t4_features():
    m15, m5 = boom_tf()
    e = m15[-1]["epoch"]
    sig = {"instrument": "BOOM1000", "direction": "bullish",
           "entry": m15[-1]["close"], "entry_epoch": e}
    hist = {"BOOM1000": {"M15": m15, "M5": m5, "M30": m15[::5]}}
    fr = RESEARCH.features(sig, hist, [])
    fp = PM.paper_features("BOOM1000", "bullish", sig["entry"], e, m15, m5, e + 900)
    assert fr is not None and fp is not None
    assert fp["atr15"] == fr["atr15"] and fp["ob"] == fr["ob"] and \
        fp["amps"] == fr["amps"], (fp, fr)
    assert len(fp["amps"]) >= 10 and PM.pair_sl_raw("C-spk-P50", fp) == 20.0
    # Insuffisant des deux côtés.
    assert RESEARCH.features(sig, {"BOOM1000": {"M15": m15[:30], "M5": m5,
                                                "M30": []}}, []) is None
    assert PM.paper_features("BOOM1000", "bullish", sig["entry"], e,
                             m15[:30], m5, e + 900) is None
    print(f"T4 features OK (atr15={fp['atr15']:.2f}, spikes={len(fp['amps'])}, P50=20)")


def _e2e(m15, m5, instrument, direction, pair, m, fut_mk, want):
    e = m15[-1]["epoch"]
    live = {"id": f"LIVE-{instrument}", "instrument": instrument,
            "direction": direction, "entry": m15[-1]["close"], "entry_epoch": e}
    tf = {"M15": m15, "M5": m5}
    p1 = PM.paper_position(live, tf, e + 900)
    p2 = PM.paper_position(live, tf, e + 900)
    assert p1 == p2 and "no_trade" not in p1, p1  # déterminisme
    assert p1["id"] == f"LIVE-{instrument}:{pair}@TP{m:g}", p1["id"]
    hist = {instrument: {"M15": m15, "M5": m5, "M30": m15[::5]}}
    fr = RESEARCH.features(dict(live, sl_pts=1.0), hist, [])
    raw_r = RESEARCH.variants_for(dict(live, sl_pts=1.0), fr, [])[pair]
    sl_r, hit_r = RESEARCH.apply_rails(instrument, raw_r, fr["atr15"])
    assert (p1["sl_pts"], p1["rail_hit"], p1["raw_sl"]) == (sl_r, hit_r, raw_r)
    be = p1["entry"]
    slp, tpp = (be - sl_r, be + m * sl_r) if direction == "bullish" \
        else (be + sl_r, be - m * sl_r)
    assert (p1["sl_price"], p1["tp_price"]) == (slp, tpp)
    fut = fut_mk(e, be, slp, tpp)
    o_p = PM.resolve_paper(p1, fut)
    o_r = RESEARCH.simulate(dict(live, sl_pts=1.0), sl_r, m, fut)
    assert o_p is not None and o_r is not None
    for k in ("result", "r", "points", "bars_held", "closed_epoch"):
        assert o_p[k] == o_r[k], (k, o_p, o_r)
    assert (o_p["result"], o_p["r"]) == want, o_p
    return p1, o_p


def t5_e2e():
    m15, m5 = boom_tf()
    p, o = _e2e(m15, m5, "BOOM1000", "bullish", "C-spk-P50", 3.0,
                lambda e, be, slp, tpp:
                [C(e + 900 * i, be + (tpp - be) * i / 4,
                   be + (tpp - be) * i / 4 + 1, slp + 5,
                   be + (tpp - be) * i / 4) for i in range(1, 5)],
                ("TP", 3.0))
    print(f"T5a BOOM OK (SL={p['sl_pts']:.1f} rail={p['rail_hit']} -> TP +3R)")
    m15, m5 = jd_tf()
    fp = PM.paper_features("JD10", "bearish", m15[-1]["close"], m15[-1]["epoch"],
                           m15, m5, m15[-1]["epoch"] + 900)
    assert fp["ob"] is not None and fp["ob"]["direction"] == "supply" and \
        fp["ob"]["top"] == 1037.0, fp["ob"]
    p, o = _e2e(m15, m5, "JD10", "bearish", "A2-ob-b0", 4.0,
                lambda e, be, slp, tpp:
                [C(e + 900, be + 1, slp + 2, be - 1, be)] * 1,  # SL barre 1
                ("SL", -1.0))
    assert p["raw_sl"] == 1037.0 - m15[-1]["close"] > 0
    print(f"T5b JD10 OK (OB top=1037, SL={p['sl_pts']:.2f} rail={p['rail_hit']} -> SL -1R)")
    # Paires non suivies -> NO_TRADE.
    live = {"id": "X", "instrument": "JD10", "direction": "bullish",
            "entry": 1.0, "entry_epoch": 1}
    assert PM.paper_position(live, {"M15": [], "M5": []}, 2)["no_trade"] == \
        "paire non suivie en paper"


def t6_store():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        PStore.init_db(path)
        pos = {"id": "L:A2-ob-b0@TP4", "live_id": "L", "instrument": "JD10",
               "direction": "bearish", "entry_epoch": 10, "entry": 100.0,
               "pair": "A2-ob-b0", "m": 4.0, "sl_pts": 30.0, "tp_pts": 120.0,
               "sl_price": 130.0, "tp_price": -20.0, "rail_hit": None, "raw_sl": 30.0}
        assert PStore.save_signal(path, pos, 11) is True
        assert PStore.save_signal(path, pos, 11) is False  # idempotent
        assert len(PStore.get_open(path)) == 1
        out = {"signal_id": pos["id"], "closed_epoch": 20, "result": "TP",
               "r": 4.0, "points": 120.0, "bars_held": 5, "exit_price": -20.0,
               "note": ""}
        assert PStore.close_signal(path, out) == "inserted"
        assert PStore.close_signal(path, out) == "already"
        assert PStore.get_open(path) == []
        st = PStore.get_stats(path)
        assert (st["n"], st["TP"], st["r_total"], st["open"]) == (1, 1, 4.0, 0), st
        assert st["by_pair"] == {"A2-ob-b0": {"n": 1, "r": 4.0}}, st
    finally:
        os.remove(path)
    print("T6 store OK (roundtrip + idempotence + stats)")


def main():
    t1_r_math()
    t2_rails()
    t3_sl_whitebox()
    t4_features()
    t5_e2e()
    t6_store()
    print("\n✅ PAPER : 6/6 checks verts")


if __name__ == "__main__":
    main()
