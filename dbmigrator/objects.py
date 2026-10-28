"""Extraction of programmable objects (procedures, functions, triggers) from each source family.

Every extractor returns a list of DbObject, or None when the family has no catalog
support for that object kind. Extracted DDL is always written to a .sql script; it is
only executed on the target when source and target share a procedural-language family.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

import sqlalchemy as sa


@dataclass
class DbObject:
    kind: str          # PROCEDURE, FUNCTION, PACKAGE, TRIGGER
    name: str
    ddl: str
    complete: bool = True  # False when only the body was recoverable; never executed


_DEFINER = re.compile(r"\s+DEFINER\s*=\s*(`[^`]*`|'[^']*'|\S+?)@(`[^`]*`|'[^']*'|\S+)", re.I)
_ORACLE_TERMINATOR = re.compile(r"^\s*/\s*$", re.M)


def _text(value) -> str:
    if value is None:
        return ""
    if hasattr(value, "read"):  # Oracle / DB2 LOB
        value = value.read()
    return str(value).strip()


def _rows(conn, sql: str, **params):
    return conn.execute(sa.text(sql), params).all()


def _mysql_quote(name: str) -> str:
    return "`" + name.replace("`", "``") + "`"


# -- MySQL / MariaDB -------------------------------------------------------------------

def _mysql_routines(conn, schema):
    out = []
    for name, rtype in _rows(conn, "SELECT ROUTINE_NAME, ROUTINE_TYPE FROM information_schema.ROUTINES "
                                   "WHERE ROUTINE_SCHEMA = :s ORDER BY ROUTINE_TYPE, ROUTINE_NAME", s=schema):
        row = conn.exec_driver_sql(f"SHOW CREATE {rtype} {_mysql_quote(schema)}.{_mysql_quote(name)}").first()
        ddl = _text(row[2]) if row else ""
        if not ddl:
            out.append(DbObject(rtype, name, f"-- {rtype} {name}: body hidden (needs SHOW_ROUTINE privilege or ownership)", False))
            continue
        out.append(DbObject(rtype, name, _DEFINER.sub("", ddl, count=1)))
    return out


def _mysql_triggers(conn, schema):
    out = []
    for (name,) in _rows(conn, "SELECT TRIGGER_NAME FROM information_schema.TRIGGERS "
                               "WHERE TRIGGER_SCHEMA = :s ORDER BY EVENT_OBJECT_TABLE, ACTION_ORDER", s=schema):
        row = conn.exec_driver_sql(f"SHOW CREATE TRIGGER {_mysql_quote(schema)}.{_mysql_quote(name)}").first()
        out.append(DbObject("TRIGGER", name, _DEFINER.sub("", _text(row[2]), count=1)))
    return out


# -- PostgreSQL ------------------------------------------------------------------------

def _pg_routines(conn, schema):
    rows = _rows(conn, """
        SELECT p.oid::regprocedure::text, p.prokind, pg_get_functiondef(p.oid)
        FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
        WHERE n.nspname = :s AND p.prokind IN ('f', 'p')
          AND NOT EXISTS (SELECT 1 FROM pg_depend d WHERE d.objid = p.oid AND d.deptype = 'e')
        ORDER BY 1""", s=schema)
    return [DbObject("PROCEDURE" if kind == "p" else "FUNCTION", name, _text(ddl)) for name, kind, ddl in rows]


def _pg_triggers(conn, schema):
    rows = _rows(conn, """
        SELECT t.tgname, pg_get_triggerdef(t.oid, true)
        FROM pg_trigger t
        JOIN pg_class c ON c.oid = t.tgrelid
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE NOT t.tgisinternal AND n.nspname = :s
        ORDER BY c.relname, t.tgname""", s=schema)
    return [DbObject("TRIGGER", name, _text(ddl)) for name, ddl in rows]


def _redshift_routines(conn, schema):
    rows = _rows(conn, """
        SELECT p.proname, p.prosrc FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
        WHERE n.nspname = :s AND p.proowner > 1 ORDER BY 1""", s=schema)
    return [DbObject("ROUTINE", name, f"-- Body only; recreate the signature manually.\n{_text(src)}", False)
            for name, src in rows]


# -- SQL Server --------------------------------------------------------------------------

_MSSQL_KINDS = {"P": "PROCEDURE", "FN": "FUNCTION", "IF": "FUNCTION", "TF": "FUNCTION", "TR": "TRIGGER"}


def _mssql_objects(conn, schema, types):
    placeholders = ", ".join(f"'{t}'" for t in types)
    rows = _rows(conn, f"""
        SELECT o.name, RTRIM(o.type), m.definition
        FROM sys.sql_modules m JOIN sys.objects o ON o.object_id = m.object_id
        WHERE o.is_ms_shipped = 0 AND RTRIM(o.type) IN ({placeholders}) AND SCHEMA_NAME(o.schema_id) = :s
        ORDER BY o.type, o.name""", s=schema)
    return [DbObject(_MSSQL_KINDS[t], name, _text(ddl), bool(ddl)) for name, t, ddl in rows]


# -- Oracle ------------------------------------------------------------------------------

def _oracle_objects(conn, schema, types):
    conn.exec_driver_sql("BEGIN DBMS_METADATA.SET_TRANSFORM_PARAM("
                         "DBMS_METADATA.SESSION_TRANSFORM, 'SQLTERMINATOR', TRUE); END;")
    placeholders = ", ".join(f"'{t}'" for t in types)
    out = []
    for name, otype in _rows(conn, f"SELECT object_name, object_type FROM all_objects "
                                   f"WHERE owner = :s AND object_type IN ({placeholders}) "
                                   f"ORDER BY object_type, object_name", s=schema):
        ddl = conn.execute(sa.text("SELECT DBMS_METADATA.GET_DDL(:t, :n, :s) FROM dual"),
                           {"t": otype, "n": name, "s": schema}).scalar()
        out.append(DbObject(otype, name, _text(ddl)))  # PACKAGE DDL includes its body
    return out


# -- DB2 ---------------------------------------------------------------------------------

def _db2_routines(conn, schema):
    rows = _rows(conn, "SELECT ROUTINENAME, ROUTINETYPE, TEXT FROM SYSCAT.ROUTINES "
                       "WHERE ROUTINESCHEMA = :s AND TEXT IS NOT NULL ORDER BY 2, 1", s=schema)
    return [DbObject("PROCEDURE" if t.strip() == "P" else "FUNCTION", name, _text(ddl)) for name, t, ddl in rows]


def _db2_triggers(conn, schema):
    rows = _rows(conn, "SELECT TRIGNAME, TEXT FROM SYSCAT.TRIGGERS WHERE TRIGSCHEMA = :s ORDER BY 1", s=schema)
    return [DbObject("TRIGGER", name, _text(ddl)) for name, ddl in rows]


# -- Snowflake ---------------------------------------------------------------------------

def _snowflake_routines(conn, schema):
    out = []
    for kind, view, col in (("PROCEDURE", "PROCEDURES", "PROCEDURE"), ("FUNCTION", "FUNCTIONS", "FUNCTION")):
        rows = _rows(conn, f"SELECT {col}_NAME, ARGUMENT_SIGNATURE, DATA_TYPE, {col}_LANGUAGE, {col}_DEFINITION "
                           f"FROM INFORMATION_SCHEMA.{view} WHERE {col}_SCHEMA = :s ORDER BY 1", s=schema)
        for name, signature, rtype, lang, body in rows:
            ddl = (f"CREATE OR REPLACE {kind} {name}{signature}\nRETURNS {rtype}\nLANGUAGE {lang}\n"
                   f"AS $$\n{_text(body)}\n$$")
            # Reconstructed from the catalog: options such as EXECUTE AS are not recoverable.
            out.append(DbObject(kind, name, ddl, False))
    return out


# -- SQLite ------------------------------------------------------------------------------

def _sqlite_triggers(conn, _schema):
    rows = _rows(conn, "SELECT name, sql FROM sqlite_master WHERE type = 'trigger' ORDER BY tbl_name, name")
    return [DbObject("TRIGGER", name, _text(ddl)) for name, ddl in rows]


ROUTINE_EXTRACTORS = {
    "mysql": _mysql_routines,
    "postgresql": _pg_routines,
    "redshift": _redshift_routines,
    "mssql": lambda c, s: _mssql_objects(c, s, ("P", "FN", "IF", "TF")),
    "oracle": lambda c, s: _oracle_objects(c, s, ("PROCEDURE", "FUNCTION", "PACKAGE")),
    "db2": _db2_routines,
    "snowflake": _snowflake_routines,
}

TRIGGER_EXTRACTORS = {
    "mysql": _mysql_triggers,
    "postgresql": _pg_triggers,
    "mssql": lambda c, s: _mssql_objects(c, s, ("TR",)),
    "oracle": lambda c, s: _oracle_objects(c, s, ("TRIGGER",)),
    "db2": _db2_triggers,
    "sqlite": _sqlite_triggers,
}


def script_block(obj: DbObject, family: str) -> str:
    """Render one object for a .sql script runnable in the family's usual client."""
    header = f"-- {obj.kind} {obj.name}" + ("" if obj.complete else "  [INCOMPLETE - review before use]")
    ddl = obj.ddl.rstrip()
    if family == "mysql":
        return f"{header}\nDELIMITER $$\n{ddl.rstrip(';')}$$\nDELIMITER ;\n"
    if family == "mssql":
        return f"{header}\n{ddl}\nGO\n"
    if family == "oracle":
        return f"{header}\n{ddl}\n"  # GET_DDL already emits '/' terminators
    return f"{header}\n{ddl if ddl.endswith(';') else ddl + ';'}\n"


def executable_statements(obj: DbObject, family: str) -> list[str]:
    """Split one object's DDL into statements the DBAPI can execute individually."""
    if family != "oracle":
        return [obj.ddl.strip()]
    statements = []
    for chunk in _ORACLE_TERMINATOR.split(obj.ddl):
        chunk = chunk.strip()
        if not chunk:
            continue
        # Plain SQL (e.g. "ALTER TRIGGER x ENABLE;") must lose its ';'; PL/SQL blocks keep "END;".
        if not re.match(r"(CREATE|EDITIONABLE|NONEDITIONABLE)\b", chunk, re.I):
            chunk = chunk.rstrip(";").strip()
        statements.append(chunk)
    return statements
