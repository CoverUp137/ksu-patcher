#!/usr/bin/env python3
"""生成 Release 说明（Markdown）：KernelSU / APatch / 纯分区三种产物。"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from utils import human_size, log, ok  # noqa: E402


def file_table(files: list[dict]) -> list[str]:
    lines = ["| 文件 | 大小 | SHA256 |", "|---|---|---|"]
    for f in files:
        lines.append(f"| `{f['name']}` | {human_size(f['size'])} | `{f['sha256'][:16]}…` |")
    return lines


def main() -> int:
    parser = argparse.ArgumentParser(description="生成 Release 说明")
    parser.add_argument("--manifest", required=True, help="manifest.json 路径")
    parser.add_argument("--tag", default="", help="Release tag")
    parser.add_argument("--tool", choices=["kernelsu", "apatch", "firmware"],
                        default="kernelsu", help="产物类型")
    parser.add_argument("--version", default="", help="KernelSU / KernelPatch 版本")
    parser.add_argument("--ksu", default="", help="（兼容）KernelSU 版本")
    parser.add_argument("--apatch", default="", help="APatch 管理器版本")
    parser.add_argument("--kpimg", default="", help="kpimg 版本（APatch）")
    parser.add_argument("--superkey-mode", choices=["with", "without"], default="",
                        help="APatch：是否启用了 SuperKey 校验")
    parser.add_argument("--kmi", default="", help="KMI 版本（KernelSU）")
    parser.add_argument("--arch", default="", help="目标架构")
    parser.add_argument("--firmware", default="", help="固件直链")
    parser.add_argument("--out", required=True, help="输出文件")
    args = parser.parse_args()

    manifest_path = Path(args.manifest)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {}

    files = manifest.get("files", [])
    patched = [f for f in files if f["path"].startswith("patched/")]
    parts = [f for f in files if f["path"].startswith("partitions/")]

    version = args.version or args.ksu or manifest.get("version", "")
    apatch = args.tool == "apatch"

    lines: list[str] = []
    if args.tool == "firmware":
        lines.append("## 固件分区镜像")
        lines.append("")
        lines.append("由 GitHub Actions 自动构建：下载固件 → 解包提取分区 → 发布（未做任何修补）。")
    elif apatch:
        lines.append("## APatch 修补镜像")
        lines.append("")
        lines.append("由 GitHub Actions 自动构建：下载固件 → 解包提取分区 → "
                     "APatch（KernelPatch）修补内核 → 发布。")
    else:
        lines.append("## KernelSU 修补镜像")
        lines.append("")
        lines.append("由 GitHub Actions 自动构建：下载固件 → 解包提取分区 → "
                     "KernelSU 修补 → 发布。")
    lines.append("")
    lines.append("| 项目 | 值 |")
    lines.append("|---|---|")
    if apatch:
        if args.apatch:
            lines.append(f"| APatch 管理器 | `{args.apatch}` |")
        if version:
            lines.append(f"| KernelPatch | `{version}` |")
        if args.kpimg:
            lines.append(f"| kpimg | `{args.kpimg}` |")
    else:
        if version:
            lines.append(f"| KernelSU 版本 | `{version}` |")
        if args.kmi:
            lines.append(f"| KMI | `{args.kmi}` |")
    if args.arch:
        lines.append(f"| 目标架构 | `{args.arch}` |")
    lines.append(f"| 构建时间 | {manifest.get('generated_at', '')[:19].replace('T', ' ')} UTC |")
    lines.append("")

    if args.firmware:
        # 固件直链可能很长且带签名，放到折叠块里
        lines.append("<details><summary>固件来源</summary>")
        lines.append("")
        for url in args.firmware.replace(",", "\n").split():
            if url.strip():
                lines.append(f"- `{url.strip()}`")
        lines.append("")
        lines.append("</details>")
        lines.append("")

    if patched:
        lines.append("### 📦 已 Root 镜像（可直接刷入）")
        lines.append("")
        lines.extend(file_table(patched))
        lines.append("")

    if parts:
        lines.append("### 🗂 原始分区镜像")
        lines.append("")
        lines.extend(file_table(parts))
        lines.append("")

    lines.append("### 🔧 刷入方法")
    lines.append("")
    lines.append("```bash")
    if args.tool == "firmware":
        lines.append("adb reboot bootloader")
        lines.append("fastboot flash boot partitions/boot.img   # 文件名就是分区名")
        lines.append("fastboot reboot")
        lines.append("```")
        lines.append("")
        lines.append("> 文件名与分区一一对应（如 `init_boot.img` → `init_boot`）；")
        lines.append("> A/B 设备请按当前 slot 刷到对应槽位，刷前先备份原分区。")
    else:
        lines.append("# 确认当前 slot，A 槽为 boot_a，B 槽为 boot_b")
        lines.append("adb reboot bootloader")
        if apatch:
            lines.append("fastboot flash boot patched/boot-*.img")
        else:
            lines.append("fastboot flash boot patched/boot-*.img          # 或 init_boot")
        lines.append("fastboot reboot")
        lines.append("```")
        lines.append("")
        if apatch:
            lines.append("> APatch 修补的是**内核**，请刷入 `boot` 分区（不要用 `init_boot`）。")
            if args.superkey_mode == "without":
                lines.append("> 本次构建**未设置 SuperKey**，开机后直接打开 APatch App 即可接管 root"
                             "（不校验密钥，注意安全）。")
            else:
                lines.append("> 开机后打开 APatch App，在「安装 / 修补」里输入构建时填写的 "
                             "**SuperKey** 即可接管 root。")
            lines.append("> 首次刷入后建议保留原始 `boot` 镜像备份，异常时可回刷。")
        else:
            lines.append("> 使用 `init_boot` 修补包的设备请将 `boot` 换成 `init_boot`；")
            lines.append("> 刷入前建议先备份原分区，并确认与固件版本完全一致。")
    lines.append("")
    lines.append("### ✅ 校验")
    lines.append("")
    lines.append("```bash")
    lines.append("sha256sum -c SHA256SUMS.txt")
    lines.append("```")
    lines.append("")
    lines.append("完整清单见 `manifest.json`。")

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    ok(f"Release 说明已生成: {out} ({len(lines)} 行)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
