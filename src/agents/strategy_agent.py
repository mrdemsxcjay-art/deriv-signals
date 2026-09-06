"""
Agent stratégie — pipeline 5 portes (§4) + spécialisations JD10/BOOM1000.

Portes (tout doit s'aligner) :
1. D1  : tendance de fond — EMA200 + dernière structure SMC (BOS/CHoCH).
2. H4  : tendance principale — EMA50/EMA200 + zone OB/FVG active directionnelle.
3. H1  : direction autorisée — position vs EMA50 (structure H1 contraire tolérée).
4. M15 : zone de décision — DERNIÈRE cassure (≤ 18 bougies) + retest (≤ 1,2 ATR).
5. M5/M30 : timing — confirmation (engulfing/pin bar) = BONUS +10, jamais bloquante.

Spécialisations :
- V10 : symétrique pur.
- JD10 : symétrique + SL ≥ max(plancher, 1,5 × jump médian des 20 derniers) +
  risque JUMP repensé (SL vs distribution : ÉLEVÉ < médiane, MOYEN < P90, FAIBLE ≥ P90).
- BOOM1000 : asymétrique — SELL pipeline complet (setup principal) ; BUY uniquement
  en « capitalisation de spike » (spike ≤ 45 min + porte M15 haussière, SANS les
  portes D1/H4/H1 — exception contre-tendance documentée). Ratio 1:3 inviolé partout.

Paramètre `prep` (Étape 4, replay) : détecteurs PRÉ-CALCULÉS sur l'historique complet
puis filtrés au curseur : {"D1": {"struct": [...]}, "H4": {"struct","obs","fvgs"},
"M15": {...}}. Les détecteurs étant causaux (preuve anti-repaint étape 2), le
résultat est IDENTIQUE au calcul frais sur le préfixe — en ~100× plus rapide.
Production : prep=None (calcul frais). Fidélité prouvée dans test_replay.py.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from ..analysis.candles import atr_value, find_swings  # noqa: F401 (réexport doc)
from ..analysis.fvg import active_fvgs, detect_fvg
from ..analysis.indicators import (
    body_ratio, ema_value, is_bearish_engulfing, is_bullish_engulfing,
    pin_bar, rsi_value,
)
from ..analysis.order_blocks import active_zones, detect_order_blocks
from ..analysis.structure import detect_structure
from ..signals.scoring import compute_score

BULL, BEAR = "bullish", "bearish"
LABEL = {BULL: "haussier", BEAR: "baissier"}

JUMP_CAVEAT = ("~3 jumps/h : le risque PAR JUMP s'accumule sur la durée du trade "
               "(replay étape 4 mesurera le taux réel de stoppés-par-jump).")


# ------------------------------------------------------------------ portes ---

@dataclass
class GateResult:
    name: str
    direction: str
    passed: bool
    detail: str
    data: dict = field(default_factory=dict)


def gate_d1(d1: List[dict], direction: str, ema_period: int = 200,
            strength: int = 2, prep: Optional[dict] = None) -> GateResult:
    closes = [c["close"] for c in d1]
    ema = ema_value(closes, ema_period)
    if ema is None:
        return GateResult("D1", direction, False,
                          f"D1 : EMA{ema_period} indisponible ({len(d1)}<{ema_period} bougies)")
    close = closes[-1]
    ok = close > ema if direction == BULL else close < ema
    if not ok:
        op = "≤" if direction == BULL else "≥"
        return GateResult("D1", direction, False,
                          f"D1 : clôture {close:.2f} {op} EMA{ema_period} {ema:.2f}")
    struct = prep["struct"] if prep else detect_structure(d1, strength)
    if not struct:
        return GateResult("D1", direction, False, "D1 : aucune structure (BOS/CHoCH)")
    last = struct[-1]
    if last["direction"] != direction:
        return GateResult("D1", direction, False,
                          f"D1 : dernière structure {LABEL[last['direction']]} "
                          f"@{last['index']} (attendue {LABEL[direction]})")
    return GateResult("D1", direction, True,
                      f"D1 {LABEL[direction]} : clôture {close:.2f} vs EMA{ema_period} {ema:.2f} "
                      f"+ {last['type']} @{last['index']} (niveau {last['level']:.2f})",
                      {"ema": ema, "close": close, "break": last})


def _directional_zones(tf: List[dict], direction: str, strength: int) -> Tuple[list, list]:
    struct = detect_structure(tf, strength)
    want_ob = "demand" if direction == BULL else "supply"
    want_fvg = BULL if direction == BULL else BEAR
    obs = [o for o in active_zones(detect_order_blocks(tf, struct)) if o["direction"] == want_ob]
    fvgs = [f for f in active_fvgs(detect_fvg(tf)) if f["direction"] == want_fvg]
    return obs, fvgs


def _prep_zones(prep: dict, direction: str) -> Tuple[list, list]:
    want_ob = "demand" if direction == BULL else "supply"
    want_fvg = BULL if direction == BULL else BEAR
    obs = [o for o in prep.get("obs", []) if o["direction"] == want_ob and not o["mitigated"]]
    fvgs = [f for f in prep.get("fvgs", []) if f["direction"] == want_fvg and not f["mitigated"]]
    return obs, fvgs


def gate_h4(h4: List[dict], direction: str, ema_fast: int = 50, ema_slow: int = 200,
            strength: int = 2, prep: Optional[dict] = None) -> GateResult:
    closes = [c["close"] for c in h4]
    ef, es = ema_value(closes, ema_fast), ema_value(closes, ema_slow)
    if ef is None or es is None:
        return GateResult("H4", direction, False,
                          f"H4 : EMA{ema_fast}/EMA{ema_slow} indisponibles ({len(h4)} bougies)")
    aligned = ef > es if direction == BULL else ef < es
    if not aligned:
        return GateResult("H4", direction, False,
                          f"H4 : EMA{ema_fast} {ef:.2f} vs EMA{ema_slow} {es:.2f} "
                          f"(pas {LABEL[direction]})")
    close = closes[-1]
    if (direction == BULL and close <= ef) or (direction == BEAR and close >= ef):
        op = "≤" if direction == BULL else "≥"
        return GateResult("H4", direction, False,
                          f"H4 : clôture {close:.2f} {op} EMA{ema_fast} {ef:.2f}")
    obs, fvgs = _prep_zones(prep, direction) if prep else _directional_zones(h4, direction, strength)
    if not obs and not fvgs:
        return GateResult("H4", direction, False,
                          f"H4 : aucune zone OB/FVG active {LABEL[direction]}")
    z = (obs + fvgs)[0]
    kind = "OB" if z in obs else "FVG"
    return GateResult("H4", direction, True,
                      f"H4 {LABEL[direction]} : EMA{ema_fast} {ef:.2f} vs EMA{ema_slow} {es:.2f} "
                      f"+ {len(obs)} OB / {len(fvgs)} FVG actifs "
                      f"(ex. {kind} {z['bottom']:.2f}-{z['top']:.2f})",
                      {"ema_fast": ef, "ema_slow": es, "obs": obs, "fvgs": fvgs})


def gate_h1(h1: List[dict], direction: str, ema_period: int = 50) -> GateResult:
    closes = [c["close"] for c in h1]
    ema = ema_value(closes, ema_period)
    if ema is None:
        return GateResult("H1", direction, False,
                          f"H1 : EMA{ema_period} indisponible ({len(h1)}<{ema_period} bougies)")
    close = closes[-1]
    ok = close > ema if direction == BULL else close < ema
    if not ok:
        op = "≤" if direction == BULL else "≥"
        return GateResult("H1", direction, False,
                          f"H1 : clôture {close:.2f} {op} EMA{ema_period} {ema:.2f} "
                          f"(direction {LABEL[direction]} interdite)")
    return GateResult("H1", direction, True,
                      f"H1 {LABEL[direction]} : clôture {close:.2f} vs EMA{ema_period} {ema:.2f} "
                      f"(structure H1 non vérifiée = tolérée)",
                      {"ema": ema, "close": close})


def gate_m15(m15: List[dict], direction: str, strength: int = 2, max_break_age: int = 18,
             retest_atr_mult: float = 1.2, atr_period: int = 14,
             prep: Optional[dict] = None) -> GateResult:
    struct = prep["struct"] if prep else detect_structure(m15, strength)
    if not struct:
        return GateResult("M15", direction, False, "M15 : aucune cassure (BOS/CHoCH)")
    last = struct[-1]
    if last["direction"] != direction:
        return GateResult("M15", direction, False,
                          f"M15 : dernière cassure {LABEL[last['direction']]} "
                          f"@{last['index']} (attendue {LABEL[direction]})")
    age = (len(m15) - 1) - last["index"]
    if age > max_break_age:
        return GateResult("M15", direction, False,
                          f"M15 : cassure {LABEL[direction]} trop ancienne "
                          f"(@{last['index']}, il y a {age}>{max_break_age} bougies)")
    atr = atr_value(m15, atr_period)
    if atr is None or atr <= 0:
        return GateResult("M15", direction, False, "M15 : ATR indisponible")
    close = m15[-1]["close"]
    best, via, in_zone, used = abs(close - last["level"]), "niveau", False, None
    obs, fvgs = _prep_zones(prep, direction) if prep else _directional_zones(m15, direction, strength)
    for z in obs + fvgs:
        if z["bottom"] <= close <= z["top"]:
            best, via, in_zone, used = 0.0, "zone", True, z
            break
        d = min(abs(close - z["top"]), abs(close - z["bottom"]))
        if d < best:
            best, via, used = d, "zone", z
    limit = retest_atr_mult * atr
    if best > limit:
        return GateResult("M15", direction, False,
                          f"M15 : pas de retest (dist {best:.1f} pts > "
                          f"{retest_atr_mult}×ATR {limit:.1f})")
    return GateResult("M15", direction, True,
                      f"M15 {LABEL[direction]} : {last['type']} @{last['index']} "
                      f"(il y a {age}) + retest {via} ({best:.1f} pts, {best / atr:.2f} ATR)",
                      {"break": last, "level": last["level"], "age": age, "atr": atr,
                       "dist": best, "dist_atr": best / atr, "in_zone": in_zone,
                       "zone": used})


def timing_bonus(m5: List[dict], m30: List[dict], direction: str) -> Tuple[bool, str]:
    """Porte 5 = BONUS uniquement. Confirmation sur dernière M5 ou M30 clôturée."""
    want_pin = BULL if direction == BULL else BEAR
    for name, tf in (("M5", m5), ("M30", m30)):
        if len(tf) < 2:
            continue
        prev, cur = tf[-2], tf[-1]
        eng = (is_bullish_engulfing(prev, cur) if direction == BULL
               else is_bearish_engulfing(prev, cur))
        if eng:
            return True, f"timing : englobante {LABEL[direction]} {name}"
        if pin_bar(cur) == want_pin:
            return True, f"timing : pin bar {LABEL[direction]} {name}"
    return False, "timing : aucune confirmation M5/M30 (bonus non acquis, non bloquant)"


# ------------------------------------------------- SL / contexte / risque ---

def stop_points(instrument: str, jump_ctx: Optional[dict], floors: Dict[str, float],
                jump_mult: float = 1.5) -> Tuple[float, str]:
    """SL en points : plancher, élargi (jamais réduit) pour JD10."""
    floor = floors[instrument]
    if instrument == "JD10" and jump_ctx and jump_ctx.get("median_size"):
        need = jump_mult * jump_ctx["median_size"]
        sl = max(floor, need)
        mult_fr = f"{jump_mult}".replace(".", ",")
        return sl, f"SL JD10 = max(plancher {floor}, {mult_fr}×jump médian {need:.1f}) = {sl:.1f} pts"
    note = f"SL plancher {instrument} = {floor} pts"
    if instrument == "JD10":
        note += " (stats jumps insuffisantes → plancher, prudent)"
    return floor, note


def jump_risk_label(sl_pts: float, med: Optional[float],
                    p90: Optional[float]) -> Tuple[str, str]:
    """Risque JUMP repensé (validé étape 2) : SL vs distribution des jumps."""
    if med is None or p90 is None:
        return "INCONNU", "stats jumps insuffisantes"
    if sl_pts < med:
        return "ÉLEVÉ", f"SL {sl_pts:.1f} < jump médian {med:.1f} (>50% des jumps traversent)"
    if sl_pts < p90:
        return "MOYEN", f"SL {sl_pts:.1f} < P90 {p90:.1f}"
    return "FAIBLE", f"SL {sl_pts:.1f} ≥ P90 {p90:.1f}"


def context_aligned(instrument: str, direction: str, ctx: dict, sl_pts: float,
                    P: Dict[str, Any]) -> Tuple[bool, str]:
    """Bonus +10 contexte (§6) — règles par instrument (provisoires, replay étape 4)."""
    if instrument == "V10":
        pct = ctx["vol"]["percentile"]
        ok = pct is not None and P.get("v10_lo", 33) <= pct <= P.get("v10_hi", 66)
        return ok, f"V10 : percentile ATR {pct} dans [33,66] : {ok}"
    if instrument == "JD10":
        p90 = (ctx.get("jump") or {}).get("p90_size")
        ok = p90 is not None and sl_pts >= p90
        return ok, f"JD10 : SL {sl_pts:.1f} vs P90 jumps {p90} : {ok}"
    b = ctx.get("boom") or {}
    if direction == BEAR:  # SELL juste après un spike = drift qui reprend
        tss, avg = b.get("time_since_spike_min"), b.get("avg_interval_min")
        mult = P.get("boom_sell_spike_mult", 2.0)
        ok = tss is not None and avg is not None and tss <= mult * avg
        return ok, f"BOOM SELL : spike il y a {tss} min (≤{mult}×{avg}) : {ok}"
    tss = b.get("time_since_spike_min")  # BUY : porte ⇒ spike ≤45 min ⇒ aligné
    return True, f"BOOM BUY : spike il y a {tss} min (capitalisation)"


# ------------------------------------------------------------- décisions ---

@dataclass
class Decision:
    instrument: str
    direction: str
    passed: bool
    blocked_by: Optional[str] = None
    gates: List[GateResult] = field(default_factory=list)
    score: Optional[int] = None
    grade: Optional[str] = None
    breakdown: dict = field(default_factory=dict)
    features: dict = field(default_factory=dict)
    plan: dict = field(default_factory=dict)
    confluences: List[str] = field(default_factory=list)
    context_snapshot: dict = field(default_factory=dict)

    @property
    def gates_passed(self) -> int:
        return sum(1 for g in self.gates if g.passed)


def _blocked(instrument: str, direction: str, gates: List[GateResult]) -> Decision:
    return Decision(instrument, direction, False,
                    blocked_by=gates[-1].detail if gates else "aucune porte évaluée",
                    gates=gates)


def _finish(instrument: str, direction: str, gates: List[GateResult], tf: dict,
            ctx: dict, P: Dict[str, Any], floors: Dict[str, float]) -> Decision:
    """Assemble plan + features + score une fois les portes passées."""
    m15, m5, m30, h1 = tf["M15"], tf["M5"], tf["M30"], tf["H1"]
    conf, conf_detail = timing_bonus(m5, m30, direction)
    sl_pts, sl_note = stop_points(instrument, ctx.get("jump"), floors,
                                  P.get("jump_sl_mult", 1.5))
    entry = m15[-1]["close"]
    tp_pts = 3.0 * sl_pts
    if direction == BULL:
        sl_price, tp_price = entry - sl_pts, entry + tp_pts
    else:
        sl_price, tp_price = entry + sl_pts, entry - tp_pts
    g4 = next(g for g in gates if g.name == "M15")
    ctx_ok, ctx_detail = context_aligned(instrument, direction, ctx, sl_pts, P)
    pct = ctx["vol"]["percentile"]
    regime_ok = pct is not None and P.get("regime_lo", 10) <= pct <= P.get("regime_hi", 90)
    strength_ok = body_ratio(m15[-1]) >= P.get("strength_min", 0.5)
    rsi = rsi_value([c["close"] for c in h1], P.get("rsi_period", 14))
    margin = P.get("rsi_margin", 5)
    rsi_ok = rsi is not None and ((rsi >= 50 + margin) if direction == BULL
                                  else (rsi <= 50 - margin))
    score, grade, breakdown = compute_score(
        g4.data.get("in_zone", False), conf, ctx_ok, regime_ok, strength_ok, rsi_ok)
    confluences = [g.detail for g in gates if g.passed]
    if g4.data.get("in_zone"):
        confluences.append("retest DANS zone OB/FVG (+10)")
    if conf:
        confluences.append(f"{conf_detail} (+10)")
    if ctx_ok:
        confluences.append(f"{ctx_detail} (+10)")
    if regime_ok:
        confluences.append(f"régime volatilité favorable (pct {pct:.0f}) (+10)")
    if strength_ok:
        confluences.append("bougie signal forte (+5)")
    if rsi_ok:
        confluences.append(f"RSI H1 {rsi:.1f} avec marge (+5)")
    snapshot = {"sl_note": sl_note, "rsi_h1": rsi, "timing": conf_detail,
                "vol": ctx["vol"]}
    if instrument == "JD10":
        j = ctx.get("jump") or {}
        label, detail = jump_risk_label(sl_pts, j.get("median_size"), j.get("p90_size"))
        snapshot["jump"] = dict(j)
        snapshot["jump_risk"] = label
        snapshot["jump_risk_detail"] = f"{detail}. {JUMP_CAVEAT}"
        confluences.append(f"risque JUMP : {label} ({detail})")
    if instrument == "BOOM1000":
        b = dict(ctx.get("boom") or {})
        hi = b.get("last_spike_high")
        b["dist_to_spike"] = (hi - entry) if hi is not None else None
        snapshot["boom"] = b
        if direction == BEAR and hi is not None:
            confluences.append(f"distance au dernier spike : {hi - entry:+.1f} pts "
                               f"(un spike peut stopper ce SELL)")
    if instrument == "V10":
        snapshot["v10"] = dict(ctx.get("v10") or {})
    return Decision(instrument, direction, True, gates=gates, score=score,
                    grade=grade, breakdown=breakdown,
                    features={"retest_in_zone": g4.data.get("in_zone", False),
                              "confirmation": conf, "context_aligned": ctx_ok,
                              "regime_ok": regime_ok, "strength_ok": strength_ok,
                              "rsi_ok": rsi_ok},
                    plan={"entry": entry, "sl_pts": sl_pts, "tp_pts": tp_pts,
                          "sl_price": sl_price, "tp_price": tp_price,
                          "entry_epoch": m15[-1]["epoch"]},
                    confluences=confluences, context_snapshot=snapshot)


def decide(instrument: str, direction: str, tf: dict, ctx: dict,
           P: Dict[str, Any], floors: Dict[str, float],
           prep: Optional[dict] = None) -> Decision:
    """Pipeline complet 4 portes (+timing bonus) pour UNE direction."""
    s = P.get("swing_strength", 2)
    prep = prep or {}
    gates = []
    g1 = gate_d1(tf["D1"], direction, P.get("ema_d1", 200), s, prep.get("D1"))
    gates.append(g1)
    if not g1.passed:
        return _blocked(instrument, direction, gates)
    g2 = gate_h4(tf["H4"], direction, P.get("ema_h4_fast", 50), P.get("ema_h4_slow", 200),
                 s, prep.get("H4"))
    gates.append(g2)
    if not g2.passed:
        return _blocked(instrument, direction, gates)
    g3 = gate_h1(tf["H1"], direction, P.get("ema_h1", 50))
    gates.append(g3)
    if not g3.passed:
        return _blocked(instrument, direction, gates)
    g4 = gate_m15(tf["M15"], direction, s, P.get("m15_max_break_age", 18),
                  P.get("m15_retest_atr_mult", 1.2), P.get("atr_period", 14),
                  prep.get("M15"))
    gates.append(g4)
    if not g4.passed:
        return _blocked(instrument, direction, gates)
    return _finish(instrument, direction, gates, tf, ctx, P, floors)


def decide_boom_buy(tf: dict, ctx: dict, P: Dict[str, Any],
                    floors: Dict[str, float], prep: Optional[dict] = None) -> Decision:
    """BOOM BUY exceptionnel : spike ≤ 45 min + porte M15 haussière (sans D1/H4/H1)."""
    tss = (ctx.get("boom") or {}).get("time_since_spike_min")
    max_age = P.get("boom_buy_max_age_min", 45)
    if tss is None or tss > max_age:
        return Decision("BOOM1000", BULL, False,
                        blocked_by=f"BOOM BUY : dernier spike il y a {tss} min "
                                   f"(requis ≤ {max_age} : pas de capitalisation)")
    g4 = gate_m15(tf["M15"], BULL, P.get("swing_strength", 2),
                  P.get("m15_max_break_age", 18), P.get("m15_retest_atr_mult", 1.2),
                  P.get("atr_period", 14), (prep or {}).get("M15"))
    if not g4.passed:
        return _blocked("BOOM1000", BULL, [g4])
    spike_gate = GateResult("SPIKE", BULL, True,
                            f"spike confirmé il y a {tss:.0f} min (≤ {max_age})")
    return _finish("BOOM1000", BULL, [spike_gate, g4], tf, ctx, P, floors)


def evaluate_instrument(instrument: str, tf: dict, ctx: dict,
                        P: Dict[str, Any], floors: Dict[str, float],
                        prep: Optional[dict] = None) -> Decision:
    """1 instrument ⇒ 0 ou 1 décision (jamais 2 signaux opposés simultanés)."""
    if instrument == "BOOM1000":
        cands = [decide(instrument, BEAR, tf, ctx, P, floors, prep),
                 decide_boom_buy(tf, ctx, P, floors, prep)]
    else:
        cands = [decide(instrument, BULL, tf, ctx, P, floors, prep),
                 decide(instrument, BEAR, tf, ctx, P, floors, prep)]
    passed = [d for d in cands if d.passed]
    if len(passed) == 1:
        return passed[0]
    if len(passed) == 2:
        if passed[0].score != passed[1].score:
            return max(passed, key=lambda d: d.score or 0)
        return Decision(instrument, "ambiguous", False,
                        blocked_by=f"signaux opposés à égalité ({passed[0].score} pts : ambigu)")
    cands.sort(key=lambda d: d.gates_passed, reverse=True)  # log le plus informatif
    return cands[0]
