import os
import requests
from dotenv import load_dotenv
from pathlib import Path

# .env dosyasını yükle (isterseniz bu satırı tamamen kaldırabilirsiniz)
BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

# API anahtarını buraya doğrudan tırnak içinde yazabilirsiniz:
RAPIDAPI_KEY = "53a45c5132msh5feffd7b6b8efc6p1f26a3jsnb7988c969106".strip()

def test_rapidapi_connection():
    print("=" * 60)
    print("🔍 DETAYLI RAPIDAPI (AERODATABOX) HATA TANI TESTİ")
    print("=" * 60)

    if not RAPIDAPI_KEY or RAPIDAPI_KEY == "BURAYA_KENDI_API_ANAHTARINIZI_YAZIN":
        print("❌ HATA: Lütfen geçerli bir API anahtarı girin!")
        return

    print(f"🔑 Kullanılan API Key (Maskelenmiş): {RAPIDAPI_KEY[:6]}...{RAPIDAPI_KEY[-4:]}")
    print(f"🔑 API Key Uzunluğu: {len(RAPIDAPI_KEY)} karakter")

    url = "https://aerodatabox.p.rapidapi.com/flights/airports/iata/DXB"
    headers = {
        "X-RapidAPI-Key": RAPIDAPI_KEY,
        "X-RapidAPI-Host": "aerodatabox.p.rapidapi.com"
    }
    params = {
        "offsetMinutes": "-60",
        "durationMinutes": "120",
        "direction": "Departure",
        "withCancelled": "true"
    }

    try:
        print("🌐 AeroDataBox sunucusuna test isteği gönderiliyor...")
        response = requests.get(url, headers=headers, params=params, timeout=15)
        
        print(f"📡 HTTP Yanıt Kodu: {response.status_code} ({response.reason})")
        
        if response.status_code == 200:
            data = response.json()
            departures = data.get("departures", [])
            print("\n✅ BAŞARILI! API aktif, anahtar geçerli ve yanıt veriyor.")
            print(f"✈️ Çekilen Uçuş Sayısı: {len(departures)}")
            
            with open("rapidapi_API.txt", "w", encoding="utf-8") as f:
                f.write(response.text)
            print("💾 Yanıt başarıyla 'rapidapi_API.txt' dosyasına yazıldı.")
            
        elif response.status_code == 429:
            print("\n⚠️ AŞIRI İSTEK HATASI (429 Too Many Requests):")
            print("   -> Saniyede/dakikada izin verilen istek sınırını aştınız veya aylık kota doldu[cite: 3].")
            print(f"   -> Sunucu Yanıtı: {response.text}")
        elif response.status_code == 403:
            print("\n❌ YETKİSİZ ERİŞİM HATASI (403 Forbidden):")
            print("   -> API Key yanlış, süresi dolmuş veya RapidAPI üzerinde AeroDataBox servisine aboneliğiniz bulunmuyor.")
            print(f"   -> Sunucu Yanıtı: {response.text}")
        elif response.status_code == 401:
            print("\n❌ KİMLİK DOĞRULAMA HATASI (401 Unauthorized):")
            print("   -> Gönderdiğiniz API anahtarı geçersiz veya hatalı kopyalanmış.")
            print(f"   -> Sunucu Yanıtı: {response.text}")
        else:
            print(f"\n❌ BEKLENMEYEN DURUM KODU: {response.status_code}")
            print(f"   -> Sunucu Yanıt Gövdesi (Body): {response.text}")

    except requests.exceptions.Timeout:
        print("\n❌ ZAMAN AŞIMI HATASI (Timeout):")
        print("   -> AeroDataBox sunucusuna 15 saniye içinde ulaşılamadı.")
    except requests.exceptions.ConnectionError:
        print("\n❌ BAĞLANTI HATASI (Connection Error):")
        print("   -> İnternet bağlantınızı kontrol edin.")
    except Exception as e:
        print(f"\n❌ BEKLENMEYEN PYTHON İSTİSNASI: {type(e).__name__} -> {str(e)}")

    print("=" * 60)

if __name__ == "__main__":
    test_rapidapi_connection()