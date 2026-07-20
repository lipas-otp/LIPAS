"""Release metadata stays synchronized across package and documentation."""
from __future__ import annotations

from pathlib import Path
import subprocess
import sys
import tomllib

from lipas import __version__


RELEASE = "0.20.0"


def test_version_has_one_packaging_source_of_truth():
    metadata = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    assert __version__ == RELEASE
    assert "version" in metadata["project"]["dynamic"]
    assert "version" not in metadata["project"]
    assert metadata["tool"]["hatch"]["version"]["path"] == "lipas/_version.py"


def test_release_headings_and_product_banners_are_current():
    assert (
        f"## [{RELEASE}] — 2026-07-20 · Local Task Product Alpha"
        in Path("CHANGELOG.md").read_text()
    )
    assert f"**{RELEASE} local task product alpha.**" in Path("README.md").read_text()
    assert f"**{RELEASE} 本地任务产品 alpha。**" in Path(
        "README.zh-CN.md",
    ).read_text()


def test_core_only_environment_can_import_the_cli():
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; sys.modules['httpx'] = None; "
                "import lipas.cli; print(lipas.cli.__version__)"
            ),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == RELEASE
