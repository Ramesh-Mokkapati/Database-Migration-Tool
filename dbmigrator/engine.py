"""Core migration engine: reflect the source, translate it to the target dialect, copy data."""
from __future__ import annotations

import datetime
import fnmatch
import re
import threading
import traceback
import warnings
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

import sqlalchemy as sa
from sqlalchemy.schema import AddConstraint, CreateIndex, CreateTable, DropTable

from . import objects
from .connections import PROFILES, ConnectionConfig, create_db_engine, offline_dialect

IF_EXISTS_CHOICES = ("skip", "append", "truncate", "replace")

# Engines where index names must be unique per schema rather than per table (unlike MySQL / SQL Server).
_SCHEMA_WIDE_INDEX_NAMES = {"sqlite", "postgresql", "redshift", "oracle", "db2"}


@dataclass
class MigrationOptions:
    migrate_tables: bool = True
    migrate_data: bool = True
    migrate_indexes: bool = True
    migrate_foreign_keys: bool = True
    migrate_routines: bool = False
    migrate_triggers: bool = False
    execute_objects: bool = True   # run extracted objects on the target when engine families match
    if_exists: str = "skip"        # what to do with tables already on the target: see IF_EXISTS_CHOICES
    table_filter: str = ""         # comma-separated glob patterns; blank = all tables
    migrate_views: bool = False    # copy views' rows into plain target tables
    raw_byte_columns: str = ""     # "table.column" / "column" globs copied as bytes (binary data in text columns)
    raw_byte_encoding: str = "cp1252"  # how those columns' text maps back to bytes; blank = the server's stored bytes
    batch_size: int = 5000
    dry_run: bool = False          # write scripts only; never touch the target
    output_dir: str = "migration_output"

    @classmethod
    def from_dict(cls, data: dict) -> "MigrationOptions":
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


@dataclass
class TableResult:
    name: str
    status: str = "pending"        # created, existing, skipped, copied, verified, mismatch, failed
    source_rows: int | None = None
    target_rows: int | None = None
    error: str | None = None


@dataclass
class MigrationReport:
    output_dir: Path | None = None
    tables: list[TableResult] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    errors: int = 0
    cancelled: bool = False

    @property
    def succeeded(self) -> bool:
        return not self.errors and not self.cancelled


class MigrationCancelled(Exception):
    pass


# -- Type and default translation ----------------------------------------------------------

_LITERAL_DEFAULT = re.compile(r"^(-?\d+(\.\d+)?|'(?:[^']|'')*')$")
_NOW_DEFAULTS = {"CURRENT_TIMESTAMP", "CURRENT_TIMESTAMP()", "NOW()", "GETDATE()", "SYSDATETIME()",
                 "SYSDATE", "SYSTIMESTAMP", "LOCALTIMESTAMP", "CURRENT TIMESTAMP"}
_INTEGER_LIKE = {"YEAR", "BIT"}


def _compiles(coltype, dialect) -> bool:
    try:
        dialect.type_compiler_instance.process(coltype)
        return True
    except Exception:
        return False


def translate_type(coltype, dialect, same_family: bool):
    """Return (type usable on the target dialect, whether values must be stringified)."""
    if same_family and _compiles(coltype, dialect):
        return coltype, False

    if coltype.__visit_name__.upper() in _INTEGER_LIKE:
        return sa.BigInteger(), False
    try:
        generic = coltype.as_generic()
    except NotImplementedError:
        generic = None
    if isinstance(generic, sa.Enum):
        generic = sa.String(max((len(e) for e in generic.enums), default=1))
    if isinstance(generic, sa.String):
        generic.collation = None  # collation names never carry across engines
    for candidate in (generic, coltype):
        if candidate is not None and _compiles(candidate, dialect):
            return candidate, False

    try:
        py = coltype.python_type
    except NotImplementedError:
        py = None
    if py is bytes:
        return sa.LargeBinary(), False
    if py is bool:
        return sa.Boolean(), False
    if py is int:
        return sa.BigInteger(), False
    if py is float:
        return sa.Float(), False
    return sa.Text(), py is not str


