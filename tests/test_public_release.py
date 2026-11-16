"""Public release safety checks; temporary private content must never be shipped."""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
import zipfile

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"scripts"))
from release_files import audit,inventory,payload
from package_release import write_zip


class PublicReleaseTests(unittest.TestCase):
    def copy_source(self,destination):
        for name,source in inventory(ROOT).items():
            path=destination/name
            path.parent.mkdir(parents=True,exist_ok=True)
            path.write_bytes(source.read_bytes())

    def test_source_inventory_is_clean_and_preserves_validated_runtime(self):
        result=audit(ROOT)
        self.assertTrue(result["ok"],result)
        self.assertGreater(result["runtime_files"],0)

    def test_private_home_path_is_detected_without_exposing_its_value(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);self.copy_source(root)
            personal="C:"+chr(92)+"Users"+chr(92)+"example"+chr(92)+"recording"
            with (root/"app.py").open("a",encoding="utf-8") as stream:
                stream.write("\n# "+personal+"\n")
            result=audit(root)
            self.assertFalse(result["ok"])
            self.assertIn({"file":"app.py","rule":"personal_home_path"},result["findings"])
            self.assertNotIn(personal,json.dumps(result))

    def test_unlisted_recordings_credentials_and_generated_content_are_excluded(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);self.copy_source(root)
            expected=set(inventory(root))
            for name in (".env","recording.mp4","private.align.json","evidence/old.json",
                         "models/model.onnx","examples/generated/board.mp4"):
                path=root/name;path.parent.mkdir(parents=True,exist_ok=True)
                path.write_bytes(b"must not ship")
            self.assertEqual(set(inventory(root)),expected)
            output=root/"bundle.zip"
            write_zip(output,{n:payload(p) for n,p in inventory(root).items()},"source/")
            with zipfile.ZipFile(output) as archive:
                self.assertEqual(set(archive.namelist()),
                                 {"source/"+name for name in expected}|{"source/MANIFEST.sha256"})

    def test_runtime_edit_cannot_silently_reuse_validated_identity(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);self.copy_source(root)
            name=next(iter(json.loads((root/"docs/RUNTIME_IDENTITY.json").read_text())["files_sha256"]))
            with (root/name).open("ab") as stream:stream.write(b"\n# changed runtime\n")
            self.assertIn({"file":name,"rule":"validated_runtime_changed"},audit(root)["findings"])

    def test_text_payload_has_stable_lf_and_windows_launcher_crlf(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            text=root/"example.py";text.write_bytes(b"one\r\ntwo\n")
            self.assertEqual(payload(text),b"one\ntwo\n")
            text=root/"launch.cmd";text.write_bytes(b"one\r\ntwo\n")
            self.assertEqual(payload(text),b"one\r\ntwo\r\n")

    def test_zip_is_repeatable_and_each_manifest_digest_matches_payload(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);entries={"b.txt":b"second\n","a.txt":b"first\n"}
            first=write_zip(root/"first.zip",entries,"source/")
            second=write_zip(root/"second.zip",dict(reversed(list(entries.items()))),"source/")
            self.assertEqual(first.read_bytes(),second.read_bytes())
            with zipfile.ZipFile(first) as archive:
                for line in archive.read("source/MANIFEST.sha256").decode().splitlines():
                    digest,name=line.split("  ",1)
                    self.assertEqual(hashlib.sha256(archive.read("source/"+name)).hexdigest(),digest)
                self.assertTrue(all(info.date_time==(1980,1,1,0,0,0) for info in archive.infolist()))

if __name__=="__main__":unittest.main()
