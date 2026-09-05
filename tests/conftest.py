from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest

# 尽早导入并安装外部网络 guard：pytest_configure 在收集任何测试模块之前执行，
# 且本文件顶层导入发生在插件加载期，可覆盖“模块 import 期间触网”的场景。
ROOT_FOR_CONFTEST = Path(__file__).resolve().parents[1]
if str(ROOT_FOR_CONFTEST) not in sys.path:
    sys.path.insert(0, str(ROOT_FOR_CONFTEST))

from tests.offline_guard import install_offline_guard, restore_proxy_env  # noqa: E402


def pytest_configure(config: pytest.Config) -> None:
    """最早 pytest hook：默认禁止测试访问外部网络（fail-closed）。"""
    install_offline_guard()


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """会话结束恢复本进程内被移除的代理环境变量（不影响系统/用户环境）。"""
    restore_proxy_env()


# app_paths is imported while pytest collects test modules. Give that import a
# process-private root so collection can never touch developer data or open the
# first-run directory chooser.
PYTEST_SESSION_DATA_ROOT = Path(tempfile.mkdtemp(prefix="binggo-pytest-"))
os.environ["BINGGO_DATA_ROOT"] = str(PYTEST_SESSION_DATA_ROOT)
os.environ["BINGGO_LEGACY_DATA_ROOT"] = str(PYTEST_SESSION_DATA_ROOT)
os.environ["BINGGO_DATA_ROOT_LOCATOR"] = str(
    PYTEST_SESSION_DATA_ROOT / "data_root_locator.json"
)

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """隔离用户数据目录与 SQLite 引擎，供依赖 DB 的测试使用。"""
    monkeypatch.setenv("BINGGO_DATA_ROOT", str(tmp_path))
    monkeypatch.setenv("BINGGO_HOME", str(tmp_path))
    monkeypatch.setenv("BINGGO_LEGACY_DATA_ROOT", str(tmp_path))
    data_dir = tmp_path / "data"
    config_dir = tmp_path / "config"
    data_dir.mkdir(parents=True, exist_ok=True)
    config_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "output").mkdir(parents=True, exist_ok=True)
    (data_dir / "cache").mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr("src.app_paths.USER_HOME", tmp_path)
    monkeypatch.setattr("src.app_paths.DATA_DIR", data_dir)
    monkeypatch.setattr("src.app_paths.CONFIG_DIR", config_dir)
    monkeypatch.setattr("src.app_paths.user_home", lambda: tmp_path)
    monkeypatch.setattr("src.app_paths.data_dir", lambda: data_dir)
    monkeypatch.setattr("src.app_paths.config_dir", lambda: config_dir)
    monkeypatch.setattr("src.app_paths.cookie_file", lambda: config_dir / "cookies.txt")
    monkeypatch.setattr("src.app_paths.llm_env_file", lambda: config_dir / "llm.env")
    # 已建空库，跳过 ensure_user_dirs 的种子 bootstrap，避免污染隔离用例
    monkeypatch.setattr("src.app_paths._SEEDED", True)
    monkeypatch.setattr("src.app_paths._BOOTSTRAPPED", True)

    # 旧测试仍以 tmp/data/binggo.db 为夹具契约；新生产路径改为 Profile 后，
    # 只在此兼容夹具里保留旧落点，避免批量改写无关测试。
    from src import data_paths

    data_paths.reset_runtime_profile_for_tests()
    monkeypatch.setattr(
        data_paths,
        "get_database_path",
        lambda profile_id=None: data_dir / "binggo.db",
    )
    monkeypatch.setattr("src.db.import_json._user_home", lambda: tmp_path)
    monkeypatch.setattr("src.db.import_json._data_dir", lambda: data_dir)
    monkeypatch.setattr("src.db.import_json._config_dir", lambda: config_dir)

    # 常见 Store 模块级 DATA_DIR 副本
    for mod in (
        "src.state_store",
        "src.activity_store",
        "src.user_data",
        "src.source_outputs",
        "src.watch_sync",
        "web.activity_service",
    ):
        try:
            monkeypatch.setattr(f"{mod}.DATA_DIR", data_dir, raising=False)
        except Exception:
            pass

    from src.db.engine import db_path, reset_engine_for_tests
    from src.db.schema import init_db

    reset_engine_for_tests()
    init_db()
    # 硬隔离：引擎路径必须落在 tmp_path，防止污染开发机真实 binggo.db
    resolved_db = db_path().resolve()
    resolved_home = tmp_path.resolve()
    assert resolved_db.is_relative_to(resolved_home), resolved_db
    # 额外保险：绝不能落到仓库真实 data/binggo.db
    real_db = (ROOT / "data" / "binggo.db").resolve()
    assert resolved_db != real_db, "isolated_home 仍指向仓库真实数据库"
    yield tmp_path
    reset_engine_for_tests()
    assert db_path().resolve().is_relative_to(tmp_path.resolve()), db_path()
    data_paths.reset_runtime_profile_for_tests()
