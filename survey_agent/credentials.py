"""Credential storage — macOS Keychain via Python keyring."""

from __future__ import annotations

import contextlib
from dataclasses import dataclass

import keyring

SERVICE_PREFIX = "survey-agent"


@dataclass
class Credentials:
    """Stored credentials for a survey site."""

    username: str
    password: str


class CredentialStore:
    """Store and retrieve site credentials from macOS Keychain."""

    def __init__(self, service_prefix: str = SERVICE_PREFIX):
        self._prefix = service_prefix

    def _service_name(self, site_slug: str) -> str:
        return f"{self._prefix}/{site_slug}"

    def store(self, site_slug: str, username: str, password: str) -> None:
        """Store credentials for a site (keychain with file fallback)."""
        service = self._service_name(site_slug)
        try:
            # Pre-delete to avoid -25244 on overwrite
            try:
                import keyring.backends.macOS.api as _api  # noqa: PLC0415
                _api.delete_generic_password(service, username)
            except Exception:
                pass
            try:
                keyring.delete_password(service, username)
            except Exception:
                pass
            keyring.set_password(service, username, password)
        except Exception as exc:
            # File fallback: data/credentials/{site}.json with 0600
            import json
            import pathlib  # noqa: PLC0415
            fb = pathlib.Path(__file__).resolve().parent / "data" / "credentials" / f"{site_slug}.json"
            fb.parent.mkdir(parents=True, exist_ok=True)
            fb.write_text(json.dumps({"username": username, "password": password}))
            fb.chmod(0o600)
            import logging  # noqa: PLC0415

            logging.getLogger(__name__).warning("Keychain failed for %s/%s (%s) — fell back to %s", service, username, exc, fb)

    def retrieve(self, site_slug: str) -> Credentials | None:
        """Retrieve credentials for a site (keychain, then file fallback).

        Returns None if no credentials found.
        """
        service = self._service_name(site_slug)
        username = None
        try:
            username = keyring.get_password(service, "__last_username__")
        except Exception:
            pass
        if not username:
            # File fallback
            import json
            import pathlib  # noqa: PLC0415
            fb = pathlib.Path(__file__).resolve().parent / "data" / "credentials" / f"{site_slug}.json"
            if fb.exists():
                try:
                    data = json.loads(fb.read_text())
                    username = data.get("__last_username__") or data.get("username")
                    password = data.get("password")
                    if username and password:
                        return Credentials(username=username, password=password)
                except Exception:
                    pass
            return None
        try:
            password = keyring.get_password(service, username)
        except Exception:
            password = None
        if password is None:
            # Check file fallback for password
            import json
            import pathlib  # noqa: PLC0415
            fb = pathlib.Path(__file__).resolve().parent / "data" / "credentials" / f"{site_slug}.json"
            if fb.exists():
                try:
                    data = json.loads(fb.read_text())
                    if data.get("username") == username:
                        password = data.get("password")
                except Exception:
                    pass
        if password is None:
            return None
        return Credentials(username=username, password=password)

    def store_with_tracking(self, site_slug: str, username: str, password: str) -> None:
        """Store credentials and track the last-used username (with file fallback)."""
        service = self._service_name(site_slug)
        self.store(site_slug, username, password)
        try:
            try:
                import keyring.backends.macOS.api as _api  # noqa: PLC0415
                _api.delete_generic_password(service, "__last_username__")
            except Exception:
                pass
            try:
                keyring.delete_password(service, "__last_username__")
            except Exception:
                pass
            keyring.set_password(service, "__last_username__", username)
        except Exception:
            import json
            import pathlib  # noqa: PLC0415
            fb = pathlib.Path(__file__).resolve().parent / "data" / "credentials" / f"{site_slug}.json"
            if fb.exists():
                try:
                    data = json.loads(fb.read_text())
                    data["__last_username__"] = username
                    fb.write_text(json.dumps(data))
                except Exception:
                    pass

    def delete(self, site_slug: str) -> None:
        """Delete credentials for a site."""
        service = self._service_name(site_slug)
        creds = self.retrieve(site_slug)
        if creds:
            with contextlib.suppress(keyring.errors.PasswordDeleteError):
                keyring.delete_password(service, creds.username)
        with contextlib.suppress(keyring.errors.PasswordDeleteError):
            keyring.delete_password(service, "__last_username__")

    def has_credentials(self, site_slug: str) -> bool:
        """Check if credentials exist for a site."""
        return self.retrieve(site_slug) is not None
