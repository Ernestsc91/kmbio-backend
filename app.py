from flask import Flask, jsonify
from flask_cors import CORS
import requests
from bs4 import BeautifulSoup
from datetime import datetime
import os
import re
import json
from apscheduler.schedulers.background import BackgroundScheduler
import firebase_admin
from firebase_admin import credentials, firestore
import logging
import pytz

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
DEFAULT_USD_RATE = 0.01
DEFAULT_EUR_RATE = 0.01
DEFAULT_USDT_RATE = 0.01
FIXED_UT_RATE = 43.00 # Valor fijo UT
BCV_URL = "https://www.bcv.org.ve/"

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
            doc = db.collection('rates').document('current').get()
            if doc.exists:
                current_rates_in_memory = doc.to_dict()
                logger.info("Datos cargados de Firestore.")
            
            hist_doc = db.collection('rates').document('history').get()
            if hist_doc.exists and 'data' in hist_doc.to_dict():
                historical_rates_in_memory = hist_doc.to_dict()['data']
        except Exception as e:
            logger.error(f"Error cargando Firestore: {e}")

# --- FUNCIÓN: Binance P2P (Promedio 15 órdenes) ---
def fetch_binance_usdt():
    """Calcula promedio de USDT/VES (15 Buy + 15 Sell) de Binance P2P"""
    url = "https://p2p.binance.com/bapi/c2c/v2/public/c2c/adv/search"
    
    # --- CORRECCIÓN 1: HEADERS COMPLETOS ---
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/114.0.0.0 Safari/537.36",
        "Origin": "https://p2p.binance.com",
        "Referer": "https://p2p.binance.com/en/trade/all/USDT",
        "Cache-Control": "no-cache"
    }
    
    prices = []
    
    try:
        for trade_type in ["BUY", "SELL"]:
            # --- CORRECCIÓN 2: PAYLOAD ROBUSTO ---
            payload = {
                "asset": "USDT",
                "fiat": "VES",
                "merchantCheck": False,
                "page": 1,
                "rows": 15,
                "tradeType": trade_type,
                "publisherType": None,
                "payTypes": [],      # Importante agregarlo
                "countries": []      # Importante agregarlo
            }
            
            # Timeout aumentado ligeramente a 15s por si hay latencia en la API
            resp = requests.post(url, headers=headers, json=payload, timeout=15)
            
            # Verificamos si la respuesta es exitosa
            if resp.status_code == 200:
                data = resp.json()
                
                # Binance devuelve 'code': '000000' si es exitoso
                if data and "data" in data and isinstance(data["data"], list):
                    for ad in data["data"]:
                        # Validamos que exista la estructura 'adv' y 'price'
                        if "adv" in ad and "price" in ad["adv"]:
                            prices.append(float(ad["adv"]["price"]))
                else:
                    logger.warning(f"Binance devolvió estructura inesperada para {trade_type}: {data.get('message', 'Sin mensaje')}")
            else:
                logger.error(f"Error HTTP Binance {resp.status_code}: {resp.text}")
        
        if prices:
            avg_price = sum(prices) / len(prices)
            logger.info(f"Binance USDT Promedio ({len(prices)} órdenes): {avg_price}")
            return avg_price
        
        logger.warning("No se encontraron precios en Binance.")
        return None

    except Exception as e:
        logger.error(f"Excepción en Binance: {e}")
        return None

