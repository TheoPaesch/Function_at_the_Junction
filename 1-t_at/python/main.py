"""Entry point on the Arduino UNO Q (App Lab runs this file).

Wiring only: storage, recipes, board link, state machine, REST routes, the
board sync thread, the LLM warm-up thread, then App.run().
The app starts and serves the UI even when the sketch is not running
(the phone then shows "Board offline").
"""

import logging
import os
import threading
import time

from arduino.app_utils import App, Bridge
from arduino.app_bricks.web_ui import WebUI

import webapi
from board import Board, RouterBridgeLink
from core import Machine
from intent import IntentEngine
from recipes import RecipeBook
from storage import Storage

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("main")

DATA_DIR = os.environ.get("APP_DATA_DIR", "/app/data")

ui = WebUI()   # serves /app/assets at http://<board-name>.local:7000

storage = Storage(DATA_DIR)                               # creates /app/data if missing
book = RecipeBook.load()
board = Board(RouterBridgeLink(Bridge))                   # registers mcu_status and mcu_event
intent = IntentEngine(book, enabled=storage.settings()["llm_enabled"], use_brick=True)
machine = Machine(board, book, storage, intent)

webapi.register(ui, machine)                              # every route, before App.run()

sync_thread = threading.Thread(target=machine.sync_loop, name="board-sync")
sync_thread.daemon = True
sync_thread.start()                                       # waits for info(), then pushes parameters

intent.start()                                            # warms the local LLM up in the background

_last_error_log = [0.0]


def loop():
    try:
        machine.tick()
    except Exception:
        if time.monotonic() - _last_error_log[0] > 5.0:   # keep running, but do not flood the log
            _last_error_log[0] = time.monotonic()
            log.exception("tick failed")
    time.sleep(0.05)


App.run(user_loop=loop)
