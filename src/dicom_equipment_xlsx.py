"""Small, offline OOXML writer for inserting two DICOM equipment columns.

The source workbook is never saved through a spreadsheet library.  All ZIP
members except the selected worksheet retain their original uncompressed
bytes, except a built-in filter defined name when its range needs updating.
The selected worksheet is streamed a row at a time, so a template with
formatted cells through XFD does not create a full Python cell grid.

Excel cannot shift XFC/XFD two columns to the right.  Only *empty, style-only*
cells in those two original columns may be discarded; real values or other
cell metadata there cause an error.  Existing column formatting is shifted
and clipped at XFD.  This exception is reported by ``inspect_workbook``.

The intentionally narrow writer rejects formulas anywhere in the workbook,
general defined names, and unsupported structures on the selected worksheet rather
than silently invalidating references.  Ordinary cell styles, row heights,
column widths, frozen panes, selections, merges not spanning the insertion,
and basic filters are supported.  No openpyxl or proprietary runtime is needed.
"""

from __future__ import annotations

import copy
import os
from pathlib import Path
import posixpath
import re
import shutil
import tempfile
from typing import Dict, Iterable, Mapping, Optional, Tuple, Union
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape, quoteattr
from zipfile import ZIP_DEFLATED, ZipFile


MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
OFFICE_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PACKAGE_REL = "http://schemas.openxmlformats.org/package/2006/relationships"
XML_NS = "http://www.w3.org/XML/1998/namespace"
NS = "{" + MAIN + "}"
MAX_COLUMN = 16384
MAX_ROW = 1048576
EQUIPMENT_HEADERS = ("Manufacturer", "ManufacturerModelName")
_CELL = re.compile(r"(\$?)([A-Z]{1,3})(\$?)([1-9][0-9]*)\Z")
_ALLOWED_CHILDREN = {
    "sheetPr", "dimension", "sheetViews", "sheetFormatPr", "cols",
    "sheetData", "sheetProtection", "autoFilter", "mergeCells",
    "printOptions", "pageMargins", "pageSetup", "headerFooter", "rowBreaks",
    "ignoredErrors",
}
_REFERENCE_STRUCTURES = {
    "conditionalFormatting", "dataValidations", "hyperlinks", "drawing",
    "legacyDrawing", "legacyDrawingHF", "picture", "tableParts", "controls",
    "oleObjects", "protectedRanges", "extLst",
}
PathLike = Union[str, os.PathLike]


class WorkbookStructureError(ValueError):
    """The workbook cannot safely be edited by this deliberately narrow writer."""


def _column_number(letters: str) -> int:
    result = 0
    for char in letters:
        result = result * 26 + ord(char) - 64
    return result


def _column_letters(number: int) -> str:
    if not 1 <= number <= MAX_COLUMN:
        raise WorkbookStructureError("Column is outside Excel's A:XFD range")
    chars = []
    while number:
        number, remainder = divmod(number - 1, 26)
        chars.append(chr(65 + remainder))
    return "".join(reversed(chars))


def _cell_ref(reference: str) -> Tuple[int, int]:
    match = _CELL.fullmatch(reference)
    if not match:
        raise WorkbookStructureError("Unsupported cell reference: " + repr(reference))
    column = _column_number(match.group(2))
    row = int(match.group(4))
    if column > MAX_COLUMN or row > MAX_ROW:
        raise WorkbookStructureError("Cell reference exceeds Excel limits")
    return column, row


def _shift_ref(reference: str, after: int, clamp: bool = False) -> str:
    parts = reference.split(":")
    if len(parts) not in (1, 2):
        raise WorkbookStructureError("Unsupported range: " + repr(reference))
    shifted = []
    for part in parts:
        col, row = _cell_ref(part)
        match = _CELL.fullmatch(part)
        new_col = col + (2 if col > after else 0)
        if new_col > MAX_COLUMN:
            if not clamp:
                raise WorkbookStructureError("A referenced range would exceed XFD")
            new_col = MAX_COLUMN
        shifted.append(match.group(1) + _column_letters(new_col) + match.group(3) + str(row))
    return ":".join(shifted)


