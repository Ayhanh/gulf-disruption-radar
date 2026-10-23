from fastapi import APIRouter, Request, Form, Response, HTTPException
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from datetime import datetime, timedelta
import hashlib
import os

from database import get_db, format_query

router = APIRouter(tags=["Auth"])
templates = Jinja2Templates(directory="templates")

def hash_password(password: str) -> str:
    return hashlib.sha256(password.encode()).hexdigest()

@router.get("/auth")
async def auth_page(request: Request, mode: str = "login"):
    return templates.TemplateResponse(request=request, name="auth.html", context={"request": request, "mode": mode})

@router.post("/auth/login")
async def login_user(response: Response, email: str = Form(...), password: str = Form(...)):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(format_query("SELECT * FROM users WHERE email = ?"), (email.strip().lower(),))
    user = cursor.fetchone()
    conn.close()

    if not user or user["password_hash"] != hash_password(password):
        raise HTTPException(status_code=400, detail="Invalid email or password.")

    # Cookie tabanlı oturum yönetimi (örnek token)
    response = RedirectResponse(url="/", status_code=303)
    response.set_cookie(key="user_email", value=user["email"], httponly=True)
    return response

@router.get("/auth/logout")
async def logout_user(response: Response):
    response = RedirectResponse(url="/", status_code=303)
    response.delete_cookie(key="user_email")
    return response