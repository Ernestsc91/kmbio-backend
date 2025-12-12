from flask import Flask, jsonify
from flask_cors import CORS
import requests
from bs4 import BeautifulSoup
from datetime import datetime, timedelta
import random
import os
import re
import json
from apscheduler.schedulers.background import BackgroundScheduler
import firebase_admin
from firebase_admin import credentials, firestore
import logging

# Configuración básica de logs
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)
CORS(app)

# Variables globales para almacenamiento en memoria
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
         logger.warning("ADVERTENCIA: No se encontró la variable de entorno 'FIREBASE_CREDENTIALS_JSON'. Firebase no inicializado.")
except Exception as e:
    logger.error(f"ERROR: Fallo al inicializar Firebase: {e}")

DEFAULT_USD_RATE = 00.01
DEFAULT_EUR_RATE = 00.01
FIXED_UT_RATE = 43.00

BCV_URL = "https://www.bcv.org.ve/"

# --- Función de Carga (Se debe ejecutar al inicio) ---
def load_rates_from_firestore():
    global current_rates_in_memory, historical_rates_in_memory
    if db:
        try:
            # Cargar Tasas Actuales
            current_doc = db.collection('rates').document('current').get()
            if current_doc.exists:
                current_rates_in_memory = current_doc.to_dict()
                logger.info("Tasas actuales cargadas de Firestore exitosamente.")
            else:
                logger.info("No se encontró documento 'current' en Firestore.")

            # Cargar Historial
            history_doc = db.collection('rates').document('history').get()
            if history_doc.exists and 'data' in history_doc.to_dict():
                historical_rates_in_memory = history_doc.to_dict()['data']
                logger.info(f"Historial cargado de Firestore: {len(historical_rates_in_memory)} registros.")
            
        except Exception as e:
            logger.error(f"Error al cargar datos de Firestore: {e}")
    else:
        logger.warning("ADVERTENCIA: No se puede cargar de Firestore, DB no inicializada.")

# --- Función de Scraping ---
def fetch_and_update_bcv_rates():
    global current_rates_in_memory, historical_rates_in_memory
    logger.info(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Intentando actualizar tasas del BCV...")
    
    try:
        # Se usa verify=False para evitar errores de SSL con la página del BCV en Render
        response = requests.get(BCV_URL, timeout=30, verify=False)
        response.raise_for_status()

        soup = BeautifulSoup(response.text, 'lxml')

        usd_rate = None
        eur_rate = None

        # Extracción USD
        usd_container = soup.find('div', id='dolar')
        if usd_container:
            centrado_div_usd = usd_container.find('div', class_='centrado')
            if centrado_div_usd:
                usd_strong_tag = centrado_div_usd.find('strong')
                if usd_strong_tag:
                    match = re.search(r'[\d,\.]+', usd_strong_tag.text)
                    if match:
                        usd_rate = float(match.group(0).replace(',', '.').strip())

        # Extracción EUR
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

        # Cálculo de variaciones
        usd_change_percent = 0.0
        eur_change_percent = 0.0
        previous_day_rate_usd = None
        previous_day_rate_eur = None
        
        today_date_str_for_history_check = datetime.now().strftime("%d de %B de %Y")
        
        if historical_rates_in_memory:
            for entry in historical_rates_in_memory:
                if entry.get("date") != today_date_str_for_history_check:
                    previous_day_rate_usd = entry.get("usd")
                    previous_day_rate_eur = entry.get("eur")
                    break

        if previous_day_rate_usd and previous_day_rate_usd != 0:
            usd_change_percent = ((usd_rate - previous_day_rate_usd) / previous_day_rate_usd) * 100
        if previous_day_rate_eur and previous_day_rate_eur != 0:
            eur_change_percent = ((eur_rate - previous_day_rate_eur) / previous_day_rate_eur) * 100

        # Actualización en memoria
        new_rates = {
            "usd": usd_rate,
            "eur": eur_rate,
            "ut": FIXED_UT_RATE,
            "last_updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "usd_change_percent": round(usd_change_percent, 2),
            "eur_change_percent": round(eur_change_percent, 2)
        }
        current_rates_in_memory = new_rates

        # Guardado en Firestore
        if db:
            db.collection('rates').document('current').set(current_rates_in_memory)

        # Lógica de Historial
        should_update_history = False
        if not historical_rates_in_memory:
            should_update_history = True
        elif historical_rates_in_memory[0]["date"] != today_date_str_for_history_check:
            should_update_history = True
            
        if should_update_history:
            historical_rates_in_memory.insert(0, {
                "date": today_date_str_for_history_check,
                "usd": usd_rate,
                "eur": eur_rate
            })
            historical_rates_in_memory = historical_rates_in_memory[:30] # Mantener solo 30 días
            
            if db:
                db.collection('rates').document('history').set({'data': historical_rates_in_memory}) 
        
        logger.info(f"Tasas actualizadas: USD={usd_rate}, EUR={eur_rate}")

    except Exception as e:
        logger.error(f"Error en scraping: {e}")

# --- RUTAS API ---

@app.route('/', methods=['GET'])
def home():
    return "Backend Kmbio Vzla Activo", 200

@app.route('/api/bcv-rates', methods=['GET'])
def get_current_bcv_rates():
    # Si la memoria está vacía, intentar cargar de Firestore de emergencia
    if not current_rates_in_memory:
        load_rates_from_firestore()
    
    # Si sigue vacía, intentar scraping de emergencia
    if not current_rates_in_memory:
        fetch_and_update_bcv_rates()
        
    return jsonify(current_rates_in_memory)

@app.route('/api/bcv-history', methods=['GET'])
def get_bcv_history():
    if not historical_rates_in_memory:
        load_rates_from_firestore()
    return jsonify(historical_rates_in_memory)

# --- EJECUCIÓN AL INICIO (CRÍTICO PARA RENDER) ---
# Esto se ejecuta cuando Gunicorn importa la app
try:
    logger.info("Iniciando carga de datos y planificador...")
    load_rates_from_firestore()
    
    # Iniciar planificador
    scheduler = BackgroundScheduler(timezone="America/Caracas")
    if not scheduler.running:
        # Ejecutar scraping inmediatamente si no hay datos
        if not current_rates_in_memory:
             fetch_and_update_bcv_rates()
             
        scheduler.add_job(fetch_and_update_bcv_rates, 'cron', hour=0, minute=1, day_of_week='mon-sun')
        scheduler.start()
        logger.info("Planificador iniciado.")
except Exception as e:
    logger.error(f"Error en inicialización: {e}")

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)