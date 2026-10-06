"""
Export graded results with an export profile to CSV, XLSX, PDF or SQL.

    from src.export import ExportProfile, export_results
    report = export_results(lambda: iter(results), "xlsx", "out.xlsx",
                            ExportProfile.from_dict(profile_json), template_infos)

results is a re-iterable source of stored result dicts (result.json content):
values are the corrected ones, never the original read. Everything streams,
so exports of millions of sheets run in constant memory (PDF excepted: it is
meant for subsets and refuses very large counts).

The API (POST /exports) and the CLI (python -m src.export) are thin wrappers.
"""

from src.export.profile import (
    FIELD_TYPES,
    META_COLUMNS,
    Column,
    ExportError,
    ExportProfile,
    LossyCastError,
)
from src.export.rows import RowBuilder, field_names
from src.export.writers import (
    pdf_sheets,
    pdf_table,
    sql_columns,
    write_csv,
    write_sqlalchemy,
    write_sqlite,
    write_xlsx,
)

FORMATS = ("csv", "xlsx", "pdf", "sqlite", "sql")
EXTENSIONS = {
    "csv": "csv",
    "xlsx": "xlsx",
    "pdf": "pdf",
    "sqlite": "sqlite",
    "sql": "txt",
}
MEDIA_TYPES = {
    "csv": "text/csv",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "pdf": "application/pdf",
    "sqlite": "application/vnd.sqlite3",
    "sql": "text/plain",
}

__all__ = [
    "Column",
    "EXTENSIONS",
    "ExportError",
    "ExportProfile",
    "FIELD_TYPES",
    "FORMATS",
    "LossyCastError",
    "MEDIA_TYPES",
    "META_COLUMNS",
    "export_results",
    "plan_columns",
]


def plan_columns(profile, template_infos, sample=()):
    custom_labels = {}
    for info in template_infos:
        custom_labels.update(info.get("custom_labels") or {})
    names = field_names(template_infos, sample)
    columns, warnings = profile.columns(names, custom_labels)
    return columns, warnings, custom_labels


def export_results(
    results_factory,
    fmt,
    out_path,
    profile,
    template_infos,
    image_provider=None,
    total=None,
    title="OMR results",
    sql_url=None,
    progress=None,
):
    """
    Write an export; returns a report {rows, warnings, cast_failures, ...}.
    results_factory() must return a fresh iterator of result dicts each call.
    Raises ExportError / LossyCastError (the caller removes partial files).
    """
    if fmt not in FORMATS:
        raise ExportError(f"Unknown format '{fmt}'; choose from {list(FORMATS)}")
    sample = []
    for result in results_factory():
        sample.append(result)
        if len(sample) >= 20:
            break
    columns, warnings, custom_labels = plan_columns(profile, template_infos, sample)
    if fmt in ("sqlite", "sql"):
        columns = sql_columns(columns)
    builder = RowBuilder(columns, profile, custom_labels)

    def rows():
        for index, result in enumerate(results_factory()):
            if progress and index % 500 == 0:
                progress(index)
            yield builder.build(result)

    if fmt == "csv":
        count = write_csv(out_path, columns, rows(), profile.csv)
    elif fmt == "xlsx":
        count = write_xlsx(out_path, columns, rows(), profile.xlsx)
    elif fmt == "sqlite":
        count = write_sqlite(
            out_path,
            profile.sql.get("table") or "omr_results",
            columns,
            rows(),
            int(profile.sql.get("batchSize") or 1000),
        )
    elif fmt == "sql":
        url = sql_url or profile.sql.get("url")
        if not url:
            raise ExportError("SQL export needs a database URL (sql.url)")
        table = profile.sql.get("table") or "omr_results"
        batch = int(profile.sql.get("batchSize") or 1000)
        if url.startswith("sqlite:///") and "+" not in url.split(":", 1)[0]:
            count = write_sqlite(
                url[len("sqlite:///") :], table, columns, rows(), batch
            )
        else:
            count = write_sqlalchemy(url, table, columns, rows(), batch)
        with open(out_path, "w") as handle:
            handle.write(f"Wrote {count} rows to table '{table}' at {_redact(url)}\n")
    else:
        count = _write_pdf(
            out_path,
            columns,
            results_factory,
            builder,
            profile,
            image_provider,
            total,
            title,
        )
    if builder.cast_failures:
        warnings.append(
            f"{builder.cast_failures} value(s) could not be converted and were left "
            "empty (and highlighted): " + "; ".join(builder.failure_examples)
        )
    return {
        "rows": count,
        "columns": [c.key for c in columns],
        "warnings": warnings,
        "cast_failures": builder.cast_failures,
    }


def _write_pdf(
    out_path, columns, results_factory, builder, profile, image_provider, total, title
):
    from src.export.writers import _reportlab

    _, _, pdf_canvas = _reportlab()
    options = profile.pdf
    mode = options.get("mode", "table")
    max_sheets = int(options.get("maxSheets") or 500)
    max_rows = int(options.get("maxRows") or 100000)
    if mode in ("sheets", "both") and total is not None and total > max_sheets:
        raise ExportError(
            f"{total} sheets selected: one PDF page per sheet is limited to "
            f"{max_sheets} (pdf.maxSheets). Narrow the filter or use the table mode."
        )
    if mode == "table" and total is not None and total > max_rows:
        raise ExportError(
            f"{total} rows selected: the PDF table is limited to {max_rows} rows "
            "(pdf.maxRows). Use CSV/XLSX for large exports."
        )
    c = pdf_canvas.Canvas(str(out_path))
    c.setTitle(title)
    count = 0
    if mode in ("table", "both"):

        def rows_factory():
            for result in results_factory():
                yield builder.build(result)

        count = pdf_table(c, columns, rows_factory, options, title)
        builder.rows = 0
    if mode in ("sheets", "both"):

        def items():
            for index, result in enumerate(results_factory()):
                if index >= max_sheets:
                    raise ExportError(
                        f"More than {max_sheets} sheets (pdf.maxSheets); narrow the filter"
                    )
                values, states = builder.build(result)
                yield result, values, states

        count = pdf_sheets(c, columns, items(), options, image_provider)
    c.save()
    return count


def _redact(url):
    if "@" in url and "://" in url:
        scheme, rest = url.split("://", 1)
        return f"{scheme}://***@{rest.split('@', 1)[1]}"
    return url
