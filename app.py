# app.py
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
import warnings # Importar el módulo warnings

# Importar las librerías de Firebase Admin SDK
import firebase_admin
from firebase_admin import credentials
from firebase_admin import firestore

# Suprimir todas las advertencias. Útil para entornos de producción donde las advertencias de librerías no son críticas.
warnings.filterwarnings("ignore")

app = Flask(__name__)
CORS(app)

# NOTA: Estas tasas predeterminadas son muy bajas. Si las ves en la app,
# es un fuerte indicio de que el scraping está fallando.
DEFAULT_USD_RATE = 0.01 
DEFAULT_EUR_RATE = 0.01
FIXED_UT_RATE = 43.00

BCV_URL = "https://www.bcv.org.ve/"

VENEZUELA_TZ = pytz.timezone("America/Caracas")

# --- Configuración e Inicialización de Firebase Firestore ---
# Las credenciales se cargarán desde una variable de entorno en Render.com
# Asegúrate de que FIREBASE_CREDENTIALS_JSON contenga el JSON de tu Service Account Key
try:
    firebase_credentials_json = os.environ.get('FIREBASE_CREDENTIALS_JSON')
    if firebase_credentials_json:
        cred = credentials.Certificate(json.loads(firebase_credentials_json))
        firebase_admin.initialize_app(cred)
        db = firestore.client()
        print(f"[{datetime.now(VENEZUELA_TZ).strftime('%Y-%m-%d %H:%M:%S')}] Firebase inicializado exitosamente.")
    else:
        print(f"[{datetime.now(VENEZUELA_TZ).strftime('%Y-%m-%d %H:%M:%S')}] Advertencia: FIREBASE_CREDENTIALS_JSON no está configurado. La aplicación no podrá usar Firestore.")
        db = None # Si no hay credenciales, db será None
except Exception as e:
    print(f"[{datetime.now(VENEZUELA_TZ).strftime('%Y-%m-%d %H:%M:%S')}] Error al inicializar Firebase: {e}")
    db = None

# Variables globales para almacenar las tasas actuales y el historial (en memoria)
# Estas se sincronizarán con Firestore
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
    """Carga las tasas actuales y el historial desde Firestore."""
    global current_rates_in_memory, historical_rates_in_memory
    if db is None:
        print(f"[{datetime.now(VENEZUELA_TZ).strftime('%Y-%m-%d %H:%M:%S')}] Firestore no está inicializado. Usando datos predeterminados en memoria.")
        return

    print(f"[{datetime.now(VENEZUELA_TZ).strftime('%Y-%m-%d %H:%M:%S')}] Intentando cargar tasas desde Firestore...")
    try:
        # Cargar tasas actuales (documento 'latest_rates' en la colección 'current_rates')
        current_rates_doc_ref = db.collection('current_rates').document('latest_rates')
        current_rates_doc = current_rates_doc_ref.get()
        if current_rates_doc.exists:
            loaded_data = current_rates_doc.to_dict()
            # Limpiar el campo 'placeholder' si existe
            if 'placeholder' in loaded_data:
                del loaded_data['placeholder']
            current_rates_in_memory.update(loaded_data)
            print(f"[{datetime.now(VENEZUELA_TZ).strftime('%Y-%m-%d %H:%M:%S')}] Tasas actuales cargadas de Firestore: {current_rates_in_memory}")
        else:
            print(f"[{datetime.now(VENEZUELA_TZ).strftime('%Y-%m-%d %H:%M:%S')}] Documento 'latest_rates' no encontrado en Firestore. Usando valores predeterminados.")
            # Guardar los valores predeterminados en Firestore si no existen
            save_current_rates_to_firestore(current_rates_in_memory)

        # Cargar historial (últimos 15 días de la colección 'historical_rates')
        historical_docs = db.collection('historical_rates') \
                            .order_by('date_ymd', direction=firestore.Query.DESCENDING) \
                            .limit(15) \
                            .get()
        historical_rates_in_memory = []
        for doc in historical_docs:
            loaded_history_entry = doc.to_dict()
            if 'placeholder' in loaded_history_entry: # Limpiar también en el historial
                del loaded_history_entry['placeholder']
            historical_rates_in_memory.append(loaded_history_entry)
        
        # Asegurarse de que el historial esté ordenado por fecha de más reciente a más antiguo
        historical_rates_in_memory.sort(key=lambda x: datetime.strptime(x['date_ymd'], "%Y-%m-%d"), reverse=True)

        if not historical_rates_in_memory:
            print(f"[{datetime.now(VENEZUELA_TZ).strftime('%Y-%m-%d %H:%M:%S')}] No hay historial en Firestore. Generando datos simulados para el historial.")
            today = datetime.now(VENEZUELA_TZ)
            for i in range(15):
                date = today - timedelta(days=i)
                sim_usd = round(DEFAULT_USD_RATE + (random.random() - 0.5) * 0.5, 2)
                sim_eur = round(DEFAULT_EUR_RATE + (random.random() - 0.5) * 0.6, 2)
                historical_rates_in_memory.append({
                    "date": date.strftime("%d de %B de %Y"),
                    "date_ymd": date.strftime("%Y-%m-%d"), # Clave para ordenar
                    "usd": sim_usd,
                    "eur": sim_eur
                })
            # Guardar el historial simulado en Firestore
            for entry in historical_rates_in_memory:
                save_historical_rate_to_firestore(entry)
            print(f"[{datetime.now(VENEZUELA_TZ).strftime('%Y-%m-%d %H:%M:%S')}] Historial simulado guardado en Firestore.")
        else:
            print(f"[{datetime.now(VENEZUELA_TZ).strftime('%Y-%m-%d %H:%M:%S')}] Historial cargado de Firestore: {len(historical_rates_in_memory)} entradas.")

    except Exception as e:
        print(f"[{datetime.now(VENEZUELA_TZ).strftime('%Y-%m-%d %H:%M:%S')}] Error al cargar datos de Firestore: {e}. Usando datos en memoria/predeterminados.")

