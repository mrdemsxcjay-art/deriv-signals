"""
Modèles de SL adaptatif — re-simulation exacte par (SL × TP) (H-SL-ADAPTIVE §2, §4, §6).

Lecture seule sur les données ; n'écrit que le JSON de sortie. AUCUN impact live.
Usage :
  python scripts/sl_models.py --db /tmp/fold_o0.db --histories data/backtest/histories_52d.json \
      --ticks data/backtest/JD10_ticks_deep.json --mae /tmp/mae_o0.json --out /tmp/sl_o0.json [--print]

Protocole :
- Univers FIXE par fold : signaux clôtures d'origine avec ≥96 barres M15 après
  l'entrée (horizon d'expiry complet ; sinon exclusion comptée, équitable).
- Chaque variante SL recalcule sl_pts (causal, bougies ≤ barre d'entrée ; M5
  clôturées au curseur), rails de sécurité §9, puis re-simule via le VRAI
  resolve_signal de production (SL-first, expiry 96).
- TP = m × sl_pts avec m ∈ {1.5, 2, 2.5, 3, 3.5, 4}. Risque 1 $ invariant (compta en R).
- Validations : FIXED@3R doit reproduire les outcomes d'origine à l'identique
  (preuve moteur) ; déterminisme (2ᵉ passe identique).
"""
import argparse
import json
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.analysis.candles import atr_value, find_swings  # noqa: E402
from src.analysis.order_blocks import active_zones, detect_order_blocks  # noqa: E402
from src.analysis.structure import detect_structure  # noqa: E402
from src.backtest.history import load_cache  # noqa: E402
from src.signals.tracker import resolve_signal  # noqa: E402
from src.synthetics.context import detect_jumps, detect_spikes  # noqa: E402

SYM = {"JD10": "JD10", "BOOM1000": "BOOM1000"}
TP_MULTS = (1.5, 2.0, 2.5, 3.0, 3.5, 4.0)
# Rails §9 (sécurité, pas du tuning) : [min_abs, max_abs], cap ATR relatif.
RAILS = {"BOOM1000": (10.0, 75.0), "JD10": (20.0, 300.0)}
ATR_CAP = 3.0


