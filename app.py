import asyncio
import csv
from datetime import date, datetime, timezone
import hashlib
import hmac
import io
import json
import os
from pathlib import Path
import re
import secrets
from typing import Optional
from collections import defaultdict

from dotenv import load_dotenv
from fastapi import FastAPI, Form, Header, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
import requests
import sqlite3

# ---------------------------------------------------------
# KONFİGÜRASYON & SABİT DEĞİŞKENLER
# ---------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

DB_FILE = BASE_DIR / "flights.db"
DATA_CACHE_FILE = BASE_DIR / "rapidapi_API.txt"

RAPIDAPI_KEY = os.getenv("RAPIDAPI_KEY", "").strip()
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
APP_BASE_URL = os.getenv("APP_BASE_URL", "http://127.0.0.1:8000").rstrip("/")
DEBUG_MODE = os.getenv("DEBUG", "true").strip().lower() in {"1", "true", "yes", "on"}
DODO_WEBHOOK_SECRET = os.getenv("DODO_WEBHOOK_SECRET", "").strip()

DODO_MONTHLY_LINK = os.getenv("Radar_Pro_Monthly", "").strip()
DODO_ANNUAL_LINK = os.getenv("Radar_Pro_Annual", "").strip()

ADMIN_EMAILS = [email.strip().lower() for email in os.getenv("ADMIN_EMAIL", "ayhan.hakyemez@gmail.com").split(",") if email.strip()]

# KOTA VE PLAN LİMİTLERİ
FREE_SEARCH_LIMIT = 3
PRO_DAILY_SEARCH_LIMIT = 50
PRO_MAX_WATCHLIST_LIMIT = 20

app = FastAPI(title="Gulf Disruption Radar")

static_dir = BASE_DIR / "static"
static_dir.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

