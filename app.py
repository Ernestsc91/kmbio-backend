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
import random
import time

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

# --- FUNCIÓN: Binance P2P (Scraping Avanzado) ---
def fetch_binance_usdt():
    """Calcula promedio de USDT/VES (15 Buy + 15 Sell) de Binance P2P imitando un navegador real"""
    url = "https://p2p.binance.com/bapi/c2c/v2/friendly/c2c/adv/search"
    
    # Headers extendidos para evitar bloqueo 403 o respuestas vacías
    headers = {
        "Accept": "*/*",
        "Accept-Encoding": "gzip, deflate, br",
        "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
        "Cache-Control": "no-cache",
        "Clienttype": "web",  # CRÍTICO para Binance
        "Content-Type": "application/json",
        "Origin": "https://p2p.binance.com",
        "Pragma": "no-cache",
        "Referer": "https://p2p.binance.com/es/trade/all/USDT?fiat=VES",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Lang": "es" 
    }
    
    prices = []
    
    try:
        for trade_type in ["BUY", "SELL"]:
            # Payload completo que espera la API "friendly"
            payload = {
                "asset": "USDT",
                "fiat": "VES",
                "merchantCheck": False,
                "page": 1,
                "rows": 15,
                "tradeType": trade_type,
                "transAmount": 0,
                "countries": [],
                "proMerchantAds": False,
                "shieldMerchantAds": False,
                "publisherType": None,
                "payTypes": [],
                "classifies": ["mass", "profession"]
            }
            
            # Pequeña pausa para no saturar y parecer humano
            time.sleep(random.uniform(0.5, 1.5))
            
            resp = requests.post(url, headers=headers, json=payload, timeout=15)
            
            if resp.status_code == 200:
                data = resp.json()
                # Verificar estructura exitosa de Binance
                if data.get("code") == "000000" and "data" in data:
                    ads_list = data["data"]
                    if isinstance(ads_list, list) and len(ads_list) > 0:
                        for ad in ads_list:
                            if "adv" in ad and "price" in ad["adv"]:
                                try:
                                    price = float(ad["adv"]["price"])
                                    if price > 0:
                                        prices.append(price)
                                except ValueError:
                                    continue
                    else:
                        logger.warning(f"Binance: Lista vacía para {trade_type}")
                else:
                    logger.warning(f"Binance error lógico: {data.get('message')}")
            else:
                logger.error(f"Error HTTP Binance {resp.status_code}: {resp.text[:100]}")
        
        if prices:
            avg_price = sum(prices) / len(prices)
            logger.info(f"Binance USDT Promedio calculado ({len(prices)} órdenes): {avg_price:.2f}")
            return avg_price
        
        logger.error("No se obtuvieron precios válidos de Binance.")
        return None

    except Exception as e:
        logger.error(f"Excepción crítica en Binance Scraper: {e}")
        return None

