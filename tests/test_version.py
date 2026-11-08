"""One version everywhere: comparison, generated resources and diagnostics."""

import re

import pytest

from meenifx import config, release
from meenifx import crashlog
from meenifx.updates import download
from meenifx.version import (STABLE, VERSION, Version, current_version, diagnostics,
                             display_version, numeric_version, parse_version)


def test_the_single_version_is_a_well_formed_alpha():
    match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)-(alpha|beta|rc)\.(\d+)", VERSION)
    assert match is not None, f"VERSION is not a prerelease tag: {VERSION!r}"
    parsed = parse_version(VERSION)
    assert parsed is not None and parsed.prerelease
    assert config.APP_VERSION == VERSION
    assert config.APP_CHANNEL == "alpha"


@pytest.mark.parametrize("text,expected", [
    ("0.1.0-alpha.1", "0.1.0-alpha.1"),
    ("v0.1.0-alpha.1", "0.1.0-alpha.1"),
    ("1.2.3", "1.2.3"),
    ("0.1.0-rc.2", "0.1.0-rc.2"),
])
def test_parse_version_normalises_tags(text, expected):
    assert str(parse_version(text)) == expected


@pytest.mark.parametrize("text", ["", "v1.3", "build-2026", "0.1.0-alpha", "abc",
                                  "0.1.0-alpha.x", "01.0.0", None, 3])
def test_malformed_tags_are_rejected(text):
    assert parse_version(text) is None


def test_prereleases_sort_below_the_stable_release():
    assert parse_version("0.1.0-alpha.1") < parse_version("0.1.0-alpha.2") < \
        parse_version("0.1.0-beta.1") < parse_version("0.1.0-rc.9") < parse_version("0.1.0")
    # The explicit requirement: a prerelease is never newer than stable of the same base.
    assert not (parse_version("0.1.0-rc.9") > parse_version("0.1.0"))
    assert parse_version("0.1.0-alpha.1") < parse_version("0.1.1-alpha.1")


def test_numeric_windows_metadata_is_stable_and_monotonic():
    assert numeric_version("0.1.0") == (0, 1, 0, 0)
    assert numeric_version("0.1.0-alpha.1") == (0, 1, 0, 1001)
    assert numeric_version("0.1.0-alpha.2") > numeric_version("0.1.0-alpha.1")
    assert numeric_version("0.1.1-alpha.1") > numeric_version("0.1.0-alpha.9")


def test_generated_packaging_resources_carry_the_same_version():
    info = release.version_info_text()
    assert "0.1.0-alpha.1" in info or VERSION in info
    assert release.numeric_text() in info
    iss = release.version_iss_text()
    assert f'#define MyAppVersion "{VERSION}"' in iss
    assert f'#define MyAppNumericVersion "{release.numeric_text()}"' in iss


def test_generated_resources_are_written_atomically_for_a_build(tmp_path):
    info, iss = release.write_version_files(tmp_path)
    assert info.is_file() and iss.is_file()
    assert VERSION in info.read_text(encoding="utf-8")
    assert VERSION in iss.read_text(encoding="utf-8")


def test_diagnostics_name_the_version_and_channel():
    text = diagnostics()
    assert VERSION in text
    assert "Alpha" in text
    assert display_version().startswith(VERSION)


def test_crash_log_records_the_version(monkeypatch, tmp_path):
    log = tmp_path / "crash.log"
    monkeypatch.setattr(crashlog, "_log_path", str(log))
    crashlog.record("test failure")
    text = log.read_text(encoding="utf-8")
    assert "test failure" in text
    assert VERSION in text


def test_portable_zip_and_checksums_are_real_files(tmp_path):
    dist = tmp_path / "dist" / "meeniFX"
    dist.mkdir(parents=True)
    (dist / "meeniFX.exe").write_bytes(b"exe")
    (dist / "nested").mkdir()
    (dist / "nested" / "data.bin").write_bytes(b"data")
    archive = release.make_portable_zip(dist, tmp_path / "dist")
    assert archive.is_file() and archive.name.endswith("-Windows-Portable.zip")
    import zipfile
    with zipfile.ZipFile(archive) as bundle:
        names = set(bundle.namelist())
    assert "meeniFX/meeniFX.exe" in names
    assert "meeniFX/nested/data.bin" in names

    exe = tmp_path / "dist" / f"meeniFX-Studios-{VERSION}-Windows-Setup.exe"
    exe.write_bytes(b"installer")
    sums = release.write_checksums([exe, archive], tmp_path / "dist" / "SHA256SUMS.txt")
    parsed = download.parse_checksums(sums.read_text(encoding="utf-8"))
    assert parsed[exe.name] == release.sha256_file(exe)
    assert parsed[archive.name] == release.sha256_file(archive)


def test_a_portable_zip_is_refused_for_an_unbuilt_folder(tmp_path):
    empty = tmp_path / "dist" / "meeniFX"
    empty.mkdir(parents=True)
    with pytest.raises(FileNotFoundError):
        release.make_portable_zip(empty, tmp_path / "dist")


def test_current_version_matches_the_constant():
    assert current_version() == parse_version(VERSION)
    assert current_version().stage < STABLE
