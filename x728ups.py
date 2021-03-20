#!/usr/bin/env python3
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

Intended for use with the X728 UPS for the Raspberry Pi.
Jumper 3 must be present for Power Loss Detection (PLD) functionality.


GPIO 5 - "SHUTDOWN"
 - High > 600ms when UPS requests RPi shutdown
 - High > 200ms & < 600ms when UPS requests RPi reboot

GPIO 6 - "PLD"
 - AC power loss detection
 - High when external power is disconnected (and jumper 3 present)
 - Low when external power is connected (or jumper 3 is missing)

GPIO 12 - "BOOT OK"
 - RPi should set this high after boot
 - when the UPS is in shutdown-waiting mode, the RPi setting this to low initiates a UPS power-off after ~5300ms.

GPIO 13 - "BUTTON"
 - can be set by RPi for a period of time to simulate a button-press
 - therefore can initiate a shutdown sequence on the UPS

RPi can request UPS shutdown with pulses on GPIO 13
UPS can request RPi shutdown with pulses on GPIO 5
UPS can notify RPi of external power status with GPIO 6
RPi can tell UPS that it is shut down with falling edge on GPIO 12 - UPS will auto-power off ~5300ms after this event.


Sequence of operation for this service is:

1. User initiates RPi power up, or external power is restored and Jumper 2 (AON) is present.
2. RPi boots and starts this systemd service (x728ups).
3. This service sets GPIO 12 ("BOOT OK") high immediately.
4. This service monitors GPIO 5:
     - If GPIO 5 is continuously high for more than 200ms and less than 600ms, this service will reboot the RPi.
     - if GPIO 5 is continuously high for more than 600ms, this service will shut down the RPi.
5. This service monitors GPIO 6:
     - If GPIO 6 is continuously high for more than X seconds, this service will request a shut down of the system via
       GPIO 13 (see note below).

Note: System reboot (UPS & RPi) is requested by holding GPIO 13 high for 240 - 1500ms.
System shutdown is requested by holding GPIO 13 high for longer than 1500ms.
System immediate power-off occurs if GPIO 13 is high for longer than ~6400ms.


UPS states:

 - Idle mode - blue PWR LED is solid on.
 - ~200ms to ~1500ms button press results in ~500ms pulse on GPIO 5, triple-blink of blue PWR LED - request reboot.
 - ~1500ms or longer button press results in continual high on GPIO 5, pulsing of blue PWR LED - request shutdown.
 - If GPIO 12 does not go low shortly after either of these requests are issued, the mode expires after about 50 seconds.

