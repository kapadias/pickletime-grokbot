"""The booking flow: pick a date, walk candidate slots, confirm the reservation.

Three correctness bugs from the previous implementation are fixed here, and they
are the difference between "the log says booked" and "there is a court waiting".

1. **The duration was never reliably selected.**  The old code ran
   ``setTimeout(function(){ ... opt.click(); }, 200)`` inside ``execute_script``
   and then slept 0.5s.  ``execute_script`` returns as soon as the synchronous
   part finishes, the ``return`` inside the callback went nowhere, and nothing
   ever checked the result — so a slow render silently booked whatever duration
   the modal defaulted to.  Selection is now synchronous, prefers Kendo's own
   widget API, and is *verified* against the control's displayed value before
   anything is saved.

2. **Success was inferred from the absence of a Save button.**  If the modal
   never opened, there was no Save button either, so a total no-op reported
   success and fired the "court booked!" SMS.  Confirmation is now explicit
   where the site gives one, and clearly labelled as *inferred* where it does
   not.

3. **The booked details were thrown away.**  ``last_booking_details`` was never
   assigned, so every success notification said "Court booked successfully"
   with the date, time and duration all ``None``.
"""

from __future__ import annotations

import json
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date as date_type
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any

from ..core import locators
from ..core.waits import Waiter
from ..errors import BookingError
from ..logging_setup import get_logger
from ..settings import BookingSettings
from ..timeutil import format_clock_time, format_long_date, format_us_date, now_in

logger = get_logger()


class SlotOutcome(StrEnum):
    """What happened when we tried one (slot, duration) pair."""

    BOOKED = "booked"
    WOULD_BOOK = "would_book"  # dry run stopped short of saving
    #: Saved, but the site never confirmed and never objected. Might be a
    #: reservation, might be nothing — see _await_confirmation.
    UNVERIFIED = "unverified"
    UNAVAILABLE = "unavailable"  # the slot is not on the grid (or not clickable)
    DURATION_UNAVAILABLE = "duration_unavailable"  # slot exists, this length does not
    REJECTED = "rejected"  # the site refused: taken, conflict, quota
    ERROR = "error"  # something broke mid-attempt


@dataclass(frozen=True)
class SlotCandidate:
    """One (start time, duration) pair to try."""

    start: datetime
    duration: int

    @property
    def label(self) -> str:
        return format_clock_time(self.start.time())

    def __str__(self) -> str:
        return f"{self.label} / {self.duration}min"


@dataclass
class BookingResult:
    """Outcome of a whole booking run."""

    booked: bool
    court_date: str
    slot_time: str | None = None
    duration: int | None = None
    confirmation: str = "none"  # "explicit" | "inferred" | "none"
    dry_run: bool = False
    attempts: int = 0
    unavailable: int = 0
    rejected: int = 0
    detail: str | None = None
    timings: dict[str, float] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "booked": self.booked,
            "date": self.court_date,
            "time": self.slot_time,
            "duration": self.duration,
            "confirmation": self.confirmation,
            "dry_run": self.dry_run,
            "attempts": self.attempts,
            "unavailable": self.unavailable,
            "rejected": self.rejected,
            "detail": self.detail,
            "timings": {k: round(v, 3) for k, v in self.timings.items()},
        }


# --------------------------------------------------------------------------
# JavaScript payloads. Kept as named constants so they can be linted by eye and
# unit-tested by asserting on what the driver was asked to run.
# --------------------------------------------------------------------------

_JS_OPEN_SLOT = """
var exact = arguments[0], needles = arguments[1], sched = arguments[2];
var links = document.querySelectorAll('a[data-href]');
var fallback = null;
for (var i = 0; i < links.length; i++) {
    var href = links[i].getAttribute('data-href') || '';
    if (href === exact) {
        if (links[i].offsetParent === null) { return 'hidden'; }
        links[i].click();
        return 'clicked';
    }
    if (!fallback && href.indexOf(sched) !== -1) {
        for (var n = 0; n < needles.length; n++) {
            if (needles[n] && href.indexOf(needles[n]) !== -1) { fallback = links[i]; break; }
        }
    }
}
if (fallback) {
    if (fallback.offsetParent === null) { return 'hidden'; }
    fallback.click();
    return 'clicked-fallback';
}
return 'missing';
"""

_JS_SET_DURATION_WIDGET = """
var label = arguments[0], displaySelector = arguments[1];
function norm(v) { return String(v == null ? '' : v).replace(/\\s+/g, ' ').trim(); }

if (!window.jQuery) { return {status: 'no-jquery', options: []}; }
var widget = null;
try { widget = window.jQuery('#Duration').data('kendoDropDownList'); } catch (e) { widget = null; }
if (!widget) {
    // The control is not always id'd 'Duration'; fall back to the labelled span.
    try {
        var host = document.querySelector(displaySelector);
        if (host && window.kendo && window.kendo.widgetInstance) {
            widget = window.kendo.widgetInstance(window.jQuery(host));
        }
    } catch (e) { widget = null; }
}
if (!widget || !widget.dataSource) { return {status: 'no-widget', options: []}; }

var items = widget.dataSource.data() || [];
var seen = [];
for (var i = 0; i < items.length; i++) {
    var item = items[i];
    var text = '';
    if (typeof item === 'string') {
        text = item;
    } else if (item) {
        // Known field names first, then any non-empty string field, because the
        // dataSource schema is not documented and varies by tenant.
        text = item.Text || item.text || item.Name || item.name ||
               item.DisplayName || item.Display || item.Label || item.label || '';
        if (!text) {
            for (var key in item) {
                if (typeof item[key] === 'string' && item[key]) { text = item[key]; break; }
            }
        }
    }
    text = norm(text);
    seen.push(text);
    if (text === label) {
        widget.select(i);
        widget.trigger('change');
        return {status: 'selected', options: seen};
    }
}
return {status: 'not-in-list', options: seen};
"""

