#!/usr/bin/env python3
"""生成 Release 说明（Markdown）。"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from utils import human_size, log, ok  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="生成 Release 说明")
    parser.add_argument("--manifest", required=True, help="manifest.json 路径")
    parser.add_argument("--tag", default="", help="Release tag")
    parser.add_argument("--ksu", default="", help="KernelSU 版本")
    parser.add_argument("--kmi", default="", help="KMI 版本")
    parser.add_argument("--arch", default="", help="目标架构")
    parser.add_argument("--firmware", default="", help="固件直链")
    parser.add_argument("--out", required=True, help="输出文件")
    args = parser.parse_args()

    manifest_path = Path(args.manifest)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {}

    files = manifest.get("files", [])
    patched = [f for f in files if f["path"].startswith("patched/")]
    parts = [f for f in files if f["path"].startswith("partitions/")]

    lines: list[str] = []
    lines.append("## KernelSU 修补镜像")
    lines.append("")
    lines.append("由 GitHub Actions 自动构建：下载固件 → 解包提取分区 → KernelSU 修补 → 发布。")
    lines.append("")
    lines.append("| 项目 | 值 |")
    lines.append("|---|---|")
    if args.ksu:
        lines.append(f"| KernelSU 版本 | `{args.ksu}` |")
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
        lines.append("| 文件 | 大小 | SHA256 |")
        lines.append("|---|---|---|")
        for f in patched:
            lines.append(f"| `{f['name']}` | {human_size(f['size'])} | `{f['sha256'][:16]}…` |")
        lines.append("")

    if parts:
        lines.append("### 🗂 原始分区镜像")
        lines.append("")
        lines.append("| 文件 | 大小 | SHA256 |")
        lines.append("|---|---|---|")
        for f in parts:
            lines.append(f"| `{f['name']}` | {human_size(f['size'])} | `{f['sha256'][:16]}…` |")
        lines.append("")

    lines.append("### 🔧 刷入方法")
    lines.append("")
    lines.append("```bash")
    lines.append("# 确认当前 slot，A 槽为 boot_a，B 槽为 boot_b")
    lines.append("adb reboot bootloader")
    lines.append("fastboot flash boot patched/boot-*.img          # 或 init_boot")
    lines.append("fastboot reboot")
    lines.append("```")
    lines.append("")
    lines.append("> 使用 `init_boot` 修补包的设备请将 `boot` 换成 `init_boot`；")
    lines.append("> 刷入前建议先备份原分区，并确认与固件版本完全一致。")
    lines.append("")
    lines.append("### ✅ 校验")
    lines.append("")
    lines.append("```bash")
    lines.append("sha256sum -c SHA256SUMS.txt")
    lines.append("```")
    lines.append("")
    lines.append(f"完整清单见 `manifest.json`。")

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    ok(f"Release 说明已生成: {out} ({len(lines)} 行)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
