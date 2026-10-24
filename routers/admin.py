from datetime import datetime
import csv
import io
import os

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates

from database import (
    cleanup_old_logs,
    delete_user,
    format_query,
    get_counter,
    get_current_user,
    get_db,
    get_user_stats,
    suspend_user,
    SYSTEM_METRICS,
)
from services import check_stale_data, check_telegram_bot_health, fetch_live_airport_data

router = APIRouter(prefix="/admin", tags=["Admin"])
templates = Jinja2Templates(directory="templates")


@router.get("/", response_class=HTMLResponse)
async def admin_panel(request: Request):
    user = get_current_user(request)
    if not user or not user.get("is_admin"):
        raise HTTPException(status_code=404, detail="Not Found")

    # Sistem ve Bot Sağlık Kontrolleri
    bot_healthy = check_telegram_bot_health()
    is_stale, stale_msg = check_stale_data()
    api_usage = get_counter("api_requests_this_month")
    user_stats = get_user_stats()

    conn = get_db()
    cursor = conn.cursor()

    cursor.execute("SELECT * FROM users ORDER BY id DESC")
    users = cursor.fetchall()

    api_logs = []
    try:
        cursor.execute("SELECT * FROM api_logs ORDER BY id DESC LIMIT 20")
        api_logs = cursor.fetchall()
    except Exception:
        api_logs = []

    notif_logs = []
    try:
        cursor.execute("SELECT * FROM notification_logs ORDER BY id DESC LIMIT 20")
        notif_logs = cursor.fetchall()
    except Exception:
        notif_logs = []

    error_logs = []
    try:
        cursor.execute("SELECT * FROM error_logs ORDER BY id DESC LIMIT 20")
        error_logs = cursor.fetchall()
    except Exception:
        error_logs = []

    conn.close()

    context = {
        "request": request,
        "bot_healthy": bot_healthy,
        "is_stale": is_stale,
        "stale_msg": stale_msg,
        "api_usage": api_usage,
        "user_stats": user_stats,
        "users": users,
        "api_logs": api_logs,
        "notif_logs": notif_logs,
        "error_logs": error_logs,
        "bot_status": SYSTEM_METRICS.get("bot_status", "Unknown"),
        "last_bot_run": SYSTEM_METRICS.get("last_bot_run"),
        "last_payment_time": SYSTEM_METRICS.get("last_payment_time", "No payments received yet"),
        "last_webhook_status": SYSTEM_METRICS.get("last_webhook_status", "Normal"),
    }

    return templates.TemplateResponse("admin_dashboard.html", {"request": request, **context})


# ---------------------------------------------------------
# ADMIN SYNC NOW - Cache Bypass + Force Fresh FIDS Fetch
# ---------------------------------------------------------
@router.post("/sync-now")
async def admin_sync_now(request: Request):
    """Yetkili admin için 15 dakikalık önbelleği bypass ederek anlık canlı FIDS verisi çeker."""
    user = get_current_user(request)
    if not user or not user.get("is_admin"):
        raise HTTPException(status_code=403, detail="Admin only")

    ADMIN_EMAILS = [os.getenv("ADMIN_EMAIL", "ayhan.hakyemez@gmail.com").strip().lower()]
    if user["email"].lower() not in ADMIN_EMAILS:
        raise HTTPException(status_code=403, detail="Admin only")

    try:
        success, msg = fetch_live_airport_data("DXB", force_refresh=True)
        SYSTEM_METRICS["last_bot_run"] = datetime.now()
        SYSTEM_METRICS["bot_status"] = "Admin Sync Forced"
        if success:
            return {"ok": True, "message": msg}
        return {"ok": False, "message": msg}
    except Exception as e:
        SYSTEM_METRICS["bot_status"] = f"Admin Sync Error: {str(e)}"
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------
# ADMIN API: User Management (Suspend / Delete / Export)
# Hem /admin/api/... hem de /api/admin/... isteklerini karşılayacak şekilde güncellendi
# ---------------------------------------------------------
@router.post("/api/suspend/{user_id}")
@router.post("/api/admin/suspend/{user_id}")
async def api_suspend_user(user_id: int, request: Request):
    user = get_current_user(request)
    if not user or not user.get("is_admin"):
        raise HTTPException(status_code=403, detail="Admin only")
    suspend_user(user_id)
    return {"ok": True}


@router.post("/api/delete/{user_id}")
@router.post("/api/admin/delete/{user_id}")
async def api_delete_user(user_id: int, request: Request):
    user = get_current_user(request)
    if not user or not user.get("is_admin"):
        raise HTTPException(status_code=403, detail="Admin only")
    delete_user(user_id)
    return {"ok": True}


@router.get("/api/export-users")
@router.get("/api/admin/export-users")
async def api_export_users(request: Request):
    user = get_current_user(request)
    if not user or not user.get("is_admin"):
        raise HTTPException(status_code=403, detail="Admin only")

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT id, email, is_pro, telegram_chat_id, telegram_credits, created_at FROM users ORDER BY id DESC")
    rows = cursor.fetchall()
    conn.close()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["id", "email", "is_pro", "telegram_chat_id", "telegram_credits", "created_at"])
    for r in rows:
        writer.writerow([r["id"], r["email"], r["is_pro"], r["telegram_chat_id"] or "", r["telegram_credits"] or 0, r["created_at"]])

    output.seek(0)
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=users_export.csv"},
    )