"""

import struct
import subprocess

import smbus
import RPi.GPIO as GPIO
import time
import sys
import multiprocessing
import argparse
import logging

import mqtt


logger = logging.getLogger(__name__)

# Shut down the pi if any of these thresholds are met
SHUTDOWN_BATTERY_CAPACITY = 50  # capacity falls below this percentage
SHUTDOWN_BATTERY_VOLTAGE = 3.6  # voltage falls below this value
SHUTDOWN_SECONDS = 20  # power-fail for longer than this duration

# x728 I2C bus info
I2C_BUS_ID = 1
I2C_ADDRESS = 0x36

GPIO_X728_SHUTDOWN = 5   # high for a duration if UPS requests reboot or shutdown
GPIO_X728_PLD = 6        # high if power loss detected
GPIO_X728_BOOT_OK = 12   # high if RPi is booted, UPS powers off when set low after shutdown request
GPIO_X728_SYSTEM = 13    # hold high to initiate system (UPS & RPi) reboot or shutdown

MQTT_SERVER_ADDRESS = "localhost"
MQTT_SERVER_PORT = 1883
MQTT_ROOT = "ups"

DATA_SEND_PERIOD = 60  # seconds between publishing battery data
MQTT_RETRY_PERIOD = 600  # seconds between retrying MQTT connection

SYSTEM_SHUTDOWN_REQUEST_DURATION = 4  # duration of GPIO 13 pulse, in seconds, to initiate system shutdown

REBOOT_PULSE_MINIMUM = 200  # milliseconds
REBOOT_PULSE_MAXIMUM = 600  # milliseconds

DOCKER_COMPOSE_FILE = "/home/pi/poolmon/services/docker-compose.yml"


def current_time_ms():
    return time.time_ns() // 1000000


def init_smbus(bus_id):
    return smbus.SMBus(bus_id) # 0 = /dev/i2c-0 (port I2C0), 1 = /dev/i2c-1 (port I2C1)


def read_voltage(bus):
    read = bus.read_word_data(I2C_ADDRESS, 2)
    swapped = struct.unpack("<H", struct.pack(">H", read))[0]
    voltage = swapped * 1.25 / 1000 / 16
    return voltage


def read_capacity(bus):
    read = bus.read_word_data(I2C_ADDRESS, 4)
    swapped = struct.unpack("<H", struct.pack(">H", read))[0]
    capacity = swapped / 256
    return capacity


def init_gpio():
    GPIO.setmode(GPIO.BCM)
    GPIO.setup(GPIO_X728_SHUTDOWN, GPIO.IN)
    GPIO.setup(GPIO_X728_PLD, GPIO.IN)
    GPIO.setup(GPIO_X728_BOOT_OK, GPIO.OUT)
    GPIO.output(GPIO_X728_BOOT_OK, 1)
    GPIO.setup(GPIO_X728_SYSTEM, GPIO.OUT)
    GPIO.output(GPIO_X728_SYSTEM, 0)


def detect_ups(bus) -> bool:
    detected = False
    try:
        bus.read_byte(I2C_ADDRESS)
        detected = True
    except Exception as e:
        print(e)
    return detected


def detect_pld() -> bool:
    return bool(GPIO.input(GPIO_X728_PLD))


def request_shutdown():
    """
    Request system shutdown of both the X728 and Raspberry Pi

    Holding GPIO 13 high for 1500ms to no longer than 6400ms will put the UPS into
    a mode where it expects to see GPIO 12 go low within ~50 seconds. At that point
    it will shut off the power to the RPi after ~5300ms.

    When the RPi shuts down and the kernel is put into power-off mode, GPIO 12 will be set low.

    Therefore the UPS will power off the Raspberry Pi after shutdown has completed.
    """
    logger.info("Request shutdown")
    GPIO.output(GPIO_X728_SYSTEM, 1)
    time.sleep(SYSTEM_SHUTDOWN_REQUEST_DURATION)
    GPIO.output(GPIO_X728_SYSTEM, 0)
    while True:
        pass


def log_data(log_queue, voltage, capacity):
    logger.info(f"Voltage {voltage:0.2f}V, capacity {capacity:0.1f}%")
    log_queue.put((f"{MQTT_ROOT}/voltage", voltage))
    log_queue.put((f"{MQTT_ROOT}/capacity", capacity))


def log_event(log_queue, reason):
    logger.warning(reason)
    log_queue.put((f"{MQTT_ROOT}/event", reason))


def do_command(command):
    logger.info(f"Execute: {command}")
    subprocess.run(command.split(), shell=False)


def do_sync():
    do_command("sync")


def do_docker_stop():
    # don't actually do this because they won't automatically start again on next boot
    #do_command(f"docker-compose --file {DOCKER_COMPOSE_FILE} stop")
    pass


def do_shutdown():
    do_docker_stop()
    do_sync()
    do_command("sudo /sbin/shutdown -h now")


def do_reboot():
    do_docker_stop()
    do_sync()
    do_command("sudo /sbin/shutdown -r now")


def monitor_shutdown(log_queue):
    logger.info("monitor_shutdown started")

    while True:
        if GPIO.input(GPIO_X728_SHUTDOWN) == 0:
            time.sleep(0.1)
        else:
            pulse_start = current_time_ms()
            logger.debug("pulse detected")
            while GPIO.input(GPIO_X728_SHUTDOWN) == 1:
                time.sleep(0.02)
                if current_time_ms() - pulse_start > REBOOT_PULSE_MAXIMUM:
                    log_event(log_queue, "Shutdown requested, halting RPi...")
                    time.sleep(5)  # give enough time for MQTT messages to be logged
                    do_shutdown()
                    while True: pass
            if current_time_ms() - pulse_start > REBOOT_PULSE_MINIMUM:
                log_event(log_queue, "Reboot requested, restarting RPi...")
                time.sleep(5)  # give enough time for MQTT messages to be logged
                do_reboot()
                while True: pass


def check_power(log_queue, pld, ups_pld, wait_start):
    """
    Return the time when the power was last good.
    """
    if pld:
        if not ups_pld:
            log_event(log_queue, f"Power loss detected - shutting down in {SHUTDOWN_SECONDS} seconds")
    else:
        if ups_pld:
            log_event(log_queue, "Power restored")
        wait_start = time.time()
    return wait_start


def check_conditions(log_queue, ups_pld, wait_start, voltage, capacity):
    if ups_pld and (time.time() - wait_start > SHUTDOWN_SECONDS):
        log_event(log_queue, f"Power loss detected for at least {SHUTDOWN_SECONDS} seconds - request shutdown now")
        request_shutdown()

    if voltage < SHUTDOWN_BATTERY_VOLTAGE:
        log_event(log_queue, f"Voltage {voltage:0.2f}V below threshold {SHUTDOWN_BATTERY_VOLTAGE:0.2f}V - request shutdown now")
        request_shutdown()

    if capacity < SHUTDOWN_BATTERY_CAPACITY:
        log_event(log_queue, f"Capacity {capacity:0.1f}% below threshold {SHUTDOWN_BATTERY_CAPACITY:0.1f}% - request shutdown now")
        request_shutdown()


def monitor_pld(log_queue, bus):
    logger.info("monitor_pld started")

    last_ups_detected = False
    last_pld = False
    wait_start = sys.float_info.max

    count = 0
    while True:
        ups_detected = detect_ups(bus)

        if ups_detected:
            if not last_ups_detected:
                log_event(log_queue, "UPS detected")

            pld = detect_pld()

            wait_start = check_power(log_queue, pld, last_pld, wait_start)
            last_pld = pld

            voltage = read_voltage(bus)
            capacity = read_capacity(bus)
            #logger.debug(f"Voltage: {voltage:0.2f}V")
            #logger.debug(f"Capacity: {capacity:0.1f}%")

            check_conditions(log_queue, last_pld, wait_start, voltage, capacity)

            if count % DATA_SEND_PERIOD == 0:
                log_data(log_queue, voltage, capacity)
            count += 1

        else:
            if last_ups_detected:
                log_event(log_queue, "UPS not detected")

        last_ups_detected = ups_detected

        time.sleep(1)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('-d', '--debug',   action="store_const", dest="loglevel", const=logging.DEBUG,     default=logging.WARNING, help="Show debug output")
    parser.add_argument('-v', '--verbose', action="store_const", dest="loglevel", const=logging.INFO,                               help="Show more output")
    args = parser.parse_args()

    logging.basicConfig(level=args.loglevel)

    init_gpio()
    bus = init_smbus(I2C_BUS_ID)

    log_queue = multiprocessing.Queue()

    p1 = multiprocessing.Process(target=monitor_shutdown, args=(log_queue,))
    p2 = multiprocessing.Process(target=monitor_pld, args=(log_queue, bus, ))

    p1.start()
    p2.start()

    mqtt_client = mqtt.init(MQTT_SERVER_ADDRESS, MQTT_SERVER_PORT)

    count = 0
    while True:
        count += 1
        if count % MQTT_RETRY_PERIOD == MQTT_RETRY_PERIOD - 1:
            mqtt.attempt_mqtt_reconnect(mqtt_client)

        topic, payload = log_queue.get()
        logger.debug(f"mqtt: topic {topic}, payload {payload}")
        mqtt_client.publish(topic, payload=payload, qos=0, retain=False)

    p1.join()
    p2.join()




if __name__ == '__main__':
    main()
