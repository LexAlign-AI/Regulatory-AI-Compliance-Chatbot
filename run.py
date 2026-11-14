"""One-click launcher: starts server.py, waits until the site is up, then opens it in Google Chrome.

    python run.py          (or double-click run.bat on Windows)
Press Ctrl+C in this window to stop the server.
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
import webbrowser
from pathlib import Path

HERE = Path(__file__).resolve().parent
START_PORT = 8000


def port_free(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(("127.0.0.1", port)) != 0


def is_our_app(port):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/state", timeout=2) as r:
            json.loads(r.read())
            return True
    except Exception:
        return False


def find_chrome():
    cands = []
    if sys.platform.startswith("win"):
        for base in (os.environ.get("PROGRAMFILES"), os.environ.get("PROGRAMFILES(X86)"), os.environ.get("LOCALAPPDATA")):
            if base:
                cands.append(Path(base) / "Google" / "Chrome" / "Application" / "chrome.exe")
    elif sys.platform == "darwin":
        cands.append(Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"))
    else:
        for n in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser"):
            p = shutil.which(n)
            if p:
                cands.append(Path(p))
    return next((str(c) for c in cands if c.is_file()), None)


def open_chrome(url):
    chrome = find_chrome()
    if chrome:
        try:
            subprocess.Popen([chrome, url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return
        except OSError:
            pass
    print("Chrome was not found, opening your default browser instead.")
    webbrowser.open(url)


def main():
    port = START_PORT
    while not port_free(port):
        if is_our_app(port):  # already running: just open it
            print(f"Lex Align is already running on port {port}.")
            return open_chrome(f"http://localhost:{port}")
        port += 1
        if port > START_PORT + 20:
            sys.exit("No free port found between 8000 and 8020.")

    url = f"http://localhost:{port}"
    server = subprocess.Popen([sys.executable, str(HERE / "server.py"), "--port", str(port)], cwd=str(HERE))
    try:
        deadline = time.time() + 90  # first start can be slow while the libraries load
        while time.time() < deadline:
            if server.poll() is not None:
                sys.exit("The server stopped unexpectedly. See the message above.")
            if is_our_app(port):
                break
            time.sleep(0.4)
        else:
            sys.exit("The server did not start in time.")
        print(f"Opening {url} in Chrome...")
        open_chrome(url)
        server.wait()
    except KeyboardInterrupt:
        pass
    finally:
        if server.poll() is None:
            server.terminate()
            try:
                server.wait(5)
            except subprocess.TimeoutExpired:
                server.kill()


if __name__ == "__main__":
    main()
