#!/usr/bin/env python3
"""静态稿结构与链接检查。

检查 可视化页面/静态稿/：
- index.html 与 screens/ 存在
- 每个 screen 都被 index 引用（run-setup.html 作为重定向页除外）
- 所有本地 href/src 都能解析到真实文件
- 没有 <script>、没有外部 http(s) 资源
- 每个页面都有 viewport meta

用法：
    python -I check_static.py [--root <仓库根目录>]

退出码：0 通过；1 有错误。
"""

from __future__ import annotations

import argparse
import sys
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

DEFAULT_ROOT = Path(__file__).resolve().parents[4]
STATIC_REL = Path("可视化页面") / "静态稿"
REDIRECT_PAGES = {"run-setup.html"}
EXTERNAL_PREFIXES = ("http://", "https://", "//", "mailto:", "tel:", "javascript:", "data:")
FILE_SUFFIXES = {
    ".html", ".htm", ".css", ".js", ".mjs", ".json", ".md",
    ".svg", ".png", ".jpg", ".jpeg", ".webp", ".gif", ".ico",
    ".woff", ".woff2", ".ttf", ".otf",
}


class PageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.refs: list[tuple[str, str, str]] = []
        self.scripts: list[str] = []
        self.has_viewport = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        values = {key.lower(): (value or "") for key, value in attrs}

        if tag == "script":
            self.scripts.append(values.get("src", "(inline)"))
        if tag == "meta" and values.get("name", "").lower() == "viewport":
            self.has_viewport = True

        for attr in ("href", "src"):
            value = values.get(attr)
            if value:
                self.refs.append((tag, attr, value))


def parse_page(path: Path) -> PageParser:
    parser = PageParser()
    parser.feed(path.read_text(encoding="utf-8"))
    parser.close()
    return parser


def split_ref(ref: str) -> str:
    """去掉 query 和 fragment，返回用于解析的路径部分。"""
    return unquote(ref.split("#", 1)[0].split("?", 1)[0])


def check_refs(page: Path, parser: PageParser, errors: list[str]) -> None:
    for tag, attr, ref in parser.refs:
        if ref.startswith(EXTERNAL_PREFIXES):
            errors.append(f"{page.name}: 外部资源 <{tag} {attr}=\"{ref}\">")
            continue

        target_part = split_ref(ref)
        if not target_part:
            continue  # 页内锚点，例如 <use href="#i-run">

        target = (page.parent / target_part).resolve()
        if target.suffix.lower() in FILE_SUFFIXES and not target.exists():
            errors.append(f"{page.name}: 断链 <{tag} {attr}=\"{ref}\"> -> {target}")


def main() -> int:
    arg_parser = argparse.ArgumentParser(description="检查静态稿结构与链接")
    arg_parser.add_argument("--root", type=Path, default=DEFAULT_ROOT, help="仓库根目录")
    args = arg_parser.parse_args()

    root = args.root.resolve()
    static_dir = root / STATIC_REL
    index_path = static_dir / "index.html"
    screens_dir = static_dir / "screens"

    errors: list[str] = []

    if not static_dir.is_dir():
        print(f"找不到静态稿目录：{static_dir}")
        return 1
    if not index_path.is_file():
        errors.append(f"找不到总览页：{index_path}")
    if not screens_dir.is_dir():
        errors.append(f"找不到 screens 目录：{screens_dir}")

    if errors:
        for item in errors:
            print(f"[错误] {item}")
        return 1

    screens = sorted(screens_dir.glob("*.html"))
    all_pages = [index_path] + screens

    # 1. 每页结构检查
    for page in all_pages:
        try:
            parser = parse_page(page)
        except UnicodeDecodeError as exc:
            errors.append(f"{page.name}: 不是 UTF-8：{exc}")
            continue

        if parser.scripts:
            detail = ", ".join(parser.scripts)
            errors.append(f"{page.name}: 静态稿不应包含 <script>（{detail}）")
        if not parser.has_viewport:
            errors.append(f"{page.name}: 缺少 <meta name=\"viewport\">")
        check_refs(page, parser, errors)

    # 2. index 覆盖检查
    index_refs: set[str] = set()
    if index_path.is_file():
        index_parser = parse_page(index_path)
        for _tag, _attr, ref in index_parser.refs:
            part = split_ref(ref)
            if part.startswith("screens/") and part.endswith(".html"):
                index_refs.add(Path(part).name)

    screen_names = {page.name for page in screens}
    for name in sorted(screen_names - index_refs - REDIRECT_PAGES):
        errors.append(f"{name}: 存在于 screens/，但 index.html 没有入口")

    for name in sorted(index_refs - screen_names):
        errors.append(f"index.html: 引用了不存在的状态页 {name}")

    # 3. 输出
    redirects = screen_names - index_refs
    redirect_note = f"，重定向页 {len(redirects)} 个" if redirects else ""
    print(f"静态稿检查 · {static_dir}")
    print(f"总览：index.html")
    print(f"HTML 页面：{len(screens)} 个（index 入口 {len(index_refs)} 个{redirect_note}）")
    print()

    if errors:
        print(f"发现 {len(errors)} 个问题：")
        for item in errors[:80]:
            print(f"  - {item}")
        if len(errors) > 80:
            print(f"  ... 另有 {len(errors) - 80} 个")
        return 1

    print("通过：0 断链，0 外部资源，0 脚本，viewport 齐全。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