def _shift_sqref(value: str, after: int) -> str:
    return " ".join(_shift_ref(part, after) for part in value.split())


def _empty_style_cell(cell: ET.Element) -> bool:
    if not set(cell.attrib).issubset({"r", "s", "t"}) or cell.get("t") not in (None, "n"):
        return False
    if (cell.text or "").strip():
        return False
    children = list(cell)
    return all(child.tag == NS + "v" and not (child.text or "").strip() and not list(child)
               for child in children)


def _has_value(cell: ET.Element) -> bool:
    # Empty numeric <v/> elements are emitted by some editors for style-only cells.
    if (cell.text or "").strip():
        return True
    for child in cell:
        if child.tag != NS + "v" or (child.text or "").strip() or list(child):
            return True
    return False


def _cell_text(cell: ET.Element, shared_strings: Iterable[str]) -> str:
    if cell.get("t") == "inlineStr":
        return "".join(item.text or "" for item in cell.iter(NS + "t"))
    value = cell.find(NS + "v")
    text = "" if value is None else value.text or ""
    if cell.get("t") == "s":
        try:
            index = int(text)
            if index < 0:
                raise ValueError()
            return shared_strings[index]  # type: ignore[index]
        except (ValueError, IndexError):
            raise WorkbookStructureError("Invalid shared-string index") from None
    return text


def _xml_from_zip(archive: ZipFile, name: str) -> ET.Element:
    if archive.getinfo(name).file_size > 128 * 1024 * 1024:
        raise WorkbookStructureError("XML metadata part is too large: " + name)
    return ET.fromstring(archive.read(name))


def _book_parts(archive: ZipFile, sheet_name: Optional[str]) -> dict:
    names = archive.namelist()
    if len(names) != len(set(names)):
        raise WorkbookStructureError("Duplicate ZIP member names are unsupported")
    if any(name.startswith("_xmlsignatures/") for name in names):
        raise WorkbookStructureError("Signed workbooks are unsupported")
    if any("vbaproject" in name.lower() for name in names):
        raise WorkbookStructureError("Macro-enabled workbooks are unsupported")
    if any(name.startswith(("xl/charts/", "xl/tables/", "xl/pivotTables/", "xl/externalLinks/")) for name in names):
        raise WorkbookStructureError("Charts, tables, pivots and external references require reference-aware editing")
    workbook = _xml_from_zip(archive, "xl/workbook.xml")
    if workbook.tag != NS + "workbook":
        raise WorkbookStructureError("Only transitional OOXML .xlsx workbooks are supported")
    defined_names = list(workbook.iter(NS + "definedName"))
    if any(node.get("name") != "_xlnm._FilterDatabase" for node in defined_names):
        raise WorkbookStructureError("General defined names need reference-aware editing and are unsupported")
    relationships = _xml_from_zip(archive, "xl/_rels/workbook.xml.rels")
    targets = {}
    for rel in relationships:
        if rel.get("TargetMode") == "External":
            continue
        target = rel.get("Target", "")
        resolved = posixpath.normpath(target.lstrip("/") if target.startswith("/") else posixpath.join("xl", target))
        if not resolved.startswith("xl/"):
            raise WorkbookStructureError("Unsafe workbook relationship target")
        targets[rel.get("Id")] = resolved
    sheets = workbook.find(NS + "sheets")
    if sheets is None or not list(sheets):
        raise WorkbookStructureError("Workbook has no worksheets")
    available = []
    for sheet in sheets:
        path = targets.get(sheet.get("{" + OFFICE_REL + "}id"))
        if path is None or path not in names:
            raise WorkbookStructureError("Missing worksheet relationship or ZIP member")
        available.append((sheet.get("name", ""), path))
    if sheet_name is None:
        if len(available) != 1:
            raise WorkbookStructureError("Specify a worksheet name for a multi-sheet workbook")
        chosen = available[0]
    else:
        matches = [item for item in available if item[0] == sheet_name]
        if len(matches) != 1:
            raise WorkbookStructureError("Worksheet was not found uniquely: " + repr(sheet_name))
        chosen = matches[0]
    shared = []
    if "xl/sharedStrings.xml" in names:
        shared_root = _xml_from_zip(archive, "xl/sharedStrings.xml")
        shared = ["".join(t.text or "" for t in entry.iter(NS + "t")) for entry in shared_root]
    for node in defined_names:
        try:
            local_id = int(node.get("localSheetId", ""))
            if not 0 <= local_id < len(available):
                raise ValueError()
        except ValueError:
            raise WorkbookStructureError("Filter defined name needs a valid localSheetId") from None
        if list(node):
            raise WorkbookStructureError("Nested content in a filter defined name is unsupported")
        _filter_name_range(node.text or "", available[local_id][0])
    return {"sheet_name": chosen[0], "sheet_path": chosen[1], "sheet_index": available.index(chosen), "sheets": available, "shared_strings": shared, "filter_name_count": len(defined_names)}


