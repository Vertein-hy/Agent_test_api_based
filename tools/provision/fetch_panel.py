"""Fetch pinned panel build inputs; preserve upstream files and verify every archive.

This explicit operator tool never runs during import or application startup.
Archives extract in a temporary directory; unsafe paths and links are rejected
before any vendor tree is installed. Existing complete inputs are not replaced.
"""

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import shutil
import tarfile
import tempfile
import urllib.request

ROOT = Path(__file__).resolve().parents[2]


def verified_download(url, destination, expected):
    digest = hashlib.sha256()
    with urllib.request.urlopen(url, timeout=60) as response, destination.open("xb") as output:
        for chunk in iter(lambda: response.read(1024 * 1024), b""):
            digest.update(chunk)
            output.write(chunk)
    if digest.hexdigest() != expected:
        destination.unlink()
        raise ValueError("Upstream archive checksum mismatch")


def extract_source(archive, destination):
    """Require one archive root and regular files/directories; no link traversal."""
    with tarfile.open(archive, "r:gz") as source:
        members = source.getmembers()
        roots = set()
        for member in members:
            path = PurePosixPath(member.name)
            if (
                path.is_absolute()
                or not path.parts
                or ".." in path.parts
                or not (member.isfile() or member.isdir())
            ):
                raise ValueError("Unsafe upstream archive member")
            roots.add(path.parts[0])
        if len(roots) != 1:
            raise ValueError("Expected one upstream archive root")
        source.extractall(destination, members=members, filter="data")
    return destination / roots.pop()


def main():
    parser = argparse.ArgumentParser()
    parser.parse_args()
    provenance = json.loads((ROOT / "bridge/yunzai/provenance.json").read_text())
    target = ROOT / "third_party/yunzai/vendor"
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        raise SystemExit("Vendor directory already exists; inspect it before replacing anything")
    with tempfile.TemporaryDirectory(prefix="panel-inputs-", dir=target.parent) as tmp:
        temporary = Path(tmp)
        vendor = temporary / "vendor"
        vendor.mkdir()
        for key, folder in (("yunzai", "runtime"), ("miao", "miao-plugin")):
            info = provenance[key]
            repo = info["repository"].removeprefix("https://github.com/")
            archive = temporary / (key + ".tar.gz")
            verified_download(
                "https://codeload.github.com/" + repo + "/tar.gz/" + info["commit"],
                archive,
                info["archive_sha256"],
            )
            extracted = extract_source(archive, temporary / key)
            shutil.move(str(extracted), vendor / folder)
        node = provenance["node"]
        verified_download(node["source"], vendor / "node.tar.xz", node["sha256"])
        vendor.rename(target)
    print("Pinned panel build inputs verified; upstream license files retained")


if __name__ == "__main__":
    main()
