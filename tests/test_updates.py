"""The update service: channel selection, caching, settings, checksums and safety rules.

GitHub is replaced by a fake opener, so these tests cover rate limits, offline starts,
malformed metadata and bad checksums without touching the network.
"""

import hashlib
import json
import types
from datetime import datetime, timedelta, timezone
from urllib.error import HTTPError, URLError

import pytest

from meenifx.updates import download, installer, model, service, whats_new
from meenifx.updates.settings import UpdateSettings
from meenifx.version import VERSION, parse_version

TAG = "v0.1.0-alpha.2"
ASSET_URL = f"https://github.com/meeniDev/meeniFX/releases/download/{TAG}/file.exe"


def asset(name, tag=TAG, size=1000, url=None):
    return {"name": name, "size": size,
            "browser_download_url": url or
            f"https://github.com/meeniDev/meeniFX/releases/download/{tag}/{name}"}


def payload(tag=TAG, *, prerelease=None, installer=True, portable=True, checksums=True,
            notes="### Added\n- Something new", published="2026-11-01T10:00:00Z",
            url=None):
    text = tag.lstrip("v")
    assets = []
    if installer:
        assets.append(asset(model.installer_name(text), tag))
    if portable:
        assets.append(asset(model.portable_name(text), tag))
    if checksums:
        assets.append(asset(model.CHECKSUMS_NAME, tag))
    data = {"tag_name": tag, "draft": False, "body": notes, "published_at": published,
            "html_url": url or f"https://github.com/meeniDev/meeniFX/releases/tag/{tag}",
            "assets": assets}
    if prerelease is not None:
        data["prerelease"] = prerelease
    return data


class Response:
    def __init__(self, data=None, *, status=200, headers=None, url=""):
        if data is None:
            data = []
        self._data = data if isinstance(data, bytes) else json.dumps(data).encode("utf-8")
        self.status = status
        self.headers = headers or {}
        self._url = url
        self._pos = 0

    def read(self, n=-1):
        if n is None or n < 0:
            chunk, self._pos = self._data[self._pos:], len(self._data)
            return chunk
        chunk = self._data[self._pos:self._pos + n]
        self._pos += len(chunk)
        return chunk

    def geturl(self):
        return self._url

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class Opener:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []

    def __call__(self, request, timeout=None):
        self.requests.append(request)
        item = self.responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


class Store(dict):
    def value(self, key, default=None):
        return dict.get(self, key, default)

    def setValue(self, key, value):
        self[key] = value


# --------------------------------------------------------------------- selection
def test_alpha_channel_picks_the_newest_installable_prerelease():
    releases = [model.parse_release(payload("v0.1.0-alpha.1")),
                model.parse_release(payload("v0.1.0-alpha.3")),
                model.parse_release(payload("v0.0.9"))]
    chosen = model.select_update(releases, "0.1.0-alpha.1", "alpha")
    assert chosen is not None and chosen.version_text == "0.1.0-alpha.3"
    assert chosen.installer.name.endswith("-Windows-Setup.exe")


def test_stable_channel_skips_prereleases_but_takes_a_newer_stable():
    alpha = model.parse_release(payload("v0.1.1-alpha.1"))
    stable = model.parse_release(payload("v0.1.0"))
    assert model.select_update([alpha, stable], "0.1.0-alpha.9", "stable").version_text == "0.1.0"
    assert model.select_update([alpha], "0.1.0-alpha.9", "stable") is None


def test_a_release_flagged_prerelease_is_not_stable_just_because_of_its_tag():
    fake = payload("v0.2.0", prerelease=True)
    release = model.parse_release(fake)
    assert release.prerelease is True
    assert model.select_update([release], "0.1.0", "stable") is None


def test_older_releases_are_never_offered():
    old = model.parse_release(payload("v0.1.0-alpha.1"))
    assert model.select_update([old], "0.1.0-alpha.1", "alpha") is None
    assert model.select_update([old], "0.1.0-alpha.2", "alpha") is None


