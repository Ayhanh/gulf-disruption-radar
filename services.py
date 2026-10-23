import io
import json
import os
import smtplib
import time
from collections import defaultdict
from datetime import datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import requests

from database import (
    DB_FILE, format_query, get_db, get_counter, increment_counter, USE_PG,
)

BASE_DIR = Path(__file__).resolve().parent
DATA_CACHE_FILE = BASE_DIR / "rapidapi_API.txt"

RAPIDAPI_KEY = os.getenv("RAPIDAPI_KEY", "").strip()
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()

SMTP_SERVER = os.getenv("SMTP_SERVER", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "ayhan.hakyemez@gmail.com")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
SMTP_FROM = os.getenv("SMTP_FROM", "ayhan.hakyemez@gmail.com")

ADMIN_EMAILS = [os.getenv("ADMIN_EMAIL", "ayhan.hakyemez@gmail.com").strip().lower()]


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
        print("\n" + "=" * 70)
        print(f"[PASSWORD RESET LINK FOR {to_email}]:")
        print(reset_url)
        print("=" * 70 + "\n")
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
        print("\n" + "=" * 70)
        print(f"[FALLBACK RESET LINK FOR {to_email}]:")
        print(reset_url)
        print("=" * 70 + "\n")


# ---------------------------------------------------------
# NOTIFICATION ENGINE (Telegram)
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
    import asyncio
    await asyncio.to_thread(_send_telegram_worker, chat_id, message)


def check_telegram_bot_health() -> bool:
    """Telegram bot token'ının geçerli olup olmadığını kontrol eder."""
    if not TELEGRAM_BOT_TOKEN:
        return False
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getMe"
        res = requests.get(url, timeout=5)
        if res.status_code == 200 and res.json().get("ok"):
            return True
        return False
    except Exception:
        return False


def check_stale_data() -> tuple[bool, str]:
    """Önbellek verisinin 30 dakikadan eski olup olmadığını kontrol eder."""
    if not DATA_CACHE_FILE.exists():
        return True, "No cache file found. Data is missing."

    file_age_seconds = time.time() - DATA_CACHE_FILE.stat().st_mtime
    if file_age_seconds > 1800:
        minutes_old = int(file_age_seconds / 60)
        return True, f"Warning: Data is stale ({minutes_old} minutes old)."

    return False, "Data is fresh."


