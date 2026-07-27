from __future__ import annotations

import argparse
import base64
import os
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate an offline Ed25519 update key pair")
    parser.add_argument("--private-output", required=True)
    parser.add_argument("--public-output", required=True)
    args = parser.parse_args()
    private_path, public_path = Path(args.private_output), Path(args.public_output)
    for path in (private_path, public_path):
        if path.exists():
            raise SystemExit(f"Refusing to overwrite {path}")
        path.parent.mkdir(parents=True, exist_ok=True)
    private = Ed25519PrivateKey.generate()
    private_raw = private.private_bytes(
        serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption()
    )
    public_raw = private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    private_path.write_text(base64.b64encode(private_raw).decode("ascii"), encoding="ascii")
    public_path.write_text(base64.b64encode(public_raw).decode("ascii"), encoding="ascii")
    if os.name != "nt":
        private_path.chmod(0o600)
    print(f"Private key (keep offline): {private_path}")
    print(f"Public key: {public_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
