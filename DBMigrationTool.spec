# -*- mode: python ; coding: utf-8 -*-
# Build:  pyinstaller DBMigrationTool.spec   ->  dist/DBMigrationTool.exe
# Only drivers installed in the build environment are bundled.
import importlib.util

from PyInstaller.utils.hooks import collect_data_files, collect_submodules, copy_metadata

hiddenimports = collect_submodules("sqlalchemy.dialects") + ["dbmigrator.gui"]
datas = []

OPTIONAL = {
    # module to collect: distributions whose metadata the driver reads at runtime
    "pymysql": [],
    "mysql.connector": ["mysql-connector-python"],
    "psycopg2": [],
    "oracledb": ["oracledb"],
    "pyodbc": [],
    "ibm_db": [],
    "ibm_db_sa": ["ibm_db_sa"],
    "snowflake.sqlalchemy": ["snowflake-sqlalchemy", "snowflake-connector-python"],
    "snowflake.connector": [],
    "sqlalchemy_redshift": ["sqlalchemy-redshift"],
}


def _installed(name):
    try:
        return importlib.util.find_spec(name) is not None
    except ModuleNotFoundError:
        return False


for module, dists in OPTIONAL.items():
    if not _installed(module):
        continue
    hiddenimports += collect_submodules(module)
    datas += collect_data_files(module)
    for dist in dists:
        datas += copy_metadata(dist)

a = Analysis(
    ["migration_tool.py"],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "pytest"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="DBMigrationTool",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
)
