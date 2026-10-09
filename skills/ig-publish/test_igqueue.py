#!/usr/bin/env python3
"""
Tests for igqueue.py. Everything runs in a temporary folder: no network, no
real queue, no real media. The "media" here are a few placeholder bytes with a
video extension, which is all the extension check looks at.

  python3 test_igqueue.py
"""

import ast
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

import igqueue

WHEN = "2099-06-15 19:00"
TZ = "Europe/Moscow"


class QueueCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.root = self.tmp / "queue"
        self.src = self.tmp / "src"
        self.src.mkdir()
        self.caption = self.src / "caption.txt"
        self.caption.write_text("Проверка слуха.\n\n#один #два #три\n", encoding="utf-8")
        self.video = self.src / "clip.mp4"
        self.video.write_bytes(b"placeholder bytes, not a video")

    def tearDown(self):
        self._tmp.cleanup()

    def run_cli(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            code = igqueue.main(["--queue", str(self.root)] + list(argv))
        return code, out.getvalue()

    def new(self, pid="post-one", media=True, caption=True, when=WHEN, tz=TZ, ptype="reel"):
        argv = ["new", "--id", pid, "--type", ptype, "--when", when, "--tz", tz]
        if caption:
            argv += ["--caption-file", str(self.caption)]
        if media:
            argv += ["--media", str(self.video)]
        return self.run_cli(*argv)

    def post(self, pid="post-one"):
        return json.loads((self.root / pid / "post.json").read_text(encoding="utf-8"))

    def edit(self, pid="post-one", **fields):
        f = self.root / pid / "post.json"
        data = json.loads(f.read_text(encoding="utf-8"))
        data.update(fields)
        f.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    def approve_by_hand(self, pid="post-one", approval_id=None):
        """Stand-in for the approval step that does not exist yet."""
        d = self.root / pid
        post = self.post(pid)
        self.edit(pid, status="approved",
                  approval={"id": approval_id or pid, "approved_at": "2099-01-01T00:00:00Z",
                            "fingerprint": igqueue.fingerprint(d, post)})


class TestCreate(QueueCase):
    def test_new_creates_folder_with_copies(self):
        code, _ = self.new()
        self.assertEqual(code, 0)
        d = self.root / "post-one"
        self.assertTrue((d / "post.json").is_file())
        self.assertTrue((d / "caption.txt").is_file())
        self.assertTrue((d / "clip.mp4").is_file())
        self.assertTrue(self.video.is_file(), "the source file must stay in place")
        post = self.post()
        self.assertEqual(post["status"], "draft")
        self.assertEqual(post["scheduled_at"], "2099-06-15T19:00")
        self.assertIsNone(post["approval"])

    def test_duplicate_id_is_refused_and_nothing_is_overwritten(self):
        self.new()
        before = (self.root / "post-one" / "post.json").read_bytes()
        code, out = self.new()
        self.assertEqual(code, 2)
        self.assertIn("уже занят", out)
        self.assertEqual(before, (self.root / "post-one" / "post.json").read_bytes())

    def test_bad_id_is_refused(self):
        for bad in ("Post-One", "ab", "../escape", "with space"):
            code, _ = self.new(pid=bad)
            self.assertEqual(code, 2, bad)

    def test_missing_source_media_creates_nothing(self):
        self.video.unlink()
        code, out = self.new()
        self.assertEqual(code, 2)
        self.assertIn("не найден", out)
        self.assertFalse((self.root / "post-one").exists())

    def test_bad_date_or_zone_creates_nothing(self):
        for when, tz in (("2099-02-30 19:00", TZ), ("завтра", TZ), ("2099-06-15 25:00", TZ),
                         ("2099-06-15 19:00", "Mars/Olympus"), ("2099-06-15T19:00+03:00", TZ)):
            code, _ = self.new(when=when, tz=tz)
            self.assertEqual(code, 2, (when, tz))
        self.assertFalse((self.root / "post-one").exists())


class TestValidation(QueueCase):
    def test_valid_draft_becomes_validated(self):
        self.new()
        code, out = self.run_cli("validate", "post-one")
        self.assertEqual(code, 0, out)
        post = self.post()
        self.assertEqual(post["status"], "validated")
        self.assertEqual(len(post["validated"]["fingerprint"]["caption_sha256"]), 64)

    def test_draft_without_media_fails(self):
        self.new(media=False)
        code, out = self.run_cli("validate", "post-one")
        self.assertEqual(code, 1)
        self.assertIn("файл не указан", out)
        self.assertEqual(self.post()["status"], "draft")

    def test_listed_media_missing_on_disk(self):
        self.new()
        (self.root / "post-one" / "clip.mp4").unlink()
        code, out = self.run_cli("content", "post-one")
        self.assertEqual(code, 1)
        self.assertIn("нет файла clip.mp4", out)

    def test_wrong_extension_and_empty_file(self):
        self.new()
        d = self.root / "post-one"
        (d / "clip.avi").write_bytes(b"x")
        self.edit(media=["clip.avi"])
        code, out = self.run_cli("content", "post-one")
        self.assertEqual(code, 1)
        self.assertIn("расширение не подходит", out)
        (d / "empty.mov").write_bytes(b"")
        self.edit(media=["empty.mov"])
        code, out = self.run_cli("content", "post-one")
        self.assertEqual(code, 1)
        self.assertIn("пустой", out)

    def test_mov_is_accepted_for_reels(self):
        self.new()
        (self.root / "post-one" / "clip.MOV").write_bytes(b"x")
        self.edit(media=["clip.MOV"])
        code, out = self.run_cli("content", "post-one")
        self.assertEqual(code, 0, out)

    def test_media_name_cannot_leave_the_folder(self):
        self.new()
        self.edit(media=["../../src/clip.mp4"])
        code, out = self.run_cli("check", "post-one")
        self.assertEqual(code, 1)
        self.assertIn("не подходит", out)

    def test_caption_rules(self):
        self.new()
        cap = self.root / "post-one" / "caption.txt"
        cases = (("   \n", 1, "пустая"),
                 ("я" * 2201, 1, "лимит 2200"),
                 ("я" * 2200, 0, ""),
                 ("Привет {{имя}}", 1, "шаблон"),
                 (" ".join("#t%d" % i for i in range(31)), 1, "хэштегов"),
                 (" ".join("@u%d" % i for i in range(21)), 1, "упоминаний"))
        for text, want, needle in cases:
            cap.write_text(text, encoding="utf-8")
            code, out = self.run_cli("content", "post-one")
            self.assertEqual(code, want, out)
            self.assertIn(needle, out)

    def test_bad_dates_in_post_json(self):
        self.new()
        for value in ("2099-13-01T10:00", "2099-06-15 19:00", "2099-06-15T19:00Z", "", None, 20990615):
            self.edit(scheduled_at=value)
            code, out = self.run_cli("check", "post-one")
            self.assertEqual(code, 1, repr(value))
            self.assertIn("scheduled_at", out)

    def test_bad_timezone(self):
        self.new()
        for value in ("MSK+3", "", None, "Europe/Nowhere"):
            self.edit(timezone=value)
            code, out = self.run_cli("check", "post-one")
            self.assertEqual(code, 1, repr(value))
            self.assertIn("timezone", out)

    def test_time_that_does_not_exist(self):
        self.new()
        self.edit(scheduled_at="2026-03-29T02:30", timezone="Europe/Berlin")
        code, out = self.run_cli("check", "post-one")
        self.assertEqual(code, 1)
        self.assertIn("не существует", out)

    def test_past_time_is_a_warning(self):
        self.new()
        self.edit(scheduled_at="2020-01-01T10:00")
        code, out = self.run_cli("check", "post-one")
        self.assertEqual(code, 0)
        self.assertIn("уже прошло", out)

    def test_id_must_match_folder_and_be_unique(self):
        self.new("post-one")
        self.new("post-two")
        self.edit("post-two", id="post-one")
        code, out = self.run_cli("check", "post-two")
        self.assertEqual(code, 1)
        self.assertIn("не совпадает", out)
        (self.root / "post-two").rename(self.root / "copy-of-one")
        code, out = self.run_cli("check", "post-one")
        self.assertEqual(code, 1)
        self.assertIn("уже занят", out)

    def test_unknown_status_and_broken_json(self):
        self.new()
        self.edit(status="live")
        code, out = self.run_cli("check", "post-one")
        self.assertEqual(code, 1)
        self.assertIn("не статус", out)
        (self.root / "post-one" / "post.json").write_text("{not json", encoding="utf-8")
        code, out = self.run_cli("validate", "post-one")
        self.assertEqual(code, 1)
        self.assertIn("не читается", out)

    def test_final_statuses_are_left_alone(self):
        self.new()
        for status in igqueue.FINAL:
            self.edit(status=status)
            before = (self.root / "post-one" / "post.json").read_bytes()
            self.run_cli("validate", "post-one")
            self.assertEqual(before, (self.root / "post-one" / "post.json").read_bytes(), status)


class TestHashes(QueueCase):
    def test_changed_caption_drops_validation_until_rerun(self):
        self.new()
        self.run_cli("validate", "post-one")
        old = self.post()["validated"]["fingerprint"]["caption_sha256"]
        (self.root / "post-one" / "caption.txt").write_text("Другой текст", encoding="utf-8")
        _, out = self.run_cli("list")
        self.assertIn("изменено после проверки", out)
        _, out = self.run_cli("dry-run", "post-one")
        self.assertIn("статус не зафиксирован", out)
        self.run_cli("validate", "post-one")
        self.assertNotEqual(old, self.post()["validated"]["fingerprint"]["caption_sha256"])

    def test_approval_survives_when_nothing_changed(self):
        self.new()
        self.run_cli("validate", "post-one")
        self.approve_by_hand()
        code, out = self.run_cli("validate", "post-one")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.post()["status"], "approved")
        _, out = self.run_cli("dry-run", "post-one")
        self.assertIn("ПОДТВЕРЖДЕНО", out)

    def test_any_change_after_approval_voids_it(self):
        changes = {
            "caption": lambda d: (d / "caption.txt").write_text("Правка после подтверждения", encoding="utf-8"),
            "media": lambda d: (d / "clip.mp4").write_bytes(b"different bytes"),
            "time": lambda d: self.edit(d.name, scheduled_at="2099-06-16T19:00"),
            "zone": lambda d: self.edit(d.name, timezone="Europe/Berlin"),
        }
        for name, change in changes.items():
            pid = "post-" + name
            self.new(pid)
            self.run_cli("validate", pid)
            self.approve_by_hand(pid)
            change(self.root / pid)
            _, out = self.run_cli("dry-run", pid)
            self.assertIn("НЕДЕЙСТВИТЕЛЬНО", out, name)
            self.assertNotIn("ИТОГ: ПОДТВЕРЖДЕНО", out, name)
            _, out = self.run_cli("validate", pid)
            self.assertIn("подтверждение аннулировано", out, name)
            post = self.post(pid)
            self.assertIsNone(post["approval"], name)
            self.assertEqual(post["status"], "validated", name)
            self.assertIn("approval_voided", [h["event"] for h in post["history"]], name)

    def test_approval_for_another_id_does_not_count(self):
        self.new()
        self.run_cli("validate", "post-one")
        self.approve_by_hand(approval_id="post-other")
        _, out = self.run_cli("dry-run", "post-one")
        self.assertIn("НЕДЕЙСТВИТЕЛЬНО", out)
        self.run_cli("validate", "post-one")
        self.assertEqual(self.post()["status"], "validated")

    def test_approval_on_a_broken_post_is_voided(self):
        self.new()
        self.run_cli("validate", "post-one")
        self.approve_by_hand()
        (self.root / "post-one" / "clip.mp4").unlink()
        code, _ = self.run_cli("validate", "post-one")
        self.assertEqual(code, 1)
        post = self.post()
        self.assertIsNone(post["approval"])
        self.assertEqual(post["status"], "draft")


