#!/usr/bin/env python3
"""解包固件并提取指定分区镜像。

支持的固件形态
--------------
1. OTA zip（含 payload.bin）  -> payload-dumper-go 直接从 zip 内解析，无需整包解压
2. factory / fastboot 包（含 *.img） -> 解压后直接抽取
3. 单独一个 .img              -> 直接使用
4. super.img（动态分区）       -> lpunpack 二次拆分

提取结果会输出分区清单，供后续步骤与 Release 使用。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from utils import (  # noqa: E402
    error,
    find_tool,
    find_files,
    group,
    info,
    is_sparse,
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

IMG_SUFFIXES = (".img", ".bin")


# --------------------------------------------------------------------------- #
# 工具定位
# --------------------------------------------------------------------------- #


def tool_path(name: str) -> str | None:
    """查找可执行工具（仓库 tools/ 优先，其次 PATH）。"""
    return find_tool(name)


# --------------------------------------------------------------------------- #
# 探测与解包
# --------------------------------------------------------------------------- #


def list_archive(path: Path) -> list[str]:
    """列出压缩包内的文件路径（不解压）。"""
    seven = tool_path("7z") or tool_path("7za")
    if seven:
        try:
            proc = run([seven, "l", "-ba", "-slt", str(path)],
                       check=False, capture=True)
            names = [
                line.split("=", 1)[1].strip()
                for line in proc.stdout.splitlines()
                if line.startswith("Path = ")
            ]
            if names:
                return names[1:] if len(names) > 1 else names  # 首行是包自身
        except Exception as exc:
            warn(f"7z 列举失败: {exc}")
    try:
        proc = run(["unzip", "-Z1", str(path)], check=False, capture=True)
        return [n.strip() for n in proc.stdout.splitlines() if n.strip()]
    except Exception as exc:
        warn(f"unzip 列举失败: {exc}")
        return []


def extract_archive(archive: Path, dest: Path) -> None:
    """完整解压压缩包。"""
    dest.mkdir(parents=True, exist_ok=True)
    seven = tool_path("7z") or tool_path("7za")
    if seven:
        run([seven, "x", "-y", f"-o{dest}", str(archive)])
    else:
        run(["unzip", "-o", "-q", str(archive), "-d", str(dest)])


def dump_payload(source: Path, dest: Path, partitions: list[str] | None) -> list[Path]:
    """用 payload-dumper-go 提取分区。

    source 既可以是 payload.bin，也可以直接是含 payload.bin 的 OTA zip
    （后者可省去一次 GB 级解压）。
    """
    pdg = tool_path("payload-dumper-go")
    if not pdg:
        raise SystemExit("未找到 payload-dumper-go，无法解析 payload")

    dest.mkdir(parents=True, exist_ok=True)

    # 先取 payload 内实际存在的分区，剔除不存在的
    # （例如 Android 12 的设备没有 init_boot，若不剔除会导致整步失败）
    available = list_payload_partitions(source)
    wanted = list(partitions) if partitions else None

    if wanted and available:
        missing = [p for p in wanted if p not in available]
        usable = [p for p in wanted if p in available]
        for name in missing:
            warn(f"payload 中不存在分区 '{name}'，已跳过。")
        if not usable:
            error(f"请求的分区在该 payload 中都不存在: {', '.join(wanted)}")
            error(f"payload 实际包含 {len(available)} 个分区，例如: "
                  f"{', '.join(available[:12])}{' ...' if len(available) > 12 else ''}")
            return []
        wanted = usable

    cmd = [pdg, "-o", str(dest), "-c", "4"]
    if wanted:
        cmd += ["-p", ",".join(wanted)]
    cmd.append(str(source))

    if run(cmd, check=False).returncode != 0:
        warn("payload-dumper-go 执行失败")
        return []

    return [p for p in dest.rglob("*") if p.is_file() and p.suffix in IMG_SUFFIXES]


def list_payload_partitions(source: Path) -> list[str]:
    """列出 payload 中的分区名。

    payload-dumper-go -l 的输出形如::

        payload.bin: /path/to/ota.zip
        Payload Version: 2
        Payload Manifest Length: 166146
        Found partitions:
        abl (217 kB), aop (246 kB), boot (201 MB), ...

    分区清单位于 "Found partitions:" 之后，且可能跨多行。
    """
    pdg = tool_path("payload-dumper-go")
    if not pdg:
        return []

    proc = run([pdg, "-l", str(source)], check=False, capture=True)
    if proc.returncode != 0:
        warn("列举 payload 分区失败")
        return []

    names: list[str] = []
    collecting = False
    for line in proc.stdout.splitlines():
        stripped = line.strip()
        if stripped.startswith("Found partitions"):
            collecting = True
            continue
        if not collecting:
            continue
        # 形如 "boot (201 MB), init_boot (4.0 MB)"
        names.extend(re.findall(r"([A-Za-z0-9_.]+)\s*\(", stripped))
    return names


def unpack_super(super_img: Path, dest: Path, wanted: list[str]) -> list[Path]:
    """用 lpunpack 展开 super.img（动态分区）。"""
    lpunpack = tool_path("lpunpack")
    if not lpunpack:
        warn("未找到 lpunpack，跳过 super.img 拆分。")
        return []

    dest.mkdir(parents=True, exist_ok=True)
    cmd = [lpunpack]
    for name in wanted:
        # lpunpack 的分区名带 _a/_b 后缀及 _simg 变体，逐个尝试
        for slot in ("", "_a", "_b"):
            cmd += ["-p", f"{name}{slot}"]
    cmd += [str(super_img), str(dest)]
    try:
        run(cmd)
    except Exception as exc:
        warn(f"lpunpack 执行失败: {exc}")
        return []
    return [p for p in dest.rglob("*") if p.is_file() and p.suffix in IMG_SUFFIXES]


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #


def index_images(images: list[Path]) -> dict[str, Path]:
    """按分区名归类镜像，忽略 slot 后缀差异（同优先级取先出现的）。"""
    index: dict[str, Path] = {}
    for img in images:
        base = img.stem.lower()
        for suffix in (".raw", ".simg"):
            if base.endswith(suffix):
                base = base[: -len(suffix)]
        for slot in ("_a", "_b", "-a", "-b"):
            if base.endswith(slot):
                base = base[: -len(slot)]
                break
        index.setdefault(base, img)
    return index


def main() -> int:
    parser = argparse.ArgumentParser(description="解包固件并提取分区")
    parser.add_argument("--input", required=True, help="固件文件或目录")
    parser.add_argument("--out-dir", default="work/unpacked", help="解包输出目录")
    parser.add_argument("--partitions", default="", help="要提取的分区，逗号分隔")
    parser.add_argument("--extract-all", action="store_true",
                        help="提取 payload 中全部分区（否则只提取指定分区）")
    args = parser.parse_args()

    src = Path(args.input)
    if not src.exists():
        error(f"输入不存在: {src}")
        return 1

    out_dir = Path(args.out_dir)
    wanted = [p.strip().lower() for p in args.partitions.split(",") if p.strip()]

    log(f"输入:   {src}")
    log(f"目标分区: {', '.join(wanted) if wanted else '(全部)'}")

    extracted: list[Path] = []

    with timed("解包固件"):
        sources = [src] if src.is_file() else find_files(src, [".zip", ".img", ".bin", ".tar.md5"])
        if not sources:
            error("未找到可解包的固件文件")
            return 1
        info(f"待处理文件 {len(sources)} 个")

        part_dir = out_dir / "partitions"

        for item in sources:
            log("")
            log(f"处理: {item.name} ({size_of(item)})")

            if item.suffix.lower() in (".zip", ".tar.md5"):
                entries = list_archive(item)
                has_payload = any(e.endswith("payload.bin") for e in entries)

                if has_payload:
                    # 关键优化：直接把 zip 交给 payload-dumper-go，免去整包解压
                    info("检测到 payload.bin（OTA 包），直接解析，跳过整包解压")
                    with group(f"解析 payload: {item.name}"):
                        names = list_payload_partitions(item)
                        if names:
                            info(f"payload 内分区 ({len(names)}): {', '.join(names)}")
                        target = None if args.extract_all else (wanted or None)
                        if args.extract_all:
                            info("已启用 --extract-all，将提取全部分区")
                        extracted.extend(dump_payload(item, part_dir, target))
                else:
                    with group(f"解压 {item.name}"):
                        raw_dir = out_dir / "raw" / item.stem
                        extract_archive(item, raw_dir)
                        imgs = [p for p in raw_dir.rglob("*")
                                if p.is_file() and p.suffix in IMG_SUFFIXES]
                        info(f"抽取到 {len(imgs)} 个镜像")
                        extracted.extend(imgs)
            else:
                extracted.append(item)

        # super.img 需要二次拆分才能拿到 boot 等分区
        index_now = index_images(extracted)
        if "super" in index_now and wanted:
            info("检测到 super.img（动态分区），尝试拆分以获取目标分区")
            with group("拆分 super.img"):
                extracted.extend(
                    unpack_super(index_now["super"], part_dir / "super", wanted)
                )

    index = index_images(extracted)

    log("")
    log("分区清单:")
    for name in sorted(index):
        log(f"  - {name:<20} {size_of(index[name]):>10}  {index[name]}")

    # 匹配目标分区
    targets: list[Path] = []
    for want in wanted:
        match = index.get(want)
        if match:
            targets.append(match)
        else:
            warn(f"未找到目标分区: {want}")

    # 标记 sparse 镜像（ksud 需要 raw 格式）
    sparse_targets = [p for p in targets if is_sparse(p)]
    for path in sparse_targets:
        warn(f"{path.name} 是 sparse 镜像，修补前会自动转换为 raw")

    log("")
    if targets:
        ok(f"提取完成，共 {len(index)} 个分区，命中目标 {len(targets)} 个")
    else:
        warn(f"提取完成，共 {len(index)} 个分区，但未命中任何目标分区")

    set_output(
        partitions_json=json.dumps({k: str(v) for k, v in index.items()}),
        targets_json=json.dumps([str(p) for p in targets]),
        sparse_json=json.dumps([str(p) for p in sparse_targets]),
        partition_names="\n".join(sorted(index)),
    )

    rows = "\n".join(
        f"| `{name}` | {size_of(index[name])} |"
        for name in sorted(index)
    )
    summary(f"### 解包结果\n\n共 {len(index)} 个分区\n\n| 分区 | 大小 |\n|---|---|\n{rows}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
