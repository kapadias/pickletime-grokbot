"""Read-only capture of what the live booking site renders, for fixing selectors.

When CourtReserve changes its pages, the engine's selectors stop matching and
the fix needs the page's real structure. This logs in, opens the bookings page,
steps to the target date and (optionally) opens one slot's booking form, then
records:

* a structural outline of each page: tags, classes, roles and data attributes;
* the site's own data requests (fetch/XHR), with JSON responses reduced to
  their shape;
* screenshots, for the owner's eyes.

It never books. It never clicks a button labelled save, confirm, submit, pay,
book or reserve inside a dialog, and it closes any dialog it opened.

Privacy: text is kept only when it is made entirely of times, dates, numbers
and common interface words ("Reserve", "Pickleball", "60 minutes", "Today").
Anything else - names, emails, notes - becomes a placeholder such as
``«text:14»`` before anything is written. Screenshots are not scrubbed: they
show what the owner sees, and stay on the owner's computer unless shared.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit

from .core.browser import BrowserManager
from .logging_setup import get_logger
from .services.auth import AuthService
from .settings import Settings
from .timeutil import format_us_date, now_in

logger = get_logger()

__all__ = ["scrub_text", "scrub_url", "json_shape", "capture_site", "render_summary"]

# ---------------------------------------------------------------------------
# Scrubbing
# ---------------------------------------------------------------------------
# A word list reads better as text; SIM905 is ignored for this file in pyproject.toml.
_VOCAB = frozenset(
    """
    a add all am an and any are at available back book booked booking bookings by
    calendar camps cancel choose class classes clear close closed confirm continue court
    courts date day days details disclosure done duration edit end event events fee
    filter for free from full guest guests home hour hours hr hrs in info item items
    list log login logout member members menu min mins minute minutes month more my new
    next no none not notes of off ok on open or out pay payment pick pickleball player
    players please pm previous prev price refresh remove required reservation
    reservations reserve reserved resource save schedule select selected show sign slot
    slots sport sports start submit tennis the time times to today total type
    unavailable view waitlist week with yes your announcements reservations activities
    settings profile help search loading monday tuesday wednesday thursday friday
    saturday sunday mon tue tues wed thu thur thurs fri sat sun january february march
    april may june july august september october november december jan feb mar apr jun
    jul aug sep sept oct nov dec agree terms waiver accept another again i have read
    understand
    """.split()
)
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_LONG_DIGITS = re.compile(r"\d{7,}")
_TOKEN = re.compile(r"[A-Za-z]+|\d+")
_SAFE_ATTR = re.compile(r"^[\w\-:./#\[\]=, ]{0,160}$")


def scrub_text(text: str | None) -> str:
    """Keep text only when every word is a number or an interface word."""
    if not text:
        return ""
    cleaned = " ".join(str(text).split())
    if not cleaned:
        return ""
    if _EMAIL.search(cleaned):
        return f"«email:{len(cleaned)}»"
    for token in _TOKEN.findall(cleaned):
        if token.isdigit():
            if len(token) >= 7:
                return f"«text:{len(cleaned)}»"
            continue
        if token.lower() not in _VOCAB:
            return f"«text:{len(cleaned)}»"
    return cleaned[:120]


def _scrub_attr_value(value: str) -> str:
    value = str(value)
    if _EMAIL.search(value):
        return "«email»"
    if value.startswith(("/", "http://", "https://", "?")):
        return scrub_url(value)
    value = _LONG_DIGITS.sub("«num»", value)
    if _SAFE_ATTR.match(value):
        return value
    return scrub_text(value) if len(value) < 200 else f"«value:{len(value)}»"


def scrub_url(url: str | None) -> str:
    """Path and query keys are kept; query values only when short and plain."""
    if not url:
        return ""
    url = str(url)
    if url.startswith("#") or url.startswith("javascript:"):
        return url[:40]
    parts = urlsplit(url)
    path = _LONG_DIGITS.sub("«num»", parts.path)
    query = []
    for key, value in parse_qsl(parts.query, keep_blank_values=True):
        if _EMAIL.search(value):
            query.append(f"{key}=«v»")
        elif (
            re.fullmatch(r"\d{1,6}|true|false", value)
            or _URL_DATETIME.fullmatch(value)
            or (value and not scrub_text(value).startswith("«"))
        ):
            query.append(f"{key}={value}")
        else:
            query.append(f"{key}=«v»")
    prefix = f"{parts.scheme}://{parts.netloc}" if parts.netloc else ""
    return f"{prefix}{path}" + (f"?{'&'.join(query)}" if query else "")


_URL_DATETIME = re.compile(
    r"\d{1,2}/\d{1,2}/\d{4}( \d{1,2}:\d{2}( ?[AP]M)?)?|\d{4}-\d{2}-\d{2}([T ][\d:.]+)?(Z|[+-]\d{2}:?\d{2})?",
    re.I,
)
_DATEISH = re.compile(r"^\d{4}-\d{2}-\d{2}([T ][\d:.]+)?(Z|[+-]\d{2}:?\d{2})?$|^\d{1,2}/\d{1,2}/\d{4}")


def json_shape(value: Any, *, depth: int = 0, max_depth: int = 7) -> Any:
    """The structure of a JSON value, with scalars reduced to safe examples."""
    if depth > max_depth:
        return "…"
    if isinstance(value, dict):
        return {str(k)[:60]: json_shape(v, depth=depth + 1) for k, v in list(value.items())[:60]}
    if isinstance(value, list):
        if not value:
            return []
        shaped = [json_shape(item, depth=depth + 1) for item in value[:2]]
        return shaped + ([f"… {len(value)} items"] if len(value) > 2 else [])
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, int | float):
        return value if abs(value) < 1_000_000 else "«num»"
    text = str(value)
    if _DATEISH.match(text):
        return text
    scrubbed = scrub_text(text)
    return scrubbed if not scrubbed.startswith("«") else f"«str:{len(text)}»"


# ---------------------------------------------------------------------------
# In-page JavaScript
# ---------------------------------------------------------------------------
#: Builds a compact outline of a subtree. Raw text comes back to Python and is
#: scrubbed there before anything is written.
_OUTLINE_JS = r"""
const root = arguments[0] || document.body;
const maxNodes = arguments[1] || 1500;
const SKIP = new Set(['SCRIPT','STYLE','NOSCRIPT','LINK','META','TEMPLATE','IFRAME']);
const KEEP_ATTRS = ['id','class','role','type','name','aria-label','aria-selected','aria-disabled',
  'aria-expanded','aria-modal','aria-haspopup','title','placeholder','href','disabled','tabindex','for','colspan','rowspan'];
