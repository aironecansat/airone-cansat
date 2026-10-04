#  AirOne CanSat

**AirOne** is an experimental CanSat system designed to collect, process, record, and transmit atmospheric and environmental data during flight.

The system combines an onboard flight computer, environmental sensors, navigation, radiation measurement, long-range telemetry, redundant data storage, and a dedicated ground station into a single integrated platform.

The project is designed around reliable data collection throughout the flight, with particular focus on **altitude reconstruction, atmospheric measurements, radiation, and air-quality data**.

---

## Mission

AirOne's primary objective is to collect atmospheric measurements during ascent and descent while maintaining a reliable telemetry link to the ground station.

The system records measurements from multiple sensors and associates them with time and altitude, allowing the flight data to be analysed after recovery.

### Primary measurements

- Atmospheric pressure
- Temperature
- Relative humidity
- Altitude
- GPS position
- Air-quality / VOC data
- Ultraviolet radiation
- Ambient light
- Magnetic field
- Acceleration and motion
- Ionising radiation
- Battery voltage
- Radio signal strength

The collected data can be used to reconstruct the flight profile and investigate how environmental conditions change with altitude.

---

## System Overview

AirOne is divided into two main software systems:

```text
                    ┌─────────────────────────┐
                    │       AirOne CanSat     │
                    │                         │
                    │       ESP32 Flight      │
                    │       Computer           │
                    └────────────┬────────────┘
                                 │
                    ┌────────────▼────────────┐
                    │       Sensors            │
                    │                          │
                    │  BME688                  │
                    │  BMP581                  │
                    │  SGP41 / ENS160          │
                    │  VEML6075 / OPT3001      │
                    │  MMC5603 / BMI270        │
                    │  MAX-M10S GNSS           │
                    │  SEN0463 Geiger          │
                    │  INA219 / DS3231         │
                    └────────────┬────────────┘
                                 │
                                 ▼
                         Telemetry Packet
                                 │
                                 │ LoRa
                                 ▼
                    ┌─────────────────────────┐
                    │      Ground Station     │
                    │                          │
                    │  Laptop / PC             │
                    │  Telemetry Interface     │
                    │  Data Logging             │
                    │  Flight Visualisation   │
                    └─────────────────────────┘
```

---

## Onboard System

The onboard electronics are centred around an **ESP32-WROVER-E**, which manages sensor acquisition, flight-state detection, telemetry, storage, and system monitoring.

### Flight computer

- ESP32-WROVER-E
- Deterministic flight software
- Sensor management
- Telemetry generation
- Flight-state management
- Data logging
- System diagnostics

The flight software uses a **single-threaded state-machine architecture** to keep the flight sequence predictable and straightforward to verify.

---

## Sensors

AirOne integrates multiple sensors for atmospheric, environmental, navigation, motion, and radiation measurements.

| Sensor | Measurement |
|---|---|
| BME688 | Temperature, humidity, pressure and gas/VOC information |
| BMP581 | High-resolution atmospheric pressure / altitude reconstruction |
| SGP41 | VOC / NOx index |
| ENS160 | TVOC, eCO₂ and air-quality index |
| VEML6075 | UVA / UVB |
| OPT3001 | Ambient light |
| MMC5603 | Magnetic field |
| BMI270 | Acceleration and rotation (spin) |
| MAX-M10S | GNSS position and timing |
| SEN0463 | Ionising radiation (Geiger counter) |
| INA219 | Battery voltage and current |
| DS3231 | Real-time clock (timestamp backup) |

All I²C sensors are connected through a PCA9548A I²C multiplexer. Full pin map: [`AirOne_Firmware_CanSat/AirOne_Firmware_CanSat/README.md`](AirOne_Firmware_CanSat/AirOne_Firmware_CanSat/README.md).

The pressure sensors are particularly important for **altitude reconstruction**, allowing the flight profile to be analysed even when GNSS altitude data is unavailable or unreliable.

---

## Communications

AirOne uses a LoRa-based telemetry link for communication between the CanSat and ground station.

### Airborne

- EBYTE E22 LoRa module
- 433 MHz telemetry link
- External / routed antenna system
- Periodic telemetry transmission

### Ground

