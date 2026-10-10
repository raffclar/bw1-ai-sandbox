#!/usr/bin/env python3
"""Unpack the uploaded game-files archive into bw1-decomp's orig/ folder, safely.

Accepts a .zip (e.g. bw1-original.zip) or a .tar.gz (scripts/pack-orig.ps1). Every member must sit under
ONE top folder named `bw1-original/` or `orig/`; that folder is stripped, so `bw1-original/BW1W120/x.exe`
becomes `<dest>/BW1W120/x.exe`. Refused: any other top folder, absolute paths, `..`, drive letters,
backslashes, symlinks, hard links and device files. Nothing is written until every member has been checked.

Usage: unpack-orig.py ARCHIVE DEST
"""

import shutil
import sys
import tarfile
import zipfile
from pathlib import Path, PurePosixPath

TOPS = ("bw1-original", "orig")


class Refused(Exception):
    pass


def check_name(name: str) -> PurePosixPath:
    """Return the path below the top folder, or raise Refused."""
    if "\\" in name or name.startswith("/") or (len(name) > 1 and name[1] == ":"):
        raise Refused(f"unsafe path: {name!r}")
    parts = PurePosixPath(name).parts
    if not parts or parts[0] not in TOPS:
        raise Refused(f"member outside a bw1-original/ or orig/ top folder: {name!r}")
    if any(p in ("..", "") for p in parts):
        raise Refused(f"path traversal: {name!r}")
    return PurePosixPath(*parts[1:]) if len(parts) > 1 else PurePosixPath()


def members(archive: Path):
    """(top, relative path, is_dir, reader) for each member, all checked first."""
    out, tops = [], set()
    if archive.name.lower().endswith(".zip"):
        zf = zipfile.ZipFile(archive)
        for info in zf.infolist():
            mode = (info.external_attr >> 16) & 0o170000
            if mode in (0o120000,):  # symlink stored in a zip
                raise Refused(f"symlink in archive: {info.filename!r}")
            rel = check_name(info.filename.rstrip("/") if info.is_dir() else info.filename)
            tops.add(PurePosixPath(info.filename).parts[0])
            out.append((rel, info.is_dir(), (lambda i=info: zf.open(i))))
    elif archive.name.lower().endswith((".tar.gz", ".tgz")):
        tf = tarfile.open(archive, "r:gz")
        for m in tf.getmembers():
            if not (m.isfile() or m.isdir()):
                raise Refused(f"not a plain file or folder: {m.name!r}")
            rel = check_name(m.name.rstrip("/"))
            tops.add(PurePosixPath(m.name).parts[0])
            out.append((rel, m.isdir(), (lambda mm=m: tf.extractfile(mm))))
    else:
        raise Refused(f"not a .zip or .tar.gz: {archive.name}")
    if len(tops) != 1:
        raise Refused(f"expected exactly one top folder (bw1-original/ or orig/), found {sorted(tops)}")
    return out


def main(argv) -> int:
    if len(argv) != 2:
        print(__doc__, file=sys.stderr)
        return 64
    archive, dest = Path(argv[0]), Path(argv[1])
    try:
        entries = members(archive)
    except (Refused, zipfile.BadZipFile, tarfile.TarError) as e:
        print(f"REFUSED {archive}: {e}", file=sys.stderr)
        return 1
    dest.mkdir(parents=True, exist_ok=True)
    root = dest.resolve()
    count = 0
    for rel, is_dir, opener in entries:
        target = (root / rel).resolve()
        if target != root and root not in target.parents:  # belt and braces after check_name
            print(f"REFUSED {archive}: {rel} escapes {root}", file=sys.stderr)
            return 1
        if is_dir:
            target.mkdir(parents=True, exist_ok=True)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        with opener() as src, open(target, "wb") as dst:
            shutil.copyfileobj(src, dst)
        count += 1
    print(f"unpacked {count} file(s) from {archive.name} into {root}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
