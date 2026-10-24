"""Open authorization links only in an explicitly private browser window."""
import os
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path
from urllib.parse import urlparse


def candidates():
    roots = [os.environ.get(k, "") for k in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA")]
    choices = (("Google/Chrome/Application/chrome.exe", "--incognito"),
               ("Microsoft/Edge/Application/msedge.exe", "--inprivate"),
               ("BraveSoftware/Brave-Browser/Application/brave.exe", "--incognito"),
               ("Mozilla Firefox/firefox.exe", "--private-window"))
    for suffix, flag in choices:
        for root in roots:
            path = Path(root) / suffix
            if root and path.is_file():
                yield str(path), flag
    for name, flag in (("google-chrome", "--incognito"), ("chromium", "--incognito"),
                       ("msedge", "--inprivate"), ("brave-browser", "--incognito"), ("firefox", "--private-window")):
        if path := shutil.which(name):
            yield path, flag


def open_private(url):
    parsed = urlparse(url)
    local_meta=parsed.hostname=='localhost' and parsed.port==8443 and parsed.path=='/ready'
    if parsed.scheme != "https" or (not local_meta and parsed.hostname not in ("accounts.google.com", "github.com", "www.facebook.com", "facebook.com")):
        raise ValueError("Only official authorization links may open in a private window.")
    for executable, flag in candidates():
        try:
            profile=tempfile.mkdtemp(prefix='drivecast-private-')
            args=([executable,'--no-remote','--profile',profile,flag,url] if flag=='--private-window' else
                  [executable,'--user-data-dir='+profile,'--no-first-run',flag,'--new-window',url])
            process=subprocess.Popen(args,
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            def cleanup(process=process,profile=profile):
                process.wait()
                shutil.rmtree(profile,ignore_errors=True)  # Only this freshly created temporary profile.
            threading.Thread(target=cleanup,daemon=True).start()
            return True
        except OSError:
            shutil.rmtree(profile,ignore_errors=True)
            continue
    return False  # Caller offers a copyable URL; never silently use a normal window.


if __name__=='__main__':
    import sys
    raise SystemExit(0 if len(sys.argv)==2 and open_private(sys.argv[1]) else 1)
