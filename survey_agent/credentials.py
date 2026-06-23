"""Credential storage — macOS Keychain via Python keyring."""

from __future__ import annotations

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
        """Store credentials for a site."""
        service = self._service_name(site_slug)
        keyring.set_password(service, username, password)

    def retrieve(self, site_slug: str) -> Credentials | None:
        """Retrieve credentials for a site.

        Returns None if no credentials found.
        """
        service = self._service_name(site_slug)
        # keyring doesn't have a "list usernames" API, so we store the
        # last-used username in the service itself as a special key.
        username = keyring.get_password(service, "__last_username__")
        if not username:
            return None
        password = keyring.get_password(service, username)
        if password is None:
            return None
        return Credentials(username=username, password=password)

    def store_with_tracking(self, site_slug: str, username: str, password: str) -> None:
        """Store credentials and track the last-used username."""
        service = self._service_name(site_slug)
        keyring.set_password(service, username, password)
        keyring.set_password(service, "__last_username__", username)

    def delete(self, site_slug: str) -> None:
        """Delete credentials for a site."""
        service = self._service_name(site_slug)
        creds = self.retrieve(site_slug)
        if creds:
            try:
                keyring.delete_password(service, creds.username)
            except keyring.errors.PasswordDeleteError:
                pass
        try:
            keyring.delete_password(service, "__last_username__")
        except keyring.errors.PasswordDeleteError:
            pass

    def has_credentials(self, site_slug: str) -> bool:
        """Check if credentials exist for a site."""
        return self.retrieve(site_slug) is not None
