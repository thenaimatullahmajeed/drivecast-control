"""One-time Desktop OAuth authorization using a local callback and PKCE."""

import argparse
import base64
import hashlib
import json
import secrets
import sys
import time
from google_identity import has_scopes
from private_browser import open_private
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.request import Request, urlopen

from main import SCOPES, CHANNEL_SCOPE


def authorize(config, scopes=None, announce=print, open_browser=True):
    scopes = SCOPES if scopes is None else scopes
    if 'web' in config:
        from oauth_web import google
        return google(config,scopes,announce)
    if "installed" not in config:
        raise ValueError("Download an OAuth client of type Desktop app (not Web application).")
    config = config["installed"]
    state = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    result = {}

    class Callback(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass  # Never log the authorization code or state.

        def do_GET(self):
            parsed = urlparse(self.path)
            params = parse_qs(parsed.query)
            if parsed.path != "/callback" or not secrets.compare_digest(params.get("state", [""])[0], state):
                self.send_error(400, "Invalid OAuth callback")
                return
            if params.get("error"):
                result["error"] = params["error"][0]
            elif params.get("code"):
                result["code"] = params["code"][0]
            else:
                self.send_error(400, "Missing authorization code")
                return
            message = b"Authorization received. Return to your terminal; you can close this tab."
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(message)))
            self.end_headers()
            self.wfile.write(message)

    with HTTPServer(("127.0.0.1", 0), Callback) as server:
        server.timeout = 1
        redirect_uri = f"http://127.0.0.1:{server.server_port}/callback"
        url = "https://accounts.google.com/o/oauth2/v2/auth?" + urlencode({
            "client_id": config["client_id"], "redirect_uri": redirect_uri,
            "response_type": "code", "scope": " ".join(scopes), "access_type": "offline",
            "prompt": "consent select_account", "include_granted_scopes": "false",
            "state": state, "code_challenge": challenge,
            "code_challenge_method": "S256",
        })
        announce("Authorize the Google account/YouTube channel that should receive uploads.")
        announce(url)
        if open_browser and not open_private(url):
            announce("Open this link in a private window. Automatic private-browser launch was unavailable.")
        deadline = time.monotonic() + 300
        while not result and time.monotonic() < deadline:
            server.handle_request()
        if not result:
            raise TimeoutError("Authorization timed out after five minutes. Re-run get_token.py.")
        if "error" in result:
            raise ValueError("Google authorization was declined: " + result["error"])

    body = urlencode({
        "client_id": config["client_id"], "client_secret": config["client_secret"],
        "code": result["code"], "code_verifier": verifier,
        "grant_type": "authorization_code", "redirect_uri": redirect_uri,
    }).encode()
    request = Request("https://oauth2.googleapis.com/token", data=body,
                      headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urlopen(request, timeout=60) as response:
            tokens = json.load(response)
    except HTTPError as error:
        raise RuntimeError(f"Google token exchange failed (HTTP {error.code}). Re-run get_token.py.") from None
    if not tokens.get("refresh_token"):
        raise RuntimeError("Google returned no refresh token. Re-run and grant offline consent.")
    if "scope" in tokens and not has_scopes(tokens['scope'],scopes):
        raise RuntimeError("Grant all requested Drive/YouTube permissions, then reconnect.")
    return {"CLIENT_ID": config["client_id"], "CLIENT_SECRET": config["client_secret"],
            "REFRESH_TOKEN": tokens["refresh_token"]}


def get_token(client_file, verify=False, split=False):
    config = json.loads(Path(client_file).read_text(encoding="utf-8-sig"))
    if split:
        print("Step 1/2: choose a regular Google account with Drive folder editor access.")
        values = authorize(config, [SCOPES[0]])
        print("Step 2/2: choose the target YouTube channel / Brand Account.")
        youtube = authorize(config, [SCOPES[1]] + ([CHANNEL_SCOPE] if verify else []))
        values["YOUTUBE_REFRESH_TOKEN"] = youtube["REFRESH_TOKEN"]
    else:
        values = authorize(config, SCOPES + ([CHANNEL_SCOPE] if verify else []))
    print("\nCopy these into GitHub Actions repository secrets. Do not commit or share them.")
    for name, value in values.items():
        print(name + "=" + value)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("client_file", nargs="?", default="client_secret.json",
                        help="Downloaded Desktop OAuth client JSON (default: client_secret.json)")
    parser.add_argument("--verify-channel", action="store_true", help="Also grant read-only channel identity checks")
    parser.add_argument("--split", action="store_true", help="Authorize Drive and YouTube separately for Brand channels or different accounts")
    args = parser.parse_args()
    try:
        get_token(args.client_file, args.verify_channel, args.split)
    except (Exception, KeyboardInterrupt) as error:
        print(f"Authorization failed: {error}", file=sys.stderr)
        sys.exit(1)
