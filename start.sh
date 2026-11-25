#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"
export PRESSFURNACE_MODE=real
python main.py
