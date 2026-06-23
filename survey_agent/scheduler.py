"""Scheduler — heartbeat and realistic work schedule with breaks."""

from __future__ import annotations

import asyncio
import logging
import random
import signal
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta, timezone
from enum import Enum
from typing import Any, Callable

logger = logging.getLogger(__name__)


class SessionState(str, Enum):
    WORKING = "working"
    BREAK = "break"
    LUNCH = "lunch"
    SLEEPING = "sleeping"
    IDLE = "idle"
    STOPPED = "stopped"


@dataclass
class ScheduleConfig:
    """Configuration for realistic work schedule."""

    # Work hours (when the agent is "awake" and active)
    wake_time: time = time(8, 0)   # 8:00 AM
    sleep_time: time = time(23, 0)  # 11:00 PM

    # Work session parameters
    min_session_minutes: int = 15
    max_session_minutes: int = 45
    min_break_minutes: int = 5
    max_break_minutes: int = 15

    # Lunch break
    lunch_start: time = time(12, 0)
    lunch_duration_minutes: int = 30

    # Evening wind-down (shorter sessions)
    wind_down_start: time = time(21, 0)
    wind_down_session_minutes: int = 20

    # Heartbeat
    heartbeat_interval_seconds: int = 30

    # Timezone
    timezone: timezone = timezone.utc


@dataclass
class HeartbeatState:
    """Current heartbeat state."""

    last_beat: datetime | None = None
    state: SessionState = SessionState.IDLE
    current_session_start: datetime | None = None
    total_work_seconds: int = 0
    total_break_seconds: int = 0
    surveys_completed: int = 0
    questions_answered: int = 0
    errors: int = 0


class Scheduler:
    """Manages realistic work scheduling with heartbeat.

    The agent works in sessions during "awake" hours, takes random breaks,
    has a lunch break, and sleeps at night. This makes the activity pattern
    look human rather than bot-like.
    """

    def __init__(self, config: ScheduleConfig | None = None):
        self._config = config or ScheduleConfig()
        self._state = HeartbeatState()
        self._running = False
        self._stop_event = asyncio.Event()
        self._work_fn: Callable | None = None
        self._rng = random.Random()

    @property
    def state(self) -> HeartbeatState:
        return self._state

    @property
    def is_running(self) -> bool:
        return self._running

    def set_work_function(self, fn: Callable) -> None:
        """Set the function to call during work sessions."""
        self._work_fn = fn

    async def start(self) -> None:
        """Start the scheduler loop."""
        self._running = True
        self._stop_event.clear()
        logger.info("Scheduler started")

        try:
            while self._running and not self._stop_event.is_set():
                now = datetime.now(self._config.timezone)
                current_time = now.time()

                # Check if we should be sleeping
                if self._should_sleep(current_time):
                    self._state.state = SessionState.SLEEPING
                    sleep_until = self._next_wake_time(now)
                    sleep_seconds = (sleep_until - now).total_seconds()
                    if sleep_seconds > 0:
                        logger.info(f"Sleeping until {self._config.wake_time} ({sleep_seconds/3600:.1f}h)")
                        await self._interruptible_sleep(sleep_seconds)
                    continue

                # Check if it's lunch time
                if self._is_lunch_time(current_time):
                    self._state.state = SessionState.LUNCH
                    lunch_seconds = self._config.lunch_duration_minutes * 60
                    logger.info(f"Lunch break ({self._config.lunch_duration_minutes} min)")
                    self._state.total_break_seconds += lunch_seconds
                    await self._interruptible_sleep(lunch_seconds)
                    continue

                # Work session
                session_minutes = self._get_session_duration(current_time)
                session_seconds = session_minutes * 60

                self._state.state = SessionState.WORKING
                self._state.current_session_start = now
                logger.info(f"Work session ({session_minutes} min)")

                if self._work_fn:
                    try:
                        await asyncio.wait_for(
                            self._work_fn(),
                            timeout=session_seconds,
                        )
                    except asyncio.TimeoutError:
                        pass  # Normal — session time limit reached
                    except Exception as e:
                        logger.error(f"Work session error: {e}")
                        self._state.errors += 1

                self._state.total_work_seconds += session_seconds

                # Break
                if not self._should_sleep(datetime.now(self._config.timezone).time()):
                    break_minutes = self._rng.randint(
                        self._config.min_break_minutes,
                        self._config.max_break_minutes,
                    )
                    self._state.state = SessionState.BREAK
                    logger.info(f"Break ({break_minutes} min)")
                    self._state.total_break_seconds += break_minutes * 60
                    await self._interruptible_sleep(break_minutes * 60)

        finally:
            self._running = False
            self._state.state = SessionState.STOPPED
            logger.info("Scheduler stopped")

    async def stop(self) -> None:
        """Stop the scheduler gracefully."""
        self._running = False
        self._stop_event.set()

    async def heartbeat(self) -> HeartbeatState:
        """Send a heartbeat and return current state."""
        self._state.last_beat = datetime.now(self._config.timezone)
        return self._state

    async def _interruptible_sleep(self, seconds: float) -> None:
        """Sleep that can be interrupted by stop()."""
        try:
            await asyncio.wait_for(self._stop_event.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass  # Normal wakeup

    def _should_sleep(self, current_time: time) -> bool:
        """Check if the agent should be sleeping."""
        if self._config.wake_time < self._config.sleep_time:
            # Normal hours: awake 8am-11pm
            return current_time < self._config.wake_time or current_time >= self._config.sleep_time
        else:
            # Wrapping: awake 11pm-8am (unlikely but handle it)
            return self._config.sleep_time <= current_time < self._config.wake_time

    def _next_wake_time(self, now: datetime) -> datetime:
        """Calculate the next wake time."""
        wake = now.replace(
            hour=self._config.wake_time.hour,
            minute=self._config.wake_time.minute,
            second=0, microsecond=0,
        )
        if wake <= now:
            wake += timedelta(days=1)
        return wake

    def _is_lunch_time(self, current_time: time) -> bool:
        """Check if it's lunch time."""
        lunch_end = (
            datetime.combine(datetime.today(), self._config.lunch_start)
            + timedelta(minutes=self._config.lunch_duration_minutes)
        ).time()
        return self._config.lunch_start <= current_time < lunch_end

    def _get_session_duration(self, current_time: time) -> int:
        """Get session duration based on time of day."""
        if current_time >= self._config.wind_down_start:
            # Evening: shorter sessions
            return self._rng.randint(
                max(10, self._config.wind_down_session_minutes - 10),
                self._config.wind_down_session_minutes,
            )
        # Normal hours: standard sessions
        return self._rng.randint(
            self._config.min_session_minutes,
            self._config.max_session_minutes,
        )

    def get_status_text(self) -> str:
        """Human-readable status text."""
        s = self._state
        lines = [
            f"State: {s.state.value}",
            f"Work: {s.total_work_seconds // 60} min",
            f"Break: {s.total_break_seconds // 60} min",
            f"Surveys: {s.surveys_completed}",
            f"Questions: {s.questions_answered}",
            f"Errors: {s.errors}",
        ]
        if s.last_beat:
            lines.append(f"Last heartbeat: {s.last_beat.strftime('%H:%M:%S')}")
        return "\n".join(lines)
