import asyncio
import io
import logging
import os
import secrets
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional
from collections import defaultdict

from dotenv import load_dotenv
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, HRFlowable

from database import (
    cleanup_old_logs,
    format_query,
    get_counter,
    get_current_user,
    get_db,
    get_daily_search_count,
    get_free_user_count,
    increment_counter,
    increment_search_count,
    init_db,
    SYSTEM_METRICS,
    USE_PG,
)
from services import (
    DATA_CACHE_FILE,
    fetch_live_airport_data,
    parse_radar_data,
    send_admin_email,
    send_telegram,
)
from routers.admin import router as admin_router
from routers.auth import router as auth_router
from routers.payments import router as payments_router

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("gulf-radar")

RAPIDAPI_KEY = os.getenv("RAPIDAPI_KEY", "").strip()
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
APP_BASE_URL = os.getenv("APP_BASE_URL", "https://gulfdisruptionradar.co.uk").rstrip("/")
DEBUG_MODE = os.getenv("DEBUG", "false").strip().lower() in {"1", "true", "yes", "on"}
ADMIN_EMAILS = [os.getenv("ADMIN_EMAIL", "").strip().lower()]

RADAR_PRO_MONTHLY = os.getenv("Radar_Pro_Monthly", "").strip()
RADAR_PRO_ANNUAL = os.getenv("Radar_Pro_Annual", "").strip()

app = FastAPI(title="Gulf Disruption Radar")

# ---------------------------------------------------------
# CSRF PROTECTION (Double-Submit Cookie Pattern)
# ---------------------------------------------------------
CSRF_EXEMPT_PATHS = {"/api/payments/webhook"}


@app.middleware("http")
async def csrf_middleware(request: Request, call_next):
    if request.method in {"GET", "HEAD", "OPTIONS"}:
        response = await call_next(request)
        csrf_token = request.cookies.get("csrf_token")
        if not csrf_token:
            csrf_token = secrets.token_urlsafe(32)
            response.set_cookie(
                "csrf_token",
                csrf_token,
                httponly=True,
                samesite="lax",
                secure=False,
                path="/",
            )
        return response

    if request.url.path in CSRF_EXEMPT_PATHS:
        return await call_next(request)

    cookie_token = request.cookies.get("csrf_token")

    header_token = request.headers.get("x-csrf-token", "")
    form_token = None

    content_type = request.headers.get("content-type", "")
    if content_type.startswith("application/x-www-form-urlencoded"):
        try:
            form_data = await request.form()
            form_token = form_data.get("csrf_token")
        except Exception:
            pass

    token = header_token or form_token

    if not cookie_token or not token or not secrets.compare_digest(cookie_token, token):
        raise HTTPException(status_code=403, detail="CSRF token missing or invalid")

    response = await call_next(request)
    return response

static_dir = BASE_DIR / "static"
static_dir.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

# ---------------------------------------------------------
# ROUTERS
# ---------------------------------------------------------
app.include_router(auth_router)
app.include_router(admin_router)
app.include_router(payments_router)

# ---------------------------------------------------------
# BACKGROUND MONITOR (WITH CORRIDOR RISK MATCHING & HEARTBEAT)
# ---------------------------------------------------------
# AeroDataBox heartbeat: 3 ardışık hata/429 sonrası uyarı
_API_ERROR_COUNT = 0


