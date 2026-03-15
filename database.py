#!/usr/bin/env python3
"""
MorgaIA — database.py
Gestion SQLite : historique des analyses
"""

import sqlite3
import json
import os
from datetime import datetime

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "morgaia.db")


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS analyses (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            home        TEXT NOT NULL,
            away        TEXT NOT NULL,
            league      TEXT,
            top_pick    TEXT,
            top_pct     REAL,
            data        TEXT,
            created_at  TEXT DEFAULT (datetime('now'))
        )
    """)
    conn.commit()
    conn.close()


def save_analysis(home, away, league, data, top_pick=None, top_pct=None):
    conn = get_db()
    conn.execute(
        "INSERT INTO analyses (home, away, league, top_pick, top_pct, data) VALUES (?,?,?,?,?,?)",
        (home, away, league, top_pick, top_pct, json.dumps(data, ensure_ascii=False))
    )
    conn.commit()
    conn.close()


def get_history(limit=50):
    conn = get_db()
    rows = conn.execute(
        "SELECT id, home, away, league, top_pick, top_pct, created_at FROM analyses ORDER BY created_at DESC LIMIT ?",
        (limit,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_analysis_by_id(hid):
    conn = get_db()
    row = conn.execute("SELECT * FROM analyses WHERE id=?", (hid,)).fetchone()
    conn.close()
    if not row:
        return None
    d = dict(row)
    try:
        d["data"] = json.loads(d["data"])
    except:
        pass
    return d


def delete_analysis(hid):
    conn = get_db()
    conn.execute("DELETE FROM analyses WHERE id=?", (hid,))
    conn.commit()
    conn.close()
