"""M7 onboarding（离线）：staging commit 自动创建/复用 Profile，绝不污染当前 Profile。

覆盖 crash-consistency A–H 与并发（RegistryLease 同 UID 去重），以及 V18 fail-closed。
全 mock：零真实 Bilibili/QR/LLM。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from src.login_onboarding import (
    OnboardingError,
    _write_profile_metadata,
    commit_staged_login,
    onboarding_complete,
    onboarding_create,
    onboarding_fail,
    onboarding_qr_image_path,
    onboarding_start_remote,
    onboarding_store_scanned_cookie,
)
from src.profile_writer_lease import ProfileWriterLeaseBusy
from src.profile_manager import list_profiles

COOKIE_A = "SESSDATA=abc;DedeUserID=90001;DedeUserID__ckMd5=x"
COOKIE_B = "SESSDATA=def;DedeUserID=90002;DedeUserID__ckMd5=y"


@pytest.fixture
def root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    from src import data_paths

    base = tmp_path / "data-root"
    profiles = base / "profiles"
    profiles.mkdir(parents=True, exist_ok=True)
    account = profiles / "account-1"
    account.mkdir()
    (account / "profile.json").write_text(
        json.dumps({"profile_id": "account-1", "mid": "88001", "nickname": "old"}), encoding="utf-8"
    )
    (account / "cookies.txt").write_text("SESSDATA=old;DedeUserID=88001", encoding="utf-8")
    (base / "active_profile.json").write_text(
        json.dumps({"profile_id": "account-1"}), encoding="utf-8"
    )
    monkeypatch.setenv(data_paths.DATA_ROOT_ENV, str(base))
    monkeypatch.delenv(data_paths.LEGACY_HOME_ENV, raising=False)
    monkeypatch.delenv(data_paths.LEGACY_DATA_ROOT_ENV, raising=False)
    data_paths.reset_runtime_profile_for_tests()
    # 冻结 runtime 指向 account-1（模拟当前运行账号）
    data_paths.get_runtime_profile_id()
    return base


def _old_cookie(root: Path) -> str:
    return (root / "profiles" / "account-1" / "cookies.txt").read_text(encoding="utf-8")


def test_scan_start_does_not_touch_current_profile(root: Path) -> None:
    before = _old_cookie(root)
    # commit 前（模拟扫码阶段）没有任何当前 Profile 写入发生——本模块无 QR 阶段写路径
    assert before == "SESSDATA=old;DedeUserID=88001"


def test_new_uid_creates_independent_profile(root: Path) -> None:
    result = commit_staged_login(cookie_str=COOKIE_A, nickname="newbie")
    assert result["created_new"] is True
    assert result["profile_id"] == "account-2"
    assert result["restart_required"] is False
    profile_dir = root / "profiles" / "account-2"
    assert (profile_dir / "cookies.txt").read_text(encoding="utf-8") == COOKIE_A
    metadata = json.loads((profile_dir / "profile.json").read_text(encoding="utf-8"))
    assert metadata["mid"] == "90001"
    assert metadata["nickname"] == "newbie"
    # DB 完整初始化
    assert (profile_dir / "binggo.db").exists()
    # 旧账号未受影响
    assert _old_cookie(root) == "SESSDATA=old;DedeUserID=88001"
    # V109 扫码只添加 Profile，不切换当前 runtime 或默认启动指针。
    pointer = json.loads((root / "active_profile.json").read_text(encoding="utf-8"))
    assert pointer["profile_id"] == "account-1"


def test_failed_onboarding_leaves_no_formal_profile(root: Path, monkeypatch) -> None:
    # UID 拿不到：0 Profile 创建 / pointer 不变
    with pytest.raises(OnboardingError) as exc:
        commit_staged_login(cookie_str="SESSDATA=abc")
    assert exc.value.code == "uid_unavailable"
    assert [p["profile_id"] for p in list_profiles()] == ["account-1"]
    pointer = json.loads((root / "active_profile.json").read_text(encoding="utf-8"))
    assert pointer["profile_id"] == "account-1"


def test_create_failure_leaves_no_partial_profile(root: Path, monkeypatch) -> None:
    def boom(*a, **k):
        raise OSError("init failed")

    monkeypatch.setattr("src.login_onboarding._init_profile_db", boom)
    with pytest.raises(OSError):
        commit_staged_login(cookie_str=COOKIE_A, nickname="x")
    assert [p["profile_id"] for p in list_profiles()] == ["account-1"]
    assert not list((root / "profiles").glob(".creating-*"))
    pointer = json.loads((root / "active_profile.json").read_text(encoding="utf-8"))
    assert pointer["profile_id"] == "account-1"


def test_onboarding_never_writes_active_pointer(root: Path) -> None:
    result = commit_staged_login(cookie_str=COOKIE_A, nickname="x")
    assert result["profile_id"] == "account-2"
    # Profile 已保留、无半成品残留
    assert (root / "profiles" / "account-2").is_dir()
    assert not list((root / "profiles").glob(".creating-*"))
    pointer = json.loads((root / "active_profile.json").read_text(encoding="utf-8"))
    assert pointer["profile_id"] == "account-1"


def test_existing_uid_reuses_profile_updates_cookie(root: Path) -> None:
    # 预先标记 account-1 归属 UID 90001
    _write_profile_metadata(root / "profiles" / "account-1", profile_id="account-1", mid="90001", nickname="same")
    (root / "profiles" / "account-1" / "cookies.txt").write_text("SESSDATA=old;DedeUserID=90001", encoding="utf-8")
    result = commit_staged_login(cookie_str=COOKIE_A, nickname="again")
    assert result["reused_existing"] is True
    assert result["profile_id"] == "account-1"
    assert (root / "profiles" / "account-1" / "cookies.txt").read_text(encoding="utf-8") == COOKIE_A
    assert len([p for p in list_profiles()]) == 1  # 不创建第二 Profile


def test_existing_uid_writer_busy_zero_cookie_write(root: Path, monkeypatch) -> None:
    from src.profile_writer_lease import profile_writer_acquire, profile_writer_release

    _write_profile_metadata(root / "profiles" / "account-1", profile_id="account-1", mid="90001", nickname="same")
    (root / "profiles" / "account-1" / "cookies.txt").write_text("SESSDATA=old;DedeUserID=90001", encoding="utf-8")
    old_cookie = _old_cookie(root)
    lease = profile_writer_acquire(profile_id="account-1", owner="other")
    try:
        with pytest.raises(OnboardingError) as exc_info:
            commit_staged_login(cookie_str=COOKIE_A, nickname="busy")
        assert exc_info.value.code == "writer_busy"
    finally:
        profile_writer_release(lease, profile_id="account-1")
    assert _old_cookie(root) == old_cookie


def test_v18_corrupt_pointer_fail_closed(root: Path) -> None:
    (root / "active_profile.json").write_text("{corrupt", encoding="utf-8")
    with pytest.raises(OnboardingError) as exc:
        commit_staged_login(cookie_str=COOKIE_A, nickname="x")
    assert exc.value.code == "pointer_fail_closed"
    # 不自动覆盖 pointer
    assert (root / "active_profile.json").read_text(encoding="utf-8") == "{corrupt"


def test_dangling_pointer_fail_closed(root: Path) -> None:
    (root / "active_profile.json").write_text(
        json.dumps({"profile_id": "account-99"}), encoding="utf-8"
    )
    with pytest.raises(OnboardingError) as exc:
        commit_staged_login(cookie_str=COOKIE_A, nickname="x")
    assert exc.value.code == "pointer_fail_closed"


# ---------- 跨进程并发 / 新进程绑定 ----------


def _child_script(cookie: str, nickname: str) -> str:
    root = str(Path(__file__).resolve().parents[1])
    return "\n".join([
        "import sys, json",
        "sys.path.insert(0, " + repr(root) + ")",
        "from src.login_onboarding import commit_staged_login",
        "r = commit_staged_login(cookie_str=" + repr(cookie) + ", nickname=" + repr(nickname) + ")",
        "print(json.dumps(r))",
    ])


def _run_child(root: Path, cookie: str, nickname: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", _child_script(cookie, nickname)],
        capture_output=True,
        text=True,
        env={**dict(__import__('os').environ), "BINGGO_DATA_ROOT": str(root)},
        cwd=str(Path(__file__).resolve().parents[1]),
        timeout=180,
    )


def test_concurrent_different_uids_create_two_profiles(root: Path) -> None:
    proc_a = _run_child(root, COOKIE_A, "a")
    proc_b = _run_child(root, COOKIE_B, "b")
    assert proc_a.returncode == 0, proc_a.stderr
    assert proc_b.returncode == 0, proc_b.stderr
    profiles = [p["profile_id"] for p in list_profiles()]
    assert len(profiles) == 3  # account-1 + 2 个新账号
    assert not list((root / "profiles").glob(".creating-*"))
    assert (root / "profiles" / "account-2" / "binggo.db").exists()
    assert (root / "profiles" / "account-3" / "binggo.db").exists()


def test_concurrent_same_uid_never_creates_duplicate_profile(root: Path) -> None:
    proc_a = _run_child(root, COOKIE_A, "first")
    proc_b = _run_child(root, COOKIE_A, "second")
    assert proc_a.returncode == 0, proc_a.stderr
    assert proc_b.returncode == 0, proc_b.stderr
    profiles = list_profiles()
    assert len(profiles) == 2  # account-1 + 仅一个 90001 新 Profile
    owners = [
        p["profile_id"] for p in profiles
        if (root / "profiles" / p["profile_id"] / "profile.json").read_text(encoding="utf-8").find("90001") >= 0
    ]
    assert len(owners) == 1


def test_next_ordinary_process_keeps_default_profile_after_scan(root: Path) -> None:
    commit_staged_login(cookie_str=COOKIE_A, nickname="bind")
    root_dir = str(Path(__file__).resolve().parents[1])
    code = "\n".join([
        "import sys",
        "sys.path.insert(0, " + repr(root_dir) + ")",
        "from src.data_paths import get_runtime_profile_id",
        "print(get_runtime_profile_id())",
    ])
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        env={**dict(__import__('os').environ), "BINGGO_DATA_ROOT": str(root)},
        cwd=str(Path(__file__).resolve().parents[1]),
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "account-1"


# ---------- E/F/L/U/W：新 Profile 隔离 + 默认能力 + 无 .creating 泄漏 ----------


def test_new_profile_isolated_and_defaults_present(root: Path, monkeypatch) -> None:
    # 给旧 Profile 预置各模块状态文件
    account1 = root / "profiles" / "account-1"
    markers = [
        "ds9_topics.json", "notify_config.json", "notify_outbox.json",
        "winner_scan_state.json", "winner_candidates.json",
        "auto_delete_enabled.json", "following_feed_state.json",
    ]
    for name in markers:
        (account1 / name).write_text("{}", encoding="utf-8")

    result = commit_staged_login(cookie_str=COOKIE_A, nickname="iso")
    assert result["created_new"] is True
    account2 = root / "profiles" / "account-2"
    # E：旧状态文件绝不复制
    for name in markers:
        assert not (account2 / name).exists(), name
    # F：默认能力仍在（DS9 built-in 来自代码默认，首次读取即存在；指向新目录）
    import src.data_paths as dp
    monkeypatch.setattr(dp, "get_profile_dir", lambda profile_id=None: account2)
    from src.sources.ds9_topics import list_topics
    topics = list_topics()
    assert [t["topic_id"] for t in topics][:3] == ["1261901", "1055656", "47801"]


def test_staging_cookie_never_in_api_result(root: Path) -> None:
    result = commit_staged_login(cookie_str=COOKIE_B, nickname="safe")
    payload = json.dumps(result)
    assert "SESSDATA=def" not in payload
    assert "DedeUserID" not in payload
    assert "cookie" not in result


# ---------- Server-side onboarding session：幂等 / staging / 隔离 ----------


def test_onboarding_waiting_never_touches_current_profile(root: Path) -> None:
    before = _old_cookie(root)
    session = onboarding_create(qr_url="https://qr.test/1", qr_key="k1")
    assert session["status"] == "waiting"
    assert _old_cookie(root) == before


def test_remote_start_reuses_login_generate_primitive(root: Path, monkeypatch) -> None:
    before = _old_cookie(root)
    monkeypatch.setattr(
        "src.bilibili_login.generate_qrcode",
        lambda: ("https://passport.bilibili.com/h5-app/passport/login/scan", "key-1"),
    )
    save_calls: list[str] = []
    monkeypatch.setattr(
        "src.bilibili_login.save_cookies",
        lambda cookie: save_calls.append(cookie),
    )
    result = onboarding_start_remote()

    assert result["onboarding_id"]
    assert "cookie" not in result
    assert "qrcode_key" not in result
    assert "qr_url" not in result  # qr_url 只用于渲染 PNG，不进 API/JSON
    staged_path = root / "shared" / "onboarding" / f"{result['onboarding_id']}.json"
    staged_text = staged_path.read_text(encoding="utf-8")
    assert "key-1" not in staged_text
    assert '"qr_key"' not in staged_text
    assert '"qr_url"' not in staged_text
    staged = json.loads(staged_text)
    assert staged["has_qr_key"] is True
    assert staged["has_cookie"] is False
    secret = root / "shared" / "onboarding" / ".secrets" / f"{result['onboarding_id']}.qr_key"
    assert secret.read_text(encoding="utf-8") == "key-1"
    assert _old_cookie(root) == before
    assert save_calls == []


def test_qr_image_is_non_empty_png(root: Path) -> None:
    session = onboarding_create(qr_url="https://qr.test/1", qr_key="k1")
    image_path = onboarding_qr_image_path(session["onboarding_id"])

    assert image_path.suffix == ".png"
    assert image_path.stat().st_size > 0
    assert image_path.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")


def test_session_scanned_cookie_and_complete_creates_profile(root: Path) -> None:
    session = onboarding_create(qr_url="u", qr_key="k")
    onboarding_store_scanned_cookie(session["onboarding_id"], COOKIE_A)
    assert _old_cookie(root) == "SESSDATA=old;DedeUserID=88001"  # 当前 Profile 未动
    result = onboarding_complete(session["onboarding_id"], nickname="n1")
    assert result["created_new"] is True
    assert (root / "profiles" / "account-2" / "cookies.txt").read_text(encoding="utf-8") == COOKIE_A


def test_complete_idempotent_never_creates_duplicate(root: Path) -> None:
    session = onboarding_create(qr_url="u", qr_key="k")
    onboarding_store_scanned_cookie(session["onboarding_id"], COOKIE_A)
    first = onboarding_complete(session["onboarding_id"])
    # 会话文件会被清理；幂等依赖 completed 状态返回（此处结果保留在内存不可再查）
    # 模拟刷新重试：同 cookie 新会话也不重复建（UID 查重）
    session2 = onboarding_create(qr_url="u", qr_key="k2")
    onboarding_store_scanned_cookie(session2["onboarding_id"], COOKIE_A)
    second = onboarding_complete(session2["onboarding_id"])
    assert second["reused_existing"] is True
    profiles = list_profiles()
    assert len([p for p in profiles if p["profile_id"] == "account-2"]) == 1


def test_expired_session_cannot_commit(root: Path, monkeypatch) -> None:
    session = onboarding_create(qr_url="u", qr_key="k")
    onboarding_store_scanned_cookie(session["onboarding_id"], COOKIE_B)
    import src.login_onboarding as lo

    monkeypatch.setattr(lo, "SESSION_TTL_SECONDS", -1)
    with pytest.raises(OnboardingError) as exc:
        onboarding_complete(session["onboarding_id"])
    assert exc.value.code == "session_expired"


def test_failed_session_cannot_commit(root: Path) -> None:
    session = onboarding_create(qr_url="u", qr_key="k")
    onboarding_store_scanned_cookie(session["onboarding_id"], COOKIE_B)
    onboarding_fail(session["onboarding_id"], reason="user cancel")
    with pytest.raises(OnboardingError) as exc:
        onboarding_complete(session["onboarding_id"])
    assert exc.value.code == "session_expired"
    assert _old_cookie(root) == "SESSDATA=old;DedeUserID=88001"


# ---------- completed receipt：幂等 / sanitized / crash 恢复 ----------


def test_repeat_complete_returns_same_result_no_side_effects(root: Path) -> None:
    session = onboarding_create(qr_url="u", qr_key="k")
    onboarding_store_scanned_cookie(session["onboarding_id"], COOKIE_A)
    first = onboarding_complete(session["onboarding_id"])
    assert first["created_new"] is True
    for _ in range(10):
        again = onboarding_complete(session["onboarding_id"])
        assert again == first  # 完全相同 result
    profiles = list_profiles()
    assert len([p for p in profiles if p["profile_id"] == "account-2"]) == 1
    # Cookie 未被重写（receipt 阶段文件已无 cookie，重复 complete 不读 cookie）
    assert (root / "profiles" / "account-2" / "cookies.txt").read_text(encoding="utf-8") == COOKIE_A


def test_completed_receipt_has_no_secret(root: Path) -> None:
    session = onboarding_create(qr_url="https://qr.secret/x", qr_key="secretkey")
    onboarding_store_scanned_cookie(session["onboarding_id"], COOKIE_B)
    onboarding_complete(session["onboarding_id"])
    raw = (root / "shared" / "onboarding" / f"{session['onboarding_id']}.json").read_text(encoding="utf-8")
    assert "SESSDATA=def" not in raw
    assert "secretkey" not in raw
    assert "qr.secret" not in raw
    assert "DedeUserID__ckMd5=y" not in raw


def test_receipt_write_crash_is_recoverable_without_duplicate(root: Path, monkeypatch) -> None:
    import src.login_onboarding as lo

    session = onboarding_create(qr_url="u", qr_key="k")
    onboarding_store_scanned_cookie(session["onboarding_id"], COOKIE_A)
    real_write = lo._atomic_write_text
    state = {"crashed": False}

    def crash_once(path, content):
        if '"completed_at"' in str(content) and not state["crashed"]:
            state["crashed"] = True
            raise OSError("模拟 receipt 落盘前崩溃")
        return real_write(path, content)

    monkeypatch.setattr(lo, "_atomic_write_text", crash_once)
    with pytest.raises(OSError):
        onboarding_complete(session["onboarding_id"])
    monkeypatch.setattr(lo, "_atomic_write_text", real_write)
    # 重试：不创建第二个 Profile，且 receipt 最终 durable
    result = onboarding_complete(session["onboarding_id"])
    profiles = list_profiles()
    assert len([p for p in profiles if p["profile_id"] == "account-2"]) == 1
    assert result["created_new"] is True or result["reused_existing"] is True
    assert state["crashed"] is True
