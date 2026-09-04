"""Tests for the deterministic mission state machine."""
import pytest

from src.core.errors import InvalidTransitionError
from src.core.models import MissionState
from src.core.mission.state_machine import MissionStateMachine


def test_valid_transitions():
    sm = MissionStateMachine()
    sequence = [
        MissionState.SELF_TEST, MissionState.PRELAUNCH, MissionState.ASCENT,
        MissionState.APOGEE, MissionState.DESCENT, MissionState.LANDED,
    ]
    for state in sequence:
        sm.transition(state, reason="test")
    assert sm.state == MissionState.LANDED


def test_invalid_transition_rejected():
    sm = MissionStateMachine()
    with pytest.raises(InvalidTransitionError):
        sm.transition(MissionState.LANDED, reason="illegal jump")


def test_any_to_fault():
    for start in [
        MissionState.BOOT, MissionState.SELF_TEST, MissionState.PRELAUNCH,
        MissionState.ASCENT, MissionState.APOGEE, MissionState.DESCENT,
        MissionState.LANDED, MissionState.SAFE,
    ]:
        sm = MissionStateMachine(initial=start)
        t = sm.force_fault("anomaly detected")
        assert sm.state == MissionState.FAULT
        assert t.new_state == MissionState.FAULT


def test_transition_recorded():
    sm = MissionStateMachine()
    sm.transition(MissionState.SELF_TEST, reason="boot complete", confidence=0.9)
    hist = sm.history
    assert len(hist) == 1
    assert hist[0].prev_state == MissionState.BOOT
    assert hist[0].new_state == MissionState.SELF_TEST
    assert hist[0].reason == "boot complete"
    assert hist[0].confidence == 0.9


def test_guard_blocks_transition():
    from datetime import datetime, timezone
    from src.core.models import Measurement

    sm = MissionStateMachine(initial=MissionState.PRELAUNCH)
    # Guard requires altitude rising (vertical_velocity > 1). Provide falling.
    ts = datetime.now(timezone.utc)
    vv = Measurement(-5.0, "m/s", ts, "imu", field_name="vertical_velocity")
    with pytest.raises(InvalidTransitionError):
        sm.transition(MissionState.ASCENT, reason="premature", supporting=[vv])
