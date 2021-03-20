"""
MIT License

Copyright (c) 2021 David Antliff <david.antliff@gmail.com>

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
"""

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
