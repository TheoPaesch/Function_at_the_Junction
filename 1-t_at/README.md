# 1-T_AT

1-T_AT is a cooking water assistant for the Arduino UNO Q: say what you are making ("2 cups of tea", "pasta for 4", "Kartoffeln für 3") and it dispenses exactly the water that dish needs.
Everything runs on the board: the web app for your phone, a small local LLM that understands free text, and the microcontroller that runs the scale and the valve. No cloud, no internet.
After each run it shows the energy you saved compared with the usual overfilled kettle or pot.

We have no load cell and no valve yet, so the microcontroller runs a **virtual scale** and a **virtual valve** behind the same interface the real parts will use. An HX711 on D4/D5 is detected at boot and used; a valve driver on D7 works with no code change.

## Run it without the board

Everything can be rehearsed on a laptop. Dev mode runs the same Python app against a simulation of the sketch.

```bash
pip install fastapi uvicorn
python3 1-t_at/python/dev_server.py
```

Open `http://localhost:7000`. A phone on the same Wi-Fi or hotspot can open `http://<laptop-ip>:7000`; the script prints both addresses. If port 7000 is busy (on a Mac, AirPlay Receiver holds it) the script takes the next free port and says so.

In dev mode the demo panel also shows a "Virtual board" card: the LED matrix screen, the RGB LED colour, the valve state and Knob buttons, so you can see what the board would do.

Tests, both without the board:

```bash
python3 1-t_at/python/selftest.py
```

```bash
tools/hosttest/run.sh
```

`selftest.py` (standard library only) drives the real state machine against the simulated board on a fake clock: the flows, every safety stop, the English and German parser, storage, and a fake LLM server. `tools/hosttest/run.sh` compiles the real `sketch.ino` against stub headers with any C++17 compiler and runs its dispense loop, safety stops and HX711 driver on a simulated board.

To test the free-text parser with a model, point dev mode at any OpenAI-compatible server and name its model:

```bash
LLM_URL=http://localhost:8080 LLM_MODEL=<model-name> python3 1-t_at/python/dev_server.py
```

`http://localhost:8080` is llama.cpp's `llama-server`; for Ollama use `http://localhost:11434`. Without `LLM_URL` dev mode uses the rules only.

## Deploy in App Lab

1. Get the app onto the board, one of three ways:
   - Run `tools/make_zip.sh` and import `dist/1-t_at.zip` in App Lab. The import needs `app.yaml` at the root of the ZIP; GitHub's own "Download ZIP" does not work because it adds a top-level folder.
   - Or create a new app named `1-T_AT` in App Lab and copy the files in.
   - Or copy the `1-t_at` folder to `/home/arduino/ArduinoApps/` on the board.
2. Add the libraries if App Lab asks (they are listed in `sketch/sketch.yaml`).
3. Download the LLM model in the LLM brick's model settings.
4. Press Run.

## Open it on the phone

`http://<board-name>.local:7000`, or the board's IP address (App Lab's console prints the network URL; some Android phones cannot open `.local` names).

