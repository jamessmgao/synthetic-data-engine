"""CSV export of raw readings."""


def export_csv(readings, path):
    lines = ["sensor,timestamp,value"]
    for r in readings:
        lines.append("%s,%s,%s" % (r.sensor, r.timestamp.isoformat(),
                                    format_value(r.value)))
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")


def format_value(value):
    return str(value)
