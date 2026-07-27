from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description="Create an offline-signed Creator Assistant release manifest")
    parser.add_argument("--installer", required=True)
    parser.add_argument("--edition", choices=("developer", "commercial"), required=True)
    parser.add_argument("--channel", choices=("developer", "stable", "beta"), required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--build-number", required=True, type=int)
    parser.add_argument("--minimum-supported-version", required=True)
    parser.add_argument("--architecture", default="x86_64")
    parser.add_argument("--download-url", required=True)
    parser.add_argument("--release-notes", default="")
    parser.add_argument("--mandatory", action="store_true")
    parser.add_argument("--private-key-file", required=True)
    parser.add_argument("--key-id", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root / "src"))
    from creator_assistant.infrastructure.release_updates import sign_manifest

    installer = Path(args.installer).resolve()
    data = installer.read_bytes()
    payload = {
        "schema_version": 1,
        "edition": args.edition,
        "channel": args.channel,
        "version": args.version,
        "build_number": args.build_number,
        "minimum_supported_version": args.minimum_supported_version,
        "architecture": args.architecture,
        "download_url": args.download_url,
        "sha256": hashlib.sha256(data).hexdigest(),
        "file_size": len(data),
        "release_notes": args.release_notes,
        "published_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "mandatory": args.mandatory,
    }
    private_key = Path(args.private_key_file).read_text(encoding="ascii").strip()
    manifest = sign_manifest(payload, private_key, args.key_id)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
