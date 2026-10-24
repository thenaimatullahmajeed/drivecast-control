"""Conservative, single-worker Google Drive -> YouTube uploader."""

import io
import http.client
import json
import logging
import os
import random
import re
import sys
import tempfile
import time
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from google.auth.exceptions import RefreshError, TransportError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload

SCOPES = [
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/youtube.upload",
]
CHANNEL_SCOPE = "https://www.googleapis.com/auth/youtube.readonly"
FOLDER_MIME = "application/vnd.google-apps.folder"
VIDEO_TYPES = {".mp4": "video/mp4", ".mov": "video/quicktime", ".mkv": "video/x-matroska"}
CHUNK_SIZE = 8 * 1024 * 1024
RETRIES = 5
MARKER = "YouTube video ID: "
LOG = logging.getLogger(__name__)
DAILY_KEY = "drivecast_daily_v1"


def daily_budget(drive, folder_id, reserve=False):
    """Reserve before an attempt; crashes/failures consume a slot, never refund it.

    A persistent folder property survives fresh Actions runners and OAuth clients.
    All writers must use the repository's single-worker concurrency group.
    """
    maximum = int(os.environ.get('MAX_UPLOADS_PER_DAY','2'))
    if not 1 <= maximum <= 100:
        raise ValueError('MAX_UPLOADS_PER_DAY must be between 1 and 100.')
    today = datetime.now(ZoneInfo("America/New_York")).date()
    result = drive.files().get(fileId=folder_id, fields="properties",
                               supportsAllDrives=True).execute(num_retries=RETRIES)
    raw = result.get("properties", {}).get(DAILY_KEY)
    count = 0
    if raw is not None:
        try:
            ledger = json.loads(raw)
            recorded = date.fromisoformat(ledger["date"])
            used = ledger["count"]
            if type(used) is not int or not 0 <= used <= 100 or recorded > today:
                raise ValueError("Invalid daily upload ledger")
            count = used if recorded == today else 0
        except (ValueError, TypeError, KeyError) as error:
            raise ValueError("Daily upload counter is invalid; stopped without uploading. Review Drive folder properties.") from error
    if reserve and count < maximum:
        value = json.dumps({"date": today.isoformat(), "count": count + 1}, separators=(",", ":"))
        saved = drive.files().update(fileId=folder_id,
            body={"properties": {DAILY_KEY: value}}, fields="properties",
            supportsAllDrives=True).execute(num_retries=0)
        # A lost/ambiguous acknowledgment stops before claiming or uploading.
        if saved.get("properties", {}).get(DAILY_KEY) != value:
            raise ValueError("Daily slot reservation was not confirmed; no upload started.")
        LOG.info("Daily slot %d/%d reserved for %s (America/New_York).", count + 1, maximum, today)
    return max(0,maximum - count)


class ClaimError(RuntimeError):
    """The Drive move was not confirmed; never upload after an ambiguous claim."""


def error_message(error):
    if isinstance(error, RefreshError):
        return "Google OAuth refresh failed; re-run get_token.py and update the GitHub secrets."
    if isinstance(error, HttpError):
        # HttpError.__str__ includes the request URL, possibly an upload-session URL.
        return f"HTTP {error.resp.status}: {error.reason}"
    return str(error)


def quota_exceeded(error):
    if not isinstance(error, HttpError):
        return quota_exceeded(error.__cause__) if error.__cause__ else False
    try:
        details = json.loads(error.content).get("error", {}).get("errors", [])
        return any(item.get("reason") == "quotaExceeded" for item in details)
    except (ValueError, AttributeError, TypeError):
        return False


def list_files(drive, folder_id):
    files, page = [], None
    while True:
        result = drive.files().list(
            q=f"'{folder_id}' in parents and trashed = false",
            fields="nextPageToken,files(id,name,mimeType,createdTime,parents,description,size)",
            orderBy="createdTime,name", pageSize=1000, pageToken=page,
            supportsAllDrives=True, includeItemsFromAllDrives=True,
        ).execute(num_retries=RETRIES)
        files.extend(result.get("files", []))
        page = result.get("nextPageToken")
        if not page:
            return files


def get_folders(drive, folder_id, files, dry_run):
    folders = {}
    for name in ("Processing", "Done", "Failed"):
        matches = [f for f in files if f["name"] == name and f["mimeType"] == FOLDER_MIME]
        if len(matches) > 1:
            raise ValueError(f"Multiple {name!r} folders exist. Keep exactly one before running.")
        if matches:
            folders[name] = matches[0]["id"]
    if not dry_run:
        for name in ("Processing", "Done", "Failed"):
            if name in folders:
                continue
            # Do not retry creation: an ambiguous response could create two folders.
            folders[name] = drive.files().create(
                body={"name": name, "mimeType": FOLDER_MIME, "parents": [folder_id]},
                fields="id", supportsAllDrives=True,
            ).execute(num_retries=0)["id"]
    return folders


