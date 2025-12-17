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
            else:
                current_rates_in_memory = DEFAULT_RATES.copy()

            # Cargar Historial
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
    global current_rates_in_memory, historical_rates_in_memory
    
    # 1. Sincronizar primero
    load_rates_from_firestore()
    
    usd_rate = current_rates_in_memory.get('usd', 0.01)
    eur_rate = current_rates_in_memory.get('eur', 0.01)
    usdt_rate = current_rates_in_memory.get('usdt', 0.01)

    # Obtener valores "anteriores" para calcular el porcentaje. 
    # Usamos el historial porque representa el "cierre anterior".
    prev_usd = usd_rate
    prev_eur = eur_rate
    
    if historical_rates_in_memory and len(historical_rates_in_memory) > 0:
        last_hist = historical_rates_in_memory[0]
        prev_usd = last_hist.get('usd', usd_rate)
        prev_eur = last_hist.get('eur', eur_rate)
    
    bcv_official_date_str = None
    bcv_date_object = None # Variable para almacenar el objeto fecha real para comparación

    # 2. Actualizar USDT
    new_usdt = fetch_binance_usdt()
    if new_usdt and new_usdt > 1.0:
        usdt_rate = new_usdt

    # 3. Actualizar BCV (Scraping + Fecha Oficial)
    if not only_usdt:
        try:
            resp = requests.get(BCV_URL, timeout=30, verify=False) 
            if resp.status_code == 200:
                soup = BeautifulSoup(resp.text, 'lxml')
                
                # --- EXTRAER FECHA OFICIAL DEL HTML ---
                date_span = soup.find('span', class_='date-display-single')
                if date_span and date_span.has_attr('content'):
                    try:
                        # ISO format: "2025-12-16T00:00:00-04:00"
                        raw_date = date_span['content'].split('T')[0] 
                        dt_obj = datetime.strptime(raw_date, "%Y-%m-%d")
                        
                        # Guardamos el objeto fecha para comparar matemáticamente
                        bcv_date_object = dt_obj.date()
                        
                        meses_es = ["Enero", "Febrero", "Marzo", "Abril", "Mayo", "Junio", "Julio", "Agosto", "Septiembre", "Octubre", "Noviembre", "Diciembre"]
                        bcv_official_date_str = f"{dt_obj.day} de {meses_es[dt_obj.month - 1]} de {dt_obj.year}"
                        
                    except Exception as e:
                        logger.error(f"Error parseando fecha BCV: {e}")

                # Extraer tasas (Solo tomamos los valores, no guardamos todavía)
                usd_div = soup.find('div', id='dolar')
                if usd_div: usd_rate = float(usd_div.find('strong').text.strip().replace(',', '.'))
                eur_div = soup.find('div', id='euro')
                if eur_div: eur_rate = float(eur_div.find('strong').text.strip().replace(',', '.'))
        except Exception as e:
            logger.error(f"Error BCV: {e}")

    now_vzla = datetime.now(VENEZUELA_TZ)
    
    # --- LOGICA CRITICA: BLOQUEO DE FECHA FUTURA (LUNES BANCARIO) ---
    # Si tenemos una fecha oficial del BCV y esa fecha es MAYOR a hoy (Futuro), NO actualizamos.
    # Ejemplo: Hoy es Lunes 15. BCV dice Martes 16. (16 > 15) -> STOP.
    if bcv_date_object:
        today_date = now_vzla.date()
        if bcv_date_object > today_date:
            logger.info(f"DETENIDO: La tasa del BCV es para el futuro ({bcv_official_date_str}). Se mantiene la tasa actual en Frontend.")
            
            # Sin embargo, SI podemos actualizar el USDT que es tiempo real.
            # Actualizamos solo USDT en memoria y base de datos, pero NO tocamos USD/EUR ni fecha BCV
            current_rates_in_memory['usdt'] = usdt_rate
            # Mantenemos last_updated para que se sepa que el sistema corre, pero la data BCV no cambia
            current_rates_in_memory['last_updated'] = now_vzla.strftime("%Y-%m-%d %H:%M:%S")
            
            if db:
                db.collection('rates').document('current').set(current_rates_in_memory)
            return # Salimos de la función aquí para no guardar historial futuro ni cambiar tasas oficiales
            
    # Si la fecha es Hoy o Pasada, o no se pudo leer (fallback), continuamos normal:
    final_date_str = bcv_official_date_str if bcv_official_date_str else now_vzla.strftime("%d de %B de %Y")

    # --- CÁLCULO MATEMÁTICO DE PORCENTAJES ---
    # Fórmula: ((Nuevo - Viejo) / Viejo) * 100
    try:
        usd_change = ((usd_rate - prev_usd) / prev_usd) * 100 if prev_usd > 0 else 0.0
        eur_change = ((eur_rate - prev_eur) / prev_eur) * 100 if prev_eur > 0 else 0.0
    except:
        usd_change = 0.0
        eur_change = 0.0
    
    # Guardar cambios
    new_data = {
        "usd": usd_rate,
        "eur": eur_rate,
        "usdt": usdt_rate,
        "ut": 43.00,
        "last_updated": now_vzla.strftime("%Y-%m-%d %H:%M:%S"),
        "usd_change_percent": round(usd_change, 2), # Aquí guardamos el cálculo nuevo
        "eur_change_percent": round(eur_change, 2), # Aquí guardamos el cálculo nuevo
        "usdt_change_percent": 0.0
    }
    
    current_rates_in_memory = new_data

    # 5. Escribir en Firebase
    if db:
        try:
            db.collection('rates').document('current').set(current_rates_in_memory)
            logger.info(f"Guardado en Firebase: {final_date_str} - USD:{usd_rate}")
            
            if not only_usdt:
                load_rates_from_firestore()
                
                should_save_history = False
                if not historical_rates_in_memory:
                    should_save_history = True
                else:
                    last_entry = historical_rates_in_memory[0]
                    # Solo guardar en historial si la fecha oficial cambió respecto al último registro
                    if last_entry.get("date") != final_date_str:
                        should_save_history = True

                if should_save_history:
                    new_hist = {"date": final_date_str, "usd": usd_rate, "eur": eur_rate, "usdt": usdt_rate}
                    historical_rates_in_memory.insert(0, new_hist)
                    db.collection('rates').document('history').set({'data': historical_rates_in_memory[:30]})
        except Exception as e:
            logger.error(f"Error escribiendo Firestore: {e}")

# Jobs
def job_daily_bcv():
    # Lunes a Viernes
    update_rates_logic(only_usdt=False)

def job_usdt_update():
    update_rates_logic(only_usdt=True)

# Rutas
@app.route('/', methods=['GET'])
def index():
    return "API Kmbio Vzla v2.5 (DateGuard Active)", 200

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
            scheduler.add_job(job_daily_bcv, 'cron', day_of_week='mon-fri', hour=0, minute=1)
            scheduler.add_job(job_usdt_update, 'interval', minutes=15)
            scheduler.start()
    except Exception as e:
        logger.error(f"Error scheduler: {e}")

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))