# --- LÓGICA DE ACTUALIZACIÓN ---
def update_rates_logic(only_usdt=False):
    global current_rates_in_memory, historical_rates_in_memory
    
    # 1. Recuperar memoria o DB
    if not current_rates_in_memory:
        load_rates_from_firestore()

    # Valores actuales (para no perderlos si falla el scraping)
    usd_rate = current_rates_in_memory.get('usd', DEFAULT_USD_RATE)
    eur_rate = current_rates_in_memory.get('eur', DEFAULT_EUR_RATE)
    usdt_rate = current_rates_in_memory.get('usdt', DEFAULT_USDT_RATE)

    # 2. Actualizar USDT (Siempre intentar)
    new_usdt = fetch_binance_usdt()
    if new_usdt and new_usdt > 1.0: # Validación básica anti-cero
        usdt_rate = new_usdt
    else:
        logger.warning("Manteniendo tasa USDT anterior por fallo en scraping.")

    # 3. Actualizar BCV (Solo si NO es solo USDT)
    if not only_usdt:
        try:
            # verify=False necesario a veces en entornos cloud para BCV
            resp = requests.get(BCV_URL, timeout=45, verify=False)
            if resp.status_code == 200:
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
                
                logger.info(f"BCV Scrapeado Exitoso: USD={usd_rate}, EUR={eur_rate}")
            else:
                logger.error(f"Error conexión BCV: {resp.status_code}")
        except Exception as e:
            logger.error(f"Error BCV scraping: {e}")

    # 4. Calcular Porcentajes
    usd_pct, eur_pct, usdt_pct = 0.0, 0.0, 0.0
    today_str = datetime.now(VENEZUELA_TZ).strftime("%d de %B de %Y")
    
    prev_usd, prev_eur, prev_usdt = None, None, None
    
    if historical_rates_in_memory:
        for entry in historical_rates_in_memory:
            # Buscar el primer día diferente a hoy
            if entry.get("date") != today_str:
                prev_usd = float(entry.get("usd", 0))
                prev_eur = float(entry.get("eur", 0))
                prev_usdt = float(entry.get("usdt", 0))
                break
    
    # Cálculos seguros
    if prev_usd and prev_usd > 0: usd_pct = ((usd_rate - prev_usd) / prev_usd) * 100
    if prev_eur and prev_eur > 0: eur_pct = ((eur_rate - prev_eur) / prev_eur) * 100
    if prev_usdt and prev_usdt > 0: usdt_pct = ((usdt_rate - prev_usdt) / prev_usdt) * 100

    # 5. Construir objeto
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
        try:
            db.collection('rates').document('current').set(current_rates_in_memory)
            
            # Historial (Solo en actualización completa diaria)
            if not only_usdt:
                should_save = False
                if not historical_rates_in_memory:
                    should_save = True
                elif historical_rates_in_memory[0].get("date") != today_str:
                    should_save = True
                
                if should_save:
                    historical_rates_in_memory.insert(0, {
                        "date": today_str,
                        "usd": usd_rate,
                        "eur": eur_rate,
                        "usdt": usdt_rate
                    })
                    # Limitar historial a 60 días
                    historical_rates_in_memory = historical_rates_in_memory[:60]
                    db.collection('rates').document('history').set({'data': historical_rates_in_memory})
        except Exception as e:
            logger.error(f"Error guardando en Firestore: {e}")

# Jobs Wrappers
def job_daily_bcv():
    logger.info("Scheduler: Ejecutando Update BCV Completo")
    update_rates_logic(only_usdt=False)

def job_usdt_update():
    logger.info("Scheduler: Ejecutando Update USDT")
    update_rates_logic(only_usdt=True)

# Rutas
@app.route('/', methods=['GET'])
def index():
    return "Kmbio Vzla API Running", 200

@app.route('/api/bcv-rates', methods=['GET'])
def get_rates():
    if not current_rates_in_memory:
        load_rates_from_firestore()
    # Si sigue vacío, intento de emergencia
    if not current_rates_in_memory:
        job_daily_bcv()
    return jsonify(current_rates_in_memory)

@app.route('/api/bcv-history', methods=['GET'])
def get_history():
    if not historical_rates_in_memory:
        load_rates_from_firestore()
    return jsonify(historical_rates_in_memory)

# Arranque Scheduler
try:
    load_rates_from_firestore()
    scheduler = BackgroundScheduler(timezone="America/Caracas")
    if not scheduler.running:
        # BCV: Lunes a Viernes varias veces para asegurar cambios
        scheduler.add_job(job_daily_bcv, 'cron', day_of_week='mon-fri', hour=8, minute=30)
        scheduler.add_job(job_daily_bcv, 'cron', day_of_week='mon-fri', hour=13, minute=30)
        scheduler.add_job(job_daily_bcv, 'cron', day_of_week='mon-fri', hour=18, minute=5)
        # Fin de semana una vez
        scheduler.add_job(job_daily_bcv, 'cron', day_of_week='sat,sun', hour=10, minute=0)
        
        # USDT: Cada 15 minutos siempre
        scheduler.add_job(job_usdt_update, 'interval', minutes=15)
        
        scheduler.start()
        logger.info("Scheduler iniciado correctamente.")
except Exception as e:
    logger.error(f"Error iniciando Scheduler: {e}")

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)