def _pct(sorted_vals, p):
    """Percentile déterministe (nearest-rank)."""
    if not sorted_vals:
        return None
    idx = min(len(sorted_vals) - 1, max(0, -(-p * len(sorted_vals) // 100) - 1))
    return sorted_vals[idx]


def features(sig, hist, jump_sizes):
    """Tout ce qui est causal à l'entrée (None si insuffisant → NO_TRADE ciblé)."""
    sym = SYM[sig["instrument"]]
    e = sig["entry_epoch"]
    m15 = [b for b in hist[sym]["M15"] if b["epoch"] <= e][-500:]
    if len(m15) < 60:
        return None
    m5 = [b for b in hist[sym]["M5"] if b["epoch"] + 300 <= e + 900]
    m30 = [b for b in hist[sym]["M30"] if b["epoch"] <= e]
    atr15 = atr_value(m15[-200:], 14)
    atr5 = atr_value(m5[-200:], 14) if len(m5) >= 15 else None
    atr30 = atr_value(m30[-200:], 14) if len(m30) >= 15 else None
    if atr15 is None or atr15 <= 0:
        return None
    n = len(m15)
    swings = find_swings(m15, 2)
    want = "L" if sig["direction"] == "bullish" else "H"
    cands = [s for s in swings if s["kind"] == want and s["index"] <= n - 4]
    swing = max(cands, key=lambda s: s["index"]) if cands else None
    struct = detect_structure(m15, 2)
    obs = active_zones(detect_order_blocks(m15, struct))
    want_ob = "demand" if sig["direction"] == "bullish" else "supply"
    ob = None
    rel = [o for o in obs if o["direction"] == want_ob]
    if rel:
        ob = max(rel, key=lambda o: o["index"])
    spikes = detect_spikes(m5, 100, 3.0) if len(m5) >= 101 else []
    amps = sorted(s["amplitude"] for s in spikes[-50:])
    return {"atr15": atr15, "atr5": atr5, "atr30": atr30, "swing": swing, "ob": ob,
            "amps": amps, "entry": sig["entry"], "direction": sig["direction"]}


def variants_for(sig, feat, jump_sizes):
    """Variantes SL : nom → distance brute (None = NO_TRADE : données insuffisantes)."""
    out = {"FIXED": sig["sl_pts"]}  # verbatim d'origine (preuve moteur)
    if feat is None:
        return out
    e, d = feat["entry"], feat["direction"]
    atr15 = feat["atr15"]
    bull = d == "bullish"
    # A1 : structurel swing (+ buffer × ATR).
    sw = feat["swing"]
    if sw is not None:
        dist = (e - sw["price"]) if bull else (sw["price"] - e)
        if dist > 0:
            for b in (0.0, 0.25, 0.5):
                out[f"A1-swing-b{b:g}"] = dist + b * atr15
    # A2 : structurel OB pertinent (+ buffer).
    ob = feat["ob"]
    if ob is not None:
        dist = (e - ob["bottom"]) if bull else (ob["top"] - e)
        if dist > 0:
            for b in (0.0, 0.25, 0.5):
                out[f"A2-ob-b{b:g}"] = dist + b * atr15
    # B : ATR.
    for k in (0.8, 1.0, 1.2, 1.5, 1.8, 2.0):
        out[f"B-atr15-k{k:g}"] = k * atr15
    # M5/M30 : grille complète (devenue pertinente : B-atr30-k1.5 en tête TRAIN BOOM).
    if feat["atr5"]:
        for k in (1.0, 1.2, 1.5, 1.8):
            out[f"B-atr5-k{k:g}"] = k * feat["atr5"]
    if feat["atr30"]:
        for k in (1.0, 1.2, 1.5, 1.8):
            out[f"B-atr30-k{k:g}"] = k * feat["atr30"]
    # C : volatilité (spikes BOOM / jumps JD10).
    amps = feat["amps"] if sig["instrument"] == "BOOM1000" else sorted(jump_sizes)
    if len(amps) >= 10:
        tag = "spk" if sig["instrument"] == "BOOM1000" else "jmp"
        for p in (50, 60, 70, 75, 80, 90):
            out[f"C-{tag}-P{p}"] = _pct(amps, p)
    # D1 : max(struct, ATR1.5, vol75).
    parts = [v for k, v in out.items()
             if k in ("A1-swing-b0.25", "B-atr15-k1.5",
                      "C-spk-P75", "C-jmp-P75") and v]
    if len(parts) == 3:
        out["D1-max"] = max(parts)
    # D2 : structurel + buffer ATR.
    base = out.get("A1-swing-b0")
    if base:
        for b in (0.25, 0.5, 1.0):
            out[f"D2-plus-b{b:g}"] = base + b * atr15
    # EXT : max(structure, k×ATR).
    if base:
        for k in (1.0, 1.25, 1.5, 1.75, 2.0):
            out[f"EXT-k{k:g}"] = max(base, k * atr15)
    return out


def apply_rails(inst, raw, atr15):
    lo, hi = RAILS[inst]
    cap = ATR_CAP * atr15
    v, hit = raw, None
    if v < lo:
        v, hit = lo, "min"
    if v > min(hi, cap):
        v, hit = min(hi, cap), ("max" if hi <= cap else "atr_cap")
    return v, hit


def simulate(sig, sl_pts, m, futures):
    mod = dict(sig)
    mod["sl_pts"] = sl_pts
    mod["tp_pts"] = m * sl_pts
    if sig["direction"] == "bullish":
        mod["sl_price"] = sig["entry"] - sl_pts
        mod["tp_price"] = sig["entry"] + m * sl_pts
    else:
        mod["sl_price"] = sig["entry"] + sl_pts
        mod["tp_price"] = sig["entry"] - m * sl_pts
    o = resolve_signal(mod, futures, 96)
    if o is None:
        return None
    # R COMPTABLE CORRECT : le tracker de production crédite TP=+3R / SL=−1R
    # (ratio 1:3 fixe). Ici le TP vaut m×SL : recréditer le TP à +m (l'EXPIRE
    # du tracker est déjà en unités du SL simulé : latent clampé — vérifié).
    o = dict(o)
    if o["result"] == "TP":
        o["r"] = float(m)
        o["points"] = float(mod["tp_pts"])
    elif o["result"] == "SL":
        o["r"] = -1.0
        o["points"] = -float(mod["sl_pts"])
    return o


def metrics(rows):
    """rows : [(r, result)] en ordre de clôture. Métriques §4 (+ streaks)."""
    n = len(rows)
    tps = sum(1 for _, res in rows if res == "TP")
    sls = sum(1 for _, res in rows if res == "SL")
    exs = n - tps - sls
    rsum = sum(r for r, _ in rows)
    gp = sum(r for r, _ in rows if r > 0)
    gl = -sum(r for r, _ in rows if r < 0)
    eq, peak, dd = 0.0, 0.0, 0.0
    mx, cur = 0, 0
    for r, _ in rows:
        eq += r
        peak = max(peak, eq)
        dd = max(dd, peak - eq)
        cur = cur + 1 if r < 0 else 0
        mx = max(mx, cur)
    wins = [r for r, _ in rows if r > 0]
    loss = [r for r, _ in rows if r < 0]
    return {"n": n, "TP": tps, "SL": sls, "EX": exs,
            "winrate": round(tps / n, 3) if n else None,
            "r_total": round(rsum, 2), "expectancy": round(rsum / n, 3) if n else None,
            "profit_factor": round(gp / gl, 2) if gl > 0 else (None if gp == 0 else 99.9),
            "avg_win": round(sum(wins) / len(wins), 2) if wins else None,
            "avg_loss": round(sum(loss) / len(loss), 2) if loss else None,
            "max_dd": round(dd, 2), "max_losing_streak": mx}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--histories", required=True)
    ap.add_argument("--ticks", required=True)
    ap.add_argument("--mae", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--print", action="store_true")
    args = ap.parse_args()

    hist = load_cache(args.histories)["histories"]
    ticks = load_cache(args.ticks)["ticks"]
    jump_sizes = [j["size"] for j in detect_jumps(ticks, 10.0)][-20:]
    mae = {t["id"]: t for t in json.load(open(args.mae, encoding="utf-8"))["trades"]}

    con = sqlite3.connect(args.db)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        """SELECT s.*, o.result AS o_result, o.r AS o_r, o.closed_epoch AS o_closed,
                  o.bars_held AS o_bars
           FROM signals s JOIN outcomes o ON o.signal_id = s.id
           ORDER BY s.created_epoch""").fetchall()

    universe, excluded_short, details = [], 0, {}
    m15_by_sym = {s: hist[s]["M15"] for s in hist}
    for r in rows:
        sig = dict(r)
        futures = [b for b in m15_by_sym[SYM[sig["instrument"]]]
                   if b["epoch"] > sig["entry_epoch"]]
        if len(futures) < 96:
            excluded_short += 1
            continue
        universe.append((sig, futures))

    # Variantes + features par signal (déterministe : 2ᵉ passe identique vérifiée).
    var_cache = {}
    for sig, _ in universe:
        feat = features(sig, hist, jump_sizes)
        var_cache[sig["id"]] = (feat, variants_for(sig, feat, jump_sizes))
    for sig, _ in universe[:5]:
        feat2 = features(sig, hist, jump_sizes)
        assert variants_for(sig, feat2, jump_sizes) == var_cache[sig["id"]][1], sig["id"]

    # PREUVE MOTEUR : FIXED@3R doit reproduire les outcomes d'origine à l'identique.
    for sig, futures in universe:
        o = simulate(sig, sig["sl_pts"], 3.0, futures)
        assert o is not None, sig["id"]
        assert (o["result"], round(o["r"], 9), o["closed_epoch"], o["bars_held"]) == \
               (sig["o_result"], round(sig["o_r"], 9), sig["o_closed"], sig["o_bars"]), \
               (sig["id"], o, sig["o_result"], sig["o_r"])
    print(f"PREUVE MOTEUR OK : FIXED@3R reproduit {len(universe)} outcomes à l'identique")

    # Grille complète : variante × TP.
    cells = {}
    for sig, futures in universe:
        feat, varmap = var_cache[sig["id"]]
        key = (sig["instrument"], sig["direction"])
        for vname, raw in varmap.items():
            sl, hit = apply_rails(sig["instrument"], raw, feat["atr15"]) \
                if feat is not None else (raw, None)
            for m in TP_MULTS:
                o = simulate(sig, sl, m, futures)
                assert o is not None
                cell = cells.setdefault((key, vname, m),
                                        {"rows": [], "sl_sum": 0.0, "rail": 0, "trades": []})
                cell["rows"].append((o["closed_epoch"], o["r"], o["result"]))
                cell["sl_sum"] += sl
                cell["rail"] += 1 if hit else 0
                cell["trades"].append({"id": sig["id"], "sl": round(sl, 2),
                                       "result": o["result"], "r": o["r"],
                                       "mae_pts": mae[sig["id"]]["mae_pts"],
                                       "mfe_pts": mae[sig["id"]]["mfe_pts"]})

    report = {"db": args.db, "universe": len(universe),
              "excluded_short_futures": excluded_short,
              "cells": {}}
    for (key, vname, m), cell in cells.items():
        ordered = sorted(cell["rows"])
        met = metrics([(r, res) for _, r, res in ordered])
        met["sl_avg"] = round(cell["sl_sum"] / met["n"], 1)
        met["rail_hit_pct"] = round(100 * cell["rail"] / met["n"], 1)
        maes = sorted(t["mae_pts"] for t in cell["trades"])
        mfes = sorted(t["mfe_pts"] for t in cell["trades"])
        met["mae_med"] = round(maes[len(maes) // 2], 1)
        met["mfe_med"] = round(mfes[len(mfes) // 2], 1)
        # Couverture : signaux exclus NO_TRADE pour cette variante.
        n_elig = sum(1 for sig, _ in universe
                     if (sig["instrument"], sig["direction"]) == key
                     and vname in var_cache[sig["id"]][1])
        met["coverage"] = f"{met['n']}/{n_elig + sum(1 for sig, _ in universe if (sig['instrument'], sig['direction']) == key and vname not in var_cache[sig['id']][1])}"
        report["cells"][f"{key[0]}|{key[1]}|{vname}|TP{m:g}"] = met

    report["trade_rows"] = {
        f"{key[0]}|{key[1]}|{vname}|TP{m:g}": cell["trades"]
        for (key, vname, m), cell in cells.items()}

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(report, f)
    print(f"{args.db}: univers={len(universe)} exclus(futures<96)={excluded_short} "
          f"cellules={len(report['cells'])} -> {args.out}")

    if args.print:
        for inst, d in (("BOOM1000", "bullish"), ("BOOM1000", "bearish"),
                        ("JD10", "bearish"), ("JD10", "bullish")):
            cand = [(k, v) for k, v in report["cells"].items()
                    if k.startswith(f"{inst}|{d}|") and k.endswith("|TP3")
                    and v["expectancy"] is not None]
            if not cand:
                continue
            print(f"--- {inst} {d} @TP3R (top 5 expectancy) ---")
            for k, v in sorted(cand, key=lambda kv: kv[1]["expectancy"], reverse=True)[:5]:
                name = k.split("|")[2]
                print(f"  {name:>16}: n={v['n']} WR={v['winrate']} exp={v['expectancy']:+} "
                      f"R={v['r_total']:+} DD={v['max_dd']} sl~{v['sl_avg']} "
                      f"rail={v['rail_hit_pct']}% cov={v['coverage']}")


if __name__ == "__main__":
    main()
