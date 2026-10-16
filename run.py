"""Start the local GUI. Keep this console open while the job is running."""
from __future__ import annotations
import argparse
import threading
import webbrowser
from werkzeug.serving import make_server
from planet_downloader.app import create_app

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--port", type=int, default=0)
    args = parser.parse_args()
    app = create_app()
    server = make_server("127.0.0.1", args.port, app, threaded=True)
    url = f"http://127.0.0.1:{server.server_port}/"
    print(f"\nPlanet Downloader\nOpen: {url}\nKeep this window open while the job is running.\nPress Ctrl+C to close.\n", flush=True)
    if not args.no_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        app.extensions["planet_state"]["cancel"].set()
    finally:
        server.server_close()

if __name__ == "__main__":
    main()
