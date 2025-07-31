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

import firebase_admin
from firebase_admin import credentials
from firebase_admin import firestore

warnings.filterwarnings("ignore")

app = Flask(__name__)
CORS(app)

DEFAULT_USD_RATE = 00.01
DEFAULT_EUR_RATE = 00.01
FIXED_UT_RATE = 43.00

BCV_URL = "https://www.bcv.org.ve/"

VENEZUELA_TZ = pytz.timezone("America/Caracas")

try:
    firebase_credentials_json = os.environ.get('FIREBASE_CREDENTIALS_JSON')
    if firebase_credentials_json:
        cred = credentials.Certificate(json.loads(firebase_credentials_json))
        firebase_admin.initialize_app(cred)
        db = firestore.client()
        print(f"[{datetime.now(VENEZUELA_TZ).strftime('%Y-%m-%d %H:%M:%S')}] Firebase inicializado exitosamente.", flush=True)
    else:
        print(f"[{datetime.now(VENEZUELA_TZ).strftime('%Y-%m-%d %H:%M:%S')}] Advertencia: FIREBASE_CREDENTIALS_JSON no está configurado. La aplicación no podrá usar Firestore.", flush=True)
        db = None
except Exception as e:
    print(f"[{datetime.now(VENEZUELA_TZ).strftime('%Y-%m-%d %H:%M:%S')}] Error al inicializar Firebase: {e}", flush=True)
    db = None

current_rates_in_memory = {
    "usd": DEFAULT_USD_RATE,
    "eur": DEFAULT_EUR_RATE,
    "ut": FIXED_UT_RATE,
    "last_updated": datetime.now(VENEZUELA_TZ).strftime("%Y-%m-%d %H:%M:%S") + " (predeterminado)",
    "usd_change_percent": 0.0,
    "eur_change_percent": 0.0,
    "rates_effective_date": datetime.now(VENEZUELA_TZ).strftime("%Y-%m-%d")
}
historical_rates_in_memory = []

def load_rates_from_firestore():
    global current_rates_in_memory, historical_rates_in_memory
    if db is None:
        print(f"[{datetime.now(VENEZUELA_TZ).strftime('%Y-%m-%d %H:%M:%S')}] Firestore no está inicializado. Usando datos predeterminados en memoria.", flush=True)
        return

    print(f"[{datetime.now(VENEZUELA_TZ).strftime('%Y-%m-%d %H:%M:%S')}] Intentando cargar tasas desde Firestore...", flush=True)
    try:
        current_rates_doc_ref = db.collection('current_rates').document('latest_rates')
        current_rates_doc = current_rates_doc_ref.get()
        if current_rates_doc.exists:
            current_rates_in_memory = current_rates_doc.to_dict()
            print(f"[{datetime.now(VENEZUELA_TZ).strftime('%Y-%m-%d %H:%M:%S')}] Tasas actuales cargadas de Firestore: {current_rates_in_memory}", flush=True)
        else:
            print(f"[{datetime.now(VENEZUELA_TZ).strftime('%Y-%m-%d %H:%M:%S')}] Documento 'latest_rates' no encontrado en Firestore. Usando valores predeterminados.", flush=True)
            save_current_rates_to_firestore(current_rates_in_memory)

        historical_docs = db.collection('historical_rates') \
                            .order_by('date_ymd', direction=firestore.Query.DESCENDING) \
                            .limit(15) \
                            .get()
        historical_rates_in_memory = []
        for doc in historical_docs:
            historical_rates_in_memory.append(doc.to_dict())
        
        historical_rates_in_memory.sort(key=lambda x: datetime.strptime(x['date_ymd'], "%Y-%m-%d"), reverse=True)

        if not historical_rates_in_memory:
            print(f"[{datetime.now(VENEZUELA_TZ).strftime('%Y-%m-%d %H:%M:%S')}] No hay historial en Firestore. Generando datos simulados para el historial.", flush=True)
            today = datetime.now(VENEZUELA_TZ)
            for i in range(15):
                date = today - timedelta(days=i)
                sim_usd = round(DEFAULT_USD_RATE + (random.random() - 0.5) * 0.5, 2)
                sim_eur = round(DEFAULT_EUR_RATE + (random.random() - 0.5) * 0.6, 2)
                historical_rates_in_memory.append({
                    "date": date.strftime("%d de %B de %Y"),
                    "date_ymd": date.strftime("%Y-%m-%d"),
                    "usd": sim_usd,
                    "eur": sim_eur
                })
            for entry in historical_rates_in_memory:
                save_historical_rate_to_firestore(entry)
            print(f"[{datetime.now(VENEZUELA_TZ).strftime('%Y-%m-%d %H:%M:%S')}] Historial simulado guardado en Firestore.", flush=True)
        else:
            print(f"[{datetime.now(VENEZUELA_TZ).strftime('%Y-%m-%d %H:%M:%S')}] Historial cargado de Firestore: {len(historical_rates_in_memory)} entradas.", flush=True)

    except Exception as e:
        print(f"[{datetime.now(VENEZUELA_TZ).strftime('%Y-%m-%d %H:%M:%S')}] Error al cargar datos de Firestore: {e}. Usando datos en memoria/predeterminados.", flush=True)

