#!/usr/bin/env python3
"""BMS offline event logger.

Subscribes to the 4 battery MQTT topics and writes a timestamped
log line whenever a BMS transitions between online and offline.
Runs indefinitely as a background process.
"""

import json
import os
import time
from datetime import datetime, timezone

import paho.mqtt.client as mqtt

BROKER = os.environ.get("MQTT_BROKER", "localhost")
PORT = 1883
TOPICS = [
    "vessels/self/electrical/batteries/Thor",
    "vessels/self/electrical/batteries/Poseidone",
    "vessels/self/electrical/batteries/Zeus",
    "vessels/self/electrical/batteries/Odino",
]
LOG_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs")
LOG_FILE = os.path.join(LOG_DIR, "bms-events.log")

# Ensure log directory exists
os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)

# Track current online state for each battery
states: dict[str, bool | None] = {}  # name -> bool or None (initial)


def log_event(bms: str, event: str, offline_count: int):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    line = f"{ts} | {bms:10s} | {event:8s} | offline_count={offline_count}\n"
    with open(LOG_FILE, "a") as f:
        f.write(line)
    # Also print to stdout so journald/systemd can capture it
    print(line.strip())


def on_connect(client, userdata, flags, reason_code, properties=None):
    for topic in TOPICS:
        client.subscribe(topic)
    print(f"Connected to MQTT broker {BROKER}, subscribed to {len(TOPICS)} topics")


def on_message(client, userdata, msg):
    try:
        payload = json.loads(msg.payload.decode())
    except (json.JSONDecodeError, UnicodeDecodeError):
        return

    name = payload.get("name")
    online = payload.get("online")
    if name is None or online is None:
        return

    prev = states.get(name)
    if prev is not None and prev == online:
        return  # No change

    states[name] = online

    # Count how many are currently offline
    offline_count = sum(1 for v in states.values() if v is False)

    event = "online" if online else "offline"
    log_event(name, event, offline_count)


def main():
    os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    client.on_connect = on_connect
    client.on_message = on_message

    client.connect(BROKER, PORT, 60)
    print(f"BMS event logger started, writing to {LOG_FILE}")
    client.loop_forever()


if __name__ == "__main__":
    main()
