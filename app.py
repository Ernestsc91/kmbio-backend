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

app = Flask(__name__)
CORS(app)

DEFAULT_USD_RATE = 36.50
DEFAULT_EUR_RATE = 39.80
FIXED_UT_RATE = 43.00

BCV_URL = "https://www.bcv.org.ve/"

CURRENT_RATES_FILE = 'current_rates.json'
HISTORICAL_RATES_FILE = 'historical_rates.json'

def load_data(file_path, default_data):
    """Carga datos desde un archivo JSON, o devuelve datos por defecto si el archivo no existe."""
    if os.path.exists(file_path):
        with open(file_path, 'r', encoding='utf-8') as f:
            return json.load(f)
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
    "last_updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S") + " (predeterminado)",
    "usd_change_percent": 0.0, # Inicializar con 0% de cambio
    "eur_change_percent": 0.0  # Inicializar con 0% de cambio
})
historical_rates_data = load_data(HISTORICAL_RATES_FILE, [])

# Si no hay historial, generar datos simulados para 30 días
if not historical_rates_data:
    today = datetime.now()
    for i in range(30):
        date = today - timedelta(days=i)
        sim_usd = round(DEFAULT_USD_RATE + (random.random() - 0.5) * 0.5, 2)
        sim_eur = round(DEFAULT_EUR_RATE + (random.random() - 0.5) * 0.6, 2)
        historical_rates_data.append({
            "date": date.strftime("%d de %B de %Y"),
            "usd": sim_usd,
            "eur": sim_eur
        })
    # Asegurarse de que el historial esté ordenado de más reciente a más antiguo
    historical_rates_data.sort(key=lambda x: datetime.strptime(x['date'], "%d de %B de %Y"), reverse=True)
    save_data(HISTORICAL_RATES_FILE, historical_rates_data)

