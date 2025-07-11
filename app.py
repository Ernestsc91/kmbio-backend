# app.py
from flask import Flask, jsonify
from flask_cors import CORS
import requests
from bs4 import BeautifulSoup
from datetime import datetime, timedelta
import random
import os
import re # Importar la librería re para expresiones regulares

app = Flask(__name__)
CORS(app) # Habilitar CORS para todas las rutas. ¡Importante para que tu app HTML pueda acceder!

# --- Configuración de Tasas Predeterminadas (Fallback) ---
# Estas tasas se usarán si el web scraping falla o si no se pueden obtener datos.
# Puedes ajustarlas manualmente si es necesario.
DEFAULT_USD_RATE = 00.01
DEFAULT_EUR_RATE = 00.01
# DEFAULT_COL_RATE = 0.0095 # Eliminado: Tasa de COP a VEF
FIXED_UT_RATE = 43.00 # La Unidad Tributaria es un valor fijo que no cambia diariamente.

# --- URL del Banco Central de Venezuela ---
# ¡ADVERTENCIA! La estructura HTML de esta página puede cambiar en cualquier momento.
# Si la aplicación deja de obtener las tasas, es probable que necesites actualizar los selectores CSS aquí.
BCV_URL = "https://www.bcv.org.ve/"

# --- Función para realizar Web Scraping ---
def fetch_bcv_rates():
    """
    Realiza web scraping en la página del BCV para obtener las tasas actuales de USD y EUR.
    Devuelve un diccionario con las tasas o None si falla.
    """
    try:
        # Realizar la solicitud HTTP a la página del BCV
        # CAMBIO: Añadido verify=False para ignorar errores de verificación SSL
        response = requests.get(BCV_URL, timeout=15, verify=False) 
        response.raise_for_status() # Lanzar excepción para errores HTTP (4xx o 5xx)

        # Parsear el contenido HTML
        soup = BeautifulSoup(response.text, 'lxml') # Usar 'lxml' para mayor velocidad y robustez

        usd_rate = None
        eur_rate = None
        # col_rate = None # Eliminado: Variable para COP

        # --- SELECTORES CSS ACTUALIZADOS PARA USD (Basado en tu inspección) ---
        # Busca el div principal con id='dolar'
        usd_container = soup.find('div', id='dolar')
        if usd_container:
            # Dentro de ese div, buscar el div con la clase 'centrado'
            centrado_div_usd = usd_container.find('div', class_='centrado')
            if centrado_div_usd:
                # Dentro de ese div 'centrado', buscar la etiqueta <strong> que contiene el valor
                usd_strong_tag = centrado_div_usd.find('strong')
                if usd_strong_tag:
                    # Extraer el texto, reemplazar coma por punto y convertir a float
                    # Utilizar una expresión regular para limpiar el texto y obtener solo el número
                    match = re.search(r'[\d,\.]+', usd_strong_tag.text)
                    if match:
                        usd_rate = float(match.group(0).replace(',', '.').strip())
                    print(f"USD rate scraped: {usd_rate}") # Para depuración

        # --- SELECTORES CSS ACTUALIZADOS PARA EUR (Basado en tu inspección) ---
        # Busca el div principal con id='euro'
        eur_container = soup.find('div', id='euro')
        if eur_container:
            # Dentro de ese div, buscar el div con la clase 'centrado'
            centrado_div_eur = eur_container.find('div', class_='centrado')
            if centrado_div_eur:
                # Dentro de ese div 'centrado', buscar la etiqueta <strong> que contiene el valor
                eur_strong_tag = centrado_div_eur.find('strong')
                if eur_strong_tag:
                    # Extraer el texto, reemplazar coma por punto y convertir a float
                    match = re.search(r'[\d,\.]+', eur_strong_tag.text)
                    if match:
                        eur_rate = float(match.group(0).replace(',', '.').strip())
                    print(f"EUR rate scraped: {eur_rate}") # Para depuración

        # --- Para el Peso Colombiano (COP) ---
        # Como se eliminó del frontend, no necesitamos scrapear ni devolverlo aquí.
        # Si en el futuro se desea añadir, se deberá reintroducir la lógica.

        if usd_rate is None or eur_rate is None:
            raise ValueError("No se pudieron encontrar las tasas de USD o EUR en la página del BCV. La estructura HTML pudo haber cambiado.")

        return {
            "usd": usd_rate,
            "eur": eur_rate,
            # "col": None, # Eliminado: Ya no se maneja COP
            "ut": FIXED_UT_RATE, # UT es fija
            "last_updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        }

    except requests.exceptions.Timeout:
        print("Error: Tiempo de espera agotado al conectar con el BCV.")
        return None
    except requests.exceptions.RequestException as e:
        print(f"Error de red o HTTP al conectar con el BCV: {e}")
        return None
    except AttributeError:
        print("Error de scraping: No se encontraron los elementos HTML esperados. La estructura de la página del BCV pudo haber cambiado.")
        return None
    except ValueError as e:
        print(f"Error de procesamiento de datos: {e}")
        return None
    except Exception as e:
        print(f"Ocurrió un error inesperado durante el scraping: {e}")
        return None

# --- Almacenamiento de Tasas (Simulado para el historial) ---
# En una aplicación real, usarías una base de datos (SQLite, PostgreSQL, MongoDB)
# para almacenar el historial de tasas de forma persistente.
# Para este ejemplo, simularemos un historial en memoria.
historical_rates_data = []

def generate_simulated_history():
    """Genera datos históricos simulados para 30 días."""
    global historical_rates_data
    historical_rates_data = []
    today = datetime.now()
    
    # Usar las tasas actuales o predeterminadas como base para la simulación
    base_usd = DEFAULT_USD_RATE
    base_eur = DEFAULT_EUR_RATE
    # base_col = DEFAULT_COL_RATE # Eliminado: Ya no se simula COP

    for i in range(30):
        date = today - timedelta(days=i)
        # Simular pequeñas variaciones alrededor de las tasas base
        sim_usd = round(base_usd + (random.random() - 0.5) * 0.5, 2) # +/- 0.25
        sim_eur = round(base_eur + (random.random() - 0.5) * 0.6, 2) # +/- 0.30
        # sim_col = round(base_col + (random.random() - 0.5) * 0.0005, 4) # Eliminado: Simular COP

        historical_rates_data.append({
            "date": date.strftime("%d de %B de %Y"), # Formato legible
            "usd": sim_usd,
            "eur": sim_eur,
            # "col": sim_col # Eliminado: Ya no se añade COP al historial
        })
    # Asegurarse de que el historial esté ordenado por fecha más reciente primero
    historical_rates_data.sort(key=lambda x: datetime.strptime(x['date'], "%d de %B de %Y"), reverse=True)


# --- Rutas de la API ---

@app.route('/api/bcv-rates', methods=['GET'])
def get_current_bcv_rates():
    """
    Endpoint para obtener las tasas de cambio actuales del BCV.
    Intenta scrapear; si falla, usa tasas predeterminadas.
    """
    rates = fetch_bcv_rates()
    if rates:
        # No hay necesidad de manejar COP aquí ya que fue eliminado
        return jsonify(rates)
    else:
        # Fallback a tasas predeterminadas si el scraping falla
        fallback_rates = {
            "usd": DEFAULT_USD_RATE,
            "eur": DEFAULT_EUR_RATE,
            # "col": DEFAULT_COL_RATE, # Eliminado: Ya no se devuelve COP
            "ut": FIXED_UT_RATE,
            "last_updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S") + " (simulado)"
        }
        return jsonify(fallback_rates), 200 # Devolver 200 OK incluso con datos simulados si es un fallback esperado

@app.route('/api/bcv-history', methods=['GET'])
def get_bcv_history():
    """
    Endpoint para obtener el historial de tasas de cambio del BCV.
    Actualmente usa datos simulados. En un entorno real, consultaría una base de datos.
    """
    if not historical_rates_data:
        generate_simulated_history() # Generar historial si no existe (solo al inicio o si se limpia)
    return jsonify(historical_rates_data)

# --- Inicialización del Servidor ---
if __name__ == '__main__':
    # Generar el historial simulado al iniciar el servidor por primera vez
    generate_simulated_history()
    
    # Obtener el puerto del entorno (para despliegue) o usar 5000 por defecto
    port = int(os.environ.get('PORT', 5000))
    # '0.0.0.0' hace que el servidor sea accesible desde cualquier IP (necesario en despliegue)
    # debug=True es útil para desarrollo (recarga el servidor automáticamente con cambios),
    # pero debe ser False en producción por seguridad y rendimiento.
    app.run(host='0.0.0.0', port=port, debug=True)
