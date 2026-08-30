#!/usr/bin/env python3
"""Install one sha-bound FlashInfer source distribution into the image."""

from __future__ import annotations

import argparse
import hashlib
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path, PurePosixPath
from urllib.request import Request, urlopen


SOURCE_ROOT = Path("/tmp/weinfer-flashinfer-source")
MAX_SDIST_BYTES = 128 * 1024 * 1024


def download(url: str) -> bytes:
    request = Request(url, headers={"User-Agent": "weinfer-image-builder/1"})
    with urlopen(request, timeout=180) as response:
        declared = response.headers.get("Content-Length")
        if declared is not None and int(declared) > MAX_SDIST_BYTES:
            raise RuntimeError("FlashInfer source distribution exceeds size bound")
        body = response.read(MAX_SDIST_BYTES + 1)
    if len(body) > MAX_SDIST_BYTES:
        raise RuntimeError("FlashInfer source distribution exceeds size bound")
    return body


def safe_member(member: tarfile.TarInfo) -> bool:
    path = PurePosixPath(member.name)
    return (
        not path.is_absolute()
        and ".." not in path.parts
        and not member.issym()
        and not member.islnk()
        and (member.isfile() or member.isdir())
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True)
    parser.add_argument("--sha256", required=True)
    args = parser.parse_args()
    if len(args.sha256) != 64 or any(c not in "0123456789abcdef" for c in args.sha256):
        parser.error("--sha256 must be a lowercase SHA-256 digest")

    body = download(args.url)
    observed = hashlib.sha256(body).hexdigest()
    if observed != args.sha256:
        raise SystemExit("FlashInfer source distribution SHA-256 mismatch")

    if SOURCE_ROOT.exists():
        shutil.rmtree(SOURCE_ROOT)
    SOURCE_ROOT.mkdir(mode=0o700)
    archive = SOURCE_ROOT / "flashinfer-python.tar.gz"
    archive.write_bytes(body)
    extract_root = SOURCE_ROOT / "unpacked"
    extract_root.mkdir()
    with tarfile.open(archive, "r:gz") as bundle:
        members = bundle.getmembers()
        if not members or not all(safe_member(member) for member in members):
            raise SystemExit("FlashInfer source distribution has an unsafe member")
        bundle.extractall(extract_root, members=members, filter="data")

    roots = [path for path in extract_root.iterdir() if path.is_dir()]
    if len(roots) != 1 or roots[0].name != "flashinfer_python-0.3.1":
        raise SystemExit("FlashInfer source distribution root drift")
    subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--no-cache-dir",
            "--no-deps",
            "--no-build-isolation",
            "--force-reinstall",
            str(roots[0]),
        ],
        check=True,
    )
    print(f"FlashInfer source verified and installed: sha256={observed}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
