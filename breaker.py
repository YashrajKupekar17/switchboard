"""Circuit breaker, one per provider+model ("deployment"). Decisions: DECISIONS.md section G.

    CLOSED --5 consecutive failures--> OPEN --30s cooldown--> HALF_OPEN (one trial request)
       ^                                 ^                        |
       +--------- trial succeeds --------|------ trial fails -----+

No locks needed: the gateway runs on one asyncio event loop, and allow()/record_*() never await,
so two requests can't interleave inside them.
"""
import logging
import math
import time
from typing import Callable, Optional

from metrics import BREAKER_STATE

log = logging.getLogger("gateway")

CLOSED, OPEN, HALF_OPEN = "closed", "open", "half_open"
NORMAL, TRIAL = "normal", "trial"  # permits handed out by allow()


class CircuitBreaker:
    def __init__(
        self, name: str, failure_threshold: int = 5, cooldown: float = 30.0, clock: Callable[[], float] = time.monotonic
    ):
        self.name = name
        self.failure_threshold = failure_threshold
        self.cooldown = cooldown
        self.clock = clock
        self.consecutive_failures = 0
        self.opened_at = 0.0
        self.trial_in_flight = False
        self._set_state(CLOSED)

    def allow(self) -> Optional[str]:
        """NORMAL or TRIAL if this request may call the provider; None means skip it and fall back."""
        if self.state == CLOSED:
            return NORMAL
        if self.state == OPEN and self.clock() - self.opened_at >= self.cooldown:
            self._set_state(HALF_OPEN)
            log.info("breaker %s half-open: sending one trial request", self.name)
        if self.state == HALF_OPEN and not self.trial_in_flight:
            self.trial_in_flight = True
            return TRIAL
        return None  # open, or a trial is already in flight: everyone else keeps falling back

    def record_success(self, permit: str) -> None:
        if permit == TRIAL:
            self.trial_in_flight, self.consecutive_failures = False, 0
            self._set_state(CLOSED)
            log.info("breaker %s closed: trial succeeded", self.name)
        elif self.state == CLOSED:
            self.consecutive_failures = 0
        # A success from a request that started before the breaker opened doesn't close it; only the trial decides.

    def record_failure(self, permit: str) -> None:
        if permit == TRIAL:
            self.trial_in_flight = False
            self._open("trial failed")
        elif self.state == CLOSED:
            self.consecutive_failures += 1
            if self.consecutive_failures >= self.failure_threshold:
                self._open(f"{self.consecutive_failures} consecutive failures")
        # Failures from requests that started before the breaker opened are ignored: we already know.

    def release(self, permit: str) -> None:
        """The request ended with no verdict (e.g. client disconnected). Free the trial slot so the breaker can't get stuck."""
        if permit == TRIAL:
            self.trial_in_flight = False

    def seconds_until_trial(self) -> int:
        return max(1, math.ceil(self.cooldown - (self.clock() - self.opened_at)))

    def _open(self, reason: str) -> None:
        self.opened_at = self.clock()
        self._set_state(OPEN)
        log.warning("breaker %s OPEN (%s): skipping it for %.0fs", self.name, reason, self.cooldown)

    def _set_state(self, state: str) -> None:
        self.state = state
        for s in (CLOSED, HALF_OPEN, OPEN):  # one-hot: exactly one state reads 1 (the Prometheus "state set" pattern)
            BREAKER_STATE.labels(self.name, s).set(1 if s == state else 0)


class Breakers:
    """One breaker per (provider, model), created on first use."""

    def __init__(self, **breaker_options):
        self._options = breaker_options
        self._by_deployment: dict = {}

    def get(self, provider: str, model: str) -> CircuitBreaker:
        key = (provider, model)
        if key not in self._by_deployment:
            self._by_deployment[key] = CircuitBreaker(name=f"{provider}/{model}", **self._options)
        return self._by_deployment[key]
