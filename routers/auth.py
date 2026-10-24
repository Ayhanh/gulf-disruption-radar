import os
import secrets
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from database import (
    format_query,
    get_counter,
    get_current_user,
    get_db,
    get_free_user_count,
    hash_password,
    is_valid_telegram_chat_id,
    verify_password,
)
from services import send_admin_email, send_telegram, send_reset_email

router = APIRouter(tags=["Auth"])
templates = Jinja2Templates(directory="templates")


@router.get("/auth", response_class=HTMLResponse)
async def auth_page(request: Request, mode: Optional[str] = "register"):
    user = get_current_user(request)
    if user:
        return RedirectResponse(url="/", status_code=303)

    context = {
        "request": request,
        "error": None,
        "message": None,
        "initial_tab": "register" if mode == "register" else "login",
        "free_slots_left": max(0, 10 - get_free_user_count()),
    }
    return templates.TemplateResponse(request=request, name="auth.html", context=context)


@router.get("/auth/login")
@router.get("/login")
async def login_redirect():
    return RedirectResponse(url="/auth?mode=login", status_code=303)


@router.get("/auth/register")
@router.get("/register")
async def register_redirect():
    return RedirectResponse(url="/auth?mode=register", status_code=303)


@router.post("/auth/register")
async def register(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    telegram_chat_id: str = Form(...),
):
    email_clean = email.strip().lower()
    chat_id_clean = telegram_chat_id.strip() if telegram_chat_id else ""

    if not chat_id_clean or not is_valid_telegram_chat_id(chat_id_clean):
        context = {
            "request": request,
            "error": "Telegram Chat ID is required for live flight alerts. Please enter a valid numeric Chat ID.",
            "message": None,
            "initial_tab": "register",
            "free_slots_left": max(0, 10 - get_free_user_count()),
        }
        return templates.TemplateResponse(request=request, name="auth.html", context=context)

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(format_query("SELECT id FROM users WHERE lower(email) = ?"), (email_clean,))
    if cursor.fetchone():
        conn.close()
        context = {
            "request": request,
            "error": "This email is already registered.",
            "message": None,
            "initial_tab": "register",
            "free_slots_left": max(0, 10 - get_free_user_count()),
        }
        return templates.TemplateResponse(request=request, name="auth.html", context=context)

    free_count = get_free_user_count()
    ADMIN_EMAILS = [os.getenv("ADMIN_EMAIL", "ayhan.hakyemez@gmail.com").strip().lower()]
    is_pro_val = 1 if email_clean in ADMIN_EMAILS else 0

    if free_count >= 10 and is_pro_val == 0:
        conn.close()
        context = {
            "request": request,
            "error": "Founding Member Free Tier quota (10 users) is full. Please subscribe to Standard Pro.",
            "message": None,
            "initial_tab": "register",
            "free_slots_left": 0,
        }
        return templates.TemplateResponse(request=request, name="auth.html", context=context)

    pwd_hash = hash_password(password)

    from database import USE_PG

    if USE_PG:
        cursor.execute(
            "INSERT INTO users (email, password_hash, telegram_chat_id, is_pro, telegram_credits) VALUES (%s, %s, %s, %s, 10) RETURNING id",
            (email_clean, pwd_hash, chat_id_clean, is_pro_val),
        )
        user_id = cursor.fetchone()["id"]
    else:
        cursor.execute(
            "INSERT INTO users (email, password_hash, telegram_chat_id, is_pro, telegram_credits) VALUES (?, ?, ?, ?, 10)",
            (email_clean, pwd_hash, chat_id_clean, is_pro_val),
        )
        user_id = cursor.lastrowid

    session_id = secrets.token_hex(24)
    cursor.execute(format_query("INSERT INTO sessions (session_id, user_id) VALUES (?, ?)"), (session_id, user_id))
    conn.commit()
    conn.close()

    send_admin_email(
        "New User Registration",
        f"A new user registered to the system:\nEmail: {email_clean}\nTelegram Chat ID: {chat_id_clean}",
    )

    if free_count + 1 == 10 and is_pro_val == 0:
        admin_chat = os.getenv("MY_TELEGRAM_ID", "").strip()
        if admin_chat:
            import asyncio

            asyncio.create_task(
                send_telegram(admin_chat, "🚨 <b>TIME TO UPGRADE TO PAID TIER!</b>\n\n10th Free Founding Member user registered! Quota is now full.")
            )

    import asyncio

    asyncio.create_task(
        send_telegram(
            chat_id_clean,
            "👋 <b>Gulf Disruption Radar Active!</b>\n\nYour corridor & flight disruption alerts are linked successfully. You have 10 complimentary Telegram notification credits.",
        )
    )

    res = RedirectResponse(url="/", status_code=303)
    res.set_cookie("session_id", session_id, max_age=30 * 86400, httponly=True, samesite="lax")
    return res


