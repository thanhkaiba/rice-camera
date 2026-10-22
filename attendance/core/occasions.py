"""Holidays and festive days, Vietnamese and Western, for the late picture's background.

Around each occasion the late picture takes that occasion's background: from 5 days before to
5 days after it, 10 for the big ones (Tet, New Year, Christmas, Mid-Autumn, 30 April - 1 May,
National Day), since the day itself is often a day off. Where two overlap, the nearer one wins,
and of two equally near the one still to come.

Lunar dates (Tet, the Hung Kings' day, Mid-Autumn) follow the Vietnamese calendar, computed for
UTC+7 with Ho Ngoc Duc's algorithm (astronomical new moons and solar terms, after Jean Meeus):
it differs from the Chinese calendar in some years, e.g. Tet 1985 fell a month apart.
"""
from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass
from typing import Callable

TZ = 7.0  # Vietnam's lunar calendar is reckoned at UTC+7
SMALL, BIG = 5, 10  # days around an occasion its background is shown


# Vietnamese lunar calendar ------------------------------------------------------------------

def _jd(day: dt.date) -> int:
    """Julian day number."""
    return day.toordinal() + 1721425


def _date(jd: int) -> dt.date:
    return dt.date.fromordinal(jd - 1721425)


def _new_moon(k: int) -> float:
    """Julian day of the k-th new moon after 1900-01-01 13:52 UTC."""
    t = k / 1236.85
    t2, t3 = t * t, t * t * t
    dr = math.pi / 180
    jd1 = 2415020.75933 + 29.53058868 * k + 0.0001178 * t2 - 0.000000155 * t3
    jd1 += 0.00033 * math.sin((166.56 + 132.87 * t - 0.009173 * t2) * dr)
    m = 359.2242 + 29.10535608 * k - 0.0000333 * t2 - 0.00000347 * t3  # sun's mean anomaly
    mpr = 306.0253 + 385.81691806 * k + 0.0107306 * t2 + 0.00001236 * t3  # moon's mean anomaly
    f = 21.2964 + 390.67050646 * k - 0.0016528 * t2 - 0.00000239 * t3  # moon's argument of latitude
    c1 = (0.1734 - 0.000393 * t) * math.sin(m * dr) + 0.0021 * math.sin(2 * dr * m)
    c1 = c1 - 0.4068 * math.sin(mpr * dr) + 0.0161 * math.sin(dr * 2 * mpr)
    c1 = c1 - 0.0004 * math.sin(dr * 3 * mpr)
    c1 = c1 + 0.0104 * math.sin(dr * 2 * f) - 0.0051 * math.sin(dr * (m + mpr))
    c1 = c1 - 0.0074 * math.sin(dr * (m - mpr)) + 0.0004 * math.sin(dr * (2 * f + m))
    c1 = c1 - 0.0004 * math.sin(dr * (2 * f - m)) - 0.0006 * math.sin(dr * (2 * f + mpr))
    c1 = c1 + 0.0010 * math.sin(dr * (2 * f - mpr)) + 0.0005 * math.sin(dr * (2 * mpr + m))
    if t < -11:
        delta_t = 0.001 + 0.000839 * t + 0.0002261 * t2 - 0.00000845 * t3 - 0.000000081 * t * t3
    else:
        delta_t = -0.000278 + 0.000265 * t + 0.000262 * t2
    return jd1 + c1 - delta_t


def _new_moon_day(k: int) -> int:
    return math.floor(_new_moon(k) + 0.5 + TZ / 24)


def _sun_longitude(jdn: float) -> float:
    """The sun's longitude, in radians, at Julian day `jdn` (UTC)."""
    t = (jdn - 2451545.0) / 36525
    t2 = t * t
    dr = math.pi / 180
    m = 357.52910 + 35999.05030 * t - 0.0001559 * t2 - 0.00000048 * t * t2
    l0 = 280.46645 + 36000.76983 * t + 0.0003032 * t2
    dl = (1.914600 - 0.004817 * t - 0.000014 * t2) * math.sin(dr * m)
    dl += (0.019993 - 0.000101 * t) * math.sin(dr * 2 * m) + 0.000290 * math.sin(dr * 3 * m)
    lon = (l0 + dl) * dr
    return lon - math.pi * 2 * math.floor(lon / (math.pi * 2))


def _sun_sector(day_number: int) -> int:
    """Which of the 12 solar-term sectors (0-11) the sun is in at the start of that local day."""
    return math.floor(_sun_longitude(day_number - 0.5 - TZ / 24) / math.pi * 6)


