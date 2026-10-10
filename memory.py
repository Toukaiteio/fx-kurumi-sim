"""模型的长期记忆：文件投影（总索引 + 按天索引 + 每条记忆一个文件）。

为什么是文件
============

SQLite 里的 ``agent_notes`` / ``agent_turns`` / ``hour_summaries`` 是**权威数据**
（追加式、接在审计哈希链上、可查询、可幂等去重）。但模型和人都需要一个**能直接读**
的形态，而把全部笔记塞进一个文件会越写越大。所以这里把权威数据**投影**成：

::

    memory/
    ├── INDEX.md                          # 总索引：所有天 + 每天的条数/小时数
    ├── index/2026-10-10.md               # 当天索引：每条笔记 + 每个小时摘要各一行
    ├── notes/2026-10-10/0001-LESSON-….md # 一条笔记一个文件
    └── hours/2026-10-10/14.md            # 一个小时结束后的压缩摘要

要点：

* **投影是可再生的**：``rebuild_all()`` 能从 SQLite 完整重建。删掉 ``memory/``
  不会丢任何东西——这是刻意的，免得「文件被手改过」变成新的真相来源。
* **索引按天拆开**，所以单文件大小只跟「那一天」有关，不随时间无限膨胀。
* **一条笔记一个文件**，所以单条笔记写多长都不会影响别的笔记。
* 文件里写入的每条记录都带 ``note_no`` / ``hour_key``，可以反向定位到数据库。

写文件失败**不阻断决策**：记忆是辅助，账本才是权威。失败只记日志。
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone, tzinfo
from pathlib import Path
from typing import Any, Final, Iterable, Sequence

from config import Config, ConfigError, load_config
from storage import AgentNote, HourSummary, Storage, StorageError

__all__ = [
    "INDEX_FILENAME",
    "MEMORY_DIRNAME",
    "MemoryStore",
    "note_filename",
    "slugify",
]

logger = logging.getLogger("kurumi_fx_sim.memory")

#: 记忆目录名（位于项目根下，已被 .gitignore 忽略）。
MEMORY_DIRNAME: Final[str] = "memory"

#: 总索引文件名。
INDEX_FILENAME: Final[str] = "INDEX.md"

#: 文件名里不允许出现的字符（Windows 最严格，按它来）。
_ILLEGAL_FILENAME_CHARS: Final[re.Pattern[str]] = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

#: slug 最大长度（字符数）。太长的文件名在 Windows 上容易踩路径长度上限。
_MAX_SLUG_CHARS: Final[int] = 32

#: 天内索引里每条笔记摘要截断长度。
_SUMMARY_CHARS: Final[int] = 120


# --------------------------------------------------------------------------- #
# 文件名工具
# --------------------------------------------------------------------------- #


def slugify(text: str, *, max_chars: int = _MAX_SLUG_CHARS) -> str:
    """把任意文本压成文件名安全的小段。

    保留中日韩字符（Windows 允许），只清掉非法字符与控制字符，并把空白折成 ``-``。
    结果可能为空（例如全是非法字符），调用方要能接受空串。
    """
    cleaned = _ILLEGAL_FILENAME_CHARS.sub(" ", str(text or ""))
    cleaned = re.sub(r"\s+", "-", cleaned.strip())
    cleaned = cleaned.strip("-.")
    return cleaned[:max_chars].strip("-.")


def note_filename(note: AgentNote) -> str:
    """``0001-LESSON-别在休市时开仓.md``；slug 为空时退化为 ``0001-LESSON.md``。"""
    slug = slugify(note.content)
    stem = f"{note.note_no:04d}-{slugify(note.kind, max_chars=20) or 'NOTE'}"
    return f"{stem}-{slug}.md" if slug else f"{stem}.md"


# --------------------------------------------------------------------------- #
# MemoryStore
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class MemoryPaths:
    root: Path
    master_index: Path
    index_dir: Path
    notes_dir: Path
    hours_dir: Path


class MemoryStore:
    """把 SQLite 里的记忆投影成文件。**只读数据库，只写 memory/ 目录。**"""

    def __init__(
        self,
        cfg: Config | None = None,
        *,
        storage: Storage | None = None,
        root: str | Path | None = None,
        logger_: logging.Logger | None = None,
    ) -> None:
        self.cfg: Config = cfg or load_config()
        self.storage: Storage = storage or Storage.from_config(self.cfg)
        self.logger = logger_ or logger
        base = Path(root).expanduser() if root is not None else (
            self.cfg.paths.project_root / MEMORY_DIRNAME
        )
        self.paths = MemoryPaths(
            root=base,
            master_index=base / INDEX_FILENAME,
            index_dir=base / "index",
            notes_dir=base / "notes",
            hours_dir=base / "hours",
        )

    # -- 路径 -------------------------------------------------------------- #

    def day_index_path(self, day_key: str) -> Path:
        return self.paths.index_dir / f"{_safe_day(day_key)}.md"

    def note_path(self, note: AgentNote) -> Path:
        return self.paths.notes_dir / _safe_day(_day_of(note.created_at, self.cfg.tz)) / note_filename(note)

    def hour_path(self, hour_key: str) -> Path:
        day, _, hour = str(hour_key).partition("T")
        return self.paths.hours_dir / _safe_day(day) / f"{hour[:2] or '00'}.md"

    # -- 写入（单个同步） -------------------------------------------------- #

    def sync_note(self, note: AgentNote) -> Path:
        """写一条笔记的文件，并重建它所在天的索引与总索引。"""
        day = _day_of(note.created_at, self.cfg.tz)
        target = self.note_path(note)
        self._write(target, _render_note(note, self.cfg.tz))
        self.rebuild_day_index(day)
        self.rebuild_master_index()
        return target

    def sync_hour(self, summary: HourSummary) -> Path:
        """写一个小时摘要文件，并重建它所在天的索引与总索引。"""
        target = self.hour_path(summary.hour_key)
        self._write(target, _render_hour(summary, self.cfg.tz))
        self.rebuild_day_index(summary.day_key)
        self.rebuild_master_index()
        return target

    # -- 重建 -------------------------------------------------------------- #

    def rebuild_day_index(self, day_key: str) -> Path:
        day = _safe_day(day_key)
        notes = [
            n for n in self.storage.read_notes(limit=None, newest_first=False)
            if _day_of(n.created_at, self.cfg.tz) == day
        ]
        hours = self.storage.read_hour_summaries(day_key=day, newest_first=False)
        target = self.day_index_path(day)
        self._write(target, _render_day_index(day, notes, hours, self))
        return target

    def rebuild_master_index(self) -> Path:
        notes = self.storage.read_notes(limit=None, newest_first=False)
        hours = self.storage.read_hour_summaries(newest_first=False)
        target = self.paths.master_index
        self._write(target, _render_master_index(notes, hours, self))
        return target

    def rebuild_all(self) -> dict[str, int]:
        """从 SQLite 完整重建整个 ``memory/`` 目录（幂等）。

        返回 ``{"notes": 文件数, "hours": 文件数, "days": 索引数}``。
        """
        notes = self.storage.read_notes(limit=None, newest_first=False)
        hours = self.storage.read_hour_summaries(newest_first=False)

        note_files = 0
        for note in notes:
            self._write(self.note_path(note), _render_note(note, self.cfg.tz))
            note_files += 1
        hour_files = 0
        for summary in hours:
            self._write(self.hour_path(summary.hour_key), _render_hour(summary, self.cfg.tz))
            hour_files += 1

        days = sorted(
            {
                _day_of(n.created_at, self.cfg.tz) for n in notes
            }
            | {h.day_key for h in hours}
        )
        for day in days:
            self.rebuild_day_index(day)
        self.rebuild_master_index()
        self.logger.info(
            "记忆投影重建完成：笔记 %d 个文件、小时 %d 个文件、天索引 %d 个",
            note_files, hour_files, len(days),
        )
        return {"notes": note_files, "hours": hour_files, "days": len(days)}

    # -- 读取 -------------------------------------------------------------- #

    def read_master_index(self) -> str:
        if not self.paths.master_index.is_file():
            return ""
        return self.paths.master_index.read_text(encoding="utf-8")

    def read_day_index(self, day_key: str) -> str:
        path = self.day_index_path(day_key)
        return path.read_text(encoding="utf-8") if path.is_file() else ""

    def list_note_files(self) -> list[Path]:
        if not self.paths.notes_dir.is_dir():
            return []
        return sorted(p for p in self.paths.notes_dir.rglob("*.md") if p.is_file())

    def list_hour_files(self) -> list[Path]:
        if not self.paths.hours_dir.is_dir():
            return []
        return sorted(p for p in self.paths.hours_dir.rglob("*.md") if p.is_file())

    # -- 内部 -------------------------------------------------------------- #

    def _write(self, path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


# --------------------------------------------------------------------------- #
# 渲染
# --------------------------------------------------------------------------- #


def _day_of(moment: datetime, tz: tzinfo) -> str:
    return moment.astimezone(tz).date().isoformat()


def _safe_day(day_key: str) -> str:
    """只允许 ``YYYY-MM-DD``；其它一律拒绝，避免路径穿越。"""
    text = str(day_key or "").strip()
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        raise StorageError(f"非法日期键（期望 YYYY-MM-DD）：{day_key!r}")
    return text


def _one_line(text: str, limit: int = _SUMMARY_CHARS) -> str:
    flat = re.sub(r"\s+", " ", str(text or "")).strip()
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def _render_note(note: AgentNote, tz: tzinfo) -> str:
    tags = f"\n- 标签：{'、'.join(note.tags)}" if note.tags else ""
    decision = f"\n- 决策编号：`{note.decision_id}`" if note.decision_id else ""
    return (
        f"# 记忆 #{note.note_no}　{note.kind}\n\n"
        f"- 时间：{note.created_at.astimezone(tz).isoformat(timespec='seconds')}\n"
        f"- 作者：{note.author}{tags}{decision}\n"
        f"- 交易日：{note.trading_date or '（未记录）'}\n\n"
        f"{note.content}\n"
    )


def _render_hour(summary: HourSummary, tz: tzinfo) -> str:
    stats = summary.stats or {}
    stat_lines = "\n".join(f"- {key}：{value}" for key, value in sorted(stats.items()))
    return (
        f"# {summary.hour_key} 小时摘要\n\n"
        f"- 天：{summary.day_key}\n"
        f"- 决策次数：{summary.turn_count}\n"
        f"- 生成时间：{summary.created_at.astimezone(tz).isoformat(timespec='seconds')}\n\n"
        f"## 压缩摘要\n\n{summary.summary}\n\n"
        f"## 统计\n\n{stat_lines or '（无）'}\n"
    )


def _render_day_index(
    day: str, notes: Sequence[AgentNote], hours: Sequence[HourSummary], store: MemoryStore
) -> str:
    lines = [
        f"# {day} 记忆索引",
        "",
        f"- 笔记：{len(notes)} 条",
        f"- 小时摘要：{len(hours)} 个",
        "",
        "## 小时摘要",
        "",
    ]
    if hours:
        for summary in hours:
            rel = _rel(store.hour_path(summary.hour_key), store.paths.root)
            lines.append(
                f"- [{summary.hour_key[11:13]}:00]({rel})"
                f"　决策 {summary.turn_count} 次　{_one_line(summary.summary)}"
            )
    else:
        lines.append("（无）")
    lines += ["", "## 笔记", ""]
    if notes:
        for note in notes:
            rel = _rel(store.note_path(note), store.paths.root)
            tags = f"　`{' '.join(note.tags)}`" if note.tags else ""
            lines.append(
                f"- [{note.note_no:04d} {note.kind}]({rel})"
                f"　{note.created_at.astimezone(store.cfg.tz).strftime('%H:%M')}"
                f"{tags}　{_one_line(note.content)}"
            )
    else:
        lines.append("（无）")
    lines.append("")
    return "\n".join(lines)


def _render_master_index(
    notes: Sequence[AgentNote], hours: Sequence[HourSummary], store: MemoryStore
) -> str:
    by_day: dict[str, dict[str, int]] = {}
    for note in notes:
        day = _day_of(note.created_at, store.cfg.tz)
        by_day.setdefault(day, {"notes": 0, "hours": 0})["notes"] += 1
    for summary in hours:
        by_day.setdefault(summary.day_key, {"notes": 0, "hours": 0})["hours"] += 1

    lines = [
        "# 久留美的记忆总索引",
        "",
        "本目录由 `memory.py` 从 SQLite 投影生成，**可随时重建**（`python -m main memory --rebuild`）。",
        "手改这里的内容不会影响权威数据；权威数据是数据库里的 `agent_notes` / `hour_summaries`。",
        "",
        f"- 笔记总数：{len(notes)}",
        f"- 小时摘要总数：{len(hours)}",
        f"- 覆盖天数：{len(by_day)}",
        "",
        "## 按天",
        "",
    ]
    if by_day:
        for day in sorted(by_day, reverse=True):
            counts = by_day[day]
            rel = _rel(store.day_index_path(day), store.paths.root)
            lines.append(
                f"- [{day}]({rel})　笔记 {counts['notes']} 条　小时摘要 {counts['hours']} 个"
            )
    else:
        lines.append("（还没有任何记忆）")
    lines.append("")
    return "\n".join(lines)


def _rel(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:  # pragma: no cover - 只在跨盘符时发生
        return path.as_posix()


# --------------------------------------------------------------------------- #
# 命令行自检
# --------------------------------------------------------------------------- #


def _main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m memory",
        description=(
            "模型的长期记忆（文件投影）。权威数据在 SQLite，本命令把 memory/ 目录重建出来。"
        ),
    )
    parser.add_argument("--db", default=None, help="数据库路径（默认取 config.json）")
    parser.add_argument("--root", default=None, help="记忆目录（默认 <项目根>/memory）")
    parser.add_argument("--rebuild", action="store_true", help="从数据库完整重建文件投影")
    parser.add_argument("--json", action="store_true", dest="as_json", help="以 JSON 输出")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    try:
        cfg = load_config()
    except ConfigError as exc:
        print(f"[配置错误] {exc}", file=sys.stderr)
        return 2
    storage = Storage(Path(args.db).expanduser() if args.db else cfg.paths.database_path,
                      tz=cfg.tz)
    store = MemoryStore(cfg, storage=storage, root=args.root)

    if not args.rebuild:
        parser.print_help()
        return 2

    result = store.rebuild_all()
    if args.as_json:
        import json

        print(json.dumps({**result, "root": str(store.paths.root)}, ensure_ascii=False, indent=2))
        return 0
    print(f"记忆目录  : {store.paths.root}")
    print(f"  笔记文件: {result['notes']}")
    print(f"  小时文件: {result['hours']}")
    print(f"  天索引  : {result['days']}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_main())