def next_upload_chunk(request):
    for attempt in range(RETRIES + 1):
        try:
            # On a lost response, the same request probes its existing session on the next call.
            return request.next_chunk(num_retries=0)
        except Exception as error:
            if isinstance(error, HttpError):
                retryable = 500 <= error.resp.status < 600
            else:
                retryable = isinstance(error, (OSError, http.client.HTTPException, TransportError))
                # httplib2 is Google's client's transitive transport (e.g. DNS errors).
                retryable = retryable or type(error).__module__ == "httplib2"
            if not retryable or attempt == RETRIES:
                raise
            delay = min(2 ** attempt + random.random(), 32)
            LOG.warning("Transient upload error; retry %d/%d in %.1fs on the same request: %s",
                        attempt + 1, RETRIES, delay, error_message(error))
            time.sleep(delay)


def move(drive, file_id, source_id, destination_id):
    return drive.files().update(
        fileId=file_id, addParents=destination_id, removeParents=source_id,
        fields="id,parents", supportsAllDrives=True,
    ).execute(num_retries=0)


def read_sidecar(drive, files, video):
    base = os.path.splitext(video["name"])[0]
    for extension in (".json", ".txt"):
        matches = [f for f in files if f["name"].casefold() == (base + extension).casefold() and f["mimeType"] != FOLDER_MIME]
        if len(matches) > 1:
            raise ValueError(f"Multiple {base + extension!r} sidecars exist.")
        if not matches:
            continue
        # Bound sidecar memory use. The check also catches a file enlarged during download.
        buffer = io.BytesIO()
        download = MediaIoBaseDownload(buffer, drive.files().get_media(
            fileId=matches[0]["id"], supportsAllDrives=True,
        ), chunksize=64 * 1024)
        done = False
        while not done:
            _, done = download.next_chunk(num_retries=RETRIES)
            if buffer.tell() > 1024 * 1024:
                raise ValueError("Sidecar exceeds 1 MiB.")
        text = buffer.getvalue().decode("utf-8-sig")
        if extension == ".json":
            metadata = json.loads(text)
        else:
            from metadata import text_sidecar
            metadata = text_sidecar(text)
        if not isinstance(metadata, dict):
            raise ValueError("JSON sidecar must contain an object.")
        return metadata
    return {}


def video_body(drive, files, video, privacy, category):
    metadata = read_sidecar(drive, files, video)
    title = metadata.get("title", os.path.splitext(video["name"])[0])
    description = metadata.get("description", "")
    tags = metadata.get("tags", [])
    privacy = metadata.get("privacy", privacy)
    if not isinstance(title, str) or not title.strip():
        raise ValueError("Title must be a nonempty string.")
    if not isinstance(description, str):
        raise ValueError("Description must be a string.")
    if not isinstance(tags, list) or not all(isinstance(tag, str) for tag in tags):
        raise ValueError("Tags must be an array of strings.")
    if privacy not in ("private", "unlisted", "public"):
        raise ValueError("Privacy must be private, unlisted, or public.")
    from metadata import prepare
    if os.environ.get("FORCE_PRIVATE", "false").lower() == "true":
        privacy = "private"
    elif os.environ.get('FORCE_PUBLIC','false').lower()=='true':
        privacy='public'
    return prepare(title, description, tags, category, privacy,
                   basic_seo=os.environ.get("SEO_MODE", "basic") == "basic")


def record_visibility(body,response):
    body['_requested_privacy']=body.get('status',{}).get('privacyStatus','unknown')
    body['_effective_privacy']=response.get('status',{}).get('privacyStatus') or 'unknown'
    if body['_requested_privacy']=='public' and body['_effective_privacy']!='public':
        LOG.warning('Public was requested but effective visibility is %s. Check YouTube Studio and any required API-project audit; never re-upload this accepted video.',body['_effective_privacy'])