def test_a_release_without_a_windows_installer_is_skipped():
    no_installer = model.parse_release(payload("v0.1.0-alpha.5", installer=False))
    good = model.parse_release(payload("v0.1.0-alpha.2"))
    assert model.select_update([no_installer, good], "0.1.0-alpha.1").version_text == \
        "0.1.0-alpha.2"


def test_untrusted_asset_urls_and_malformed_tags_are_rejected():
    evil = payload("v0.1.0-alpha.2")
    evil["assets"][0] = asset(model.installer_name("0.1.0-alpha.2"),
                              url="https://evil.example/meeniFX-Studios-0.1.0-alpha.2-Windows-Setup.exe")
    release = model.parse_release(evil)
    assert release.asset_named(model.installer_name("0.1.0-alpha.2")) is None
    assert model.parse_release(payload("build-2026")) is None
    assert model.parse_release(payload("v0.1.0-alpha.2")) is not None


def test_a_malformed_release_url_falls_back_to_the_releases_page():
    broken = payload("v0.1.0-alpha.2", url="https://evil.example/release")
    release = model.parse_release(broken)
    assert release.html_url.startswith(model.RELEASES_PAGE)


def test_asset_names_include_the_normalised_version():
    assert model.installer_name("0.1.0-alpha.2") == \
        "meeniFX-Studios-0.1.0-alpha.2-Windows-Setup.exe"
    assert model.portable_name("0.1.0-alpha.2") == \
        "meeniFX-Studios-0.1.0-alpha.2-Windows-Portable.zip"


# --------------------------------------------------------------------- service
def service_for(tmp_path, *responses, clock=None):
    return service.ReleaseService(tmp_path / "cache.json", opener=Opener(*responses),
                                  clock=clock or (lambda: 1000.0))


def test_a_fresh_cache_avoids_a_second_request(tmp_path):
    svc = service_for(tmp_path, Response([payload()], headers={"ETag": '"abc"'}))
    first = svc.check(current="0.1.0-alpha.1")
    assert first.ok and first.update and not first.from_cache
    assert len(svc.opener.requests) == 1
    second = svc.check(current="0.1.0-alpha.1")
    assert second.ok and second.update and second.from_cache
    assert len(svc.opener.requests) == 1, "a manual check within the interval hit GitHub"


def test_forced_check_revalidates_with_the_etag(tmp_path):
    svc = service_for(tmp_path, Response([payload()], headers={"ETag": '"abc"'}))
    svc.check(current="0.1.0-alpha.1")
    error = HTTPError("https://api.github.com", 304, "Not Modified", {}, None)
    svc.opener = Opener(error)
    result = svc.check(current="0.1.0-alpha.1", force=True)
    assert result.ok and result.update and result.from_cache
    request = svc.opener.requests[0]
    assert any(key.lower() == "if-none-match" for key in request.headers)


