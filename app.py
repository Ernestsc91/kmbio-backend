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
            else:
                current_rates_in_memory = DEFAULT_RATES.copy()

            # Cargar Historial
            hist_doc = db.collection('rates').document('history').get()
            if hist_doc.exists and 'data' in hist_doc.to_dict():
                historical_rates_in_memory = hist_doc.to_dict()['data']
            else:
                historical_rates_in_memory = []
                
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
        
        # Si hay resultados (aunque sean menos de 15), calculamos promedio
        if all_prices:
            avg_price = sum(all_prices) / len(all_prices)
            logger.info(f"Binance Promedio calculado: {avg_price} ({len(all_prices)} ofertas)")
            return avg_price
        return None
    except Exception as e:
        logger.error(f"Error Binance: {e}")
        return None

# --- AUXILIAR: Formatear Fecha en Español ---
def get_current_date_string():
    now = datetime.now(VENEZUELA_TZ)
    meses_es = ["Enero", "Febrero", "Marzo", "Abril", "Mayo", "Junio", "Julio", "Agosto", "Septiembre", "Octubre", "Noviembre", "Diciembre"]
    return f"{now.day} de {meses_es[now.month - 1]} de {now.year}"

# --- LÓGICA DE ACTUALIZACIÓN ---
def update_rates_logic(only_usdt=False):
    global current_rates_in_memory, historical_rates_in_memory
    
    # 1. Sincronizar estado actual
    load_rates_from_firestore()
    
    # Valores actuales antes de actualizar
    usd_rate = current_rates_in_memory.get('usd', 0.01)
    eur_rate = current_rates_in_memory.get('eur', 0.01)
    usdt_rate = current_rates_in_memory.get('usdt', 0.01)

    # 2. Actualizar USDT (Siempre corre)
    new_usdt = fetch_binance_usdt()
    if new_usdt and new_usdt > 1.0:
        usdt_rate = new_usdt

    # 3. Actualizar BCV (Scraping)
    # Se ejecuta si no es "only_usdt"
    if not only_usdt:
        try:
            resp = requests.get(BCV_URL, timeout=30, verify=False) 
            if resp.status_code == 200:
                soup = BeautifulSoup(resp.text, 'lxml')
                
                # Extraer tasas numéricas
                usd_div = soup.find('div', id='dolar')
                if usd_div: 
                    raw_usd = float(usd_div.find('strong').text.strip().replace(',', '.'))
                    if raw_usd > 0: usd_rate = raw_usd

                eur_div = soup.find('div', id='euro')
                if eur_div: 
                    raw_eur = float(eur_div.find('strong').text.strip().replace(',', '.'))
                    if raw_eur > 0: eur_rate = raw_eur
                
                # NOTA: Ya no bloqueamos por fecha futura. Tomamos el valor que esté en la web.
        except Exception as e:
            logger.error(f"Error BCV: {e}")

    now_vzla = datetime.now(VENEZUELA_TZ)
    today_str = get_current_date_string()

    # --- CÁLCULO DE PORCENTAJES (CORREGIDO) ---
    # Para calcular el porcentaje real, necesitamos comparar la tasa NUEVA (usd_rate)
    # con la tasa del CIERRE ANTERIOR (Ayer).
    
    prev_usd = usd_rate # Fallback por defecto
    prev_eur = eur_rate
    
    # Buscamos en el historial una entrada que NO sea la de hoy
    if historical_rates_in_memory:
        # Recorremos el historial para encontrar el primer registro que tenga una fecha diferente a hoy
        for entry in historical_rates_in_memory:
            if entry.get('date') != today_str:
                prev_usd = entry.get('usd', usd_rate)
                prev_eur = entry.get('eur', eur_rate)
                break
    
    # Calcular Porcentajes
    try:
        usd_change = ((usd_rate - prev_usd) / prev_usd) * 100 if prev_usd > 0 else 0.0
        eur_change = ((eur_rate - prev_eur) / prev_eur) * 100 if prev_eur > 0 else 0.0
    except:
        usd_change = 0.0
        eur_change = 0.0

    # Guardar objeto Current
    new_data = {
        "usd": usd_rate,
        "eur": eur_rate,
        "usdt": usdt_rate,
        "ut": 43.00,
        "last_updated": now_vzla.strftime("%Y-%m-%d %H:%M:%S"),
        "usd_change_percent": round(usd_change, 2),
        "eur_change_percent": round(eur_change, 2),
        "usdt_change_percent": 0.0
    }
    
    current_rates_in_memory = new_data

    # --- ACTUALIZAR FIREBASE ---
    if db:
        try:
            # 1. Guardar Current
            db.collection('rates').document('current').set(current_rates_in_memory)
            
            # 2. Lógica de Historial (Solo si es la rutina diaria BCV, no la de USDT solo)
            if not only_usdt:
                # Verificar si ya existe una entrada para "Hoy" (basado en fecha calendario Vzla)
                entry_exists_for_today = False
                if historical_rates_in_memory and historical_rates_in_memory[0]['date'] == today_str:
                    entry_exists_for_today = True

                new_hist_entry = {
                    "date": today_str,
                    "usd": usd_rate,
                    "eur": eur_rate,
                    "usdt": usdt_rate
                }

                if entry_exists_for_today:
                    # Si ya corrió hoy, actualizamos el valor (por si cambió algo)
                    historical_rates_in_memory[0] = new_hist_entry
                else:
                    # Si es un nuevo día, insertamos al principio
                    historical_rates_in_memory.insert(0, new_hist_entry)

                # Mantener solo los últimos 30 días para no saturar
                historical_rates_in_memory = historical_rates_in_memory[:30]
                
                # Guardar Historial
                db.collection('rates').document('history').set({'data': historical_rates_in_memory})
                logger.info(f"Historial actualizado para fecha: {today_str}")

        except Exception as e:
            logger.error(f"Error escribiendo Firestore: {e}")

# Jobs del Scheduler
def job_daily_bcv():
    # Se ejecuta todos los días a las 12:01 AM Vzla
    logger.info("Iniciando Job Diario BCV (Lun-Dom)...")
    update_rates_logic(only_usdt=False)

def job_usdt_update():
    # Se ejecuta cada 15 min
    update_rates_logic(only_usdt=True)

# Rutas API
@app.route('/', methods=['GET'])
def index():
    return "API Kmbio Vzla v3.0 (Continuous Daily History)", 200

@app.route('/api/bcv-rates', methods=['GET'])
def get_rates():
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
            # CAMBIO: day_of_week='mon-sun' para que corra sábados y domingos
            scheduler.add_job(job_daily_bcv, 'cron', day_of_week='mon-sun', hour=0, minute=1)
            scheduler.add_job(job_usdt_update, 'interval', minutes=15)
            scheduler.start()
    except Exception as e:
        logger.error(f"Error scheduler: {e}")

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))