"""Portable launcher: recreate a copied/non-working virtual environment safely."""
import argparse
import subprocess
import sys
import time
import venv
from pathlib import Path


def prepare(root):
    root = Path(root).resolve()
    work = root / "work"
    work.mkdir(exist_ok=True)
    environment = work / "venv"
    executable = environment / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    healthy = False
    if executable.is_file():
        try:
            healthy = subprocess.run([str(executable),"-c","import sys; assert sys.version_info >= (3,11)"],
                                     capture_output=True,timeout=15).returncode == 0
        except (OSError,subprocess.TimeoutExpired):
            pass
    if not healthy:
        if environment.exists():
            destination = work / ("venv-copied-" + str(time.time_ns()))
            if environment.is_symlink() or environment.resolve().parent != work.resolve() or destination.resolve().parent != work.resolve():
                raise RuntimeError("Unexpected virtual environment path; nothing moved.")
            environment.rename(destination)
        print("Preparing local Python environment...",flush=True)
        venv.EnvBuilder(with_pip=True).create(environment)
    check = "import google.auth, tzdata, argon2; from googleapiclient.discovery import build; from cryptography.hazmat.primitives.ciphers.aead import AESGCM"
    if subprocess.run([str(executable),"-c",check],capture_output=True,timeout=30).returncode:
        subprocess.run([str(executable),"-m","pip","install","--disable-pip-version-check","-r",str(root/"requirements-local.txt")],check=True)
    return executable


if __name__ == "__main__":
    parser=argparse.ArgumentParser()
    parser.add_argument("--chrome",action="store_true")
    args=parser.parse_args()
    if sys.version_info < (3,11):
        raise SystemExit("Install Python 3.11 or newer: https://www.python.org/downloads/windows/")
    source=Path(__file__).resolve().parent
    python=prepare(source)
    raise SystemExit(subprocess.call([str(python),str(source/"setup.py"),"--port","8765"]+(["--chrome"] if args.chrome else []),cwd=source))