def fetch_and_update_bcv_rates():
    """
    Intenta obtener las tasas de USD y EUR del BCV, las actualiza,
    calcula el cambio porcentual y guarda los datos.
    """
    global current_rates, historical_rates_data
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Intentando actualizar tasas del BCV...")

    # Guardar las tasas actuales antes de intentar el scraping para el cálculo del cambio
    # Esto es útil si el scraping falla y necesitamos comparar con las últimas tasas conocidas
    previous_usd_rate_for_calc = current_rates.get("usd", DEFAULT_USD_RATE)
    previous_eur_rate_for_calc = current_rates.get("eur", DEFAULT_EUR_RATE)

    # Si hay historial, usar la tasa del día anterior para el cálculo del porcentaje
    # El historial está ordenado de más reciente a más antiguo, así que el día anterior es el índice 1
    if len(historical_rates_data) >= 2:
        previous_usd_rate_for_calc = historical_rates_data[1]["usd"]
        previous_eur_rate_for_calc = historical_rates_data[1]["eur"]
    elif len(historical_rates_data) == 1:
        # Si solo hay una entrada, no hay un "día anterior" para calcular el cambio
        previous_usd_rate_for_calc = historical_rates_data[0]["usd"]
        previous_eur_rate_for_calc = historical_rates_data[0]["eur"]
    # Si historical_rates_data está vacío, se usarán los valores de current_rates iniciales (DEFAULT_USD_RATE/EUR_RATE)

    try:
        response = requests.get(BCV_URL, timeout=15, verify=False)
        response.raise_for_status() # Lanza una excepción para errores HTTP (4xx o 5xx)

        soup = BeautifulSoup(response.text, 'lxml')

        usd_rate = None
        eur_rate = None

        # Scraping para la tasa de USD
        usd_container = soup.find('div', id='dolar')
        if usd_container:
            centrado_div_usd = usd_container.find('div', class_='centrado')
            if centrado_div_usd:
                usd_strong_tag = centrado_div_usd.find('strong')
                if usd_strong_tag:
                    match = re.search(r'[\d,\.]+', usd_strong_tag.text)
                    if match:
                        usd_rate = float(match.group(0).replace(',', '.').strip())

        # Scraping para la tasa de EUR
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

        # Calcular el cambio porcentual
        usd_change_percent = 0.0
        eur_change_percent = 0.0

        if previous_usd_rate_for_calc != 0:
            usd_change_percent = ((usd_rate - previous_usd_rate_for_calc) / previous_usd_rate_for_calc) * 100
        if previous_eur_rate_for_calc != 0:
            eur_change_percent = ((eur_rate - previous_eur_rate_for_calc) / previous_eur_rate_for_calc) * 100

        # Actualizar las tasas actuales
        current_rates = {
            "usd": usd_rate,
            "eur": eur_rate,
            "ut": FIXED_UT_RATE,
            "last_updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "usd_change_percent": usd_change_percent,
            "eur_change_percent": eur_change_percent
        }
        save_data(CURRENT_RATES_FILE, current_rates)

        # Actualizar el historial
        today_date_str = datetime.now().strftime("%d de %B de %Y")
        if not historical_rates_data or historical_rates_data[0]["date"] != today_date_str:
            # Si la fecha de hoy no es la primera entrada, añadir una nueva
            historical_rates_data.insert(0, {
                "date": today_date_str,
                "usd": usd_rate,
                "eur": eur_rate
            })
            historical_rates_data = historical_rates_data[:30] # Mantener solo los últimos 30 días
        else:
            # Si la fecha de hoy ya es la primera entrada, actualizarla
            historical_rates_data[0]["usd"] = usd_rate
            historical_rates_data[0]["eur"] = eur_rate
        save_data(HISTORICAL_RATES_FILE, historical_rates_data)

        print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Tasas actualizadas y guardadas: USD={usd_rate:.4f} ({usd_change_percent:.2f}%), EUR={eur_rate:.4f} ({eur_change_percent:.2f}%)")

    except requests.exceptions.Timeout:
        print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Error: Tiempo de espera agotado al conectar con el BCV. Usando tasas guardadas/predeterminadas.")
        # En caso de error, actualizar los porcentajes con las tasas cargadas/predeterminadas
        # para que el frontend no muestre porcentajes incorrectos o vacíos.
        if len(historical_rates_data) >= 2:
            current_rates["usd_change_percent"] = ((current_rates["usd"] - previous_usd_rate_for_calc) / previous_usd_rate_for_calc) * 100 if previous_usd_rate_for_calc != 0 else 0.0
            current_rates["eur_change_percent"] = ((current_rates["eur"] - previous_eur_rate_for_calc) / previous_eur_rate_for_calc) * 100 if previous_eur_rate_for_calc != 0 else 0.0
        else:
            current_rates["usd_change_percent"] = 0.0
            current_rates["eur_change_percent"] = 0.0
        save_data(CURRENT_RATES_FILE, current_rates) # Guardar las tasas actuales con los porcentajes actualizados
    except requests.exceptions.RequestException as e:
        print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Error de red o HTTP al conectar con el BCV: {e}. Usando tasas guardadas/predeterminadas.")
        if len(historical_rates_data) >= 2:
            current_rates["usd_change_percent"] = ((current_rates["usd"] - previous_usd_rate_for_calc) / previous_usd_rate_for_calc) * 100 if previous_usd_rate_for_calc != 0 else 0.0
            current_rates["eur_change_percent"] = ((current_rates["eur"] - previous_eur_rate_for_calc) / previous_eur_rate_for_calc) * 100 if previous_eur_rate_for_calc != 0 else 0.0
        else:
            current_rates["usd_change_percent"] = 0.0
            current_rates["eur_change_percent"] = 0.0
        save_data(CURRENT_RATES_FILE, current_rates)
    except AttributeError:
        print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Error de scraping: No se encontraron los elementos HTML esperados. La estructura de la página del BCV pudo haber cambiado. Usando tasas guardadas/predeterminadas.")
        if len(historical_rates_data) >= 2:
            current_rates["usd_change_percent"] = ((current_rates["usd"] - previous_usd_rate_for_calc) / previous_usd_rate_for_calc) * 100 if previous_usd_rate_for_calc != 0 else 0.0
            current_rates["eur_change_percent"] = ((current_rates["eur"] - previous_eur_rate_for_calc) / previous_eur_rate_for_calc) * 100 if previous_eur_rate_for_calc != 0 else 0.0
        else:
            current_rates["usd_change_percent"] = 0.0
            current_rates["eur_change_percent"] = 0.0
        save_data(CURRENT_RATES_FILE, current_rates)
    except ValueError as e:
        print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Error de procesamiento de datos: {e}. Usando tasas guardadas/predeterminadas.")
        if len(historical_rates_data) >= 2:
            current_rates["usd_change_percent"] = ((current_rates["usd"] - previous_usd_rate_for_calc) / previous_usd_rate_for_calc) * 100 if previous_usd_rate_for_calc != 0 else 0.0
            current_rates["eur_change_percent"] = ((current_rates["eur"] - previous_eur_rate_for_calc) / previous_eur_rate_for_calc) * 100 if previous_eur_rate_for_calc != 0 else 0.0
        else:
            current_rates["usd_change_percent"] = 0.0
            current_rates["eur_change_percent"] = 0.0
        save_data(CURRENT_RATES_FILE, current_rates)
    except Exception as e:
        print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Ocurrió un error inesperado durante el scraping: {e}. Usando tasas guardadas/predeterminadas.")
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

scheduler = BackgroundScheduler(timezone="America/Caracas")

if __name__ == '__main__':
    # Ejecutar el scraping al inicio para tener datos frescos
    fetch_and_update_bcv_rates()

    # Programar el scraping para que se ejecute diariamente a la 00:01 (medianoche)
    scheduler.add_job(fetch_and_update_bcv_rates, 'cron', hour=0, minute=1)
    scheduler.start()

    # Obtener el puerto de las variables de entorno (para Render.com) o usar 5000 por defecto
    port = int(os.environ.get('PORT', 5000))
    # Iniciar la aplicación Flask
    app.run(host='0.0.0.0', port=port, debug=False)
