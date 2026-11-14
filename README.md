# Function_at_the_Junction
we be coding at junction (claude is coding ngl)

## 1-T_AT

A cooking water assistant for the Arduino UNO Q: it dispenses only the water a dish needs, so nobody heats more than necessary. Everything runs on the board, with no cloud.

The app, how to run it without the board and how to deploy it are in **[1-t_at/README.md](1-t_at/README.md)**.

| Path | What |
| --- | --- |
| `1-t_at/` | The App Lab app (Python, sketch, phone UI) |
| `tools/make_zip.sh` | Builds `dist/1-t_at.zip` for App Lab's import, with `app.yaml` at the ZIP root |
| `tools/hosttest/` | Runs the real sketch on a laptop against stub headers |
| `tools/make_icon.py` | Draws the PNG home-screen icon |
| `.github/workflows/ci.yml` | CI: self-test, sketch host test, sketch compile |

Try it on a laptop:

```bash
pip install fastapi uvicorn
python3 1-t_at/python/dev_server.py
```
