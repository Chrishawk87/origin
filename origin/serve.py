"""Serve Origin over the network so you can use it from any device (phone,
another laptop) — on your LAN, or online via a tunnel.

SECURITY: Origin can run terminal commands on this machine. Exposing it to a
network is a remote-control surface, so a secret access token is REQUIRED for
any non-localhost binding. Share the token URL only with yourself/trusted people.

Usage:
    python -m origin.serve                       # localhost only (safe default)
    python -m origin.serve --lan                 # reachable on your home network
    python -m origin.serve --lan --token SECRET  # set your own token
    python -m origin.serve --online              # public https URL via cloudflared
"""

from __future__ import annotations

import argparse
import os
import secrets
import shutil
import socket
import subprocess
import threading
import time


def _lan_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def _start_tunnel(port: int) -> None:
    """Best-effort public URL via cloudflared quick tunnel (no account needed)."""
    cf = shutil.which("cloudflared")
    if not cf:
        print("  (to get a public https URL, install cloudflared: https://developers.cloudflare.com/"
              "cloudflare-one/connections/connect-networks/downloads/  then re-run with --online)")
        return

    def run():
        try:
            subprocess.run([cf, "tunnel", "--url", f"http://127.0.0.1:{port}"], check=False)
        except Exception as e:
            print(f"  tunnel error: {e}")

    threading.Thread(target=run, daemon=True).start()
    print("  starting public tunnel via cloudflared… watch for the https://…trycloudflare.com URL below")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="origin-serve", description="Serve Origin over the network.")
    parser.add_argument("--host", default=None, help="Bind address (default 127.0.0.1).")
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8000)),
                        help="Port (defaults to $PORT if set — used by Railway/Render/etc.).")
    parser.add_argument("--lan", action="store_true", help="Bind 0.0.0.0 so other devices on your network can reach it.")
    parser.add_argument("--online", action="store_true", help="Also expose a public https URL via cloudflared.")
    parser.add_argument("--token", default=os.environ.get("ORIGIN_TOKEN"), help="Access token (auto-generated if omitted).")
    parser.add_argument("--insecure", action="store_true", help="Allow network binding with NO token (dangerous).")
    args = parser.parse_args(argv)

    try:
        import uvicorn
    except ImportError:
        raise SystemExit("Serving needs: pip install fastapi uvicorn")

    from .config import load_config
    from .server import create_app

    networked = args.lan or args.online or (args.host not in (None, "127.0.0.1", "localhost"))
    host = args.host or ("0.0.0.0" if networked else "127.0.0.1")

    token = args.token
    if networked and not token and not args.insecure:
        token = secrets.token_urlsafe(12)
        print(f"⚠  Network access needs a token — generated one for you: {token}")
    if networked and not token and args.insecure:
        print("⚠  DANGER: serving on the network with NO token. Anyone who reaches this port can "
              "run commands on this machine.")

    app = create_app(load_config(), token=token)

    print("\n◍  Origin server")
    print(f"   local:    http://127.0.0.1:{args.port}/" + (f"?token={token}" if token else ""))
    if networked:
        ip = _lan_ip()
        print(f"   network:  http://{ip}:{args.port}/" + (f"?token={token}" if token else ""))
        print("   → open that URL on your phone/other computer (same Wi-Fi).")
    if token:
        print(f"   token:    {token}   (required; it's already in the URLs above)")
    if args.online:
        _start_tunnel(args.port)
    print()

    if networked and not token:
        print("Refusing to serve on the network without a token. Re-run with --token <secret> "
              "or --insecure if you really mean it.")
        if not args.insecure:
            return 1

    uvicorn.run(app, host=host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
