"""codereader 命令行入口：运行完整管线（原 scripts/analyze.py 的替代）。"""
from __future__ import annotations

import sys

from .pipeline import main as run_pipeline


def main() -> int:
    # Windows 控制台默认按 GBK 解码，打印 emoji/中文时会报错；切换到 UTF-8。
    # CI 环境本就是 UTF-8，此步骤为空操作。
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:
        pass
    return run_pipeline()


if __name__ == "__main__":
    sys.exit(main())
