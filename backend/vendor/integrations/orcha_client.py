"""Orcha integration: installation detection and command invocation.

Orcha is Amazon's internal AI desktop agent. The Daily Summary uses its
command binary to read the operator's Outlook and Slack through approved MCP
connectors. New Orcha desktop releases install the command binary under the
application's ``resources/agent`` directory and no longer advertise the old
``--headless`` switch, so invocation must follow the selected binary's real
``--help`` output instead of assuming one version's arguments.
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import socket
import subprocess
import time
from pathlib import Path

logger = logging.getLogger(__name__)

_LOG_LINE_RE = re.compile(r"^\s*\[[A-Za-z0-9_.\-]+\]")
_FLAG_RE = re.compile(r"(?<![\w-])(--[a-z][a-z0-9-]*)", re.IGNORECASE)

ORCHA_DOWNLOAD_URL = "https://orcha.beta.harmony.a2z.com/download"

_DEFAULT_AGENT = "orcha_default"
_TIMEOUT_SEC = int(os.environ.get("ORCHA_TIMEOUT_SEC", "480") or 480)
_HELP_TIMEOUT_SEC = 10
_CAPABILITY_CACHE_TTL_SEC = 300.0
_DAEMON_CONNECT_TIMEOUT_SEC = 0.35
_CAPABILITY_CACHE: dict[str, tuple[float, frozenset[str]]] = {}
_AGENT_CACHE: dict[str, tuple[float, str]] = {}


class OrchaProbeError(RuntimeError):
    """Raised when an Orcha capability/configuration probe cannot complete."""


def _local_app_data() -> Path | None:
    value = os.environ.get("LOCALAPPDATA", "").strip()
    return Path(value) if value else None


def _existing_candidates(candidates: list[Path]) -> list[Path]:
    seen: set[str] = set()
    found: list[Path] = []
    for candidate in candidates:
        key = str(candidate).lower()
        if key in seen:
            continue
        seen.add(key)
        if candidate.is_file():
            found.append(candidate)
    return found


def _modern_agent_candidates() -> list[Path]:
    local = _local_app_data()
    if not local:
        return []
    programs = local / "Programs"
    candidates = [
        programs / "Orcha" / "resources" / "agent" / "orcha.exe",
        programs / "@amznorcha" / "resources" / "agent" / "orcha.exe",
        programs / "@amznorcha" / "Orcha" / "resources" / "agent" / "orcha.exe",
    ]
    scoped = programs / "@amznorcha"
    if scoped.exists():
        candidates.extend(sorted(scoped.glob("*/resources/agent/orcha.exe"), reverse=True))
    return _existing_candidates(candidates)


def _legacy_agent_candidates() -> list[Path]:
    local = _local_app_data()
    if not local:
        return []
    base = local / "Toolbox" / "tools" / "orcha"
    if not base.exists():
        return []
    return _existing_candidates(
        sorted(base.glob("*/resources/agent/orcha.exe"), reverse=True)
    )


def _find_agent_exe() -> Path | None:
    """Locate the direct command binary without using the multiline-mangling shim."""
    candidates = _modern_agent_candidates() + _legacy_agent_candidates()
    return candidates[0] if candidates else None


def find_cli() -> str | None:
    """Locate a drivable Orcha command entrypoint, preferring a direct exe."""
    exe = _find_agent_exe()
    if exe:
        return str(exe)

    found = shutil.which("orcha")
    if found:
        return found

    local = _local_app_data()
    if local:
        base = local / "Toolbox" / "tools" / "orcha"
        candidates = _existing_candidates([base / "orcha.cmd", base / "orcha.exe"])
        if candidates:
            return str(candidates[0])
    return None


def find_desktop_app() -> str | None:
    """Locate the Orcha desktop launcher separately from its command binary."""
    local = _local_app_data()
    if not local:
        return None
    programs = local / "Programs"
    candidates = [
        programs / "Orcha" / "Orcha.exe",
        programs / "@amznorcha" / "Orcha.exe",
        programs / "@amznorcha" / "Orcha" / "Orcha.exe",
    ]
    scoped = programs / "@amznorcha"
    if scoped.exists():
        candidates.extend(sorted(scoped.glob("*/Orcha.exe"), reverse=True))
    found = _existing_candidates(candidates)
    return str(found[0]) if found else None


def open_desktop() -> None:
    """Launch the desktop app once. Callers must invoke this only after a gesture."""
    desktop = find_desktop_app()
    if not desktop:
        raise RuntimeError("Orcha desktop launcher not found")
    subprocess.Popen([desktop], close_fds=True)


def _config_dir() -> Path | None:
    home = os.environ.get("USERPROFILE") or os.path.expanduser("~")
    directory = Path(home) / ".orcha"
    return directory if directory.exists() else None


def _daemon_port() -> int | None:
    directory = _config_dir()
    marker = directory / "agent_port" if directory else None
    if not marker or not marker.is_file():
        return None
    try:
        port = int(marker.read_text(encoding="utf-8", errors="ignore").strip())
    except (OSError, TypeError, ValueError):
        return None
    return port if 1 <= port <= 65535 else None


def daemon_running() -> bool:
    """Return true only when the recorded localhost daemon port accepts TCP."""
    port = _daemon_port()
    if port is None:
        return False
    try:
        with socket.create_connection(
            ("127.0.0.1", port), timeout=_DAEMON_CONNECT_TIMEOUT_SEC
        ):
            return True
    except OSError:
        return False


def midway_active() -> bool:
    """Best-effort check of the operator's CLI Midway session."""
    home = os.environ.get("USERPROFILE") or os.path.expanduser("~")
    jar = Path(home) / ".midway" / "cookie"
    if not jar.exists():
        return False
    try:
        now = time.time()
        for line in jar.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip()
            if not line or (line.startswith("#") and not line.startswith("#HttpOnly_")):
                continue
            parts = line.split("\t")
            if len(parts) < 7:
                continue
            domain, name = parts[0].lower(), parts[5]
            if "midway" not in domain or name != "session":
                continue
            try:
                expiry = float(parts[4])
            except ValueError:
                continue
            if expiry > now + 60:
                return True
        return False
    except Exception:  # noqa: BLE001 - unknown format: do not block Orcha
        return True


