import asyncio
import csv
from datetime import date, datetime, timedelta
import hashlib
import io
import json
import os
from pathlib import Path
import re
import secrets
from typing import Optional
from collections import defaultdict
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

from dotenv import load_dotenv
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
import requests
import sqlite3

from reportlab.lib.pagesizes import letter
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, HRFlowable
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib import colors

# ---------------------------------------------------------
# CONFIGURATION & CONSTANTS
# ---------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

DB_FILE = BASE_DIR / "flights.db"
DATA_CACHE_FILE = BASE_DIR / "rapidapi_API.txt"

RAPIDAPI_KEY = os.getenv("RAPIDAPI_KEY", "").strip()
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
APP_BASE_URL = os.getenv("APP_BASE_URL", "https://gulfdisruptionradar.co.uk").rstrip("/")
DEBUG_MODE = os.getenv("DEBUG", "false").strip().lower() in {"1", "true", "yes", "on"}
ADMIN_EMAILS = [os.getenv("ADMIN_EMAIL", "ayhan.hakyemez@gmail.com").strip().lower()]

# SMTP Configuration
SMTP_SERVER = os.getenv("SMTP_SERVER", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "ayhan.hakyemez@gmail.com")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
SMTP_FROM = os.getenv("SMTP_FROM", "ayhan.hakyemez@gmail.com")

app = FastAPI(title="Gulf Disruption Radar")

static_dir = BASE_DIR / "static"
static_dir.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

# Global Monitoring / Health Variables
SYSTEM_METRICS = {
    "last_bot_run": datetime.now(),
    "bot_status": "Active",
    "last_payment_time": "No payments received yet",
    "last_webhook_status": "Normal",
    "user_sync_limits": {},
}

# ---------------------------------------------------------
# EMAIL NOTIFICATION & PASSWORD RESET ENGINE (SMTP)
# ---------------------------------------------------------
def send_admin_email(subject: str, body: str):
    if not SMTP_PASSWORD:
        print(f"[SMTP Warning] SMTP_PASSWORD not set, email skipped: {subject}")
        return
    try:
        msg = MIMEMultipart()
        msg['From'] = SMTP_FROM
        msg['To'] = ADMIN_EMAILS[0]
        msg['Subject'] = f"🚨 GULF RADAR: {subject}"
        msg.attach(MIMEText(body, 'plain', 'utf-8'))

        server = smtplib.SMTP(SMTP_SERVER, SMTP_PORT)
        server.starttls()
        server.login(SMTP_USER, SMTP_PASSWORD)
        server.sendmail(SMTP_FROM, ADMIN_EMAILS[0], msg.as_string())
        server.quit()
        print(f"[SMTP Success] Email sent: {subject}")
    except Exception as e:
        print(f"[SMTP Error] Email sending failed: {e}")

def send_reset_email(to_email: str, reset_url: str):
    subject = "Password Reset Request - Gulf Disruption Radar"
    html_content = f"""
    <div style="font-family: Arial, sans-serif; padding: 20px; color: #333;">
        <h2>Password Reset Request</h2>
        <p>We received a request to reset your password for your Gulf Disruption Radar account.</p>
        <p>Click the link below to set a new password (valid for 1 hour):</p>
        <p><a href="{reset_url}" style="background-color: #0f172a; color: white; padding: 10px 20px; text-decoration: none; border-radius: 5px; display: inline-block;">Reset Password</a></p>
        <p>If you did not request this, please ignore this email.</p>
    </div>
    """
    
    if not SMTP_PASSWORD:
        print("\n" + "="*70)
        print(f"[PASSWORD RESET LINK FOR {to_email}]:")
        print(reset_url)
        print("="*70 + "\n")
        return

    try:
        msg = MIMEMultipart('alternative')
        msg['From'] = SMTP_FROM
        msg['To'] = to_email
        msg['Subject'] = subject
        msg.attach(MIMEText(html_content, 'html', 'utf-8'))

        server = smtplib.SMTP(SMTP_SERVER, SMTP_PORT)
        server.starttls()
        server.login(SMTP_USER, SMTP_PASSWORD)
        server.sendmail(SMTP_FROM, to_email, msg.as_string())
        server.quit()
        print(f"[SMTP Success] Password reset email sent to {to_email}")
    except Exception as e:
        print(f"[SMTP Error] Failed to send password reset email: {e}")
        print("\n" + "="*70)
        print(f"[FALLBACK RESET LINK FOR {to_email}]:")
        print(reset_url)
        print("="*70 + "\n")

# ---------------------------------------------------------
# DATABASE MANAGEMENT & MIGRATION GUARD
# ---------------------------------------------------------
USE_PG = bool(DATABASE_URL)
if USE_PG:
    import psycopg2
    import psycopg2.extras
    if DATABASE_URL.startswith("postgres://"):
        DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

def get_db():
    if USE_PG:
        return psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn

def format_query(sql: str) -> str:
    return sql.replace("?", "%s") if USE_PG else sql

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

    try:
        cursor.execute("ALTER TABLE users ADD COLUMN telegram_credits INTEGER DEFAULT 10;")
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
        
        if current_val >= 1120:
            cursor.execute(format_query("SELECT val FROM app_counters WHERE key = 'api_70_sent'"))
            sent_70 = cursor.fetchone()
            if not sent_70 or sent_70["val"] == 0:
                send_admin_email(
                    "API Quota Exceeded 70% Threshold!",
                    f"Monthly RapidAPI consumption exceeded 1,120 limit.\nCurrent Usage: {current_val} / 1,600"
                )
                cursor.execute(format_query("INSERT OR REPLACE INTO app_counters (key, val, last_reset) VALUES ('api_70_sent', 1, ?)"), (current_month_str,))
                conn.commit()

        if current_val >= 1440:
            cursor.execute(format_query("SELECT val FROM app_counters WHERE key = 'api_90_sent'"))
            sent_90 = cursor.fetchone()
            if not sent_90 or sent_90["val"] == 0:
                send_admin_email(
                    "CRITICAL: API Quota Reached 90%!",
                    f"Monthly RapidAPI consumption reached 1,440 limit! Please take action.\nCurrent Usage: {current_val} / 1,600"
                )
                cursor.execute(format_query("INSERT OR REPLACE INTO app_counters (key, val, last_reset) VALUES ('api_90_sent', 1, ?)"), (current_month_str,))
                conn.commit()
    conn.close()

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
# NOTIFICATION ENGINE
# ---------------------------------------------------------
def _send_telegram_worker(chat_id: str, message: str):
    if not chat_id or not TELEGRAM_BOT_TOKEN:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": str(chat_id).strip(), "text": message, "parse_mode": "HTML"}
    try:
        requests.post(url, json=payload, timeout=8)
        increment_counter("telegram_sent_today", 1)
    except Exception as e:
        print(f"[Telegram Error]: {e}")

async def send_telegram(chat_id: str, message: str):
    await asyncio.to_thread(_send_telegram_worker, chat_id, message)

def get_free_user_count() -> int:
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) as cnt FROM users WHERE is_pro = 0")
    row = cursor.fetchone()
    conn.close()
    return row["cnt"] if row else 0