let count = 0;
function attrs(el) {
  const out = {};
  for (const a of el.attributes) {
    if (KEEP_ATTRS.includes(a.name) || a.name.startsWith('data-')) out[a.name] = a.value;
  }
  if (el.tagName === 'INPUT') {
    const t = (el.getAttribute('type') || 'text').toLowerCase();
    if (['checkbox','radio','submit','button'].includes(t)) out['value'] = el.value;
    if (['checkbox','radio'].includes(t)) out['checked'] = String(el.checked);
  }
  if (el.tagName === 'OPTION') out['value'] = el.value;
  return out;
}
function ownText(el) {
  let t = '';
  for (const n of el.childNodes) if (n.nodeType === 3) t += n.textContent;
  return t.trim().slice(0, 160);
}
function sig(el) { return el.tagName + '|' + (el.getAttribute('class') || '') + '|' + (el.getAttribute('role') || ''); }
function walk(el, depth) {
  if (count >= maxNodes) return null;
  count++;
  const node = {tag: el.tagName.toLowerCase(), attrs: attrs(el), text: ownText(el)};
  try {
    const cs = getComputedStyle(el);
    if (cs.cursor === 'pointer') node.pointer = true;
    if (cs.display === 'none' || cs.visibility === 'hidden') node.hidden = true;
  } catch (e) {}
  if (el.tagName === 'svg' || el.tagName === 'SVG') return node;
  if (depth > 45) { node.truncated = 'depth'; return node; }
  const kids = [];
  let prevSig = null, run = 0;
  for (const child of el.children) {
    if (SKIP.has(child.tagName)) continue;
    const s = sig(child);
    if (s === prevSig) { run++; } else { run = 0; prevSig = s; }
    if (run >= 3) { if (run === 3) kids.push({collapsed: true, like: s}); else kids[kids.length-1].more = (kids[kids.length-1].more || 1) + 1; continue; }
    const k = walk(child, depth + 1);
    if (k) kids.push(k);
  }
  if (kids.length) node.children = kids;
  if (shadowOf(el)) node.shadow = true;
  return node;
}
function shadowOf(el) { return !!el.shadowRoot; }
return {outline: walk(root, 0), nodes: count, truncated: count >= maxNodes};
"""

#: Finds the toolbar's date label and its next-day control.
_DATE_NAV_JS = r"""
const DATE_RE = /(Mon|Tue|Wed|Thu|Fri|Sat|Sun)[a-z]*,?\s+(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+\d{1,2}/;
const all = Array.from(document.querySelectorAll('body *')).filter(e => e.offsetParent !== null);
const labels = all.filter(e => DATE_RE.test(e.textContent || '') && (e.textContent || '').length < 60)
  .sort((a, b) => (a.textContent.length - b.textContent.length));
const label = labels[0] || null;
let next = null, how = null;
const clickable = e => e.matches('button,a,[role=button]') || getComputedStyle(e).cursor === 'pointer';
const aria = Array.from(document.querySelectorAll('button,a,[role=button]'))
  .filter(e => e.offsetParent !== null && /next|forward/i.test((e.getAttribute('aria-label') || '') + ' ' + (e.getAttribute('title') || '') + ' ' + (e.className || '')));
if (aria.length) { next = aria[0]; how = 'aria/title/class mentions next'; }
if (!next && label) {
  let box = label;
  for (let i = 0; i < 4 && box && !next; i++) {
    box = box.parentElement;
    if (!box) break;
    const btns = Array.from(box.querySelectorAll('button,a,[role=button]')).filter(e => e.offsetParent !== null && !e.contains(label) && !label.contains(e));
    const after = btns.filter(b => label.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_PRECEDING);
    const before = btns.filter(b => label.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING);
    if (before.length >= 1 && /today/i.test(btns[0].textContent || '') && btns.length >= 3) { next = btns[2]; how = 'third control after Today'; }
    else if (after.length) { next = after[after.length - 1]; how = 'control just before the date label'; }
  }
}
return {label: label, labelText: label ? label.textContent.trim().slice(0, 60) : null, next: next, how: how};
"""

#: Candidate open slots inside the main content (not the header/nav/toolbar).
_SLOT_CANDIDATES_JS = r"""
const TIME = /\b\d{1,2}:\d{2}\s?(AM|PM)\b/i;
const WORD = /\b(reserve|book|available|open)\b/i;
const BAD = /\b(save|confirm|submit|pay|checkout|cancel|today|next|prev|previous|log ?out|sign ?out)\b/i;
const inChrome = e => !!e.closest('header,nav,[role=navigation],[role=banner],[role=toolbar]');
const els = Array.from(document.querySelectorAll('button,a,[role=button],[role=gridcell],td,div,span'))
  .filter(e => e.offsetParent !== null && !inChrome(e));
const out = [];
for (const e of els) {
  const label = ((e.getAttribute('aria-label') || '') + ' ' + (e.getAttribute('title') || '') + ' ' + (e.innerText || '')).trim();
  if (!label || label.length > 120) continue;
  const clickable = e.matches('button,a,[role=button]') || getComputedStyle(e).cursor === 'pointer' || e.hasAttribute('onclick') || Object.keys(e.dataset || {}).length > 0;
  if (!clickable) continue;
  if (BAD.test(label)) continue;
  if (!(TIME.test(label) || WORD.test(label))) continue;
  if (Array.from(e.children).some(c => c.matches('button,a,[role=button]'))) continue;
  out.push(e);
  if (out.length >= 15) break;
}
return out;
"""

#: The most likely dialog after a slot click.
_DIALOG_JS = r"""
const sel = '[role=dialog],[aria-modal=true],.modal.show,.modal.in,.k-window,dialog[open],.MuiDialog-root,.ReactModal__Content,[data-testid*=modal i],[class*=modal i][class*=open i],[class*=drawer i][class*=open i]';
const found = Array.from(document.querySelectorAll(sel)).filter(e => e.offsetParent !== null || getComputedStyle(e).position === 'fixed');
found.sort((a, b) => b.querySelectorAll('*').length - a.querySelectorAll('*').length);
return found[0] || null;
"""

#: Every form control inside an element, with its options and labels.
_CONTROLS_JS = r"""
const root = arguments[0];
const out = [];
for (const e of root.querySelectorAll('select,input,textarea,button,[role=combobox],[role=listbox],[role=option],[role=radio],[role=checkbox],[role=switch]')) {
  const item = {tag: e.tagName.toLowerCase(), type: e.getAttribute('type'), id: e.id, name: e.getAttribute('name'),
    role: e.getAttribute('role'), cls: e.getAttribute('class'), testid: e.getAttribute('data-testid'),
    label: (e.labels && e.labels[0] ? e.labels[0].innerText : '') || e.getAttribute('aria-label') || '',
    text: (e.innerText || '').slice(0, 80), disabled: e.disabled || e.getAttribute('aria-disabled') === 'true'};
  if (e.tagName === 'SELECT') item.options = Array.from(e.options).map(o => ({value: o.value, text: o.text}));
  out.push(item);
  if (out.length > 80) break;
}
return out;
"""

_DANGEROUS_BUTTON = re.compile(r"\b(save|confirm|submit|pay|checkout|book|reserve|purchase|complete)\b", re.I)
_CLOSE_BUTTON = re.compile(r"^\s*(cancel|close|×|x|back|no)\s*$", re.I)


# ---------------------------------------------------------------------------
# Scrubbing structured results
# ---------------------------------------------------------------------------
def _scrub_outline(node: dict[str, Any] | None) -> dict[str, Any] | None:
    if node is None:
        return None
    if node.get("collapsed"):
        return {"collapsed": True, "like": _scrub_attr_value(node.get("like", "")), "more": node.get("more", 1)}
    attrs = {}
    for key, value in (node.get("attrs") or {}).items():
        if key == "href":
            attrs[key] = scrub_url(value)
        elif key in {"aria-label", "title", "placeholder"}:
            attrs[key] = scrub_text(value)
        else:
            attrs[key] = _scrub_attr_value(value)
    out: dict[str, Any] = {"tag": node.get("tag"), "attrs": attrs}
    text = scrub_text(node.get("text"))
    if text:
        out["text"] = text
    for flag in ("pointer", "hidden", "truncated", "shadow"):
        if node.get(flag):
            out[flag] = node[flag]
    kids = [_scrub_outline(child) for child in node.get("children") or []]
    if kids:
        out["children"] = kids
    return out


def outline_lines(node: dict[str, Any] | None, depth: int = 0, *, limit: int = 900) -> list[str]:
    """Indented one-line-per-element rendering of a scrubbed outline."""
    lines: list[str] = []

    def emit(n: dict[str, Any], d: int) -> None:
        if len(lines) >= limit:
            return
        pad = "  " * d
        if n.get("collapsed"):
            lines.append(f"{pad}… {n.get('more', 1)} more sibling(s) like {n.get('like')}")
            return
        a = n.get("attrs") or {}
        head = n.get("tag", "?")
        if a.get("id"):
            head += f"#{a['id']}"
        if a.get("class"):
            head += "." + ".".join(str(a["class"]).split()[:4])
        rest = " ".join(f'{k}="{v}"' for k, v in a.items() if k not in {"id", "class"})
        flags = "".join(f" [{f}]" for f in ("pointer", "hidden", "shadow") if n.get(f))
        text = f'  "{n["text"]}"' if n.get("text") else ""
        lines.append(f"{pad}{head}{(' ' + rest) if rest else ''}{flags}{text}")
        for child in n.get("children") or []:
            emit(child, d + 1)

    if node:
        emit(node, depth)
    if len(lines) >= limit:
        lines.append(f"… outline cut at {limit} lines")
    return lines


# ---------------------------------------------------------------------------
# The capture
# ---------------------------------------------------------------------------
class _CaptureBrowser(BrowserManager):
    """The engine's Chrome, plus the network log and with images on."""

    def build_options(self):  # type: ignore[no-untyped-def]
        options = super().build_options()
        options.set_capability("goog:loggingPrefs", {"performance": "ALL"})
        return options


def _settle(driver: Any, seconds: float = 6.0, quiet: float = 1.5) -> None:
    """Wait until the page has stopped issuing requests for ``quiet`` seconds."""
    deadline = time.monotonic() + seconds
    last_count, last_change = -1, time.monotonic()
    while time.monotonic() < deadline:
        try:
            count = driver.execute_script("return performance.getEntriesByType('resource').length")
        except Exception:  # noqa: BLE001
            count = last_count
        if count != last_count:
            last_count, last_change = count, time.monotonic()
        elif time.monotonic() - last_change >= quiet:
            return
        time.sleep(0.25)


class _Network:
    """Collects the site's own requests from Chrome's performance log."""

    INTERESTING = re.compile(r"reserv|slot|court|sched|avail|book|time|event|calendar|resource|member|api", re.I)

    def __init__(self, driver: Any, site_host: str) -> None:
        self.driver = driver
        self.site_host = site_host
        self.requests: dict[str, dict[str, Any]] = {}
        try:
            driver.execute_cdp_cmd("Network.enable", {})
        except Exception:  # noqa: BLE001 - capture works without it, just less
            logger.warning("Network domain unavailable; data requests will not be captured")

    def drain(self, phase: str) -> None:
        try:
            entries = self.driver.get_log("performance")
        except Exception:  # noqa: BLE001
            return
        for entry in entries:
            try:
                message = json.loads(entry["message"])["message"]
            except (KeyError, ValueError):
                continue
            method, params = message.get("method"), message.get("params", {})
            if method == "Network.requestWillBeSent":
                req = params.get("request", {})
                url = req.get("url", "")
                if self.site_host not in urlsplit(url).netloc:
                    continue
                if params.get("type") not in {"XHR", "Fetch", "Document", None}:
                    continue
                self.requests[params["requestId"]] = {
                    "phase": phase,
                    "method": req.get("method"),
                    "url": url,
                    "type": params.get("type"),
                    "post_keys": _post_keys(req.get("postData")),
                }
            elif method == "Network.responseReceived":
                rid = params.get("requestId")
                if rid in self.requests:
                    resp = params.get("response", {})
                    self.requests[rid].update(status=resp.get("status"), mime=resp.get("mimeType"))
        for rid, item in self.requests.items():
            if "shape" in item or "json" not in str(item.get("mime", "")):
                continue
            if not self.INTERESTING.search(item["url"]):
                item["shape"] = None
                continue
            try:
                body = self.driver.execute_cdp_cmd("Network.getResponseBody", {"requestId": rid})
                item["shape"] = json_shape(json.loads(body.get("body", "")))
            except Exception:  # noqa: BLE001 - body gone or not JSON
                item["shape"] = None

    def report(self) -> list[dict[str, Any]]:
        out = []
        for item in self.requests.values():
            out.append({
                "phase": item["phase"],
                "method": item.get("method"),
                "type": item.get("type"),
                "status": item.get("status"),
                "mime": item.get("mime"),
                "url": scrub_url(item["url"]),
                "post_keys": item.get("post_keys"),
                "shape": item.get("shape"),
            })
        return out


def _post_keys(data: str | None) -> list[str] | None:
    if not data:
        return None
    try:
        parsed = json.loads(data)
        return sorted(str(k) for k in parsed)[:40] if isinstance(parsed, dict) else ["<json list>"]
    except ValueError:
        return sorted({k for k, _ in parse_qsl(data)})[:40] or ["<body>"]


def capture_site(
    settings: Settings,
    out_dir: Path,
    *,
    open_slot: bool = False,
    browser_factory: Callable[[Any], BrowserManager] | None = None,
) -> dict[str, Any]:
    """Run the capture and write ``capture.json``, ``summary.txt`` and screenshots."""
    out_dir.mkdir(parents=True, exist_ok=True)
    booking = settings.booking.model_copy(update={"block_images": False, "dry_run": True})
    target: date = booking.target_date()
    booking = booking.for_date(target)
    result: dict[str, Any] = {
        "target_date": format_us_date(target),
        "bookings_url": scrub_url(booking.bookings_url),
        "open_slot": open_slot,
        "steps": [],
    }

    def step(name: str, ok: bool, detail: str = "") -> None:
        result["steps"].append({"step": name, "ok": ok, "detail": detail})
        logger.info(f"capture step {name}: {'ok' if ok else 'FAILED'} {detail}")

    manager = (browser_factory or _CaptureBrowser)(booking)
    driver, waiter = manager.start()
    network = _Network(driver, urlsplit(booking.base_url).netloc)
    try:
        def snapshot(name: str, element: Any = None, max_nodes: int = 1500) -> None:
            _settle(driver)
            network.drain(name)
            try:
                driver.save_screenshot(str(out_dir / f"{name}.png"))
            except Exception as exc:  # noqa: BLE001 - a missing screenshot is not fatal
                logger.debug(f"screenshot {name} failed: {exc}")
            raw = driver.execute_script(_OUTLINE_JS, element, max_nodes)
            result[name] = {
                "url": scrub_url(driver.current_url),
                "title": scrub_text(driver.title),
                "frames": len(driver.find_elements("css selector", "iframe")),
                "nodes": raw.get("nodes"),
                "truncated": raw.get("truncated"),
                "outline": _scrub_outline(raw.get("outline")),
            }

        # 1. Login, with the engine's own service.
        try:
            AuthService(driver, waiter, booking, settings.credentials).login()
            step("login", True)
        except Exception as exc:  # noqa: BLE001
            step("login", False, f"{type(exc).__name__}: {str(exc).splitlines()[0]}")
            snapshot("after_login")
            return _finish(result, network, out_dir)
        snapshot("after_login", max_nodes=600)

        # 2. The bookings page as the engine opens it.
        driver.get(booking.bookings_url)
        snapshot("bookings_today")
        legacy = bool(driver.find_elements("css selector", ".k-scheduler-content"))
        step("bookings_page", True, f"legacy Kendo grid present: {legacy}")

        # 3. Step forward to the target date.
        days = (target - now_in(booking.tz).date()).days
        moved = 0
        nav = driver.execute_script(_DATE_NAV_JS)
        result["date_nav"] = {"label": scrub_text(nav.get("labelText")), "how": nav.get("how")}
        if days <= 0:
            step("date_nav", True, "target date is today; no stepping needed")
        elif nav.get("next") is not None:
            for _ in range(days):
                before = driver.execute_script(_DATE_NAV_JS).get("labelText")
                try:
                    driver.execute_script("arguments[0].click()", nav["next"])
                except Exception:  # noqa: BLE001 - element went stale after a re-render
                    nav = driver.execute_script(_DATE_NAV_JS)
                    if nav.get("next") is None:
                        break
                    driver.execute_script("arguments[0].click()", nav["next"])
                def label_changed(previous: str | None = before) -> bool:
                    return bool(driver.execute_script(_DATE_NAV_JS).get("labelText") != previous)

                waiter.until(label_changed, timeout=6)
                nav = driver.execute_script(_DATE_NAV_JS)
                moved += 1
            label = driver.execute_script(_DATE_NAV_JS).get("labelText")
            step("date_nav", True, f"clicked next {moved}x via {result['date_nav']['how']}; label now {scrub_text(label)!r}")
        else:
            step("date_nav", False, f"no next-day control found (wanted {days} day(s) ahead)")
        snapshot("bookings_target")

        # 4. Slot candidates, and optionally one booking form.
        candidates = driver.execute_script(_SLOT_CANDIDATES_JS) or []
        result["slot_candidates"] = [
            _scrub_outline(driver.execute_script(_OUTLINE_JS, c, 25).get("outline")) for c in candidates[:10]
        ]
        step("slot_candidates", bool(candidates), f"{len(candidates)} found")

        if open_slot and candidates:
            _open_one_slot(driver, waiter, candidates, result, step, snapshot)
        elif open_slot:
            step("open_slot", False, "no candidate slot to open")
    finally:
        try:
            network.drain("end")
        finally:
            manager.close()
    return _finish(result, network, out_dir)


def _open_one_slot(driver, waiter, candidates, result, step, snapshot) -> None:  # type: ignore[no-untyped-def]
    for element in candidates[:5]:
        try:
            label = driver.execute_script(
                "return ((arguments[0].getAttribute('aria-label')||'')+' '+(arguments[0].innerText||'')).trim()", element
            )
            url_before = driver.current_url
            driver.execute_script("arguments[0].scrollIntoView({block:'center'}); arguments[0].click()", element)
        except Exception as exc:  # noqa: BLE001 - try the next candidate
            logger.debug(f"slot candidate click failed: {exc}")
            continue
        dialog = waiter.until(lambda: driver.execute_script(_DIALOG_JS), timeout=6)
        if dialog is None and driver.current_url == url_before:
            continue
        step("open_slot", True, f"clicked {scrub_text(label)!r}; dialog found: {dialog is not None}; "
             f"url changed: {driver.current_url != url_before}")
        if dialog is not None:
            snapshot("slot_dialog", dialog, 1200)
            controls = driver.execute_script(_CONTROLS_JS, dialog) or []
        else:
            snapshot("slot_page", None, 1500)
            controls = driver.execute_script(_CONTROLS_JS, driver.find_element("css selector", "body")) or []
        result["slot_controls"] = [_scrub_control(c) for c in controls]
        _close_dialog(driver, waiter, dialog, url_before, step)
        return
    step("open_slot", False, "clicking the first 5 candidates opened no dialog")


def _close_dialog(driver, waiter, dialog, url_before, step) -> None:  # type: ignore[no-untyped-def]
    from selenium.webdriver.common.keys import Keys  # noqa: PLC0415

    try:
        driver.find_element("css selector", "body").send_keys(Keys.ESCAPE)
    except Exception as exc:  # noqa: BLE001 - the buttons below are the fallback
        logger.debug(f"Escape failed: {exc}")
    if dialog is not None and waiter.until(lambda: driver.execute_script(_DIALOG_JS) is None, timeout=2):
        step("close_dialog", True, "Escape")
        return
    if dialog is not None:
        for button in dialog.find_elements("css selector", "button,a,[role=button]"):
            text = (button.text or button.get_attribute("aria-label") or "").strip()
            if _CLOSE_BUTTON.match(text) and not _DANGEROUS_BUTTON.search(text):
                driver.execute_script("arguments[0].click()", button)
                step("close_dialog", True, f"clicked {text!r}")
                return
    if driver.current_url != url_before:
        driver.back()
        step("close_dialog", True, "navigated back")
        return
    step("close_dialog", False, "left open; the browser is discarded next, nothing was saved")


def _scrub_control(control: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in control.items():
        if value in (None, "", False):
            continue
        if key in {"label", "text"}:
            out[key] = scrub_text(value)
        elif key == "options":
            out[key] = [{"value": _scrub_attr_value(o.get("value", "")), "text": scrub_text(o.get("text"))} for o in value[:30]]
        elif isinstance(value, str):
            out[key] = _scrub_attr_value(value)
        else:
            out[key] = value
    return out


def _finish(result: dict[str, Any], network: _Network, out_dir: Path) -> dict[str, Any]:
    result["network"] = network.report()
    (out_dir / "capture.json").write_text(json.dumps(result, indent=1, default=str), encoding="utf-8")
    (out_dir / "summary.txt").write_text(render_summary(result), encoding="utf-8")
    return result


def render_summary(result: dict[str, Any], *, max_chars: int = 24_000) -> str:
    """A pasteable text version: steps, requests, then the outlines that matter."""
    lines = [
        "PickleTime Bot site capture (scrubbed: names, emails and free text removed)",
        f"target date: {result.get('target_date')}  bookings url: {result.get('bookings_url')}",
        "",
        "== steps",
    ]
    lines += [f"{'ok ' if s['ok'] else 'FAIL'} {s['step']}: {s['detail']}" for s in result.get("steps", [])]
    lines += ["", "== data requests (site host only)"]
    for req in result.get("network", []):
        if req.get("type") == "Document" and not req.get("shape"):
            lines.append(f"[{req['phase']}] {req.get('method')} {req['url']} -> {req.get('status')} (page)")
            continue
        lines.append(f"[{req['phase']}] {req.get('method')} {req['url']} -> {req.get('status')} {req.get('mime') or ''}")
        if req.get("post_keys"):
            lines.append(f"    body keys: {', '.join(req['post_keys'])}")
        if req.get("shape") is not None:
            shape = json.dumps(req["shape"], separators=(",", ":"))
            lines.append(f"    shape: {shape[:1500]}{'…' if len(shape) > 1500 else ''}")
    if result.get("slot_controls"):
        lines += ["", "== booking form controls"]
        lines += [json.dumps(c, separators=(",", ":")) for c in result["slot_controls"]]
    if result.get("slot_candidates"):
        lines += ["", "== slot candidates (first 10)"]
        for cand in result["slot_candidates"]:
            lines += outline_lines(cand, limit=12)
            lines.append("--")
    for name in ("slot_dialog", "slot_page", "bookings_target", "bookings_today", "after_login"):
        snap = result.get(name)
        if not snap:
            continue
        lines += ["", f"== page: {name}  url={snap['url']}  title={snap['title']!r}  iframes={snap['frames']}  "
                      f"nodes={snap['nodes']}{' (truncated)' if snap.get('truncated') else ''}"]
        lines += outline_lines(snap.get("outline"), limit=400 if name != "after_login" else 120)
    text = "\n".join(lines) + "\n"
    if len(text) > max_chars:
        text = text[:max_chars] + f"\n… summary cut at {max_chars} characters; the full scrubbed capture is capture.json\n"
    return text