@router.post("/auth/login")
async def login(request: Request, email: str = Form(...), password: str = Form(...)):
    email_clean = email.strip().lower()
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(format_query("SELECT * FROM users WHERE lower(email) = ?"), (email_clean,))
    user = cursor.fetchone()

    if not user or not verify_password(user["password_hash"], password):
        conn.close()
        context = {
            "request": request,
            "error": "Invalid email or password.",
            "message": None,
            "initial_tab": "login",
            "free_slots_left": max(0, 10 - get_free_user_count()),
        }
        return templates.TemplateResponse(request=request, name="auth.html", context=context)

    target_user_id = user["id"]
    session_id = secrets.token_hex(24)
    cursor.execute(format_query("INSERT INTO sessions (session_id, user_id) VALUES (?, ?)"), (session_id, target_user_id))
    conn.commit()
    conn.close()

    res = RedirectResponse(url="/", status_code=303)
    res.set_cookie("session_id", session_id, max_age=30 * 86400, httponly=True, samesite="lax")
    return res


@router.get("/auth/logout")
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


@router.post("/auth/forgot-password")
async def forgot_password(request: Request, email: str = Form(...)):
    email_clean = email.strip().lower()
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(format_query("SELECT id FROM users WHERE lower(email) = ?"), (email_clean,))
    user = cursor.fetchone()

    if user:
        user_id = user["id"]
        token = secrets.token_urlsafe(32)
        expires_at = datetime.now()

        from datetime import timedelta

        expires_at_str = (expires_at + timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")

        cursor.execute(
            format_query("INSERT INTO password_resets (user_id, token, expires_at) VALUES (?, ?, ?)"),
            (user_id, token, expires_at_str),
        )
        conn.commit()

        APP_BASE_URL = os.getenv("APP_BASE_URL", "https://gulfdisruptionradar.co.uk").rstrip("/")
        reset_url = f"{APP_BASE_URL}/auth/reset-password?token={token}"
        send_reset_email(email_clean, reset_url)

    conn.close()
    context = {
        "request": request,
        "message": "If an account with that email exists, a password reset link has been sent.",
        "error": None,
        "initial_tab": "login",
        "free_slots_left": max(0, 10 - get_free_user_count()),
    }
    return templates.TemplateResponse(request=request, name="auth.html", context=context)


@router.get("/auth/reset-password", response_class=HTMLResponse)
async def reset_password_get(request: Request, token: str):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        format_query("SELECT * FROM password_resets WHERE token = ? AND expires_at > ?"),
        (token, datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
    )
    record = cursor.fetchone()
    conn.close()

    if not record:
        return HTMLResponse("<h3>Invalid or expired password reset link.</h3><p><a href='/auth'>Return to Sign In</a></p>", status_code=400)

    return templates.TemplateResponse(request=request, name="reset_password.html", context={"token": token, "error": None})


@router.post("/auth/reset-password")
async def reset_password_post(
    request: Request,
    token: str = Form(...),
    password: str = Form(...),
    confirm_password: str = Form(...),
):
    if password != confirm_password:
        return templates.TemplateResponse(request=request, name="reset_password.html", context={"token": token, "error": "Passwords do not match."})

    if len(password) < 6:
        return templates.TemplateResponse(request=request, name="reset_password.html", context={"token": token, "error": "Password must be at least 6 characters long."})

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        format_query("SELECT * FROM password_resets WHERE token = ? AND expires_at > ?"),
        (token, datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
    )
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
@router.post("/api/support")
async def support_ticket_api(request: Request):
    data = await request.json()
    user_email = (data.get("email") or "").strip().lower()
    msg = (data.get("message") or "").strip()
    admin_chat = os.getenv("MY_TELEGRAM_ID", "").strip()

    if admin_chat:
        import asyncio

        asyncio.create_task(
            send_telegram(admin_chat, f"🛠️ <b>GULF RADAR SUPPORT</b>\n\nFrom: {user_email}\nMessage: {msg}")
        )
    return {"success": True}
