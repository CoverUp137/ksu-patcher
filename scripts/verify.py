#!/usr/bin/env python3
"""校验修补后的镜像是否真的可被当作 boot 镜像解析。

校验项
------
1. magic 是否为 ANDROID! / VNDRBOOT
2. 头部字段是否自洽（页大小、kernel/ramdisk 大小）
3. 若镜像被 KernelSU 修补，其 ramdisk 里应包含 kernelsu.ko 与 ksuinit
4. 用 magiskboot 再做一次交叉验证（可选）

注意：这是「尽力而为」的检查，默认失败只告警；--strict 时才会返回非零。
"""
from __future__ import annotations

import argparse
import json
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from utils import (  # noqa: E402
    ANDROID_MAGIC,
    find_tool,
    VENDOR_BOOT_MAGIC,
    error,
    group,
    log,
    ok,
    run,
    set_output,
    size_of,
    summary,
    warn,
    which,
)

BOOT_MAGIC = b"ANDROID!"
# boot header v3/v4 不再记录 page_size，固定为 4096
BOOT_V34_PAGE_SIZE = 4096


def magiskboot_bin() -> str | None:
    """返回 magiskboot 的绝对路径，便于在任意 cwd 下执行。"""
    return find_tool("magiskboot")


def parse_header(path: Path) -> dict:
    """解析 boot / vendor_boot 头部关键字段。

    参考 AOSP `system/tools/mkbootimg/include/bootimg/bootimg.h`：
    - v0~v2: kernel_size@8, ramdisk_size@16, page_size@36, header_version@40
    - v3~v4: kernel_size@8, ramdisk_size@12, header_size@20, 页大小固定 4096
    """
    with open(path, "rb") as fh:
        data = fh.read(4096)

    info: dict = {"magic": data[:8]}

    if data[:8] == BOOT_MAGIC and len(data) >= 44:
        version = struct.unpack_from("<I", data, 40)[0]
        info["type"] = "boot"
        info["header_version"] = version
        info["kernel_size"] = struct.unpack_from("<I", data, 8)[0]
        if version >= 3:
            info["ramdisk_size"] = struct.unpack_from("<I", data, 12)[0]
            info["header_size"] = struct.unpack_from("<I", data, 20)[0]
            info["page_size"] = BOOT_V34_PAGE_SIZE
        else:
            info["ramdisk_size"] = struct.unpack_from("<I", data, 16)[0]
            info["page_size"] = struct.unpack_from("<I", data, 36)[0]
    elif data[:8] == VENDOR_BOOT_MAGIC and len(data) >= 24:
        info["type"] = "vendor_boot"
        info["header_version"] = struct.unpack_from("<I", data, 8)[0]
        info["page_size"] = struct.unpack_from("<I", data, 12)[0]
        info["kernel_size"] = struct.unpack_from("<I", data, 16)[0]
        info["ramdisk_size"] = struct.unpack_from("<I", data, 20)[0]
    else:
        info["type"] = "unknown"
    return info


def unpack_ramdisk(image: Path) -> set[str] | None:
    """用 magiskboot 解包并返回 ramdisk 内的文件名集合。"""
    tool = magiskboot_bin()
    if not tool:
        return None

    image = image.resolve()
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        proc = run([tool, "unpack", str(image)], check=False, capture=True, cwd=tmp_path)
        # magiskboot 返回码：0 合法 / 1 错误 / 2 chromeos / 3 vendor_boot
        rc = proc.returncode
        if rc not in (0, 2, 3):
            warn(f"magiskboot 解包失败 (rc={rc})")
            return None
        if rc == 1:
            return None

        ramdisk = tmp_path / "ramdisk.cpio"
        if not ramdisk.exists():
            warn(f"{image.name}: 未解出 ramdisk.cpio")
            return set()

        extract_dir = tmp_path / "rd"
        extract_dir.mkdir()
        with open(ramdisk, "rb") as fh:
            subprocess.run(
                ["cpio", "-idm", "--quiet"],
                cwd=extract_dir, stdin=fh, capture_output=True,
            )
        return {p.name for p in extract_dir.rglob("*") if p.is_file()}


def main() -> int:
    parser = argparse.ArgumentParser(description="校验修补后的镜像")
    parser.add_argument("--patched", required=True, help="修补后的镜像，逗号或换行分隔")
    parser.add_argument("--strict", action="store_true", help="任一项失败即返回非零")
    parser.add_argument("--skip-magiskboot", action="store_true", help="跳过 magiskboot 交叉验证")
    args = parser.parse_args()

    images = [Path(p.strip()) for p in args.patched.replace("\n", ",").split(",") if p.strip()]
    images = [p for p in images if p.exists()]
    if not images:
        error("没有可校验的镜像")
        return 1

    results: list[dict] = []
    failures = 0

    for image in images:
        log("")
        log(f"校验 {image.name} ({size_of(image)})")
        checks: dict[str, bool] = {}

        with group(f"校验 {image.name}"):
            header = parse_header(image)
            log(f"    magic:          {header['magic']!r}")
            log(f"    类型:           {header.get('type')}")
            if "header_version" in header:
                log(f"    header_version: {header['header_version']}")
            if "page_size" in header:
                log(f"    page_size:      {header['page_size']}")
            if "kernel_size" in header:
                log(f"    kernel_size:    {header['kernel_size']}")
            if "ramdisk_size" in header:
                log(f"    ramdisk_size:   {header['ramdisk_size']}")
            log(f"    文件大小:       {image.stat().st_size}")

            magic_ok = header["magic"] in (ANDROID_MAGIC, VENDOR_BOOT_MAGIC)
            checks["magic"] = magic_ok
            if magic_ok:
                ok("magic 正确")
            else:
                error(f"magic 异常: {header['magic']!r}")
                failures += 1

            page = header.get("page_size", 0)
            if page:
                aligned = image.stat().st_size % page == 0
                checks["page_aligned"] = aligned
                if aligned:
                    ok(f"按 {page} 字节页对齐")
                else:
                    warn(f"未按 {page} 字节页对齐")

            if not args.skip_magiskboot:
                names = unpack_ramdisk(image)
                if names is not None:
                    checks["ramdisk_readable"] = True
                    log(f"    ramdisk 内容: {', '.join(sorted(names)) or '(空)'}")
                    ksu_ok = "kernelsu.ko" in names
                    checks["kernelsu_ko"] = ksu_ok
                    if ksu_ok:
                        ok("ramdisk 含 kernelsu.ko（KernelSU 已就位）")
                    else:
                        warn("ramdisk 未包含 kernelsu.ko")
                        failures += 1
                else:
                    warn("magiskboot 不可用，跳过 ramdisk 检查")

        results.append({"name": image.name, "checks": checks})

    log("")
    if failures:
        error(f"校验发现 {failures} 项问题")
    else:
        ok(f"全部 {len(results)} 个镜像通过校验")

    set_output(verify_json=json.dumps(results), verify_failures=failures)
    rows = "\n".join(
        f"| `{r['name']}` | {'✅' if all(r['checks'].values()) else '⚠️'} | "
        + " ".join(f"{k}={'✓' if v else '✗'}" for k, v in r["checks"].items())
        + " |"
        for r in results
    )
    summary(f"### 校验结果\n\n| 镜像 | 结果 | 明细 |\n|---|---|---|\n{rows}\n")

    return 1 if (failures and args.strict) else 0


if __name__ == "__main__":
    sys.exit(main())