_JS_OPEN_DURATION_DROPDOWN = """
var dropdown = document.querySelector(arguments[0]);
if (!dropdown) { return 'missing'; }
dropdown.click();
return 'opened';
"""

_JS_PICK_DURATION_OPTION = """
var selector = arguments[0], label = arguments[1];
var options = document.querySelectorAll(selector);
var seen = [];
for (var i = 0; i < options.length; i++) {
    var textNode = options[i].querySelector('.k-list-item-text') || options[i];
    var text = (textNode.textContent || '').replace(/\\s+/g, ' ').trim();
    seen.push(text);
    if (text === label) { options[i].click(); return {status: 'clicked', seen: seen}; }
}
return {status: 'missing', seen: seen};
"""

_JS_PAGE_EVIDENCE = """
/* Whatever the page is currently saying, from the containers a site of this
   kind puts messages in. Deliberately broad: when a save fails silently the
   reason is usually on screen somewhere, and we do not know in advance which
   container this tenant uses. */
var selectors = [
    '.toast', '.toast-message', '.k-notification', '.k-notification-content',
    '.alert', '.alert-danger', '.alert-warning', '.alert-success',
    '.validation-summary-errors', '.field-validation-error',
    '[role="alert"]', '.swal2-title', '.swal2-html-container', '.notyf__message'
];
var messages = [];
for (var s = 0; s < selectors.length; s++) {
    var found = document.querySelectorAll(selectors[s]);
    for (var i = 0; i < found.length; i++) {
        var el = found[i];
        if (el.offsetParent === null) { continue; }
        var text = (el.textContent || '').replace(/\\s+/g, ' ').trim();
        if (text && messages.indexOf(text) === -1) { messages.push(text); }
    }
}
return {url: window.location.href, messages: messages};
"""

_JS_MODAL_PRESENT = """
return document.querySelector(arguments[0]) !== null;  /* modal-present */
"""

_JS_DURATION_CONTROL_PRESENT = """
var host = document.querySelector(arguments[0]);
var input = document.querySelector(arguments[1]);
return !!(host || input);  /* duration-control-present */
"""

_JS_SAVE_PRESENT = """
return document.querySelectorAll(arguments[0]).length > 0;  /* save-present */
"""

_JS_DURATION_COUNT = """
/* duration-count */
var rendered = document.querySelectorAll(arguments[0]).length;
if (rendered > 0) { return rendered; }
try {
    if (window.jQuery) {
        var w = window.jQuery('#Duration').data('kendoDropDownList');
        if (w && w.dataSource && w.dataSource.total) { return w.dataSource.total(); }
    }
} catch (e) { return 0; }
return 0;
"""

_JS_READ_DURATION = """
var el = document.querySelector(arguments[0]) || document.querySelector(arguments[1]);
return el ? (el.textContent || '').replace(/\\s+/g, ' ').trim() : null;
"""

_JS_AGREE = """
var box = document.querySelector(arguments[0]);
if (!box) { return 'absent'; }
if (!box.checked) {
    box.click();
    if (!box.checked) {
        box.checked = true;
        box.dispatchEvent(new Event('change', {bubbles: true}));
    }
}
return box.checked ? 'checked' : 'failed';
"""

_JS_SAVE = """
var selector = arguments[0], wanted = arguments[1];
var nodes = document.querySelectorAll(selector);
var seen = [];
for (var i = 0; i < nodes.length; i++) {
    var button = nodes[i];
    if (button.disabled || button.offsetParent === null) { continue; }
    var text = (button.textContent || '').replace(/\\s+/g, ' ').trim();
    seen.push(text);
    for (var j = 0; j < wanted.length; j++) {
        if (text.toLowerCase() === wanted[j].toLowerCase()) {
            button.click();
            return {status: 'clicked', text: text};
        }
    }
}
if (seen.length === 0) {
    // Nothing matched the scoped selector at all — report every button on the
    // page so a selector change is diagnosable from one log line.
    var all = document.querySelectorAll('button, input[type=submit], a.btn');
    for (var k = 0; k < all.length && seen.length < 40; k++) {
        if (all[k].offsetParent === null) { continue; }
        var t = (all[k].textContent || all[k].value || '').replace(/\\s+/g, ' ').trim();
        if (t) { seen.push(t); }
    }
    return {status: 'missing', seen: seen, scope: 'page'};
}
return {status: 'missing', seen: seen, scope: 'modal'};
"""

_JS_OUTCOME = """
var popupSel = arguments[0], modalSel = arguments[1];
var successRe = /(confirmed|successful|success|booked|reserved|thank you)/i;
var errorRe = /(already|not available|unavailable|no longer|conflict|exceed|maximum|limit|invalid|cannot|unable|failed|sorry|error)/i;

var popups = document.querySelectorAll(popupSel);
for (var i = 0; i < popups.length; i++) {
    if (popups[i].offsetParent === null) { continue; }
    var text = (popups[i].textContent || '').replace(/\\s+/g, ' ').trim();
    if (!text) { continue; }
    if (errorRe.test(text)) { return {state: 'error', text: text.slice(0, 300)}; }
    if (successRe.test(text)) {
        var ok = popups[i].querySelector('button');
        if (ok) { ok.click(); }
        return {state: 'success', text: text.slice(0, 300)};
    }
}

var modal = document.querySelector(modalSel);
if (!modal || modal.offsetParent === null) { return {state: 'closed', text: ''}; }

var invalid = modal.querySelector('.field-validation-error, .validation-summary-errors, .text-danger');
if (invalid && invalid.offsetParent !== null) {
    var vtext = (invalid.textContent || '').replace(/\\s+/g, ' ').trim();
    if (vtext) { return {state: 'error', text: vtext.slice(0, 300)}; }
}
return {state: 'pending', text: ''};
"""

