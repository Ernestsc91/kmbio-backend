from flask import Flask, jsonify
from flask_cors import CORS
import requests
from bs4 import BeautifulSoup
from datetime import datetime, timedelta
import os
import json
from apscheduler.schedulers.background import BackgroundScheduler
import firebase_admin
from firebase_admin import credentials, firestore
import logging
import pytz
import time
import random

# Configuración de logs
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)
CORS(app)

VENEZUELA_TZ = pytz.timezone("America/Caracas")

# Variables globales en memoria
current_rates_in_memory = {}
historical_rates_in_memory = []
db = None

# --- CONSTANTES ---
BCV_URL = "https://www.bcv.org.ve/"
# Fallbacks por si todo falla
DEFAULT_RATES = {
    "usd": 0.01,
    "eur": 0.01,
    "usdt": 0.01,
    "ut": 43.00,
    "usd_change_percent": 0.0,
    "eur_change_percent": 0.0,
    "usdt_change_percent": 0.0
}

# --- INICIALIZACIÓN FIREBASE ---
try:
    firebase_credentials_json = os.environ.get('FIREBASE_CREDENTIALS_JSON')
    if firebase_credentials_json and not firebase_admin._apps:
        cred = credentials.Certificate(json.loads(firebase_credentials_json)) 
        firebase_admin.initialize_app(cred)
        db = firestore.client()
        logger.info("Firebase inicializado correctamente.")
    elif not firebase_credentials_json:
         logger.warning("ADVERTENCIA: No se encontró variable 'FIREBASE_CREDENTIALS_JSON'.")
except Exception as e:
    logger.error(f"ERROR Firebase: {e}")

# --- FUNCIÓN: Cargar datos guardados al inicio ---
def load_rates_from_firestore():
    global current_rates_in_memory, historical_rates_in_memory
    if db:
        try:
            # Cargar Tasas Actuales
            doc = db.collection('rates').document('current').get()
            if doc.exists:
                current_rates_in_memory = doc.to_dict()
                logger.info("Datos actuales cargados de Firestore.")
            else:
                current_rates_in_memory = DEFAULT_RATES.copy()

            # Cargar Historial
            hist_doc = db.collection('rates').document('history').get()
            if hist_doc.exists and 'data' in hist_doc.to_dict():
                historical_rates_in_memory = hist_doc.to_dict()['data']
                logger.info(f"Historial cargado: {len(historical_rates_in_memory)} entradas.")
        except Exception as e:
            logger.error(f"Error cargando Firestore: {e}")

# --- FUNCIÓN: Binance P2P (Scraper Avanzado) ---
def fetch_binance_usdt():
    """Obtiene precios P2P de Binance imitando un navegador real"""
    url = "https://p2p.binance.com/bapi/c2c/v2/friendly/c2c/adv/search"
    
    # Headers críticos para evitar bloqueo
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "*/*",
        "Accept-Language": "es-ES,es;q=0.9",
        "Content-Type": "application/json",
        "Clienttype": "web",
        "Origin": "https://p2p.binance.com",
        "Referer": "https://p2p.binance.com/es/trade/all/USDT?fiat=VES"
    }

    all_prices = []

    try:
        # Consultar COMPRA y VENTA
        for trade_type in ["BUY", "SELL"]:
            payload = {
                "asset": "USDT",
                "fiat": "VES",
                "merchantCheck": False,
                "page": 1,
                "rows": 15, # Pedimos 15
                "tradeType": trade_type,
                "transAmount": 0,
                "countries": [],
                "proMerchantAds": False,
                "publisherType": None,
                "payTypes": []
            }
            
            # Pequeña pausa para no saturar
            time.sleep(random.uniform(0.2, 0.5))
            
            response = requests.post(url, json=payload, headers=headers, timeout=10)
            
            if response.status_code == 200:
                data = response.json()
                if data.get("code") == "000000" and "data" in data:
                    ads = data["data"]
                    for ad in ads:
                        if "adv" in ad and "price" in ad["adv"]:
                            try:
                                price = float(ad["adv"]["price"])
                                if price > 0:
                                    all_prices.append(price)
                            except:
                                continue
                else:
                    logger.warning(f"Binance devolvió data vacía para {trade_type}")
            else:
                logger.error(f"Binance Error HTTP: {response.status_code}")

        # Calcular promedio total
        if all_prices:
            avg_price = sum(all_prices) / len(all_prices)
            logger.info(f"Binance Promedio Calculado ({len(all_prices)} ofertas): {avg_price}")
            return avg_price
        
        return None

    except Exception as e:
        logger.error(f"Error crítico en Binance: {e}")
        return None