def test_rate_limiting_offline_and_bad_json_are_friendly_results(tmp_path):
    limited = HTTPError("https://api.github.com", 403, "rate", {
        "X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "1700000000"}, None)
    result = service_for(tmp_path, limited).check()
    assert result.error and result.error.kind == "rate_limited"
    assert "limit" in result.error.message.lower()

    offline = service_for(tmp_path / "b", URLError("no route")).check()
    assert offline.error and offline.error.kind == "offline"

    invalid = service_for(tmp_path / "c", Response(b"{not json")).check()
    assert invalid.error and invalid.error.kind == "invalid"

    missing = service_for(tmp_path / "d", HTTPError("u", 404, "gone", {}, None)).check()
    assert missing.error and missing.error.kind == "no_release"


def test_no_releases_is_a_successful_empty_check(tmp_path):
    result = service_for(tmp_path, Response([])).check()
    assert result.ok and result.update is None
    assert not result.from_cache


def test_malformed_release_entries_do_not_break_the_list(tmp_path):
    result = service_for(tmp_path, Response([
        {"tag_name": "build-2026", "assets": []},
        payload()])).check(current="0.1.0-alpha.1")
    assert result.ok and result.update.version_text == "0.1.0-alpha.2"


def test_a_stale_cache_is_refetched(tmp_path):
    clock = {"now": 1000.0}
    svc = service.ReleaseService(tmp_path / "cache.json",
                                 opener=Opener(Response([payload("v0.1.0-alpha.2")]),
                                               Response([payload("v0.1.0-alpha.3")])),
                                 clock=lambda: clock["now"])
    first = svc.check(current="0.1.0-alpha.1")
    assert first.update.version_text == "0.1.0-alpha.2"
    clock["now"] += service.CACHE_TTL_SECONDS + 1
    second = svc.check(current="0.1.0-alpha.1")
    assert second.update.version_text == "0.1.0-alpha.3"
    assert len(svc.opener.requests) == 2


# --------------------------------------------------------------------- settings
def test_defaults_are_automatic_checks_on_the_alpha_channel():
    settings = UpdateSettings.load(Store())
    assert settings.auto_check is True
    assert settings.auto_download is True
    assert settings.channel == "alpha"
    assert settings.skipped == []


def test_skipped_versions_channels_and_auto_download_persist(tmp_path):
    store = Store()
    settings = UpdateSettings.load(store)
    settings.channel = "stable"
    settings.auto_check = False
    settings.auto_download = False
    settings.skip("0.1.0-alpha.2")
    settings.save(store)
    loaded = UpdateSettings.load(store)
    assert (loaded.channel, loaded.auto_check, loaded.auto_download) == ("stable", False, False)
    assert loaded.is_skipped("0.1.0-alpha.2")


def test_skip_only_suppresses_that_version_and_manual_checks_win():
    settings = UpdateSettings()
    settings.skip("0.1.0-alpha.2")
    update = types.SimpleNamespace(version_text="0.1.0-alpha.2")
    assert settings.should_notify(update) is False
    assert settings.should_notify(update, manual=True) is True
    newer = types.SimpleNamespace(version_text="0.1.0-alpha.3")
    assert settings.should_notify(newer) is True
    settings.clear_skipped()
    assert settings.should_notify(update) is True


def test_remind_me_later_waits_a_day_and_manual_checks_ignore_it():
    now = datetime(2026, 11, 8, 12, 0, tzinfo=timezone.utc)
    settings = UpdateSettings()
    settings.remind_later(now)
    update = types.SimpleNamespace(version_text="0.1.0-alpha.2")
    assert settings.reminder_active(now + timedelta(hours=1))
    assert settings.should_notify(update, now + timedelta(hours=1)) is False
    assert settings.should_notify(update, now + timedelta(hours=1), manual=True) is True
    assert not settings.reminder_active(now + timedelta(hours=25))


def test_automatic_checks_respect_the_interval():
    now = datetime(2026, 11, 8, 12, 0, tzinfo=timezone.utc)
    settings = UpdateSettings()
    assert settings.should_auto_check(now)
    settings.mark_checked(now)
    assert not settings.should_auto_check(now + timedelta(hours=1))
    assert settings.should_auto_check(now + timedelta(hours=7))
    settings.auto_check = False
    assert not settings.should_auto_check(now + timedelta(days=2))


def test_whats_new_stays_pending_until_it_is_acknowledged():
    settings = UpdateSettings(last_seen_version="0.1.0-alpha.1",
                              last_ack_version="0.1.0-alpha.1")
    assert settings.record_launch("0.1.0-alpha.2") is True
    store = Store()
    settings.save(store)
    loaded = UpdateSettings.load(store)
    assert loaded.record_launch("0.1.0-alpha.2") is True, "unread notes were dropped"
    loaded.acknowledge_whats_new()
    assert loaded.record_launch("0.1.0-alpha.2") is False


def test_whats_new_is_shown_once_only_after_a_real_update():
    settings = UpdateSettings()
    assert settings.record_launch("0.1.0-alpha.1") is False, "a first install is not an update"
    assert settings.pending_whats_new == ""
    assert settings.record_launch("0.1.0-alpha.2") is True
    assert settings.pending_whats_new == "0.1.0-alpha.2"
    settings.acknowledge_whats_new()
    assert settings.pending_whats_new == ""
    assert settings.last_ack_version == "0.1.0-alpha.2"
    assert settings.record_launch("0.1.0-alpha.2") is False


# --------------------------------------------------------------------- checksums
def test_a_checksum_file_is_fetched_only_from_the_official_release(tmp_path):
    body = f"{'c' * 64}  installer.exe\n".encode()
    url = "https://github.com/meeniDev/meeniFX/releases/download/v0.1.0-alpha.2/SHA256SUMS.txt"
    parsed = download.fetch_checksums(url, opener=Opener(download_response(body, url=url)))
    assert parsed["installer.exe"] == "c" * 64
    with pytest.raises(download.DownloadError) as caught:
        download.fetch_checksums("https://evil.example/SHA256SUMS.txt")
    assert caught.value.kind == "untrusted"


def test_githubs_current_release_asset_redirect_host_is_trusted(tmp_path):
    body = f"{'d' * 64}  installer.exe\n".encode()
    source = ("https://github.com/meeniDev/meeniFX/releases/download/"
              "v0.1.0-alpha.2/SHA256SUMS.txt")
    final = "https://release-assets.githubusercontent.com/github-production-release-asset/file"
    parsed = download.fetch_checksums(source, opener=Opener(download_response(body, url=final)))
    assert parsed["installer.exe"] == "d" * 64
    assert model.is_trusted_redirect_url(final)
    assert model.is_trusted_redirect_url("https://objects.githubusercontent.com/asset")
    assert not model.is_trusted_redirect_url("https://release-assets.githubusercontent.com.evil/x")


def test_checksums_parse_common_sha256sum_shapes():
    digest = "a" * 64
    parsed = download.parse_checksums(f"{digest}  installer.exe\n# comment\n"
                                      f"{'b' * 64} *portable.zip\nnot a line\n")
    assert parsed["installer.exe"] == digest
    assert parsed["portable.zip"] == "b" * 64
    assert download.checksum_for(f"{digest} *installer.exe", "installer.exe") == digest
    assert download.checksum_for("", "installer.exe") is None


def download_response(data, url=ASSET_URL, headers=None):
    return Response(data, headers=headers or {"Content-Length": str(len(data))}, url=url)


def test_a_verified_download_writes_the_file_and_reports_progress(tmp_path):
    data = b"installer bytes" * 5000
    digest = hashlib.sha256(data).hexdigest()
    seen = []
    target = tmp_path / "meeniFX-Setup.exe"
    result = download.download(ASSET_URL, target, digest, expected_size=len(data),
                               progress=lambda done, total: seen.append((done, total)),
                               opener=Opener(download_response(data)))
    assert result == str(target)
    assert target.read_bytes() == data
    assert not (tmp_path / "meeniFX-Setup.exe.part").exists()
    assert seen and seen[-1][0] == len(data) and seen[-1][1] == len(data)


def test_a_bad_checksum_removes_every_trace(tmp_path):
    data = b"installer"
    target = tmp_path / "meeniFX-Setup.exe"
    with pytest.raises(download.DownloadError) as caught:
        download.download(ASSET_URL, target, "0" * 64,
                          opener=Opener(download_response(data)))
    assert caught.value.kind == "checksum"
    assert not target.exists()
    assert not (tmp_path / "meeniFX-Setup.exe.part").exists()


def test_a_cancelled_download_leaves_nothing(tmp_path):
    cancel = __import__("threading").Event()
    cancel.set()
    with pytest.raises(download.DownloadError) as caught:
        download.download(ASSET_URL, tmp_path / "setup.exe", "0" * 64,
                          cancel=cancel, opener=Opener(download_response(b"data")))
    assert caught.value.kind == "cancelled"
    assert not (tmp_path / "setup.exe.part").exists()


def test_size_limits_and_untrusted_urls_are_refused(tmp_path):
    with pytest.raises(download.DownloadError) as caught:
        download.download(ASSET_URL, tmp_path / "setup.exe", "0" * 64, max_bytes=4,
                          opener=Opener(download_response(b"1234567890")))
    assert caught.value.kind == "too_large"
    with pytest.raises(download.DownloadError) as caught:
        download.download("https://evil.example/setup.exe", tmp_path / "setup.exe", "0" * 64,
                          opener=Opener(download_response(b"x")))
    assert caught.value.kind == "untrusted"
    with pytest.raises(download.DownloadError) as caught:
        download.download(ASSET_URL, tmp_path / "setup.exe", "0" * 64,
                          opener=Opener(download_response(b"x",
                                                          url="https://evil.example/asset")))
    assert caught.value.kind == "untrusted"


def test_disk_space_is_checked_before_downloading(tmp_path, monkeypatch):
    monkeypatch.setattr(download.shutil, "disk_usage",
                        lambda path: types.SimpleNamespace(free=1024))
    with pytest.raises(download.DownloadError) as caught:
        download.download(ASSET_URL, tmp_path / "setup.exe", "0" * 64, expected_size=10_000_000,
                          opener=Opener(download_response(b"x")))
    assert caught.value.kind == "disk"


def test_verify_file_reports_the_actual_digest(tmp_path):
    target = tmp_path / "file.bin"
    target.write_bytes(b"abc")
    digest = hashlib.sha256(b"abc").hexdigest()
    assert download.verify_file(target, digest) == (True, digest)
    ok, actual = download.verify_file(target, "0" * 64)
    assert not ok and actual == digest


# --------------------------------------------------------------------- installer
def test_installation_kind_distinguishes_installer_portable_and_source(tmp_path):
    assert installer.installation_kind(frozen=False, exe_dir=tmp_path) == "source"
    assert installer.installation_kind(frozen=True, exe_dir=tmp_path) == "portable"
    (tmp_path / "unins000.exe").write_bytes(b"uninstaller")
    assert installer.installation_kind(frozen=True, exe_dir=tmp_path) == "installed"


def test_only_the_expected_installer_file_is_accepted(tmp_path):
    good = tmp_path / model.installer_name("0.1.0-alpha.2")
    good.write_bytes(b"installer")
    assert installer.validate_installer(good) == str(good)
    wrong = tmp_path / "something.exe"
    wrong.write_bytes(b"x")
    with pytest.raises(installer.InstallerError):
        installer.validate_installer(wrong)
    with pytest.raises(installer.InstallerError):
        installer.validate_installer(tmp_path / "missing.exe")


def test_installer_arguments_are_silent_and_never_request_elevation():
    arguments = installer.installer_arguments()
    assert "/SILENT" in arguments and "/NORESTART" in arguments
    assert not any(argument.lower().startswith("/admin") for argument in arguments)


# --------------------------------------------------------------------- notes
def test_release_notes_bundle_and_parse_into_sections():
    text = whats_new.notes_text(VERSION)
    assert "meeniFX Studios" in text
    sections = whats_new.parse_notes(text)
    headings = [heading for heading, _items in sections]
    assert "Added" in headings and "Fixed" in headings
    assert any("timeline" in item.lower() for _heading, items in sections for item in items)


def test_whats_new_url_points_at_the_matching_release():
    assert whats_new.notes_url(VERSION).endswith(f"/tag/v{VERSION}")


def test_a_release_body_wins_over_the_bundled_notes():
    assert whats_new.notes_text(VERSION, "### Added\n- From GitHub") == "### Added\n- From GitHub"


def test_parse_notes_without_headings_uses_highlights():
    sections = whats_new.parse_notes("- one\n- two")
    assert sections == [("Highlights", ["one", "two"])]
