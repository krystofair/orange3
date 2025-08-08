from AnyQt.QtWidgets import QComboBox, QTextEdit, QMessageBox, QApplication
from AnyQt.QtGui import QCursor
from AnyQt.QtCore import Qt

from Orange.data import Table
from Orange.data.sql.backend import Backend
from Orange.data.sql.backend.base import BackendError
from Orange.data.sql.table import SqlTable, LARGE_TABLE, AUTO_DL_LIMIT
from Orange.widgets import gui
from Orange.widgets.settings import Setting
from Orange.widgets.utils.itemmodels import PyListModel
from Orange.widgets.utils.owbasesql import OWBaseSql
from Orange.widgets.utils.widgetpreview import WidgetPreview
from Orange.widgets.widget import Output, Msg

MAX_DL_LIMIT = 1000000


def is_postgres(backend):
    return getattr(backend, 'display_name', '') == "PostgreSQL"


class TableModel(PyListModel):
    def data(self, index, role=Qt.DisplayRole):
        row = index.row()
        if role == Qt.DisplayRole:
            return str(self[row])
        return super().data(index, role)


class BackendModel(PyListModel):
    def data(self, index, role=Qt.DisplayRole):
        row = index.row()
        if role == Qt.DisplayRole:
            return self[row].display_name
        return super().data(index, role)