# ---------------------------------------------------------
# USER & QUOTA CONTROLS
# ---------------------------------------------------------
def is_valid_telegram_chat_id(chat_id: str) -> bool:
    return bool(re.fullmatch(r"-?\d{6,20}", str(chat_id).strip())) if chat_id else False

def get_current_user(request: Request):
    session_id = request.cookies.get("session_id")
    if not session_id:
        return None
    conn = get_db()
    cursor = conn.cursor()
    query = format_query("""
        SELECT u.id, u.email, u.telegram_chat_id, u.is_pro, u.telegram_credits, u.created_at
        FROM users u 
        JOIN sessions s ON u.id = s.user_id 
        WHERE s.session_id = ?
    """)
    cursor.execute(query, (session_id,))
    user = cursor.fetchone()
    conn.close()

    if not user:
        return None

    user_dict = dict(user)
    created_at = user_dict.get("created_at")
    
    # 3 Günlük (72 saat) Deneme Süresi Kontrolü
    trial_active = False
    if created_at:
        try:
            if isinstance(created_at, str):
                reg_date = datetime.fromisoformat(created_at)
            else:
                reg_date = created_at
            
            if datetime.now() - reg_date <= timedelta(days=3):
                trial_active = True
        except Exception:
            pass

    # Admin, Gerçek Pro veya 3 Günlük Trial Süresindeyse PRO yetkileri verilir
    if user_dict["email"].lower() in ADMIN_EMAILS or user_dict.get("is_pro") == 1 or trial_active:
        user_dict["is_pro"] = 1
        user_dict["is_trial"] = trial_active
    else:
        user_dict["is_pro"] = 0
        user_dict["is_trial"] = False

    user_dict["is_admin"] = user_dict["email"].lower() in ADMIN_EMAILS
    return user_dict

def get_daily_search_count(identifier: str) -> int:
    today = date.today().isoformat()
    conn = get_db()
    cursor = conn.cursor()
    query = format_query("SELECT count FROM search_logs WHERE identifier = ? AND search_date = ?")
    cursor.execute(query, (identifier, today))
    row = cursor.fetchone()
    conn.close()
    return row["count"] if row else 0

def increment_search_count(identifier: str):
    today = date.today().isoformat()
    conn = get_db()
    cursor = conn.cursor()
    query = format_query("""
        INSERT INTO search_logs (identifier, search_date, count) 
        VALUES (?, ?, 1)
        ON CONFLICT(identifier, search_date) DO UPDATE SET count = search_logs.count + 1
    """)
    cursor.execute(query, (identifier, today))
    conn.commit()
    conn.close()

# ---------------------------------------------------------
# LIVE AIRPORT & FIDS ENGINE
# ---------------------------------------------------------
def fetch_live_airport_data(iata_code="DXB"):
    if not RAPIDAPI_KEY:
        return False, "RapidAPI Key is not defined."
    
    url = f"https://aerodatabox.p.rapidapi.com/flights/airports/iata/{iata_code}"
    headers = {"X-RapidAPI-Key": RAPIDAPI_KEY, "X-RapidAPI-Host": "aerodatabox.p.rapidapi.com"}
    params = {
        "offsetMinutes": "-120",
        "durationMinutes": "720",
        "withLeg": "true",
        "direction": "Both",
        "withCancelled": "true",
        "withCodeshared": "true",
        "withCargo": "true"
    }
    try:
        res = requests.get(url, headers=headers, params=params, timeout=15)
        increment_counter("api_requests_this_month", 1)
        if res.status_code == 200:
            with open(DATA_CACHE_FILE, "w", encoding="utf-8") as f:
                f.write(res.text)
            return True, f"Live {iata_code} FIDS data synchronized."
        return False, f"API Error ({res.status_code})"
    except Exception as e:
        return False, str(e)

def parse_radar_data(search_query=""):
    try:
        with open(DATA_CACHE_FILE, "r", encoding="utf-8") as f:
            text = f.read()
        try:
            data = json.loads(text)
        except Exception:
            last_bracket = text.rstrip().rstrip(",").rfind("}")
            data = json.loads(text[:last_bracket+1] + "]}")
    except Exception:
        return {"total": 0, "canceled": 0, "delayed": 0, "critical_routes": [], "all_flights": [], "top_8_flights": []}

    departures = data.get("departures", [])
    total_count = len(departures)
    canceled_count = 0
    delayed_count = 0
    route_stats = defaultdict(lambda: {"canceled": 0, "delayed": 0, "total": 0, "name": ""})
    all_flights = []

    for f in departures:
        status = f.get("status", "Expected")
        airline = f.get("airline", {}).get("name", "Unknown Airline")
        flight_num = f.get("number", "N/A")
        call_sign = f.get("callSign", "-")
        is_cargo = f.get("isCargo", False)

        arr_info = f.get("arrival", {})
        dest_name = arr_info.get("airport", {}).get("name", "Unknown")
        dest_iata = arr_info.get("airport", {}).get("iata", "N/A")
        dest_icao = arr_info.get("airport", {}).get("icao", "N/A")
        dest_country = arr_info.get("airport", {}).get("countryCode", "-").upper()
        dest_tz = arr_info.get("airport", {}).get("timeZone", "-")
        loc = arr_info.get("airport", {}).get("location", {})
        coords = f"{loc.get('lat', '-')}, {loc.get('lon', '-')}" if loc else "-"

        dep_info = f.get("departure", {})
        sched_time_raw = dep_info.get("scheduledTime", {}).get("local", "-")
        sched_time_utc = dep_info.get("scheduledTime", {}).get("utc", "-")
        revised_time = dep_info.get("revisedTime", {}).get("local", "-")
        runway_time = dep_info.get("runwayTime", {}).get("local", "-")
        terminal = dep_info.get("terminal", "-")
        gate = dep_info.get("gate", "-")
        runway = dep_info.get("runway", "-")

        ac = f.get("aircraft", {})
        ac_model = ac.get("model", "Unspecified")
        ac_reg = ac.get("reg", "-")
        ac_modes = ac.get("modeS", "-")

        sched_display = sched_time_raw.split("T")[-1][:5] if "T" in sched_time_raw else (sched_time_raw[-11:-6] if len(sched_time_raw) >= 11 else sched_time_raw)

        if status == "Canceled":
            canceled_count += 1
            route_stats[dest_iata]["canceled"] += 1
        elif status == "Delayed":
            delayed_count += 1
            route_stats[dest_iata]["delayed"] += 1

        route_stats[dest_iata]["total"] += 1
        route_stats[dest_iata]["name"] = dest_name

        all_flights.append({
            "flight_num": flight_num,
            "call_sign": call_sign,
            "airline": airline,
            "dest_name": dest_name,
            "dest_iata": dest_iata,
            "dest_icao": dest_icao,
            "dest_country": dest_country,
            "dest_tz": dest_tz,
            "coords": coords,
            "time": sched_display,
            "sched_local": sched_time_raw,
            "sched_utc": sched_time_utc,
            "revised_time": revised_time,
            "runway_time": runway_time,
            "terminal": terminal,
            "gate": gate,
            "runway": runway,
            "status": status,
            "aircraft_model": ac_model,
            "aircraft_reg": ac_reg,
            "aircraft_modes": ac_modes,
            "is_cargo": is_cargo
        })

    if search_query:
        q = search_query.lower()
        all_flights = [fl for fl in all_flights if q in fl["dest_name"].lower() or q in fl["dest_iata"].lower() or q in fl["airline"].lower() or q in fl["flight_num"].lower()]

    all_flights = sorted(all_flights, key=lambda x: x.get("time", ""))

    critical_routes = []
    for iata, r in route_stats.items():
        if r["canceled"] > 0 or r["delayed"] > 0:
            critical_routes.append({
                "iata": iata,
                "name": r["name"],
                "canceled": r["canceled"],
                "delayed": r["delayed"],
                "total": r["total"],
                "risk_level": "CRITICAL" if r["canceled"] >= 3 else ("WARNING" if r["canceled"] > 0 else "MODERATE")
            })

    critical_routes = sorted(critical_routes, key=lambda x: x["canceled"], reverse=True)

    sliced_flights = all_flights[:35]
    top_8_flights = sliced_flights[:8]

    return {
        "total": total_count,
        "canceled": canceled_count,
        "delayed": delayed_count,
        "critical_routes": critical_routes,
        "all_flights": sliced_flights,
        "top_8_flights": top_8_flights
    }

