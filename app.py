from flask import Flask, jsonify
from flask_cors import CORS
import requests
from bs4 import BeautifulSoup
from datetime import datetime
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

# Variables globales en memoria (Cache temporal)
current_rates_in_memory = {}
historical_rates_in_memory = []
db = None

# --- CONSTANTES ---
BCV_URL = "https://www.bcv.org.ve/"
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

# --- FUNCIÓN: Cargar datos desde Firestore (Sincronización) ---
def load_rates_from_firestore():
    global current_rates_in_memory, historical_rates_in_memory
    if db:
        try:
            # Cargar Tasas Actuales
            doc = db.collection('rates').document('current').get()
            if doc.exists:
                current_rates_in_memory = doc.to_dict()
                # logger.info("Datos sincronizados desde Firestore.")
            else:
                current_rates_in_memory = DEFAULT_RATES.copy()

            # Cargar Historial (Solo si la lista en memoria está vacía para no saturar lecturas)
            if not historical_rates_in_memory:
                hist_doc = db.collection('rates').document('history').get()
                if hist_doc.exists and 'data' in hist_doc.to_dict():
                    historical_rates_in_memory = hist_doc.to_dict()['data']
        except Exception as e:
            logger.error(f"Error cargando Firestore: {e}")

# --- FUNCIÓN: Binance P2P ---
def fetch_binance_usdt():
    url = "https://p2p.binance.com/bapi/c2c/v2/friendly/c2c/adv/search"
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Content-Type": "application/json",
        "Clienttype": "web"
    }
    all_prices = []
    try:
        # Consultar COMPRA y VENTA (15 ofertas cada uno)
        for trade_type in ["BUY", "SELL"]:
            payload = {
                "asset": "USDT", "fiat": "VES", "merchantCheck": False, "page": 1, "rows": 15, 
                "tradeType": trade_type, "transAmount": 0, "payTypes": []
            }
            time.sleep(random.uniform(0.1, 0.5))
            response = requests.post(url, json=payload, headers=headers, timeout=10)
            if response.status_code == 200:
                data = response.json()
                if data.get("code") == "000000" and "data" in data:
                    for ad in data["data"]:
                        try:
                            price = float(ad["adv"]["price"])
                            if price > 0: all_prices.append(price)
                        except: continue
        
        if all_prices:
            avg_price = sum(all_prices) / len(all_prices)
            logger.info(f"Binance Promedio calculado: {avg_price}")
            return avg_price
        return None
    except Exception as e:
        logger.error(f"Error Binance: {e}")
        return None

# --- LÓGICA DE ACTUALIZACIÓN ---
def update_rates_logic(only_usdt=False):
    global current_rates_in_memory
    
    # 1. Sincronizar primero para tener la base más reciente
    load_rates_from_firestore()
    
    usd_rate = current_rates_in_memory.get('usd', 0.01)
    eur_rate = current_rates_in_memory.get('eur', 0.01)
    usdt_rate = current_rates_in_memory.get('usdt', 0.01)

    # 2. Actualizar USDT
    new_usdt = fetch_binance_usdt()
    if new_usdt and new_usdt > 1.0:
        usdt_rate = new_usdt

    # 3. Actualizar BCV
    if not only_usdt:
        try:
            resp = requests.get(BCV_URL, timeout=30, verify=False) 
            if resp.status_code == 200:
                soup = BeautifulSoup(resp.text, 'lxml')
                usd_div = soup.find('div', id='dolar')
                if usd_div: usd_rate = float(usd_div.find('strong').text.strip().replace(',', '.'))
                eur_div = soup.find('div', id='euro')
                if eur_div: eur_rate = float(eur_div.find('strong').text.strip().replace(',', '.'))
        except Exception as e:
            logger.error(f"Error BCV: {e}")

    # 4. Calcular Porcentajes (Simplificado)
    now_vzla = datetime.now(VENEZUELA_TZ)
    today_str = now_vzla.strftime("%d de %B de %Y")
    
    # Guardar cambios
    new_data = {
        "usd": usd_rate,
        "eur": eur_rate,
        "usdt": usdt_rate,
        "ut": 43.00,
        "last_updated": now_vzla.strftime("%Y-%m-%d %H:%M:%S"),
        # Mantenemos lógica de porcentajes básica o en 0 si falla historial
        "usd_change_percent": current_rates_in_memory.get('usd_change_percent', 0),
        "eur_change_percent": current_rates_in_memory.get('eur_change_percent', 0),
        "usdt_change_percent": 0.0 # Eliminado porcentaje USDT según solicitud anterior
    }
    
    current_rates_in_memory = new_data

    # 5. Escribir en Firebase
    if db:
        try:
            db.collection('rates').document('current').set(current_rates_in_memory)
            logger.info(f"Guardado en Firebase: USDT={usdt_rate}")
            
            # Guardar historial si es actualización completa y cambio de día
            if not only_usdt:
                load_rates_from_firestore() # Refrescar historial
                if not historical_rates_in_memory or historical_rates_in_memory[0].get("date") != today_str:
                    new_hist = {"date": today_str, "usd": usd_rate, "eur": eur_rate, "usdt": usdt_rate}
                    historical_rates_in_memory.insert(0, new_hist)
                    db.collection('rates').document('history').set({'data': historical_rates_in_memory[:30]})
        except Exception as e:
            logger.error(f"Error escribiendo Firestore: {e}")

# Jobs
def job_daily_bcv():
    update_rates_logic(only_usdt=False)

def job_usdt_update():
    update_rates_logic(only_usdt=True)

# Rutas
@app.route('/', methods=['GET'])
def index():
    return "API Kmbio Vzla v2.2 OK", 200

@app.route('/api/bcv-rates', methods=['GET'])
def get_rates():
    # CORRECCIÓN CRÍTICA: SIEMPRE leer de Firebase al recibir petición
    # Esto asegura que si el Job corrió en otro worker, este worker tenga el dato nuevo.
    load_rates_from_firestore() 
    return jsonify(current_rates_in_memory)

@app.route('/api/bcv-history', methods=['GET'])
def get_history():
    load_rates_from_firestore()
    return jsonify(historical_rates_in_memory)

if __name__ != '__main__':
    try:
        load_rates_from_firestore()
        scheduler = BackgroundScheduler(timezone="America/Caracas")
        if not scheduler.running:
            scheduler.add_job(job_daily_bcv, 'cron', hour=0, minute=1)
            scheduler.add_job(job_usdt_update, 'interval', minutes=15)
            scheduler.start()
    except Exception as e:
        logger.error(f"Error scheduler: {e}")

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))