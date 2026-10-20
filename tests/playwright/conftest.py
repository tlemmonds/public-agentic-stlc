import json
import os
import time
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright

# LambdaTest browser map: browser key → (Playwright launcher attr, LT browserName capability)
# LambdaTest now requires playwright.browser.connect() (Playwright wire protocol), NOT
# connect_over_cdp() which produces "Browser.getVersion: undefined" errors.
# With connect(), use standard LambdaTest browser names (Chrome, Firefox, etc.).
# Android routes to a real LambdaTest Android device; the HE runner stays Linux.
_BROWSER_MAP = {
    "chrome":   ("chromium", "Chrome"),
    "firefox":  ("firefox",  "pw-firefox"),   # the grid names it pw-firefox (400 for "Firefox")
    "edge":     ("chromium", "MicrosoftEdge"),
    "safari":   ("webkit",   "Safari"),
    "android":  ("chromium", "Chrome"),   # real Galaxy device (see _ANDROID_DEVICES)
    "pixel":    ("chromium", "Chrome"),   # real Pixel device
}

# Platform overrides per browser key (defaults to "Windows 10")
_PLATFORM_MAP = {
    "safari":  "macOS Ventura",
    "android": "Android",
}

# Real Android devices (NFR-11 device families), each with its default browser
# (Chrome). Override with LT_<KEY>_DEVICE / LT_<KEY>_OS, e.g. LT_ANDROID_DEVICE.
# Names and versions come from the LambdaTest real-device list.
_ANDROID_DEVICES = {
    "android": ("Galaxy S26", "16"),   # Samsung Galaxy S-series flagship
    "pixel":   ("Pixel 11", "17"),     # Google Pixel flagship
}


def _android_device(key: str) -> tuple[str, str]:
    name, version = _ANDROID_DEVICES[key]
    return (os.environ.get(f"LT_{key.upper()}_DEVICE", name),
            os.environ.get(f"LT_{key.upper()}_OS", version))


def _playwright_version() -> str:
    try:
        from importlib.metadata import version
        return version("playwright")
    except Exception:  # noqa: BLE001
        return ""


def _build_name():
    run_number = os.environ.get("GITHUB_RUN_NUMBER", "")
    # hyperexecute.yaml passes GITHUB_RUN_NUMBER: ${GITHUB_RUN_NUMBER}; on a local
    # submit the variable is unset and the VM receives the literal placeholder.
    if run_number.startswith("${"):
        run_number = ""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return f"Agentic STLC #{run_number} | {today}" if run_number else f"Agentic STLC | {today}"


def _session_name(request, browser_key: str) -> str:
    scenario_marker = request.node.get_closest_marker("scenario")
    scenario_id = scenario_marker.args[0] if scenario_marker else "unknown"
    tc_id = f"TC-{scenario_id.split('-')[1]}" if "-" in scenario_id else "TC-000"
    return f"{scenario_id} | {tc_id} | {request.node.name} | {browser_key}"


def pytest_configure(config):
    config.addinivalue_line("markers", "scenario(id): scenario ID this test covers")
    config.addinivalue_line("markers", "requirement(id): requirement ID this test covers")


# Read target browsers from env — comma-separated, e.g. "chrome,firefox,android,pixel".
# Falls back to single BROWSER env var (legacy), then to chrome.
def _target_browsers() -> list[str]:
    multi = os.environ.get("BROWSERS", "")
    if multi:
        return [b.strip().lower() for b in multi.split(",") if b.strip()]
    single = os.environ.get("BROWSER", "chrome").strip().lower()
    return [single]


