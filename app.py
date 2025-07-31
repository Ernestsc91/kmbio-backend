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

db = None # Inicializar db como None para manejar el caso de no credenciales

try:
    firebase_credentials_json = os.environ.get('FIREBASE_CREDENTIALS_JSON')
    if firebase_credentials_json:
        cred = credentials.Certificate(json.loads(firebase_credentials_json))
        firebase_admin.initialize_app(cred)
        db = firestore.client()
        print(f"[{datetime.now(VENEZUELA_TZ).strftime('%Y-%m-%d %H:%M:%S')}] Firebase inicializado exitosamente.", flush=True)
    else:
        print(f"[{datetime.now(VENEZUELA_TZ).strftime('%Y-%m-%d %H:%M:%S')}] Advertencia: FIREBASE_CREDENTIALS_JSON no está configurado. La aplicación no podrá usar Firestore.", flush=True)
except Exception as e:
    print(f"[{datetime.now(VENEZUELA_TZ).strftime('%Y-%m-%d %H:%M:%S')}] Error al inicializar Firebase: {e}", flush=True)

current_rates_in_memory = {
    "usd": DEFAULT_USD_RATE,
    "eur": DEFAULT_EUR_RATE,
    "ut": FIXED_UT_RATE,
    "last_updated": datetime.now(VENEZUELA_TZ).strftime("%Y-%m-%d %H:%M:%S") + " (predeterminado)",
    "usd_change_percent": 0.0,
    "eur_change_percent": 0.0,
    "rates_effective_date": datetime.now(VENEZUELA_TZ).strftime("%Y-%m-%d") # Fecha efectiva predeterminada
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
            # Asegurarse de que 'rates_effective_date' exista, si no, usar la fecha actual
            if 'rates_effective_date' not in current_rates_in_memory:
                current_rates_in_memory['rates_effective_date'] = datetime.now(VENEZUELA_TZ).strftime("%Y-%m-%d")
                save_current_rates_to_firestore(current_rates_in_memory) # Guardar para persistir la nueva clave
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

def add_scrape_log_entry(status, message, rates_effective_date=None, usd_rate=None, eur_rate=None, error_details=None, doc_id=None):
    if db is None: return None
    try:
        log_entry = {
            "timestamp": datetime.now(VENEZUELA_TZ).isoformat(),
            "status": status,
            "message": message,
            "rates_effective_date": rates_effective_date,
            "usd_rate": usd_rate,
            "eur_rate": eur_rate,
            "error_details": error_details
        }
        if doc_id:
            doc_ref = db.collection('scrape_logs').document(doc_id)
            doc_ref.update(log_entry)
            print(f"[{datetime.now(VENEZUELA_TZ).strftime('%Y-%m-%d %H:%M:%S')}] Log de scraping actualizado (ID: {doc_id}, Estado: {status}).", flush=True)
            return doc_id
        else:
            doc_ref = db.collection('scrape_logs').add(log_entry)
            print(f"[{datetime.now(VENEZUELA_TZ).strftime('%Y-%m-%d %H:%M:%S')}] Log de scraping añadido (ID: {doc_ref[1].id}, Estado: {status}).", flush=True)
            return doc_ref[1].id
    except Exception as e:
        print(f"[{datetime.now(VENEZUELA_TZ).strftime('%Y-%m-%d %H:%M:%S')}] Error al añadir/actualizar log de scraping en Firestore: {e}", flush=True)
        return None

def parse_bcv_date(date_str):
    """
    Intenta parsear una cadena de fecha del BCV (ej. 'Jueves, 31 Julio 2025' o '31 de Julio de 2025')
    a un formato YYYY-MM-DD.
    """
    if not date_str:
        return None
    
    month_map = {
        'enero': 1, 'febrero': 2, 'marzo': 3, 'abril': 4, 'mayo': 5, 'junio': 6,
        'julio': 7, 'agosto': 8, 'septiembre': 9, 'octubre': 10, 'noviembre': 11, 'diciembre': 12
    }

    # Patrón para "Weekday, DD Mes YYYY" (ej. "Jueves, 31 Julio 2025")
    match = re.search(r'(?:[a-zA-ZáéíóúÁÉÍÓÚñÑ]+,?\s*)?(\d{1,2})\s+([a-zA-ZáéíóúÁÉÍÓÚñÑ]+)\s+(\d{4})', date_str)
    if match:
        day = int(match.group(1))
        month_name = match.group(2).lower()
        year = int(match.group(3))
        
        month = month_map.get(month_name)
        if month:
            try:
                return datetime(year, month, day).strftime("%Y-%m-%d")
            except ValueError:
                pass # Continue to next pattern if this fails
    
    # Patrón para "DD de Mes de YYYY" (ej. '31 de Julio de 2025')
    match = re.search(r'(\d{1,2}) de ([a-zA-Z]+) de (\d{4})', date_str)
    if match:
        day = int(match.group(1))
        month_name = match.group(2).lower()
        year = int(match.group(3))
        
        month = month_map.get(month_name)
        if month:
            try:
                return datetime(year, month, day).strftime("%Y-%m-%d")
            except ValueError:
                pass # Continue to next pattern if this fails
    
    # Patrón para "DD/MM/YYYY"
    match = re.search(r'(\d{1,2})/(\d{1,2})/(\d{4})', date_str)
    if match:
        day = int(match.group(1))
        month = int(match.group(2))
        year = int(match.group(3))
        try:
            return datetime(year, month, day).strftime("%Y-%m-%d")
        except ValueError:
            pass # Continue if this fails

    return None


def fetch_and_update_bcv_rates_firestore():
    global current_rates_in_memory, historical_rates_in_memory
    
    now_venezuela = datetime.now(VENEZUELA_TZ)
    today_date_str_ymd = now_venezuela.strftime("%Y-%m-%d")
    today_date_str_human = now_venezuela.strftime("%d de %B de %Y")
    
    # Solo ejecutar en días de semana (Lunes=0 a Viernes=4)
    if now_venezuela.weekday() >= 5: # Sábado=5, Domingo=6
        print(f"[{now_venezuela.strftime('%Y-%m-%d %H:%M:%S')}] fetch_and_update_bcv_rates_firestore: Hoy es fin de semana. No se realizará scraping.", flush=True)
        return

    print(f"[{now_venezuela.strftime('%Y-%m-%d %H:%M:%S')}] fetch_and_update_bcv_rates_firestore: --- INICIANDO EJECUCIÓN BAJO DEMANDA ---", flush=True)
    
    log_doc_id = add_scrape_log_entry("STARTED", "Iniciando intento de scraping bajo demanda.", rates_effective_date=today_date_str_ymd)

    load_rates_from_firestore() # Esto actualiza current_rates_in_memory con los datos de Firestore

    print(f"[{now_venezuela.strftime('%Y-%m-%d %H:%M:%S')}] fetch_and_update_bcv_rates_firestore: Después de cargar de Firestore. rates_effective_date en memoria = {current_rates_in_memory.get('rates_effective_date')}", flush=True)
    print(f"[{now_venezuela.strftime('%Y-%m-%d %H:%M:%S')}] fetch_and_update_bcv_rates_firestore: Fecha actual del sistema (YMD) = {today_date_str_ymd}", flush=True)

    scraped_effective_date_ymd = None # Inicializar para el scraping

    try:
        print(f"[{now_venezuela.strftime('%Y-%m-%d %H:%M:%S')}] fetch_and_update_bcv_rates_firestore: Realizando solicitud GET a {BCV_URL}...", flush=True)
        response = requests.get(BCV_URL, timeout=15, verify=False)
        response.raise_for_status()
        print(f"[{now_venezuela.strftime('%Y-%m-%d %H:%M:%S')}] fetch_and_update_bcv_rates_firestore: Solicitud GET exitosa. Status: {response.status_code}", flush=True)

        soup = BeautifulSoup(response.text, 'lxml')
        print(f"[{now_venezuela.strftime('%Y-%m-%d %H:%M:%S')}] fetch_and_update_bcv_rates_firestore: BeautifulSoup parseado.", flush=True)

        usd_rate = None
        eur_rate = None
        scraped_date_text = None

        # Scraping de la tasa USD
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

        # Scraping de la tasa EUR
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

        # --- Nuevo: Scraping de la Fecha de Validez ---
        scraped_date_element = soup.find('span', class_='date-display-single')
        if scraped_date_element and 'content' in scraped_date_element.attrs:
            # Priorizar el atributo 'content' que ya está en formato ISO
            iso_date_str = scraped_date_element['content']
            # Extraer solo la parte YYYY-MM-DD
            scraped_effective_date_ymd = iso_date_str.split('T')[0]
            print(f"[{now_venezuela.strftime('%Y-%m-%d %H:%M:%S')}] fetch_and_update_bcv_rates_firestore: Fecha de validez scrapeada (ISO content): '{iso_date_str}' -> Parsed YMD: {scraped_effective_date_ymd}", flush=True)
        elif scraped_date_element and scraped_date_element.text:
            # Fallback a parsear el texto si el atributo 'content' no está presente
            scraped_date_text = scraped_date_element.text.strip()
            scraped_effective_date_ymd = parse_bcv_date(scraped_date_text) # Usar el parser existente para texto
            print(f"[{now_venezuela.strftime('%Y-%m-%d %H:%M:%S')}] fetch_and_update_bcv_rates_firestore: Fecha de validez scrapeada (text fallback): '{scraped_date_text}' -> Parsed YMD: {scraped_effective_date_ymd}", flush=True)
        else:
            print(f"[{now_venezuela.strftime('%Y-%m-%d %H:%M:%S')}] fetch_and_update_bcv_rates_firestore: No se encontró el elemento de fecha de validez o su contenido. Usando la fecha del sistema como fallback para la condición de scraping.", flush=True)
            scraped_effective_date_ymd = today_date_str_ymd # Fallback para la condición

        if usd_rate is None or eur_rate is None:
            raise ValueError("No se pudieron encontrar los elementos HTML esperados para USD o EUR. La estructura de la página del BCV pudo haber cambiado.")

        # --- Lógica de Condición de Scraping Basada en Fecha de Validez ---
        # SOLO actualizamos si la fecha scrapeada es LA FECHA ACTUAL DEL SISTEMA.
        # Si el BCV publica tasas futuras, las ignoramos por ahora.
        if scraped_effective_date_ymd != today_date_str_ymd:
            message = f"Tasas scrapeadas del BCV ({scraped_effective_date_ymd}) no corresponden a la fecha actual del sistema ({today_date_str_ymd}). Saltando actualización de tasas."
            print(f"[{now_venezuela.strftime('%Y-%m-%d %H:%M:%S')}] {message}", flush=True)
            add_scrape_log_entry("SKIPPED", message, rates_effective_date=scraped_effective_date_ymd, doc_id=log_doc_id)
            return # Salir de la función sin actualizar las tasas

        # Si llegamos aquí, significa que scraped_effective_date_ymd ES today_date_str_ymd.
        # Ahora, verificamos si las tasas en Firestore ya son para hoy.
        # Esto previene escrituras innecesarias si la app se abre varias veces en el mismo día
        # y el BCV no ha cambiado sus tasas *de hoy*.
        if current_rates_in_memory.get("rates_effective_date") == today_date_str_ymd:
            message = f"Tasas del BCV para la fecha efectiva {today_date_str_ymd} ya están fijadas en Firestore. Saltando re-actualización."
            print(f"[{now_venezuela.strftime('%Y-%m-%d %H:%M:%S')}] {message}", flush=True)
            add_scrape_log_entry("SKIPPED", message, rates_effective_date=today_date_str_ymd, doc_id=log_doc_id)
            return # Salir de la función si ya tenemos las tasas de hoy

        print(f"[{now_venezuela.strftime('%Y-%m-%d %H:%M:%S')}] fetch_and_update_bcv_rates_firestore: Procediendo con el scraping y actualización.", flush=True)
        print(f"[{now_venezuela.strftime('%Y-%m-%d %H:%M:%S')}] fetch_and_update_bcv_rates_firestore: --- ENTRANDO EN EL BLOQUE DE ACTUALIZACIÓN DE TASAS ---", flush=True)

        usd_change_percent = 0.0
        eur_change_percent = 0.0

        previous_usd_rate_for_calc = current_rates_in_memory.get("usd", DEFAULT_USD_RATE)
        previous_eur_rate_for_calc = current_rates_in_memory.get("eur", DEFAULT_EUR_RATE)

        # Buscar la tasa del día anterior en el historial para el cálculo de cambio porcentual
        found_previous_day_rate_usd = None
        found_previous_day_rate_eur = None
        for entry in historical_rates_in_memory:
            # Si el historial tiene una entrada para la fecha de validez scrapeada, no la usamos como "anterior"
            # Buscamos la primera entrada que NO sea la fecha de validez scrapeada
            if entry.get("date_ymd") != scraped_effective_date_ymd:
                found_previous_day_rate_usd = entry.get("usd")
                found_previous_day_rate_eur = entry.get("eur")
                break

        if found_previous_day_rate_usd is not None:
            previous_usd_rate_for_calc = found_previous_day_rate_usd
        if found_previous_day_rate_eur is not None:
            previous_eur_rate_for_calc = found_previous_day_rate_eur

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
            "rates_effective_date": scraped_effective_date_ymd # Usar la fecha scrapeada (que ya validamos que es la de hoy)
        }
        print(f"[{now_venezuela.strftime('%Y-%m-%d %H:%M:%S')}] fetch_and_update_bcv_rates_firestore: Tasas calculadas y actualizadas en memoria: {current_rates_in_memory}", flush=True)
        
        save_current_rates_to_firestore(current_rates_in_memory)

        # Actualizar o añadir al historial con la fecha de validez scrapeada
        history_date_to_use = scraped_effective_date_ymd
        today_history_doc_ref = db.collection('historical_rates').document(history_date_to_use)
        today_history_doc = today_history_doc_ref.get()

        if not today_history_doc.exists:
            # Si no existe, crear una nueva entrada con la fecha de validez
            new_history_entry = {
                "date": datetime.strptime(history_date_to_use, "%Y-%m-%d").strftime("%d de %B de %Y"),
                "date_ymd": history_date_to_use,
                "usd": usd_rate,
                "eur": eur_rate
            }
            save_historical_rate_to_firestore(new_history_entry)
            load_rates_from_firestore() # Recargar historial para tenerlo actualizado en memoria
        else:
            # Si ya existe, actualizar los valores de USD y EUR
            updated_history_entry = {
                "usd": usd_rate,
                "eur": eur_rate
            }
            today_history_doc_ref.update(updated_history_entry)
            print(f"[{now_venezuela.strftime('%Y-%m-%d %H:%M:%S')}] fetch_and_update_bcv_rates_firestore: Entrada de historial existente actualizada en Firestore para {history_date_to_use}.", flush=True)
            load_rates_from_firestore() # Recargar historial para tenerlo actualizado en memoria

        message = f"Tasas actualizadas y guardadas en Firestore (Fecha efectiva: {current_rates_in_memory['rates_effective_date']}): USD={usd_rate:.4f} ({usd_change_percent:.2f}%), EUR={eur_rate:.4f} ({eur_change_percent:.2f}%)"
        print(f"[{now_venezuela.strftime('%Y-%m-%d %H:%M:%S')}] {message}", flush=True)
        add_scrape_log_entry("SUCCESS", message, rates_effective_date=current_rates_in_memory['rates_effective_date'], usd_rate=usd_rate, eur_rate=eur_rate, doc_id=log_doc_id)

    except Exception as e:
        message = f"Ocurrió un error durante el scraping: {e}. Usando tasas cargadas de Firestore/predeterminadas."
        print(f"[{now_venezuela.strftime('%Y-%m-%d %H:%M:%S')}] {message}", flush=True)
        # Si falla el scraping, aún intentamos marcar la fecha efectiva como la fecha del sistema
        # para evitar reintentos infinitos si el problema es persistente en el BCV.
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
        add_scrape_log_entry("ERROR", message, rates_effective_date=current_rates_in_memory['rates_effective_date'], error_details=str(e), doc_id=log_doc_id)
    finally:
        print(f"[{now_venezuela.strftime('%Y-%m-%d %H:%M:%S')}] fetch_and_update_bcv_rates_firestore: --- FINALIZANDO EJECUCIÓN ---", flush=True)

