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

# Suprimir todas las advertencias. Útil para entornos de producción donde las advertencias de librerías no son críticas.
warnings.filterwarnings("ignore")

app = Flask(__name__)
CORS(app)

DEFAULT_USD_RATE = 00.01
DEFAULT_EUR_RATE = 00.01
FIXED_UT_RATE = 43.00

BCV_URL = "https://www.bcv.org.ve/"

CURRENT_RATES_FILE = 'current_rates.json'
HISTORICAL_RATES_FILE = 'historical_rates.json'

VENEZUELA_TZ = pytz.timezone("America/Caracas")

def load_data(file_path, default_data):
    """
    Carga datos desde un archivo JSON.
    Si el archivo no existe o está corrupto/vacío, devuelve los datos por defecto.
    """
    if os.path.exists(file_path):
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except json.JSONDecodeError:
            print(f"[{datetime.now(VENEZUELA_TZ).strftime('%Y-%m-%d %H:%M:%S')}] Error: Archivo {file_path} corrupto o vacío. Usando datos predeterminados.")
            return default_data
    return default_data
    return default_data

def save_data(file_path, data):
    """Guarda datos en un archivo JSON."""
    with open(file_path, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=4, ensure_ascii=False)

# Cargar las tasas actuales y el historial al iniciar la aplicación
current_rates = load_data(CURRENT_RATES_FILE, {
    "usd": DEFAULT_USD_RATE,
    "eur": DEFAULT_EUR_RATE,
    "ut": FIXED_UT_RATE,
    "last_updated": datetime.now(VENEZUELA_TZ).strftime("%Y-%m-%d %H:%M:%S") + " (predeterminado)",
    "usd_change_percent": 0.0,
    "eur_change_percent": 0.0
})
historical_rates_data = load_data(HISTORICAL_RATES_FILE, [])

# Si no hay historial, generar datos simulados para 15 días
if not historical_rates_data:
    today = datetime.now(VENEZUELA_TZ)
    for i in range(15):
        date = today - timedelta(days=i)
        sim_usd = round(DEFAULT_USD_RATE + (random.random() - 0.5) * 0.5, 2)
        sim_eur = round(DEFAULT_EUR_RATE + (random.random() - 0.5) * 0.6, 2)
        historical_rates_data.append({
            "date": date.strftime("%d de %B de %Y"),
            "usd": sim_usd,
            "eur": sim_eur
        })
    historical_rates_data.sort(key=lambda x: datetime.strptime(x['date'], "%d de %B de %Y"), reverse=True)
    save_data(HISTORICAL_RATES_FILE, historical_rates_data)