def save_current_rates_to_firestore(data):
    """Guarda las tasas actuales en Firestore."""
    if db is None: return
    try:
        doc_ref = db.collection('current_rates').document('latest_rates')
        doc_ref.set(data)
        print(f"[{datetime.now(VENEZUELA_TZ).strftime('%Y-%m-%d %H:%M:%S')}] Tasas actuales guardadas en Firestore.")
    except Exception as e:
        print(f"[{datetime.now(VENEZUELA_TZ).strftime('%Y-%m-%d %H:%M:%S')}] Error al guardar tasas actuales en Firestore: {e}")

def save_historical_rate_to_firestore(data):
    """Guarda una entrada de historial en Firestore."""
    if db is None: return
    try:
        # Usar la fecha YMD como ID del documento para evitar duplicados por día
        doc_ref = db.collection('historical_rates').document(data['date_ymd'])
        doc_ref.set(data)
        print(f"[{datetime.now(VENEZUELA_TZ).strftime('%Y-%m-%d %H:%M:%S')}] Entrada de historial guardada/actualizada en Firestore para {data['date_ymd']}.")
    except Exception as e:
        print(f"[{datetime.now(VENEZUELA_TZ).strftime('%Y-%m-%d %H:%M:%S')}] Error al guardar historial en Firestore: {e}")

def cleanup_old_historical_rates():
    """Elimina entradas de historial antiguas de Firestore (más de 15 días)."""
    if db is None: return
    try:
        # Obtener la fecha de hace 15 días
        limit_date = datetime.now(VENEZUELA_TZ) - timedelta(days=15)
        limit_date_str = limit_date.strftime("%Y-%m-%d")

        # Consultar documentos más antiguos que el límite de 15 días
        old_docs = db.collection('historical_rates') \
                     .where('date_ymd', '<', limit_date_str) \
                     .get()
        
        deleted_count = 0
        for doc in old_docs:
            doc.reference.delete()
            deleted_count += 1
        
        if deleted_count > 0:
            print(f"[{datetime.now(VENEZUELA_TZ).strftime('%Y-%m-%d %H:%M:%S')}] Limpieza de historial: {deleted_count} documentos antiguos eliminados de Firestore.")
    except Exception as e:
        print(f"[{datetime.now(VENEZUELA_TZ).strftime('%Y-%m-%d %H:%M:%S')}] Error al limpiar historial en Firestore: {e}")