# ---------------------------------------------------------
# BACKGROUND MONITOR (WITH CORRIDOR RISK MATCHING)
# ---------------------------------------------------------
async def automated_radar_monitor():
    while True:
        try:
            fetch_live_airport_data("DXB")
            SYSTEM_METRICS["last_bot_run"] = datetime.now()
            SYSTEM_METRICS["bot_status"] = "Active"

            radar = parse_radar_data()
            current_flight_map = {fl["flight_num"]: fl for fl in radar["all_flights"]}
            
            critical_iata_set = {r["iata"] for r in radar["critical_routes"] if r["risk_level"] == "CRITICAL"}

            conn = get_db()
            cursor = conn.cursor()

            cursor.execute("""
                SELECT tf.id, tf.user_id, tf.flight_num, tf.airline, tf.destination_name, tf.last_status, 
                       u.email, u.telegram_chat_id, u.is_pro, u.telegram_credits, u.created_at
                FROM tracked_flights tf
                JOIN users u ON tf.user_id = u.id
            """)
            tracked_flights_list = cursor.fetchall()

            for tf in tracked_flights_list:
                is_pro = tf["is_pro"]
                credits_left = tf["telegram_credits"]
                created_at = tf["created_at"]

                if not is_pro:
                    if credits_left <= 0:
                        continue
                    try:
                        if isinstance(created_at, str):
                            reg_date = datetime.fromisoformat(created_at)
                        else:
                            reg_date = created_at
                        if datetime.now() - reg_date > timedelta(days=14):
                            continue
                    except Exception:
                        pass

                f_num = tf["flight_num"]
                old_status = tf["last_status"]

                if f_num in current_flight_map:
                    fl_obj = current_flight_map[f_num]
                    new_status = fl_obj["status"]
                    dest_iata = fl_obj["dest_iata"]

                    if new_status != old_status:
                        cursor.execute(format_query("UPDATE tracked_flights SET last_status = ? WHERE id = ?"), (new_status, tf["id"]))
                        conn.commit()

                        if new_status in ["Canceled", "Delayed"]:
                            if not is_pro:
                                new_credits = max(0, credits_left - 1)
                                cursor.execute(format_query("UPDATE users SET telegram_credits = ? WHERE id = ?"), (new_credits, tf["user_id"]))
                                conn.commit()

                            if tf["telegram_chat_id"]:
                                alert_msg = (
                                    "🚨 <b>GULF DISRUPTION RADAR | FLIGHT ALERT</b>\n\n"
                                    f"✈️ <b>Flight:</b> <code>{f_num}</code> ({tf['airline']})\n"
                                    f"📍 <b>Route:</b> DXB ➔ {tf['destination_name']}\n"
                                    f"⚠️ <b>Status Changed:</b> <b>{new_status.upper()}</b> (Prev: {old_status})\n"
                                    f"🕒 <b>Scheduled:</b> {fl_obj['time']} GST\n"
                                    f"🚪 <b>Terminal/Gate:</b> T{fl_obj['terminal']} / G-{fl_obj['gate']}"
                                )
                                await send_telegram(tf["telegram_chat_id"], alert_msg)

                    elif dest_iata in critical_iata_set and new_status == "Expected":
                        if tf["telegram_chat_id"]:
                            pass

            conn.close()
        except Exception as e:
            SYSTEM_METRICS["bot_status"] = f"Error: {str(e)}"
            print(f"[Monitor Exception]: {e}")
        await asyncio.sleep(1800)

@app.on_event("startup")
async def startup_event():
    init_db()
    asyncio.create_task(automated_radar_monitor())