def _filter_name_range(text: str, expected_sheet: str) -> Tuple[str, str]:
    # Built-in filter names contain one sheet-qualified absolute A1 range.
    match = re.fullmatch(r"('(?:[^']|'')*'|[^'!]+)!([^!]+)", text)
    if not match:
        raise WorkbookStructureError("Unsupported filter defined-name expression")
    qualifier, reference = match.groups()
    sheet = qualifier[1:-1].replace("''", "'") if qualifier.startswith("'") else qualifier
    if sheet != expected_sheet:
        raise WorkbookStructureError("Filter defined name points to an unexpected worksheet")
    if len(reference.split(":")) != 2:
        raise WorkbookStructureError("Filter defined name must contain one rectangular range")
    for part in reference.split(":"):
        _cell_ref(part)
    return qualifier, reference


def _updated_filter_names(archive: ZipFile, parts: dict, after: int) -> Optional[bytes]:
    if not parts["filter_name_count"]:
        return None
    original = archive.read("xl/workbook.xml")
    expression = re.compile(rb"(<definedName\b[^>]*>)(.*?)(</definedName\s*>)", re.DOTALL)
    matches = list(expression.finditer(original))
    if len(matches) != parts["filter_name_count"]:
        raise WorkbookStructureError("Unsupported serialization of built-in filter defined names")
    def replace(match):
        try:
            node = ET.fromstring(match.group(0))
        except ET.ParseError:
            raise WorkbookStructureError("Unsupported namespaced filter defined name") from None
        if int(node.get("localSheetId")) != parts["sheet_index"]:
            return match.group(0)
        qualifier, reference = _filter_name_range(node.text or "", parts["sheet_name"])
        replacement = qualifier + "!" + _shift_ref(reference, after)
        return match.group(1) + escape(replacement).encode("utf-8") + match.group(3)
    updated = expression.sub(replace, original)
    return updated if updated != original else None


def _check_other_sheets(archive: ZipFile, parts: dict) -> None:
    for _, path in parts["sheets"]:
        if path == parts["sheet_path"]:
            continue
        with archive.open(path) as stream:
            stack = []
            for event, elem in ET.iterparse(stream, events=("start", "end")):
                if event == "start":
                    stack.append(elem)
                    local = elem.tag.removeprefix(NS)
                    if local in {"f", "formula", "formula1", "formula2"}:
                        raise WorkbookStructureError("Formula cells anywhere in the workbook are unsupported")
                    if local in _REFERENCE_STRUCTURES:
                        raise WorkbookStructureError("Unsupported reference-bearing structure on another worksheet: " + local)
                else:
                    if len(stack) > 1:
                        stack[-2].remove(elem)
                    elem.clear()
                    stack.pop()