# Función para realizar el self-ping
def self_ping():
    """
    Realiza un ping a la propia aplicación para mantenerla activa en servicios como Render Free Tier.
    Esto evita que la aplicación se "duerma" por inactividad.
    """
    app_external_hostname = os.environ.get('RENDER_EXTERNAL_HOSTNAME')
    if app_external_hostname:
        ping_url = f"https://{app_external_hostname}/api/bcv-rates" # Usar la variable de entorno para la URL
        try:
            # Usar un método HEAD para el ping para reducir el consumo de recursos
            response = requests.head(ping_url, timeout=5)
            if response.status_code == 200:
                print(f"[{datetime.now(VENEZUELA_TZ).strftime('%Y-%m-%d %H:%M:%S')}] Self-ping exitoso a {ping_url}. Estado: {response.status_code}")
            else:
                print(f"[{datetime.now(VENEZUELA_TZ).strftime('%Y-%m-%d %H:%M:%S')}] Self-ping fallido a {ping_url}. Estado: {response.status_code}")
        except requests.exceptions.RequestException as e:
            print(f"[{datetime.now(VENEZUELA_TZ).strftime('%Y-%m-%d %H:%M:%S')}] Error en self-ping a {ping_url}: {e}")
    else:
        print(f"[{datetime.now(VENEZUELA_TZ).strftime('%Y-%m-%d %H:%M:%S')}] Advertencia: La variable de entorno 'RENDER_EXTERNAL_HOSTNAME' no está configurada. No se puede realizar el self-ping.")
        print(f"[{datetime.now(VENEZUELA_TZ).strftime('%Y-%m-%d %H:%M:%S')}] Esto podría significar que tu app se duerma en Render Free Tier.")