def upload(drive, youtube, video, body):
    suffix = os.path.splitext(video["name"])[1].lower()
    with tempfile.TemporaryDirectory(prefix="drive-youtube-") as directory:
        path = Path(directory) / ("video" + suffix)
        with path.open("wb") as target:
            download = MediaIoBaseDownload(target, drive.files().get_media(
                fileId=video["id"], supportsAllDrives=True,
            ), chunksize=CHUNK_SIZE)
            done = False
            while not done:
                _, done = download.next_chunk(num_retries=RETRIES)
        media = MediaFileUpload(str(path), mimetype=VIDEO_TYPES[suffix],
                                chunksize=CHUNK_SIZE, resumable=True)
        try:
            # Create exactly one request. Never start a new session after an upload error.
            request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)
            response = None
            while response is None:
                progress, response = next_upload_chunk(request)
                if progress:
                    LOG.info("Upload %s: %.0f%%", video["id"], progress.progress() * 100)
            if not response.get("id"):
                raise RuntimeError("YouTube returned no video ID; outcome is uncertain. Review manually.")
            record_visibility(body,response)
            return response["id"]
        finally:
            media.stream().close()


def process(drive, youtube, video, files, folders, root_id, privacy, category, report=None):
    # Confirm it is still queued before claiming it. Only one worker may use this folder.
    try:
        current = drive.files().get(fileId=video["id"], fields="id,parents,description,trashed",
                                    supportsAllDrives=True).execute(num_retries=RETRIES)
    except RefreshError:
        raise
    except Exception as error:
        raise ClaimError(f"Cannot check queued file {video['id']}: {error_message(error)}. Stopping.") from error
    if current.get("trashed") or root_id not in current.get("parents", []):
        LOG.warning("Skipping %s: no longer in the source folder.", video["id"])
        return False
    if MARKER in current.get("description", ""):
        LOG.warning("Skipping %s: already has a YouTube upload marker.", video["id"])
        return False

    # A failed/ambiguous claim must stop the run before any video bytes are sent.
    try:
        claimed = move(drive, video["id"], root_id, folders["Processing"])
    except RefreshError:
        raise
    except Exception as error:
        raise ClaimError(f"Unconfirmed Processing move for {video['id']}: {error_message(error)}. "
                         "No upload started; check the file's location manually.") from error
    if folders["Processing"] not in claimed.get("parents", []):
        raise ClaimError(f"Could not confirm Processing move for {video['id']}; stopping.")
    try:
        body = video_body(drive, files, video, privacy, category)
        video_id = upload(drive, youtube, video, body)
        LOG.info("YouTube accepted %r (%s): https://youtu.be/%s", video["name"], video["id"], video_id)
        if report is not None:
            report(video_id, body)
        description = current.get("description", "")
        description = (description + "\n" if description else "") + MARKER + video_id
        drive.files().update(fileId=video["id"], body={"description": description},
                             fields="id", supportsAllDrives=True).execute(num_retries=RETRIES)
        move(drive, video["id"], folders["Processing"], folders["Done"])
        LOG.info("Done: %r (%s)", video["name"], video["id"])
        return True
    except Exception as error:
        LOG.error("Failed: %r (%s): %s", video["name"], video["id"], error_message(error))
        # YouTube may already contain this video. Failed is a quarantine, never a retry queue.
        try:
            move(drive, video["id"], folders["Processing"], folders["Failed"])
        except Exception as move_error:
            LOG.error("Could not move %s to Failed: %s. Check Processing/Done manually.",
                      video["id"], error_message(move_error))
        raise


def verify_channel(youtube, expected_id):
    result = youtube.channels().list(part="id,snippet", mine=True).execute(num_retries=RETRIES)
    channels = result.get("items", [])
    if len(channels) != 1 or channels[0]["id"] != expected_id:
        raise ValueError("Wrong YouTube channel authorization. No upload started. "
                         "Reconnect this client's Google account in setup.py.")
    LOG.info("Verified YouTube channel: %s (%s)", channels[0]["snippet"]["title"], expected_id)


