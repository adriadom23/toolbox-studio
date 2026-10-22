"""Local Studio replacement for ToolBox's `app.shared_excel.toolbox_output`.

ToolBox's real module is an 817-line output-path/report-writing layer.
`manifest_lookup.py` only needs one function from it: a local directory to
cache manifest data in. This stub provides exactly that, nothing more.

Not vendored (see design.md §1.2/§1.6): the real module's report-writing
and pandas-heavy concerns have no bearing on whether the vendored
integration clients import and run.
"""

from pathlib import Path


def get_base_toolbox_dir() -> Path:
    """Return the local directory Studio uses in place of ToolBox's
    station-workbook-rooted output directory, creating it if missing.

    Resolves to `ToolboxStudio/backend/data/` (three levels up from this
    file: vendor/support/toolbox_output_stub.py -> vendor/support ->
    vendor -> backend, then into data/).
    """

    base_dir = Path(__file__).resolve().parents[2] / "data"
    base_dir.mkdir(parents=True, exist_ok=True)
    return base_dir
