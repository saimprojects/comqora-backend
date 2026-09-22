"""Railway/Docker process entry points; no shell expansion or migrations per replica."""

import os
import subprocess
import sys
from pathlib import Path


def main():
    os.chdir(Path(__file__).resolve().parent)
    mode = sys.argv[1] if len(sys.argv) > 1 else "web"
    if mode not in {"web", "worker"}:
        raise SystemExit("Usage: python deploy.py [web|worker]")
    subprocess.run([sys.executable, "manage.py", "check"], check=True)
    subprocess.run(
        [
            sys.executable,
            "manage.py",
            "check",
            "--deploy",
            "--tag",
            "security",
            "--tag",
            "comqora",
            "--fail-level",
            "WARNING",
        ],
        check=True,
    )
    if mode == "worker":
        os.execv(sys.executable, [sys.executable, "manage.py", "sync_tracking", "--loop"])
    subprocess.run([sys.executable, "manage.py", "collectstatic", "--noinput"], check=True)
    os.execvp("gunicorn", ["gunicorn", "config.wsgi:application", "--config", "gunicorn.conf.py"])


if __name__ == "__main__":
    main()
