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

app = Flask(__name__)
CORS(app)

current_rates_in_memory = {}
historical_rates_in_memory = []

db = None

try:
    firebase_credentials_json = os.environ.get('FIREBASE_CREDENTIALS_JSON')
    if firebase_credentials_json and not firebase_admin._apps:
        cred = credentials.Certificate(json.loads(firebase_credentials_json)) 
        firebase_admin.initialize_app(cred)
        db = firestore.client()
        print("Firebase inicializado correctamente.")
    elif not firebase_credentials_json:
         print("ADVERTENCIA: No se encontró la variable de entorno 'FIREBASE_CREDENTIALS_JSON'. Firebase no inicializado.")
except Exception as e:
    print(f"ERROR: Fallo al inicializar Firebase: {e}")

DEFAULT_USD_RATE = 00.01
DEFAULT_EUR_RATE = 00.01
FIXED_UT_RATE = 43.00

BCV_URL = "https://www.bcv.org.ve/"

def load_rates_from_firestore():
    global current_rates_in_memory, historical_rates_in_memory
    if db:
        try:
            current_doc = db.collection('rates').document('current').get()
            if current_doc.exists:
                current_rates_in_memory = current_doc.to_dict()
                print("Tasas actuales cargadas de Firestore.")

            history_doc = db.collection('rates').document('history').get()
            if history_doc.exists and 'data' in history_doc.to_dict():
                historical_rates_in_memory = history_doc.to_dict()['data']
                print("Historial cargado de Firestore.")
            
        except Exception as e:
            print(f"Error al cargar datos de Firestore: {e}")
    else:
        print("ADVERTENCIA: No se puede cargar de Firestore, DB no inicializada.")

def fetch_and_update_bcv_rates():
    global current_rates_in_memory, historical_rates_in_memory
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Intentando actualizar tasas del BCV...")
    try:
        response = requests.get(BCV_URL, timeout=15, verify=False)
        response.raise_for_status()

        soup = BeautifulSoup(response.text, 'lxml')

        usd_rate = None
        eur_rate = None

        usd_container = soup.find('div', id='dolar')
        if usd_container:
            centrado_div_usd = usd_container.find('div', class_='centrado')
            if centrado_div_usd:
                usd_strong_tag = centrado_div_usd.find('strong')
                if usd_strong_tag:
                    match = re.search(r'[\d,\.]+', usd_strong_tag.text)
                    if match:
                        usd_rate = float(match.group(0).replace(',', '.').strip())

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
            raise ValueError("No se pudieron encontrar las tasas de USD o EUR en la página del BCV. La estructura HTML pudo haber cambiado.")

        usd_change_percent = 0.0
        eur_change_percent = 0.0

        previous_day_rate_usd = None
        previous_day_rate_eur = None
        
        today_date_str_for_history_check = datetime.now().strftime("%d de %B de %Y")
        for entry in historical_rates_in_memory:
            if entry.get("date") != today_date_str_for_history_check:
                previous_day_rate_usd = entry.get("usd")
                previous_day_rate_eur = entry.get("eur")
                break

        if previous_day_rate_usd is not None and previous_day_rate_usd != 0:
            usd_change_percent = ((usd_rate - previous_day_rate_usd) / previous_day_rate_usd) * 100
        if previous_day_rate_eur is not None and previous_day_rate_eur != 0:
            eur_change_percent = ((eur_rate - previous_day_rate_eur) / previous_day_rate_eur) * 100

        current_rates_in_memory = {
            "usd": usd_rate,
            "eur": eur_rate,
            "ut": FIXED_UT_RATE,
            "last_updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "usd_change_percent": round(usd_change_percent, 2),
            "eur_change_percent": round(eur_change_percent, 2)
        }
        if db:
            db.collection('rates').document('current').set(current_rates_in_memory)

        if not historical_rates_in_memory or historical_rates_in_memory[0]["date"] != today_date_str_for_history_check:
            # 1. Actualizar la variable en memoria
            historical_rates_in_memory.insert(0, {
                "date": today_date_str_for_history_check,
                "usd": usd_rate,
                "eur": eur_rate
            })
            historical_rates_in_memory = historical_rates_in_memory[:30]
        if db:
            db.collection('rates').document('history').set({'data': historical_rates_in_memory}) 
        
        print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Tasas actualizadas y guardadas en FIRESTORE: USD={usd_rate}, EUR={eur_rate}")

    except requests.exceptions.Timeout:
        print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Error: Tiempo de espera agotado al conectar con el BCV. Usando tasas guardadas/predeterminadas.")
    except requests.exceptions.RequestException as e:
        print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Error de red o HTTP al conectar con el BCV: {e}. Usando tasas guardadas/predeterminadas.")
    except AttributeError:
        print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Error de scraping: No se encontraron los elementos HTML esperados. La estructura de la página del BCV pudo haber cambiado. Usando tasas guardadas/predeterminadas.")
    except ValueError as e:
        print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Error de procesamiento de datos: {e}. Usando tasas guardadas/predeterminadas.")
    except Exception as e:
        print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Ocurrió un error inesperado durante el scraping: {e}. Usando tasas guardadas/predeterminadas.")

@app.route('/api/bcv-rates', methods=['GET'])
def get_current_bcv_rates():
    # Retorna las tasas cargadas de Firestore
    return jsonify(current_rates_in_memory)

@app.route('/api/bcv-history', methods=['GET'])
def get_bcv_history():
    # Retorna el historial cargado de Firestore
    return jsonify(historical_rates_in_memory)

scheduler = BackgroundScheduler(timezone="America/Caracas")

if __name__ == '__main__':
    load_rates_from_firestore()
    fetch_and_update_bcv_rates()

    scheduler.add_job(fetch_and_update_bcv_rates, 'cron', hour=0, minute=1, day_of_week='mon-sun')
    scheduler.start()

    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
