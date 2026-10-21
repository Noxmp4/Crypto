"""Planificateur minimal, persistant : la prochaine exécution de chaque tâche est
enregistrée dans la table `state`. Après un arrêt, une tâche en retard s'exécute une
seule fois au redémarrage (pas de rattrapage en rafale)."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable
from zoneinfo import ZoneInfo

log = logging.getLogger("daybot")
UTC = timezone.utc


class Every:
    def __init__(self, seconds: int, offset: int = 0):
        self.seconds, self.offset = seconds, offset

    def next_after(self, now: datetime) -> datetime:
        ts = now.timestamp() - self.offset
        nxt = (ts // self.seconds + 1) * self.seconds + self.offset
        return datetime.fromtimestamp(nxt, UTC)

    def __repr__(self) -> str:
        return f"toutes les {self.seconds} s"


class Daily:
    def __init__(self, hour: int, minute: int = 0, tz: str = "UTC"):
        self.hour, self.minute, self.tz = hour, minute, ZoneInfo(tz)

    def next_after(self, now: datetime) -> datetime:
        local = now.astimezone(self.tz)
        cand = local.replace(hour=self.hour, minute=self.minute, second=0, microsecond=0)
        while cand <= local:
            cand = (cand + timedelta(days=1)).replace(hour=self.hour, minute=self.minute)
        return cand.astimezone(UTC)

    def __repr__(self) -> str:
        return f"chaque jour à {self.hour:02d}:{self.minute:02d} ({self.tz})"


class Weekly(Daily):
    def __init__(self, weekday: int, hour: int, minute: int = 0, tz: str = "UTC"):
        super().__init__(hour, minute, tz)
        self.weekday = weekday

    def next_after(self, now: datetime) -> datetime:
        cand = super().next_after(now)
        while cand.astimezone(self.tz).weekday() != self.weekday:
            cand = super().next_after(cand)
        return cand

    def __repr__(self) -> str:
        days = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]
        return f"chaque {days[self.weekday]} à {self.hour:02d}:{self.minute:02d} ({self.tz})"


@dataclass
class Job:
    name: str
    schedule: object
    fn: Callable[[], object]
    run_at_start: bool = False


def _parse(s: str) -> datetime | None:
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")) if s else None
    except ValueError:
        return None


class Scheduler:
    def __init__(self, store, on_error: Callable[[str, BaseException], None] | None = None):
        self.store = store
        self.jobs: list[Job] = []
        self.on_error = on_error or (lambda name, e: log.exception("tâche %s en échec", name))

    def add(self, name: str, schedule, fn: Callable[[], object], run_at_start: bool = False) -> None:
        self.jobs.append(Job(name, schedule, fn, run_at_start))

    def next_run(self, job: Job, now: datetime) -> datetime:
        stored = _parse(self.store.get_state(f"job:{job.name}:next"))
        if stored:
            return stored
        return now if job.run_at_start else job.schedule.next_after(now)

    def tick(self, now: datetime | None = None) -> list[str]:
        now = now or datetime.now(UTC)
        ran = []
        for job in self.jobs:
            if now < self.next_run(job, now):
                continue
            try:
                job.fn()
                self.store.set_state(f"job:{job.name}:last_ok", now.strftime("%Y-%m-%dT%H:%M:%SZ"))
            except Exception as e:  # noqa: BLE001 — une tâche en échec ne bloque pas les autres
                self.on_error(job.name, e)
            finally:
                nxt = job.schedule.next_after(now)
                self.store.set_state(f"job:{job.name}:next", nxt.strftime("%Y-%m-%dT%H:%M:%SZ"))
                ran.append(job.name)
        return ran
