"""Deterministic, thread-safe mission state machine.

Transitions are explicit. Only whitelisted transitions are permitted, plus a
universal transition to ``FAULT`` from any state. Each transition can be gated
by a guard function that inspects the supporting measurements (e.g. altitude
must be rising to enter ASCENT). Every accepted transition is recorded and
emitted on the event bus.
"""
from __future__ import annotations

import logging
import threading
from typing import Callable, Dict, List, Optional, Set

from ..errors import InvalidTransitionError
from ..events import EventBus, TOPIC_MISSION_TRANSITION, get_event_bus
from ..models import Measurement, MissionState, MissionTransition, utcnow

logger = logging.getLogger(__name__)

# Guard receives the list of supporting measurements and returns True if the
# physical preconditions for the transition are satisfied.
Guard = Callable[[List[Measurement]], bool]


# Allowed forward transitions (FAULT is handled separately as a universal sink).
_VALID_TRANSITIONS: Dict[MissionState, Set[MissionState]] = {
    MissionState.BOOT: {MissionState.SELF_TEST},
    MissionState.SELF_TEST: {MissionState.PRELAUNCH},
    MissionState.PRELAUNCH: {MissionState.ASCENT},
    MissionState.ASCENT: {MissionState.APOGEE},
    MissionState.APOGEE: {MissionState.DESCENT},
    MissionState.DESCENT: {MissionState.LANDED},
    MissionState.LANDED: {MissionState.SAFE},
    MissionState.SAFE: set(),
    MissionState.FAULT: {MissionState.SAFE},
}


def _measurement_named(
    measurements: List[Measurement], field_name: str
) -> Optional[Measurement]:
    for m in measurements:
        if m.field_name == field_name or m.sensor_id == field_name:
            return m
    return None


def _altitude_rising(measurements: List[Measurement]) -> bool:
    vv = _measurement_named(measurements, "vertical_velocity")
    if vv is not None and vv.valid:
        return vv.value > 1.0
    return True  # No data available: do not block the transition.


def _altitude_falling(measurements: List[Measurement]) -> bool:
    vv = _measurement_named(measurements, "vertical_velocity")
    if vv is not None and vv.valid:
        return vv.value < -1.0
    return True


def _low_vertical_velocity(measurements: List[Measurement]) -> bool:
    vv = _measurement_named(measurements, "vertical_velocity")
    if vv is not None and vv.valid:
        return abs(vv.value) < 0.5
    return True


# Default guards keyed by (from_state, to_state).
_DEFAULT_GUARDS: Dict[tuple, Guard] = {
    (MissionState.PRELAUNCH, MissionState.ASCENT): _altitude_rising,
    (MissionState.APOGEE, MissionState.DESCENT): _altitude_falling,
    (MissionState.DESCENT, MissionState.LANDED): _low_vertical_velocity,
}


class MissionStateMachine:
    """Thread-safe mission state machine with recorded transition history."""

    def __init__(
        self,
        mission_id: str = "default",
        initial: MissionState = MissionState.BOOT,
        event_bus: Optional[EventBus] = None,
    ) -> None:
        self.mission_id = mission_id
        self._state = initial
        self._lock = threading.Lock()
        self._history: List[MissionTransition] = []
        self._event_bus = event_bus or get_event_bus()
        self._guards: Dict[tuple, Guard] = dict(_DEFAULT_GUARDS)

    @property
    def state(self) -> MissionState:
        with self._lock:
            return self._state

    @property
    def history(self) -> List[MissionTransition]:
        with self._lock:
            return list(self._history)

    def can_transition(self, new_state: MissionState) -> bool:
        with self._lock:
            return self._is_allowed(self._state, new_state)

    @staticmethod
    def _is_allowed(current: MissionState, new_state: MissionState) -> bool:
        if new_state == MissionState.FAULT:
            return True  # Any state may fault.
        return new_state in _VALID_TRANSITIONS.get(current, set())

    def register_guard(
        self, from_state: MissionState, to_state: MissionState, guard: Guard
    ) -> None:
        self._guards[(from_state, to_state)] = guard

    def transition(
        self,
        new_state: MissionState,
        reason: str = "",
        supporting: Optional[List[Measurement]] = None,
        confidence: float = 1.0,
    ) -> MissionTransition:
        """Attempt a transition. Raises :class:`InvalidTransitionError` if not allowed."""

        supporting = supporting or []
        with self._lock:
            current = self._state
            if not self._is_allowed(current, new_state):
                raise InvalidTransitionError(
                    f"Transition {current.value} -> {new_state.value} is not permitted"
                )
            guard = self._guards.get((current, new_state))
            if guard is not None and not guard(supporting):
                raise InvalidTransitionError(
                    f"Guard rejected transition {current.value} -> {new_state.value}: "
                    "physical preconditions not met"
                )
            transition = MissionTransition(
                timestamp=utcnow(),
                prev_state=current,
                new_state=new_state,
                reason=reason,
                supporting_measurements=supporting,
                confidence=confidence,
            )
            self._state = new_state
            self._history.append(transition)

        logger.info(
            "Mission %s transition %s -> %s (%s, confidence=%.2f)",
            self.mission_id,
            current.value,
            new_state.value,
            reason,
            confidence,
        )
        self._event_bus.publish(TOPIC_MISSION_TRANSITION, transition)
        return transition

    def force_fault(self, reason: str) -> MissionTransition:
        return self.transition(MissionState.FAULT, reason=reason, confidence=1.0)
