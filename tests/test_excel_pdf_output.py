from pathlib import Path
from types import SimpleNamespace

from app.domain import ModelDefinition
import app.excel_executor as executor


class FakeRange:
    def __init__(self, book, sheet, cell):
        self.book = book
        self.sheet = sheet
        self.cell = cell

    @property
    def value(self):
        return self.book.values.get((self.sheet, self.cell))

    @value.setter
    def value(self, value):
        self.book.values[(self.sheet, self.cell)] = value


class FakeSheet:
    def __init__(self, book, name):
        self.book = book
        self.name = name

    def range(self, cell):
        return FakeRange(self.book, self.name, cell)


class FakeBook:
    opened_books = []

    def __init__(self, path):
        self.path = Path(path)
        self.name = self.path.name
        self.values = {}
        if "calc" in self.name:
            self.values.update({
                ("Input", "A1"): 100,
                    ("Input", "T11"): 17.32,
                    ("Input", "G74"): 100,
                ("Output", "B1"): 120.0,
            })
        self.sheets = FakeSheets(self)
        self.api = self
        self.opened_books.append(self)

    def save(self):
        self.path.write_bytes(b"private xlsm copy")

    def close(self):
        pass

    def ExportAsFixedFormat(self, export_type, filename, quality, include_props,
                            ignore_print_areas):
        assert export_type == 0
        assert filename.lower().endswith(".pdf")
        assert quality == 0
        assert include_props is True
        assert ignore_print_areas is False
        Path(filename).write_bytes(b"%PDF-1.4 fake exported pages")


class FakeSheets:
    def __init__(self, book):
        self.book = book

    def __getitem__(self, name):
        return FakeSheet(self.book, name)


class FakeBooks:
    def open(self, path, **kwargs):
        return FakeBook(path)


class FakeApp:
    def __init__(self, **kwargs):
        self.books = FakeBooks()
        self.api = SimpleNamespace(CalculateFullRebuild=lambda: None)
        self.display_alerts = True
        self.screen_updating = True
        self.enable_events = True

    def quit(self):
        pass


def test_executor_keeps_editable_workbooks_private_and_publishes_only_pdfs(tmp_path: Path, monkeypatch):
    FakeBook.opened_books.clear()
    monkeypatch.setattr(executor, "require_interactive_windows_session", lambda: 3)
    monkeypatch.setitem(__import__("sys").modules, "xlwings", SimpleNamespace(App=FakeApp))

    template_root = tmp_path / "templates"
    template_root.mkdir()
    (template_root / "calc.xlsm").write_bytes(b"template-a")
    (template_root / "heater.xlsm").write_bytes(b"template-b")
    artifact_dir = tmp_path / "public-artifacts" / "job"
    private_dir = tmp_path / "private-workbooks" / "job"
    model = ModelDefinition.model_validate({
        "schema_version": "2.0",
        "model": {"id": "two-book-test", "version": 1, "name": "Two book test"},
        "parameters": {
            "amount": {"display_name": "Amount", "description": "Amount", "required": True, "missing": {"policy": "error"}},
            "optional_hydrogen": {"display_name": "Hydrogen", "description": "Optional hydrogen", "required": False, "missing": {"policy": "template_default"}},
            "optional_temperature": {"display_name": "Temperature", "description": "Optional temperature", "required": False, "missing": {"policy": "template_default"}},
        },
        "results": {
            "thermal_result": {"display_name": "Calculation result", "type": "number"}
        },
        "artifacts": {
            "calc": {"type": "excel", "template": "calc.xlsm"},
            "heater": {"type": "excel", "template": "heater.xlsm"},
        },
        "bindings": {
            "calc": {
                "inputs": {
                    "amount": {"sheet": "Input", "cell": "A1"},
                    "optional_hydrogen": {"sheet": "Input", "cell": "T11"},
                    "optional_temperature": {"sheet": "Input", "cell": "G74"},
                },
                "outputs": {"thermal_result": {"sheet": "Output", "cell": "B1"}},
            },
            "heater": {
                "inputs": {"thermal_result": {"sheet": "Input", "cell": "A1"}},
                "outputs": {},
            },
        },
        "workflow": {"steps": [
            {"id": "calc", "type": "excel_calculation", "artifact": "calc"},
            {"id": "heater", "type": "excel_calculation", "artifact": "heater", "depends_on": ["calc"]},
        ]},
        "handoffs": [{"source": "calc.thermal_result", "target": "heater.thermal_result"}],
    })

    stages = []
    results, artifacts = executor.execute_model(
        model, {"amount": 10}, template_root, artifact_dir, private_dir,
        on_stage=stages.append,
    )

    assert results == {"thermal_result": 120.0}
    calc_book = FakeBook.opened_books[0]
    assert calc_book.values[("Input", "A1")] == 10
    assert calc_book.values[("Input", "T11")] == 17.32
    assert calc_book.values[("Input", "G74")] == 100
    heater_book = FakeBook.opened_books[1]
    assert heater_book.values[("Input", "A1")] == 120.0
    assert len(artifacts) == 2
    assert all(artifact["kind"] == "pdf" for artifact in artifacts)
    assert [artifact["file_name"] for artifact in artifacts] == ["calc.pdf", "heater.pdf"]
    assert all(Path(artifact["file_path"]).parent == artifact_dir.resolve() for artifact in artifacts)
    assert all(Path(artifact["file_path"]).read_bytes().startswith(b"%PDF-") for artifact in artifacts)
    private_workbooks = list(private_dir.glob("*.xlsm"))
    assert len(private_workbooks) == 2
    assert all("xlsm" not in artifact["file_name"].lower() for artifact in artifacts)
    assert not list(artifact_dir.glob("*.xlsm"))
    assert stages == [
        "calc_running", "calc_pdf_export",
        "heater_running", "heater_pdf_export",
    ]