def fetch_and_update_bcv_rates_firestore():
    """
    Intenta obtener las tasas de USD y EUR del BCV mediante web scraping,
    las actualiza, calcula el cambio porcentual y guarda los datos en Firestore.
    
    Esta función ahora solo realiza el scraping si la fecha efectiva actual
    no es la fecha de hoy, asegurando que las tasas se fijen una vez al día.
    """
    global current_rates_in_memory, historical_rates_in_memory
    
    now_venezuela = datetime.now(VENEZUELA_TZ)
    today_date_str_ymd = now_venezuela.strftime("%Y-%m-%d")
    today_date_str_human = now_venezuela.strftime("%d de %B de %Y")

    # Horarios específicos de scraping temprano en la mañana
    early_morning_scrape_minutes = [1, 2, 4, 6, 8, 10]
    is_scheduled_early_morning_call = (
        now_venezuela.hour == 0 and now_venezuela.minute in early_morning_scrape_minutes
    )

    # Cargar las tasas actuales desde Firestore antes de decidir si raspar
    load_rates_from_firestore()

    # REVISIÓN DE LA LÓGICA DE SALTO DE SCRAPING:
    # Se saltará el scraping si:
    # 1. La fecha efectiva de las tasas en memoria ya es la de hoy.
    # 2. Y (las tasas de USD O EUR NO son los valores predeterminados). Esto significa que ya tenemos tasas reales para hoy.
    # 3. Y NO es una de las llamadas programadas de la madrugada (estas deben forzar siempre un intento de scraping).
    if (current_rates_in_memory.get("rates_effective_date") == today_date_str_ymd and
        (current_rates_in_memory.get("usd") != DEFAULT_USD_RATE or
         current_rates_in_memory.get("eur") != DEFAULT_EUR_RATE) and
        not is_scheduled_early_morning_call):
        print(f"[{now_venezuela.strftime('%Y-%m-%d %H:%M:%S')}] Tasas del BCV para hoy ({today_date_str_ymd}) ya están fijadas en Firestore (no son predeterminadas) y no es un horario de scraping programado. No se realizará scraping nuevamente.")
        return

    print(f"[{now_venezuela.strftime('%Y-%m-%d %H:%M:%S')}] Intentando actualizar tasas del BCV (Scraping forzado por nueva fecha, reinicio o horario programado)...")

    previous_usd_rate_for_calc = current_rates_in_memory.get("usd", DEFAULT_USD_RATE)
    previous_eur_rate_for_calc = current_rates_in_memory.get("eur", DEFAULT_EUR_RATE)

    # Buscar la entrada más reciente del historial que NO sea la de hoy
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
        print(f"[{now_venezuela.strftime('%Y-%m-%d %H:%M:%S')}] Realizando solicitud GET a {BCV_URL}...")
        response = requests.get(BCV_URL, timeout=15, verify=False)
        response.raise_for_status()
        print(f"[{now_venezuela.strftime('%Y-%m-%d %H:%M:%S')}] Solicitud GET exitosa. Status: {response.status_code}")

        soup = BeautifulSoup(response.text, 'lxml')
        print(f"[{now_venezuela.strftime('%Y-%m-%d %H:%M:%S')}] BeautifulSoup parseado.")

        usd_rate = None
        eur_rate = None

        # --- INICIO DE DEPURACIÓN DETALLADA DEL SCRAPING ---
        usd_container = soup.find('div', id='dolar')
        print(f"[{now_venezuela.strftime('%Y-%m-%d %H:%M:%S')}] USD container (id='dolar') encontrado: {usd_container is not None}")
        if usd_container:
            centrado_div_usd = usd_container.find('div', class_='centrado')
            print(f"[{now_venezuela.strftime('%Y-%m-%d %H:%M:%S')}] USD div con clase 'centrado' encontrado: {centrado_div_usd is not None}")
            if centrado_div_usd:
                usd_strong_tag = centrado_div_usd.find('strong')
                print(f"[{now_venezuela.strftime('%Y-%m-%d %H:%M:%S')}] USD strong tag encontrado: {usd_strong_tag is not None}")
                if usd_strong_tag:
                    match = re.search(r'[\d,\.]+', usd_strong_tag.text)
                    if match:
                        usd_rate = float(match.group(0).replace(',', '.').strip())
                        print(f"[{now_venezuela.strftime('%Y-%m-%d %H:%M:%S')}] Tasa USD extraída: {usd_rate}")
                    else:
                        print(f"[{now_venezuela.strftime('%Y-%m-%d %H:%M:%S')}] No se encontró coincidencia para la tasa USD en el texto del strong tag: '{usd_strong_tag.text}'")
                else:
                    print(f"[{now_venezuela.strftime('%Y-%m-%d %H:%M:%S')}] No se encontró strong tag dentro del div 'centrado' para USD.")
            else:
                print(f"[{now_venezuela.strftime('%Y-%m-%d %H:%M:%S')}] No se encontró div 'centrado' dentro del contenedor 'dolar' para USD.")
        else:
            print(f"[{now_venezuela.strftime('%Y-%m-%d %H:%M:%S')}] Contenedor USD (id='dolar') no encontrado en la página.")

        eur_container = soup.find('div', id='euro')
        print(f"[{now_venezuela.strftime('%Y-%m-%d %H:%M:%S')}] EUR container (id='euro') encontrado: {eur_container is not None}")
        if eur_container:
            centrado_div_eur = eur_container.find('div', class_='centrado')
            print(f"[{now_venezuela.strftime('%Y-%m-%d %H:%M:%S')}] EUR div con clase 'centrado' encontrado: {centrado_div_eur is not None}")
            if centrado_div_eur:
                eur_strong_tag = centrado_div_eur.find('strong')
                print(f"[{now_venezuela.strftime('%Y-%m-%d %H:%M:%S')}] EUR strong tag encontrado: {eur_strong_tag is not None}")
                if eur_strong_tag:
                    match = re.search(r'[\d,\.]+', eur_strong_tag.text)
                    if match:
                        eur_rate = float(match.group(0).replace(',', '.').strip())
                        print(f"[{now_venezuela.strftime('%Y-%m-%d %H:%M:%S')}] Tasa EUR extraída: {eur_rate}")
                    else:
                        print(f"[{now_venezuela.strftime('%Y-%m-%d %H:%M:%S')}] No se encontró coincidencia para la tasa EUR en el texto del strong tag: '{eur_strong_tag.text}'")
                else:
                    print(f"[{now_venezuela.strftime('%Y-%m-%d %H:%M:%S')}] No se encontró strong tag dentro del div 'centrado' para EUR.")
            else:
                print(f"[{now_venezuela.strftime('%Y-%m-%d %H:%M:%S')}] No se encontró div 'centrado' dentro del contenedor 'euro' para EUR.")
        else:
            print(f"[{now_venezuela.strftime('%Y-%m-%d %H:%M:%S')}] Contenedor EUR (id='euro') no encontrado en la página.")
        # --- FIN DE DEPURACIÓN DETALLADA DEL SCRAPING ---

        if usd_rate is None or eur_rate is None:
            # Si el scraping falla, usamos las tasas que ya teníamos cargadas (de Firestore o predeterminadas)
            print(f"[{now_venezuela.strftime('%Y-%m-%d %H:%M:%S')}] Advertencia: No se pudieron extraer ambas tasas (USD y/o EUR) del BCV. Usando las tasas previamente cargadas/predeterminadas para el cálculo de porcentajes y actualización.")
            usd_rate = current_rates_in_memory.get("usd", DEFAULT_USD_RATE)
            eur_rate = current_rates_in_memory.get("eur", DEFAULT_EUR_RATE)
            # No se lanza ValueError aquí para permitir que la aplicación continúe funcionando con datos de respaldo.

        usd_change_percent = 0.0
        eur_change_percent = 0.0

        # Asegurarse de que previous_usd_rate_for_calc no sea cero para evitar división por cero
        if previous_usd_rate_for_calc != 0 and usd_rate is not None:
            usd_change_percent = ((usd_rate - previous_usd_rate_for_calc) / previous_usd_rate_for_calc) * 100
        else:
            current_rates_in_memory["usd_change_percent"] = 0.0 # Asegura que el porcentaje sea 0 si la tasa anterior es 0
        if previous_eur_rate_for_calc != 0 and eur_rate is not None:
            eur_change_percent = ((eur_rate - previous_eur_rate_for_calc) / previous_eur_rate_for_calc) * 100
        else:
            current_rates_in_memory["eur_change_percent"] = 0.0 # Asegura que el porcentaje sea 0 si la tasa anterior es 0

        # Actualizar current_rates_in_memory con las nuevas tasas (extraídas o de respaldo)
        # y los porcentajes calculados.
        current_rates_in_memory.update({
            "usd": usd_rate,
            "eur": eur_rate,
            "ut": FIXED_UT_RATE,
            "last_updated": now_venezuela.strftime("%Y-%m-%d %H:%M:%S"),
            "usd_change_percent": round(usd_change_percent, 2),
            "eur_change_percent": round(eur_change_percent, 2),
            "rates_effective_date": today_date_str_ymd
        })
        print(f"[{now_venezuela.strftime('%Y-%m-%d %H:%M:%S')}] Tasas finales para actualización: {current_rates_in_memory}")
        
        # Guardar las tasas actuales en Firestore (siempre, incluso si son de respaldo)
        save_current_rates_to_firestore(current_rates_in_memory)

        # Actualizar el historial en Firestore solo si el scraping fue exitoso para USD y EUR
        if usd_rate is not None and eur_rate is not None:
            today_history_doc_ref = db.collection('historical_rates').document(today_date_str_ymd)
            today_history_doc = today_history_doc_ref.get()

            if not today_history_doc.exists:
                new_history_entry = {
                    "date": today_date_str_human,
                    "date_ymd": today_date_str_ymd,
                    "usd": usd_rate,
                    "eur": eur_rate
                }
                save_historical_rate_to_firestore(new_history_entry)
            else:
                updated_history_entry = {
                    "usd": usd_rate,
                    "eur": eur_rate
                }
                today_history_doc_ref.update(updated_history_entry)
                print(f"[{now_venezuela.strftime('%Y-%m-%d %H:%M:%S')}] Entrada de historial existente actualizada en Firestore para {today_date_str_ymd}.")
            
            # Volver a cargar el historial en memoria para reflejar el cambio
            load_rates_from_firestore()

        print(f"[{now_venezuela.strftime('%Y-%m-%d %H:%M:%S')}] Tasas procesadas y guardadas en Firestore: USD={current_rates_in_memory['usd']:.4f} ({current_rates_in_memory['usd_change_percent']:.2f}%), EUR={current_rates_in_memory['eur']:.4f} ({current_rates_in_memory['eur_change_percent']:.2f}%)")

    except requests.exceptions.Timeout:
        print(f"[{now_venezuela.strftime('%Y-%m-%d %H:%M:%S')}] Error: Tiempo de espera agotado al conectar con el BCV. Usando tasas cargadas de Firestore/predeterminadas.")
        # Recalcular porcentajes con los valores actuales en memoria y los del día anterior si hay un error
        if current_rates_in_memory["usd"] is not None and previous_usd_rate_for_calc != 0:
            current_rates_in_memory["usd_change_percent"] = ((current_rates_in_memory["usd"] - previous_usd_rate_for_calc) / previous_usd_rate_for_calc) * 100
        else:
            current_rates_in_memory["usd_change_percent"] = 0.0
        if current_rates_in_memory["eur"] is not None and previous_eur_rate_for_calc != 0:
            current_rates_in_memory["eur_change_percent"] = ((current_rates_in_memory["eur"] - previous_eur_rate_for_calc) / previous_eur_rate_for_calc) * 100
        else:
            current_rates_in_memory["eur_change_percent"] = 0.0
        save_current_rates_to_firestore(current_rates_in_memory) # Guardar en Firestore para persistencia
    except requests.exceptions.RequestException as e:
        print(f"[{now_venezuela.strftime('%Y-%m-%d %H:%M:%S')}] Error de red o HTTP al conectar con el BCV: {e}. Usando tasas cargadas de Firestore/predeterminadas.")
        if current_rates_in_memory["usd"] is not None and previous_usd_rate_for_calc != 0:
            current_rates_in_memory["usd_change_percent"] = ((current_rates_in_memory["usd"] - previous_usd_rate_for_calc) / previous_usd_rate_for_calc) * 100
        else:
            current_rates_in_memory["usd_change_percent"] = 0.0
        if current_rates_in_memory["eur"] is not None and previous_eur_rate_for_calc != 0:
            current_rates_in_memory["eur_change_percent"] = ((current_rates_in_memory["eur"] - previous_eur_rate_for_calc) / previous_eur_rate_for_calc) * 100
        else:
            current_rates_in_memory["eur_change_percent"] = 0.0
        save_current_rates_to_firestore(current_rates_in_memory)
    except AttributeError:
        print(f"[{now_venezuela.strftime('%Y-%m-%d %H:%M:%S')}] Error de scraping (AttributeError): No se encontraron los elementos HTML esperados o su estructura cambió. Usando tasas cargadas de Firestore/predeterminadas.")
        if current_rates_in_memory["usd"] is not None and previous_usd_rate_for_calc != 0:
            current_rates_in_memory["usd_change_percent"] = ((current_rates_in_memory["usd"] - previous_usd_rate_for_calc) / previous_usd_rate_for_calc) * 100
        else:
            current_rates_in_memory["usd_change_percent"] = 0.0
        if current_rates_in_memory["eur"] is not None and previous_eur_rate_for_calc != 0:
            current_rates_in_memory["eur_change_percent"] = ((current_rates_in_memory["eur"] - previous_eur_rate_for_calc) / previous_eur_rate_for_calc) * 100
        else:
            current_rates_in_memory["eur_change_percent"] = 0.0
        save_current_rates_to_firestore(current_rates_in_memory)
    except ValueError as e:
        print(f"[{now_venezuela.strftime('%Y-%m-%d %H:%M:%S')}] Error de procesamiento de datos (ValueError): {e}. Usando tasas cargadas de Firestore/predeterminadas.")
        if current_rates_in_memory["usd"] is not None and previous_usd_rate_for_calc != 0:
            current_rates_in_memory["usd_change_percent"] = ((current_rates_in_memory["usd"] - previous_usd_rate_for_calc) / previous_usd_rate_for_calc) * 100
        else:
            current_rates_in_memory["usd_change_percent"] = 0.0
        if current_rates_in_memory["eur"] is not None and previous_eur_rate_for_calc != 0:
            current_rates_in_memory["eur_change_percent"] = ((current_rates_in_memory["eur"] - previous_eur_rate_for_calc) / previous_eur_rate_for_calc) * 100
        else:
            current_rates_in_memory["eur_change_percent"] = 0.0
        save_current_rates_to_firestore(current_rates_in_memory)
    except Exception as e:
        print(f"[{now_venezuela.strftime('%Y-%m-%d %H:%M:%S')}] Ocurrió un error inesperado durante el scraping: {e}. Usando tasas cargadas de Firestore/predeterminadas.")
        if current_rates_in_memory["usd"] is not None and previous_usd_rate_for_calc != 0:
            current_rates_in_memory["usd_change_percent"] = ((current_rates_in_memory["usd"] - previous_usd_rate_for_calc) / previous_usd_rate_for_calc) * 100
        else:
            current_rates_in_memory["usd_change_percent"] = 0.0
        if current_rates_in_memory["eur"] is not None and previous_eur_rate_for_calc != 0:
            current_rates_in_memory["eur_change_percent"] = ((current_rates_in_memory["eur"] - previous_eur_rate_for_calc) / previous_eur_rate_for_calc) * 100
        else:
            current_rates_in_memory["eur_change_percent"] = 0.0
        save_current_rates_to_firestore(current_rates_in_memory)

