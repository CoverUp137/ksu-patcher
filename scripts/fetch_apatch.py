#!/usr/bin/env python3
"""获取 APatch 修补所需的 KernelPatch 工具链：kptools + kpimg。

设计要点
--------
1. APatch 管理器本身只是一个 App，命令行修补依赖 KernelPatch 提供的
   `kptools`（卸载/打补丁/重打包）与 `kpimg`（注入内核的补丁镜像）。
2. 版本默认跟随 APatch 上游：读取其 `app/src/main/cpp/version`，
   这个文件就是 APatch 当前打包的 KernelPatch 版本，二者必须一致，
   否则 App 可能认不出补丁（kpimg 版本不匹配）。
3. 同时解析最新的 APatch 管理器版本号，仅用于产物命名与说明。
4. 官方已提供 Linux x86_64 版 `kptools-linux`，可直接在 runner 上运行。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from utils import (  # noqa: E402
    error,
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
)

UA = "AKRT-APatch-Builder"
API_APATCH = "https://api.github.com/repos/bmax121/APatch"
API_KP = "https://api.github.com/repos/bmax121/KernelPatch"
KP_VERSION_FILE = (
    "https://raw.githubusercontent.com/bmax121/APatch/{ref}/app/src/main/cpp/version"
)

KPTOOLS_ASSET = "kptools-linux"
KPIMG_ASSET = "kpimg-android"


# --------------------------------------------------------------------------- #
# 网络
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


def fetch_text(url: str) -> str | None:
    """抓取纯文本（用于读取 APatch 的 version 头文件）。"""
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except Exception as exc:
        warn(f"读取失败: {url} ({exc})")
        return None


def latest_release(api: str, label: str) -> dict:
    """取 latest release；404 时退化为遍历列表取第一个正式版。"""
    try:
        return api_get(f"{api}/releases/latest")
    except urllib.error.HTTPError as exc:  # type: ignore[attr-defined]
        if exc.code != 404:
            raise
        warn(f"{label} 的 latest 接口 404，改为遍历 release 列表。")
        releases = api_get(f"{api}/releases?per_page=20")
        for rel in releases:
            if not rel.get("prerelease") and not rel.get("draft"):
                return rel
        raise SystemExit(f"未能找到可用的 {label} release")


# --------------------------------------------------------------------------- #
# 版本解析
# --------------------------------------------------------------------------- #


def parse_kp_version(text: str) -> str | None:
    """从 `#define MAJOR/MINOR/PATCH` 头文件解析出 0.13.9 这样的版本号。"""
    parts = {}
    for name in ("MAJOR", "MINOR", "PATCH"):
        match = re.search(rf"#define\s+{name}\s+(\d+)", text)
        if not match:
            return None
        parts[name] = match.group(1)
    return f"{parts['MAJOR']}.{parts['MINOR']}.{parts['PATCH']}"


def normalise_tag(raw: str) -> str:
    """把 v1.2.3 / 1.2.3 统一成 tag 形式。"""
    raw = raw.strip()
    return raw[1:] if raw.lower().startswith("v") else raw


def resolve_apatch(version: str) -> str:
    """解析 APatch 管理器版本（仅用于命名/说明）。"""
    if not version or version.lower() in ("latest", "auto"):
        rel = latest_release(API_APATCH, "APatch")
        return rel["tag_name"]
    return normalise_tag(version)


def resolve_kp_version(mode: str, apatch_tag: str) -> tuple[str, str]:
    """解析 KernelPatch 版本，返回 (版本号, 来源说明)。

    优先级：
      1. 用户显式指定（0.13.9）
      2. auto：读取 APatch 上游 version 头文件（本地打包的 KP 版本）
      3. latest：KernelPatch 最新 release
    """
    if mode and mode.lower() not in ("auto", "latest"):
        return normalise_tag(mode), "用户指定"

    if not mode or mode.lower() == "auto":
        # 优先用被固定 APatch 版本对应的头文件，保证与 App 配套
        for ref in (apatch_tag, "main"):
            text = fetch_text(KP_VERSION_FILE.format(ref=ref))
            if not text:
                continue
            version = parse_kp_version(text)
            if version:
                ok(f"APatch {ref} 打包的 KernelPatch 版本: {version}")
                return version, f"跟随 APatch（{ref}）"
        warn("无法从 APatch 上游读取 KernelPatch 版本，回退到最新 release。")

    rel = latest_release(API_KP, "KernelPatch")
    return rel["tag_name"].lstrip("v"), "KernelPatch 最新 release"


def kp_release(version: str) -> dict:
    """按 tag 取 KernelPatch release。"""
    try:
        return api_get(f"{API_KP}/releases/tags/{version}")
    except urllib.error.HTTPError as exc:  # type: ignore[attr-defined]
        if exc.code != 404:
            raise
        warn(f"KernelPatch 未发布 {version}，回退到最新 release。")
        return latest_release(API_KP, "KernelPatch")


def pick_asset(assets: list[dict], name: str) -> dict | None:
    for asset in assets:
        if asset["name"] == name:
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
# kpimg / kptools 信息
# --------------------------------------------------------------------------- #


def parse_ini(text: str) -> dict[str, dict[str, str]]:
    """解析 kptools 输出的 INI 风格内容（[section] + key=value）。"""
    sections: dict[str, dict[str, str]] = {}
    current: dict[str, str] | None = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith(("[+]", "[?]", "[-]", "#")):
            continue
        if line.startswith("[") and line.endswith("]"):
            current = sections.setdefault(line[1:-1].strip(), {})
            continue
        if current is not None and "=" in line:
            key, _, value = line.partition("=")
            current[key.strip()] = value.strip()
    return sections


def kpimg_info(kptools: Path, kpimg: Path) -> dict[str, str]:
    """读取 kpimg 自述信息，确认架构与版本。"""
    proc = run([str(kptools), "-l", "-k", str(kpimg)], check=False, capture=True)
    if proc.returncode != 0:
        raise SystemExit("kptools 无法识别 kpimg，文件可能损坏")
    sections = parse_ini(proc.stdout)
    if "kpimg" not in sections:
        raise SystemExit(f"kpimg 信息解析失败：\n{proc.stdout}\n{proc.stderr}")
    return sections["kpimg"]


def kptools_version(kptools: Path) -> str:
    proc = run([str(kptools), "--version"], check=False, capture=True)
    text = (proc.stdout or proc.stderr).strip()
    match = re.search(r"version:\s*([0-9A-Za-z._-]+)", text)
    return match.group(1) if match else text.splitlines()[0] if text else "unknown"


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #


def main() -> int:
    parser = argparse.ArgumentParser(description="获取 APatch（KernelPatch）修补工具链")
    parser.add_argument("--kp-version", default="auto",
                        help="KernelPatch 版本：auto（跟随 APatch 上游）/ latest / 0.13.9")
    parser.add_argument("--apatch-version", default="latest",
                        help="APatch 管理器版本，仅用于命名与说明；默认 latest")
    parser.add_argument("--out-dir", default="tools", help="kptools / kpimg 存放目录")
    parser.add_argument("--libs-dir", default="libs",
                        help="可选：<kmi>_kernelpatch.ko 存放目录")
    parser.add_argument("--with-ko", action="store_true",
                        help="额外下载各 KMI 的 kernelpatch.ko（仅供 late-load 参考）")
    args = parser.parse_args()

    out_dir = Path(args.out_dir)
    libs_dir = Path(args.libs_dir)

    with timed("获取 APatch / KernelPatch"):
        apatch_tag = resolve_apatch(args.apatch_version)
        kp_version, source = resolve_kp_version(args.kp_version, apatch_tag)
        info(f"APatch 管理器: {apatch_tag}")
        info(f"KernelPatch: {kp_version}（{source}）")

        release = kp_release(kp_version)
        tag = release["tag_name"]
        assets = release.get("assets", [])
        log("")
        log(f"KernelPatch release: {tag}（{len(assets)} 个资源）")

        kptools_asset = pick_asset(assets, KPTOOLS_ASSET)
        kpimg_asset = pick_asset(assets, KPIMG_ASSET)
        if not kptools_asset:
            error(f"该版本未提供 {KPTOOLS_ASSET}，无法在 Linux runner 上修补。")
            error("请改用已发布 Linux 工具的 KernelPatch 版本（如 0.13.9）。")
            return 2
        if not kpimg_asset:
            error(f"该版本未提供 {KPIMG_ASSET}。")
            return 2

        kptools = download_file(kptools_asset["browser_download_url"],
                               out_dir / "kptools", executable=True)
        kpimg = download_file(kpimg_asset["browser_download_url"], out_dir / "kpimg")

        if args.with_ko:
            kos = [a for a in assets if a["name"].endswith("_kernelpatch.ko")]
            info(f"下载 {len(kos)} 个 kernelpatch.ko ...")
            for asset in kos:
                kmi = asset["name"].replace("_kernelpatch.ko", "")
                download_file(asset["browser_download_url"],
                              libs_dir / "aarch64" / f"{kmi}_kernelpatch.ko")

    # 校验工具链
    log("")
    tool_version = kptools_version(kptools)
    ok(f"kptools 可用: {tool_version}")

    kpimg_meta = kpimg_info(kptools, kpimg)
    kpimg_ver = kpimg_meta.get("version", "")
    kpimg_arch = kpimg_meta.get("arch", "")
    kpimg_config = kpimg_meta.get("config", "")
    log("")
    log("kpimg 信息:")
    log(f"  version      {kpimg_ver}")
    log(f"  arch         {kpimg_arch}")
    log(f"  config       {kpimg_config}")
    log(f"  compile_time {kpimg_meta.get('compile_time', '')}")

    if kpimg_arch and kpimg_arch not in ("arm64", "aarch64"):
        error(f"kpimg 架构为 {kpimg_arch}，Android 设备需要 arm64 版本。")
        return 1
    if "android" not in kpimg_config:
        warn(f"kpimg 配置为 {kpimg_config}，非 Android 版本，设备上可能无法启动。")

    # 打码：superkey 的哈希值不建议出现在公开日志里
    for key in ("root_superkey", "superkey"):
        value = kpimg_meta.get(key, "")
        if value and value not in ("<redacted>", ""):
            mask(value)

    set_output(
        apatch_version=apatch_tag,
        kernelpatch_version=tag.lstrip("v"),
        kptools_version=tool_version,
        kpimg_version=kpimg_ver,
        kpimg_arch=kpimg_arch,
        kpimg_config=kpimg_config,
        version_source=source,
        kptools_path=str(kptools.resolve()),
        kpimg_path=str(kpimg.resolve()),
    )

    summary(
        "### APatch 工具链\n\n"
        f"- APatch 管理器：`{apatch_tag}`\n"
        f"- KernelPatch：`{tag}`（{source}）\n"
        f"- kptools：`{tool_version}`\n"
        f"- kpimg：`{kpimg_ver}`（{kpimg_arch}, {kpimg_config}）\n"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
