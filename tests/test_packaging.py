"""Packaging inputs: generated versions, installer safety and release workflow rules."""

from pathlib import Path
import subprocess
import sys

from meenifx import release
from meenifx.version import VERSION

ROOT = Path(__file__).resolve().parents[1]
PACKAGING = ROOT / "packaging"


def test_the_committed_version_resources_match_the_version_source():
    assert (PACKAGING / "version_info.txt").read_text(encoding="utf-8") == \
        release.version_info_text()
    assert (PACKAGING / "version.iss").read_text(encoding="utf-8") == \
        release.version_iss_text()


def test_the_installer_is_per_user_and_uses_the_generated_version():
    text = (PACKAGING / "meenifx.iss").read_text(encoding="utf-8")
    assert '#include "version.iss"' in text
    assert "PrivilegesRequired=lowest" in text
    assert "OutputBaseFilename=meeniFX-Studios-{#MyAppVersion}-Windows-Setup" in text
    assert "SetupIconFile=..\\assets\\icon.ico" in text
    assert "DefaultDirName={localappdata}\\Programs\\meeniFX Studios" in text
    assert "UninstallDisplayIcon" in text
    # A silent in-app update must reopen the new build after the install.
    run_section = text.split("[Run]", 1)[1].split("[UninstallDelete]", 1)[0]
    assert 'Filename: "{app}\\{#MyAppExeName}"' in run_section
    assert "Flags: nowait" in run_section
    assert "skipifsilent" not in run_section.lower()
    # Upgrades must not remove user projects, presets or settings.
    assert "{userdocs}" not in text.lower()
    assert "UninstallDelete" in text


def test_both_windows_builds_ship_the_preview_libraries_and_sample():
    spec = (ROOT / "meeniFX Studios.spec").read_text(encoding="utf-8")
    assert "('presets', 'presets')" in spec
    assert "('looks', 'looks')" in spec
    assert "('figure_cafe.png', '.')" in spec
    assert "('examples', 'examples')" in spec
    assert (ROOT / "figure_cafe.png").is_file()
    assert (ROOT / "examples" / "Getting Started" / "Getting Started.mfxpf").is_file()


def test_the_build_script_can_explain_itself():
    result = subprocess.run([sys.executable, str(PACKAGING / "build_windows.py"), "--help"],
                            cwd=ROOT, capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr
    assert "--skip-installer" in result.stdout
    assert "portable" in result.stdout.lower()


def test_the_release_workflow_is_tag_driven_and_tests_before_building():
    text = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    assert 'tags:' in text and '"v*"' in text
    assert "workflow_dispatch" in text
    assert "permissions:\n  contents: write" in text
    assert "python tools/run_tests.py" in text
    runner = (ROOT / "tools" / "run_tests.py").read_text(encoding="utf-8")
    assert '"-m", "pytest"' in runner and "subprocess.run" in runner
    assert text.index("Run automated tests") < text.index("Build installer")
    assert "meeniFX-Studios-*-Windows-Setup.exe" in text
    assert "meeniFX-Studios-*-Windows-Portable.zip" in text
    assert "SHA256SUMS.txt" in text
    # Publishing is only for a version tag, and Alpha releases are prereleases.
    assert "startsWith(github.ref, 'refs/tags/v')" in text
    assert "--prerelease" in text
    assert "--verify-tag" in text


def test_the_bundled_release_notes_exist_for_the_current_version():
    notes = ROOT / "docs" / "releases" / f"{VERSION}.md"
    assert notes.is_file()
    text = notes.read_text(encoding="utf-8")
    assert "## Known issues" in text
    assert VERSION in text
