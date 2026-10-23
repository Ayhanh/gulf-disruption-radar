import os
import time
import json
import requests
from datetime import datetime, date, timedelta
from pathlib import Path
from database import get_db, format_query, get_counter

RAPIDAPI_KEY = os.getenv("RAPIDAPI_KEY", "")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
DATA_CACHE_FILE = Path("rapidapi_API.txt")

def send_admin_email(subject: str, body: str):
    # E-posta gönderim mantığı
    pass

def check_telegram_bot_health() -> bool:
    """Checks if the Telegram Bot token is valid and the API is responsive (Heartbeat)."""
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
    """Checks if the cached/live flight data is older than 30 minutes (Stale Data Check)."""
    if not DATA_CACHE_FILE.exists():
        return True, "No cache file found. Data is missing."
    
    file_age_seconds = time.time() - DATA_CACHE_FILE.stat().st_mtime
    if file_age_seconds > 1800: # 1800 saniye = 30 dakika
        minutes_old = int(file_age_seconds / 60)
        return True, f"Warning: Data is stale ({minutes_old} minutes old)."
    
    return False, "Data is fresh."

def increment_counter(key: str, amount: int = 1):
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
                send_admin_email(
                    "API Quota Exceeded 70% Threshold!",
                    f"Monthly RapidAPI consumption exceeded 1,120 limit.\nCurrent Usage: {current_val} / 1,600"
                )
                if os.getenv("DATABASE_URL"):
                    cursor2.execute("""
                        INSERT INTO app_counters (key, val, last_reset) VALUES ('api_70_sent', 1, %s)
                        ON CONFLICT (key) DO UPDATE SET val = EXCLUDED.val, last_reset = EXCLUDED.last_reset
                    """, (current_month_str,))
                else:
                    cursor2.execute("""
                        INSERT OR REPLACE INTO app_counters (key, val, last_reset) VALUES ('api_70_sent', 1, ?)
                    """, (current_month_str,))
                conn2.commit()
        conn2.close()

def fetch_live_airport_data(iata_code="DXB"):
    if not RAPIDAPI_KEY:
        return False, "RapidAPI Key is not defined."
    
    if DATA_CACHE_FILE.exists():
        file_age_seconds = time.time() - DATA_CACHE_FILE.stat().st_mtime
        if file_age_seconds < 900:
            return True, "Using cached FIDS data."

    url = f"https://aerodatabox.p.rapidapi.com/flights/airports/iata/{iata_code}"
    headers = {"X-RapidAPI-Key": RAPIDAPI_KEY, "X-RapidAPI-Host": "aerodatabox.p.rapidapi.com"}
    params = {
        "offsetMinutes": "-120", "durationMinutes": "720", "withLeg": "true",
        "direction": "Both", "withCancelled": "true", "withCodeshared": "true", "withCargo": "true"
    }
    
    start_time = time.time()
    try:
        res = requests.get(url, headers=headers, params=params, timeout=15)
        response_time_ms = (time.time() - start_time) * 1000
        increment_counter("api_requests_this_month", 1)

        # Log API request to database for observability
        conn_log = get_db()
        cur_log = conn_log.cursor()
        cur_log.execute(format_query("""
            INSERT INTO api_logs (endpoint, status_code, response_time_ms, error_message)
            VALUES (?, ?, ?, ?)
        """), (f"/flights/airports/iata/{iata_code}", res.status_code, response_time_ms, None if res.status_code == 200 else res.text[:255]))
        conn_log.commit()
        conn_log.close()

        if res.status_code == 200:
            res_text = res.text
            with open(DATA_CACHE_FILE, "w", encoding="utf-8") as f:
                f.write(res_text)
            
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
                
                # Cleanup: 24 saatten eski uçuş geçmişini temizle
                cutoff_time = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
                cursor.execute(format_query("DELETE FROM flights_history WHERE recorded_at < ?"), (cutoff_time,))

                conn.commit()
                conn.close()
            except Exception as db_err:
                print(f"[History Log Error]: {db_err}")

            return True, f"Live {iata_code} FIDS data synchronized."
        return False, f"API Error ({res.status_code})"
    except Exception as e:
        response_time_ms = (time.time() - start_time) * 1000
        # Log exception to api_logs & error_logs
        try:
            conn_err = get_db()
            cur_err = conn_err.cursor()
            cur_err.execute(format_query("""
                INSERT INTO api_logs (endpoint, status_code, response_time_ms, error_message)
                VALUES (?, ?, ?, ?)
            """), (f"/flights/airports/iata/{iata_code}", 500, response_time_ms, str(e)[:255]))
            cur_err.execute(format_query("""
                INSERT INTO error_logs (module, error_message, stack_trace)
                VALUES (?, ?, ?)
            """), ("services.fetch_live_airport_data", str(e), ""))
            conn_err.commit()
            conn_err.close()
        except Exception:
            pass
        return False, str(e)