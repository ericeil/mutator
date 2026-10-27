"""Prepare Kotlin source for tree-sitter without moving line numbers.

tree-sitter-kotlin, loaded through tree-sitter-language-pack, misses a few
constructs and misreads a few others. The same repairs crapper and dryer
apply before parsing are done here, so a mutation site's byte offset is
still an offset in the original file. Each edit is the same length and
keeps every newline:

* A context receiver (`context(Foo)` before a declaration, or a function type
  `context(Foo) () -> R`) is replaced with spaces. The bad parse turns
  `object Name { ... }` into an expression, so every function in the object
  disappears. A call named `context` — `.context(ctx)` or `val x = context(1)`
  — is left alone. Strings, comments, and character literals are not rewritten.
* `\\$` inside a regular string is two spaces. The grammar treats `$` as
  interpolation, and a long string of escaped dollars swallows the rest of
  the file. Raw strings do not use backslash escapes, so they are left alone.
* A statement that starts with the identifier `where` gets a semicolon in
  the indent before it, and only when that removes an ERROR node. `where` is
  also the type-constraint keyword; `where T : Bound` is not a statement.
* `while (i < out.size ...)` is parenthesized as `while (i<(out).size ...)`
  when the grammar reads `<` as a type argument, and only when that removes
  an ERROR node. A comparison that already parses, including `by < 0 ->`,
  is not changed.
"""

from __future__ import annotations

from mutator.crapper_link import ensure_crapper


def parse(source: str, language: str):
    """Parse with tree-sitter-language-pack, the same way crapper does."""

    crapper = ensure_crapper()
    return crapper.languages.treesitter.parse(source, language)

# Words that may sit between a context-receiver clause and the declaration.
_MODIFIERS = {
    "public",
    "private",
    "protected",
    "internal",
    "override",
    "open",
    "abstract",
    "final",
    "suspend",
    "inline",
    "tailrec",
    "operator",
    "infix",
    "external",
    "expect",
    "actual",
    "inner",
    "annotation",
    "crossinline",
    "noinline",
}
_DECLARATIONS = {"fun", "val", "var", "get", "set"}


def prepare_kotlin(source: str) -> str:
    """Return source the Kotlin grammar can parse, with line numbers intact."""

    prepared = _prepare_text(source)
    if "where" not in prepared and "while" not in prepared:
        return prepared
    _data, tree = parse(prepared, "kotlin")
    repaired = _repair_where_statements(prepared, tree)
    if repaired is not None:
        prepared = repaired
        _data, tree = parse(prepared, "kotlin")
    repaired = _repair_while_comparisons(prepared, tree)
    if repaired is not None:
        prepared = repaired
    return prepared


def _prepare_text(source: str) -> str:
    """Blank escaped dollars and context receivers. Both keep the length."""

    if "context" not in source and "\\$" not in source:
        return source
    chars = list(source)
    if "\\$" in source:
        _blank_escaped_dollars(chars)
    text = "".join(chars)
    if "context" in text:
        text = blank_context_receivers(text)
    return text


def blank_context_receivers(source: str) -> str:
    """Replace context-receiver clauses with spaces of the same length.

    Newlines stay, so a function's line numbers are the lines in the file.
    Strings and comments are left alone, including a call named `context`.
    """

    if "context" not in source:
        return source
    chars = list(source)
    i = 0
    while i < len(chars):
        i = _scan_context(chars, i)
    return "".join(chars)


def _scan_context(chars: list[str], i: int) -> int:
    skipped = _skip_trivia(chars, i)
    if skipped != i:
        return skipped
    if _is_word(chars, i, "context"):
        keyword_end = i + len("context")
        open_paren = _skip_ws(chars, keyword_end)
        if open_paren < len(chars) and chars[open_paren] == "(":
            close_paren = _match_paren(chars, open_paren)
            if close_paren is not None and _introduces_receiver(chars, i, close_paren + 1):
                _blank(chars, i, close_paren)
                return close_paren + 1
    return i + 1


def _introduces_receiver(chars: list[str], keyword_at: int, i: int) -> bool:
    """True for `context(T) fun` and for a function type `context(T) () -> R`.

    A call named `context` is left alone. `.context(ctx)` is a method, and
    `val x = context(1)` is a call even when a `fun` follows it.
    """

    if _is_function_type(chars, i):
        return True
    if not _at_declaration_position(chars, keyword_at):
        return False
    i = _skip_declaration_prefix(chars, i)
    if i >= len(chars):
        return False
    word, _end = _read_word(chars, i)
    return word in _DECLARATIONS


