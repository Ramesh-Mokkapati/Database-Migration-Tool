"""Run the built executable headless on a small SQLite->SQLite job and check the result.

    python scripts/smoke_test_exe.py dist/DBMigrationTool.exe
"""
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]

from dbmigrator.connections import ConnectionConfig  # noqa: E402
from dbmigrator.engine import MigrationOptions  # noqa: E402
from dbmigrator.jobs import save_job  # noqa: E402
from test_engine import make_source  # noqa: E402


def main(exe: str) -> int:
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        make_source(tmp / "src.db", rows=2000)
        save_job(tmp / "job.json", ConnectionConfig("SQLite", database=str(tmp / "src.db")),
                 ConnectionConfig("SQLite", database=str(tmp / "tgt.db")),
                 MigrationOptions(migrate_triggers=True, output_dir=str(tmp / "out")))
        proc = subprocess.run([exe, "--job", str(tmp / "job.json")], timeout=300)
        reports = list((tmp / "out").glob("*/report.txt"))
        report = reports[0].read_text(encoding="utf-8") if reports else "(no report written)"
        print(report)
        if proc.returncode != 0 or "Result: COMPLETED (0 error(s))" not in report:
            print(f"Smoke test FAILED (exit code {proc.returncode})", file=sys.stderr)
            return 1
    print("Smoke test passed")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else str(ROOT / "dist" / "DBMigrationTool.exe")))
