"""
Rapport paper H-SL-ADAPTIVE — LIVE BASELINE vs PAPER ADAPTATIF.

Lecture seule sur data/paper.db (auto-suffisant : snapshots baseline inclus).
Usage : python scripts/paper_report.py [--db data/paper.db]

Règle de décision (rappel, JAMAIS automatique) : recommander l'activation
réelle uniquement si le paper confirme les refs OOS ; sinon KEEP CURRENT LIVE.
"""
import argparse
import os
import sys
from statistics import median

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from src.paper import store as PStore  # noqa: E402

# Références OOS validées (verdicts 07/09/2026) — comparaison, pas tuning.
OOS_REF = {("JD10", "A2-ob-b0"): {"exp": 0.394, "n": 37, "tp": "TP4"},
           ("BOOM1000", "C-spk-P50"): {"exp": 0.304, "n": 47, "tp": "TP3"}}


def seq_metrics(rows):
    """rows : [(r, result)] ordre chrono -> n, WR, exp, R, DD."""
    n = len(rows)
    if not n:
        return {"n": 0, "WR": None, "exp": None, "R": 0.0, "DD": 0.0}
    tps = sum(1 for _, res in rows if res == "TP")
    rs = [r for r, _ in rows]
    eq, peak, dd = 0.0, 0.0, 0.0
    for r in rs:
        eq += r
        peak = max(peak, eq)
        dd = max(dd, peak - eq)
    return {"n": n, "WR": round(tps / n, 3), "exp": round(sum(rs) / n, 3),
            "R": round(sum(rs), 2), "DD": round(dd, 2)}


def build_report(paper_db):
    PStore.init_db(paper_db)
    outs = PStore.get_outcomes(paper_db)
    stats = PStore.get_stats(paper_db)
    per = {}
    for o in outs:
        per.setdefault((o["instrument"], o["pair"]), []).append(o)
    rows = []
    for key in sorted(per):
        os_ = per[key]
        pm = seq_metrics([(o["o_r"], o["o_result"]) for o in os_])
        bl = [(o["live_r"], o["live_result"]) for o in os_ if o["live_r"] is not None]
        bm = seq_metrics(bl)
        maes = sorted(o["o_mae"] for o in os_ if o["o_mae"] is not None)
        mfes = sorted(o["o_mfe"] for o in os_ if o["o_mfe"] is not None)
        ref = OOS_REF.get(key, {})
        rows.append({"instrument": key[0], "pair": key[1],
                     "paper": pm, "baseline": bm,
                     "mae_med": round(median(maes), 1) if maes else None,
                     "mfe_med": round(median(mfes), 1) if mfes else None,
                     "oos_exp": ref.get("exp"), "oos_n": ref.get("n")})
    return {"rows": rows, "open": stats["open"], "no_trade": stats["no_trade"],
            "protocol": PStore.get_meta(paper_db, "protocol")}


def _fmt(x, dec=3):
    return "—" if x is None else f"{x:+.{dec}f}" if isinstance(x, float) else str(x)


def render(rep):
    L = ["", "📝 PAPER H-SL-ADAPTIVE — LIVE BASELINE vs PAPER ADAPTATIF",
         f"   protocole : {rep['protocol']} | ouverts : {rep['open']} | "
         f"NO_TRADE : {rep['no_trade']}"]
    if not rep["rows"]:
        return "\n".join(L + ["   (aucune clôture paper pour l'instant)"])
    L.append("")
    L.append("   Instrument | Modèle    | Signaux | WR    | Expectancy | R total | DD   | MAE   | MFE   | Baseline (fixe)      | Verdict")
    for r in rep["rows"]:
        p, b = r["paper"], r["baseline"]
        base = (f"exp{_fmt(b['exp'])} R{_fmt(b['R'], 2)} n={b['n']}"
                if b["n"] else "en attente")
        gap = "" if p["exp"] is None or r["oos_exp"] is None \
            else f" (OOS {r['oos_exp']:+}, écart {p['exp'] - r['oos_exp']:+.3f})"
        L.append(f"   {r['instrument']:>10} | {r['pair']:<9} | {p['n']:>7} | "
                 f"{_fmt(p['WR']):>5} | {_fmt(p['exp']):>10} | {_fmt(p['R'], 2):>7} | "
                 f"{p['DD']:>4} | {_fmt(r['mae_med'], 1):>5} | {_fmt(r['mfe_med'], 1):>5} | "
                 f"{base:<20} | EN COURS{gap}")
    L += ["",
          "   Règle de décision (humaine, JAMAIS automatique) : activation réelle",
          "   uniquement si le paper confirme les refs OOS ; sinon KEEP CURRENT LIVE."]
    return "\n".join(L)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/paper.db")
    args = ap.parse_args()
    print(render(build_report(args.db)))


if __name__ == "__main__":
    main()