def _is_function_type(chars: list[str], i: int) -> bool:
    i = _skip_declaration_prefix(chars, i)
    if i >= len(chars) or chars[i] != "(":
        return False
    close_paren = _match_paren(chars, i)
    if close_paren is None:
        return False
    arrow = _skip_ws(chars, close_paren + 1)
    return arrow + 1 < len(chars) and chars[arrow] == "-" and chars[arrow + 1] == ">"


def _at_declaration_position(chars: list[str], i: int) -> bool:
    """True when `context` starts a declaration, not an expression."""

    line_start = i
    while line_start > 0 and chars[line_start - 1] not in "\n\r":
        line_start -= 1
    cursor = line_start
    while cursor < i:
        while cursor < i and chars[cursor] in " \t":
            cursor += 1
        if cursor >= i:
            return True
        if chars[cursor] == "@":
            end = _skip_annotation(chars, cursor)
            if end <= cursor:
                return False
            cursor = end
            continue
        word, end = _read_word(chars, cursor)
        if word in _MODIFIERS and end <= i:
            cursor = end
            continue
        return False
    return True


def _skip_declaration_prefix(chars: list[str], i: int) -> int:
    while True:
        i = _skip_ws(chars, i)
        if i >= len(chars):
            return i
        if chars[i] == "@":
            i = _skip_annotation(chars, i)
            continue
        word, end = _read_word(chars, i)
        if word not in _MODIFIERS:
            return i
        i = end


def _skip_annotation(chars: list[str], i: int) -> int:
    i += 1
    if i < len(chars) and chars[i] == "[":
        close = _match_closer(chars, i, "[", "]")
        return i if close is None else close + 1
    while i < len(chars) and (chars[i].isalnum() or chars[i] in "._:"):
        i += 1
    i = _skip_ws(chars, i)
    if i < len(chars) and chars[i] == "(":
        close = _match_paren(chars, i)
        return i if close is None else close + 1
    return i


def _skip_trivia(chars: list[str], i: int) -> int:
    if i + 1 < len(chars) and chars[i] == "/" and chars[i + 1] == "/":
        return _skip_line_comment(chars, i)
    if i + 1 < len(chars) and chars[i] == "/" and chars[i + 1] == "*":
        return _skip_block_comment(chars, i)
    if chars[i] == '"':
        return _skip_string(chars, i)
    if chars[i] == "'":
        return _skip_char(chars, i)
    return i


def _skip_line_comment(chars: list[str], i: int) -> int:
    newline = _find_char(chars, "\n", i)
    return len(chars) if newline is None else newline


def _skip_block_comment(chars: list[str], i: int) -> int:
    i += 2
    depth = 1
    while i < len(chars) and depth:
        if i + 1 < len(chars) and chars[i] == "/" and chars[i + 1] == "*":
            depth += 1
            i += 2
            continue
        if i + 1 < len(chars) and chars[i] == "*" and chars[i + 1] == "/":
            depth -= 1
            i += 2
            continue
        i += 1
    return i


def _skip_string(chars: list[str], i: int) -> int:
    if _starts_with(chars, i, '"""'):
        return _skip_raw_string(chars, i)
    i += 1
    while i < len(chars):
        if chars[i] == "\\":
            i += 2
            continue
        if chars[i] == "$":
            i = _skip_interpolation(chars, i)
            continue
        if chars[i] == '"':
            return i + 1
        if chars[i] == "\n":
            return i
        i += 1
    return i


def _skip_raw_string(chars: list[str], i: int) -> int:
    i += 3
    while i < len(chars):
        if _starts_with(chars, i, '"""'):
            return i + 3
        if chars[i] == "$":
            i = _skip_interpolation(chars, i)
            continue
        i += 1
    return i


def _skip_interpolation(chars: list[str], i: int) -> int:
    if i + 1 < len(chars) and chars[i + 1] == "{":
        close = _match_closer(chars, i + 1, "{", "}")
        return i + 1 if close is None else close + 1
    if i + 1 < len(chars) and (chars[i + 1].isalpha() or chars[i + 1] == "_"):
        i += 2
        while i < len(chars) and (chars[i].isalnum() or chars[i] == "_"):
            i += 1
        return i
    return i + 1


def _skip_char(chars: list[str], i: int) -> int:
    if i + 2 < len(chars) and chars[i + 1] == "\\" and chars[i + 2] == "'":
        return i + 4 if i + 3 < len(chars) and chars[i + 3] == "'" else i + 1
    if i + 2 < len(chars) and chars[i + 1] != "'" and chars[i + 1] != "\n" and chars[i + 2] == "'":
        return i + 3
    if i + 1 < len(chars) and chars[i + 1] == "\\":
        end = i + 2
        while end < len(chars) and chars[end] != "'" and chars[end] != "\n":
            end += 1
        return end + 1 if end < len(chars) and chars[end] == "'" else i + 1
    return i + 1


