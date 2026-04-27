import os
import sqlite3
from agno.db.sqlite import SqliteDb


def initialize_database(db_file: str) -> None:
    """Initialize the database with required tables and seed data."""
    conn = sqlite3.connect(db_file)
    conn.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id TEXT NULL,
            email TEXT,
            role TEXT CHECK(role IN ('admin', 'user')) NOT NULL,
            mobile_phone TEXT,
            full_name TEXT
        )
    ''')
    # Insert seed data if not exists
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM users WHERE telegram_id = ?", ("8594319243",))
    if cursor.fetchone()[0] == 0:
        conn.execute('''
            INSERT INTO users (telegram_id, email, role, mobile_phone, full_name)
            VALUES (?, ?, ?, ?, ?)
        ''', ("8594319243", "andrea.menozzi@infinitearea.com", "admin", "+393479351303", "Andrea Menozzi"))
    conn.commit()
    conn.close()


def get_user_by_telegram_id(db_file: str, telegram_id: str) -> dict | None:
    """Return the user row matching telegram_id, or None if not found."""
    conn = sqlite3.connect(db_file)
    try:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM users WHERE telegram_id = ?", (telegram_id,))
        row = cursor.fetchone()
        return dict(row) if row is not None else None
    finally:
        conn.close()


def register_user_from_contact(db_file: str, telegram_id: str, phone: str, full_name: str) -> None:
    """Insert or update a user row from a Telegram contact-share event."""
    conn = sqlite3.connect(db_file)
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT id FROM users WHERE telegram_id = ?", (telegram_id,))
        row = cursor.fetchone()
        if row:
            conn.execute(
                "UPDATE users SET mobile_phone = ?, full_name = ? WHERE telegram_id = ?",
                (phone, full_name, telegram_id),
            )
        else:
            conn.execute(
                "INSERT INTO users (telegram_id, mobile_phone, full_name, role) VALUES (?, ?, ?, 'user')",
                (telegram_id, phone, full_name),
            )
        conn.commit()
    finally:
        conn.close()


def get_sqlite_db(db_file: str) -> SqliteDb:
    """Get configured SqliteDb instance."""
    return SqliteDb(
        db_file=db_file,
        session_table=os.getenv("AGNO_SESSION_TABLE", "telegram_sessions"),
        memory_table=os.getenv("AGNO_MEMORY_TABLE", "user_memories"),
    )