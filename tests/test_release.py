"""Release metadata stays synchronized across package and documentation."""
from __future__ import annotations

from pathlib import Path
import subprocess
import sys
import tomllib

from lipas import __version__


RELEASE = "0.32.0"


def test_version_has_one_packaging_source_of_truth():
    metadata = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    assert __version__ == RELEASE
    assert "version" in metadata["project"]["dynamic"]
    assert "version" not in metadata["project"]
    assert metadata["tool"]["hatch"]["version"]["path"] == "lipas/_version.py"
    extras = metadata["project"]["optional-dependencies"]
    assert extras["compatible"] == ["httpx>=0.27"]
    assert extras["openai"] == extras["compatible"]


def test_release_headings_and_product_banners_are_current():
    assert (
        f"## [{RELEASE}] — 2026-08-11 · Compatible Model Endpoints Alpha"
        in Path("CHANGELOG.md").read_text()
    )
    assert f"**{RELEASE} compatible model endpoints alpha.**" in Path("README.md").read_text()
    assert f"**{RELEASE} Compatible 模型端点 Alpha。**" in Path(
        "README.zh-CN.md",
    ).read_text()
    package_info = Path("PKG-INFO").read_text(encoding="utf-8")
    assert f"Version: {RELEASE}" in package_info
    embedded_readme = "# LIPAS\n" + package_info.split("\n# LIPAS\n", 1)[1]
    assert embedded_readme == Path("README.md").read_text(encoding="utf-8")


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
