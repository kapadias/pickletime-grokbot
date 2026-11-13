"""Chrome WebDriver lifecycle.

Notable departures from the previous implementation:

* **Implicit waits are off.** They are mutually exclusive with the explicit
  polling in :mod:`~pickletime.core.waits`; leaving both on made every missed
  locator cost a full timeout.
* **No ``--disable-web-security``.** The bot types real account credentials into
  this browser; turning off the same-origin policy for a claimed speed benefit
  it does not actually provide is a bad trade.
* **Only real flags.** ``--disable-images``, ``--shm-size`` and
  ``--max_old_space_size`` are not Chrome switches (they are a myth, a Docker
  flag, and a Node flag respectively). Image blocking is done through the
  preference that actually works.
* **Always an isolated profile directory**, cleaned up on exit, so a reused
  Cloud Run instance never hits "user data directory is already in use".
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from types import TracebackType
from typing import Any

from selenium import webdriver
from selenium.common.exceptions import WebDriverException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service

from ..errors import BrowserStartError
from ..logging_setup import get_logger
from ..settings import BookingSettings
from .waits import Waiter

logger = get_logger()

#: Switches that are safe, real, and worth the startup cost in a container.
_BASE_ARGS: tuple[str, ...] = (
    "--no-sandbox",
    "--disable-dev-shm-usage",
    "--disable-gpu",
    "--disable-extensions",
    "--disable-default-apps",
    "--disable-background-networking",
    "--disable-background-timer-throttling",
    "--disable-backgrounding-occluded-windows",
    "--disable-renderer-backgrounding",
    "--disable-client-side-phishing-detection",
    "--disable-component-update",
    "--disable-sync",
    "--disable-translate",
    "--disable-breakpad",
    "--disable-crash-reporter",
    "--disable-blink-features=AutomationControlled",
    "--no-first-run",
    "--no-default-browser-check",
    "--no-service-autorun",
    "--metrics-recording-only",
    "--mute-audio",
    "--password-store=basic",
    "--use-mock-keychain",
    "--log-level=3",
)

#: Extra switches for a memory-capped container. Chrome's default multi-process
#: model spawns a renderer per site plus GPU/utility processes; in a small
#: instance that gets the renderer OOM-killed. Collapsing site isolation is an
#: acceptable trade here — the browser visits exactly one trusted origin and is
#: destroyed at the end of the run.
#:
#: ``--no-zygote`` is deliberately NOT here. It stops Chrome forking renderers
#: from a pre-initialised template process, and is a well-known cause of
#: "Unable to receive message from renderer" — the same message a genuinely
#: OOM-killed or gVisor-killed renderer produces, which makes it actively
#: expensive to debug. It saves one small process; it is not worth the ambiguity.
_CONTAINER_ARGS: tuple[str, ...] = (
    "--disable-site-isolation-trials",
    "--renderer-process-limit=1",
    "--disable-software-rasterizer",
    "--disable-accelerated-2d-canvas",
    "--disable-gpu-sandbox",
)

#: Chrome honours only the LAST --disable-features on the command line, so every
#: feature has to go in one flag. Passing two separate ones silently drops the
#: first.
_DISABLED_FEATURES: tuple[str, ...] = (
    "Translate",
    "MediaRouter",
    "OptimizationHints",
    "InterestFeedContentSuggestions",
)

#: Collapsing site isolation cuts one renderer process per origin. Acceptable
#: only because this browser visits a single trusted site and is then destroyed.
_CONTAINER_DISABLED_FEATURES: tuple[str, ...] = ("site-per-process", "IsolateOrigins")


def _env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _extra_args_from_env() -> list[str]:
    """Chrome switches from ``PICKLETIME_CHROME_ARGS`` (space or comma separated).

    A debugging aid, not configuration: anything that turns out to be needed
    permanently belongs in the tuples above, where it is reviewed and tested.
    Entries that are not switches are dropped rather than passed on, since a
    stray word becomes a URL for Chrome to open.
    """
    raw = os.environ.get("PICKLETIME_CHROME_ARGS", "")
    parts = [part.strip() for chunk in raw.split(",") for part in chunk.split()]
    kept = [part for part in parts if part.startswith("--")]
    for part in parts:
        if part and not part.startswith("--"):
            logger.warning(f"Ignoring PICKLETIME_CHROME_ARGS entry {part!r}: not a switch")
    return kept


def in_container() -> bool:
    """True on Cloud Run, or when explicitly forced."""
    return bool(
        os.environ.get("K_SERVICE")
        or os.environ.get("CLOUD_RUN_JOB")
        or os.environ.get("PICKLETIME_CONTAINER")
    )


_HEADLESS_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


class BrowserManager:
    """Starts, hands out, and disposes of a Chrome session."""

    def __init__(self, settings: BookingSettings) -> None:
        self.settings = settings
        self.driver: webdriver.Chrome | None = None
        self.waiter: Waiter | None = None
        self._profile_dir: str | None = None

    # -- options ----------------------------------------------------------
    def build_options(self) -> Options:
        options = Options()
        verbose = _env_flag("PICKLETIME_CHROMEDRIVER_VERBOSE")
        for arg in _BASE_ARGS:
            # --log-level=3 silences Chrome's own stderr, which is where a dying
            # renderer says why. Keep it off while diagnosing.
            if verbose and arg == "--log-level=3":
                continue
            options.add_argument(arg)
        if verbose:
            options.add_argument("--enable-logging=stderr")
            options.add_argument("--v=1")
        options.add_argument(f"--window-size={self.settings.window_size}")

        features = list(_DISABLED_FEATURES)
        if in_container() and not _env_flag("PICKLETIME_DISABLE_CONTAINER_ARGS"):
            logger.info("Container detected — applying low-memory Chrome switches")
            for arg in _CONTAINER_ARGS:
                options.add_argument(arg)
            features.extend(_CONTAINER_DISABLED_FEATURES)
        options.add_argument(f"--disable-features={','.join(features)}")

        # Bisecting a browser-level failure otherwise means a rebuild and a
        # redeploy per guess; this makes it `gcloud run services update
        # --update-env-vars`, which is seconds.
        for arg in _extra_args_from_env():
            logger.info(f"Adding Chrome switch from the environment: {arg}")
            options.add_argument(arg)

        if self.settings.headless:
            # Only "new" exists. The old headless shell — a lighter binary with
            # no toolbar, New Tab Page or signin machinery, and an obvious thing
            # to reach for when that machinery is what crashed — was removed in
            # Chrome 132. Chrome 141+ rejects `--headless=old` and refuses to
            # start, reporting "probably user data directory is already in use",
            # which sends you somewhere else entirely. Verified against 141.
            options.add_argument("--headless=new")
            options.add_argument(f"--user-agent={_HEADLESS_UA}")

        if self.settings.chrome_binary:
            options.binary_location = self.settings.chrome_binary

        # Isolated, disposable profile — never share one between runs.
        self._profile_dir = tempfile.mkdtemp(prefix="pickletime-profile-")
        options.add_argument(f"--user-data-dir={self._profile_dir}")

        # 'eager' returns control at DOMContentLoaded instead of waiting on every
        # subresource. The slot grid is rendered by then; the trailing analytics
        # beacons are not worth waiting for.
        options.page_load_strategy = "eager"

        content_settings: dict[str, int] = {
            "plugins": 2,
            "popups": 2,
            "geolocation": 2,
            "notifications": 2,
            "media_stream": 2,
        }
        if self.settings.block_images:
            content_settings["images"] = 2
        options.add_experimental_option(
            "prefs",
            {
                "profile.default_content_setting_values": content_settings,
                "profile.managed_default_content_settings": (
                    {"images": 2} if self.settings.block_images else {}
                ),
                "credentials_enable_service": False,
                "profile.password_manager_enabled": False,
                # Start on a blank page. With a fresh --user-data-dir (which we
                # always use, for isolation) Chrome opens the New Tab Page
                # instead of chromedriver's usual `data:,`, and the NTP drags in
                # the WebUI toolbar, the NTP modules and Google account
                # reconciliation — a live call to accounts.google.com — before
                # the bot has asked for anything. On Cloud Run a renderer dies
                # partway through that, half a second after chromedriver
                # connects, having never reached CourtReserve.
                #
                # 4 is SessionStartupPref::kURLs. Only the preference works:
                # --homepage and --no-first-run do not change the startup page,
                # and a bare "about:blank" in the argument list cannot work at
                # all, because chromedriver prefixes "--" to every argument and
                # Chrome then discards "--about:blank" as an unknown switch.
                # All four were measured against Chrome 141, not assumed.
                "session.restore_on_startup": 4,
                "session.startup_urls": ["about:blank"],
            },
        )
        options.add_experimental_option("excludeSwitches", ["enable-automation"])
        return options

    # -- lifecycle --------------------------------------------------------
    def start(self, max_attempts: int = 3) -> tuple[webdriver.Chrome, Waiter]:
        last_error: Exception | None = None

        for attempt in range(1, max_attempts + 1):
            try:
                logger.info(f"Starting Chrome (attempt {attempt}/{max_attempts})")
                options = self.build_options()
                service = self._build_service()

                started = time.monotonic()
                self.driver = webdriver.Chrome(service=service, options=options)

                # Explicit polling only — see the module docstring.
                self.driver.implicitly_wait(0)
                self.driver.set_page_load_timeout(self.settings.page_load_timeout)
                self.driver.set_script_timeout(self.settings.webdriver_timeout)

                self.waiter = Waiter(
                    self.driver,
                    default_timeout=self.settings.webdriver_timeout,
                    poll_interval=self.settings.poll_interval_ms / 1000.0,
                )
                logger.info(f"Chrome ready in {time.monotonic() - started:.2f}s")
                return self.driver, self.waiter

            except WebDriverException as exc:
                last_error = exc
                logger.warning(
                    f"Chrome start attempt {attempt} failed: {str(exc).splitlines()[0]}"
                )
                log_renderer_crash_hint(exc)
                self.close()
                if attempt < max_attempts:
                    backoff = 2.0 * attempt
                    logger.info(f"Retrying Chrome start in {backoff:.0f}s")
                    time.sleep(backoff)
            except Exception as exc:  # noqa: BLE001 - surface as a typed error
                self.close()
                raise BrowserStartError(f"Unexpected error starting Chrome: {exc}") from exc

        raise BrowserStartError(
            f"Chrome failed to start after {max_attempts} attempts: {last_error}"
        ) from last_error

    def _build_service(self) -> Service:
        """Use the configured chromedriver, else let Selenium Manager resolve one."""
        path = self.settings.chrome_driver_path
        if path and not Path(path).is_file():
            logger.warning(
                f"chrome_driver_path {path!r} does not exist; "
                "falling back to Selenium Manager"
            )
            path = None
        # Chromedriver's own log goes to the void unless someone asks for it;
        # writing it into the image's working directory (the old behaviour) just
        # consumed tmpfs on Cloud Run.
        service_args: list[str] = []
        log_output: str | int | None = None

        if _env_flag("PICKLETIME_CHROMEDRIVER_VERBOSE"):
            # The renderer's actual cause of death is in chromedriver's verbose
            # log and in Chrome's stderr, and nowhere else. Without this a
            # renderer crash reports only "Unable to receive message from
            # renderer", which is the same sentence for an OOM kill, a sandbox
            # kill, and a bad flag combination — so it identifies nothing and
            # every fix is a guess. Sent to stderr so Cloud Logging collects it.
            logger.warning("Verbose chromedriver logging is on — noisy, for diagnosis only")
            service_args.append("--verbose")
            log_output = subprocess.STDOUT
        elif self.settings.screenshot_dir:
            directory = Path(self.settings.screenshot_dir)
            directory.mkdir(parents=True, exist_ok=True)
            log_output = str(directory / "chromedriver.log")

        # log_output is omitted rather than passed as None: Selenium's own
        # default is None, but its signature does not admit it.
        extra: dict[str, Any] = {} if log_output is None else {"log_output": log_output}
        return Service(
            executable_path=path,
            service_args=service_args or None,
            **extra,
        )

    def close(self) -> None:
        if self.driver is not None:
            try:
                self.driver.quit()
                logger.debug("Chrome closed")
            except Exception as exc:  # noqa: BLE001 - shutdown must not raise
                logger.warning(f"Error closing Chrome: {type(exc).__name__}: {exc}")
            finally:
                self.driver = None
                self.waiter = None

        if self._profile_dir:
            shutil.rmtree(self._profile_dir, ignore_errors=True)
            self._profile_dir = None

    # -- context manager --------------------------------------------------
    def __enter__(self) -> tuple[webdriver.Chrome, Waiter]:
        return self.start()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()


def log_renderer_crash_hint(exc: BaseException) -> None:
    """Name the two causes of a dead renderer, and say which one this looks like.

    They produce an identical message, so an earlier version of this hint blamed
    memory unconditionally and sent the investigation to the wrong place — the
    real fault was the sandbox, and raising ``--memory`` did nothing twice.
    """
    text = str(exc)
    if "renderer" not in text and "invalid session id" not in text:
        return

    limit = _cgroup_memory_limit()
    logger.error(
        f"Chrome's renderer died. Memory ceiling here: {limit or 'unknown'}. "
        "Two causes produce this exact message:\n"
        "  1. The sandbox. Cloud Run's default first-generation environment runs "
        "under gVisor, which does not implement every syscall Chrome's renderer "
        "makes. Deploy with --execution-environment=gen2. This is insensitive to "
        "--memory, so suspect it first when raising memory changed nothing.\n"
        "  2. Actual memory. Raise --memory (2Gi is marginal for Chrome, 4Gi is "
        "comfortable) and check /dev/shm is not tiny.\n"
        "Set PICKLETIME_DISABLE_CONTAINER_ARGS=1 to rule out the low-memory "
        "Chrome switches without rebuilding the image."
    )


def _cgroup_memory_limit() -> str | None:
    """The container's memory ceiling, if the kernel exposes one."""
    for path in (
        "/sys/fs/cgroup/memory.max",
        "/sys/fs/cgroup/memory/memory.limit_in_bytes",
    ):
        try:
            raw = Path(path).read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if raw in {"max", ""}:
            return "unlimited"
        try:
            return f"{int(raw) / (1024 ** 3):.2f} GiB"
        except ValueError:
            return raw
    return None


def chrome_version(binary: str | None = None) -> str | None:
    """Installed Chrome version, for the ``/version`` endpoint. Never raises."""
    candidates = [binary] if binary else ["google-chrome", "chromium", "chromium-browser"]
    for candidate in candidates:
        if candidate is None:
            continue
        resolved = shutil.which(candidate) or (candidate if Path(candidate).is_file() else None)
        if not resolved:
            continue
        try:
            output = subprocess.run(  # noqa: S603 - fixed argv, no shell
                [resolved, "--version"],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            if output.returncode == 0 and output.stdout.strip():
                return output.stdout.strip()
        except (OSError, subprocess.SubprocessError):
            continue
    return None