# ---------------------------------------------------------
# LIVE AIRPORT & FIDS ENGINE (WITH 15-MIN CACHE & HISTORY LOGGING)
# ---------------------------------------------------------
def fetch_live_airport_data(iata_code="DXB", force_refresh=False):
    """Fetches live data from AeroDataBox only if the last fetch was more than 15 minutes ago, and logs snapshots into flights_history."""
    if not RAPIDAPI_KEY:
        return False, "RapidAPI Key is not defined."

    # 15 Dakika Cache Kontrolü (force_refresh=False ise atla)
    if not force_refresh and DATA_CACHE_FILE.exists():
        file_age_seconds = time.time() - DATA_CACHE_FILE.stat().st_mtime
        if file_age_seconds < 900:
            return True, "Using cached FIDS data."
    elif force_refresh and DATA_CACHE_FILE.exists():
        # Admin Sync Now: cache dosyasını silerek anlık taze veri çekimi zorla
        try:
            DATA_CACHE_FILE.unlink()
        except Exception:
            pass

    url = f"https://aerodatabox.p.rapidapi.com/flights/airports/iata/{iata_code}"
    headers = {"X-RapidAPI-Key": RAPIDAPI_KEY, "X-RapidAPI-Host": "aerodatabox.p.rapidapi.com"}
    params = {
        "offsetMinutes": "-120",
        "durationMinutes": "720",
        "withLeg": "true",
        "direction": "Both",
        "withCancelled": "true",
        "withCodeshared": "true",
        "withCargo": "true",
    }
    try:
        res = requests.get(url, headers=headers, params=params, timeout=15)
        increment_counter("api_requests_this_month", 1)
        if res.status_code == 200:
            res_text = res.text
            with open(DATA_CACHE_FILE, "w", encoding="utf-8") as f:
                f.write(res_text)

            # Veritabanına geçmiş kayıtları ekle ve eski verileri temizle
            try:
                data = json.loads(res_text)
                departures = data.get("departures", [])
                conn = get_db()
                cursor = conn.cursor()
                for dep in departures:
                    fl_num = dep.get("number", "N/A")
                    airline = dep.get("airline", {}).get("name", "Unknown Airline")
                    arr_info = dep.get("arrival", {}).get("airport", {})
                    dest_iata = arr_info.get("iata", "N/A")
                    dest_name = arr_info.get("name", "Unknown")
                    status = dep.get("status", "Expected")
                    dep_info = dep.get("departure", {})
                    sched_time = dep_info.get("scheduledTime", {}).get("local", "")
                    terminal = dep_info.get("terminal", "-")
                    gate = dep_info.get("gate", "-")
                    ac_model = dep.get("aircraft", {}).get("model", "Commercial Jet")

                    cursor.execute(format_query("""
                        INSERT INTO flights_history (flight_num, airline, dest_iata, dest_name, status, scheduled_time, terminal, gate, aircraft_model, recorded_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                    """), (fl_num, airline, dest_iata, dest_name, status, sched_time, terminal, gate, ac_model))

                # 24 saatten eski kayıtları temizleme mekanizması
                cutoff_time = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
                cursor.execute(format_query("DELETE FROM flights_history WHERE recorded_at < ?"), (cutoff_time,))

                conn.commit()
                conn.close()
            except Exception as db_err:
                print(f"[History Log Error]: {db_err}")

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
            data = json.loads(text[: last_bracket + 1] + "]}")
    except Exception:
        data = {
            "departures": [
                {"number": "FZ-821", "airline": {"name": "flydubai Cargo"}, "status": "Canceled",
                 "arrival": {"airport": {"iata": "RUH", "name": "Riyadh King Khalid International", "countryCode": "SA"}},
                 "departure": {"scheduledTime": {"local": "2026-06-06T14:30:00+04:00"}, "terminal": "2", "gate": "14"}},
                {"number": "EK-833", "airline": {"name": "Emirates SkyCargo"}, "status": "Delayed",
                 "arrival": {"airport": {"iata": "DMM", "name": "Dammam King Fahd International", "countryCode": "SA"}},
                 "departure": {"scheduledTime": {"local": "2026-06-06T15:00:00+04:00"}, "terminal": "3", "gate": "B2"}},
                {"number": "QR-101", "airline": {"name": "Qatar Airways"}, "status": "Expected",
                 "arrival": {"airport": {"iata": "DOH", "name": "Doha Hamad International", "countryCode": "QA"}},
                 "departure": {"scheduledTime": {"local": "2026-06-06T16:15:00+04:00"}, "terminal": "1", "gate": "A1"}},
                {"number": "GF-512", "airline": {"name": "Gulf Air"}, "status": "Canceled",
                 "arrival": {"airport": {"iata": "BAH", "name": "Bahrain International", "countryCode": "BH"}},
                 "departure": {"scheduledTime": {"local": "2026-06-06T17:00:00+04:00"}, "terminal": "1", "gate": "C4"}},
                {"number": "WY-603", "airline": {"name": "Oman Air"}, "status": "Expected",
                 "arrival": {"airport": {"iata": "MCT", "name": "Muscat International", "countryCode": "OM"}},
                 "departure": {"scheduledTime": {"local": "2026-06-06T17:45:00+04:00"}, "terminal": "2", "gate": "9"}},
                {"number": "SV-554", "airline": {"name": "Saudia Cargo"}, "status": "Delayed",
                 "arrival": {"airport": {"iata": "JED", "name": "Jeddah King Abdulaziz", "countryCode": "SA"}},
                 "departure": {"scheduledTime": {"local": "2026-06-06T18:30:00+04:00"}, "terminal": "3", "gate": "B8"}},
                {"number": "FZ-825", "airline": {"name": "flydubai"}, "status": "Expected",
                 "arrival": {"airport": {"iata": "KWI", "name": "Kuwait International", "countryCode": "KW"}},
                 "departure": {"scheduledTime": {"local": "2026-06-06T19:00:00+04:00"}, "terminal": "2", "gate": "11"}},
                {"number": "EK-841", "airline": {"name": "Emirates"}, "status": "Expected",
                 "arrival": {"airport": {"iata": "RUH", "name": "Riyadh King Khalid International", "countryCode": "SA"}},
                 "departure": {"scheduledTime": {"local": "2026-06-06T20:15:00+04:00"}, "terminal": "3", "gate": "C12"}}
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
        runway_time = dep_info.get("runwayTime", {}).get("local", "-")
        terminal = dep_info.get("terminal", "-")
        gate = dep_info.get("gate", "-")
        runway = dep_info.get("runway", "-")

        ac = f.get("aircraft", {})
        ac_model = ac.get("model", "Unspecified")
        ac_reg = ac.get("reg", "-")
        ac_modes = ac.get("modeS", "-")

        sched_display = (
            sched_time_raw.split("T")[-1][:5]
            if "T" in sched_time_raw
            else (sched_time_raw[-11:-6] if len(sched_time_raw) >= 11 else sched_time_raw)
        )

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
            "is_cargo": is_cargo,
        })

    if search_query:
        q = search_query.lower()
        all_flights = [fl for fl in all_flights if q in fl["dest_name"].lower() or q in fl["dest_iata"].lower() or q in fl["airline"].lower() or q in fl["flight_num"].lower()]

        # Eğer canlı cache'de bulunamadıysa flights_history tablosundan geçmiş kayıtları sorgulayalım
        if not all_flights:
            try:
                conn = get_db()
                cursor = conn.cursor()
                query_str = format_query("""
                    SELECT flight_num, airline, dest_iata, dest_name, status, scheduled_time, terminal, gate, aircraft_model
                    FROM flights_history
                    WHERE lower(flight_num) LIKE ? OR lower(airline) LIKE ? OR lower(dest_name) LIKE ? OR lower(dest_iata) LIKE ?
                    ORDER BY recorded_at DESC LIMIT 10
                """)
                cursor.execute(query_str, (f"%{q}%", f"%{q}%", f"%{q}%", f"%{q}%"))
                history_rows = cursor.fetchall()
                conn.close()

                for hr in history_rows:
                    all_flights.append({
                        "flight_num": hr["flight_num"],
                        "call_sign": "-",
                        "airline": hr["airline"],
                        "dest_name": hr["dest_name"],
                        "dest_iata": hr["dest_iata"],
                        "dest_icao": "-",
                        "dest_country": "-",
                        "dest_tz": "-",
                        "coords": "-",
                        "time": hr["scheduled_time"][-5:] if hr["scheduled_time"] else "-",
                        "sched_local": hr["scheduled_time"],
                        "sched_utc": "-",
                        "revised_time": "-",
                        "runway_time": "-",
                        "terminal": hr["terminal"],
                        "gate": hr["gate"],
                        "runway": "-",
                        "status": hr["status"],
                        "aircraft_model": hr["aircraft_model"],
                        "aircraft_reg": "-",
                        "aircraft_modes": "-",
                        "is_cargo": False,
                    })
            except Exception as hist_err:
                print(f"[History Query Error]: {hist_err}")

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
                "risk_level": "CRITICAL" if r["canceled"] >= 3 else ("WARNING" if r["canceled"] > 0 else "MODERATE"),
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
        "top_8_flights": top_8_flights,
    }
