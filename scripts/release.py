#!/usr/bin/env python3
"""准备发布产物：收集修补后的镜像与提取出的分区，生成校验和与清单。

产物结构
--------
release/
├── patched/            # KernelSU 修补后的镜像（root 用）
├── partitions/         # 用户指定提取的原始分区镜像
├── SHA256SUMS.txt
└── manifest.json
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from utils import (  # noqa: E402
    error,
    info,
    log,
    ok,
    set_output,
    sha256_of,
    size_of,
    summary,
    warn,
)

# 体积上限：GitHub Release 单个资产 2GiB，留出余量
MAX_ASSET_BYTES = 1900 * 1024 * 1024


def copy_into(src: Path, dest_dir: Path) -> Path | None:
    """复制文件到目标目录，超限或缺失时跳过。"""
    if not src.exists():
        warn(f"源文件不存在: {src}")
        return None
    if src.stat().st_size > MAX_ASSET_BYTES:
        warn(f"文件超过 GitHub Release 单资产上限，跳过: {src.name} ({size_of(src)})")
        return None
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / src.name
    shutil.copy2(src, dest)
    return dest


def write_sums(files: list[Path], out_dir: Path) -> Path:
    """生成 SHA256SUMS.txt。"""
    lines = []
    for path in sorted(files):
        rel = path.relative_to(out_dir)
        lines.append(f"{sha256_of(path)}  {rel.as_posix()}")
    target = out_dir / "SHA256SUMS.txt"
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return target


def main() -> int:
    parser = argparse.ArgumentParser(description="整理发布产物")
    parser.add_argument("--patched", default="", help="修补后的镜像列表，逗号分隔")
    parser.add_argument("--partitions", default="", help="提取的原始分区列表，逗号分隔")
    parser.add_argument("--out-dir", default="release", help="发布目录")
    parser.add_argument("--version", default="", help="KernelSU 版本")
    parser.add_argument("--firmware", default="", help="固件来源标识")
    parser.add_argument("--kmi", default="", help="KMI 版本")
    args = parser.parse_args()

    out_dir = Path(args.out_dir)
    patched_dir = out_dir / "patched"
    part_dir = out_dir / "partitions"
    out_dir.mkdir(parents=True, exist_ok=True)

    patched_src = [Path(p.strip()) for p in args.patched.split(",") if p.strip()]
    part_src = [Path(p.strip()) for p in args.partitions.split(",") if p.strip()]

    collected: list[Path] = []

    log("")
    log("收集修补镜像 -> release/patched/")
    for src in patched_src:
        dest = copy_into(src, patched_dir)
        if dest:
            collected.append(dest)
            ok(f"{dest.name} ({size_of(dest)})")

    log("")
    log("收集原始分区 -> release/partitions/")
    for src in part_src:
        dest = copy_into(src, part_dir)
        if dest:
            collected.append(dest)
            ok(f"{dest.name} ({size_of(dest)})")

    if not collected:
        error("没有任何可发布的产物")
        return 1

    sums = write_sums(collected, out_dir)
    total = sum(p.stat().st_size for p in collected)

    manifest = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "kernelsu_version": args.version,
        "kmi": args.kmi,
        "firmware": args.firmware,
        "patched": [p.name for p in collected if p.parent == patched_dir],
        "partitions": [p.name for p in collected if p.parent == part_dir],
        "files": [
            {
                "name": p.name,
                "path": p.relative_to(out_dir).as_posix(),
                "size": p.stat().st_size,
                "sha256": sha256_of(p),
            }
            for p in collected
        ],
        "total_size": total,
        "sha256sums": sums.name,
    }
    (out_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    log("")
    log("发布清单:")
    for item in manifest["files"]:
        log(f"  - {item['path']:<48} {size_of(out_dir / item['path']):>10}")
    log(f"  - {sums.name}")
    log(f"  - manifest.json")
    log(f"合计 {len(collected)} 个文件, {total / 1024 / 1024:.1f} MB")

    set_output(
        files="\n".join(str(p) for p in collected),
        file_count=len(collected),
        total_size=total,
        manifest=json.dumps(manifest, ensure_ascii=False),
    )

    patched_list = "\n".join(f"- `{p.name}`" for p in collected if p.parent == patched_dir)
    part_list = "\n".join(f"- `{p.name}`" for p in collected if p.parent == part_dir)
    summary(
        "### 发布产物\n\n"
        f"- KernelSU：`{args.version}`\n"
        f"- KMI：`{args.kmi}`\n"
        f"- 总大小：{total / 1024 / 1024:.1f} MB\n\n"
        + (f"**已修补（Root）**\n{patched_list}\n\n" if patched_list else "")
        + (f"**原始分区**\n{part_list}\n" if part_list else "")
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
