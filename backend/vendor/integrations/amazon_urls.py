from __future__ import annotations


def fmc_execution_url(station_code: str) -> str:
    return f"https://trans-logistics-eu.amazon.com/fmc/execution/V3Tkz--{station_code}_total"
