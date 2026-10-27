"""Gmail OAuth2 helper — obtains a refresh token for read-only Gmail access.

This is the modern replacement for App Passwords, which Google now
marks as legacy for personal accounts. Requires the user to have created
a Desktop OAuth client at https://console.cloud.google.com/apis/credentials
and saved it as data/gmail_oauth_client.json.

Flow:
  survey-agent email oauth   -> opens browser, stores data/gmail_oauth_token.json
  survey-agent email test    -> prefers OAuth token, falls back to app password

Scope is intentionally minimal: gmail.readonly.
"""

from __future__ import annotations

import json
import pathlib
from typing import Any

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]


def run_oauth_flow(client_path: str, token_path: str, open_browser: bool = True) -> dict[str, Any]:
    """Run the installed-app OAuth flow and store the token.

    Returns the token dict. Requires google-auth-oauthlib.
    """
    try:
        from google_auth_oauthlib.flow import InstalledAppFlow  # type: ignore
    except ImportError as e:
        raise ImportError("google-auth-oauthlib not installed") from e

    flow = InstalledAppFlow.from_client_secrets_file(client_path, SCOPES)
    # Let the library pick a free port; open_browser controls whether we auto-open
    if open_browser:
        creds = flow.run_local_server(port=0, open_browser=True)
    else:
        # Use manual console flow for headless/SSH
        flow.redirect_uri = flow.redirect_uri or "http://localhost:8080/"
        auth_url, _ = flow.authorization_url(prompt="consent", access_type="offline")
        print(f"Open this URL in your browser:\n{auth_url}\n")
        code = input("Paste the authorization code (or full redirect URL) here: ").strip()
        # Extract code from full URL if user pasted URL
        if "code=" in code:
            from urllib.parse import parse_qs, urlparse

            parsed = urlparse(code)
            code = parse_qs(parsed.query).get("code", [code])[0]
        flow.fetch_token(code=code)
        creds = flow.credentials

    data = {
        "token": creds.token,
        "refresh_token": creds.refresh_token,
        "token_uri": creds.token_uri,
        "client_id": creds.client_id,
        "client_secret": creds.client_secret,
        "scopes": creds.scopes,
    }
    p = pathlib.Path(token_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2))
    p.chmod(0o600)
    return data


def load_oauth_token(token_path: str = "data/gmail_oauth_token.json") -> dict[str, Any] | None:
    p = pathlib.Path(token_path)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except Exception:
        return None
