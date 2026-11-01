"""Scheduler for cron triggers with quiet hours and caps."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

try:
    from apscheduler.schedulers.background import BackgroundScheduler
    from apscheduler.triggers.cron import CronTrigger
except ImportError:  # pragma: no cover
    BackgroundScheduler = None  # type: ignore[assignment, misc]
    CronTrigger = None  # type: ignore[assignment, misc]

DEFAULT_DAILY_CAP = 5
DEFAULT_QUIET_START = 20
DEFAULT_QUIET_END = 8
DEFAULT_WORK_START = 8
DEFAULT_WORK_END = 18


def _zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name or "UTC")
    except ZoneInfoNotFoundError:
        return ZoneInfo("UTC")


def _parse_time(value: str) -> tuple[int, int]:
    text = (value or "09:00").strip()
    hour = 9
    minute = 0
    if ":" in text:
        left, _, right = text.partition(":")
        try:
            hour = int(left.strip())
            minute = int(right.strip())
        except ValueError:
            hour, minute = 9, 0
    else:
        try:
            hour = int(text)
        except ValueError:
            hour = 9
    hour = min(max(hour, 0), 23)
    minute = min(max(minute, 0), 59)
    return hour, minute


class Scheduler:
    """APScheduler wrapper gated by working hours, snooze, and cap.

    schedule_morning registers a cron job per project. should_send is the
    runtime gate: it blocks sends during quiet hours, outside working
    hours, while snoozed, or once the daily cap is reached.
    """

    def __init__(
        self,
        *,
        daily_cap: int = DEFAULT_DAILY_CAP,
        quiet_start: int = DEFAULT_QUIET_START,
        quiet_end: int = DEFAULT_QUIET_END,
        work_start: int = DEFAULT_WORK_START,
        work_end: int = DEFAULT_WORK_END,
    ) -> None:
        self.daily_cap = daily_cap
        self.quiet_start = quiet_start
        self.quiet_end = quiet_end
        self.work_start = work_start
        self.work_end = work_end
        self._counts: dict[tuple[str, str, str], int] = {}
        self._snoozed: set[tuple[str, str]] = set()
        self._specs: dict[str, dict[str, Any]] = {}
        self._sched: Any = None
        self._project_caps: dict[str, int] = {}

    def _backend(self) -> Any:
        if self._sched is None:
            if BackgroundScheduler is None:
                raise RuntimeError("apscheduler is not installed")
            self._sched = BackgroundScheduler()
        return self._sched

    def configure_project(
        self,
        project: str,
        *,
        daily_cap: int | None = None,
    ) -> None:
        if daily_cap is not None:
            self._project_caps[project] = int(daily_cap)

    def cap_for(self, project: str) -> int:
        return self._project_caps.get(project, self.daily_cap)

    def schedule_morning(
        self,
        project: str,
        time: str = "09:00",
        tz: str = "UTC",
        days: str = "mon-fri",
        func: Any | None = None,
        **kwargs: Any,
    ) -> str:
        """Schedule a morning job with cron in the given time zone."""
        hour, minute = _parse_time(time)
        zone = _zone(tz)
        job_id = f"morning:{project}:{hour:02d}{minute:02d}:{tz}:{days}"
        self._specs[job_id] = {
            "project": project,
            "time": f"{hour:02d}:{minute:02d}",
            "tz": tz,
            "days": days,
        }
        if func is None:
            return job_id
        backend = self._backend()
        trigger = CronTrigger(
            hour=hour,
            minute=minute,
            day_of_week=days,
            timezone=zone,
        )
        existing = backend.get_job(job_id)
        if existing is not None:
            backend.remove_job(job_id)
        backend.add_job(func, trigger, id=job_id, args=[project], **kwargs)
        return job_id

    def jobs(self) -> list[str]:
        return sorted(self._specs)

    def spec_for(self, job_id: str) -> dict[str, Any]:
        return dict(self._specs.get(job_id, {}))

    def start(self) -> None:
        self._backend().start()

    def shutdown(self, wait: bool = False) -> None:
        if self._sched is not None:
            self._sched.shutdown(wait=wait)
            self._sched = None

    def snooze(self, project: str, user_id: str) -> None:
        self._snoozed.add((project, user_id))

    def unsnooze(self, project: str, user_id: str) -> None:
        self._snoozed.discard((project, user_id))

    def is_snoozed(self, project: str, user_id: str) -> bool:
        return (project, user_id) in self._snoozed

    def _day_key(self, now: datetime, tz: str) -> str:
        zone = _zone(tz)
        current = now
        if current.tzinfo is None:
            current = current.replace(tzinfo=zone)
        else:
            current = current.astimezone(zone)
        return current.date().isoformat()

    def _hour_in(self, now: datetime, tz: str) -> int:
        zone = _zone(tz)
        current = now
        if current.tzinfo is None:
            current = current.replace(tzinfo=zone)
        else:
            current = current.astimezone(zone)
        return current.hour

    def count_for(
        self,
        project: str,
        user_id: str,
        now: datetime | None = None,
        tz: str = "UTC",
    ) -> int:
        day = self._day_key(now or datetime.now(tz=_zone(tz)), tz)
        return self._counts.get((project, user_id, day), 0)

    def record_send(
        self,
        project: str,
        user_id: str,
        now: datetime | None = None,
        tz: str = "UTC",
    ) -> int:
        day = self._day_key(now or datetime.now(tz=_zone(tz)), tz)
        key = (project, user_id, day)
        self._counts[key] = self._counts.get(key, 0) + 1
        return self._counts[key]

    def in_quiet_hours(self, now: datetime, tz: str = "UTC") -> bool:
        hour = self._hour_in(now, tz)
        if self.quiet_start <= self.quiet_end:
            return self.quiet_start <= hour < self.quiet_end
        return hour >= self.quiet_start or hour < self.quiet_end

    def in_working_hours(self, now: datetime, tz: str = "UTC") -> bool:
        hour = self._hour_in(now, tz)
        return self.work_start <= hour < self.work_end

    def should_send(
        self,
        project: str,
        user_id: str,
        now: datetime | None = None,
        tz: str = "UTC",
    ) -> bool:
        """Return True when a DM may go out right now."""
        moment = now or datetime.now(tz=_zone(tz))
        if self.is_snoozed(project, user_id):
            return False
        if self.in_quiet_hours(moment, tz):
            return False
        if not self.in_working_hours(moment, tz):
            return False
        return self.count_for(project, user_id, moment, tz) < self.cap_for(project)
