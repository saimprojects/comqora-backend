"""Railway/Docker process entry points; no shell expansion or migrations per replica."""

import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path


def run_combined():
    """Supervise both services; Railway restarts the container if either exits."""
    stopping = threading.Event()
    children = []
    previous = {}

    def stop(signum, frame):
        stopping.set()

    def terminate(process, force=False):
        if os.name == "posix":
            try:
                os.killpg(process.pid, signal.SIGKILL if force else signal.SIGTERM)
            except ProcessLookupError:
                pass
        elif process.poll() is None:
            process.kill() if force else process.terminate()

    try:
        for signum in (signal.SIGTERM, signal.SIGINT):
            previous[signum] = signal.signal(signum, stop)
        for command in (
            ["gunicorn", "config.wsgi:application", "--config", "gunicorn.conf.py"],
            [sys.executable, "manage.py", "sync_tracking", "--loop"],
        ):
            if stopping.is_set():
                return 0
            children.append(subprocess.Popen(command, start_new_session=os.name == "posix"))
        while not stopping.is_set():
            for process in children:
                if process.poll() is not None:
                    print(
                        f"Service process {process.pid} exited ({process.returncode}); stopping container.",
                        flush=True,
                    )
                    return 1
            stopping.wait(0.5)
        return 0
    finally:
        for process in children:
            terminate(process)
        deadline = time.monotonic() + 30
        for process in children:
            try:
                process.wait(timeout=max(0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                terminate(process, force=True)
                process.wait()
        for signum, handler in previous.items():
            signal.signal(signum, handler)


def main():
    os.chdir(Path(__file__).resolve().parent)
    mode = sys.argv[1] if len(sys.argv) > 1 else "web"
    if mode not in {"web", "worker", "all"}:
        raise SystemExit("Usage: python deploy.py [web|worker|all]")
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
    try:
        subprocess.run([sys.executable, "manage.py", "migrate", "--check", "--noinput"], check=True)
    except subprocess.CalledProcessError:
        raise SystemExit(
            "Database migration check failed. Check database connectivity and run "
            "'python manage.py migrate --noinput' against this service's database. "
            "Set the same command as Railway's Pre-deploy Command before redeploying."
        ) from None
    if mode == "worker":
        os.execv(sys.executable, [sys.executable, "manage.py", "sync_tracking", "--loop"])
    subprocess.run([sys.executable, "manage.py", "collectstatic", "--noinput"], check=True)
    if mode == "all":
        raise SystemExit(run_combined())
    os.execvp("gunicorn", ["gunicorn", "config.wsgi:application", "--config", "gunicorn.conf.py"])


if __name__ == "__main__":
    main()
