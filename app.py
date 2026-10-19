import os
import sqlite3
import requests
from datetime import datetime, timezone, timedelta
from fastapi import FastAPI, Request, Form, HTTPException, Depends
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

app = FastAPI()

# Static & Templates
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

DB_FILE = "database.db"

# Admin & SMTP Settings
ADMIN_EMAILS = ["ayhan.hakyemez@gmail.com"]
SMTP_SERVER = os.getenv("SMTP_SERVER", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "ayhan.hakyemez@gmail.com")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
SMTP_FROM = os.getenv("SMTP_FROM", "ayhan.hakyemez@gmail.com")

# In-memory tracking for User Sync limits and Monitor Health
user_sync_limits = {}  # {user_id: timestamp}
last_monitor_run_time = datetime.now(timezone.utc)

def send_email_alert(subject: string, body: string):
    """Sends email notifications via SMTP to the admin."""
    if not SMTP_PASSWORD:
        print(f"[SMTP WARNING] Password not set. Email skipped: {subject}")
        return
    try:
        msg = MIMEMultipart()
        msg['From'] = SMTP_FROM
        msg['To'] = ADMIN_EMAILS[0]
        msg['Subject'] = f"🚨 GULF RADAR: {subject}"
        msg.attach(MIMEText(body, 'plain'))

        server = smtplib.SMTP(SMTP_SERVER, SMTP_PORT)
        server.starttls()
        server.login(SMTP_USER, SMTP_PASSWORD)
        server.sendmail(SMTP_FROM, ADMIN_EMAILS[0], msg.as_string())
        server.quit()
        print(f"[SMTP SUCCESS] Email sent: {subject}")
    except Exception as e:
        print(f"[SMTP ERROR] Failed to send email: {e}")

