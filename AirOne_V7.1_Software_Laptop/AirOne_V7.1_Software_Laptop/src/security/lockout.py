"""Login brute-force protection: per-account and per-IP throttling.

Every failed attempt increments two counters (account, source IP). Once either
counter reaches its threshold the principal is locked for a back-off period
that doubles on each consecutive lockout (capped). Successful login clears the
account counter. State is in-memory (per process) — a restart resets it, which
is acceptable because the API is bound to the operator's laptop; the audit log
retains the permanent record of every attempt.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Dict, Optional


@dataclass
class _Bucket:
    failures: int = 0
    locked_until: float = 0.0
    lockouts: int = 0
    last_failure: float = 0.0


@dataclass
class ThrottleDecision:
    allowed: bool
    reason: str = "OK"  # OK | ACCOUNT_LOCKED | IP_LOCKED
    retry_after_s: int = 0


@dataclass
class LoginThrottle:
    max_attempts_per_account: int = 5
    max_attempts_per_ip: int = 20
    base_lockout_s: float = 300.0
    max_lockout_s: float = 3600.0
    # Failures older than this are forgotten (sliding reset).
    failure_window_s: float = 900.0
    _accounts: Dict[str, _Bucket] = field(default_factory=dict)
    _ips: Dict[str, _Bucket] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _clock: object = field(default=time.monotonic)

    def _now(self) -> float:
        return float(self._clock())  # type: ignore[operator]

    def _bucket(self, table: Dict[str, _Bucket], key: str) -> _Bucket:
        b = table.get(key)
        if b is None:
            b = _Bucket()
            table[key] = b
        elif b.last_failure and self._now() - b.last_failure > self.failure_window_s and b.locked_until <= self._now():
            b.failures = 0
        return b

    def check(self, account: str, ip: Optional[str]) -> ThrottleDecision:
        with self._lock:
            now = self._now()
            a = self._bucket(self._accounts, account)
            if a.locked_until > now:
                return ThrottleDecision(False, "ACCOUNT_LOCKED", int(a.locked_until - now) + 1)
            if ip:
                i = self._bucket(self._ips, ip)
                if i.locked_until > now:
                    return ThrottleDecision(False, "IP_LOCKED", int(i.locked_until - now) + 1)
            return ThrottleDecision(True)

    def record_failure(self, account: str, ip: Optional[str]) -> ThrottleDecision:
        """Record a failure; returns the (possibly new) lock state."""

        with self._lock:
            now = self._now()
            decision = ThrottleDecision(True)
            a = self._bucket(self._accounts, account)
            a.failures += 1
            a.last_failure = now
            if a.failures >= self.max_attempts_per_account:
                a.lockouts += 1
                dur = min(self.base_lockout_s * (2 ** (a.lockouts - 1)), self.max_lockout_s)
                a.locked_until = now + dur
                a.failures = 0
                decision = ThrottleDecision(False, "ACCOUNT_LOCKED", int(dur))
            if ip:
                i = self._bucket(self._ips, ip)
                i.failures += 1
                i.last_failure = now
                if i.failures >= self.max_attempts_per_ip:
                    i.lockouts += 1
                    dur = min(self.base_lockout_s * (2 ** (i.lockouts - 1)), self.max_lockout_s)
                    i.locked_until = now + dur
                    i.failures = 0
                    if decision.allowed:
                        decision = ThrottleDecision(False, "IP_LOCKED", int(dur))
            return decision

    def record_success(self, account: str) -> None:
        with self._lock:
            a = self._accounts.get(account)
            if a is not None:
                a.failures = 0
                a.locked_until = 0.0

    def snapshot(self) -> Dict[str, int]:
        with self._lock:
            now = self._now()
            return {
                "locked_accounts": sum(1 for b in self._accounts.values() if b.locked_until > now),
                "locked_ips": sum(1 for b in self._ips.values() if b.locked_until > now),
            }
