import hashlib
import logging
import os
import re
import secrets
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("gulf-radar")

BASE_DIR = Path(__file__).resolve().parent

# ---------------------------------------------------------
# DATABASE CONFIGURATION (PostgreSQL via DATABASE_URL, SQLite fallback)
# ---------------------------------------------------------
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
USE_PG = bool(DATABASE_URL)

if USE_PG:
    import psycopg2
    import psycopg2.extras
    if DATABASE_URL.startswith("postgres://"):
        DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

DB_FILE = BASE_DIR / "flights.db"


def get_db():
    if USE_PG:
        return psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn


def format_query(sql: str) -> str:
    return sql.replace("?", "%s") if USE_PG else sql


# ---------------------------------------------------------
# SHARED SYSTEM STATE
# ---------------------------------------------------------
SYSTEM_METRICS = {
    "last_bot_run": datetime.now(),
    "bot_status": "Active",
    "last_payment_time": "No payments received yet",
    "last_webhook_status": "Normal",
    "user_sync_limits": {},
}


# ---------------------------------------------------------
# INITIALIZATION - ALL TABLES IN ONE PLACE
# ---------------------------------------------------------
def init_db():
    conn = get_db()
    cursor = conn.cursor()

    if USE_PG:
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id SERIAL PRIMARY KEY,
                email TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                telegram_chat_id TEXT,
                is_pro INTEGER DEFAULT 0,
                telegram_credits INTEGER DEFAULT 10,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS password_resets (
                id SERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                token TEXT UNIQUE NOT NULL,
                expires_at TIMESTAMP NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS watchlist (
                id SERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                route_code TEXT NOT NULL,
                destination_name TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(user_id, route_code)
            );
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS tracked_flights (
                id SERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                flight_num TEXT NOT NULL,
                airline TEXT NOT NULL,
                destination_name TEXT NOT NULL,
                scheduled_time TEXT,
                last_status TEXT NOT NULL DEFAULT 'Expected',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(user_id, flight_num)
            );
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS search_logs (
                id SERIAL PRIMARY KEY,
                identifier TEXT NOT NULL,
                search_date DATE NOT NULL,
                count INTEGER DEFAULT 1,
                UNIQUE(identifier, search_date)
            );
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                session_id TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS app_counters (
                key TEXT PRIMARY KEY,
                val INTEGER DEFAULT 0,
                last_reset DATE
            );
        """)
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
            );
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS api_logs (
                id SERIAL PRIMARY KEY,
                endpoint TEXT NOT NULL,
                status_code INTEGER NOT NULL,
                response_time_ms INTEGER NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS notification_logs (
                id SERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                channel TEXT NOT NULL,
                message TEXT,
                sent_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS error_logs (
                id SERIAL PRIMARY KEY,
                module TEXT NOT NULL,
                error_message TEXT NOT NULL,
                occurred_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS admin_access_logs (
                id SERIAL PRIMARY KEY,
                admin_email TEXT NOT NULL,
                ip_address TEXT NOT NULL,
                accessed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        try:
            cursor.execute("ALTER TABLE users ADD COLUMN is_admin INTEGER DEFAULT 0;")
        except Exception:
            pass
    else:
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                telegram_chat_id TEXT,
                is_pro INTEGER DEFAULT 0,
                telegram_credits INTEGER DEFAULT 10,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS password_resets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                token TEXT UNIQUE NOT NULL,
                expires_at DATETIME NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users (id)
            );
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS watchlist (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                route_code TEXT NOT NULL,
                destination_name TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users (id),
                UNIQUE(user_id, route_code)
            );
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS tracked_flights (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                flight_num TEXT NOT NULL,
                airline TEXT NOT NULL,
                destination_name TEXT NOT NULL,
                scheduled_time TEXT,
                last_status TEXT NOT NULL DEFAULT 'Expected',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users (id),
                UNIQUE(user_id, flight_num)
            );
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS search_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                identifier TEXT NOT NULL,
                search_date DATE NOT NULL,
                count INTEGER DEFAULT 1,
                UNIQUE(identifier, search_date)
            );
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                session_id TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users (id)
            );
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS app_counters (
                key TEXT PRIMARY KEY,
                val INTEGER DEFAULT 0,
                last_reset DATE
            );
        """)
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
            );
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS api_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                endpoint TEXT NOT NULL,
                status_code INTEGER NOT NULL,
                response_time_ms INTEGER NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS notification_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                channel TEXT NOT NULL,
                message TEXT,
                sent_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users (id)
            );
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS error_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                module TEXT NOT NULL,
                error_message TEXT NOT NULL,
                occurred_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS admin_access_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                admin_email TEXT NOT NULL,
                ip_address TEXT NOT NULL,
                accessed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
    try:
        cursor.execute("ALTER TABLE users ADD COLUMN telegram_credits INTEGER DEFAULT 10;")
    except Exception:
        pass

    try:
        cursor.execute("ALTER TABLE users ADD COLUMN is_admin INTEGER DEFAULT 0;")
    except Exception:
        pass

    conn.commit()
    conn.close()


