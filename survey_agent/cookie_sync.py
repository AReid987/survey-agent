"""Chrome cookie sync utility.

Copies Google OAuth session cookies from the user's real Chrome profile
to the agent's Chrome profile. This allows the agent Chrome to have an
active Google session, fixing Prime Opinion and other Google OAuth logins.

Usage:
    from .cookie_sync import sync_google_cookies
    sync_google_cookies()
"""

from __future__ import annotations

import logging
import os
import sqlite3
import shutil
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)

# Agent Chrome profile (our dedicated instance) — resolve from project root
_THIS_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _THIS_DIR.parent  # survey_agent/ -> survey-agent/
DATA_DIR_ENV = os.environ.get("SURVEY_AGENT_DATA_DIR", str(_PROJECT_ROOT / "data"))

REAL_CHROME_PROFILE = Path.home() / "Library" / "Application Support" / "Google" / "Chrome" / "Default"
AGENT_CHROME_PROFILE = Path(DATA_DIR_ENV) / "chrome_agent" / "Default"

# Domains whose cookies we need to copy
TARGET_DOMAINS = [
    ".google.com",
    "accounts.google.com",
    "google.com",
    ".app.primeopinion.com",
    "app.primeopinion.com",
    ".primeopinion.com",
    "primeopinion.com",
]

# Cookie names that carry Google session state
GOOGLE_SESSION_COOKIES = [
    "GAUTH",       # Google +1 and auth
    "GAPS",        # Google Auth Profile Session
    "LSID",        # Google Apps Login
    "SID",         # Google Secure ID
    "SSID",        # Google Secure Session ID
    "APISID",      # Google API Session ID
    "SAPISID",     # Google Secure API Session ID
    "HSID",        # Google Hosted Service ID
    "OSID",        # Google One Service ID
    "oauth_token", # OAuth token
    "__Secure-1PSID",
    "__Secure-1PSIDTS",
    "__Secure-1PSIDCC",
    "__Secure-3PSID",
    "__Secure-3PSIDTS",
    "__Secure-3PSIDCC",
    "__Host-GAPS",
    "NID",         # Google preferences
    "SNID",        # Google secure preferences
]


