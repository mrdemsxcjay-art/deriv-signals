"""
Analyse par trade — MAE/MFE, régimes D1, durées (H-SL-ADAPTIVE §3 + §4 diagnostic).

Lecture seule : lit une base replay + un fichier d'historiques, écrit un JSON.
Usage :
  python scripts/analyze_trades.py --db /tmp/fold_o0.db --histories data/backtest/histories_52d.json --out /tmp/mae_o0.json [--print]

Définitions (causales, en points, fenêtre = entrée → clôture d'origine) :
- MAE = excursion adverse max (bull : entry − min low ; bear : max high − entry).
- MFE = excursion favorable max (miroir).
- R = points / sl_pts d'origine. Régime D1 = (entry − EMA200)/ATR14, causal.
- would_tp_at_X : MAE < X et MFE ≥ tp_pts d'origine (diagnostic ; les vrais
  basculements sont re-simulés exactement par sl_models.py, pas approximés ici).
"""
import argparse
import json
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.analysis.candles import atr_value  # noqa: E402
from src.analysis.indicators import ema_value  # noqa: E402
from src.backtest.history import load_cache  # noqa: E402

SYM = {"JD10": "JD10", "BOOM1000": "BOOM1000"}
XGRID = {"BOOM1000": (25, 30, 35, 40, 50), "JD10": (40, 60, 80, 100, 150)}


def regime_of(d1, entry_epoch, entry):
    prior = [c for c in d1 if c["epoch"] <= entry_epoch]
    if len(prior) < 200:
        return None, None
    closes = [c["close"] for c in prior]
    ema = ema_value(closes, 200)
    atr = atr_value(prior, 14)
    if ema is None or not atr:
        return None, None
    dist = (entry - ema) / atr
    if dist < -1.0:
        bucket = "bear_profond"
    elif dist < -0.25:
        bucket = "bear_modere"
    elif dist <= 0.25:
        bucket = "neutre"
    else:
        bucket = "bull"
    return round(dist, 3), bucket


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--histories", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--print", action="store_true")
    args = ap.parse_args()

    hist = load_cache(args.histories)["histories"]
    con = sqlite3.connect(args.db)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        """SELECT s.*, o.result, o.r, o.points, o.bars_held, o.closed_epoch, o.exit_price
           FROM signals s LEFT JOIN outcomes o ON o.signal_id = s.id
           ORDER BY s.created_epoch""").fetchall()

    trades, n_open = [], 0
    for r in rows:
        s = dict(r)
        if s["result"] is None:
            n_open += 1
            continue
        m15 = hist[SYM[s["instrument"]]]["M15"]
        after = [b for b in m15 if s["entry_epoch"] < b["epoch"] <= s["closed_epoch"]]
        if s["direction"] == "bullish":
            mae = s["entry"] - min(b["low"] for b in after) if after else 0.0
            mfe = max(b["high"] for b in after) - s["entry"] if after else 0.0
        else:
            mae = max(b["high"] for b in after) - s["entry"] if after else 0.0
            mfe = s["entry"] - min(b["low"] for b in after) if after else 0.0
        dist, bucket = regime_of(hist[SYM[s["instrument"]]]["D1"],
                                 s["entry_epoch"], s["entry"])
        trades.append({
            "id": s["id"], "instrument": s["instrument"], "direction": s["direction"],
            "score": s["confidence"], "grade": s["grade"],
            "entry_epoch": s["entry_epoch"], "entry": s["entry"],
            "sl_pts": s["sl_pts"], "tp_pts": s["tp_pts"],
            "result": s["result"], "r": s["r"], "bars_held": s["bars_held"],
            "closed_epoch": s["closed_epoch"],
            "mae_pts": round(mae, 2), "mfe_pts": round(mfe, 2),
            "mae_r": round(mae / s["sl_pts"], 3), "mfe_r": round(mfe / s["sl_pts"], 3),
            "d1_dist_atr": dist, "regime": bucket,
            "model": "boom_buy" if (s["instrument"] == "BOOM1000"
                                    and s["direction"] == "bullish") else "decide",
            "would_tp_at": {str(x): bool(mae < x and mfe >= s["tp_pts"])
                            for x in XGRID[s["instrument"]]},
        })
    payload = {"db": args.db, "histories": args.histories,
               "n_closed": len(trades), "n_open": n_open, "trades": trades}
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(payload, f)
    print(f"{args.db}: {len(trades)} clôtures, {n_open} ouverts -> {args.out}")

    if args.print:
        from collections import defaultdict
        by = defaultdict(list)
        for t in trades:
            by[(t["instrument"], t["direction"])].append(t)
        for k in sorted(by):
            ts = by[k]
            tp = sum(1 for t in ts if t["result"] == "TP")
            sl = sum(1 for t in ts if t["result"] == "SL")
            maes = sorted(t["mae_pts"] for t in ts)
            med = maes[len(maes) // 2]
            prem = sum(1 for t in ts if t["result"] == "SL" and
                       any(t["would_tp_at"].values()))
            print(f"  {k[0]:>9} {k[1]:>8}: n={len(ts)} TP={tp} SL={sl} "
                  f"MAE_med={med:.1f} SL-avec-TP-atteignable(X-grid)={prem}")


if __name__ == "__main__":
    main()
