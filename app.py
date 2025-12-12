from flask import Flask, jsonify
from flask_cors import CORS
import requests
from bs4 import BeautifulSoup
from datetime import datetime, timedelta
import os
import re
import json
from apscheduler.schedulers.background import BackgroundScheduler
import firebase_admin
from firebase_admin import credentials, firestore
import logging
import pytz

# Configuración básica de logs
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)
CORS(app)

VENEZUELA_TZ = pytz.timezone("America/Caracas")

# Variables globales
current_rates_in_memory = {}
historical_rates_in_memory = []
db = None

# --- Inicialización de Firebase ---
try:
    firebase_credentials_json = os.environ.get('FIREBASE_CREDENTIALS_JSON')
    if firebase_credentials_json and not firebase_admin._apps:
        cred = credentials.Certificate(json.loads(firebase_credentials_json)) 
        firebase_admin.initialize_app(cred)
        db = firestore.client()
        logger.info("Firebase inicializado correctamente.")
    elif not firebase_credentials_json:
         logger.warning("ADVERTENCIA: Variable 'FIREBASE_CREDENTIALS_JSON' no encontrada.")
except Exception as e:
    logger.error(f"ERROR: Fallo al inicializar Firebase: {e}")

DEFAULT_USD_RATE = 00.01
DEFAULT_EUR_RATE = 00.01
DEFAULT_USDT_RATE = 00.01
FIXED_UT_RATE = 43.00 # Valor fijo de la Unidad Tributaria

BCV_URL = "https://www.bcv.org.ve/"

# --- Función de Carga Inicial ---
def load_rates_from_firestore():
    global current_rates_in_memory, historical_rates_in_memory
    if db:
        try:
            current_doc = db.collection('rates').document('current').get()
            if current_doc.exists:
                current_rates_in_memory = current_doc.to_dict()
                logger.info("Tasas actuales cargadas de Firestore.")

            history_doc = db.collection('rates').document('history').get()
            if history_doc.exists and 'data' in history_doc.to_dict():
                historical_rates_in_memory = history_doc.to_dict()['data']
                logger.info(f"Historial cargado: {len(historical_rates_in_memory)} registros.")
        except Exception as e:
            logger.error(f"Error al cargar datos de Firestore: {e}")

# --- Función Scraping Binance P2P (USDT) ---
def fetch_binance_p2p_average():
    """Obtiene el promedio de las primeras 15 ofertas de Compra y Venta de USDT/VES"""
    url = "https://p2p.binance.com/bapi/c2c/v2/public/c2c/adv/search"
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0"
    }
    
    prices = []
    
    try:
        # 1. Obtener ofertas de VENTA (Lo que paga el usuario para comprar USDT) - TradeType BUY
        # 2. Obtener ofertas de COMPRA (Lo que recibe el usuario al vender USDT) - TradeType SELL
        for trade_type in ["BUY", "SELL"]:
            payload = {
                "asset": "USDT",
                "fiat": "VES",
                "merchantCheck": False,
                "page": 1,
                "rows": 15, # Solicitud: 15 ofertas
                "tradeType": trade_type,
                "publisherType": None
            }
            response = requests.post(url, headers=headers, json=payload, timeout=10)
            data = response.json()
            
            if data and "data" in data:
                for ad in data["data"]:
                    price = float(ad["adv"]["price"])
                    prices.append(price)
        
        if prices:
            average_price = sum(prices) / len(prices)
            logger.info(f"Promedio Binance P2P calculado ({len(prices)} ofertas): {average_price}")
            return average_price
        else:
            logger.warning("No se encontraron precios en Binance P2P.")
            return None

    except Exception as e:
        logger.error(f"Error consultando Binance P2P: {e}")
        return None

