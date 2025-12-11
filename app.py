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
import pytz
import warnings

# --- Configuración de Firebase ---
import firebase_admin
from firebase_admin import credentials
from firebase_admin import firestore

warnings.filterwarnings("ignore")

app = Flask(__name__)
CORS(app)

# --- Constantes y Variables Globales ---
DEFAULT_USD_RATE = 00.01
DEFAULT_EUR_RATE = 00.01
FIXED_UT_RATE = 43.00 # Tasa de respaldo para USDT si falla el scraping

BCV_URL = "https://www.bcv.org.ve/"
VENEZUELA_TZ = pytz.timezone("America/Caracas")

db = None
current_rates_in_memory = {}
historical_rates_in_memory = []
last_scrape_time = None
last_log_id = 0

# --- Inicialización de Firebase (NO TOCAR) ---
try:
    firebase_credentials_json = os.environ.get('FIREBASE_CREDENTIALS_JSON')
    if firebase_credentials_json:
        cred = credentials.Certificate(json.loads(firebase_credentials_json))
        firebase_admin.initialize_app(cred)
        db = firestore.client()
        print(f"[{datetime.now(VENEZUELA_TZ).strftime('%Y-%m-%d %H:%M:%S')}] Firebase inicializado exitosamente.", flush=True)
    else:
        print(f"[{datetime.now(VENEZUELA_TZ).strftime('%Y-%m-%d %H:%M:%S')}] ADVERTENCIA: Variable FIREBASE_CREDENTIALS_JSON no encontrada. Firestore no estará disponible.", flush=True)
except Exception as e:
    print(f"[{datetime.now(VENEZUELA_TZ).strftime('%Y-%m-%d %H:%M:%S')}] ERROR al inicializar Firebase: {e}", flush=True)

# --- Funciones de Utilidad y DB ---

def log_scrape_event(status, message):
    global last_log_id
    if db:
        log_data = {
            'timestamp': firestore.SERVER_TIMESTAMP,
            'status': status,
            'message': message,
            'id': last_log_id + 1
        }
        try:
            db.collection('scrape_logs').add(log_data)
            last_log_id += 1
        except Exception as e:
            print(f"[{datetime.now(VENEZUELA_TZ).strftime('%Y-%m-%d %H:%M:%S')}] Error al guardar log: {e}", flush=True)

def load_rates_from_firestore():
    global current_rates_in_memory, historical_rates_in_memory
    if db:
        try:
            # Cargar tasas actuales
            current_doc = db.collection('rates').document('current').get()
            if current_doc.exists:
                current_rates_in_memory = current_doc.to_dict().get('data', {})
            
            # Cargar historial
            history_docs = db.collection('history').order_by('timestamp', direction=firestore.Query.DESCENDING).limit(10).get()
            historical_rates_in_memory = [doc.to_dict() for doc in history_docs]
            
        except Exception as e:
            print(f"[{datetime.now(VENEZUELA_TZ).strftime('%Y-%m-%d %H:%M:%S')}] Error al cargar tasas de Firestore: {e}", flush=True)

def cleanup_old_historical_rates():
    if db:
        try:
            # Define la fecha límite (ej. 7 días)
            seven_days_ago = datetime.now(VENEZUELA_TZ) - timedelta(days=7)
            
            # Consulta para obtener documentos más antiguos que la fecha límite
            old_docs = db.collection('history').where('timestamp', '<', seven_days_ago).stream()
            
            count = 0
            for doc in old_docs:
                doc.reference.delete()
                count += 1
            
            print(f"[{datetime.now(VENEZUELA_TZ).strftime('%Y-%m-%d %H:%M:%S')}] Limpieza completada. {count} registros históricos eliminados.", flush=True)
            log_scrape_event('info', f'{count} registros históricos eliminados.')
        except Exception as e:
            print(f"[{datetime.now(VENEZUELA_TZ).strftime('%Y-%m-%d %H:%M:%S')}] Error durante la limpieza del historial: {e}", flush=True)


# --- Funciones de Scraping (BCV y USDT) ---

