"""Data model: Value, Item, Log, Observation (see Plan.md「資料模型」).

Everything except ``Item.text`` may be empty. Numbers that matter to the
engine are wrapped in :class:`Value` so their origin (stated / stat / llm /
default) and uncertainty travel with them.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

Source = Literal["stated", "stat", "llm", "default"]
# Strongest first. The weakest input decides the source of any derived value.
SOURCE_RANK: dict[str, int] = {"stated": 0, "stat": 1, "llm": 2, "default": 3}


def weakest(sources) -> Source:
    sources = [s for s in sources if s]
    if not sources:
        return "stated"
    return max(sources, key=lambda s: SOURCE_RANK[s])  # type: ignore[return-value]


class Value(BaseModel):
    """A number with provenance. ``range`` defaults to ``[v, v]``."""

    v: float
    range: tuple[float, float] | None = None
    source: Source = "stated"
    basis: str | None = None

    @model_validator(mode="after")
    def _check_range(self) -> "Value":
        if self.range is not None:
            lo, hi = self.range
            if lo > hi:
                lo, hi = hi, lo
            self.range = (min(lo, self.v), max(hi, self.v))
        return self

    @property
    def lo(self) -> float:
        return self.range[0] if self.range else self.v

    @property
    def hi(self) -> float:
        return self.range[1] if self.range else self.v

    @property
    def estimated(self) -> bool:
        return self.source != "stated"


# ---------------------------------------------------------------- time specs

TimeType = Literal["none", "deadline", "fixed", "anchored", "recurring", "habit"]


class TimeSpec(BaseModel):
    """Time relation of an Item. Which fields apply depends on ``type``.

    - deadline: ``due`` (and optional ``due_earliest`` for fuzzy deadlines such
      as「期中前」, plus ``start`` = earliest moment work can begin)
    - fixed: ``start`` / ``end``
    - anchored: ``target`` item id, ``relation`` before/after, ``offset_h``
    - recurring: ``rrule`` (RFC 5545 RRULE string), ``dtstart``,
      ``duration_h``, ``as_`` fixed|deadline (what each instance becomes)
    - habit: ``count`` per ``per`` (day / week / month)
    """

    type: TimeType = "none"
    due: datetime | None = None
    due_earliest: datetime | None = None
    start: datetime | None = None
    end: datetime | None = None
    allday: bool = False
    target: str | None = None
    relation: Literal["before", "after"] | None = None
    offset_h: float = 0.0
    rrule: str | None = None
    dtstart: datetime | None = None
    duration_h: float | None = None
    as_: Literal["fixed", "deadline"] = Field("fixed", alias="as")
    count: int | None = None
    per: Literal["day", "week", "month"] | None = None

    model_config = {"populate_by_name": True}

    @model_validator(mode="after")
    def _check(self) -> "TimeSpec":
        t = self.type
        if t == "deadline" and self.due is None:
            raise ValueError("deadline needs 'due'")
        if t == "fixed" and (self.start is None or self.end is None):
            raise ValueError("fixed needs 'start' and 'end'")
        if t == "anchored" and (self.target is None or self.relation is None):
            raise ValueError("anchored needs 'target' and 'relation'")
        if t == "recurring" and (self.rrule is None or self.dtstart is None):
            raise ValueError("recurring needs 'rrule' and 'dtstart'")
        if t == "habit" and (not self.count or self.per is None):
            raise ValueError("habit needs 'count' and 'per'")
        return self


class Plan(BaseModel):
    """When the user intends to do it. Precision: range (這週) / part (今晚) / slot."""

    precision: Literal["none", "range", "part", "slot"] = "none"
    start: datetime | None = None
    end: datetime | None = None
    label: str | None = None  # 原話，例如「今晚」


class Commitment(BaseModel):
    level: Value | None = None  # v: 0 = must, 1 = try, 2 = droppable (see LEVELS)
    cost: Value | None = None  # 推掉 / 延後的代價，0–10

    @property
    def level_name(self) -> str:
        if self.level is None:
            return "try"
        return LEVELS[max(0, min(2, round(self.level.v)))]


LEVELS = ["must", "try", "droppable"]
Status = Literal["open", "active", "done", "dropped"]


class Item(BaseModel):
    id: str
    text: str
    parent: str | None = None
    category: str | None = None
    time: TimeSpec = Field(default_factory=TimeSpec)
    plan: Plan | None = None
    effort: Value | None = None  # 剩餘工時（小時）
    commitment: Commitment = Field(default_factory=Commitment)
    status: Status = "open"
    created: datetime
    updated: datetime
    # 外部來源資訊（行事曆匯入的 uid、課程、網址…）與其他雜項
    meta: dict[str, Any] = Field(default_factory=dict)


LogKind = Literal[
    "created",
    "planned",
    "started",
    "done",
    "deferred",
    "skipped",
    "dropped",
    "rec_accepted",
    "rec_rejected",
    "estimate_corrected",
    "said",
    "alert_feedback",
    "updated",
]


class Log(BaseModel):
    id: int | None = None
    ts: datetime
    item_id: str | None = None
    kind: LogKind
    data: dict[str, Any] = Field(default_factory=dict)


class Observation(BaseModel):
    id: str
    text: str
    scope: str | None = None  # category or situation
    params: dict[str, Any] = Field(default_factory=dict)
    evidence: list[int] = Field(default_factory=list)  # log ids
    confidence: float = 0.3
    status: Literal["hypothesis", "confirmed", "rejected"] = "hypothesis"
    last_seen: datetime
    origin: Literal["stats", "reflection", "user"] = "stats"
