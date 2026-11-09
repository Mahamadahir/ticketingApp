"""Builds the Teams app package (a zip of manifest.json and two icons) for staging or production.

    python teams-app/build_package.py --environment staging --bot-app-id <guid> \\
        --public-base-url https://... --developer-name "Vardags IT" [--version 1.0.0] [--out dist]

The privacy, terms and website addresses default to pages of the environment's public base URL (PUBLIC_BASE_URL):
`<base>/legal/privacy` and `<base>/legal/terms` (served by the app, text edited under Admin > Email templates) and
`<base>/portal` (the portal home). Each can still be set by hand with --privacy-url, --terms-url and --website-url,
in which case --public-base-url is not needed for it. The developer name is shown in Teams as the publisher.

The bot application id is the same value as TEAMS_BOT_APP_ID and the Bicep parameter teamsBotAppId (with a managed
identity, the identity's client id: `teamsBotAppId` in the deployment outputs). It is an identifier, not a secret, and
nothing secret goes into the package. Upload the zip in the Teams admin center (Manage apps > Upload new app) and
install it for a test user. Raise `--version` each time the package changes, or Teams keeps the old one.

Standard library only. The icons are drawn here (plain squares, no artwork): the colour icon is 192x192 and the
outline icon 32x32 with a transparent background and a white outline, which is what Teams requires.
"""

import argparse
import json
import re
import struct
import sys
import zlib
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
TEMPLATE = HERE / "manifest.template.json"
GUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
VERSION = re.compile(r"^\d+\.\d+\.\d+$")
NAMES = {
    "staging": ("IT Support (staging)", "IT Support ticketing (staging)"),
    "production": ("IT Support", "IT Support ticketing"),
}
ACCENT = (0x2F, 0x55, 0x97)


def png(width: int, height: int, pixel) -> bytes:
    """A PNG of the given size; pixel(x, y) gives (r, g, b, a)."""
    rows = b"".join(b"\x00" + b"".join(bytes(pixel(x, y)) for x in range(width)) for y in range(height))

    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    header = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(rows, 9)) + chunk(b"IEND", b"")


def color_icon() -> bytes:
    """192x192, a solid square with a white ticket-shaped block in the middle."""
    def pixel(x, y):
        inside = 56 <= x < 136 and 72 <= y < 120
        return (255, 255, 255, 255) if inside else (*ACCENT, 255)

    return png(192, 192, pixel)


def outline_icon() -> bytes:
    """32x32, transparent, with a white outline of the same block."""
    def pixel(x, y):
        on_edge = (7 <= x < 25 and 10 <= y < 22) and not (8 <= x < 24 and 11 <= y < 21)
        return (255, 255, 255, 255) if on_edge else (0, 0, 0, 0)

    return png(32, 32, pixel)


def fill(value, replacements: dict[str, str]):
    if isinstance(value, str):
        for name, text in replacements.items():
            value = value.replace("{{" + name + "}}", text)
        if "{{" in value:
            raise ValueError(f"A placeholder in the manifest was not filled: {value}")
        return value
    if isinstance(value, list):
        return [fill(v, replacements) for v in value]
    if isinstance(value, dict):
        return {k: fill(v, replacements) for k, v in value.items()}
    return value


def default_urls(public_base_url: str) -> dict[str, str]:
    """The website, privacy and terms addresses under the environment's public base URL."""
    base = public_base_url.strip().rstrip("/")
    if not base.startswith("https://"):
        raise ValueError("The public base URL must be an https address")
    return {"website": f"{base}/portal", "privacy": f"{base}/legal/privacy", "terms": f"{base}/legal/terms"}


def manifest(environment: str, bot_app_id: str, version: str, developer: str, website: str, privacy: str, terms: str) -> dict:
    if environment not in NAMES:
        raise ValueError("The environment must be staging or production")
    if not GUID.match(bot_app_id.lower()):
        raise ValueError("The bot application id must be a GUID")
    if not VERSION.match(version):
        raise ValueError("The version must look like 1.0.0")
    for label, url in (("website", website), ("privacy", privacy), ("terms", terms)):
        if not url.startswith("https://"):
            raise ValueError(f"The {label} URL must be an https address")
    short, full = NAMES[environment]
    return fill(
        json.loads(TEMPLATE.read_text(encoding="utf-8")),
        {
            "VERSION": version, "BOT_APP_ID": bot_app_id.lower(), "DEVELOPER_NAME": developer, "WEBSITE_URL": website,
            "PRIVACY_URL": privacy, "TERMS_URL": terms, "SHORT_NAME": short, "FULL_NAME": full,
        },
    )


def build(out_dir: Path, environment: str, **fields) -> Path:
    body = manifest(environment, **fields)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"it-support-{environment}-{fields['version']}.zip"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("manifest.json", json.dumps(body, indent=2) + "\n")
        archive.writestr("color.png", color_icon())
        archive.writestr("outline.png", outline_icon())
    return path


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--environment", required=True, choices=sorted(NAMES))
    parser.add_argument("--bot-app-id", required=True)
    parser.add_argument("--public-base-url", help="the environment's public base URL; the three addresses below default to pages under it")
    parser.add_argument("--website-url", help="default: <public base URL>/portal")
    parser.add_argument("--privacy-url", help="default: <public base URL>/legal/privacy")
    parser.add_argument("--terms-url", help="default: <public base URL>/legal/terms")
    parser.add_argument("--developer-name", default="IT Support", help='the publisher shown in Teams, for example "Vardags IT"')
    parser.add_argument("--version", default="1.0.0")
    parser.add_argument("--out", default="dist")
    args = parser.parse_args(argv)
    try:
        defaults = default_urls(args.public_base_url) if args.public_base_url else {}
        urls = {
            "website": args.website_url or defaults.get("website"),
            "privacy": args.privacy_url or defaults.get("privacy"),
            "terms": args.terms_url or defaults.get("terms"),
        }
        missing = [name for name, url in urls.items() if not url]
        if missing:
            raise ValueError(f"Give --public-base-url, or the address of: {', '.join(missing)}")
        path = build(
            Path(args.out), args.environment, bot_app_id=args.bot_app_id, version=args.version, developer=args.developer_name,
            website=urls["website"], privacy=urls["privacy"], terms=urls["terms"],
        )
    except ValueError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    print(path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
