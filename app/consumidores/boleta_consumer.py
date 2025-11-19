import os
import json
import time
import traceback
import pika
import requests
from email.message import EmailMessage
import smtplib
from dotenv import load_dotenv

load_dotenv()

# --- CONFIGURACIÓN ---
RABBITMQ_HOST = os.getenv('RABBITMQ_HOST', 'localhost')

SMTP_SERVER = os.getenv('MAIL_SERVER', 'smtp.gmail.com')
SMTP_PORT   = int(os.getenv('MAIL_PORT', 587))
SMTP_USER   = os.getenv('MAIL_USERNAME')
SMTP_PASS   = os.getenv('MAIL_PASSWORD')
MAIL_USE_TLS = os.getenv('MAIL_USE_TLS', 'true').lower() == 'true'

SUNAT_TOKEN = os.getenv('SUNAT_TOKEN')

boletas = []

# --- OPTIMIZACIONES ---
# Sesión persistente para evitar reconexiones HTTP
session = requests.Session()

# Encabezados fijos para API
DNI_HEADERS = {
    "Accept": "application/json",
    "Content-Type": "application/json",
    "Authorization": f"Bearer {SUNAT_TOKEN}"
}

# Reutilización del servidor SMTP
smtp_global = None
def get_smtp():
    global smtp_global
    if smtp_global is None:
        smtp_global = smtplib.SMTP(SMTP_SERVER, SMTP_PORT)
        smtp_global.ehlo()
        if MAIL_USE_TLS:
            smtp_global.starttls()
            smtp_global.ehlo()
        smtp_global.login(SMTP_USER, SMTP_PASS)
    return smtp_global


# --- CONSULTA DNI ---
def obtener_datos_dni(dni: str):
    payload = json.dumps({"dni": dni})
    try:
        response = session.post(
            "https://apiperu.dev/api/dni",
            headers=DNI_HEADERS,
            data=payload,
            timeout=6,     # menor timeout → menor bloqueo
        )
        if response.status_code == 200:
            return response.json()
        return {"error": f"HTTP {response.status_code}"}
    except Exception:
        return {"error": "Error de conexión con API DNI"}


# --- ENVÍO DE CORREO ---
def enviar_correo(destino, asunto, cuerpo):
    try:
        msg = EmailMessage()
        msg["Subject"] = asunto
        msg["From"] = SMTP_USER
        msg["To"] = destino
        msg.set_content(cuerpo)

        server = get_smtp()
        server.send_message(msg)

    except Exception as e:
        print(f"[EMAIL] Error: {e}")


# --- PROCESAMIENTO DE MENSAJE ---
def callback(ch, method, properties, body):
    try:
        data = json.loads(body)

        if data.get("tipo_comprobante") != "boleta":
            ch.basic_ack(method.delivery_tag)
            return

        dni = data.get("dni")
        if not dni:
            ch.basic_ack(method.delivery_tag)
            return

        # Consulta DNI optimizada
        datos_dni = obtener_datos_dni(dni)
        data.update(datos_dni.get("data", {"dni_error": datos_dni.get("error")}))

        # Guardar boleta
        boletas.append(data)

        # Envío correo
        if (email := data.get("email_destino")):
            cuerpo = "Detalle de la boleta:\n\n" + "\n".join(
                f"{k}: {v}" for k, v in data.items()
            )
            enviar_correo(email, f"Boleta {data.get('numero', 'N/A')}", cuerpo)

        ch.basic_ack(method.delivery_tag)

    except Exception as e:
        print(f"[BOLETA] Error: {e}")
        traceback.print_exc()
        ch.basic_ack(method.delivery_tag)


# --- CONSUMIDOR RABBITMQ ---
def consumir():
    for i in range(10):
        try:
            print(f"[BOLETA] Conectando a RabbitMQ ({i+1}/10)…")

            connection = pika.BlockingConnection(
                pika.ConnectionParameters(
                    RABBITMQ_HOST,
                    5672,
                    heartbeat=300,
                    blocked_connection_timeout=200,
                )
            )

            channel = connection.channel()
            channel.queue_declare(queue="cola_boletas", durable=True)

            # Permite procesar 10 mensajes antes de pedir más  → mejora throughput
            channel.basic_qos(prefetch_count=10)

            channel.basic_consume(
                queue="cola_boletas",
                on_message_callback=callback,
                auto_ack=False
            )

            print("[BOLETA] Esperando mensajes…")
            channel.start_consuming()
            break

        except pika.exceptions.AMQPConnectionError:
            print("[RabbitMQ] Error de conexión, reintentando en 3s…")
            time.sleep(3)
        except Exception as e:
            print(f"[RabbitMQ] Error inesperado: {e}")
            traceback.print_exc()
            break


# --- INICIO ---
if __name__ == "__main__":
    print("[BOLETA] Iniciando consumidor…")
    consumir()