_JS_CLOSE_MODAL = """
var modalSel = arguments[0];
var modal = document.querySelector(modalSel);
if (modal) {
    var buttons = modal.querySelectorAll('button, a');
    for (var i = 0; i < buttons.length; i++) {
        var b = buttons[i];
        var label = ((b.getAttribute('aria-label') || '') + ' ' + (b.className || '') + ' ' +
                     (b.textContent || '')).toLowerCase();
        if (label.indexOf('close') !== -1 || label.indexOf('cancel') !== -1 ||
            b.classList.contains('k-window-action')) {
            b.click();
            return 'closed';
        }
    }
}
var backdrop = document.querySelector('.modal-backdrop, .k-overlay');
if (backdrop) { backdrop.click(); return 'backdrop'; }
document.body.dispatchEvent(new KeyboardEvent('keydown', {key: 'Escape', keyCode: 27, bubbles: true}));
return 'escape';
"""

_JS_REFRESH_GRID = """
if (!window.jQuery) { return 'no-jquery'; }
try {
    var el = window.jQuery('[data-role=scheduler]');
    var widget = el.data('kendoScheduler') || window.jQuery('#scheduler').data('kendoScheduler');
    if (widget && widget.dataSource) { widget.dataSource.read(); return 'refreshed'; }
} catch (e) { return 'error'; }
return 'no-widget';
"""

_JS_OPEN_DATEPICKER = """
var toggle = document.querySelector(arguments[0]);
if (!toggle) { return 'missing'; }
toggle.click();
return 'opened';
"""

_JS_PICK_DAY = """
var selector = arguments[0], title = arguments[1];
var days = document.querySelectorAll(selector);
for (var i = 0; i < days.length; i++) {
    if (days[i].getAttribute('title') === title) { days[i].click(); return 'clicked'; }
}
return 'missing';
"""

_JS_NEXT_MONTH = """
var next = document.querySelector('.k-calendar .k-nav-next, .k-calendar-nav-next, [title="Next"]');
if (!next) { return 'missing'; }
next.click();
return 'clicked';
"""

_JS_COUNT_SLOTS = """
return document.querySelectorAll('a[data-href*="CreateReservation"]').length;
"""

_SAVE_BUTTON_TEXTS = ["Save", "Book", "Submit", "Confirm", "Reserve"]

_JS_DUMP_GRID = """
var links = document.querySelectorAll('a[data-href]');
var out = [];
for (var i = 0; i < links.length && out.length < 120; i++) {
    out.push(links[i].getAttribute('data-href'));
}
return {total: links.length, hrefs: out};
"""

_JS_DUMP_MODAL = """
function norm(v) { return String(v == null ? '' : v).replace(/\\s+/g, ' ').trim(); }
var out = {};

var modal = document.querySelector(arguments[0]);
out.modal_found = !!modal;
out.modal_classes = modal ? modal.className : null;

// Duration options as rendered.
var dom = [];
document.querySelectorAll(arguments[1]).forEach(function (li) {
    var t = li.querySelector('.k-list-item-text') || li;
    dom.push(norm(t.textContent));
});
out.duration_options_dom = dom;

// Duration options according to Kendo.
out.duration_options_widget = [];
out.duration_widget_raw = null;
try {
    if (window.jQuery) {
        var w = window.jQuery('#Duration').data('kendoDropDownList');
        if (!w && window.kendo && window.kendo.widgetInstance) {
            var host = document.querySelector(arguments[2]);
            if (host) { w = window.kendo.widgetInstance(window.jQuery(host)); }
        }
        if (w && w.dataSource) {
            var items = w.dataSource.data() || [];
            out.duration_widget_raw = JSON.stringify(items).slice(0, 2000);
            for (var i = 0; i < items.length; i++) {
                var it = items[i];
                out.duration_options_widget.push(
                    typeof it === 'string' ? it :
                    norm(it.Text || it.text || it.Name || it.DisplayName || JSON.stringify(it))
                );
            }
        }
    }
} catch (e) { out.duration_widget_error = String(e); }

out.duration_display = norm(
    (document.querySelector(arguments[3]) || document.querySelector(arguments[2]) || {}).textContent
);

// Every visible button, scoped and unscoped.
function buttons(root) {
    var res = [];
    if (!root) { return res; }
    root.querySelectorAll('button, input[type=submit], a.btn').forEach(function (b) {
        if (b.offsetParent === null) { return; }
        res.push({
            text: norm(b.textContent || b.value),
            id: b.id || null,
            cls: b.className || null,
            type: b.getAttribute('type')
        });
    });
    return res;
}
out.buttons_in_modal = buttons(modal);
out.buttons_on_page = buttons(document.body).slice(0, 40);

out.disclosure_present = !!document.querySelector(arguments[4]);
out.modal_html = modal ? modal.outerHTML.slice(0, 6000) : null;
return out;
"""


