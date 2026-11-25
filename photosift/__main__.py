"""Entry point.

    photosift            open the desktop window
    photosift --browser  serve on localhost and open the default browser instead
    photosift --server   serve only (no window, no browser)
"""

from __future__ import annotations

import argparse
import socket
import threading
import time
import webbrowser

import uvicorn

from .context import Ctx
from .server import create_app


def _free_port(preferred: int) -> int:
    for port in (preferred, 0):
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", port))
                return s.getsockname()[1]
            except OSError:
                continue
    raise RuntimeError("no free port")


def _wait_up(port: int, timeout: float = 20.0) -> None:
    t0 = time.time()
    while time.time() - t0 < timeout:
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return
        time.sleep(0.1)


def main() -> None:
    ap = argparse.ArgumentParser(prog="photosift", description="相片整理與比賽選片助手")
    ap.add_argument("--browser", action="store_true", help="用預設瀏覽器開啟，不開原生視窗")
    ap.add_argument("--server", action="store_true", help="只啟動本機服務")
    ap.add_argument("--port", type=int, default=8765)
    args = ap.parse_args()

    app = create_app(Ctx())
    port = _free_port(args.port)
    url = f"http://127.0.0.1:{port}"
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)

    if args.server:
        print(f"PhotoSift 服務已啟動：{url}")
        server.run()
        return

    window_mode = not args.browser
    webview = None
    if window_mode:
        try:
            import webview  # pywebview: a native window around the system web view
        except ImportError:
            print("找不到 pywebview，改用瀏覽器開啟。")
            window_mode = False

    threading.Thread(target=server.run, daemon=True).start()
    _wait_up(port)

    if window_mode:
        window = webview.create_window("PhotoSift", url, width=1440, height=920, min_size=(1024, 680))
        app.state.window = window  # lets the UI open the native folder chooser
        webview.start()
        server.should_exit = True
    else:
        print(f"PhotoSift：{url}（按 Ctrl+C 結束）")
        webbrowser.open(url)
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            server.should_exit = True


if __name__ == "__main__":
    main()