- E22 LoRa receiver
- USB/UART interface
- Laptop ground station
- Live telemetry display
- Data logging

Telemetry is designed to be transmitted continuously during flight while also being stored locally onboard.

---

## Telemetry

Each telemetry packet contains the measurements required to reconstruct and analyse the flight.

Typical telemetry data includes:

```text
Packet ID
Timestamp
GPS Latitude
GPS Longitude
Altitude
Temperature
Pressure
Humidity
VOC / Air Quality
Radiation CPM
UV Index
Battery Voltage
RSSI
CRC
```

Telemetry packets are transmitted to the ground station and recorded for later analysis.

---

## Data Storage

AirOne uses redundant onboard data storage to reduce the risk of losing mission data.

### Primary storage

**Industrial MicroSD card**

Used for full-flight telemetry logging.

### Backup storage

**MB85RC512 FRAM**

Stores the packet sequence number and mission state so the CanSat resumes correctly after a brownout or reset.

This provides a second storage path in case the primary storage system becomes unavailable.

Important mission information can therefore remain available even after a partial storage failure.

---

## Flight Software

The onboard software is organised around a deterministic flight state machine.

```text
BOOT
  │
  ▼
SELF_TEST
  │
  ▼
PRELAUNCH
  │
  ▼
ASCENT
  │
  ▼
APOGEE
  │
  ▼
DESCENT
  │
  ▼
LANDED
```

Each state has a defined purpose and allows the system to respond appropriately to the current phase of the mission.

### Flight software responsibilities

- Initialise hardware
- Check sensors
- Acquire measurements
- Monitor battery
- Determine flight state
- Generate telemetry
- Store telemetry
- Monitor GNSS
- Monitor communications
- Detect landing
- Maintain mission state

---

## Altitude Reconstruction

Altitude is an important part of the AirOne mission.

The system uses atmospheric pressure measurements to reconstruct altitude throughout the flight.

The BMP581 provides high-resolution pressure data, while temperature and other environmental measurements provide additional information for analysing atmospheric conditions.

The resulting data can be used to produce:

- Altitude vs. time
- Pressure vs. time
- Temperature vs. altitude
- Humidity vs. altitude
- VOC vs. altitude
- Radiation vs. altitude
- Descent-rate analysis

This allows the mission data to be analysed as a complete flight profile rather than as independent sensor readings.

---

## Secondary Mission — AirChem-Rad

AirChem-Rad uses the sensors already on board to build one time-aligned **vertical profile** of air chemistry (BME688, SGP41, ENS160), UV light (VEML6075, OPT3001) and ionising radiation (SEN0463) during ascent and descent, plus descent spin dynamics from the BMI270 and MMC5603. On the ground the data is used to test whether the boundary layer is chemically different from the air above it and whether UV and radiation follow their expected altitude trends.

Full mission plan: [`SECONDARY_MISSION.md`](SECONDARY_MISSION.md) (also as PDF / DOCX).

---

## Ground Station

The ground station runs on a laptop or desktop computer and provides the interface between the CanSat and the operator.

The software is designed to provide:

- Live telemetry
- Sensor monitoring
- Flight status
- Packet statistics
- Data logging
- Flight replay
- Graphing
- GPS information
- Offline flight analysis

The ground station receives the radio telemetry through the ground LoRa module and processes the incoming packets for display and storage.

---

## Repository Structure

```text
airone-cansat/
│
├── AirOne_Firmware_CanSat/
│   └── AirOne_Firmware_CanSat/
│       ├── airone_cansat/          ESP32 flight firmware (Arduino sketch)
│       ├── docs/                   Wiring, protocol and security docs
│       └── README.md               Firmware build + pin map
│
├── AirOne_Software_Laptop/
│   └── AirOne_Software_Laptop/
│       ├── src/                    Ground station (pipeline, API, GUI, science, ML)
│       ├── tests/                  Test suite (pytest)
│       ├── docs/                   Operator / engineering manuals
│       ├── requirements.txt
│       └── README.md               Ground station setup
│
├── SECONDARY_MISSION.md            Secondary mission (AirChem-Rad)
└── README.md
```

The repository currently separates the **onboard flight software** from the **laptop ground-station software**.

---

## Getting Started

### 1. Clone the repository

