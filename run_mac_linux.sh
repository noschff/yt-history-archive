#!/bin/bash
cd "$(dirname "$0")"
if ! command -v python3 >/dev/null; then echo "Python 3.10+ is required: https://www.python.org/downloads/"; exit 1; fi
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)' || { echo "Python 3.10 or newer is required."; exit 1; }
python3 -m pip install -U -r requirements.txt >/dev/null 2>&1 || python3 -m pip install --user -U -r requirements.txt
python3 app.py
