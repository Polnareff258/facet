"""pytest 共享夹具。"""
from __future__ import annotations

import shutil
import sys
import uuid
from pathlib import Path

import pytest

# 让 tests/ 在未安装包的情况下也能 import csmon
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from csmon.config import Config, NotifyConfig, SourceConfig, WebConfig  # noqa: E402
from csmon.mapping import MappingService  # noqa: E402
from csmon.store import Store  # noqa: E402

# 沙箱环境的系统临时目录不可写，因此把测试临时目录固定在工作区内
WORKROOT = ROOT / ".testwork"


@pytest.fixture()
def tmp_path():
    """覆盖 pytest 内建实现：把临时目录放在仓库内，规避系统 temp 权限限制。"""
    WORKROOT.mkdir(parents=True, exist_ok=True)
    path = WORKROOT / uuid.uuid4().hex[:12]
    path.mkdir(parents=True, exist_ok=True)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


@pytest.fixture()
def store(tmp_path: Path) -> Store:
    s = Store(tmp_path / "test.db")
    yield s
    s.close()


@pytest.fixture()
def mapping(store: Store) -> MappingService:
    return MappingService(store)


def make_config(tmp_path: Path, **overrides) -> Config:
    cfg = Config(
        database=str(tmp_path / "cfg.db"),
        poll_interval=60,
        sources={
            "mock": SourceConfig(name="mock", enabled=True, priority=1, min_interval=0.0),
        },
        notify=NotifyConfig(enabled=False),
        web=WebConfig(host="127.0.0.1", port=0),
    )
    for key, value in overrides.items():
        setattr(cfg, key, value)
    return cfg


@pytest.fixture()
def config(tmp_path: Path) -> Config:
    return make_config(tmp_path)
