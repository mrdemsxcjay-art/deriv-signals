"""
Stockage SQLite — signaux + issues (TP/SL/EXPIRE).

- 1 fichier, 0 dépendance (sqlite3 stdlib), transmissible en CI via git.
- Chaque fonction ouvre/ferme sa connexion (pas d'état global).
- C1 (idempotence) : IDs déterministes (barre M15) + INSERT OR IGNORE
  (le 1er événement gagne, jamais d'écrasement) + flags notified_*
  (at-most-once par état persisté) + table meta (migrations one-shot).
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from typing import Any, Dict, List, Optional, Tuple

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
    status TEXT NOT NULL DEFAULT 'ACTIVE',
    notified_entry_at INTEGER NOT NULL DEFAULT 0,
    notified_close_at INTEGER NOT NULL DEFAULT 0
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
CREATE TABLE IF NOT EXISTS meta (
    k TEXT PRIMARY KEY,
    v TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_signals_inst_created ON signals(instrument, created_epoch);
"""

MIGRATION_NOTIF = "notif_flags_v1"

# notified_* : 0 = impayé (à envoyer), >0 = epoch d'envoi, -1 = expiré sans
# envoi (trop vieux), -2 = invalidé avant envoi (SL/TP déjà touché).


def init_db(path: str) -> str:
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    with sqlite3.connect(path) as con:
        con.executescript(SCHEMA)
    _migrate_notif_flags(path)
    return path


def _migrate_notif_flags(path: str) -> None:
    """Ajoute les colonnes notified_* (bases pré-C1) + backfill one-shot.

    Backfill : l'ancien système notifiait entrée et clôture dans le même run
    vert (une ligne présente ⟹ run vert ⟹ envoi effectué — un run rouge
    annulait tout via rollback). Les lignes existantes sont donc marquées
    notifiées pour ne jamais être ré-envoyées par le balayage des impayés.
    """
    with sqlite3.connect(path) as con:
        cols = {r[1] for r in con.execute("PRAGMA table_info(signals)").fetchall()}
        if "notified_entry_at" not in cols:
            con.execute("ALTER TABLE signals ADD COLUMN "
                        "notified_entry_at INTEGER NOT NULL DEFAULT 0")
        if "notified_close_at" not in cols:
            con.execute("ALTER TABLE signals ADD COLUMN "
                        "notified_close_at INTEGER NOT NULL DEFAULT 0")
        done = con.execute("SELECT v FROM meta WHERE k=?", (MIGRATION_NOTIF,)).fetchone()
        if done:
            return
        con.execute("UPDATE signals SET notified_entry_at = created_epoch "
                    "WHERE notified_entry_at = 0")
        con.execute("""UPDATE signals SET notified_close_at =
                       (SELECT closed_epoch FROM outcomes
                        WHERE outcomes.signal_id = signals.id)
                       WHERE status = 'CLOSED' AND notified_close_at = 0""")
        con.execute("INSERT INTO meta(k, v) VALUES (?, '1')", (MIGRATION_NOTIF,))