def _match_paren(chars: list[str], i: int) -> int | None:
    return _match_closer(chars, i, "(", ")")


def _match_closer(chars: list[str], i: int, open_char: str, close_char: str) -> int | None:
    depth = 0
    while i < len(chars):
        skipped = _skip_trivia(chars, i)
        if skipped != i:
            i = skipped
            continue
        if chars[i] == open_char:
            depth += 1
        elif chars[i] == close_char:
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return None


def _skip_ws(chars: list[str], i: int) -> int:
    while i < len(chars) and chars[i] in " \t\r\n":
        i += 1
    return i


def _read_word(chars: list[str], i: int) -> tuple[str, int]:
    if i >= len(chars) or not (chars[i].isalpha() or chars[i] == "_"):
        return "", i
    end = i + 1
    while end < len(chars) and (chars[end].isalnum() or chars[end] == "_"):
        end += 1
    return "".join(chars[i:end]), end


def _is_word(chars: list[str], i: int, word: str) -> bool:
    if not _starts_with(chars, i, word):
        return False
    before_ok = i == 0 or not (chars[i - 1].isalnum() or chars[i - 1] == "_")
    after = i + len(word)
    after_ok = after >= len(chars) or not (chars[after].isalnum() or chars[after] == "_")
    return before_ok and after_ok


def _starts_with(chars: list[str], i: int, text: str) -> bool:
    return chars[i : i + len(text)] == list(text)


def _find_char(chars: list[str], needle: str, i: int) -> int | None:
    while i < len(chars):
        if chars[i] == needle:
            return i
        i += 1
    return None


def _blank(chars: list[str], start: int, end: int) -> None:
    for index in range(start, end + 1):
        if chars[index] not in "\n\r":
            chars[index] = " "


def _blank_escaped_dollars(chars: list[str]) -> None:
    """Turn `\\$` in a regular string into spaces.

    In a regular string `\\$` is a literal dollar. The grammar treats `$` as
    the start of interpolation, and a long string full of escaped dollars
    (a script embedded in Kotlin) then swallows the rest of the file.
    Raw strings do not use backslash escapes, so they are left alone.
    """

    i = 0
    while i < len(chars):
        skipped = _skip_trivia_except_string(chars, i)
        if skipped != i:
            i = skipped
            continue
        if chars[i] == '"':
            if _starts_with(chars, i, '"""'):
                i = _skip_raw_string(chars, i)
                continue
            i = _blank_dollars_in_string(chars, i)
            continue
        i += 1


def _skip_trivia_except_string(chars: list[str], i: int) -> int:
    if i + 1 < len(chars) and chars[i] == "/" and chars[i + 1] == "/":
        return _skip_line_comment(chars, i)
    if i + 1 < len(chars) and chars[i] == "/" and chars[i + 1] == "*":
        return _skip_block_comment(chars, i)
    if chars[i] == "'":
        return _skip_char(chars, i)
    return i


def _blank_dollars_in_string(chars: list[str], i: int) -> int:
    i += 1
    while i < len(chars):
        if chars[i] == "\\":
            if i + 1 < len(chars) and chars[i + 1] == "$":
                chars[i] = " "
                chars[i + 1] = " "
            i += 2
            continue
        if chars[i] == "$":
            i = _skip_interpolation(chars, i)
            continue
        if chars[i] == '"':
            return i + 1
        if chars[i] == "\n":
            return i
        i += 1
    return i


def _separate_where_statements(chars: list[str], spans: list[tuple[int, int]]) -> bool:
    """Put a semicolon before a statement that starts with the identifier `where`.

    `where` is also the type-constraint keyword. At the start of a line the
    grammar can keep it as a constraint and then fail on `where.foo` or
    `where to x`. A constraint is `where T : Bound`, which is not rewritten.
    The semicolon is inserted only on a line that already has a parse error,
    so a `where.foo` the grammar already accepts is left alone.
    """

    changed = False
    i = 0
    line_start = 0
    while i < len(chars):
        skipped = _skip_trivia(chars, i)
        if skipped != i:
            line_start = _line_start_after(chars, i, skipped, line_start)
            i = skipped
            continue
        if chars[i] == "\n":
            line_start = i + 1
            i += 1
            continue
        if _is_word(chars, i, "where") and _only_indent(chars, line_start, i):
            line_end = _find_char(chars, "\n", i)
            line_end = len(chars) - 1 if line_end is None else line_end
            if (
                _where_is_a_statement(chars, i + len("where"))
                and i > line_start
                and _span_overlaps(chars, line_start, line_end, spans)
            ):
                chars[i - 1] = ";"
                changed = True
            i += len("where")
            continue
        i += 1
    return changed


