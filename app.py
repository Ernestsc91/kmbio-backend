# app.py
from flask import Flask, jsonify
from flask_cors import CORS
import requests
from bs4 import BeautifulSoup
from datetime import datetime, timedelta
import os
import re
import json
from apscheduler.schedulers.background import BackgroundScheduler
import pytz # Necesario para la zona horaria del scheduler

app = Flask(__name__)
CORS(app)

# Asegúrate de tener la zona horaria definida
try:
    VENEZUELA_TZ = pytz.timezone("America/Caracas")
except:
    print("Advertencia: pytz no está instalado o no se puede configurar la zona horaria. Usando el timezone por defecto.")
    VENEZUELA_TZ = None

# Tasas de Respaldo
DEFAULT_USD_RATE = 00.01
DEFAULT_EUR_RATE = 00.01
FIXED_UT_RATE = 43.00 # Tasa de respaldo

BCV_URL = "https://www.bcv.org.ve/"

CURRENT_RATES_FILE = 'current_rates.json'
HISTORICAL_RATES_FILE = 'historical_rates.json'

def load_data(file_path, default_data):
    if os.path.exists(file_path):
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except json.JSONDecodeError:
            print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Error: Archivo {file_path} corrupto o vacío. Usando datos predeterminados.")
            return default_data
    return default_data

def save_data(file_path, data):
    with open(file_path, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=4, ensure_ascii=False)

# Inicialización (Añadido last_updated_ut para el timestamp del USDT)
current_rates = load_data(CURRENT_RATES_FILE, {
    "usd": DEFAULT_USD_RATE,
    "eur": DEFAULT_EUR_RATE,
    "ut": FIXED_UT_RATE,
    "last_updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S") + " (predeterminado)",
    "last_updated_ut": datetime.now().strftime("%Y-%m-%d %H:%M:%S") + " (predeterminado)",
    "usd_change_percent": 0.0,
    "eur_change_percent": 0.0
})

historical_rates_data = load_data(HISTORICAL_RATES_FILE, [])

# --- NUEVA FUNCIÓN: Scraping de USDT (Binance P2P) ---
def fetch_usdt_rate():
    """Scrape la tasa de USDT/VES de Binance P2P (tasa de Compra más baja)."""
    try:
        # URL de scraping (confirmada)
        url = 'https://p2p.binance.com/bapi/c2c/v2/public/c2c/adv/search'
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
        }
        payload = {
            "asset": "USDT",
            "fiat": "VES",
            "merchantCheck": True,
            "page": 1,
            "rows": 10,
            "tradeType": "BUY", 
            "filterType": "all"
        }
        
        response = requests.post(url, headers=headers, json=payload, timeout=10)
        response.raise_for_status()
        data = response.json()

        if data['data']:
            rate = float(data['data'][0]['adv']['price'])
            return rate
        else:
            print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Advertencia: No se encontraron anuncios USDT en Binance P2P. Usando tasa de respaldo.")
            return FIXED_UT_RATE
            
    except Exception as e:
        print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Error en scraping Binance P2P: {e}. Usando tasa de respaldo.", flush=True)
        return FIXED_UT_RATE