# ---------------------------------------------------------
# COUNTER & THRESHOLD MANAGEMENT
# ---------------------------------------------------------
def get_counter(key: str) -> int:
    today_str = date.today().isoformat()
    current_month_str = datetime.now().strftime("%Y-%m")

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(format_query("SELECT val, last_reset FROM app_counters WHERE key = ?"), (key,))
    row = cursor.fetchone()

    if not row:
        cursor.execute(format_query("INSERT INTO app_counters (key, val, last_reset) VALUES (?, 0, ?)"), (key, today_str if "day" in key else current_month_str))
        conn.commit()
        conn.close()
        return 0

    last_reset = str(row["last_reset"])
    val = row["val"]

    if "day" in key and last_reset != today_str:
        cursor.execute(format_query("UPDATE app_counters SET val = 0, last_reset = ? WHERE key = ?"), (today_str, key))
        conn.commit()
        val = 0
    elif "month" in key and not last_reset.startswith(current_month_str):
        cursor.execute(format_query("UPDATE app_counters SET val = 0, last_reset = ? WHERE key = ?"), (current_month_str, key))
        conn.commit()
        cursor.execute(format_query("DELETE FROM app_counters WHERE key IN ('api_70_sent', 'api_90_sent')"))
        conn.commit()
        val = 0

    conn.close()
    return val


def increment_counter(key: str, amount: int = 1):
    today_str = date.today().isoformat()
    current_month_str = datetime.now().strftime("%Y-%m")

    get_counter(key)
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(format_query("UPDATE app_counters SET val = val + ? WHERE key = ?"), (amount, key))
    conn.commit()
    conn.close()

    if key == "api_requests_this_month":
        current_val = get_counter("api_requests_this_month")

        conn2 = get_db()
        cursor2 = conn2.cursor()

        if current_val >= 1120:
            cursor2.execute(format_query("SELECT val FROM app_counters WHERE key = ?"), ('api_70_sent',))
            sent_70 = cursor2.fetchone()
            if not sent_70 or sent_70["val"] == 0:
                from services import send_admin_email
                send_admin_email(
                    "API Quota Exceeded 70% Threshold!",
                    f"Monthly RapidAPI consumption exceeded 1,120 limit.\nCurrent Usage: {current_val} / 1,600"
                )
                if USE_PG:
                    cursor2.execute("""
                        INSERT INTO app_counters (key, val, last_reset) VALUES ('api_70_sent', 1, %s)
                        ON CONFLICT (key) DO UPDATE SET val = EXCLUDED.val, last_reset = EXCLUDED.last_reset
                    """, (current_month_str,))
                else:
                    cursor2.execute("""
                        INSERT OR REPLACE INTO app_counters (key, val, last_reset) VALUES ('api_70_sent', 1, ?)
                    """, (current_month_str,))
                conn2.commit()

        if current_val >= 1440:
            cursor2.execute(format_query("SELECT val FROM app_counters WHERE key = ?"), ('api_90_sent',))
            sent_90 = cursor2.fetchone()
            if not sent_90 or sent_90["val"] == 0:
                from services import send_admin_email
                send_admin_email(
                    "CRITICAL: API Quota Reached 90%!",
                    f"Monthly RapidAPI consumption reached 1,440 limit! Please take action.\nCurrent Usage: {current_val} / 1,600"
                )
                if USE_PG:
                    cursor2.execute("""
                        INSERT INTO app_counters (key, val, last_reset) VALUES ('api_90_sent', 1, %s)
                        ON CONFLICT (key) DO UPDATE SET val = EXCLUDED.val, last_reset = EXCLUDED.last_reset
                    """, (current_month_str,))
                else:
                    cursor2.execute("""
                        INSERT OR REPLACE INTO app_counters (key, val, last_reset) VALUES ('api_90_sent', 1, ?)
                    """, (current_month_str,))
                conn2.commit()
        conn2.close()


