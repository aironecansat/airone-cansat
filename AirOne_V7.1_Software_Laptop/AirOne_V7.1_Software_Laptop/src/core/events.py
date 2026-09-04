"""Thread-safe in-process event bus.

Workers publish events (mission transitions, telemetry frames, alarms) and any
number of subscribers receive them. Delivery is synchronous but each callback
is wrapped so one failing subscriber never blocks the others.
"""
from __future__ import annotations

import logging
import threading
from collections import defaultdict
from typing import Any, Callable, DefaultDict, List

logger = logging.getLogger(__name__)

# Well-known event topics.
TOPIC_TELEMETRY_FRAME = "telemetry.frame"
TOPIC_MISSION_TRANSITION = "mission.transition"
TOPIC_ALARM = "alarm"
TOPIC_SYSTEM = "system"
TOPIC_VISUALIZATION = "visualization"


Callback = Callable[[str, Any], None]


class EventBus:
    """A minimal publish/subscribe bus safe for concurrent use."""

    def __init__(self) -> None:
        self._subscribers: DefaultDict[str, List[Callback]] = defaultdict(list)
        self._lock = threading.RLock()

    def subscribe(self, topic: str, callback: Callback) -> None:
        with self._lock:
            self._subscribers[topic].append(callback)
            logger.debug("Subscribed callback to topic '%s'", topic)

    def unsubscribe(self, topic: str, callback: Callback) -> None:
        with self._lock:
            if callback in self._subscribers.get(topic, []):
                self._subscribers[topic].remove(callback)

    def publish(self, topic: str, payload: Any) -> int:
        """Publish ``payload`` on ``topic``. Returns number of callbacks invoked."""

        with self._lock:
            callbacks = list(self._subscribers.get(topic, []))
        delivered = 0
        for cb in callbacks:
            try:
                cb(topic, payload)
                delivered += 1
            except Exception:  # noqa: BLE001 - isolate subscriber failures
                logger.exception(
                    "Event subscriber for topic '%s' raised; continuing", topic
                )
        return delivered

    def clear(self) -> None:
        with self._lock:
            self._subscribers.clear()


# Process-wide default bus.
_GLOBAL_BUS = EventBus()


def get_event_bus() -> EventBus:
    """Return the process-wide event bus singleton."""

    return _GLOBAL_BUS