# Configuración del scheduler para tareas en segundo plano
scheduler = BackgroundScheduler(timezone="America/Caracas")

# Mover la inicialización del scheduler y la llamada inicial fuera del if __name__ == '__main__':
# Esto asegura que se ejecuten cuando Gunicorn carga la aplicación.
print(f"[{datetime.now(VENEZUELA_TZ).strftime('%Y-%m-%d %H:%M:%S')}] Iniciando la aplicación. Ejecutando scraping inicial...")
fetch_and_update_bcv_rates_firestore()
print(f"[{datetime.now(VENEZUELA_TZ).strftime('%Y-%m-%d %H:%M:%S')}] Scraping inicial completado.")

# Programar el scraping para que se ejecute diariamente a la 00:01, 00:02, 00:04, 00:06, 00:08, 00:10 (medianoche)
scheduler.add_job(fetch_and_update_bcv_rates_firestore, 'cron', hour=0, minute=1, day_of_week='mon-fri')
scheduler.add_job(fetch_and_update_bcv_rates_firestore, 'cron', hour=0, minute=2, day_of_week='mon-fri')
scheduler.add_job(fetch_and_update_bcv_rates_firestore, 'cron', hour=0, minute=4, day_of_week='mon-fri')
scheduler.add_job(fetch_and_update_bcv_rates_firestore, 'cron', hour=0, minute=6, day_of_week='mon-fri')
scheduler.add_job(fetch_and_update_bcv_rates_firestore, 'cron', hour=0, minute=8, day_of_week='mon-fri')
scheduler.add_job(fetch_and_update_bcv_rates_firestore, 'cron', hour=0, minute=10, day_of_week='mon-fri')
print(f"[{datetime.now(VENEZUELA_TZ).strftime('%Y-%m-%d %H:%M:%S')}] Scraping diario programado para 00:01, 00:02, 00:04, 00:06, 00:08, 00:10 (L-V).")

