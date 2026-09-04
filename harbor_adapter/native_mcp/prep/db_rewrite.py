"""Safely rewrite literal PostgreSQL connection settings to runtime env."""

from __future__ import annotations

import ast
import re

_PG_CONNECTORS = frozenset({"asyncpg", "psycopg", "psycopg2"})
_CONNECTION_FIELDS = frozenset(
    {"database", "dbname", "host", "password", "port", "user"}
)
_RUNTIME_ENV = {
    "database": "PGDATABASE",
    "dbname": "PGDATABASE",
    "host": "PGHOST",
    "password": "PGPASSWORD",
    "port": "PGPORT",
    "user": "PGUSER",
}
_PG_ENV_RE = re.compile(r"\bPG(?:DATABASE|HOST|PASSWORD|PORT|USER)\b")
_DSN_RE = re.compile(r"^(postgres(?:ql)?)(?:\+[^:]+)?://", re.IGNORECASE)


def _is_connection_call(node: ast.Call) -> bool:
    func = node.func
    if isinstance(func, ast.Attribute) and func.attr == "connect":
        return isinstance(func.value, ast.Name) and func.value.id in _PG_CONNECTORS
    return isinstance(func, ast.Name) and func.id == "connect"


def _is_engine_call(node: ast.Call) -> bool:
    return isinstance(node.func, ast.Name) and node.func.id == "create_engine"


def _is_runtime_expression(source: str, node: ast.AST) -> bool:
    segment = ast.get_source_segment(source, node) or ""
    if not _PG_ENV_RE.search(segment):
        return False
    # Any hardcoded defaults defeat the runtime
    # contract even though the expression mentions a PG variable.
    return not bool(re.search(r"\.(?:get|getenv)\s*\([^)]*,", segment))


def _env_expression(field: str) -> str:
    env_name = _RUNTIME_ENV[field]
    if field == "port":
        return f'int(__import__("os").environ.get("{env_name}", "5432"))'
    return f'__import__("os").environ["{env_name}"]'


def _dsn_expression() -> str:
    osenv = '__import__("os").environ'
    return (
        f'"postgresql://" + {osenv}["PGUSER"] + ":" + {osenv}["PGPASSWORD"] + "@"'
        f' + {osenv}["PGHOST"] + ":" + {osenv}.get("PGPORT", "5432") + "/"'
        f' + {osenv}["PGDATABASE"]'
    )


def _is_postgres_dsn(value: ast.AST) -> bool:
    return (
        isinstance(value, ast.Constant)
        and isinstance(value.value, str)
        and bool(_DSN_RE.match(value.value))
    )


def _literal_connection_dict(node: ast.AST, source: str) -> bool:
    if isinstance(node, ast.Dict):
        keys = {
            key.value.lower()
            for key in node.keys
            if isinstance(key, ast.Constant) and isinstance(key.value, str)
        }
        return len(keys & _CONNECTION_FIELDS) >= 2
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "dict"
    ):
        keys = {keyword.arg.lower() for keyword in node.keywords if keyword.arg}
        return len(keys & _CONNECTION_FIELDS) >= 2
    return False


def _looks_like_connection_name(name: str) -> bool:
    lowered = name.lower()
    return "db" in lowered or lowered.startswith("pg")


def _replacement_for_field(field: str, value: ast.AST, source: str) -> str | None:
    if field not in _CONNECTION_FIELDS or _is_runtime_expression(source, value):
        return None
    if isinstance(value, (ast.Constant, ast.JoinedStr, ast.Call, ast.Name)):
        return _env_expression(field)
    return None


def _line_offsets(source: str) -> list[int]:
    offsets = [0]
    for line in source.splitlines(keepends=True):
        offsets.append(offsets[-1] + len(line.encode("utf-8")))
    return offsets


def _span(source: str, node: ast.AST, offsets: list[int]) -> tuple[int, int]:
    if node.lineno is None or node.end_lineno is None:
        raise ValueError("AST node has no source location")
    start = offsets[node.lineno - 1] + node.col_offset
    end = offsets[node.end_lineno - 1] + node.end_col_offset
    return start, end


