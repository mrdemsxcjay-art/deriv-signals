"""
Stockage paper — base SQLite DÉDIÉE (jamais la base live).

Mêmes garanties C1 que la base live : IDs déterministes, INSERT OR IGNORE
(le 1er événement gagne), clôture idempotente ('inserted' / 'already').

La base est AUTO-SUFFISANTE pour la comparaison : chaque ligne paper porte
le snapshot baseline live (résultat + R + MAE/MFE du scénario fixe), copié
à la clôture du signal live (pas de dépendance à un historique périssable).
"""
from __future__ import annotations

import os
import sqlite3
from typing import Any, Dict, List, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS paper_signals (
    id TEXT PRIMARY KEY,
    live_id TEXT NOT NULL,
    instrument TEXT NOT NULL,
    direction TEXT NOT NULL,
    created_epoch INTEGER NOT NULL,
    entry_epoch INTEGER NOT NULL,
    entry REAL NOT NULL,
    pair TEXT,
    m REAL,
    sl_pts REAL,
    tp_pts REAL,
    sl_price REAL,
    tp_price REAL,
    rail_hit TEXT,
    raw_sl REAL,
    sl_fixed_pts REAL,
    tp_fixed_pts REAL,
    score INTEGER,
    grade TEXT,
    regime TEXT,
    d1_dist_atr REAL,
    live_created_epoch INTEGER,
    compute_ms REAL,
    no_trade_reason TEXT,
    live_result TEXT,
    live_r REAL,
    live_bars_held INTEGER,
    live_closed_epoch INTEGER,
    live_mae_pts REAL,
    live_mfe_pts REAL,
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
    mae_pts REAL,
    mfe_pts REAL,
    note TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS meta (
    k TEXT PRIMARY KEY,
    v TEXT NOT NULL DEFAULT ''
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
                rail_hit, raw_sl, sl_fixed_pts, tp_fixed_pts, score, grade,
                regime, d1_dist_atr, live_created_epoch, compute_ms, status)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'ACTIVE')""",
            (pos["id"], pos["live_id"], pos["instrument"], pos["direction"],
             created_epoch, pos["entry_epoch"], pos["entry"], pos.get("pair"),
             pos.get("m"), pos.get("sl_pts"), pos.get("tp_pts"),
             pos.get("sl_price"), pos.get("tp_price"), pos.get("rail_hit"),
             pos.get("raw_sl"), pos.get("sl_fixed_pts"), pos.get("tp_fixed_pts"),
             pos.get("score"), pos.get("grade"), pos.get("regime"),
             pos.get("d1_dist_atr"), pos.get("live_created_epoch"),
             pos.get("compute_ms")))
        return cur.rowcount == 1


def save_no_trade(path: str, row: Dict[str, Any], created_epoch: int) -> bool:
    """Ligne NO_TRADE (paire suivie mais SL incalculable) — baseline conservée."""
    with sqlite3.connect(path) as con:
        cur = con.execute(
            """INSERT OR IGNORE INTO paper_signals
               (id, live_id, instrument, direction, created_epoch, entry_epoch,
                entry, sl_fixed_pts, tp_fixed_pts, score, grade, regime,
                d1_dist_atr, live_created_epoch, compute_ms, no_trade_reason,
                status)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?, 'NO_TRADE')""",
            (row["id"], row["live_id"], row["instrument"], row["direction"],
             created_epoch, row["entry_epoch"], row["entry"],
             row.get("sl_fixed_pts"), row.get("tp_fixed_pts"), row.get("score"),
             row.get("grade"), row.get("regime"), row.get("d1_dist_atr"),
             row.get("live_created_epoch"), row.get("compute_ms"),
             row.get("no_trade_reason")))
        return cur.rowcount == 1


def has_live(path: str, live_id: str) -> bool:
    with sqlite3.connect(path) as con:
        r = con.execute("SELECT 1 FROM paper_signals WHERE live_id=? LIMIT 1",
                        (live_id,)).fetchone()
        return r is not None


def snapshot_live_close(path: str, live_id: str, result: str, r: float,
                        bars_held: int, closed_epoch: int,
                        mae_pts: float, mfe_pts: float) -> None:
    """Snapshot baseline : résultat du scénario SL fixe (règle 4-5)."""
    with sqlite3.connect(path) as con:
        con.execute(
            """UPDATE paper_signals SET live_result=?, live_r=?, live_bars_held=?,
                      live_closed_epoch=?, live_mae_pts=?, live_mfe_pts=?
               WHERE live_id=?""",
            (result, r, bars_held, closed_epoch, mae_pts, mfe_pts, live_id))


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
                exit_price, mae_pts, mfe_pts, note) VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (outcome["signal_id"], outcome["closed_epoch"], outcome["result"],
             outcome["r"], outcome["points"], outcome["bars_held"],
             outcome["exit_price"], outcome.get("mae_pts"), outcome.get("mfe_pts"),
             outcome.get("note", "")))
        if cur.rowcount == 1:
            con.execute("UPDATE paper_signals SET status='CLOSED' WHERE id=?",
                        (outcome["signal_id"],))
            return "inserted"
        return "already"


def get_outcomes(path: str) -> List[Dict[str, Any]]:
    """Clôtures paper + meta signal + snapshots live, ordre chronologique."""
    with sqlite3.connect(path) as con:
        con.row_factory = sqlite3.Row
        rows = con.execute(
            """SELECT s.*, o.closed_epoch AS o_closed, o.result AS o_result,
                      o.r AS o_r, o.points AS o_points, o.bars_held AS o_bars,
                      o.exit_price AS o_exit, o.mae_pts AS o_mae, o.mfe_pts AS o_mfe
               FROM paper_outcomes o JOIN paper_signals s ON s.id=o.signal_id
               ORDER BY o.closed_epoch""").fetchall()
        return [dict(r) for r in rows]


def get_stats(path: str) -> Dict[str, Any]:
    with sqlite3.connect(path) as con:
        o = con.execute(
            "SELECT result, COUNT(*), COALESCE(SUM(r),0) FROM paper_outcomes "
            "GROUP BY result").fetchall()
        n_open = con.execute(
            "SELECT COUNT(*) FROM paper_signals WHERE status='ACTIVE'").fetchone()[0]
        n_nt = con.execute(
            "SELECT COUNT(*) FROM paper_signals WHERE status='NO_TRADE'").fetchone()[0]
        by_pair = con.execute(
            """SELECT s.pair, COUNT(*), COALESCE(SUM(o.r),0)
               FROM paper_outcomes o JOIN paper_signals s ON s.id=o.signal_id
               GROUP BY s.pair""").fetchall()
    d = {r: (c, s) for r, c, s in o}
    n = sum(c for c, _ in d.values())
    return {"n": n, "TP": d.get("TP", (0, 0))[0], "SL": d.get("SL", (0, 0))[0],
            "EXPIRE": d.get("EXPIRE", (0, 0))[0],
            "r_total": round(sum(s for _, s in d.values()), 2), "open": n_open,
            "no_trade": n_nt,
            "by_pair": {p: {"n": c, "r": round(s, 2)} for p, c, s in by_pair}}


def get_meta(path: str, k: str) -> Optional[str]:
    with sqlite3.connect(path) as con:
        r = con.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
        return r[0] if r else None


def set_meta(path: str, k: str, v: str) -> None:
    with sqlite3.connect(path) as con:
        con.execute("INSERT OR REPLACE INTO meta(k, v) VALUES (?,?)", (k, v))
