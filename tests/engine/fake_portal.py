"""A stand-in for CourtReserve's new member portal (seen live 2026-11-14).

Built from a scrubbed capture of Lifetime Sunnyvale's bookings page, so the
hooks match what the live portal renders:

* ``/Online/Reservations/Bookings/{org}`` is a single-page app; the grid is
  drawn from ``/Online/Reservations/ReadConsolidated/{org}`` JSON, one row per
  half hour with ``AvailableCourts`` and a ``/Date(ms)/`` start;
* an open slot is ``div[data-testid=consolidated-reserve-slot][role=button]``
  reading "Reserve";
* the toolbar has Today, previous and next controls and a date label such as
  "Friday, November 13";
* clicking a slot opens an Ant Design modal
  (``[data-testid=reservation-create-modal]``) with a ``role=combobox`` duration
  picker that opens a ``role=listbox``, a ``role=checkbox`` disclosure, and the
  ``reservation-mobile-submit-btn`` "Confirm" button.

What happens after Confirm on the live site has not been observed. Here a
confirmed booking posts to ``/api/portal/book``, closes the modal and shows an
Ant ``.ant-message-success`` notice; tests must not treat that as the live
site's behaviour.

A decoy "Next announcement" button sits outside the toolbar, ahead of it in
the page, so a capture that grabs the first "next" anywhere gets it wrong.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

ORG_ID = 13233
SPORT_ID = 16984

DURATIONS = [("30 minutes", 30), ("1 hour", 60), ("1 hour & 30 minutes", 90), ("2 hours", 120)]


@dataclass
class PortalState:
    """Knobs the tests turn."""

    username: str = "player@example.com"
    password: str = "correct-horse"
    today: date = field(default_factory=date.today)
    #: date -> {"6:30 PM": available court count}. Dates absent here have every
    #: slot full, so only what a test lists is bookable.
    open_slots: dict[date, dict[str, int]] = field(default_factory=dict)
    #: How many days ahead the toolbar's "next" reaches; the last day opens only
    #: once ``released`` is true (flip it from a test thread to simulate noon).
    days_ahead: int = 8
    released: bool = True
    #: Milliseconds the toolbar stays disabled while a day loads. The live portal
    #: does this, and a stepper that clicks during it silently gets nowhere.
    load_delay_ms: int = 300
    #: slot label -> durations its form offers; absent means all of them.
    durations_by_slot: dict[str, list[str]] = field(default_factory=dict)
    #: Shift the time the form displays, to prove the bot checks it.
    modal_time_offset_min: int = 0
    #: Refuse every booking with this message (shown inside the form).
    reject_message: str | None = None
    #: Refuse only these slot labels.
    reject_slots: set[str] = field(default_factory=set)
    #: On success, close the form without any message (what the live portal did).
    silent_success: bool = False
    #: On success, navigate away instead of staying on the scheduler.
    navigate_after_confirm: bool = False
    bookings: list[dict[str, str]] = field(default_factory=list)
    reads: list[str] = field(default_factory=list)


_LOGIN_PAGE = """<!doctype html><html><head><title>Log In</title></head><body>
<form method="POST" action="/login">
  <input type="email" id="UserNameOrEmail" name="UserNameOrEmail" placeholder="Email">
  <input type="password" id="Password" name="Password">
  <button type="submit">Log In</button>
