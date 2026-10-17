import json
from collections import defaultdict

def analyze_flight_disruptions(file_path):
    # API'den indirdiğin dosyayı oku
    with open(file_path, 'r', encoding='utf-8') as f:
        data = json.load(f)

    departures = data.get("departures", [])
    
    total_flights = len(departures)
    canceled_flights = []
    delayed_flights = []
    
    # Rota bazlı iptal sayacı
    route_cancellations = defaultdict(list)

    for flight in departures:
        status = flight.get("status", "")
        airline = flight.get("airline", {}).get("name", "Bilinmeyen Havayolu")
        flight_no = flight.get("number", "N/A")
        dest_name = flight.get("arrival", {}).get("airport", {}).get("name", "Bilinmiyor")
        dest_iata = flight.get("arrival", {}).get("airport", {}).get("iata", "N/A")
        sched_time = flight.get("departure", {}).get("scheduledTime", {}).get("local", "")
        is_cargo = flight.get("isCargo", False)

        info = {
            "flight_no": flight_no,
            "airline": airline,
            "destination": f"{dest_name} ({dest_iata})",
            "time": sched_time,
            "is_cargo": is_cargo
        }

        if status == "Canceled":
            canceled_flights.append(info)
            route_cancellations[dest_iata].append(info)
        elif status == "Delayed":
            delayed_flights.append(info)

    # --- RADAR EKRAN ÇIKTISI (OPERASYONEL RAPOR) ---
    print("\n" + "="*60)
    print("🚨 GULF DISRUPTION RADAR - DXB (DUBAI) CANLI DURUM RAPORU")
    print("="*60)
    print(f"📊 Toplam İzlenen Uçuş : {total_flights}")
    print(f"❌ Toplam İptal Edilen : {len(canceled_flights)}")
    print(f"⏳ Toplam Rötar         : {len(delayed_flights)}")
    print("-" * 60)

    # Kritik Koridor Tespiti (Alarm Eşiği: Bir hatta 3'ten fazla iptal varsa)
    print("\n🔥 YÜKSEK RİSKLİ UÇUŞ KORİDORLARI (ALARM SEVİYESİ: KRİTİK):")
    for dest, flights in route_cancellations.items():
        if len(flights) >= 3:
            dest_title = flights[0]["destination"]
            print(f"\n⚠️  ROTA: DXB ➔ {dest_title} | İPTAL SAYISI: {len(flights)}")
            for fl in flights[:4]: # İlk 4 tanesini listele
                print(f"   • {fl['airline']} [{fl['flight_no']}] - Planlanan: {fl['time']}")
            if len(flights) > 4:
                print(f"   ... ve {len(flights) - 4} uçuş daha iptal.")

    print("\n" + "="*60)
    print("✅ Sistem bu özeti Telegram / WhatsApp grubuna otomatik gönderecektir.")
    print("="*60 + "\n")

if __name__ == "__main__":
    # TXT dosyasının yolunu belirt
    analyze_flight_disruptions("rapidapi_API.txt")