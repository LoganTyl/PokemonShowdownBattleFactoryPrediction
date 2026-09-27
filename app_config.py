"""Application configuration loaded from a local .env file.

Copy .env.example to .env and replace the placeholder values before running
database-backed commands. The real .env file is intentionally ignored by Git.
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parent
load_dotenv(PROJECT_ROOT / ".env")


def get_connection_string() -> str:
    """Return PS_CONN_STR or build one from the individual DB_* variables."""
    explicit = os.getenv("PS_CONN_STR", "").strip()
    if explicit:
        return explicit

    driver = os.getenv("DB_DRIVER", "ODBC Driver 17 for SQL Server").strip()
    server = os.getenv("DB_SERVER", "").strip()
    database = os.getenv("DB_DATABASE", "").strip()
    trusted = os.getenv("DB_TRUSTED_CONNECTION", "yes").strip()
    user = os.getenv("DB_USER", "").strip()
    password = os.getenv("DB_PASSWORD", "").strip()

    missing = [
        name
        for name, value in (("DB_SERVER", server), ("DB_DATABASE", database))
        if not value
    ]
    if missing:
        raise RuntimeError(
            "Missing database configuration: "
            + ", ".join(missing)
            + ". Copy .env.example to .env and fill in your local values."
        )

    parts = [f"DRIVER={{{driver.strip('{}')}}}", f"SERVER={server}", f"DATABASE={database}"]
    if trusted.lower() in {"yes", "true", "1"}:
        parts.append("Trusted_Connection=yes")
    else:
        if not user or not password:
            raise RuntimeError(
                "DB_USER and DB_PASSWORD are required when DB_TRUSTED_CONNECTION is disabled."
            )
        parts.extend((f"UID={user}", f"PWD={password}"))

    return ";".join(parts) + ";"


CONN_STR = get_connection_string()