@app.route('/api/bcv-rates', methods=['GET', 'HEAD'])
def get_current_bcv_rates():
    # Al recibir una solicitud, primero intenta actualizar las tasas
    fetch_and_update_bcv_rates_firestore()
    # Luego, carga las tasas (ya actualizadas o las últimas disponibles) de Firestore
    load_rates_from_firestore()
    return jsonify(current_rates_in_memory)

@app.route('/api/bcv-history', methods=['GET'])
def get_bcv_history():
    load_rates_from_firestore()
    return jsonify(historical_rates_in_memory)

@app.route('/api/scrape-logs', methods=['GET'])
def get_scrape_logs():
    if db is None:
        return jsonify({"error": "Firestore no está inicializado."}), 500
    try:
        # Obtener los últimos 20 logs de scraping, ordenados por timestamp descendente
        logs_ref = db.collection('scrape_logs')
        query = logs_ref.order_by('timestamp', direction=firestore.Query.DESCENDING).limit(20)
        docs = query.get()
        
        scrape_logs = []
        for doc in docs:
            log_data = doc.to_dict()
            # Convertir timestamp de ISO a formato legible si es necesario para el frontend
            if 'timestamp' in log_data and isinstance(log_data['timestamp'], str):
                try:
                    dt_object = datetime.fromisoformat(log_data['timestamp'])
                    log_data['timestamp_formatted'] = dt_object.strftime("%Y-%m-%d %H:%M:%S VET")
                except ValueError:
                    log_data['timestamp_formatted'] = log_data['timestamp'] # Fallback
            scrape_logs.append(log_data)
        
        return jsonify(scrape_logs)
    except Exception as e:
        print(f"[{datetime.now(VENEZUELA_TZ).strftime('%Y-%m-%d %H:%M:%S')}] Error al obtener logs de scraping: {e}", flush=True)
        return jsonify({"error": f"Error al obtener logs de scraping: {str(e)}"}), 500


