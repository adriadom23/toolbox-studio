from __future__ import annotations

import sys

try:
    import vendor.integrations as _vendor_integrations

    sys.modules["app.integrations"] = _vendor_integrations
    from vendor.integrations._ssl_truststore import install_os_trust_store

    install_os_trust_store()
except ImportError:
    pass


def create_app():
    """Compatibility factory returning only the authorized Flask bridge."""
    from app.wsgi_bridge import create_wsgi_bridge

    return create_wsgi_bridge()
