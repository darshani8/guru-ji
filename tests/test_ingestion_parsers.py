import io
import json
import unittest
import zipfile

from app.ingestion import parsers
from app.ingestion.detector import detect_file_kind
from app.ingestion.models import FileKind, ParserError, ParserUnavailable
from app.ingestion.parsers import archive as archive_guard
from app.ingestion.parsers import pdf_parser
from app.ingestion.parsers.csv_parser import parse_csv
from app.ingestion.parsers.docx_parser import parse_docx
from app.ingestion.parsers.excel_parser import parse_xlsx
from app.ingestion.parsers.image_parser import parse_image
from app.ingestion.parsers.json_parser import parse_json
from app.ingestion.parsers.ocr import OcrResult, OcrUnavailable, parse_textract_blocks
from app.ingestion.parsers.tabular import detect_header_row
from app.ingestion.parsers.text_layout import key_value_fields, text_to_table
from app.ingestion.registry import ParserRegistry


def build_xlsx(sheets: dict[str, list[list[object]]]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        overrides = "".join(f'<Override PartName="/xl/worksheets/sheet{i}.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>' for i in range(1, len(sheets) + 1))
        archive.writestr("[Content_Types].xml", f'<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>{overrides}</Types>')
        archive.writestr("_rels/.rels", '<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>')
        names = "".join(f'<sheet name="{name}" sheetId="{i}" r:id="rId{i}"/>' for i, name in enumerate(sheets, start=1))
        archive.writestr("xl/workbook.xml", f'<?xml version="1.0"?><workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets>{names}</sheets></workbook>')
        rels = "".join(f'<Relationship Id="rId{i}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet{i}.xml"/>' for i in range(1, len(sheets) + 1))
        archive.writestr("xl/_rels/workbook.xml.rels", f'<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">{rels}</Relationships>')
        strings = ["shared one"]
        archive.writestr("xl/sharedStrings.xml", '<?xml version="1.0"?><sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><si><t>shared one</t></si></sst>')
        archive.writestr("xl/styles.xml", '<?xml version="1.0"?><styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><cellXfs count="2"><xf numFmtId="0"/><xf numFmtId="14"/></cellXfs></styleSheet>')
        for index, rows in enumerate(sheets.values(), start=1):
            xml = ['<?xml version="1.0"?><worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>']
            for r, row in enumerate(rows, start=1):
                cells = []
                for c, value in enumerate(row):
                    ref = f"{chr(65 + c)}{r}"
                    if value == "__shared__":
                        cells.append(f'<c r="{ref}" t="s"><v>0</v></c>')
                    elif value == "__date__":
                        cells.append(f'<c r="{ref}" s="1"><v>45000</v></c>')
                    elif isinstance(value, bool):
                        cells.append(f'<c r="{ref}" t="b"><v>{int(value)}</v></c>')
                    elif isinstance(value, (int, float)):
                        cells.append(f'<c r="{ref}"><v>{value}</v></c>')
                    else:
                        cells.append(f'<c r="{ref}" t="inlineStr"><is><t>{value}</t></is></c>')
                xml.append(f'<row r="{r}">' + "".join(cells) + "</row>")
            xml.append("</sheetData></worksheet>")
            archive.writestr(f"xl/worksheets/sheet{index}.xml", "".join(xml))
        del strings
    return buffer.getvalue()


def build_docx(paragraphs: list[str], table: list[list[str]] | None = None) -> bytes:
    body = "".join(f"<w:p><w:r><w:t>{text}</w:t></w:r></w:p>" for text in paragraphs)
    if table:
        rows = "".join("<w:tr>" + "".join(f"<w:tc><w:p><w:r><w:t>{cell}</w:t></w:r></w:p></w:tc>" for cell in row) + "</w:tr>" for row in table)
        body += f"<w:tbl>{rows}</w:tbl>"
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("[Content_Types].xml", '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="xml" ContentType="application/xml"/></Types>')
        archive.writestr("word/document.xml", f'<?xml version="1.0"?><w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>{body}</w:body></w:document>')
    return buffer.getvalue()


def rewrite_entry(content: bytes, name: str, transform) -> bytes:
    """Rebuild a zip archive (deflated) with one entry's bytes transformed."""

    source = zipfile.ZipFile(io.BytesIO(content))
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as out:
        for info in source.infolist():
            data = source.read(info.filename)
            out.writestr(info.filename, transform(data) if info.filename == name else data)
    return buffer.getvalue()


class _FixtureOcr:
    engine_name = "fixture"

    def __init__(self, text: str, confidence: float = 0.9):
        self.text = text
        self.confidence = confidence

    def recognize(self, image_bytes: bytes, *, content_type: str = "image/png") -> OcrResult:
        return OcrResult(text=self.text, confidence=self.confidence, engine=self.engine_name)


class ParserTests(unittest.TestCase):
    def test_detection_prefers_magic_bytes_over_extension(self):
        self.assertEqual(detect_file_kind("scan.xlsx", b"%PDF-1.4 ..."), FileKind.PDF)
        self.assertEqual(detect_file_kind("photo.bin", b"\x89PNG\r\n"), FileKind.IMAGE)
        self.assertEqual(detect_file_kind("students.csv", b"a,b\n1,2"), FileKind.CSV)
        self.assertEqual(detect_file_kind("data", b"a\tb\n1\t2", "text/tab-separated-values"), FileKind.TSV)
        self.assertEqual(detect_file_kind("x.docx", build_docx(["hi"])), FileKind.DOCX)
        self.assertEqual(detect_file_kind("x.xlsx", build_xlsx({"S": [["a"]]})), FileKind.XLSX)
        self.assertEqual(detect_file_kind("blob", b"\x00\x01\x02"), FileKind.UNKNOWN)

    def test_csv_header_detection_skips_titles_and_dedupes_headers(self):
        content = "ABC College\nStudent list 2026\n\nName,USN,Sem,Name\nRavi,1MS23MBA001,1,x\n,,,\nAsha,1MS23MBA002,2,y\n".encode("utf-8-sig")
        result = parse_csv("students.csv", content)
        table = result.tables[0]
        self.assertEqual(table.headers, ("Name", "USN", "Sem", "Name (2)"))
        self.assertEqual(table.row_count, 2)
        self.assertEqual(table.records[0].locator, "csv;row=5")
        self.assertIn("skipped_3_leading_rows", table.warnings)
        self.assertIsNone(detect_header_row([["1", "2"], ["3", "4"]]))

    def test_xlsx_reader_handles_shared_strings_dates_and_multiple_sheets(self):
        content = build_xlsx({"MBA": [["Name", "Student ID", "Joined", "Active"], ["__shared__", "MBA001", "__date__", True], ["Asha", 42, "2026-01-05", False]], "Notes": [["only a title"]]})
        result = parse_xlsx("students.xlsx", content)
        self.assertEqual(result.metadata["engine"], "stdlib")
        table = next(item for item in result.tables if item.name == "MBA")
        self.assertEqual(table.headers, ("Name", "Student ID", "Joined", "Active"))
        self.assertEqual(table.records[0].fields["Name"], "shared one")
        self.assertEqual(table.records[0].fields["Joined"], "2023-03-15")
        self.assertIs(table.records[0].fields["Active"], True)
        self.assertEqual(table.records[1].fields["Student ID"], 42)

    def test_docx_parser_returns_text_and_tables(self):
        result = parse_docx("staff.docx", build_docx(["Faculty list", "Department of MBA"], [["Name", "Designation"], ["Dr Meena", "Professor"], ["Mr Rao", "Assistant Professor"]]))
        self.assertEqual(result.tables[0].headers, ("Name", "Designation"))
        self.assertEqual(result.tables[0].records[1].fields["Name"], "Mr Rao")
        self.assertIn("Department of MBA", result.full_text())

    def test_json_parser_finds_row_arrays(self):
        payload = {"meta": {"x": 1}, "students": [{"name": "A", "usn": "1"}, {"name": "B", "usn": "2", "extra": True}]}
        result = parse_json("export.json", json.dumps(payload).encode())
        self.assertEqual(result.tables[0].headers, ("name", "usn", "extra"))
        self.assertEqual(result.tables[0].row_count, 2)

    def test_text_layout_detects_tables_and_form_fields(self):
        text = "USN          Name         Attendance\n1MS23MBA001  Ravi Kumar   72%\n1MS23MBA002  Asha Rao     91%\n1MS23MBA003  Kiran        65%\n"
        table = text_to_table(text, name="layout", source_file="scan.png", page=1, ocr=True, ocr_confidence=0.9)
        self.assertIsNotNone(table)
        self.assertEqual(table.headers, ("USN", "Name", "Attendance"))
        self.assertEqual(table.records[0].fields["Attendance"], "72%")
        fields = key_value_fields("Name: Ravi Kumar\nDate of Birth: 12/05/2003\nProgram - MBA\nSignature")
        self.assertEqual(fields["Name"], "Ravi Kumar")
        self.assertEqual(fields["Program"], "MBA")

    def test_image_parser_uses_ocr_engine_and_flags_low_confidence(self):
        result = parse_image("form.png", b"\x89PNG", ocr_engine=_FixtureOcr("Name: Ravi\nUSN: 1MS23MBA001\nProgram: MBA\nSem: 1", confidence=0.6))
        self.assertEqual(result.tables[0].name, "form_fields")
        self.assertEqual(result.tables[0].records[0].fields["USN"], "1MS23MBA001")
        self.assertTrue(result.tables[0].records[0].ocr)
        self.assertIn("low_ocr_confidence", result.warnings)
        with self.assertRaises(ParserUnavailable):
            parse_image("form.png", b"\x89PNG")

    def test_textract_blocks_become_tables(self):
        blocks = [
            {"Id": "l1", "BlockType": "LINE", "Text": "Attendance register", "Confidence": 99.0},
            {"Id": "t1", "BlockType": "TABLE", "Relationships": [{"Type": "CHILD", "Ids": ["c1", "c2", "c3", "c4"]}]},
            {"Id": "c1", "BlockType": "CELL", "RowIndex": 1, "ColumnIndex": 1, "Relationships": [{"Type": "CHILD", "Ids": ["w1"]}]},
            {"Id": "c2", "BlockType": "CELL", "RowIndex": 1, "ColumnIndex": 2, "Relationships": [{"Type": "CHILD", "Ids": ["w2"]}]},
            {"Id": "c3", "BlockType": "CELL", "RowIndex": 2, "ColumnIndex": 1, "Relationships": [{"Type": "CHILD", "Ids": ["w3"]}]},
            {"Id": "c4", "BlockType": "CELL", "RowIndex": 2, "ColumnIndex": 2, "Relationships": [{"Type": "CHILD", "Ids": ["w4"]}]},
            {"Id": "w1", "BlockType": "WORD", "Text": "USN"}, {"Id": "w2", "BlockType": "WORD", "Text": "Present"},
            {"Id": "w3", "BlockType": "WORD", "Text": "MBA001"}, {"Id": "w4", "BlockType": "WORD", "Text": "12"},
        ]
        result = parse_textract_blocks(blocks)
        self.assertEqual(result.tables[0], (("USN", "Present"), ("MBA001", "12")))
        self.assertEqual(result.confidence, 0.99)

    def test_pdf_requires_an_engine_and_decides_ocr_page_by_page(self):
        with self.assertRaises(ParserUnavailable):
            pdf_parser.parse_pdf("x.pdf", b"%PDF-1.4\n")
        original = pdf_parser.extract_pdf_pages
        pdf_parser.extract_pdf_pages = lambda content: ([
            {"number": 1, "text": "Fee policy\nTuition is payable in two instalments each semester, with penalties after the due date.", "tables": [[["USN", "Paid"], ["MBA001", "5000"]]], "image": None},
            {"number": 2, "text": "", "tables": [], "image": b"\x89PNG"},
            {"number": 3, "text": "", "tables": [], "image": None},
        ], "fake")
        try:
            result = pdf_parser.parse_pdf("fees.pdf", b"%PDF-1.4\n", ocr_engine=_FixtureOcr("USN   Paid\nMBA002  4000\nMBA003  3000\nMBA004  2500"))
        finally:
            pdf_parser.extract_pdf_pages = original
        self.assertEqual(result.metadata["ocr_pages"], 1)
        self.assertEqual(result.metadata["text_pages"], 1)
        self.assertIn("page_3_needs_ocr", result.warnings)
        self.assertEqual(result.tables[0].records[0].fields["USN"], "MBA001")
        ocr_table = next(table for table in result.tables if table.ocr)
        self.assertEqual(ocr_table.records[0].fields["Paid"], "4000")
        self.assertEqual(len(result.texts), 2)

    def test_registry_rejects_empty_and_oversized_files(self):
        registry = ParserRegistry(max_bytes=10)
        with self.assertRaises(ValueError):
            registry.parse("x.csv", b"")
        with self.assertRaises(ValueError):
            registry.parse("x.csv", b"a,b\n" * 10)
        self.assertIn(FileKind.IMAGE, registry.supported_kinds())
        self.assertTrue(parsers.__doc__)

    def test_parsers_raise_parser_error_for_their_own_failure_modes(self):
        # An unbalanced quote swallows the rest of the file into one field (csv.Error).
        with self.assertRaises(ParserError):
            parse_csv("bad.csv", b"a,b\n\"" + b"x" * 200_000 + b"\n1,2\n")
        # A zip with xl/ entries but no workbook index (KeyError before).
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as bad:
            bad.writestr("xl/other.xml", "<x/>")
        with self.assertRaises(ParserError):
            parse_xlsx("bad.xlsx", buffer.getvalue())
        # Malformed workbook parts are reported, not raised as ElementTree.ParseError.
        broken = rewrite_entry(build_xlsx({"S": [["a", "b"], [1, 2]]}), "xl/sharedStrings.xml", lambda data: b"<sst><si><t>unterminated")
        with self.assertRaises(ParserError):
            parse_xlsx("broken.xlsx", broken)
        broken_sheet = rewrite_entry(build_xlsx({"S": [["a", "b"], [1, 2]]}), "xl/worksheets/sheet1.xml", lambda data: data[: len(data) // 2])
        self.assertIn("sheet_unreadable:S", parse_xlsx("broken.xlsx", broken_sheet).warnings)
        with self.assertRaises(ParserError):
            parse_docx("bad.docx", rewrite_entry(build_docx(["hi"]), "word/document.xml", lambda data: data[:20]))
        # Deeply nested JSON (RecursionError before).
        with self.assertRaises(ParserError):
            parse_json("deep.json", b"[" * 100_000 + b"]" * 100_000)

    def test_zip_bombs_are_rejected_before_inflating(self):
        base = build_xlsx({"S": [["a", "b"], [1, 2]]})
        # 1.5 MB of padding deflates to a few KB: ratio far above 100:1.
        pad = lambda data: data.replace(b"</sheetData>", b"<!--" + b" " * 1_500_000 + b"--></sheetData>")
        bomb = rewrite_entry(base, "xl/worksheets/sheet1.xml", pad)
        self.assertLess(len(bomb), 20_000)
        with self.assertRaises(ParserError) as raised:
            parse_xlsx("bomb.xlsx", bomb)
        self.assertIn("compression ratio", str(raised.exception))
        with self.assertRaises(ParserError):
            parse_xlsx("bomb.xlsx", rewrite_entry(base, "xl/sharedStrings.xml", lambda data: data.replace(b"</sst>", b"<!--" + b" " * 1_500_000 + b"--></sst>")))
        doc_bomb = rewrite_entry(build_docx(["hi"]), "word/document.xml", lambda data: data.replace(b"</w:body>", b"<!--" + b" " * 1_500_000 + b"--></w:body>"))
        with self.assertRaises(ParserError):
            parse_docx("bomb.docx", doc_bomb)
        # Below the ratio floor the total inflated size cap still applies.
        small = rewrite_entry(base, "xl/worksheets/sheet1.xml", lambda data: data.replace(b"</sheetData>", b"<!--" + b"y" * 300_000 + b"--></sheetData>"))
        original = archive_guard.MAX_INFLATED_BYTES
        archive_guard.MAX_INFLATED_BYTES = 200_000
        try:
            with self.assertRaises(ParserError):
                parse_xlsx("large.xlsx", small)
            with self.assertRaises(ParserError):
                parse_docx("large.docx", rewrite_entry(build_docx(["hi"]), "word/document.xml", lambda data: data.replace(b"</w:body>", b"<!--" + b"y" * 300_000 + b"--></w:body>")))
        finally:
            archive_guard.MAX_INFLATED_BYTES = original
        self.assertEqual(parse_xlsx("ok.xlsx", small).tables[0].row_count, 1)
        # The byte budget is enforced on delivered bytes, not only on declared sizes.
        reader = archive_guard.BoundedReader(io.BytesIO(b"z" * 1000), budget=500)
        with self.assertRaises(ParserError):
            while reader.read(200):
                pass

    def test_json_parser_caps_distinct_keys_and_keeps_only_present_keys(self):
        result = parse_json("sparse.json", json.dumps([{"a": 1}, {"b": 2, "a": 3}]).encode())
        self.assertEqual(result.tables[0].headers, ("a", "b"))
        self.assertEqual([record.fields for record in result.tables[0].records], [{"a": 1}, {"b": 2, "a": 3}])
        wide = [{f"k{i}": i} for i in range(201)]
        with self.assertRaises(ParserError):
            parse_json("wide.json", json.dumps(wide).encode())
        self.assertEqual(len(parse_json("ok.json", json.dumps(wide[:200]).encode()).tables[0].headers), 200)

    def test_disabled_ocr_engine_reports_configuration(self):
        from app.ingestion.parsers.ocr import DisabledOcrEngine

        with self.assertRaises(OcrUnavailable):
            DisabledOcrEngine().recognize(b"x")


class WideSheetTests(unittest.TestCase):
    def test_giant_cell_references_and_wide_rows_are_bounded(self):
        import time

        from app.ingestion.parsers.excel_parser import MAX_SHEET_COLUMNS, parse_xlsx

        content = build_xlsx({"Sheet1": [["Name", "USN"], ["Ravi", "MBA001"]]})
        wide = "".join(f'<c r="{chr(65 + (i % 26))}{chr(65 + (i // 26 % 26))}{chr(65 + (i // 676 % 26))}3"><v>{i}</v></c>' for i in range(3000))

        def transform(data: bytes) -> bytes:
            text = data.decode("utf-8")
            text = text.replace("</row></sheetData>", '<c r="ZZZZZZ2" t="inlineStr"><is><t>far</t></is></c></row><row r="3">' + wide + "</row></sheetData>")
            return text.encode("utf-8")

        crafted = rewrite_entry(content, "xl/worksheets/sheet1.xml", transform)
        started = time.perf_counter()
        result = parse_xlsx("wide.xlsx", crafted)
        self.assertLess(time.perf_counter() - started, 5.0)
        table = result.tables[0]
        self.assertLessEqual(len(table.headers), MAX_SHEET_COLUMNS)
        self.assertLessEqual(max(len(record.fields) for record in table.records), MAX_SHEET_COLUMNS)
        self.assertNotIn("far", str([record.fields for record in table.records]), "a cell beyond the last supported column is ignored, never padded out to")


if __name__ == "__main__":
    unittest.main()
