"""memory 模块的自动化测试。

覆盖：总索引 / 按天索引 / 每条记忆一个文件的布局、按天拆分的索引、小时摘要文件、
从 SQLite 完整重建（删掉目录也不丢数据）、文件名清洗与路径穿越防护。

全部在临时目录里跑，**不联网**、不碰正式数据库。

运行方式（仓库根目录）::

    python -m unittest discover -s tests -t . -v
"""

from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import memory as mem_mod  # noqa: E402
from config import load_config  # noqa: E402
from memory import INDEX_FILENAME, MEMORY_DIRNAME, MemoryStore, note_filename, slugify  # noqa: E402
from storage import Storage, StorageError  # noqa: E402

_JST = timezone(timedelta(hours=9))
_INITIAL = 50000


class MemoryTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.db = self.root / "data" / "fxsim.sqlite3"
        self.memory_root = self.root / "memory"
        self.cfg = load_config()
        self.storage = Storage(self.db, tz=_JST)
        self.storage.initialize_account(_INITIAL, opened_at=datetime(2026, 10, 10, 8, 0, tzinfo=_JST))
        self.store = MemoryStore(self.cfg, storage=self.storage, root=self.memory_root)

    def add_note(self, kind: str, content: str, *, at: datetime | None = None, **kwargs):
        note, _ = self.storage.record_note(
            kind, content, occurred_at=at or datetime(2026, 10, 10, 14, 5, tzinfo=_JST),
            **kwargs
        )
        return note


# --------------------------------------------------------------------------- #
# 1. 文件名工具
# --------------------------------------------------------------------------- #


class TestFilenames(unittest.TestCase):
    def test_slug_keeps_cjk(self) -> None:
        self.assertEqual(slugify("别在休市时开仓"), "别在休市时开仓")

    def test_slug_replaces_illegal_chars(self) -> None:
        self.assertEqual(slugify("a/b\\c:d*e?f\"g<h>i|j"), "a-b-c-d-e-f-g-h-i-j")

    def test_slug_folds_whitespace(self) -> None:
        self.assertEqual(slugify("  a   b\tc\nd  "), "a-b-c-d")

    def test_slug_strips_control_chars(self) -> None:
        self.assertEqual(slugify("a\x00b\x1fc"), "a-b-c")

    def test_slug_is_bounded(self) -> None:
        self.assertLessEqual(len(slugify("x" * 500)), mem_mod._MAX_SLUG_CHARS)

    def test_slug_of_garbage_can_be_empty(self) -> None:
        self.assertEqual(slugify("///"), "")
        self.assertEqual(slugify(""), "")

    def test_slug_does_not_end_with_dot_or_dash(self) -> None:
        self.assertEqual(slugify("hello..."), "hello")
        self.assertEqual(slugify("--x--"), "x")


# --------------------------------------------------------------------------- #
# 2. 文件布局
# --------------------------------------------------------------------------- #


