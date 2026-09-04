"""Telemetry-input hardening, audit-chain and launcher bootstrap tests.

Authored by Team AirOne.

Covers the controls introduced by the security audit:

* header pre-checks (VERSION / PACKET_TYPE / FLAGS / LENGTH) with counters,
* HMAC-SHA256 link authentication (accept / reject / UNVERIFIABLE / required),
* replay flagging on sequence regression,
* hostile JSON payloads (size, depth, type, non-object) never raise and never
  produce fabricated VALID measurements,
* pipeline AuthenticateStage downgrades,
* simulated frames are authenticated when a key is configured,
* API bind-address resolution defaults to loopback,
* audit log hash-chain verification (tamper detection, cross-restart chain, CLI),
* launcher account bootstrap commands.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timezone

import pytest

from src.data_processing.pipeline import AuthenticateStage, PipelineContext
from src.security.audit import AuditEvent, AuditLogger, verify_audit_file
from src.simulation import FlightProfile, MissionSimulator, SimulatedPacketSource
from src.telemetry import protocol
from src.telemetry.parser import StreamParser
from src.workers.orchestrator import resolve_api_host
from src.workers.packet_processor import (
    MAX_JSON_BYTES,
    decode_payload_to_frame,
)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KEY = bytes(range(32))
OTHER_KEY = bytes(range(32, 64))
TS = int(datetime(2026, 9, 4, tzinfo=timezone.utc).timestamp() * 1e6)


def _frame(seq: int, payload: bytes = b'{"altitude": 1.0}', key=None, ptype=int(protocol.PacketType.SENSOR_DATA)):
    return protocol.pack_frame(ptype, seq, TS, payload, link_key=key)


# --- header pre-checks -----------------------------------------------------------
def test_parser_counts_bad_version_type_flags_and_length():
    good = bytearray(_frame(1))
    bad_version = bytearray(good)
    bad_version[4] = 0x70
    bad_type = bytearray(good)
    bad_type[5] = 0xEE
    bad_flags = bytearray(good)
    bad_flags[20] = 0x80
    parser = StreamParser()
    pkts = parser.parse_stream(bytes(bad_version) + bytes(bad_type) + bytes(bad_flags) + bytes(good))
    valid = [p for p in pkts if p.crc_valid]
    assert len(valid) == 1 and valid[0].sequence == 1
    c = parser.counters()
    assert c["invalid_version"] == 1
    assert c["invalid_type"] == 1
    assert c["invalid_flags"] == 1
    assert c["frames_parsed"] == 1
    for key in ("invalid_length", "replays", "auth_failures", "unauthenticated", "rejected"):
        assert key in c


def test_parser_flags_sequence_regression_as_replay():
    parser = StreamParser(order_tolerance=2)
    stream = b"".join(_frame(s) for s in (100, 101, 102, 103))
    parser.parse_stream(stream)
    replayed = parser.parse_stream(_frame(5))  # far behind, not in dedup window
    assert len(replayed) == 1
    assert replayed[0].replay is True and replayed[0].out_of_order is True
    assert parser.counters()["replays"] == 1


# --- link authentication ---------------------------------------------------------
def test_authenticated_frame_verifies_and_tamper_is_rejected():
    parser = StreamParser(link_key=KEY)
    ok = parser.parse_stream(_frame(1, key=KEY))
    assert ok[0].auth_state == "AUTHENTICATED" and ok[0].authenticated and not ok[0].rejected

    wrong = StreamParser(link_key=OTHER_KEY).parse_stream(_frame(2, key=KEY))
    assert len(wrong) == 1
    assert wrong[0].crc_valid is False and wrong[0].auth_state == "INVALID_TAG" and wrong[0].rejected

    # Flipping a payload byte AND fixing the CRC still fails the tag.
    raw = bytearray(_frame(3, key=KEY))
    raw[protocol.HEADER_SIZE + 2] ^= 0x01
    body = bytes(raw[:-protocol.CRC_SIZE])
    raw[-protocol.CRC_SIZE:] = protocol.crc32(body).to_bytes(4, "little")
    tampered = StreamParser(link_key=KEY).parse_stream(bytes(raw))
    assert tampered[0].auth_state == "INVALID_TAG" and tampered[0].crc_valid is False
    with pytest.raises(protocol.AuthTagError):
        protocol.unpack_frame(bytes(raw), link_key=KEY)


def test_unverifiable_and_unauthenticated_states_are_explicit():
    # Tag present, but the receiver has no key: honest UNVERIFIABLE, not "ok".
    header, _ = protocol.unpack_frame(_frame(1, key=KEY))
    assert header["auth_state"] == "UNVERIFIABLE"
    # Key configured, frame has no tag: UNAUTHENTICATED (counted).
    parser = StreamParser(link_key=KEY)
    pkts = parser.parse_stream(_frame(2))
    assert pkts[0].auth_state == "UNAUTHENTICATED" and not pkts[0].rejected
    assert parser.counters()["unauthenticated"] == 1
    assert parser.auth_mode == "OPTIONAL"
    assert StreamParser().auth_mode == "NOT_CONFIGURED"


def test_require_auth_rejects_unauthenticated_frames():
    parser = StreamParser(link_key=KEY, require_auth=True)
    assert parser.auth_mode == "REQUIRED"
    pkts = parser.parse_stream(_frame(1) + _frame(2, key=KEY))
    assert [p.rejected for p in pkts] == [True, False]
    assert parser.counters()["rejected"] == 1
    with pytest.raises(ValueError):
        StreamParser(require_auth=True)
    with pytest.raises(ValueError):
        StreamParser(link_key=b"short")


def test_parse_link_key_validation():
    assert protocol.parse_link_key(None) is None
    assert protocol.parse_link_key("") is None
    assert protocol.parse_link_key(KEY.hex()) == KEY
    with pytest.raises(ValueError):
        protocol.parse_link_key("00ff")  # too short
    with pytest.raises(ValueError):
        protocol.parse_link_key("zz" * 16)  # not hex


# --- hostile payloads --------------------------------------------------------------
def _decode(payload: bytes, seq: int = 7):
    pkts = StreamParser().parse_stream(_frame(seq, payload))
    assert len(pkts) == 1
    return decode_payload_to_frame(pkts[0])


def test_oversized_payload_is_rejected_without_measurements():
    big = b'{"a": "' + b"x" * (MAX_JSON_BYTES + 10) + b'"}'
    frame = _decode(big)
    assert frame.measurements == {}
    assert "exceeds" in frame.metadata["payload_error"]


def test_deep_nesting_and_non_object_payloads_are_rejected():
    deep = json.dumps({"a": {"b": {"c": {"d": 1}}}}).encode()
    assert "depth" in _decode(deep).metadata["payload_error"]
    assert "object" in _decode(b"[1, 2, 3]").metadata["payload_error"]
    assert "JSON" in _decode(b"{not json").metadata["payload_error"]
    assert "UTF-8" in _decode(b"\xff\xfe{}").metadata["payload_error"]


def test_non_numeric_values_become_invalid_not_fabricated():
    frame = _decode(json.dumps({"altitude": "1e3", "temp": True, "ok": 3.5, "nan": "NaN"}).encode())
    by_name = {m.field_name: m for m in frame.measurements.values()}
    assert by_name["ok"].valid is True
    assert by_name["temp"].valid is False and by_name["temp"].quality.name == "INVALID"
    assert by_name["altitude"].valid is False
    assert frame.metadata["invalid_fields"] >= 2


def test_payload_cannot_upgrade_its_own_provenance():
    payload = json.dumps({"alt": {"value": 1.0, "source": "SIMULATED"},
                          "pres": {"value": 2.0, "source": "CALIBRATED"}}).encode()
    frame = _decode(payload)
    by_name = {m.field_name: m for m in frame.measurements.values()}
    assert by_name["alt"].source.value == "SIMULATED"
    assert by_name["pres"].source.value == "RAW"  # declared upgrade ignored


def test_implausible_timestamp_is_replaced_and_flagged():
    raw = protocol.pack_frame(int(protocol.PacketType.SENSOR_DATA), 1, 0, b'{"a": 1}')
    frame = decode_payload_to_frame(StreamParser().parse_stream(raw)[0])
    assert frame.metadata["timestamp_replaced"] is True
    assert frame.metadata["timestamp_us_declared"] == 0


def test_corrupt_crc_frame_carries_no_measurements():
    raw = bytearray(_frame(9))
    raw[-1] ^= 0xFF
    pkts = StreamParser().parse_stream(bytes(raw))
    assert pkts[0].crc_valid is False
    frame = decode_payload_to_frame(pkts[0])
    assert frame.measurements == {} and frame.crc_valid is False


# --- pipeline verdict ------------------------------------------------------------------
def test_authenticate_stage_marks_rejected_frames_invalid():
    stage = AuthenticateStage()
    ctx = PipelineContext(hmac_key=KEY, require_auth=True)
    frame = _decode(b'{"altitude": 12.5}')
    frame.metadata["auth_state"] = "UNAUTHENTICATED"
    stage.process(frame, ctx)
    assert all(not m.valid and m.quality.name == "INVALID" for m in frame.measurements.values())
    assert frame.metadata["auth_rejected"] is True

    # Optional mode: unauthenticated frames are SUSPECT, still valid.
    frame2 = _decode(b'{"altitude": 12.5}')
    frame2.metadata["auth_state"] = "UNAUTHENTICATED"
    stage.process(frame2, PipelineContext(hmac_key=KEY, require_auth=False))
    assert all(m.valid and m.quality.name == "SUSPECT" for m in frame2.measurements.values())

    # Bad tag is always INVALID regardless of mode.
    frame3 = _decode(b'{"altitude": 12.5}')
    frame3.metadata["auth_state"] = "INVALID_TAG"
    stage.process(frame3, PipelineContext(hmac_key=KEY))
    assert all(not m.valid for m in frame3.measurements.values())


# --- simulation / orchestrator -------------------------------------------------------
def test_simulated_frames_are_authenticated_when_key_configured():
    sim = MissionSimulator(profile=FlightProfile(apogee_m=300.0), sample_rate_hz=2.0, seed=1)
    source = SimulatedPacketSource(sim, link_key=KEY)
    parser = StreamParser(link_key=KEY, require_auth=True)
    frames = source.collect()
    assert frames
    pkts = parser.parse_stream(b"".join(frames[:20]))
    assert pkts and all(p.auth_state == "AUTHENTICATED" and not p.rejected for p in pkts)
    # Simulated provenance survives authentication: the tag proves origin, not realness.
    frame = decode_payload_to_frame(pkts[0])
    assert any(m.source.value == "SIMULATED" for m in frame.measurements.values())


def test_resolve_api_host_defaults_to_loopback(monkeypatch):
    monkeypatch.delenv("AIRONE_API_HOST", raising=False)
    assert resolve_api_host({}) == "127.0.0.1"
    assert resolve_api_host({"host": "0.0.0.0"}) == "0.0.0.0"
    monkeypatch.setenv("AIRONE_API_HOST", "10.0.0.5")
    assert resolve_api_host({"host": "0.0.0.0"}) == "10.0.0.5"


# --- audit chain --------------------------------------------------------------------------
def test_audit_chain_verifies_survives_restart_and_detects_tampering(tmp_path):
    log_dir = str(tmp_path / "logs")
    a = AuditLogger(log_dir=log_dir)
    a.log(AuditEvent.LOGIN_SUCCESS, user_id="u1")
    a.log(AuditEvent.CONFIG_CHANGE, user_id="u1", details={"k": 1})
    path = os.path.join(log_dir, "audit.jsonl")
    res = verify_audit_file(path)
    assert res.ok and res.entries == 2

    # A new logger on the same file continues the chain (cross-restart).
    b = AuditLogger(log_dir=log_dir)
    b.log(AuditEvent.LOGOUT, user_id="u1")
    res = verify_audit_file(path)
    assert res.ok and res.entries == 3

    # Edit the middle entry -> detected at that line.
    lines = open(path, encoding="utf-8").read().splitlines()
    entry = json.loads(lines[1])
    entry["user_id"] = "attacker"
    lines[1] = json.dumps(entry, separators=(",", ":"))
    open(path, "w", encoding="utf-8").write("\n".join(lines) + "\n")
    res = verify_audit_file(path)
    assert not res.ok and res.first_bad_line == 2 and "tamper" in res.reason

    # Delete an entry -> chain break.
    open(path, "w", encoding="utf-8").write("\n".join([lines[0], lines[2]]) + "\n")
    res = verify_audit_file(path)
    assert not res.ok and res.first_bad_line == 2 and "chain break" in res.reason
    assert verify_audit_file(str(tmp_path / "nope.jsonl")).ok is False
    assert "ok" in res.to_dict()


def test_audit_cli_exit_codes(tmp_path):
    log_dir = str(tmp_path / "logs")
    AuditLogger(log_dir=log_dir).log(AuditEvent.LOGIN_SUCCESS, user_id="u1")
    path = os.path.join(log_dir, "audit.jsonl")
    ok = subprocess.run([sys.executable, "-m", "src.security.audit", path], cwd=ROOT,
                        capture_output=True, text=True, timeout=60)
    assert ok.returncode == 0 and '"ok": true' in ok.stdout
    with open(path, "a", encoding="utf-8") as fh:
        fh.write("garbage\n")
    bad = subprocess.run([sys.executable, "-m", "src.security.audit", path], cwd=ROOT,
                         capture_output=True, text=True, timeout=60)
    assert bad.returncode == 2 and '"ok": false' in bad.stdout


# --- launcher bootstrap -------------------------------------------------------------------
@pytest.fixture
def launcher_env(tmp_path):
    import yaml

    cfg = {
        "storage": {"db_path": str(tmp_path / "data" / "airone.db")},
        "logging": {"log_dir": str(tmp_path / "logs"), "level": "WARNING", "json_output": False},
        "security": {"bcrypt_rounds": 4},
        "api": {"host": "127.0.0.1", "port": 5000},
        "telemetry": {"enabled": False},
    }
    cfg_path = tmp_path / "cfg.yaml"
    cfg_path.write_text(yaml.safe_dump(cfg))
    env = dict(os.environ)
    env.pop("AIRONE_ADMIN_PASSWORD", None)
    env.pop("AIRONE_ALLOW_DEFAULT_USERS", None)
    env["AIRONE_JWT_SECRET"] = "unit_test_secret_key_that_is_definitely_long_enough_123456"
    env["PYTHONPATH"] = ROOT

    def run(*args, **extra_env):
        e = dict(env)
        e.update(extra_env)
        return subprocess.run(
            [sys.executable, os.path.join(ROOT, "launcher.py"), "--config", str(cfg_path), *args],
            cwd=ROOT, env=e, capture_output=True, text=True, timeout=120,
        )

    return run, tmp_path


def test_launcher_account_bootstrap_flow(launcher_env):
    run, tmp_path = launcher_env

    # No password source -> refused, nothing created.
    res = run("--create-admin")
    assert res.returncode == 2
    assert "No user accounts" in run("--list-users").stdout

    # Weak password rejected by policy.
    res = run("--create-admin", AIRONE_ADMIN_PASSWORD="password")
    assert res.returncode == 2

    res = run("--create-admin", AIRONE_ADMIN_PASSWORD="Falcon-Orbit!7731-Boot")
    assert res.returncode == 0, res.stderr
    listing = run("--list-users").stdout
    assert "admin" in listing and "ADMIN" in listing

    # Duplicate creation refused.
    assert run("--create-admin", AIRONE_ADMIN_PASSWORD="Falcon-Orbit!7731-Boot").returncode == 2

    # Additional user with generated password: printed once, must_change forced.
    res = run("--create-user", "ops1", "--role", "operator", "--generate-password")
    assert res.returncode == 0, res.stderr
    assert "GENERATED PASSWORD" in res.stdout
    assert "must_change=True" in run("--list-users").stdout

    # Unknown role and unknown user are refused.
    assert run("--create-user", "x1", "--role", "root", AIRONE_ADMIN_PASSWORD="Falcon-Orbit!7731-Boot").returncode == 2
    assert run("--reset-password", "ghost", AIRONE_ADMIN_PASSWORD="Falcon-Orbit!7731-Boot").returncode == 2
    assert run("--reset-password", "ops1", AIRONE_ADMIN_PASSWORD="Rocket-Nozzle!4482-New").returncode == 0

    # The created accounts really authenticate with the stored hashes.
    from src.security.users import UserStore
    from src.storage.database import DatabaseManager

    db = DatabaseManager(str(tmp_path / "data" / "airone.db"))
    try:
        store = UserStore(db, bcrypt_rounds=4)
        assert store.authenticate("admin", "Falcon-Orbit!7731-Boot").state == "OK"
        assert store.authenticate("ops1", "Rocket-Nozzle!4482-New").state == "OK"
        assert store.authenticate("admin", "wrong-password-123!").state == "BAD_CREDENTIALS"
    finally:
        db.close()

    # Account operations were audited and the chain verifies via the CLI.
    audit_path = tmp_path / "logs" / "audit.jsonl"
    assert audit_path.exists()
    text = audit_path.read_text()
    assert "USER_CREATE" in text and "PASSWORD_CHANGE" in text
    assert "Falcon-Orbit!7731-Boot" not in text  # secrets never audited
    res = run("--verify-audit")
    assert res.returncode == 0, res.stdout + res.stderr
    assert "ok: True" in res.stdout

    # Validation passes with an admin present (no accounts warning needed).
    res = run("--validate-only", "--no-serial")
    assert res.returncode == 0, res.stderr
