"""One-time local script to generate a Drive OAuth refresh token.

Usage:
    python scanner/auth_bootstrap.py path/to/client_secret.json

Opens a browser window, asks you to log in (use the account that has the
NomNom files), then prints three values to paste into GitHub Secrets:
GOOGLE_OAUTH_CLIENT_ID, GOOGLE_OAUTH_CLIENT_SECRET, GOOGLE_OAUTH_REFRESH_TOKEN.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: python scanner/auth_bootstrap.py <client_secret.json>")
        return 2
    secret_path = Path(sys.argv[1])
    if not secret_path.exists():
        print(f"file not found: {secret_path}")
        return 1

    flow = InstalledAppFlow.from_client_secrets_file(str(secret_path), SCOPES)
    # access_type=offline + prompt=consent guarantees a refresh_token even if
    # the user has previously consented.
    creds = flow.run_local_server(
        port=0,
        access_type="offline",
        prompt="consent",
        open_browser=True,
    )

    if not creds.refresh_token:
        print("ERROR: no refresh_token returned. Try revoking access and retry.")
        return 1

    with secret_path.open() as f:
        secret = json.load(f)
    installed = secret.get("installed") or secret.get("web") or {}
    client_id = installed.get("client_id", "")
    client_secret = installed.get("client_secret", "")

    print()
    print("=" * 60)
    print("Paste these into GitHub repo Settings -> Secrets -> Actions:")
    print("=" * 60)
    print(f"GOOGLE_OAUTH_CLIENT_ID={client_id}")
    print(f"GOOGLE_OAUTH_CLIENT_SECRET={client_secret}")
    print(f"GOOGLE_OAUTH_REFRESH_TOKEN={creds.refresh_token}")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