def save_signal(path: str, sig: Any) -> bool:
    """Insère un Signal (dataclass ou dict). Idempotent : True si inséré,
    False si doublon (la ligne existante est CONSERVÉE telle quelle —
    un signal CLOSED ne ressuscite jamais en ACTIVE)."""
    d = sig if isinstance(sig, dict) else sig.__dict__
    with sqlite3.connect(path) as con:
        cur = con.execute(
            """INSERT OR IGNORE INTO signals
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
        return cur.rowcount == 1


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


def close_signal(path: str, outcome: Any) -> str:
    """Enregistre l'issue et passe le signal en CLOSED (atomique).

    Idempotent : le 1er événement gagne. Retourne 'inserted' (clôture
    fraîche → à notifier) ou 'already' (re-résolution → ne PAS notifier,
    valeurs conservées telles quelles pour une comptabilisation R unique).
    """
    d = outcome if isinstance(outcome, dict) else outcome.__dict__
    with sqlite3.connect(path) as con:
        cur = con.execute(
            """INSERT OR IGNORE INTO outcomes
               (signal_id, closed_epoch, result, r, points, bars_held, exit_price, note)
               VALUES (?,?,?,?,?,?,?,?)""",
            (d["signal_id"], d["closed_epoch"], d["result"], d["r"],
             d["points"], d["bars_held"], d["exit_price"], d.get("note", "")),
        )
        if cur.rowcount != 1:
            return "already"
        con.execute("UPDATE signals SET status='CLOSED' WHERE id=?", (d["signal_id"],))
        return "inserted"


def mark_notified(path: str, signal_id: str, kind: str,
                  at_epoch: Optional[int] = None) -> bool:
    """Marque l'entrée ('entry') ou la clôture ('close') comme notifiée.

    Le 1er marquage gagne (idempotent). `at_epoch` : epoch d'envoi,
    -1 = expiré sans envoi, -2 = invalidé avant envoi. Retourne True si
    le marquage a été posé, False s'il existait déjà.
    """
    assert kind in ("entry", "close"), kind
    col = "notified_entry_at" if kind == "entry" else "notified_close_at"
    at = int(at_epoch) if at_epoch is not None else int(time.time())
    with sqlite3.connect(path) as con:
        cur = con.execute(
            f"UPDATE signals SET {col}=? WHERE id=? AND {col}=0", (at, signal_id)
        )
        return cur.rowcount == 1


def get_pending_entries(path: str, now: int, max_age_s: int) -> List[Dict[str, Any]]:
    """Entrées jamais notifiées et encore fraîches (rattrapage inter-runs)."""
    if not os.path.exists(path):
        return []
    with sqlite3.connect(path) as con:
        cur = con.execute(
            "SELECT * FROM signals WHERE notified_entry_at=0 AND created_epoch>=? "
            "ORDER BY created_epoch", (now - max_age_s,))
        return [_row_to_dict(cur, r) for r in cur.fetchall()]


def get_pending_closes(path: str, now: int,
                       max_age_s: int) -> List[Tuple[Dict[str, Any], Dict[str, Any]]]:
    """Clôtures jamais notifiées et encore fraîches : [(signal, outcome)]."""
    if not os.path.exists(path):
        return []
    out: List[Tuple[Dict[str, Any], Dict[str, Any]]] = []
    with sqlite3.connect(path) as con:
        cur = con.execute(
            """SELECT s.*,
                      o.closed_epoch AS o_closed_epoch, o.result AS o_result,
                      o.r AS o_r, o.points AS o_points, o.bars_held AS o_bars_held,
                      o.exit_price AS o_exit_price, o.note AS o_note
               FROM signals s JOIN outcomes o ON o.signal_id = s.id
               WHERE s.notified_close_at = 0 AND s.status = 'CLOSED'
                 AND o.closed_epoch >= ?
               ORDER BY o.closed_epoch""", (now - max_age_s,))
        cols = [c[0] for c in cur.description]
        for row in cur.fetchall():
            raw = dict(zip(cols, row))
            sig = {k: v for k, v in raw.items() if not k.startswith("o_")}
            for k in ("gates_json", "confluences_json", "context_json"):
                if k in sig and isinstance(sig[k], str):
                    try:
                        sig[k.replace("_json", "")] = json.loads(sig[k])
                    except json.JSONDecodeError:
                        sig[k.replace("_json", "")] = [] if k != "context_json" else {}
            out.append((sig, {
                "signal_id": sig["id"], "closed_epoch": raw["o_closed_epoch"],
                "result": raw["o_result"], "r": raw["o_r"], "points": raw["o_points"],
                "bars_held": raw["o_bars_held"], "exit_price": raw["o_exit_price"],
                "note": raw["o_note"]}))
    return out


def expire_stale_pending(path: str, now: int, entry_max_age_s: int,
                         close_max_age_s: int) -> Dict[str, int]:
    """Marque -1 (expiré, jamais envoyé) les impayés trop vieux.

    Un rattrapage trop tardif enverrait un signal au prix irréaliste : mieux
    vaut l'enterrer explicitement que le spammer. Retourne les compteurs.
    """
    if not os.path.exists(path):
        return {"entries": 0, "closes": 0}
    with sqlite3.connect(path) as con:
        e = con.execute(
            "UPDATE signals SET notified_entry_at=-1 WHERE notified_entry_at=0 "
            "AND created_epoch<?", (now - entry_max_age_s,)).rowcount
        c = con.execute(
            """UPDATE signals SET notified_close_at=-1 WHERE notified_close_at=0
               AND status='CLOSED' AND id IN
               (SELECT signal_id FROM outcomes WHERE closed_epoch<?)""",
            (now - close_max_age_s,)).rowcount
        return {"entries": e, "closes": c}


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