def _transform_metadata(elem: ET.Element, after: int) -> None:
    """Modify only supported worksheet references, rejecting unknown variants."""
    local = elem.tag.removeprefix(NS)
    if local == "dimension":
        shifted = _shift_ref(elem.get("ref", "A1"), after, clamp=True)
        edges = shifted.split(":")
        end_col, end_row = _cell_ref(edges[-1])
        if end_col < after + 2:
            shifted = edges[0] + ":" + _column_letters(after + 2) + str(end_row)
        elem.set("ref", shifted)
    elif local == "sheetViews":
        for node in elem.iter():
            for attribute in ("topLeftCell", "activeCell"):
                if attribute in node.attrib:
                    node.set(attribute, _shift_ref(node.get(attribute), after))
            if "sqref" in node.attrib:
                node.set("sqref", _shift_sqref(node.get("sqref"), after))
            if node.tag == NS + "pane" and "xSplit" in node.attrib:
                state = node.get("state", "split")
                if state not in ("frozen", "frozenSplit"):
                    raise WorkbookStructureError("Non-frozen horizontal split panes are unsupported")
                split = float(node.get("xSplit"))
                if not split.is_integer():
                    raise WorkbookStructureError("Invalid frozen pane xSplit")
                if split >= after:
                    node.set("xSplit", str(int(split) + 2))
    elif local == "cols":
        transformed = []
        for col in elem:
            if col.tag != NS + "col":
                raise WorkbookStructureError("Unsupported column metadata")
            start, end = int(col.get("min")), int(col.get("max"))
            if not 1 <= start <= end <= MAX_COLUMN:
                raise WorkbookStructureError("Invalid column-format range")
            if start <= after:
                left = copy.deepcopy(col)
                left.set("max", str(min(end, after)))
                transformed.append(left)
            if end > after and max(start, after + 1) + 2 <= MAX_COLUMN:
                right = copy.deepcopy(col)
                right.set("min", str(max(start, after + 1) + 2))
                right.set("max", str(min(end + 2, MAX_COLUMN)))
                transformed.append(right)
        for offset, width in ((1, "24"), (2, "32")):
            transformed.append(ET.Element(NS + "col", {"min": str(after + offset), "max": str(after + offset), "width": width, "customWidth": "1"}))
        elem[:] = sorted(transformed, key=lambda item: int(item.get("min")))
    elif local == "mergeCells":
        for merged in elem:
            ref = merged.get("ref", "")
            edges = ref.split(":")
            if len(edges) == 2 and _cell_ref(edges[0])[0] <= after < _cell_ref(edges[1])[0]:
                raise WorkbookStructureError("A merged range spans the equipment insertion point")
            merged.set("ref", _shift_ref(ref, after))
    elif local == "autoFilter":
        ref = elem.get("ref", "")
        edges = ref.split(":")
        first = _cell_ref(edges[0])[0]
        last = _cell_ref(edges[-1])[0]
        if first <= after < last:
            for child in elem:
                if child.tag == NS + "filterColumn":
                    col_id = int(child.get("colId"))
                    if first + col_id > after:
                        child.set("colId", str(col_id + 2))
        elem.set("ref", _shift_ref(ref, after))
        for node in elem.iter():
            if node is not elem and "ref" in node.attrib:
                node.set("ref", _shift_ref(node.get("ref"), after))
            if node.tag == NS + "sortCondition" and node.get("sortBy", "value") != "value":
                raise WorkbookStructureError("Non-value filter sort conditions are unsupported")
    elif local == "ignoredErrors":
        for node in elem:
            if "sqref" in node.attrib:
                node.set("sqref", _shift_sqref(node.get("sqref"), after))
    elif local == "rowBreaks":
        for node in elem:
            for attribute in ("min", "max"):
                if attribute in node.attrib:
                    value = int(node.get(attribute))
                    if not 0 <= value < MAX_COLUMN:
                        raise WorkbookStructureError("Invalid row-break column bound")
                    if value >= after:
                        node.set(attribute, str(min(value + 2, MAX_COLUMN - 1)))


