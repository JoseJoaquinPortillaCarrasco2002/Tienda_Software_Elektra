import os
import json
import pika
import time
import traceback
import requests
from email.message import EmailMessage
import smtplib
from dotenv import load_dotenv

# Cargar variables de entorno
load_dotenv()
SUNAT_TOKEN = os.getenv('SUNAT_TOKEN')
RABBITMQ_HOST = os.getenv('RABBITMQ_HOST', 'localhost')
SMTP_SERVER = os.getenv('MAIL_SERVER', 'smtp.gmail.com')
SMTP_PORT = int(os.getenv('MAIL_PORT', 587))
SMTP_USER = os.getenv('MAIL_USERNAME')
SMTP_PASS = os.getenv('MAIL_PASSWORD')
MAIL_USE_TLS = os.getenv('MAIL_USE_TLS', 'true').lower() == 'true'

# Sesión persistente para requests (reduce latencia)
session = requests.Session()
SUNAT_HEADERS = {
    "Accept": "application/json",
    "Content-Type": "application/json",
    "Authorization": f"Bearer {SUNAT_TOKEN}"
}

# Reusar servidor SMTP (solo se inicia una vez)
smtp_server_global = None

# Lista para almacenar las facturas temporalmente
facturas = []

# Consultar datos del RUC en SUNAT usando sesión persistente
def obtener_datos_sunat(ruc: str):
    payload = json.dumps({"ruc": ruc})
    try:
        response = session.post(
            "https://apiperu.dev/api/ruc",
            headers=SUNAT_HEADERS,
            data=payload,
            timeout=6   # Menor timeout para menor bloqueo
        )
        if response.status_code == 200:
            return response.json()
        return {"error": f"Error {response.status_code}"}
    except requests.RequestException:
        return {"error": "Error de conexión con SUNAT"}

# Inicializar SMTP una sola vez
def get_smtp_connection():
    global smtp_server_global
    if smtp_server_global is None:
        server = smtplib.SMTP(SMTP_SERVER, SMTP_PORT)
        server.ehlo()
        if MAIL_USE_TLS:
            server.starttls()
            server.ehlo()
        server.login(SMTP_USER, SMTP_PASS)
        smtp_server_global = server
    return smtp_server_global

# Enviar correo
def enviar_correo(destino, asunto, cuerpo):
    try:
        if not SMTP_USER or not SMTP_PASS:
            raise ValueError("Credenciales SMTP faltantes.")

        msg = EmailMessage()
        msg['Subject'] = asunto
        msg['From'] = SMTP_USER
        msg['To'] = destino
        msg.set_content(cuerpo)

        server = get_smtp_connection()
        server.send_message(msg)

    except Exception as e:
        print(f"[EMAIL] Error al enviar correo: {e}")

# Procesar mensajes
def callback(ch, method, properties, body):
    try:
        data = json.loads(body)

        # Validaciones rápidas
        if data.get('tipo_comprobante') != 'factura':
            ch.basic_ack(method.delivery_tag)
            return

        ruc = data.get('ruc')
        if not ruc:
            ch.basic_ack(method.delivery_tag)
            return

        # SUNAT
        datos_sunat = obtener_datos_sunat(ruc)
        data.update(datos_sunat.get("data", {"sunat_error": datos_sunat.get("error")}))

        # Guardar factura
        facturas.append(data)

        # Enviar correo
        if email_destino := data.get('email_destino'):
            cuerpo_correo = "Detalle de la factura:\n\n" + "\n".join(
                f"{k}: {v}" for k, v in data.items()
            )
            enviar_correo(email_destino, f"Factura {ruc}", cuerpo_correo)

        ch.basic_ack(method.delivery_tag)

    except Exception as e:
        print(f"[FACTURA] Error procesando mensaje: {e}")
        traceback.print_exc()
        ch.basic_ack(method.delivery_tag)

# Consumidor RabbitMQ
def consumir():
    for attempt in range(10):
        try:
            print(f"[FACTURA] Conectando a RabbitMQ (intento {attempt+1}) ...")

            connection = pika.BlockingConnection(
                pika.ConnectionParameters(
                    RABBITMQ_HOST,
                    5672,
                    heartbeat=300,
                    blocked_connection_timeout=200,
                )
            )

            channel = connection.channel()
            channel.queue_declare(queue='cola_facturas', durable=True)

            # Permite procesar varios mensajes más rápido
            channel.basic_qos(prefetch_count=10)

            channel.basic_consume(
                queue='cola_facturas',
                on_message_callback=callback,
                auto_ack=False
            )

            print("[FACTURA] Esperando mensajes...")
            channel.start_consuming()
            break

        except pika.exceptions.AMQPConnectionError:
            print("[RabbitMQ] Fallo conexión. Reintentando en 3s...")
            time.sleep(3)

        except Exception as e:
            print(f"[RabbitMQ] Error inesperado: {e}")
            traceback.print_exc()
            break

# Iniciar
if _name_ == "_main_":
    print("[FACTURA] Iniciando consumidor...")
    consumir()