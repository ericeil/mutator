"""Find mutation sites.

Clojure follows clj-mutate's symbol rules. Go follows mutate4go, including
one-way `*` to `/`. Java follows mutate4java. TypeScript, Rust, Python, and
Kotlin use the same decisions as Java, spelled in that language. TypeScript
also mutates `??` to `||` and `?.` to `.` (removed on a call or index).
JavaScript files use the TypeScript rules. Kotlin also mutates referential
equality, safe calls, elvis, not-null assertions, `in`/`is`, ranges, and
`++`/`--`.
"""

from __future__ import annotations

import sys

from mutator.crapper_link import ensure_crapper
from mutator.kotlin_source import has_parse_error, prepare_kotlin

_BINARY = {
    "+": "-",
    "-": "+",
    "*": "/",
    "/": "*",
    ">": ">=",
    ">=": ">",
    "<": "<=",
    "<=": "<",
    "==": "!=",
    "!=": "==",
    "===": "!==",
    "!==": "===",
    "&&": "||",
    "||": "&&",
    "??": "||",
    "and": "or",
    "or": "and",
}

_BINARY_PARENTS = {
    "binary_expression",
    "binary_operator",
    "comparison_operator",
    "boolean_operator",
}

_UNARY_PARENTS = {"unary_expression", "unary_operator", "not_operator"}

# Kotlin spells a binary operator as a child of one of these nodes, not of
# `binary_expression`. `<` inside `List<Int>` has parent `type_arguments` and
# is not in this set.
_KOTLIN_BINARY_PARENTS = _BINARY_PARENTS | {
    "additive_expression",
    "comparison_expression",
    "conjunction_expression",
    "disjunction_expression",
    "equality_expression",
    "multiplicative_expression",
}

_BOOLEANS = {"true": "false", "false": "true", "True": "False", "False": "True"}

_NUMBERS = {
    "decimal_integer_literal",
    "integer_literal",
    "int_literal",
    "integer",
    "number",
    "num_lit",
}

_CLOJURE_HEAD = {
    "+": ("-", "arithmetic"),
    "-": ("+", "arithmetic"),
    "*": ("/", "arithmetic"),
    "inc": ("dec", "arithmetic"),
    "dec": ("inc", "arithmetic"),
    ">": (">=", "comparison"),
    ">=": (">", "comparison"),
    "<": ("<=", "comparison"),
    "<=": ("<", "comparison"),
    "=": ("not=", "equality"),
    "not=": ("=", "equality"),
    "if": ("if-not", "conditional"),
    "if-not": ("if", "conditional"),
    "when": ("when-not", "conditional"),
    "when-not": ("when", "conditional"),
    "and": ("or", "logical"),
    "or": ("and", "logical"),
    "double": ("int", "coercion"),
    "int": ("double", "coercion"),
    "first": ("second", "seq"),
    "second": ("first", "seq"),
    "filter": ("remove", "seq"),
    "remove": ("filter", "seq"),
    "take": ("drop", "seq"),
    "drop": ("take", "seq"),
    "rest": ("next", "seq"),
    "next": ("rest", "seq"),
    "every?": ("some", "seq"),
    "some": ("every?", "seq"),
    "min": ("max", "numeric"),
    "max": ("min", "numeric"),
    "pos?": ("neg?", "predicate"),
    "neg?": ("pos?", "predicate"),
    "even?": ("odd?", "predicate"),
    "odd?": ("even?", "predicate"),
    "nil?": ("some?", "predicate"),
    "some?": ("nil?", "predicate"),
}


class RawSite:
    def __init__(
        self, line: int, start: int, end: int, original: str, mutant: str, category: str, anchor_line: int = 0
    ):
        self.line = line
        # The line its statement starts on. LCOV marks only that line of a multi-line statement.
        self.anchor_line = anchor_line or line
        self.start = start
        self.end = end
        self.original = original
        self.mutant = mutant
        self.category = category


def _grammar(language: str, path: str) -> str:
    if language == "typescript":
        if path.endswith(".tsx"):
            return "tsx"
        if path.endswith((".js", ".jsx", ".mjs", ".cjs")):
            return "javascript"
        return "typescript"
    if language == "go":
        return "go"
    return language