def fetch_and_update_bcv_rates():
    """
    Intenta obtener las tasas de USD y EUR del BCV mediante web scraping,
    las actualiza, calcula el cambio porcentual y guarda los datos.
    """
    global current_rates, historical_rates_data
    
    now_venezuela = datetime.now(VENEZUELA_TZ)
    today_date_str_ymd = now_venezuela.strftime("%Y-%m-%d")

    # Verificar si las tasas para hoy ya fueron actualizadas después de la medianoche
    if "last_updated" in current_rates:
        stored_last_updated_dt_str = current_rates["last_updated"].split(" (")[0]
        stored_date_str_ymd = stored_last_updated_dt_str.split(" ")[0]

        # Si la fecha de la última actualización es hoy y ya hemos pasado la medianoche (00:01),
        # no es necesario volver a hacer scraping.
        if stored_date_str_ymd == today_date_str_ymd and \
           (now_venezuela.hour > 0 or (now_venezuela.hour == 0 and now_venezuela.minute > 1)):
            print(f"[{now_venezuela.strftime('%Y-%m-%d %H:%M:%S')}] Tasas del BCV para hoy ({today_date_str_ymd}) ya están cargadas. No se realizará scraping hasta mañana.")
            return

    print(f"[{now_venezuela.strftime('%Y-%m-%d %H:%M:%S')}] Intentando actualizar tasas del BCV...")

    previous_usd_rate_for_calc = current_rates.get("usd", DEFAULT_USD_RATE)
    previous_eur_rate_for_calc = current_rates.get("eur", DEFAULT_EUR_RATE)

    if len(historical_rates_data) >= 2:
        previous_usd_rate_for_calc = historical_rates_data[1]["usd"]
        previous_eur_rate_for_calc = historical_rates_data[1]["eur"]
    elif len(historical_rates_data) == 1:
        previous_usd_rate_for_calc = historical_rates_data[0]["usd"]
        previous_eur_rate_for_calc = historical_rates_data[0]["eur"]

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
            raise ValueError("No se pudieron encontrar los elementos HTML esperados para USD o EUR. La estructura de la página del BCV pudo haber cambiado.")

        usd_change_percent = 0.0
        eur_change_percent = 0.0

        if previous_usd_rate_for_calc != 0:
            usd_change_percent = ((usd_rate - previous_usd_rate_for_calc) / previous_usd_rate_for_calc) * 100
        if previous_eur_rate_for_calc != 0:
            eur_change_percent = ((eur_rate - previous_eur_rate_for_calc) / previous_eur_rate_for_calc) * 100

        current_rates = {
            "usd": usd_rate,
            "eur": eur_rate,
            "ut": FIXED_UT_RATE,
            "last_updated": now_venezuela.strftime("%Y-%m-%d %H:%M:%S"),
            "usd_change_percent": usd_change_percent,
            "eur_change_percent": eur_change_percent
        }
        save_data(CURRENT_RATES_FILE, current_rates)

        today_date_str_for_history = now_venezuela.strftime("%d de %B de %Y")
        
        if not historical_rates_data or \
           datetime.strptime(historical_rates_data[0]["date"], "%d de %B de %Y").date() != now_venezuela.date():
            historical_rates_data.insert(0, {
                "date": today_date_str_for_history,
                "usd": usd_rate,
                "eur": eur_rate
            })
            historical_rates_data = historical_rates_data[:15]
        else:
            historical_rates_data[0]["usd"] = usd_rate
            historical_rates_data[0]["eur"] = eur_rate
        save_data(HISTORICAL_RATES_FILE, historical_rates_data)

        print(f"[{now_venezuela.strftime('%Y-%m-%d %H:%M:%S')}] Tasas actualizadas y guardadas: USD={usd_rate:.4f} ({usd_change_percent:.2f}%), EUR={eur_rate:.4f} ({eur_change_percent:.2f}%)")

    except requests.exceptions.Timeout:
        print(f"[{now_venezuela.strftime('%Y-%m-%d %H:%M:%S')}] Error: Tiempo de espera agotado al conectar con el BCV. Usando tasas guardadas/predeterminadas.")
        if len(historical_rates_data) >= 2:
            current_rates["usd_change_percent"] = ((current_rates["usd"] - previous_usd_rate_for_calc) / previous_usd_rate_for_calc) * 100 if previous_usd_rate_for_calc != 0 else 0.0
            current_rates["eur_change_percent"] = ((current_rates["eur"] - previous_eur_rate_for_calc) / previous_eur_rate_for_calc) * 100 if previous_eur_rate_for_calc != 0 else 0.0
        else:
            current_rates["usd_change_percent"] = 0.0
            current_rates["eur_change_percent"] = 0.0
        save_data(CURRENT_RATES_FILE, current_rates)
    except requests.exceptions.RequestException as e:
        print(f"[{now_venezuela.strftime('%Y-%m-%d %H:%M:%S')}] Error de red o HTTP al conectar con el BCV: {e}. Usando tasas guardadas/predeterminadas.")
        if len(historical_rates_data) >= 2:
            current_rates["usd_change_percent"] = ((current_rates["usd"] - previous_usd_rate_for_calc) / previous_usd_rate_for_calc) * 100 if previous_usd_rate_for_calc != 0 else 0.0
            current_rates["eur_change_percent"] = ((current_rates["eur"] - previous_eur_rate_for_calc) / previous_eur_rate_for_calc) * 100 if previous_eur_rate_for_calc != 0 else 0.0
        else:
            current_rates["usd_change_percent"] = 0.0
            current_rates["eur_change_percent"] = 0.0
        save_data(CURRENT_RATES_FILE, current_rates)
    except AttributeError:
        print(f"[{now_venezuela.strftime('%Y-%m-%d %H:%M:%S')}] Error de scraping: No se encontraron los elementos HTML esperados. La estructura de la página del BCV pudo haber cambiado. Usando tasas guardadas/predeterminadas.")
        if len(historical_rates_data) >= 2:
            current_rates["usd_change_percent"] = ((current_rates["usd"] - previous_usd_rate_for_calc) / previous_usd_rate_for_calc) * 100 if previous_usd_rate_for_calc != 0 else 0.0
            current_rates["eur_change_percent"] = ((current_rates["eur"] - previous_eur_rate_for_calc) / previous_eur_rate_for_calc) * 100 if previous_eur_rate_for_calc != 0 else 0.0
        else:
            current_rates["usd_change_percent"] = 0.0
            current_rates["eur_change_percent"] = 0.0
        save_data(CURRENT_RATES_FILE, current_rates)
    except ValueError as e:
        print(f"[{now_venezuela.strftime('%Y-%m-%d %H:%M:%S')}] Error de procesamiento de datos: {e}. Usando tasas guardadas/predeterminadas.")
        if len(historical_rates_data) >= 2:
            current_rates["usd_change_percent"] = ((current_rates["usd"] - previous_usd_rate_for_calc) / previous_usd_rate_for_calc) * 100 if previous_usd_rate_for_calc != 0 else 0.0
            current_rates["eur_change_percent"] = ((current_rates["eur"] - previous_eur_rate_for_calc) / previous_eur_rate_for_calc) * 100 if previous_eur_rate_for_calc != 0 else 0.0
        else:
            current_rates["usd_change_percent"] = 0.0
            current_rates["eur_change_percent"] = 0.0
        save_data(CURRENT_RATES_FILE, current_rates)
    except Exception as e:
        print(f"[{now_venezuela.strftime('%Y-%m-%d %H:%M:%S')}] Ocurrió un error inesperado durante el scraping: {e}. Usando tasas guardadas/predeterminadas.")
        if len(historical_rates_data) >= 2:
            current_rates["usd_change_percent"] = ((current_rates["usd"] - previous_usd_rate_for_calc) / previous_usd_rate_for_calc) * 100 if previous_usd_rate_for_calc != 0 else 0.0
            current_rates["eur_change_percent"] = ((current_rates["eur"] - previous_eur_rate_for_calc) / previous_eur_rate_for_calc) * 100 if previous_eur_rate_for_calc != 0 else 0.0
        else:
            current_rates["usd_change_percent"] = 0.0
            current_rates["eur_change_percent"] = 0.0
        save_data(CURRENT_RATES_FILE, current_rates)