async def automated_radar_monitor():
    global _API_ERROR_COUNT
    while True:
        try:
            success, msg = fetch_live_airport_data("DXB")
            if success:
                _API_ERROR_COUNT = 0
                SYSTEM_METRICS["bot_status"] = "Active"
            else:
                _API_ERROR_COUNT += 1
                SYSTEM_METRICS["bot_status"] = f"API Error ({msg})"
                if _API_ERROR_COUNT >= 3:
                    SYSTEM_METRICS["bot_status"] = "CRITICAL: 3+ consecutive API failures"

            SYSTEM_METRICS["last_bot_run"] = datetime.now()

            radar = parse_radar_data()
            current_flight_map = {fl["flight_num"]: fl for fl in radar["all_flights"]}

            critical_iata_set = {r["iata"] for r in radar["critical_routes"] if r["risk_level"] == "CRITICAL"}

            conn = get_db()
            cursor = conn.cursor()

            cursor.execute(
                """
                SELECT tf.id, tf.user_id, tf.flight_num, tf.airline, tf.destination_name, tf.last_status,
                       u.email, u.telegram_chat_id, u.is_pro, u.telegram_credits, u.created_at
                FROM tracked_flights tf
                JOIN users u ON tf.user_id = u.id
            """
            )
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
                    fl_obj = current_flight_map[f_num]
                    new_status = fl_obj["status"]
                    dest_iata = fl_obj["dest_iata"]

                    if new_status != old_status:
                        cursor.execute(format_query("UPDATE tracked_flights SET last_status = ? WHERE id = ?"), (new_status, tf["id"]))
                        conn.commit()

                        if new_status in ["Canceled", "Delayed"]:
                            if not is_pro:
                                new_credits = max(0, credits_left - 1)
                                cursor.execute(format_query("UPDATE users SET telegram_credits = ? WHERE id = ?"), (new_credits, tf["user_id"]))
                                conn.commit()

                            if tf["telegram_chat_id"]:
                                alert_msg = (
                                    "🚨 <b>GULF DISRUPTION RADAR | FLIGHT ALERT</b>\n\n"
                                    f"✈️ <b>Flight:</b> <code>{f_num}</code> ({tf['airline']})\n"
                                    f"📍 <b>Route:</b> DXB ➔ {tf['destination_name']}\n"
                                    f"⚠️ <b>Status Changed:</b> <b>{new_status.upper()}</b> (Prev: {old_status})\n"
                                    f"🕒 <b>Scheduled:</b> {fl_obj['time']} GST\n"
                                    f"🚪 <b>Terminal/Gate:</b> T{fl_obj['terminal']} / G-{fl_obj['gate']}"
                                )
                                await send_telegram(tf["telegram_chat_id"], alert_msg)

                    elif dest_iata in critical_iata_set and new_status == "Expected":
                        if tf["telegram_chat_id"]:
                            pass

            conn.close()

            # Periyodik temizleme: her 24 saatte bir (yaklaşık 12 monitor döngüsü @ 2 saat)
            if hasattr(automated_radar_monitor, "_cleanup_counter"):
                automated_radar_monitor._cleanup_counter += 1
            else:
                automated_radar_monitor._cleanup_counter = 1

            if automated_radar_monitor._cleanup_counter >= 12:
                cleanup_old_logs()
                automated_radar_monitor._cleanup_counter = 0

        except Exception as e:
            _API_ERROR_COUNT += 1
            SYSTEM_METRICS["bot_status"] = f"Error: {str(e)}"
            logger.exception("Monitor exception: %s", e)
        await asyncio.sleep(7200)  # 2 saatlik periyot


@app.on_event("startup")
async def startup_event():
    init_db()
    asyncio.create_task(automated_radar_monitor())