def main():
    required = ("CLIENT_ID", "CLIENT_SECRET", "REFRESH_TOKEN", "FOLDER_ID")
    missing = [name for name in required if not os.environ.get(name, "").strip()]
    if missing:
        raise ValueError("Missing environment variables: " + ", ".join(missing))
    root_id = os.environ["FOLDER_ID"].strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]+", root_id):
        raise ValueError("FOLDER_ID must be the folder ID, not a Drive URL.")
    limit = int(os.environ.get("UPLOAD_LIMIT", "1"))
    if limit < 1:
        raise ValueError("UPLOAD_LIMIT must be at least 1.")
    dry_value = os.environ.get("DRY_RUN", "false").strip().lower()
    if dry_value not in ("true", "false", "1", "0"):
        raise ValueError("DRY_RUN must be true, false, 1, or 0.")
    dry_run = dry_value in ("true", "1")
    privacy = os.environ.get("PRIVACY", "private").strip()
    category = os.environ.get("CATEGORY_ID", "22").strip()
    if privacy not in ("private", "unlisted", "public") or not category.isdigit():
        raise ValueError("Set a valid PRIVACY and a numeric CATEGORY_ID.")
    expected_channel = os.environ.get("EXPECTED_CHANNEL_ID", "").strip()
    if expected_channel and not re.fullmatch(r"UC[A-Za-z0-9_-]{22}", expected_channel):
        raise ValueError("EXPECTED_CHANNEL_ID must be a YouTube channel ID beginning UC.")
    youtube_token = os.environ.get("YOUTUBE_REFRESH_TOKEN", "").strip()
    credentials = Credentials(
        token=None, refresh_token=os.environ["REFRESH_TOKEN"].strip(),
        client_id=os.environ["CLIENT_ID"].strip(), client_secret=os.environ["CLIENT_SECRET"].strip(),
        token_uri="https://oauth2.googleapis.com/token",
        scopes=[SCOPES[0]] if youtube_token else SCOPES + ([CHANNEL_SCOPE] if expected_channel else []),
    )
    credentials.refresh(Request())  # Fail before touching Drive if credentials are invalid.
    youtube_credentials = credentials
    if youtube_token and not dry_run:
        youtube_credentials = Credentials(token=None, refresh_token=youtube_token,
            client_id=os.environ["CLIENT_ID"].strip(), client_secret=os.environ["CLIENT_SECRET"].strip(),
            token_uri="https://oauth2.googleapis.com/token",
            scopes=[SCOPES[1]] + ([CHANNEL_SCOPE] if expected_channel else []))
        youtube_credentials.refresh(Request())
    if expected_channel and not dry_run:
        with build("youtube", "v3", credentials=youtube_credentials, cache_discovery=False) as youtube:
            verify_channel(youtube, expected_channel)
    with build("drive", "v3", credentials=credentials, cache_discovery=False) as drive:
        files = list_files(drive, root_id)
        folders = get_folders(drive, root_id, files, dry_run)
        if "Processing" in folders:
            for stuck in list_files(drive, folders["Processing"]):
                LOG.warning("STUCK IN PROCESSING — never retried: %r (%s). Review YouTube manually.",
                            stuck["name"], stuck["id"])
        candidates = [f for f in files if f["mimeType"] != FOLDER_MIME
                      and os.path.splitext(f["name"])[1].lower() in VIDEO_TYPES
                      and MARKER not in f.get("description", "")]
        remaining = daily_budget(drive, root_id)
        candidates = candidates[:min(limit, remaining)]
        if not remaining:
            LOG.info("Daily limit reached: 2 upload attempts already reserved today (America/New_York). No uploads.")
        if dry_run:
            for video in candidates:
                LOG.info("DRY RUN — would upload: %r (%s)", video["name"], video["id"])
            LOG.info("DRY RUN: %d candidate(s); no folders created, files changed, or YouTube calls.", len(candidates))
            return 0
        failures = 0
        with build("youtube", "v3", credentials=youtube_credentials, cache_discovery=False) as youtube:
            for video in candidates:
                if daily_budget(drive, root_id, reserve=True) == 0:
                    LOG.info("Daily limit reached; remaining videos stay queued.")
                    break
                try:
                    process(drive, youtube, video, files, folders, root_id, privacy, category)
                except (RefreshError, ClaimError):
                    raise
                except Exception as error:
                    if quota_exceeded(error):
                        LOG.warning("quotaExceeded: stopping gracefully. Current file is quarantined; remaining files stay queued.")
                        return 0
                    failures += 1
        LOG.info("Run finished: %d candidate(s), %d failure(s). Failed/Processing files are never retried.",
                 len(candidates), failures)
        return 1 if failures else 0


def cli():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    # Google retry logs may contain resumable session URLs; keep those out of Actions logs.
    logging.getLogger("googleapiclient.http").setLevel(logging.ERROR)
    try:
        return main()
    except RefreshError:
        LOG.error("Google OAuth refresh failed. Drive REFRESH_TOKEN or YOUTUBE_REFRESH_TOKEN may be invalid, expired, or revoked. "
                  "Reconnect Google in the setup panel, or re-run get_token.py --split --verify-channel locally and update GitHub secrets.")
        return 1
    except Exception as error:
        if quota_exceeded(error):
            LOG.warning("quotaExceeded: stopping gracefully; no more files will be processed.")
            return 0
        LOG.error("Run stopped: %s", error_message(error))
        return 1


if __name__ == "__main__":
    sys.exit(cli())
