"""Database Migration Tool entry point.

    python migration_tool.py                 # GUI
    python migration_tool.py --job job.json  # headless, using a job saved from the GUI
"""
import argparse
import sys

from dbmigrator.jobs import load_job


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Cross-database schema and data migration tool")
    parser.add_argument("--job", help="run headless from a job JSON file; passwords may come from "
                                      "DBMIG_SOURCE_PASSWORD / DBMIG_TARGET_PASSWORD")
    args = parser.parse_args(argv)

    if args.job:
        from dbmigrator.engine import MigrationEngine
        source, target, options = load_job(args.job)
        report = MigrationEngine(source, target, options).run()
        return 0 if report.succeeded else 1

    from dbmigrator.gui import run_gui
    run_gui()
    return 0


if __name__ == "__main__":
    sys.exit(main())