# ---------------------------------------------------------
# VERİTABANI YÖNETİMİ
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
                created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS watchlist (
                id SERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                route_code TEXT NOT NULL,
                destination_name TEXT NOT NULL,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(user_id, route_code)
            );
            CREATE TABLE IF NOT EXISTS tracked_flights (
                id SERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                flight_num TEXT NOT NULL,
                airline TEXT NOT NULL,
                destination_name TEXT NOT NULL,
                scheduled_time TEXT,
                last_status TEXT NOT NULL DEFAULT 'Expected',
                created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(user_id, flight_num)
            );
            CREATE TABLE IF NOT EXISTS search_logs (
                id SERIAL PRIMARY KEY,
                identifier TEXT NOT NULL,
                search_date DATE NOT NULL,
                count INTEGER DEFAULT 1,
                UNIQUE(identifier, search_date)
            );
            CREATE TABLE IF NOT EXISTS sessions (
                session_id TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
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
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
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
    conn.commit()
    conn.close()

# ---------------------------------------------------------
# ŞİFRELEME & GÜVENLİK
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
# TELEGRAM BİLDİRİM MOTORU & KOTA BİLDİRİMİ
# ---------------------------------------------------------
def _send_telegram_worker(chat_id: str, message: str):
    if not chat_id or not TELEGRAM_BOT_TOKEN:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": str(chat_id).strip(), "text": message, "parse_mode": "HTML"}
    try:
        requests.post(url, json=payload, timeout=8)
    except Exception as e:
        print(f"[Telegram Error]: {e}")

async def send_telegram(chat_id: str, message: str):
    await asyncio.to_thread(_send_telegram_worker, chat_id, message)

def notify_admin_quota_exceeded(user_email: str, current_searches: int):
    admin_chat = os.getenv("MY_TELEGRAM_ID", "").strip()
    if admin_chat:
        msg = (
            "🚨 <b>GULF RADAR | QUOTA EXCEEDED ALERT</b>\n\n"
            f"User <code>{user_email}</code> reached the Pro limit ({current_searches} searches).\n"
            "System paused automated lookups for this user.\n\n"
            "💡 <i>Action Required:</i> Create a custom Gold Tier or increase subscription fee."
        )
        asyncio.create_task(send_telegram(admin_chat, msg))

# ---------------------------------------------------------
# KULLANICI VE KOTA KONTROLLERİ (UTC TARIH YÖNETIMI)
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
        SELECT u.id, u.email, u.telegram_chat_id, u.is_pro 
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
    if user_dict["email"].lower() in ADMIN_EMAILS:
        user_dict["is_pro"] = 1
        user_dict["is_admin"] = True
    else:
        user_dict["is_admin"] = False
    return user_dict

def get_today_utc_str() -> str:
    return datetime.now(timezone.utc).date().isoformat()

def get_search_count(identifier: str) -> int:
    today_utc = get_today_utc_str()
    conn = get_db()
    cursor = conn.cursor()
    query = format_query("SELECT count FROM search_logs WHERE identifier = ? AND search_date = ?")
    cursor.execute(query, (identifier, today_utc))
    row = cursor.fetchone()
    conn.close()
    return row["count"] if row else 0

def increment_search_count(identifier: str):
    today_utc = get_today_utc_str()
    conn = get_db()
    cursor = conn.cursor()
    query = format_query("""
        INSERT INTO search_logs (identifier, search_date, count) 
        VALUES (?, ?, 1)
        ON CONFLICT(identifier, search_date) DO UPDATE SET count = search_logs.count + 1
    """)
    cursor.execute(query, (identifier, today_utc))
    conn.commit()
    conn.close()

# ---------------------------------------------------------
# CANLI HAVALİMANI & FIDS MOTORU
# ---------------------------------------------------------
def fetch_live_airport_data(iata_code="DXB"):
    if not RAPIDAPI_KEY:
        return False, "RAPIDAPI_KEY çevresel değişkeni tanımlanmamış."

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
        if res.status_code == 200:
            with open(DATA_CACHE_FILE, "w", encoding="utf-8") as f:
                f.write(res.text)
            return True, f"Canlı {iata_code} FIDS verisi senkronize edildi."
        return False, f"API Hatası ({res.status_code})"
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
        airline = f.get("airline", {}).get("name", "Bilinmeyen Havayolu")
        flight_num = f.get("number", "N/A")
        call_sign = f.get("callSign") or "N/A"
        is_cargo = f.get("isCargo", False)

        arr_info = f.get("arrival", {})
        dest_name = arr_info.get("airport", {}).get("name", "Bilinmiyor")
        dest_iata = arr_info.get("airport", {}).get("iata", "N/A")
        dest_icao = arr_info.get("airport", {}).get("icao", "N/A")
        dest_country = (arr_info.get("airport", {}).get("countryCode") or "-").upper()
        dest_tz = arr_info.get("airport", {}).get("timeZone", "-")
        loc = arr_info.get("airport", {}).get("location", {})
        
        lat = loc.get('lat') if loc else None
        lon = loc.get('lon') if loc else None
        coords = f"{lat}, {lon}" if lat is not None and lon is not None else "N/A"

        dep_info = f.get("departure", {})
        sched_time_raw = dep_info.get("scheduledTime", {}).get("local", "-")
        sched_time_utc = dep_info.get("scheduledTime", {}).get("utc", "-")
        revised_time = dep_info.get("revisedTime", {}).get("local") or "Zamanında"
        runway_time = dep_info.get("runwayTime", {}).get("local") or "Atanmadı"
        terminal = dep_info.get("terminal") or "N/A"
        gate = dep_info.get("gate") or "Atanmadı"
        runway = dep_info.get("runway") or "Atanmadı"

        ac = f.get("aircraft", {})
        ac_model = ac.get("model") or "Belirtilmemiş"
        ac_reg = ac.get("reg") or "N/A"
        ac_modes = ac.get("modeS") or "N/A"

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
# GELİŞMİŞ STATE-CHANGE ARKA PLAN MONİTÖRÜ
# ---------------------------------------------------------
async def automated_radar_monitor():
    while True:
        try:
            fetch_live_airport_data("DXB")
            radar = parse_radar_data()
            current_flight_map = {fl["flight_num"]: fl for fl in radar["all_flights"]}

            conn = get_db()
            cursor = conn.cursor()

            cursor.execute("""
                SELECT tf.id, tf.user_id, tf.flight_num, tf.airline, tf.destination_name, tf.last_status, u.telegram_chat_id
                FROM tracked_flights tf
                JOIN users u ON tf.user_id = u.id
                WHERE u.telegram_chat_id IS NOT NULL
            """)
            tracked_flights_list = cursor.fetchall()

            for tf in tracked_flights_list:
                f_num = tf["flight_num"]
                old_status = tf["last_status"]

                if f_num in current_flight_map:
                    new_status = current_flight_map[f_num]["status"]

                    if new_status != old_status:
                        cursor.execute(format_query("UPDATE tracked_flights SET last_status = ? WHERE id = ?"), (new_status, tf["id"]))
                        conn.commit()

                        if new_status in ["Canceled", "Delayed"]:
                            fl_obj = current_flight_map[f_num]
                            alert_msg = (
                                "🚨 <b>GULF DISRUPTION RADAR | INDIVIDUAL FLIGHT ALERT</b>\n\n"
                                f"✈️ <b>Flight:</b> <code>{f_num}</code> ({tf['airline']})\n"
                                f"📍 <b>Route:</b> DXB ➔ {tf['destination_name']}\n"
                                f"⚠️ <b>Status Changed:</b> <b>{new_status.upper()}</b> (Prev: {old_status})\n"
                                f"🕒 <b>Scheduled:</b> {fl_obj['time']} GST\n"
                                f"🚪 <b>Terminal/Gate:</b> T{fl_obj['terminal']} / G-{fl_obj['gate']}\n\n"
                                f"🔗 <a href='{APP_BASE_URL}'>View Live Radar Dashboard</a>"
                            )
                            await send_telegram(tf["telegram_chat_id"], alert_msg)

            cursor.execute("""
                SELECT w.route_code, w.destination_name, u.telegram_chat_id 
                FROM watchlist w
                JOIN users u ON w.user_id = u.id
                WHERE u.telegram_chat_id IS NOT NULL
            """)
            watchlist_items = cursor.fetchall()
            route_map = {r["iata"]: r for r in radar["critical_routes"]}

            for item in watchlist_items:
                code = item["route_code"]
                if code in route_map and route_map[code]["canceled"] >= 1:
                    r = route_map[code]
                    alert_msg = (
                        "🚨 <b>GULF DISRUPTION RADAR | CORRIDOR ALERT</b>\n\n"
                        f"📍 <b>Corridor:</b> DXB ➔ {item['destination_name']} ({code})\n"
                        f"⚠️ <b>Impact:</b> <b>{r['canceled']} CANCELLATIONS</b> out of {r['total']} monitored flights\n"
                        f"⏱️ <b>Delays:</b> {r['delayed']} flights\n\n"
                        f"🔗 <a href='{APP_BASE_URL}/?q={code}'>Inspect Route Details</a>"
                    )
                    await send_telegram(item["telegram_chat_id"], alert_msg)

            conn.close()
        except Exception as e:
            print(f"[Monitor Exception]: {e}")
        await asyncio.sleep(1800)

@app.on_event("startup")
async def startup_event():
    init_db()
    asyncio.create_task(automated_radar_monitor())

# ---------------------------------------------------------
# DODO PAYMENTS WEBHOOK
# ---------------------------------------------------------
def verify_dodo_signature(payload_bytes: bytes, headers: dict) -> bool:
    if not DODO_WEBHOOK_SECRET:
        return DEBUG_MODE

    msg_id = headers.get("webhook-id") or headers.get("x-webhook-id") or ""
    msg_timestamp = headers.get("webhook-timestamp") or headers.get("x-webhook-timestamp") or ""
    msg_signature = headers.get("webhook-signature") or headers.get("x-webhook-signature") or ""

    if not msg_signature:
        return False

    to_sign = f"{msg_id}.{msg_timestamp}.".encode("utf-8") + payload_bytes
    expected_sig = hmac.new(DODO_WEBHOOK_SECRET.encode("utf-8"), to_sign, hashlib.sha256).hexdigest()
    
    return hmac.compare_digest(expected_sig, msg_signature) or expected_sig in msg_signature

@app.post("/api/dodo-webhook")
async def dodo_webhook(request: Request):
    raw_body = await request.body()
    headers = dict(request.headers)

    if DODO_WEBHOOK_SECRET and not verify_dodo_signature(raw_body, headers):
        raise HTTPException(status_code=401, detail="Invalid webhook signature")

    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

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
        cursor.execute(format_query("UPDATE users SET is_pro = 1 WHERE lower(email) = ?"), (customer_email,))
        conn.commit()

        cursor.execute(format_query("SELECT telegram_chat_id FROM users WHERE lower(email) = ?"), (customer_email,))
        user_row = cursor.fetchone()

        if user_row and user_row["telegram_chat_id"]:
            pro_msg = (
                "🎉 <b>Gulf Disruption Radar | PRO Activated!</b>\n\n"
                "Your account is upgraded to <b>PRO</b>.\n"
                "• 50 daily corridor lookups & CSV export\n"
                "• Up to 20 tracked routes & individual flights\n"
                "• Instant state-change push alerts"
            )
            await send_telegram(user_row["telegram_chat_id"], pro_msg)

        if admin_chat:
            sale_msg = f"💰 <b>NEW GULF RADAR SALE!</b>\n\nCustomer: <code>{customer_email}</code>\nProvider: Dodo Payments"
            await send_telegram(admin_chat, sale_msg)

    elif event_type in ["subscription.cancelled", "subscription.expired"]:
        cursor.execute(format_query("UPDATE users SET is_pro = 0 WHERE lower(email) = ?"), (customer_email,))
        conn.commit()

    conn.close()
    return {"status": "success"}

# ---------------------------------------------------------
# CSV RAPOR DIŞA AKTARMA
# ---------------------------------------------------------
@app.get("/export-csv")
async def export_csv(request: Request):
    user = get_current_user(request)
    if not user:
        return RedirectResponse(url="/auth?mode=login", status_code=303)

    radar = parse_radar_data()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Time (GST)", "Flight Number", "Airline", "Destination", "IATA", "Terminal", "Gate", "Status", "Aircraft Model"])

    for fl in radar["all_flights"]:
        writer.writerow([
            fl["time"], fl["flight_num"], fl["airline"], fl["dest_name"],
            fl["dest_iata"], fl["terminal"], fl["gate"], fl["status"], fl["aircraft_model"]
        ])

    output.seek(0)
    today_utc = get_today_utc_str()
    filename = f"Gulf_Disruption_Report_{today_utc}.csv"
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"}
    )

