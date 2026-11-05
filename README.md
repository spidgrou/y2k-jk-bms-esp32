# ESP32-S3 BLE bridge for JK BMS → MQTT

Read the data of up to **four JK BMS** units over BLE with a single ESP32-S3 and
publish them as **one compact JSON payload per battery** to MQTT.

Built for a 4× LiFePO4 house bank on a liveaboard sailboat, where the battery
monitor (a Victron Cerbo GX, or any small Linux gateway) could not keep four
concurrent BLE connections alive: the links dropped, reconnected, and the data
arrived with holes. The ESP32-S3 handles the BLE side natively, so every other
consumer on board — Signal K, Node-RED, InfluxDB, Grafana — only needs plain
MQTT.

```
   JK BMS #1 ──BLE──┐
   JK BMS #2 ──BLE──┤
   JK BMS #3 ──BLE──┼── ESP32-S3 ──WiFi/MQTT──> broker (JSON every 5 s)
   JK BMS #4 ──BLE──┘
```

## Requirements

- **ESP32-S3** with 8 MB PSRAM (tested: N16R8 module, dual USB-C). Board id
  `esp32-s3-devkitc-1`, PSRAM in octal mode at 80 MHz.
- **ESPHome** 2026.5.3 or newer.
- The [`syssi/esphome-jk-bms`](https://github.com/syssi/esphome-jk-bms) external
  component (pulled automatically by ESPHome, `JK02_32S` protocol, for JK BMS
  hardware revision 11.0+).
- An MQTT broker reachable from the ESP32.

The Arduino framework is used on purpose: a full build takes ~45 s instead of
several minutes with ESP-IDF.

## Quick start

```bash
git clone https://github.com/spidgrou/y2k-jk-bms-esp32.git
cd y2k-jk-bms-esp32

cp secrets.yaml.example secrets.yaml
$EDITOR secrets.yaml          # WiFi, OTA password, broker, 4 BLE MAC addresses

esphome config jk-bms-bridge.yaml    # validates substitutions and secrets
esphome run jk-bms-bridge.yaml       # first flash over USB, later OTA
```

`secrets.yaml` is git-ignored — never commit it.

### Finding the BMS MAC addresses

The JK app shows the address of each unit, or use any BLE scanner
(`bleak`, `nRF Connect`, `bluetoothctl`). If the BMS uses a random/private
address, disable address randomisation in the JK app before pairing.

### First USB flash — download mode

Hold **BOOT**, press **RST**, release **BOOT**. On Linux the port needs write
access: `sudo chmod 666 /dev/ttyACM0`.

## Configuration

| Where | What |
|---|---|
| `secrets.yaml` | `wifi_ssid`, `wifi_password`, `ota_password`, `mqtt_broker`, `bms1_mac` … `bms4_mac` |
| `jk-bms-bridge.yaml` | battery labels, MQTT base topic, polling interval (5 s), warning logic |

Battery labels (`Thor`, `Poseidone`, `Zeus`, `Odino` in the shipped config) are
only names: they appear in the MQTT topic and in the JSON `name` field. Each
battery needs its own `ble_client` + `jk_bms_ble` block and its own `interval`
lambda, because every sensor id is battery-specific. To use fewer than four
units, delete a whole block (client, BMS, sensors, interval) — nothing else
depends on the count, except the "3+ offline" rule described below.

`esp32_ble.max_connections: 4` is what allows four simultaneous links; lower it
if you use fewer batteries.

## MQTT output

Topic: `vessels/self/electrical/batteries/<Name>`

```json
{
  "voltage": 13.081, "current": -1.599, "power": -20.92, "soc": 61,
  "capacityRemaining": 186.68, "fullChargeCapacity": 304,
  "minCellVoltage": 3.269, "maxCellVoltage": 3.271, "deltaCellVoltage": 0.002,
  "cell1": 3.271, "cell2": 3.271, "cell3": 3.271, "cell4": 3.269,
  "temp1": 20.5, "temp2": 20.7, "mosfetTemp": 23.0,
  "cycles": 0, "runtimeS": 419441, "errors": "", "warning": 0,
  "name": "Thor", "charging": true, "discharging": true,
  "balancing": false, "heating": false, "online": true
}
```

| Field | Type | Meaning |
|---|---|---|
| `voltage` | float | Pack voltage (V) |
| `current` | float | Pack current, negative = discharge (A) |
| `power` | float | Instantaneous power (W) |
| `soc` | int | State of charge (%) |
| `capacityRemaining` | float | Remaining capacity (Ah) |
| `fullChargeCapacity` | int | Nominal capacity (Ah) |
| `minCellVoltage` / `maxCellVoltage` | float | Lowest / highest cell (V) |
| `deltaCellVoltage` | float | max − min (V) |
| `cell1` … `cell4` | float | Individual cell voltages (4S pack) |
| `temp1`, `temp2` | float | Temperature sensors (°C) |
| `mosfetTemp` | float | MOSFET temperature (°C) |
| `cycles` | int | Charge cycles |
| `runtimeS` | int | BMS uptime (s) |
| `errors` | string | Active alarms, empty = OK (`"Offline"` means "BLE link lost") |
| `warning` | int | `0` = OK, `1` = alarm (see below) |
| `name` | string | Battery label |
| `charging`, `discharging` | bool | Charge / discharge MOSFET state |
| `balancing`, `heating` | bool | Balancing / heating active |
| `online` | bool | BLE link up |

All individual ESPHome sensors are declared `internal: true`: only this single
JSON document is published, which keeps the broker clean and gives consumers an
atomic snapshot instead of a burst of 26 separate messages.

While the bridge is paused for maintenance the payload is reduced to
`{"name": "...", "maintenance": true, "online": false}` so consumers can tell
"paused" apart from "dead".

### Alarm logic (`warning`)

| Priority | Condition | `warning` |
|---|---|---|
| 1 | `errors` is non-empty **and** the BMS is online and the value is not `"Offline"` | `1` — immediate |
| 2 | 3 or more batteries offline at the same time (ESP down, WiFi down, broker gone) | `1` — immediate |
| — | A single battery offline on its own | `0` — no alarm |

Two details that cost real debugging time and are therefore handled in code:

- the `jk_bms_ble` component writes the literal string `"Offline"` into the
  `errors` sensor when the BLE link drops. Without filtering it, every
  disconnection looks like a BMS alarm. Hence `err != "Offline"`.
- `online` flips to `true` a few seconds before `errors` is cleared on
  reconnection, so `online && !errors.empty()` alone produces a false alarm
  right after every reconnect. Hence the explicit string comparison **and** the
  cooldowns below.

Cooldowns: `maintenance_cooldown` and `boot_cooldown` (48 ticks ÷ 4 batteries ×
5 s ≈ 60 s) suppress `warning` after a reboot and around maintenance-mode
transitions, while the BLE links come back one by one.

## Maintenance mode

Sometimes you need the JK app on your phone, which means the ESP32 must let go
of the BLE links.

| Topic | Payload | Effect |
|---|---|---|
| `…/batteries/maintenance` | `1` / `0` | Disconnect / reconnect **all** batteries |
| `…/batteries/<Name>/maintenance` | `1` / `0` | Disconnect / reconnect **one** battery |
| `…/batteries/status` | read-only | Current bridge state as JSON |

```bash
mosquitto_pub -h <broker> -t "vessels/self/electrical/batteries/maintenance" -m 1
mosquitto_pub -h <broker> -t "vessels/self/electrical/batteries/maintenance" -m 0
```

While paused, each battery publishes `{"name": "...", "maintenance": true,
"online": false}` so consumers can distinguish "paused" from "dead". The flags
are not persisted: a reboot brings the bridge back in normal mode.

## Helper scripts

Optional, they run on the MQTT host — not on the ESP32. Install the dependency
with `pip install -r requirements.txt`; the broker is read from `MQTT_BROKER`
(default `localhost`).

- `scripts/bms-event-logger.py` — appends an `online`/`offline` transition line
  per battery with UTC timestamp and simultaneous-offline count.
- `scripts/warning-snapshot-logger.py` — keeps a rolling ~3 minute telemetry
  buffer per battery and dumps it to a file when `warning=1` arrives, so you can
  see what led to an alarm (MQTT itself keeps no history).
- `systemd/bms-warning-snapshot.service` — user unit for the snapshot logger;
  adjust the path for your install.

## Troubleshooting

| Symptom | Cause |
|---|---|
| Alarm right after every reconnect | `errors` still holds `"Offline"` for 1–2 poll cycles — the filter above must be present in the firmware that is actually running |
| Alarm right after reboot | no `boot_cooldown` in the running firmware |
| Changes have no effect | `esphome compile` succeeded but no OTA was done — the ESP still runs the old firmware |
| Full rebuild takes minutes | never delete `src/main.cpp`; incremental builds are ~45 s, a full rebuild 5+ min |
| One battery flaps | weak BLE path — shorten the distance, or check the antenna; each link is polled every 5 s |

## Limitations

- Cell voltages are exposed for a 4S pack. Other configurations need the sensor
  block adapted to your BMS (`cell_count`, per-cell sensors).
- No discovery: topics are fixed, consumers subscribe to
  `vessels/self/electrical/batteries/+`.
- No authentication on MQTT — keep the broker on a trusted network.
- The "3+ offline" rule assumes four batteries; with fewer, adjust the
  threshold.

## License

MIT — see [LICENSE](LICENSE).

Credits: the JK BMS protocol implementation comes from
[`syssi/esphome-jk-bms`](https://github.com/syssi/esphome-jk-bms), used here as
an external component.