class TestLayout(MemoryTestCase):
    def test_note_file_is_named_with_number_kind_and_slug(self) -> None:
        note = self.add_note("LESSON", "别在休市时开仓")
        self.assertEqual(note_filename(note), "0001-LESSON-别在休市时开仓.md")

    def test_note_file_without_slug(self) -> None:
        note = self.add_note("LESSON", "///")
        self.assertEqual(note_filename(note), "0001-LESSON.md")

    def test_sync_note_writes_one_file_per_note(self) -> None:
        for index in range(3):
            self.store.sync_note(self.add_note("LESSON", f"第 {index} 条"))
        files = self.store.list_note_files()
        self.assertEqual(len(files), 3, "一条笔记一个文件，不合并成一个大文件")
        self.assertTrue(all(f.parent.name == "2026-10-10" for f in files))

    def test_note_file_contains_metadata_and_body(self) -> None:
        note = self.add_note("PLAN", "157.5 是支撑", tags=["level"], decision_id="dec-9")
        path = self.store.sync_note(note)
        text = path.read_text(encoding="utf-8")
        self.assertIn("# 记忆 #1", text)
        self.assertIn("PLAN", text)
        self.assertIn("level", text)
        self.assertIn("dec-9", text)
        self.assertIn("157.5 是支撑", text)

    def test_day_index_is_split_by_day(self) -> None:
        self.store.sync_note(self.add_note(
            "LESSON", "第一天", at=datetime(2026, 10, 10, 14, 0, tzinfo=_JST)))
        self.store.sync_note(self.add_note(
            "LESSON", "第二天", at=datetime(2026, 10, 11, 9, 0, tzinfo=_JST)))
        first = self.memory_root / "index" / "2026-10-10.md"
        second = self.memory_root / "index" / "2026-10-11.md"
        self.assertTrue(first.is_file())
        self.assertTrue(second.is_file())
        self.assertIn("第一天", first.read_text(encoding="utf-8"))
        self.assertNotIn("第二天", first.read_text(encoding="utf-8"))
        self.assertIn("第二天", second.read_text(encoding="utf-8"))

    def test_master_index_lists_days(self) -> None:
        self.store.sync_note(self.add_note(
            "LESSON", "甲", at=datetime(2026, 10, 10, 14, 0, tzinfo=_JST)))
        self.store.sync_note(self.add_note(
            "LESSON", "乙", at=datetime(2026, 10, 11, 9, 0, tzinfo=_JST)))
        master = (self.memory_root / INDEX_FILENAME).read_text(encoding="utf-8")
        self.assertIn("2026-10-10", master)
        self.assertIn("2026-10-11", master)
        self.assertIn("笔记总数：2", master)
        self.assertIn("覆盖天数：2", master)
        self.assertIn("index/2026-10-10.md", master)

    def test_master_index_links_are_relative(self) -> None:
        self.store.sync_note(self.add_note("LESSON", "甲"))
        master = (self.memory_root / INDEX_FILENAME).read_text(encoding="utf-8")
        self.assertIn("(index/2026-10-10.md)", master)
        self.assertNotIn(str(self.memory_root), master)

    def test_day_index_links_to_note_files(self) -> None:
        note = self.add_note("LESSON", "甲")
        self.store.sync_note(note)
        day = (self.memory_root / "index" / "2026-10-10.md").read_text(encoding="utf-8")
        self.assertIn(note_filename(note), day)

    def test_hour_file_layout(self) -> None:
        summary, _ = self.storage.record_hour_summary(
            "2026-10-10T14", "本小时观望 2 次", turn_count=2, stats={"HELD": 2},
        )
        path = self.store.sync_hour(summary)
        self.assertEqual(path, self.memory_root / "hours" / "2026-10-10" / "14.md")
        text = path.read_text(encoding="utf-8")
        self.assertIn("小时摘要", text)
        self.assertIn("本小时观望 2 次", text)
        self.assertIn("HELD", text)

    def test_day_index_mentions_hours(self) -> None:
        summary, _ = self.storage.record_hour_summary(
            "2026-10-10T14", "观望", turn_count=1,
        )
        self.store.sync_hour(summary)
        day = (self.memory_root / "index" / "2026-10-10.md").read_text(encoding="utf-8")
        self.assertIn("14:00", day)
        self.assertIn("hours/2026-10-10/14.md", day)

    def test_empty_memory_produces_no_files(self) -> None:
        self.assertEqual(self.store.list_note_files(), [])
        self.assertEqual(self.store.list_hour_files(), [])
        self.assertEqual(self.store.read_master_index(), "")

    def test_default_root_is_under_project(self) -> None:
        store = MemoryStore(self.cfg, storage=self.storage)
        self.assertEqual(store.paths.root, self.cfg.paths.project_root / MEMORY_DIRNAME)


# --------------------------------------------------------------------------- #
# 3. 路径安全
# --------------------------------------------------------------------------- #


class TestPathSafety(MemoryTestCase):
    def test_illegal_day_key_rejected(self) -> None:
        for bad in ("../etc", "2026-10-10/../..", "today", "", "2026-1-1"):
            with self.assertRaises(StorageError, msg=bad):
                self.store.day_index_path(bad)

    def test_hour_path_uses_day_and_hour(self) -> None:
        path = self.store.hour_path("2026-10-10T14")
        self.assertEqual(path.parent.name, "2026-10-10")
        self.assertEqual(path.name, "14.md")

    def test_note_path_stays_inside_root(self) -> None:
        note = self.add_note("LESSON", "../../escape")
        path = self.store.note_path(note)
        self.assertTrue(str(path.resolve()).startswith(str(self.memory_root.resolve())))


# --------------------------------------------------------------------------- #
# 4. 重建
# --------------------------------------------------------------------------- #


