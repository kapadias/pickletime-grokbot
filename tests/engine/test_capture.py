"""The site capture: scrubbing rules, and a real run against the fake site.

The capture logs in to the owner's real account, so the two things that must
hold are tested directly: it never books, and nothing personal reaches its
output files.
"""

from __future__ import annotations

import json
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


@pytest.mark.e2e
def test_a_failed_date_step_still_captures_a_booking_form(chrome_paths, make_settings, tmp_path: Path):
    """Stepping 8 days on a toolbar it does not understand must not cost the capture.

    The fake site has no next-day button, so stepping fails; the capture must
    fall back to today's slots, open one form, and still book nothing.
    """
    binary, driver = chrome_paths
    state = SiteState(slots={"11:30 AM": ["30 minutes", "1 hour"]})
    overrides = {"chrome_binary": binary, "days_in_advance": 8}
    if driver:
        overrides["chrome_driver_path"] = driver
    target = make_settings(**overrides).booking.target_date()
    titles = [format_long_date(target + timedelta(days=o)) for o in (-1, 0, 1)]

    with FakeCourtReserve(state, titles, format_us_date(target)) as site:
        settings = make_settings(base_url=site.base_url, **overrides)
        result = capture_site(settings, tmp_path / "cap", open_slot=True)

    steps = {s["step"]: s for s in result["steps"]}
    assert steps["open_slot"]["ok"], result["steps"]
    assert result.get("slot_controls")
    assert state.bookings == []


# -- against the new portal layout --------------------------------------------------
@pytest.mark.e2e
def test_capture_on_the_new_portal_layout(chrome_paths, make_settings, tmp_path: Path):
    """Everything the engine rewrite needs, gathered from a portal-shaped page."""
    from fake_portal import FakePortal, PortalState

    binary, driver = chrome_paths
    overrides = {"chrome_binary": binary, "days_in_advance": 8}
    if driver:
        overrides["chrome_driver_path"] = driver
    target = make_settings(**overrides).booking.target_date()
    state = PortalState(today=target - timedelta(days=8),
                        open_slots={target: {"6:30 PM": 2, "7:00 PM": 1, "9:00 AM": 3}})

    with FakePortal(state) as site:
        settings = make_settings(base_url=site.base_url, **overrides)
        result = capture_site(settings, tmp_path / "cap", open_slot=True)

    steps = {s["step"]: s for s in result["steps"]}
    assert steps["login"]["ok"], result["steps"]
    assert "legacy Kendo grid present: False" in steps["bookings_page"]["detail"]

    # Stepped all 8 days with the toolbar's own control, not the decoy carousel.
    assert steps["date_nav"]["ok"], steps["date_nav"]
    assert result["date_nav"]["how"] == "toolbar control naming next"
    assert len(result["date_nav"]["trail"]) == 8
    assert state.reads[-1] == target.isoformat()

    # Evening slots sit below the fold; the map must still pair them with times.
    times = {m["time"] for m in result["slot_map"] if m["testid"] == "consolidated-reserve-slot"}
    assert {"9:00 AM", "6:30 PM", "7:00 PM"} <= times, result["slot_map"]

    # One form opened; its duration list read; nothing booked.
    assert steps["open_slot"]["ok"], result["steps"]
    assert steps["duration_list"]["ok"], result["steps"]
    assert [o["text"] for o in result["duration_list"]["options"]] == [
        "30 minutes", "1 hour", "1 hour & 30 minutes", "2 hours"]
    controls = result["slot_controls"]
    assert any(c.get("testid") == "reservation-mobile-submit-btn" for c in controls)
    assert any(c.get("role") == "checkbox" and c.get("checked") == "false" for c in controls)
    assert state.bookings == []

    assert "consolidated-reserve-slot" in result["testids"]
    reads = [r for r in result["network"] if "ReadConsolidated" in r["url"]]
    assert reads and "«json:" in reads[-1]["url"] and "startDate" in reads[-1]["url"]
    assert "/Date(" in json.dumps(reads[-1]["shape"])

    written = (tmp_path / "cap" / "summary.txt").read_text(encoding="utf-8")
    assert "player@example.com" not in written and "correct-horse" not in written
    assert written.index("== slot map") < written.index("== data requests")


def test_dotnet_dates_survive_the_shape():
    assert json_shape({"Start": "/Date(1763136000000)/"}) == {"Start": "/Date(1763136000000)/"}


@pytest.mark.e2e
def test_capture_on_the_reserve_button_grid(chrome_paths, make_settings, tmp_path: Path):
    """The 19 Nov grid: Reserve buttons map to times, and a refusal is not the form.

    The first Reserve answers "Reservation unavailable" (no duration control).
    The form on the next slot appears only after a delay. Capture must wait,
    record the real controls, and still book nothing.
    """
    from fake_portal import PortalState
    from fake_portal_grid import FakePortalGrid

    binary, driver = chrome_paths
    overrides = {"chrome_binary": binary, "days_in_advance": 8}
    if driver:
        overrides["chrome_driver_path"] = driver
    target = make_settings(**overrides).booking.target_date()
    state = PortalState(
        today=target - timedelta(days=8),
        form_delay_ms=1200,
        open_slots={target: {"8:00 AM": 1, "9:00 AM": 2, "6:30 PM": 1, "7:00 PM": 1}},
        refuse_open={"8:00 AM"},
    )

    with FakePortalGrid(state) as site:
        settings = make_settings(base_url=site.base_url, **overrides)
        result = capture_site(settings, tmp_path / "cap", open_slot=True)

    steps = {s["step"]: s for s in result["steps"]}
    assert steps["login"]["ok"], result["steps"]
    assert "legacy Kendo grid present: False" in steps["bookings_page"]["detail"]
    assert steps["date_nav"]["ok"], steps["date_nav"]
    assert result["date_nav"]["how"] == "toolbar control naming next"
    assert state.reads[-1] == target.isoformat()

    reserves = {m["time"] for m in result["slot_map"] if m.get("text") == "Reserve"}
    assert {"9:00 AM", "6:30 PM", "7:00 PM"} <= reserves, result["slot_map"]
    assert all(not m.get("testid") for m in result["slot_map"] if m.get("text") == "Reserve")
    unavailable = {m["time"] for m in result["slot_map"] if m.get("text") == "None Available"}
    assert "10:30 AM" in unavailable

    assert "consolidated-reserve-slot" not in result["testids"]
    assert "scheduler-page" not in result["testids"]
    assert "scheduler-next-btn" in result["testids"]

    assert steps["open_slot"]["ok"], result["steps"]
    assert "skipped 1 refusal" in steps["open_slot"]["detail"]
    assert steps["duration_list"]["ok"], result["steps"]
    assert [o["text"] for o in result["duration_list"]["options"]] == [
        "30 minutes", "1 hour", "1 hour & 30 minutes", "2 hours"]
    assert any(c.get("testid") == "reservation-mobile-submit-btn" for c in result["slot_controls"])
    assert state.bookings == []

    written = (tmp_path / "cap" / "summary.txt").read_text(encoding="utf-8")
    assert "player@example.com" not in written and "correct-horse" not in written
    assert "Reservation unavailable" in written
    assert written.index("== slot map") < written.index("== data requests")
