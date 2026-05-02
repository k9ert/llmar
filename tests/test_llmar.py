"""End-to-end tests for the llmar CLI.

Each test points LLMAR_LOCAL_DIR / LLMAR_ARCHIVE_DIR / LLMAR_REGISTRY at temp
dirs and invokes the script as a subprocess, so the module-level config is
re-read fresh each run.
"""

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

LLMAR = Path(__file__).resolve().parent.parent / "llmar"


def make_model(root: Path, model_id: str, weights_size: int = 1024) -> None:
    d = root / model_id
    d.mkdir(parents=True)
    (d / "config.json").write_text("{}")
    (d / "weights.bin").write_bytes(b"x" * weights_size)


def tree_size(path: Path) -> int:
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file())


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="llmar-test-"))
        self.local = self.tmp / "local"
        self.archive = self.tmp / "archive"
        self.registry = self.tmp / "registry.json"
        self.local.mkdir()
        self.archive.mkdir()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_cli(self, *args, stdin: str = "", check: bool = True):
        env = os.environ.copy()
        env["LLMAR_LOCAL_DIR"] = str(self.local)
        env["LLMAR_ARCHIVE_DIR"] = str(self.archive)
        env["LLMAR_REGISTRY"] = str(self.registry)
        result = subprocess.run(
            ["python3", str(LLMAR), *args],
            input=stdin,
            capture_output=True,
            text=True,
            env=env,
        )
        if check and result.returncode != 0:
            self.fail(
                f"llmar {args} failed (exit {result.returncode})\n"
                f"stdout={result.stdout}\nstderr={result.stderr}"
            )
        return result

    def reg(self) -> dict:
        if not self.registry.exists():
            return {"models": {}}
        return json.loads(self.registry.read_text())

    def set_desc(self, model_id: str, text: str = "test model"):
        self.run_cli("desc", model_id, text)


class TestHelp(Base):
    def test_help_runs(self):
        r = self.run_cli("--help")
        self.assertIn("llmar", r.stdout)
        self.assertIn("archive", r.stdout)
        self.assertIn("LLMAR_LOCAL_DIR", r.stdout)


class TestList(Base):
    def test_empty(self):
        r = self.run_cli("list")
        self.assertIn("no models found", r.stdout)

    def test_shows_local_only(self):
        make_model(self.local, "pub/a")
        r = self.run_cli("list")
        self.assertIn("pub/a", r.stdout)
        self.assertIn("L-", r.stdout)

    def test_shows_archive_only(self):
        make_model(self.archive, "pub/b")
        r = self.run_cli("list")
        self.assertIn("pub/b", r.stdout)
        self.assertIn("-A", r.stdout)

    def test_shows_both(self):
        make_model(self.local, "pub/x")
        make_model(self.archive, "pub/x")
        r = self.run_cli("list")
        self.assertIn("LA", r.stdout)


class TestDesc(Base):
    def test_set_and_show(self):
        make_model(self.local, "pub/m")
        self.run_cli("desc", "pub/m", "small test model")
        self.assertEqual(
            self.reg()["models"]["pub/m"]["description"], "small test model"
        )
        r = self.run_cli("desc", "pub/m")
        self.assertIn("small test model", r.stdout)
        self.assertIn("(empty)", r.stdout)  # no journal yet

    def test_empty_description_rejected(self):
        make_model(self.local, "pub/m")
        r = self.run_cli("desc", "pub/m", "   ", check=False)
        self.assertNotEqual(r.returncode, 0)

    def test_unknown_model_errors(self):
        r = self.run_cli("desc", "pub/missing", check=False)
        self.assertNotEqual(r.returncode, 0)


