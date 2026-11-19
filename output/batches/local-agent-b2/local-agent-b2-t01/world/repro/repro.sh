#!/bin/sh
set -eu
exec python3 /world/repro/repro_check.py --sequence /world/repro/sequence.json