@pytest.fixture(params=_target_browsers(), scope="function")
def page(request):
    lt_username = os.environ.get("LT_USERNAME", "")
    lt_access_key = os.environ.get("LT_ACCESS_KEY", "")
    browser_key: str = request.param
    playwright_launcher, lt_browser_name = _BROWSER_MAP.get(browser_key, ("chromium", "pw-chromium"))

    build = _build_name()
    session_name = _session_name(request, browser_key)

    platform = _PLATFORM_MAP.get(browser_key, "Windows 10")
    lt_options: dict = {
        "platform": platform,
        "build": build,
        "name": session_name,
        "project": "Agentic STLC",
        "user": lt_username,
        "accessKey": lt_access_key,
        "video": True,
        "visual": True,
        "network": True,
        "console": True,
        # Pin the grid's Playwright server to this client's version. Without it a
        # mismatched client fails on connect (KeyError: 'options', HE job #28171).
        "playwrightClientVersion": _playwright_version(),
    }
    is_device = browser_key in _ANDROID_DEVICES
    if is_device:
        # Real devices take a smaller capability set ("capability not supported
        # for real devices" for platform, visual, browserVersion, …).
        device_name, platform_version = _android_device(browser_key)
        lt_options = {
            "platformName": "android",
            "deviceName": device_name,
            "platformVersion": platform_version,
            "isRealMobile": True,
            "build": build,
            "name": session_name,
            "projectName": "Agentic STLC",
            "user": lt_username,
            "accessKey": lt_access_key,
            "video": True,
            "console": True,
            "network": False,
        }
        capabilities = {"LT:Options": lt_options}
    else:
        capabilities = {
            "browserName": lt_browser_name,
            "browserVersion": "latest",
            "LT:Options": lt_options,
        }
    cdp_url = (
        f"wss://cdp.lambdatest.com/playwright"
        f"?capabilities={urllib.parse.quote(json.dumps(capabilities))}"
    )

    start_time = datetime.now(timezone.utc)
    start_mono = time.monotonic()

    with sync_playwright() as p:
        launcher = getattr(p, playwright_launcher)
        # LambdaTest requires Playwright wire-protocol connect(), not connect_over_cdp().
        browser = launcher.connect(cdp_url, timeout=180_000)
        if is_device:
            # Real Android device: the device's own Chrome, with its own screen
            # size and touch input (no desktop viewport).
            context = browser.contexts[0] if browser.contexts else browser.new_context()
        else:
            context = browser.new_context(viewport={"width": 1366, "height": 900})  # pw-firefox on the grid reports a 0-size viewport otherwise
        pw_page = context.new_page()

        yield pw_page

        end_mono = time.monotonic()
        end_time = datetime.now(timezone.utc)
        duration_ms = round((end_mono - start_mono) * 1000)

        scenario_marker = request.node.get_closest_marker("scenario")
        requirement_marker = request.node.get_closest_marker("requirement")
        scenario_id = scenario_marker.args[0] if scenario_marker else "unknown"
        requirement_id = requirement_marker.args[0] if requirement_marker else "unknown"

        rep = getattr(request.node, "rep_call", None)
        if rep is None:
            status = "unknown"
        elif rep.passed:
            status = "passed"
        elif rep.skipped:
            status = "skipped"
        else:
            status = "failed"

        error_message = None
        if rep and rep.failed and hasattr(rep, "longrepr"):
            error_message = str(rep.longrepr)[:500]

        try:
            pw_page.evaluate(f"() => {{ window['lambda-status'] = '{status}'; }}")
        except Exception:
            pass

        tc_id = f"TC-{scenario_id.split('-')[1]}" if "-" in scenario_id else "TC-000"

        Path("reports").mkdir(exist_ok=True)
        # Include browser in filename so multi-browser runs don't overwrite each other
        result_path = Path(f"reports/kane_result_{scenario_id}_{browser_key}.json")
        result_path.write_text(
            json.dumps(
                {
                    "requirement_id": requirement_id,
                    "scenario_id": scenario_id,
                    "test_case_id": tc_id,
                    "function_name": request.node.name,
                    "browser": browser_key,
                    "lt_browser": lt_browser_name,
                    "status": status,
                    "start_time": start_time.isoformat(),
                    "end_time": end_time.isoformat(),
                    "duration_ms": duration_ms,
                    "error_message": error_message,
                    "session_name": session_name,
                    "build": build,
                    "source": "conftest",
                },
                indent=2,
            ),
            encoding="utf-8",
        )

        # On a real device the context is the device browser's own default
        # context; closing it ends the browser, so close only what's still open.
        for close in ((pw_page.close, browser.close) if is_device
                      else (pw_page.close, context.close, browser.close)):
            try:
                close()
            except Exception:  # noqa: BLE001 — already closed by the grid
                pass


@pytest.hookimpl(tryfirst=True, hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    rep = outcome.get_result()
    setattr(item, "rep_" + rep.when, rep)
