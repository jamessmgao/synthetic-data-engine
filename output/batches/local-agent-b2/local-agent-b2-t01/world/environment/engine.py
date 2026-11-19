import argparse
import json

from parser import Node, Span, parse


class TemplateRuntimeError(ValueError):
    pass


class Engine:
    def evaluate(self, node, scope):
        if node.kind == "num":
            return node.value
        if node.kind == "bool":
            return node.value
        if node.kind == "text":
            return node.value
        if node.kind == "value":
            return node.value
        if node.kind == "var":
            if node.value not in scope:
                raise TemplateRuntimeError(f"unknown binding {node.value!r}")
            return scope[node.value]
        if node.kind == "add":
            left = self.evaluate(node.kids[0], scope)
            right = self.evaluate(node.kids[1], scope)
            if isinstance(left, int) and isinstance(right, int):
                return left + right
            return f"{left}{right}"
        if node.kind == "eq":
            return self.evaluate(node.kids[0], scope) == self.evaluate(node.kids[1], scope)
        if node.kind == "and":
            return bool(self.evaluate(node.kids[0], scope)) and bool(self.evaluate(node.kids[1], scope))
        if node.kind == "or":
            return bool(self.evaluate(node.kids[0], scope)) or bool(self.evaluate(node.kids[1], scope))
        raise TemplateRuntimeError(f"cannot evaluate {node.kind!r}")

    def reference(self, program, environment):
        scope = dict(environment)
        return "".join(self.execute(program.kids, scope, program.macros))

    def execute(self, nodes, scope, macros):
        local = dict(scope)
        output = []
        for node in nodes:
            if node.kind == "text":
                output.append(node.value)
            elif node.kind == "output":
                output.append(self.render_value(self.evaluate(node.kids[0], local)))
            elif node.kind == "let":
                local[node.value] = self.evaluate(node.kids[0], local)
            elif node.kind == "if":
                if self.evaluate(node.kids[0], local):
                    output.extend(self.execute(node.kids[1].kids, local, macros))
                else:
                    output.extend(self.execute(node.kids[2].kids, local, macros))
            elif node.kind == "use":
                if node.value not in macros:
                    raise TemplateRuntimeError(f"unknown macro {node.value!r}")
                body = macros[node.value]
                output.extend(self.execute(body.kids, local, macros))
            else:
                raise TemplateRuntimeError(f"cannot execute {node.kind!r}")
        return output

    def render_value(self, value):
        if value is True:
            return "true"
        if value is False:
            return "false"
        return str(value)

    def materialize(self, program, environment):
        worker = Materializer(self)
        kids = worker.nodes(program.kids, dict(environment), program.macros)
        return Node("block", None, kids, Span(0, len(program.source)))


class Materializer:
    def __init__(self, engine):
        self.engine = engine

    def nodes(self, nodes, scope, macros):
        local = dict(scope)
        output = []
        for node in nodes:
            if node.kind == "text":
                output.append(Node(node.kind, node.value, [], node.span))
            elif node.kind == "output":
                value = self.engine.evaluate(node.kids[0], local)
                output.append(Node("text", self.engine.render_value(value), [], node.span))
            elif node.kind == "let":
                value = self.engine.evaluate(node.kids[0], local)
                replacement = Node("value", value, [], node.kids[0].span)
                node.kids = [replacement]
                local[node.value] = value
                output.append(node)
            elif node.kind == "if":
                condition = self.engine.evaluate(node.kids[0], local)
                branch = 1 if condition else 2
                kids = self.nodes(node.kids[branch].kids, local, macros)
                output.append(Node("if", condition, [Node("block", None, kids)], node.span))
            elif node.kind == "use":
                if node.value not in macros:
                    raise TemplateRuntimeError(f"unknown macro {node.value!r}")
                body = macros[node.value]
                kids = self.nodes(body.kids, local, macros)
                output.append(Node("inline", node.value, kids, node.span))
            else:
                raise TemplateRuntimeError(f"cannot materialize {node.kind!r}")
        return output


def run_document(document):
    program = parse(document["template"])
    environment = document.get("environment", {})
    engine = Engine()
    return {
        "reference": engine.reference(program, environment),
        "materialized": engine.materialize(program, environment),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("input")
    args = ap.parse_args()
    with open(args.input, encoding="utf-8") as handle:
        document = json.load(handle)
    result = run_document(document)
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