def _repair_where_statements(source: str, tree) -> str | None:
    spans = _error_byte_spans(tree)
    if not spans or "where" not in source:
        return None
    chars = list(source)
    if not _separate_where_statements(chars, spans):
        return None
    repaired = "".join(chars)
    _data, repaired_tree = parse(repaired, "kotlin")
    if _error_count(repaired_tree) < _error_count(tree):
        return repaired
    return None


def _line_start_after(chars: list[str], start: int, end: int, line_start: int) -> int:
    last = line_start
    for index in range(start, end):
        if chars[index] == "\n":
            last = index + 1
    return last


def _only_indent(chars: list[str], start: int, end: int) -> bool:
    return all(chars[index] in " \t" for index in range(start, end))


def _where_is_a_statement(chars: list[str], i: int) -> bool:
    nxt = _skip_ws(chars, i)
    if nxt < len(chars) and chars[nxt] == ".":
        return True
    if not _is_word(chars, nxt, "to"):
        return False
    after = _skip_ws(chars, nxt + len("to"))
    return after >= len(chars) or chars[after] != ":"


def _error_count(tree) -> int:
    count = 0
    stack = [tree.root_node]
    while stack:
        current = stack.pop()
        if current.type == "ERROR":
            count += 1
        stack.extend(current.children)
    return count


def _error_byte_spans(tree) -> list[tuple[int, int]]:
    spans = []
    stack = [tree.root_node]
    while stack:
        current = stack.pop()
        if current.type == "ERROR":
            spans.append((current.start_byte, current.end_byte))
        stack.extend(current.children)
    return spans


def _repair_while_comparisons(source: str, tree) -> str | None:
    """Parenthesize `while (i < out.size ...)` when the grammar calls `<` a type argument.

    The rewrite is kept only when it removes an ERROR node. A comparison that
    already parses, including `by < 0 ->`, is not changed.
    """

    spans = _error_byte_spans(tree)
    if not spans or "while" not in source:
        return None
    chars = list(source)
    changed = False
    i = 0
    while i < len(chars):
        skipped = _skip_trivia(chars, i)
        if skipped != i:
            i = skipped
            continue
        if _is_word(chars, i, "while"):
            open_paren = _skip_ws(chars, i + len("while"))
            if open_paren < len(chars) and chars[open_paren] == "(":
                close = _match_paren(chars, open_paren)
                if close is not None and _span_overlaps(chars, i, close, spans):
                    if _rewrite_while_condition(chars, open_paren + 1, close):
                        changed = True
                i = (close + 1) if close is not None else open_paren + 1
                continue
        i += 1
    if not changed:
        return None
    repaired = "".join(chars)
    _data, repaired_tree = parse(repaired, "kotlin")
    if _error_count(repaired_tree) < _error_count(tree):
        return repaired
    return None


def _span_overlaps(chars: list[str], start: int, end: int, spans: list[tuple[int, int]]) -> bool:
    start_byte = len("".join(chars[:start]).encode("utf-8"))
    end_byte = start_byte + len("".join(chars[start : end + 1]).encode("utf-8"))
    return any(span_start < end_byte and span_end > start_byte for span_start, span_end in spans)


def _rewrite_while_condition(chars: list[str], start: int, end: int) -> bool:
    """Replace ` < name.` with `<(name).` inside one while condition.

    Both sides of `<` are spaces in the source this repairs, and the
    replacement has the same length, so later lines stay put.
    """

    changed = False
    i = start
    while i < end:
        skipped = _skip_trivia(chars, i)
        if skipped != i:
            i = skipped
            continue
        if (
            chars[i] == "<"
            and i > start
            and chars[i - 1] == " "
            and i + 1 < end
            and chars[i + 1] == " "
        ):
            name_end = _identifier_end(chars, i + 2, end)
            if name_end is not None and name_end < end and chars[name_end] == ".":
                ident = "".join(chars[i + 2 : name_end])
                replacement = "<(" + ident + ")"
                chars[i - 1 : name_end] = list(replacement)
                changed = True
                i = i - 1 + len(replacement)
                continue
        i += 1
    return changed


def _identifier_end(chars: list[str], i: int, end: int) -> int | None:
    if i >= end or not (chars[i].isalpha() or chars[i] == "_"):
        return None
    while i + 1 < end and (chars[i + 1].isalnum() or chars[i + 1] == "_"):
        i += 1
    return i + 1


def has_parse_error(tree) -> bool:
    """True for an ERROR node, not the grammar's missing automatic semicolon.

    tree-sitter-kotlin marks some valid one-line members as
    `MISSING _automatic_semicolon`. That sets `has_error` without an ERROR
    node, and the functions are still in the tree.
    """

    stack = [tree.root_node]
    while stack:
        current = stack.pop()
        if current.type == "ERROR" or current.is_error:
            return True
        stack.extend(current.children)
    return False

