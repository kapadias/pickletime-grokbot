"""Lightweight doubles for tests that must not start a browser."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any


class FakeElement:
    """A WebElement stand-in."""

    def __init__(self, *, displayed: bool = True, text: str = "") -> None:
        self.displayed = displayed
        self.text = text
        self.clicks = 0
        self.keys: list[str] = []
        self.cleared = 0

    def is_displayed(self) -> bool:
        return self.displayed

    def click(self) -> None:
        self.clicks += 1

    def send_keys(self, value: str) -> None:
        self.keys.append(value)

    def clear(self) -> None:
        self.cleared += 1


class FakeDriver:
    """Records interactions and replays canned answers.

    ``elements`` maps ``(by, value)`` to a list of :class:`FakeElement`.
    ``scripts`` maps a substring of the JS source to either a value or a
    zero-argument callable, so a test can make a script's answer change over
    successive calls.
    """

    def __init__(
        self,
        elements: dict[tuple[str, str], list[FakeElement]] | None = None,
        scripts: dict[str, Any] | None = None,
        *,
        current_url: str = "https://example.test/",
    ) -> None:
        self.elements = elements or {}
        self.scripts = scripts or {}
        self.current_url = current_url
        self.visited: list[str] = []
        self.executed: list[tuple[str, tuple[Any, ...]]] = []
        self.refreshes = 0
        self.quit_calls = 0

    # -- WebDriver surface ------------------------------------------------
    def find_elements(self, by: str, value: str) -> list[FakeElement]:
        return list(self.elements.get((by, value), []))

    def get(self, url: str) -> None:
        self.visited.append(url)
        self.current_url = url

    def refresh(self) -> None:
        self.refreshes += 1

    def quit(self) -> None:
        self.quit_calls += 1

    def execute_script(self, script: str, *args: Any) -> Any:
        self.executed.append((script, args))
        for needle, answer in self.scripts.items():
            if needle in script:
                return answer() if callable(answer) else answer
        return None

    def save_screenshot(self, path: str) -> bool:
        return True

    def implicitly_wait(self, seconds: float) -> None: ...

    def set_page_load_timeout(self, seconds: float) -> None: ...

    def set_script_timeout(self, seconds: float) -> None: ...

    # -- assertions helpers -----------------------------------------------
    def ran(self, needle: str) -> bool:
        return any(needle in script for script, _ in self.executed)

    def calls_to(self, needle: str) -> int:
        return sum(1 for script, _ in self.executed if needle in script)


class FakeClock:
    """Monotonic clock that only advances when a sleep is requested."""

    def __init__(self, start: float = 0.0) -> None:
        self.value = start
        self.slept: list[float] = []

    def now(self) -> float:
        return self.value

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.value += max(seconds, 0.0)


def counter(answers: list[Any]) -> Callable[[], Any]:
    """Return a callable yielding each answer in turn, repeating the last."""
    remaining = list(answers)

    def next_answer() -> Any:
        if len(remaining) > 1:
            return remaining.pop(0)
        return remaining[0] if remaining else None

    return next_answer