def translate_default(default: str | None) -> str | None:
    """Keep only server defaults that mean the same thing on any engine."""
    if default is None:
        return None
    text = default.strip()
    while text.startswith("(") and text.endswith(")"):   # SQL Server wraps defaults: ((0))
        text = text[1:-1].strip()
    text = re.sub(r"::[\w\s\"\[\]]+$", "", text)         # PostgreSQL casts: 'x'::character varying
    if _LITERAL_DEFAULT.match(text):
        return text
    if text.upper() in _NOW_DEFAULTS:
        return "CURRENT_TIMESTAMP"
    return None


class SQLiteTemporal(sa.types.UserDefinedType):
    """Date/time column for SQLite targets that stores values as str(value) would.

    SQLAlchemy's own SQLite DATETIME always appends microseconds and rejects MySQL TIME
    values (timedelta, may exceed 24h). Applications reading the file with the C API
    expect the plain ISO text MySQL itself prints, so that is what gets written.
    """
    cache_ok = True

    def __init__(self, spec: str):
        self.spec = spec

    def get_col_spec(self, **kw):
        return self.spec

    def bind_processor(self, dialect):
        def process(value):
            if isinstance(value, datetime.timedelta):
                seconds = int(value.total_seconds())
                sign, seconds = ("-" if seconds < 0 else ""), abs(seconds)
                return f"{sign}{seconds // 3600:02d}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"
            if isinstance(value, datetime.datetime):
                return value.isoformat(sep=" ")
            if isinstance(value, (datetime.date, datetime.time)):
                return value.isoformat()
            return value
        return process


def _sqlite_temporal(coltype):
    for cls, spec in ((sa.DateTime, "DATETIME"), (sa.Date, "DATE"), (sa.Time, "TIME")):
        if isinstance(coltype, cls):
            return SQLiteTemporal(spec)
    return None


def _coerce_value(value, target_type):
    if value is None:
        return None
    if value == "" and isinstance(target_type, (sa.Integer, sa.Numeric)):
        return None  # MySQL loose typing can yield '' in numeric columns
    if isinstance(value, datetime.timedelta) and isinstance(target_type, sa.Time):
        if datetime.timedelta(0) <= value < datetime.timedelta(days=1):
            return (datetime.datetime.min + value).time()
    if isinstance(value, bytearray):
        return bytes(value)
    return value


# -- Engine --------------------------------------------------------------------------------