If the venue Wi-Fi stops devices from reaching each other, put the board and the phone on a phone hotspot (set the board's Wi-Fi in App Lab). The page can be added to the home screen.

## How a run goes

| Step | What happens |
| --- | --- |
| Home | Type what you are making, or tap a recipe tile with its −/+ portions. Free text gets a confirm card: "Pasta · 4 people · 2.0 L water", with how it was understood. |
| Vessel | "Put the empty kettle on the plate." Continues by itself when the weight rises by 150 g and is stable, then zeroes the scale. "It is on the plate" or a Knob press also continues. |
| Ingredient | Potatoes only: put them in, the grams and the water update live. "Done" or a Knob press, once at least 50 g are in. |
| Ready | "Adding 1,300 ml", a 3-second countdown with Cancel, then the water starts. No extra tap. |
| Dispensing | Live progress and a big STOP. The board closes the valve a little early (it learns how much water is still in flight). |
| Done | Water added, water not heated, Wh, cost and boiling time saved, today's totals, one tip. Back to Home after 60 s or with "New". |
| Error | A plain reason and "Try again": no water, timeout, vessel removed, link lost, tilt, stopped, board offline. |

Recipes live in `python/recipes.json`; the UI, the parser and the maths all read it.

| Recipe | Vessel | Water | Usual habit (baseline) |
| --- | --- | --- | --- |
| Tea | kettle | 250 ml per cup | 2.0 × needed (survey: tea drinkers boil about double) |
| Instant noodles | kettle | 500 ml per pack | 2.0 × needed (assumed) |
| Pasta | pot | 5 ml per g, at least 1,000 ml; 100 g per person | 10 ml per g (1 L per 100 g) |
| Potatoes | pot | 1 ml per g + 300 ml, from the weighed potatoes | 2.0 × needed (assumed) |

Synonyms are in `recipes.json` too. Two bits of syntax keep them short: `*tee` or `kartoffel*` is a wildcard for German compounds (Pfefferminztee, Kartoffelsalat), and `instant+nudeln` means both words anywhere in the text.

## Demo script (about two minutes, virtual scale)

1. Open the page on the phone. On the board the drop pulses on the LED matrix.
2. Type "Kartoffeln für 3" and tap Go. While the local model reads it the matrix shows the thinking dots; the card then says "Potatoes · 3 people · about 1.1 L water" and how it was understood. Tap Start.
3. "Put the empty pot on the plate": tap "Place pot (virtual)", or turn the Knob at least three clicks. The scale zeroes itself.
4. "Put the potatoes in": turn the Knob (50 g per click) or tap "+250 g". Grams and water update live. Press the Knob or tap Done.
5. The countdown runs on the phone and on the matrix, the LED turns blue, and the water level rises on the matrix while the ring fills on the phone.
6. Done: check mark, green LED, three rising tones, and the phone shows what was saved.
7. Safety: start a tea and press the Knob (or tap STOP) while the water runs. The valve closes at once, the LED turns red and the matrix shows the X. Setting the virtual flow to 0 in Settings shows the "no water" stop instead.

## Wiring

- **Modulino Knob and Modulino Buzzer**: chained on the Qwiic connector. Both are optional and detected at boot. With the virtual scale each Knob click adds or removes 50 g; a press means next / done, and it stops the water while the valve is open.
- **Tilt switch (KY-017), optional**: between D6 and GND. Set `#define USE_TILT 1` in `sketch/sketch.ino`. Whatever it reads at boot counts as normal.
- **HX711 load-cell amplifier (future)**: DT to D4, SCK to D5, VCC to 3.3 V, GND to GND. The board's I/O is 3.3 V.
- **Valve (future)**: a logic-level MOSFET with its gate on D7 (HIGH = open) switching a normally-closed 12 V solenoid valve. Use a separate 12 V supply, a shared ground and a flyback diode across the valve. Never a latching relay: the valve must close whenever D7 goes low or the board loses power. A pull-down resistor on the gate keeps it closed while the board boots.

With the virtual scale and a real valve on D7 the app works as a timed dispenser: set the virtual flow in Settings to the valve's real flow in g/s.

## Calibration (real scale)

Open Settings on the phone. The "Scale calibration" section appears when an HX711 was found.

1. Take everything off the plate and tap **Zero**.
2. Put a known weight on the plate (a full 500 ml bottle is about 500 g plus the bottle).
3. Enter its weight in grams and tap **Calibrate**.

The factor (counts per gram) is stored in `/app/data/settings.json` and sent back to the microcontroller at every boot.

## Troubleshooting

- **The LLM runs out of memory**: lower `LLAMA_ARG_CTX_SIZE` in `app.yaml`, or switch the LLM off in Settings, or remove the `arduino:llm` brick from `app.yaml`. The app then uses the rules only; nothing else changes.
- **The LLM is slow or shows "failed"**: free text still works, through the rules. A request never waits longer than 15 s for the model, and a failed model is tried again by itself. Settings shows its status, the reason and the last answer time.
- **"Board offline"**: the sketch is not running or the Bridge is not up yet. Check App Lab's console and press Run again. The web app keeps working and reconnects by itself.
- **No Knob or Buzzer**: they are detected once at boot. Check the Qwiic cables and restart the app. Settings shows what was found.
- **The phone cannot open the page**: try the IP address instead of the `.local` name, and use a hotspot if the Wi-Fi isolates devices.
- **Dev mode says port 7000 is busy**: use the port it prints, or set `PORT`.

## Security note

There is no login: anyone on the same network can start and stop the water. Run it on a private hotspot. Add a PIN before you connect a real valve.

## CI

`.github/workflows/ci.yml` runs on every push and pull request, two jobs:

1. `python3 1-t_at/python/selftest.py`, the sketch host test and the import ZIP build.
2. The sketch compiled with `arduino-cli` for `arduino:zephyr:unoq`, against Arduino's real core and libraries.

To compile the sketch on your own machine, install `arduino-cli` and run the same four commands:

```bash
arduino-cli core update-index
```

```bash
arduino-cli core install arduino:zephyr
```

```bash
arduino-cli lib install "Arduino_Modulino@0.7.0"
```

```bash
arduino-cli compile --fqbn arduino:zephyr:unoq 1-t_at/sketch
```

The sketch was last compiled this way with core 1.1.0: about 149 kB of flash (18%) and 60 kB of RAM (22%), no warnings in our code.

## Where things are

| File | What |
| --- | --- |
| `app.yaml` | App Lab app: the WebUI and LLM bricks |
| `python/main.py` | Entry point on the board: wiring only |
| `python/core.py` | The flow state machine and everything the REST API needs |
| `python/board.py` | The link to the microcontroller: `RouterBridgeLink` (real) and `SimLink` (simulation) |
| `python/intent.py` | Free text to order: local LLM first, rules always |
| `python/recipes.py`, `recipes.json` | Recipes and water maths |
| `python/energy.py` | Energy and savings maths |
| `python/storage.py` | Settings and history in `/app/data`, atomic writes |
| `python/webapi.py` | The REST routes |
| `python/dev_server.py`, `selftest.py` | Dev mode and tests |
| `sketch/sketch.ino` | The microcontroller: scale, valve, safety, LED matrix, Knob, Buzzer |
| `assets/` | The phone UI: plain HTML, CSS and JavaScript, no external files |

## Assumptions

- **Parser**: the rules always run and numbers written in the text ("for 4", "300 g") come from them. When the keywords in the text point to one recipe, that decides; the model decides when the rules find nothing, or keywords of two recipes in different places ("tea and pasta"). "cup", "mug" and "Tasse" are also measures: "2 cups of noodles" is noodles, "2 cups" alone is tea. German "Nudeln" alone means pasta; English "noodles" alone means instant noodles.
- **LLM readiness**: the model counts as ready only after it gave a usable answer (a JSON order) within 15 s. One request never waits longer than 15 s for it in total, whatever fallbacks it goes through. After two late or unusable answers in a row the model is marked failed and tried again by itself, after 1 minute at first and then with pauses that double up to 15 minutes.
- **Vessel step**: the reference is the lowest weight seen since the step began, so taking an old kettle off and putting an empty one on is detected too.
- **Safety beyond the brief**: Python does not start the water when the vessel left the plate after the tare, and the board refuses `dispense` in that case as well. While water runs, a Knob press stops it on the board itself.
- **Who shows a stop**: when the board stops the water itself (timeout, no flow, vessel removed, link lost, tilt, Knob press) it shows the X, goes red and beeps on its own, because Python may be gone. After Python's `stop()` it only closes the valve and reports `error_stopped`; Python then sets the screen and the beep, so a cancel shows the idle drop and never an error.
- **Odd numbers**: NaN, Infinity and oversized numbers in a request are treated as missing. The virtual plate holds at most 50 kg.
- **Lead time**: the learning step is limited to 0.3 s per run so one odd run cannot throw it off.
- **Knob faults**: the Modulino library waits up to 100 ms for a Knob that does not answer and then reports position 0. The sketch treats such a slow read as failed, ignores it, stays off the I2C bus for 3 s (and completely while the valve is open), and ignores jumps of more than 30 clicks.
- **Events**: an error event carries the grams dispensed before the stop. The board repeats its `boot` event every 2 s until Python has asked for `info()`, so a missed event cannot leave it without its parameters.
- **Knob with a real scale**: turning it changes the portions before the water starts (not when the user gave a weight).
- **Timeouts**: the done screen returns to Home after 60 s, an error after 2 minutes.
- **Savings**: the baseline is worked out from the needed amount and compared with what was really dispensed. History keeps the last 500 runs; the all-time totals are kept separately. "Today" follows the board's clock.
- **Calibration**: `POST /api/calibrate` with `known_g` 0 zeroes the empty plate; with a weight it calibrates.
- **Read aloud** is a setting of each phone (stored in the browser), not of the board.
- **API extras**: the snapshot holds a few fields beyond the documented ones (`hint`, `phase`, `progress`, `sim`), and POST answers add `ok` and `note`. The app name comes from `/api/state`; the UI never hard-codes it.
- **Extra files**: `python/webapi.py` holds the routes so `main.py` and `dev_server.py` share them, and `assets/icon-192.png` is there because iPhones ignore SVG home-screen icons (`tools/make_icon.py` draws it).
- **Virtual vessel weights**: 900 g for a kettle, 1,500 g for a pot.
