"""UTC occurrences, calendar wall time, first fold only; nonexistent times are skipped."""

from datetime import datetime, timedelta, timezone, time as wall_time
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .models import DispatcherError

UTC = timezone.utc


class ScheduleSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    mode: Literal["once", "interval", "daily", "weekly"]
    timezone: str = "UTC"
    start: int = Field(ge=946684800, le=4102444800)  # UTC seconds, 2000..2100
    interval: int = Field(default=60, ge=1, le=31536000)
    time: str = "09:00:00"
    weekdays: list[int] = Field(default_factory=lambda: [0, 1, 2, 3, 4])
    misfire: Literal["skip", "latest"] = "skip"
    grace: int = Field(default=60, ge=0, le=3600)

    @field_validator("timezone")
    @classmethod
    def zone_exists(cls, value):
        try:
            ZoneInfo(value)
        except (KeyError, ValueError) as exc:
            raise ValueError("Unknown IANA timezone") from exc
        return value

    @field_validator("time")
    @classmethod
    def wall_clock(cls, value):
        if len(value) != 8 or wall_time.fromisoformat(value).tzinfo is not None:
            raise ValueError("HH:MM:SS required")
        return value

    @model_validator(mode="after")
    def days(self):
        if (
            not self.weekdays
            or len(set(self.weekdays)) != len(self.weekdays)
            or any(type(d) is not int or not 0 <= d <= 6 for d in self.weekdays)
        ):
            raise ValueError("Select unique weekdays, Monday=0")
        return self

    def candidate(self, day):
        local = datetime.combine(day, wall_time.fromisoformat(self.time))
        aware = local.replace(tzinfo=ZoneInfo(self.timezone), fold=0)
        stamp = int(aware.timestamp())
        # Roundtrip rejects spring gaps. fold=0 always chooses the first fall occurrence.
        if datetime.fromtimestamp(stamp, ZoneInfo(self.timezone)).replace(tzinfo=None) != local:
            return None
        return stamp

    def next_after(self, after):
        if self.mode == "once":
            return self.start if self.start > after else None
        if self.mode == "interval":
            return self.start + max(0, int((after - self.start) // self.interval) + 1) * self.interval
        floor = max(after, self.start - 1)
        day = datetime.fromtimestamp(floor, ZoneInfo(self.timezone)).date()
        for shift in range(15):
            date = day + timedelta(days=shift)
            if self.mode == "weekly" and date.weekday() not in self.weekdays:
                continue
            stamp = self.candidate(date)
            if stamp is not None and stamp >= self.start and stamp > after:
                return stamp
        raise DispatcherError("SCHEDULE_INVALID")

    def due_span(self, first, through):
        """Count and latest due without materializing missed interval occurrences."""
        if first > through:
            return 0, None
        if self.mode == "once":
            return 1, first
        if self.mode == "interval":
            count = int((through - first) // self.interval) + 1
            return count, first + (count - 1) * self.interval
        day = datetime.fromtimestamp(first, ZoneInfo(self.timezone)).date()
        end = datetime.fromtimestamp(through, ZoneInfo(self.timezone)).date()
        count, last = 0, None
        while day <= end:
            if self.mode == "daily" or day.weekday() in self.weekdays:
                stamp = self.candidate(day)
                if stamp is not None and first <= stamp <= through:
                    count, last = count + 1, stamp
            day += timedelta(days=1)
        return count, last


def parse_spec(value):
    try:
        return ScheduleSpec.model_validate(value)
    except Exception as exc:
        raise DispatcherError("SCHEDULE_INVALID") from exc


def preview(value, *, after, count=5):
    spec, result = parse_spec(value), []
    for _ in range(min(20, max(1, count))):
        stamp = spec.next_after(after)
        if stamp is None:
            break
        result.append(
            {"utc": stamp, "local": datetime.fromtimestamp(stamp, ZoneInfo(spec.timezone)).isoformat()}
        )
        after = stamp
    return result
