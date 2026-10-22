"""Firefox / Selenium helpers used by DINO (Harmony + Honeypot SSO login).

The legacy DINO.py opened Firefox through Selenium so the user could complete
their SSO login in a real browser. We keep the same UX in the backend:

* ``harmony_login()`` opens ``HARMONY_LOGIN`` and waits for the
  ``amzn_sso_token`` cookie.
* ``honeypot_login()`` opens the Honeypot POD search and waits for
  ``amzn_sso_token`` / ``amzn_sso_rfp`` cookies.

Both return a ``dict[str, str]`` with the captured cookies. They raise
``RuntimeError`` with a readable message if Firefox or geckodriver are
missing.

These functions are blocking: they spawn a Firefox window on the server
machine. The Flask server is designed to run locally alongside the Vue
frontend (same laptop), so this matches the legacy behaviour.
"""
from __future__ import annotations

import os
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any, Iterable

try:  # pragma: no cover - optional import
    import winreg  # type: ignore
except ImportError:  # pragma: no cover
    winreg = None  # type: ignore[assignment]


HARMONY_LOGIN_URL = "https://console.harmony.a2z.com/customer-calling-details/"
HONEYPOT_BASE_URL = "https://honeypot.amazon.dev/"

LOGIN_WAIT_SECONDS = 240
HARMONY_WAIT_SECONDS = 180


def honeypot_url_for_tid(tid: str) -> str:
    return (
        "https://honeypot.amazon.dev/"
        f"?id={tid or 'TEST'}"
        "&searchType=TRACKING_ID"
        "&imageType=PHOTO_ON_DELIVERY"
        "&region=eu-west-1"
        "&tab=pod-search"
    )


def resolve_firefox_binary() -> str | None:
    local_appdata = os.environ.get("LOCALAPPDATA", "")

    def _is_local_appdata(path: str) -> bool:
        if not path or not local_appdata:
            return False
        try:
            return os.path.commonpath([path, local_appdata]) == local_appdata
        except ValueError:
            return False

    def _allowed_local_appdata() -> bool:
        return os.getenv("ALLOW_LOCALAPPDATA_FIREFOX", "").strip() in {"1", "true", "True"}

    env_path = os.getenv("FIREFOX_BIN", "").strip()
    if env_path and os.path.exists(env_path):
        if _is_local_appdata(env_path) and not _allowed_local_appdata():
            return None
        return env_path

    which = shutil.which("firefox")
    if which:
        if _is_local_appdata(which) and not _allowed_local_appdata():
            which = None
        else:
            return which

    if winreg is not None:
        reg_paths = [
            r"SOFTWARE\Mozilla\Mozilla Firefox",
            r"SOFTWARE\WOW6432Node\Mozilla\Mozilla Firefox",
        ]
        reg_roots = [winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER]
        for reg_path in reg_paths:
            for reg_root in reg_roots:
                try:
                    with winreg.OpenKey(reg_root, reg_path) as key:
                        current, _ = winreg.QueryValueEx(key, "CurrentVersion")
                        with winreg.OpenKey(reg_root, rf"{reg_path}\{current}\Main") as sub:
                            exe, _ = winreg.QueryValueEx(sub, "PathToExe")
                            if os.path.exists(exe):
                                if _is_local_appdata(exe) and not _allowed_local_appdata():
                                    continue
                                return exe
                except OSError:
                    continue

    program_files = os.environ.get("ProgramFiles", r"C:\Program Files")
    program_files_x86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    for candidate in (
        os.path.join(program_files, "Mozilla Firefox", "firefox.exe"),
        os.path.join(program_files_x86, "Mozilla Firefox", "firefox.exe"),
    ):
        if candidate and os.path.exists(candidate):
            return candidate

    if _allowed_local_appdata():
        candidate = os.path.join(local_appdata, "Mozilla Firefox", "firefox.exe")
        if os.path.exists(candidate):
            return candidate

    return None