# ---------------------------------------------------------
# DODO PAYMENTS WEBHOOK
# ---------------------------------------------------------
@app.post("/api/dodo-webhook")
async def dodo_webhook(request: Request):
    try:
        payload = await request.json()
        SYSTEM_METRICS["last_webhook_status"] = "Success (200 OK)"
    except Exception as e:
        SYSTEM_METRICS["last_webhook_status"] = f"Error: {str(e)}"
        raise HTTPException(status_code=400, detail="Invalid payload")

    event_type = payload.get("type") or payload.get("event")
    event_data = payload.get("data", {})
    customer_email = ""

    if isinstance(event_data.get("customer"), dict):
        customer_email = event_data["customer"].get("email") or ""
    elif event_data.get("billing_email"):
        customer_email = event_data.get("billing_email")
    elif event_data.get("customer_email"):
        customer_email = event_data.get("customer_email")

    customer_email = customer_email.strip().lower()
    if not customer_email:
        return {"status": "no_email_ignored"}

    admin_chat = os.getenv("MY_TELEGRAM_ID", "").strip()
    conn = get_db()
    cursor = conn.cursor()

    if event_type in ["payment.succeeded", "subscription.active", "checkout.session.completed"]:
        SYSTEM_METRICS["last_payment_time"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        cursor.execute(format_query("UPDATE users SET is_pro = 1 WHERE lower(email) = ?"), (customer_email,))
        conn.commit()

        send_admin_email(
            "New Pro Sale (+$29.90)",
            f"Successful Pro subscription sale completed!\nCustomer Email: {customer_email}"
        )

        cursor.execute(format_query("SELECT telegram_chat_id FROM users WHERE lower(email) = ?"), (customer_email,))
        user_row = cursor.fetchone()

        if user_row and user_row["telegram_chat_id"]:
            pro_msg = (
                "🎉 <b>Gulf Disruption Radar | PRO Activated!</b>\n\n"
                "Your account is upgraded to <b>PRO</b>.\n"
                "• Up to 100 corridor queries / day\n"
                "• Up to 50 tracked routes & individual flights\n"
                "• Instant state-change push alerts & PDF reports"
            )
            await send_telegram(user_row["telegram_chat_id"], pro_msg)

        if admin_chat:
            sale_msg = f"💰 <b>NEW GULF RADAR SALE!</b>\n\nCustomer: <code>{customer_email}</code>"
            await send_telegram(admin_chat, sale_msg)

    elif event_type in ["subscription.cancelled", "subscription.expired"]:
        cursor.execute(format_query("UPDATE users SET is_pro = 0 WHERE lower(email) = ?"), (customer_email,))
        conn.commit()

    conn.close()
    return {"status": "success"}

# ---------------------------------------------------------
# 🛡️ ADMIN PANEL ROUTE (/admin)
# ---------------------------------------------------------
@app.get("/admin", response_class=HTMLResponse)
async def admin_panel(request: Request):
    user = get_current_user(request)
    if not user or not user.get("is_admin"):
        raise HTTPException(status_code=404, detail="Not Found")

    conn = get_db()
    cursor = conn.cursor()

    cursor.execute("SELECT COUNT(*) as cnt FROM users")
    total_users = cursor.fetchone()["cnt"]

    cursor.execute("SELECT COUNT(*) as cnt FROM users WHERE is_pro = 1")
    pro_users = cursor.fetchone()["cnt"]

    free_users = total_users - pro_users
    free_slots_left = max(0, 10 - free_users)
    mrr = pro_users * 29.90
    conversion_rate = round((pro_users / total_users * 100), 1) if total_users > 0 else 0.0

    cursor.execute("SELECT COUNT(*) as cnt FROM watchlist")
    total_watchlist = cursor.fetchone()["cnt"]

    cursor.execute("SELECT COUNT(*) as cnt FROM tracked_flights")
    total_tracked_flights = cursor.fetchone()["cnt"]

    cursor.execute("SELECT email, telegram_credits, created_at FROM users WHERE is_pro = 0 AND telegram_credits <= 0")
    exhausted_credits_users = cursor.fetchall()

    cursor.execute("SELECT COUNT(DISTINCT session_id) as cnt FROM sessions")
    active_sessions = cursor.fetchone()["cnt"]

    cursor.execute("SELECT email, created_at FROM users ORDER BY id DESC LIMIT 1")
    last_user_row = cursor.fetchone()
    last_registered_email = last_user_row["email"] if last_user_row else "None yet"

    conn.close()

    api_limit = 1600
    api_used = get_counter("api_requests_this_month")
    api_pct = round((api_used / api_limit * 100), 1) if api_limit > 0 else 0

    api_color = "emerald"
    if api_pct >= 90:
        api_color = "rose"
    elif api_pct >= 70:
        api_color = "amber"

    cache_time_str = "Unknown"
    if DATA_CACHE_FILE.exists():
        m_time = datetime.fromtimestamp(DATA_CACHE_FILE.stat().st_mtime)
        diff_mins = int((datetime.now() - m_time).total_seconds() / 60)
        cache_time_str = f"{diff_mins} minutes ago"

    last_run = SYSTEM_METRICS.get("last_bot_run")
    if isinstance(last_run, datetime):
        mins_since_run = int((datetime.now() - last_run).total_seconds() / 60)
    else:
        mins_since_run = 0

    if mins_since_run <= 30:
        uptime_html = '<span class="font-bold text-emerald-400">🟢 Active / Keep-Alive OK</span>'
    else:
        uptime_html = f'<span class="font-bold text-rose-500">🔴 WARNING: Bot has not run for {mins_since_run} minutes!</span>'

    html_content = f"""
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <title>Gulf Disruption Radar | Admin Control Center</title>
        <script src="https://cdn.tailwindcss.com"></script>
        <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet">
        <style>body {{ font-family: 'Inter', sans-serif; }}</style>
    </head>
    <body class="bg-slate-950 text-slate-100 min-h-screen p-6 antialiased">
        <div class="max-w-6xl mx-auto space-y-8">
            <header class="flex items-center justify-between border-b border-slate-800 pb-4">
                <div>
                    <h1 class="text-xl font-extrabold text-white">🎛️ GULF DISRUPTION RADAR | ADMIN CONTROL CENTER</h1>
                    <p class="text-xs text-slate-400 mt-0.5">Authorized Founder Access Only • Secured by ADMIN_EMAILS</p>
                </div>
                <a href="/" class="px-4 py-2 bg-slate-800 hover:bg-slate-700 text-xs font-semibold rounded-xl transition">← Back to Radar App</a>
            </header>

            <!-- 1. API & QUOTA MANAGEMENT -->
            <section class="bg-slate-900 border border-slate-800 rounded-2xl p-6 space-y-4">
                <h2 class="text-xs font-bold uppercase tracking-wider text-sky-400">📊 1. API Cost & Quota Management</h2>
                <div class="grid grid-cols-1 md:grid-cols-4 gap-4">
                    <div class="bg-slate-950 p-4 rounded-xl border border-slate-800">
                        <p class="text-[11px] text-slate-400 font-medium">Monthly API Requests</p>
                        <p class="text-2xl font-extrabold text-white mt-1">{api_used} <span class="text-xs font-normal text-slate-400">/ {api_limit}</span></p>
                        <div class="mt-3 w-full bg-slate-800 rounded-full h-2">
                            <div class="bg-{api_color}-500 h-2 rounded-full" style="width: {min(api_pct, 100)}%;"></div>
                        </div>
                        <p class="text-[10px] text-{api_color}-400 font-semibold mt-1">Utilization: %{api_pct} ({'Normal' if api_pct < 70 else ('Amber Warning' if api_pct < 90 else 'CRITICAL ALERT!')})</p>
                    </div>
                    <div class="bg-slate-950 p-4 rounded-xl border border-slate-800">
                        <p class="text-[11px] text-slate-400 font-medium">Telegram Sent Today</p>
                        <p class="text-2xl font-extrabold text-emerald-400 mt-1">{get_counter("telegram_sent_today")}</p>
                        <p class="text-[10px] text-slate-400 mt-2">1 unit = 1 push message</p>
                    </div>
                    <div class="bg-slate-950 p-4 rounded-xl border border-slate-800">
                        <p class="text-[11px] text-slate-400 font-medium">Background Bot Status</p>
                        <div class="flex items-center gap-2 mt-2">
                            <span class="w-3 h-3 rounded-full bg-emerald-500 animate-pulse"></span>
                            <span class="text-xs font-bold text-white">{SYSTEM_METRICS['bot_status']}</span>
                        </div>
                        <p class="text-[10px] text-slate-400 mt-1">Last Run: {SYSTEM_METRICS['last_bot_run'].strftime('%H:%M:%S')}</p>
                    </div>
                    <div class="bg-slate-950 p-4 rounded-xl border border-slate-800">
                        <p class="text-[11px] text-slate-400 font-medium">Cache Update</p>
                        <p class="text-lg font-bold text-white mt-1">{cache_time_str}</p>
                        <p class="text-[10px] text-slate-400 mt-1">AeroDataBox FIDS data</p>
                    </div>
                </div>
            </section>

            <!-- 2. FINANCE & USER METRICS -->
            <section class="bg-slate-900 border border-slate-800 rounded-2xl p-6 space-y-4">
                <h2 class="text-xs font-bold uppercase tracking-wider text-emerald-400">💵 2. Finance & User Metrics</h2>
                <div class="grid grid-cols-1 sm:grid-cols-2 md:grid-cols-5 gap-4">
                    <div class="bg-slate-950 p-4 rounded-xl border border-slate-800">
                        <p class="text-[11px] text-slate-400">Total Users</p>
                        <p class="text-2xl font-bold text-white mt-1">{total_users} <span class="text-xs font-normal text-slate-400">({free_users} Free / {pro_users} Pro)</span></p>
                    </div>
                    <div class="bg-slate-950 p-4 rounded-xl border border-slate-800">
                        <p class="text-[11px] text-slate-400">Founding Member Quota</p>
                        <p class="text-2xl font-bold text-sky-400 mt-1">{free_slots_left} <span class="text-xs font-normal text-slate-400">/ 10 left</span></p>
                    </div>
                    <div class="bg-slate-950 p-4 rounded-xl border border-slate-800">
                        <p class="text-[11px] text-slate-400">Estimated Revenue (MRR)</p>
                        <p class="text-2xl font-bold text-emerald-400 mt-1">${mrr:.2f}</p>
                    </div>
                    <div class="bg-slate-950 p-4 rounded-xl border border-slate-800">
                        <p class="text-[11px] text-slate-400">Conversion Rate</p>
                        <p class="text-2xl font-bold text-purple-400 mt-1">%{conversion_rate}</p>
                    </div>
                    <div class="bg-slate-950 p-4 rounded-xl border border-slate-800">
                        <p class="text-[11px] text-slate-400">Active Sessions</p>
                        <p class="text-2xl font-bold text-amber-400 mt-1">{active_sessions}</p>
                    </div>
                </div>
            </section>

            <!-- 3. OPERATIONAL LOAD & HEALTH -->
            <div class="grid grid-cols-1 md:grid-cols-2 gap-6">
                <section class="bg-slate-900 border border-slate-800 rounded-2xl p-6 space-y-4">
                    <h2 class="text-xs font-bold uppercase tracking-wider text-amber-400">📈 3. Operational Load & Infrastructure</h2>
                    <ul class="space-y-3 text-xs text-slate-300">
                        <li class="flex justify-between bg-slate-950 p-3 rounded-xl border border-slate-800">
                            <span>Total Watchlist Routes:</span>
                            <span class="font-bold text-white">{total_watchlist} Routes</span>
                        </li>
                        <li class="flex justify-between bg-slate-950 p-3 rounded-xl border border-slate-800">
                            <span>Total Tracked Individual Flights:</span>
                            <span class="font-bold text-white">{total_tracked_flights} Flights</span>
                        </li>
                        <li class="flex justify-between bg-slate-950 p-3 rounded-xl border border-slate-800">
                            <span>Render Server Status (Dynamic Uptime):</span>
                            <span>{uptime_html}</span>
                        </li>
                        <li class="flex justify-between bg-slate-950 p-3 rounded-xl border border-slate-800">
                            <span>Dodo Payments Last Payment / Webhook:</span>
                            <span class="font-bold text-white">{SYSTEM_METRICS['last_payment_time']} ({SYSTEM_METRICS['last_webhook_status']})</span>
                        </li>
                        <li class="flex justify-between bg-slate-950 p-3 rounded-xl border border-slate-800">
                            <span>Last Registered User:</span>
                            <span class="font-bold font-mono text-sky-400">{last_registered_email}</span>
                        </li>
                    </ul>
                </section>

                <!-- 4. SALES OPPORTUNITY: USERS WITH EXHAUSTED CREDITS -->
                <section class="bg-slate-900 border border-slate-800 rounded-2xl p-6 space-y-4">
                    <h2 class="text-xs font-bold uppercase tracking-wider text-rose-400">🎯 4. Sales Opportunity: Exhausted Credits</h2>
                    <p class="text-xs text-slate-400">The following free users have exhausted their notification credits. You can email them to pitch an upgrade to Pro:</p>
                    
                    <div class="bg-slate-950 rounded-xl border border-slate-800 overflow-hidden max-h-52 overflow-y-auto">
                        <table class="w-full text-left text-xs">
                            <thead class="bg-slate-900 text-slate-400 border-b border-slate-800">
                                <tr>
                                    <th class="p-3">User Email</th>
                                    <th class="p-3">Credits</th>
                                    <th class="p-3 text-right">Action</th>
                                </tr>
                            </thead>
                            <tbody class="divide-y divide-slate-800">
    """

    if not exhausted_credits_users:
        html_content += """
                                <tr>
                                    <td colspan="3" class="p-4 text-center text-slate-500 italic">No users with exhausted credits yet.</td>
                                </tr>
        """
    else:
        for cu in exhausted_credits_users:
            html_content += f"""
                                <tr>
                                    <td class="p-3 font-mono text-slate-200">{cu['email']}</td>
                                    <td class="p-3 text-rose-400 font-bold">{cu['telegram_credits']}</td>
                                    <td class="p-3 text-right">
                                        <a href="mailto:{cu['email']}?subject=Gulf%20Disruption%20Radar%20-%20Upgrade%20to%20Pro" class="px-2.5 py-1 bg-sky-600 hover:bg-sky-700 text-white rounded-lg font-semibold">Email Send ✉️</a>
                                    </td>
                                </tr>
            """

    html_content += f"""
                            </tbody>
                        </table>
                    </div>
                </section>
            </div>
        </div>
    </body>
    </html>
    """
    return HTMLResponse(content=html_content)

# ---------------------------------------------------------
# PDF REPORT EXPORT (PRO) - BLOOMBERG TERMINAL STYLE (ENGLISH)
# ---------------------------------------------------------
@app.get("/export-pdf")
async def export_pdf(request: Request):
    user = get_current_user(request)
    if not user:
        return RedirectResponse(url="/auth?mode=login", status_code=303)
    
    if not user.get("is_pro"):
        return RedirectResponse(url="/?limit_reached=1", status_code=303)

    radar = parse_radar_data()
    
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=letter,
        rightMargin=36, leftMargin=36,
        topMargin=36, bottomMargin=36
    )
    
    elements = []
    styles = getSampleStyleSheet()
    
    primary_color = colors.HexColor("#0f172a") # Slate 950
    accent_color = colors.HexColor("#0284c7")  # Sky 600
    text_color = colors.HexColor("#334155")    # Slate 700
    
    title_style = ParagraphStyle(
        'DocTitle',
        parent=styles['Heading1'],
        fontName='Helvetica-Bold',
        fontSize=14,
        textColor=primary_color,
        spaceAfter=4
    )
    
    meta_style = ParagraphStyle(
        'DocMeta',
        parent=styles['Normal'],
        fontName='Helvetica',
        fontSize=8,
        textColor=colors.HexColor("#64748b"),
        alignment=2
    )
    
    section_heading = ParagraphStyle(
        'SectionHeading',
        parent=styles['Heading2'],
        fontName='Helvetica-Bold',
        fontSize=11,
        textColor=primary_color,
        spaceBefore=12,
        spaceAfter=6
    )
    
    body_style = ParagraphStyle(
        'BodyDark',
        parent=styles['Normal'],
        fontName='Helvetica',
        fontSize=9,
        textColor=text_color,
        spaceAfter=6
    )

    table_header_style = ParagraphStyle(
        'TableHeader',
        parent=styles['Normal'],
        fontName='Helvetica-Bold',
        fontSize=8,
        textColor=colors.white
    )

    table_cell_style = ParagraphStyle(
        'TableCell',
        parent=styles['Normal'],
        fontName='Helvetica',
        fontSize=8,
        textColor=text_color
    )

    # 1. HEADER
    current_time_str = datetime.now().strftime("Operational Risk Report - %B %d, %Y | %H:%M GST")
    header_data = [
        [
            Paragraph("<b>GULF DISRUPTION RADAR</b>", title_style),
            Paragraph(current_time_str, meta_style)
        ]
    ]
    header_table = Table(header_data, colWidths=[250, 290])
    header_table.setStyle(TableStyle([
        ('VALIGN', (0,0), (-1,-1), 'MIDDLE'),
        ('BOTTOMPADDING', (0,0), (-1,-1), 0),
    ]))
    
    elements.append(header_table)
    elements.append(HRFlowable(width="100%", thickness=1, color=accent_color, spaceBefore=6, spaceAfter=12))

    # 2. EXECUTIVE SUMMARY
    elements.append(Paragraph("Executive Summary", section_heading))
    summary_text = f"In the last 24 hours across monitored corridors, a total of <b>{radar['canceled']}</b> cancellations and <b>{radar['delayed']}</b> serious delays have been detected. Operational risk distribution is summarized below."
    elements.append(Paragraph(summary_text, body_style))
    elements.append(Spacer(1, 6))

    # 3. CRITICAL RISK TABLE
    elements.append(Paragraph("Critical Corridor Disruption Table", section_heading))
    crit_table_data = [[
        Paragraph("Route / IATA", table_header_style),
        Paragraph("Destination Name", table_header_style),
        Paragraph("Canceled", table_header_style),
        Paragraph("Delayed", table_header_style),
        Paragraph("Risk Level", table_header_style)
    ]]
    
    if radar["critical_routes"]:
        for r in radar["critical_routes"][:8]:
            crit_table_data.append([
                Paragraph(f"<b>DXB ➔ {r['iata']}</b>", table_cell_style),
                Paragraph(r['name'], table_cell_style),
                Paragraph(str(r['canceled']), table_cell_style),
                Paragraph(str(r['delayed']), table_cell_style),
                Paragraph(f"<font color='{'#e11d48' if r['risk_level']=='CRITICAL' else '#d97706'}'><b>{r['risk_level']}</b></font>", table_cell_style)
            ])
    else:
        crit_table_data.append([Paragraph("No critical routes found.", table_cell_style), "", "", "", ""])

    c_table = Table(crit_table_data, colWidths=[80, 200, 60, 60, 140])
    c_table.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (-1,0), primary_color),
        ('VALIGN', (0,0), (-1,-1), 'MIDDLE'),
        ('BOTTOMPADDING', (0,0), (-1,-1), 5),
        ('TOPPADDING', (0,0), (-1,-1), 5),
        ('GRID', (0,0), (-1,-1), 0.5, colors.HexColor("#cbd5e1")),
    ]))
    elements.append(c_table)
    elements.append(Spacer(1, 10))

    # 4. AFFECTED FLIGHTS LIST
    elements.append(Paragraph("Affected / Critical Flights List", section_heading))
    flight_table_data = [[
        Paragraph("Flight No", table_header_style),
        Paragraph("Airline", table_header_style),
        Paragraph("Destination", table_header_style),
        Paragraph("Scheduled Time", table_header_style),
        Paragraph("Status", table_header_style)
    ]]
    
    disrupted_flights = [f for f in radar["all_flights"] if f["status"] in ["Canceled", "Delayed"]][:12]
    if disrupted_flights:
        for f in disrupted_flights:
            status_color = "#e11d48" if f['status'] == "Canceled" else "#d97706"
            flight_table_data.append([
                Paragraph(f"<b>{f['flight_num']}</b>", table_cell_style),
                Paragraph(f['airline'], table_cell_style),
                Paragraph(f"{f['dest_name']} ({f['dest_iata']})", table_cell_style),
                Paragraph(f['time'], table_cell_style),
                Paragraph(f"<font color='{status_color}'><b>{f['status']}</b></font>", table_cell_style)
            ])
    else:
        flight_table_data.append([Paragraph("No active disruption or delay records found.", table_cell_style), "", "", "", ""])

    f_table = Table(flight_table_data, colWidths=[70, 130, 160, 70, 110])
    f_table.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (-1,0), primary_color),
        ('VALIGN', (0,0), (-1,-1), 'MIDDLE'),
        ('BOTTOMPADDING', (0,0), (-1,-1), 4),
        ('TOPPADDING', (0,0), (-1,-1), 4),
        ('GRID', (0,0), (-1,-1), 0.5, colors.HexColor("#cbd5e1")),
    ]))
    elements.append(f_table)

    # 5. FOOTER
    def add_footer(canvas, doc):
        canvas.saveState()
        canvas.setFont('Helvetica', 8)
        canvas.setFillColor(colors.HexColor("#64748b"))
        footer_text = "Generated by Gulf Disruption Radar. Powered by AeroDataBox. Confidential."
        canvas.drawCentredString(letter[0] / 2.0, 20, footer_text)
        canvas.restoreState()

    doc.build(elements, onFirstPage=add_footer, onLaterPages=add_footer)
    buffer.seek(0)
    filename = f"Gulf_Disruption_Risk_Report_{date.today().isoformat()}.pdf"
    return StreamingResponse(
        iter([buffer.getvalue()]),
        media_type="application/pdf",
        headers={"Content-Disposition": f"attachment; filename={filename}"}
    )

