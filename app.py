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
import time

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

# Dodo Payments Product IDs from Environment
RADAR_PRO_MONTHLY = os.getenv("Radar_Pro_Monthly", "").strip()
RADAR_PRO_ANNUAL = os.getenv("Radar_Pro_Annual", "").strip()

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
    except Exception as e:
        print(f"[SMTP Error]: {e}")

def send_reset_email(to_email: str, reset_url: str):
    subject = "Password Reset Request - Gulf Disruption Radar"
    html_content = f"""
    <div style="font-family: Arial, sans-serif; padding: 20px; color: #333;">
        <h2>Password Reset Request</h2>
        <p>Click the link below to set a new password:</p>
        <p><a href="{reset_url}" style="background-color: #0f172a; color: white; padding: 10px 20px; text-decoration: none; border-radius: 5px;">Reset Password</a></p>
    </div>
    """
    if not SMTP_PASSWORD:
        print(f"[FALLBACK RESET LINK FOR {to_email}]: {reset_url}")
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
    except Exception as e:
        print(f"[SMTP Error]: {e}")

# ---------------------------------------------------------
# DATABASE MANAGEMENT (POSTGRESQL & SQLITE SUPPORT)
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
    conn.commit()
    conn.close()

# ---------------------------------------------------------
# COUNTER & SECURITY
# ---------------------------------------------------------
def get_counter(key: str) -> int:
    today_str = date.today().isoformat()
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(format_query("SELECT val, last_reset FROM app_counters WHERE key = ?"), (key,))
    row = cursor.fetchone()
    if not row:
        cursor.execute(format_query("INSERT INTO app_counters (key, val, last_reset) VALUES (?, 0, ?)"), (key, today_str))
        conn.commit()
        conn.close()
        return 0
    conn.close()
    return row["val"]

def increment_counter(key: str, amount: int = 1):
    get_counter(key)
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(format_query("UPDATE app_counters SET val = val + ? WHERE key = ?"), (amount, key))
    conn.commit()
    conn.close()

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
# NOTIFICATIONS & USERS
# ---------------------------------------------------------
def _send_telegram_worker(chat_id: str, message: str):
    if not chat_id or not TELEGRAM_BOT_TOKEN:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        requests.post(url, json={"chat_id": str(chat_id).strip(), "text": message, "parse_mode": "HTML"}, timeout=8)
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

def is_valid_telegram_chat_id(chat_id: str) -> bool:
    return bool(re.fullmatch(r"-?\d{6,20}", str(chat_id).strip())) if chat_id else False

def get_current_user(request: Request):
    session_id = request.cookies.get("session_id")
    if not session_id:
        return None
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(format_query("""
        SELECT u.id, u.email, u.telegram_chat_id, u.is_pro, u.telegram_credits, u.created_at
        FROM users u JOIN sessions s ON u.id = s.user_id WHERE s.session_id = ?
    """), (session_id,))
    user = cursor.fetchone()
    conn.close()
    if not user:
        return None

    user_dict = dict(user)
    created_at = user_dict.get("created_at")
    trial_active = False
    if created_at:
        try:
            reg_date = datetime.fromisoformat(str(created_at))
            if datetime.now() - reg_date <= timedelta(days=7):
                trial_active = True
        except Exception:
            pass

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
    cursor.execute(format_query("SELECT count FROM search_logs WHERE identifier = ? AND search_date = ?"), (identifier, today))
    row = cursor.fetchone()
    conn.close()
    return row["count"] if row else 0

def increment_search_count(identifier: str):
    today = date.today().isoformat()
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(format_query("""
        INSERT INTO search_logs (identifier, search_date, count) VALUES (?, ?, 1)
        ON CONFLICT(identifier, search_date) DO UPDATE SET count = search_logs.count + 1
    """), (identifier, today))
    conn.commit()
    conn.close()

