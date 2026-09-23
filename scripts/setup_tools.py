#!/usr/bin/env python3
"""准备 runner 上所需的全部外部工具。

安装内容
--------
- payload-dumper-go : 解析 OTA 里的 payload.bin
- lpunpack          : 拆分 super.img 动态分区
- magiskboot        : 从 Magisk APK 中提取，用于检查/解包镜像
- simg2img 等       : 通过 apt 的 android-sdk-libsparse-utils 安装
- 7z / cpio / file  : 解压与检查用
"""
from __future__ import annotations

import argparse
import json
import sys
import tarfile
import urllib.request
import zipfile
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

UA = "AKRT-tool-setup"
TOOLS_DIR = Path("tools")

PDG_API = "https://api.github.com/repos/ssut/payload-dumper-go/releases/latest"
MAGISK_API = "https://api.github.com/repos/topjohnwu/Magisk/releases/latest"
# 静态链接的 lpunpack，避免 runner 上的 glibc 差异
LPUNPACK_URL = (
    "https://raw.githubusercontent.com/ravindu644/"
    "android-lptools-static-x86_64/main/lpunpack"
)

APT_PACKAGES = [
    "android-sdk-libsparse-utils",  # simg2img / img2simg / simg_dump
    "p7zip-full",                   # 7z
    "cpio",
    "file",
    "zstd",
    "lz4",
    "xz-utils",
    "unzip",
    "aria2",
]


def api_get(url: str) -> dict:
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "application/vnd.github+json",
    })
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.load(resp)


def download(url: str, dest: Path) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    info(f"下载 {dest.name} ...")
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=300) as resp, open(dest, "wb") as fh:
        while chunk := resp.read(1 << 20):
            fh.write(chunk)
    ok(f"{dest.name} ({size_of(dest)})")
    return dest


def install_apt() -> None:
    """安装 apt 包（尽量批量、静默）。"""
    if not which("apt-get"):
        warn("非 Debian/Ubuntu 环境，跳过 apt 安装。")
        return
    run(["sudo", "apt-get", "update", "-qq"], check=False)
    run(
        ["sudo", "apt-get", "install", "-y", "-qq", *APT_PACKAGES],
        check=False,
    )
    for tool in ("simg2img", "7z", "cpio"):
        if which(tool):
            ok(f"{tool}: {which(tool)}")
        else:
            warn(f"{tool} 未安装成功")


def install_payload_dumper_go() -> None:
    if which("payload-dumper-go"):
        ok("payload-dumper-go 已存在")
        return
    release = api_get(PDG_API)
    tag = release["tag_name"]
    asset = next(
        (a for a in release["assets"]
         if "linux_amd64" in a["name"] and a["name"].endswith(".tar.gz")
         and "avx2" not in a["name"]),
        None,
    )
    if not asset:
        warn(f"未找到 linux_amd64 资源 (tag={tag})")
        return
    archive = download(asset["browser_download_url"], Path("/tmp/pdg.tar.gz"))
    with tarfile.open(archive) as tar:
        for member in tar.getmembers():
            if member.name.endswith("payload-dumper-go"):
                member.name = "payload-dumper-go"
                tar.extract(member, TOOLS_DIR)
                break
    binary = TOOLS_DIR / "payload-dumper-go"
    binary.chmod(0o755)
    ok(f"payload-dumper-go {tag} -> {binary}")


def install_lpunpack() -> None:
    dest = TOOLS_DIR / "lpunpack"
    if dest.exists():
        ok("lpunpack 已存在")
        return
    try:
        download(LPUNPACK_URL, dest)
        dest.chmod(0o755)
        run([str(dest)], check=False, capture=True)  # 无参数会打印用法，返回非零属正常
        ok(f"lpunpack -> {dest}")
    except Exception as exc:
        warn(f"lpunpack 安装失败: {exc}")


def install_magiskboot() -> None:
    """从 Magisk APK 中提取 magiskboot。"""
    dest = TOOLS_DIR / "magiskboot"
    if dest.exists():
        ok("magiskboot 已存在")
        return
    release = api_get(MAGISK_API)
    tag = release["tag_name"]
    asset = next(
        (a for a in release["assets"]
         if a["name"].endswith(".apk") and "debug" not in a["name"]),
        None,
    )
    if not asset:
        warn("未找到 Magisk APK 资源")
        return
    apk = download(asset["browser_download_url"], Path("/tmp/magisk.apk"))
    with zipfile.ZipFile(apk) as zf:
        member = next(
            (n for n in zf.namelist()
             if n.endswith("libmagiskboot.so") and "x86_64" in n),
            None,
        )
        if not member:
            warn("APK 中未找到 x86_64 的 magiskboot")
            return
        dest.write_bytes(zf.read(member))
    dest.chmod(0o755)
    ok(f"magiskboot {tag} -> {dest}")


def verify() -> dict[str, str]:
    """汇总工具可用状态。"""
    log("")
    log("工具状态:")
    status: dict[str, str] = {}
    for name in ("payload-dumper-go", "lpunpack", "magiskboot", "simg2img", "7z", "cpio", "file"):
        path = (TOOLS_DIR / name) if (TOOLS_DIR / name).is_file() else which(name)
        status[name] = str(path) if path else "MISSING"
        mark = "✓" if path else "✗"
        log(f"  {mark} {name:<20} {status[name]}")
    return status


def main() -> int:
    ap = argparse.ArgumentParser(description="安装解包/修补所需工具")
    ap.add_argument("--skip-apt", action="store_true", help="跳过 apt 安装")
    ap.add_argument("--skip-magiskboot", action="store_true", help="跳过 magiskboot")
    args = ap.parse_args()

    TOOLS_DIR.mkdir(parents=True, exist_ok=True)

    with timed("安装工具"):
        if not args.skip_apt:
            install_apt()
        install_payload_dumper_go()
        install_lpunpack()
        if not args.skip_magiskboot:
            install_magiskboot()

    status = verify()
    missing = [k for k, v in status.items() if v == "MISSING"]
    if missing:
        warn(f"缺失工具: {', '.join(missing)}")

    set_output(tools_status=json.dumps(status))
    summary(
        "### 工具准备\n\n| 工具 | 状态 |\n|---|---|\n"
        + "\n".join(f"| `{k}` | {v} |" for k, v in status.items())
        + "\n"
    )
    # payload-dumper-go 缺失会直接影响 OTA 解包，视为致命
    if status.get("payload-dumper-go") == "MISSING":
        error("payload-dumper-go 缺失，无法解析 OTA 包")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
