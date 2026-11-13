"""A stand-in for CourtReserve, for end-to-end tests against a real browser.

Reproduces the parts of the real site the bot actually touches: the login form,
the Kendo-style scheduler grid with ``data-href`` slot links, the reservation
modal with its duration dropdown and disclosure checkbox, and the confirmation
popup.

Deliberately built without jQuery/Kendo so the tests exercise the **DOM
fallback** path in :meth:`BookingService._set_duration` — the path that runs
whenever the widget API is not reachable, and the one whose race condition used
to book the wrong duration.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

ORG_ID = 13233
SPORT_ID = 16984


@dataclass
class SiteState:
    """Knobs the tests turn."""

    username: str = "player@example.com"
    password: str = "correct-horse"
    #: slot label -> durations the modal will offer, e.g. {"11:30 AM": ["1 hour"]}
    slots: dict[str, list[str]] = field(default_factory=dict)
    #: Slot links stay hidden until this many grid loads have happened.
    reveal_after_loads: int = 0
    #: Refuse the save and show an error instead.
    reject_save: bool = False
    #: Close the modal without any confirmation popup (the "inferred" path).
    silent_success: bool = False
    #: Milliseconds before the duration list is populated, mimicking the real
    #: modal, which renders its shell first and fills the dropdown afterwards.
    duration_delay_ms: int = 0
    #: Expose a Kendo-like widget whose dataSource uses an unexpected field name,
    #: so the widget path misses and must fall through to the rendered list.
    kendo_shim: bool = False

    grid_loads: int = 0
    bookings: list[dict[str, str]] = field(default_factory=list)
    login_attempts: list[str] = field(default_factory=list)


_LOGIN_PAGE = """<!doctype html><html><head><title>Log In</title></head><body>
<form method="POST" action="/login">
  <input type="email" id="UserNameOrEmail" name="UserNameOrEmail" placeholder="Email">
  <input type="password" id="Password" name="Password">
  <button type="submit">Log In</button>
</form>
{error}
</body></html>"""

_ERROR_BLOCK = '<div class="validation-summary-errors" role="alert">Invalid email or password.</div>'

_GRID_PAGE = """<!doctype html><html><head><title>Bookings</title>
<style>
  .hidden {{ display: none; }}
  .modal {{ display: none; }}
  .modal.show {{ display: block; }}
</style>
</head><body>
<ul id="respMenu"><li><a class="parent-header-link" href="/Online/Reservations/Bookings/13233?sId=16984"><span>Reservations</span></a></li></ul>
<a href="/Online/Account/LogOff">Log off</a>

<div id="scheduler" class="k-scheduler">
  <a role="button" class="k-nav-current" id="datepicker-toggle">{current_date}</a>
  <div id="calendar" class="k-calendar hidden">{day_links}</div>
  <div class="k-scheduler-content" id="grid">{slot_links}</div>
</div>

<!-- Mirrors the live CourtReserve modal: id="modal1", the confirm button in a
     header button group with data-testid, and deliberately NO .modal-footer. -->
<div class="modal modal-modal1 fade" id="modal1" role="dialog">
 <div class="modal-dialog"><div class="modal-content">
  <div class="modal-header-container">
    <div class="modal-title"><span class="modal-title-span">Book a reservation</span></div>
    <div class="modal-title-buttons">
      <button type="reset" data-testid="close-btn-modal-header" class="btn btn-light" id="close-btn">Close</button>
      <button type="button" data-testid="save-btn" class="btn btn-primary btn-submit" id="save-btn">Save</button>
      <button type="button" class="btn btn-secondary" id="save-another-btn">Save &amp; Add Another</button>
    </div>
  </div>
  <div class="modal-body">
    <label id="Duration_label">Duration</label>
    <span aria-labelledby="Duration_label" id="duration-display" class="k-dropdownlist">
      <span class="k-input-value-text">Select duration</span>
    </span>
    <input id="Duration" type="hidden">
    <ul id="Duration_listbox" class="hidden"></ul>
    <label><input type="checkbox" id="DisclosureAgree"> I agree</label>
    <div class="field-validation-error hidden" id="modal-error"></div>
  </div>
 </div></div>
</div>

<div class="swal2-popup hidden" id="confirmation"><span id="confirmation-text"></span><button id="ok-btn">OK</button></div>

<script>
var STATE = {state_json};