# ---------------------------------------------------------
# PWA ENDPOINT'LERİ
# ---------------------------------------------------------
@app.get("/manifest.json")
async def get_manifest():
    return FileResponse(static_dir / "manifest.json", media_type="application/manifest+json")

@app.get("/sw.js")
async def get_sw():
    return FileResponse(static_dir / "sw.js", media_type="application/javascript")

# ---------------------------------------------------------
# ANA SAYFA & KOTA YÖNETİMİ
# ---------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
async def index(request: Request, q: Optional[str] = None):
    user = get_current_user(request)
    client_ip = request.client.host if request.client else "127.0.0.1"
    is_pro = bool(user and user.get("is_pro"))
    identifier = f"user_{user['id']}" if user else f"guest_{client_ip}"
    search_count = get_search_count(identifier)

    if is_pro:
        search_limit = PRO_DAILY_SEARCH_LIMIT
        search_limit_reached = search_count >= search_limit
        remaining_searches = max(0, search_limit - search_count)
        
        # PRO kullanıcı kotayı aşarsa otomatik duraklat ve admin'e bildir
        if search_limit_reached and search_count == search_limit:
            notify_admin_quota_exceeded(user["email"], search_count)
    else:
        search_limit = FREE_SEARCH_LIMIT
        search_limit_reached = search_count >= search_limit
        remaining_searches = max(0, search_limit - search_count)

    if q and not search_limit_reached:
        increment_search_count(identifier)
        search_count += 1
        remaining_searches = max(0, search_limit - search_count)
        search_limit_reached = search_count >= search_limit
        if is_pro and search_limit_reached:
            notify_admin_quota_exceeded(user["email"], search_count)

    radar_data = parse_radar_data(search_query=q or "")

    watchlist = []
    tracked_flights = []
    watchlist_limit_reached = False
    max_allowed_watchlist = PRO_MAX_WATCHLIST_LIMIT if is_pro else 3

    if user:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute(format_query("SELECT * FROM watchlist WHERE user_id = ? ORDER BY created_at DESC"), (user["id"],))
        watchlist = cursor.fetchall()

        cursor.execute(format_query("SELECT * FROM tracked_flights WHERE user_id = ? ORDER BY created_at DESC"), (user["id"],))
        tracked_flights = cursor.fetchall()
        conn.close()

        total_tracked = len(watchlist) + len(tracked_flights)
        if total_tracked >= max_allowed_watchlist:
            watchlist_limit_reached = True

    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "user": user,
            "query": q,
            "data": radar_data,
            "watchlist": watchlist,
            "tracked_flights": tracked_flights,
            "search_count": search_count,
            "search_limit": search_limit,
            "search_limit_reached": search_limit_reached,
            "remaining_searches": remaining_searches,
            "watchlist_limit_reached": watchlist_limit_reached,
            "is_pro": is_pro,
            "dodo_monthly_link": DODO_MONTHLY_LINK,
            "dodo_annual_link": DODO_ANNUAL_LINK,
            "max_allowed_watchlist": max_allowed_watchlist,
        }
    )

