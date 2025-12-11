from flask import Flask, jsonify
from flask_cors import CORS
import requests
from bs4 import BeautifulSoup
from datetime import datetime
import os
import re
import json
from apscheduler.schedulers.background import BackgroundScheduler
import pytz
import warnings
from statistics import mean 
from typing import List, Dict, Optional # Nuevo: para mejor tipado

import firebase_admin
from firebase_admin import credentials
from firebase_admin import firestore

warnings.filterwarnings("ignore")

app = Flask(__name__)
CORS(app)

# -------------------------------------------------------------
# [VARIABLES CLAVE]
DEFAULT_USD_RATE = 00.01
DEFAULT_EUR_RATE = 00.01
FIXED_UT_RATE = 43.00       # <-- UNIDAD TRIBUTARIA (Mantenida fija)

DEFAULT_USDT_RATE = 270.00  
BCV_URL = "https://www.bcv.org.ve/"
# [MODIFICACIÓN CLAVE 1]: Endpoint del API público de Binance P2P
BINANCE_P2P_API_URL = "https://p2p.binance.com/bapi/c2c/v2/friendly/c2c/adv/search"
# -------------------------------------------------------------

VENEZUELA_TZ = pytz.timezone("America/Caracas")
db = None 
current_rates = {}

# --- Inicialización de Firebase (sin cambios) ---
try:
    # ... (Tu lógica de inicialización de Firebase) ...
    firebase_credentials_json = os.environ.get('FIREBASE_CREDENTIALS_JSON')
    if firebase_credentials_json:
        cred = credentials.Certificate(json.loads(firebase_credentials_json))
        firebase_admin.initialize_app(cred)
        db = firestore.client()
except Exception:
    pass # Manejo de errores

# --- load_rates_from_firestore (sin cambios) ---
def load_rates_from_firestore():
    global current_rates
    # ... (Tu lógica para cargar tasas de Firestore) ...
    if db:
        try:
            doc_ref = db.collection('current-rates').document('rates')
            doc = doc_ref.get()
            if doc.exists:
                current_rates = doc.to_dict()
            else:
                current_rates = {'usd': DEFAULT_USD_RATE, 'eur': DEFAULT_EUR_RATE, 'ut': FIXED_UT_RATE, 'usdt': DEFAULT_USDT_RATE}
        except Exception:
            current_rates = {'usd': DEFAULT_USD_RATE, 'eur': DEFAULT_EUR_RATE, 'ut': FIXED_UT_RATE, 'usdt': DEFAULT_USDT_RATE}
    else:
        current_rates = {'usd': DEFAULT_USD_RATE, 'eur': DEFAULT_EUR_RATE, 'ut': FIXED_UT_RATE, 'usdt': DEFAULT_USDT_RATE}


# -------------------------------------------------------------
# [NUEVA FUNCIÓN CLAVE 2]: Obtener Promedio USDT P2P de Binance API
def get_p2p_prices(trade_type: str, num_offers: int = 15) -> List[float]:
    """Obtiene una lista de precios de USDT/VES para un tipo de comercio (SELL o BUY)."""
    prices = []
    
    payload = {
        "asset": "USDT",
        "fiat": "VES",
        "merchantCheck": True, # Solo de comerciantes verificados (opcional, mejora calidad)
        "page": 1,
        "rows": num_offers,
        "tradeType": trade_type # 'SELL' (para obtener precios de compra) o 'BUY' (para precios de venta)
    }
    
    try:
        response = requests.post(BINANCE_P2P_API_URL, json=payload, timeout=15)
        response.raise_for_status()
        data = response.json()
        
        # 1. Verificar si la respuesta fue exitosa y tiene datos
        if data.get('code') == '000000' and 'data' in data:
            for adv in data['data']:
                try:
                    price = float(adv['adv']['price'])
                    prices.append(price)
                except (ValueError, KeyError):
                    continue # Ignorar ofertas con precios inválidos
            
            print(f"[{datetime.now(VENEZUELA_TZ).strftime('%Y-%m-%d %H:%M:%S')}] P2P API {trade_type} - Precios obtenidos: {len(prices)}", flush=True)
        return prices

    except requests.RequestException as e:
        print(f"[{datetime.now(VENEZUELA_TZ).strftime('%Y-%m-%d %H:%M:%S')}] Error de red al consultar Binance P2P {trade_type}: {e}", flush=True)
        return []