# ---------------------------------------------------------
# LIVE AIRPORT & FIDS ENGINE (WITH SMART FALLBACK)
# ---------------------------------------------------------
def fetch_live_airport_data(iata_code="DXB"):
    if not RAPIDAPI_KEY:
        return False, "RapidAPI Key is not defined."
    if DATA_CACHE_FILE.exists() and (time.time() - DATA_CACHE_FILE.stat().st_mtime < 900):
        return True, "Using cached FIDS data."

    url = f"https://aerodatabox.p.rapidapi.com/flights/airports/iata/{iata_code}"
    headers = {"X-RapidAPI-Key": RAPIDAPI_KEY, "X-RapidAPI-Host": "aerodatabox.p.rapidapi.com"}
    params = {"offsetMinutes": "-120", "durationMinutes": "720", "withLeg": "true", "direction": "Both", "withCancelled": "true", "withCodeshared": "true", "withCargo": "true"}
    try:
        res = requests.get(url, headers=headers, params=params, timeout=15)
        increment_counter("api_requests_this_month", 1)
        if res.status_code == 200:
            with open(DATA_CACHE_FILE, "w", encoding="utf-8") as f:
                f.write(res.text)
            return True, f"Live {iata_code} FIDS synchronized."
        return False, f"API Error ({res.status_code})"
    except Exception as e:
        return False, str(e)

def parse_radar_data(search_query=""):
    data = None
    if DATA_CACHE_FILE.exists():
        try:
            with open(DATA_CACHE_FILE, "r", encoding="utf-8") as f:
                text = f.read()
            data = json.loads(text)
        except Exception:
            try:
                last_bracket = text.rstrip().rstrip(",").rfind("}")
                data = json.loads(text[:last_bracket+1] + "]}")
            except Exception:
                data = None

    if not data or not data.get("departures"):
        data = {
            "departures": [
                {"number": "FZ-821", "airline": {"name": "flydubai Cargo"}, "status": "Canceled", "arrival": {"airport": {"iata": "RUH", "name": "Riyadh King Khalid International", "countryCode": "SA"}}, "departure": {"scheduledTime": {"local": "2026-06-06T14:30:00+04:00"}, "terminal": "2", "gate": "14"}},
                {"number": "EK-833", "airline": {"name": "Emirates SkyCargo"}, "status": "Delayed", "arrival": {"airport": {"iata": "DMM", "name": "Dammam King Fahd International", "countryCode": "SA"}}, "departure": {"scheduledTime": {"local": "2026-06-06T15:00:00+04:00"}, "terminal": "3", "gate": "B2"}},
                {"number": "QR-101", "airline": {"name": "Qatar Airways"}, "status": "Expected", "arrival": {"airport": {"iata": "DOH", "name": "Doha Hamad International", "countryCode": "QA"}}, "departure": {"scheduledTime": {"local": "2026-06-06T16:15:00+04:00"}, "terminal": "1", "gate": "A1"}},
                {"number": "GF-512", "airline": {"name": "Gulf Air"}, "status": "Canceled", "arrival": {"airport": {"iata": "BAH", "name": "Bahrain International", "countryCode": "BH"}}, "departure": {"scheduledTime": {"local": "2026-06-06T17:00:00+04:00"}, "terminal": "1", "gate": "C4"}},
                {"number": "WY-603", "airline": {"name": "Oman Air"}, "status": "Expected", "arrival": {"airport": {"iata": "MCT", "name": "Muscat International", "countryCode": "OM"}}, "departure": {"scheduledTime": {"local": "2026-06-06T17:45:00+04:00"}, "terminal": "2", "gate": "9"}},
                {"number": "SV-554", "airline": {"name": "Saudia Cargo"}, "status": "Delayed", "arrival": {"airport": {"iata": "JED", "name": "Jeddah King Abdulaziz", "countryCode": "SA"}}, "departure": {"scheduledTime": {"local": "2026-06-06T18:30:00+04:00"}, "terminal": "3", "gate": "B8"}},
                {"number": "FZ-825", "airline": {"name": "flydubai"}, "status": "Expected", "arrival": {"airport": {"iata": "KWI", "name": "Kuwait International", "countryCode": "KW"}}, "departure": {"scheduledTime": {"local": "2026-06-06T19:00:00+04:00"}, "terminal": "2", "gate": "11"}},
                {"number": "EK-841", "airline": {"name": "Emirates"}, "status": "Expected", "arrival": {"airport": {"iata": "RUH", "name": "Riyadh King Khalid International", "countryCode": "SA"}}, "departure": {"scheduledTime": {"local": "2026-06-06T20:15:00+04:00"}, "terminal": "3", "gate": "C12"}}
            ]
        }

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
        terminal = dep_info.get("terminal", "-")
        gate = dep_info.get("gate", "-")
        runway = dep_info.get("runway", "-")

        ac = f.get("aircraft", {})
        ac_model = ac.get("model", "Commercial Jet")
        ac_reg = ac.get("reg", "-")

        sched_display = sched_time_raw.split("T")[-1][:5] if "T" in sched_time_raw else sched_time_raw

        if status == "Canceled":
            canceled_count += 1
            route_stats[dest_iata]["canceled"] += 1
        elif status == "Delayed":
            delayed_count += 1
            route_stats[dest_iata]["delayed"] += 1

        route_stats[dest_iata]["total"] += 1
        route_stats[dest_iata]["name"] = dest_name

        all_flights.append({
            "flight_num": flight_num, "call_sign": call_sign, "airline": airline,
            "dest_name": dest_name, "dest_iata": dest_iata, "dest_icao": dest_icao,
            "dest_country": dest_country, "dest_tz": dest_tz, "coords": coords,
            "time": sched_display, "sched_local": sched_time_raw, "sched_utc": sched_time_utc,
            "revised_time": revised_time, "terminal": terminal, "gate": gate, "runway": runway,
            "status": status, "aircraft_model": ac_model, "aircraft_reg": ac_reg, "is_cargo": is_cargo
        })

    if search_query:
        q = search_query.lower()
        all_flights = [fl for fl in all_flights if q in fl["dest_name"].lower() or q in fl["dest_iata"].lower() or q in fl["airline"].lower() or q in fl["flight_num"].lower()]

    all_flights = sorted(all_flights, key=lambda x: x.get("time", ""))

    critical_routes = []
    for iata, r in route_stats.items():
        if r["canceled"] > 0 or r["delayed"] > 0:
            critical_routes.append({
                "iata": iata, "name": r["name"], "canceled": r["canceled"],
                "delayed": r["delayed"], "total": r["total"],
                "risk_level": "CRITICAL" if r["canceled"] >= 2 else "WARNING"
            })

    critical_routes = sorted(critical_routes, key=lambda x: x["canceled"], reverse=True)
    sliced_flights = all_flights[:35]
    top_8_flights = sliced_flights[:8]

    return {
        "total": max(total_count, len(all_flights)),
        "canceled": max(canceled_count, sum(1 for f in all_flights if f["status"] == "Canceled")),
        "delayed": max(delayed_count, sum(1 for f in all_flights if f["status"] == "Delayed")),
        "critical_routes": critical_routes,
        "all_flights": sliced_flights,
        "top_8_flights": top_8_flights
    }

