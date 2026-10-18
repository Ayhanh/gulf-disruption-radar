import asyncio
import csv
from datetime import date, datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
import hashlib
import hmac
import io
import os
from pathlib import Path
import re
import secrets
import smtplib
from typing import Optional

from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
import requests
import sqlite3

# ---------------------------------------------------------
# KONFİGÜRASYON & SABİT DEĞİŞKENLER
# ---------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")
DB_FILE = BASE_DIR / "companies.db"
BASE_URL = "https://api.company-information.service.gov.uk"

COMPANIES_HOUSE_API_KEY = os.getenv("COMPANIES_HOUSE_API_KEY", "").strip()
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
SECRET_KEY = os.getenv("SECRET_KEY", "").strip()
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
APP_BASE_URL = os.getenv("APP_BASE_URL", "https://uk-company-radar.onrender.com").rstrip("/")
DEBUG_MODE = os.getenv("DEBUG", "false").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}

# DODO PAYMENTS / ÖDEME LİNKLERİ (.env üzerinden)
DODO_PRO_MONTHLY = os.getenv("DODO_PRO_MONTHLY", "https://checkout.dodopayments.com/buy/pdt_0NpeP9HBH35DX4vKzdC6Y?quantity=1")
DODO_PRO_ANNUAL = os.getenv("DODO_PRO_ANNUAL", "https://checkout.dodopayments.com/buy/pdt_0NpePTmT2ndnjwe2mU0uY?quantity=1")
DODO_GOLD_MONTHLY = os.getenv("DODO_GOLD_MONTHLY", "https://checkout.dodopayments.com/buy/pdt_0Nq25oKX07UrcVGce0Mic?quantity=1")
DODO_GOLD_ANNUAL = os.getenv("DODO_GOLD_ANNUAL", "https://checkout.dodopayments.com/buy/pdt_0Nq265Wntokc3Gz6tRp2N?quantity=1")

# SMTP Ayarları
SMTP_SERVER = os.getenv("SMTP_SERVER", "").strip()
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "").strip()
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "").strip()
SMTP_FROM = os.getenv("SMTP_FROM", "support@ukcompanyradar.com").strip()

# Lemon Squeezy Signing Secret
LEMON_WEBHOOK_SECRET = os.getenv(
    "LEMON_WEBHOOK_SECRET", "uk_radar_secret_2026"
).strip()

# Dodo Payments Webhook Secret
DODO_WEBHOOK_SECRET = os.getenv("DODO_WEBHOOK_SECRET", "").strip()

ADMIN_EMAILS = [
    os.getenv("ADMIN_EMAIL", "ayhan.hakyemez@gmail.com").strip().lower()
]

# PLAN KATMANLARI VE TAKİP LİMİTLERİ
# 0 = Free (3), 1 = Pro (50), 2 = Gold (100)
PLAN_LIMITS = {
    0: 3,
    1: 50,
    2: 100
}

# FastAPI Başlatma & Statik Klasör Bağlama
app = FastAPI(title="UK Company Radar")

static_dir = BASE_DIR / "static"
static_dir.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

# ---------------------------------------------------------
# VERİTABANI YÖNETİMİ (POSTGRESQL / SQLITE HİBRİT)
# ---------------------------------------------------------
USE_PG = bool(DATABASE_URL)
if USE_PG:
    import psycopg2
    import psycopg2.extras
    if DATABASE_URL.startswith("postgres://"):
        DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)


def get_db():
    if USE_PG:
        conn = psycopg2.connect(
            DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor
        )
        return conn
    else:
        conn = sqlite3.connect(DB_FILE)
        conn.row_factory = sqlite3.Row
        return conn


