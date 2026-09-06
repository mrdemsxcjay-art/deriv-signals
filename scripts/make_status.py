"""
Page statut publique — génère docs/index.html (100 % statique, auto-refresh 15 min).

Lit la base live (signaux + outcomes) + settings : config, stats, derniers signaux.
Base absente/vide → page valide « aucun signal pour le moment » (jamais de crash).

Usage : python scripts/make_status.py [--db data/signals.db] [--out docs/index.html]
"""
import argparse
import html
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import yaml  # noqa: E402


def esc(v) -> str:
    return html.escape("" if v is None else str(v), quote=False)


def hm(epoch) -> str:
    if epoch is None:
        return "—"
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%d/%m %H:%M")


def read_db(path):
    """Retourne (lignes, stats) ; base absente → ([], stats vides)."""
    empty = {"n": 0, "today": 0, "open": 0, "r_total": 0.0, "tp": 0, "sl": 0,
             "by_inst": {}}
    if not os.path.exists(path):
        return [], empty
    con = sqlite3.connect(path)
    try:
        rows = con.execute(
            """SELECT s.instrument, s.direction, s.confidence, s.grade,
                      s.created_epoch, s.entry, s.sl_pts, s.tp_pts, s.status,
                      o.result, o.r
               FROM signals s LEFT JOIN outcomes o ON o.signal_id = s.id
               ORDER BY s.created_epoch DESC LIMIT 20""").fetchall()
        day = (int(time.time()) // 86400) * 86400
        stats = dict(empty)
        stats["n"] = con.execute("SELECT COUNT(*) FROM signals").fetchone()[0]
        stats["today"] = con.execute(
            "SELECT COUNT(*) FROM signals WHERE created_epoch >= ?", (day,)).fetchone()[0]
        stats["open"] = con.execute(
            "SELECT COUNT(*) FROM signals s LEFT JOIN outcomes o ON o.signal_id = s.id "
            "WHERE o.signal_id IS NULL").fetchone()[0]
        agg = con.execute(
            "SELECT COALESCE(SUM(r), 0), SUM(result = 'TP'), SUM(result = 'SL') "
            "FROM outcomes").fetchone()
        stats["r_total"], stats["tp"], stats["sl"] = agg[0], agg[1] or 0, agg[2] or 0
        for inst, n in con.execute(
                "SELECT instrument, COUNT(*) FROM signals GROUP BY instrument"):
            stats["by_inst"][inst] = n
        return rows, stats
    finally:
        con.close()


CSS = """body{background:#0e1621;color:#e8eef5;font-family:system-ui,sans-serif;
max-width:720px;margin:0 auto;padding:16px}h1{font-size:20px}h2{font-size:15px;
color:#8b9cb5;margin-top:24px}.cards{display:flex;gap:8px;flex-wrap:wrap}
.card{background:#182533;border-radius:10px;padding:10px 14px;min-width:100px}
.card b{font-size:20px;display:block}.card span{font-size:12px;color:#8b9cb5}
table{width:100%;border-collapse:collapse;font-size:13px}
th{color:#8b9cb5;text-align:left;padding:6px 4px;border-bottom:1px solid #2a3b4d}
td{padding:6px 4px;border-bottom:1px solid #1c2a3a}.bull{color:#4ade80}
.bear{color:#f87171}.foot{color:#8b9cb5;font-size:12px;margin-top:24px}
.chip{display:inline-block;background:#182533;border-radius:20px;padding:4px 12px;
margin:2px;font-size:13px}"""


def build(settings, rows, stats) -> str:
    now = datetime.now(timezone.utc).strftime("%d/%m/%Y %H:%M UTC")
    insts = settings.get("instruments", {})
    on = [n for n, i in insts.items() if i.get("enabled", True)]
    off = [n for n, i in insts.items() if not i.get("enabled", True)]
    sc = settings.get("scoring", {})
    wr = (100 * stats["tp"] / (stats["tp"] + stats["sl"])
          if stats["tp"] + stats["sl"] else 0)
    chips = "".join(f"<span class='chip'>🟢 {esc(n)}</span>" for n in on)
    chips += "".join(f"<span class='chip'>⏸️ {esc(n)}</span>" for n in off)
    trs = ""
    for inst, direction, conf, grade, created, entry, sl, tp, status, result, r in rows:
        side = ("<span class='bull'>ACHAT</span>" if direction == "bullish"
                else "<span class='bear'>VENTE</span>")
        outcome = "⏳ ouvert" if result is None else (
            f"✅ TP (+{r:.1f}R)" if result == "TP" else (
                f"🛑 SL ({r:.1f}R)" if result == "SL" else f"⌛ EXPIRE ({r:+.1f}R)"))
        trs += (f"<tr><td>{esc(hm(created))}</td><td>{esc(inst)}</td><td>{side}</td>"
                f"<td>{esc(conf)} ({esc(grade)})</td>"
                f"<td>{'' if entry is None else f'{entry:.2f}'}</td>"
                f"<td>{outcome}</td></tr>")
    if not trs:
        trs = ("<tr><td colspan='6' style='color:#8b9cb5'>"
               "Aucun signal pour le moment.</td></tr>")
    return f"""<!DOCTYPE html><html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="900">
<title>🤖 Signaux Deriv — statut</title><style>{CSS}</style></head><body>
<h1>🤖 Robot signaux Deriv — statut</h1>
<p>🟢 En ligne · dernière mise à jour : <b>{esc(now)}</b> (recalculée toutes les 15 min)</p>
<p>{chips}</p>
<p style="color:#8b9cb5;font-size:13px">Seuil {esc(sc.get('threshold', '?'))} ·
max {esc(sc.get('max_per_day_per_instrument', '?'))}/j/instrument ·
stake {esc(settings.get('account', {}).get('stake_usd', '?'))} $ ·
analyse seule, 0 ordre, 100 % gratuit</p>
<h2>📊 STATS</h2><div class="cards">
<div class="card"><b>{stats['today']}</b><span>signaux aujourd'hui (UTC)</span></div>
<div class="card"><b>{stats['open']}</b><span>positions ouvertes</span></div>
<div class="card"><b>{stats['r_total']:+.1f}R</b><span>R cumulé (clôturés)</span></div>
<div class="card"><b>{wr:.0f} %</b><span>winrate TP ({stats['tp']} TP / {stats['sl']} SL)</span></div>
<div class="card"><b>{stats['n']}</b><span>signaux au total</span></div></div>
<h2>📝 DERNIERS SIGNAUX</h2>
<table><tr><th>Heure UTC</th><th>Instr.</th><th>Sens</th><th>Score</th>
<th>Entrée</th><th>Statut</th></tr>{trs}</table>
<p class="foot">⚠️ Analyse automatique — aucun ordre exécuté, pas un conseil financier.
· V10 en pause (étape 4 : 0 TP / 243 signaux).</p></body></html>
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/signals.db")
    ap.add_argument("--settings", default="config/settings.yaml")
    ap.add_argument("--out", default="docs/index.html")
    args = ap.parse_args()
    with open(args.settings, "r", encoding="utf-8") as f:
        settings = yaml.safe_load(f)
    rows, stats = read_db(args.db)
    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(build(settings, rows, stats))
    print(f"✅ {args.out} ({stats['n']} signaux, {len(rows)} affichés).")


if __name__ == "__main__":
    main()
