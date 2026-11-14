#!/usr/bin/env bash
# Build dist/1-t_at.zip for App Lab's import.
#
# App Lab needs app.yaml at the root of the ZIP. GitHub's own "Download ZIP"
# does not work for that, because it wraps everything in a top-level folder.
#
#   tools/make_zip.sh
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP="$ROOT/1-t_at"
OUT="$ROOT/dist/1-t_at.zip"
PY="$(command -v python3 || command -v python || true)"

if [ -z "$PY" ]; then
  echo "make_zip.sh needs Python 3 (python3 or python on the PATH)" >&2
  exit 1
fi

mkdir -p "$ROOT/dist"
rm -f "$OUT"

"$PY" - "$APP" "$OUT" <<'EOF'
import os
import sys
import zipfile

app, out = sys.argv[1], sys.argv[2]
skip_dirs = {"__pycache__", "data", ".git"}             # caches and local data stay out
skip_files = {".DS_Store", "Thumbs.db"}
names = []

with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
    for folder, dirs, files in os.walk(app):
        dirs[:] = sorted(d for d in dirs if d not in skip_dirs)
        for name in sorted(files):
            if name in skip_files or name.endswith((".pyc", ".tmp", ".bad")):
                continue
            path = os.path.join(folder, name)
            relative = os.path.relpath(path, app).replace(os.sep, "/")
            archive.write(path, relative)
            names.append(relative)

for needed in ("app.yaml", "python/main.py", "sketch/sketch.ino", "sketch/sketch.yaml", "assets/index.html"):
    if needed not in names:
        sys.exit("missing from the ZIP: " + needed)

print("built {} ({} files, {:.0f} kB), app.yaml at the root".format(
    os.path.relpath(out), len(names), os.path.getsize(out) / 1024.0))
EOF