@app.route('/api/bcv-rates', methods=['GET'])
def get_current_bcv_rates():
    """Endpoint para obtener las tasas actuales del BCV."""
    return jsonify(current_rates)

@app.route('/api/bcv-history', methods=['GET'])
def get_bcv_history():
    """Endpoint para obtener el historial de tasas del BCV."""
    return jsonify(historical_rates_data)

def self_ping():
    """
    Realiza un ping a la propia aplicación para mantenerla activa en servicios como Render Free Tier.
    Esto evita que la aplicación se "duerma" por inactividad.
    """
    # Obtener el hostname externo desde las variables de entorno de Render
    app_external_hostname = os.environ.get('RENDER_EXTERNAL_HOSTNAME')

    if app_external_hostname:
        # Usar la nueva dirección proporcionada por el usuario
        ping_url = f"https://kmbio-backend.onrender.com/api/bcv-rates"
        try:
            response = requests.get(ping_url, timeout=5)
            if response.status_code == 200:
                print(f"[{datetime.now(VENEZUELA_TZ).strftime('%Y-%m-%d %H:%M:%S')}] Self-ping exitoso a {ping_url}. Estado: {response.status_code}")
            else:
                print(f"[{datetime.now(VENEZUELA_TZ).strftime('%Y-%m-%d %H:%M:%S')}] Self-ping fallido a {ping_url}. Estado: {response.status_code}")
        except requests.exceptions.RequestException as e:
            print(f"[{datetime.now(VENEZUELA_TZ).strftime('%Y-%m-%d %H:%M:%S')}] Error en self-ping a {ping_url}: {e}")
    else:
        print(f"[{datetime.now(VENEZUELA_TZ).strftime('%Y-%m-%d %H:%M:%S')}] Advertencia: La variable de entorno 'RENDER_EXTERNAL_HOSTNAME' no está configurada. No se puede realizar el self-ping.")
        print(f"[{datetime.now(VENEZUELA_TZ).strftime('%Y-%m-%d %H:%M:%S')}] Esto podría significar que tu app se duerma en Render Free Tier.")


# Configuración del scheduler para tareas en segundo plano
scheduler = BackgroundScheduler(timezone="America/Caracas")

if __name__ == '__main__':
    # Ejecutar el scraping al inicio para tener datos frescos tan pronto como la aplicación inicie
    fetch_and_update_bcv_rates()

    # Programar el scraping para que se ejecute diariamente a la 00:01 (medianoche)
    # Esto asegura que las tasas se actualicen al inicio de cada día hábil.
    scheduler.add_job(fetch_and_update_bcv_rates, 'cron', hour=0, minute=1, day_of_week='mon-fri')
    
    # Programar el self-ping para que se ejecute cada 10 minutos
    # Esto ayuda a mantener la aplicación "despierta" en el plan gratuito de Render.com
    scheduler.add_job(self_ping, 'interval', minutes=10)

    # Iniciar el scheduler
    scheduler.start()

    # Obtener el puerto de las variables de entorno (para entornos de despliegue como Render.com)
    port = int(os.environ.get('PORT', 5000))
    # Iniciar la aplicación Flask
    app.run(host='0.0.0.0', port=port, debug=False)
