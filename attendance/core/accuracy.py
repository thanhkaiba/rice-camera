"""How well the camera is doing, measured from what admins corrected.

Every identification remembers who the camera said it was. When an admin moves a sighting to
someone else or removes it, that is a wrong identification; when someone's arrival had to be
typed in, or came only from naming an unidentified face, the camera missed them. Watching
these numbers day by day shows whether a change (camera position, photos, settings) helped,
and which people need more reference photos.
"""
from __future__ import annotations

import datetime as dt
from collections import Counter
from dataclasses import dataclass, field


@dataclass
class DayAccuracy:
    day: dt.date
    identified: int = 0  # identifications made by the camera
    wrong: int = 0  # of those, moved to someone else or removed by an admin
    arrivals: int = 0  # people with an arrival that day
    missed: int = 0  # of those, never recognised by the camera that day
    unknown_faces: int = 0  # faces the camera could not name


@dataclass
class PersonAccuracy:
    employee_id: str
    name: str
    photos: int
    missed: int = 0  # days the camera did not recognise them
    taken_as_other: int = 0  # they were recognised as someone else
    others_taken_as_them: int = 0  # someone else was recognised as them

    @property
    def advice(self) -> str:
        tips = []
        if self.missed or self.taken_as_other:
            tips.append("add more photos (name their faces on the review page, or upload)")
        if self.others_taken_as_them >= 2:
            tips.append("check their photos: one may show someone else or look like a colleague")
        if self.photos < 3:
            tips.append("has fewer than 3 photos")
        return "; ".join(tips)


@dataclass
class AccuracyReport:
    start: dt.date
    end: dt.date
    days: list[DayAccuracy]
    confusions: list[tuple[str, str | None, int]]  # (camera said, really, times); really None = "not them"
    people: list[PersonAccuracy] = field(default_factory=list)

    @property
    def identified(self) -> int:
        return sum(d.identified for d in self.days)

    @property
    def wrong(self) -> int:
        return sum(d.wrong for d in self.days)

    @property
    def arrivals(self) -> int:
        return sum(d.arrivals for d in self.days)

    @property
    def missed(self) -> int:
        return sum(d.missed for d in self.days)

    @property
    def unknown_faces(self) -> int:
        return sum(d.unknown_faces for d in self.days)


def accuracy_report(store, gallery, start: dt.date, end: dt.date) -> AccuracyReport:
    names = store.employees()
    by_day: dict[dt.date, DayAccuracy] = {}
    confusions: Counter = Counter()
    people = {e: PersonAccuracy(e, n, gallery.counts().get(e, 0)) for e, n in names.items()}
    sightings = store.sightings_between(start, end)
    recognised: set[tuple[dt.date, str]] = set()

    for s in sightings:
        if s.camera_id is None:
            continue
        d = by_day.setdefault(s.day, DayAccuracy(s.day))
        d.identified += 1
        really = None if s.status == "rejected" else s.employee_id
        if really == s.camera_id:
            recognised.add((s.day, s.camera_id))
            continue
        d.wrong += 1
        confusions[(s.camera_id, really)] += 1
        if s.camera_id in people:
            people[s.camera_id].others_taken_as_them += 1
        if really in people:
            people[really].taken_as_other += 1

    for day, c in store.checkins_between(start, end):
        d = by_day.setdefault(day, DayAccuracy(day))
        d.arrivals += 1
        if (day, c.employee_id) not in recognised:
            d.missed += 1
            if c.employee_id in people:
                people[c.employee_id].missed += 1

    for day, n in store.unknown_counts(start, end).items():
        by_day.setdefault(day, DayAccuracy(day)).unknown_faces = n

    attention = sorted((p for p in people.values() if p.advice),
                       key=lambda p: (-(p.missed + p.taken_as_other + p.others_taken_as_them), p.photos, p.name))
    return AccuracyReport(start, end, sorted(by_day.values(), key=lambda d: d.day),
                          sorted(((a, b, n) for (a, b), n in confusions.items()), key=lambda x: -x[2]), attention)