def fetch_bcv_rates():
    """Scrape USD y EUR del BCV."""
    rates = {}
    try:
        response = requests.get(BCV_URL, timeout=10)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')
        
        # USD
        usd_element = soup.find('div', class_='col-sm-6 col-xs-6 col-yadio')
        if usd_element:
            usd_rate_str = usd_element.find('strong').text.strip().replace(',', '.')
            rates['usd'] = float(usd_rate_str) if re.match(r"^\d+\.\d+$", usd_rate_str) else DEFAULT_USD_RATE
        else:
            rates['usd'] = DEFAULT_USD_RATE
            log_scrape_event('warning', 'No se encontró el elemento USD del BCV.')
        
        # EUR
        eur_element = usd_element.find_next_sibling('div') if usd_element else None
        if eur_element:
            eur_rate_str = eur_element.find('strong').text.strip().replace(',', '.')
            rates['eur'] = float(eur_rate_str) if re.match(r"^\d+\.\d+$", eur_rate_str) else DEFAULT_EUR_RATE
        else:
            rates['eur'] = DEFAULT_EUR_RATE
            log_scrape_event('warning', 'No se encontró el elemento EUR del BCV.')
            
        log_scrape_event('success', f"Scraping BCV exitoso. USD: {rates.get('usd')}, EUR: {rates.get('eur')}")
        return rates
        
    except Exception as e:
        error_msg = f"Error en scraping BCV: {e}"
        print(f"[{datetime.now(VENEZUELA_TZ).strftime('%Y-%m-%d %H:%M:%S')}] {error_msg}", flush=True)
        log_scrape_event('error', error_msg)
        return {'usd': DEFAULT_USD_RATE, 'eur': DEFAULT_EUR_RATE}


def fetch_usdt_rate():
    """Scrape la tasa de USDT/VES de Binance P2P (tasa de Compra más baja)."""
    try:
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
            # Tomar la tasa del primer anuncio (la más baja de compra)
            rate = float(data['data'][0]['adv']['price'])
            log_scrape_event('success', f"Scraping USDT exitoso. Tasa: {rate}")
            return rate
        else:
            log_scrape_event('warning', "No se encontraron anuncios USDT en Binance P2P. Usando tasa de respaldo.")
            return FIXED_UT_RATE
            
    except Exception as e:
        error_msg = f"Error en scraping Binance P2P: {e}"
        print(f"[{datetime.now(VENEZUELA_TZ).strftime('%Y-%m-%d %H:%M:%S')}] {error_msg}", flush=True)
        log_scrape_event('error', error_msg)
        return FIXED_UT_RATE


def fetch_and_update_bcv_rates_firestore():
    """Ejecuta todos los scrapers, actualiza la memoria y guarda en Firestore."""
    global current_rates_in_memory, historical_rates_in_memory, last_scrape_time
    
    if db:
        try:
            new_rates = {}
            
            # 1. Obtener USD y EUR del BCV
            bcv_rates = fetch_bcv_rates() 
            new_rates.update(bcv_rates)

            # 2. Obtener USDT de Binance P2P
            usdt_rate = fetch_usdt_rate()
            new_rates['usdt'] = usdt_rate
            
            # 3. Preparar datos para Firestore
            timestamp_utc = datetime.utcnow().replace(tzinfo=pytz.utc)
            timestamp_ve = datetime.now(VENEZUELA_TZ)
            
            # Datos de tasas actuales y para el historial
            rates_data = {
                'usd': new_rates.get('usd', DEFAULT_USD_RATE),
                'eur': new_rates.get('eur', DEFAULT_EUR_RATE),
                'usdt': new_rates.get('usdt', FIXED_UT_RATE),
                'timestamp': timestamp_utc,
                'fecha_ve': timestamp_ve.strftime('%Y-%m-%d %H:%M:%S')
            }
            
            # 4. Guardar en Firestore
            db.collection('rates').document('current').set({'data': rates_data})
            
            # Solo guardar en historial si es un nuevo día (o forzar el guardado cada cierto tiempo, elegimos guardar solo una vez al día para no saturar)
            last_history_doc = db.collection('history').order_by('timestamp', direction=firestore.Query.DESCENDING).limit(1).get()
            
            should_save_history = True
            if last_history_doc:
                last_doc = last_history_doc[0].to_dict()
                last_timestamp = last_doc.get('timestamp').astimezone(VENEZUELA_TZ)
                
                # Si el último registro es del mismo día, no guardar a menos que la diferencia sea grande
                if last_timestamp.date() == timestamp_ve.date():
                    should_save_history = False # Evitar duplicados del mismo día

            if should_save_history:
                db.collection('history').add(rates_data)

            # 5. Actualizar la memoria
            current_rates_in_memory = rates_data
            last_scrape_time = timestamp_ve
            
            print(f"[{last_scrape_time.strftime('%Y-%m-%d %H:%M:%S')}] Tasas actualizadas y guardadas: {current_rates_in_memory}", flush=True)
            
        except Exception as e:
            error_msg = f"Error general al actualizar y guardar tasas: {e}"
            print(f"[{datetime.now(VENEZUELA_TZ).strftime('%Y-%m-%d %H:%M:%S')}] {error_msg}", flush=True)
            log_scrape_event('critical', error_msg)