# ---------------------------------------------------------
# PWA ENDPOINTS
# ---------------------------------------------------------
@app.get("/manifest.json")
async def get_manifest():
    manifest_path = static_dir / "manifest.json"
    if manifest_path.exists():
        return FileResponse(manifest_path, media_type="application/manifest+json")
    return {"name": "Gulf Disruption Radar"}

@app.get("/sw.js")
async def get_sw():
    sw_path = static_dir / "sw.js"
    if sw_path.exists():
        return FileResponse(sw_path, media_type="application/javascript")
    return HTMLResponse("self.addEventListener('fetch', function(e) {});", media_type="application/javascript")

# ---------------------------------------------------------
# HOME & QUOTA CONTROLS
# ---------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
async def index(request: Request, q: Optional[str] = None):
    user = get_current_user(request)
    client_ip = request.client.host if request.client else "127.0.0.1"
    
    # Gerçek PRO mu yoksa sadece Trial aşamasında mı?
    real_pro = bool(user and user.get("is_pro") == 1 and not user.get("is_trial"))
    
    identifier = f"user_{user['id']}" if user else f"guest_{client_ip}"
    daily_search_count = get_daily_search_count(identifier)

    # Arama limiti: Gerçek Pro 100/gün, Trial veya Free kullanıcılar 5/gün
    if real_pro:
        search_limit = 100
        search_limit_reached = False
        remaining_searches = "100/day"
    else:
        search_limit = 5
        search_limit_reached = daily_search_count >= search_limit
        remaining_searches = max(0, search_limit - daily_search_count)

    if q and not search_limit_reached:
        increment_search_count(identifier)
        if not real_pro:
            daily_search_count += 1
            remaining_searches = max(0, search_limit - daily_search_count)
            search_limit_reached = daily_search_count >= search_limit

    radar_data = parse_radar_data(search_query=q or "")

    watchlist = []
    tracked_flights = []
    watchlist_limit_reached = False
    
    # Şablon için is_pro değişkenini güvenli şekilde tanımlıyoruz
    is_pro = bool(user and user.get("is_pro") == 1)

    if user:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute(format_query("SELECT * FROM watchlist WHERE user_id = ? ORDER BY created_at DESC"), (user["id"],))
        watchlist = cursor.fetchall()

        cursor.execute(format_query("SELECT * FROM tracked_flights WHERE user_id = ? ORDER BY created_at DESC"), (user["id"],))
        tracked_flights = cursor.fetchall()
        conn.close()

        total_tracked = len(watchlist) + len(tracked_flights)
        max_track_limit = 50 if is_pro else 3
        if total_tracked >= max_track_limit:
            watchlist_limit_reached = True

    free_slots_left = max(0, 10 - get_free_user_count())

    context = {
        "request": request,
        "user": user,
        "query": q,
        "data": radar_data,
        "watchlist": watchlist,
        "tracked_flights": tracked_flights,
        "search_count": daily_search_count,
        "search_limit": search_limit,
        "search_limit_reached": search_limit_reached,
        "remaining_searches": remaining_searches,
        "watchlist_limit_reached": watchlist_limit_reached,
        "is_pro": is_pro,
        "free_slots_left": free_slots_left,
    }
    return templates.TemplateResponse(request, "index.html", context)

