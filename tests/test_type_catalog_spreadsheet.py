from copy import deepcopy

from openpyxl import load_workbook
import pytest

from yuku.scraping_type_catalog import get_scraping_catalog, observation_id
from yuku.scraping_type_review import export_excel, import_excel
from yuku.type_catalog_spreadsheet import COLUMNS, unified_rows


def payload():
    data = deepcopy(get_scraping_catalog().payload)
    data["catalog_version"] = "1.0.0"
    data["entries"] = [{"id": observation_id("Generación de Contenido Impreso", "Manual"),
        "parent": "Generación de Contenido Impreso", "type": "Manual", "status": "inherited",
        "entity": "works", "type_impactu": "Publicaciones editoriales no especializadas",
        "justification": "Exact match", "evidence": [], "examples": [], "frequency": 1, "gruplac": 1, "cvlac": 0}]
    return data


def scraping_row(sheet):
    return next(row for row in sheet.iter_rows(min_row=2) if row[1].value == "scraping")


def test_default_export_has_one_sheet_and_seven_columns(tmp_path):
    data = payload(); path = tmp_path / "catalog.xlsx"
    export_excel(data, path)
    book = load_workbook(path)
    assert book.sheetnames == ["Catalogo"]
    sheet = book.active
    assert tuple(c.value for c in sheet[1]) == COLUMNS
    assert sheet.max_column == 7
    assert {row[0].value for row in sheet.iter_rows(min_row=2)} >= {"scienti", "minciencias", "coar", "redcol", "eu-repo"}
    row = scraping_row(sheet)
    assert row[4].value is None
    assert row[6].value == "Publicaciones editoriales no especializadas"
    book.close()
    reviewed = import_excel(path, data, "1.0.1")
    assert reviewed["entries"] == data["entries"]


def test_same_native_classification_merges_origins_only_with_same_source_and_context():
    data = payload(); entry = data["entries"][0]
    entry.update(parent="Apropiación social del conocimiento y divulgación pública de la ciencia", type="Artículos")
    rows, _, _ = unified_rows(data)
    row = next(r for r in rows if r[0] == "minciencias" and r[2] == entry["parent"] and r[3] == "Artículos")
    assert row[1] == "socrata; scraping"
    assert row[4] == row[6] == "Publicaciones editoriales no especializadas"
    # A translation shared by many unrelated types is not an origin match.
    dump = next(r for r in rows if r[0] == "scienti" and r[3] == "Manual")
    assert dump[1] == "dumps" and not dump[6]


def test_suggested_correction_imports_without_changing_original_vocabulary(tmp_path):
    data = payload(); path = tmp_path / "catalog.xlsx"
    export_excel(data, path)
    book = load_workbook(path)
    row = scraping_row(book.active)
    row[6].value = "Libro"
    book.save(path); book.close()
    reviewed = import_excel(path, data, "1.0.1")
    assert reviewed["entries"][0]["type_impactu"] == "Libro"
    assert reviewed["entries"][0]["status"] == "approved"
    assert data["entries"][0]["type_impactu"] == "Publicaciones editoriales no especializadas"


def test_original_suggestion_is_saved_as_a_proposal_only(tmp_path):
    data = payload(); path = tmp_path / "catalog.xlsx"
    export_excel(data, path)
    book = load_workbook(path)
    row = next(r for r in book.active.iter_rows(min_row=2) if r[0].value == "scholar" and r[3].value == "article")
    row[6].value = "Libro"
    book.save(path); book.close()
    reviewed = import_excel(path, data, "1.0.1")
    assert reviewed["entries"] == data["entries"]
    assert reviewed["shared_catalog_suggestions"][0]["Tipo ImpactU"] == "Artículo de revista"
    assert reviewed["shared_catalog_suggestions"][0]["Tipo ImpactU Sugerido"] == "Libro"


def test_invalid_or_deleted_rows_are_rejected(tmp_path):
    data = payload(); path = tmp_path / "catalog.xlsx"
    export_excel(data, path)
    book = load_workbook(path); scraping_row(book.active)[6].value = "Tipo inventado"
    book.save(path); book.close()
    with pytest.raises(ValueError, match="canonical"):
        import_excel(path, data, "1.0.1")
    export_excel(data, path)
    book = load_workbook(path); book.active.delete_rows(2); book.save(path); book.close()
    with pytest.raises(ValueError, match="missing"):
        import_excel(path, data, "1.0.1")


def test_new_scraping_row_is_imported_and_formulas_stay_literal(tmp_path):
    data = payload(); path = tmp_path / "catalog.xlsx"
    export_excel(data, path)
    book = load_workbook(path)
    book.active.append(["minciencias", "scraping", "Sección verificada", "=Tipo verificado", "", "works", "Libro"])
    book.save(path); book.close()
    reviewed = import_excel(path, data, "1.0.1")
    assert reviewed["entries"][-1]["type"] == "=Tipo verificado"
    export_excel(reviewed, path)
    book = load_workbook(path)
    cell = next(r[3] for r in book.active.iter_rows(min_row=2) if r[3].value == "=Tipo verificado")
    assert cell.data_type == "s"
    book.close()
