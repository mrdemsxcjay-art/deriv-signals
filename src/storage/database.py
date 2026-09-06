"""
Stockage SQLite — signaux + issues (TP/SL/EXPIRE).

- 1 fichier, 0 dépendance (sqlite3 stdlib), transmissible en CI via actions/cache.
- Chaque fonction ouvre/ferme sa connexion (pas d'état global).
"""
from __future__ import annotations

import json
import os
import sqlite3
from typing import Any, Dict, List, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS signals (
    id TEXT PRIMARY KEY,
    instrument TEXT NOT NULL,
    symbol TEXT NOT NULL,
    direction TEXT NOT NULL,
    created_epoch INTEGER NOT NULL,
    entry_epoch INTEGER NOT NULL,
    entry REAL NOT NULL,
    sl_pts REAL NOT NULL,
    tp_pts REAL NOT NULL,
    sl_price REAL NOT NULL,
    tp_price REAL NOT NULL,
    stake_usd REAL NOT NULL,
    confidence INTEGER NOT NULL,
    grade TEXT NOT NULL,
    gates_json TEXT NOT NULL DEFAULT '[]',
    confluences_json TEXT NOT NULL DEFAULT '[]',
    context_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'ACTIVE'
);
CREATE TABLE IF NOT EXISTS outcomes (
    signal_id TEXT PRIMARY KEY REFERENCES signals(id),
    closed_epoch INTEGER NOT NULL,
    result TEXT NOT NULL,
    r REAL NOT NULL,
    points REAL NOT NULL,
    bars_held INTEGER NOT NULL,
    exit_price REAL NOT NULL,
    note TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_signals_inst_created ON signals(instrument, created_epoch);
"""


def init_db(path: str) -> str:
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    with sqlite3.connect(path) as con:
        con.executescript(SCHEMA)
    return path


def save_signal(path: str, sig: Any) -> str:
    """Insère un Signal (dataclass ou dict). Retourne son id."""
    d = sig if isinstance(sig, dict) else sig.__dict__
    with sqlite3.connect(path) as con:
        con.execute(
            """INSERT OR REPLACE INTO signals
               (id, instrument, symbol, direction, created_epoch, entry_epoch,
                entry, sl_pts, tp_pts, sl_price, tp_price, stake_usd,
                confidence, grade, gates_json, confluences_json, context_json, status)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (d["id"], d["instrument"], d["symbol"], d["direction"],
             d["created_epoch"], d["entry_epoch"], d["entry"],
             d["sl_pts"], d["tp_pts"], d["sl_price"], d["tp_price"],
             d["stake_usd"], d["confidence"], d["grade"],
             json.dumps(d.get("gates", [])), json.dumps(d.get("confluences", [])),
             json.dumps(d.get("context", {})), d.get("status", "ACTIVE")),
        )
    return d["id"]


def _row_to_dict(cur: sqlite3.Cursor, row: tuple) -> Dict[str, Any]:
    d = {c[0]: v for c, v in zip(cur.description, row)}
    for k in ("gates_json", "confluences_json", "context_json"):
        if k in d and isinstance(d[k], str):
            try:
                d[k.replace("_json", "")] = json.loads(d[k])
            except json.JSONDecodeError:
                d[k.replace("_json", "")] = [] if k != "context_json" else {}
    return d


def get_open_signals(path: str) -> List[Dict[str, Any]]:
    if not os.path.exists(path):
        return []
    with sqlite3.connect(path) as con:
        cur = con.execute("SELECT * FROM signals WHERE status='ACTIVE' ORDER BY created_epoch")
        return [_row_to_dict(cur, r) for r in cur.fetchall()]


def get_signal(path: str, signal_id: str) -> Optional[Dict[str, Any]]:
    """Un signal par son id (dict, JSON décodés) ou None."""
    if not os.path.exists(path):
        return None
    with sqlite3.connect(path) as con:
        cur = con.execute("SELECT * FROM signals WHERE id=?", (signal_id,))
        row = cur.fetchone()
        return _row_to_dict(cur, row) if row else None


def count_since(path: str, instrument: str, since_epoch: int) -> int:
    """Nombre de signaux émis pour l'instrument depuis `since_epoch` (anti-spam)."""
    if not os.path.exists(path):
        return 0
    with sqlite3.connect(path) as con:
        cur = con.execute(
            "SELECT COUNT(*) FROM signals WHERE instrument=? AND created_epoch>=?",
            (instrument, since_epoch),
        )
        return int(cur.fetchone()[0])


def last_created(path: str, instrument: str) -> Optional[int]:
    """Epoch du dernier signal émis pour l'instrument (cooldown), None si aucun."""
    if not os.path.exists(path):
        return None
    with sqlite3.connect(path) as con:
        cur = con.execute(
            "SELECT MAX(created_epoch) FROM signals WHERE instrument=?", (instrument,)
        )
        v = cur.fetchone()[0]
        return int(v) if v is not None else None


def close_signal(path: str, outcome: Any) -> None:
    """Enregistre l'issue et passe le signal en CLOSED (atomique)."""
    d = outcome if isinstance(outcome, dict) else outcome.__dict__
    with sqlite3.connect(path) as con:
        con.execute(
            """INSERT OR REPLACE INTO outcomes
               (signal_id, closed_epoch, result, r, points, bars_held, exit_price, note)
               VALUES (?,?,?,?,?,?,?,?)""",
            (d["signal_id"], d["closed_epoch"], d["result"], d["r"],
             d["points"], d["bars_held"], d["exit_price"], d.get("note", "")),
        )
        con.execute("UPDATE signals SET status='CLOSED' WHERE id=?", (d["signal_id"],))


def get_stats(path: str) -> Dict[str, Any]:
    """KPIs : n, TP/SL/EXPIRE, winrate (TP / résolus), R total, R moyen."""
    stats: Dict[str, Any] = {"n": 0, "TP": 0, "SL": 0, "EXPIRE": 0,
                             "winrate": None, "r_total": 0.0, "r_avg": None, "open": 0}
    if not os.path.exists(path):
        return stats
    with sqlite3.connect(path) as con:
        cur = con.execute("SELECT result, r FROM outcomes")
        rows = cur.fetchall()
        cur = con.execute("SELECT COUNT(*) FROM signals WHERE status='ACTIVE'")
        stats["open"] = int(cur.fetchone()[0])
    stats["n"] = len(rows)
    for res, r in rows:
        if res in stats:
            stats[res] += 1
        stats["r_total"] += float(r)
    if rows:
        stats["r_avg"] = stats["r_total"] / len(rows)
        resolved = stats["TP"] + stats["SL"] + stats["EXPIRE"]
        stats["winrate"] = stats["TP"] / resolved if resolved else None
    return stats
