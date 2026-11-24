#!/usr/bin/env python3
"""页面逻辑文档与静态稿一致性检查。

检查 可视化页面/UI设计说明.md：
- 6 个页面章节（### 3.x）存在
- 每页包含必需字段：目的 / 功能清单 / 入口与出口 / 页面结构 / 交互 / 状态 / 文案 / 验收要点
- 文档中引用的 .html 状态页与 screens/ 双向覆盖
- 静态稿/README.md 的状态表与 screens/ 双向覆盖
- 文档里的页面 / 状态计数与实际一致（如果写了计数）

用法：
    python -I check_docs.py [--root <仓库根目录>]

退出码：0 通过；1 有错误。
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

DEFAULT_ROOT = Path(__file__).resolve().parents[4]
DOC_REL = Path("可视化页面") / "UI设计说明.md"
README_REL = Path("可视化页面") / "静态稿" / "README.md"
SCREENS_REL = Path("可视化页面") / "静态稿" / "screens"

EXCLUDE = {"index.html", "run-setup.html"}
REQUIRED_FIELDS = (
    "目的",
    "功能清单",
    "入口与出口",
    "页面结构",
    "交互",
    "状态",
    "文案",
    "验收要点",
)
PAGE_HEADING_RE = re.compile(r"^### 3\.\d+\s+(.+)$", re.M)
HTML_REF_RE = re.compile(r"`(?:screens/)?([A-Za-z0-9_-]+\.html)`")
BARE_STATE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]*$")
COUNT_RE = re.compile(r"共\s*(\d+)\s*个页面[、,]\s*(\d+)\s*个单状态页面")


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def html_refs(text: str) -> set[str]:
    return {name for name in HTML_REF_RE.findall(text) if name not in EXCLUDE}


def split_page_sections(doc: str) -> list[tuple[str, str]]:
    matches = list(PAGE_HEADING_RE.finditer(doc))
    sections: list[tuple[str, str]] = []
    for index, match in enumerate(matches):
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(doc)
        body = doc[start:end]
        # 最后一个页面章节在下一个 ## 标题处结束
        next_h2 = re.search(r"^## ", body, re.M)
        if index + 1 == len(matches) and next_h2:
            body = body[: next_h2.start()]
        sections.append((match.group(1).strip(), body))
    return sections


def readme_state_refs(readme: str) -> set[str]:
    start = readme.find("## 状态清单")
    end = readme.find("## 规范对照")
    if start == -1:
        return set()
    section = readme[start : end if end != -1 else len(readme)]
    refs: set[str] = set()
    for token in re.findall(r"`([^`]+)`", section):
        token = token.strip()
        if token.endswith(".html"):
            name = Path(token).name
            if name not in EXCLUDE:
                refs.add(name)
        elif BARE_STATE_RE.fullmatch(token):
            refs.add(f"{token}.html")
    return refs


def main() -> int:
    parser = argparse.ArgumentParser(description="检查页面逻辑文档与静态稿一致性")
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT, help="仓库根目录")
    args = parser.parse_args()

    root = args.root.resolve()
    doc_path = root / DOC_REL
    readme_path = root / README_REL
    screens_dir = root / SCREENS_REL

    errors: list[str] = []

    if not doc_path.is_file():
        errors.append(f"找不到文件：{doc_path}")
    if errors:
        for item in errors:
            print(f"[错误] {item}")
        return 1

    static_available = readme_path.is_file() and screens_dir.is_dir()
    if not static_available:
        print("[提示] 未找到 静态稿/README.md 或 静态稿/screens/，跳过状态页覆盖检查。")

    doc = read(doc_path)
    readme = read(readme_path) if readme_path.is_file() else ""
    screens = {path.name for path in screens_dir.glob("*.html")} if screens_dir.is_dir() else set()
    actual_screens = screens - EXCLUDE

    # 1. 页面章节与必需字段
    sections = split_page_sections(doc)
    if not sections:
        errors.append("UI设计说明.md 没有找到 ### 3.x 页面章节")
    for title, body in sections:
        for field in REQUIRED_FIELDS:
            if f"#### {field}" not in body:
                errors.append(f"页面「{title}」缺少字段：#### {field}")
        if static_available and not html_refs(body):
            errors.append(f"页面「{title}」没有引用任何 .html 状态页")

    # 2. 文档与 screens 双向覆盖
    doc_refs = html_refs(doc)
    if static_available:
        for name in sorted(actual_screens - doc_refs):
            errors.append(f"{name}: 存在于 screens/，但 UI设计说明.md 没有引用")
        for name in sorted(doc_refs - actual_screens):
            errors.append(f"UI设计说明.md: 引用了不存在的状态页 {name}")

    # 3. README 与 screens 双向覆盖
    readme_refs = readme_state_refs(readme)
    if static_available:
        for name in sorted(actual_screens - readme_refs):
            errors.append(f"{name}: 存在于 screens/，但 静态稿/README.md 状态表没有条目")
        for name in sorted(readme_refs - actual_screens):
            errors.append(f"静态稿/README.md: 状态表引用了不存在的页面 {name}")

    # 4. 计数一致性（如果文档写了计数）
    count_match = COUNT_RE.search(doc)
    if count_match:
        page_count, state_count = int(count_match.group(1)), int(count_match.group(2))
        if page_count != len(sections):
            errors.append(f"文档写 {page_count} 个页面，实际找到 {len(sections)} 个 ### 3.x 章节")
        if static_available and state_count != len(actual_screens):
            errors.append(f"文档写 {state_count} 个单状态页面，实际 screens/ 有 {len(actual_screens)} 个")

    # 5. 输出
    print(f"页面逻辑文档检查 · {doc_path}")
    print(f"页面章节：{len(sections)} 个")
    if static_available:
        print(f"静态稿状态页：{len(actual_screens)} 个")
        print(f"文档引用：{len(doc_refs)} 个")
        print(f"README 引用：{len(readme_refs)} 个")
    print()

    if errors:
        print(f"发现 {len(errors)} 个问题：")
        for item in errors[:80]:
            print(f"  - {item}")
        if len(errors) > 80:
            print(f"  ... 另有 {len(errors) - 80} 个")
        return 1

    if static_available:
        print("通过：页面字段齐全，文档 / README / screens 双向覆盖一致。")
    else:
        print("通过：页面字段齐全；静态稿缺席，未检查状态页覆盖。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
