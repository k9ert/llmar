"""End-to-end tests for the llmar CLI.

Each test points LLMAR_LOCAL_DIR / LLMAR_ARCHIVE_DIR / LLMAR_REGISTRY at temp
dirs and invokes the script as a subprocess, so the module-level config is
re-read fresh each run.
"""

import hashlib
import http.server
import json
import os
import shutil
import socketserver
import subprocess
import tempfile
import threading
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


DUMMY_LLMAR = b"""#!/usr/bin/env python3
__version__ = "v9.9.9"

import sys


def main():
    if "--version" in sys.argv:
        print("llmar v9.9.9")
        sys.exit(0)
    print("dummy llmar v9.9.9")
    sys.exit(0)


if __name__ == "__main__":
    main()
"""


class _RouteHandler(http.server.BaseHTTPRequestHandler):
    routes: dict = {}

    def do_GET(self):  # noqa: N802
        body = self.routes.get(self.path)
        if body is None:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args, **kwargs):
        pass


def make_server(routes: dict):
    handler_cls = type("H", (_RouteHandler,), {"routes": routes})
    server = socketserver.TCPServer(("127.0.0.1", 0), handler_cls)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


class TestUpdate(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="llmar-update-"))
        # working copy of llmar so updates don't clobber the dev script
        self.script = self.tmp / "llmar"
        src = LLMAR.read_text()
        # set a known starting version so same-version skip can be tested
        src = src.replace('__version__ = "dev"', '__version__ = "v0.0.1"', 1)
        self.script.write_text(src)
        os.chmod(self.script, 0o755)

        # default served bytes + checksum
        self.new_bytes = DUMMY_LLMAR
        self.new_sha = hashlib.sha256(self.new_bytes).hexdigest()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, *args, routes=None, check=True):
        if routes is None:
            routes = self._default_routes()
        server, _ = make_server(routes)
        port = server.server_address[1]
        try:
            env = os.environ.copy()
            env["LLMAR_RELEASE_API"] = f"http://127.0.0.1:{port}/api"
            env["LLMAR_RELEASE_DOWNLOAD"] = f"http://127.0.0.1:{port}/dl"
            # isolate other env so module-level config doesn't trip on
            # missing dirs etc.
            env["LLMAR_LOCAL_DIR"] = str(self.tmp / "local")
            env["LLMAR_ARCHIVE_DIR"] = str(self.tmp / "archive")
            env["LLMAR_REGISTRY"] = str(self.tmp / "registry.json")
            r = subprocess.run(
                ["python3", str(self.script), "update", *args],
                capture_output=True, text=True, env=env, timeout=15,
            )
        finally:
            server.shutdown()
            server.server_close()
        if check and r.returncode != 0:
            self.fail(
                f"update {args} failed (exit {r.returncode})\n"
                f"stdout={r.stdout}\nstderr={r.stderr}"
            )
        return r

    def _default_routes(self) -> dict:
        return {
            "/api/releases/latest": json.dumps({"tag_name": "v9.9.9"}).encode(),
            "/api/releases": json.dumps(
                [{"tag_name": "v9.9.9-pre"}, {"tag_name": "v9.9.9"}]
            ).encode(),
            "/dl/v9.9.9/llmar": self.new_bytes,
            "/dl/v9.9.9/llmar.sha256": f"{self.new_sha}  llmar\n".encode(),
            "/dl/v9.9.9-pre/llmar": self.new_bytes,
            "/dl/v9.9.9-pre/llmar.sha256": f"{self.new_sha}  llmar\n".encode(),
        }

    def test_update_to_latest(self):
        r = self._run()
        self.assertIn("v0.0.1 -> v9.9.9", r.stdout)
        self.assertEqual(self.script.read_bytes(), self.new_bytes)
        # the replaced script reports the new version
        v = subprocess.run(
            ["python3", str(self.script), "--version"],
            capture_output=True, text=True,
        )
        self.assertIn("v9.9.9", v.stdout)

    def test_update_explicit_tag(self):
        r = self._run("v9.9.9")
        self.assertIn("v9.9.9", r.stdout)
        self.assertEqual(self.script.read_bytes(), self.new_bytes)

    def test_update_pre(self):
        r = self._run("--pre")
        self.assertIn("v9.9.9-pre", r.stdout)
        self.assertEqual(self.script.read_bytes(), self.new_bytes)

    def test_same_version_no_op(self):
        r = self._run("v0.0.1")
        self.assertIn("already on v0.0.1", r.stdout)
        # script untouched
        self.assertIn(b'__version__ = "v0.0.1"', self.script.read_bytes())

    def test_force_reinstalls_same_version(self):
        # serve v0.0.1 with matching script
        same_bytes = DUMMY_LLMAR.replace(b'v9.9.9', b'v0.0.1')
        same_sha = hashlib.sha256(same_bytes).hexdigest()
        routes = {
            "/dl/v0.0.1/llmar": same_bytes,
            "/dl/v0.0.1/llmar.sha256": f"{same_sha}  llmar\n".encode(),
        }
        r = self._run("v0.0.1", "--force", routes=routes)
        self.assertIn("v0.0.1 -> v0.0.1", r.stdout)
        self.assertEqual(self.script.read_bytes(), same_bytes)

    def test_checksum_mismatch_refuses(self):
        original = self.script.read_bytes()
        bad_routes = self._default_routes()
        bad_routes["/dl/v9.9.9/llmar.sha256"] = b"0" * 64 + b"  llmar\n"
        r = self._run(routes=bad_routes, check=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("checksum mismatch", r.stderr)
        self.assertEqual(self.script.read_bytes(), original)

    def test_unknown_tag_refuses(self):
        original = self.script.read_bytes()
        # default routes have nothing for v1.2.3
        r = self._run("v1.2.3", check=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(self.script.read_bytes(), original)

    def test_sanity_check_rejects_non_python(self):
        original = self.script.read_bytes()
        bad = b"not a python script\n"
        bad_sha = hashlib.sha256(bad).hexdigest()
        routes = {
            "/api/releases/latest": json.dumps({"tag_name": "v9.9.9"}).encode(),
            "/dl/v9.9.9/llmar": bad,
            "/dl/v9.9.9/llmar.sha256": f"{bad_sha}  llmar\n".encode(),
        }
        r = self._run(routes=routes, check=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("does not look like the llmar script", r.stderr)
        self.assertEqual(self.script.read_bytes(), original)


class TestVersion(Base):
    def test_version_flag(self):
        r = self.run_cli("--version")
        self.assertIn("llmar", r.stdout)


class TestCompletion(Base):
    def test_bash_emits_script(self):
        r = self.run_cli("completion", "bash")
        self.assertIn("_llmar_complete", r.stdout)
        self.assertIn("complete -F _llmar_complete llmar", r.stdout)

    def test_bash_script_syntax_valid(self):
        r = self.run_cli("completion", "bash")
        script = self.tmp / "llmar.bash"
        script.write_text(r.stdout)
        check = subprocess.run(
            ["bash", "-n", str(script)],
            capture_output=True, text=True,
        )
        self.assertEqual(check.returncode, 0,
                         f"bash -n failed: {check.stderr}")

    def test_zsh_emits_script(self):
        r = self.run_cli("completion", "zsh")
        self.assertIn("compdef _llmar llmar", r.stdout)
        self.assertIn("_llmar()", r.stdout)

    def test_zsh_script_syntax_valid(self):
        if not shutil.which("zsh"):
            self.skipTest("zsh not available")
        r = self.run_cli("completion", "zsh")
        script = self.tmp / "llmar.zsh"
        script.write_text(r.stdout)
        check = subprocess.run(
            ["zsh", "-n", str(script)],
            capture_output=True, text=True,
        )
        self.assertEqual(check.returncode, 0,
                         f"zsh -n failed: {check.stderr}")

    def test_complete_models_empty(self):
        r = self.run_cli("_complete-models")
        self.assertEqual(r.stdout.strip(), "")

    def test_complete_models_lists_local_and_archive(self):
        make_model(self.local, "pub/local-only")
        make_model(self.archive, "pub/archive-only")
        make_model(self.local, "pub/both")
        make_model(self.archive, "pub/both")
        r = self.run_cli("_complete-models")
        lines = r.stdout.strip().splitlines()
        # deduped + sorted
        self.assertEqual(lines, sorted(set(lines)))
        self.assertIn("pub/local-only", lines)
        self.assertIn("pub/archive-only", lines)
        self.assertIn("pub/both", lines)


if __name__ == "__main__":
    unittest.main()