# ---------------------------------------------------------
# BACKGROUND MONITOR & STARTUP
# ---------------------------------------------------------
async def automated_radar_monitor():
    while True:
        try:
            fetch_live_airport_data("DXB")
            SYSTEM_METRICS["last_bot_run"] = datetime.now()
            SYSTEM_METRICS["bot_status"] = "Active"
        except Exception as e:
            SYSTEM_METRICS["bot_status"] = f"Error: {str(e)}"
        await asyncio.sleep(1800)

@app.on_event("startup")
async def startup_event():
    init_db()
    asyncio.create_task(automated_radar_monitor())

# ---------------------------------------------------------
# ADMIN PANEL (FULL FEATURED)
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

            <section class="bg-slate-900 border border-slate-800 rounded-2xl p-6 space-y-4">
                <h2 class="text-xs font-bold uppercase tracking-wider text-sky-400">📊 1. API Cost & Quota Management</h2>
                <div class="grid grid-cols-1 md:grid-cols-4 gap-4">
                    <div class="bg-slate-950 p-4 rounded-xl border border-slate-800">
                        <p class="text-[11px] text-slate-400 font-medium">Monthly API Requests</p>
                        <p class="text-2xl font-extrabold text-white mt-1">{api_used} <span class="text-xs font-normal text-slate-400">/ {api_limit}</span></p>
                        <div class="mt-3 w-full bg-slate-800 rounded-full h-2">
                            <div class="bg-{api_color}-500 h-2 rounded-full" style="width: {min(api_pct, 100)}%;"></div>
                        </div>
                    </div>
                    <div class="bg-slate-950 p-4 rounded-xl border border-slate-800">
                        <p class="text-[11px] text-slate-400 font-medium">Telegram Sent Today</p>
                        <p class="text-2xl font-extrabold text-emerald-400 mt-1">{get_counter("telegram_sent_today")}</p>
                    </div>
                    <div class="bg-slate-950 p-4 rounded-xl border border-slate-800">
                        <p class="text-[11px] text-slate-400 font-medium">Background Bot Status</p>
                        <div class="flex items-center gap-2 mt-2">
                            <span class="w-3 h-3 rounded-full bg-emerald-500 animate-pulse"></span>
                            <span class="text-xs font-bold text-white">{SYSTEM_METRICS['bot_status']}</span>
                        </div>
                    </div>
                    <div class="bg-slate-950 p-4 rounded-xl border border-slate-800">
                        <p class="text-[11px] text-slate-400 font-medium">Cache Update</p>
                        <p class="text-lg font-bold text-white mt-1">{cache_time_str}</p>
                    </div>
                </div>
            </section>

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
        </div>
    </body>
    </html>
    """
    return HTMLResponse(content=html_content)

@app.get("/export-pdf")
async def export_pdf(request: Request):
    user = get_current_user(request)
    if not user or not user.get("is_pro"):
        return RedirectResponse(url="/?limit_reached=1", status_code=303)
    radar = parse_radar_data()
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=letter, rightMargin=36, leftMargin=36, topMargin=36, bottomMargin=36)
    elements = []
    styles = getSampleStyleSheet()
    elements.append(Paragraph("<b>GULF DISRUPTION RADAR - RISK REPORT</b>", styles['Heading1']))
    elements.append(Spacer(1, 10))
    elements.append(Paragraph(f"Cancellations: {radar['canceled']} | Delays: {radar['delayed']} | Total Monitored: {radar['total']}", styles['Normal']))
    doc.build(elements)
    buffer.seek(0)
    return StreamingResponse(iter([buffer.getvalue()]), media_type="application/pdf", headers={"Content-Disposition": "attachment; filename=Gulf_Report.pdf"})

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
# HOME & CORE ROUTES
# ---------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
async def index(request: Request, q: Optional[str] = None):
    user = get_current_user(request)
    client_ip = request.client.host if request.client else "127.0.0.1"
    real_pro = bool(user and user.get("is_pro") == 1 and not user.get("is_trial"))
    
    identifier = f"user_{user['id']}" if user else f"guest_{client_ip}"
    daily_search_count = get_daily_search_count(identifier)

    search_limit = 100 if real_pro else 5
    search_limit_reached = (not real_pro) and (daily_search_count >= search_limit)
    remaining_searches = "100/day" if real_pro else max(0, search_limit - daily_search_count)

    if q and not search_limit_reached:
        increment_search_count(identifier)

    radar_data = parse_radar_data(search_query=q or "")

    watchlist = []
    tracked_flights = []
    watchlist_limit_reached = False
    is_pro = bool(user and user.get("is_pro") == 1)

    if user:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute(format_query("SELECT * FROM watchlist WHERE user_id = ? ORDER BY created_at DESC"), (user["id"],))
        watchlist = cursor.fetchall()
        cursor.execute(format_query("SELECT * FROM tracked_flights WHERE user_id = ? ORDER BY created_at DESC"), (user["id"],))
        tracked_flights = cursor.fetchall()
        conn.close()

        if (len(watchlist) + len(tracked_flights)) >= (50 if is_pro else 3):
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
        "radar_pro_monthly": RADAR_PRO_MONTHLY,
        "radar_pro_annual": RADAR_PRO_ANNUAL,
    }
    return templates.TemplateResponse(request, "index.html", context)

@app.post("/track")
async def track_route(request: Request, route_code: str = Form(...), destination_name: str = Form(...)):
    user = get_current_user(request)
    if not user:
        return RedirectResponse(url="/auth?mode=login", status_code=303)
    conn = get_db()
    cursor = conn.cursor()
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
async def track_flight(request: Request, flight_num: str = Form(...), airline: str = Form(...), destination_name: str = Form(...), scheduled_time: str = Form(""), status: str = Form("Expected")):
    user = get_current_user(request)
    if not user:
        return RedirectResponse(url="/auth?mode=login", status_code=303)
    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute(format_query("INSERT INTO tracked_flights (user_id, flight_num, airline, destination_name, scheduled_time, last_status) VALUES (?, ?, ?, ?, ?, ?)"),
                       (user["id"], flight_num.strip(), airline.strip(), destination_name.strip(), scheduled_time.strip(), status.strip()))
        conn.commit()
    except Exception:
        pass
    conn.close()
    return RedirectResponse(url="/", status_code=303)

@app.post("/untrack-flight")
async def untrack_flight(request: Request, flight_id: int = Form(...)):
    user = get_current_user(request)
    if not user:
        return RedirectResponse(url="/auth?mode=login", status_code=303)
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(format_query("DELETE FROM tracked_flights WHERE id = ? AND user_id = ?"), (flight_id, user["id"]))
    conn.commit()
    conn.close()
    return RedirectResponse(url="/", status_code=303)

@app.get("/sync")
async def sync_live(request: Request):
    user = get_current_user(request)
    if not user:
        return RedirectResponse(url="/auth?mode=login", status_code=303)
    fetch_live_airport_data("DXB")
    return RedirectResponse(url="/", status_code=303)

# ---------------------------------------------------------
# AUTH & SUPPORT ROUTES
# ---------------------------------------------------------
@app.get("/auth", response_class=HTMLResponse)
async def auth_page(request: Request, mode: Optional[str] = "register"):
    user = get_current_user(request)
    if user:
        return RedirectResponse(url="/", status_code=303)
    return templates.TemplateResponse(request, "auth.html", {"request": request, "error": None, "message": None, "initial_tab": "register" if mode == "register" else "login", "free_slots_left": max(0, 10 - get_free_user_count())})

@app.post("/auth/register")
async def register(request: Request, email: str = Form(...), password: str = Form(...), telegram_chat_id: str = Form(...)):
    email_clean = email.strip().lower()
    chat_id_clean = telegram_chat_id.strip() if telegram_chat_id else ""
    if not chat_id_clean or not is_valid_telegram_chat_id(chat_id_clean):
        return templates.TemplateResponse(request, "auth.html", {"request": request, "error": "Valid Telegram Chat ID required.", "message": None, "initial_tab": "register", "free_slots_left": max(0, 10 - get_free_user_count())})

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(format_query("SELECT id FROM users WHERE lower(email) = ?"), (email_clean,))
    if cursor.fetchone():
        conn.close()
        return templates.TemplateResponse(request, "auth.html", {"request": request, "error": "Email already registered.", "message": None, "initial_tab": "register", "free_slots_left": max(0, 10 - get_free_user_count())})

    pwd_hash = hash_password(password)
    is_pro_val = 1 if email_clean in ADMIN_EMAILS else 0

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
    conn.close()

    if not user or not verify_password(user["password_hash"], password):
        return templates.TemplateResponse(request, "auth.html", {"request": request, "error": "Invalid email or password.", "message": None, "initial_tab": "login", "free_slots_left": max(0, 10 - get_free_user_count())})

    session_id = secrets.token_hex(24)
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(format_query("INSERT INTO sessions (session_id, user_id) VALUES (?, ?)"), (session_id, user["id"]))
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

@app.post("/api/support")
async def support_ticket_api(request: Request):
    data = await request.json()
    user_email = (data.get("email") or "").strip().lower()
    msg = (data.get("message") or "").strip()
    admin_chat = os.getenv("MY_TELEGRAM_ID", "").strip()
    if admin_chat:
        await send_telegram(admin_chat, f"🛠️ <b>SUPPORT TICKET</b>\nFrom: {user_email}\nMessage: {msg}")
    return {"success": True}

@app.get("/security", response_class=HTMLResponse)
async def security_page(request: Request):
    return templates.TemplateResponse(request=request, name="security.html", context={"request": request, "user": get_current_user(request), "free_slots_left": max(0, 10 - get_free_user_count())})


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="127.0.0.1", port=8000, reload=True)