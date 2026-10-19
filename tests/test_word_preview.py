import sys
from pathlib import Path
from types import ModuleType

import pytest

import app.reporting as reporting


class FakeDocument:
    def __init__(self):
        self.closed = None

    def ExportAsFixedFormat(self, output_path, export_format):
        assert export_format == 17
        Path(output_path).write_bytes(b"%PDF-1.4 word preview")

    def Close(self, save_changes):
        self.closed = save_changes


class FakeDocuments:
    def __init__(self, document):
        self.document = document

    def Open(self, path, **kwargs):
        assert Path(path).is_file()
        assert kwargs["ReadOnly"] is True
        return self.document


class FakeWord:
    def __init__(self, document):
        self.Documents = FakeDocuments(document)
        self.DisplayAlerts = None
        self.Visible = None
        self.quit = False

    def Quit(self):
        self.quit = True


def test_word_report_exports_pdf_preview_without_replacing_docx(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(reporting, "_is_windows", lambda: True)
    docx_path = tmp_path / "calculation-report.docx"
    docx_path.write_bytes(b"original docx")
    pdf_path = tmp_path / "calculation-report-preview.pdf"
    document = FakeDocument()
    word = FakeWord(document)

    client_module = ModuleType("win32com.client")
    client_module.DispatchEx = lambda name: word
    package_module = ModuleType("win32com")
    package_module.client = client_module
    monkeypatch.setitem(sys.modules, "win32com", package_module)
    monkeypatch.setitem(sys.modules, "win32com.client", client_module)

    result = reporting.export_word_preview(docx_path, pdf_path)
    assert result == pdf_path
    assert pdf_path.read_bytes().startswith(b"%PDF-")
    assert docx_path.read_bytes() == b"original docx"
    assert document.closed is False
    assert word.quit is True


def test_word_pdf_preview_rejects_non_windows(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(reporting, "_is_windows", lambda: False)
    with pytest.raises(RuntimeError, match="仅支持 Windows"):
        reporting.export_word_preview(tmp_path / "report.docx", tmp_path / "report.pdf")