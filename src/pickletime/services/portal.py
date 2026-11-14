"""Booking on CourtReserve's member portal (the newer, single-page layout).

Lifetime Sunnyvale moved to this layout in November 2026. Everything here was
built from scrubbed captures of the live page (``run.sh capture --open-slot``):

* the scheduler shows one day; a toolbar steps it with ``scheduler-next-btn``,
  which the page disables while a day loads and beyond the last released day;
* each half-hour row is either an open slot
  (``[data-testid=consolidated-reserve-slot]``, "Reserve"), "None Available",
  or empty (past). Rows line up with a column of time labels;
* clicking a slot opens a form (``reservation-create-modal``) that shows the
  slot's date and time, a duration combobox, a disclosure checkbox, and
  Confirm (``reservation-mobile-submit-btn``).

What the site does after Confirm has not yet been seen live. So a booking
counts as confirmed only when a message says so; a form that closes silently
is reported as unverified (the run stops rather than risk a second court),
and every outcome records the page's messages so the first real booking
teaches us the site's confirmation.

The plan (which times, which durations, in what order) and the sprint loop are
inherited from :class:`BookingService`; only the site-specific steps differ.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Sequence
from datetime import date as date_type
from typing import Any

from ..core import locators
from ..errors import BookingError
from ..logging_setup import get_logger
from ..timeutil import format_long_date, format_us_date, now_in
from .booking import BookingService, SlotCandidate, SlotOutcome

logger = get_logger()

__all__ = ["PortalBookingService", "parse_portal_label"]

_MONTHS = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july", "august",
     "september", "october", "november", "december"], start=1)}

_SUCCESS = re.compile(r"\b(success|successful|successfully|confirmed|reserved|booked|created|thank you)\b", re.I)
_FAILURE = re.compile(
    r"\b(error|unable|unavailable|not available|no longer|failed|conflict|already|limit|exceed\w*|"
    r"cannot|can't|could not|invalid|required|denied|insufficient|overlap\w*|maximum)\b",
    re.I,
)


def parse_portal_label(text: str | None, today: date_type) -> date_type | None:
    """``"Saturday, November 14"`` -> the nearest such date to ``today``.

    The label carries no year, so pick the year that puts the date closest to
    today (December-to-January stepping must not jump a year).
    """
    if not text:
        return None
    match = re.search(r"([A-Za-z]+)\s+(\d{1,2})\b", text.split(",", 1)[-1])
    if not match or match.group(1).lower() not in _MONTHS:
        return None
    month, day = _MONTHS[match.group(1).lower()], int(match.group(2))
    best: date_type | None = None
    for year in (today.year - 1, today.year, today.year + 1):
        try:
            candidate = date_type(year, month, day)
        except ValueError:
            continue
        if best is None or abs((candidate - today).days) < abs((best - today).days):
            best = candidate
    return best


# --------------------------------------------------------------------------
# In-page JavaScript
# --------------------------------------------------------------------------
_JS_NAV_STATE = """
var label = document.querySelector(arguments[0]);
var next = document.querySelector(arguments[1]);
var prev = document.querySelector(arguments[2]);
var enabled = function (b) { return !!b && !b.disabled && b.getAttribute('aria-disabled') !== 'true'; };
return {label: label ? label.textContent.trim() : null, next: enabled(next), prev: enabled(prev),
        hasNext: !!next};
"""

#: How many slot-data reads the page has completed: a new one means a day loaded.
_JS_READ_COUNT = """
return performance.getEntriesByType('resource')
  .filter(function (e) { return e.name.indexOf('ReadConsolidated') !== -1; }).length;
