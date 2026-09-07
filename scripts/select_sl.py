"""
Sélection H-SL-ADAPTIVE — TRAIN → VAL → OOS + sensibilité (règle pré-enregistrée).

Lecture seule (JSON /tmp), affiche le classement et la proposition de verdict.
Protocole (registre) :
- JD10 bear : TRAIN=o21 → VAL=o14 (chevauchant : confirmation directionnelle
  uniquement, déclaré) → OOS=o0 (disjoint de TRAIN ✓).
- BOOM bull : TRAIN=o7+o14 poolés dédupliqués (chevauchement déclaré) → OOS=o0.
  Pas de VAL indépendant (mur M5) → robustesse par sensibilité ±1 cran.
- BOOM bear / JD10 bull : verdict INSUFFISANT si n < 15 (quelle que soit l'exp).

Règle d'acceptation (pré-enregistrée) : OOS_exp ≥ max(0, FIXED_OOS) ET TRAIN_exp > 0
ET signe confirmé VAL (JD10) ET sensibilité ±1 cran sans flip de signe ni chute
sous FIXED ET DD ≤ 1.5× FIXED ET couverture ≥ 80 %.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "scripts"))

from sl_models import TP_MULTS, metrics  # noqa: E402

FOLDS = {o: (json.load(open(f"/tmp/sl_o{o}.json", encoding="utf-8")),
             {t["id"]: t for t in
              json.load(open(f"/tmp/mae_o{o}.json", encoding="utf-8"))["trades"]})
         for o in (0, 7, 14, 21)}
FAMILY = lambda v: v.split("-")[0]  # noqa: E731


def pooled_train(inst, d, folds):
    """TRAIN poolé dédupliqué par id : {(vname, m): metrics} + n univers."""
    pool, ids = {}, set()
    for fold in folds:
        rep, mae = FOLDS[fold]
        for key, _ in rep["cells"].items():
            k_inst, k_d, vname, tpm = key.split("|")
            if (k_inst, k_d) != (inst, d):
                continue
            m = float(tpm[2:])
            for t in rep.get("trade_rows", {}).get(key, []):
                ids.add(t["id"])
                pool.setdefault((vname, m), {})[t["id"]] = (
                    t["r"], t["result"], mae[t["id"]]["closed_epoch"])
    out = {}
    for k, trades in pool.items():
        ordered = sorted(trades.values(), key=lambda x: x[2])
        out[k] = metrics([(r, res) for r, res, _ in ordered])
    return out, len(ids)


def fold_table(fold, inst, d):
    return {(v, float(t[2:])): m for (k, m) in FOLDS[fold][0]["cells"].items()
            if k.startswith(f"{inst}|{d}|") for v, t in [tuple(k.split("|")[2:4])]}


def show(title, table, top=12):
    print(f"--- {title} ---")
    rows = sorted(table.items(), key=lambda kv: (kv[1]["expectancy"]
                                                 if kv[1]["expectancy"] is not None
                                                 else -9), reverse=True)
    for (v, m), met in rows[:top]:
        print(f"  {v:>16} TP{m:<4g}: n={met['n']} WR={met['winrate']} "
              f"exp={met['expectancy']:+} R={met['r_total']:+} DD={met['max_dd']} PF={met['profit_factor']}")
    for (v, m), met in sorted(rows):
        if v == "FIXED":
            print(f"  [REF] {v:>11} TP{m:<4g}: n={met['n']} WR={met['winrate']} "
                  f"exp={met['expectancy']:+} R={met['r_total']:+} DD={met['max_dd']}")
    print()
    return rows


def finalists_of(ranked, k=3):
    fams, fins = set(), []
    for (v, m), met in ranked:
        if met["n"] < 15:
            continue
        if FAMILY(v) not in fams and len(fins) < k:
            fams.add(FAMILY(v))
            fins.append((v, m))
    fins.append(("FIXED", 3.0))
    return fins


def main():
    # ---------- JD10 bear : TRAIN o21 ----------
    train = fold_table(21, "JD10", "bearish")
    print("========== JD10 bearish — TRAIN o21 (n=23) ==========")
    ranked = show("TRAIN o21 (toutes variantes × TP)", train, top=12)
    fins = finalists_of(ranked)
    print(f"Finalistes JD10 : {fins}\n")

    # ---------- JD10 bear : VAL o14 ----------
    val = fold_table(14, "JD10", "bearish")
    print("========== JD10 bearish — VAL o14 (n=13, chevauchant) ==========")
    for v, m in fins:
        met = val.get((v, m))
        print(f"  {v:>16} TP{m:<4g}: " + (f"exp={met['expectancy']:+} R={met['r_total']:+} n={met['n']}"
                                          if met else "ABSENT"))
    for mm in TP_MULTS:
        met = val.get(("FIXED", mm))
        if met:
            print(f"  [REF] {'FIXED':>11} TP{mm:<4g}: exp={met['expectancy']:+} R={met['r_total']:+}")
    print()

    # ---------- BOOM bull : TRAIN poolé ----------
    btrain, nuniv = pooled_train("BOOM1000", "bullish", (7, 14))
    print(f"========== BOOM bullish — TRAIN poolé o7+o14 (univers dédupliqué n={nuniv}) ==========")
    ranked_b = show("TRAIN BOOM bull", btrain, top=12)
    bfins = finalists_of(ranked_b)
    print(f"Finalistes BOOM : {bfins}\n")

    # ---------- OOS o0 : finalistes ----------
    for title, inst, d, fns in (("JD10 bearish", "JD10", "bearish", fins),
                                ("BOOM bullish", "BOOM1000", "bullish", bfins)):
        oos = fold_table(0, inst, d)
        print(f"========== OOS o0 — {title} ==========")
        for v, m in fns:
            line = []
            for mm in TP_MULTS:
                met = oos.get((v, mm))
                line.append(f"TP{mm:g}:{met['expectancy']:+}/{met['r_total']:+}"
                            if met and met["expectancy"] is not None else f"TP{mm:g}:?")
            det = oos.get((v, m), {})
            print(f"  {v:>16} " + " ".join(line))
            print(f"    @TP{m:g}: n={det.get('n')} WR={det.get('winrate')} "
                  f"DD={det.get('max_dd')} PF={det.get('profit_factor')}")
        print()

    # ---------- Sensibilité ±1 cran (TRAIN, même TP) ----------
    grids = {"B-atr15-k": [0.8, 1.0, 1.2, 1.5, 1.8, 2.0],
             "B-atr5-k": [1.0, 1.2, 1.5, 1.8],
             "B-atr30-k": [1.0, 1.2, 1.5, 1.8],
             "C-spk-P": [50, 60, 70, 75, 80, 90], "C-jmp-P": [50, 60, 70, 75, 80, 90],
             "A1-swing-b": [0.0, 0.25, 0.5], "A2-ob-b": [0.0, 0.25, 0.5],
             "D2-plus-b": [0.25, 0.5, 1.0], "EXT-k": [1.0, 1.25, 1.5, 1.75, 2.0]}
    print("========== Sensibilité ±1 cran (TRAIN, même TP) ==========")
    for title, table, fns in (("JD10/o21", train, fins), ("BOOM/TRAIN", btrain, bfins)):
        print(f"--- {title} ---")
        for v, m in fns:
            if v == "FIXED":
                continue
            grid = next(((g, vals) for g, vals in grids.items()
                         if v == g or v.startswith(g)), None)
            if grid is None:
                print(f"  {v}: pas de grille (D1-max) — sensibilité N/A")
                continue
            gname, vals = grid
            try:
                cur = float(v[len(gname):])
            except ValueError:
                print(f"  {v}: suffixe illisible — N/A")
                continue
            i = vals.index(cur)
            row = []
            for j in (i - 1, i, i + 1):
                if 0 <= j < len(vals):
                    vv = f"{gname}{vals[j]:g}"
                    met = table.get((vv, m))
                    e = met["expectancy"] if met and met["expectancy"] is not None else None
                    row.append(f"{vv}={e:+}" if e is not None else f"{vv}=?")
            print(f"  {v} TP{m:g}: " + " | ".join(row))
        print()

    # ---------- Cas insuffisants ----------
    for inst, d in (("BOOM1000", "bearish"), ("JD10", "bullish")):
        ns = []
        for o in (0, 7, 14, 21):
            ks = [k for k in FOLDS[o][0]["cells"] if k.startswith(f"{inst}|{d}|FIXED|TP3")]
            ns.append(FOLDS[o][0]["cells"][ks[0]]["n"] if ks else 0)
        print(f"{inst} {d} : n par fold (o0/o7/o14/o21) = {ns} → "
              f"{'INSUFFISANT (<15, non validable)' if max(ns) < 15 else 'à traiter'}")


if __name__ == "__main__":
    main()
