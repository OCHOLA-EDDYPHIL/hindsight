"""Export or compare a canonical CockroachDB schema and role manifest."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

import psycopg
from psycopg import sql

from hindsight.db import database_url


ROOT = Path(__file__).resolve().parents[1]
VIEW_DDL_PATTERN = re.compile(r"^\s*CREATE(?:\s+OR\s+REPLACE)?\s+VIEW\b", re.IGNORECASE)
DOLLAR_QUOTE_PATTERN = re.compile(r"\$(?:[A-Za-z_][A-Za-z0-9_]*)?\$")


def _normalize_view_sql(value: str) -> str:
    if not VIEW_DDL_PATTERN.match(value):
        return value

    normalized: list[str] = []
    pending_space = False
    index = 0
    while index < len(value):
        character = value[index]
        if character.isspace():
            pending_space = True
            index += 1
            continue
        if value.startswith("--", index) or value.startswith("/*", index):
            return value
        if pending_space and normalized:
            normalized.append(" ")
        pending_space = False

        if character in {"'", '"'}:
            quote = character
            end = index + 1
            while end < len(value):
                if value[end] == "\\" and quote == "'" and end + 1 < len(value):
                    end += 2
                    continue
                if value[end] != quote:
                    end += 1
                    continue
                if end + 1 < len(value) and value[end + 1] == quote:
                    end += 2
                    continue
                end += 1
                break
            normalized.append(value[index:end])
            index = end
            continue

        dollar_quote = DOLLAR_QUOTE_PATTERN.match(value, index)
        if dollar_quote is not None:
            delimiter = dollar_quote.group(0)
            end = value.find(delimiter, dollar_quote.end())
            if end < 0:
                return value
            end += len(delimiter)
            normalized.append(value[index:end])
            index = end
            continue

        normalized.append(character)
        index += 1

    return "".join(normalized)


def _normalize(value: object, *, database: str) -> object:
    if isinstance(value, str):
        database_neutral = value.replace(database, "<database>")
        return _normalize_view_sql(database_neutral)
    if isinstance(value, dict):
        return {
            key: _normalize(item, database=database) for key, item in value.items()
        }
    if isinstance(value, list):
        return [_normalize(item, database=database) for item in value]
    if isinstance(value, tuple):
        return [_normalize(item, database=database) for item in value]
    return value


def _rows(cur: psycopg.Cursor, query: str) -> list[list[object]]:
    return [list(row) for row in cur.execute(query).fetchall()]


def export_manifest(url: str, *, apply_roles: bool) -> dict[str, object]:
    with psycopg.connect(url, autocommit=True) as conn:
        database = str(conn.execute("SELECT current_database()").fetchone()[0])
        if apply_roles:
            conn.execute((ROOT / "infra/db/roles.sql").read_text())
        with conn.cursor() as cur:
            tables = [str(row[0]) for row in cur.execute("SHOW CREATE ALL TABLES").fetchall()]
            table_names = [
                str(row[0])
                for row in cur.execute(
                    """
                        SELECT table_name FROM information_schema.tables
                        WHERE table_schema = 'public' AND table_type = 'BASE TABLE'
                        ORDER BY table_name
                    """
                ).fetchall()
            ]
            views = _rows(
                cur,
                """
                    SELECT table_name, view_definition
                    FROM information_schema.views
                    WHERE table_schema = 'public'
                    ORDER BY table_name
                """,
            )
            functions = _rows(
                cur,
                """
                    SELECT n.nspname, p.proname,
                           pg_get_function_identity_arguments(p.oid),
                           pg_get_functiondef(p.oid)
                    FROM pg_proc AS p
                    JOIN pg_namespace AS n ON n.oid = p.pronamespace
                    WHERE n.nspname = 'public'
                    ORDER BY n.nspname, p.proname,
                             pg_get_function_identity_arguments(p.oid)
                """,
            )
            triggers: list[list[object]] = []
            policies: list[list[object]] = []
            for table in table_names:
                trigger_rows = cur.execute(
                    sql.SQL("SHOW TRIGGERS FROM {}").format(sql.Identifier(table))
                ).fetchall()
                for trigger_name, enabled in trigger_rows:
                    definition = cur.execute(
                        sql.SQL("SHOW CREATE TRIGGER {} ON {}").format(
                            sql.Identifier(str(trigger_name)), sql.Identifier(table)
                        )
                    ).fetchone()[1]
                    triggers.append([table, str(trigger_name), bool(enabled), str(definition)])
                for policy in cur.execute(
                    sql.SQL("SHOW POLICIES FOR {}").format(sql.Identifier(table))
                ).fetchall():
                    policies.append([table, *policy])
            grants = _rows(
                cur,
                """
                    SELECT table_name, grantee, privilege_type, is_grantable
                    FROM information_schema.table_privileges
                    WHERE table_schema = 'public' AND grantee LIKE 'hindsight_%'
                    ORDER BY table_name, grantee, privilege_type
                """,
            )
            roles = _rows(
                cur,
                """
                    SELECT rolname, rolcanlogin, rolsuper, rolbypassrls
                    FROM pg_roles WHERE rolname LIKE 'hindsight_%'
                    ORDER BY rolname
                """,
            )
        raw = {
            "tables": sorted(tables),
            "views": views,
            "functions": functions,
            "triggers": sorted(triggers),
            "policies": sorted(policies),
            "table_grants": grants,
            "roles": roles,
        }
        return _normalize(raw, database=database)  # type: ignore[return-value]


def compare(left: Path, right: Path) -> None:
    left_value = json.loads(left.read_text())
    right_value = json.loads(right.read_text())
    if left_value != right_value:
        sections = sorted(
            key
            for key in set(left_value).union(right_value)
            if left_value.get(key) != right_value.get(key)
        )
        raise RuntimeError("schema manifests differ in: " + ", ".join(sections))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    export = subparsers.add_parser("export")
    export.add_argument("--output", type=Path, required=True)
    export.add_argument("--apply-roles", action="store_true")
    compare_parser = subparsers.add_parser("compare")
    compare_parser.add_argument("left", type=Path)
    compare_parser.add_argument("right", type=Path)
    args = parser.parse_args()

    if args.command == "export":
        manifest = export_manifest(database_url(), apply_roles=args.apply_roles)
        args.output.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    else:
        compare(args.left, args.right)


if __name__ == "__main__":
    main()
