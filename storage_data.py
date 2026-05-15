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




def get_sqlite_db(db_file: str) -> SqliteDb:
    """Get configured SqliteDb instance."""
    return SqliteDb(
        db_file=db_file,
        session_table=os.getenv("AGNO_SESSION_TABLE", "telegram_sessions"),
        memory_table=os.getenv("AGNO_MEMORY_TABLE", "user_memories"),
    )


def get_user_by_phone(db_file: str, phone: str) -> dict | None:
    conn = sqlite3.connect(db_file)
    try:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM users WHERE mobile_phone = ?", (phone,))
        row = cursor.fetchone()
        return dict(row) if row is not None else None
    finally:
        conn.close()


def create_user_by_phone(db_file: str, phone: str) -> tuple[bool, dict | None]:
    """Add user by phone if not already present. Returns (created, user_dict)."""
    conn = sqlite3.connect(db_file)
    try:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM users WHERE mobile_phone = ?", (phone,))
        row = cursor.fetchone()
        if row is not None:
            return False, dict(row)
        conn.execute(
            "INSERT INTO users (mobile_phone, role) VALUES (?, 'user')",
            (phone,),
        )
        conn.commit()
        cursor.execute("SELECT * FROM users WHERE mobile_phone = ?", (phone,))
        row = cursor.fetchone()
        return True, dict(row) if row is not None else None
    finally:
        conn.close()


def list_users(db_file: str) -> list[dict]:
    conn = sqlite3.connect(db_file)
    try:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute(
            "SELECT id, telegram_id, role, mobile_phone, full_name FROM users ORDER BY role DESC, id ASC"
        )
        return [dict(row) for row in cursor.fetchall()]
    finally:
        conn.close()


def delete_user_by_phone(db_file: str, phone: str) -> bool:
    """Delete a non-admin user by phone. Returns True if a row was deleted."""
    conn = sqlite3.connect(db_file)
    try:
        cursor = conn.cursor()
        cursor.execute(
            "DELETE FROM users WHERE mobile_phone = ? AND role != 'admin'",
            (phone,),
        )
        conn.commit()
        return cursor.rowcount > 0
    finally:
        conn.close()


def register_user_from_contact(db_file: str, telegram_id: str, phone: str, full_name: str) -> None:
    """Insert or update a user row from a Telegram contact-share event.

    Tries telegram_id first, then phone (for users pre-registered by admin), then inserts fresh.
    """
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
            # User may have been pre-registered by admin via phone
            cursor.execute("SELECT id FROM users WHERE mobile_phone = ?", (phone,))
            row = cursor.fetchone()
            if row:
                conn.execute(
                    "UPDATE users SET telegram_id = ?, full_name = ? WHERE mobile_phone = ?",
                    (telegram_id, full_name, phone),
                )
            else:
                conn.execute(
                    "INSERT INTO users (telegram_id, mobile_phone, full_name, role) VALUES (?, ?, ?, 'user')",
                    (telegram_id, phone, full_name),
                )
        conn.commit()
    finally:
        conn.close()