"""Every CSS/XPath selector the bot depends on, in one place.

When CourtReserve reskins something, this is the only file that needs editing —
previously the same selectors were scattered across three services and inlined
into JavaScript strings.

Each entry is an ordered list of candidates: the most specific selector first,
then progressively looser fallbacks.  :meth:`~pickletime.core.waits.Waiter.find_first`
evaluates the whole list on every poll, so extra fallbacks cost nothing in the
common case (unlike the old code, where each miss burned a full timeout).
"""

from __future__ import annotations

from selenium.webdriver.common.by import By

Locator = tuple[str, str]

EMAIL_FIELD: list[Locator] = [
    (By.ID, "UserNameOrEmail"),
    (By.NAME, "UserNameOrEmail"),
    (By.NAME, "email"),
    (By.CSS_SELECTOR, "input[type='email']"),
    (By.CSS_SELECTOR, "input[placeholder*='email' i]"),
]

PASSWORD_FIELD: list[Locator] = [
    (By.ID, "Password"),
    (By.NAME, "Password"),
    (By.NAME, "password"),
    (By.CSS_SELECTOR, "input[type='password']"),
]

SUBMIT_BUTTON: list[Locator] = [
    (By.CSS_SELECTOR, "form button[type='submit']"),
    (By.CSS_SELECTOR, "button[type='submit']"),
    (By.CSS_SELECTOR, "input[type='submit']"),
    (By.XPATH, "//button[normalize-space()='Log In' or normalize-space()='Login']"),
    (By.XPATH, "//button[contains(normalize-space(), 'Continue')]"),
]

LOGIN_ERROR: list[Locator] = [
    (By.CSS_SELECTOR, ".validation-summary-errors"),
    (By.CSS_SELECTOR, ".field-validation-error"),
    (By.CSS_SELECTOR, ".alert-danger"),
    (By.CSS_SELECTOR, ".error-message"),
    (By.CSS_SELECTOR, "[role='alert']"),
]

#: Present once authentication has succeeded and the shell has rendered.
AUTHENTICATED_MARKER: list[Locator] = [
    (By.CSS_SELECTOR, "a.parent-header-link"),
    (By.CSS_SELECTOR, "ul#respMenu"),
    (By.CSS_SELECTOR, "#navbarSupportedContent"),
    (By.CSS_SELECTOR, "a[href*='/Online/Account/LogOff']"),
]

RESERVATIONS_LINK: list[Locator] = [
    (By.XPATH, "//a[@class='parent-header-link']//span[normalize-space()='Reservations']"),
    (By.XPATH, "//a[contains(@href, '/Online/Reservations/')]"),
    (By.CSS_SELECTOR, "a[href*='Reservations']"),
    (By.XPATH, "//a[contains(normalize-space(), 'Reserve')]"),
]

#: The scheduler grid — its presence means slot links are rendered.
SCHEDULER_GRID: list[Locator] = [
    (By.CSS_SELECTOR, ".k-scheduler-content"),
    (By.CSS_SELECTOR, "#scheduler"),
    (By.CSS_SELECTOR, ".k-scheduler"),
]

DATEPICKER_TOGGLE: str = "a[role='button'].k-nav-current"
DATEPICKER_DAY: str = "a.k-link"

#: Kendo dropdown for the reservation duration, inside the booking modal.
DURATION_DROPDOWN: str = "span[aria-labelledby='Duration_label']"
#: The underlying <select>/<input> Kendo binds to.
DURATION_INPUT: str = "#Duration"
DURATION_LISTBOX: str = "#Duration_listbox"
DURATION_OPTION: str = "#Duration_listbox li.k-list-item"
DURATION_SELECTED_TEXT: str = "span[aria-labelledby='Duration_label'] .k-input-value-text"

DISCLOSURE_CHECKBOX: str = "#DisclosureAgree"

#: CourtReserve puts the confirm button in the modal *header*
#: (``.modal-title-buttons``), not a footer, and the modal's id is ``modal1`` —
#: a footer-only selector matched nothing and the bot reported "no Save button".
#: ``data-testid`` is the most stable hook the page offers, so it goes first.
#: Still scoped to the open modal, and the caller matches the label exactly, so
#: "Save & Add Another" cannot be clicked by accident.
SAVE_BUTTON: str = (
    "[data-testid='save-btn'], "
    ".modal.show .modal-title-buttons button, "
    ".modal.show button.btn-submit, "
    ".modal.show .modal-footer button, "
    ".modal.in .modal-footer button, "
    ".k-window-content button"
)

MODAL_CONTAINER: str = ".modal.show, .modal.in, .k-window, #createReservation-modal"

SUCCESS_POPUP: str = ".swal2-popup, .toast-success, .alert-success, [role='alert']"


# ---------------------------------------------------------------------------
# The member portal (CourtReserve's newer layout, live at Lifetime Sunnyvale
# from 2026-11). A single-page app; every hook below is a data-testid read from
# a scrubbed capture of the live page (`run.sh capture --open-slot`), except
# where noted. These are far steadier than its generated `acss-*` class names.
# ---------------------------------------------------------------------------

#: Present once the portal's scheduler has rendered.
PORTAL_SCHEDULER: str = "[data-testid='scheduler-page']"
PORTAL_NEXT_DAY: str = "[data-testid='scheduler-next-btn']"
PORTAL_PREV_DAY: str = "[data-testid='scheduler-prev-btn']"
PORTAL_TODAY: str = "[data-testid='scheduler-today-btn']"
#: Reads like "Saturday, November 14" (no year).
PORTAL_DATE_LABEL: str = "[data-testid='scheduler-date-label']"
PORTAL_REFRESH: str = "[data-testid='scheduler-refresh-btn']"
#: An open half-hour slot, reading "Reserve". Full ones read "None Available"
#: and carry no testid; past ones are empty.
PORTAL_SLOT: str = "[data-testid='consolidated-reserve-slot']"

PORTAL_MODAL: str = "[data-testid='reservation-create-modal']"
#: "NOV 14 · Saturday · 8:00 AM – 8:30 AM" - checked before confirming.
PORTAL_DATE_TIME: str = "[data-testid='reservation-date-time-display']"
PORTAL_DURATION_COMBO: str = "[data-testid='reservation-duration'] [role='combobox']"
#: The duration list opens in a body-level portal, outside the modal.
PORTAL_DURATION_OPTION: str = "[role='listbox'] [role='option']"
#: role=checkbox sits on the parent of the testid'd box.
PORTAL_DISCLOSURE_BOX: str = "[data-testid='reservation-disclosure-agree']"
PORTAL_CONFIRM: str = "[data-testid='reservation-mobile-submit-btn']"
PORTAL_CANCEL: str = "[data-testid='reservation-mobile-back-btn'], .ant-modal-close"
#: Where the portal shows toasts. What it shows after Confirm has not been
#: observed live yet; the service records it on the first booking.
PORTAL_MESSAGES: str = (
    "[data-toast-portal] [role='region'] *, [role='status'], [role='alert'], "
    ".ant-message-notice, .ant-notification-notice"
)
