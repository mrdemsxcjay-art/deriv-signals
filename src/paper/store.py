"""
Stockage paper — base SQLite DÉDIÉE (jamais la base live).

Mêmes garanties C1 que la base live : IDs déterministes, INSERT OR IGNORE
(le 1er événement gagne), clôture idempotente ('inserted' / 'already').
"""
from __future__ import annotations

import os
import sqlite3
from typing import Any, Dict, List

SCHEMA = """
CREATE TABLE IF NOT EXISTS paper_signals (
    id TEXT PRIMARY KEY,
    live_id TEXT NOT NULL,
    instrument TEXT NOT NULL,
    direction TEXT NOT NULL,
    created_epoch INTEGER NOT NULL,
    entry_epoch INTEGER NOT NULL,
    entry REAL NOT NULL,
    pair TEXT NOT NULL,
    m REAL NOT NULL,
    sl_pts REAL NOT NULL,
    tp_pts REAL NOT NULL,
    sl_price REAL NOT NULL,
    tp_price REAL NOT NULL,
    rail_hit TEXT,
    raw_sl REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'ACTIVE'
);
CREATE TABLE IF NOT EXISTS paper_outcomes (
    signal_id TEXT PRIMARY KEY REFERENCES paper_signals(id),
    closed_epoch INTEGER NOT NULL,
    result TEXT NOT NULL,
    r REAL NOT NULL,
    points REAL NOT NULL,
    bars_held INTEGER NOT NULL,
    exit_price REAL NOT NULL,
    note TEXT NOT NULL DEFAULT ''
);
"""


def init_db(path: str) -> str:
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    with sqlite3.connect(path) as con:
        con.executescript(SCHEMA)
    return path


def save_signal(path: str, pos: Dict[str, Any], created_epoch: int) -> bool:
    """INSERT OR IGNORE : True = inséré, False = déjà présent."""
    with sqlite3.connect(path) as con:
        cur = con.execute(
            """INSERT OR IGNORE INTO paper_signals
               (id, live_id, instrument, direction, created_epoch, entry_epoch,
                entry, pair, m, sl_pts, tp_pts, sl_price, tp_price,
                rail_hit, raw_sl, status)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'ACTIVE')""",
            (pos["id"], pos["live_id"], pos["instrument"], pos["direction"],
             created_epoch, pos["entry_epoch"], pos["entry"], pos["pair"],
             pos["m"], pos["sl_pts"], pos["tp_pts"], pos["sl_price"],
             pos["tp_price"], pos.get("rail_hit"), pos["raw_sl"]))
        return cur.rowcount == 1


def _row_to_dict(cols: List[str], row: tuple) -> Dict[str, Any]:
    return dict(zip(cols, row))


def get_open(path: str) -> List[Dict[str, Any]]:
    with sqlite3.connect(path) as con:
        con.row_factory = sqlite3.Row
        rows = con.execute(
            "SELECT * FROM paper_signals WHERE status='ACTIVE' "
            "ORDER BY created_epoch").fetchall()
        return [dict(r) for r in rows]


def close_signal(path: str, outcome: Dict[str, Any]) -> str:
    """Clôture idempotente : 'inserted' (fraîche) ou 'already'."""
    with sqlite3.connect(path) as con:
        cur = con.execute(
            """INSERT OR IGNORE INTO paper_outcomes
               (signal_id, closed_epoch, result, r, points, bars_held,
                exit_price, note) VALUES (?,?,?,?,?,?,?,?)""",
            (outcome["signal_id"], outcome["closed_epoch"], outcome["result"],
             outcome["r"], outcome["points"], outcome["bars_held"],
             outcome["exit_price"], outcome.get("note", "")))
        if cur.rowcount == 1:
            con.execute("UPDATE paper_signals SET status='CLOSED' WHERE id=?",
                        (outcome["signal_id"],))
            return "inserted"
        return "already"


def get_stats(path: str) -> Dict[str, Any]:
    with sqlite3.connect(path) as con:
        o = con.execute(
            "SELECT result, COUNT(*), COALESCE(SUM(r),0) FROM paper_outcomes "
            "GROUP BY result").fetchall()
        n_open = con.execute(
            "SELECT COUNT(*) FROM paper_signals WHERE status='ACTIVE'").fetchone()[0]
        by_pair = con.execute(
            """SELECT s.pair, COUNT(*), COALESCE(SUM(o.r),0)
               FROM paper_outcomes o JOIN paper_signals s ON s.id=o.signal_id
               GROUP BY s.pair""").fetchall()
    d = {r: (c, s) for r, c, s in o}
    n = sum(c for c, _ in d.values())
    return {"n": n, "TP": d.get("TP", (0, 0))[0], "SL": d.get("SL", (0, 0))[0],
            "EXPIRE": d.get("EXPIRE", (0, 0))[0],
            "r_total": round(sum(s for _, s in d.values()), 2), "open": n_open,
            "by_pair": {p: {"n": c, "r": round(s, 2)} for p, c, s in by_pair}}
