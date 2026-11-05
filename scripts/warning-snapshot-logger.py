#!/usr/bin/env python3
"""
MQTT warning snapshot logger for JK BMS batteries.

⚠️  QUESTO SERVIZIO È TEMPORANEO  ⚠️
Serve solo per diagnosticare il bug dei falsi warning.
Quando il bug è RISOLTO e l'allarme non suona più ingiustamente:
   → systemctl --user stop y2k-warning-snapshot
   → systemctl --user disable y2k-warning-snapshot
Poi lo possiamo rimuovere.

Maintains a circular buffer of recent MQTT payloads per battery.
When warning=1 is detected, writes the full buffer to a log file.

The buffer captures ~3 minutes of data before the event, so we can
see exactly what was in the errors field (and all other fields) when
the alarm triggered, and what led up to it.

Usage:
  python3 warning-snapshot-logger.py

Service:
  systemctl --user start y2k-warning-snapshot
"""

import json
import os
import signal
import sys
import time
from collections import deque
from datetime import datetime, timezone

import paho.mqtt.client as mqtt

# --- Config ---
MQTT_BROKER = os.environ.get("MQTT_BROKER", "localhost")
MQTT_PORT = 1883
BASE_TOPIC = "vessels/self/electrical/batteries"
BATTERIES = ["Thor", "Poseidone", "Zeus", "Odino"]
BUFFER_SIZE = 36  # ~3 minuti a 5s di pubblicazione
LOG_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs")
LOG_FILE = os.path.join(LOG_DIR, "warning-snapshots.log")
WARNING_DEBOUNCE_S = 30  # non scrivere due snapshot ravvicinati per la stessa batteria

# Buffer circolare: per ogni batteria, gli ultimi BUFFER_SIZE messaggi
buffer: dict[str, deque] = {name: deque(maxlen=BUFFER_SIZE) for name in BATTERIES}
last_warning_ts: dict[str, float] = {}


def on_connect(client, userdata, flags, rc):
    if rc != 0:
        print(f"MQTT connection failed, rc={rc}", flush=True)
        return
    for name in BATTERIES:
        topic = f"{BASE_TOPIC}/{name}"
        client.subscribe(topic)
        print(f"Subscribed to {topic}", flush=True)


def on_message(client, userdata, msg):
    battery = msg.topic.split("/")[-1]
    if battery not in buffer:
        return

    try:
        data = json.loads(msg.payload)
    except json.JSONDecodeError:
        return

    # Inietta timestamp di arrivo
    data["_mqtt_ts"] = time.time()
    buffer[battery].append(data)

    # Verifica se questo messaggio ha warning attivo
    warning = data.get("warning", 0)
    if warning == 1:
        handle_warning(battery, data)


def handle_warning(battery, data):
    """Salva uno snapshot del buffer quando warning=1 viene pubblicato."""
    # Debounce: evita raffiche ravvicinate
    now = time.time()
    last = last_warning_ts.get(battery, 0.0)
    if now - last < WARNING_DEBOUNCE_S:
        return
    last_warning_ts[battery] = now

    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")

    snapshot = {
        "timestamp": ts,
        "trigger": {"battery": battery, "data": data},
        "buffer": {
            name: list(buf) for name, buf in buffer.items()
        },
    }

    with open(LOG_FILE, "a") as f:
        f.write(json.dumps(snapshot, default=str) + "\n")

    errors = data.get("errors", "")
    soc = data.get("soc", "?")
    print(
        f"[{ts}] WARNING from {battery}: errors=[{errors}] "
        f"soc={soc} online={data.get('online')}",
        flush=True,
    )


def main():
    os.makedirs(LOG_DIR, exist_ok=True)

    # Segnala su file che è partito (utile per debug)
    startup = {
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        "event": "startup",
        "config": {"buffer_size": BUFFER_SIZE, "debounce_s": WARNING_DEBOUNCE_S},
    }
    with open(LOG_FILE, "a") as f:
        f.write(json.dumps(startup) + "\n")
    print(f"warning-snapshot-logger started, writing to {LOG_FILE}", flush=True)

    client = mqtt.Client()
    client.on_connect = on_connect
    client.on_message = on_message

    def shutdown(sig, frame):
        print("Shutting down...", flush=True)
        client.disconnect()
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    client.connect(MQTT_BROKER, MQTT_PORT, 60)
    client.loop_forever()


if __name__ == "__main__":
    main()