def resolve_geckodriver() -> str | None:
    env_path = os.getenv("GECKODRIVER_BIN", "").strip()
    if env_path and os.path.exists(env_path):
        return env_path

    which = shutil.which("geckodriver.exe" if os.name == "nt" else "geckodriver")
    if which:
        return which

    name = "geckodriver.exe" if os.name == "nt" else "geckodriver"
    candidates = [
        Path(__file__).resolve().parents[2] / name,
        Path(__file__).resolve().parents[3] / name,
        Path(__file__).resolve().parents[3] / "legacy" / "original_scripts" / name,
    ]
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    return None


def _build_firefox_options(headless: bool, profile_dir: Path | None):
    from selenium.webdriver.firefox.options import Options

    opts = Options()
    if headless:
        opts.add_argument("-headless")
    if profile_dir is not None:
        opts.add_argument("-profile")
        opts.add_argument(str(profile_dir))
    firefox_bin = resolve_firefox_binary()
    if firefox_bin:
        opts.binary_location = firefox_bin
    return opts


def _make_firefox(headless: bool = False, profile_dir: Path | None = None):
    try:
        from selenium import webdriver
        from selenium.webdriver.firefox.service import Service
    except ImportError as exc:  # pragma: no cover - dependency missing
        raise RuntimeError(
            "Selenium no esta instalado. Ejecuta pip install selenium para habilitar DINO."
        ) from exc

    if resolve_firefox_binary() is None:
        raise RuntimeError(
            "Firefox no encontrado. Instala Mozilla Firefox o define FIREFOX_BIN."
        )

    gecko = resolve_geckodriver()
    if gecko is None:
        raise RuntimeError(
            "geckodriver no encontrado. Colocalo en el PATH o define GECKODRIVER_BIN."
        )

    if profile_dir is not None:
        profile_dir.mkdir(parents=True, exist_ok=True)

    return webdriver.Firefox(
        service=Service(gecko),
        options=_build_firefox_options(headless=headless, profile_dir=profile_dir),
    )


def _cookies_to_dict(cookies: Iterable[dict[str, Any]]) -> dict[str, str]:
    out: dict[str, str] = {}
    for item in cookies or []:
        name = item.get("name")
        value = item.get("value")
        if name and value is not None:
            out[str(name)] = str(value)
    return out


def harmony_login(wait_seconds: int = HARMONY_WAIT_SECONDS) -> dict[str, str]:
    """Open Harmony SSO page and wait for the ``amzn_sso_token`` cookie."""
    driver = _make_firefox(headless=False)
    try:
        driver.get(HARMONY_LOGIN_URL)
        deadline = time.time() + max(30, wait_seconds)
        last: dict[str, str] = {}
        while time.time() < deadline:
            try:
                sso = driver.get_cookie("amzn_sso_token")
            except Exception:
                sso = None
            cookies = _cookies_to_dict(driver.get_cookies() or [])
            if cookies:
                last = cookies
            if sso and cookies.get("amzn_sso_token"):
                return cookies
            time.sleep(1)
        if last.get("amzn_sso_token"):
            return last
        raise RuntimeError("No se detecto cookie amzn_sso_token en Harmony.")
    finally:
        try:
            driver.quit()
        except Exception:
            pass


def honeypot_login(
    sample_tid: str | None = None,
    wait_seconds: int = LOGIN_WAIT_SECONDS,
) -> dict[str, str]:
    """Open Honeypot POD search page and wait for SSO cookies."""
    profile_dir = Path(tempfile.mkdtemp(prefix="ff_profile_pod_"))
    driver = _make_firefox(headless=False, profile_dir=profile_dir)
    try:
        driver.get(honeypot_url_for_tid(sample_tid or "TEST"))
        deadline = time.time() + max(30, wait_seconds)
        while time.time() < deadline:
            cookies = _cookies_to_dict(driver.get_cookies() or [])
            if "amzn_sso_token" in cookies or "amzn_sso_rfp" in cookies:
                return cookies
            time.sleep(2)
        raise RuntimeError("No se detectaron cookies SSO POD (Honeypot).")
    finally:
        try:
            driver.quit()
        except Exception:
            pass
        shutil.rmtree(profile_dir, ignore_errors=True)
