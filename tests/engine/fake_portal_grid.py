"""A stand-in for the member-portal scheduler as captured on 19 Nov 2026.

Built from that scrubbed capture (no names, no club text). Differences from
``fake_portal.py``, which is the 14 Nov page:

* no ``scheduler-page``, ``scheduler-date-label``, ``scheduler-refresh-btn``
  or ``consolidated-reserve-slot``;
* the date is a plain span ("Friday, November 27") beside ``scheduler-date-picker``;
* refresh is a ``[role=button]`` named "Refresh scheduler" with a rotate icon;
* an open half hour is a ``[role=button]`` reading "Reserve", lined up with a
  time column; a full one reads "None Available" and is not a button;
* clicking Reserve calls ``CreateReservation``. A slot in ``refuse_open``
  answers ``isValid: false`` and the modal shows "Reservation unavailable"
  and Close only. An allowed slot paints the duration form after
  ``form_delay_ms``, the way the controls were absent from the first paint.

A decoy "Next announcement" button sits outside the date toolbar.
"""

from __future__ import annotations

import json
import threading
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from fake_portal import DURATIONS, ORG_ID, PortalState

_LOGIN_PAGE = """<!doctype html><html><head><title>Log In</title></head><body>
<form method="POST" action="/login">
  <input type="email" id="UserNameOrEmail" name="UserNameOrEmail" placeholder="Email">
  <input type="password" id="Password" name="Password">
  <button type="submit">Log In</button>
</form>
</body></html>"""