def _month11(year: int) -> int:
    """Julian day on which the lunar month holding the winter solstice of `year` begins."""
    k = math.floor((_jd(dt.date(year, 12, 31)) - 2415021) / 29.530588853)
    nm = _new_moon_day(k)
    return _new_moon_day(k - 1) if _sun_sector(nm) >= 9 else nm


def _leap_offset(a11: int) -> int:
    """In a year with 13 lunar months: the leap month's place after month 11 (the first without a solar term)."""
    k = math.floor((a11 - 2415021.076998695) / 29.530588853 + 0.5)
    i, arc = 1, _sun_sector(_new_moon_day(k + 1))
    while True:
        last = arc
        i += 1
        arc = _sun_sector(_new_moon_day(k + i))
        if arc == last or i >= 14:
            return i - 1


def lunar_to_solar(day: int, month: int, year: int) -> dt.date:
    """A date of the Vietnamese lunar calendar (not a leap month) -> the Gregorian date."""
    if month < 11:
        a11, b11 = _month11(year - 1), _month11(year)
    else:
        a11, b11 = _month11(year), _month11(year + 1)
    k = math.floor(0.5 + (a11 - 2415021.076998695) / 29.530588853)
    off = (month - 11) % 12
    if b11 - a11 > 365 and off >= _leap_offset(a11):
        off += 1  # a leap month came before this one
    return _date(_new_moon_day(k + off) + day - 1)


def easter(year: int) -> dt.date:
    """Western (Gregorian) Easter Sunday: the anonymous Gregorian algorithm."""
    a, b, c = year % 19, year // 100, year % 100
    d, e = b // 4, b % 4
    g = (8 * b + 13) // 25
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 19 * l) // 433
    month = (h + l - 7 * m + 90) // 25
    return dt.date(year, month, (h + l - 7 * m + 33 * month + 19) % 32)


# occasions -----------------------------------------------------------------------------------

@dataclass(frozen=True)
class Occasion:
    key: str  # also the folder name for the admin's own pictures: late/backgrounds/<key>/
    name: str
    big: bool
    on: Callable[[int], dt.date]  # its date in a given year

    @property
    def days(self) -> int:
        return BIG if self.big else SMALL


def _fixed(month: int, day: int) -> Callable[[int], dt.date]:
    return lambda year: dt.date(year, month, day)


OCCASIONS = (
    Occasion("tet", "Tết Nguyên Đán", True, lambda y: lunar_to_solar(1, 1, y)),
    Occasion("new_year", "New Year", True, _fixed(1, 1)),
    Occasion("valentine", "Valentine's Day", False, _fixed(2, 14)),
    Occasion("womens_day", "International Women's Day", False, _fixed(3, 8)),
    Occasion("easter", "Easter", False, easter),
    Occasion("hung_kings", "Giỗ Tổ Hùng Vương", False, lambda y: lunar_to_solar(10, 3, y)),
    Occasion("reunification", "30/4 – 1/5", True, _fixed(4, 30)),
    Occasion("children", "Children's Day", False, _fixed(6, 1)),
    Occasion("national_day", "Quốc khánh 2/9", True, _fixed(9, 2)),
    Occasion("mid_autumn", "Tết Trung thu", True, lambda y: lunar_to_solar(15, 8, y)),
    Occasion("vn_womens_day", "Vietnamese Women's Day", False, _fixed(10, 20)),
    Occasion("halloween", "Halloween", False, _fixed(10, 31)),
    Occasion("teachers", "Teachers' Day", False, _fixed(11, 20)),
    Occasion("christmas", "Christmas", True, _fixed(12, 25)),
)
BY_KEY = {o.key: o for o in OCCASIONS}


def occasion_on(day: dt.date) -> Occasion | None:
    """The occasion whose background `day` takes, if any: within its days of it, the nearest one,
    the coming one of two equally near."""
    best = None
    for o in OCCASIONS:
        for year in (day.year - 1, day.year, day.year + 1):
            gap = (o.on(year) - day).days  # > 0: still to come
            if abs(gap) <= o.days:
                rank = (abs(gap), gap < 0)
                if best is None or rank < best[0]:
                    best = (rank, o)
    return best[1] if best else None


def upcoming(day: dt.date, n: int = 3) -> list[tuple[Occasion, dt.date]]:
    """The next n occasions from `day` on (for the Settings page)."""
    dates = sorted(((o.on(y), o) for o in OCCASIONS for y in (day.year, day.year + 1) if o.on(y) >= day),
                   key=lambda pair: pair[0])
    return [(o, d) for d, o in dates[:n]]
