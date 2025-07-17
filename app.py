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

# Inicialización de la aplicación Flask
app = Flask(__name__)
# Habilitar CORS para permitir solicitudes desde el frontend de tu aplicación Android
CORS(app)

# Tasas predeterminadas en caso de que el scraping falle o no haya datos
DEFAULT_USD_RATE = 00.01
DEFAULT_EUR_RATE = 00.01
# Tasa de Unidad Tributaria (UT) fija
FIXED_UT_RATE = 43.00

# URL del Banco Central de Venezuela para el scraping
BCV_URL = "https://www.bcv.org.ve/"

# Nombres de archivos para almacenar las tasas actuales y el historial
CURRENT_RATES_FILE = 'current_rates.json'
HISTORICAL_RATES_FILE = 'historical_rates.json'

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
            print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Error: Archivo {file_path} corrupto o vacío. Usando datos predeterminados.")
            return default_data
    return default_data

def save_data(file_path, data):
    """Guarda datos en un archivo JSON."""
    with open(file_path, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=4, ensure_ascii=False)

# Cargar las tasas actuales y el historial al iniciar la aplicación
# Si los archivos no existen o están vacíos, se usarán los valores predeterminados
current_rates = load_data(CURRENT_RATES_FILE, {
    "usd": DEFAULT_USD_RATE,
    "eur": DEFAULT_EUR_RATE,
    "ut": FIXED_UT_RATE,
    "last_updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S") + " (predeterminado)",
    "usd_change_percent": 0.0, # Inicializar con 0% de cambio
    "eur_change_percent": 0.0  # Inicializar con 0% de cambio
})
historical_rates_data = load_data(HISTORICAL_RATES_FILE, [])