# ---------------------------------------------------------
# HASHING & SECURITY
# ---------------------------------------------------------
def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    key = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 100000)
    return f"{salt}${key.hex()}"


def verify_password(stored_hash: str, password: str) -> bool:
    try:
        salt, key_hex = stored_hash.split("$")
        key = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 100000)
        return key.hex() == key_hex
    except Exception:
        return False


# ---------------------------------------------------------
# NOTIFICATION ENGINE (lightweight helpers; full SMTP in services.py)
# ---------------------------------------------------------
def get_free_user_count() -> int:
    conn = get_db()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) as cnt FROM users WHERE is_pro = 0")
        row = cursor.fetchone()
        return row["cnt"] if row else 0
    finally:
        conn.close()


def is_valid_telegram_chat_id(chat_id: str) -> bool:
    return bool(re.fullmatch(r"-?\d{6,20}", str(chat_id).strip())) if chat_id else False


# ---------------------------------------------------------
# USER & QUOTA CONTROLS
# ---------------------------------------------------------
def get_current_user(request):
    session_id = request.cookies.get("session_id")
    if not session_id:
        return None
    conn = get_db()
    try:
        cursor = conn.cursor()
        query = format_query("""
            SELECT u.id, u.email, u.telegram_chat_id, u.is_pro, u.telegram_credits, u.created_at
            FROM users u
            JOIN sessions s ON u.id = s.user_id
            WHERE s.session_id = ?
        """)
        cursor.execute(query, (session_id,))
        user = cursor.fetchone()

        if not user:
            return None

        user_dict = dict(user)
        created_at = user_dict.get("created_at")

        trial_active = False
        if created_at:
            try:
                if isinstance(created_at, str):
                    reg_date = datetime.fromisoformat(created_at)
                else:
                    reg_date = created_at
                if datetime.now() - reg_date <= timedelta(days=7):
                    trial_active = True
            except Exception:
                pass

        ADMIN_EMAILS = [os.getenv("ADMIN_EMAIL", "").strip().lower()]
        if user_dict["email"].lower() in ADMIN_EMAILS or user_dict.get("is_pro") == 1 or trial_active:
            user_dict["is_pro"] = 1
            user_dict["is_trial"] = trial_active
        else:
            user_dict["is_pro"] = 0
            user_dict["is_trial"] = False

        user_dict["is_admin"] = user_dict["email"].lower() in ADMIN_EMAILS
        return user_dict
    finally:
        conn.close()


def get_daily_search_count(identifier: str) -> int:
    today = date.today().isoformat()
    conn = get_db()
    try:
        cursor = conn.cursor()
        query = format_query("SELECT count FROM search_logs WHERE identifier = ? AND search_date = ?")
        cursor.execute(query, (identifier, today))
        row = cursor.fetchone()
        return row["count"] if row else 0
    finally:
        conn.close()


