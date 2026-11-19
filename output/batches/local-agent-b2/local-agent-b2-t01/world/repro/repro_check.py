import argparse
import json
import sys

sys.path.insert(0, "/app")

from renderer import run_document


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sequence", default="/world/repro/sequence.json")
    args = ap.parse_args()
    with open(args.sequence, encoding="utf-8") as handle:
        sequence = json.load(handle)
    failures = []
    for document in sequence["documents"]:
        result = run_document(document)
        expected = document["expected"]
        if result["reference"] != expected["reference"]:
            failures.append(f"{document['id']}: reference output differs from the recorded outcome")
        if result["rendered"] != result["reference"]:
            failures.append(
                f"{document['id']}: rendered {result['rendered']!r}, reference is {result['reference']!r}"
            )
        bindings = [
            {"name": event["name"], "value": event["value"]}
            for event in result["plan"]
            if event["type"] == "binding"
        ]
        if bindings != expected["bindings"]:
            failures.append(
                f"{document['id']}: binding events are {bindings!r}, expected {expected['bindings']!r}"
            )
    if failures:
        for failure in failures:
            print(f"invariant violated: {failure}")
        return 1
    print("all invariants hold")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
