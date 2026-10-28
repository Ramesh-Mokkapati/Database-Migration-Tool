"""wxPython front end. All database work runs on worker threads; results return via wx.CallAfter."""
from __future__ import annotations

import os
import threading

import wx

from . import __version__
from .connections import PROFILES, ConnectionConfig, list_schemas, test_connection
from .engine import IF_EXISTS_CHOICES, MigrationEngine, MigrationOptions
from .jobs import load_job, save_job


def _call_after(window, fn, *args):
    """wx.CallAfter that tolerates the window having been closed meanwhile."""
    def guarded():
        if window:
            fn(*args)
    wx.CallAfter(guarded)


class ConnectionBox(wx.StaticBoxSizer):
    def __init__(self, parent: wx.Window, title: str, engine: str):
        super().__init__(wx.VERTICAL, parent, title)
        self.parent = parent
        box = self.GetStaticBox()

        self.engine = wx.Choice(box, choices=list(PROFILES))
        self.host = wx.TextCtrl(box)
        self.port = wx.TextCtrl(box)
        self.user = wx.TextCtrl(box)
        self.password = wx.TextCtrl(box, style=wx.TE_PASSWORD)
        self.database = wx.TextCtrl(box)
        self.browse = wx.Button(box, label="...", style=wx.BU_EXACTFIT)
        self.schema = wx.TextCtrl(box)
        self.options = wx.TextCtrl(box)
        self.host_label = wx.StaticText(box, label="Host:")
        self.test_btn = wx.Button(box, label="Test connection")

        self.schema.SetHint("default")
        self.options.SetHint("key=value&key2=value2")
        self.browse.SetToolTip("Browse for a SQLite file, or list the server's databases/schemas")

        db_row = wx.BoxSizer(wx.HORIZONTAL)
        db_row.Add(self.database, 1, wx.EXPAND)
        db_row.Add(self.browse, 0, wx.LEFT, 4)

        grid = wx.FlexGridSizer(cols=2, vgap=5, hgap=8)
        grid.AddGrowableCol(1)
        for label, ctrl in (("Engine:", self.engine), (self.host_label, self.host), ("Port:", self.port),
                            ("User:", self.user), ("Password:", self.password),
                            ("Database / file:", db_row), ("Schema:", self.schema), ("URL options:", self.options)):
            grid.Add(label if isinstance(label, wx.StaticText) else wx.StaticText(box, label=label),
                     0, wx.ALIGN_CENTER_VERTICAL)
            grid.Add(ctrl, 1, wx.EXPAND)
        self.Add(grid, 1, wx.ALL | wx.EXPAND, 6)
        self.Add(self.test_btn, 0, wx.ALL | wx.ALIGN_RIGHT, 6)

        self.engine.Bind(wx.EVT_CHOICE, lambda e: self._apply_profile(reset=True))
        self.browse.Bind(wx.EVT_BUTTON, self._on_browse)
        self.test_btn.Bind(wx.EVT_BUTTON, self._on_test)
        self.set_config(ConnectionConfig(engine=engine))
        self._apply_profile(reset=True)

    def _apply_profile(self, reset: bool):
        profile = PROFILES[self.engine.GetStringSelection()]
        if reset:
            self.port.SetValue(profile.default_port)
            self.options.SetValue(profile.default_options)
        for ctrl in (self.host, self.port, self.user, self.password, self.schema, self.options):
            ctrl.Enable(not profile.file_based)
        self.host_label.SetLabel(profile.host_label + ":")
        self.database.SetHint("path\\to\\file.db" if profile.file_based else
                              "service name" if self.engine.GetStringSelection() == "Oracle" else "")

    def get_config(self) -> ConnectionConfig:
        return ConnectionConfig(engine=self.engine.GetStringSelection(), host=self.host.GetValue(),
                                port=self.port.GetValue(), user=self.user.GetValue(),
                                password=self.password.GetValue(), database=self.database.GetValue(),
                                schema=self.schema.GetValue(), options=self.options.GetValue())

    def set_config(self, cfg: ConnectionConfig):
        self.engine.SetStringSelection(cfg.engine if cfg.engine in PROFILES else "MySQL")
        self.host.SetValue(cfg.host)
        self.port.SetValue(cfg.port)
        self.user.SetValue(cfg.user)
        self.password.SetValue(cfg.password)
        self.database.SetValue(cfg.database)
        self.schema.SetValue(cfg.schema)
        self.options.SetValue(cfg.options)
        self._apply_profile(reset=False)

    def _run_async(self, button: wx.Button, work, done):
        button.Disable()
        wx.BeginBusyCursor()

        def worker():
            try:
                result, error = work(), None
            except Exception as exc:
                result, error = None, exc

            def finish():
                wx.EndBusyCursor()
                button.Enable()
                done(result, error)
            _call_after(self.parent, finish)
        threading.Thread(target=worker, daemon=True).start()

    def _on_test(self, _event):
        cfg = self.get_config()

        def done(message, error):
            if error:
                wx.MessageBox(f"Connection failed:\n\n{error}", "Test connection", wx.ICON_ERROR, self.parent)
            else:
                wx.MessageBox(message, "Test connection", wx.ICON_INFORMATION, self.parent)
        self._run_async(self.test_btn, lambda: test_connection(cfg), done)

    def _on_browse(self, _event):
        cfg = self.get_config()
        profile = PROFILES[cfg.engine]
        if profile.file_based:
            with wx.FileDialog(self.parent, "Choose SQLite database", defaultFile=cfg.database,
                               wildcard="SQLite databases (*.db;*.sqlite;*.sqlite3)|*.db;*.sqlite;*.sqlite3|All files|*.*",
                               style=wx.FD_OPEN) as dlg:
                if dlg.ShowModal() == wx.ID_OK:
                    self.database.SetValue(dlg.GetPath())
            return

        def done(names, error):
            if error:
                wx.MessageBox(f"Could not list schemas:\n\n{error}", "Browse", wx.ICON_ERROR, self.parent)
                return
            if not names:
                wx.MessageBox("No user schemas found.", "Browse", wx.ICON_INFORMATION, self.parent)
                return
            what = "database" if profile.family == "mysql" else "schema"
            with wx.SingleChoiceDialog(self.parent, f"Choose a {what}:", "Browse", names) as dlg:
                if dlg.ShowModal() == wx.ID_OK:
                    (self.database if profile.family == "mysql" else self.schema).SetValue(dlg.GetStringSelection())
        self._run_async(self.browse, lambda: list_schemas(cfg), done)


