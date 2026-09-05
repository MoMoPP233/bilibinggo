"""代码/UI 冗余收敛 V5 静态回归：字体外链、历史 backup、调度器 UI 单一概念。"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "web" / "frontend"


def test_no_google_fonts_external_requests() -> None:
    html = (FRONTEND / "index.html").read_text(encoding="utf-8")
    assert "fonts.googleapis.com" not in html
    assert "fonts.gstatic.com" not in html
    # CSS 变量仍保留系统字体回退栈（不允许外部域名出现在任意前端资源中）。
    css = (FRONTEND / "src" / "styles" / "styles.css").read_text(encoding="utf-8")
    assert "fonts.googleapis" not in css
    assert "fonts.gstatic" not in css


def test_backup_pre_frontend_removed_without_path_references() -> None:
    assert not (ROOT / "web" / "static" / "backup_pre_frontend").exists()
    for pattern in ("*.md", "*.html", "*.py", "*.ts", "*.txt"):
        for path in ROOT.rglob(pattern):
            if (
                "node_modules" in path.parts
                or "dist" in path.parts
                or path.name == "test_frontend_cleanup_v5.py"
            ):
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            assert "backup_pre_frontend" not in text, str(path)


def test_scheduler_ui_has_only_start_stop_controls() -> None:
    html = (FRONTEND / "index.html").read_text(encoding="utf-8")
    assert 'id="auto-dock-start"' in html
    assert 'id="auto-dock-stop"' in html
    # 没有 cleanup/following 独立 toggle 入口。
    assert "auto-dock-cleanup" not in html
    assert "auto-dock-following" not in html
    # 过时“仅点击四个按钮”文案已收敛。
    assert "仅点击四个按钮" not in html


def test_scheduler_copy_and_next_concept_are_current() -> None:
    html = (FRONTEND / "index.html").read_text(encoding="utf-8")
    assert "按计划调度自动任务" in html
    auto_src = (FRONTEND / "src" / "auto" / "index.ts").read_text(encoding="utf-8")
    # 前端只消费统一的 next_task；不再展示第二套 next_slot。
    assert "next_slot" not in auto_src
    assert "next_task" in auto_src
    # backend 启动日志文案已更新为覆盖全部自动任务类型。
    scheduler = (ROOT / "web" / "auto_scheduler.py").read_text(encoding="utf-8")
    assert "仅点击 4 个按钮" not in scheduler