@app.post("/track")
async def track_route(request: Request, route_code: str = Form(...), destination_name: str = Form(...)):
    user = get_current_user(request)
    if not user:
        return RedirectResponse(url="/auth?mode=login", status_code=303)

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(format_query("SELECT COUNT(*) as count FROM watchlist WHERE user_id = ?"), (user["id"],))
    count = cursor.fetchone()["count"]

    max_limit = 50 if user.get("is_pro") else 3
    if count >= max_limit:
        conn.close()
        return RedirectResponse(url="/?limit_reached=1", status_code=303)

    try:
        cursor.execute(format_query("INSERT INTO watchlist (user_id, route_code, destination_name) VALUES (?, ?, ?)"),
                       (user["id"], route_code.strip().upper(), destination_name.strip()))
        conn.commit()
    except Exception:
        pass
    conn.close()
    return RedirectResponse(url="/", status_code=303)

@app.post("/untrack")
async def untrack_route(request: Request, item_id: int = Form(...)):
    user = get_current_user(request)
    if not user:
        return RedirectResponse(url="/auth?mode=login", status_code=303)
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(format_query("DELETE FROM watchlist WHERE id = ? AND user_id = ?"), (item_id, user["id"]))
    conn.commit()
    conn.close()
    return RedirectResponse(url="/", status_code=303)