def _text(data: bytes, node) -> str:
    return data[node.start_byte : node.end_byte].decode("utf-8")


def _category(token: str) -> str:
    if token in {"+", "-", "*", "/"}:
        return "arithmetic"
    if token in {">", ">=", "<", "<="}:
        return "comparison"
    if token in {"==", "!=", "===", "!=="}:
        return "equality"
    if token in {"&&", "||", "??", "and", "or"}:
        return "logical"
    return "arithmetic"


def _binary_parents(language: str) -> set[str]:
    if language == "kotlin":
        return _KOTLIN_BINARY_PARENTS
    return _BINARY_PARENTS


def _unary_parents(language: str) -> set[str]:
    if language == "kotlin":
        return _UNARY_PARENTS | {"prefix_expression"}
    return _UNARY_PARENTS


def _binary_mutant(language: str, token: str) -> str | None:
    if language == "go" and token == "/":
        return None
    if token == "??" and language != "typescript":
        return None
    return _BINARY.get(token)


def _optional_mutant(node) -> str | None:
    """`a?.b` becomes `a.b`. A call or index drops `?.`, so `a?.()` becomes `a()`."""

    parent = node.parent
    if parent is None:
        return None
    host = parent.parent if parent.type == "optional_chain" else parent
    if host is None:
        return None
    if host.type == "member_expression":
        return "."
    if host.type in {"call_expression", "subscript_expression"}:
        return ""
    return None


def _add(found: list[RawSite], node, original: str, mutant: str, category: str) -> None:
    if mutant == original:
        return
    found.append(
        RawSite(
            line=node.start_point[0] + 1,
            start=node.start_byte,
            end=node.end_byte,
            original=original,
            mutant=mutant,
            category=category,
            anchor_line=_statement_line(node),
        )
    )


def _is_statement(node_type: str) -> bool:
    return node_type.endswith("_statement") or node_type.endswith("_declaration")


def _statement_line(node) -> int:
    current = node.parent
    while current is not None:
        if _is_statement(current.type):
            return current.start_point[0] + 1
        current = current.parent
    return node.start_point[0] + 1


# A literal in a TypeScript type, such as `{ ok: true }` in a return type, never runs.
_TYPE_NODES = {
    "type_annotation",
    "type_alias_declaration",
    "interface_declaration",
    "type_arguments",
    "type_parameters",
    "literal_type",
}


def _in_type(node) -> bool:
    current = node.parent
    while current is not None:
        if current.type in _TYPE_NODES:
            return True
        current = current.parent
    return False


def _clojure_head(node, data: bytes) -> str | None:
    for child in node.children:
        if child.type == "sym_lit":
            return _text(data, child)
    return None


def _is_head(node) -> bool:
    parent = node.parent
    if parent is None or parent.type != "list_lit":
        return False
    for child in parent.children:
        if child.type == "sym_lit":
            return child.id == node.id
    return False


def _clojure_skipped(node, data: bytes) -> bool:
    current = node
    while current is not None:
        if any(part in current.type for part in ("comment", "quot", "string", "str_lit", "char_lit")):
            return True
        if current.type == "list_lit":
            head = _clojure_head(current, data)
            if head in {"comment", "quote"}:
                return True
        current = current.parent
    return False


def _clojure_text(node, data: bytes) -> str:
    if node.child_count == 0 or node.type in {"sym_lit", "bool_lit", "num_lit"}:
        return _text(data, node)
    return ""


def _flip01(text: str) -> str:
    if text == "0":
        return "1"
    return "0"


def _clojure_node(node, data: bytes, found: list[RawSite]) -> None:
    if _clojure_skipped(node, data):
        return
    text = _clojure_text(node, data)
    if node.type == "sym_lit" and _is_head(node) and text in _CLOJURE_HEAD:
        mutant, category = _CLOJURE_HEAD[text]
        _add(found, node, text, mutant, category)
        return
    if node.type == "bool_lit" and text in _BOOLEANS:
        _add(found, node, text, _BOOLEANS[text], "boolean")
        return
    if node.type == "num_lit" and text in {"0", "1"}:
        _add(found, node, text, _flip01(text), "constant")