def inspect_workbook(path: PathLike, sheet_name: Optional[str] = None) -> dict:
    """Inspect and preflight a source .xlsx without writing it.

    Returns ``sheet_name``, ``sheet_path``, ``header_row`` (always 1),
    ``headers`` (label -> first 1-based column), ``header_columns`` (label ->
    all columns), ``duplicate_headers``, ``modality_column``,
    ``max_data_row/column`` (stored values, not blank formatting),
    ``max_cell_row/column``, ``data_rows`` (rows containing stored values),
    and ``dropped_blank_cells`` (blank style cells at original XFC/XFD).
    Row/header values and patient identifiers are not included in this report.
    """
    with ZipFile(path) as archive:
        parts = _book_parts(archive, sheet_name)
        _check_other_sheets(archive, parts)
        headers: Dict[str, list] = {}
        metadata = []
        max_data_row = max_data_col = max_cell_row = max_cell_col = dropped = 0
        data_rows = []
        prior_row = 0
        with archive.open(parts["sheet_path"]) as stream:
            stack = []
            for event, elem in ET.iterparse(stream, events=("start", "end")):
                if event == "start":
                    stack.append(elem)
                    if not elem.tag.startswith(NS):
                        raise WorkbookStructureError("Foreign worksheet extension elements are unsupported")
                    if len(stack) == 1 and elem.tag != NS + "worksheet":
                        raise WorkbookStructureError("Selected part is not a worksheet")
                    if len(stack) == 2 and elem.tag.removeprefix(NS) not in _ALLOWED_CHILDREN:
                        raise WorkbookStructureError("Unsupported worksheet structure: " + elem.tag.removeprefix(NS))
                    if elem.tag.removeprefix(NS) in {"f", "formula", "formula1", "formula2"}:
                        raise WorkbookStructureError("Formula cells anywhere in the workbook are unsupported")
                    continue
                if elem.tag == NS + "row":
                    row_number = int(elem.get("r", "0"))
                    if not prior_row < row_number <= MAX_ROW:
                        raise WorkbookStructureError("Worksheet rows must have unique increasing row numbers")
                    prior_row = row_number
                    previous_col = 0
                    row_has_data = False
                    for cell in elem:
                        if cell.tag != NS + "c":
                            raise WorkbookStructureError("Unsupported row child")
                        col, cell_row = _cell_ref(cell.get("r", ""))
                        if cell_row != row_number or col <= previous_col:
                            raise WorkbookStructureError("Cells must be ordered and agree with their row number")
                        previous_col = col
                        max_cell_col = max(max_cell_col, col)
                        max_cell_row = max(max_cell_row, row_number)
                        if col > MAX_COLUMN - 2:
                            if not _empty_style_cell(cell):
                                raise WorkbookStructureError("Nonempty or annotated XFC/XFD cells cannot be shifted")
                            dropped += 1
                        if _has_value(cell):
                            row_has_data = True
                            max_data_row = max(max_data_row, row_number)
                            max_data_col = max(max_data_col, col)
                            if row_number == 1:
                                label = _cell_text(cell, parts["shared_strings"]).strip()
                                if label:
                                    headers.setdefault(label, []).append(col)
                    if row_has_data and row_number > 1:
                        data_rows.append(row_number)
                    stack[-2].remove(elem)
                    elem.clear()
                elif len(stack) == 2:
                    if elem.tag != NS + "sheetData":
                        metadata.append(copy.deepcopy(elem))
                    stack[-2].remove(elem)
                    elem.clear()
                stack.pop()
        modality = [col for label, cols in headers.items() if label.casefold() == "modality" for col in cols]
        if len(modality) != 1:
            raise WorkbookStructureError("Header row 1 must contain exactly one modality column")
        if any(label.casefold() in {h.casefold() for h in EQUIPMENT_HEADERS} for label in headers):
            raise WorkbookStructureError("Equipment columns already exist; refusing duplicate insertion")
        after = modality[0]
        if after > MAX_COLUMN - 2:
            raise WorkbookStructureError("No room for equipment columns after modality")
        for elem in metadata:
            _transform_metadata(elem, after)
        filter_update = _updated_filter_names(archive, parts, after)
        return {
            "sheet_name": parts["sheet_name"], "sheet_path": parts["sheet_path"],
            "header_row": 1, "headers": {label: cols[0] for label, cols in headers.items()},
            "header_columns": headers,
            "duplicate_headers": {label: cols for label, cols in headers.items() if len(cols) > 1},
            "modality_column": after, "max_data_row": max_data_row,
            "max_data_column": max_data_col, "max_cell_row": max_cell_row,
            "max_cell_column": max_cell_col, "data_rows": data_rows,
            "dropped_blank_cells": dropped,
            "updates_filter_defined_name": filter_update is not None,
        }