def increment_search_count(identifier: str):
    today = date.today().isoformat()
    conn = get_db()
    try:
        cursor = conn.cursor()
        query = format_query("""
            INSERT INTO search_logs (identifier, search_date, count)
            VALUES (?, ?, 1)
            ON CONFLICT(identifier, search_date) DO UPDATE SET count = search_logs.count + 1
        """)
        cursor.execute(query, (identifier, today))
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------
# DATA RETENTION POLICY (30-day cleanup)
# ---------------------------------------------------------
def cleanup_old_logs():
    """Veritabanının şişmesini önlemek için 30 günden eski log kayıtlarını temizler."""
    conn = get_db()
    try:
        cursor = conn.cursor()
        cutoff_date = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")
        cursor.execute(format_query("DELETE FROM api_logs WHERE created_at < ?"), (cutoff_date,))
        cursor.execute(format_query("DELETE FROM notification_logs WHERE sent_at < ?"), (cutoff_date,))
        cursor.execute(format_query("DELETE FROM error_logs WHERE occurred_at < ?"), (cutoff_date,))
        cursor.execute(format_query("DELETE FROM admin_access_logs WHERE accessed_at < ?"), (cutoff_date,))
        conn.commit()
    except Exception as e:
        logger.error("Log cleanup error: %s", e)
    finally:
        conn.close()


# ---------------------------------------------------------
# ADMIN ACCESS LOG HELPER
# ---------------------------------------------------------
def log_admin_access(admin_email: str, ip_address: str):
    conn = get_db()
    try:
        cursor = conn.cursor()
        cursor.execute(format_query("INSERT INTO admin_access_logs (admin_email, ip_address) VALUES (?, ?)"), (admin_email, ip_address))
        conn.commit()
    except Exception as e:
        logger.error("Admin access log error: %s", e)
    finally:
        conn.close()


# ---------------------------------------------------------
# USER MANAGEMENT HELPERS (CRM)
# ---------------------------------------------------------
def get_all_users():
    conn = get_db()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT id, email, is_pro, telegram_chat_id, telegram_credits, created_at FROM users ORDER BY id DESC")
        rows = cursor.fetchall()
        return rows
    finally:
        conn.close()


def get_user_stats():
    conn = get_db()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) as cnt FROM users")
        total = cursor.fetchone()["cnt"]
        cursor.execute("SELECT COUNT(*) as cnt FROM users WHERE is_pro = 1")
        pro = cursor.fetchone()["cnt"]
        cursor.execute("SELECT COUNT(*) as cnt FROM users WHERE is_pro = 0 AND telegram_credits <= 0")
        exhausted = cursor.fetchone()["cnt"]
        return {"total": total, "pro": pro, "free": total - pro, "exhausted": exhausted}
    finally:
        conn.close()


def suspend_user(user_id: int):
    conn = get_db()
    try:
        cursor = conn.cursor()
        cursor.execute(format_query("UPDATE users SET is_pro = 0 WHERE id = ?"), (user_id,))
        conn.commit()
    finally:
        conn.close()


def delete_user(user_id: int):
    conn = get_db()
    try:
        cursor = conn.cursor()
        cursor.execute(format_query("DELETE FROM sessions WHERE user_id = ?"), (user_id,))
        cursor.execute(format_query("DELETE FROM watchlist WHERE user_id = ?"), (user_id,))
        cursor.execute(format_query("DELETE FROM tracked_flights WHERE user_id = ?"), (user_id,))
        cursor.execute(format_query("DELETE FROM password_resets WHERE user_id = ?"), (user_id,))
        cursor.execute(format_query("DELETE FROM users WHERE id = ?"), (user_id,))
        conn.commit()
    finally:
        conn.close()

