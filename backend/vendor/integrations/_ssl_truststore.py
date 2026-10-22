"""Inject the OS certificate store into Python's SSL context.

Amazon's corporate proxy performs TLS interception using a private CA
that is preinstalled in the OS certificate store (Windows / macOS
Keychain / Linux ca-certificates). Firefox honours that store, but
``requests`` ships its own certifi bundle and does not, so calls to
internal hosts (mercury.amazon.com, harmony, honeypot, etc.) fail with::

    SSLError(SSLCertVerificationError(1, '[SSL:
    CERTIFICATE_VERIFY_FAILED] self-signed certificate in certificate
    chain'))

The :pypi:`truststore` package wires the standard ``ssl`` module to use
the OS store, which fixes every downstream ``requests`` / ``urllib3``
call without resorting to ``verify=False``.

The injection is idempotent and safe to call multiple times; we only
log the outcome on first success.
"""
from __future__ import annotations

import logging
import os

_INJECTED = False
_LOG = logging.getLogger(__name__)


def install_os_trust_store() -> bool:
    """Make the OS certificate store the default for ``ssl`` and ``requests``.

    Returns True when the system store is now in effect (or already
    was). Failures are logged but never raised — the rest of the app
    keeps working with the default certifi bundle.
    """
    global _INJECTED
    if _INJECTED:
        return True
    if os.environ.get("TOOLBOX_DISABLE_TRUSTSTORE", "").strip() in ("1", "true", "yes"):
        _LOG.info("truststore disabled via TOOLBOX_DISABLE_TRUSTSTORE.")
        return False
    try:
        import truststore  # type: ignore[import-not-found]
    except ImportError:
        _LOG.warning(
            "truststore is not installed; corporate TLS interception may fail. "
            "Run `pip install truststore` to fix it."
        )
        return False
    try:
        truststore.inject_into_ssl()
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("Could not inject the OS trust store into ssl: %s", exc)
        return False
    _INJECTED = True
    _LOG.info("OS trust store injected (ssl + requests + urllib3).")
    return True
