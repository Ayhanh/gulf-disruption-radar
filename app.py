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

from dotenv import load_dotenv
from fastapi import FastAPI, Form, HTTPException, Request
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

# Güvenlik: Varsayılan anahtar silindi, .env üzerinden yüklenmesi zorunlu kılındı.
RAPIDAPI_KEY = os.getenv("RAPIDAPI_KEY", "").strip()
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
APP_BASE_URL = os.getenv("APP_BASE_URL", "http://127.0.0.1:8000").rstrip("/")
DEBUG_MODE = os.getenv("DEBUG", "true").strip().lower() in {"1", "true", "yes", "on"}
ADMIN_EMAILS = [os.getenv("ADMIN_EMAIL", "ayhan.hakyemez@gmail.com").strip().lower()]

app = FastAPI(title="Gulf Disruption Radar")

static_dir = BASE_DIR / "static"
static_dir.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

# Global İzleme / Sağlık Değişkenleri (Admin Paneli İçin)
SYSTEM_METRICS = {
    "last_bot_run": "Henüz çalışmadı",
    "bot_status": "Active",
    "last_payment_time": "Henüz ödeme alınmadı",
    "last_webhook_status": "Normal",
    "sync_last_called": None,
    "telegram_sent_today": 0,
    "api_requests_this_month": 0
}

