"""The site capture: scrubbing rules, and a real run against the fake site.

The capture logs in to the owner's real account, so the two things that must
hold are tested directly: it never books, and nothing personal reaches its
output files.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest
from fake_site import FakeCourtReserve, SiteState

from pickletime.capture import capture_site, json_shape, scrub_text, scrub_url
from pickletime.timeutil import format_long_date, format_us_date


# -- scrubbing ----------------------------------------------------------------
@pytest.mark.parametrize(
    "text",
    ["6:30 PM", "Reserve", "60 minutes", "Friday, November 13", "Pickleball Reservations",
     "Today", "1 hour & 30 minutes", "Court 3"],
)
def test_interface_text_is_kept(text):
    assert scrub_text(text) == text


@pytest.mark.parametrize(
    "text",
    ["Shashank Kapadia", "smhk@example.com", "Bring extra paddles for Priya", "Member 12345678"],
)
def test_personal_text_is_replaced(text):
    out = scrub_text(text)
    assert out.startswith("«")
    assert text not in out


def test_urls_keep_paths_and_plain_ids_but_drop_free_values():
    url = "https://app.courtreserve.com/Online/Reservations/Bookings/13233?sId=16984&name=Shashank%20K&email=a%40b.com"
    out = scrub_url(url)
    assert out.startswith("https://app.courtreserve.com/Online/Reservations/Bookings/13233?sId=16984")
    assert "Shashank" not in out and "a@b.com" not in out and "%40" not in out


def test_json_shape_keeps_structure_and_times_but_not_names():
    shape = json_shape(
        {"slots": [{"start": "2026-11-21T18:30:00", "court": "Court 2", "member": "Shashank Kapadia",
                    "id": 123, "available": True}] * 5}
    )
    slot = shape["slots"][0]
    assert slot["start"] == "2026-11-21T18:30:00"
    assert slot["court"] == "Court 2"
    assert slot["member"].startswith("«str:")
    assert slot["available"] is True
    assert shape["slots"][-1] == "… 5 items"


# -- a real run -----------------------------------------------------------------
@pytest.mark.e2e
@pytest.mark.parametrize("open_slot", [False, True])
def test_capture_never_books_and_leaks_nothing(open_slot, chrome_paths, make_settings, tmp_path: Path):
    binary, driver = chrome_paths
    state = SiteState(slots={"11:30 AM": ["30 minutes", "1 hour"]})
    overrides = {"chrome_binary": binary, "days_in_advance": 0}
    if driver:
        overrides["chrome_driver_path"] = driver
    target = make_settings(**overrides).booking.target_date()
    titles = [format_long_date(target + timedelta(days=o)) for o in (-1, 0, 1)]

    with FakeCourtReserve(state, titles, format_us_date(target)) as site:
        settings = make_settings(base_url=site.base_url, **overrides)
        result = capture_site(settings, tmp_path / "cap", open_slot=open_slot)

    steps = {s["step"]: s for s in result["steps"]}
    assert steps["login"]["ok"], result["steps"]
    assert steps["bookings_page"]["ok"]
    assert "legacy Kendo grid present: True" in steps["bookings_page"]["detail"]
    assert steps["slot_candidates"]["ok"], result["steps"]

    # Never books: the fake site records every submitted reservation.
    assert state.bookings == []

    if open_slot:
        assert steps["open_slot"]["ok"], result["steps"]
        assert "dialog found: True" in steps["open_slot"]["detail"]
        assert result.get("slot_controls"), "the booking form's controls were not captured"
        assert any(c.get("testid") == "save-btn" for c in result["slot_controls"])

    # Nothing personal in any written text file.
    written = "".join(p.read_text(encoding="utf-8") for p in (tmp_path / "cap").glob("*.*") if p.suffix in {".txt", ".json"})
    assert "player@example.com" not in written
    assert "correct-horse" not in written
    assert (tmp_path / "cap" / "summary.txt").is_file()
    assert list((tmp_path / "cap").glob("*.png"))


def test_slot_links_keep_their_date_time_and_ids():
    href = ("/Online/Reservations/CreateReservation/13233?start=11/21/2026%206:30%20PM"
            "&end=11/21/2026%207:30%20PM&courtType=Pickleball&customSchedulerId=16984&note=Shashank")
    out = scrub_url(href)
    assert out == ("/Online/Reservations/CreateReservation/13233?start=11/21/2026 6:30 PM"
                   "&end=11/21/2026 7:30 PM&courtType=Pickleball&customSchedulerId=16984&note=«v»")


def test_fragment_links_are_left_alone():
    assert scrub_url("#") == "#"


def test_the_safe_word_list_holds_only_plain_words():
    """A stray comment or punctuation in the list would quietly let text through."""
    from pickletime.capture import _VOCAB

    assert _VOCAB and all(word.isalpha() and word.islower() for word in _VOCAB)
