"""首板观察池的前向研究样本，不依赖状态变化事件，也不参与买入判定。

调用方负责交易日历和后台调度；只接受当前五分钟锚点所在分钟，不补写错过的
时点。完整候选（含负例和未知）按股票汇总，仍保留各形态的输入证据。
"""
from __future__ import annotations

import gzip
import json
import os
import re
import threading
import uuid
import zlib
from collections import OrderedDict
from datetime import date, datetime, timedelta
from datetime import time as day_time
from itertools import islice
from pathlib import Path, PurePath, PureWindowsPath

from app.market_time import CN_TZ

_VERSION = re.compile(r"[0-9a-f]{8,128}\Z")
_MEMBER_KEYS = {"id", "kind", "name", "source_id", "field", "members"}
_MAX_SAMPLES_PER_DAY = 48


def _beijing(value: datetime) -> datetime:
    return value.astimezone(CN_TZ) if value.tzinfo else value.replace(tzinfo=CN_TZ)


def _due(now: datetime) -> bool:
    clock = now.time().replace(tzinfo=None)
    return now.weekday() < 5 and now.minute % 5 == 0 and (
        day_time(9, 30) <= clock < day_time(11, 30)
        or day_time(13) <= clock < day_time(14, 57)
    )


def _date_json(value):
    if isinstance(value, datetime):
        return _beijing(value).isoformat()
    if isinstance(value, date):
        return value.isoformat()
    raise TypeError(f"观察样本包含不可序列化的类型: {type(value).__name__}")


def _json(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"),
                      default=_date_json).encode("utf-8")


def _version(value: str) -> None:
    if not isinstance(value, str) or not _VERSION.fullmatch(value):
        raise ValueError("成分版本须为 8 至 128 位小写十六进制摘要")


def _groups(groups: list[dict]) -> None:
    if not isinstance(groups, list) or any(
        not isinstance(group, dict) or not group.keys() >= _MEMBER_KEYS
        or not isinstance(group["members"], list)
        or any(not isinstance(member, str) or not member for member in group["members"])
        for group in groups
    ):
        raise ValueError("成分快照必须保留完整分组身份及成员")


def _timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("时间缺少时区")
    return _beijing(parsed)


def _path_identity(path: PurePath) -> PurePath:
    """Compare already resolved Windows paths in one namespace.

    During concurrent mkdir, Python's Windows realpath may retain the extended
    prefix on one resolution and omit it on another. Only canonical drive/UNC
    prefixes are normalized; the directory components and resolved link target
    remain intact. This value is never used to open or publish files.
    """
    if isinstance(path, PureWindowsPath):
        text = str(path)
        if text.upper().startswith("\\\\?\\UNC\\"):
            return PureWindowsPath("\\\\" + text[8:])
        if text.startswith("\\\\?\\") and re.match(r"[A-Za-z]:\\", text[4:]):
            return PureWindowsPath(text[4:])
    return path


