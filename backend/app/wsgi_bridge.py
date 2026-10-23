from __future__ import annotations

from flask import Flask, jsonify, request


def create_wsgi_bridge() -> Flask:
    app = Flask(__name__)

    from app.api.browser_routes import browser_bp
    from app.api.connections_routes import connections_bp
    from app.api.manifest_routes import manifest_bp

    app.register_blueprint(connections_bp, url_prefix="/api/connections")
    app.register_blueprint(manifest_bp, url_prefix="/api/manifest")
    app.register_blueprint(browser_bp, url_prefix="/api/browser")

    @app.before_request
    def require_bridge_authorization():
        identity = request.environ.get("HTTP_X_STUDIO_AUTHORIZED_IDENTITY")
        if not identity:
            return jsonify({"error": "unauthorized", "detail": "ASGI authorization required"}), 401
        return None

    return app
