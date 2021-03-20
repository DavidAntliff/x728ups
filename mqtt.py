import logging
import paho.mqtt.client as mqtt
from datetime import datetime

logger = logging.getLogger(__name__)

connected = 0


def on_connect(client, userdata, flags, rc):
    if rc == 0:
        global connected
        connected = True
        logger.info("MQTT connected")
    else:
        logger.warning("MQTT connection failed")


def on_disconnect(client, userdata, rc):
    global connected
    connected = False
    logger.info("MQTT disconnected")


def init(server_address, server_port):
    client = mqtt.Client("mqtt" + str(datetime.now()))
    client.on_connect = on_connect
    client.on_disconnect = on_disconnect
    logger.debug("Attempting MQTT connect")
    try:
        client.connect(server_address, port=server_port, keepalive=60)
        client.loop_start()
    except ConnectionRefusedError:
        logger.warning("MQTT connection attempt failed - will retry later")
    return client


def attempt_mqtt_reconnect(mqtt_client):
    if not connected:
        logger.debug("Attempting MQTT reconnect")
        try:
            mqtt_client.reconnect()
        except ConnectionRefusedError:
            logger.warning("MQTT reconnection attempt failed - will retry later")