class FirstBoardObservationStore:
    """不可变 gzip JSON 文件；最多缓存四个交易日/规则版本的采样目录索引。

    不捕获磁盘权限、空间或硬链接能力错误，由调用方隔离研究采样失败。发布使用
    临时文件 + 排他硬链接，读侧只见完整文件，跨实例也不会覆盖先到的样本。
    """

    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        self._lock = threading.RLock()
        self._indices: OrderedDict[tuple[str, int], dict[str, str]] = OrderedDict()

    def _path(self, *parts: str) -> Path:
        path = self.root.joinpath(*parts)
        if path.is_symlink():
            raise ValueError("观察样本路径指向符号链接")
        if not _path_identity(path.parent.resolve()).is_relative_to(_path_identity(self.root)):
            raise ValueError("观察样本路径超出存储目录")
        return path

    @staticmethod
    def _read(path: Path) -> dict:
        try:
            value = json.loads(gzip.decompress(path.read_bytes()))
            if not isinstance(value, dict):
                raise ValueError("样本必须为对象")
            # JSON 解码器默认允许 NaN；既有损坏文件也必须拒绝。
            _json(value)
            return value
        except (ValueError, EOFError, zlib.error, gzip.BadGzipFile) as exc:
            raise ValueError(f"首板研究文件损坏: {path.name}") from exc

    def _publish(self, path: Path, value: dict) -> bool:
        compressed = gzip.compress(_json(value), mtime=0)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._path(*path.relative_to(self.root).parts[:-1],
                               f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            with temporary.open("xb") as stream:
                stream.write(compressed)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(temporary, path)
                return True
            except FileExistsError:
                return False
        finally:
            temporary.unlink(missing_ok=True)

    def _membership(self, version: str) -> dict:
        path = self._path("memberships", f"{version}.json.gz")
        if not path.exists():
            raise ValueError("首板研究成分快照不存在")
        value = self._read(path)
        try:
            if value["schema_version"] != 1 or value["version"] != version:
                raise ValueError("版本不匹配")
            _timestamp(value["observed_at"])
            _groups(value["groups"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"首板研究成分快照损坏: {path.name}") from exc
        return value

    def write_membership(self, groups: list[dict], version: str, observed_at: datetime) -> None:
        """保存成分版本；相同版本只验证内容，不改写首次观测时间。"""
        _version(version)
        _groups(groups)
        value = {"schema_version": 1, "version": version,
                 "observed_at": _beijing(observed_at).isoformat(), "groups": groups}
        with self._lock:
            path = self._path("memberships", f"{version}.json.gz")
            if path.exists() or not self._publish(path, value):
                existing = self._membership(version)
                if existing["groups"] != groups:
                    raise ValueError("首板研究成分快照版本冲突，已有文件未修改")

    @staticmethod
    def _rows(rows: list[dict]) -> list[dict]:
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise ValueError("观察样本候选必须为对象列表")
        by_symbol: dict[str, list[dict]] = {}
        for row in rows:
            symbol = row.get("symbol")
            if not isinstance(symbol, str) or not symbol:
                raise ValueError("观察样本缺少股票代码")
            by_symbol.setdefault(symbol, []).append(row)
        result = []
        for symbol, candidates in by_symbol.items():
            flags = [candidate.get("can_buy") for candidate in candidates]
            contexts = [candidate.get("attention_context") for candidate in candidates]
            result.append({
                "symbol": symbol, "name": candidates[0].get("name"),
                "patterns": list(dict.fromkeys(row.get("pattern") for row in candidates)),
                "states": list(dict.fromkeys(row.get("state") for row in candidates)),
                "can_buy": True if any(flag is True for flag in flags)
                else False if all(flag is False for flag in flags) else None,
                "attention_context": contexts[0] if all(item == contexts[0] for item in contexts)
                else None,
                "candidates": candidates,
            })
        return result

    def _sample(self, path: Path, day: str, revision: int) -> dict:
        value = self._read(path)
        try:
            anchor = _timestamp(value["anchor"])
            observed_at = _timestamp(value["observed_at"])
            rows = value["rows"]
            if (value["schema_version"] != 1 or value["trade_date"] != day
                    or value["rule_revision"] != revision or anchor.date().isoformat() != day
                    or path.name != f"{anchor:%H%M}.json.gz" or not _due(anchor)
                    or anchor.second or anchor.microsecond
                    or not anchor <= observed_at < anchor + timedelta(minutes=1)
                    or not isinstance(rows, list) or not isinstance(value["metadata"], dict)
                    or value["symbol_count"] != len(rows)
                    or len({row["symbol"] for row in rows}) != len(rows)
                    or value["candidate_count"] != sum(len(row["candidates"]) for row in rows)
                    or any(candidate["symbol"] != row["symbol"] for row in rows
                           for candidate in row["candidates"])):
                raise ValueError("样本结构与时点不一致")
            if value["membership_version"] is not None:
                _version(value["membership_version"])
                members = self._membership(value["membership_version"])
                if _timestamp(members["observed_at"]) > observed_at:
                    raise ValueError("成分版本晚于样本")
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"首板研究样本损坏: {path.name}") from exc
        return value

    def _index(self, day: str, revision: int) -> dict[str, str]:
        key = (day, revision)
        if key not in self._indices:
            directory = self._path(day, str(revision))
            paths = list(islice(directory.glob("*.json.gz"), _MAX_SAMPLES_PER_DAY + 1))
            if len(paths) > _MAX_SAMPLES_PER_DAY:
                raise ValueError("首板研究采样目录超出单日锚点数量")
            index = {}
            for path in paths:
                safe_path = self._path(day, str(revision), path.name)
                index[path.name] = self._sample(safe_path, day, revision)["observed_at"]
            self._indices[key] = index
            if len(self._indices) > 4:
                self._indices.popitem(last=False)
        self._indices.move_to_end(key)
        return self._indices[key]

    def record(self, rows: list[dict], *, now: datetime, rule_revision: int,
               membership_version: str | None, metadata: dict) -> dict:
        """按当前北京时间的五分钟锚点采集；不会补采，也不会更新已有样本。"""
        if type(rule_revision) is not int or rule_revision < 0:
            raise ValueError("规则版本须为非负整数")
        if not isinstance(metadata, dict):
            raise ValueError("观察样本元信息必须为对象")
        if membership_version is not None:
            _version(membership_version)
        now = _beijing(now)
        day = now.date().isoformat()
        with self._lock:
            index = self._index(day, rule_revision)
            status = "not_due"
            if _due(now):
                anchor = now.replace(second=0, microsecond=0)
                path = self._path(day, str(rule_revision), f"{anchor:%H%M}.json.gz")
                if path.exists():
                    sample = self._sample(path, day, rule_revision)
                    status = "already_recorded"
                else:
                    if membership_version is not None:
                        members = self._membership(membership_version)
                        if _timestamp(members["observed_at"]) > now:
                            raise ValueError("成分快照首次观测时间晚于采样时间")
                    grouped = self._rows(rows)
                    sample = {
                        "schema_version": 1, "trade_date": day, "rule_revision": rule_revision,
                        "membership_version": membership_version, "anchor": anchor.isoformat(),
                        "observed_at": now.isoformat(), "candidate_count": len(rows),
                        "symbol_count": len(grouped), "metadata": metadata, "rows": grouped,
                    }
                    if self._publish(path, sample):
                        status = "recorded"
                    else:
                        sample = self._sample(path, day, rule_revision)
                        status = "already_recorded"
                index[path.name] = sample["observed_at"]
            return {"status": status, "last_sample_at": max(index.values(), default=None),
                    "sample_count": len(index)}

    def iter_samples(self, start: date, end: date, revision: int):
        """Read immutable samples in time order; never populate or repair files."""
        if (type(start) is not date or type(end) is not date or start > end
                or (end - start).days >= 92 or type(revision) is not int or revision < 0):
            raise ValueError("观察读取需要有效规则版本及不超过92个自然日的区间")
        for offset in range((end - start).days + 1):
            day = (start + timedelta(days=offset)).isoformat()
            directory = self._path(day, str(revision))
            paths = list(islice(directory.glob("*.json.gz"), _MAX_SAMPLES_PER_DAY + 1))
            if len(paths) > _MAX_SAMPLES_PER_DAY:
                raise ValueError("首板研究采样目录超出单日锚点数量")
            for path in sorted(paths):
                yield self._sample(self._path(day, str(revision), path.name), day, revision)