# --- Función Principal de Actualización (BCV y Lógica General) ---
def update_rates_logic(only_usdt=False):
    global current_rates_in_memory, historical_rates_in_memory
    
    logger.info(f"Iniciando actualización de tasas (Solo USDT: {only_usdt})...")
    
    # 1. Obtener valores actuales en memoria para no perder lo que no se actualice
    usd_rate = current_rates_in_memory.get('usd', DEFAULT_USD_RATE)
    eur_rate = current_rates_in_memory.get('eur', DEFAULT_EUR_RATE)
    usdt_rate = current_rates_in_memory.get('usdt', DEFAULT_USDT_RATE)

    # 2. Actualizar USDT (Cada 30 min o diario)
    new_usdt = fetch_binance_p2p_average()
    if new_usdt:
        usdt_rate = new_usdt

    # 3. Actualizar BCV (Solo si NO es una actualización exclusiva de USDT, es decir, la diaria)
    if not only_usdt:
        try:
            response = requests.get(BCV_URL, timeout=30, verify=False)
            response.raise_for_status()
            soup = BeautifulSoup(response.text, 'lxml')

            # USD BCV
            usd_container = soup.find('div', id='dolar')
            if usd_container:
                centrado = usd_container.find('div', class_='centrado')
                if centrado:
                    tag = centrado.find('strong')
                    if tag:
                        match = re.search(r'[\d,\.]+', tag.text)
                        if match:
                            usd_rate = float(match.group(0).replace(',', '.').strip())

            # EUR BCV
            eur_container = soup.find('div', id='euro')
            if eur_container:
                centrado = eur_container.find('div', class_='centrado')
                if centrado:
                    tag = centrado.find('strong')
                    if tag:
                        match = re.search(r'[\d,\.]+', tag.text)
                        if match:
                            eur_rate = float(match.group(0).replace(',', '.').strip())
            
            logger.info(f"BCV Scrapeado: USD={usd_rate}, EUR={eur_rate}")
        except Exception as e:
            logger.error(f"Error scrapeando BCV: {e}")

    # 4. Cálculos de Variación (Comparando con el historial de ayer)
    usd_change = 0.0
    eur_change = 0.0
    usdt_change = 0.0
    
    today_str = datetime.now(VENEZUELA_TZ).strftime("%d de %B de %Y")
    prev_usd, prev_eur, prev_usdt = None, None, None

    if historical_rates_in_memory:
        for entry in historical_rates_in_memory:
            if entry.get("date") != today_str:
                prev_usd = entry.get("usd")
                prev_eur = entry.get("eur")
                prev_usdt = entry.get("usdt") # Buscar USDT histórico
                break
    
    if prev_usd: usd_change = ((usd_rate - prev_usd) / prev_usd) * 100
    if prev_eur: eur_change = ((eur_rate - prev_eur) / prev_eur) * 100
    if prev_usdt: usdt_change = ((usdt_rate - prev_usdt) / prev_usdt) * 100

    # 5. Guardar en Memoria
    new_data = {
        "usd": usd_rate,
        "eur": eur_rate,
        "usdt": usdt_rate, # Incluir USDT
        "ut": FIXED_UT_RATE,
        "last_updated": datetime.now(VENEZUELA_TZ).strftime("%Y-%m-%d %H:%M:%S"),
        "usd_change_percent": round(usd_change, 2),
        "eur_change_percent": round(eur_change, 2),
        "usdt_change_percent": round(usdt_change, 2) # Incluir % USDT
    }
    current_rates_in_memory = new_data

    # 6. Guardar en Firestore (Current)
    if db:
        db.collection('rates').document('current').set(current_rates_in_memory)

    # 7. Guardar en Historial (Solo si es la actualización DIARIA completa del BCV)
    if not only_usdt:
        should_update_history = False
        if not historical_rates_in_memory:
            should_update_history = True
        elif historical_rates_in_memory[0]["date"] != today_str:
            should_update_history = True
        
        if should_update_history:
            historical_rates_in_memory.insert(0, {
                "date": today_str,
                "usd": usd_rate,
                "eur": eur_rate,
                "usdt": usdt_rate # Guardar USDT en el historial también
            })
            historical_rates_in_memory = historical_rates_in_memory[:30]
            
            if db:
                db.collection('rates').document('history').set({'data': historical_rates_in_memory})

# Wrappers para el Scheduler
def job_daily_bcv():
    update_rates_logic(only_usdt=False)

def job_interval_usdt():
    update_rates_logic(only_usdt=True)

# --- RUTAS API ---
@app.route('/', methods=['GET'])
def home():
    return "Backend Kmbio Vzla Activo v2.1", 200

@app.route('/api/bcv-rates', methods=['GET'])
def get_current_bcv_rates():
    if not current_rates_in_memory:
        load_rates_from_firestore()
    if not current_rates_in_memory: # Si sigue vacío, intento forzado
        job_daily_bcv()
    return jsonify(current_rates_in_memory)

@app.route('/api/bcv-history', methods=['GET'])
def get_bcv_history():
    if not historical_rates_in_memory:
        load_rates_from_firestore()
    return jsonify(historical_rates_in_memory)

# --- INICIALIZACIÓN ---
try:
    load_rates_from_firestore()
    
    # Si la memoria está vacía al arrancar, forzamos una carga inicial completa
    if not current_rates_in_memory:
        job_daily_bcv()

    scheduler = BackgroundScheduler(timezone="America/Caracas")
    
    # 1. Tarea Diaria (BCV + USDT + Historial) a las 00:01 AM
    scheduler.add_job(job_daily_bcv, 'cron', hour=0, minute=1, day_of_week='mon-sun')
    
    # 2. Tarea Intervalo (Solo USDT) cada 30 minutos
    scheduler.add_job(job_interval_usdt, 'interval', minutes=30)
    
    scheduler.start()
    logger.info("Planificador iniciado: BCV diario y USDT cada 30 min.")

except Exception as e:
    logger.error(f"Error en inicialización: {e}")

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)