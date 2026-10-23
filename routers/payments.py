from fastapi import APIRouter, Request, HTTPException
from database import get_db, format_query

router = APIRouter(prefix="/api/payments", tags=["Payments"])

@router.post("/webhook")
async def dodo_webhook(request: Request):
    try:
        payload = await request.json()
        # Dodo Payments webhook doğrulama ve abonelik güncelleme mantığı
        event_type = payload.get("type")
        data = payload.get("data", {})
        
        if event_type == "payment.successful":
            customer_email = data.get("customer", {}).get("email")
            if customer_email:
                conn = get_db()
                cursor = conn.cursor()
                cursor.execute(format_query("UPDATE users SET is_pro = 1 WHERE email = ?"), (customer_email.strip().lower(),))
                conn.commit()
                conn.close()
                
        return {"status": "success"}
    except Exception as e:
        print(f"[Webhook Error]: {e}")
        raise HTTPException(status_code=400, detail=str(e))