class MainFrame(wx.Frame):
    def __init__(self):
        super().__init__(None, title=f"Database Migration Tool {__version__}", size=(980, 860))
        self.cancel_event: threading.Event | None = None
        self.last_output: str | None = None
        self._build_menu()
        self._build_ui()
        self.Bind(wx.EVT_CLOSE, self._on_close)
        self.Centre()

    # -- layout --------------------------------------------------------------------------

    def _build_menu(self):
        menu = wx.Menu()
        open_item = menu.Append(wx.ID_OPEN, "&Open job...\tCtrl+O")
        save_item = menu.Append(wx.ID_SAVE, "&Save job...\tCtrl+S")
        menu.AppendSeparator()
        exit_item = menu.Append(wx.ID_EXIT, "E&xit")
        bar = wx.MenuBar()
        bar.Append(menu, "&File")
        self.SetMenuBar(bar)
        self.Bind(wx.EVT_MENU, self._on_open_job, open_item)
        self.Bind(wx.EVT_MENU, self._on_save_job, save_item)
        self.Bind(wx.EVT_MENU, lambda e: self.Close(), exit_item)

    def _build_ui(self):
        panel = wx.Panel(self)
        self.panel = panel
        self.source = ConnectionBox(panel, "Source database", "MySQL")
        self.target = ConnectionBox(panel, "Target database", "SQLite")
        conns = wx.BoxSizer(wx.HORIZONTAL)
        conns.Add(self.source, 1, wx.EXPAND | wx.RIGHT, 5)
        conns.Add(self.target, 1, wx.EXPAND | wx.LEFT, 5)

        opt = wx.StaticBoxSizer(wx.VERTICAL, panel, "Migration scope")
        ob = opt.GetStaticBox()
        self.chk = {}
        checks = wx.WrapSizer()
        for key, label, default in (("migrate_tables", "Tables && schema", True), ("migrate_data", "Table data", True),
                                    ("migrate_indexes", "Indexes", True), ("migrate_foreign_keys", "Foreign keys", True),
                                    ("migrate_routines", "Procedures && functions", False),
                                    ("migrate_triggers", "Triggers", False),
                                    ("migrate_views", "Views (copied as tables)", False),
                                    ("execute_objects", "Create objects on target (same engine family)", True),
                                    ("dry_run", "Dry run (write scripts only)", False)):
            cb = wx.CheckBox(ob, label=label)
            cb.SetValue(default)
            self.chk[key] = cb
            checks.Add(cb, 0, wx.ALL, 4)
        opt.Add(checks, 0, wx.EXPAND | wx.ALL, 2)

        self.if_exists = wx.Choice(ob, choices=list(IF_EXISTS_CHOICES))
        self.if_exists.SetStringSelection("replace")
        self.if_exists.SetToolTip("skip: leave existing tables alone\nappend: add rows\n"
                                  "truncate: delete rows, then copy\nreplace: drop, recreate, copy")
        self.batch = wx.SpinCtrl(ob, min=100, max=1_000_000, initial=5000)
        self.table_filter = wx.TextCtrl(ob)
        self.table_filter.SetHint("all tables  (e.g. customer*, orders)")
        self.raw_bytes = wx.TextCtrl(ob)
        self.raw_bytes.SetHint("e.g. users.UserPassword")
        self.raw_bytes.SetToolTip("Columns holding binary data in CHAR/VARCHAR; written to the target as bytes (BLOB)")
        self.raw_enc = wx.ComboBox(ob, value="cp1252", choices=["cp1252", "latin-1", "utf-8", ""])
        self.raw_enc.SetToolTip("Codec that turns those columns' text back into the original bytes.\n"
                                "cp1252 matches data written through a latin1 MySQL connection.\n"
                                "Blank = the server's stored bytes (CAST AS BINARY).")
        self.output_dir = wx.DirPickerCtrl(ob, path=os.path.abspath("migration_output"),
                                           style=wx.DIRP_USE_TEXTCTRL | wx.DIRP_SMALL)

        grid = wx.FlexGridSizer(cols=4, vgap=5, hgap=8)
        grid.AddGrowableCol(1)
        grid.AddGrowableCol(3)
        for label, ctrl in (("Existing tables:", self.if_exists), ("Batch size:", self.batch),
                            ("Tables:", self.table_filter), ("Raw-byte columns:", self.raw_bytes),
                            ("", wx.StaticText(ob)), ("Raw-byte encoding:", self.raw_enc)):
            grid.Add(wx.StaticText(ob, label=label), 0, wx.ALIGN_CENTER_VERTICAL)
            grid.Add(ctrl, 1, wx.EXPAND)
        opt.Add(grid, 0, wx.EXPAND | wx.ALL, 6)
        out_row = wx.BoxSizer(wx.HORIZONTAL)
        out_row.Add(wx.StaticText(ob, label="Output folder:"), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 8)
        out_row.Add(self.output_dir, 1, wx.EXPAND)
        opt.Add(out_row, 0, wx.EXPAND | wx.ALL, 6)

        self.btn_start = wx.Button(panel, label="Start migration")
        self.btn_cancel = wx.Button(panel, label="Cancel")
        self.btn_output = wx.Button(panel, label="Open output folder")
        self.btn_cancel.Disable()
        self.btn_output.Disable()
        self.btn_start.Bind(wx.EVT_BUTTON, self._on_start)
        self.btn_cancel.Bind(wx.EVT_BUTTON, self._on_cancel)
        self.btn_output.Bind(wx.EVT_BUTTON, lambda e: self.last_output and os.startfile(self.last_output))
        buttons = wx.BoxSizer(wx.HORIZONTAL)
        for b in (self.btn_start, self.btn_cancel, self.btn_output):
            buttons.Add(b, 0, wx.RIGHT, 8)

        self.gauge = wx.Gauge(panel, range=100)
        self.log_ctrl = wx.TextCtrl(panel, style=wx.TE_MULTILINE | wx.TE_READONLY | wx.HSCROLL | wx.TE_RICH2)
        self.log_ctrl.SetFont(wx.Font(9, wx.FONTFAMILY_TELETYPE, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_NORMAL))
        self.status = self.CreateStatusBar()

        root = wx.BoxSizer(wx.VERTICAL)
        root.Add(conns, 0, wx.EXPAND | wx.ALL, 10)
        root.Add(opt, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 10)
        root.Add(buttons, 0, wx.ALL, 10)
        root.Add(self.gauge, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 10)
        root.Add(self.log_ctrl, 1, wx.EXPAND | wx.ALL, 10)
        panel.SetSizer(root)
        frame_sizer = wx.BoxSizer(wx.VERTICAL)
        frame_sizer.Add(panel, 1, wx.EXPAND)
        self.SetSizer(frame_sizer)
        self.SetMinSize((820, 760))
        self.Layout()

    # -- job files ---------------------------------------------------------------------

    def _options(self) -> MigrationOptions:
        return MigrationOptions(**{k: cb.GetValue() for k, cb in self.chk.items()},
                                if_exists=self.if_exists.GetStringSelection(),
                                table_filter=self.table_filter.GetValue(),
                                raw_byte_columns=self.raw_bytes.GetValue(),
                                raw_byte_encoding=self.raw_enc.GetValue(),
                                batch_size=self.batch.GetValue(), output_dir=self.output_dir.GetPath())

    def _on_save_job(self, _event):
        with wx.FileDialog(self, "Save job", wildcard="Job files (*.json)|*.json",
                           style=wx.FD_SAVE | wx.FD_OVERWRITE_PROMPT) as dlg:
            if dlg.ShowModal() == wx.ID_OK:
                save_job(dlg.GetPath(), self.source.get_config(), self.target.get_config(), self._options())
                self.status.SetStatusText(f"Saved {dlg.GetPath()} (passwords are not stored)")

    def _on_open_job(self, _event):
        with wx.FileDialog(self, "Open job", wildcard="Job files (*.json)|*.json", style=wx.FD_OPEN) as dlg:
            if dlg.ShowModal() != wx.ID_OK:
                return
            try:
                src, tgt, opts = load_job(dlg.GetPath())
            except Exception as exc:
                wx.MessageBox(f"Could not read job:\n\n{exc}", "Open job", wx.ICON_ERROR, self)
                return
        self.source.set_config(src)
        self.target.set_config(tgt)
        for key, cb in self.chk.items():
            cb.SetValue(bool(getattr(opts, key)))
        self.if_exists.SetStringSelection(opts.if_exists)
        self.table_filter.SetValue(opts.table_filter)
        self.raw_bytes.SetValue(opts.raw_byte_columns)
        self.raw_enc.SetValue(opts.raw_byte_encoding)
        self.batch.SetValue(opts.batch_size)
        self.output_dir.SetPath(os.path.abspath(opts.output_dir))
        self.status.SetStatusText(f"Loaded {dlg.GetPath()}")

    # -- run ---------------------------------------------------------------------------

    def _on_start(self, _event):
        src, tgt, opts = self.source.get_config(), self.target.get_config(), self._options()
        if not src.database.strip() or not tgt.database.strip():
            wx.MessageBox("Enter a database (or SQLite file) for both source and target.", "Start migration",
                          wx.ICON_WARNING, self)
            return
        if opts.if_exists == "replace" and not opts.dry_run:
            answer = wx.MessageBox(f"Tables that already exist in the target ({tgt.engine} {tgt.database}) "
                                   f"will be DROPPED and recreated.\n\nContinue?", "Confirm replace",
                                   wx.YES_NO | wx.NO_DEFAULT | wx.ICON_WARNING, self)
            if answer != wx.YES:
                return

        self.cancel_event = threading.Event()
        self._set_running(True)
        self.gauge.SetValue(0)
        self.log_ctrl.Clear()

        def log(line):
            _call_after(self, self._append_log, line)

        def progress(pct):
            _call_after(self, self.gauge.SetValue, pct)

        def worker():
            report = MigrationEngine(src, tgt, opts, log, progress, self.cancel_event).run()
            _call_after(self, self._finished, report)
        threading.Thread(target=worker, daemon=True).start()

    def _append_log(self, line: str):
        colour = wx.RED if " ERROR " in line else wx.Colour(200, 120, 0) if " WARN " in line else wx.BLACK
        self.log_ctrl.SetDefaultStyle(wx.TextAttr(colour))
        self.log_ctrl.AppendText(line + "\n")

    def _set_running(self, running: bool):
        self.btn_start.Enable(not running)
        self.btn_cancel.Enable(running)
        self.status.SetStatusText("Running..." if running else "")

    def _finished(self, report):
        self._set_running(False)
        self.cancel_event = None
        if report.output_dir:
            self.last_output = str(report.output_dir)
            self.btn_output.Enable()
        if report.cancelled:
            self.status.SetStatusText("Cancelled")
        else:
            self.status.SetStatusText("Completed" if report.succeeded else f"Completed with {report.errors} error(s)")

    def _on_cancel(self, _event):
        if self.cancel_event:
            self.cancel_event.set()
            self.btn_cancel.Disable()
            self.status.SetStatusText("Cancelling after the current batch...")

    def _on_close(self, event):
        if self.cancel_event and event.CanVeto():
            if wx.MessageBox("A migration is running. Cancel it and exit?", "Exit",
                             wx.YES_NO | wx.NO_DEFAULT | wx.ICON_QUESTION, self) != wx.YES:
                event.Veto()
                return
            self.cancel_event.set()
        event.Skip()


def run_gui():
    app = wx.App(False)
    MainFrame().Show()
    app.MainLoop()
