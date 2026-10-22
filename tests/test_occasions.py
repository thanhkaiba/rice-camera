"""Holiday backgrounds for the late picture."""
import datetime as dt

import cv2
import numpy as np
import pytest

from attendance.core import latephoto
from attendance.core.occasions import OCCASIONS, easter, lunar_to_solar, occasion_on, upcoming

D = dt.date.fromisoformat


@pytest.mark.parametrize("year,tet", [(1985, "1985-01-21"), (2007, "2007-02-17"), (2024, "2024-02-10"),
                                      (2025, "2025-01-29"), (2026, "2026-02-17"), (2027, "2027-02-06"),
                                      (2030, "2030-02-02")])
def test_tet_follows_the_vietnamese_calendar(year, tet):
    """1985, 2007 and 2030 are years when Vietnam's Tet differs from China's."""
    assert lunar_to_solar(1, 1, year) == D(tet)


def test_other_lunar_and_moving_dates():
    assert [lunar_to_solar(15, 8, y) for y in (2024, 2025, 2026)] == [D("2024-09-17"), D("2025-10-06"), D("2026-09-25")]
    assert [lunar_to_solar(10, 3, y) for y in (2024, 2025, 2026)] == [D("2024-04-18"), D("2025-04-07"), D("2026-04-26")]
    assert [easter(y) for y in (2024, 2025, 2026)] == [D("2024-03-31"), D("2025-04-20"), D("2026-04-05")]


@pytest.mark.parametrize("day,key", [
    ("2026-10-14", None),  # nothing near
    ("2026-10-15", "vn_womens_day"), ("2026-10-25", "vn_womens_day"),  # 5 days either side of 20/10
    ("2026-10-26", "halloween"),  # nearer to 31/10 than to 20/10
    ("2026-12-15", "christmas"), ("2026-12-28", "christmas"), ("2026-12-29", "new_year"), ("2027-01-11", "new_year"),
    ("2027-01-12", None),
    ("2027-01-27", "tet"), ("2027-02-08", "tet"),  # Tet 6/2/2027 ± 10,
    ("2027-02-14", "valentine"), ("2027-02-16", "valentine"),  # but nearer to Valentine's Day after the 9th
    ("2026-04-25", "hung_kings"), ("2026-04-28", "reunification"),  # Gio To 26/4/2026, next to 30/4
    ("2027-09-06", "national_day"), ("2027-09-11", "mid_autumn"),  # Trung thu 15/9/2027
])
def test_the_nearest_occasion_within_its_days(day, key):
    o = occasion_on(D(day))
    assert (o.key if o else None) == key


def test_equally_near_the_coming_one_wins():
    assert occasion_on(D("2026-04-28")).key == "reunification"  # Gio To 26/4 and 30/4 both 2 days away
    assert len({o.key for o in OCCASIONS}) == 14
    assert [o.key for o, _ in upcoming(D("2026-10-22"))] == ["halloween", "teachers", "christmas"]


def test_holiday_pictures_come_before_the_daily_ones_and_the_admins_own_first(tmp_path):
    christmas = D("2026-12-20")
    own = tmp_path / latephoto.FOLDER / latephoto.BACKGROUNDS / "christmas"
    own.mkdir(parents=True)
    cv2.imwrite(str(own / "ours.jpg"), np.full((90, 160, 3), 128, np.uint8))
    assert latephoto.day_background(tmp_path, christmas) == own / "ours.jpg"
    assert latephoto.day_background(tmp_path, christmas + dt.timedelta(1)) == own / "ours.jpg"  # all through it
    assert latephoto.day_background(tmp_path, christmas, occasions=False).parent == latephoto.BUNDLED
    single = latephoto.background_path(tmp_path)
    cv2.imwrite(str(single), np.full((90, 160, 3), 200, np.uint8))
    assert latephoto.day_background(tmp_path, christmas) == single  # the one uploaded in Settings wins


def test_every_occasion_has_a_bundled_picture():
    folder = latephoto.BUNDLED / latephoto.OCCASIONS
    missing = [o.key for o in OCCASIONS if not list(folder.glob(o.key + "*.jpg"))]
    assert missing == []
    for o in OCCASIONS:
        assert latephoto.day_background(latephoto.BUNDLED.parent / "nowhere", o.on(2027)).parent == folder
