"""Interval parsing and epoch-aligned bucketing helpers."""
import re
from datetime import datetime, timedelta

_EPOCH = datetime(1970, 1, 1)
_UNITS = {"m": 60, "h": 3600, "d": 86400}
_PATTERN = re.compile(r"^([1-9][0-9]*)([mhd])$")


def interval_seconds(interval):
    match = _PATTERN.match(str(interval))
    if not match:
        raise ValueError("invalid interval: %r" % (interval,))
    return int(match.group(1)) * _UNITS[match.group(2)]


def bucket_start(ts, seconds):
    delta = ts - _EPOCH
    offset = (delta // timedelta(seconds=seconds)) * seconds
    return _EPOCH + timedelta(seconds=offset)