// Optional Kendo/jQuery stand-in whose dataSource items use a field name the
// widget path does not know, mirroring a real tenant. The rendered <li> list is
// still correct, so a correct implementation recovers via the DOM.
if (STATE.kendo_shim) {{
  window.jQuery = function (sel) {{
    return {{
      data: function (key) {{
        if (key !== 'kendoDropDownList') {{ return null; }}
        return {{
          dataSource: {{
            data: function () {{
              return (current && current.durations ? current.durations : []).map(
                function (d) {{ return {{ SomeUnknownField: d }}; }}
              );
            }}
          }},
          select: function () {{ throw new Error('widget select must not be reached'); }},
          trigger: function () {{}}
        }};
      }}
    }};
  }};
}}
var current = null;

document.getElementById('datepicker-toggle').addEventListener('click', function () {{
  document.getElementById('calendar').classList.toggle('hidden');
}});

document.querySelectorAll('#calendar a.k-link').forEach(function (day) {{
  day.addEventListener('click', function (event) {{
    event.preventDefault();
    document.getElementById('datepicker-toggle').textContent = day.getAttribute('title');
    document.getElementById('calendar').classList.add('hidden');
  }});
}});

function openModal(link) {{
  current = {{
    href: link.getAttribute('data-href'),
    label: link.getAttribute('data-slot'),
    durations: JSON.parse(link.getAttribute('data-durations'))
  }};
  var listbox = document.getElementById('Duration_listbox');
  listbox.innerHTML = '';
  var fill = function () {{
  current.durations.forEach(function (text) {{
    var item = document.createElement('li');
    item.className = 'k-list-item';
    var span = document.createElement('span');
    span.className = 'k-list-item-text';
    span.textContent = text;
    item.appendChild(span);
    item.addEventListener('click', function () {{
      // Mirrors Kendo: the display text only updates on selection.
      document.querySelector('#duration-display .k-input-value-text').textContent = text;
      current.selected = text;
      listbox.classList.add('hidden');
    }});
    listbox.appendChild(item);
  }});
  }};
  if (STATE.duration_delay_ms > 0) {{ setTimeout(fill, STATE.duration_delay_ms); }} else {{ fill(); }}
  document.querySelector('#duration-display .k-input-value-text').textContent = 'Select duration';
  document.getElementById('DisclosureAgree').checked = false;
  document.getElementById('modal-error').classList.add('hidden');
  document.getElementById('modal1').classList.add('show');
}}

document.getElementById('duration-display').addEventListener('click', function () {{
  document.getElementById('Duration_listbox').classList.remove('hidden');
}});

document.querySelectorAll('a[data-href]').forEach(function (link) {{
  link.addEventListener('click', function (event) {{
    event.preventDefault();
    openModal(link);
  }});
}});

function closeModal() {{
  document.getElementById('modal1').classList.remove('show');
  document.getElementById('Duration_listbox').classList.add('hidden');
  current = null;
}}
document.getElementById('close-btn').addEventListener('click', closeModal);

document.getElementById('save-another-btn').addEventListener('click', function () {{
  // If the bot ever clicks this instead of Save, the test should notice.
  document.getElementById('modal-error').textContent = 'WRONG BUTTON: Save & Add Another';
  document.getElementById('modal-error').classList.remove('hidden');
}});

