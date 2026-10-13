"""Observable release boundaries: secrets cannot stage silently; upstream paths stay contained."""

import hashlib
import importlib.util
import io
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


audit = load("release_audit", ROOT / "scripts/audit_public.py")
panel = load("panel_inputs", ROOT / "tools/provision/fetch_panel.py")


class ReleaseTests(unittest.TestCase):
    def test_tracked_runtime_and_secret_files_are_rejected_without_content(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(audit, "ROOT", Path(tmp)):
            paths = [Path("secrets/token.txt"), Path("state/chat.sqlite3"), Path("config/.env")]
            for path in paths:
                target = Path(tmp) / path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text("private fixture value")
            report = audit.audit(paths)
            self.assertFalse(report["passed"])
            self.assertEqual(len(report["findings"]), len(paths))
            self.assertNotIn("private fixture value", str(report))

    def test_credential_shapes_are_rejected_and_exact_fixture_allowlist_is_scoped(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(audit, "ROOT", Path(tmp)):
            path = Path(tmp) / "sample.py"
            path.write_text("key = '" + "ghp_" + "x" * 32 + "'\n")
            self.assertFalse(audit.audit([Path("sample.py")])["passed"])
            path.write_text("key = '" + "sk-" + "0123456789abcdef" + "'\n")
            self.assertFalse(audit.audit([Path("sample.py")])["passed"])
            (Path(tmp) / "tests").mkdir()
            path.rename(Path(tmp) / "tests/fixture.py")
            self.assertTrue(audit.audit([Path("tests/fixture.py")])["passed"])

    def test_model_training_source_allowed_but_weights_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(audit, "ROOT", Path(tmp)):
            folder = Path(tmp) / "tools/models"
            folder.mkdir(parents=True)
            (folder / "train.py").write_text("pass\n")
            (folder / "weights.pt").write_bytes(b"fixture")
            self.assertTrue(audit.audit([Path("tools/models/train.py")])["passed"])
            self.assertFalse(audit.audit([Path("tools/models/weights.pt")])["passed"])

    def test_checksum_failure_never_leaves_installable_input(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "archive.gz"
            with patch.object(panel.urllib.request, "urlopen", return_value=io.BytesIO(b"fixture")):
                with self.assertRaises(ValueError):
                    panel.verified_download("https://example.com/archive", target, "0" * 64)
            self.assertFalse(target.exists())
            with patch.object(panel.urllib.request, "urlopen", return_value=io.BytesIO(b"fixture")):
                panel.verified_download(
                    "https://example.com/archive", target, hashlib.sha256(b"fixture").hexdigest()
                )
            self.assertEqual(target.read_bytes(), b"fixture")

    def test_archive_traversal_and_links_are_rejected_before_extraction(self):
        for name, kind in (("../outside", tarfile.REGTYPE), ("root/link", tarfile.SYMTYPE)):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                archive = Path(tmp) / "source.tar.gz"
                with tarfile.open(archive, "w:gz") as stream:
                    item = tarfile.TarInfo(name)
                    item.type = kind
                    item.linkname = "../../outside" if kind == tarfile.SYMTYPE else ""
                    stream.addfile(item)
                with self.assertRaises(ValueError):
                    panel.extract_source(archive, Path(tmp) / "extract")
                self.assertFalse((Path(tmp) / "extract").exists())

    def test_valid_archive_preserves_upstream_license(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / "source.tar.gz"
            with tarfile.open(archive, "w:gz") as stream:
                text = b"upstream license fixture"
                item = tarfile.TarInfo("root/LICENSE")
                item.size = len(text)
                stream.addfile(item, io.BytesIO(text))
            directory = panel.extract_source(archive, Path(tmp) / "extract")
            self.assertEqual((directory / "LICENSE").read_bytes(), text)
