import datetime
import decimal
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy.dialects import mssql, mysql, oracle, postgresql, sqlite

from dbmigrator.connections import ConnectionConfig, build_url
from dbmigrator.engine import MigrationEngine, MigrationOptions, translate_default, translate_type
from dbmigrator.jobs import load_job, save_job
from dbmigrator.objects import DbObject, executable_statements

SOURCE_DDL = """
CREATE TABLE parent (id INTEGER PRIMARY KEY AUTOINCREMENT, name VARCHAR(50) NOT NULL DEFAULT 'x',
                     created DATETIME DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE child (id INTEGER PRIMARY KEY, parent_id INTEGER REFERENCES parent(id),
                    amount NUMERIC(10, 2), note TEXT);
CREATE INDEX ix_child_parent ON child(parent_id);
CREATE TABLE audit (msg TEXT);
CREATE TRIGGER trg_parent AFTER INSERT ON parent BEGIN INSERT INTO audit(msg) VALUES ('new ' || NEW.name); END;
"""


def make_source(path: Path, rows: int = 12_345):
    con = sqlite3.connect(path)
    con.executescript(SOURCE_DDL)
    con.executemany("INSERT INTO parent(id, name, created) VALUES (?, ?, ?)",
                    [(i, f"p{i}", "2024-05-01 10:20:30") for i in range(1, 51)])
    con.executemany("INSERT INTO child(id, parent_id, amount, note) VALUES (?, ?, ?, ?)",
                    [(i, i % 50 + 1, i / 100, None if i % 7 else "seven") for i in range(1, rows + 1)])
    con.commit()
    con.close()


class SQLiteEndToEnd(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.src = self.dir / "src.db"
        self.tgt = self.dir / "tgt.db"
        make_source(self.src)

    def tearDown(self):
        self.tmp.cleanup()

    def run_migration(self, **opts):
        options = MigrationOptions(output_dir=str(self.dir / "out"), batch_size=1000, **opts)
        lines = []
        report = MigrationEngine(ConnectionConfig("SQLite", database=str(self.src)),
                                 ConnectionConfig("SQLite", database=str(self.tgt)),
                                 options, log=lines.append).run()
        return report, lines

    def test_full_migration(self):
        report, lines = self.run_migration(migrate_triggers=True)
        self.assertTrue(report.succeeded, "\n".join(lines))
        status = {t.name: (t.status, t.source_rows, t.target_rows) for t in report.tables}
        self.assertEqual(status["child"], ("verified", 12_345, 12_345))
        self.assertEqual(status["parent"], ("verified", 50, 50))

        with closing(sqlite3.connect(self.tgt)) as con:
            self.assertEqual(con.execute("SELECT created FROM parent WHERE id = 1").fetchone()[0], "2024-05-01 10:20:30")
            self.assertIn("REFERENCES", con.execute("SELECT sql FROM sqlite_master WHERE name='child'").fetchone()[0])
            self.assertIsNotNone(con.execute("SELECT 1 FROM sqlite_master WHERE name='ix_child_parent'").fetchone())
            self.assertEqual(con.execute("SELECT count(*) FROM audit").fetchone()[0], 50)  # copied from source
            con.execute("INSERT INTO parent(name) VALUES ('fired')")  # trigger was recreated
            self.assertEqual(con.execute("SELECT msg FROM audit ORDER BY rowid DESC").fetchone(), ("new fired",))
        for name in ("migration.log", "report.txt", "schema.sql", "triggers.sql"):
            self.assertTrue((report.output_dir / name).exists(), name)

    def test_if_exists_modes(self):
        self.run_migration()
        report, _ = self.run_migration(if_exists="skip")
        self.assertEqual({t.status for t in report.tables}, {"skipped"})
        report, _ = self.run_migration(if_exists="append", table_filter="audit")
        self.assertTrue(report.succeeded)
        report, _ = self.run_migration(if_exists="truncate", table_filter="child")
        self.assertEqual(report.tables[0].status, "verified")
        report, lines = self.run_migration(if_exists="replace")
        self.assertTrue(report.succeeded, "\n".join(lines))

    def test_table_filter_and_dry_run(self):
        report, _ = self.run_migration(table_filter="par*", dry_run=True)
        self.assertEqual([t.name for t in report.tables], ["parent"])
        self.assertFalse(self.tgt.exists())
        self.assertIn("CREATE TABLE parent", (report.output_dir / "schema.sql").read_text())

    def test_raw_byte_columns(self):
        con = sqlite3.connect(self.src)
        con.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, UserPassword CHAR(32))")
        con.execute("INSERT INTO users VALUES (1, ?)", ("N‰ý",))  # cp1252 bytes 4E 89 FD read as text
        con.commit()
        con.close()
        report, lines = self.run_migration(table_filter="users", raw_byte_columns="users.UserPassword")
        self.assertTrue(report.succeeded, "\n".join(lines))
        with closing(sqlite3.connect(self.tgt)) as con:
            self.assertEqual(con.execute("SELECT typeof(UserPassword), UserPassword FROM users").fetchone(),
                             ("blob", b"N\x89\xfd"))

    def test_index_name_clash_is_renamed(self):
        with closing(sqlite3.connect(self.tgt)) as con:  # index name already taken by another table
            con.execute("CREATE TABLE other (parent_id INTEGER)")
            con.execute("CREATE INDEX ix_child_parent ON other(parent_id)")
        report, lines = self.run_migration(table_filter="child, other")
        self.assertTrue(report.succeeded, "\n".join(lines))
        with closing(sqlite3.connect(self.tgt)) as con:
            self.assertEqual(con.execute("SELECT tbl_name FROM sqlite_master WHERE name='child_ix_child_parent'")
                             .fetchone(), ("child",))
        self.assertTrue(any("renamed to child_ix_child_parent" in n for n in report.notes))

    def test_cancel(self):
        options = MigrationOptions(output_dir=str(self.dir / "out"), batch_size=100)
        engine = MigrationEngine(ConnectionConfig("SQLite", database=str(self.src)),
                                 ConnectionConfig("SQLite", database=str(self.tgt)), options, log=lambda _: None)
        engine.cancel_event.set()
        self.assertTrue(engine.run().cancelled)

    def test_job_roundtrip(self):
        path = self.dir / "job.json"
        save_job(path, ConnectionConfig("MySQL", password="secret", database="db"),
                 ConnectionConfig("SQLite", database="x.db"), MigrationOptions(batch_size=42))
        self.assertNotIn("secret", path.read_text())
        src, tgt, opts = load_job(path)
        self.assertEqual((src.engine, src.database, tgt.database, opts.batch_size), ("MySQL", "db", "x.db", 42))