def rewrite_preprocess_db_connections(source: str) -> str:
    """Rewrite only identified PostgreSQL connection arguments.

    The transform is AST-guided and replaces expression spans, preserving all
    unrelated source text. It supports mapping/dict kwargs, direct connect
    kwargs, and literal PostgreSQL DSNs.
    """

    tree = ast.parse(source)
    connection_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and _is_connection_call(node)
    ]
    engine_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and _is_engine_call(node)
    ]
    dict_names: set[str] = set()
    dict_nodes: set[int] = set()

    for call in connection_calls:
        for arg in call.args:
            if isinstance(arg, ast.Starred) and isinstance(arg.value, ast.Name):
                dict_names.add(arg.value.id)
            elif isinstance(arg, ast.Starred) and _literal_connection_dict(
                arg.value, source
            ):
                dict_nodes.add(id(arg.value))
            elif _literal_connection_dict(arg, source):
                dict_nodes.add(id(arg))
        for keyword in call.keywords:
            if keyword.arg and keyword.arg.lower() in _CONNECTION_FIELDS:
                replacement = _replacement_for_field(
                    keyword.arg.lower(), keyword.value, source
                )
                if replacement:
                    dict_nodes.add(id(keyword.value))
            elif keyword.arg is None and isinstance(keyword.value, ast.Name):
                dict_names.add(keyword.value.id)

    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and isinstance(
            node.value, (ast.Dict, ast.Call)
        ):
            if any(
                isinstance(target, ast.Name)
                and (target.id in dict_names or _looks_like_connection_name(target.id))
                for target in node.targets
            ) and _literal_connection_dict(node.value, source):
                dict_nodes.add(id(node.value))
        elif (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and (
                (
                    node.target.id in dict_names
                    or _looks_like_connection_name(node.target.id)
                )
                and isinstance(node.value, (ast.Dict, ast.Call))
                and _literal_connection_dict(node.value, source)
            )
        ):
            dict_nodes.add(id(node.value))

    replacements: list[tuple[int, int, str]] = []
    offsets = _line_offsets(source)

    def add(node: ast.AST, text: str) -> None:
        start, end = _span(source, node, offsets)
        replacements.append((start, end, text))

    for call in connection_calls:
        for keyword in call.keywords:
            field = keyword.arg.lower() if keyword.arg else ""
            replacement = _replacement_for_field(field, keyword.value, source)
            if replacement:
                add(keyword.value, replacement)
        if call.args and _is_postgres_dsn(call.args[0]):
            add(call.args[0], _dsn_expression())

    for call in engine_calls:
        if call.args and _is_postgres_dsn(call.args[0]):
            add(call.args[0], _dsn_expression())

    for node in ast.walk(tree):
        if id(node) not in dict_nodes:
            continue
        if isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values):
                if (
                    isinstance(key, ast.Constant)
                    and isinstance(key.value, str)
                    and value is not None
                ):
                    replacement = _replacement_for_field(
                        key.value.lower(), value, source
                    )
                    if replacement:
                        add(value, replacement)
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "dict"
        ):
            for keyword in node.keywords:
                field = keyword.arg.lower() if keyword.arg else ""
                replacement = _replacement_for_field(field, keyword.value, source)
                if replacement:
                    add(keyword.value, replacement)

    if not replacements:
        return source
    data = source.encode("utf-8")
    for start, end, replacement in sorted(set(replacements), reverse=True):
        data = data[:start] + replacement.encode("utf-8") + data[end:]
    updated = data.decode("utf-8")
    ast.parse(updated)
    return updated


def unresolved_connection_literals(source: str) -> list[int]:
    """Return line numbers of literal PG connection fields left unresolved."""

    tree = ast.parse(source)
    lines: list[int] = []
    connection_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and _is_connection_call(node)
    ]
    dict_nodes: list[ast.AST] = []
    for call in connection_calls:
        for keyword in call.keywords:
            field = keyword.arg.lower() if keyword.arg else ""
            if (
                field in _CONNECTION_FIELDS
                and not _is_runtime_expression(source, keyword.value)
                and isinstance(keyword.value, (ast.Constant, ast.JoinedStr))
            ):
                lines.append(keyword.value.lineno)
        for arg in call.args:
            if _is_postgres_dsn(arg):
                lines.append(arg.lineno)
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and isinstance(
            node.value, (ast.Dict, ast.Call)
        ):
            if any(
                isinstance(target, ast.Name) and _looks_like_connection_name(target.id)
                for target in node.targets
            ) and _literal_connection_dict(node.value, source):
                dict_nodes.append(node.value)
        elif (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and (
                _looks_like_connection_name(node.target.id)
                and isinstance(node.value, (ast.Dict, ast.Call))
                and _literal_connection_dict(node.value, source)
            )
        ):
            dict_nodes.append(node.value)
    for node in dict_nodes:
        if isinstance(node, ast.Dict):
            pairs = zip(node.keys, node.values)
        elif isinstance(node, ast.Call):
            pairs = (
                (ast.Constant(keyword.arg), keyword.value)
                for keyword in node.keywords
                if keyword.arg
            )
        else:
            pairs = ()
        for key, value in pairs:
            if (
                isinstance(key, ast.Constant)
                and isinstance(key.value, str)
                and key.value.lower() in _CONNECTION_FIELDS
                and not _is_runtime_expression(source, value)
                and isinstance(value, (ast.Constant, ast.JoinedStr))
            ):
                lines.append(value.lineno)
    return sorted(set(lines))