@app.post("/track")
async def track_route(request: Request, route_code: str = Form(...), destination_name: str = Form(...)):
    user = get_current_user(request)
    if not user:
        return RedirectResponse(url="/auth?mode=login", status_code=303)

    is_pro = bool(user.get("is_pro"))
    max_allowed = PRO_MAX_WATCHLIST_LIMIT if is_pro else 3

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(format_query("SELECT (SELECT COUNT(*) FROM watchlist WHERE user_id = ?) + (SELECT COUNT(*) FROM tracked_flights WHERE user_id = ?) as total"), (user["id"], user["id"]))
    total_count = cursor.fetchone()["total"]

    if total_count >= max_allowed:
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

    is_pro = bool(user.get("is_pro"))
    max_allowed = PRO_MAX_WATCHLIST_LIMIT if is_pro else 3

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(format_query("SELECT (SELECT COUNT(*) FROM watchlist WHERE user_id = ?) + (SELECT COUNT(*) FROM tracked_flights WHERE user_id = ?) as total"), (user["id"], user["id"]))
    total_count = cursor.fetchone()["total"]

    if total_count >= max_allowed:
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

@app.get("/sync")
async def sync_live(request: Request):
    user = get_current_user(request)
    if not user:
        return RedirectResponse(url="/auth?mode=login", status_code=303)
    fetch_live_airport_data("DXB")
    return RedirectResponse(url="/", status_code=303)

