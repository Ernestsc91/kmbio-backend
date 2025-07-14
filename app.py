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

# Tasas predeterminadas en caso de que el scraping falle o no se pueda conectar
DEFAULT_USD_RATE = 114.41
DEFAULT_EUR_RATE = 134.86
FIXED_UT_RATE = 43.00

# URL del Banco Central de Venezuela
BCV_URL = "https://www.bcv.org.ve/"

# Archivos para guardar las tasas actuales e históricas
CURRENT_RATES_FILE = 'current_rates.json'
HISTORICAL_RATES_FILE = 'historical_rates.json'

# Función para cargar datos desde un archivo JSON
def load_data(file_path, default_data):
    """Carga datos desde un archivo JSON, o devuelve datos predeterminados si el archivo no existe."""
    if os.path.exists(file_path):
        with open(file_path, 'r', encoding='utf-8') as f:
            return json.load(f)
    return default_data

# Función para guardar datos en un archivo JSON
def save_data(file_path, data):
    """Guarda datos en un archivo JSON."""
    with open(file_path, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=4, ensure_ascii=False)

# Cargar tasas actuales e históricas al iniciar la aplicación
current_rates = load_data(CURRENT_RATES_FILE, {
    "usd": DEFAULT_USD_RATE,
    "eur": DEFAULT_EUR_RATE,
    "ut": FIXED_UT_RATE,
    "last_updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S") + " (predeterminado)"
})
historical_rates_data = load_data(HISTORICAL_RATES_FILE, [])

# Inicializar datos históricos si están vacíos (con datos simulados para los últimos 30 días)
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
    # Asegurarse de que el historial esté ordenado por fecha descendente
    historical_rates_data.sort(key=lambda x: datetime.strptime(x['date'], "%d de %B de %Y"), reverse=True)
    save_data(HISTORICAL_RATES_FILE, historical_rates_data)

def fetch_and_update_bcv_rates():
    """
    Intenta obtener las tasas de USD y EUR del BCV.
    Si tiene éxito, actualiza las tasas actuales y el historial.
    Si falla, imprime un error y continúa usando las tasas guardadas/predeterminadas.
    """
    global current_rates, historical_rates_data
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Intentando actualizar tasas del BCV...")
    try:
        # Realiza la solicitud HTTP a la página del BCV
        response = requests.get(BCV_URL, timeout=15, verify=False)
        response.raise_for_status() # Lanza una excepción para errores HTTP

        # Analiza el contenido HTML con BeautifulSoup
        soup = BeautifulSoup(response.text, 'lxml')

        usd_rate = None
        eur_rate = None

        # Intenta encontrar la tasa de USD
        usd_container = soup.find('div', id='dolar')
        if usd_container:
            centrado_div_usd = usd_container.find('div', class_='centrado')
            if centrado_div_usd:
                usd_strong_tag = centrado_div_usd.find('strong')
                if usd_strong_tag:
                    # Usa regex para extraer el número, maneja comas como decimales
                    match = re.search(r'[\d,\.]+', usd_strong_tag.text)
                    if match:
                        usd_rate = float(match.group(0).replace(',', '.').strip())

        # Intenta encontrar la tasa de EUR
        eur_container = soup.find('div', id='euro')
        if eur_container:
            centrado_div_eur = eur_container.find('div', class_='centrado')
            if centrado_div_eur:
                eur_strong_tag = centrado_div_eur.find('strong')
                if eur_strong_tag:
                    match = re.search(r'[\d,\.]+', eur_strong_tag.text)
                    if match:
                        eur_rate = float(match.group(0).replace(',', '.').strip())

        # Verifica si se obtuvieron ambas tasas
        if usd_rate is None or eur_rate is None:
            raise ValueError("No se pudieron encontrar las tasas de USD o EUR en la página del BCV. La estructura HTML pudo haber cambiado.")

        # Actualiza las tasas actuales en memoria y en el archivo
        current_rates = {
            "usd": usd_rate,
            "eur": eur_rate,
            "ut": FIXED_UT_RATE, # La UT permanece fija
            "last_updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        }
        save_data(CURRENT_RATES_FILE, current_rates)

        # Actualiza el historial de tasas si la fecha es diferente a la última guardada
        today_date_str = datetime.now().strftime("%d de %B de %Y")
        if not historical_rates_data or historical_rates_data[0]["date"] != today_date_str:
            historical_rates_data.insert(0, {
                "date": today_date_str,
                "usd": usd_rate,
                "eur": eur_rate
            })
            # Mantener solo los últimos 30 días de historial
            historical_rates_data = historical_rates_data[:30]
            save_data(HISTORICAL_RATES_FILE, historical_rates_data)
        
        print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Tasas actualizadas y guardadas: USD={usd_rate}, EUR={eur_rate}")

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

# Endpoint para obtener las tasas actuales
@app.route('/api/bcv-rates', methods=['GET'])
def get_current_bcv_rates():
    """Devuelve las tasas de cambio actuales en formato JSON."""
    return jsonify(current_rates)

# Endpoint para obtener el historial de tasas
@app.route('/api/bcv-history', methods=['GET'])
def get_bcv_history():
    """Devuelve el historial de tasas en formato JSON."""
    return jsonify(historical_rates_data)

# Configuración del programador de tareas en segundo plano (APScheduler)
# Se establece la zona horaria a "America/Caracas" para asegurar la correcta programación.
scheduler = BackgroundScheduler(timezone="America/Caracas")

if __name__ == '__main__':
    # Ejecuta la actualización de tasas inmediatamente al iniciar el servidor
    fetch_and_update_bcv_rates()

    # Programa la tarea para ejecutarse de lunes a viernes a las 12:01 AM (00:01)
    # day_of_week='mon-fri' asegura que solo se ejecute en esos días.
    scheduler.add_job(fetch_and_update_bcv_rates, 'cron', hour=0, minute=1, day_of_week='mon-fri')
    scheduler.start()

    # Obtiene el puerto del entorno (para despliegue en Render) o usa 5000 por defecto
    port = int(os.environ.get('PORT', 5000))
    # Inicia la aplicación Flask
    app.run(host='0.0.0.0', port=port, debug=False)