```bash
git clone https://github.com/aironecansat/airone-cansat.git
cd airone-cansat
```

### 2. Flight firmware

Navigate to:

```text
AirOne_Firmware_CanSat/AirOne_Firmware_CanSat/airone_cansat/
```

Open `airone_cansat.ino` in the Arduino IDE, select **ESP32 Wrover Module**, install the libraries listed in the firmware README, then compile and upload.

> The firmware has not yet been compiled or flown on the real hardware — bench-test every sensor before flight.

Before flight, verify:

- Correct board selection
- Serial configuration
- Sensor connections
- GNSS connection
- LoRa connection
- Storage hardware
- Battery monitoring
- Telemetry configuration

### 3. Ground station

Navigate to:

```text
AirOne_Software_Laptop/AirOne_Software_Laptop/
```

```bash
cd AirOne_Software_Laptop/AirOne_Software_Laptop
pip install -r requirements.txt
cp .env.example .env               # then set AIRONE_JWT_SECRET
python launcher.py --validate-only # check the installation
python launcher.py --simulate --gui   # simulated flight with the GUI
python -m pytest -q                # run the test suite
```

To use the real LoRa receiver: `python launcher.py --serial-port /dev/ttyUSB0 --baud 115200 --force-baud` (see the ground station README).

The ground station should be configured for the serial port associated with the connected LoRa receiver.

---

## Flight Data Flow

```text
Sensors
   │
   ▼
ESP32
   │
   ├──────────────► MicroSD
   │
   ├──────────────► FRAM backup
   │
   ▼
Telemetry Packet
   │
   ▼
LoRa
   │
   ▼
Ground LoRa Receiver
   │
   ▼
Laptop
   │
   ├──────────────► Live Display
   │
   ├──────────────► Graphs
   │
   ├──────────────► Data Logger
   │
   └──────────────► Flight Analysis
```

---

## Reliability

AirOne is designed with redundancy and fault tolerance in mind.

Key features include:

- Local onboard data logging
- FRAM backup storage
- GNSS position and timing
- Pressure-based altitude reconstruction
- Radio telemetry
- CRC-protected telemetry packets
- Battery monitoring
- Flight-state management
- Independent ground-side data logging

The goal is to ensure that useful mission data remains available even if individual subsystems experience problems during flight.

---

## Hardware

The complete hardware design includes:

- Custom PCB system
- ESP32 flight computer
- Environmental sensors
- GNSS receiver
- LoRa telemetry
- Radiation detector
- Battery and power-management circuitry
- Data storage
- Status indicators
- Recovery electronics
- Antenna system
- Mechanical enclosure

The hardware is designed around the standard CanSat form factor while providing sufficient separation between sensitive sensors, communications electronics, power electronics, and the radiation subsystem.

---

## Mission Data Analysis

After the flight, recorded telemetry can be used to reconstruct the complete mission.

Example analysis:

```text
             ALTITUDE
                ▲
                │       /\
                │      /  \
                │     /    \
                │    /      \
                │   /        \
                │  /          \
                │ /            \
                │/              \
                └──────────────────► TIME
                     ASCENT  DESCENT
```

The resulting dataset can be used to investigate relationships such as:

**Altitude ↔ Pressure**

**Altitude ↔ Temperature**

**Altitude ↔ Humidity**

**Altitude ↔ VOC concentration**

**Altitude ↔ Radiation**

This forms the basis for post-flight scientific analysis.

---

## Project Goals

AirOne aims to demonstrate that a compact CanSat platform can combine:

- Scientific instrumentation
- Autonomous flight software
- Reliable telemetry
- Redundant storage
- GNSS navigation
- Environmental sensing
- Radiation measurement
- Ground-based data analysis

while remaining within the physical constraints of a CanSat.

---

## Status

AirOne is an active engineering project covering:

- Flight software
- Ground-station software
- Electronics
- Sensors
- Telemetry
- Data logging
- Mechanical integration
- Flight testing
- Mission analysis

The repository contains the software required for the onboard and ground portions of the system.

---

## License

See the `LICENSE` file in each project folder.

---

## AirOne

**Compact platform.  
Reliable telemetry.  
Scientific data.  
One complete CanSat system.** 🛰️