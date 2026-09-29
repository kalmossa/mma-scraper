#!/usr/bin/env python3
"""
db_connection.py - PostgreSQL connection helpers shared by every script.

Configuration is read from a `.env` file at the repository root (see
`.env.example`). Two targets are supported and selected with USE_SUPABASE:

  * a local PostgreSQL instance  (DB_* variables)
  * a hosted PostgreSQL such as Supabase  (SUPABASE_* variables)
"""

import os
from pathlib import Path

import psycopg2
from dotenv import load_dotenv
from psycopg2.extras import RealDictCursor

# Load .env from the repository root, whatever the current working directory.
ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

LOCAL_CONFIG = {
    "dbname": os.getenv("DB_NAME", "mma_scraper"),
    "user": os.getenv("DB_USER", "postgres"),
    "password": os.getenv("DB_PASSWORD", ""),
    "host": os.getenv("DB_HOST", "localhost"),
    "port": int(os.getenv("DB_PORT", "5432")),
}

SUPABASE_CONFIG = {
    "dbname": os.getenv("SUPABASE_DB", "postgres"),
    "user": os.getenv("SUPABASE_USER", "postgres"),
    "password": os.getenv("SUPABASE_PASSWORD", ""),
    "host": os.getenv("SUPABASE_HOST", ""),
    "port": int(os.getenv("SUPABASE_PORT", "5432")),
}

USE_SUPABASE = os.getenv("USE_SUPABASE", "False").lower() == "true"
DB_CONFIG = SUPABASE_CONFIG if USE_SUPABASE else LOCAL_CONFIG


def get_connection():
    """Return a new connection, or None if the database is unreachable."""
    try:
        return psycopg2.connect(**DB_CONFIG)
    except Exception as e:
        print(f"Database connection error: {e}")
        return None


def get_cursor(conn, dict_cursor=True):
    """Return a cursor, by default one that yields rows as dicts."""
    if dict_cursor:
        return conn.cursor(cursor_factory=RealDictCursor)
    return conn.cursor()


if __name__ == "__main__":
    conn = get_connection()
    if conn is None:
        raise SystemExit(1)
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM fighters")
    print(f"Connected ({'remote' if USE_SUPABASE else 'local'}): {cur.fetchone()[0]} fighters")
    conn.close()
