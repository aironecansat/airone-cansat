"""Time synchronisation for telemetry timestamps.

Tracks the estimated clock offset and drift between the flight computer and the
ground station, and chooses the best available time source. A visible warning
is emitted whenever timestamp uncertainty exceeds 100 ms.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

UNCERTAINTY_WARN_US = 100_000  # 100 ms in microseconds


class TimeSource(str, Enum):
    GNSS = "GNSS"
    RTC = "RTC"
    NTP = "NTP"
    ESTIMATED = "ESTIMATED"


@dataclass
class TimeSyncState:
    offset_us: float = 0.0
    drift_ppm: float = 0.0
    uncertainty_us: float = 0.0
    last_sync_timestamp: Optional[datetime] = None
    source: TimeSource = TimeSource.ESTIMATED


class TimeSynchronizer:
    """NTP-style offset estimation and best-timestamp selection."""

    def __init__(self) -> None:
        self.state = TimeSyncState()

    @staticmethod
    def ping_pong_offset(t1: float, t2: float, t3: float, t4: float) -> float:
        """Compute clock offset using the NTP four-timestamp formula.

        ``t1`` request send (ground), ``t2`` request receive (remote),
        ``t3`` response send (remote), ``t4`` response receive (ground). All in
        microseconds. Returns the remote-minus-local offset in microseconds.
        """

        return ((t2 - t1) + (t3 - t4)) / 2.0

    @staticmethod
    def round_trip_delay(t1: float, t2: float, t3: float, t4: float) -> float:
        return (t4 - t1) - (t3 - t2)

    def update_offset(
        self,
        t1: float,
        t2: float,
        t3: float,
        t4: float,
        source: TimeSource = TimeSource.NTP,
    ) -> TimeSyncState:
        offset = self.ping_pong_offset(t1, t2, t3, t4)
        delay = self.round_trip_delay(t1, t2, t3, t4)
        self.state.offset_us = offset
        # Uncertainty approximated as half the round-trip delay.
        self.state.uncertainty_us = abs(delay) / 2.0
        self.state.last_sync_timestamp = datetime.now(timezone.utc)
        self.state.source = source
        if self.state.uncertainty_us > UNCERTAINTY_WARN_US:
            logger.warning(
                "Time sync uncertainty %.1f ms exceeds 100 ms threshold",
                self.state.uncertainty_us / 1000.0,
            )
        return self.state

    def get_best_timestamp(
        self,
        gnss_time: Optional[datetime],
        rtc_time: Optional[datetime],
        ground_time: Optional[datetime] = None,
    ) -> Tuple[datetime, TimeSource, float]:
        """Select the most trustworthy timestamp available.

        Preference order: GNSS > RTC > ground clock. Returns
        ``(timestamp, source, uncertainty_us)``.
        """

        ground_time = ground_time or datetime.now(timezone.utc)

        if gnss_time is not None:
            unc = 1_000.0  # ~1 ms GNSS-disciplined
            source = TimeSource.GNSS
            chosen = gnss_time
        elif rtc_time is not None:
            unc = 50_000.0  # ~50 ms typical RTC
            source = TimeSource.RTC
            chosen = rtc_time
        else:
            unc = max(self.state.uncertainty_us, UNCERTAINTY_WARN_US)
            source = TimeSource.ESTIMATED
            chosen = ground_time

        if chosen.tzinfo is None:
            chosen = chosen.replace(tzinfo=timezone.utc)

        if unc > UNCERTAINTY_WARN_US:
            logger.warning(
                "Best timestamp source=%s has uncertainty %.1f ms (>100 ms)",
                source.value,
                unc / 1000.0,
            )
        return chosen, source, unc
