#!/usr/bin/env python3
"""获取 KernelSU：自动解析最新（或指定）release，下载 ksud 与 LKM 资源。

设计要点
--------
1. 默认走 GitHub API 拿 latest release；也可通过 --version 固定某个 tag。
2. v3.3.0 起每个平台的 ksud 是独立产物；v3.2.x 只在 APK 里附带
   (`lib/<abi>/libksud.so`)，无法直接在 Linux 上运行，因此这类版本
   回退到「外部 magiskboot + 旧版 ksud」或直接提示。
3. LKM (`<kmi>_kernelsu.ko`) 在新版被编译进 ksud 内部，无需单独下载；
   但为兼容旧版与便于手动指定，仍支持下载到 libs/ 目录。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from utils import (  # noqa: E402
    error,
    info,
    log,
    ok,
    run,
    set_output,
    size_of,
    summary,
    timed,
    warn,
    which,
)

API = "https://api.github.com/repos/tiann/KernelSU"
REPO = "https://github.com/tiann/KernelSU"
UA = "AKRT-KernelSU-Builder"


# --------------------------------------------------------------------------- #
# GitHub API
# --------------------------------------------------------------------------- #


def api_get(url: str) -> dict:
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "application/vnd.github+json",
    })
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.load(resp)


def get_release(version: str | None) -> dict:
    """取指定版本或最新版本的 release 信息。"""
    if version and version.lower() not in ("latest", "auto", ""):
        tag = version if version.startswith("v") else f"v{version}"
        info(f"查询指定版本: {tag}")
        return api_get(f"{API}/releases/tags/{tag}")

    info("查询最新 release ...")
    try:
        return api_get(f"{API}/releases/latest")
    except urllib.error.HTTPError as exc:  # type: ignore[attr-defined]
        if exc.code != 404:
            raise
        warn("latest 接口 404，改为遍历 release 列表取第一个正式版。")
        releases = api_get(f"{API}/releases?per_page=20")
        for rel in releases:
            if not rel.get("prerelease") and not rel.get("draft"):
                return rel
        raise SystemExit("未能找到可用的 KernelSU release")


# --------------------------------------------------------------------------- #
# 资源选择
# --------------------------------------------------------------------------- #

KSIUD_NAMES = [
    "ksud-x86_64-unknown-linux-musl",
    "ksud-x86_64-unknown-linux-gnu",
    "ksud-x86_64",
    "ksud",
]


def pick_asset(assets: list[dict], names: list[str], contains: str | None = None) -> dict | None:
    """按候选名精确匹配，失败后按关键字模糊匹配。"""
    by_name = {a["name"]: a for a in assets}
    for name in names:
        if name in by_name:
            return by_name[name]
    if contains:
        for asset in assets:
            if contains in asset["name"]:
                return asset
    return None


def pick_apk(assets: list[dict]) -> dict | None:
    for asset in assets:
        name = asset["name"]
        if name.endswith(".apk") and "release" in name.lower():
            return asset
    for asset in assets:
        if asset["name"].endswith(".apk"):
            return asset
    return None


def download_file(url: str, dest: Path, executable: bool = False) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() and dest.stat().st_size > 0:
        info(f"已存在，跳过: {dest.name} ({size_of(dest)})")
    else:
        info(f"下载 {dest.name} ...")
        run([
            "curl", "-fL", "--retry", "3", "--retry-delay", "3",
            "--connect-timeout", "30", "-o", str(dest), url,
        ])
        ok(f"{dest.name} ({size_of(dest)})")
    if executable:
        dest.chmod(0o755)
    return dest


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #


def main() -> int:
    parser = argparse.ArgumentParser(description="获取 KernelSU 与 ksud")
    parser.add_argument("--version", default="latest",
                        help="KernelSU 版本，如 v3.3.0；默认 latest 自动取最新")
    parser.add_argument("--out-dir", default="tools",
                        help="ksud 存放目录")
    parser.add_argument("--libs-dir", default="libs",
                        help="LKM / magiskboot 等辅助资源存放目录")
    parser.add_argument("--with-lkm", action="store_true",
                        help="额外下载各 KMI 的 kernelsu.ko（新版已内置于 ksud）")
    args = parser.parse_args()

    out_dir = Path(args.out_dir)
    libs_dir = Path(args.libs_dir)

    with timed("获取 KernelSU"):
        release = get_release(args.version)
        tag = release["tag_name"]
        assets = release.get("assets", [])
        log("")
        log(f"版本: {tag}")
        log(f"发布: {release.get('published_at', '')[:10]}")
        log(f"资源: {len(assets)} 个")

        ksud_asset = pick_asset(assets, KSIUD_NAMES, contains="ksud-x86_64")
        apk_asset = pick_apk(assets)

        if ksud_asset is None:
            # 旧版本没有独立 ksud：只能拿到 APK（内部为 Android 动态链接，无法直接在 runner 运行）
            warn(f"版本 {tag} 未提供独立的 Linux ksud 产物。")
            if apk_asset:
                warn(f"仅有 APK: {apk_asset['name']}；该版本无法在 Linux 上执行修补，"
                     "建议改用 v3.1.0+ 或手动提供 ksud。")
            set_output(version=tag, ksud_available="false", apk_name=apk_asset["name"] if apk_asset else "")
            summary(f"### KernelSU\n\n- 版本：`{tag}`\n- 独立 ksud：**不可用**（该版本未发布 Linux 产物）\n")
            return 2

        download_file(ksud_asset["browser_download_url"], out_dir / "ksud", executable=True)
        # 兼容旧版 ksud 需要外部 magiskboot 的情况
        lkm_assets = [a for a in assets if a["name"].endswith("_kernelsu.ko") and "/" not in a["name"]]

        if args.with_lkm and lkm_assets:
            info(f"下载 {len(lkm_assets)} 个 LKM 模块 ...")
            for asset in lkm_assets:
                # 资产名形如 android13-5.15_kernelsu.ko，落到 libs/aarch64/ 下
                name = asset["name"]
                kmi = name.replace("_kernelsu.ko", "")
                abi = "aarch64"
                target = libs_dir / abi / f"{kmi}_kernelsu.ko"
                download_file(asset["browser_download_url"], target)

    # 输出 ksud 版本信息，便于排查
    ksud_path = out_dir / "ksud"
    version_line = tag
    try:
        proc = run([str(ksud_path), "--version"], check=False, capture=True)
        if proc.returncode == 0 and proc.stdout.strip():
            version_line = proc.stdout.strip()
            ok(f"ksud 可用: {version_line}")
        else:
            warn("ksud --version 执行异常，可能是平台不匹配。")
    except Exception as exc:  # pragma: no cover
        warn(f"无法执行 ksud: {exc}")

    set_output(
        version=tag,
        ksud_version=version_line,
        ksud_path=str(ksud_path),
        ksud_available="true",
        release_url=release.get("html_url", f"{REPO}/releases/tag/{tag}"),
    )
    summary(
        f"### KernelSU\n\n"
        f"- 版本：`{tag}`\n"
        f"- ksud：`{version_line}`\n"
        f"- 发布页：{release.get('html_url', '')}\n"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