# ---------------------------------------------------------
# AUTH ROTALARI
# ---------------------------------------------------------
@app.get("/auth", response_class=HTMLResponse)
async def auth_page(request: Request, mode: Optional[str] = "register"):
    user = get_current_user(request)
    if user:
        return RedirectResponse(url="/", status_code=303)
    return templates.TemplateResponse(
        request=request, name="auth.html",
        context={"error": None, "message": None, "initial_tab": "register" if mode == "register" else "login"}
    )

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
    telegram_chat_id: str = Form(None)
):
    email_clean = email.strip().lower()
    chat_id_clean = telegram_chat_id.strip() if telegram_chat_id else None

    if chat_id_clean and not is_valid_telegram_chat_id(chat_id_clean):
        return templates.TemplateResponse(request=request, name="auth.html", context={"error": "Invalid Telegram numeric ID.", "message": None, "initial_tab": "register"})

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(format_query("SELECT id FROM users WHERE lower(email) = ?"), (email_clean,))
    if cursor.fetchone():
        conn.close()
        return templates.TemplateResponse(request=request, name="auth.html", context={"error": "This email is already registered.", "message": None, "initial_tab": "register"})

    pwd_hash = hash_password(password)
    is_pro_val = 1 if email_clean in ADMIN_EMAILS else 0

    if USE_PG:
        cursor.execute("INSERT INTO users (email, password_hash, telegram_chat_id, is_pro) VALUES (%s, %s, %s, %s) RETURNING id", (email_clean, pwd_hash, chat_id_clean, is_pro_val))
        user_id = cursor.fetchone()["id"]
    else:
        cursor.execute("INSERT INTO users (email, password_hash, telegram_chat_id, is_pro) VALUES (?, ?, ?, ?)", (email_clean, pwd_hash, chat_id_clean, is_pro_val))
        user_id = cursor.lastrowid

    session_id = secrets.token_hex(24)
    cursor.execute(format_query("INSERT INTO sessions (session_id, user_id) VALUES (?, ?)"), (session_id, user_id))
    conn.commit()
    conn.close()

    if chat_id_clean:
        await send_telegram(chat_id_clean, "👋 <b>Gulf Disruption Radar Active!</b>\n\nYour corridor & flight disruption alerts are linked successfully.")

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
        return templates.TemplateResponse(request=request, name="auth.html", context={"error": "Invalid email or password.", "message": None, "initial_tab": "login"})

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

# ---------------------------------------------------------
# SUPPORT DESK APISI
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