"""8A: the Teams app package builder (teams-app/build_package.py)."""

import importlib.util
import io
import json
import re
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("build_package", ROOT / "teams-app" / "build_package.py")
build_package = importlib.util.module_from_spec(spec)
spec.loader.exec_module(build_package)

APP_ID = "11111111-1111-1111-1111-111111111111"
ARGS = dict(bot_app_id=APP_ID, version="1.2.3", developer="Vardags IT", website="https://it.example.org", privacy="https://it.example.org/privacy", terms="https://it.example.org/terms")


def read(path):
    with zipfile.ZipFile(path) as archive:
        return {name: archive.read(name) for name in archive.namelist()}


def test_the_package_holds_a_manifest_and_two_icons_with_the_ids_filled_in(tmp_path):
    files = read(build_package.build(tmp_path, "staging", **ARGS))
    assert sorted(files) == ["color.png", "manifest.json", "outline.png"]
    manifest = json.loads(files["manifest.json"])
    assert manifest["id"] == APP_ID and manifest["bots"][0]["botId"] == APP_ID
    assert manifest["bots"][0]["scopes"] == ["personal"]
    assert manifest["version"] == "1.2.3" and manifest["name"]["short"] == "IT Support (staging)"
    assert manifest["developer"]["privacyUrl"] == "https://it.example.org/privacy"
    assert "{{" not in files["manifest.json"].decode()
    assert len(manifest["name"]["short"]) <= 30 and len(manifest["description"]["short"]) <= 80


def test_production_has_its_own_name(tmp_path):
    manifest = json.loads(read(build_package.build(tmp_path, "production", **ARGS))["manifest.json"])
    assert manifest["name"] == {"short": "IT Support", "full": "IT Support ticketing"}


def test_icons_are_the_sizes_teams_requires(tmp_path):
    files = read(build_package.build(tmp_path, "staging", **ARGS))
    sizes = {n: (int.from_bytes(files[n][16:20], "big"), int.from_bytes(files[n][20:24], "big")) for n in ("color.png", "outline.png")}
    assert sizes == {"color.png": (192, 192), "outline.png": (32, 32)}
    assert all(files[n].startswith(b"\x89PNG") for n in sizes)


def test_the_package_holds_no_secret_looking_value(tmp_path):
    text = read(build_package.build(tmp_path, "staging", **ARGS))["manifest.json"].decode()
    assert not re.search(r"secret|password|token", text, re.I)


@pytest.mark.parametrize(
    "change",
    [dict(bot_app_id="not-a-guid"), dict(version="one"), dict(website="http://insecure.example"), dict(privacy="ftp://x"), dict(terms="")],
)
def test_bad_values_are_refused(tmp_path, change):
    with pytest.raises(ValueError):
        build_package.build(tmp_path, "staging", **(ARGS | change))


def test_an_unknown_environment_is_refused(tmp_path):
    with pytest.raises(ValueError):
        build_package.build(tmp_path, "dev", **ARGS)


def test_the_command_line_writes_the_zip_and_reports_errors(tmp_path, capsys):
    argv = ["--environment", "staging", "--bot-app-id", APP_ID, "--website-url", "https://a.example", "--privacy-url", "https://a.example/p",
            "--terms-url", "https://a.example/t", "--out", str(tmp_path)]
    assert build_package.main(argv) == 0
    assert (tmp_path / "it-support-staging-1.0.0.zip").exists()
    assert build_package.main(argv[:3] + ["x"] + argv[4:]) == 2
    assert "GUID" in capsys.readouterr().err