# --- LÓGICA DE ACTUALIZACIÓN ---
def update_rates_logic(only_usdt=False):
    global current_rates_in_memory, historical_rates_in_memory
    
    # Asegurar datos base
    if not current_rates_in_memory:
        load_rates_from_firestore()
    
    # 1. Obtener valores actuales (para no perderlos si falla uno)
    usd_rate = current_rates_in_memory.get('usd', 0.01)
    eur_rate = current_rates_in_memory.get('eur', 0.01)
    usdt_rate = current_rates_in_memory.get('usdt', 0.01)

    # 2. Actualizar USDT (Siempre intentar)
    new_usdt = fetch_binance_usdt()
    if new_usdt and new_usdt > 1.0:
        usdt_rate = new_usdt
    else:
        logger.warning("Fallo al obtener USDT, manteniendo valor anterior.")

    # 3. Actualizar BCV (Solo si NO es solo USDT)
    if not only_usdt:
        try:
            resp = requests.get(BCV_URL, timeout=30, verify=False) # verify=False para evitar errores SSL en algunos hostings
            if resp.status_code == 200:
                soup = BeautifulSoup(resp.text, 'lxml')
                
                usd_div = soup.find('div', id='dolar')
                if usd_div:
                    val = usd_div.find('strong').text.strip().replace(',', '.')
                    usd_rate = float(val)

                eur_div = soup.find('div', id='euro')
                if eur_div:
                    val = eur_div.find('strong').text.strip().replace(',', '.')
                    eur_rate = float(val)
                
                logger.info(f"BCV Actualizado: USD={usd_rate}, EUR={eur_rate}")
            else:
                logger.error("Error conectando a BCV web.")
        except Exception as e:
            logger.error(f"Error scraping BCV: {e}")

    # 4. Calcular Porcentajes (Comparación con el día anterior)
    usd_pct, eur_pct, usdt_pct = 0.0, 0.0, 0.0
    today_str = datetime.now(VENEZUELA_TZ).strftime("%d de %B de %Y")
    
    prev_entry = None
    
    # Buscar la entrada más reciente que NO sea de hoy
    if historical_rates_in_memory:
        for entry in historical_rates_in_memory:
            if entry.get("date") != today_str:
                prev_entry = entry
                break
    
    if prev_entry:
        p_usd = float(prev_entry.get("usd", 0))
        p_eur = float(prev_entry.get("eur", 0))
        p_usdt = float(prev_entry.get("usdt", 0))

        if p_usd > 0: usd_pct = ((usd_rate - p_usd) / p_usd) * 100
        if p_eur > 0: eur_pct = ((eur_rate - p_eur) / p_eur) * 100
        if p_usdt > 0: usdt_pct = ((usdt_rate - p_usdt) / p_usdt) * 100
    
    # 5. Guardar en Memoria
    new_data = {
        "usd": usd_rate,
        "eur": eur_rate,
        "usdt": usdt_rate,
        "ut": DEFAULT_RATES["ut"],
        "last_updated": datetime.now(VENEZUELA_TZ).strftime("%Y-%m-%d %H:%M:%S"),
        "usd_change_percent": round(usd_pct, 2),
        "eur_change_percent": round(eur_pct, 2),
        "usdt_change_percent": round(usdt_pct, 2)
    }
    current_rates_in_memory = new_data

    # 6. Guardar en Firestore
    if db:
        try:
            db.collection('rates').document('current').set(current_rates_in_memory)
            
            # Solo guardar historial si es actualización completa (BCV)
            if not only_usdt:
                should_save = False
                if not historical_rates_in_memory:
                    should_save = True
                elif historical_rates_in_memory[0].get("date") != today_str:
                    should_save = True
                
                if should_save:
                    new_hist_entry = {
                        "date": today_str,
                        "usd": usd_rate,
                        "eur": eur_rate,
                        "usdt": usdt_rate
                    }
                    historical_rates_in_memory.insert(0, new_hist_entry)
                    # Mantener solo últimos 30 días
                    historical_rates_in_memory = historical_rates_in_memory[:30]
                    db.collection('rates').document('history').set({'data': historical_rates_in_memory})
                    logger.info("Historial actualizado en Firestore.")
        except Exception as e:
            logger.error(f"Error guardando Firestore: {e}")

# Jobs
def job_daily_bcv():
    update_rates_logic(only_usdt=False)

def job_usdt_update():
    update_rates_logic(only_usdt=True)

# Rutas
@app.route('/', methods=['GET'])
def index():
    return "API Kmbio Vzla v2.0 OK", 200

@app.route('/api/bcv-rates', methods=['GET'])
def get_rates():
    if not current_rates_in_memory:
        load_rates_from_firestore()
    # Si sigue vacío (primer despliegue), intentar fetch
    if not current_rates_in_memory:
        job_daily_bcv()
    return jsonify(current_rates_in_memory)

@app.route('/api/bcv-history', methods=['GET'])
def get_history():
    if not historical_rates_in_memory:
        load_rates_from_firestore()
    return jsonify(historical_rates_in_memory)

# Arranque
try:
    load_rates_from_firestore()
    scheduler = BackgroundScheduler(timezone="America/Caracas")
    if not scheduler.running:
        # BCV: 6am, 1pm, 6pm (para asegurar cambios durante el día)
        scheduler.add_job(job_daily_bcv, 'cron', hour=6, minute=5)
        scheduler.add_job(job_daily_bcv, 'cron', hour=13, minute=5)
        scheduler.add_job(job_daily_bcv, 'cron', hour=18, minute=5)
        
        # USDT: Cada 10 minutos
        scheduler.add_job(job_usdt_update, 'interval', minutes=10)
        
        scheduler.start()
        logger.info("Scheduler iniciado.")
except Exception as e:
    logger.error(f"Error scheduler: {e}")

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)