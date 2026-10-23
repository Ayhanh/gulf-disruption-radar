from fastapi import APIRouter, HTTPException, Request

from database import format_query, get_db, SYSTEM_METRICS
from services import send_admin_email, send_telegram

router = APIRouter(prefix="/api/payments", tags=["Payments"])


@router.post("/webhook")
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

    admin_chat = os_getenv("MY_TELEGRAM_ID", "").strip()
    conn = get_db()
    cursor = conn.cursor()

    if event_type in ["payment.succeeded", "subscription.active", "checkout.session.completed"]:
        SYSTEM_METRICS["last_payment_time"] = datetime_now().strftime("%Y-%m-%d %H:%M:%S")
        cursor.execute(format_query("UPDATE users SET is_pro = 1 WHERE lower(email) = ?"), (customer_email,))
        conn.commit()

        send_admin_email(
            "New Pro Sale (+$29.90)",
            f"Successful Pro subscription sale completed!\nCustomer Email: {customer_email}",
        )

        cursor.execute(format_query("SELECT telegram_chat_id FROM users WHERE lower(email) = ?"), (customer_email,))
        user_row = cursor.fetchone()

        if user_row and user_row["telegram_chat_id"]:
            pro_msg = (
                "🎉 <b>Gulf Disruption Radar | PRO Activated!</b>\n\n"
                "Your account is upgraded to <b>PRO</b>.\n"
                "• Up to 100 corridor queries / day\n"
                "• Up to 50 tracked routes & individual flights\n"
                "• Instant state-change push alerts & PDF reports"
            )
            import asyncio

            asyncio.create_task(send_telegram(user_row["telegram_chat_id"], pro_msg))

        if admin_chat:
            sale_msg = f"💰 <b>NEW GULF RADAR SALE!</b>\n\nCustomer: <code>{customer_email}</code>"
            import asyncio

            asyncio.create_task(send_telegram(admin_chat, sale_msg))

    elif event_type in ["subscription.cancelled", "subscription.expired"]:
        cursor.execute(format_query("UPDATE users SET is_pro = 0 WHERE lower(email) = ?"), (customer_email,))
        conn.commit()

    conn.close()
    return {"status": "success"}


def os_getenv(key, default=""):
    import os

    return os.getenv(key, default)


def datetime_now():
    from datetime import datetime

    return datetime.now()
