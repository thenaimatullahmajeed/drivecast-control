"""Small GitHub REST client. Tokens never appear in URLs or error messages."""
import base64
import json
import re
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen


class GitHubError(RuntimeError):
    def __init__(self, status, message="GitHub request failed", reason="unknown"):
        self.status, self.reason = status, reason
        super().__init__(f"{message} (HTTP {status}). Check connection/permissions; no automatic upload retry.")


class GitHub:
    def __init__(self, token, owner=""):
        if not token:
            raise ValueError("Connect/unlock this GitHub account first.")
        if owner and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}", owner):
            raise ValueError("Invalid GitHub account name.")
        self.token, self.owner = token, owner

    def request(self, path, method="GET", body=None):
        if not path.startswith("/") or ".." in path or path.startswith("//"):
            raise ValueError("Invalid GitHub API path.")
        headers = {"Authorization": "Bearer " + self.token, "Accept": "application/vnd.github+json",
                   "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "DriveCast-Local"}
        payload = json.dumps(body).encode() if body is not None else None
        if payload is not None:
            headers["Content-Type"] = "application/json"
        try:
            with urlopen(Request("https://api.github.com" + path, data=payload, method=method, headers=headers), timeout=45) as response:
                raw = response.read(32 * 1024 * 1024)
                return json.loads(raw) if raw else {}
        except HTTPError as error:
            # Classify known provider errors without echoing arbitrary response
            # content (which can contain private URLs or request details).
            try:
                data=json.loads(error.read(65536))
                message=str(data.get('message','')).lower() if isinstance(data,dict) else ''
            except Exception:message=''
            error_headers=error.headers or {}
            if error.code==403 and 'account' in message and 'suspended' in message:
                reason,notice='suspended','GitHub account suspended. Request an account review through GitHub Appeal and Reinstatement'
            elif error.code==429 or 'rate limit' in message or error_headers.get('X-RateLimit-Remaining')=='0':
                reason,notice='rate_limit','GitHub API rate limit reached. Wait for the limit to reset before checking again'
            elif error_headers.get('X-GitHub-SSO'):
                reason,notice='sso','GitHub organization authorization required. Authorize this token for the organization'
            elif error.code==401:
                reason,notice='authentication','GitHub authorization expired or was revoked. Reconnect this account'
            elif error.code==403 and ('resource not accessible' in message or 'permission' in message):
                reason,notice='permission','GitHub token cannot access this resource. Check private repository and workflow permissions'
            else:reason,notice='unknown','GitHub request failed'
            error.close()
            raise GitHubError(error.code,notice,reason) from None
        except (URLError, TimeoutError, OSError):
            raise GitHubError("network", "GitHub did not confirm the request") from None

    def repo_path(self, repo):
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}", repo):
            raise ValueError("Invalid repository name.")
        return f"/repos/{self.owner}/{repo}"

    def repository(self, repo):
        return self.request(self.repo_path(repo))

    def create_private(self, repo, description, auto_init=True):
        identity = self.request("/user")["login"]
        if identity.casefold() != self.owner.casefold():
            raise ValueError("GitHub token does not belong to the selected account.")
        return self.request("/user/repos", "POST", {"name": repo, "private": True,
            "description": description, "auto_init": bool(auto_init)})

    def read_file(self, repo, path, branch="main", missing=False):
        route = self.repo_path(repo) + "/contents/" + quote(path, safe="/") + "?ref=" + quote(branch, safe="")
        try:
            item = self.request(route)
        except GitHubError as error:
            if error.status == 404 and missing:
                return None, None
            raise
        if isinstance(item, dict) and item.get("encoding") == "none" and item.get("sha"):
            item = self.request(self.repo_path(repo) + "/git/blobs/" + item["sha"])
        if not isinstance(item, dict) or item.get("encoding") != "base64":
            raise ValueError("Unexpected repository file format.")
        return base64.b64decode(item["content"]), item["sha"]

    def write_file(self, repo, path, content, branch="main", sha=None, message="Update DriveCast records"):
        body = {"message": message, "content": base64.b64encode(content).decode(), "branch": branch}
        if sha:
            body["sha"] = sha
        return self.request(self.repo_path(repo) + "/contents/" + quote(path, safe="/"), "PUT", body)

    def snapshot(self, repo, files, branch, expected_head=None, message="Save DriveCast workspace"):
        """One atomic non-force commit; stale local writers are refused."""
        path = self.repo_path(repo)
        head = self.request(path + "/git/ref/heads/" + branch)["object"]["sha"]
        if expected_head and head != expected_head:
            raise ValueError("GitHub workspace changed on another laptop. Fetch it before saving; nothing overwritten.")
        commit = self.request(path + "/git/commits/" + head)
        tree = self.request(path + "/git/trees", "POST", {"base_tree": commit["tree"]["sha"], "tree": [
            {"path": name, "type": "blob", "mode": "100644", "content": value}
            for name, value in files.items()]})
        new_commit = self.request(path + "/git/commits", "POST", {"message": message, "tree": tree["sha"], "parents": [head]})
        self.request(path + "/git/refs/heads/" + branch, "PATCH", {"sha": new_commit["sha"], "force": False})
        return new_commit["sha"]