class TestPinUnpin(Base):
    def test_pin_then_unpin(self):
        make_model(self.local, "pub/m")
        self.set_desc("pub/m")
        self.run_cli("pin", "pub/m")
        self.assertTrue(self.reg()["models"]["pub/m"]["pinned"])
        r = self.run_cli("list")
        self.assertIn("* ", r.stdout)
        self.run_cli("unpin", "pub/m")
        self.assertFalse(self.reg()["models"]["pub/m"]["pinned"])

    def test_pin_without_description_aborts(self):
        make_model(self.local, "pub/m")
        # empty stdin -> EOFError in input() -> ensure_description returns False
        r = self.run_cli("pin", "pub/m", check=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertNotIn("pub/m", json.dumps(self.reg()))


class TestLog(Base):
    def test_log_appends(self):
        make_model(self.local, "pub/m")
        self.set_desc("pub/m")
        self.run_cli("log", "pub/m", "first entry")
        self.run_cli("log", "pub/m", "second entry")
        journal = self.reg()["models"]["pub/m"]["journal"]
        self.assertEqual([e["text"] for e in journal], ["first entry", "second entry"])
        # timestamps populated and ordered
        self.assertTrue(all(e.get("ts") for e in journal))
        self.assertLessEqual(journal[0]["ts"], journal[1]["ts"])

    def test_log_shows_in_desc(self):
        make_model(self.local, "pub/m")
        self.set_desc("pub/m")
        self.run_cli("log", "pub/m", "an event happened")
        r = self.run_cli("desc", "pub/m")
        self.assertIn("an event happened", r.stdout)
        self.assertIn("1 entry", r.stdout)

    def test_log_empty_text_rejected(self):
        make_model(self.local, "pub/m")
        self.set_desc("pub/m")
        # no text arg + no EDITOR -> read_from_editor reads stdin; empty -> abort
        env_no_editor = {"EDITOR": ""}
        r = subprocess.run(
            ["python3", str(LLMAR), "log", "pub/m"],
            input="",
            capture_output=True,
            text=True,
            env={
                **os.environ,
                "LLMAR_LOCAL_DIR": str(self.local),
                "LLMAR_ARCHIVE_DIR": str(self.archive),
                "LLMAR_REGISTRY": str(self.registry),
                **env_no_editor,
            },
        )
        self.assertNotEqual(r.returncode, 0)


class TestArchive(Base):
    def test_archive_single_model(self):
        make_model(self.local, "pub/m")
        self.set_desc("pub/m")
        self.run_cli("archive", "pub/m", stdin="y\n")
        self.assertFalse((self.local / "pub/m").exists())
        self.assertTrue((self.archive / "pub/m").exists())

    def test_archive_refuses_pinned(self):
        make_model(self.local, "pub/m")
        self.set_desc("pub/m")
        self.run_cli("pin", "pub/m")
        r = self.run_cli("archive", "pub/m", stdin="y\n", check=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("pinned", r.stderr)
        self.assertTrue((self.local / "pub/m").exists())

    def test_archive_force_pinned(self):
        make_model(self.local, "pub/m")
        self.set_desc("pub/m")
        self.run_cli("pin", "pub/m")
        self.run_cli("archive", "pub/m", "--force", stdin="y\n")
        self.assertFalse((self.local / "pub/m").exists())
        self.assertTrue((self.archive / "pub/m").exists())

    def test_archive_all_unpinned_skips_pinned(self):
        make_model(self.local, "pub/keep")
        make_model(self.local, "pub/move")
        self.set_desc("pub/keep")
        self.set_desc("pub/move")
        self.run_cli("pin", "pub/keep")
        self.run_cli("archive", stdin="y\n")
        self.assertTrue((self.local / "pub/keep").exists())
        self.assertFalse((self.local / "pub/move").exists())
        self.assertTrue((self.archive / "pub/move").exists())

    def test_archive_decline_exits_zero(self):
        make_model(self.local, "pub/m")
        self.set_desc("pub/m")
        r = self.run_cli("archive", "pub/m", stdin="n\n")
        self.assertEqual(r.returncode, 0)
        self.assertTrue((self.local / "pub/m").exists())

    def test_archive_existing_size_match_drops_local(self):
        make_model(self.local, "pub/m", weights_size=4096)
        make_model(self.archive, "pub/m", weights_size=4096)
        self.set_desc("pub/m")
        self.run_cli("archive", "pub/m", stdin="y\n")
        self.assertFalse((self.local / "pub/m").exists())
        self.assertTrue((self.archive / "pub/m").exists())

    def test_archive_existing_size_mismatch_refuses(self):
        make_model(self.local, "pub/m", weights_size=4096)
        make_model(self.archive, "pub/m", weights_size=1024)
        self.set_desc("pub/m")
        r = self.run_cli("archive", "pub/m", stdin="y\n", check=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("size differs", r.stderr)
        self.assertTrue((self.local / "pub/m").exists())
        # archive copy left untouched
        self.assertEqual(tree_size(self.archive / "pub/m"), 1024 + 2)

    def test_archive_unknown_model_errors(self):
        r = self.run_cli("archive", "pub/nope", check=False)
        self.assertNotEqual(r.returncode, 0)


class TestBackup(Base):
    def test_backup_keeps_local(self):
        make_model(self.local, "pub/m")
        self.set_desc("pub/m")
        self.run_cli("backup", "pub/m", stdin="y\n")
        self.assertTrue((self.local / "pub/m").exists())
        self.assertTrue((self.archive / "pub/m").exists())

    def test_backup_refuses_existing_without_force(self):
        make_model(self.local, "pub/m")
        make_model(self.archive, "pub/m")
        self.set_desc("pub/m")
        r = self.run_cli("backup", "pub/m", stdin="y\n", check=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("already in archive", r.stderr)

    def test_backup_force_overwrites(self):
        make_model(self.local, "pub/m", weights_size=4096)
        make_model(self.archive, "pub/m", weights_size=512)
        self.set_desc("pub/m")
        self.run_cli("backup", "pub/m", "--force", stdin="y\n")
        self.assertEqual(
            tree_size(self.archive / "pub/m"),
            tree_size(self.local / "pub/m"),
        )

    def test_backup_all_skips_already_archived(self):
        make_model(self.local, "pub/already")
        make_model(self.archive, "pub/already")
        make_model(self.local, "pub/fresh")
        self.set_desc("pub/already")
        self.set_desc("pub/fresh")
        r = self.run_cli("backup", stdin="y\n")
        # only pub/fresh should be in the candidate list
        self.assertIn("pub/fresh", r.stdout)
        self.assertTrue((self.archive / "pub/fresh").exists())

    def test_backup_decline_exits_zero(self):
        make_model(self.local, "pub/m")
        self.set_desc("pub/m")
        r = self.run_cli("backup", "pub/m", stdin="n\n")
        self.assertEqual(r.returncode, 0)
        self.assertFalse((self.archive / "pub/m").exists())


class TestRestore(Base):
    def test_restore_from_archive(self):
        make_model(self.archive, "pub/m")
        self.set_desc("pub/m")
        self.run_cli("restore", "pub/m")
        self.assertTrue((self.local / "pub/m").exists())
        self.assertTrue((self.archive / "pub/m").exists())

    def test_restore_refuses_if_local_exists(self):
        make_model(self.local, "pub/m")
        make_model(self.archive, "pub/m")
        self.set_desc("pub/m")
        r = self.run_cli("restore", "pub/m", check=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("already exists locally", r.stderr)

    def test_restore_unknown_model_errors(self):
        r = self.run_cli("restore", "pub/nope", check=False)
        self.assertNotEqual(r.returncode, 0)


class TestRoundTrip(Base):
    def test_archive_then_restore(self):
        make_model(self.local, "pub/m", weights_size=2048)
        self.set_desc("pub/m", "a model with history")
        self.run_cli("log", "pub/m", "trained on synthetic data")
        original_size = tree_size(self.local / "pub/m")

        self.run_cli("archive", "pub/m", stdin="y\n")
        self.assertFalse((self.local / "pub/m").exists())

        self.run_cli("restore", "pub/m")
        self.assertEqual(tree_size(self.local / "pub/m"), original_size)

        # registry preserved across the round trip
        entry = self.reg()["models"]["pub/m"]
        self.assertEqual(entry["description"], "a model with history")
        self.assertEqual(len(entry["journal"]), 1)


if __name__ == "__main__":
    unittest.main()