def init_db():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT UNIQUE,
            password TEXT,
            is_pro INTEGER DEFAULT 0,
            telegram_credits INTEGER DEFAULT 10,
            created_at TEXT
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS watchlist (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            route_code TEXT,
            destination_name TEXT,
            FOREIGN KEY(user_id) REFERENCES users(id)
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS tracked_flights (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            flight_num TEXT,
            airline TEXT,
            destination_name TEXT,
            scheduled_time TEXT,
            last_status TEXT,
            FOREIGN KEY(user_id) REFERENCES users(id)
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS app_metrics (
            key TEXT PRIMARY KEY,
            value TEXT,
            updated_at TEXT
        )
    """)
    conn.commit()
    conn.close()

init_db()

def get_metric(key, default="0"):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT value FROM app_metrics WHERE key = ?", (key,))
    row = cursor.fetchone()
    conn.close()
    return row[0] if row else default

def set_metric(key, value):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    now = datetime.now(timezone.utc).isoformat()
    cursor.execute("INSERT OR REPLACE INTO app_metrics (key, value, updated_at) VALUES (?, ?, ?)", (key, str(value), now))
    conn.commit()
    conn.close()

def check_and_reset_counters():
    """Checks date/month boundaries and automatically resets API and Telegram usage counters."""
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    current_month_str = datetime.now(timezone.utc).strftime("%Y-%m")

    last_day = get_metric("last_reset_day", today_str)
    last_month = get_metric("last_reset_month", current_month_str)

    if last_day != today_str:
        set_metric("telegram_sent_today", 0)
        set_metric("last_reset_day", today_str)

    if last_month != current_month_str:
        set_metric("api_requests_this_month", 0)
        set_metric("last_reset_month", current_month_str)
        set_metric("api_70_alert_sent", "0")
        set_metric("api_90_alert_sent", "0")

check_and_reset_counters()

def increment_api_counter():
    check_and_reset_counters()
    current = int(get_metric("api_requests_this_month", "0")) + 1
    set_metric("api_requests_this_month", current)

    # API Threshold Checks (1,120 = 70% and 1,440 = 90% of 1,600 RapidAPI limit)
    if current >= 1120 and get_metric("api_70_alert_sent", "0") == "0":
        send_email_alert("API Kotası Uyarısı (%70 Doldu)", f"Aylık API kullanımı 1,120 eşiğini aştı. Toplam Kullanım: {current}/1600")
        set_metric("api_70_alert_sent", "1")
    if current >= 1440 and get_metric("api_90_alert_sent", "0") == "0":
        send_email_alert("KRİTİK: API Kotası Dolmak Üzere (%90 Doldu)", f"Aylık API kullanımı 1,440 eşiğini aştı! Toplam Kullanım: {current}/1600")
        set_metric("api_90_alert_sent", "1")

# Helper to get current logged-in user from cookies
def get_current_user(request: Request):
    user_email = request.cookies.get("user_session")
    if not user_email:
        return None
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM users WHERE email = ?", (user_email,))
    user = cursor.fetchone()
    conn.close()
    if user:
        u_dict = dict(user)
        u_dict["is_admin"] = u_dict["email"].strip().lower() in [e.lower() for e in ADMIN_EMAILS]
        return u_dict
    return None

@app.get("/", response_class=HTMLResponse)
async def home(request: Request, q: str = None):
    user = get_current_user(request)
    is_pro = 1 if (user and (user["is_pro"] == 1 or user["is_admin"])) else 0

    # Free slots calculation (10 lifetime seats)
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM users")
    total_users = cursor.fetchone()[0]
    conn.close()
    free_slots_left = max(0, 10 - total_users)

    # Simulated Flight Data & AeroDataBox integration
    flights_data = [
        {"time": "08:15", "flight_num": "FZ-821", "airline": "flydubai Cargo", "dest_name": "Riyadh", "dest_iata": "RUH", "terminal": "2", "gate": "14", "status": "Canceled", "call_sign": "FDB821", "aircraft_model": "Boeing 737-800", "aircraft_reg": "A6-FEG", "is_cargo": True},
        {"time": "09:30", "flight_num": "SV-552", "airline": "Saudia Cargo", "dest_name": "Riyadh", "dest_iata": "RUH", "terminal": "1", "gate": "22", "status": "Expected", "call_sign": "SVA552", "aircraft_model": "Airbus A330", "aircraft_reg": "HZ-AQ4", "is_cargo": True},
        {"time": "11:00", "flight_num": "EK-813", "airline": "Emirates", "dest_name": "Dammam", "dest_iata": "DMM", "terminal": "3", "gate": "B8", "status": "Delayed", "call_sign": "UAE813", "aircraft_model": "Boeing 777-300ER", "aircraft_reg": "A6-ECQ", "is_cargo": False},
        {"time": "12:45", "flight_num": "GF-512", "airline": "Gulf Air", "dest_name": "Jeddah", "dest_iata": "JED", "terminal": "1", "gate": "05", "status": "Expected", "call_sign": "GFA512", "aircraft_model": "Airbus A320neo", "aircraft_reg": "A9C-NB", "is_cargo": False},
        {"time": "14:20", "flight_num": "FZ-825", "airline": "flydubai", "dest_name": "Riyadh", "dest_iata": "RUH", "terminal": "2", "gate": "19", "status": "Canceled", "call_sign": "FDB825", "aircraft_model": "Boeing 737 MAX 8", "aircraft_reg": "A6-MAX", "is_cargo": False},
        {"time": "15:10", "flight_num": "QR-104", "airline": "Qatar Airways", "dest_name": "Doha", "dest_iata": "DOH", "terminal": "1", "gate": "C2", "status": "Expected", "call_sign": "QTR104", "aircraft_model": "Boeing 777", "aircraft_reg": "A7-BAA", "is_cargo": False},
        {"time": "16:40", "flight_num": "SV-556", "airline": "Saudia", "dest_name": "Riyadh", "dest_iata": "RUH", "terminal": "1", "gate": "20", "status": "Delayed", "call_sign": "SVA556", "aircraft_model": "Airbus A321", "aircraft_reg": "HZ-AS1", "is_cargo": False},
        {"time": "18:00", "flight_num": "EK-823", "airline": "Emirates Cargo", "dest_name": "Jeddah", "dest_iata": "JED", "terminal": "3", "gate": "B12", "status": "Canceled", "call_sign": "UAE823", "aircraft_model": "Boeing 777F", "aircraft_reg": "A6-EFR", "is_cargo": True},
        {"time": "19:30", "flight_num": "G9-312", "airline": "Air Arabia", "dest_name": "Dammam", "dest_iata": "DMM", "terminal": "2", "gate": "08", "status": "Expected", "call_sign": "ABY312", "aircraft_model": "Airbus A320", "aircraft_reg": "A6-ATC", "is_cargo": False},
    ]

    # Filter if search query exists
    filtered_flights = flights_data
    if q:
        q_lower = q.strip().lower()
        filtered_flights = [f for f in flights_data if q_lower in f['flight_num'].lower() or q_lower in f['dest_name'].lower() or q_lower in f['dest_iata'].lower() or q_lower in f['airline'].lower()]
        increment_api_counter()

    data_summary = {
        "total": len(flights_data),
        "canceled": len([f for f in flights_data if f['status'] == 'Canceled']),
        "delayed": len([f for f in flights_data if f['status'] == 'Delayed']),
        "all_flights": flights_data,
        "top_8_flights": filtered_flights[:8],
        "critical_routes": [
            {"iata": "RUH", "name": "Riyadh (King Khalid)", "risk_level": "CRITICAL", "canceled": 3, "total": 4},
            {"iata": "DMM", "name": "Dammam (King Fahd)", "risk_level": "WARNING", "canceled": 1, "total": 3},
            {"iata": "JED", "name": "Jeddah (King Abdulaziz)", "risk_level": "WARNING", "canceled": 2, "total": 2}
        ]
    }

    watchlist = []
    tracked_flights = []
    if user:
        conn = sqlite3.connect(DB_FILE)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM watchlist WHERE user_id = ?", (user["id"],))
        watchlist = [dict(row) for row in cursor.fetchall()]
        cursor.execute("SELECT * FROM tracked_flights WHERE user_id = ?", (user["id"],))
        tracked_flights = [dict(row) for row in cursor.fetchall()]
        conn.close()

    return templates.TemplateResponse("index.html", {
        "request": request,
        "user": user,
        "is_pro": is_pro,
        "free_slots_left": free_slots_left,
        "data": data_summary,
        "watchlist": watchlist,
        "tracked_flights": tracked_flights,
        "query": q,
        "search_count": 2,
        "search_limit": 5 if not is_pro else 100,
        "search_limit_reached": False,
        "watchlist_limit_reached": False,
        "remaining_searches": 3
    })

@app.get("/auth", response_class=HTMLResponse)
async def auth_page(request: Request, mode: str = "login"):
    return templates.TemplateResponse("auth.html", {"request": request, "mode": mode})

@app.post("/auth/login")
async def login(email: str = Form(...), password: str = Form(...)):
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM users WHERE email = ? AND password = ?", (email.strip().lower(), password))
    user = cursor.fetchone()
    conn.close()

    if user:
        response = RedirectResponse(url="/", status_code=303)
        response.set_cookie(key="user_session", value=user["email"], httponly=True)
        return response
    return RedirectResponse(url="/auth?mode=login&error=1", status_code=303)

@app.post("/auth/register")
async def register(email: str = Form(...), password: str = Form(...)):
    clean_email = email.strip().lower()
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    try:
        cursor.execute("INSERT INTO users (email, password, created_at) VALUES (?, ?, ?)", 
                       (clean_email, password, datetime.now(timezone.utc).isoformat()))
        conn.commit()
        
        # Send Email Notification to Admin for New User Registration
        send_email_alert("Yeni Kullanıcı Kaydı", f"Sisteme yeni bir kullanıcı kayıt oldu:\nE-posta: {clean_email}")

    except sqlite3.IntegrityError:
        pass
    conn.close()

    response = RedirectResponse(url="/", status_code=303)
    response.set_cookie(key="user_session", value=clean_email, httponly=True)
    return response

@app.get("/auth/logout")
async def logout():
    response = RedirectResponse(url="/", status_code=303)
    response.delete_cookie(key="user_session")
    return response

# 🛡️ /sync Route: Only for PRO users, individual user-based rate limit (1 hour per user)
@app.get("/sync")
async def sync_hubs(request: Request):
    user = get_current_user(request)
    if not user:
        return RedirectResponse(url="/auth?mode=login", status_code=303)
    
    # Check if user is Pro or Admin
    if not user["is_pro"] and not user["is_admin"]:
        raise HTTPException(status_code=403, detail="Bu özellik yalnızca Standard Pro üyeler içindir.")

    user_id = user["id"]
    now = datetime.now(timezone.utc)
    
    if user_id in user_sync_limits:
        last_called = user_sync_limits[user_id]
        if (now - last_called).total_seconds() < 3600:
            raise HTTPException(status_code=429, detail="Rate limit exceeded. You can sync once per hour.")

    user_sync_limits[user_id] = now
    increment_api_counter()
    return RedirectResponse(url="/?synced=1", status_code=303)

@app.post("/track")
async def track_route(request: Request, route_code: str = Form(...), destination_name: str = Form(...)):
    user = get_current_user(request)
    if not user:
        return RedirectResponse(url="/auth", status_code=303)

    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("INSERT INTO watchlist (user_id, route_code, destination_name) VALUES (?, ?, ?)", 
                   (user["id"], route_code, destination_name))
    conn.commit()
    conn.close()
    return RedirectResponse(url="/", status_code=303)

@app.post("/untrack")
async def untrack_route(request: Request, item_id: int = Form(...)):
    user = get_current_user(request)
    if not user:
        return RedirectResponse(url="/auth", status_code=303)

    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM watchlist WHERE id = ? AND user_id = ?", (item_id, user["id"]))
    conn.commit()
    conn.close()
    return RedirectResponse(url="/", status_code=303)

@app.post("/track-flight")
async def track_flight(request: Request, flight_num: str = Form(...), airline: str = Form(...), destination_name: str = Form(...), scheduled_time: str = Form(...), status: str = Form(...)):
    user = get_current_user(request)
    if not user:
        return RedirectResponse(url="/auth", status_code=303)

    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("INSERT INTO tracked_flights (user_id, flight_num, airline, destination_name, scheduled_time, last_status) VALUES (?, ?, ?, ?, ?, ?)", 
                   (user["id"], flight_num, airline, destination_name, scheduled_time, status))
    conn.commit()
    conn.close()
    return RedirectResponse(url="/", status_code=303)

@app.post("/untrack-flight")
async def untrack_flight(request: Request, flight_id: int = Form(...)):
    user = get_current_user(request)
    if not user:
        return RedirectResponse(url="/auth", status_code=303)

    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM tracked_flights WHERE id = ? AND user_id = ?", (flight_id, user["id"]))
    conn.commit()
    conn.close()
    return RedirectResponse(url="/", status_code=303)

@app.post("/api/dodo-webhook")
async def dodo_webhook(payload: dict):
    """Dodo Payments webhook for payment success verification."""
    event_type = payload.get("type") or payload.get("event_type")
    if event_type == "payment.succeeded" or payload.get("status") == "succeeded":
        data = payload.get("data", {})
        customer = data.get("customer", {})
        email = customer.get("email") or data.get("email")
        
        if email:
            clean_email = email.strip().lower()
            conn = sqlite3.connect(DB_FILE)
            cursor = conn.cursor()
            cursor.execute("UPDATE users SET is_pro = 1 WHERE email = ?", (clean_email,))
            conn.commit()
            conn.close()
            
            # Send Email Notification for Pro Sale
            send_email_alert("Yeni Pro Satışı (+$29.90)", f"Başarılı Pro abonelik satışı gerçekleşti!\nMüşteri E-posta: {clean_email}")
            
    return {"status": "received"}

# 🛡️ /admin Dashboard (Restricted exclusively to admin)
@app.get("/admin", response_class=HTMLResponse)
async def admin_dashboard(request: Request):
    user = get_current_user(request)
    if not user or not user["is_admin"]:
        raise HTTPException(status_code=404, detail="Not Found")

    check_and_reset_counters()
    api_requests = int(get_metric("api_requests_this_month", "0"))
    telegram_sent = int(get_metric("telegram_sent_today", "0"))

    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM users")
    total_users = cursor.fetchone()[0]
    cursor.execute("SELECT COUNT(*) FROM users WHERE is_pro = 1")
    pro_users = cursor.fetchone()[0]
    cursor.execute("SELECT email, telegram_credits FROM users WHERE telegram_credits <= 2")
    exhausted_users = [dict(row) for row in cursor.fetchall()]
    conn.close()

    # Dynamic Keep-Alive status calculation
    global last_monitor_run_time
    now_utc = datetime.now(timezone.utc)
    diff_minutes = int((now_utc - last_monitor_run_time).total_seconds() / 60)
    server_status = "Active / Keep-Alive OK" if diff_minutes < 45 else f"Warning: Last run {diff_minutes} min ago"

    return templates.TemplateResponse("admin.html", {
        "request": request,
        "user": user,
        "api_requests": api_requests,
        "telegram_sent": telegram_sent,
        "total_users": total_users,
        "pro_users": pro_users,
        "free_users": total_users - pro_users,
        "free_slots_left": max(0, 10 - total_users),
        "exhausted_users": exhausted_users,
        "server_status": server_status
    })

def automated_radar_monitor():
    """Background monitoring job that checks credit limits and triggers admin email alerts."""
    global last_monitor_run_time
    last_monitor_run_time = datetime.now(timezone.utc)

    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("SELECT email, telegram_credits FROM users WHERE telegram_credits = 0")
    zero_credit_users = cursor.fetchall()
    conn.close()

    for u in zero_credit_users:
        # Check if we already alerted or trigger email alert
        send_email_alert("Kullanıcı Kredisi Bitti", f"Ücretsiz kullanıcının Telegram kredisi tükendi:\nKullanıcı: {u['email']}")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.py", host="0.0.0.0", port=10000, reload=True)