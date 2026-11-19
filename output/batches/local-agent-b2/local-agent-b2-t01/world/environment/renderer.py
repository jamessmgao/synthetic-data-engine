import argparse
import json

from engine import TemplateRuntimeError


class Renderer:
    def render(self, root):
        return "".join(self.values(root.kids))

    def values(self, nodes):
        output = []
        for node in nodes:
            if node.kind == "text":
                output.append(node.value)
            elif node.kind == "let":
                self.check_binding(node)
            elif node.kind == "if":
                if type(node.value) is not bool:
                    raise TemplateRuntimeError("materialized condition is not boolean")
                if node.value:
                    output.extend(self.values(node.kids[0].kids))
            elif node.kind == "inline":
                output.extend(self.values(node.kids))
            else:
                raise TemplateRuntimeError(f"renderer received unmaterialized node {node.kind!r}")
        return output

    def check_binding(self, node):
        if len(node.kids) != 1 or node.kids[0].kind != "value":
            raise TemplateRuntimeError("let node was not materialized")

    def plan(self, root):
        events = []
        self.collect(root.kids, events)
        return events

    def collect(self, nodes, events):
        for node in nodes:
            if node.kind == "text":
                events.append({
                    "type": "text",
                    "value": node.value,
                    "start": node.span.start,
                    "end": node.span.end,
                })
            elif node.kind == "let":
                self.check_binding(node)
                child = node.kids[0]
                events.append({
                    "type": "binding",
                    "name": node.value,
                    "value": child.value,
                    "start": child.span.start,
                    "end": child.span.end,
                })
            elif node.kind == "if":
                if type(node.value) is not bool:
                    raise TemplateRuntimeError("materialized condition is not boolean")
                branch = node.kids[0] if node.value else None
                if branch is not None:
                    self.collect(branch.kids, events)
            elif node.kind == "inline":
                self.collect(node.kids, events)
            else:
                raise TemplateRuntimeError(f"renderer received unmaterialized node {node.kind!r}")


def run_document(document):
    from engine import run_document as compile_document
    from parser import parse

    result = compile_document(document)
    materialized = result["materialized"]
    renderer = Renderer()
    return {
        "reference": result["reference"],
        "rendered": renderer.render(materialized),
        "plan": renderer.plan(materialized),
        "source": parse(document["template"]).source,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("input")
    args = ap.parse_args()
    with open(args.input, encoding="utf-8") as handle:
        document = json.load(handle)
    print(json.dumps(run_document(document), sort_keys=True))


if __name__ == "__main__":
    main()