@app.post("/track-flight")
async def track_individual_flight(
    request: Request,
    flight_num: str = Form(...),
    airline: str = Form(...),
    destination_name: str = Form(...),
    scheduled_time: str = Form(""),
    status: str = Form("Expected")
):
    user = get_current_user(request)
    if not user:
        return RedirectResponse(url="/auth?mode=login", status_code=303)

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(format_query("SELECT COUNT(*) as count FROM tracked_flights WHERE user_id = ?"), (user["id"],))
    count = cursor.fetchone()["count"]

    max_limit = 50 if user.get("is_pro") else 3
    if count >= max_limit:
        conn.close()
        return RedirectResponse(url="/?limit_reached=1", status_code=303)

    try:
        cursor.execute(
            format_query("INSERT INTO tracked_flights (user_id, flight_num, airline, destination_name, scheduled_time, last_status) VALUES (?, ?, ?, ?, ?, ?)"),
            (user["id"], flight_num.strip(), airline.strip(), destination_name.strip(), scheduled_time.strip(), status.strip())
        )
        conn.commit()

        if user.get("telegram_chat_id"):
            msg = (
                "✈️ <b>FLIGHT MONITORING ACTIVE</b>\n\n"
                f"Flight <b>{flight_num}</b> ({destination_name}) is now actively watched.\n"
                "You will receive an instant alert if it enters cancellation or delay status."
            )
            await send_telegram(user["telegram_chat_id"], msg)
    except Exception:
        pass

    conn.close()
    return RedirectResponse(url="/", status_code=303)

@app.post("/untrack-flight")
async def untrack_individual_flight(request: Request, flight_id: int = Form(...)):
    user = get_current_user(request)
    if not user:
        return RedirectResponse(url="/auth?mode=login", status_code=303)
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(format_query("DELETE FROM tracked_flights WHERE id = ? AND user_id = ?"), (flight_id, user["id"]))
    conn.commit()
    conn.close()
    return RedirectResponse(url="/", status_code=303)

# ---------------------------------------------------------
# 🛡️ /sync BUTTON
# ---------------------------------------------------------
@app.get("/sync")
async def sync_live(request: Request):
    user = get_current_user(request)
    if not user:
        return RedirectResponse(url="/auth?mode=login", status_code=303)
    
    if not user.get("is_pro") and not user.get("is_admin"):
        raise HTTPException(status_code=403, detail="This feature is only for Standard Pro members.")

    user_id = user["id"]
    now = datetime.now()
    user_sync_limits = SYSTEM_METRICS["user_sync_limits"]

    if user_id in user_sync_limits and not user.get("is_admin"):
        last_called = user_sync_limits[user_id]
        diff_mins = (now - last_called).total_seconds() / 60
        if diff_mins < 60:
            remaining_mins = int(60 - diff_mins)
            raise HTTPException(
                status_code=429, 
                detail=f"Rate limit exceeded. You can sync once per hour. Please wait {remaining_mins} minutes."
            )

    user_sync_limits[user_id] = now
    fetch_live_airport_data("DXB")
    return RedirectResponse(url="/", status_code=303)

# ---------------------------------------------------------
# AUTH & PASSWORD RESET ROUTES
# ---------------------------------------------------------
@app.get("/auth", response_class=HTMLResponse)
async def auth_page(request: Request, mode: Optional[str] = "register"):
    user = get_current_user(request)
    if user:
        return RedirectResponse(url="/", status_code=303)
    
    context = {
        "request": request,
        "error": None,
        "message": None,
        "initial_tab": "register" if mode == "register" else "login",
        "free_slots_left": max(0, 10 - get_free_user_count())
    }
    return templates.TemplateResponse(request, "auth.html", context)

@app.get("/auth/login")
@app.get("/login")
async def login_redirect():
    return RedirectResponse(url="/auth?mode=login", status_code=303)

@app.get("/auth/register")
@app.get("/register")
async def register_redirect():
    return RedirectResponse(url="/auth?mode=register", status_code=303)

