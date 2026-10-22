"""``GET /api/health`` — static shape matching the trimmed
``stationConfig.js``'s ``loadStationConfig()`` expectations (design.md
§4.3), with none of ToolBox's real multi-station logic behind it.
"""
from __future__ import annotations

from flask import Blueprint, jsonify

health_bp = Blueprint("health", __name__)


@health_bp.get("/health")
def get_health():
    return jsonify(
        {
            "status": "ok",
            "profile": "",
            "station_codes": [],
            "station_code": "",
            "station_country": "ES",
        }
    )