# Si no hay historial, generar datos simulados para 15 días
# Esto asegura que siempre haya algo de historial para calcular los porcentajes de cambio
if not historical_rates_data:
    today = datetime.now()
    for i in range(15):
        date = today - timedelta(days=i)
        # Generar tasas simuladas con pequeñas variaciones
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
    Intenta obtener las tasas de USD y EUR del BCV mediante web scraping,
    las actualiza, calcula el cambio porcentual con respecto al día anterior
    y guarda los datos en archivos JSON.
    """
    global current_rates, historical_rates_data
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Intentando actualizar tasas del BCV...")

    # Obtener las tasas del día anterior del historial para calcular el cambio porcentual
    # El historial está ordenado de más reciente a más antiguo, así que el índice 1 es el día anterior.
    previous_usd_rate_for_calc = current_rates.get("usd", DEFAULT_USD_RATE)
    previous_eur_rate_for_calc = current_rates.get("eur", DEFAULT_EUR_RATE)

    if len(historical_rates_data) >= 2:
        previous_usd_rate_for_calc = historical_rates_data[1]["usd"]
        previous_eur_rate_for_calc = historical_rates_data[1]["eur"]
    elif len(historical_rates_data) == 1:
        # Si solo hay una entrada, no hay un "día anterior" para calcular el cambio,
        # se usa la tasa actual como referencia para evitar división por cero si es 0.
        previous_usd_rate_for_calc = historical_rates_data[0]["usd"]
        previous_eur_rate_for_calc = historical_rates_data[0]["eur"]
    # Si historical_rates_data está vacío, se usarán los valores de current_rates iniciales (DEFAULT_USD_RATE/EUR_RATE)

    try:
        # Realizar la solicitud HTTP a la página del BCV
        # timeout para evitar que la solicitud se cuelgue indefinidamente
        # verify=False se usa aquí para evitar problemas con certificados SSL en algunos entornos,
        # pero en producción se recomienda True si el certificado es válido.
        response = requests.get(BCV_URL, timeout=5, verify=False)
        response.raise_for_status() # Lanza una excepción para códigos de estado HTTP de error (4xx o 5xx)

        # Parsear el contenido HTML de la página
        soup = BeautifulSoup(response.text, 'lxml')

        usd_rate = None
        eur_rate = None

        # --- Lógica de Web Scraping para USD ---
        # Buscar el div con id='dolar'
        usd_container = soup.find('div', id='dolar')
        if usd_container:
            # Dentro del contenedor del dólar, buscar el div con la clase 'centrado'
            centrado_div_usd = usd_container.find('div', class_='centrado')
            if centrado_div_usd:
                # Dentro de ese div, buscar la etiqueta <strong> que contiene el valor
                usd_strong_tag = centrado_div_usd.find('strong')
                if usd_strong_tag:
                    # Extraer el texto, reemplazar la coma por un punto y convertir a float
                    match = re.search(r'[\d,\.]+', usd_strong_tag.text)
                    if match:
                        usd_rate = float(match.group(0).replace(',', '.').strip())

        # --- Lógica de Web Scraping para EUR ---
        # Buscar el div con id='euro' (asumiendo una estructura similar al dólar)
        eur_container = soup.find('div', id='euro')
        if eur_container:
            # Dentro del contenedor del euro, buscar el div con la clase 'centrado'
            centrado_div_eur = eur_container.find('div', class_='centrado')
            if centrado_div_eur:
                # Dentro de ese div, buscar la etiqueta <strong> que contiene el valor
                eur_strong_tag = centrado_div_eur.find('strong')
                if eur_strong_tag:
                    # Extraer el texto, reemplazar la coma por un punto y convertir a float
                    match = re.search(r'[\d,\.]+', eur_strong_tag.text)
                    if match:
                        eur_rate = float(match.group(0).replace(',', '.').strip())

        # Verificar si se pudieron obtener ambas tasas
        if usd_rate is None or eur_rate is None:
            raise ValueError("No se pudieron encontrar las tasas de USD o EUR en la página del BCV. La estructura HTML pudo haber cambiado.")

        # Calcular el cambio porcentual
        usd_change_percent = 0.0
        eur_change_percent = 0.0

        if previous_usd_rate_for_calc != 0:
            usd_change_percent = ((usd_rate - previous_usd_rate_for_calc) / previous_usd_rate_for_calc) * 100
        if previous_eur_rate_for_calc != 0:
            eur_change_percent = ((eur_rate - previous_eur_rate_for_calc) / previous_eur_rate_for_calc) * 100

        # Actualizar el diccionario de tasas actuales
        current_rates = {
            "usd": usd_rate,
            "eur": eur_rate,
            "ut": FIXED_UT_RATE,
            "last_updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "usd_change_percent": round(usd_change_percent, 2), # Redondear a 2 decimales
            "eur_change_percent": round(eur_change_percent, 2)  # Redondear a 2 decimales
        }
        save_data(CURRENT_RATES_FILE, current_rates) # Guardar las tasas actuales en el archivo

        # Actualizar el historial de tasas
        today_date_str = datetime.now().strftime("%d de %B de %Y")
        if not historical_rates_data or historical_rates_data[0]["date"] != today_date_str:
            # Si la fecha de hoy no es la primera entrada, añadir una nueva entrada al inicio
            historical_rates_data.insert(0, {
                "date": today_date_str,
                "usd": usd_rate,
                "eur": eur_rate
            })
            # Mantener solo los últimos 15 días en el historial
            historical_rates_data = historical_rates_data[:15]
        else:
            # Si la fecha de hoy ya es la primera entrada, actualizar sus valores
            historical_rates_data[0]["usd"] = usd_rate
            historical_rates_data[0]["eur"] = eur_rate
        save_data(HISTORICAL_RATES_FILE, historical_rates_data) # Guardar el historial actualizado

        print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Tasas actualizadas y guardadas: USD={usd_rate:.4f} ({usd_change_percent:.2f}%), EUR={eur_rate:.4f} ({eur_change_percent:.2f}%)")

    except requests.exceptions.Timeout:
        print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Error: Tiempo de espera agotado al conectar con el BCV. Usando tasas guardadas/predeterminadas.")
        # Si hay un error, recalcular los porcentajes con las tasas actuales y las del día anterior
        # para que el frontend no muestre porcentajes incorrectos o vacíos.
        if previous_usd_rate_for_calc != 0:
            current_rates["usd_change_percent"] = ((current_rates["usd"] - previous_usd_rate_for_calc) / previous_usd_rate_for_calc) * 100
        else:
            current_rates["usd_change_percent"] = 0.0
        if previous_eur_rate_for_calc != 0:
            current_rates["eur_change_percent"] = ((current_rates["eur"] - previous_eur_rate_for_calc) / previous_eur_rate_for_calc) * 100
        else:
            current_rates["eur_change_percent"] = 0.0
        save_data(CURRENT_RATES_FILE, current_rates) # Guardar las tasas actuales con los porcentajes actualizados
    except requests.exceptions.RequestException as e:
        print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Error de red o HTTP al conectar con el BCV: {e}. Usando tasas guardadas/predeterminadas.")
        if previous_usd_rate_for_calc != 0:
            current_rates["usd_change_percent"] = ((current_rates["usd"] - previous_usd_rate_for_calc) / previous_usd_rate_for_calc) * 100
        else:
            current_rates["usd_change_percent"] = 0.0
        if previous_eur_rate_for_calc != 0:
            current_rates["eur_change_percent"] = ((current_rates["eur"] - previous_eur_rate_for_calc) / previous_eur_rate_for_calc) * 100
        else:
            current_rates["eur_change_percent"] = 0.0
        save_data(CURRENT_RATES_FILE, current_rates)
    except AttributeError:
        print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Error de scraping: No se encontraron los elementos HTML esperados. La estructura de la página del BCV pudo haber cambiado. Usando tasas guardadas/predeterminadas.")
        if previous_usd_rate_for_calc != 0:
            current_rates["usd_change_percent"] = ((current_rates["usd"] - previous_usd_rate_for_calc) / previous_usd_rate_for_calc) * 100
        else:
            current_rates["usd_change_percent"] = 0.0
        if previous_eur_rate_for_calc != 0:
            current_rates["eur_change_percent"] = ((current_rates["eur"] - previous_eur_rate_for_calc) / previous_eur_rate_for_calc) * 100
        else:
            current_rates["eur_change_percent"] = 0.0
        save_data(CURRENT_RATES_FILE, current_rates)
    except ValueError as e:
        print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Error de procesamiento de datos: {e}. Usando tasas guardadas/predeterminadas.")
        if previous_usd_rate_for_calc != 0:
            current_rates["usd_change_percent"] = ((current_rates["usd"] - previous_usd_rate_for_calc) / previous_usd_rate_for_calc) * 100
        else:
            current_rates["usd_change_percent"] = 0.0
        if previous_eur_rate_for_calc != 0:
            current_rates["eur_change_percent"] = ((current_rates["eur"] - previous_eur_rate_for_calc) / previous_eur_rate_for_calc) * 100
        else:
            current_rates["eur_change_percent"] = 0.0
        save_data(CURRENT_RATES_FILE, current_rates)
    except Exception as e:
        print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Ocurrió un error inesperado durante el scraping: {e}. Usando tasas guardadas/predeterminadas.")
        if previous_usd_rate_for_calc != 0:
            current_rates["usd_change_percent"] = ((current_rates["usd"] - previous_usd_rate_for_calc) / previous_usd_rate_for_calc) * 100
        else:
            current_rates["usd_change_percent"] = 0.0
        if previous_eur_rate_for_calc != 0:
            current_rates["eur_change_percent"] = ((current_rates["eur"] - previous_eur_rate_for_calc) / previous_eur_rate_for_calc) * 100
        else:
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
        # Corregido: Eliminar el doble "https://"
        ping_url = f"https://kmbio-api.onrender.com/api/bcv-rates" # Usar la variable de entorno para la URL
        try:
            response = requests.get(ping_url, timeout=5)
            if response.status_code == 200:
                print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Self-ping exitoso a {ping_url}. Estado: {response.status_code}")
            else:
                print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Self-ping fallido a {ping_url}. Estado: {response.status_code}")
        except requests.exceptions.RequestException as e:
            print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Error en self-ping a {ping_url}: {e}")
    else:
        print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Advertencia: La variable de entorno 'RENDER_EXTERNAL_HOSTNAME' no está configurada. No se puede realizar el self-ping.")
        print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Esto podría significar que tu app se duerma en Render Free Tier.")


# Configuración del scheduler para tareas en segundo plano
# La zona horaria es crucial para que las tareas se ejecuten a la hora correcta en Venezuela
scheduler = BackgroundScheduler(timezone="America/Caracas")

if __name__ == '__main__':
    # Ejecutar el scraping al inicio para tener datos frescos tan pronto como la aplicación inicie
    fetch_and_update_bcv_rates()

    # Programar el scraping para que se ejecute diariamente a la 00:01 (medianoche)
    # Esto asegura que las tasas se actualicen al inicio de cada día hábil.
    scheduler.add_job(fetch_and_update_bcv_rates, 'cron', hour=0, minute=1, day_of_week='mon-fri')
    
    # Programar el self-ping para que se ejecute cada 5 segundos
    # Esto ayuda a mantener la aplicación "despierta" en el plan gratuito de Render.com
    # (Render duerme los servicios después de 15 minutos de inactividad).
    scheduler.add_job(self_ping, 'interval', seconds=5)

    # Iniciar el scheduler
    scheduler.start()

    # Obtener el puerto de las variables de entorno (para entornos de despliegue como Render.com)
    # o usar el puerto 5000 por defecto si no está definido (para pruebas locales).
    port = int(os.environ.get('PORT', 5000))
    # Iniciar la aplicación Flask
    # host='0.0.0.0' permite que la aplicación sea accesible desde cualquier IP (necesario en servidores)
    # debug=False es importante para producción
    app.run(host='0.0.0.0', port=port, debug=False)