def _visit_clojure(node, data: bytes, found: list[RawSite]) -> None:
    # An explicit stack: a 1,200-deep form must not raise RecursionError.
    stack = [node]
    while stack:
        current = stack.pop()
        _clojure_node(current, data, found)
        stack.extend(reversed(current.children))


def _parent_type(node) -> str:
    if node.parent is None:
        return ""
    return node.parent.type


def _visit_binary(node, token: str, parent: str, language: str, found: list[RawSite]) -> None:
    if parent not in _binary_parents(language):
        return
    mutant = _binary_mutant(language, token)
    if mutant is not None:
        _add(found, node, token, mutant, _category(token))


def _visit_optional(node, token: str, language: str, found: list[RawSite]) -> None:
    if language != "typescript" or token != "?.":
        return
    optional = _optional_mutant(node)
    if optional is not None:
        _add(found, node, "?.", optional, "optional")


def _visit_not(node, token: str, parent: str, language: str, found: list[RawSite]) -> None:
    if token in {"!", "not"} and parent in _unary_parents(language) | {"comparison_operator"}:
        _add(found, node, token, "", "unary")


def _visit_negation(node, token: str, parent: str, language: str, found: list[RawSite]) -> None:
    if token == "-" and parent in _unary_parents(language):
        _add(found, node, token, "", "unary")


def _kotlin_check(node, token: str, found: list[RawSite]) -> None:
    """Flip `in`/`!in` and `is`/`!is`. A `for` loop's `in` is not a check."""

    parent = node.parent
    if parent is None or parent.type != "check_expression":
        return
    if token == "!":
        _add(found, node, "!", "", "check")
        return
    if token not in {"in", "is"}:
        return
    for child in parent.children:
        if child.id == node.id:
            break
        if child.type == "!":
            return
    _add(found, node, token, "!" + token, "check")


def _kotlin_safe_call(node, token: str, found: list[RawSite]) -> None:
    """`a?.b` becomes `a.b`. The call stays; only the safe-call dot changes."""

    if token != "?.":
        return
    parent = node.parent
    if parent is None or parent.type != "navigation_suffix":
        return
    _add(found, node, "?.", ".", "optional")


def _kotlin_assert(node, token: str, found: list[RawSite]) -> None:
    if token != "!!" or _parent_type(node) != "postfix_expression":
        return
    _add(found, node, "!!", "", "assertion")


def _kotlin_increment(node, token: str, found: list[RawSite]) -> None:
    if token not in {"++", "--"}:
        return
    if _parent_type(node) not in {"prefix_expression", "postfix_expression"}:
        return
    mutant = "--" if token == "++" else "++"
    _add(found, node, token, mutant, "arithmetic")


def _kotlin_range(node, token: str, found: list[RawSite]) -> None:
    parent = _parent_type(node)
    if parent == "range_expression" and token in {"..", "..<"}:
        mutant = "..<" if token == ".." else ".."
        _add(found, node, token, mutant, "range")
        return
    if parent == "infix_expression" and token == "until":
        _add(found, node, "until", "..", "range")


def _elvis_parts(parent, operator) -> tuple | None:
    parts = [
        child
        for child in parent.children
        if child.type not in {"line_comment", "multiline_comment"}
    ]
    if len(parts) != 3 or parts[1].id != operator.id:
        return None
    return parts[0], parts[2]


def _kotlin_elvis(node, data: bytes, token: str, found: list[RawSite]) -> None:
    """`a ?: b` can take the default, or refuse null with `!!`.

    Both still compile when `a` is nullable and `b` has the expected type.
    Replacing the two-character operator alone cannot.
    """

    if token != "?:":
        return
    parent = node.parent
    if parent is None or parent.type != "elvis_expression":
        return
    parts = _elvis_parts(parent, node)
    if parts is None:
        return
    left, right = parts
    whole = _text(data, parent)
    _add(found, parent, whole, _text(data, right), "elvis")
    _add(found, parent, whole, f"({_text(data, left)})!!", "elvis")


