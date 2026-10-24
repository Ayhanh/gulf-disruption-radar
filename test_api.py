import os
import requests
from dotenv import load_dotenv
from pathlib import Path

# .env dosyasını yükle
BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

RAPIDAPI_KEY = os.getenv("RAPIDAPI_KEY", "").strip()

def test_rapidapi_connection():
    print("=" * 60)
    print("🔍 DETAYLI RAPIDAPI (AERODATABOX) HATA TANI TESTİ")
    print("=" * 60)

    if not RAPIDAPI_KEY:
        print("❌ HATA: .env dosyasında 'RAPIDAPI_KEY' bulunamadı veya boş!")
        print("💡 Çözüm: Proje dizinine .env dosyası oluşturup RAPIDAPI_KEY=... ekleyin.")
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
        
        # Rate limit ve kota başlıklarını kontrol et
        print("\n📊 API Yanıt Başlıkları (Headers):")
        for header_key in ["X-RateLimit-Requests-Limit", "X-RateLimit-Requests-Remaining", "X-RateLimit-Requests-Reset", "X-RapidAPI-Proxy-Response"]:
            if header_key in response.headers:
                print(f"   - {header_key}: {response.headers[header_key]}")

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
        print("   -> AeroDataBox sunucusuna 15 saniye içinde ulaşılamadı. İnternet bağlantınızı veya ağ/DNS ayarlarınızı kontrol edin.")
    except requests.exceptions.ConnectionError:
        print("\n❌ BAĞLANTI HATASI (Connection Error):")
        print("   -> DNS çözümlenemedi veya sunucuya TCP bağlantısı kurulamadı. İnternetinizi kontrol edin.")
    except Exception as e:
        print(f"\n❌ BEKLENMEYEN PYTHON İSTİSNASI: {type(e).__name__} -> {str(e)}")

    print("=" * 60)

if __name__ == "__main__":
    test_rapidapi_connection()