def format_query(sql: str) -> str:
    if USE_PG:
        return sql.replace("?", "%s")
    return sql


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
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS watchlist (
                id SERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                company_number TEXT NOT NULL,
                company_name TEXT NOT NULL,
                status TEXT NOT NULL,
                type TEXT,
                jurisdiction TEXT,
                last_filing_date TEXT,
                last_filing_description TEXT,
                last_filing_category TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(user_id, company_number)
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
            CREATE TABLE IF NOT EXISTS password_resets (
                token TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                expires_at TIMESTAMP NOT NULL,
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
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS watchlist (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                company_number TEXT NOT NULL,
                company_name TEXT NOT NULL,
                status TEXT NOT NULL,
                type TEXT,
                jurisdiction TEXT,
                last_filing_date TEXT,
                last_filing_description TEXT,
                last_filing_category TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users (id),
                UNIQUE(user_id, company_number)
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS search_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                identifier TEXT NOT NULL,
                search_date DATE NOT NULL,
                count INTEGER DEFAULT 1,
                UNIQUE(identifier, search_date)
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                session_id TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users (id)
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS password_resets (
                token TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                expires_at TIMESTAMP NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users (id)
            )
        """)
        cursor.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_watchlist_user_company ON"
            " watchlist(user_id, company_number)"
        )

    conn.commit()
    conn.close()


# ---------------------------------------------------------
# ŞİFRELEME
# ---------------------------------------------------------
def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    key = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt.encode("utf-8"), 100000
    )
    return f"{salt}${key.hex()}"


def verify_password(stored_hash: str, password: str) -> bool:
    try:
        salt, key_hex = stored_hash.split("$")
        key = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), salt.encode("utf-8"), 100000
        )
        return key.hex() == key_hex
    except Exception:
        return False


# ---------------------------------------------------------
# E-POSTA İLE ŞİFRE SIFIRLAMA GÖNDERİCİSİ
# ---------------------------------------------------------
def send_reset_email(to_email: str, reset_link: str):
    if not SMTP_SERVER or not SMTP_USER:
        print(f"\n=======================================================")
        print(f"[PASSWORD RESET LINK for {to_email}]:")
        print(reset_link)
        print(f"=======================================================\n")
        return

    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = "Reset Your UK Company Radar Password"
        msg["From"] = SMTP_FROM
        msg["To"] = to_email

        html = f"""
        <div style="font-family: Arial, sans-serif; max-width: 500px; margin: 0 auto; padding: 20px; border: 1px solid #e2e8f0; border-radius: 12px;">
            <h2 style="color: #0f172a; margin-bottom: 8px;">UK Company Radar</h2>
            <p style="color: #475569; font-size: 14px;">We received a request to reset your password. Click the link below to set a new password:</p>
            <div style="margin: 25px 0;">
                <a href="{reset_link}" style="background-color: #0f172a; color: #ffffff; padding: 10px 20px; text-decoration: none; border-radius: 8px; font-weight: bold; font-size: 14px; display: inline-block;">Reset Password</a>
            </div>
            <p style="color: #94a3b8; font-size: 12px;">This link will expire in 1 hour. If you did not request this, please ignore this email.</p>
        </div>
        """
        msg.attach(MIMEText(html, "html"))

        server = smtplib.SMTP(SMTP_SERVER, SMTP_PORT, timeout=10)
        server.starttls()
        server.login(SMTP_USER, SMTP_PASSWORD)
        server.sendmail(SMTP_FROM, [to_email], msg.as_string())
        server.quit()
    except Exception as e:
        print(f"[SMTP Send Error]: {e}")


# ---------------------------------------------------------
# TELEGRAM BİLDİRİM (NON-BLOCKING ASYNC)
# ---------------------------------------------------------
def _send_telegram_worker(chat_id: str, message: str):
    if not chat_id or not TELEGRAM_BOT_TOKEN:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": str(chat_id).strip(),
        "text": message,
        "parse_mode": "HTML",
    }
    try:
        res = requests.post(url, json=payload, timeout=8)
        data = res.json()
        if not data.get("ok"):
            print(f"[Telegram Alert Failed for {chat_id}]: {data.get('description')}")
    except Exception as e:
        print(f"[Telegram Network Error]: {e}")


async def send_telegram(chat_id: str, message: str):
    await asyncio.to_thread(_send_telegram_worker, chat_id, message)


# ---------------------------------------------------------
# YARDIMCI METODLAR, PAKET YÖNETİMİ & KULLANICI
# ---------------------------------------------------------
def normalize_company_number(value: str) -> str:
    val = (value or "").strip().upper().replace(" ", "")
    if len(val) == 7 and val.isdigit():
        return "0" + val
    return val


def is_valid_telegram_chat_id(chat_id: str) -> bool:
    if chat_id is None:
        return False
    return bool(re.fullmatch(r"-?\d{6,20}", str(chat_id).strip()))


def get_user_watchlist_limit(user: dict) -> int:
    if not user:
        return 3
    if user.get("is_admin"):
        return 100
    plan_tier = user.get("is_pro", 0)  # 0: Free (3), 1: Pro (50), 2: Gold (100)
    return PLAN_LIMITS.get(plan_tier, 3)


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
        user_dict["is_pro"] = 2  # Admin hesabı Gold katman yetkisinde
        user_dict["is_admin"] = True
    else:
        user_dict["is_admin"] = False

    return user_dict


def get_search_count(identifier: str) -> int:
    today = date.today().isoformat()
    conn = get_db()
    cursor = conn.cursor()
    query = format_query(
        "SELECT count FROM search_logs WHERE identifier = ? AND search_date = ?"
    )
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
# COMPANIES HOUSE API
# ---------------------------------------------------------
def search_companies_house(query: str):
    if not COMPANIES_HOUSE_API_KEY:
        print("[Companies House Error]: Key missing!")
        return []
    cleaned_query = (query or "").strip()
    if not cleaned_query:
        return []
    url = f"{BASE_URL}/search/companies"
    try:
        res = requests.get(
            url,
            params={"q": cleaned_query},
            auth=(COMPANIES_HOUSE_API_KEY, ""),
            timeout=8,
        )
        if res.status_code == 200:
            return res.json().get("items", [])
    except Exception as e:
        print(f"[Companies House Exception]: {e}")
    return []


def get_company_profile(company_number: str):
    if not COMPANIES_HOUSE_API_KEY:
        return None
    normalized_number = normalize_company_number(company_number)
    if not normalized_number:
        return None
    url = f"{BASE_URL}/company/{normalized_number}"
    try:
        res = requests.get(url, auth=(COMPANIES_HOUSE_API_KEY, ""), timeout=8)
        if res.status_code == 200:
            return res.json()
    except Exception as e:
        print(f"[Companies House Profile Exception]: {e}")
    return None


def get_latest_filing_update(company_number: str):
    if not COMPANIES_HOUSE_API_KEY:
        return None, None, None
    normalized_number = normalize_company_number(company_number)
    if not normalized_number:
        return None, None, None

    url = f"{BASE_URL}/company/{normalized_number}/filing-history"
    params = {"category": "insolvency", "items_per_page": 1}
    try:
        res = requests.get(
            url, params=params, auth=(COMPANIES_HOUSE_API_KEY, ""), timeout=8
        )
        if res.status_code == 200:
            items = res.json().get("items", [])
            if items:
                item = items[0]
                return (
                    item.get("date"),
                    item.get("description", ""),
                    item.get("category", "insolvency"),
                )
    except Exception as e:
        print(f"[Companies House Filing Exception]: {e}")
    return None, None, None


def get_company_officers(company_number: str):
    if not COMPANIES_HOUSE_API_KEY:
        return []
    normalized_number = normalize_company_number(company_number)
    url = f"{BASE_URL}/company/{normalized_number}/officers"
    try:
        res = requests.get(url, auth=(COMPANIES_HOUSE_API_KEY, ""), timeout=8)
        if res.status_code == 200:
            return res.json().get("items", [])
    except Exception as e:
        print(f"[Companies House Officers Error]: {e}")
    return []


def get_company_charges(company_number: str):
    if not COMPANIES_HOUSE_API_KEY:
        return []
    normalized_number = normalize_company_number(company_number)
    url = f"{BASE_URL}/company/{normalized_number}/charges"
    try:
        res = requests.get(url, auth=(COMPANIES_HOUSE_API_KEY, ""), timeout=8)
        if res.status_code == 200:
            return res.json().get("items", [])
    except Exception as e:
        print(f"[Companies House Charges Error]: {e}")
    return []


# ---------------------------------------------------------
# KURAL TABANLI RİSK SKORU VE DEĞERLEME MOTORU
# ---------------------------------------------------------
def calculate_company_risk_score(profile: dict, charges: list = None) -> dict:
    score = 100
    factors = []
    
    if not profile:
        return {
            "score": 50,
            "level": "UNKNOWN",
            "badge_color": "slate",
            "action_required": False,
            "factors": [{"type": "warning", "text": "Insufficient registry data to evaluate counterparty risk."}]
        }
    
    status = (profile.get("company_status") or "unknown").lower()
    
    # 1. Hukuki Varlık / İflas Durumu
    if status in ["liquidation", "administration", "receivership", "insolvent"]:
        return {
            "score": 10,
            "level": "CRITICAL RISK",
            "badge_color": "rose",
            "action_required": True,
            "factors": [
                {"type": "danger", "text": f"Entity is officially under {status.upper()} / insolvency proceedings."},
                {"type": "danger", "text": "Immediate creditor exposure. Trading on credit strongly discouraged."}
            ]
        }
    elif status in ["dissolved", "converted-closed"]:
        return {
            "score": 15,
            "level": "HIGH RISK",
            "badge_color": "rose",
            "action_required": True,
            "factors": [
                {"type": "danger", "text": f"Entity is officially {status.upper()} on the corporate registry."},
                {"type": "danger", "text": "Ceased trading legally; contracts cannot be enforced."}
            ]
        }
    else:
        factors.append({"type": "positive", "text": "Entity maintains ACTIVE status on official UK registries."})
    
    # 2. Yasal Teslimat Tarihleri
    accounts = profile.get("accounts", {})
    if accounts.get("overdue", False):
        score -= 25
        factors.append({"type": "danger", "text": "Annual financial accounts filing is OVERDUE at Companies House (-25 pts)."})
    else:
        factors.append({"type": "positive", "text": "Financial reporting obligations are up to date."})
        
    conf_stmt = profile.get("confirmation_statement", {})
    if conf_stmt.get("overdue", False):
        score -= 20
        factors.append({"type": "danger", "text": "Annual Confirmation Statement (PSC / ownership) is past due (-20 pts)."})
    else:
        factors.append({"type": "positive", "text": "Confirmation statement filings are compliant."})
        
    # 3. İpotek & Teminatlı Borçlar
    if charges:
        outstanding_charges = [c for c in charges if c.get("status") == "outstanding"]
        if outstanding_charges:
            penalty = min(30, len(outstanding_charges) * 10)
            score -= penalty
            factors.append({
                "type": "warning", 
                "text": f"{len(outstanding_charges)} outstanding secured floating charge(s)/mortgage(s) registered on assets (-{penalty} pts)."
            })
        else:
            factors.append({"type": "positive", "text": "Zero outstanding floating charges registered (No secured bank encumbrances)."})
    else:
        factors.append({"type": "positive", "text": "No registered mortgage or debenture encumbrance on file."})
            
    # 4. Şirket Yaşı
    inc_date_str = profile.get("date_of_creation")
    if inc_date_str:
        try:
            inc_date = datetime.strptime(inc_date_str, "%Y-%m-%d").date()
            age_days = (date.today() - inc_date).days
            if age_days < 365:
                score -= 10
                factors.append({"type": "warning", "text": "Newly formed entity (<12 months trading history) (-10 pts)."})
            elif age_days > 1825:
                factors.append({"type": "positive", "text": f"Established corporate track record ({age_days // 365} years in business)."})
        except Exception:
            pass

    score = max(5, min(100, score))
    
    if score >= 80:
        level = "LOW RISK"
        badge_color = "emerald"
        action_required = False
    elif score >= 50:
        level = "MODERATE RISK"
        badge_color = "amber"
        action_required = False
    else:
        level = "HIGH RISK"
        badge_color = "rose"
        action_required = True

    return {
        "score": score,
        "level": level,
        "badge_color": badge_color,
        "action_required": action_required,
        "factors": factors
    }


# ---------------------------------------------------------
# ARKA PLAN GÖREVİ (AUTOMATED BACKGROUND MONITOR)
# ---------------------------------------------------------
async def automated_background_monitor():
    while True:
        try:
            conn = get_db()
            cursor = conn.cursor()
            cursor.execute("""
                SELECT w.id, w.user_id, w.company_number, w.company_name, w.status, w.last_filing_date,
                       w.last_filing_description, w.last_filing_category, u.telegram_chat_id
                FROM watchlist w
                JOIN users u ON w.user_id = u.id
            """)
            items = cursor.fetchall()

            for item in items:
                live_profile = get_company_profile(item["company_number"])
                if not live_profile:
                    continue

                new_status = (live_profile.get("company_status") or "unknown").lower()
                old_status = (item["status"] or "unknown").lower()
                (
                    latest_filing_date,
                    latest_filing_description,
                    latest_filing_category,
                ) = get_latest_filing_update(item["company_number"])

                status_changed = old_status != new_status
                filing_changed = (
                    latest_filing_date is not None
                    and latest_filing_date != item["last_filing_date"]
                )

                if status_changed or filing_changed:
                    if status_changed:
                        q_update = format_query("""
                            UPDATE watchlist 
                            SET status = ?, 
                                last_filing_date = COALESCE(?, last_filing_date), 
                                last_filing_description = COALESCE(?, last_filing_description), 
                                last_filing_category = COALESCE(?, last_filing_category) 
                            WHERE id = ?
                        """)
                        cursor.execute(
                            q_update,
                            (
                                live_profile.get("company_status", "unknown"),
                                latest_filing_date,
                                latest_filing_description,
                                latest_filing_category,
                                item["id"],
                            ),
                        )
                        if item["telegram_chat_id"]:
                            alert_msg = (
                                "🚨 <b>CRITICAL RISK ALERT: STATUS CHANGE DETECTED</b>\n\n"
                                f"<b>Company:</b> {item['company_name']}"
                                f" ({item['company_number']})\n<b>Previous Status:</b>"
                                f" {old_status.upper()}\n<b>New Status:</b> ⚠️"
                                f" <b>{new_status.upper()}</b>\n\n<i>Immediate attention"
                                " recommended. Check official Companies House filing.</i>"
                            )
                            await send_telegram(item["telegram_chat_id"], alert_msg)
                    if filing_changed and item["telegram_chat_id"]:
                        filing_msg = (
                            "🚨 <b>INSOLVENCY / GAZETTE ALERT</b>\n\n<b>Company:</b>"
                            f" {item['company_name']} ({item['company_number']})\n<b>Latest"
                            f" filing:</b> {latest_filing_description or 'Insolvency Notice'}\n<b>Category:</b>"
                            f" {latest_filing_category or 'Insolvency'}\n<b>Date:</b>"
                            f" {latest_filing_date}\n\n<i>New official notice detected in"
                            " Companies House filing history.</i>"
                        )
                        await send_telegram(item["telegram_chat_id"], filing_msg)

                q_sync = format_query("""
                    UPDATE watchlist 
                    SET company_name = ?, 
                        status = ?, 
                        last_filing_date = COALESCE(?, last_filing_date), 
                        last_filing_description = COALESCE(?, last_filing_description), 
                        last_filing_category = COALESCE(?, last_filing_category) 
                    WHERE id = ?
                """)
                cursor.execute(
                    q_sync,
                    (
                        live_profile.get("company_name", item["company_name"]),
                        live_profile.get("company_status", "unknown"),
                        latest_filing_date,
                        latest_filing_description,
                        latest_filing_category,
                        item["id"],
                    ),
                )

            conn.commit()
            conn.close()
        except Exception as e:
            print(f"[Background Monitor Error]: {e}")

        await asyncio.sleep(3600)


@app.on_event("startup")
async def startup_event():
    init_db()
    asyncio.create_task(automated_background_monitor())


# ---------------------------------------------------------
# DODO PAYMENTS WEBHOOK ENTEGRASYONU (KATMAN DÜZELTMESİ)
# ---------------------------------------------------------
@app.post("/api/dodo-webhook")
async def dodo_webhook(request: Request):
    raw_body = await request.body()
    headers = request.headers
    
    webhook_signature = headers.get("webhook-signature") or headers.get("Webhook-Signature")
    if DODO_WEBHOOK_SECRET and webhook_signature:
        computed_sig = hmac.new(
            DODO_WEBHOOK_SECRET.encode("utf-8"), raw_body, hashlib.sha256
        ).hexdigest()
        if computed_sig in webhook_signature:
            pass

    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

    event_type = payload.get("type") or payload.get("event")
    event_data = payload.get("data", {})

    customer_email = ""
    if isinstance(event_data.get("customer"), dict):
        customer_email = event_data["customer"].get("email") or ""
    elif isinstance(event_data.get("customer"), str) and "@" in event_data.get("customer"):
        customer_email = event_data.get("customer")
    elif event_data.get("billing_email"):
        customer_email = event_data.get("billing_email")
    elif event_data.get("customer_email"):
        customer_email = event_data.get("customer_email")

    customer_email = customer_email.strip().lower()

    if not customer_email:
        print(f"[Dodo Webhook Warning]: No email found in payload. Event: {event_type}")
        return {"status": "no_email_ignored"}

    product_id = str(event_data.get("product_id") or payload.get("product_id") or "")
    target_tier = 1  # Pro Tier (1)
    if "gold" in product_id.lower() or "gold" in str(payload).lower():
        target_tier = 2  # Gold Tier (2)

    admin_telegram_chat_id = os.getenv("MY_TELEGRAM_ID", "-4367179502").strip()
    conn = get_db()
    cursor = conn.cursor()

    if event_type in ["payment.succeeded", "subscription.active", "checkout.session.completed"]:
        q_update = format_query("UPDATE users SET is_pro = ? WHERE lower(email) = ?")
        cursor.execute(q_update, (target_tier, customer_email))
        conn.commit()

        q_user = format_query("SELECT telegram_chat_id FROM users WHERE lower(email) = ?")
        cursor.execute(q_user, (customer_email,))
        user_row = cursor.fetchone()

        tier_name = "GOLD (Max 100 Entities)" if target_tier == 2 else "PRO (Max 50 Entities)"

        if user_row and user_row.get("telegram_chat_id"):
            pro_msg = (
                f"🎉 <b>UK Company Radar | {tier_name} Activated!</b>\n\n"
                "Thank you for subscribing! Your account has been upgraded.\n"
                "• Unlimited daily company searches\n"
                f"• Track up to {PLAN_LIMITS[target_tier]} entities in your portfolio\n"
                "• Batch CSV import enabled\n"
                "• Priority real-time alert notifications"
            )
            await send_telegram(user_row["telegram_chat_id"], pro_msg)

        admin_sale_msg = (
            "💰 <b>NEW DODO PAYMENTS SALE!</b>\n\n"
            f"👤 <b>Customer:</b> <code>{customer_email}</code>\n"
            f"📦 <b>Tier:</b> <code>{tier_name}</code>\n"
            f"💳 <b>Provider:</b> Dodo Payments (MoR)\n"
            f"🕒 <i>Time: {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC</i>"
        )
        await send_telegram(admin_telegram_chat_id, admin_sale_msg)

    elif event_type in ["subscription.cancelled", "subscription.expired", "subscription.paused"]:
        q_cancel = format_query("UPDATE users SET is_pro = 0 WHERE lower(email) = ?")
        cursor.execute(q_cancel, (customer_email,))
        conn.commit()

        q_user = format_query("SELECT telegram_chat_id FROM users WHERE lower(email) = ?")
        cursor.execute(q_user, (customer_email,))
        user_row = cursor.fetchone()

        if user_row and user_row.get("telegram_chat_id"):
            cancel_msg = (
                "ℹ️ <b>UK Company Radar | Subscription Ended</b>\n\n"
                "Your subscription has concluded. Your account is now on the complimentary Free tier."
            )
            await send_telegram(user_row["telegram_chat_id"], cancel_msg)

    conn.close()
    return {"status": "success", "event": event_type}


# ---------------------------------------------------------
# LEMON SQUEEZY WEBHOOK ENTEGRASYONU (YEDEK)
# ---------------------------------------------------------
@app.post("/api/lemon-webhook")
async def lemon_webhook(
    request: Request, x_signature: str = Header(None, alias="X-Signature")
):
    raw_body = await request.body()
    digest = hmac.new(
        LEMON_WEBHOOK_SECRET.encode("utf-8"), raw_body, hashlib.sha256
    ).hexdigest()

    if not hmac.compare_digest(digest, x_signature or ""):
        raise HTTPException(status_code=400, detail="Invalid signature")

    payload = await request.json()
    meta = payload.get("meta", {})
    event_name = meta.get("event_name")
    attributes = payload.get("data", {}).get("attributes", {})

    customer_email = (attributes.get("user_email") or "").strip().lower()
    status = attributes.get("status")

    if not customer_email:
        return {"status": "no_email"}

    conn = get_db()
    cursor = conn.cursor()

    if event_name in ["subscription_created", "subscription_updated"]:
        if status == "active":
            q_update = format_query(
                "UPDATE users SET is_pro = 1 WHERE lower(email) = ?"
            )
            cursor.execute(q_update, (customer_email,))
            conn.commit()

    elif event_name in ["subscription_cancelled", "subscription_expired"]:
        q_cancel = format_query(
            "UPDATE users SET is_pro = 0 WHERE lower(email) = ?"
        )
        cursor.execute(q_cancel, (customer_email,))
        conn.commit()

    conn.close()
    return {"status": "success"}


# ---------------------------------------------------------
# ANA SAYFA VE TAKİP ROTALARI (Ödeme Linkleri Context'e Ekleniyor)
# ---------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
async def index(request: Request, q: Optional[str] = None):
    user = get_current_user(request)
    client_ip = request.client.host if request.client else "127.0.0.1"

    is_pro = bool(user and user.get("is_pro", 0) > 0)
    user_tier = user.get("is_pro", 0) if user else 0
    max_watchlist_limit = get_user_watchlist_limit(user)

    identifier = f"user_{user['id']}" if user else f"guest_{client_ip}"
    search_count = get_search_count(identifier)

    if is_pro:
        search_limit = 999999
        search_limit_reached = False
        remaining_searches = "Unlimited"
    elif user:
        search_limit = 5
        search_limit_reached = search_count >= search_limit
        remaining_searches = max(0, search_limit - search_count)
    else:
        search_limit = 3
        search_limit_reached = search_count >= search_limit
        remaining_searches = max(0, search_limit - search_count)

    search_results = []
    if q and not search_limit_reached:
        search_results = search_companies_house(q.strip())
        increment_search_count(identifier)
        if not is_pro:
            search_count += 1
            remaining_searches = max(0, search_limit - search_count)
            if search_count >= search_limit:
                search_limit_reached = True

    watchlist = []
    watchlist_limit_reached = False
    if user:
        conn = get_db()
        cursor = conn.cursor()
        query = format_query(
            "SELECT * FROM watchlist WHERE user_id = ? ORDER BY created_at DESC"
        )
        cursor.execute(query, (user["id"],))
        watchlist = cursor.fetchall()
        conn.close()

        if len(watchlist) >= max_watchlist_limit:
            watchlist_limit_reached = True

    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "user": user,
            "query": q,
            "results": search_results,
            "watchlist": watchlist,
            "search_count": search_count,
            "search_limit": search_limit,
            "search_limit_reached": search_limit_reached,
            "remaining_searches": remaining_searches,
            "watchlist_limit_reached": watchlist_limit_reached,
            "max_watchlist_limit": max_watchlist_limit,
            "is_pro": is_pro,
            "user_tier": user_tier,
            # Ödeme Linkleri (.env değişkenleri)
            "pro_monthly_link": DODO_PRO_MONTHLY,
            "pro_annual_link": DODO_PRO_ANNUAL,
            "gold_monthly_link": DODO_GOLD_MONTHLY,
            "gold_annual_link": DODO_GOLD_ANNUAL,
        },
    )


@app.post("/track")
async def track_company(
    request: Request,
    company_number: str = Form(...),
    company_name: str = Form(...),
    status: str = Form(...),
    type: str = Form(None),
    jurisdiction: str = Form(None),
):
    user = get_current_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=303)

    company_number = normalize_company_number(company_number)
    if not company_number or not re.fullmatch(r"[A-Z0-9]{6,10}", company_number):
        return RedirectResponse(url="/?invalid_company=1", status_code=303)

    conn = get_db()
    cursor = conn.cursor()
    q_check = format_query(
        "SELECT id FROM watchlist WHERE user_id = ? AND company_number = ?"
    )
    cursor.execute(q_check, (user["id"], company_number))
    existing = cursor.fetchone()
    if existing:
        conn.close()
        return RedirectResponse(url="/?already_tracked=1", status_code=303)

    q_count = format_query(
        "SELECT COUNT(*) as count FROM watchlist WHERE user_id = ?"
    )
    cursor.execute(q_count, (user["id"],))
    count = cursor.fetchone()["count"]

    max_limit = get_user_watchlist_limit(user)
    if count >= max_limit:
        conn.close()
        return RedirectResponse(url="/?limit_reached=1", status_code=303)

    q_insert = format_query("""
        INSERT INTO watchlist (user_id, company_number, company_name, status, type, jurisdiction, last_filing_date, last_filing_description, last_filing_category)
        VALUES (?, ?, ?, ?, ?, ?, NULL, NULL, NULL)
    """)
    cursor.execute(
        q_insert,
        (
            user["id"],
            company_number,
            company_name.strip(),
            status.strip(),
            type,
            jurisdiction,
        ),
    )
    conn.commit()
    conn.close()

    if user.get("telegram_chat_id"):
        welcome_alert = (
            "🛡️ <b>UK Company Radar | Monitoring Active</b>\n\n<b>Company:</b>"
            f" {company_name.strip()} ({company_number})\n<b>Current Legal"
            f" Status:</b> {status.strip().upper()}\n\n✅ <i>24/7 automated"
            " insolvency monitoring is now active. You will receive an instant"
            " notification here if this entity enters liquidation, administration,"
            " or strike-off.</i>"
        )
        await send_telegram(user["telegram_chat_id"], welcome_alert)

    return RedirectResponse(url="/", status_code=303)


@app.post("/untrack")
async def untrack_company(request: Request, company_id: int = Form(...)):
    user = get_current_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=303)

    conn = get_db()
    cursor = conn.cursor()
    query = format_query("DELETE FROM watchlist WHERE id = ? AND user_id = ?")
    cursor.execute(query, (company_id, user["id"]))
    conn.commit()
    conn.close()
    return RedirectResponse(url="/", status_code=303)


# ---------------------------------------------------------
# CSV / EXCEL TOPLU PORTFÖY YÜKLEME (BATCH UPLOAD API)
# ---------------------------------------------------------
@app.post("/api/watchlist/upload_csv")
async def upload_csv_watchlist(request: Request, file: UploadFile = File(...)):
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Authentication required")

    if not file.filename.endswith(('.csv', '.txt')):
        raise HTTPException(status_code=400, detail="Only CSV or TXT files are supported.")

    content = await file.read()
    decoded = content.decode('utf-8-sig', errors='ignore')
    reader = csv.reader(io.StringIO(decoded))

    conn = get_db()
    cursor = conn.cursor()

    q_count = format_query("SELECT COUNT(*) as count FROM watchlist WHERE user_id = ?")
    cursor.execute(q_count, (user["id"],))
    current_count = cursor.fetchone()["count"]

    max_limit = get_user_watchlist_limit(user)
    available_slots = max_limit - current_count

    if available_slots <= 0:
        conn.close()
        raise HTTPException(
            status_code=400, 
            detail=f"Watchlist limit reached ({current_count}/{max_limit}). Please upgrade your plan."
        )

    added_count = 0
    skipped_count = 0

    for row in reader:
        if not row:
            continue
        
        raw_number = row[0].strip()
        if not raw_number or raw_number.lower() in ["company_number", "company number", "reg_no", "reg no"]:
            continue

        company_number = normalize_company_number(raw_number)
        if not re.fullmatch(r"[A-Z0-9]{6,10}", company_number):
            skipped_count += 1
            continue

        if added_count >= available_slots:
            break

        q_check = format_query("SELECT id FROM watchlist WHERE user_id = ? AND company_number = ?")
        cursor.execute(q_check, (user["id"], company_number))
        if cursor.fetchone():
            skipped_count += 1
            continue

        profile = get_company_profile(company_number)
        if not profile:
            skipped_count += 1
            continue

        company_name = profile.get("company_name", "Unknown Entity")
        status = profile.get("company_status", "active")
        jurisdiction = (profile.get("registered_office_address") or {}).get("locality", "UK Entity")

        q_insert = format_query("""
            INSERT INTO watchlist (user_id, company_number, company_name, status, type, jurisdiction, last_filing_date, last_filing_description, last_filing_category)
            VALUES (?, ?, ?, ?, ?, ?, NULL, NULL, NULL)
        """)
        cursor.execute(q_insert, (user["id"], company_number, company_name, status, profile.get("type"), jurisdiction))
        added_count += 1

    conn.commit()
    conn.close()

    return {
        "success": True,
        "added": added_count,
        "skipped": skipped_count,
        "total_now": current_count + added_count,
        "limit": max_limit
    }


# ---------------------------------------------------------
# AUTH ROTALARI (GİRİŞ, KAYIT & ŞİFRE SIFIRLAMA)
# ---------------------------------------------------------
@app.get("/auth", response_class=HTMLResponse)
async def auth_page(request: Request, mode: Optional[str] = "login"):
    user = get_current_user(request)
    if user:
        return RedirectResponse(url="/", status_code=303)
    initial_tab = "register" if mode == "register" else "login"
    return templates.TemplateResponse(
        request=request, 
        name="auth.html", 
        context={"error": None, "message": None, "initial_tab": initial_tab}
    )


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    user = get_current_user(request)
    if user:
        return RedirectResponse(url="/", status_code=303)
    return templates.TemplateResponse(
        request=request, 
        name="auth.html", 
        context={"error": None, "message": None, "initial_tab": "login"}
    )


@app.get("/register", response_class=HTMLResponse)
async def register_page(request: Request):
    user = get_current_user(request)
    if user:
        return RedirectResponse(url="/", status_code=303)
    return templates.TemplateResponse(
        request=request, 
        name="auth.html", 
        context={"error": None, "message": None, "initial_tab": "register"}
    )


@app.post("/auth/register")
async def register(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    telegram_chat_id: str = Form(None),
):
    email_clean = email.strip().lower()
    chat_id_clean = telegram_chat_id.strip() if telegram_chat_id else None

    if chat_id_clean and not is_valid_telegram_chat_id(chat_id_clean):
        return templates.TemplateResponse(
            request=request,
            name="auth.html",
            context={"error": "Telegram Chat ID must be a valid numeric ID.", "message": None, "initial_tab": "register"},
        )

    conn = get_db()
    cursor = conn.cursor()

    q_email = format_query("SELECT id FROM users WHERE lower(email) = ?")
    cursor.execute(q_email, (email_clean,))
    if cursor.fetchone():
        conn.close()
        return templates.TemplateResponse(
            request=request,
            name="auth.html",
            context={"error": "This email is already registered.", "message": None, "initial_tab": "register"},
        )

    if chat_id_clean and (email_clean not in ADMIN_EMAILS):
        q_tg = format_query("SELECT id FROM users WHERE telegram_chat_id = ?")
        cursor.execute(q_tg, (chat_id_clean,))
        existing_tg_user = cursor.fetchone()
        if existing_tg_user:
            conn.close()
            return templates.TemplateResponse(
                request=request,
                name="auth.html",
                context={
                    "error": (
                        "This Telegram Chat ID is already registered to another"
                        " account."
                    ),
                    "message": None,
                    "initial_tab": "register",
                },
            )

    pwd_hash = hash_password(password)
    is_pro_val = 2 if email_clean in ADMIN_EMAILS else 0

    if USE_PG:
        cursor.execute(
            """
                INSERT INTO users (email, password_hash, telegram_chat_id, is_pro) 
                VALUES (%s, %s, %s, %s) RETURNING id
            """,
            (email_clean, pwd_hash, chat_id_clean, is_pro_val),
        )
        user_id = cursor.fetchone()["id"]
    else:
        cursor.execute(
            """
                INSERT INTO users (email, password_hash, telegram_chat_id, is_pro) 
                VALUES (?, ?, ?, ?)
            """,
            (email_clean, pwd_hash, chat_id_clean, is_pro_val),
        )
        user_id = cursor.lastrowid

    session_id = secrets.token_hex(24)
    q_session = format_query(
        "INSERT INTO sessions (session_id, user_id) VALUES (?, ?)"
    )
    cursor.execute(q_session, (session_id, user_id))
    conn.commit()
    conn.close()

    if chat_id_clean:
        reg_msg = (
            "👋 <b>Welcome to UK Company Radar!</b>\n\nYour Telegram Chat ID has"
            " been linked successfully.\nAutomated monitoring will dispatch alerts"
            " directly here."
        )
        await send_telegram(chat_id_clean, reg_msg)

    response = RedirectResponse(url="/", status_code=303)
    response.set_cookie(
        "session_id",
        session_id,
        max_age=30 * 86400,
        httponly=True,
        secure=not DEBUG_MODE,
        samesite="lax",
    )
    return response


@app.post("/auth/login")
async def login(
    request: Request, email: str = Form(...), password: str = Form(...)
):
    email_clean = email.strip().lower()
    conn = get_db()
    cursor = conn.cursor()
    query = format_query("SELECT * FROM users WHERE lower(email) = ?")
    cursor.execute(query, (email_clean,))
    user = cursor.fetchone()

    if not user or not verify_password(user["password_hash"], password):
        conn.close()
        return templates.TemplateResponse(
            request=request,
            name="auth.html",
            context={"error": "Invalid email or password.", "message": None, "initial_tab": "login"},
        )

    session_id = secrets.token_hex(24)
    q_session = format_query(
        "INSERT INTO sessions (session_id, user_id) VALUES (?, ?)"
    )
    cursor.execute(q_session, (session_id, user["id"]))
    conn.commit()
    conn.close()

    response = RedirectResponse(url="/", status_code=303)
    response.set_cookie(
        "session_id",
        session_id,
        max_age=30 * 86400,
        httponly=True,
        secure=not DEBUG_MODE,
        samesite="lax",
    )
    return response


@app.post("/auth/forgot-password")
async def forgot_password(request: Request, email: str = Form(...)):
    email_clean = email.strip().lower()
    conn = get_db()
    cursor = conn.cursor()
    query = format_query("SELECT id FROM users WHERE lower(email) = ?")
    cursor.execute(query, (email_clean,))
    user = cursor.fetchone()

    if user:
        token = secrets.token_urlsafe(32)
        expires_at = datetime.utcnow() + timedelta(hours=1)
        
        q_reset = format_query("""
            INSERT INTO password_resets (token, user_id, expires_at)
            VALUES (?, ?, ?)
        """)
        cursor.execute(q_reset, (token, user["id"], expires_at))
        conn.commit()

        reset_link = f"{APP_BASE_URL}/auth/reset-password?token={token}"
        send_reset_email(email_clean, reset_link)

    conn.close()
    return templates.TemplateResponse(
        request=request,
        name="auth.html",
        context={
            "error": None,
            "message": "If an account exists with this email, a password reset link has been dispatched.",
            "initial_tab": "login",
        },
    )


@app.get("/auth/reset-password", response_class=HTMLResponse)
async def reset_password_page(request: Request, token: Optional[str] = None):
    if not token:
        return RedirectResponse(url="/login", status_code=303)

    conn = get_db()
    cursor = conn.cursor()
    query = format_query("SELECT user_id, expires_at FROM password_resets WHERE token = ?")
    cursor.execute(query, (token,))
    record = cursor.fetchone()
    conn.close()

    if not record:
        return templates.TemplateResponse(
            request=request,
            name="auth.html",
            context={"error": "Invalid or expired reset token.", "message": None, "initial_tab": "login"},
        )

    expires_at = record["expires_at"]
    if isinstance(expires_at, str):
        expires_at = datetime.fromisoformat(expires_at)
    
    if datetime.utcnow() > expires_at:
        return templates.TemplateResponse(
            request=request,
            name="auth.html",
            context={"error": "This reset link has expired. Please request a new one.", "message": None, "initial_tab": "login"},
        )

    return templates.TemplateResponse(
        request=request,
        name="reset_password.html",
        context={"token": token, "error": None},
    )


@app.post("/auth/reset-password")
async def reset_password_submit(
    request: Request,
    token: str = Form(...),
    password: str = Form(...),
    confirm_password: str = Form(...),
):
    if password != confirm_password:
        return templates.TemplateResponse(
            request=request,
            name="reset_password.html",
            context={"token": token, "error": "Passwords do not match."},
        )

    if len(password) < 6:
        return templates.TemplateResponse(
            request=request,
            name="reset_password.html",
            context={"token": token, "error": "Password must be at least 6 characters."},
        )

    conn = get_db()
    cursor = conn.cursor()
    query = format_query("SELECT user_id, expires_at FROM password_resets WHERE token = ?")
    cursor.execute(query, (token,))
    record = cursor.fetchone()

    if not record:
        conn.close()
        return templates.TemplateResponse(
            request=request,
            name="auth.html",
            context={"error": "Invalid or expired reset token.", "message": None, "initial_tab": "login"},
        )

    user_id = record["user_id"]
    new_pwd_hash = hash_password(password)

    q_upd = format_query("UPDATE users SET password_hash = ? WHERE id = ?")
    cursor.execute(q_upd, (new_pwd_hash, user_id))

    q_del = format_query("DELETE FROM password_resets WHERE user_id = ?")
    cursor.execute(q_del, (user_id,))

    session_id = secrets.token_hex(24)
    q_session = format_query(
        "INSERT INTO sessions (session_id, user_id) VALUES (?, ?)"
    )
    cursor.execute(q_session, (session_id, user_id))
    conn.commit()
    conn.close()

    response = RedirectResponse(url="/", status_code=303)
    response.set_cookie(
        "session_id",
        session_id,
        max_age=30 * 86400,
        httponly=True,
        secure=not DEBUG_MODE,
        samesite="lax",
    )
    return response


@app.get("/auth/logout")
async def logout(request: Request):
    session_id = request.cookies.get("session_id")
    if session_id:
        conn = get_db()
        cursor = conn.cursor()
        query = format_query("DELETE FROM sessions WHERE session_id = ?")
        cursor.execute(query, (session_id,))
        conn.commit()
        conn.close()

    response = RedirectResponse(url="/", status_code=303)
    response.delete_cookie("session_id", path="/", samesite="lax")
    return response


# ---------------------------------------------------------
# CASE STUDY, DETAY APISI & ADMIN ROTALARI
# ---------------------------------------------------------
@app.get("/insights/glens-of-antrim-administration", response_class=HTMLResponse)
async def glens_case_study(request: Request):
    user = get_current_user(request)
    return templates.TemplateResponse(
        request=request,
        name="case_study_glens.html",
        context={"user": user}
    )


@app.get("/api/company/{company_number}/details")
async def company_details_api(request: Request, company_number: str):
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Authentication required")

    profile = get_company_profile(company_number)
    if not profile:
        raise HTTPException(status_code=404, detail="Company profile not found")

    officers = get_company_officers(company_number)
    charges = get_company_charges(company_number)

    risk_analysis = calculate_company_risk_score(profile, charges)

    return {
        "profile": profile,
        "officers": officers[:8],
        "charges": charges[:8],
        "total_charges": len(charges),
        "risk_analysis": risk_analysis,
    }


@app.get("/admin/users", response_class=HTMLResponse)
async def admin_users_dashboard(request: Request):
    user = get_current_user(request)
    if not user or not user.get("is_admin"):
        raise HTTPException(status_code=403, detail="Forbidden: Admin access only.")

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT u.id, u.email, u.telegram_chat_id, u.is_pro, u.created_at,
               COUNT(w.id) as tracked_count
        FROM users u
        LEFT JOIN watchlist w ON u.id = w.user_id
        GROUP BY u.id, u.email, u.telegram_chat_id, u.is_pro, u.created_at
        ORDER BY u.created_at DESC
    """)
    users_list = cursor.fetchall()
    conn.close()

    rows_html = ""
    for u in users_list:
        tier_val = u["is_pro"]
        if tier_val == 2:
            badge_html = '<span style="background: #f59e0b; color: white; padding: 2px 8px; border-radius: 9999px; font-size: 11px; font-weight: bold;">GOLD</span>'
        elif tier_val == 1:
            badge_html = '<span style="background: #10b981; color: white; padding: 2px 8px; border-radius: 9999px; font-size: 11px; font-weight: bold;">PRO</span>'
        else:
            badge_html = '<span style="background: #94a3b8; color: white; padding: 2px 8px; border-radius: 9999px; font-size: 11px;">FREE</span>'

        tg_info = u['telegram_chat_id'] if u['telegram_chat_id'] else '<span style="color: #94a3b8;">-</span>'
        created = str(u['created_at'])[:16] if u['created_at'] else '-'
        rows_html += f"""
        <tr style="border-bottom: 1px solid #e2e8f0;">
            <td style="padding: 12px 16px; font-size: 14px; font-family: monospace;">#{u['id']}</td>
            <td style="padding: 12px 16px; font-size: 14px; font-weight: 500;">{u['email']}</td>
            <td style="padding: 12px 16px; font-size: 14px;">{badge_html}</td>
            <td style="padding: 12px 16px; font-size: 14px;">{tg_info}</td>
            <td style="padding: 12px 16px; font-size: 14px; font-weight: 600;">{u['tracked_count']} / {PLAN_LIMITS.get(tier_val, 3)}</td>
            <td style="padding: 12px 16px; font-size: 13px; color: #64748b;">{created}</td>
        </tr>
        """

    html_content = f"""
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <title>Admin - User Monitoring | UK Company Radar</title>
        <link rel="icon" type="image/svg+xml" href="/static/favicon.svg">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <script src="https://cdn.tailwindcss.com"></script>
    </head>
    <body class="bg-slate-50 text-slate-800 p-6 md:p-10 font-sans">
        <div class="max-w-5xl mx-auto">
            <div class="flex items-center justify-between mb-8 pb-4 border-b border-slate-200">
                <div>
                    <h1 class="text-2xl font-bold text-slate-900">Registered Users Monitoring</h1>
                    <p class="text-sm text-slate-500 mt-1">Total registered accounts: <strong class="text-slate-800">{len(users_list)}</strong></p>
                </div>
                <a href="/" class="text-sm font-medium text-blue-600 hover:text-blue-700 bg-white px-4 py-2 rounded-lg border border-slate-200 shadow-sm">&larr; Back to App</a>
            </div>

            <div class="bg-white rounded-xl shadow-sm border border-slate-200 overflow-hidden">
                <table class="w-full text-left border-collapse">
                    <thead>
                        <tr class="bg-slate-100 text-xs uppercase tracking-wider text-slate-500 font-semibold border-bottom border-slate-200">
                            <th class="py-3 px-4">ID</th>
                            <th class="py-3 px-4">Email</th>
                            <th class="py-3 px-4">Plan Tier</th>
                            <th class="py-3 px-4">Telegram ID</th>
                            <th class="py-3 px-4">Tracked / Limit</th>
                            <th class="py-3 px-4">Joined Date</th>
                        </tr>
                    </thead>
                    <tbody>
                        {rows_html}
                    </tbody>
                </table>
            </div>
        </div>
    </body>
    </html>
    """
    return HTMLResponse(content=html_content)


# ---------------------------------------------------------
# SUPPORT DESK APISI
# ---------------------------------------------------------
@app.post("/api/support")
async def support_ticket_api(request: Request):
    try:
        data = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid request format")

    user_email = (data.get("email") or "").strip().lower()
    subject = (data.get("subject") or "").strip()
    message = (data.get("message") or "").strip()

    if not user_email or not message:
        raise HTTPException(status_code=400, detail="Email and message are required fields.")

    admin_telegram_chat_id = os.getenv("MY_TELEGRAM_ID", "-4367179502").strip()

    telegram_support_msg = (
        "🛠️ <b>NEW SUPPORT DESK TICKET</b>\n\n"
        f"<b>From:</b> {user_email}\n"
        f"<b>Subject:</b> {subject or 'No Subject'}\n\n"
        f"<b>Message:</b>\n{message}\n\n"
        f"<i>Received at: {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC</i>"
    )

    try:
        await send_telegram(admin_telegram_chat_id, telegram_support_msg)
        return {"success": True, "message": "Ticket successfully received."}
    except Exception as e:
        print(f"[Support Ticket Error]: {e}")
        return {"success": True, "message": "Ticket successfully received."}


# ---------------------------------------------------------
# PWA ENDPOINT'LERİ VE LEGAL SAYFA
# ---------------------------------------------------------
@app.get("/manifest.json")
async def get_manifest():
    return FileResponse(static_dir / "manifest.json", media_type="application/manifest+json")


@app.get("/sw.js")
async def get_sw():
    return FileResponse(static_dir / "sw.js", media_type="application/javascript")


@app.get("/legal", response_class=HTMLResponse)
async def legal_page(request: Request):
    user = get_current_user(request)
    return templates.TemplateResponse(
        request=request,
        name="legal.html",
        context={"user": user}
    )