def _visit_kotlin(node, data: bytes, token: str, found: list[RawSite]) -> None:
    _kotlin_check(node, token, found)
    _kotlin_safe_call(node, token, found)
    _kotlin_assert(node, token, found)
    _kotlin_increment(node, token, found)
    _kotlin_range(node, token, found)
    _kotlin_elvis(node, data, token, found)


def _visit_boolean_token(node, token: str, found: list[RawSite]) -> None:
    if node.type in {"true", "false"} and token in _BOOLEANS:
        _add(found, node, token, _BOOLEANS[token], "boolean")


def _visit_constant(node, token: str, found: list[RawSite]) -> None:
    if node.type in _NUMBERS and token in {"0", "1"}:
        _add(found, node, token, _flip01(token), "constant")


def _visit_bool_lit(node, data: bytes, found: list[RawSite]) -> None:
    if node.type != "bool_lit":
        return
    text = _text(data, node)
    if text in _BOOLEANS:
        _add(found, node, text, _BOOLEANS[text], "boolean")


def _visit_leaf(node, data: bytes, language: str, found: list[RawSite]) -> None:
    # A literal sits inside the same expression as an operator, so these
    # checks are independent. `0` is a child of `a > 0`, not an operator.
    token = _text(data, node)
    parent = _parent_type(node)
    _visit_binary(node, token, parent, language, found)
    _visit_optional(node, token, language, found)
    _visit_not(node, token, parent, language, found)
    _visit_negation(node, token, parent, language, found)
    _visit_boolean_token(node, token, found)
    _visit_constant(node, token, found)
    if language == "kotlin":
        _visit_kotlin(node, data, token, found)


def _visit_node(node, data: bytes, language: str, found: list[RawSite]) -> None:
    if language == "typescript" and _in_type(node):
        return
    if node.child_count == 0:
        _visit_leaf(node, data, language, found)
    else:
        _visit_bool_lit(node, data, found)


def _visit(node, data: bytes, language: str, found: list[RawSite]) -> None:
    # An explicit stack: a 1,200-term expression must not raise RecursionError.
    stack = [node]
    while stack:
        current = stack.pop()
        _visit_node(current, data, language, found)
        stack.extend(reversed(current.children))


def _realign(original: str, site: RawSite) -> RawSite | None:
    """Keep a site whose text is still at that offset in the original file.

    The while-comparison repair slides one `<` a byte to the left and wraps
    the member name. The token moves back before the mutant is applied.
    Anything else the repairs rewrote is not a site.
    """

    data = original.encode("utf-8")
    token = site.original.encode("utf-8")
    if data[site.start : site.end] == token:
        return site
    shifted = site.start + 1
    if (
        token == b"<"
        and site.end == site.start + 1
        and shifted + 1 <= len(data)
        and data[site.start : shifted] == b" "
        and data[shifted : shifted + 1] == b"<"
    ):
        site.start = shifted
        site.end = shifted + 1
        return site
    return None


def _prepare(source: str, language: str, path: str) -> tuple[str, object]:
    crapper = ensure_crapper()
    text = prepare_kotlin(source) if language == "kotlin" else source
    _data, tree = crapper.languages.treesitter.parse(text, _grammar(language, path))
    if language == "kotlin" and has_parse_error(tree):
        print(
            f"{path}: Kotlin parse error; mutation sites in this file may be wrong.",
            file=sys.stderr,
        )
    return text, tree


def discover_raw(source: str, language: str, path: str) -> list[RawSite]:
    """Mutation sites in source order. Offsets are UTF-8 byte offsets."""

    text, tree = _prepare(source, language, path)
    data = text.encode("utf-8")
    found: list[RawSite] = []
    if language == "clojure":
        _visit_clojure(tree.root_node, data, found)
    else:
        _visit(tree.root_node, data, language, found)
    if language == "kotlin":
        found = [site for site in (_realign(source, site) for site in found) if site is not None]
    found.sort(key=lambda site: (site.line, site.start, site.end, site.mutant))
    return found


def apply_site(source: str, start: int, end: int, mutant: str) -> str:
    data = source.encode("utf-8")
    updated = data[:start] + mutant.encode("utf-8") + data[end:]
    return updated.decode("utf-8")