# ---------------------------------------------------------
# PDF REPORT EXPORT (PRO) - BLOOMBERG TERMINAL STYLE (ENGLISH)
# ---------------------------------------------------------
@app.get("/export-pdf")
async def export_pdf(request: Request):
    user = get_current_user(request)
    if not user:
        return RedirectResponse(url="/auth?mode=login", status_code=303)

    if not user.get("is_pro"):
        return RedirectResponse(url="/?limit_reached=1", status_code=303)

    radar = parse_radar_data()

    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=letter, rightMargin=36, leftMargin=36, topMargin=36, bottomMargin=36)

    elements = []
    styles = getSampleStyleSheet()

    primary_color = colors.HexColor("#0f172a")
    accent_color = colors.HexColor("#0284c7")
    text_color = colors.HexColor("#334155")

    title_style = ParagraphStyle("DocTitle", parent=styles["Heading1"], fontName="Helvetica-Bold", fontSize=14, textColor=primary_color, spaceAfter=4)

    meta_style = ParagraphStyle("DocMeta", parent=styles["Normal"], fontName="Helvetica", fontSize=8, textColor=colors.HexColor("#64748b"), alignment=2)

    section_heading = ParagraphStyle("SectionHeading", parent=styles["Heading2"], fontName="Helvetica-Bold", fontSize=11, textColor=primary_color, spaceBefore=12, spaceAfter=6)

    body_style = ParagraphStyle("BodyDark", parent=styles["Normal"], fontName="Helvetica", fontSize=9, textColor=text_color, spaceAfter=6)

    table_header_style = ParagraphStyle("TableHeader", parent=styles["Normal"], fontName="Helvetica-Bold", fontSize=8, textColor=colors.white)

    table_cell_style = ParagraphStyle("TableCell", parent=styles["Normal"], fontName="Helvetica", fontSize=8, textColor=text_color)

    current_time_str = datetime.now().strftime("Operational Risk Report - %B %d, %Y | %H:%M GST")
    header_data = [[Paragraph("<b>GULF DISRUPTION RADAR</b>", title_style), Paragraph(current_time_str, meta_style)]]
    header_table = Table(header_data, colWidths=[250, 290])
    header_table.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("BOTTOMPADDING", (0, 0), (-1, -1), 0)]))

    elements.append(header_table)
    elements.append(HRFlowable(width="100%", thickness=1, color=accent_color, spaceBefore=6, spaceAfter=12))

    elements.append(Paragraph("Executive Summary", section_heading))
    summary_text = f"In the last 24 hours across monitored corridors, a total of <b>{radar['canceled']}</b> cancellations and <b>{radar['delayed']}</b> serious delays have been detected. Operational risk distribution is summarized below."
    elements.append(Paragraph(summary_text, body_style))
    elements.append(Spacer(1, 6))

    elements.append(Paragraph("Critical Corridor Disruption Table", section_heading))
    crit_table_data = [[Paragraph("Route / IATA", table_header_style), Paragraph("Destination Name", table_header_style), Paragraph("Canceled", table_header_style), Paragraph("Delayed", table_header_style), Paragraph("Risk Level", table_header_style)]]

    if radar["critical_routes"]:
        for r in radar["critical_routes"][:8]:
            crit_table_data.append([
                Paragraph(f"<b>DXB ➔ {r['iata']}</b>", table_cell_style),
                Paragraph(r["name"], table_cell_style),
                Paragraph(str(r["canceled"]), table_cell_style),
                Paragraph(str(r["delayed"]), table_cell_style),
                Paragraph(f"<font color='{'#e11d48' if r['risk_level'] == 'CRITICAL' else '#d97706'}'><b>{r['risk_level']}</b></font>", table_cell_style),
            ])
    else:
        crit_table_data.append([Paragraph("No critical routes found.", table_cell_style), "", "", "", ""])

    c_table = Table(crit_table_data, colWidths=[80, 200, 60, 60, 140])
    c_table.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, 0), primary_color), ("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("BOTTOMPADDING", (0, 0), (-1, -1), 5), ("TOPPADDING", (0, 0), (-1, -1), 5), ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#cbd5e1"))]))
    elements.append(c_table)
    elements.append(Spacer(1, 10))

    elements.append(Paragraph("Affected / Critical Flights List", section_heading))
    flight_table_data = [[Paragraph("Flight No", table_header_style), Paragraph("Airline", table_header_style), Paragraph("Destination", table_header_style), Paragraph("Scheduled Time", table_header_style), Paragraph("Status", table_header_style)]]

    disrupted_flights = [f for f in radar["all_flights"] if f["status"] in ["Canceled", "Delayed"]][:12]
    if disrupted_flights:
        for f in disrupted_flights:
            status_color = "#e11d48" if f["status"] == "Canceled" else "#d97706"
            flight_table_data.append([
                Paragraph(f"<b>{f['flight_num']}</b>", table_cell_style),
                Paragraph(f["airline"], table_cell_style),
                Paragraph(f"{f['dest_name']} ({f['dest_iata']})", table_cell_style),
                Paragraph(f["time"], table_cell_style),
                Paragraph(f"<font color='{status_color}'><b>{f['status']}</b></font>", table_cell_style),
            ])
    else:
        flight_table_data.append([Paragraph("No active disruption or delay records found.", table_cell_style), "", "", "", ""])

    f_table = Table(flight_table_data, colWidths=[70, 130, 160, 70, 110])
    f_table.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, 0), primary_color), ("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("BOTTOMPADDING", (0, 0), (-1, -1), 4), ("TOPPADDING", (0, 0), (-1, -1), 4), ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#cbd5e1"))]))
    elements.append(f_table)

    def add_footer(canvas, doc):
        canvas.saveState()
        canvas.setFont("Helvetica", 8)
        canvas.setFillColor(colors.HexColor("#64748b"))
        footer_text = "Generated by Gulf Disruption Radar. Powered by AeroDataBox. Confidential."
        canvas.drawCentredString(letter[0] / 2.0, 20, footer_text)
        canvas.restoreState()

    doc.build(elements, onFirstPage=add_footer, onLaterPages=add_footer)
    buffer.seek(0)
    filename = f"Gulf_Disruption_Risk_Report_{date.today().isoformat()}.pdf"
    return StreamingResponse(iter([buffer.getvalue()]), media_type="application/pdf", headers={"Content-Disposition": f"attachment; filename={filename}"})

