"""讲义目录：旧讲义注入、收割、manifest 文件名规范化。"""
from __future__ import annotations

import shutil
from pathlib import Path

from . import config as cfg


def inject_prior(control_tutorial: Path, clone_tutorial: Path) -> None:
    """将上一轮的旧讲义复制进 clone 工作区，为 incremental 模式提供基础。

    控制目录不存在旧讲义时不执行任何操作。
    """
    if not control_tutorial.exists():
        return
    clone_tutorial.parent.mkdir(parents=True, exist_ok=True)
    if clone_tutorial.exists():
        shutil.rmtree(clone_tutorial)
    shutil.copytree(control_tutorial, clone_tutorial)
    cfg.log("  注入旧讲义 → clone")


def clear_clone_tutorial(clone_tutorial: Path) -> None:
    """删除 clone 中的讲义目录；full 模式从头生成前调用以清空现场。"""
    if clone_tutorial.exists():
        shutil.rmtree(clone_tutorial)


def harvest(clone_tutorial: Path, control_tutorial: Path) -> None:
    """将 clone 中新生成的讲义收割到 control 目录，采用原子替换，中断时不会丢失旧讲义。

    Windows 上 os.replace/rename 无法原子替换非空目录，因此分三步：
    复制 staging → 旧目录改名 .old → staging 改名目标 → 删除 .old。
    任一步失败均可回滚。
    """
    if not clone_tutorial.exists() or not any(clone_tutorial.iterdir()):
        raise RuntimeError(f"未生成讲义目录（或为空）：{clone_tutorial}")
    control_tutorial.parent.mkdir(parents=True, exist_ok=True)

    # staging 必须与 control 位于同一磁盘，跨盘 rename 会失败。
    staging = control_tutorial.parent / (control_tutorial.name + ".new")
    if staging.exists():
        shutil.rmtree(staging, ignore_errors=True)
    shutil.copytree(clone_tutorial, staging)

    if control_tutorial.exists():
        old = control_tutorial.parent / (control_tutorial.name + ".old")
        if old.exists():
            shutil.rmtree(old, ignore_errors=True)
        control_tutorial.rename(old)
        try:
            staging.rename(control_tutorial)
        except OSError:
            old.rename(control_tutorial)   # 失败时回滚，恢复旧目录
            raise
        try:
            shutil.rmtree(old, ignore_errors=True)
        except OSError:
            pass   # Windows 上文件偶尔被占用无法删除，下次 harvest 会再次清理
    else:
        staging.rename(control_tutorial)


def normalize_filename(fn: str) -> str:
    """filename 只接受裸文件名：丢弃目录前缀、斜杠统一为正斜杠、确保以 .md 结尾。

    防御性处理：planner 偶尔会把「项目名-tutorial/」前缀写入 filename，若不处理
    会出现双层路径（如 Mooncake-tutorial/Mooncake-tutorial/u1-l1.md）。
    """
    fn = (fn or "").replace("\\", "/").strip().strip("/").split("/")[-1]
    if fn and not fn.lower().endswith(".md"):
        fn += ".md"
    return fn or "lecture.md"


def normalize_manifest_filenames(manifest: dict) -> None:
    """将 manifest 中所有讲义的 filename 就地规范为裸 basename，写盘前调用一次即可。"""
    for u in manifest.get("units", []):
        for lec in u.get("lectures", []):
            if "filename" in lec:
                lec["filename"] = normalize_filename(lec["filename"])


def _rel_to_root(p: Path) -> str:
    """将路径转换为相对控制仓库根目录的 posix 写法；不在根目录下时回退为绝对路径。

    使用 resolve + relative_to 而非字符串前缀比较，以避免 Windows 上大小写
    与斜杠方向导致的误判。
    """
    try:
        return p.resolve().relative_to(cfg.ROOT).as_posix()
    except ValueError:
        return str(p)


def record_harvest(entry: dict, control_tutorial: Path, head: str, generated_at: str) -> None:
    """收割完成后，将讲义路径、本次对应的 HEAD 与时间记入 state，供续传和发布使用。"""
    entry["tutorial_path"] = _rel_to_root(control_tutorial)
    entry["last_head"] = head
    entry["last_run_at"] = generated_at