class TestReadOnly(QueueCase):
    def test_dry_run_and_list_write_nothing(self):
        self.new()
        self.run_cli("validate", "post-one")
        d = self.root / "post-one"
        before = {p.name: p.read_bytes() for p in d.iterdir()}
        code, out = self.run_cli("dry-run", "post-one")
        self.assertEqual(code, 0, out)
        self.assertIn("сетевых действий: 0", out)
        self.assertIn("ГОТОВО К ПОДТВЕРЖДЕНИЮ", out)
        self.run_cli("list")
        self.run_cli("check", "post-one")
        self.run_cli("content", "post-one")
        self.assertEqual(before, {p.name: p.read_bytes() for p in d.iterdir()})

    def test_dry_run_without_media_is_not_ready(self):
        self.new(media=False)
        code, out = self.run_cli("dry-run", "post-one")
        self.assertEqual(code, 1)
        self.assertIn("НЕ ГОТОВО", out)

    def test_unknown_post(self):
        code, out = self.run_cli("dry-run", "no-such-post")
        self.assertEqual(code, 2)
        self.assertIn("нет публикации", out)

    def test_empty_queue(self):
        code, out = self.run_cli("list")
        self.assertEqual(code, 0)
        self.assertIn("Очередь пуста", out)


class TestNoNetwork(unittest.TestCase):
    """The script must not be able to reach a network or start another program."""

    FORBIDDEN = {"socket", "ssl", "http", "urllib", "requests", "ftplib", "smtplib", "telnetlib",
                 "xmlrpc", "asyncio", "subprocess", "multiprocessing", "webbrowser", "ctypes"}

    def test_imports_are_local_only(self):
        tree = ast.parse(Path(igqueue.__file__).read_text(encoding="utf-8"))
        used = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                used.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom):
                used.add((node.module or "").split(".")[0])
        self.assertEqual(used & self.FORBIDDEN, set())
        self.assertEqual(used, {"argparse", "hashlib", "json", "os", "re", "shutil", "sys",
                                "datetime", "pathlib", "zoneinfo"})

    def test_no_shell_or_dynamic_code(self):
        tree = ast.parse(Path(igqueue.__file__).read_text(encoding="utf-8"))
        calls = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                f = node.func
                calls.add(f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", ""))
        self.assertEqual(calls & {"system", "popen", "exec", "eval", "__import__", "spawnv", "execv"}, set())

    def test_no_live_flag(self):
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            igqueue.main(["dry-run", "post-one", "--live"])


if __name__ == "__main__":
    unittest.main()