def _inline_cell(row: int, column: int, value: str, style: Optional[str]) -> ET.Element:
    attrs = {"r": _column_letters(column) + str(row)}
    if style is not None:
        attrs["s"] = style
    cell = ET.Element(NS + "c", attrs)
    if value == "":
        return cell
    cell.set("t", "inlineStr")
    string = ET.SubElement(cell, NS + "is")
    text = ET.SubElement(string, NS + "t")
    if value != value.strip():
        text.set("{" + XML_NS + "}space", "preserve")
    text.text = value
    return cell


def _rewrite_row(row: ET.Element, after: int, values: Mapping[int, Tuple[str, str]]) -> None:
    number = int(row.get("r"))
    style = None
    for cell in row:
        if _cell_ref(cell.get("r"))[0] == after:
            style = cell.get("s")
            break
    cells = []
    for cell in row:
        col, _ = _cell_ref(cell.get("r"))
        if col > MAX_COLUMN - 2:
            continue  # Preflight already proved these are empty style-only cells.
        if col > after:
            cell.set("r", _column_letters(col + 2) + str(number))
        cells.append(cell)
    additions = EQUIPMENT_HEADERS if number == 1 else values.get(number)
    if additions is not None:
        cells.extend(_inline_cell(number, after + i + 1, value, style) for i, value in enumerate(additions))
    cells.sort(key=lambda cell: _cell_ref(cell.get("r"))[0])
    row[:] = cells
    if "spans" in row.attrib:
        # This is an optional optimization hint.  Derive it from actual cells.
        if cells:
            row.set("spans", str(_cell_ref(cells[0].get("r"))[0]) + ":" + str(_cell_ref(cells[-1].get("r"))[0]))
        else:
            del row.attrib["spans"]


def _opening_root(root: ET.Element, namespaces: list) -> bytes:
    # The output uses unprefixed worksheet/sheetData tags.  Inputs are also
    # allowed to use x:worksheet with MAIN bound to x rather than the default.
    namespaces = [(prefix, uri) for prefix, uri in namespaces if prefix]
    namespaces.insert(0, ("", MAIN))
    declarations = []
    prefix_by_uri = {}
    for prefix, uri in namespaces:
        prefix_by_uri[uri] = prefix
        declarations.append("xmlns" + (":" + prefix if prefix else "") + "=" + quoteattr(uri))
    attrs = []
    for name, value in root.attrib.items():
        if name.startswith("{"):
            uri, local = name[1:].split("}", 1)
            prefix = "xml" if uri == XML_NS else prefix_by_uri.get(uri)
            if not prefix:
                raise WorkbookStructureError("Unsupported namespaced worksheet root attribute")
            name = prefix + ":" + local
        attrs.append(name + "=" + quoteattr(value))
    return ("<?xml version=\"1.0\" encoding=\"UTF-8\" standalone=\"yes\"?>\n<worksheet " + " ".join(declarations + attrs) + ">").encode("utf-8")


def _write_sheet(source_stream, output_stream, after: int, values: Mapping[int, Tuple[str, str]]) -> None:
    ET.register_namespace("", MAIN)
    ET.register_namespace("r", OFFICE_REL)
    stack = []
    namespaces = []
    had_cols = False
    for event, elem in ET.iterparse(source_stream, events=("start", "end", "start-ns")):
        if event == "start-ns":
            if not stack:
                namespaces.append(elem)
            continue
        if event == "start":
            stack.append(elem)
            if len(stack) == 1:
                output_stream.write(_opening_root(elem, namespaces))
            elif len(stack) == 2 and elem.tag == NS + "sheetData":
                if not had_cols:
                    columns = ET.Element(NS + "cols")
                    _transform_metadata(columns, after)
                    output_stream.write(ET.tostring(columns, encoding="utf-8"))
                if elem.attrib:
                    raise WorkbookStructureError("Attributes on sheetData are unsupported")
                output_stream.write(b"<sheetData>")
            continue
        if elem.tag == NS + "row":
            _rewrite_row(elem, after, values)
            output_stream.write(ET.tostring(elem, encoding="utf-8"))
            stack[-2].remove(elem)
            elem.clear()
        elif len(stack) == 2:
            if elem.tag == NS + "sheetData":
                output_stream.write(b"</sheetData>")
            else:
                if elem.tag == NS + "cols":
                    had_cols = True
                _transform_metadata(elem, after)
                output_stream.write(ET.tostring(elem, encoding="utf-8"))
            stack[-2].remove(elem)
            elem.clear()
        elif len(stack) == 1:
            output_stream.write(b"</worksheet>")
        stack.pop()