</form>
</body></html>"""

_PORTAL_PAGE = """<!doctype html><html><head><title>Lifetime Activities</title>
<style>
  body { font-family: sans-serif; margin: 0; }
  header { height: 48px; }
  .toolbar { display: flex; gap: 4px; padding: 8px; }
  .scroll { height: 360px; overflow-y: auto; border: 1px solid #ccc; position: relative; }
  .row { display: flex; height: 46px; border-top: 1px solid #eee; }
  .time { width: 90px; font-size: 12px; }
  .cell { flex: 1; }
  .reserve { cursor: pointer; background: #e6f4ff; display: inline-block; padding: 4px 12px; }
  .ant-modal-root { display: none; }
  .ant-modal-root.open { display: block; }
  .ant-modal-wrap { position: fixed; inset: 0; background: rgba(0,0,0,.3); }
  .ant-modal { background: #fff; width: 480px; margin: 60px auto; padding: 16px; }
  .listbox { position: absolute; background: #fff; border: 1px solid #999; }
  .hidden { display: none; }
</style></head><body>
<header><a href="/Online/Portal/Index/13233">Home</a> <span>Reservations</span>
  <a href="/Online/Account/LogOff">Log off</a></header>
<div class="announcements"><button type="button" aria-label="Next announcement" class="carousel-next">›</button></div>
<div class="acss-toolbar toolbar" data-testid="scheduler-toolbar">
  <div data-testid="scheduler-nav-group">
  <button type="button" id="today" data-testid="scheduler-today-btn">Today</button>
  <button type="button" aria-label="Previous day" id="prev" data-testid="scheduler-prev-btn">‹</button>
  <button type="button" aria-label="Next day" id="next" data-testid="scheduler-next-btn">›</button>
  <div data-testid="scheduler-date-wrap"><span id="datetext" data-testid="scheduler-date-label"></span></div>
  </div>
  <span data-testid="scheduler-toolbar-title">Pickleball Reservations</span>
  <div role="button" aria-label="Refresh" id="refresh" data-testid="scheduler-refresh-btn">⟳</div>
</div>
<div data-testid="scheduler-page"><div class="scroll" id="scroll"><div id="grid"></div></div></div>
<div data-toast-portal="true"><div role="region" aria-label="Notifications" id="messages"></div></div>

<div class="ant-modal-root" data-testid="reservation-create-modal" id="modalroot">
  <div class="ant-modal-mask"></div>
  <div class="ant-modal-wrap ant-modal-centered">
    <div class="ant-modal" role="dialog" aria-modal="true" tabindex="-1">
      <button type="button" class="ant-modal-close" aria-label="Close" id="xclose">×</button>
      <div class="ant-modal-header"><div class="ant-modal-title">Create Reservation</div></div>
      <div class="ant-modal-body">
        <form data-testid="reservation-form" onsubmit="return false">
          <div data-testid="reservation-date-time-display"><span id="dt-tile"></span> <span id="dt-day"></span> <span id="dt-range"></span>
            <span data-testid="reservation-date-time-display-pill">Pickleball</span></div>
          <div data-testid="reservation-duration"><label>Duration</label>
            <button type="button" role="combobox" aria-expanded="false" id="combo">30 minutes</button>
            <input name="duration" type="hidden" id="durationinput" value="30"></div>
          <div data-testid="reservation-section-disclosure"><div role="checkbox" aria-checked="false" id="disclosure" tabindex="0">
            <div data-testid="reservation-disclosure-agree"></div><span>I agree to the club disclosure</span></div></div>
          <div class="ant-form-item-explain-error hidden" id="formerror"></div>
          <button type="button" data-testid="reservation-mobile-back-btn" id="backclose">Close</button>
          <button type="button" data-testid="reservation-mobile-submit-btn" id="confirm">Confirm</button>
        </form>
      </div>
    </div>
  </div>
</div>
<div class="listbox hidden" role="listbox" id="listbox"></div>

<script>
const DURATIONS = __DURATIONS__;
const TODAY = new Date(__TODAY__ + 'T00:00:00');
let current = new Date(TODAY);
let selected = null;
let maxDate = new Date(TODAY);
const LOAD_DELAY = __LOAD_DELAY__;
const DURATIONS_BY_SLOT = __DURATIONS_BY_SLOT__;
const MODAL_OFFSET = __MODAL_OFFSET__;
const SILENT = __SILENT__;
const NAVIGATE = __NAVIGATE__;

function fmtLabel(d) {
  return d.toLocaleDateString('en-US', {weekday: 'long', month: 'long', day: 'numeric'});
}
function iso(d) { return d.getFullYear() + '-' + String(d.getMonth()+1).padStart(2,'0') + '-' + String(d.getDate()).padStart(2,'0'); }
function clock(ms) {
  const d = new Date(ms);
  let h = d.getHours(), m = d.getMinutes();
  const ap = h < 12 ? 'AM' : 'PM'; h = h % 12 || 12;
  return h + ':' + String(m).padStart(2,'0') + ' ' + ap;
}
function setNav(enabled) {
  document.getElementById('next').disabled = !enabled || current >= maxDate;
  document.getElementById('prev').disabled = !enabled || current <= TODAY;
}
async function windowStatus() {
  const w = await (await fetch('/Online/AjaxController/BookingWindowStatus/13233')).json();
  maxDate = new Date(w.maxDateStringDisplay + 'T00:00:00');
}
async function load() {
  setNav(false);
  document.getElementById('datetext').textContent = fmtLabel(current);
  const q = encodeURIComponent(JSON.stringify({startDate: iso(current), orgId: 13233, CourtTypeIds: [9]}));
  const r = await fetch('/Online/Reservations/ReadConsolidated/13233?page=1&pageSize=1000&jsonData=' + q);
  const data = (await r.json()).Data;
  const grid = document.getElementById('grid');
  grid.innerHTML = '';
  for (const slot of data) {
    const ms = parseInt(slot.Start.match(/\\d+/)[0], 10);
    const row = document.createElement('div'); row.className = 'row';
    const t = document.createElement('div'); t.className = 'time'; t.textContent = clock(ms);
    const c = document.createElement('div'); c.className = 'cell';
    if (slot.AvailableCourts > 0 && !slot.IsInPast) {
      const b = document.createElement('div');
      b.className = 'reserve acss-rlrib0'; b.setAttribute('data-testid', 'consolidated-reserve-slot');
      b.setAttribute('role', 'button'); b.tabIndex = 0; b.textContent = 'Reserve';
      b.addEventListener('click', () => openModal(ms));
      c.appendChild(b);
    }
    row.appendChild(t); row.appendChild(c); grid.appendChild(row);
  }
  await new Promise(r => setTimeout(r, LOAD_DELAY));
  setNav(true);
}
function openModal(ms) {
  selected = ms;
  document.getElementById('dt-day').textContent = new Date(ms).toLocaleDateString('en-US', {weekday: 'long'});
  const shown = ms + MODAL_OFFSET * 60000;
  document.getElementById('dt-tile').textContent = new Date(shown).toLocaleDateString('en-US', {month: 'short'}).toUpperCase() + ' ' + new Date(shown).getDate();
  document.getElementById('dt-range').textContent = clock(shown) + ' \\u2013 ' + clock(shown + 30*60000);
  document.getElementById('combo').textContent = '30 minutes';
  document.getElementById('durationinput').value = '30';
  document.getElementById('disclosure').setAttribute('aria-checked', 'false');
  document.getElementById('formerror').classList.add('hidden');
  document.getElementById('modalroot').classList.add('open');
}
function closeModal() { document.getElementById('modalroot').classList.remove('open'); closeList(); }
function closeList() { document.getElementById('listbox').classList.add('hidden'); document.getElementById('combo').setAttribute('aria-expanded', 'false'); }
document.getElementById('combo').addEventListener('click', () => {
  const lb = document.getElementById('listbox'); lb.innerHTML = '';
  const allowed = DURATIONS_BY_SLOT[clock(selected)];
  for (const [text, value] of DURATIONS.filter(([t]) => !allowed || allowed.includes(t))) {
    const o = document.createElement('div'); o.setAttribute('role', 'option'); o.className = 'ant-select-item-option';
    o.setAttribute('aria-selected', String(document.getElementById('durationinput').value === String(value)));
    o.textContent = text;
    o.addEventListener('click', () => {
      document.getElementById('combo').textContent = text;
      document.getElementById('durationinput').value = String(value);
      closeList();
    });
    lb.appendChild(o);
  }
  lb.classList.remove('hidden'); document.getElementById('combo').setAttribute('aria-expanded', 'true');
});
document.getElementById('disclosure').addEventListener('click', (e) => {
  const el = e.currentTarget; el.setAttribute('aria-checked', el.getAttribute('aria-checked') === 'true' ? 'false' : 'true');
});
document.getElementById('xclose').addEventListener('click', closeModal);
document.getElementById('backclose').addEventListener('click', closeModal);
document.getElementById('confirm').addEventListener('click', async () => {
  if (document.getElementById('disclosure').getAttribute('aria-checked') !== 'true') {
    const err = document.getElementById('formerror'); err.textContent = 'Please accept the disclosure.'; err.classList.remove('hidden');
    return;
  }
  const r = await fetch('/api/portal/book', {method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({start: selected, duration: document.getElementById('durationinput').value})});
  const body = await r.json();
  if (body.status === 'ok') {
    closeModal();
    if (NAVIGATE) { location.href = '/Online/Portal/Index/13233'; return; }
    if (!SILENT) document.getElementById('messages').innerHTML = '<div class="toast" role="status">Reservation created successfully</div>';
    load();
  } else {
    const err = document.getElementById('formerror'); err.textContent = body.message; err.classList.remove('hidden');
  }
});
document.addEventListener('keydown', (e) => {
  if (e.key !== 'Escape') return;
  if (!document.getElementById('listbox').classList.contains('hidden')) closeList(); else closeModal();
});
document.getElementById('next').addEventListener('click', () => { current.setDate(current.getDate() + 1); load(); });
document.getElementById('prev').addEventListener('click', () => { current.setDate(current.getDate() - 1); load(); });
document.getElementById('today').addEventListener('click', () => { current = new Date(TODAY); load(); });
document.getElementById('refresh').addEventListener('click', async () => { await windowStatus(); load(); });
document.querySelector('.carousel-next').addEventListener('click', () => {});
windowStatus().then(load);
</script>
</body></html>"""


def _slot_times() -> list[str]:
    """8:00 AM to 9:30 PM in half hours, as the live portal's 28 rows."""
    start = datetime(2000, 1, 1, 8, 0)
    return [(start + timedelta(minutes=30 * i)).strftime("%-I:%M %p") for i in range(28)]


class _Handler(BaseHTTPRequestHandler):
    state: PortalState

    def log_message(self, *_args: object) -> None:
        return

    def _send(self, body: str, status: int = 200, content_type: str = "text/html") -> None:
        payload = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", f"{content_type}; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _authenticated(self) -> bool:
        return "pickletime-session=ok" in (self.headers.get("Cookie") or "")

    def _redirect(self, location: str) -> None:
        self.send_response(302)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        url = urlparse(self.path)
        if url.path.startswith("/Online/Account/LogIn"):
            self._send(_LOGIN_PAGE)
            return
        if not self._authenticated():
            self._redirect(f"/Online/Account/LogIn/{ORG_ID}")
            return
        if url.path.startswith(("/Online/Reservations/Bookings", "/Online/Portal/Index")):
            page = (
                _PORTAL_PAGE.replace("__DURATIONS__", json.dumps(DURATIONS))
                .replace("__TODAY__", json.dumps(self.state.today.isoformat()))
                .replace("__LOAD_DELAY__", str(self.state.load_delay_ms))
                .replace("__DURATIONS_BY_SLOT__", json.dumps(self.state.durations_by_slot))
                .replace("__MODAL_OFFSET__", str(self.state.modal_time_offset_min))
                .replace("__SILENT__", json.dumps(self.state.silent_success))
                .replace("__NAVIGATE__", json.dumps(self.state.navigate_after_confirm))
            )
            self._send(page)
            return
        if url.path.startswith("/Online/AjaxController/BookingWindowStatus"):
            ahead = self.state.days_ahead if self.state.released else self.state.days_ahead - 1
            last = (self.state.today + timedelta(days=ahead)).isoformat()
            self._send(json.dumps({"isOpen": self.state.released, "maxDateStringDisplay": last,
                                   "releasedDateStringDisplay": last}), content_type="application/json")
            return
        if url.path.startswith("/Online/Reservations/ReadConsolidated"):
            query = json.loads(parse_qs(url.query).get("jsonData", ["{}"])[0])
            day = date.fromisoformat(query.get("startDate", self.state.today.isoformat()))
            self.state.reads.append(day.isoformat())
            self._send(json.dumps({"Data": self._slots(day), "Total": 28}), content_type="application/json")
            return
        self._send("<h1>Not found</h1>", status=404)

    def _slots(self, day: date) -> list[dict[str, object]]:
        opens = self.state.open_slots.get(day, {})
        rows = []
        for label in _slot_times():
            moment = datetime.combine(day, datetime.strptime(label, "%I:%M %p").time())
            # The page renders in the browser's local time, so send local epoch ms.
            ms = int(moment.timestamp() * 1000)
            rows.append({
                "Id": f"Pickleball{day:%m/%d/%Y} {moment:%H:%M:%S}",
                "Start": f"/Date({ms})/",
                "End": f"/Date({ms + 30 * 60 * 1000})/",
                "CourtType": "Pickleball",
                "AvailableCourts": opens.get(label, 0),
                "AvailableCourtIds": [52051] * opens.get(label, 0),
                "IsClosed": False,
                "IsInPast": day < self.state.today,
                "CourtTypeId": 9,
            })
        return rows

    def do_POST(self) -> None:  # noqa: N802
        url = urlparse(self.path)
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode("utf-8") if length else ""
        if url.path == "/login":
            form = parse_qs(raw)
            email = (form.get("UserNameOrEmail") or [""])[0]
            password = (form.get("Password") or [""])[0]
            if email == self.state.username and password == self.state.password:
                self.send_response(302)
                self.send_header("Set-Cookie", "pickletime-session=ok; Path=/")
                self.send_header("Location", f"/Online/Portal/Index/{ORG_ID}?isLoginRedirect=true")
                self.send_header("Content-Length", "0")
                self.end_headers()
            else:
                self._redirect(f"/Online/Account/LogIn/{ORG_ID}?failed=1")
            return
        if url.path == "/api/portal/book":
            payload = json.loads(raw or "{}")
            moment = datetime.fromtimestamp(int(payload["start"]) / 1000)
            label = moment.strftime("%-I:%M %p")
            if self.state.reject_message or label in self.state.reject_slots:
                message = self.state.reject_message or "This time is no longer available."
                self._send(json.dumps({"status": "error", "message": message}), content_type="application/json")
                return
            self.state.bookings.append({
                "date": moment.date().isoformat(),
                "slot": moment.strftime("%-I:%M %p"),
                "duration": str(payload.get("duration")),
            })
            self._send(json.dumps({"status": "ok"}), content_type="application/json")
            return
        self._send("<h1>Not found</h1>", status=404)


class FakePortal:
    """Context manager that runs the fake portal on a free port."""

    def __init__(self, state: PortalState) -> None:
        self.state = state
        handler = type("BoundPortalHandler", (_Handler,), {"state": state})
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    def __enter__(self) -> FakePortal:
        self._thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)