_GRID_PAGE = """<!doctype html><html><head><title>Reservations</title>
<style>
  body { font-family: sans-serif; margin: 0; }
  header { height: 48px; }
  .toolbar { display: flex; gap: 8px; align-items: center; padding: 8px; }
  .scroll { height: 360px; overflow-y: auto; border-top: 1px solid #ccc; }
  .cols { display: flex; align-items: flex-start; }
  .time, .cell { height: 46px; box-sizing: border-box; }
  .time { width: 90px; font-size: 12px; }
  .cell { flex: 1; }
  .reserve { cursor: pointer; display: inline-block; padding: 4px 12px; background: #e6f4ff; }
  .none { color: #888; padding: 4px 12px; }
  .ant-modal-root { display: none; }
  .ant-modal-root.open { display: block; }
  .ant-modal-wrap { position: fixed; inset: 0; background: rgba(0,0,0,.3); }
  .ant-modal { background: #fff; width: 480px; margin: 60px auto; padding: 16px; }
  .listbox { position: absolute; background: #fff; border: 1px solid #999; z-index: 5; }
  .hidden { display: none; }
  .loading { padding: 24px; }
</style></head><body>
<header>
  <a href="/Online/Portal/Index/13233">Home</a>
  <span>Reservations</span>
  <a href="/Online/Account/LogOff">Log off</a>
</header>
<div class="announcements"><button type="button" aria-label="Next announcement" class="carousel-next">Next</button></div>
<div class="toolbar" id="toolbar">
  <button type="button" id="today" data-testid="scheduler-today-btn">Today</button>
  <button type="button" id="prev" aria-label="Previous day" data-testid="scheduler-prev-btn">Prev</button>
  <button type="button" id="next" aria-label="Next day" data-testid="scheduler-next-btn">Next</button>
  <span class="datelabel"><span id="datetext"></span><i class="fa-regular fa-calendar"></i></span>
  <input data-testid="scheduler-date-picker" placeholder="Select date">
  <span>Pickleball Reservations</span>
</div>
<div role="button" aria-label="Refresh scheduler" id="refresh" tabindex="0"><i class="fa-regular fa-arrows-rotate"></i></div>
<div>Pickleball</div>
<div class="scroll" id="scroll"><div class="cols"><div id="times"></div><div id="slots"></div></div></div>
<div data-toast-portal="true"><div role="region" aria-label="Notifications" id="messages"></div></div>

<div class="ant-modal-root" data-testid="reservation-create-modal" id="modalroot">
  <div class="ant-modal-mask"></div>
  <div class="ant-modal-wrap">
    <div class="ant-modal" role="dialog" aria-modal="true" tabindex="-1">
      <button type="button" class="ant-modal-close" aria-label="Close" data-act="close"></button>
      <div class="ant-modal-header"><div class="ant-modal-title">Reservation</div></div>
      <div class="ant-modal-body" id="modalbody"></div>
      <div class="ant-modal-footer" id="modalfoot"></div>
    </div>
  </div>
</div>
<div class="listbox hidden" role="listbox" id="listbox"></div>
<script>
const DURATIONS = __DURATIONS__;
const TODAY = new Date(__TODAY__ + 'T00:00:00');
const LOAD_DELAY = __LOAD_DELAY__;
const DURATIONS_BY_SLOT = __DURATIONS_BY_SLOT__;
const MODAL_OFFSET = __MODAL_OFFSET__;
const FORM_DELAY = __FORM_DELAY__;
const REFUSE = new Set(__REFUSE__);
let current = new Date(TODAY);
let maxDate = new Date(TODAY);
let selected = null;

function fmtLabel(d) {
  return d.toLocaleDateString('en-US', {weekday: 'long', month: 'long', day: 'numeric'});
}
function iso(d) {
  return d.getFullYear() + '-' + String(d.getMonth()+1).padStart(2,'0') + '-' + String(d.getDate()).padStart(2,'0');
}
function isoLocal(ms) {
  const d = new Date(ms);
  const p = n => String(n).padStart(2,'0');
  return d.getFullYear() + '-' + p(d.getMonth()+1) + '-' + p(d.getDate()) + 'T' + p(d.getHours()) + ':' + p(d.getMinutes()) + ':00';
}
function clock(ms) {
  const d = new Date(ms);
  let h = d.getHours(), m = d.getMinutes();
  const ap = h < 12 ? 'AM' : 'PM';
  h = h % 12 || 12;
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
  const q = encodeURIComponent(JSON.stringify({startDate: iso(current), orgId: 13233}));
  const data = (await (await fetch('/Online/Reservations/ReadConsolidated/13233?jsonData=' + q)).json()).Data;
  const times = document.getElementById('times');
  const slots = document.getElementById('slots');
  times.innerHTML = '';
  slots.innerHTML = '';
  data.forEach((slot, i) => {
    const ms = parseInt(slot.Start.match(/\\d+/)[0], 10);
    const t = document.createElement('div');
    t.className = 'time';
    t.textContent = clock(ms);
    times.appendChild(t);
    const c = document.createElement('div');
    c.className = 'cell';
    if (slot.IsInPast) {
      c.appendChild(document.createElement('div'));
    } else if (slot.AvailableCourts > 0) {
      const b = document.createElement('div');
      b.className = (i % 2 ? 'acss-rlrib0' : 'acss-1j10ohc') + ' reserve';
      b.setAttribute('role', 'button');
      b.tabIndex = 0;
      b.textContent = 'Reserve';
      b.addEventListener('click', () => openModal(ms));
      c.appendChild(b);
    } else {
      const n = document.createElement('div');
      n.className = 'none';
      n.textContent = 'None Available';
      c.appendChild(n);
    }
    slots.appendChild(c);
  });
  await new Promise(r => setTimeout(r, LOAD_DELAY));
  setNav(true);
}
function openShell() { document.getElementById('modalroot').classList.add('open'); }
function showLoading() {
  openShell();
  document.getElementById('modalbody').innerHTML = '<div class="loading">Loading</div>';
  document.getElementById('modalfoot').innerHTML = '';
}
function showRefusal(message) {
  openShell();
  document.getElementById('modalbody').innerHTML =
    '<div>Reservation unavailable</div><div id="refusemsg"></div>';
  document.getElementById('refusemsg').textContent = message;
  document.getElementById('modalfoot').innerHTML =
    '<button type="button" data-testid="reservation-mobile-back-btn" data-act="close">Close</button>';
}
function showForm(ms) {
  selected = ms;
  const shown = ms + MODAL_OFFSET * 60000;
  const when = new Date(shown);
  const tile = when.toLocaleDateString('en-US', {month: 'short'}).toUpperCase() + ' ' + when.getDate();
  const day = when.toLocaleDateString('en-US', {weekday: 'long'});
  const range = clock(shown) + ' \\u2013 ' + clock(shown + 30 * 60000);
  openShell();
  document.getElementById('modalbody').innerHTML =
    '<div data-testid="reservation-date-time-display"><span id="dt-tile"></span> <span id="dt-day"></span> <span id="dt-range"></span>' +
    ' <span>Pickleball</span></div>' +
    '<div data-testid="reservation-duration"><label>Duration</label>' +
    '<button type="button" role="combobox" aria-expanded="false" id="combo" data-act="combo">30 minutes</button>' +
    '<input name="duration" type="hidden" id="durationinput" value="30"></div>' +
    '<div role="checkbox" aria-checked="false" id="disclosure" data-act="disclosure" tabindex="0">' +
    '<div data-testid="reservation-disclosure-agree"></div><span>I agree to the disclosure</span></div>' +
    '<div class="ant-form-item-explain-error hidden" id="formerror"></div>';
  document.getElementById('dt-tile').textContent = tile;
  document.getElementById('dt-day').textContent = day;
  document.getElementById('dt-range').textContent = range;
  document.getElementById('modalfoot').innerHTML =
    '<button type="button" data-testid="reservation-mobile-back-btn" data-act="close">Close</button>' +
    '<button type="button" data-testid="reservation-mobile-submit-btn" id="confirm" data-act="confirm">Confirm</button>';
}
async function openModal(ms) {
  fetch('/api/portal/opened?slot=' + encodeURIComponent(clock(ms)));
  showLoading();
  const r = await fetch('/Online/ReservationsApi/CreateReservation/13233?isApiCall=true&start='
    + encodeURIComponent(isoLocal(ms)) + '&courtType=Pickleball&isConsolidated=true');
  const body = await r.json();
  if (!body.isValid) { showRefusal(body.message || 'No court available'); return; }
  if (FORM_DELAY) await new Promise(res => setTimeout(res, FORM_DELAY));
  showForm(ms);
}
function closeModal() {
  document.getElementById('modalroot').classList.remove('open');
  closeList();
}
function closeList() {
  document.getElementById('listbox').classList.add('hidden');
  const combo = document.getElementById('combo');
  if (combo) combo.setAttribute('aria-expanded', 'false');
}
function openList() {
  const lb = document.getElementById('listbox');
  lb.innerHTML = '';
  const allowed = DURATIONS_BY_SLOT[clock(selected)];
  for (const [text, value] of DURATIONS.filter(([t]) => !allowed || allowed.includes(t))) {
    const o = document.createElement('div');
    o.setAttribute('role', 'option');
    o.className = 'ant-select-item-option';
    o.textContent = text;
    o.dataset.value = String(value);
    lb.appendChild(o);
  }
  lb.classList.remove('hidden');
  document.getElementById('combo').setAttribute('aria-expanded', 'true');
}
document.getElementById('modalroot').addEventListener('click', (e) => {
  const act = e.target.closest('[data-act]');
  if (!act) return;
  if (act.getAttribute('data-act') === 'close') closeModal();
  if (act.getAttribute('data-act') === 'combo') openList();
  if (act.getAttribute('data-act') === 'disclosure') {
    const box = document.getElementById('disclosure');
    box.setAttribute('aria-checked', box.getAttribute('aria-checked') === 'true' ? 'false' : 'true');
  }
  if (act.getAttribute('data-act') === 'confirm') confirm();
});
document.getElementById('listbox').addEventListener('click', (e) => {
  const o = e.target.closest('[role=option]');
  if (!o) return;
  document.getElementById('combo').textContent = o.textContent;
  document.getElementById('durationinput').value = o.dataset.value;
  closeList();
});
async function confirm() {
  if (document.getElementById('disclosure').getAttribute('aria-checked') !== 'true') {
    const err = document.getElementById('formerror');
    err.textContent = 'Please accept the disclosure.';
    err.classList.remove('hidden');
    return;
  }
  const r = await fetch('/api/portal/book', {method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({start: selected, duration: document.getElementById('durationinput').value})});
  const body = await r.json();
  if (body.status === 'ok') {
    closeModal();
    document.getElementById('messages').innerHTML = '<div class="toast" role="status">Reservation created successfully</div>';
    load();
  } else {
    const err = document.getElementById('formerror');
    err.textContent = body.message;
    err.classList.remove('hidden');
  }
}
document.addEventListener('keydown', (e) => {
  if (e.key !== 'Escape') return;
  if (!document.getElementById('listbox').classList.contains('hidden')) closeList();
  else closeModal();
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

    def _page(self) -> str:
        return (
            _GRID_PAGE.replace("__DURATIONS__", json.dumps(DURATIONS))
            .replace("__TODAY__", json.dumps(self.state.today.isoformat()))
            .replace("__LOAD_DELAY__", str(self.state.load_delay_ms))
            .replace("__DURATIONS_BY_SLOT__", json.dumps(self.state.durations_by_slot))
            .replace("__MODAL_OFFSET__", str(self.state.modal_time_offset_min))
            .replace("__FORM_DELAY__", str(self.state.form_delay_ms))
            .replace("__REFUSE__", json.dumps(sorted(self.state.refuse_open)))
        )

    def do_GET(self) -> None:  # noqa: N802
        url = urlparse(self.path)
        if url.path.startswith("/Online/Account/LogIn"):
            self._send(_LOGIN_PAGE)
            return
        if not self._authenticated():
            self._redirect(f"/Online/Account/LogIn/{ORG_ID}")
            return
        if url.path.startswith(("/Online/Reservations/Bookings", "/Online/Portal/Index")):
            self._send(self._page())
            return
        if url.path == "/api/portal/opened":
            self.state.forms_opened.append(parse_qs(url.query).get("slot", [""])[0])
            self._send("{}", content_type="application/json")
            return
        if url.path.startswith("/Online/AjaxController/BookingWindowStatus"):
            ahead = self.state.days_ahead if self.state.released else self.state.days_ahead - 1
            last = (self.state.today + timedelta(days=ahead)).isoformat()
            self._send(
                json.dumps({"isOpen": self.state.released, "maxDateStringDisplay": last,
                            "releasedDateStringDisplay": last}),
                content_type="application/json",
            )
            return
        if url.path.startswith("/Online/Reservations/ReadConsolidated"):
            query = json.loads(parse_qs(url.query).get("jsonData", ["{}"])[0])
            day = date.fromisoformat(query.get("startDate", self.state.today.isoformat()))
            self.state.reads.append(day.isoformat())
            rows = self._slots(day)
            self._send(json.dumps({"Data": rows, "Total": len(rows)}), content_type="application/json")
            return
        if url.path.startswith("/Online/ReservationsApi/CreateReservation"):
            start = parse_qs(url.query).get("start", [""])[0]
            label = datetime.fromisoformat(start).strftime("%-I:%M %p") if start else ""
            if label in self.state.refuse_open:
                self._send(
                    json.dumps({"isValid": False, "isMessage": True, "message": "No court available"}),
                    content_type="application/json",
                )
                return
            self._send(json.dumps({"isValid": True, "isMessage": False}), content_type="application/json")
            return
        self._send("<h1>Not found</h1>", status=404)

    def _slots(self, day: date) -> list[dict[str, object]]:
        opens = self.state.open_slots.get(day, {})
        rows = []
        for label in _slot_times():
            moment = datetime.combine(day, datetime.strptime(label, "%I:%M %p").time())
            ms = int(moment.timestamp() * 1000)
            rows.append({
                "Id": f"Pickleball{day:%m/%d/%Y} {moment:%H:%M:%S}",
                "Start": f"/Date({ms})/",
                "End": f"/Date({ms + 30 * 60 * 1000})/",
                "CourtType": "Pickleball",
                "AvailableCourts": opens.get(label, 0),
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
            self.state.book_requests += 1
            payload = json.loads(raw or "{}")
            moment = datetime.fromtimestamp(int(payload["start"]) / 1000)
            if self.state.reject_message:
                self._send(
                    json.dumps({"status": "error", "message": self.state.reject_message}),
                    content_type="application/json",
                )
                return
            self.state.bookings.append({
                "date": moment.date().isoformat(),
                "slot": moment.strftime("%-I:%M %p"),
                "duration": str(payload.get("duration")),
            })
            self._send(json.dumps({"status": "ok"}), content_type="application/json")
            return
        self._send("<h1>Not found</h1>", status=404)


class FakePortalGrid:
    """The 19 Nov scheduler, on a free local port."""

    def __init__(self, state: PortalState) -> None:
        self.state = state
        handler = type("BoundGridHandler", (_Handler,), {"state": state})
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    def __enter__(self) -> FakePortalGrid:
        self._thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)
