"""Local launcher / official GitHub CLI utilities; no active-account routing."""
import hashlib
import io
import json
import os
import platform
import shutil
import subprocess
import webbrowser
import zipfile
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import Request as URLRequest, urlopen
from main import SCOPES, CHANNEL_SCOPE, FOLDER_MIME
import re
ROOT=Path(__file__).resolve().parent
WORK=ROOT/"work"
GITHUB={"notice":""}


PUBLIC_KEYS = ("id", "name", "folder_id", "channel_input", "repo_name", "privacy", "limit", "category",
               "status", "notice", "channel_id", "channel_title", "folder_name", "verified", "repo_id",
               "repo_url", "branch", "initial_head", "deployed", "enabled", "test_before", "test_started", "test_passed", "test_url")

def find_gh():
    local = WORK / "tools" / "gh.exe"
    return str(local) if local.is_file() else shutil.which("gh")

def download(url, maximum):
    request = URLRequest(url, headers={"User-Agent": "Drive-YouTube-Setup", "Accept": "application/octet-stream"})
    with urlopen(request, timeout=60) as response:
        result = response.read(maximum + 1)
    if len(result) > maximum:
        raise ValueError("Download exceeds the allowed size.")
    return result

def install_gh():
    if os.name != "nt":
        raise ValueError("Install GitHub CLI from https://cli.github.com, then restart this panel.")
    with urlopen(URLRequest("https://api.github.com/repos/cli/cli/releases/latest",
                           headers={"User-Agent": "Drive-YouTube-Setup"}), timeout=30) as response:
        release = json.load(response)
    architecture = "arm64" if platform.machine().lower() in ("arm64", "aarch64") else "amd64"
    assets = release["assets"]
    archive = next(a for a in assets if a["name"].endswith(f"windows_{architecture}.zip"))
    checksums = next(a for a in assets if a["name"].endswith("checksums.txt"))
    for asset in (archive, checksums):
        if not asset["browser_download_url"].startswith("https://github.com/cli/cli/releases/download/"):
            raise ValueError("Unexpected GitHub CLI release download URL.")
    GITHUB["notice"] = "Downloading official GitHub CLI; this may take a few minutes."
    checksum_text = download(checksums["browser_download_url"], 1024 * 1024).decode()
    expected = next(line.split()[0] for line in checksum_text.splitlines()
                    if line.split()[-1].lstrip("*") == archive["name"])
    data = download(archive["browser_download_url"], 100 * 1024 * 1024)
    if hashlib.sha256(data).hexdigest() != expected:
        raise ValueError("GitHub CLI checksum did not match. Nothing was installed.")
    with zipfile.ZipFile(io.BytesIO(data)) as package:
        entries = [p for p in package.namelist() if p.replace("\\", "/").endswith("/bin/gh.exe") or p == "bin/gh.exe"]
        if len(entries) != 1:
            raise ValueError("Unexpected GitHub CLI archive layout.")
        executable = package.read(entries[0])
    destination = WORK / "tools" / "gh.exe"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(executable)
    GITHUB["notice"] = "GitHub CLI ready. Click Connect GitHub."

def parse_folder(value):
    value = value.strip()
    if re.fullmatch(r"[A-Za-z0-9_-]+", value):
        return value
    parsed = urlparse(value)
    if parsed.scheme != "https" or parsed.hostname != "drive.google.com":
        raise ValueError("Enter a Google Drive folder link or folder ID.")
    match = re.search(r"/folders/([A-Za-z0-9_-]+)", parsed.path)
    if not match:
        raise ValueError("This is not a Drive folder link.")
    return match[1]

def open_panel(url, chrome=False):
    if chrome:
        locations = [Path(os.environ.get(key, "")) / "Google/Chrome/Application/chrome.exe"
                     for key in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA")
                     if os.environ.get(key)]
        installed = shutil.which("chrome") or shutil.which("google-chrome") or shutil.which("chromium")
        if installed:
            locations.insert(0, Path(installed))
        for location in locations:
            if location.is_file():
                subprocess.Popen([str(location), "--new-tab", url],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return
        print("Chrome was not found; opening your default browser instead.", flush=True)
    webbrowser.open(url)