def _source_identity(path: Path) -> tuple:
    stat = path.stat()
    return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns


def write_equipment_workbook(
    source: PathLike,
    output: PathLike,
    sheet_name: Optional[str],
    values_by_row: Mapping[int, Tuple[str, str]],
) -> None:
    """Insert the two columns and publish a new .xlsx, never overwriting a file.

    ``values_by_row`` uses original Excel row numbers (2 onward).  An omitted
    row remains empty in both inserted columns.  Every value is written as an
    explicit OOXML string, including strings beginning with '='.  The output
    parent directory must already exist.  Publication uses a same-directory
    hard link after the temporary ZIP is complete and fsynced; a filesystem
    without hard-link support fails clearly instead of weakening no-overwrite
    semantics.  Source changes detected during writing abort publication.
    """
    source_path, output_path = Path(source).resolve(), Path(output).absolute()
    if source_path == output_path.resolve():
        raise ValueError("Source and output must be different paths")
    if output_path.exists() or output_path.is_symlink():
        raise FileExistsError("Output already exists: " + str(output_path))
    if not output_path.parent.is_dir():
        raise FileNotFoundError("Output parent directory does not exist: " + str(output_path.parent))
    initial_identity = _source_identity(source_path)
    info = inspect_workbook(source_path, sheet_name)
    data_rows = set(info["data_rows"])
    normalized = {}
    for row, pair in values_by_row.items():
        if isinstance(row, bool) or not isinstance(row, int) or row not in data_rows:
            raise ValueError("Equipment values must refer to an existing data row (not the header)")
        if not isinstance(pair, (tuple, list)) or len(pair) != 2 or not all(isinstance(item, str) for item in pair):
            raise TypeError("Each equipment value must be a pair of strings")
        for value in pair:
            if len(value) > 32767:
                raise ValueError("Equipment value exceeds Excel's cell text limit")
            if any(not (char in "\t\n\r" or 0x20 <= ord(char) <= 0xD7FF or 0xE000 <= ord(char) <= 0xFFFD or 0x10000 <= ord(char) <= 0x10FFFF) for char in value):
                raise ValueError("Equipment value contains XML-invalid characters")
        normalized[row] = tuple(pair)
    fd, temporary = tempfile.mkstemp(prefix="." + output_path.name + ".", suffix=".tmp", dir=str(output_path.parent))
    os.close(fd)
    try:
        with ZipFile(source_path) as original, ZipFile(temporary, "w", compression=ZIP_DEFLATED, allowZip64=True) as result:
            result.comment = original.comment
            parts = _book_parts(original, info["sheet_name"])
            updated_workbook = _updated_filter_names(original, parts, info["modality_column"])
            for entry in original.infolist():
                # Copy metadata, including timestamps, member attributes and compression.
                copied = copy.copy(entry)
                with original.open(entry) as src, result.open(copied, "w", force_zip64=True) as dst:
                    if entry.filename == info["sheet_path"]:
                        _write_sheet(src, dst, info["modality_column"], normalized)
                    elif entry.filename == "xl/workbook.xml" and updated_workbook is not None:
                        dst.write(updated_workbook)
                    else:
                        shutil.copyfileobj(src, dst, length=1024 * 1024)
        if _source_identity(source_path) != initial_identity:
            raise RuntimeError("Source workbook changed during processing; output was not published")
        with open(temporary, "r+b") as stream:
            os.fsync(stream.fileno())
        # Atomic no-replace publication: unlike os.replace/rename this never overwrites.
        os.link(temporary, output_path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
