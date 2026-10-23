from fastapi import APIRouter, Request, HTTPException, Depends
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from datetime import datetime

from database import get_db, format_query, get_counter
from services import check_telegram_bot_health, check_stale_data

router = APIRouter(prefix="/admin", tags=["Admin"])
templates = Jinja2Templates(directory="templates")

# Yetkili admin e-posta kontrolü (Kendi e-postanızı buraya tanımlayabilirsiniz)
ADMIN_EMAIL = "ayhan.hakyemez@gmail.com"

def verify_admin(request: Request):
    user_email = request.cookies.get("user_email")
    if not user_email or user_email.strip().lower() != ADMIN_EMAIL.lower():
        raise HTTPException(status_code=403, detail="Access denied. Administrator privileges required.")
    return user_email

@router.get("", response_class=HTMLResponse)
async def admin_dashboard(request: Request):
    try:
        admin_email = verify_admin(request)
    except HTTPException:
        return RedirectResponse(url="/auth?mode=login", status_code=303)

    # Admin erişim logunu kaydet (IP ve Zaman)
    client_ip = request.client.host if request.client else "Unknown"
    try:
        conn_ip = get_db()
        cur_ip = conn_ip.cursor()
        cur_ip.execute(format_query("""
            INSERT INTO admin_access_logs (admin_email, ip_address) VALUES (?, ?)
        """), (admin_email, client_ip))
        conn_ip.commit()
        conn_ip.close()
    except Exception as e:
        print(f"[Admin Access Log Error]: {e}")

    # Sistem ve Bot Sağlık Kontrolleri
    bot_healthy = check_telegram_bot_health()
    is_stale, stale_msg = check_stale_data()
    api_usage = get_counter("api_requests_this_month")

    # Veritabanından logları ve kullanıcıları çekme
    conn = get_db()
    cursor = conn.cursor()

    # Kullanıcı Listesi (CRM)
    cursor.execute("SELECT * FROM users ORDER BY id DESC")
    users = cursor.fetchall()

    # API Logları (Son 20 kayıt)
    cursor.execute("SELECT * FROM api_logs ORDER BY id DESC LIMIT 20")
    api_logs = cursor.fetchall()

    # Bildirim Logları (Son 20 kayıt)
    cursor.execute("SELECT * FROM notification_logs ORDER BY id DESC LIMIT 20")
    notif_logs = cursor.fetchall()

    # Hata Logları (Son 20 kayıt)
    cursor.execute("SELECT * FROM error_logs ORDER BY id DESC LIMIT 20")
    error_logs = cursor.fetchall()

    # Admin Erişim Logları (Son 10 kayıt)
    cursor.execute("SELECT * FROM admin_access_logs ORDER BY id DESC LIMIT 10")
    access_logs = cursor.fetchall()

    conn.close()

    context = {
        "request": request,
        "bot_healthy": bot_healthy,
        "is_stale": is_stale,
        "stale_msg": stale_msg,
        "api_usage": api_usage,
        "users": users,
        "api_logs": api_logs,
        "notif_logs": notif_logs,
        "error_logs": error_logs,
        "access_logs": access_logs
    }

    return templates.TemplateResponse(request=request, name="admin_dashboard.html", context=context)