def has_firefox_extension() -> bool:
    """Return true when an Orcha add-on is present in the Firefox profile."""
    try:
        from app.integrations.firefox_cookies import find_firefox_profile_dir

        profile = find_firefox_profile_dir()
    except Exception:  # noqa: BLE001
        profile = None
    if not profile:
        return False

    for fname in ("extensions.json", "addons.json"):
        meta = profile / fname
        if not meta.exists():
            continue
        try:
            blob = meta.read_text(encoding="utf-8", errors="ignore").lower()
        except Exception:  # noqa: BLE001
            continue
        if "orcha" in blob:
            return True

    ext_dir = profile / "extensions"
    if ext_dir.exists():
        try:
            return any("orcha" in item.name.lower() for item in ext_dir.iterdir())
        except Exception:  # noqa: BLE001
            pass
    return False


def _probe_output(cli: str, flag: str) -> str:
    try:
        result = subprocess.run(
            [cli, flag],
            capture_output=True,
            timeout=_HELP_TIMEOUT_SEC,
            env={**os.environ, "PYTHONUTF8": "1"},
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.info("orcha %s probe failed (%s)", flag, exc)
        raise OrchaProbeError(f"Orcha {flag} probe failed") from exc
    raw = (result.stdout or b"") + b"\n" + (result.stderr or b"")
    text = raw.decode("cp1252", errors="replace")
    if result.returncode != 0:
        logger.info("orcha %s probe exited with %s", flag, result.returncode)
        raise OrchaProbeError(f"Orcha {flag} probe exited with {result.returncode}")
    return text


def cli_capabilities(cli: str, *, refresh: bool = False) -> frozenset[str]:
    """Return advertised long options; failed probes are never cached."""
    now = time.monotonic()
    cached = _CAPABILITY_CACHE.get(cli)
    if not refresh and cached and now - cached[0] < _CAPABILITY_CACHE_TTL_SEC:
        return cached[1]
    text = _probe_output(cli, "--help")
    capabilities = frozenset(match.lower() for match in _FLAG_RE.findall(text))
    _CAPABILITY_CACHE[cli] = (now, capabilities)
    return capabilities


def cli_agents(
    cli: str, capabilities: frozenset[str], *, refresh: bool = False
) -> str | None:
    """Return the successful ``--list-agents`` output when supported."""
    if "--list-agents" not in capabilities:
        return None
    now = time.monotonic()
    cached = _AGENT_CACHE.get(cli)
    if not refresh and cached and now - cached[0] < _CAPABILITY_CACHE_TTL_SEC:
        return cached[1]
    output = _probe_output(cli, "--list-agents")
    _AGENT_CACHE[cli] = (now, output)
    return output


def _selected_agent(agent: str | None = None) -> str:
    return (agent or os.environ.get("ORCHA_AGENT") or _DEFAULT_AGENT).strip() or _DEFAULT_AGENT


def _agent_available(agent: str, advertised: str | None) -> bool:
    if advertised is None:
        return agent == _DEFAULT_AGENT
    token = re.compile(
        rf"(?<![A-Za-z0-9_.-]){re.escape(agent)}(?![A-Za-z0-9_.-])",
        re.IGNORECASE,
    )
    return bool(token.search(advertised))


def clear_cli_capability_cache() -> None:
    """Clear successful capability/configuration probe caches."""
    _CAPABILITY_CACHE.clear()
    _AGENT_CACHE.clear()


def detect(*, refresh: bool = False) -> dict:
    """Return a readiness snapshot of every Orcha surface we can verify."""
    cli = find_cli()
    desktop = find_desktop_app()
    capabilities = frozenset()
    capability_probe_failed = False
    agent_probe_failed = False
    selected_agent = _selected_agent()
    agent_available = False
    if cli:
        try:
            capabilities = cli_capabilities(cli, refresh=refresh)
        except OrchaProbeError:
            capability_probe_failed = True
        if not capability_probe_failed and {"--agent", "--prompt"}.issubset(capabilities):
            try:
                agents = cli_agents(cli, capabilities, refresh=refresh)
                agent_available = _agent_available(selected_agent, agents)
            except OrchaProbeError:
                agent_probe_failed = True
    return {
        "cli_path": cli,
        "cli_available": bool(cli),
        "desktop_path": desktop,
        "desktop_available": bool(desktop),
        "installed": bool(cli or desktop),
        "daemon_running": daemon_running(),
        "firefox_extension": has_firefox_extension(),
        "midway_active": midway_active(),
        "capability_probe_failed": capability_probe_failed,
        "agent_probe_failed": agent_probe_failed,
        "agent_available": agent_available,
        "agent": selected_agent,
        "command_supported": bool(
            cli and "--agent" in capabilities and "--prompt" in capabilities
        ),
        "supports_session_id": "--session-id" in capabilities,
        "supports_headless": "--headless" in capabilities,
    }


def available() -> bool:
    """Return true when a desktop or command installation exists."""
    info = detect()
    return bool(info["installed"])


def _clean_output(stdout: str) -> str:
    kept = [line for line in (stdout or "").splitlines() if not _LOG_LINE_RE.match(line)]
    return "\n".join(kept).strip()


def _sanitize_prompt(prompt: str) -> str:
    """Replace characters outside Latin-1, which the command cannot encode."""
    return "".join(ch if ord(ch) < 256 else " " for ch in prompt)


def _command_args(
    cli: str, prompt: str, agent: str, session_id: str | None = None
) -> list[str]:
    capabilities = cli_capabilities(cli)
    if "--agent" not in capabilities or "--prompt" not in capabilities:
        raise RuntimeError("Orcha command does not advertise prompt mode")
    args = [cli, "--agent", agent]
    if "--headless" in capabilities:
        args.append("--headless")
    if session_id and "--session-id" in capabilities:
        args.extend(["--session-id", session_id])
    args.extend(["--prompt", _sanitize_prompt(prompt)])
    return args


def run_headless(
    prompt: str, agent: str | None = None, session_id: str | None = None
) -> str:
    """Run one command-mode prompt and return only usable model output."""
    cli = find_cli()
    if not cli:
        raise RuntimeError("Orcha command not found")
    selected_agent = _selected_agent(agent)
    env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
    result = subprocess.run(
        _command_args(cli, prompt, selected_agent, session_id),
        capture_output=True,
        timeout=_TIMEOUT_SEC,
        env=env,
    )
    stdout = (result.stdout or b"").decode("cp1252", errors="replace")
    stderr_text = (result.stderr or b"").decode("cp1252", errors="replace")
    cleaned = _clean_output(stdout)
    if result.returncode != 0:
        detail = cleaned[:200] or stderr_text.strip()[:200]
        raise RuntimeError(f"orcha exited with {result.returncode}: {detail}")
    if cleaned:
        return cleaned
    raise RuntimeError(stderr_text.strip()[:200] or "orcha returned no output")