# --- LÓGICA DE ACTUALIZACIÓN ---
def update_rates_logic(only_usdt=False):
    global current_rates_in_memory, historical_rates_in_memory
    
    # 1. Recuperar valores actuales o usar default
    usd_rate = current_rates_in_memory.get('usd', DEFAULT_USD_RATE)
    eur_rate = current_rates_in_memory.get('eur', DEFAULT_EUR_RATE)
    usdt_rate = current_rates_in_memory.get('usdt', DEFAULT_USDT_RATE)

    # 2. Actualizar USDT (Siempre que se ejecute esta función)
    new_usdt = fetch_binance_usdt()
    if new_usdt:
        usdt_rate = new_usdt

    # 3. Actualizar BCV (Solo si NO es solo USDT, es decir, actualización diaria)
    if not only_usdt:
        try:
            # verify=False es necesario en Render para BCV
            resp = requests.get(BCV_URL, timeout=30, verify=False)
            resp.raise_for_status()
            soup = BeautifulSoup(resp.text, 'lxml')

            # Buscar USD
            usd_div = soup.find('div', id='dolar')
            if usd_div:
                val = usd_div.find('strong').text.strip().replace(',', '.')
                usd_rate = float(val)

            # Buscar EUR
            eur_div = soup.find('div', id='euro')
            if eur_div:
                val = eur_div.find('strong').text.strip().replace(',', '.')
                eur_rate = float(val)
                
            logger.info(f"BCV Scrapeado: USD={usd_rate}, EUR={eur_rate}")
        except Exception as e:
            logger.error(f"Error BCV scraping: {e}")

    # 4. Calcular Porcentajes de Cambio (Vs Ayer)
    usd_pct, eur_pct, usdt_pct = 0.0, 0.0, 0.0
    today_str = datetime.now(VENEZUELA_TZ).strftime("%d de %B de %Y")
    
    prev_usd, prev_eur, prev_usdt = None, None, None
    
    # Buscar en historial el día diferente a hoy
    if historical_rates_in_memory:
        for entry in historical_rates_in_memory:
            if entry.get("date") != today_str:
                prev_usd = entry.get("usd")
                prev_eur = entry.get("eur")
                prev_usdt = entry.get("usdt")
                break
    
    if prev_usd: usd_pct = ((usd_rate - prev_usd) / prev_usd) * 100
    if prev_eur: eur_pct = ((eur_rate - prev_eur) / prev_eur) * 100
    if prev_usdt: usdt_pct = ((usdt_rate - prev_usdt) / prev_usdt) * 100

    # 5. Construir objeto de datos (Claves fijas y limpias)
    new_data = {
        "usd": usd_rate,
        "eur": eur_rate,
        "usdt": usdt_rate,
        "ut": FIXED_UT_RATE,
        "last_updated": datetime.now(VENEZUELA_TZ).strftime("%Y-%m-%d %H:%M:%S"),
        "usd_change_percent": round(usd_pct, 2),
        "eur_change_percent": round(eur_pct, 2),
        "usdt_change_percent": round(usdt_pct, 2)
    }
    current_rates_in_memory = new_data

    # 6. Guardar en Firestore
    if db:
        db.collection('rates').document('current').set(current_rates_in_memory)
        
        # Historial (Solo si es la actualización diaria completa)
        if not only_usdt:
            should_save_history = False
            if not historical_rates_in_memory: should_save_history = True
            elif historical_rates_in_memory[0]["date"] != today_str: should_save_history = True
            
            if should_save_history:
                historical_rates_in_memory.insert(0, {
                    "date": today_str,
                    "usd": usd_rate,
                    "eur": eur_rate,
                    "usdt": usdt_rate
                })
                historical_rates_in_memory = historical_rates_in_memory[:30] # Max 30 días
                db.collection('rates').document('history').set({'data': historical_rates_in_memory})

# Wrappers para el Scheduler
def job_daily_bcv():
    update_rates_logic(only_usdt=False)

def job_usdt_update():
    update_rates_logic(only_usdt=True)

# --- RUTAS API ---
@app.route('/', methods=['GET'])
def index():
    return "API Kmbio Vzla Activa", 200

@app.route('/api/bcv-rates', methods=['GET'])
def get_rates():
    # Si está vacío, intentar cargar o actualizar
    if not current_rates_in_memory:
        load_rates_from_firestore()
    if not current_rates_in_memory:
        job_daily_bcv() # Forzar actualización si sigue vacío
        
    return jsonify(current_rates_in_memory)

@app.route('/api/bcv-history', methods=['GET'])
def get_history():
    if not historical_rates_in_memory:
        load_rates_from_firestore()
    return jsonify(historical_rates_in_memory)

# --- ARRANQUE ---
# Carga inicial fuera del main para Gunicorn
try:
    load_rates_from_firestore()
    # Si después de cargar de DB sigue vacío, ejecutar lógica
    if not current_rates_in_memory:
        job_daily_bcv()
        
    scheduler = BackgroundScheduler(timezone="America/Caracas")
    if not scheduler.running:
        # BCV todos los días a las 00:01 AM
        scheduler.add_job(job_daily_bcv, 'cron', hour=0, minute=1)
        # USDT cada 30 minutos
        scheduler.add_job(job_usdt_update, 'interval', minutes=30)
        scheduler.start()
        logger.info("Scheduler iniciado.")
except Exception as e:
    logger.error(f"Error en arranque: {e}")

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)