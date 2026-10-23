import os
import sqlite3
from datetime import datetime, date, timedelta

# PostgreSQL support check
DATABASE_URL = os.getenv("DATABASE_URL")
USE_PG = bool(DATABASE_URL)

if USE_PG:
    import psycopg2
    import psycopg2.extras

def get_db():
    if USE_PG:
        conn = psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)
        return conn
    else:
        conn = sqlite3.connect("radar.db")
        conn.row_factory = sqlite3.Row
        return conn

def format_query(query: str) -> str:
    """Converts SQLite '?' placeholders to PostgreSQL '%s' if necessary."""
    if USE_PG:
        return query.replace("?", "%s")
    return query

def init_db():
    conn = get_db()
    cursor = conn.cursor()
    
    # App counters table
    cursor.execute(format_query("""
        CREATE TABLE IF NOT EXISTS app_counters (
            key TEXT PRIMARY KEY,
            val INTEGER DEFAULT 0,
            last_reset TEXT
        )
    """))
    
    # Flights history table
    if USE_PG:
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS flights_history (
                id SERIAL PRIMARY KEY,
                flight_num TEXT,
                airline TEXT,
                dest_iata TEXT,
                dest_name TEXT,
                status TEXT,
                scheduled_time TEXT,
                terminal TEXT,
                gate TEXT,
                aircraft_model TEXT,
                recorded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
    else:
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS flights_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                flight_num TEXT,
                airline TEXT,
                dest_iata TEXT,
                dest_name TEXT,
                status TEXT,
                scheduled_time TEXT,
                terminal TEXT,
                gate TEXT,
                aircraft_model TEXT,
                recorded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

    # --- OBSERVABILITY & LOG TABLES ---
    
    # 1. API Request Logs
    if USE_PG:
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS api_logs (
                id SERIAL PRIMARY KEY,
                endpoint TEXT,
                status_code INT,
                response_time_ms REAL,
                error_message TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
    else:
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS api_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                endpoint TEXT,
                status_code INTEGER,
                response_time_ms REAL,
                error_message TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

    # 2. Notification Logs (Telegram & Push)
    if USE_PG:
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS notification_logs (
                id SERIAL PRIMARY KEY,
                recipient TEXT,
                channel TEXT,
                message TEXT,
                status TEXT,
                error_detail TEXT,
                sent_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
    else:
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS notification_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                recipient TEXT,
                channel TEXT,
                message TEXT,
                status TEXT,
                error_detail TEXT,
                sent_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

    # 3. System Error Logs
    if USE_PG:
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS error_logs (
                id SERIAL PRIMARY KEY,
                module TEXT,
                error_message TEXT,
                stack_trace TEXT,
                occurred_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
    else:
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS error_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                module TEXT,
                error_message TEXT,
                stack_trace TEXT,
                occurred_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

    # 4. Admin Access Logs (IP & Timestamp tracking)
    if USE_PG:
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS admin_access_logs (
                id SERIAL PRIMARY KEY,
                admin_email TEXT,
                ip_address TEXT,
                accessed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
    else:
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS admin_access_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                admin_email TEXT,
                ip_address TEXT,
                accessed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

    conn.commit()
    conn.close()

def cleanup_old_logs():
    """Data Retention Policy: Automatically purges logs older than 30 days to prevent database bloat."""
    try:
        conn = get_db()
        cursor = conn.cursor()
        cutoff_date = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")
        
        cursor.execute(format_query("DELETE FROM api_logs WHERE created_at < ?"), (cutoff_date,))
        cursor.execute(format_query("DELETE FROM notification_logs WHERE sent_at < ?"), (cutoff_date,))
        cursor.execute(format_query("DELETE FROM error_logs WHERE occurred_at < ?"), (cutoff_date,))
        cursor.execute(format_query("DELETE FROM admin_access_logs WHERE accessed_at < ?"), (cutoff_date,))
        
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"[Log Cleanup Error]: {e}")

def get_counter(key: str) -> int:
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(format_query("SELECT val FROM app_counters WHERE key = ?"), (key,))
    row = cursor.fetchone()
    if not row:
        cursor.execute(format_query("INSERT INTO app_counters (key, val) VALUES (?, 0)"), (key,))
        conn.commit()
        conn.close()
        return 0
    conn.close()
    return row["val"]