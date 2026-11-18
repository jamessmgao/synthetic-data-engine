"""Reading record model."""
from dataclasses import dataclass
from datetime import datetime


class ReadingError(ValueError):
    pass


@dataclass
class Reading:
    sensor: str
    timestamp: datetime
    value: float


def from_dict(payload):
    if not isinstance(payload, dict):
        raise ReadingError("reading must be a JSON object")
    try:
        return Reading(sensor=str(payload["sensor"]),
                       timestamp=datetime.fromisoformat(str(payload["timestamp"])),
                       value=float(payload["value"]))
    except KeyError as exc:
        raise ReadingError("missing field: %s" % exc) from None


def to_dict(reading):
    return {"sensor": reading.sensor,
            "timestamp": reading.timestamp.isoformat(),
            "value": reading.value}