class Ledger:
    """Durable at-most-once reservations per client repository / target channel."""
    def __init__(self, github, repo, channel_id, branch="drivecast-state", path="records/ledger.json", max_daily=2):
        self.github, self.repo, self.channel, self.branch = github, repo, channel_id, branch
        if not re.fullmatch(r'records/[A-Za-z0-9_/-]+\.json',path) or '..' in path:
            raise ValueError('Invalid ledger path.')
        self.path, self.max_daily = path, int(max_daily)
        if not 1 <= self.max_daily <= 100:
            raise ValueError('Max uploads per day must be between 1 and 100.')
        self.sha = None
        self.data = {"version": 1, "channel_id": channel_id, "daily": {}, "items": {}}

    def load(self, create=False, required=False):
        try:
            raw, self.sha = self.github.read_file(self.repo, self.path, self.branch)
        except GitHubError as error:
            if error.status != 404:
                raise
            if not create:
                if required:
                    raise ValueError("Upload ledger missing/unavailable. Stopped to prevent duplicate uploads; restore the original state branch.") from None
                return self.data
            repo = self.github.repository(self.repo)
            head = self.github.request(self.github.repo_path(self.repo) + "/git/ref/heads/" + repo["default_branch"])["object"]["sha"]
            try:
                self.github.request(self.github.repo_path(self.repo) + "/git/refs", "POST", {"ref": "refs/heads/" + self.branch, "sha": head})
            except GitHubError as conflict:
                if conflict.status != 422:
                    raise
            raw, self.sha = self.github.read_file(self.repo, self.path, self.branch, missing=True)
        if raw:
            data = json.loads(raw)
            if data.get("version") != 1 or data.get("channel_id") != self.channel or not isinstance(data.get("items"), dict):
                raise ValueError("Ledger identity/schema mismatch; uploads stopped.")
            self.data = data
        return self.data

    def save(self):
        raw = json.dumps(self.data, ensure_ascii=False, sort_keys=True).encode()
        result = self.github.write_file(self.repo, self.path, raw, self.branch, self.sha)
        self.sha = result["content"]["sha"]
        confirmed, confirmed_sha = self.github.read_file(self.repo, self.path, self.branch)
        if confirmed != raw or confirmed_sha != self.sha:
            raise ValueError("GitHub record write was not confirmed; stopped before any further upload.")

    def reserve(self, key, item, day, attempt_id):
        if key in self.data["items"]:
            return False
        count = self.data["daily"].get(day, 0)
        if type(count) is not int or not 0 <= count <= 100:
            raise ValueError("Invalid daily counter; uploads stopped.")
        if count >= self.max_daily:
            return False
        self.data["daily"][day] = count + 1
        self.data["items"][key] = dict(item, status="Processing", attempt_id=attempt_id, reserved_at=item.get('recorded_at',''))
        self.save()
        return True

    def update(self, key, **values):
        self.data["items"][key].update(values)
        self.save()