# --- Rutas de la API (Las que faltaban y causaban el 404) ---

@app.route('/', methods=['GET', 'HEAD'])
def index():
    """Ruta de bienvenida o health check para evitar 404 en la raíz."""
    return jsonify({"status": "ok", "message": "API Kmbio Vzla activa. Use /rates para obtener las tasas actuales."})

@app.route('/rates', methods=['GET', 'HEAD'])
def get_current_rates():
    """Ruta principal para obtener las tasas actuales (USD, EUR, USDT)."""
    # Ejecutar la función de actualización. Si ya se ejecutó recientemente por el scheduler, será rápido.
    # Nota: Si el scheduler está corriendo, esta línea garantiza que si el usuario pide las tasas, 
    # se obtienen las más frescas.
    if not current_rates_in_memory or (datetime.now(VENEZUELA_TZ) - last_scrape_time > timedelta(minutes=10) if last_scrape_time else True):
        fetch_and_update_bcv_rates_firestore()
    else:
        # Si las tasas en memoria son recientes, solo cárgalas (por si acaso)
        load_rates_from_firestore()

    if current_rates_in_memory:
        return jsonify(current_rates_in_memory)
    else:
        return jsonify({"error": "Tasas no disponibles"}), 503

@app.route('/historical-rates', methods=['GET'])
def get_historical_rates():
    """Ruta para obtener el historial de tasas."""
    load_rates_from_firestore()
    return jsonify(historical_rates_in_memory)

@app.route('/api/scrape-logs', methods=['GET'])
def get_scrape_logs():
    """Ruta para obtener el historial de logs de scraping."""
    if not db:
        return jsonify({"error": "Firestore no disponible"}), 500
        
    try:
        # Obtener los últimos 50 logs ordenados por tiempo
        logs_ref = db.collection('scrape_logs').order_by('timestamp', direction=firestore.Query.DESCENDING).limit(50)
        logs_docs = logs_ref.get()
        
        scrape_logs = []
        for doc in logs_docs:
            log_data = doc.to_dict()
            # Convertir timestamp a string legible
            if log_data.get('timestamp'):
                log_data['timestamp'] = log_data['timestamp'].astimezone(VENEZUELA_TZ).strftime('%Y-%m-%d %H:%M:%S')
            scrape_logs.append(log_data)
        
        return jsonify(scrape_logs)
    except Exception as e:
        print(f"[{datetime.now(VENEZUELA_TZ).strftime('%Y-%m-%d %H:%M:%S')}] Error al obtener logs de scraping: {e}", flush=True)
        return jsonify({"error": f"Error al obtener logs de scraping: {str(e)}"}), 500


# --- Inicialización del Scheduler y Flask ---

scheduler = BackgroundScheduler(timezone="America/Caracas")

if __name__ == '__main__':
    print(f"[{datetime.now(VENEZUELA_TZ).strftime('%Y-%m-%d %H:%M:%S')}] --- INICIO DE LA APLICACIÓN FLASK ---", flush=True)
    
    # 1. Cargar las últimas tasas disponibles al arranque
    load_rates_from_firestore()
    print(f"[{datetime.now(VENEZUELA_TZ).strftime('%Y-%m-%d %H:%M:%S')}] Tasas iniciales cargadas de Firestore.", flush=True)

    # 2. Programar el Scraping de BCV y USDT cada 15 minutos
    # Nota: Usamos una diferencia de 15 minutos para evitar saturar el BCV y Binance.
    scheduler.add_job(fetch_and_update_bcv_rates_firestore, 'interval', minutes=15)
    print(f"[{datetime.now(VENEZUELA_TZ).strftime('%Y-%m-%d %H:%M:%S')}] Scraping de tasas (BCV + USDT) programado cada 15 minutos.", flush=True)

    # 3. Programar la limpieza de datos históricos (una vez al día)
    scheduler.add_job(cleanup_old_historical_rates, 'cron', hour=1, minute=0, day_of_week='mon-sun')
    print(f"[{datetime.now(VENEZUELA_TZ).strftime('%Y-%m-%d %H:%M:%S')}] Limpieza de historial programada diariamente a la 01:00 AM.", flush=True)

    scheduler.start()
    
    # Render usa Gunicorn, por lo que esta línea probablemente no se usa en producción, 
    # pero es necesaria para desarrollo local.
    # app.run(host='0.0.0.0', port=os.environ.get('PORT', 5000), debug=False)