class CrossDialectTranslation(unittest.TestCase):
    def test_types(self):
        my, pg, ms = mysql.dialect(), postgresql.dialect(), mssql.dialect()
        cases = [
            (postgresql.VARCHAR(), my, sa.Text, False),            # MySQL VARCHAR needs a length
            (postgresql.JSONB(), my, sa.JSON, False),
            (postgresql.ARRAY(sa.Integer), my, sa.Text, True),
            (postgresql.UUID(), ms, sa.Uuid, False),
            (mysql.TINYINT(display_width=1), pg, sa.Integer, False),
            (mysql.VARCHAR(20, collation="utf8mb4_bin"), pg, sa.String, False),
            (mysql.ENUM("a", "bbb"), pg, sa.String, False),
            (mysql.YEAR(), pg, sa.BigInteger, False),
            (mysql.DOUBLE(), oracle.dialect(), sa.Float, False),
        ]
        for src_type, dialect, expected, stringify in cases:
            with self.subTest(src=repr(src_type), dialect=dialect.name):
                new, s = translate_type(src_type, dialect, same_family=False)
                self.assertIsInstance(new, expected)
                self.assertEqual(s, stringify)
                dialect.type_compiler_instance.process(new)
        new, _ = translate_type(mysql.VARCHAR(20, collation="utf8mb4_bin"), pg, False)
        self.assertIsNone(new.collation)

    def test_defaults(self):
        self.assertEqual(translate_default("((0))"), "0")
        self.assertEqual(translate_default("'abc'::character varying"), "'abc'")
        self.assertEqual(translate_default("now()"), "CURRENT_TIMESTAMP")
        self.assertIsNone(translate_default("nextval('t_id_seq'::regclass)"))
        self.assertIsNone(translate_default("uuid_generate_v4()"))

    def test_dry_run_ddl_for_every_server_engine(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "src.db"
            make_source(src, rows=10)
            for engine in ("MySQL", "PostgreSQL", "Oracle", "MSSQL"):
                with self.subTest(engine=engine):
                    report = MigrationEngine(ConnectionConfig("SQLite", database=str(src)),
                                             ConnectionConfig(engine, database="db", port="1"),
                                             MigrationOptions(dry_run=True, output_dir=tmp),
                                             log=lambda _: None).run()
                    self.assertTrue(report.succeeded)
                    ddl = (report.output_dir / "schema.sql").read_text()
                    self.assertIn("FOREIGN KEY", ddl)
                    self.assertNotIn("AUTOINCREMENT", ddl.upper().replace("AUTO_INCREMENT", ""))

    def test_urls(self):
        url = build_url(ConnectionConfig("Oracle", host="h", port="1521", user="u", password="p@ss/1",
                                         database="ORCL"), "oracle+oracledb")
        self.assertEqual(url.query["service_name"], "ORCL")
        self.assertEqual(url.password, "p@ss/1")
        url = build_url(ConnectionConfig("Snowflake", host="acct", database="DB", schema="PUBLIC",
                                         options="warehouse=WH"), "snowflake")
        self.assertEqual((url.host, url.database, url.query["warehouse"]), ("acct", "DB/PUBLIC", "WH"))

    def test_oracle_statement_split(self):
        ddl = ("CREATE OR REPLACE EDITIONABLE TRIGGER T1 BEFORE INSERT ON X FOR EACH ROW\nBEGIN\n  NULL;\nEND;\n/\n"
               "ALTER TRIGGER T1 ENABLE;\n")
        self.assertEqual(executable_statements(DbObject("TRIGGER", "T1", ddl), "oracle"),
                         ["CREATE OR REPLACE EDITIONABLE TRIGGER T1 BEFORE INSERT ON X FOR EACH ROW\nBEGIN\n  NULL;\nEND;",
                          "ALTER TRIGGER T1 ENABLE"])


if __name__ == "__main__":
    unittest.main()
