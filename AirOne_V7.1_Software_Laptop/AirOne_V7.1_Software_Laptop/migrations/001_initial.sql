-- AirOne V7.1 initial schema (SQLite)
-- All timestamps stored as ISO-8601 UTC strings.

-- Raw telemetry (immutable, never overwritten)
CREATE TABLE IF NOT EXISTS raw_telemetry (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    received_at TEXT NOT NULL,
    source TEXT NOT NULL,
    receiver_id TEXT,
    rssi INTEGER,
    snr REAL,
    decode_status TEXT,
    crc_valid INTEGER,
    fec_applied INTEGER,
    fec_repaired INTEGER,
    packet_seq INTEGER,
    mission_phase TEXT,
    firmware_version TEXT,
    raw_bytes BLOB NOT NULL
);

-- Processed measurements (Measurement dataclass records)
CREATE TABLE IF NOT EXISTS measurements (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    raw_telemetry_id INTEGER REFERENCES raw_telemetry(id),
    sensor_id TEXT NOT NULL,
    field_name TEXT NOT NULL,
    value REAL,
    unit TEXT,
    timestamp TEXT NOT NULL,
    quality TEXT NOT NULL,
    valid INTEGER NOT NULL,
    uncertainty REAL,
    calibration_version TEXT,
    source TEXT NOT NULL,
    processing_stage INTEGER,
    mission_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_measurements_sensor_ts
    ON measurements (sensor_id, timestamp);

-- Mission state transitions
CREATE TABLE IF NOT EXISTS mission_transitions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mission_id TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    prev_state TEXT NOT NULL,
    new_state TEXT NOT NULL,
    reason TEXT,
    confidence REAL,
    supporting_data TEXT
);
CREATE INDEX IF NOT EXISTS idx_transitions_mission
    ON mission_transitions (mission_id, timestamp);

-- Events
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mission_id TEXT,
    timestamp TEXT NOT NULL,
    event_type TEXT NOT NULL,
    severity TEXT NOT NULL,
    source TEXT NOT NULL,
    message TEXT NOT NULL,
    details TEXT,
    acknowledged INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events (timestamp);

-- Audit log (separate table, append-only)
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    event_type TEXT NOT NULL,
    user_id TEXT,
    role TEXT,
    ip TEXT,
    endpoint TEXT,
    result TEXT,
    details TEXT,
    entry_checksum TEXT
);

-- ML model registry
CREATE TABLE IF NOT EXISTS ml_models (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    version TEXT NOT NULL,
    model_type TEXT NOT NULL,
    file_path TEXT,
    sha256_hash TEXT,
    training_data_hash TEXT,
    features TEXT,
    parameters TEXT,
    metrics TEXT,
    created_at TEXT,
    created_by TEXT,
    is_active INTEGER DEFAULT 0
);

-- Scientific analysis results
CREATE TABLE IF NOT EXISTS analysis_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mission_id TEXT,
    analysis_name TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    result_data TEXT,
    method TEXT,
    inputs TEXT,
    assumptions TEXT,
    uncertainty TEXT,
    confidence REAL,
    limitations TEXT
);

-- System health snapshots
CREATE TABLE IF NOT EXISTS system_health (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    cpu_percent REAL,
    ram_percent REAL,
    disk_percent REAL,
    telemetry_rate REAL,
    packet_loss_rate REAL,
    queue_depth INTEGER,
    api_latency_ms REAL,
    active_threads INTEGER
);