# --- NUEVA FUNCIÓN: Actualizar solo la tasa USDT (cada 15 minutos) ---
def update_only_usdt_rate():
    global current_rates
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Intentando actualizar solo la tasa USDT...")
    
    usdt_rate = fetch_usdt_rate()
    
    current_rates["ut"] = usdt_rate
    current_rates["last_updated_ut"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    save_data(CURRENT_RATES_FILE, current_rates)
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Tasa USDT actualizada y guardada: UT={usdt_rate}")


# --- FUNCIÓN MODIFICADA: Actualización Diaria (BCV + USDT + Historial) ---
def fetch_and_update_bcv_rates():
    global current_rates, historical_rates_data
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Intentando actualizar tasas BCV + USDT (Actualización diaria)...")
    
    try:
        # **CORRECCIÓN SSL:** Se mantiene verify=False para evitar el crash del certificado en Render.
        response = requests.get(BCV_URL, timeout=15, verify=False)
        response.raise_for_status()

        soup = BeautifulSoup(response.text, 'lxml')

        usd_rate = None
        eur_rate = None

        # Lógica de Scraping BCV (USD)
        usd_container = soup.find('div', id='dolar')
        if usd_container:
            centrado_div_usd = usd_container.find('div', class_='centrado')
            if centrado_div_usd:
                usd_strong_tag = centrado_div_usd.find('strong')
                if usd_strong_tag:
                    match = re.search(r'[\d,\.]+', usd_strong_tag.text)
                    if match:
                        usd_rate = float(match.group(0).replace(',', '.').strip())

        # Lógica de Scraping BCV (EUR)
        eur_container = soup.find('div', id='euro')
        if eur_container:
            centrado_div_eur = eur_container.find('div', class_='centrado')
            if centrado_div_eur:
                eur_strong_tag = centrado_div_eur.find('strong')
                if eur_strong_tag:
                    match = re.search(r'[\d,\.]+', eur_strong_tag.text)
                    if match:
                        eur_rate = float(match.group(0).replace(',', '.').strip())

        if usd_rate is None or eur_rate is None:
            raise ValueError("No se pudieron encontrar las tasas de USD o EUR en la página del BCV.")

        # Cálculo de porcentaje de cambio (manteniendo la lógica original)
        usd_change_percent = 0.0
        eur_change_percent = 0.0
        previous_day_rate_usd = None
        previous_day_rate_eur = None
        
        today_date_str_for_history_check = datetime.now().strftime("%d de %B de %Y")
        for entry in historical_rates_data:
            if entry.get("date") != today_date_str_for_history_check:
                previous_day_rate_usd = entry.get("usd")
                previous_day_rate_eur = entry.get("eur")
                break

        if previous_day_rate_usd is not None and previous_day_rate_usd != 0:
            usd_change_percent = ((usd_rate - previous_day_rate_usd) / previous_day_rate_usd) * 100
        if previous_day_rate_eur is not None and previous_day_rate_eur != 0:
            eur_change_percent = ((eur_rate - previous_day_rate_eur) / previous_day_rate_eur) * 100
        
        # **NUEVO:** Obtener la tasa USDT
        usdt_rate = fetch_usdt_rate() 

        # Actualizar las tasas globales
        current_rates = {
            "usd": usd_rate,
            "eur": eur_rate,
            "ut": usdt_rate,
            "last_updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "last_updated_ut": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "usd_change_percent": round(usd_change_percent, 2),
            "eur_change_percent": round(eur_change_percent, 2)
        }
        save_data(CURRENT_RATES_FILE, current_rates)

        # Actualizar historial
        if not historical_rates_data or historical_rates_data[0]["date"] != today_date_str_for_history_check:
            historical_rates_data.insert(0, {
                "date": today_date_str_for_history_check,
                "usd": usd_rate,
                "eur": eur_rate
            })
            historical_rates_data = historical_rates_data[:15]
            save_data(HISTORICAL_RATES_FILE, historical_rates_data)
        
        print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Tasas actualizadas y guardadas: USD={usd_rate}, EUR={eur_rate}, UT={usdt_rate}")

    except requests.exceptions.Timeout:
        print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Error: Tiempo de espera agotado al conectar con el BCV. Usando tasas guardadas/predeterminadas.")
    except requests.exceptions.RequestException as e:
        print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Error de red o HTTP al conectar con el BCV: {e}. Usando tasas guardadas/predeterminadas.")
    except Exception as e:
        print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Ocurrió un error inesperado durante el scraping: {e}. Usando tasas guardadas/predeterminadas.")

@app.route('/api/bcv-rates', methods=['GET'])
def get_current_bcv_rates():
    return jsonify(current_rates)

@app.route('/api/bcv-history', methods=['GET'])
def get_bcv_history():
    return jsonify(historical_rates_data)

# El scheduler necesita la zona horaria para los cron jobs
scheduler = BackgroundScheduler(timezone="America/Caracas")

if __name__ == '__main__':
    # 1. Ejecución inicial para asegurar que las tasas sean reales al iniciar
    fetch_and_update_bcv_rates()
    
    # 2. Programar la actualización diaria (BCV + USDT + Historial) a las 00:01 AM (Mon-Fri)
    scheduler.add_job(fetch_and_update_bcv_rates, 'cron', hour=0, minute=1, day_of_week='mon-fri')
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Actualización BCV programada para las 00:01 AM (Mon-Vie).")
    
    # 3. Programar la actualización solo de USDT (cada 15 minutos) (24/7)
    scheduler.add_job(update_only_usdt_rate, 'interval', minutes=15)
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Actualización USDT programada cada 15 minutos.")
    
    scheduler.start()

    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)