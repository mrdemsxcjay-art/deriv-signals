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
    empty = {"n": 0, "today": 0, "week": 0, "open": 0, "r_total": 0.0,
             "r_avg": None, "tp": 0, "sl": 0, "expire": 0, "by_inst": {},
             "opens": []}
    if not os.path.exists(path):
        return [], empty
    con = sqlite3.connect(path)
    try:
        rows = con.execute(
            """SELECT s.instrument, s.direction, s.confidence, s.grade,
                      s.created_epoch, s.entry, s.sl_pts, s.tp_pts, s.status,
                      s.sl_price, s.tp_price, o.result, o.r, o.bars_held
               FROM signals s LEFT JOIN outcomes o ON o.signal_id = s.id
               ORDER BY s.created_epoch DESC LIMIT 20""").fetchall()
        now = int(time.time())
        day = (now // 86400) * 86400
        stats = dict(empty)
        stats["n"] = con.execute("SELECT COUNT(*) FROM signals").fetchone()[0]
        stats["today"] = con.execute(
            "SELECT COUNT(*) FROM signals WHERE created_epoch >= ?", (day,)).fetchone()[0]
        stats["week"] = con.execute(
            "SELECT COUNT(*) FROM signals WHERE created_epoch >= ?", (now - 7 * 86400,)).fetchone()[0]
        stats["open"] = con.execute(
            "SELECT COUNT(*) FROM signals s LEFT JOIN outcomes o ON o.signal_id = s.id "
            "WHERE o.signal_id IS NULL").fetchone()[0]
        agg = con.execute(
            "SELECT COALESCE(SUM(r), 0), SUM(result = 'TP'), SUM(result = 'SL'), "
            "SUM(result = 'EXPIRE') FROM outcomes").fetchone()
        stats["r_total"] = agg[0]
        stats["tp"], stats["sl"], stats["expire"] = agg[1] or 0, agg[2] or 0, agg[3] or 0
        n_closed = stats["tp"] + stats["sl"] + stats["expire"]
        stats["r_avg"] = stats["r_total"] / n_closed if n_closed else None
        for inst, n, tp, sl, ex, r, last in con.execute(
                """SELECT s.instrument, COUNT(*), SUM(o.result = 'TP'),
                          SUM(o.result = 'SL'), SUM(o.result = 'EXPIRE'),
                          COALESCE(SUM(o.r), 0), MAX(s.created_epoch)
                   FROM signals s LEFT JOIN outcomes o ON o.signal_id = s.id
                   GROUP BY s.instrument"""):
            stats["by_inst"][inst] = {"n": n, "TP": tp or 0, "SL": sl or 0,
                                      "EX": ex or 0, "r": r, "last": last}
        stats["opens"] = con.execute(
            """SELECT s.instrument, s.direction, s.confidence, s.grade,
                      s.created_epoch, s.entry, s.sl_price, s.tp_price
               FROM signals s LEFT JOIN outcomes o ON o.signal_id = s.id
               WHERE o.signal_id IS NULL ORDER BY s.created_epoch DESC""").fetchall()
        return rows, stats
    finally:
        con.close()


def _age(created, now):
    s = max(0, int(now - (created or now)))
    if s < 3600:
        return f"{s // 60} min"
    if s < 86400:
        return f"{s // 3600}h{(s % 3600) // 60:02d}"
    return f"{s // 86400}j {(s % 86400) // 3600}h"


def _rcls(v) -> str:
    try:
        return "pos" if float(v) >= 0 else "neg"
    except (TypeError, ValueError):
        return ""


def read_paper(path):
    """Stats paper (base auto-suffisante) ; absente/vide → None (jamais de crash)."""
    if not path or not os.path.exists(path):
        return None
    try:
        con = sqlite3.connect(path)
        try:
            n = con.execute("SELECT COUNT(*) FROM paper_outcomes").fetchone()[0]
            n_open = con.execute("SELECT COUNT(*) FROM paper_signals "
                                 "WHERE status = 'ACTIVE'").fetchone()[0]
            if not n and not n_open:
                return None
            tot = con.execute("SELECT COALESCE(SUM(r), 0) FROM paper_outcomes").fetchone()[0]
            base = con.execute("SELECT COALESCE(SUM(live_r), 0) FROM paper_signals "
                               "WHERE live_r IS NOT NULL").fetchone()[0]
            pairs = {}
            for pair, c, r, b in con.execute(
                    """SELECT s.pair, COUNT(*), COALESCE(SUM(o.r), 0),
                              COALESCE(SUM(s.live_r), 0)
                       FROM paper_outcomes o JOIN paper_signals s ON s.id = o.signal_id
                       GROUP BY s.pair"""):
                pairs[pair or "?"] = {"n": c, "r": r, "base": b}
            return {"n": n, "r": tot, "base_r": base, "open": n_open, "pairs": pairs}
        finally:
            con.close()
    except Exception:
        return None


CSS = """body{background:#0e1621;color:#e8eef5;font-family:system-ui,sans-serif;
max-width:880px;margin:0 auto;padding:16px}h1{font-size:20px}h2{font-size:15px;
color:#8b9cb5;margin-top:24px}.cards{display:flex;gap:8px;flex-wrap:wrap}
.card{background:#182533;border-radius:10px;padding:10px 14px;min-width:100px}
.card b{font-size:20px;display:block}.card span{font-size:12px;color:#8b9cb5}
table{width:100%;border-collapse:collapse;font-size:13px}
th{color:#8b9cb5;text-align:left;padding:6px 4px;border-bottom:1px solid #2a3b4d}
td{padding:6px 4px;border-bottom:1px solid #1c2a3a}.bull{color:#4ade80}
.bear{color:#f87171}.foot{color:#8b9cb5;font-size:12px;margin-top:24px}
.chip{display:inline-block;background:#182533;border-radius:20px;padding:4px 12px;
margin:2px;font-size:13px}.pos{color:#4ade80}.neg{color:#f87171}"""


def build(settings, rows, stats) -> str:
    now = datetime.now(timezone.utc).strftime("%d/%m/%Y %H:%M UTC")
    insts = settings.get("instruments", {})
    on = [n for n, i in insts.items() if i.get("enabled", True)]
    off = [n for n, i in insts.items() if not i.get("enabled", True)]
    sc = settings.get("scoring", {})
    wr = (100 * stats["tp"] / (stats["tp"] + stats["sl"] + stats["expire"])
          if stats["tp"] + stats["sl"] + stats["expire"] else 0)
    r_avg = stats.get("r_avg")
    r_avg_str = f"{r_avg:+.1f}R" if r_avg is not None else "—"
    chips = "".join(f"<span class='chip'>🟢 {esc(n)}</span>" for n in on)
    chips += "".join(f"<span class='chip'>⏸️ {esc(n)}</span>" for n in off)
    trs = ""
    for inst, direction, conf, grade, created, entry, sl, tp, status, sl_p, tp_p, result, r, bars in rows:
        side = ("<span class='bull'>ACHAT</span>" if direction == "bullish"
                else "<span class='bear'>VENTE</span>")
        tenue = f", {bars}×M15" if bars is not None else ""
        outcome = "⏳ ouvert" if result is None else (
            f"✅ TP (+{r:.1f}R{tenue})" if result == "TP" else (
                f"🛑 SL ({r:.1f}R{tenue})" if result == "SL" else f"⌛ EXPIRE ({r:+.1f}R{tenue})"))
        trs += (f"<tr><td>{esc(hm(created))}</td><td>{esc(inst)}</td><td>{side}</td>"
                f"<td>{esc(conf)} ({esc(grade)})</td>"
                f"<td>{'' if entry is None else f'{entry:.2f}'}</td>"
                f"<td>{'' if sl_p is None else f'{sl_p:.2f}'}</td>"
                f"<td>{'' if tp_p is None else f'{tp_p:.2f}'}</td>"
                f"<td>{outcome}</td></tr>")
    if not trs:
        trs = ("<tr><td colspan='8' style='color:#8b9cb5'>"
               "Aucun signal pour le moment.</td></tr>")
    now_ts = int(time.time())
    irows = ""
    for inst in sorted(stats["by_inst"]):
        v = stats["by_inst"][inst]
        res = v["TP"] + v["SL"] + v["EX"]
        w = (100 * v["TP"] / res) if res else 0
        irows += (f"<tr><td>{esc(inst)}</td><td>{v['n']}</td><td>{v['TP']}</td>"
                  f"<td>{v['SL']}</td><td>{v['EX']}</td>"
                  f"<td class='{_rcls(v['r'])}'>{v['r']:+.1f}R</td><td>{w:.0f} %</td></tr>")
    inst_html = (f"<h2>📊 PAR INSTRUMENT</h2><table><tr><th>Instr.</th><th>Signaux</th>"
                 f"<th>TP</th><th>SL</th><th>EX</th><th>R</th><th>WR</th></tr>{irows}</table>"
                 if irows else "")
    orows = ""
    for inst, direction, conf, grade, created, entry, sl_p, tp_p in stats.get("opens", []):
        side = ("<span class='bull'>ACHAT</span>" if direction == "bullish"
                else "<span class='bear'>VENTE</span>")
        orows += (f"<tr><td>{esc(inst)}</td><td>{side}</td><td>{esc(conf)} ({esc(grade)})</td>"
                  f"<td>{_age(created, now_ts)}</td>"
                  f"<td>{'' if entry is None else f'{entry:.2f}'}</td>"
                  f"<td>{'' if sl_p is None else f'{sl_p:.2f}'}</td>"
                  f"<td>{'' if tp_p is None else f'{tp_p:.2f}'}</td></tr>")
    opens_html = (f"<h2>⏳ POSITIONS OUVERTES ({len(stats.get('opens', []))})</h2>"
                  f"<table><tr><th>Instr.</th><th>Sens</th><th>Score</th><th>Depuis</th>"
                  f"<th>Entrée</th><th>Stop</th><th>Objectif</th></tr>{orows}</table>"
                  if orows else "")
    paper = stats.get("paper") or {}
    paper_html = ""
    if paper.get("n"):
        prows = "".join(f"<tr><td>{esc(pair)}</td><td>{v['n']}</td>"
                        f"<td class='{_rcls(v['r'])}'>{v['r']:+.1f}R</td>"
                        f"<td class='{_rcls(v['base'])}'>{v['base']:+.1f}R</td></tr>"
                        for pair, v in sorted(paper["pairs"].items()))
        paper_html = (f"<h2>📝 PAPER H-SL-ADAPTIVE (expérimental)</h2><div class='cards'>"
                      f"<div class='card'><b class='{_rcls(paper['r'])}'>{paper['r']:+.1f}R</b>"
                      f"<span>R paper (clôturés)</span></div>"
                      f"<div class='card'><b class='{_rcls(paper['base_r'])}'>{paper['base_r']:+.1f}R</b>"
                      f"<span>R baseline fixe</span></div>"
                      f"<div class='card'><b>{paper['open']}</b><span>papers ouverts</span></div></div>"
                      f"<table><tr><th>Modèle</th><th>n</th><th>R paper</th><th>R baseline</th>"
                      f"</tr>{prows}</table>")
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
<div class="card"><b>{stats['week']}</b><span>signaux (7 jours)</span></div>
<div class="card"><b>{stats['open']}</b><span>positions ouvertes</span></div>
<div class="card"><b class='{_rcls(stats['r_total'])}'>{stats['r_total']:+.1f}R</b><span>R cumulé (clôturés)</span></div>
<div class="card"><b class='{_rcls(r_avg or 0)}'>{r_avg_str}</b><span>R moyen / signal</span></div>
<div class="card"><b>{wr:.0f} %</b><span>winrate ({stats['tp']} TP / {stats['sl']} SL / {stats['expire']} EX)</span></div>
<div class="card"><b>{stats['n']}</b><span>signaux au total</span></div></div>
{inst_html}{opens_html}{paper_html}
<h2>📝 DERNIERS SIGNAUX</h2>
<table><tr><th>Heure UTC</th><th>Instr.</th><th>Sens</th><th>Score</th>
<th>Entrée</th><th>Stop</th><th>Objectif</th><th>Statut</th></tr>{trs}</table>
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
    stats["paper"] = read_paper(settings.get("paper", {}).get("db_path", "data/paper.db"))
    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(build(settings, rows, stats))
    print(f"✅ {args.out} ({stats['n']} signaux, {len(rows)} affichés).")


if __name__ == "__main__":
    main()