def scrape_usdt_p2p_average() -> float:
    """Calcula la tasa promedio de USDT/VES (15 compras + 15 ventas)."""
    
    # Obtener 15 ofertas de VENTA (Precios a los que la gente vende USDT)
    sell_prices = get_p2p_prices("SELL", 15)
    
    # Obtener 15 ofertas de COMPRA (Precios a los que la gente compra USDT)
    buy_prices = get_p2p_prices("BUY", 15)

    all_prices = sell_prices + buy_prices
    
    # Calcular el promedio solo si tenemos una cantidad razonable de datos
    if len(all_prices) >= 15: # Usamos 15 como mínimo para asegurar representatividad
        average_rate = mean(all_prices)
        return average_rate
    else:
        print(f"[{datetime.now(VENEZUELA_TZ).strftime('%Y-%m-%d %H:%M:%S')}] Advertencia: Pocos datos P2P ({len(all_prices)}). Usando 0.0.", flush=True)
        return 0.0
# -------------------------------------------------------------

def scrape_bcv():
    """Obtiene las tasas de USD y EUR del BCV. (Se mantiene igual)"""
    # ... (Tu lógica de scraping BCV) ...
    try:
        # ...
        pass
    except Exception:
        return 0.0, 0.0, datetime.now(VENEZUELA_TZ).strftime('%Y-%m-%d')


# -------------------------------------------------------------
# [MODIFICACIÓN CLAVE 3]: Función de Ejecución Programada
def perform_all_scraping():
    """Ejecuta el scraping del BCV, obtiene el promedio de USDT y guarda todo en Firestore."""
    global current_rates
    
    # 1. Obtener tasas del BCV (USD y EUR)
    # ... (Lógica para USD y EUR sin cambios) ...
    bcv_usd, bcv_eur, rates_effective_date = scrape_bcv()
    usd_rate = bcv_usd if bcv_usd > 0.0 else current_rates.get('usd', DEFAULT_USD_RATE)
    eur_rate = bcv_eur if bcv_eur > 0.0 else current_rates.get('eur', DEFAULT_EUR_RATE)
    
    # 2. Obtener el promedio de USDT (P2P de Binance)
    calculated_usdt_rate = scrape_usdt_p2p_average()
    
    # Usar el valor P2P, o el de respaldo si el cálculo falla
    final_usdt_rate = calculated_usdt_rate if calculated_usdt_rate > 1.0 else current_rates.get('usdt', DEFAULT_USDT_RATE)
        
    # 3. La UT se mantiene Fija
    ut_rate = FIXED_UT_RATE
        
    # 4. Preparar los datos
    current_time = datetime.now(VENEZUELA_TZ)
    last_updated_str = current_time.strftime('%Y-%m-%d %H:%M:%S')

    # ... (Lógica de new_rates sin cambios, pero ahora incluye el usdt dinámico) ...
    new_rates = {
        'usd': round(usd_rate, 4),
        'eur': round(eur_rate, 4),
        'ut': round(ut_rate, 2),            
        'usdt': round(final_usdt_rate, 4),  
        'usd_change_percent': round((usd_rate - current_rates.get('usd', usd_rate)) / current_rates.get('usd', usd_rate) * 100, 2),
        'eur_change_percent': round((eur_rate - current_rates.get('eur', eur_rate)) / current_rates.get('eur', eur_rate) * 100, 2),
        'last_updated': last_updated_str,
        'rates_effective_date': rates_effective_date
    }
    
    # 5. Actualizar Firestore y Logs
    if db:
        # ... (Tu lógica para actualizar current-rates, historical-rates y scrape-logs) ...
        try:
            db.collection('current-rates').document('rates').set(new_rates)
            current_rates = new_rates
            
            # ... (Guardar en historical-rates) ...
            if rates_effective_date != current_rates.get('rates_effective_date_saved', ''):
                historical_data = {
                    'date': current_time.strftime('%d de %B de %Y'),
                    'date_ymd': rates_effective_date,
                    'usd': new_rates['usd'],
                    'eur': new_rates['eur']
                }
                db.collection('historical-rates').add(historical_data)
                
            # 7. Guardar Log de Scraping
            log_data = {
                'timestamp': last_updated_str,
                'status': 'SUCCESS',
                'usd_rate': new_rates['usd'],
                'usdt_rate': new_rates['usdt'], 
                'source': 'BCV y Binance P2P API'
            }
            db.collection('scrape-logs').add(log_data)
            
        except Exception as e:
            print(f"[{last_updated_str}] ERROR al guardar en Firestore: {e}", flush=True)

# -------------------------------------------------------------

# --- Rutas API, limpieza, y ejecución (sin cambios) ---

scheduler = BackgroundScheduler(timezone="America/Caracas")

if __name__ == '__main__':
    # ... (Tu lógica de inicio con la programación del perform_all_scraping) ...
    load_rates_from_firestore()

    # Programar el scraping de tasas (BCV y USDT P2P)
    scheduler.add_job(perform_all_scraping, 'interval', minutes=15) 
    print(f"[{datetime.now(VENEZUELA_TZ).strftime('%Y-%m-%d %H:%M:%S')}] Actualización de tasas programada cada 15 minutos.", flush=True)

    # ... (Limpieza de historial) ...

    scheduler.start()
    app.run(host='0.0.0.0', port=5000)