def save_current_rates_to_firestore(data):
    if db is None: return
    try:
        doc_ref = db.collection('current_rates').document('latest_rates')
        doc_ref.set(data)
        print(f"[{datetime.now(VENEZUELA_TZ).strftime('%Y-%m-%d %H:%M:%S')}] Tasas actuales guardadas en Firestore.", flush=True)
    except Exception as e:
        print(f"[{datetime.now(VENEZUELA_TZ).strftime('%Y-%m-%d %H:%M:%S')}] Error al guardar tasas actuales en Firestore: {e}", flush=True)

def save_historical_rate_to_firestore(data):
    if db is None: return
    try:
        doc_ref = db.collection('historical_rates').document(data['date_ymd'])
        doc_ref.set(data)
        print(f"[{datetime.now(VENEZUELA_TZ).strftime('%Y-%m-%d %H:%M:%S')}] Entrada de historial guardada/actualizada en Firestore para {data['date_ymd']}.", flush=True)
    except Exception as e:
        print(f"[{datetime.now(VENEZUELA_TZ).strftime('%Y-%m-%d %H:%M:%S')}] Error al guardar historial en Firestore: {e}", flush=True)

def cleanup_old_historical_rates():
    if db is None: return
    try:
        limit_date = datetime.now(VENEZUELA_TZ) - timedelta(days=15)
        limit_date_str = limit_date.strftime("%Y-%m-%d")

        old_docs = db.collection('historical_rates') \
                     .where('date_ymd', '<', limit_date_str) \
                     .get()
        
        deleted_count = 0
        for doc in old_docs:
            doc.reference.delete()
            deleted_count += 1
        
        if deleted_count > 0:
            print(f"[{datetime.now(VENEZUELA_TZ).strftime('%Y-%m-%d %H:%M:%S')}] Limpieza de historial: {deleted_count} documentos antiguos eliminados de Firestore.", flush=True)
    except Exception as e:
        print(f"[{datetime.now(VENEZUELA_TZ).strftime('%Y-%m-%d %H:%M:%S')}] Error al limpiar historial en Firestore: {e}", flush=True)