class TestRebuild(MemoryTestCase):
    def seed(self) -> None:
        for index in range(3):
            self.storage.record_note(
                "LESSON", f"心得 {index}",
                occurred_at=datetime(2026, 10, 10, 14, index, tzinfo=_JST),
            )
        self.storage.record_note(
            "PLAN", "隔天的事",
            occurred_at=datetime(2026, 10, 11, 9, 0, tzinfo=_JST),
        )
        self.storage.record_hour_summary("2026-10-10T14", "观望", turn_count=2)

    def test_rebuild_creates_everything(self) -> None:
        self.seed()
        counts = self.store.rebuild_all()
        self.assertEqual(counts["notes"], 4)
        self.assertEqual(counts["hours"], 1)
        self.assertEqual(counts["days"], 2)
        self.assertEqual(len(self.store.list_note_files()), 4)
        self.assertEqual(len(self.store.list_hour_files()), 1)
        self.assertTrue((self.memory_root / INDEX_FILENAME).is_file())
        self.assertTrue((self.memory_root / "index" / "2026-10-10.md").is_file())
        self.assertTrue((self.memory_root / "index" / "2026-10-11.md").is_file())

    def test_rebuild_is_idempotent(self) -> None:
        self.seed()
        self.store.rebuild_all()
        first = (self.memory_root / INDEX_FILENAME).read_text(encoding="utf-8")
        self.store.rebuild_all()
        self.assertEqual(
            (self.memory_root / INDEX_FILENAME).read_text(encoding="utf-8"), first
        )

    def test_rebuild_after_directory_is_deleted(self) -> None:
        """删掉 memory/ 不丢任何东西——权威数据在 SQLite。"""
        self.seed()
        self.store.rebuild_all()
        shutil.rmtree(self.memory_root)
        self.assertFalse(self.memory_root.exists())

        counts = self.store.rebuild_all()
        self.assertEqual(counts["notes"], 4)
        self.assertEqual(len(self.store.list_note_files()), 4)

    def test_rebuild_restores_hand_edited_content(self) -> None:
        """手改文件不影响权威数据；重建会把手改内容覆盖掉。"""
        note = self.add_note("LESSON", "原始内容")
        path = self.store.sync_note(note)
        path.write_text("被手改成别的了", encoding="utf-8")
        self.store.rebuild_all()
        self.assertIn("原始内容", path.read_text(encoding="utf-8"))

    def test_rebuild_on_empty_db(self) -> None:
        counts = self.store.rebuild_all()
        self.assertEqual(counts, {"notes": 0, "hours": 0, "days": 0})
        self.assertTrue((self.memory_root / INDEX_FILENAME).is_file())

    def test_master_index_notes_that_it_is_regenerable(self) -> None:
        self.store.rebuild_all()
        master = (self.memory_root / INDEX_FILENAME).read_text(encoding="utf-8")
        self.assertIn("可随时重建", master)
        self.assertIn("agent_notes", master)


# --------------------------------------------------------------------------- #
# 5. 命令行自检
# --------------------------------------------------------------------------- #


class TestMemoryCli(unittest.TestCase):
    def test_requires_rebuild_flag(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "db.sqlite3"
            Storage(db, tz=_JST).initialize_account(_INITIAL)
            import contextlib
            import io

            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = mem_mod._main(["--db", str(db), "--root", str(Path(tmp) / "m")])
            self.assertEqual(code, 2)
            self.assertIn("usage:", out.getvalue())

    def test_rebuild_flag(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "db.sqlite3"
            root = Path(tmp) / "m"
            storage = Storage(db, tz=_JST)
            storage.initialize_account(_INITIAL)
            storage.record_note("LESSON", "一条", occurred_at=datetime(2026, 10, 10, 14, 0, tzinfo=_JST))

            import contextlib
            import io

            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = mem_mod._main(
                    ["--db", str(db), "--root", str(root), "--rebuild"]
                )
            self.assertEqual(code, 0)
            self.assertIn("笔记文件", out.getvalue())
            self.assertTrue((root / INDEX_FILENAME).is_file())

    def test_rebuild_json(self) -> None:
        import contextlib
        import io
        import json

        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "db.sqlite3"
            Storage(db, tz=_JST).initialize_account(_INITIAL)
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = mem_mod._main(
                    ["--db", str(db), "--root", str(Path(tmp) / "m"),
                     "--rebuild", "--json"]
                )
            self.assertEqual(code, 0)
            payload = json.loads(out.getvalue())
            self.assertEqual(payload["notes"], 0)
            self.assertIn("root", payload)


if __name__ == "__main__":
    unittest.main(verbosity=2)
