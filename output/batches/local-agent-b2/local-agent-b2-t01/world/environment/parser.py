from dataclasses import dataclass, field


@dataclass(frozen=True)
class Span:
    start: int
    end: int


@dataclass
class Node:
    kind: str
    value: object = None
    kids: list = field(default_factory=list)
    span: Span | None = None


@dataclass
class Program:
    kids: list
    macros: dict
    source: str


class TemplateSyntaxError(ValueError):
    pass


class Parser:
    def __init__(self, source):
        self.source = source
        self.pos = 0
        self.macros = {}

    def error(self, message):
        return TemplateSyntaxError(f"{message} at offset {self.pos}")

    def skip_space(self):
        while self.pos < len(self.source) and self.source[self.pos].isspace():
            self.pos += 1

    def peek(self):
        return self.source[self.pos] if self.pos < len(self.source) else ""

    def read_ident(self):
        self.skip_space()
        start = self.pos
        if not self.peek().isalpha() and self.peek() != "_":
            raise self.error("expected a name")
        self.pos += 1
        while self.pos < len(self.source):
            char = self.source[self.pos]
            if char.isalnum() or char == "_":
                self.pos += 1
            else:
                break
        return self.source[start:self.pos]

    def expect(self, text):
        self.skip_space()
        if not self.source.startswith(text, self.pos):
            raise self.error(f"expected {text!r}")
        self.pos += len(text)

    def parse(self):
        kids, stop = self.parse_sequence({"end", "else"}, required=False)
        if stop is not None:
            raise self.error(f"unexpected {stop}")
        return Program(kids, self.macros, self.source)

    def parse_sequence(self, stops, required=True):
        kids = []
        while self.pos < len(self.source):
            index = self.source.find("{{", self.pos)
            if index < 0:
                if self.pos < len(self.source):
                    kids.append(self.text_node(self.source[self.pos:]))
                    self.pos = len(self.source)
                break
            if index > self.pos:
                kids.append(self.text_node(self.source[self.pos:index]))
                self.pos = index
            start = self.pos
            self.pos += 2
            name = self.read_ident()
            self.skip_space()
            if name in stops:
                self.expect("}}")
                return kids, name
            node = self.parse_named_directive(name, start)
            if node is not None:
                kids.append(node)
        if required:
            raise self.error(f"expected one of {sorted(stops)}")
        return kids, None

    def text_node(self, value):
        start = self.pos
        self.pos += len(value)
        return Node("text", value, [], Span(start, self.pos))

    def parse_named_directive(self, name, start):
        if name == "end":
            raise self.error("unexpected end")
        if name == "else":
            raise self.error("unexpected else")
        if name == "macro":
            macro_name = self.read_ident()
            self.expect("}}")
            body_start = self.pos
            body_kids, stop = self.parse_sequence({"end"})
            body = Node("block", None, body_kids, Span(body_start, self.pos))
            if macro_name in self.macros:
                raise self.error(f"macro {macro_name!r} already exists")
            self.macros[macro_name] = body
            return None
        if name == "use":
            target = self.read_ident()
            self.expect("}}")
            return Node("use", target, [], Span(start, self.pos))
        if name == "let":
            binding = self.read_ident()
            self.expect("=")
            expr = self.parse_expr()
            self.expect("}}")
            return Node("let", binding, [expr], Span(start, self.pos))
        if name == "output":
            expr = self.parse_expr()
            self.expect("}}")
            return Node("output", None, [expr], Span(start, self.pos))
        if name == "if":
            condition = self.parse_expr()
            self.expect("}}")
            true_kids, stop = self.parse_sequence({"else", "end"})
            false_kids = []
            if stop == "else":
                false_kids, stop = self.parse_sequence({"end"})
            return Node(
                "if",
                None,
                [
                    condition,
                    Node("block", None, true_kids),
                    Node("block", None, false_kids),
                ],
                Span(start, self.pos),
            )
        raise self.error(f"unknown directive {name!r}")

    def parse_expr(self):
        return self.parse_or()

    def parse_or(self):
        left = self.parse_and()
        while True:
            self.skip_space()
            if self.source.startswith("or", self.pos):
                self.pos += 2
                right = self.parse_and()
                left = Node("or", None, [left, right], self.join_span(left, right))
            else:
                return left

    def parse_and(self):
        left = self.parse_comparison()
        while True:
            self.skip_space()
            if self.source.startswith("and", self.pos):
                self.pos += 3
                right = self.parse_comparison()
                left = Node("and", None, [left, right], self.join_span(left, right))
            else:
                return left

    def parse_comparison(self):
        left = self.parse_additive()
        self.skip_space()
        if self.source.startswith("==", self.pos):
            self.pos += 2
            right = self.parse_additive()
            return Node("eq", None, [left, right], self.join_span(left, right))
        return left

    def parse_additive(self):
        left = self.parse_primary()
        while True:
            self.skip_space()
            if self.peek() == "+":
                self.pos += 1
                right = self.parse_primary()
                left = Node("add", None, [left, right], self.join_span(left, right))
            else:
                return left

    def parse_primary(self):
        self.skip_space()
        char = self.peek()
        if char in "'\"":
            return self.parse_string()
        if char.isdigit():
            start = self.pos
            while self.peek().isdigit():
                self.pos += 1
            value = int(self.source[start:self.pos])
            return Node("num", value, [], Span(start, self.pos))
        if char == "(":
            self.pos += 1
            expr = self.parse_expr()
            self.expect(")")
            return expr
        if char.isalpha() or char == "_":
            start = self.pos
            name = self.read_ident()
            if name == "true":
                return Node("bool", True, [], Span(start, self.pos))
            if name == "false":
                return Node("bool", False, [], Span(start, self.pos))
            return Node("var", name, [], Span(start, self.pos))
        raise self.error("expected an expression")

    def parse_string(self):
        start = self.pos
        quote = self.source[self.pos]
        self.pos += 1
        out = []
        while self.pos < len(self.source):
            char = self.source[self.pos]
            if char == quote:
                self.pos += 1
                return Node("text", "".join(out), [], Span(start, self.pos))
            if char == "\\":
                self.pos += 1
                if self.pos >= len(self.source):
                    raise self.error("unfinished escape")
                escaped = self.source[self.pos]
                out.append({"n": "\n", "t": "\t", "\\": "\\"}.get(escaped, escaped))
                self.pos += 1
            else:
                out.append(char)
                self.pos += 1
        raise self.error("unfinished string")

    def join_span(self, left, right):
        return Span(left.span.start, right.span.end)


def parse(source):
    return Parser(source).parse()