def fetch_and_update_bcv_rates_firestore():
    global current_rates_in_memory, historical_rates_in_memory
    
    now_venezuela = datetime.now(VENEZUELA_TZ)
    today_date_str_ymd = now_venezuela.strftime("%Y-%m-%d")
    today_date_str_human = now_venezuela.strftime("%d de %B de %Y")

    early_morning_scrape_minutes = [1, 2, 4, 6, 8, 10]
    is_scheduled_early_morning_call = (
        now_venezuela.hour == 0 and now_venezuela.minute in early_morning_scrape_minutes
    )

    print(f"[{now_venezuela.strftime('%Y-%m-%d %H:%M:%S')}] fetch_and_update_bcv_rates_firestore: Iniciando ejecución.", flush=True)

    load_rates_from_firestore()

    print(f"[{now_venezuela.strftime('%Y-%m-%d %H:%M:%S')}] fetch_and_update_bcv_rates_firestore: today_date_str_ymd = {today_date_str_ymd}", flush=True)
    print(f"[{now_venezuela.strftime('%Y-%m-%d %H:%M:%S')}] fetch_and_update_bcv_rates_firestore: current_rates_in_memory.rates_effective_date = {current_rates_in_memory.get('rates_effective_date')}", flush=True)
    print(f"[{now_venezuela.strftime('%Y-%m-%d %H:%M:%S')}] fetch_and_update_bcv_rates_firestore: is_scheduled_early_morning_call = {is_scheduled_early_morning_call}", flush=True)

    # Obtener la fecha efectiva del BCV para la validación
    bcv_effective_date_ymd = None
    try:
        response_check = requests.get(BCV_URL, timeout=5, verify=False)
        response_check.raise_for_status()
        soup_check = BeautifulSoup(response_check.text, 'lxml')
        date_span = soup_check.find('span', class_='date-display-single')
        if date_span and 'content' in date_span.attrs:
            # Extraer solo la parte de la fecha (YYYY-MM-DD)
            bcv_effective_date_ymd = date_span['content'].split('T')[0]
            print(f"[{now_venezuela.strftime('%Y-%m-%d %H:%M:%S')}] fetch_and_update_bcv_rates_firestore: Fecha efectiva del BCV encontrada: {bcv_effective_date_ymd}", flush=True)
        else:
            print(f"[{now_venezuela.strftime('%Y-%m-%d %H:%M:%S')}] fetch_and_update_bcv_rates_firestore: No se pudo encontrar la fecha efectiva en la página del BCV.", flush=True)

    except Exception as e:
        print(f"[{now_venezuela.strftime('%Y-%m-%d %H:%M:%S')}] fetch_and_update_bcv_rates_firestore: Error al intentar obtener la fecha efectiva del BCV: {e}", flush=True)
    
    # La condición de scraping ahora usa la fecha efectiva del BCV si está disponible,
    # o cae de nuevo a la fecha actual si no se pudo obtener del BCV.
    # Esto asegura que el scraping solo ocurra una vez por día efectivo del BCV.
    effective_date_to_check = bcv_effective_date_ymd if bcv_effective_date_ymd else today_date_str_ymd
    
    condition_result = (current_rates_in_memory.get("rates_effective_date") == effective_date_to_check and not is_scheduled_early_morning_call)
    print(f"[{now_venezuela.strftime('%Y-%m-%d %H:%M:%S')}] fetch_and_update_bcv_rates_firestore: Full condition result = {condition_result}", flush=True)

    if condition_result:
        print(f"[{now_venezuela.strftime('%Y-%m-%d %H:%M:%S')}] fetch_and_update_bcv_rates_firestore: Tasas del BCV para hoy ({effective_date_to_check}) ya están fijadas en Firestore y no es un horario de scraping programado. No se realizará scraping nuevamente.", flush=True)
        return
    else:
        print(f"[{now_venezuela.strftime('%Y-%m-%d %H:%M:%S')}] fetch_and_update_bcv_rates_firestore: Condición para scraping met. Procediendo con el scraping.", flush=True)

    previous_usd_rate_for_calc = current_rates_in_memory.get("usd", DEFAULT_USD_RATE)
    previous_eur_rate_for_calc = current_rates_in_memory.get("eur", DEFAULT_EUR_RATE)

    found_previous_day_rate_usd = None
    found_previous_day_rate_eur = None
    for entry in historical_rates_in_memory:
        if entry.get("date_ymd") != today_date_str_ymd:
            found_previous_day_rate_usd = entry.get("usd")
            found_previous_day_rate_eur = entry.get("eur")
            break

    if found_previous_day_rate_usd is not None:
        previous_usd_rate_for_calc = found_previous_day_rate_usd
    if found_previous_day_rate_eur is not None:
        previous_eur_rate_for_calc = found_previous_day_rate_eur

    try:
        print(f"[{now_venezuela.strftime('%Y-%m-%d %H:%M:%S')}] fetch_and_update_bcv_rates_firestore: Realizando solicitud GET a {BCV_URL}...", flush=True)
        response = requests.get(BCV_URL, timeout=15, verify=False)
        response.raise_for_status()
        print(f"[{now_venezuela.strftime('%Y-%m-%d %H:%M:%S')}] fetch_and_update_bcv_rates_firestore: Solicitud GET exitosa. Status: {response.status_code}", flush=True)

        soup = BeautifulSoup(response.text, 'lxml')
        print(f"[{now_venezuela.strftime('%Y-%m-%d %H:%M:%S')}] fetch_and_update_bcv_rates_firestore: BeautifulSoup parseado.", flush=True)

        usd_rate = None
        eur_rate = None
        scraped_effective_date_ymd = None
        scraped_effective_date_human = None

        # Scraping del Dolar
        usd_container = soup.find('div', id='dolar')
        if usd_container:
            centrado_div_usd = usd_container.find('div', class_='centrado')
            if centrado_div_usd:
                usd_strong_tag = centrado_div_usd.find('strong')
                if usd_strong_tag:
                    match = re.search(r'[\d,\.]+', usd_strong_tag.text)
                    if match:
                        usd_rate = float(match.group(0).replace(',', '.').strip())
        print(f"[{now_venezuela.strftime('%Y-%m-%d %H:%M:%S')}] fetch_and_update_bcv_rates_firestore: USD encontrado: {usd_rate}", flush=True)

        # Scraping del Euro
        eur_container = soup.find('div', id='euro')
        if eur_container:
            centrado_div_eur = eur_container.find('div', class_='centrado')
            if centrado_div_eur:
                eur_strong_tag = centrado_div_eur.find('strong')
                if eur_strong_tag:
                    match = re.search(r'[\d,\.]+', eur_strong_tag.text)
                    if match:
                        eur_rate = float(match.group(0).replace(',', '.').strip())
        print(f"[{now_venezuela.strftime('%Y-%m-%d %H:%M:%S')}] fetch_and_update_bcv_rates_firestore: EUR encontrado: {eur_rate}", flush=True)

        # Scraping de la Fecha de Valor
        date_span = soup.find('span', class_='date-display-single')
        if date_span and 'content' in date_span.attrs:
            scraped_effective_date_ymd = date_span['content'].split('T')[0]
            scraped_effective_date_human = date_span.get_text(strip=True)
        print(f"[{now_venezuela.strftime('%Y-%m-%d %H:%M:%S')}] fetch_and_update_bcv_rates_firestore: Fecha de valor scrapeada: YMD={scraped_effective_date_ymd}, Human={scraped_effective_date_human}", flush=True)


        if usd_rate is None or eur_rate is None or scraped_effective_date_ymd is None:
            raise ValueError("No se pudieron encontrar los elementos HTML esperados para USD, EUR o la fecha. La estructura de la página del BCV pudo haber cambiado.")

        usd_change_percent = 0.0
        eur_change_percent = 0.0

        if previous_usd_rate_for_calc != 0:
            usd_change_percent = ((usd_rate - previous_usd_rate_for_calc) / previous_usd_rate_for_calc) * 100
        if previous_eur_rate_for_calc != 0:
            eur_change_percent = ((eur_rate - previous_eur_rate_for_calc) / previous_eur_rate_for_calc) * 100

        current_rates_in_memory = {
            "usd": usd_rate,
            "eur": eur_rate,
            "ut": FIXED_UT_RATE,
            "last_updated": now_venezuela.strftime("%Y-%m-%d %H:%M:%S"),
            "usd_change_percent": round(usd_change_percent, 2),
            "eur_change_percent": round(eur_change_percent, 2),
            "rates_effective_date": scraped_effective_date_ymd # Usar la fecha scrapeada
        }
        print(f"[{now_venezuela.strftime('%Y-%m-%d %H:%M:%S')}] fetch_and_update_bcv_rates_firestore: Tasas calculadas y actualizadas en memoria: {current_rates_in_memory}", flush=True)
        
        save_current_rates_to_firestore(current_rates_in_memory)

        # Actualizar el historial en Firestore con la fecha scrapeada
        today_history_doc_ref = db.collection('historical_rates').document(scraped_effective_date_ymd)
        today_history_doc = today_history_doc_ref.get()

        if not today_history_doc.exists:
            new_history_entry = {
                "date": scraped_effective_date_human, # Usar la fecha legible scrapeada
                "date_ymd": scraped_effective_date_ymd,
                "usd": usd_rate,
                "eur": eur_rate
            }
            save_historical_rate_to_firestore(new_history_entry)
            load_rates_from_firestore() 
        else:
            updated_history_entry = {
                "usd": usd_rate,
                "eur": eur_rate
            }
            today_history_doc_ref.update(updated_history_entry)
            print(f"[{now_venezuela.strftime('%Y-%m-%d %H:%M:%S')}] fetch_and_update_bcv_rates_firestore: Entrada de historial existente actualizada en Firestore para {scraped_effective_date_ymd}.", flush=True)
            load_rates_from_firestore()

        print(f"[{now_venezuela.strftime('%Y-%m-%d %H:%M:%S')}] fetch_and_update_bcv_rates_firestore: Tasas actualizadas y guardadas en Firestore: USD={usd_rate:.4f} ({usd_change_percent:.2f}%), EUR={eur_rate:.4f} ({eur_change_percent:.2f}%)", flush=True)

    except Exception as e:
        print(f"[{now_venezuela.strftime('%Y-%m-%d %H:%M:%S')}] fetch_and_update_bcv_rates_firestore: Ocurrió un error durante el scraping: {e}. Usando tasas cargadas de Firestore/predeterminadas.", flush=True)
        # En caso de error, la fecha efectiva en Firestore se actualizará a la fecha actual
        # para evitar reintentos constantes del scraping si la página está rota.
        current_rates_in_memory["rates_effective_date"] = today_date_str_ymd 
        if previous_usd_rate_for_calc != 0 and current_rates_in_memory.get("usd") is not None:
            current_rates_in_memory["usd_change_percent"] = ((current_rates_in_memory["usd"] - previous_usd_rate_for_calc) / previous_usd_rate_for_calc) * 100
        else:
            current_rates_in_memory["usd_change_percent"] = 0.0
        if previous_eur_rate_for_calc != 0 and current_rates_in_memory.get("eur") is not None:
            current_rates_in_memory["eur_change_percent"] = ((current_rates_in_memory["eur"] - previous_eur_rate_for_calc) / previous_eur_rate_for_calc) * 100
        else:
            current_rates_in_memory["eur_change_percent"] = 0.0
        save_current_rates_to_firestore(current_rates_in_memory)
    finally:
        print(f"[{now_venezuela.strftime('%Y-%m-%d %H:%M:%S')}] fetch_and_update_bcv_rates_firestore: Finalizando ejecución.", flush=True)

