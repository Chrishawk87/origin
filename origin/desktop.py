"""Origin desktop launcher.

Starts the local backend and opens Origin in a native application window
(via pywebview). Falls back to your default browser if pywebview isn't
installed. This is what the packaged double-click app runs.
"""

from __future__ import annotations

import socket
import threading
import time


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _wait_until_up(port: int, timeout: float = 15.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.15)
    return False


def main() -> int:
    try:
        import uvicorn
    except ImportError:
        raise SystemExit("Origin desktop needs: pip install fastapi uvicorn (and pywebview for a native window)")

    from .config import load_config
    from .server import create_app

    config = load_config()
    app = create_app(config)
    port = _free_port()
    url = f"http://127.0.0.1:{port}"

    def run_server() -> None:
        uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")

    threading.Thread(target=run_server, daemon=True).start()
    if not _wait_until_up(port):
        print("Origin server failed to start.")
        return 1

    try:
        import webview  # pywebview
        webview.create_window("Origin", url, width=1220, height=840, min_size=(900, 600))
        webview.start()
    except ImportError:
        import webbrowser
        webbrowser.open(url)
        print(f"◍ Origin is running at {url}")
        print("  (install pywebview for a native app window)  —  Ctrl+C to quit")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
