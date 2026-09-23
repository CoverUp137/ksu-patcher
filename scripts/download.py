#!/usr/bin/env python3
"""下载固件：支持多个直链、断点续传，自动选用 aria2c 或 curl。"""
from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from utils import (  # noqa: E402
    CommandError,
    error,
    group,
    info,
    log,
    mask,
    ok,
    run,
    set_output,
    size_of,
    summary,
    timed,
    warn,
    which,
)

SPLIT_RE = re.compile(r"[\s,]+")


def parse_urls(raw: str) -> list[str]:
    """把换行/逗号分隔的输入拆成 URL 列表，过滤空项与注释。"""
    urls: list[str] = []
    for token in SPLIT_RE.split(raw.strip()):
        token = token.strip()
        if not token or token.startswith("#"):
            continue
        urls.append(token)
    return urls


def filename_for(url: str, index: int) -> str:
    """从 URL 推导文件名；无法推导时退化为 firmware_<index>.bin。"""
    parsed = urllib.parse.urlparse(url)
    name = urllib.parse.unquote(Path(parsed.path).name).split("?")[0]
    return name or f"firmware_{index}.bin"


def download_curl(url: str, dest: Path) -> None:
    """用 curl/wget 下载，支持断点续传与自动重试。"""
    tool = which("curl") or which("wget")
    if not tool:
        raise SystemExit("需要 curl 或 wget 之一")
    if Path(tool).name == "curl":
        cmd = [
            "curl", "-fL",
            "--retry", "5", "--retry-delay", "5", "--retry-all-errors",
            "--connect-timeout", "30",
            "-C", "-",                 # 断点续传
            "--progress-bar",
            "-o", str(dest), url,
        ]
    else:
        cmd = ["wget", "-c", "--tries=5", "--timeout=30", "-O", str(dest), url]

    try:
        run(cmd)
    except CommandError:
        if dest.exists() and dest.stat().st_size > 0:
            warn("下载命令返回非零，但文件已存在，继续校验。")
        else:
            raise


def download_aria2(url: str, dest: Path, connections: int) -> None:
    """用 aria2c 多线程下载。"""
    run([
        "aria2c",
        "-x", str(connections), "-s", str(connections), "-k", "1M",
        "--file-allocation=none",
        "--continue=true",
        "--max-tries=5", "--retry-wait=5",
        "--console-log-level=warn", "--summary-interval=30",
        "--allow-overwrite=true", "--auto-file-renaming=false",
        "-d", str(dest.parent), "-o", dest.name,
        url,
    ])


def download(url: str, dest: Path, connections: int, use_aria2: bool) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if use_aria2 and which("aria2c"):
        download_aria2(url, dest, connections)
    else:
        download_curl(url, dest)

    if not dest.exists() or dest.stat().st_size == 0:
        raise SystemExit(f"下载失败，未生成有效文件: {dest}")
    ok(f"{dest.name} ({size_of(dest)})")


def main() -> int:
    parser = argparse.ArgumentParser(description="下载固件")
    parser.add_argument("--urls", required=True, help="固件直链，多个用换行或逗号分隔")
    parser.add_argument("--out-dir", default="downloads", help="保存目录")
    parser.add_argument("--connections", type=int, default=8, help="aria2c 并发连接数")
    parser.add_argument("--no-aria2", action="store_true", help="强制使用 curl")
    args = parser.parse_args()

    urls = parse_urls(args.urls)
    if not urls:
        error("未提供有效的下载链接")
        return 1

    for url in urls:
        # 链接里可能带签名 token，提前在日志中遮蔽
        mask(url)

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    log(f"共 {len(urls)} 个链接，输出目录: {out_dir}")

    files: list[Path] = []
    with timed("下载固件"):
        for index, url in enumerate(urls, start=1):
            dest = out_dir / filename_for(url, index)
            if dest.exists() and dest.stat().st_size > 0:
                info(f"已存在，跳过下载: {dest.name} ({size_of(dest)})")
            else:
                info(f"[{index}/{len(urls)}] 开始下载: {dest.name}")
                with group(f"下载 {dest.name}"):
                    download(url, dest, args.connections, not args.no_aria2)
            files.append(dest)

    total = sum(f.stat().st_size for f in files)
    log("")
    log("下载结果:")
    for f in files:
        log(f"  - {f.name}  {size_of(f)}")
    log(f"合计 {len(files)} 个文件, {total / 1024 / 1024:.1f} MB")

    set_output(files_json=json.dumps([str(f) for f in files]), count=len(files))
    summary(
        f"### 固件下载\n\n"
        f"- 文件数：{len(files)}\n"
        f"- 总大小：{total / 1024 / 1024:.1f} MB\n"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
