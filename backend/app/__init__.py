from __future__ import annotations

import sys

# Vendored integration files are copied byte-for-byte from ToolBox (see
# VENDORING.md / design.md §1.4) and therefore still say
# `from app.integrations.X import Y` internally, exactly as they do in
# ToolBox itself. Studio stores that same code under `vendor.integrations`
# (see design.md §1.6). Rather than hand-edit 74 copied files (which would
# violate the "copy, don't rewrite" vendoring rule) or hand-write 74
# one-line shim modules, alias the package once: `app.integrations` and
# `vendor.integrations` become the exact same module object, so every
# `from app.integrations.<name> import ...` the vendored siblings use
# resolves without any further per-file shim.
#
# The `vendor/` tree is added in a SEPARATE git commit from this scaffold
# (design.md §8.2: commit 1 = scaffold, commit 2 = vendoring), so this
# module must still import and let the app boot when `vendor/` is absent.
# The `try/except ImportError` makes that ordering real: with no vendor
# tree, the alias and the trust-store install are skipped and the app
# still starts (only the vendor-reaching routes — all of which import
# their vendor dependencies lazily, inside the view function — would fail
# if actually called). Once commit 2 lands, the import succeeds and
# behaves exactly as in ToolBox.
try:
    import vendor.integrations as _vendor_integrations  # noqa: E402

    sys.modules["app.integrations"] = _vendor_integrations

    # Mirrors ToolBox's own backend/app/__init__.py: install the OS trust
    # store BEFORE anything else imports `requests`, so calls from the
    # vendored integration clients to internal Amazon hosts succeed
    # through a corporate TLS-intercepting proxy. Safe to call even if the
    # optional `truststore` package isn't installed (degrades to a logged
    # warning).
    from vendor.integrations._ssl_truststore import install_os_trust_store

    install_os_trust_store()
except ImportError:
    # Vendor tree not present yet (scaffold-only checkout / commit 1).
    pass

from flask import Flask
from flask_cors import CORS


def create_app() -> Flask:
    app = Flask(__name__)
    CORS(app)

    from app.api.health_routes import health_bp
    from app.api.projects_routes import projects_bp
    from app.api.submit_routes import submit_bp
    from app.api.connections_routes import connections_bp
    from app.api.manifest_routes import manifest_bp
    from app.api.config_routes import config_bp

    app.register_blueprint(health_bp, url_prefix="/api")
    app.register_blueprint(projects_bp, url_prefix="/api/projects")
    app.register_blueprint(submit_bp, url_prefix="/api/projects")
    app.register_blueprint(connections_bp, url_prefix="/api/connections")
    app.register_blueprint(manifest_bp, url_prefix="/api/manifest")
    app.register_blueprint(config_bp, url_prefix="/api/config")

    from app.projects.registry import register_all_project_blueprints

    register_all_project_blueprints(app)  # dynamically imports every
    # app/projects/<name>/routes.py that exists on disk and registers
    # its blueprint under /api/projects/<name>/app.
    return app