@app.post("/auth/register")
async def register(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    telegram_chat_id: str = Form(...)
):
    email_clean = email.strip().lower()
    chat_id_clean = telegram_chat_id.strip() if telegram_chat_id else ""

    if not chat_id_clean or not is_valid_telegram_chat_id(chat_id_clean):
        context = {
            "request": request, 
            "error": "Telegram Chat ID is required for live flight alerts. Please enter a valid numeric Chat ID.", 
            "message": None, 
            "initial_tab": "register", 
            "free_slots_left": max(0, 10 - get_free_user_count())
        }
        return templates.TemplateResponse(request, "auth.html", context)

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(format_query("SELECT id FROM users WHERE lower(email) = ?"), (email_clean,))
    if cursor.fetchone():
        conn.close()
        context = {"request": request, "error": "This email is already registered.", "message": None, "initial_tab": "register", "free_slots_left": max(0, 10 - get_free_user_count())}
        return templates.TemplateResponse(request, "auth.html", context)

    free_count = get_free_user_count()
    is_pro_val = 1 if email_clean in ADMIN_EMAILS else 0

    if free_count >= 10 and is_pro_val == 0:
        conn.close()
        context = {"request": request, "error": "Founding Member Free Tier quota (10 users) is full. Please subscribe to Standard Pro.", "message": None, "initial_tab": "register", "free_slots_left": 0}
        return templates.TemplateResponse(request, "auth.html", context)

    pwd_hash = hash_password(password)

    if USE_PG:
        cursor.execute("INSERT INTO users (email, password_hash, telegram_chat_id, is_pro, telegram_credits) VALUES (%s, %s, %s, %s, 10) RETURNING id", (email_clean, pwd_hash, chat_id_clean, is_pro_val))
        user_id = cursor.fetchone()["id"]
    else:
        cursor.execute("INSERT INTO users (email, password_hash, telegram_chat_id, is_pro, telegram_credits) VALUES (?, ?, ?, ?, 10)", (email_clean, pwd_hash, chat_id_clean, is_pro_val))
        user_id = cursor.lastrowid

    session_id = secrets.token_hex(24)
    cursor.execute(format_query("INSERT INTO sessions (session_id, user_id) VALUES (?, ?)"), (session_id, user_id))
    conn.commit()
    conn.close()

    send_admin_email(
        "New User Registration",
        f"A new user registered to the system:\nEmail: {email_clean}\nTelegram Chat ID: {chat_id_clean}"
    )

    if free_count + 1 == 10 and is_pro_val == 0:
        admin_chat = os.getenv("MY_TELEGRAM_ID", "").strip()
        if admin_chat:
            alert_admin_msg = "🚨 <b>TIME TO UPGRADE TO PAID TIER!</b>\n\n10th Free Founding Member user registered! Quota is now full."
            await send_telegram(admin_chat, alert_admin_msg)

    await send_telegram(chat_id_clean, "👋 <b>Gulf Disruption Radar Active!</b>\n\nYour corridor & flight disruption alerts are linked successfully. You have 10 complimentary Telegram notification credits.")

    res = RedirectResponse(url="/", status_code=303)
    res.set_cookie("session_id", session_id, max_age=30*86400, httponly=True, secure=not DEBUG_MODE, samesite="lax")
    return res

@app.post("/auth/login")
async def login(request: Request, email: str = Form(...), password: str = Form(...)):
    email_clean = email.strip().lower()
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(format_query("SELECT * FROM users WHERE lower(email) = ?"), (email_clean,))
    user = cursor.fetchone()

    if not user or not verify_password(user["password_hash"], password):
        conn.close()
        context = {"request": request, "error": "Invalid email or password.", "message": None, "initial_tab": "login", "free_slots_left": max(0, 10 - get_free_user_count())}
        return templates.TemplateResponse(request, "auth.html", context)

    target_user_id = user["id"]
    session_id = secrets.token_hex(24)
    cursor.execute(format_query("INSERT INTO sessions (session_id, user_id) VALUES (?, ?)"), (session_id, target_user_id))
    conn.commit()
    conn.close()

    res = RedirectResponse(url="/", status_code=303)
    res.set_cookie("session_id", session_id, max_age=30*86400, httponly=True, secure=not DEBUG_MODE, samesite="lax")
    return res

@app.get("/auth/logout")
async def logout(request: Request):
    session_id = request.cookies.get("session_id")
    if session_id:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute(format_query("DELETE FROM sessions WHERE session_id = ?"), (session_id,))
        conn.commit()
        conn.close()
    res = RedirectResponse(url="/", status_code=303)
    res.delete_cookie("session_id", path="/", samesite="lax")
    return res

@app.post("/auth/forgot-password")
async def forgot_password(request: Request, email: str = Form(...)):
    email_clean = email.strip().lower()
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(format_query("SELECT id FROM users WHERE lower(email) = ?"), (email_clean,))
    user = cursor.fetchone()

    if user:
        user_id = user["id"]
        token = secrets.token_urlsafe(32)
        expires_at = datetime.now() + timedelta(hours=1)
        
        cursor.execute(format_query("INSERT INTO password_resets (user_id, token, expires_at) VALUES (?, ?, ?)"), 
                       (user_id, token, expires_at.strftime("%Y-%m-%d %H:%M:%S")))
        conn.commit()
        
        reset_url = f"{APP_BASE_URL}/auth/reset-password?token={token}"
        send_reset_email(email_clean, reset_url)

    conn.close()
    context = {
        "request": request,
        "message": "If an account with that email exists, a password reset link has been sent.",
        "error": None,
        "initial_tab": "login",
        "free_slots_left": max(0, 10 - get_free_user_count())
    }
    return templates.TemplateResponse(request, "auth.html", context)

@app.get("/auth/reset-password", response_class=HTMLResponse)
async def reset_password_get(request: Request, token: str):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(format_query("SELECT * FROM password_resets WHERE token = ? AND expires_at > ?"), 
                   (token, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
    record = cursor.fetchone()
    conn.close()

    if not record:
        return HTMLResponse("<h3>Invalid or expired password reset link.</h3><p><a href='/auth'>Return to Sign In</a></p>", status_code=400)

    return templates.TemplateResponse(request, "reset_password.html", {"request": request, "token": token, "error": None})

@app.post("/auth/reset-password")
async def reset_password_post(request: Request, token: str = Form(...), password: str = Form(...), confirm_password: str = Form(...)):
    if password != confirm_password:
        return templates.TemplateResponse(request, "reset_password.html", {"request": request, "token": token, "error": "Passwords do not match."})

    if len(password) < 6:
        return templates.TemplateResponse(request, "reset_password.html", {"request": request, "token": token, "error": "Password must be at least 6 characters long."})

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(format_query("SELECT * FROM password_resets WHERE token = ? AND expires_at > ?"), 
                   (token, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
    record = cursor.fetchone()

    if not record:
        conn.close()
        return HTMLResponse("<h3>Invalid or expired password reset link.</h3><p><a href='/auth'>Return to Sign In</a></p>", status_code=400)

    user_id = record["user_id"]
    new_pwd_hash = hash_password(password)

    cursor.execute(format_query("UPDATE users SET password_hash = ? WHERE id = ?"), (new_pwd_hash, user_id))
    cursor.execute(format_query("DELETE FROM password_resets WHERE token = ?"), (token,))
    conn.commit()
    conn.close()

    return RedirectResponse(url="/auth?mode=login&message=Password+updated+successfully", status_code=303)

# ---------------------------------------------------------
# SUPPORT DESK API
# ---------------------------------------------------------
@app.post("/api/support")
async def support_ticket_api(request: Request):
    data = await request.json()
    user_email = (data.get("email") or "").strip().lower()
    msg = (data.get("message") or "").strip()
    admin_chat = os.getenv("MY_TELEGRAM_ID", "").strip()

    if admin_chat:
        support_msg = f"🛠️ <b>GULF RADAR SUPPORT</b>\n\nFrom: {user_email}\nMessage: {msg}"
        await send_telegram(admin_chat, support_msg)
    return {"success": True}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="127.0.0.1", port=8000, reload=True)