# ---------------------------------------------------------
# VERİTABANI YÖNETİMİ & MİGRATİON KORUMASI
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
            CREATE TABLE IF NOT EXISTS watchlist (
                id SERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                route_code TEXT NOT NULL,
                destination_name TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
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
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
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
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
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

    try:
        cursor.execute("ALTER TABLE users ADD COLUMN telegram_credits INTEGER DEFAULT 10;")
    except Exception:
        pass

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
# TELEGRAM BİLDİRİM MOTORU & KREDİ SİSTEMİ
# ---------------------------------------------------------
def _send_telegram_worker(chat_id: str, message: str):
    if not chat_id or not TELEGRAM_BOT_TOKEN:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": str(chat_id).strip(), "text": message, "parse_mode": "HTML"}
    try:
        requests.post(url, json=payload, timeout=8)
        SYSTEM_METRICS["telegram_sent_today"] += 1
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
# KULLANICI VE KOTA KONTROLLERİ
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
    if user_dict["email"].lower() in ADMIN_EMAILS:
        user_dict["is_pro"] = 1
        user_dict["is_admin"] = True
    else:
        user_dict["is_admin"] = False
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
# CANLI HAVALİMANI & FIDS MOTORU
# ---------------------------------------------------------
def fetch_live_airport_data(iata_code="DXB"):
    if not RAPIDAPI_KEY:
        return False, "RapidAPI Key tanımlanmamış."
    
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
        SYSTEM_METRICS["api_requests_this_month"] += 1
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
        call_sign = f.get("callSign", "-")
        is_cargo = f.get("isCargo", False)

        arr_info = f.get("arrival", {})
        dest_name = arr_info.get("airport", {}).get("name", "Bilinmiyor")
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
        ac_model = ac.get("model", "Belirtilmemiş")
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
# ARKA PLAN MONİTÖRÜ
# ---------------------------------------------------------
async def automated_radar_monitor():
    while True:
        try:
            fetch_live_airport_data("DXB")
            SYSTEM_METRICS["last_bot_run"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            SYSTEM_METRICS["bot_status"] = "Active"

            radar = parse_radar_data()
            current_flight_map = {fl["flight_num"]: fl for fl in radar["all_flights"]}

            conn = get_db()
            cursor = conn.cursor()

            cursor.execute("""
                SELECT tf.id, tf.user_id, tf.flight_num, tf.airline, tf.destination_name, tf.last_status, 
                       u.telegram_chat_id, u.is_pro, u.telegram_credits, u.created_at
                FROM tracked_flights tf
                JOIN users u ON tf.user_id = u.id
                WHERE u.telegram_chat_id IS NOT NULL
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
                    new_status = current_flight_map[f_num]["status"]

                    if new_status != old_status:
                        cursor.execute(format_query("UPDATE tracked_flights SET last_status = ? WHERE id = ?"), (new_status, tf["id"]))
                        conn.commit()

                        if new_status in ["Canceled", "Delayed"]:
                            fl_obj = current_flight_map[f_num]
                            
                            upsell_text = ""
                            if not is_pro:
                                new_credits = max(0, credits_left - 1)
                                cursor.execute(format_query("UPDATE users SET telegram_credits = ? WHERE id = ?"), (new_credits, tf["user_id"]))
                                conn.commit()
                                
                                if new_credits > 0:
                                    upsell_text = f"\n\n⚠️ Not: Ücretsiz üyeliğinizde 10 bildirim hakkınızdan {new_credits} tanesi kaldı. Kesintisiz takip için Pro Pakete geçin.\n🔗 {APP_BASE_URL}/#pricing"
                                else:
                                    upsell_text = f"\n\n🛑 Bildirim limitiniz doldu. Lütfen Pro Pakete yükseltin.\n🔗 {APP_BASE_URL}/#pricing"

                            alert_msg = (
                                "🚨 <b>GULF DISRUPTION RADAR | FLIGHT ALERT</b>\n\n"
                                f"✈️ <b>Flight:</b> <code>{f_num}</code> ({tf['airline']})\n"
                                f"📍 <b>Route:</b> DXB ➔ {tf['destination_name']}\n"
                                f"⚠️ <b>Status Changed:</b> <b>{new_status.upper()}</b> (Prev: {old_status})\n"
                                f"🕒 <b>Scheduled:</b> {fl_obj['time']} GST\n"
                                f"🚪 <b>Terminal/Gate:</b> T{fl_obj['terminal']} / G-{fl_obj['gate']}"
                                + upsell_text
                            )
                            await send_telegram(tf["telegram_chat_id"], alert_msg)

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

        cursor.execute(format_query("SELECT telegram_chat_id FROM users WHERE lower(email) = ?"), (customer_email,))
        user_row = cursor.fetchone()

        if user_row and user_row["telegram_chat_id"]:
            pro_msg = (
                "🎉 <b>Gulf Disruption Radar | PRO Activated!</b>\n\n"
                "Your account is upgraded to <b>PRO</b>.\n"
                "• Up to 100 corridor queries / day\n"
                "• Up to 50 tracked routes & individual flights\n"
                "• Instant state-change push alerts & CSV exports"
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
# 🛡️ ADMIN PANEL ROTASI (/admin)
# ---------------------------------------------------------
@app.get("/admin", response_class=HTMLResponse)
async def admin_panel(request: Request):
    user = get_current_user(request)
    if not user or not user.get("is_admin"):
        raise HTTPException(status_code=404, detail="Not Found")

    conn = get_db()
    cursor = conn.cursor()

    # Kullanıcı Metrikleri
    cursor.execute("SELECT COUNT(*) as cnt FROM users")
    total_users = cursor.fetchone()["cnt"]

    cursor.execute("SELECT COUNT(*) as cnt FROM users WHERE is_pro = 1")
    pro_users = cursor.fetchone()["cnt"]

    free_users = total_users - pro_users
    free_slots_left = max(0, 10 - free_users)
    mrr = pro_users * 29.90
    conversion_rate = round((pro_users / total_users * 100), 1) if total_users > 0 else 0.0

    # Watchlist & Flight Takip Sayıları
    cursor.execute("SELECT COUNT(*) as cnt FROM watchlist")
    total_watchlist = cursor.fetchone()["cnt"]

    cursor.execute("SELECT COUNT(*) as cnt FROM tracked_flights")
    total_tracked_flights = cursor.fetchone()["cnt"]

    # Kredisi Bitenler
    cursor.execute("SELECT email, telegram_credits, created_at FROM users WHERE is_pro = 0 AND telegram_credits <= 0")
    exhausted_credits_users = cursor.fetchall()

    # Aktif Oturum Sayısı
    cursor.execute("SELECT COUNT(DISTINCT session_id) as cnt FROM sessions")
    active_sessions = cursor.fetchone()["cnt"]

    # Son Kayıt Olan Kullanıcı
    cursor.execute("SELECT email, created_at FROM users ORDER BY id DESC LIMIT 1")
    last_user_row = cursor.fetchone()
    last_registered_email = last_user_row["email"] if last_user_row else "Henüz yok"

    conn.close()

    # API İsteği & Kota Oranı
    api_limit = 1600
    api_used = SYSTEM_METRICS["api_requests_this_month"]
    api_pct = round((api_used / api_limit * 100), 1) if api_limit > 0 else 0

    api_color = "emerald"
    if api_pct >= 90:
        api_color = "rose"
    elif api_pct >= 70:
        api_color = "amber"

    # Cache Zamanı
    cache_time_str = "Bilinmiyor"
    if DATA_CACHE_FILE.exists():
        m_time = datetime.fromtimestamp(DATA_CACHE_FILE.stat().st_mtime)
        diff_mins = int((datetime.now() - m_time).total_seconds() / 60)
        cache_time_str = f"{diff_mins} dakika önce"

    html_content = f"""
    <!DOCTYPE html>
    <html lang="tr">
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

            <!-- 1. API & KOTA YÖNETİMİ -->
            <section class="bg-slate-900 border border-slate-800 rounded-2xl p-6 space-y-4">
                <h2 class="text-xs font-bold uppercase tracking-wider text-sky-400">📊 1. API Maliyet & Kota Yönetimi</h2>
                <div class="grid grid-cols-1 md:grid-cols-4 gap-4">
                    <div class="bg-slate-950 p-4 rounded-xl border border-slate-800">
                        <p class="text-[11px] text-slate-400 font-medium">Aylık API İsteği</p>
                        <p class="text-2xl font-extrabold text-white mt-1">{api_used} <span class="text-xs font-normal text-slate-400">/ {api_limit}</span></p>
                        <div class="mt-3 w-full bg-slate-800 rounded-full h-2">
                            <div class="bg-{api_color}-500 h-2 rounded-full" style="width: {min(api_pct, 100)}%;"></div>
                        </div>
                        <p class="text-[10px] text-{api_color}-400 font-semibold mt-1">Doluluk: %{api_pct} ({'Normal' if api_pct < 70 else ('Sarı Uyarı' if api_pct < 90 else 'KIRMIZI UYARI!')})</p>
                    </div>
                    <div class="bg-slate-950 p-4 rounded-xl border border-slate-800">
                        <p class="text-[11px] text-slate-400 font-medium">Bugün Telegram Bildirimi</p>
                        <p class="text-2xl font-extrabold text-emerald-400 mt-1">{SYSTEM_METRICS['telegram_sent_today']}</p>
                        <p class="text-[10px] text-slate-400 mt-2">1 birim = 1 push mesajı</p>
                    </div>
                    <div class="bg-slate-950 p-4 rounded-xl border border-slate-800">
                        <p class="text-[11px] text-slate-400 font-medium">Arka Plan Bot Durumu</p>
                        <div class="flex items-center gap-2 mt-2">
                            <span class="w-3 h-3 rounded-full bg-emerald-500 animate-pulse"></span>
                            <span class="text-xs font-bold text-white">{SYSTEM_METRICS['bot_status']}</span>
                        </div>
                        <p class="text-[10px] text-slate-400 mt-1">Son Çalışma: {SYSTEM_METRICS['last_bot_run']}</p>
                    </div>
                    <div class="bg-slate-950 p-4 rounded-xl border border-slate-800">
                        <p class="text-[11px] text-slate-400 font-medium">Cache Güncellenmesi</p>
                        <p class="text-lg font-bold text-white mt-1">{cache_time_str}</p>
                        <p class="text-[10px] text-slate-400 mt-1">AeroDataBox FIDS verisi</p>
                    </div>
                </div>
            </section>

            <!-- 2. FİNANS VE KULLANICI METRİKLERİ -->
            <section class="bg-slate-900 border border-slate-800 rounded-2xl p-6 space-y-4">
                <h2 class="text-xs font-bold uppercase tracking-wider text-emerald-400">💵 2. Finans & Kullanıcı Metrikleri</h2>
                <div class="grid grid-cols-1 sm:grid-cols-2 md:grid-cols-5 gap-4">
                    <div class="bg-slate-950 p-4 rounded-xl border border-slate-800">
                        <p class="text-[11px] text-slate-400">Toplam Kullanıcı</p>
                        <p class="text-2xl font-bold text-white mt-1">{total_users} <span class="text-xs font-normal text-slate-400">({free_users} Free / {pro_users} Pro)</span></p>
                    </div>
                    <div class="bg-slate-950 p-4 rounded-xl border border-slate-800">
                        <p class="text-[11px] text-slate-400">Kurucu Üye Kontenjanı</p>
                        <p class="text-2xl font-bold text-sky-400 mt-1">{free_slots_left} <span class="text-xs font-normal text-slate-400">/ 10 kaldı</span></p>
                    </div>
                    <div class="bg-slate-950 p-4 rounded-xl border border-slate-800">
                        <p class="text-[11px] text-slate-400">Tahmini Gelir (MRR)</p>
                        <p class="text-2xl font-bold text-emerald-400 mt-1">${mrr:.2f}</p>
                    </div>
                    <div class="bg-slate-950 p-4 rounded-xl border border-slate-800">
                        <p class="text-[11px] text-slate-400">Dönüşüm Oranı (Conv.)</p>
                        <p class="text-2xl font-bold text-purple-400 mt-1">%{conversion_rate}</p>
                    </div>
                    <div class="bg-slate-950 p-4 rounded-xl border border-slate-800">
                        <p class="text-[11px] text-slate-400">Aktif Oturum (Session)</p>
                        <p class="text-2xl font-bold text-amber-400 mt-1">{active_sessions}</p>
                    </div>
                </div>
            </section>

            <!-- 3. OPERASYONEL YÜK & SAĞLIK -->
            <div class="grid grid-cols-1 md:grid-cols-2 gap-6">
                <section class="bg-slate-900 border border-slate-800 rounded-2xl p-6 space-y-4">
                    <h2 class="text-xs font-bold uppercase tracking-wider text-amber-400">📈 3. Operasyonel Yük & Altyapı</h2>
                    <ul class="space-y-3 text-xs text-slate-300">
                        <li class="flex justify-between bg-slate-950 p-3 rounded-xl border border-slate-800">
                            <span>Toplam Takip Edilen Rota (Watchlist):</span>
                            <span class="font-bold text-white">{total_watchlist} Rota</span>
                        </li>
                        <li class="flex justify-between bg-slate-950 p-3 rounded-xl border border-slate-800">
                            <span>Toplam Bireysel Takip Edilen Uçuş:</span>
                            <span class="font-bold text-white">{total_tracked_flights} Uçuş</span>
                        </li>
                        <li class="flex justify-between bg-slate-950 p-3 rounded-xl border border-slate-800">
                            <span>Render Sunucu Durumu (Uptime):</span>
                            <span class="font-bold text-emerald-400">🟢 Active / Keep-Alive OK</span>
                        </li>
                        <li class="flex justify-between bg-slate-950 p-3 rounded-xl border border-slate-800">
                            <span>Dodo Payments Son Ödeme / Webhook:</span>
                            <span class="font-bold text-white">{SYSTEM_METRICS['last_payment_time']} ({SYSTEM_METRICS['last_webhook_status']})</span>
                        </li>
                        <li class="flex justify-between bg-slate-950 p-3 rounded-xl border border-slate-800">
                            <span>En Son Kayıt Olan Üye:</span>
                            <span class="font-bold font-mono text-sky-400">{last_registered_email}</span>
                        </li>
                    </ul>
                </section>

                <!-- 4. POTANSİYEL PRO MÜŞTERİLERİ (KREDİSİ BİTENLER) -->
                <section class="bg-slate-900 border border-slate-800 rounded-2xl p-6 space-y-4">
                    <h2 class="text-xs font-bold uppercase tracking-wider text-rose-400">🎯 4. Satış Fırsatı: Kredisi Biten Üyeler</h2>
                    <p class="text-xs text-slate-400">Aşağıdaki ücretsiz kullanıcıların bildirim kredisi tükenmiştir. Onlara özel e-posta atarak Pro pakete geçirebilirsiniz:</p>
                    
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
                                    <td colspan="3" class="p-4 text-center text-slate-500 italic">Henüz kredisi biten kullanıcı yok.</td>
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
# CSV RAPOR DIŞA AKTARMA (PRO)
# ---------------------------------------------------------
@app.get("/export-csv")
async def export_csv(request: Request):
    user = get_current_user(request)
    if not user:
        return RedirectResponse(url="/auth?mode=login", status_code=303)
    
    if not user.get("is_pro"):
        return RedirectResponse(url="/?limit_reached=1", status_code=303)

    radar = parse_radar_data()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Time", "Flight Number", "Airline", "Destination", "IATA", "Terminal", "Gate", "Status", "Aircraft Model"])

    for fl in radar["all_flights"]:
        writer.writerow([
            fl["time"], fl["flight_num"], fl["airline"], fl["dest_name"],
            fl["dest_iata"], fl["terminal"], fl["gate"], fl["status"], fl["aircraft_model"]
        ])

    output.seek(0)
    filename = f"Gulf_Disruption_Report_{date.today().isoformat()}.csv"
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
# ANA SAYFA & KOTA KONTROLLERİ
# ---------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
async def index(request: Request, q: Optional[str] = None):
    user = get_current_user(request)
    client_ip = request.client.host if request.client else "127.0.0.1"
    is_pro = bool(user and user.get("is_pro"))
    identifier = f"user_{user['id']}" if user else f"guest_{client_ip}"
    
    daily_search_count = get_daily_search_count(identifier)

    if is_pro:
        search_limit = 100
        search_limit_reached = False
        remaining_searches = "100/day"
    else:
        search_limit = 5
        search_limit_reached = daily_search_count >= search_limit
        remaining_searches = max(0, search_limit - daily_search_count)

    if q and not search_limit_reached:
        increment_search_count(identifier)
        if not is_pro:
            daily_search_count += 1
            remaining_searches = max(0, search_limit - daily_search_count)
            search_limit_reached = daily_search_count >= search_limit

    radar_data = parse_radar_data(search_query=q or "")

    watchlist = []
    tracked_flights = []
    watchlist_limit_reached = False
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

    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
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
    )

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
# 🛡️ /sync BUTONU (Saatte 1 Kez Çalışma Sınırı / Rate Limit)
# ---------------------------------------------------------
@app.get("/sync")
async def sync_live(request: Request):
    user = get_current_user(request)
    if not user:
        return RedirectResponse(url="/auth?mode=login", status_code=303)

    now = datetime.now()
    last_called = SYSTEM_METRICS.get("sync_last_called")

    # Admin e-postaları saatlik sınırdan muaftır, ancak normal kullanıcılar için 1 saat kısıtı vardır.
    if last_called and not user.get("is_admin"):
        diff_mins = (now - last_called).total_seconds() / 60
        if diff_mins < 60:
            remaining_mins = int(60 - diff_mins)
            raise HTTPException(
                status_code=429, 
                detail=f"Rate limit exceeded. /sync can only be called once per hour. Please wait {remaining_mins} minutes."
            )

    SYSTEM_METRICS["sync_last_called"] = now
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
        context={"error": None, "message": None, "initial_tab": "register" if mode == "register" else "login", "free_slots_left": max(0, 10 - get_free_user_count())}
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
        return templates.TemplateResponse(request=request, name="auth.html", context={"error": "Invalid Telegram numeric ID.", "message": None, "initial_tab": "register", "free_slots_left": max(0, 10 - get_free_user_count())})

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(format_query("SELECT id FROM users WHERE lower(email) = ?"), (email_clean,))
    if cursor.fetchone():
        conn.close()
        return templates.TemplateResponse(request=request, name="auth.html", context={"error": "This email is already registered.", "message": None, "initial_tab": "register", "free_slots_left": max(0, 10 - get_free_user_count())})

    free_count = get_free_user_count()
    is_pro_val = 1 if email_clean in ADMIN_EMAILS else 0

    if free_count >= 10 and is_pro_val == 0:
        conn.close()
        return templates.TemplateResponse(request=request, name="auth.html", context={"error": "Founding Member Free Tier quota (10 users) is full. Please subscribe to Standard Pro.", "message": None, "initial_tab": "register", "free_slots_left": 0})

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

    # 10. ÜYE KAYDOLDUĞUNDA ADMIN'E ÖZEL TELEGRAM UYARISI
    if free_count + 1 == 10 and is_pro_val == 0:
        admin_chat = os.getenv("MY_TELEGRAM_ID", "").strip()
        if admin_chat:
            alert_admin_msg = "🚨 <b>ÜCRETLİ PAKETE GEÇ ZAMANI!</b>\n\n10. Ücretsiz Founding Member üyesi kayıt oldu! Kontenjan tamamen doldu."
            await send_telegram(admin_chat, alert_admin_msg)

    if chat_id_clean:
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
        return templates.TemplateResponse(request=request, name="auth.html", context={"error": "Invalid email or password.", "message": None, "initial_tab": "login", "free_slots_left": max(0, 10 - get_free_user_count())})

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