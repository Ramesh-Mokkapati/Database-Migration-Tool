"""Connection profiles: how each supported engine maps to a SQLAlchemy URL and driver."""
from __future__ import annotations

import importlib.util
from dataclasses import asdict, dataclass, field
from urllib.parse import parse_qsl

import sqlalchemy as sa
from sqlalchemy import event
from sqlalchemy.dialects import registry
from sqlalchemy.engine import URL

# Third-party dialects normally register through package entry points, which a
# PyInstaller bundle does not carry. Registering them explicitly keeps the frozen
# .exe working; the registry is lazy, so a missing package costs nothing here.
registry.register("snowflake", "snowflake.sqlalchemy", "dialect")
registry.register("redshift.psycopg2", "sqlalchemy_redshift.dialect", "RedshiftDialect_psycopg2")
registry.register("db2.ibm_db", "ibm_db_sa.ibm_db", "DB2Dialect_ibm_db")


class DriverMissingError(RuntimeError):
    pass


@dataclass(frozen=True)
class EngineProfile:
    # (drivername, required modules, pip package) in order of preference
    drivers: tuple[tuple[str, tuple[str, ...], str], ...]
    family: str  # procedural-language family; objects only execute within one family
    default_port: str = ""
    default_options: str = ""
    file_based: bool = False
    host_label: str = "Host"
    system_schemas: frozenset[str] = field(default_factory=frozenset)


_MYSQL_SYSTEM = frozenset({"information_schema", "mysql", "performance_schema", "sys"})
_PG_SYSTEM = frozenset({"information_schema", "pg_catalog", "pg_toast"})

PROFILES: dict[str, EngineProfile] = {
    "SQLite": EngineProfile(
        drivers=(("sqlite", (), ""),), family="sqlite", file_based=True),
    "MySQL": EngineProfile(
        drivers=(("mysql+pymysql", ("pymysql",), "pymysql"),
                 ("mysql+mysqlconnector", ("mysql.connector",), "mysql-connector-python")),
        family="mysql", default_port="3306", default_options="charset=utf8mb4",
        system_schemas=_MYSQL_SYSTEM),
    "MariaDB": EngineProfile(
        drivers=(("mariadb+pymysql", ("pymysql",), "pymysql"),
                 ("mariadb+mysqlconnector", ("mysql.connector",), "mysql-connector-python")),
        family="mysql", default_port="3306", default_options="charset=utf8mb4",
        system_schemas=_MYSQL_SYSTEM),
    "PostgreSQL": EngineProfile(
        drivers=(("postgresql+psycopg2", ("psycopg2",), "psycopg2-binary"),),
        family="postgresql", default_port="5432", system_schemas=_PG_SYSTEM),
    "Oracle": EngineProfile(
        drivers=(("oracle+oracledb", ("oracledb",), "oracledb"),),
        family="oracle", default_port="1521"),
    "MSSQL": EngineProfile(
        drivers=(("mssql+pyodbc", ("pyodbc",), "pyodbc"),),
        family="mssql", default_port="1433",
        default_options="driver=ODBC Driver 18 for SQL Server&TrustServerCertificate=yes",
        system_schemas=frozenset({"sys", "INFORMATION_SCHEMA", "guest"})),
    "IBM DB2": EngineProfile(
        drivers=(("db2+ibm_db", ("ibm_db_sa", "ibm_db"), "ibm_db ibm_db_sa"),),
        family="db2", default_port="50000"),
    "Snowflake": EngineProfile(
        drivers=(("snowflake", ("snowflake.sqlalchemy",), "snowflake-sqlalchemy"),),
        family="snowflake", default_options="warehouse=COMPUTE_WH", host_label="Account"),
    "Amazon Redshift": EngineProfile(
        drivers=(("redshift+psycopg2", ("sqlalchemy_redshift", "psycopg2"),
                  "sqlalchemy-redshift psycopg2-binary"),),
        family="redshift", default_port="5439", system_schemas=_PG_SYSTEM),
}


