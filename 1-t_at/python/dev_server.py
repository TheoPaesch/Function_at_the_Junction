#!/usr/bin/env python3
"""Dev mode: the same app on a laptop, no board needed.

    pip install fastapi uvicorn
    python3 python/dev_server.py

SimLink stands in for the sketch (virtual scale, virtual valve, safety stops)
and a small FastAPI shim stands in for the App Lab WebUI brick. Open
http://localhost:7000 or, from a phone on the same Wi-Fi, http://<laptop-ip>:7000.

Environment:
    PORT          web port (default 7000; the next free port is used if it is busy)
    HOST          address to listen on (default 0.0.0.0, so phones can connect)
    LLM_URL       any OpenAI-compatible server to test the parser with a model,
                  e.g. http://localhost:8080 (llama-server) or http://localhost:11434 (Ollama)
    LLM_MODEL     the model name that server expects
    LLM_API_KEY   optional bearer token
    APP_DATA_DIR  where settings.json and history.json go (default: ../data)
"""

import logging
import os
import socket
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
APP_DIR = os.path.dirname(HERE)
ASSETS_DIR = os.path.join(APP_DIR, "assets")
sys.path.insert(0, HERE)

try:
    import uvicorn
    from fastapi import FastAPI, HTTPException
    from fastapi.staticfiles import StaticFiles
except ImportError:
    sys.exit("Dev mode needs FastAPI and uvicorn:  pip install fastapi uvicorn")

import webapi                                    # noqa: E402
from board import Board, SimLink                 # noqa: E402
from core import APP_NAME, Machine               # noqa: E402
from intent import LLM_LABEL, IntentEngine       # noqa: E402
from recipes import RecipeBook                   # noqa: E402
from storage import Storage                      # noqa: E402

log = logging.getLogger("dev")


class FreshStaticFiles(StaticFiles):
    """Static files that are never cached, so an edit shows up on the phone at once."""

    async def get_response(self, path, scope):
        try:
            response = await super().get_response(path, scope)
        except ValueError:                       # a null byte in the path, for example
            raise HTTPException(status_code=404)
        response.headers["Cache-Control"] = "no-store"
        return response


class WebUIShim(object):
    """Stand-in for arduino.app_bricks.web_ui.WebUI: same expose_api(method, path, fn)."""

    def __init__(self, assets_dir=ASSETS_DIR, port=7000, host="0.0.0.0"):
        self.assets_dir = assets_dir
        self.port = int(port)
        self.host = host
        self.app = FastAPI(title=APP_NAME + " (dev mode)", docs_url=None, redoc_url=None)

    def expose_api(self, method, path, function):
        self.app.add_api_route(path, function, methods=[method.upper()])

    def run(self):
        # Mounted last, like the brick does when it starts: API routes win over files.
        self.app.mount("/", FreshStaticFiles(directory=self.assets_dir, html=True), name="assets")
        uvicorn.run(self.app, host=self.host, port=self.port, log_level="warning", access_log=False)


def free_port(host, wanted, tries=20):
    """The wanted port, or the next free one."""
    for port in range(wanted, wanted + tries):
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            probe.bind((host, port))
            return port
        except OSError:
            continue
        finally:
            probe.close()
    return wanted


def lan_address():
    """This laptop's address on the Wi-Fi (no packet is sent)."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("10.255.255.255", 1))
        return probe.getsockname()[0]
    except OSError:
        return None
    finally:
        probe.close()


def build(data_dir=None, llm_url=None, llm_model=None, llm_api_key=None):
    """Wire the app like main.py does, with SimLink instead of the Router Bridge."""
    storage = Storage(data_dir or os.environ.get("APP_DATA_DIR") or os.path.join(APP_DIR, "data"))
    book = RecipeBook.load()
    link = SimLink()
    board = Board(link)
    intent = IntentEngine(
        book,
        url=llm_url or None,                                  # no LLM_URL: rules only
        model=llm_model or "local-model",
        label=llm_model or LLM_LABEL,
        enabled=storage.settings()["llm_enabled"],
        use_brick=False,
        api_key=llm_api_key,
    )
    machine = Machine(board, book, storage, intent)
    return machine, link, intent


def start_threads(machine, link, intent):
    link.start_thread()                                       # the simulated MCU, in real time

    sync_thread = threading.Thread(target=machine.sync_loop, name="board-sync")
    sync_thread.daemon = True
    sync_thread.start()

    def main_loop():                                          # what App.run(user_loop=loop) does
        while True:
            try:
                machine.tick()
            except Exception:
                log.exception("tick failed")
            time.sleep(0.05)

    loop_thread = threading.Thread(target=main_loop, name="main-loop")
    loop_thread.daemon = True
    loop_thread.start()
    intent.start()


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    host = os.environ.get("HOST", "0.0.0.0")
    wanted = int(os.environ.get("PORT", "7000"))
    port = free_port(host, wanted)
    llm_url = os.environ.get("LLM_URL", "").strip()

    machine, link, intent = build(llm_url=llm_url, llm_model=os.environ.get("LLM_MODEL", "").strip(),
                                  llm_api_key=os.environ.get("LLM_API_KEY", "").strip() or None)
    ui = WebUIShim(port=port, host=host)
    webapi.register(ui, machine)
    start_threads(machine, link, intent)

    print("")
    print("  {} dev mode (simulated board)".format(APP_NAME))
    if port != wanted:
        print("  Port {} is busy, using {} instead.".format(wanted, port))
        if sys.platform == "darwin" and wanted == 7000:
            print("  (On a Mac, AirPlay Receiver holds port 7000: System Settings > General >")
            print("   AirDrop & Handoff > AirPlay Receiver. Or just use the port below.)")
    print("  On this laptop:  http://localhost:{}".format(port))
    address = lan_address()
    if address and host in ("0.0.0.0", "::"):
        print("  On your phone:   http://{}:{}   (same Wi-Fi or hotspot)".format(address, port))
    if llm_url:
        print("  LLM:             {}  model {}".format(llm_url, os.environ.get("LLM_MODEL", "(not set)")))
    else:
        print("  LLM:             off (rules only). Set LLM_URL and LLM_MODEL to test with a model.")
    print("  Stop with Ctrl+C.")
    print("")
    sys.stdout.flush()
    ui.run()


if __name__ == "__main__":
    main()