class OWSql(OWBaseSql):
    name = "SQL Table"
    id = "orange.widgets.data.sql"
    description = "Load dataset from SQL."
    icon = "icons/SQLTable.svg"
    priority = 30
    category = "Data"
    keywords = "sql table, load"

    class Outputs:
        data = Output("Data", Table, doc="Attribute-valued dataset read from the input file.")

    settings_version = 2

    buttons_area_orientation = None

    selected_backend = Setting(None)
    table = Setting(None)
    sql = Setting("")
    guess_values = Setting(True)
    download = Setting(False)

    path_to_res_file = Setting("")

    materialize = Setting(False)
    materialize_table_name = Setting("")

    class Information(OWBaseSql.Information):
        data_sampled = Msg("Data description was generated from a sample.")

    class Warning(OWBaseSql.Warning):
        missing_extension = Msg("Database is missing extensions: {}")

    class Error(OWBaseSql.Error):
        no_backends = Msg("Please install a backend to use this widget.")
        file_os_error = Msg("File doesn't exist or you don't have read permission.")
        empty_path = Msg("Path cannot be empty.")

    def __init__(self):
        # Lint
        self.backends = None
        self.backendcombo = None
        self.tables = None
        self.tablecombo = None
        self.sqltext = None
        self.custom_sql = None
        self.file_controls = None
        self.file_queries = {}  # not None to be checkable right away
        self.downloadcb = None
        super().__init__()

    def _setup_gui(self):
        super()._setup_gui()
        self._add_backend_controls()
        self._add_tables_controls()

    def _load_tables_from_file(self):
        if not self.path_to_res_file:
            self.Error.empty_path()
            return
        self.Error.empty_path.clear()
        try:
            selects = self._parse_sql_resource_file(self.path_to_res_file)
        except OSError:
            self.Error.file_os_error()
        else:
            self.Error.file_os_error.clear()
            self.file_queries.update(selects)
            self.refresh_tables()

    def _parse_sql_resource_file(self, filename):
        """throws `OSError` - file may be unable to open"""
        import re, os
        # for simple queries this will work, assume that user can only SELECT from.
        selects = {}  # name:query
        # This order is precious:
        name_pattern = re.compile("^-- name:(.*)$")
        ignore_line = re.compile("^--.*")
        start_query = re.compile("^SELECT|select")
        stop_query = re.compile(";$");
        # lambda to uni transformation
        trn = lambda x: str(x).strip().upper()
        def delete_comments(line):
            return line[:line.find('--')]
        with open(self.path_to_res_file, 'r', encoding='utf-8') as resfile:
            name = None
            query = []
            while line := resfile.readline():
                # Name as first line, will be overriden when line duplicate
                if n := name_pattern.search(line):
                    name = n.group(1)
                # ignore comment lines
                elif ignore_line.match(line):
                    continue
                # oneline query has start and stop in single line
                elif start_query.search(line) and stop_query.search(line):
                    selects[trn(name)] = line
                    name = None
                # another if-tree,
                # start analyzing lines as query
                if start_query.search(line) and name is not None:
                    query.append(line)  # sets state of appending consecutive lines
                elif stop_query.search(line) and name is not None:
                    query.append(line)  # add last line
                    # process query
                    selects[trn(name)] = ' '.join(map(delete_comments, query))
                    query = []  # reset state
                    name = None
                # query is not empty - means query is loading as consecutive lines
                elif query:
                    query.append(line)
        return selects

    def _add_backend_controls(self):
        box = self.serverbox
        self.backends = BackendModel(Backend.available_backends())
        self.backendcombo = QComboBox(box)
        if self.backends:
            self.backendcombo.setModel(self.backends)
            names = [backend.display_name for backend in self.backends]
            if self.selected_backend and self.selected_backend in names:
                self.backendcombo.setCurrentText(self.selected_backend)
        else:
            self.Error.no_backends()
            box.setEnabled(False)
        self.backendcombo.currentTextChanged.connect(self.__backend_changed)
        box.layout().insertWidget(0, self.backendcombo)

    def __backend_changed(self):
        backend = self.get_backend()
        self.selected_backend = backend.display_name if backend else None

    def _add_file_controls(self, box):
        self.file_controls = gui.vBox(box)
        self.file_controls.setVisible(False)
        mt = gui.hBox(self.file_controls)
        le = gui.lineEdit(mt, self, 'path_to_res_file')
        le.setToolTip('Path to file you load queries from. Be careful with choose.')
        load = gui.button(self.file_controls, self, 'Load tables from file',
                          callback=self._load_tables_from_file)

    def _add_tables_controls(self):
        vbox = gui.vBox(self.controlArea, "Tables")
        box = gui.vBox(vbox)
        self.tables = TableModel()

        self.tablecombo = QComboBox(
            minimumContentsLength=35,
            sizeAdjustPolicy=QComboBox.AdjustToMinimumContentsLengthWithIcon
        )
        self.tablecombo.setModel(self.tables)
        self.tablecombo.setToolTip('table')
        self.tablecombo.activated[int].connect(self.select_table)
        box.layout().addWidget(self.tablecombo)

        self._add_file_controls(box)

        self.custom_sql = gui.vBox(box)
        self.custom_sql.setVisible(False)
        self.sqltext = QTextEdit(self.custom_sql)
        self.sqltext.setPlainText(self.sql)
        self.custom_sql.layout().addWidget(self.sqltext)


        mt = gui.hBox(self.custom_sql)

        cb = gui.checkBox(mt, self, 'materialize', 'Materialize to table ')
        cb.setToolTip('Save results of the query in a table')
        le = gui.lineEdit(mt, self, 'materialize_table_name')
        le.setToolTip('Save results of the query in a table')

        gui.button(self.custom_sql, self, 'Execute', callback=self.open_table)

        box.layout().addWidget(self.custom_sql)

        gui.checkBox(box, self, "guess_values",
                     "Auto-discover categorical variables",
                     callback=self.open_table)

        self.downloadcb = gui.checkBox(box, self, "download",
                                       "Download data to local memory",
                                       callback=self.open_table)

    def highlight_error(self, text=""):
        err = ['', 'QLineEdit {border: 2px solid red;}']
        self.servertext.setStyleSheet(err['server' in text or 'host' in text])
        self.usernametext.setStyleSheet(err['role' in text])
        self.databasetext.setStyleSheet(err['database' in text])

    def get_backend(self):
        if self.backendcombo.currentIndex() < 0:
            return None
        return self.backends[self.backendcombo.currentIndex()]

    def on_connection_success(self):
        if getattr(self.backend, 'missing_extension', False):
            self.Warning.missing_extension(
                ", ".join(self.backend.missing_extension))
            self.download = True
            self.downloadcb.setEnabled(False)
        if not is_postgres(self.backend):
            self.download = True
            self.downloadcb.setEnabled(False)
        super().on_connection_success()
        self.refresh_tables()
        self.select_table()

    def on_connection_error(self, err):
        super().on_connection_error(err)
        self.highlight_error(str(err).split("\n")[0])

    def clear(self):
        super().clear()
        self.Warning.missing_extension.clear()
        self.downloadcb.setEnabled(True)
        self.highlight_error()
        self.tablecombo.clear()
        self.tablecombo.repaint()

    def refresh_tables(self):
        self.tables.clear()
        if self.backend is None:
            self.data_desc_table = None
            return

        self.tables.append("Select a table")
        self.tables.append("Custom SQL")
        self.file_controls.setVisible(True)
        self.tables.extend(self.file_queries.keys())
        self.tables.extend(self.backend.list_tables(self.schema))
        index = self.tablecombo.findText(str(self.table))
        self.tablecombo.setCurrentIndex(index if index != -1 else 0)
        self.tablecombo.repaint()

    # Called on tablecombo selection change:
    def select_table(self):
        curIdx = self.tablecombo.currentIndex()
        table_name = self.tablecombo.itemText(curIdx)

        if table_name != "Custom SQL" and not table_name in self.file_queries:
            self.custom_sql.setVisible(False)
            return self.open_table()
        else:
            self.custom_sql.setVisible(True)
            self.data_desc_table = None
            self.database_desc["Table"] = "(None)"
            self.table = None
            if table_name in self.file_queries:
                self.sqltext.setPlainText(self.file_queries[table_name])
            # repaint custom_sql with loaded query
            # ivokes custom query automatically or file query - disabled.
            # if len(str(self.sql)) > 14:
            #     return self.open_table()
        return None


    def get_table(self):
        curIdx = self.tablecombo.currentIndex()
        if curIdx <= 0:
            if self.database_desc:
                self.database_desc["Table"] = "(None)"
            self.data_desc_table = None
            return None

        if self.tablecombo.itemText(curIdx) in self.file_queries:
            self.table = self.tables[self.tablecombo.currentIndex()]
            self.database_desc["Table"] = "Loaded from File"
            # from textarea, because of possible modifications
            # and sqltext is loaded from :meth:`select_table`
            what = self.sql = self.sqltext.toPlainText()
        elif self.tablecombo.itemText(curIdx) != "Custom SQL":
            self.table = self.tables[self.tablecombo.currentIndex()]
            self.database_desc["Table"] = self.table
            if "Query" in self.database_desc:
                del self.database_desc["Query"]
            what = self.table
        else:
            what = self.sql = self.sqltext.toPlainText()
            self.table = "Custom SQL"
            if self.materialize:
                if not self.materialize_table_name:
                    self.Error.connection(
                        "Specify a table name to materialize the query")
                    return None
                try:
                    with self.backend.execute_sql_query("DROP TABLE IF EXISTS " +
                                                        self.materialize_table_name):
                        pass
                    with self.backend.execute_sql_query("CREATE TABLE " +
                                                        self.materialize_table_name +
                                                        " AS " + self.sql):
                        pass
                    with self.backend.execute_sql_query("ANALYZE " + self.materialize_table_name):
                        pass
                except BackendError as ex:
                    self.Error.connection(str(ex))
                    return None

        try:
            table = SqlTable(dict(host=self.host,
                                  port=self.port,
                                  database=self.database,
                                  user=self.username,
                                  password=self.password),
                             what,
                             backend=type(self.backend),
                             inspect_values=False)
        except BackendError as ex:
            self.Error.connection(str(ex))
            return None

        self.Error.connection.clear()

        sample = False

        if table.approx_len() > LARGE_TABLE and self.guess_values:
            confirm = QMessageBox(self)
            confirm.setIcon(QMessageBox.Warning)
            confirm.setText("Attribute discovery might take "
                            "a long time on large tables.\n"
                            "Do you want to auto discover attributes?")
            confirm.addButton("Yes", QMessageBox.YesRole)
            no_button = confirm.addButton("No", QMessageBox.NoRole)
            if is_postgres(self.backend):
                sample_button = confirm.addButton("Yes, on a sample",
                                                  QMessageBox.YesRole)
            confirm.exec()
            if confirm.clickedButton() == no_button:
                self.guess_values = False
            elif is_postgres(self.backend) and \
                    confirm.clickedButton() == sample_button:
                sample = True

        self.Information.clear()
        if self.guess_values:
            QApplication.setOverrideCursor(QCursor(Qt.WaitCursor))
            if sample:
                s = table.sample_time(1)
                domain = s.get_domain(inspect_values=True)
                self.Information.data_sampled()
            else:
                domain = table.get_domain(inspect_values=True)
            QApplication.restoreOverrideCursor()
            table.domain = domain

        if self.download:
            if table.approx_len() > AUTO_DL_LIMIT:
                if is_postgres(self.backend):
                    confirm = QMessageBox(self)
                    confirm.setIcon(QMessageBox.Warning)
                    confirm.setText("Data appears to be big. Do you really "
                                    "want to download it to local memory?\n"
                                    "Table length: {:,}. Limit {:,}".format(table.approx_len(), MAX_DL_LIMIT))

                    if table.approx_len() <= MAX_DL_LIMIT:
                        confirm.addButton("Yes", QMessageBox.YesRole)
                    no_button = confirm.addButton("No", QMessageBox.NoRole)
                    sample_button = confirm.addButton("Yes, a sample",
                                                      QMessageBox.YesRole)
                    confirm.exec()
                    if confirm.clickedButton() == no_button:
                        return None
                    elif confirm.clickedButton() == sample_button:
                        table = table.sample_percentage(
                            AUTO_DL_LIMIT / table.approx_len() * 100)
                else:
                    if table.approx_len() > MAX_DL_LIMIT:
                        QMessageBox.warning(
                            self, 'Warning',
                            "Data is too big to download.\n"
                            "Table length: {:,}. Limit {:,}".format(table.approx_len(), MAX_DL_LIMIT)
                        )
                        return None
                    else:
                        confirm = QMessageBox.question(
                            self, 'Question',
                            "Data appears to be big. Do you really "
                            "want to download it to local memory?",
                            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
                        if confirm == QMessageBox.No:
                            return None

            table.download_data(MAX_DL_LIMIT)
            table = Table(table)

        return table

    @classmethod
    def migrate_settings(cls, settings, version):
        if version < 2:
            # Until Orange version 3.4.4 username and password had been stored
            # in Settings.
            cm = cls._credential_manager(settings["host"], settings["port"])
            cm.username = settings["username"]
            cm.password = settings["password"]


if __name__ == "__main__":  # pragma: no cover
    WidgetPreview(OWSql).run()
