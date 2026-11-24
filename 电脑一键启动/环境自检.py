# -*- coding: utf-8 -*-
"""
校园跑 · 电脑一键启动环境自检

运行：
    python 环境自检.py
    python 环境自检.py --deep    # 真实解一次极验（不登录、不提交）

退出码：0 全部通过；1 有项目未通过。
"""

import argparse
import sys
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

import runner  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="校园跑电脑一键启动 · 环境自检")
    parser.add_argument(
        "--deep",
        action="store_true",
        help="真实调用一次自动极验（不登录、不提交）",
    )
    args = parser.parse_args()

    try:
        created = runner.ensure_config()
        if created:
            print("已生成 config.json，请先填写账号密码。")
    except Exception as exc:
        print("[FAIL] 配置模板:", exc)

    config = None
    try:
        config = runner.load_config()
        print("配置文件:", runner.CONFIG_PATH)
    except runner.ConfigError as exc:
        print("[FAIL] 配置:", exc)

    checks = runner.preflight(config, deep=args.deep)

    print("=" * 70)
    for check in checks:
        mark = "[OK]  " if check.ok else "[FAIL]"
        print("%s %s: %s" % (mark, check.name, check.detail))
    print("=" * 70)

    ok = all(check.ok for check in checks)
    if ok:
        print("全部通过。可以双击 一键跑步.pyw 开始使用。")
    else:
        print("有项目未通过，请按上面的提示处理。")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