scheduler = BackgroundScheduler(timezone="America/Caracas")

if __name__ == '__main__':
    print(f"[{datetime.now(VENEZUELA_TZ).strftime('%Y-%m-%d %H:%M:%S')}] --- INICIO DE LA APLICACIÓN FLASK ---", flush=True)
    # No se ejecuta scraping inicial al arranque del servicio, solo se carga lo que hay en Firestore
    load_rates_from_firestore()
    print(f"[{datetime.now(VENEZUELA_TZ).strftime('%Y-%m-%d %H:%M:%S')}] Tasas iniciales cargadas de Firestore.", flush=True)

    # Solo se programa la limpieza de datos históricos
    scheduler.add_job(cleanup_old_historical_rates, 'cron', hour=1, minute=0, day_of_week='mon-sun')
    print(f"[{datetime.now(VENEZUELA_TZ).strftime('%Y-%m-%d %H:%M:%S')}] Limpieza de historial programada diariamente a la 01:00 AM.", flush=True)

    scheduler.start()
    print(f"[{datetime.now(VENEZUELA_TZ).strftime('%Y-%m-%d %H:%M:%S')}] Scheduler iniciado (solo para limpieza de historial).", flush=True)

    port = int(os.environ.get('PORT', 5000))
    print(f"[{datetime.now(VENEZUELA_TZ).strftime('%Y-%m-%d %H:%M:%S')}] Iniciando servidor Flask en el puerto {port}...", flush=True)
    app.run(host='0.0.0.0', port=port, debug=False)