@dataclass
class ConnectionConfig:
    engine: str = "MySQL"
    host: str = "localhost"
    port: str = ""
    user: str = ""
    password: str = ""
    database: str = ""     # database name, Oracle service name, or SQLite file path
    schema: str = ""       # blank = the connection's default schema
    options: str = ""      # extra URL query, e.g. "warehouse=WH&role=SYSADMIN"

    def to_dict(self, include_password: bool = False) -> dict:
        data = asdict(self)
        if not include_password:
            data.pop("password")
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "ConnectionConfig":
        known = {k: str(v) for k, v in data.items() if k in cls.__dataclass_fields__}
        return cls(**known)


def _module_available(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except ModuleNotFoundError:
        return False


def resolve_driver(engine: str) -> str:
    """Return the first SQLAlchemy drivername whose Python modules are installed."""
    profile = PROFILES[engine]
    for drivername, modules, _ in profile.drivers:
        if all(_module_available(m) for m in modules):
            return drivername
    packages = " or ".join(pkg for _, _, pkg in profile.drivers)
    raise DriverMissingError(f"No Python driver installed for {engine}. Install with: pip install {packages}")


def build_url(cfg: ConnectionConfig, drivername: str | None = None) -> URL:
    profile = PROFILES[cfg.engine]
    drivername = drivername or resolve_driver(cfg.engine)
    if profile.file_based:
        return URL.create(drivername, database=cfg.database or None)

    query = dict(parse_qsl(cfg.options.strip(), keep_blank_values=True)) if cfg.options.strip() else {}
    port = int(cfg.port) if str(cfg.port).strip() else None
    database = cfg.database.strip() or None
    if cfg.engine == "Oracle" and database:
        query.setdefault("service_name", database)
        database = None
    elif cfg.engine == "Snowflake":
        port = None
        if database and cfg.schema.strip():
            database = f"{database}/{cfg.schema.strip()}"

    return URL.create(drivername, username=cfg.user or None, password=cfg.password or None,
                      host=cfg.host.strip() or None, port=port, database=database, query=query)


def _tune_sqlite(engine: sa.Engine) -> None:
    @event.listens_for(engine, "connect")
    def _pragmas(dbapi_conn, _record):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.close()


def create_db_engine(cfg: ConnectionConfig) -> sa.Engine:
    url = build_url(cfg)
    kwargs = {}
    if url.get_backend_name() == "mssql":
        kwargs["fast_executemany"] = True
    engine = sa.create_engine(url, **kwargs)
    if url.get_backend_name() == "sqlite":
        _tune_sqlite(engine)
    return engine


def offline_dialect(cfg: ConnectionConfig) -> sa.Dialect:
    """A dialect for compiling DDL without a driver or a live connection (dry runs)."""
    profile = PROFILES[cfg.engine]
    try:
        drivername = resolve_driver(cfg.engine)
    except DriverMissingError:
        drivername = profile.drivers[0][0].split("+")[0]
    return sa.create_mock_engine(build_url(cfg, drivername), lambda *a, **k: None).dialect


def list_schemas(cfg: ConnectionConfig) -> list[str]:
    """Non-system schemas on the server (MySQL/MariaDB call these databases)."""
    probe = ConnectionConfig(**cfg.to_dict(include_password=True))
    if PROFILES[cfg.engine].family == "mysql":
        probe.database = ""  # connect at server level so every database is listed
    engine = create_db_engine(probe)
    try:
        names = sa.inspect(engine).get_schema_names()
    finally:
        engine.dispose()
    system = {s.lower() for s in PROFILES[cfg.engine].system_schemas}
    return [n for n in names if n.lower() not in system and not n.lower().startswith("pg_")]


def test_connection(cfg: ConnectionConfig) -> str:
    engine = create_db_engine(cfg)
    try:
        with engine.connect() as conn:
            insp = sa.inspect(conn)
            schema = cfg.schema or None
            tables = insp.get_table_names(schema=schema)
            version = ".".join(str(v) for v in (engine.dialect.server_version_info or ())) or "n/a"
        return (f"Connected to {cfg.engine} (server version {version}).\n"
                f"Schema '{schema or insp.default_schema_name}': {len(tables)} table(s).")
    finally:
        engine.dispose()