document.getElementById('save-btn').addEventListener('click', function () {{
  if (!current || !current.selected) {{
    document.getElementById('modal-error').textContent = 'Please choose a duration.';
    document.getElementById('modal-error').classList.remove('hidden');
    return;
  }}
  if (!document.getElementById('DisclosureAgree').checked) {{
    document.getElementById('modal-error').textContent = 'You must accept the disclosure.';
    document.getElementById('modal-error').classList.remove('hidden');
    return;
  }}
  var body = JSON.stringify({{slot: current.label, duration: current.selected}});
  var request = new XMLHttpRequest();
  request.open('POST', '/api/book', false);
  request.setRequestHeader('Content-Type', 'application/json');
  request.send(body);
  var reply = JSON.parse(request.responseText);

  if (reply.status === 'rejected') {{
    document.getElementById('modal-error').textContent = reply.message;
    document.getElementById('modal-error').classList.remove('hidden');
    return;
  }}
  var link = document.querySelector('a[data-href="' + current.href.replace(/"/g, '\\\\"') + '"]');
  if (link) {{ link.parentNode.removeChild(link); }}
  closeModal();
  if (!STATE.silent_success) {{
    document.getElementById('confirmation-text').textContent =
      'Your reservation is confirmed for ' + reply.slot + '.';
    document.getElementById('confirmation').classList.remove('hidden');
  }}
}});

document.getElementById('ok-btn').addEventListener('click', function () {{
  document.getElementById('confirmation').classList.add('hidden');
}});
</script>
</body></html>"""


def _slot_link(label: str, durations: list[str], date_str: str, end_label: str) -> str:
    href = (
        f"/Online/Reservations/CreateReservation/{ORG_ID}"
        f"?start={date_str}%20{label.replace(' ', '%20')}"
        f"&end={date_str}%20{end_label.replace(' ', '%20')}"
        f"&courtType=Pickleball&customSchedulerId={SPORT_ID}"
    )
    return (
        f'<a href="#" data-href="{href}" data-slot="{label}" '
        f"data-durations='{json.dumps(durations)}'>{label}</a>"
    )


class _Handler(BaseHTTPRequestHandler):
    state: SiteState
    day_titles: list[str]
    slot_date: str

    def log_message(self, *_args: object) -> None:  # keep test output clean
        return

    # -- helpers ----------------------------------------------------------
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

    # -- routes -----------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        path = urlparse(self.path).path

        if path.startswith("/Online/Account/LogIn"):
            failed = "failed" in parse_qs(urlparse(self.path).query)
            self._send(_LOGIN_PAGE.format(error=_ERROR_BLOCK if failed else ""))
            return

        if path.startswith("/Online/Reservations/Bookings"):
            if not self._authenticated():
                self._redirect(f"/Online/Account/LogIn/{ORG_ID}")
                return
            self.state.grid_loads += 1
            reveal = self.state.grid_loads > self.state.reveal_after_loads
            links = ""
            if reveal:
                links = "".join(
                    _slot_link(label, durations, self.slot_date, _plus_30(label))
                    for label, durations in self.state.slots.items()
                )
            self._send(
                _GRID_PAGE.format(
                    current_date=self.day_titles[0] if self.day_titles else "",
                    day_links="".join(
                        f'<a href="#" class="k-link" title="{title}">{index + 1}</a>'
                        for index, title in enumerate(self.day_titles)
                    ),
                    slot_links=links,
                    state_json=json.dumps(
                        {
                            "silent_success": self.state.silent_success,
                            "kendo_shim": self.state.kendo_shim,
                            "duration_delay_ms": self.state.duration_delay_ms,
                        }
                    ),
                )
            )
            return

        self._send("<h1>Not found</h1>", status=404)

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        path = urlparse(self.path).path
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode("utf-8") if length else ""

        if path == "/login":
            form = parse_qs(raw)
            email = (form.get("UserNameOrEmail") or [""])[0]
            password = (form.get("Password") or [""])[0]
            self.state.login_attempts.append(email)
            if email == self.state.username and password == self.state.password:
                self.send_response(302)
                self.send_header("Set-Cookie", "pickletime-session=ok; Path=/")
                self.send_header("Location", f"/Online/Reservations/Bookings/{ORG_ID}?sId={SPORT_ID}")
                self.send_header("Content-Length", "0")
                self.end_headers()
            else:
                self._redirect(f"/Online/Account/LogIn/{ORG_ID}?failed=1")
            return

        if path == "/api/book":
            payload = json.loads(raw or "{}")
            if self.state.reject_save:
                self._send(
                    json.dumps({"status": "rejected", "message": "That slot is no longer available."}),
                    content_type="application/json",
                )
                return
            self.state.bookings.append(
                {"slot": payload.get("slot", ""), "duration": payload.get("duration", "")}
            )
            self._send(
                json.dumps({"status": "ok", "slot": payload.get("slot", "")}),
                content_type="application/json",
            )
            return

        self._send("<h1>Not found</h1>", status=404)


def _plus_30(label: str) -> str:
    from datetime import datetime, timedelta

    moment = datetime.strptime(label, "%I:%M %p") + timedelta(minutes=30)
    hour = moment.hour % 12 or 12
    return f"{hour}:{moment.minute:02d} {'AM' if moment.hour < 12 else 'PM'}"


class FakeCourtReserve:
    """Context manager that runs the fake site on a free port."""

    def __init__(self, state: SiteState, day_titles: list[str], slot_date: str) -> None:
        self.state = state
        handler = type(
            "BoundHandler",
            (_Handler,),
            {"state": state, "day_titles": day_titles, "slot_date": slot_date},
        )
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    def __enter__(self) -> FakeCourtReserve:
        self._thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)