class BookingService:
    """Drives the reservation modal for one court date."""

    def __init__(self, driver: Any, waiter: Waiter, settings: BookingSettings) -> None:
        self.driver = driver
        self.waiter = waiter
        self.settings = settings
        self.court_date: date_type | None = None
        self.last_result: BookingResult | None = None
        self._reported_duration_sets: set[tuple[str, ...]] = set()
        self._reported_save_failure = False

    # -- date -------------------------------------------------------------
    def select_date(self, target: date_type) -> float:
        """Point the scheduler at ``target``. Returns elapsed seconds."""
        elapsed, reason = self._attempt_select_date(target)
        if elapsed is None:
            raise BookingError(reason)
        return elapsed

    def try_select_date(self, target: date_type) -> float | None:
        """Select ``target``, or return None if it is not offered yet.

        Returning rather than raising matters during warm-up: at a facility that
        releases the Nth day out on the clock, the target date does not exist in
        the picker until the release moment, and a warm-up that treats that as
        an error dies minutes before the date it was waiting for.
        """
        elapsed, reason = self._attempt_select_date(target)
        if elapsed is None:
            logger.debug(reason)
        return elapsed

    def _attempt_select_date(self, target: date_type) -> tuple[float | None, str]:
        """(elapsed, "") on success, or (None, why not).

        The two failures are worth keeping apart: a picker that will not open is
        a broken page, while a date the picker does not offer is either a
        not-yet-released day or a wrong ``days_in_advance``.
        """
        started = time.monotonic()
        self.court_date = target
        title = format_long_date(target)

        if self.waiter.js(_JS_OPEN_DATEPICKER, locators.DATEPICKER_TOGGLE) != "opened":
            return None, "Could not open the date picker on the booking grid"

        # The picker may open on a different month than the target.
        for hop in range(3):
            found = self.waiter.until(
                lambda: self.waiter.js(_JS_PICK_DAY, locators.DATEPICKER_DAY, title) == "clicked",
                timeout=3,
            )
            if found:
                break
            if hop < 2:
                self.waiter.js(_JS_NEXT_MONTH)
        else:
            return None, f"Date {title} is not selectable in the picker"

        # Wait for the grid to re-render with that day's slots rather than
        # sleeping a flat 1.5s and hoping.
        self.waiter.until(lambda: (self.waiter.js(_JS_COUNT_SLOTS) or 0) > 0, timeout=10)

        elapsed = time.monotonic() - started
        logger.info(f"Grid showing {title} ({elapsed:.2f}s)")
        return elapsed, ""

    def await_date(self, target: date_type, *, timeout: float | None = None) -> float:
        """Wait for a date that only becomes bookable at the release moment.

        The picker's selectable range is decided when the page loads, so a soft
        grid refresh does not reveal a day that opened a moment ago — the page
        has to be reloaded. Called right after arming, so the first reload
        usually lands within a second of the release.

        Keeps reloading for ``release_grace_seconds``, because a club's "noon" is
        its server's noon and can run late. This polls continuously rather than
        sleeping a minute between tries on purpose: the good slots go in seconds,
        so if the date appears at 12:00:20 we want it at 12:00:20, not at 12:01.
        The grace window is about not giving up; the poll interval is about
        winning, and they are different concerns.
        """
        limit = self.settings.release_grace_seconds if timeout is None else timeout
        started = time.monotonic()
        title = format_long_date(target)
        announced = 0.0
        attempt = 0

        while True:
            attempt += 1
            try:
                self.driver.refresh()
            except Exception as exc:  # noqa: BLE001 - retried below
                logger.warning(f"Reload failed: {type(exc).__name__}: {exc}")

            if self.try_select_date(target) is not None:
                waited = time.monotonic() - started
                logger.info(f"{title} opened after {waited:.2f}s ({attempt} reload(s))")
                return waited

            elapsed = time.monotonic() - started
            if elapsed >= limit:
                break
            # A late release is worth saying out loud while it is happening, not
            # only in the post-mortem.
            if elapsed - announced >= 30:
                announced = elapsed
                logger.warning(
                    f"{title} still not offered {elapsed:.0f}s after release; "
                    f"still trying for another {limit - elapsed:.0f}s"
                )
            self.waiter.sleep(0.25)

        raise BookingError(self._date_never_opened(target, limit, attempt))

    def _date_never_opened(self, target: date_type, limit: float, attempts: int) -> str:
        """Say which of the two causes this is, rather than listing both.

        They need opposite responses — wait longer, or change the config — and
        the arithmetic already distinguishes them.
        """
        title = format_long_date(target)
        days_out = (target - now_in(self.settings.tz).date()).days
        configured = self.settings.days_in_advance
        head = (
            f"Date {title} never became selectable within {limit:.0f}s of the "
            f"release moment ({attempts} reload(s))"
        )
        if days_out > configured:
            return (
                f"{head}. It is {days_out} days out but days_in_advance is "
                f"{configured}, so the facility was never going to offer it yet — "
                "this is the wrong date to ask for, not a late release."
            )
        return (
            f"{head}. It is {days_out} days out, which matches days_in_advance, so "
            "the release is either later than release_time says or the facility "
            "opens fewer days ahead than configured. Raise release_grace_seconds "
            "if the club is simply slow."
        )

    def refresh_grid(self) -> str:
        """Re-read the slot data source without a full page load."""
        result = self.waiter.js(_JS_REFRESH_GRID)
        if result != "refreshed":
            logger.debug(f"Soft grid refresh unavailable ({result}); reloading page")
            try:
                self.driver.refresh()
            except Exception as exc:  # noqa: BLE001 - a failed refresh is not fatal
                logger.warning(f"Page refresh failed: {type(exc).__name__}: {exc}")
                return "failed"
            if self.court_date is not None:
                self.select_date(self.court_date)
            return "reloaded"
        self.waiter.until(lambda: (self.waiter.js(_JS_COUNT_SLOTS) or 0) > 0, timeout=5)
        return "refreshed"

    def slot_count(self) -> int:
        return int(self.waiter.js(_JS_COUNT_SLOTS) or 0)

    # -- planning ---------------------------------------------------------
    def build_plan(self, target: date_type) -> list[SlotCandidate]:
        """Ordered list of attempts, honouring priority slots and priority mode."""
        settings = self.settings
        tz = settings.tz

        starts: list[datetime] = []
        cursor = datetime.combine(target, settings.search_start_time, tzinfo=tz)
        window_end = datetime.combine(target, settings.search_end_time, tzinfo=tz)
        step = timedelta(minutes=settings.time_increment)
        while cursor <= window_end:
            starts.append(cursor)
            cursor += step

        priority = settings.priority_time_slots
        if priority:
            ranked = {label: index for index, label in enumerate(priority)}
            starts.sort(
                key=lambda dt: (
                    ranked.get(format_clock_time(dt.time()), len(ranked)),
                    dt,
                )
            )

        durations = settings.preferred_durations
        plan: list[SlotCandidate] = []
        if settings.booking_priority == "duration":
            # Longest-preferred duration across every slot, before falling back.
            for duration in durations:
                plan.extend(SlotCandidate(start, duration) for start in starts)
        else:
            for start in starts:
                plan.extend(SlotCandidate(start, duration) for duration in durations)
        return plan

    # -- attempting -------------------------------------------------------
    def data_href(self, candidate: SlotCandidate) -> str:
        """The ``data-href`` CourtReserve puts on a bookable grid cell.

        The live site emits the parameters in this order, with a ``courtTypeId``
        the previous implementation did not know about::

            ?start=...&end=...&customSchedulerId=16984&courtTypeId=9&courtType=Pickleball

        so an exact string comparison against the old ordering never matched a
        single slot. Matching does not depend on getting this exactly right —
        :meth:`_start_needles` keys on the start time and scheduler id — but the
        fast path should hit, and a wrong string here is misleading.
        """
        settings = self.settings
        start = candidate.start
        end = start + timedelta(minutes=settings.slot_length_minutes)
        start_part = (
            f"{format_us_date(start.date())}%20"
            f"{format_clock_time(start.time()).replace(' ', '%20')}"
        )
        end_part = (
            f"{format_us_date(end.date())}%20"
            f"{format_clock_time(end.time()).replace(' ', '%20')}"
        )
        href = (
            f"/Online/Reservations/CreateReservation/{settings.organization_id}"
            f"?start={start_part}"
            f"&end={end_part}"
            f"&customSchedulerId={settings.sport_id}"
        )
        if settings.court_type_id is not None:
            href += f"&courtTypeId={settings.court_type_id}"
        return href + f"&courtType={settings.court_type}"

    def _start_needles(self, candidate: SlotCandidate) -> list[str]:
        """Encoded and raw forms of the ``start=`` parameter, for fuzzy matching."""
        day = format_us_date(candidate.start.date())
        clock = format_clock_time(candidate.start.time())
        return [f"start={day}%20{clock.replace(' ', '%20')}", f"start={day} {clock}"]

    def attempt(self, candidate: SlotCandidate) -> SlotOutcome:
        """Try to book one (slot, duration) pair."""
        logger.debug(f"Attempting {candidate}")
        opened = self.waiter.js(
            _JS_OPEN_SLOT,
            self.data_href(candidate),
            self._start_needles(candidate),
            f"customSchedulerId={self.settings.sport_id}",
        )
        if opened not in {"clicked", "clicked-fallback"}:
            logger.debug(f"{candidate.label}: slot not bookable ({opened})")
            return SlotOutcome.UNAVAILABLE

        try:
            if not self._await_modal():
                logger.debug(f"{candidate.label}: reservation modal did not open")
                self._close_modal()
                return SlotOutcome.UNAVAILABLE

            if not self._set_duration(candidate.duration):
                logger.info(
                    f"{candidate.label}: {candidate.duration}min is not offered for this slot"
                )
                self._close_modal()
                return SlotOutcome.DURATION_UNAVAILABLE

            self.waiter.js(_JS_AGREE, locators.DISCLOSURE_CHECKBOX)

            if self.settings.dry_run:
                logger.warning(
                    f"DRY RUN — would have booked {candidate}; not clicking Save"
                )
                self._close_modal()
                return SlotOutcome.WOULD_BOOK

            saved = self.waiter.js(_JS_SAVE, locators.SAVE_BUTTON, _SAVE_BUTTON_TEXTS)
            if not isinstance(saved, dict) or saved.get("status") != "clicked":
                self._report_missing_save(candidate, saved if isinstance(saved, dict) else {})
                self._close_modal()
                return SlotOutcome.ERROR

            return self._await_confirmation(candidate)

        except Exception as exc:  # noqa: BLE001 - one bad slot must not end the sprint
            logger.warning(f"{candidate.label}: attempt failed: {type(exc).__name__}: {exc}")
            self._close_modal()
            return SlotOutcome.ERROR

    # -- attempt internals -------------------------------------------------
    def _await_modal(self) -> bool:
        """Wait for the reservation modal to be *usable*, not merely present.

        CourtReserve renders the modal shell first and fills the duration list
        afterwards. Waiting only for the label to exist meant acting on a
        half-built form: the dropdown read as empty and the Save button had not
        been attached yet, which is why the same slot alternated between
        "duration not offered" and "no Save button" across passes.
        """
        timeout = min(self.settings.webdriver_timeout, 10)
        present = self.waiter.until(
            lambda: self.waiter.js(_JS_MODAL_PRESENT, locators.DURATION_DROPDOWN),
            timeout=timeout,
        )
        if not present:
            return False

        # Then wait for it to finish populating. The Save button is attached with
        # the shell, so waiting on that alone would let us act on an empty list.
        if self._duration_control_present():
            ready = self.waiter.until(lambda: self._duration_option_count() > 0, timeout=timeout)
        else:
            ready = self.waiter.until(self._save_button_present, timeout=timeout)
        if not ready:
            logger.debug("Modal opened but never finished loading its controls")
        return True

    def _duration_control_present(self) -> bool:
        return bool(
            self.waiter.js(
                _JS_DURATION_CONTROL_PRESENT,
                locators.DURATION_DROPDOWN,
                locators.DURATION_INPUT,
            )
        )

    def _duration_option_count(self) -> int:
        return int(self.waiter.js(_JS_DURATION_COUNT, locators.DURATION_OPTION) or 0)

    def _save_button_present(self) -> bool:
        return bool(self.waiter.js(_JS_SAVE_PRESENT, locators.SAVE_BUTTON))

    def _set_duration(self, minutes: int) -> bool:
        """Select ``minutes`` in the duration dropdown and verify it stuck.

        Tries Kendo's widget API first, then falls back to clicking the rendered
        list. The widget path must NOT short-circuit the DOM path when it fails
        to find a match: the dataSource schema is undocumented and varies, so a
        field-name miss there says nothing about what the user can actually see.
        """
        label = self.settings.label_for_duration(minutes)
        offered: list[str] = []

        # Some reservation types fix the length to the grid cell and render no
        # duration choices at all (the modal ships IsAllowedToPickStartAndEndTime
        # = False). Booking those is correct only when the requested length *is*
        # the cell length — otherwise we would silently book the wrong duration,
        # which is the exact failure this rewrite set out to remove.
        if self._duration_option_count() == 0 and not self._duration_control_present():
            if minutes == self.settings.slot_length_minutes:
                logger.info(
                    f"No duration control in this modal; the site fixes the length at "
                    f"{minutes}min, which is what was asked for"
                )
                return True
            logger.info(
                f"No duration control in this modal; the length is fixed at "
                f"{self.settings.slot_length_minutes}min, so {minutes}min is not bookable here"
            )
            return False

        widget = self.waiter.js(
            _JS_SET_DURATION_WIDGET, label, locators.DURATION_DROPDOWN
        )
        selected = False
        if isinstance(widget, dict):
            status = widget.get("status")
            offered = list(widget.get("options") or [])
            if status == "selected":
                selected = True
            elif status == "not-in-list":
                logger.debug(
                    f"Kendo dataSource has no {label!r}; options were {offered}. "
                    "Trying the rendered list."
                )
            else:
                logger.debug(f"Kendo widget unavailable ({status}); using DOM interaction")

        if not selected:
            if self.waiter.js(_JS_OPEN_DURATION_DROPDOWN, locators.DURATION_DROPDOWN) != "opened":
                logger.warning("Duration dropdown could not be opened")
                return False
            # Poll explicitly rather than via `until`: the script returns a dict
            # either way, and a dict is truthy, so `until` would accept the very
            # first "missing" answer and never wait for a list still loading.
            picked = self._pick_duration_option(label, timeout=6.0)
            if picked.get("seen"):
                offered = list(picked["seen"])
            if picked.get("status") != "clicked":
                self._report_missing_duration(label, offered)
                return False

        # Verify, rather than assume. This is the check whose absence let the old
        # code book an unintended duration.
        confirmed = self.waiter.until(
            lambda: self.waiter.js(
                _JS_READ_DURATION,
                locators.DURATION_SELECTED_TEXT,
                locators.DURATION_DROPDOWN,
            )
            == label,
            timeout=3,
        )
        if not confirmed:
            actual = self.waiter.js(
                _JS_READ_DURATION, locators.DURATION_SELECTED_TEXT, locators.DURATION_DROPDOWN
            )
            logger.warning(f"Duration did not apply: wanted {label!r}, control shows {actual!r}")
            return False
        return True

    def _pick_duration_option(self, label: str, *, timeout: float) -> dict[str, Any]:
        """Click the option matching ``label``, waiting for a list still loading.

        Stops as soon as the list has rendered *something*: options that exist
        but do not match will not start matching later.
        """
        deadline = self.waiter.now() + timeout
        last: dict[str, Any] = {"status": "missing", "seen": []}
        while True:
            result = self.waiter.js(_JS_PICK_DURATION_OPTION, locators.DURATION_OPTION, label)
            if isinstance(result, dict):
                last = result
                if result.get("status") == "clicked" or result.get("seen"):
                    return last
            if self.waiter.now() >= deadline:
                return last
            self.waiter.sleep(self.settings.poll_interval_ms / 1000.0)

    def _report_missing_duration(self, label: str, offered: list[str]) -> None:
        """Log the durations the site actually offers, once per distinct set.

        Without this the failure reads as "60min is not offered" with no way to
        tell a genuinely-full slot from a label that no longer matches.
        """
        signature = tuple(offered)
        if signature in self._reported_duration_sets:
            logger.debug(f"Duration {label!r} not offered (options: {offered})")
            return
        self._reported_duration_sets.add(signature)
        if offered:
            logger.warning(
                f"Duration {label!r} is not in this slot's dropdown. "
                f"The site offered: {offered}. If those look like the same durations "
                "under different wording, set `duration_labels` in config.yaml, e.g. "
                "duration_labels: {60: \"" + (offered[0] if offered else "1 hr") + "\"}"
            )
        else:
            logger.warning(
                f"Duration {label!r} not selectable and the dropdown appeared empty. "
                "Run `pickletime --diagnose` to dump what the modal actually contains."
            )

    def _report_missing_save(self, candidate: SlotCandidate, result: dict[str, Any]) -> None:
        """Say what buttons were actually present, so a selector fix is obvious."""
        seen = list(result.get("seen") or [])
        scope = result.get("scope", "modal")
        if self._reported_save_failure:
            logger.debug(f"{candidate.label}: still no Save button (saw {seen})")
            return
        self._reported_save_failure = True
        if scope == "page" and seen:
            logger.warning(
                f"{candidate.label}: nothing matched the Save-button selector inside the "
                f"modal. Visible buttons on the page were: {seen}. If one of those is the "
                "confirm action, add it to SAVE_BUTTON in core/locators.py."
            )
        else:
            logger.warning(
                f"{candidate.label}: the modal opened but exposed no usable Save button "
                f"(saw {seen}). Run `pickletime --diagnose` to dump the modal."
            )

    def _await_confirmation(self, candidate: SlotCandidate) -> SlotOutcome:
        """Poll for the site's verdict after Save."""
        deadline = self.waiter.now() + max(self.settings.webdriver_timeout, 8)
        last: dict[str, Any] = {"state": "pending", "text": ""}

        while self.waiter.now() < deadline:
            raw = self.waiter.js(_JS_OUTCOME, locators.SUCCESS_POPUP, locators.MODAL_CONTAINER)
            if isinstance(raw, dict):
                last = raw
                state = raw.get("state")
                if state == "success":
                    logger.success(
                        f"BOOKED {candidate} — {self._describe(raw.get('text'))}"
                    )
                    self._remember(candidate, "explicit", raw.get("text"))
                    return SlotOutcome.BOOKED
                if state == "error":
                    logger.info(f"{candidate.label}: refused — {self._describe(raw.get('text'))}")
                    self._close_modal()
                    return SlotOutcome.REJECTED
                if state == "closed":
                    # A closed modal is NOT a reservation. It closes on cancel,
                    # on a soft validation failure, and on a redirect, and this
                    # path once reported a booking, sent the "court booked" SMS
                    # and wrote the ledger for a court that did not exist.
                    return self._unverified(candidate, "modal closed without a verdict")
            self.waiter.sleep(self.settings.poll_interval_ms / 1000.0)

        logger.warning(f"{candidate.label}: no verdict from the site ({json.dumps(last)[:200]})")
        self._close_modal()
        if self._slot_gone(candidate):
            # Suggestive, not conclusive: the slot also disappears when someone
            # else takes it in the same second.
            return self._unverified(candidate, "slot left the grid after save, no confirmation")
        return SlotOutcome.REJECTED

    def _unverified(self, candidate: SlotCandidate, why: str) -> SlotOutcome:
        """Record an ambiguous save, with whatever the page can tell us about it.

        Neither answer is safe to assume. Claiming success books nothing and
        silences the retry; assuming failure and moving on risks a second court
        on the same evening. So the run stops here and says plainly that it does
        not know, and the evidence below is what makes the next one decisive.
        """
        evidence = self._page_evidence()
        logger.warning(
            f"UNVERIFIED {candidate}: {why}. The site neither confirmed nor refused, "
            f"so this may or may not be a reservation. Page said: {evidence}"
        )
        self.last_result = BookingResult(
            booked=False,
            court_date=format_us_date(candidate.start.date()),
            slot_time=candidate.label,
            duration=candidate.duration,
            confirmation="unverified",
            detail=f"{why}; page: {evidence}",
        )
        return SlotOutcome.UNVERIFIED

    def _page_evidence(self) -> str:
        """Any visible message, plus the URL — the cause is usually in here."""
        try:
            raw = self.waiter.js(_JS_PAGE_EVIDENCE)
        except Exception as exc:  # noqa: BLE001 - diagnostics must not raise
            return f"(could not read the page: {type(exc).__name__})"
        if not isinstance(raw, dict):
            return "(no page state available)"
        messages = [m for m in (raw.get("messages") or []) if m][:5]
        return json.dumps({"url": raw.get("url"), "messages": messages})[:500]

    def _slot_gone(self, candidate: SlotCandidate) -> bool:
        present = self.waiter.js(
            """
            var exact = arguments[0], needles = arguments[1];
            var links = document.querySelectorAll('a[data-href]');
            for (var i = 0; i < links.length; i++) {
                var href = links[i].getAttribute('data-href') || '';
                if (href === exact) { return true; }
                for (var n = 0; n < needles.length; n++) {
                    if (needles[n] && href.indexOf(needles[n]) !== -1) { return true; }
                }
            }
            return false;
            """,
            self.data_href(candidate),
            self._start_needles(candidate),
        )
        return present is False

    def _describe(self, text: Any) -> str:
        return str(text or "").strip()[:200] or "no message"

    def _remember(self, candidate: SlotCandidate, confirmation: str, detail: Any) -> None:
        self.last_result = BookingResult(
            booked=True,
            court_date=format_us_date(candidate.start.date()),
            slot_time=candidate.label,
            duration=candidate.duration,
            confirmation=confirmation,
            detail=self._describe(detail),
        )

    def _close_modal(self) -> None:
        self.waiter.js(_JS_CLOSE_MODAL, locators.MODAL_CONTAINER)
        self.waiter.gone(locators.MODAL_CONTAINER, timeout=3)

    # -- diagnostics ------------------------------------------------------
    def diagnose(self, plan: Sequence[SlotCandidate]) -> dict[str, Any]:
        """Dump what the live site actually renders, for selector repair.

        Guessing at another site's DOM from a log line is slow and unreliable;
        this collects the grid's real ``data-href`` values and the reservation
        modal's real contents in one pass, without booking anything.
        """
        report: dict[str, Any] = {
            "court_date": format_us_date(self.court_date) if self.court_date else None,
            "expected_href_example": self.data_href(plan[0]) if plan else None,
        }

        grid = self.waiter.js(_JS_DUMP_GRID) or {}
        report["grid_link_count"] = grid.get("total", 0)
        report["grid_hrefs"] = grid.get("hrefs", [])

        # Which of our candidates the grid actually offers.
        matched = []
        for candidate in plan:
            href = self.data_href(candidate)
            needles = self._start_needles(candidate)
            present = any(
                href == existing or any(n in existing for n in needles)
                for existing in report["grid_hrefs"]
            )
            matched.append({"slot": str(candidate), "on_grid": present})
        report["candidates"] = matched

        # Open the first slot that responds and describe its modal.
        report["modal"] = None
        for candidate in plan:
            opened = self.waiter.js(
                _JS_OPEN_SLOT,
                self.data_href(candidate),
                self._start_needles(candidate),
                f"customSchedulerId={self.settings.sport_id}",
            )
            if opened not in {"clicked", "clicked-fallback"}:
                continue
            if not self._await_modal():
                self._close_modal()
                continue
            report["modal_opened_for"] = str(candidate)
            # Kendo populates the list lazily; open it and wait, or the dump
            # records an empty dropdown that is merely still loading.
            self.waiter.js(_JS_OPEN_DURATION_DROPDOWN, locators.DURATION_DROPDOWN)
            self.waiter.until(lambda: self._duration_option_count() > 0, timeout=6)
            report["duration_option_count"] = self._duration_option_count()
            report["duration_control_present"] = self._duration_control_present()
            report["modal"] = self.waiter.js(
                _JS_DUMP_MODAL,
                locators.MODAL_CONTAINER,
                locators.DURATION_OPTION,
                locators.DURATION_DROPDOWN,
                locators.DURATION_SELECTED_TEXT,
                locators.DISCLOSURE_CHECKBOX,
            )
            self._close_modal()
            break

        return report

    # -- the sprint -------------------------------------------------------
    def sprint(
        self,
        plan: Sequence[SlotCandidate],
        *,
        deadline: float,
        refresh_between_passes: bool = True,
    ) -> BookingResult:
        """Work the plan repeatedly until something books or ``deadline`` passes.

        ``deadline`` is a :func:`time.monotonic` timestamp.  Re-walking the plan
        matters at release time: a slot that is not on the grid on the first pass
        (because the release transaction has not committed yet) usually appears a
        fraction of a second later.  The old code made exactly one pass and gave
        up.
        """
        target = self.court_date
        result = BookingResult(
            booked=False,
            court_date=format_us_date(target) if target else "",
            dry_run=self.settings.dry_run,
        )
        if not plan:
            result.detail = "No candidate slots in the configured search window"
            return result

        # A pair that cannot work is not worth re-attempting every pass. The
        # first real run against the site opened the same modal fourteen times
        # in thirty seconds for a duration it was never going to offer — wasted
        # time during the race, and needlessly hard on the site.
        exhausted: set[tuple[str, int]] = set()
        errors: dict[tuple[str, int], int] = {}
        passes = 0

        while True:
            passes += 1
            for candidate in plan:
                key = (candidate.label, candidate.duration)
                if key in exhausted:
                    continue
                if self.waiter.now() >= deadline and passes > 1:
                    result.detail = f"Sprint window expired after {passes - 1} full pass(es)"
                    logger.warning(result.detail)
                    return result

                outcome = self.attempt(candidate)
                result.attempts += 1

                if outcome is SlotOutcome.BOOKED:
                    booked = self.last_result
                    if booked is not None:
                        booked.attempts = result.attempts
                        booked.unavailable = result.unavailable
                        booked.rejected = result.rejected
                        booked.dry_run = self.settings.dry_run
                        return booked
                    result.booked = True
                    result.slot_time = candidate.label
                    result.duration = candidate.duration
                    result.confirmation = "inferred"
                    return result

                if outcome is SlotOutcome.UNVERIFIED:
                    # Stop the sprint. If that save did go through, carrying on
                    # would book a second court on the same evening; the cost of
                    # stopping is at most one missed slot, and the caller is
                    # told plainly that the outcome is unknown.
                    unverified = self.last_result
                    if unverified is not None:
                        unverified.attempts = result.attempts
                        unverified.unavailable = result.unavailable
                        unverified.rejected = result.rejected
                        unverified.dry_run = self.settings.dry_run
                        return unverified
                    result.confirmation = "unverified"
                    result.slot_time = candidate.label
                    result.duration = candidate.duration
                    return result

                if outcome is SlotOutcome.WOULD_BOOK:
                    result.booked = False
                    result.dry_run = True
                    result.slot_time = candidate.label
                    result.duration = candidate.duration
                    result.confirmation = "dry-run"
                    result.detail = f"Dry run: {candidate} was available and would have been booked"
                    logger.warning(result.detail)
                    return result

                if outcome is SlotOutcome.UNAVAILABLE:
                    # Might still appear: this is exactly the release-time case.
                    result.unavailable += 1
                elif outcome is SlotOutcome.DURATION_UNAVAILABLE:
                    # A length the site does not offer here will not appear later.
                    result.rejected += 1
                    exhausted.add(key)
                elif outcome is SlotOutcome.REJECTED:
                    result.rejected += 1
                    exhausted.add(key)
                elif outcome is SlotOutcome.ERROR:
                    errors[key] = errors.get(key, 0) + 1
                    if errors[key] >= 3:
                        logger.warning(f"Giving up on {candidate} after 3 errors")
                        exhausted.add(key)

            if len(exhausted) >= len(plan):
                result.detail = (
                    f"Every candidate is unbookable for this date "
                    f"({len(exhausted)}/{len(plan)} ruled out after {passes} pass(es))"
                )
                logger.warning(result.detail)
                return result

            if self.waiter.now() >= deadline:
                result.detail = (
                    f"No slot won after {passes} pass(es) over {len(plan)} candidates"
                )
                logger.warning(result.detail)
                return result

            if refresh_between_passes:
                self.refresh_grid()
            self.waiter.sleep(self.settings.poll_interval_ms / 1000.0)