class MigrationEngine:
    def __init__(self, source: ConnectionConfig, target: ConnectionConfig, options: MigrationOptions,
                 log: Callable[[str], None] = print, progress: Callable[[int], None] = lambda p: None,
                 cancel_event: threading.Event | None = None):
        self.src_cfg = source
        self.tgt_cfg = target
        self.opts = options
        self._log_cb = log
        self._progress_cb = progress
        self.cancel_event = cancel_event or threading.Event()
        self.report = MigrationReport()
        self._log_file = None
        self._schema_script = None
        self._stringify: dict[str, set[str]] = {}
        self._created: list[sa.Table] = []

    # -- plumbing ------------------------------------------------------------------------

    def log(self, msg: str, level: str = "INFO"):
        line = f"{datetime.datetime.now():%H:%M:%S} {level:<5} {msg}"
        if level == "ERROR":
            self.report.errors += 1
        if self._log_file:
            self._log_file.write(line + "\n")
            self._log_file.flush()
        self._log_cb(line)

    def progress(self, pct: float):
        self._progress_cb(max(0, min(100, int(pct))))

    def _check_cancel(self):
        if self.cancel_event.is_set():
            raise MigrationCancelled()

    def _ddl(self, stmt, what: str, engine: sa.Engine | None) -> bool:
        """Record a DDL statement in schema.sql and, unless dry-running, execute it in its own transaction."""
        sql = str(stmt.compile(dialect=self.tgt_dialect)).strip()
        self._schema_script.write(sql + ";\n\n")
        if engine is None:
            return True
        try:
            with engine.begin() as conn:
                conn.execute(stmt)
            return True
        except Exception as exc:
            self.log(f"{what} failed: {_short(exc)}", "ERROR")
            return False

    # -- entry point ---------------------------------------------------------------------

    def run(self) -> MigrationReport:
        stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        out = Path(self.opts.output_dir).expanduser().resolve() / \
            f"{stamp}_{_slug(self.src_cfg.engine)}_to_{_slug(self.tgt_cfg.engine)}"
        out.mkdir(parents=True, exist_ok=True)
        self.report.output_dir = out
        self._log_file = open(out / "migration.log", "w", encoding="utf-8")
        self._schema_script = open(out / "schema.sql", "w", encoding="utf-8")

        src_engine = tgt_engine = None
        try:
            self.log(f"Output folder: {out}")
            self.log(f"Source: {self.src_cfg.engine}  Target: {self.tgt_cfg.engine}"
                     + ("  [DRY RUN - target is not modified]" if self.opts.dry_run else ""))
            src_engine = create_db_engine(self.src_cfg)
            with src_engine.connect():
                pass
            if self.opts.dry_run:
                self.tgt_dialect = offline_dialect(self.tgt_cfg)
            else:
                tgt_engine = create_db_engine(self.tgt_cfg)
                with tgt_engine.connect():
                    pass
                self.tgt_dialect = tgt_engine.dialect
            self.src_family = PROFILES[self.src_cfg.engine].family
            self.tgt_family = PROFILES[self.tgt_cfg.engine].family
            self.same_family = self.src_family == self.tgt_family
            self.progress(2)

            if self.opts.migrate_tables or self.opts.migrate_data:
                pairs = self._reflect(src_engine)
                self.progress(8)
                if pairs:
                    self._prepare_target(pairs, tgt_engine)
                    self.progress(15)
                    if self.opts.migrate_data and tgt_engine is not None:
                        self._copy_all(src_engine, tgt_engine, pairs)
                    if self.opts.migrate_foreign_keys and self.tgt_family != "sqlite":
                        self._add_foreign_keys(tgt_engine)
                    if tgt_engine is not None:
                        self._post_load(tgt_engine, pairs)
            self.progress(88)

            if self.opts.migrate_routines:
                self._migrate_objects(src_engine, tgt_engine, "routines", objects.ROUTINE_EXTRACTORS)
            self.progress(94)
            if self.opts.migrate_triggers:
                self._migrate_objects(src_engine, tgt_engine, "triggers", objects.TRIGGER_EXTRACTORS)
            self.progress(100)
        except MigrationCancelled:
            self.report.cancelled = True
            self.log("Migration cancelled by user.", "WARN")
        except Exception as exc:
            self.log(f"Migration aborted: {_short(exc)}", "ERROR")
            self._log_file.write(traceback.format_exc())
        finally:
            for eng in (src_engine, tgt_engine):
                if eng is not None:
                    eng.dispose()
            self._write_report()
            self._schema_script.close()
            self._log_file.close()
            self._log_file = None
        return self.report

    # -- reflection & translation ------------------------------------------------------

    def _matches(self, name: str, patterns: str) -> bool:
        pats = [p.strip().lower() for p in patterns.split(",") if p.strip()]
        return not pats or any(fnmatch.fnmatch(name.lower(), p) for p in pats)

    def _reflect(self, src_engine) -> list[tuple[sa.Table, sa.Table]]:
        schema = self.src_cfg.schema.strip() or None
        insp = sa.inspect(src_engine)
        names = insp.get_table_names(schema=schema)
        views = insp.get_view_names(schema=schema) if self.opts.migrate_views else []
        selected = [n for n in names + views if self._matches(n, self.opts.table_filter)]
        self.log(f"Found {len(names)} table(s)" + (f" and {len(views)} view(s)" if views else "")
                 + f" in source; {len(selected)} selected.")

        src_meta = sa.MetaData()
        for name in selected:
            self._check_cancel()
            try:
                table = sa.Table(name, src_meta, schema=schema, autoload_with=src_engine)
            except Exception as exc:
                self.log(f"Could not read table '{name}': {_short(exc)}", "ERROR")
                self.report.tables.append(TableResult(name, "failed", error=_short(exc)))
                continue
            for col in table.columns:
                if isinstance(col.type, sa.Float):
                    # Reflected MySQL DOUBLE/FLOAT default to Decimal results rounded to 10
                    # places, which silently changes values like 52.02856509999999.
                    col.type.asdecimal = False

        tgt_schema = self.tgt_cfg.schema.strip() or None
        if self.tgt_family == "snowflake":
            tgt_schema = None  # the schema is part of the Snowflake URL
        tgt_meta = sa.MetaData()
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", sa.exc.SAWarning)  # FK cycles; handled by deferring FKs
            ordered = [t for t in src_meta.sorted_tables if t.name in selected]
        pairs = []
        for src in ordered:
            tgt = src.to_metadata(tgt_meta, schema=tgt_schema)
            self._translate_table(src, tgt)
            pairs.append((src, tgt))
        # Tables only pulled in because a selected table references them: modelled so the
        # foreign keys compile, but never created or copied.
        for src in src_meta.sorted_tables:
            if src.name not in selected:
                notes = len(self.report.notes)
                self._translate_table(src, src.to_metadata(tgt_meta, schema=tgt_schema))
                del self.report.notes[notes:]  # not migrated, so nothing to report
        return pairs

    def _translate_table(self, src: sa.Table, tgt: sa.Table):
        dropped_defaults = []
        for col in tgt.columns:
            src_col = src.c[col.key]
            new_type, stringify = translate_type(src_col.type, self.tgt_dialect, self.same_family)
            if self.tgt_family == "sqlite":
                new_type = _sqlite_temporal(new_type) or new_type
            col.type = new_type
            if stringify:
                self._stringify.setdefault(tgt.name, set()).add(col.key)
                self.report.notes.append(f"{tgt.name}.{col.key}: {src_col.type!r} has no equivalent; stored as text")

            if col.computed is not None and not self.same_family:
                col.computed = None
                col.server_default = None
                self.report.notes.append(f"{tgt.name}.{col.key}: computed column copied as a plain column")
            if col.identity is not None:
                if self.same_family:
                    col.identity.always = False  # GENERATED ALWAYS would reject the copied ids
                else:
                    col.identity = None
                    col.server_default = None
            default = col.server_default
            if isinstance(default, sa.DefaultClause) and isinstance(default.arg, (str, sa.TextClause)):
                text = str(default.arg)
                keep = text if (self.same_family and "nextval(" not in text) else translate_default(text)
                if keep is None:
                    dropped_defaults.append(col.key)
                    col.server_default = None
                elif keep != text:
                    col.server_default = sa.DefaultClause(sa.text(keep))

        if self.tgt_family == "sqlite":
            pk = list(tgt.primary_key.columns)
            if len(pk) == 1 and isinstance(pk[0].type, sa.Integer):
                pk[0].type = sa.Integer()  # only INTEGER PRIMARY KEY aliases the rowid
                if src.c[pk[0].key].autoincrement is True:
                    tgt.dialect_options["sqlite"]["autoincrement"] = True

        if not self.same_family:
            for cons in [c for c in tgt.constraints if isinstance(c, sa.CheckConstraint)]:
                tgt.constraints.discard(cons)
                self.report.notes.append(f"{tgt.name}: CHECK constraint {cons.name or ''} dropped (engine-specific SQL)")
        if dropped_defaults:
            self.report.notes.append(f"{tgt.name}: engine-specific defaults dropped on {', '.join(dropped_defaults)}")

    # -- DDL ---------------------------------------------------------------------------

    def _prepare_target(self, pairs, tgt_engine):
        existing = set()
        if tgt_engine is not None:
            existing = set(sa.inspect(tgt_engine).get_table_names(schema=pairs[0][1].schema))
        mode = self.opts.if_exists
        results = {}

        if mode == "replace":
            for _, tgt in reversed(pairs):
                if tgt.name in existing:
                    if self._ddl(DropTable(tgt), f"Drop table {tgt.name}", tgt_engine):
                        existing.discard(tgt.name)
                        self.log(f"Dropped existing target table {tgt.name}")
        elif mode == "truncate" and tgt_engine is not None:
            for _, tgt in reversed(pairs):
                if tgt.name in existing:
                    try:
                        with tgt_engine.begin() as conn:
                            conn.execute(tgt.delete())
                        self.log(f"Emptied existing target table {tgt.name}")
                    except Exception as exc:
                        self.log(f"Emptying {tgt.name} failed: {_short(exc)}", "ERROR")

        index_names = set()
        if tgt_engine is not None and self.tgt_family in _SCHEMA_WIDE_INDEX_NAMES:
            insp = sa.inspect(tgt_engine)
            for name in existing:
                index_names.update(i["name"].lower() for i in insp.get_indexes(name, schema=pairs[0][1].schema)
                                   if i.get("name"))

        inline_fks = self.tgt_family == "sqlite" and self.opts.migrate_foreign_keys
        for idx, (_, tgt) in enumerate(pairs, 1):
            self._check_cancel()
            result = TableResult(tgt.name)
            results[tgt.name] = result
            if tgt.name in existing:
                result.status = "skipped" if mode == "skip" else "existing"
                self.log(f"[{idx}/{len(pairs)}] {tgt.name}: exists on target ({mode})")
            elif not self.opts.migrate_tables:
                result.status = "failed"
                result.error = "missing on target and 'Tables & schema' is off"
                self.log(f"{tgt.name}: {result.error}", "ERROR")
            else:
                fks = list(tgt.foreign_key_constraints) if inline_fks else []
                ok = self._ddl(CreateTable(tgt, include_foreign_key_constraints=fks), f"Create table {tgt.name}", tgt_engine)
                if ok and self.opts.migrate_indexes:
                    for index in sorted(tgt.indexes, key=lambda i: i.name or ""):
                        self._unique_index_name(index, tgt, index_names)
                        self._ddl(CreateIndex(index), f"Create index {index.name} on {tgt.name}", tgt_engine)
                result.status = "created" if ok else "failed"
                if ok:
                    self._created.append(tgt)
                    self.log(f"[{idx}/{len(pairs)}] {tgt.name}: created")
            self.progress(8 + 7 * idx / len(pairs))

        self.report.tables.extend(results[t.name] for _, t in pairs)

    def _unique_index_name(self, index: sa.Index, table: sa.Table, used: set[str]):
        """Rename an index whose name is already taken in a schema-wide index namespace."""
        if self.tgt_family not in _SCHEMA_WIDE_INDEX_NAMES or not index.name:
            return
        name = index.name
        limit = self.tgt_dialect.max_identifier_length or 63
        if name.lower() in used:
            base = f"{table.name}_{name}"[:limit]
            name, n = base, 2
            while name.lower() in used:
                suffix = f"_{n}"
                name, n = base[:limit - len(suffix)] + suffix, n + 1
            self.report.notes.append(f"{table.name}: index {index.name} renamed to {name} "
                                     f"(index names are schema-wide on {self.tgt_cfg.engine})")
            index.name = name
        used.add(name.lower())

    def _add_foreign_keys(self, tgt_engine):
        fks = [(t, fk) for t in self._created for fk in sorted(t.foreign_key_constraints, key=lambda f: f.name or "")]
        if fks:
            self.log(f"Adding {len(fks)} foreign key(s)...")
        for table, fk in fks:
            self._ddl(AddConstraint(fk), f"Foreign key {fk.name or ''} on {table.name}", tgt_engine)

    # -- data ----------------------------------------------------------------------------

    def _copy_all(self, src_engine, tgt_engine, pairs):
        results = {r.name: r for r in self.report.tables}
        todo = [(s, t) for s, t in pairs if results[t.name].status in ("created", "existing")]
        for idx, (src, tgt) in enumerate(todo):
            self._check_cancel()
            base, span = 15 + 70 * idx / len(todo), 70 / len(todo)
            self._copy_table(src_engine, tgt_engine, src, tgt, results[tgt.name], base, span)

    def _raw_byte_columns(self, src: sa.Table) -> set[str]:
        pats = self.opts.raw_byte_columns
        if not pats.strip():
            return set()
        return {c.key for c in src.columns if self._matches(f"{src.name}.{c.key}", pats) or self._matches(c.key, pats)}

    def _source_select(self, src: sa.Table, raw: set[str]):
        stored_bytes = not self.opts.raw_byte_encoding.strip()
        return sa.select(*(sa.cast(c, sa.LargeBinary).label(c.key) if c.key in raw and stored_bytes else c
                           for c in src.columns))

    def _copy_table(self, src_engine, tgt_engine, src, tgt, result, base, span):
        stringify = self._stringify.get(tgt.name, set())
        target_types = {c.key: c.type for c in tgt.columns}
        raw = self._raw_byte_columns(src)
        encoding = self.opts.raw_byte_encoding.strip()
        unencodable = 0
        try:
            with src_engine.connect() as sconn:
                result.source_rows = sconn.execute(sa.select(sa.func.count()).select_from(src)).scalar()
            self.log(f"Copying {tgt.name} ({result.source_rows} rows)...")
            copied = 0
            insert = tgt.insert()
            with src_engine.connect() as sconn, tgt_engine.connect() as tconn:
                rows = sconn.execution_options(stream_results=True, yield_per=self.opts.batch_size) \
                            .execute(self._source_select(src, raw))
                for chunk in rows.partitions():
                    self._check_cancel()
                    batch = []
                    for row in chunk:
                        record = {}
                        for key, value in row._mapping.items():
                            if key in raw and encoding and isinstance(value, str):
                                try:
                                    value = value.encode(encoding)
                                except UnicodeEncodeError:
                                    unencodable += 1  # left as text, as the original script did
                            elif key in stringify and value is not None and not isinstance(value, (bytes, bytearray)):
                                value = str(value)
                            record[key] = _coerce_value(value, target_types[key])
                        batch.append(record)
                    tconn.execute(insert, batch)
                    tconn.commit()
                    copied += len(batch)
                    if result.source_rows:
                        self.progress(base + span * copied / result.source_rows)
            result.status = "copied"
            self.log(f"  -> {tgt.name}: {copied} row(s) copied")
            if unencodable:
                self.log(f"{tgt.name}: {unencodable} raw-byte value(s) not representable in {encoding}; kept as text",
                         "WARN")
        except MigrationCancelled:
            raise
        except Exception as exc:
            result.status, result.error = "failed", _short(exc)
            self.log(f"Copying {tgt.name} failed: {_short(exc)}", "ERROR")

    def _post_load(self, tgt_engine, pairs):
        results = {r.name: r for r in self.report.tables}
        if self.tgt_family == "postgresql":
            self._reset_pg_sequences(tgt_engine, [t for t in self._created])
        for _, tgt in pairs:
            res = results[tgt.name]
            if res.status not in ("copied", "existing", "created"):
                continue
            try:
                with tgt_engine.connect() as conn:
                    res.target_rows = conn.execute(sa.select(sa.func.count()).select_from(tgt)).scalar()
            except Exception as exc:
                self.log(f"Counting {tgt.name} on target failed: {_short(exc)}", "WARN")
                continue
            if res.status == "copied":
                res.status = "verified" if res.target_rows == res.source_rows else "mismatch"
                if res.status == "mismatch":
                    self.log(f"{tgt.name}: source has {res.source_rows} rows, target has {res.target_rows}", "WARN")

    def _reset_pg_sequences(self, tgt_engine, tables):
        prep = self.tgt_dialect.identifier_preparer
        for table in tables:
            for col in table.primary_key.columns:
                if not isinstance(col.type, sa.Integer):
                    continue
                try:
                    with tgt_engine.begin() as conn:
                        seq = conn.execute(sa.text("SELECT pg_get_serial_sequence(:t, :c)"),
                                           {"t": prep.format_table(table), "c": col.name}).scalar()
                        if seq:
                            conn.execute(sa.text(
                                f"SELECT setval(:s, COALESCE((SELECT MAX({prep.quote(col.name)}) "
                                f"FROM {prep.format_table(table)}), 0) + 1, false)"), {"s": seq})
                except Exception as exc:
                    self.log(f"Resetting sequence for {table.name}.{col.name} failed: {_short(exc)}", "WARN")

    # -- programmable objects ------------------------------------------------------------

    def _migrate_objects(self, src_engine, tgt_engine, label, extractors):
        extractor = extractors.get(self.src_family)
        if extractor is None:
            self.log(f"{label.capitalize()}: not available for {self.src_cfg.engine} sources; skipped.", "WARN")
            return
        with src_engine.connect() as conn:
            schema = self.src_cfg.schema.strip() or (self.src_cfg.database if self.src_family == "mysql" else None)
            schema = schema or sa.inspect(conn).default_schema_name
            if conn.dialect.requires_name_normalize:
                schema = conn.dialect.denormalize_name(schema)
            found = extractor(conn, schema)
        self.log(f"Extracted {len(found)} {label}.")
        if not found:
            return

        path = self.report.output_dir / f"{label}.sql"
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(f"-- {label} extracted from {self.src_cfg.engine} ({schema})\n"
                     f"-- Written in {self.src_cfg.engine} syntax; translate before running on another engine.\n\n")
            for obj in found:
                fh.write(objects.script_block(obj, self.src_family) + "\n")
        self.log(f"Wrote {path.name}")

        if tgt_engine is None or not self.opts.execute_objects:
            return
        compatible = self.same_family and not (label == "routines" and self.tgt_family == "sqlite")
        if not compatible:
            self.log(f"{self.src_cfg.engine} and {self.tgt_cfg.engine} use different procedural languages; "
                     f"{label} were saved to {path.name} for manual conversion.", "WARN")
            return
        created = 0
        for obj in found:
            self._check_cancel()
            if not obj.complete:
                self.log(f"{obj.kind} {obj.name}: incomplete definition, not executed", "WARN")
                continue
            try:
                with tgt_engine.begin() as conn:
                    for stmt in objects.executable_statements(obj, self.src_family):
                        conn.execution_options(no_parameters=True).exec_driver_sql(stmt)
                created += 1
            except Exception as exc:
                self.log(f"Creating {obj.kind} {obj.name} failed: {_short(exc)}", "ERROR")
        self.log(f"Created {created}/{len(found)} {label} on target.")

    # -- reporting ---------------------------------------------------------------------

    def _write_report(self):
        rep = self.report
        if rep.output_dir is None:
            return
        lines = [f"Migration report  {datetime.datetime.now():%Y-%m-%d %H:%M:%S}",
                 f"Source: {self.src_cfg.engine} {self.src_cfg.database}",
                 f"Target: {self.tgt_cfg.engine} {self.tgt_cfg.database}",
                 f"Options: {asdict(self.opts)}", "",
                 f"{'Table':<40} {'Status':<10} {'Source':>12} {'Target':>12}  Error"]
        for t in rep.tables:
            lines.append(f"{t.name:<40} {t.status:<10} {_n(t.source_rows):>12} {_n(t.target_rows):>12}  {t.error or ''}")
        if rep.notes:
            lines += ["", "Translation notes:"] + [f"  - {n}" for n in rep.notes]
        outcome = "CANCELLED" if rep.cancelled else ("COMPLETED WITH ERRORS" if rep.errors else "COMPLETED")
        lines += ["", f"Result: {outcome} ({rep.errors} error(s))"]
        (rep.output_dir / "report.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
        verified = sum(t.status == "verified" for t in rep.tables)
        self.log(f"{outcome}: {verified}/{len(rep.tables)} table(s) verified, {rep.errors} error(s), "
                 f"{len(rep.notes)} translation note(s). See report.txt",
                 "INFO" if rep.succeeded else "WARN")


def _short(exc: Exception) -> str:
    text = str(getattr(exc, "orig", None) or exc).strip().splitlines()
    return text[0][:300] if text else type(exc).__name__


def _slug(name: str) -> str:
    return re.sub(r"\W+", "", name.lower())


def _n(value) -> str:
    return "" if value is None else str(value)