# ---------------------------------------------------------
# PWA ENDPOINTS
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
# HOME & QUOTA CONTROLS
# ---------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
async def index(request: Request, q: Optional[str] = None):
    user = get_current_user(request)
    client_ip = request.client.host if request.client else "127.0.0.1"

    real_pro = bool(user and user.get("is_pro") == 1 and not user.get("is_trial"))

    identifier = f"user_{user['id']}" if user else f"guest_{client_ip}"
    daily_search_count = get_daily_search_count(identifier)

    if real_pro:
        search_limit = 100
        search_limit_reached = False
        remaining_searches = "100/day"
    else:
        search_limit = 5
        search_limit_reached = daily_search_count >= search_limit
        remaining_searches = max(0, search_limit - daily_search_count)

    if q and not search_limit_reached:
        increment_search_count(identifier)
        if not real_pro:
            daily_search_count += 1
            remaining_searches = max(0, search_limit - daily_search_count)
            search_limit_reached = daily_search_count >= search_limit

    radar_data = parse_radar_data(search_query=q or "")

    watchlist = []
    tracked_flights = []
    watchlist_limit_reached = False

    is_pro = bool(user and user.get("is_pro") == 1)

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

    context = {
        "request": request,
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
    return templates.TemplateResponse(request=request, name="index.html", context=context)


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
        cursor.execute(format_query("INSERT INTO watchlist (user_id, route_code, destination_name) VALUES (?, ?, ?)"), (user["id"], route_code.strip().upper(), destination_name.strip()))
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
    status: str = Form("Expected"),
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
            (user["id"], flight_num.strip(), airline.strip(), destination_name.strip(), scheduled_time.strip(), status.strip()),
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
# TRUST CENTER & SECURITY PAGE
# ---------------------------------------------------------
@app.get("/security", response_class=HTMLResponse)
async def security_page(request: Request):
    user = get_current_user(request)
    context = {"request": request, "user": user, "free_slots_left": max(0, 10 - get_free_user_count())}
    return templates.TemplateResponse(request=request, name="security.html", context=context)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app:app", host="127.0.0.1", port=8000, reload=True)