"""

_JS_CLICK = "arguments[0].click(); return true;"

#: The open-slot element on the row whose time label is ``arguments[0]``.
_JS_FIND_SLOT = """
var want = arguments[0].replace(/\\s+/g, ' ').trim().toUpperCase();
var page = document.querySelector(arguments[1]) || document.body;
var TIME = /^\\s*\\d{1,2}:\\d{2}\\s?(AM|PM)\\s*$/i;
var labels = Array.prototype.filter.call(page.querySelectorAll('*'), function (e) {
  return e.children.length === 0 && TIME.test(e.textContent || '') && !e.closest('[role=dialog]');
});
var mine = labels.filter(function (l) { return l.textContent.replace(/\\s+/g, ' ').trim().toUpperCase() === want; })[0];
if (!mine) { return {state: 'no-label', labels: labels.length}; }
mine.scrollIntoView({block: 'center'});
var tops = labels.map(function (l) { return {l: l, top: l.getBoundingClientRect().top}; });
var slots = page.querySelectorAll(arguments[2]);
for (var i = 0; i < slots.length; i++) {
  var r = slots[i].getBoundingClientRect();
  if (r.height === 0) { continue; }
  var mid = r.top + r.height / 2, best = null;
  for (var j = 0; j < tops.length; j++) {
    if (tops[j].top <= mid + 2 && (!best || tops[j].top > best.top)) { best = tops[j]; }
  }
  if (best && best.l === mine) { return {state: 'found', el: slots[i]}; }
}
return {state: 'not-open', slots: slots.length};
"""

_JS_MODAL_OPEN = """
var m = document.querySelector(arguments[0]);
if (!m) { return false; }
var d = m.querySelector('[role=dialog]') || m;
var cs = getComputedStyle(d);
return d.offsetParent !== null && cs.display !== 'none' && cs.visibility !== 'hidden';
"""

_JS_MODAL_PARTS = """
var m = document.querySelector(arguments[0]);
if (!m) { return null; }
var q = function (sel) { return m.querySelector(sel); };
var box = q(arguments[3]);
var checkbox = box ? box.closest('[role=checkbox]') : q('[role=checkbox]');
var confirm = q(arguments[4]);
if (!confirm) {
  confirm = Array.prototype.filter.call(m.querySelectorAll('button'), function (b) {
    return (b.innerText || '').trim().toLowerCase() === 'confirm';
  })[0] || null;
}
var dt = q(arguments[1]);
return {
  dateTime: dt ? dt.innerText.replace(/\\s+/g, ' ').trim() : null,
  combo: q(arguments[2]),
  comboText: q(arguments[2]) ? q(arguments[2]).innerText.replace(/\\s+/g, ' ').trim() : null,
  checkbox: checkbox,
  checked: checkbox ? checkbox.getAttribute('aria-checked') : null,
  confirm: confirm,
  confirmText: confirm ? (confirm.innerText || '').trim() : null,
  confirmDisabled: confirm ? (!!confirm.disabled || confirm.getAttribute('aria-disabled') === 'true') : null
};
"""

_JS_OPTIONS = """
return Array.prototype.filter.call(document.querySelectorAll(arguments[0]), function (o) {
  var r = o.getBoundingClientRect(); return r.height > 0;
}).map(function (o) { return {el: o, text: (o.innerText || '').replace(/\\s+/g, ' ').trim()}; });
"""

#: Every visible message: toasts, alerts, and errors inside the form.
_JS_MESSAGES = """
var seen = [], out = {toasts: [], formErrors: []};
var add = function (list, e) {
  var t = (e.innerText || '').replace(/\\s+/g, ' ').trim();
  var r = e.getBoundingClientRect();
  if (t && r.height > 0 && seen.indexOf(t) === -1 && t.length < 400) { seen.push(t); list.push(t); }
};
Array.prototype.forEach.call(document.querySelectorAll(arguments[0]), function (e) {
  if (!e.closest('[role=dialog]')) { add(out.toasts, e); }
});
var m = document.querySelector(arguments[1]);
if (m) {
  Array.prototype.forEach.call(m.querySelectorAll(
    '.ant-form-item-explain-error, [role=alert], .ant-alert-error, [class*=error i], [class*=Error]'), function (e) {
    add(out.formErrors, e);
  });
}
out.url = location.href;
return out;
"""


class PortalBookingService(BookingService):
    """Drives the portal's scheduler and reservation form for one court date."""

    layout = "portal"

    def __init__(self, driver: Any, waiter: Any, settings: Any) -> None:
        super().__init__(driver, waiter, settings)
        self._first_confirm_logged = False

    # -- the toolbar --------------------------------------------------------
    def _nav(self) -> dict[str, Any]:
        state = self.waiter.js(
            _JS_NAV_STATE, locators.PORTAL_DATE_LABEL, locators.PORTAL_NEXT_DAY, locators.PORTAL_PREV_DAY
        )
        return state if isinstance(state, dict) else {}

    def _shown_date(self) -> date_type | None:
        return parse_portal_label(self._nav().get("label"), now_in(self.settings.tz).date())

    def _reads(self) -> int:
        return int(self.waiter.js(_JS_READ_COUNT) or 0)

    def _step(self, forward: bool) -> bool:
        """One day forward or back, waiting for the control and for the day to load."""
        key = "next" if forward else "prev"
        selector = locators.PORTAL_NEXT_DAY if forward else locators.PORTAL_PREV_DAY
        # The page disables the toolbar while a day loads; a click then does
        # nothing. That is how the first live capture stalled after one day.
        if not self.waiter.until(lambda: self._nav().get(key), timeout=self.settings.webdriver_timeout):
            return False
        before, reads = self._nav().get("label"), self._reads()
        button = self.driver.find_element("css selector", selector)
        self.waiter.js(_JS_CLICK, button)
        if not self.waiter.until(lambda: self._nav().get("label") != before, timeout=self.settings.webdriver_timeout):
            return False
        # The label changes at once; the slots arrive with the next data read.
        self.waiter.until(lambda: self._reads() > reads, timeout=self.settings.webdriver_timeout)
        return True

    def _go_to(self, target: date_type) -> tuple[bool, str]:
        for _ in range(400):  # a year either way, with room to spare
            shown = self._shown_date()
            if shown is None:
                return False, f"cannot read the scheduler's date label ({self._nav().get('label')!r})"
            if shown == target:
                return True, ""
            if not self._step(forward=shown < target):
                direction = "forward" if shown < target else "back"
                return False, f"the scheduler will not step {direction} past {format_long_date(shown)}"
        return False, "gave up stepping after 400 days"

    # -- dates (BookingService API) -----------------------------------------
    def _attempt_select_date(self, target: date_type) -> tuple[float | None, str]:
        started = time.monotonic()
        self.court_date = target
        if not self.waiter.until(lambda: self._nav().get("hasNext"), timeout=self.settings.webdriver_timeout):
            return None, "the portal scheduler's toolbar did not render"
        ok, why = self._go_to(target)
        if not ok:
            return None, f"{format_long_date(target)} is not reachable yet: {why}"
        elapsed = time.monotonic() - started
        logger.info(f"Scheduler showing {format_long_date(target)} ({elapsed:.2f}s)")
        return elapsed, ""

    def await_date(self, target: date_type, *, timeout: float | None = None) -> float:
        """Keep trying for a day that opens at the release moment.

        The cheapest probe goes first: the page may enable "next" by itself.
        Then the scheduler's refresh control, and every fourth try a full
        reload (which lands on today and steps forward again).
        """
        limit = self.settings.release_grace_seconds if timeout is None else timeout
        started = time.monotonic()
        attempt = 0
        announced = 0.0
        while True:
            attempt += 1
            if attempt % 4 == 0:
                try:
                    self.driver.refresh()
                except Exception as exc:  # noqa: BLE001 - retried below
                    logger.warning(f"Reload failed: {type(exc).__name__}: {exc}")
            elif attempt > 1:
                self._click_refresh()
            if self.try_select_date(target) is not None:
                waited = time.monotonic() - started
                logger.info(f"{format_long_date(target)} opened after {waited:.2f}s ({attempt} tries)")
                return waited
            elapsed = time.monotonic() - started
            if elapsed >= limit:
                break
            if elapsed - announced >= 30:
                announced = elapsed
                logger.warning(f"{format_long_date(target)} still not open {elapsed:.0f}s after release")
            self.waiter.sleep(0.25)
        raise BookingError(self._date_never_opened(target, limit, attempt))

    def _click_refresh(self) -> bool:
        found = self.driver.find_elements("css selector", locators.PORTAL_REFRESH)
        if not found:
            return False
        reads = self._reads()
        self.waiter.js(_JS_CLICK, found[0])
        self.waiter.until(lambda: self._reads() > reads, timeout=5)
        return True

    def refresh_grid(self) -> str:
        if self._click_refresh():
            return "refreshed"
        try:
            self.driver.refresh()
        except Exception as exc:  # noqa: BLE001 - a failed refresh is not fatal
            logger.warning(f"Page refresh failed: {type(exc).__name__}: {exc}")
            return "failed"
        if self.court_date is not None:
            self.select_date(self.court_date)
        return "reloaded"

    def slot_count(self) -> int:
        return len(self.driver.find_elements("css selector", locators.PORTAL_SLOT))

    # -- one attempt -------------------------------------------------------
    def _find_slot(self, candidate: SlotCandidate) -> dict[str, Any]:
        found = self.waiter.js(_JS_FIND_SLOT, candidate.label, locators.PORTAL_SCHEDULER, locators.PORTAL_SLOT)
        return found if isinstance(found, dict) else {"state": "error"}

    def _modal_open(self) -> bool:
        return bool(self.waiter.js(_JS_MODAL_OPEN, locators.PORTAL_MODAL))

    def _parts(self) -> dict[str, Any]:
        parts = self.waiter.js(
            _JS_MODAL_PARTS,
            locators.PORTAL_MODAL,
            locators.PORTAL_DATE_TIME,
            locators.PORTAL_DURATION_COMBO,
            locators.PORTAL_DISCLOSURE_BOX,
            locators.PORTAL_CONFIRM,
        )
        return parts if isinstance(parts, dict) else {}

    def _messages(self) -> dict[str, Any]:
        raw = self.waiter.js(_JS_MESSAGES, locators.PORTAL_MESSAGES, locators.PORTAL_MODAL)
        return raw if isinstance(raw, dict) else {"toasts": [], "formErrors": []}

    def form_shows(self, text: str | None, candidate: SlotCandidate) -> bool:
        """Does the form's date/time display describe exactly this slot?"""
        if not text:
            return False
        flat = " ".join(text.split()).upper()
        start = candidate.start
        return bool(
            re.search(rf"(^|\s){re.escape(candidate.label.upper())}\s*[–\-]", flat)
            and re.search(rf"\b{start.day}\b", flat)
            and (start.strftime("%A").upper() in flat or start.strftime("%b").upper() in flat)
        )

    def attempt(self, candidate: SlotCandidate) -> SlotOutcome:
        try:
            return self._attempt(candidate)
        except Exception as exc:  # noqa: BLE001 - one bad attempt must not end the sprint
            logger.warning(f"{candidate}: {type(exc).__name__}: {str(exc).splitlines()[0]}")
            self._close_modal()
            return SlotOutcome.ERROR

    def _attempt(self, candidate: SlotCandidate) -> SlotOutcome:
        found = self._find_slot(candidate)
        if found.get("state") != "found":
            logger.debug(f"{candidate.label}: {found.get('state')}")
            return SlotOutcome.UNAVAILABLE

        self.waiter.click(found["el"], f"{candidate.label} slot")
        if not self.waiter.until(self._modal_open, timeout=self.settings.webdriver_timeout):
            logger.warning(f"{candidate.label}: the reservation form did not open")
            return SlotOutcome.ERROR
        parts = self.waiter.until(lambda: (lambda p: p if p.get("dateTime") else None)(self._parts()), timeout=5) or {}

        # Never confirm a form for a different slot than the one we meant.
        if not self.form_shows(parts.get("dateTime"), candidate):
            logger.error(
                f"{candidate}: the form shows {parts.get('dateTime')!r}, not "
                f"{candidate.start:%A %b %-d} {candidate.label}; closing it without confirming"
            )
            self._close_modal()
            return SlotOutcome.ERROR

        outcome = self._choose_duration(candidate, parts)
        if outcome is not None:
            return outcome
        if not self._agree(candidate):
            return SlotOutcome.ERROR

        if self.settings.dry_run:
            logger.info(f"DRY RUN: {candidate} is open and the form is ready; not confirming")
            self._close_modal()
            return SlotOutcome.WOULD_BOOK
        return self._confirm(candidate)

    def _choose_duration(self, candidate: SlotCandidate, parts: dict[str, Any]) -> SlotOutcome | None:
        want = self.settings.label_for_duration(candidate.duration)
        norm = lambda t: " ".join(str(t or "").split()).lower()  # noqa: E731
        if norm(parts.get("comboText")) == norm(want):
            return None
        combo = parts.get("combo")
        if combo is None:
            logger.warning(f"{candidate}: the form has no duration control")
            self._close_modal()
            return SlotOutcome.ERROR
        self.waiter.click(combo, "duration list")
        options = self.waiter.until(
            lambda: self.waiter.js(_JS_OPTIONS, locators.PORTAL_DURATION_OPTION) or None, timeout=4
        ) or []
        match = next((o for o in options if norm(o.get("text")) == norm(want)), None)
        if match is None:
            offered = [o.get("text") for o in options]
            logger.info(f"{candidate}: {want!r} is not offered here (offered {offered})")
            self._close_modal()
            return SlotOutcome.DURATION_UNAVAILABLE
        self.waiter.click(match["el"], f"duration {want}")
        if not self.waiter.until(lambda: norm(self._parts().get("comboText")) == norm(want), timeout=3):
            logger.warning(f"{candidate}: chose {want!r} but the form still shows {self._parts().get('comboText')!r}")
            self._close_modal()
            return SlotOutcome.ERROR
        return None

    def _agree(self, candidate: SlotCandidate) -> bool:
        parts = self._parts()
        if parts.get("checkbox") is None:
            logger.info(f"{candidate}: no disclosure checkbox in the form; continuing")
            return True
        if parts.get("checked") == "true":
            return True
        self.waiter.click(parts["checkbox"], "disclosure checkbox")
        if self.waiter.until(lambda: self._parts().get("checked") == "true", timeout=3):
            return True
        logger.warning(f"{candidate}: the disclosure checkbox would not tick")
        self._close_modal()
        return False

    def _confirm(self, candidate: SlotCandidate) -> SlotOutcome:
        parts = self._parts()
        button, label = parts.get("confirm"), (parts.get("confirmText") or "").strip()
        if button is None or label.lower() != "confirm" or parts.get("confirmDisabled"):
            logger.warning(f"{candidate}: no usable Confirm button (found {label!r}, disabled={parts.get('confirmDisabled')})")
            self._close_modal()
            return SlotOutcome.ERROR

        before = self._messages()
        stale = set(before.get("toasts", [])) | set(before.get("formErrors", []))
        self.waiter.click(button, "Confirm")
        logger.info(f"Confirmed {candidate}; waiting for the site's answer")

        deadline = self.waiter.now() + max(self.settings.webdriver_timeout, 10)
        closed_at: float | None = None
        last: dict[str, Any] = {}
        while self.waiter.now() < deadline:
            last = self._messages()
            fresh_toasts = [t for t in last.get("toasts", []) if t not in stale]
            fresh_errors = [t for t in last.get("formErrors", []) if t not in stale]
            open_now = self._modal_open()
            text = " | ".join(fresh_errors + fresh_toasts)
            if fresh_errors or (fresh_toasts and _FAILURE.search(text) and not _SUCCESS.search(text)):
                self._log_first_confirm(candidate, "refused", open_now, last)
                logger.info(f"{candidate.label}: refused - {text[:200]}")
                self._close_modal()
                return SlotOutcome.REJECTED
            if fresh_toasts and _SUCCESS.search(text):
                self._log_first_confirm(candidate, "confirmed", open_now, last)
                logger.success(f"BOOKED {candidate} - {text[:200]}")
                self._remember(candidate, "explicit", text)
                return SlotOutcome.BOOKED
            if not open_now:
                # Give a toast a moment to appear after the form closes.
                closed_at = closed_at if closed_at is not None else self.waiter.now()
                if self.waiter.now() - closed_at >= 3:
                    break
            self.waiter.sleep(self.settings.poll_interval_ms / 1000.0)

        self._log_first_confirm(candidate, "no verdict", self._modal_open(), last)
        if self._modal_open():
            logger.warning(f"{candidate.label}: the form is still open {max(self.settings.webdriver_timeout, 10)}s after Confirm")
            self._close_modal()
            return SlotOutcome.REJECTED
        return self._unverified(candidate, "the form closed after Confirm with no message either way")

    def _log_first_confirm(self, candidate: SlotCandidate, verdict: str, still_open: bool, messages: dict[str, Any]) -> None:
        """What the site showed after Confirm, recorded once per run.

        Until a live booking has been observed this is the only way to learn
        the portal's confirmation, so it goes to the log in full.
        """
        if self._first_confirm_logged:
            return
        self._first_confirm_logged = True
        logger.warning(
            f"AFTER CONFIRM ({verdict}) for {candidate}: form still open={still_open}; "
            f"page said {json.dumps({k: messages.get(k) for k in ('toasts', 'formErrors', 'url')})[:800]}"
        )

    def _page_evidence(self) -> str:
        messages = self._messages()
        return json.dumps({"url": messages.get("url"), "messages": (messages.get("toasts") or [])[:5]})[:500]

    def _close_modal(self) -> None:
        if not self._modal_open():
            return
        cancel = self.driver.find_elements("css selector", f"{locators.PORTAL_MODAL} " + locators.PORTAL_CANCEL.split(",")[0])
        cancel += self.driver.find_elements("css selector", f"{locators.PORTAL_MODAL} .ant-modal-close")
        for button in cancel:
            text = (button.text or button.get_attribute("aria-label") or "").strip().lower()
            if text in {"close", "cancel", "×", "x", ""}:
                self.waiter.js(_JS_CLICK, button)
                break
        if not self.waiter.until(lambda: not self._modal_open(), timeout=3):
            from selenium.webdriver.common.keys import Keys  # noqa: PLC0415

            self.driver.find_element("css selector", "body").send_keys(Keys.ESCAPE)
            self.waiter.until(lambda: not self._modal_open(), timeout=2)

    # -- diagnostics ------------------------------------------------------
    def diagnose(self, plan: Sequence[SlotCandidate]) -> dict[str, Any]:
        """Which candidate times have an open slot right now. Opens nothing."""
        labels = list(dict.fromkeys(candidate.label for candidate in plan))
        found = {label: self._find_slot(next(c for c in plan if c.label == label)).get("state") for label in labels}
        return {
            "layout": self.layout,
            "court_date": format_us_date(self.court_date) if self.court_date else None,
            "open_slots": self.slot_count(),
            "candidates": [{"slot": label, "on_grid": state == "found", "state": state} for label, state in found.items()],
        }