@app.route('/api/bcv-rates', methods=['GET', 'HEAD'])
def get_current_bcv_rates():
    load_rates_from_firestore()
    return jsonify(current_rates_in_memory)

@app.route('/api/bcv-history', methods=['GET'])
def get_bcv_history():
    load_rates_from_firestore()
    return jsonify(historical_rates_in_memory)

scheduler = BackgroundScheduler(timezone="America/Caracas")

if __name__ == '__main__':
    print(f"[{datetime.now(VENEZUELA_TZ).strftime('%Y-%m-%d %H:%M:%S')}] Iniciando la aplicación. Ejecutando scraping inicial...", flush=True)
    try:
        fetch_and_update_bcv_rates_firestore()
    except Exception as e:
        print(f"[{datetime.now(VENEZUELA_TZ).strftime('%Y-%m-%d %H:%M:%S')}] Error durante el scraping inicial: {e}", flush=True)
    print(f"[{datetime.now(VENEZUELA_TZ).strftime('%Y-%m-%d %H:%M:%S')}] Scraping inicial completado.", flush=True)

    scheduler.add_job(fetch_and_update_bcv_rates_firestore, 'cron', hour=0, minute=1, day_of_week='mon-fri')
    scheduler.add_job(fetch_and_update_bcv_rates_firestore, 'cron', hour=0, minute=2, day_of_week='mon-fri')
    scheduler.add_job(fetch_and_update_bcv_rates_firestore, 'cron', hour=0, minute=4, day_of_week='mon-fri')
    scheduler.add_job(fetch_and_update_bcv_rates_firestore, 'cron', hour=0, minute=6, day_of_week='mon-fri')
    scheduler.add_job(fetch_and_update_bcv_rates_firestore, 'cron', hour=0, minute=8, day_of_week='mon-fri')
    scheduler.add_job(fetch_and_update_bcv_rates_firestore, 'cron', hour=0, minute=10, day_of_week='mon-fri')
    print(f"[{datetime.now(VENEZUELA_TZ).strftime('%Y-%m-%d %H:%M:%S')}] Scraping diario programado para 00:01, 00:02, 00:04, 00:06, 00:08, 00:10 (L-V).", flush=True)
    
    scheduler.add_job(cleanup_old_historical_rates, 'cron', hour=1, minute=0, day_of_week='mon-sun')
    print(f"[{datetime.now(VENEZUELA_TZ).strftime('%Y-%m-%d %H:%M:%S')}] Limpieza de historial programada diariamente a la 01:00 AM.", flush=True)

    scheduler.start()
    print(f"[{datetime.now(VENEZUELA_TZ).strftime('%Y-%m-%d %H:%M:%S')}] Scheduler iniciado.", flush=True)

    port = int(os.environ.get('PORT', 5000))
    print(f"[{datetime.now(VENEZUELA_TZ).strftime('%Y-%m-%d %H:%M:%S')}] Iniciando servidor Flask en el puerto {port}...", flush=True)
    app.run(host='0.0.0.0', port=port, debug=False)