def _read_cookies(db_path: Path, target_domains: list[str]) -> list[dict]:
    """Read cookies from a Chrome SQLite cookie database."""
    if not db_path.exists():
        logger.warning("Cookie database not found: %s", db_path)
        return []

    # Chrome locks the database; copy to temp to avoid locking issues
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        tmp_path = tmp.name

    try:
        shutil.copy2(db_path, tmp_path)
        conn = sqlite3.connect(tmp_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        # Build domain filter
        placeholders = ",".join("?" for _ in target_domains)
        cursor.execute(
            f"SELECT host_key, name, value, encrypted_value, path, "
            f"expires_utc, is_secure, is_httponly, has_expires, "
            f"is_persistent, priority, samesite "
            f"FROM cookies WHERE host_key IN ({placeholders})",
            target_domains,
        )
        rows = [dict(row) for row in cursor.fetchall()]

        # Also add any row with name matching Google session cookies
        name_placeholders = ",".join("?" for _ in GOOGLE_SESSION_COOKIES)
        cursor.execute(
            f"SELECT host_key, name, value, encrypted_value, path, "
            f"expires_utc, is_secure, is_httponly, has_expires, "
            f"is_persistent, priority, samesite "
            f"FROM cookies WHERE name IN ({name_placeholders})",
            GOOGLE_SESSION_COOKIES,
        )
        rows.extend([dict(row) for row in cursor.fetchall()])
        conn.close()

        # Deduplicate by (host_key, name)
        seen: set[tuple[str, str]] = set()
        unique_rows = []
        for row in rows:
            key = (row["host_key"], row["name"])
            if key not in seen:
                seen.add(key)
                unique_rows.append(row)

        logger.info("Read %d unique cookies from %s", len(unique_rows), db_path)
        return unique_rows

    except sqlite3.Error as e:
        logger.error("Failed to read cookies: %s", e)
        return []
    finally:
        Path(tmp_path).unlink(missing_ok=True)


def _write_cookies(db_path: Path, cookies: list[dict]) -> int:
    """Write cookies into a Chrome SQLite cookie database using dynamic schema detection."""
    if not cookies:
        return 0

    if not db_path.exists():
        logger.warning("Target cookie database not found: %s", db_path)
        return 0

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        tmp_path = tmp.name

    try:
        shutil.copy2(db_path, tmp_path)
        conn = sqlite3.connect(tmp_path)
        cursor = conn.cursor()

        # Detect target schema columns dynamically
        cursor.execute("PRAGMA table_info(cookies)")
        target_cols = {}
        for row in cursor.fetchall():
            name, col_type, not_null = row[1], row[2], row[3]
            target_cols[name] = {"type": col_type, "not_null": bool(not_null)}

        # All possible source cookie fields mapped to target column names
        FIELD_MAP = {
            "host_key": "host_key",
            "name": "name",
            "value": "value",
            "encrypted_value": "encrypted_value",
            "path": "path",
            "expires_utc": "expires_utc",
            "is_secure": "is_secure",
            "is_httponly": "is_httponly",
            "has_expires": "has_expires",
            "is_persistent": "is_persistent",
            "priority": "priority",
            "samesite": "samesite",
            "source_scheme": "source_scheme",
            "source_port": "source_port",
        }

        # Default values for columns not in FIELD_MAP
        DEFAULTS = {
            "creation_utc": 0,
            "top_frame_site_key": "",
            "last_access_utc": 0,
            "last_update_utc": 0,
            "source_type": 1,
            "has_cross_site_ancestor": 0,
            "is_same_party": 0,
        }

        # Build column list and placeholders from target schema
        col_names = []
        for col in target_cols:
            if col not in FIELD_MAP and col not in DEFAULTS:
                continue
            col_names.append(col)

        col_list = ", ".join(col_names)
        placeholders = ", ".join("?" for _ in col_names)

        written = 0
        for cookie in cookies:
            host_key = cookie["host_key"]
            name = cookie["name"]

            # Delete existing
            cursor.execute(
                "DELETE FROM cookies WHERE host_key = ? AND name = ?",
                (host_key, name),
            )

            # Build values matching col_names
            values = []
            for col in col_names:
                if col in FIELD_MAP:
                    source_field = FIELD_MAP[col]
                    val = cookie.get(source_field, "")
                    if source_field == "encrypted_value" and isinstance(val, bytes) and len(val) > 0:
                        val = sqlite3.Binary(val)
                    elif source_field == "encrypted_value" and isinstance(val, str):
                        val = sqlite3.Binary(val.encode()) if val else sqlite3.Binary(b"")
                    elif isinstance(val, str) and val == "":
                        val = val
                    values.append(val)
                elif col in DEFAULTS:
                    values.append(DEFAULTS[col])

            cursor.execute(
                f"INSERT INTO cookies ({col_list}) VALUES ({placeholders})",
                values,
            )
            written += 1

        conn.commit()
        conn.close()

        # Copy back to original location
        shutil.copy2(tmp_path, db_path)
        logger.info("Wrote %d cookies to %s", written, db_path)
        return written

    except sqlite3.Error as e:
        logger.error("Failed to write cookies: %s", e)
        return 0
    finally:
        Path(tmp_path).unlink(missing_ok=True)


def sync_google_cookies(real_profile: Path | None = None, agent_profile: Path | None = None) -> int:
    """Sync Google OAuth and Prime Opinion cookies from real Chrome to agent Chrome.

    Args:
        real_profile: Path to real Chrome's Default profile (auto-detected if None).
        agent_profile: Path to agent Chrome's Default profile (auto-detected if None).

    Returns:
        Number of cookies synced.
    """
    src = real_profile or REAL_CHROME_PROFILE
    dst = agent_profile or AGENT_CHROME_PROFILE

    src_cookies = src / "Cookies"
    dst_cookies = dst / "Cookies"

    # Ensure agent profile directory exists
    dst.mkdir(parents=True, exist_ok=True)

    # Read cookies from real Chrome
    cookies = _read_cookies(src_cookies, TARGET_DOMAINS)
    if not cookies:
        logger.warning("No relevant cookies found in real Chrome profile")
        return 0

    # Write them to agent Chrome
    written = _write_cookies(dst_cookies, cookies)

    # Also copy the encryption key ("Local State" file)
    src_state = src.parent / "Local State"
    dst_state = dst.parent / "Local State"
    if src_state.exists():
        shutil.copy2(src_state, dst_state)
        logger.info("Copied Local State (encryption key) to agent profile")

    logger.info("Cookie sync complete: %d cookies written", written)
    return written