# Programar la limpieza de datos históricos antiguos (ej. una vez al día)
scheduler.add_job(cleanup_old_historical_rates, 'cron', hour=1, minute=0, day_of_week='mon-sun')
print(f"[{datetime.now(VENEZUELA_TZ).strftime('%Y-%m-%d %H:%M:%S')}] Limpieza de historial programada diariamente a la 01:00 AM.")

# Programar el self-ping para que se ejecute cada 5 minutos (300 segundos)
scheduler.add_job(self_ping, 'interval', seconds=300)
print(f"[{datetime.now(VENEZUELA_TZ).strftime('%Y-%m-%d %H:%M:%S')}] Self-ping programado cada 5 minutos.")

# Iniciar el scheduler
scheduler.start()
print(f"[{datetime.now(VENEZUELA_TZ).strftime('%Y-%m-%d %H:%M:%S')}] Scheduler iniciado.")


@app.route('/api/bcv-rates', methods=['GET', 'HEAD']) # Se añadió 'HEAD' aquí
def get_current_bcv_rates():
    """Endpoint para obtener las tasas actuales del BCV desde Firestore."""
    load_rates_from_firestore() # Asegurarse de que las tasas en memoria estén actualizadas desde Firestore
    return jsonify(current_rates_in_memory)

@app.route('/api/bcv-history', methods=['GET'])
def get_bcv_history():
    """Endpoint para obtener el historial de tasas del BCV desde Firestore."""
    load_rates_from_firestore() # Asegurarse de que el historial en memoria esté actualizado desde Firestore
    return jsonify(historical_rates_in_memory)

if __name__ == '__main__':
    # Obtener el puerto de las variables de entorno (para entornos de despliegue como Render.com)
    port = int(os.environ.get('PORT', 5000))
    # Iniciar la aplicación Flask
    print(f"[{datetime.now(VENEZUELA_TZ).strftime('%Y-%m-%d %H:%M:%S')}] Iniciando servidor Flask en el puerto {port}...")
    app.run(host='0.0.0.0', port=port, debug=False)

