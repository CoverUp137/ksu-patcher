#!/usr/bin/env python3
"""用 KernelSU 的 ksud 修补 boot / init_boot 等镜像。

处理链路
--------
1. sparse 镜像先用 simg2img 转成 raw（ksud 不认 sparse）
2. 自动探测 KMI（android13-5.15 等）；init_boot 无内核，需从其它镜像
   或用户参数获得
3. 调用 ksud boot-patch 生成修补后的镜像
4. 校验产物（ANDROID! magic + 体积）并汇总
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from utils import (  # noqa: E402
    ANDROID_MAGIC,
    find_tool,
    VENDOR_BOOT_MAGIC,
    android_version_from_props,
    detect_kmi_from_image,
    is_gki_kernel,
    kmi_candidates_for_android,
    list_supported_kmis,
    parse_build_prop,
    parse_linux_version,
    error,
    group,
    info,
    is_sparse,
    log,
    ok,
    run,
    set_output,
    sha256_of,
    size_of,
    summary,
    timed,
    warn,
    which,
)

# 已知分区名，按长度倒序匹配，避免 init_boot 被误判成 init
KNOWN_PARTITIONS = (
    "vendor_boot", "init_boot", "system_dlkm", "vendor_dlkm",
    "dtbo", "recovery", "boot", "super", "vbmeta", "vendor",
)
PATCHABLE = {"boot", "init_boot", "vendor_boot", "recovery"}


def ksud_path() -> str:
    found = find_tool("ksud")
    if not found:
        raise SystemExit("未找到 ksud，请先执行 scripts/fetch_kernelsu.py")
    return found


def simg2img_path() -> str | None:
    return find_tool("simg2img")


def guess_partition(path: Path) -> str:
    """从文件名推断分区名，例如 init_boot.raw.img -> init_boot。"""
    name = path.name.lower()
    for suffix in (".raw.img", ".img", ".bin"):
        if name.endswith(suffix):
            name = name[: -len(suffix)]
            break
    # 去掉 slot 后缀：boot_a -> boot
    for slot in ("_a", "_b", "-a", "-b"):
        if name.endswith(slot):
            name = name[: -len(slot)]
            break
    # 与已知分区名做最长匹配（处理 boot-KernelSU 之类的命名）
    for known in KNOWN_PARTITIONS:
        if name == known or name.startswith(f"{known}-") or name.startswith(f"{known}_"):
            return known
    return name or "boot"


def to_raw(path: Path, work: Path) -> Path:
    """把 sparse 镜像转成 raw；非 sparse 原样返回。"""
    if not is_sparse(path):
        return path
    tool = simg2img_path()
    if not tool:
        raise SystemExit(f"{path.name} 是 sparse 镜像，但未找到 simg2img")
    work.mkdir(parents=True, exist_ok=True)
    raw = work / f"{path.stem}.raw.img"
    info(f"sparse -> raw: {path.name} => {raw.name}")
    run([tool, str(path), str(raw)])
    ok(f"{raw.name} ({size_of(raw)})")
    return raw


def diagnose_kernel(image: Path) -> bool:
    """解出内核并诊断版本与 GKI 兼容性。返回是否成功定位到内核。"""
    tool = magiskboot_bin()
    if not tool:
        return False
    image = image.resolve()
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        proc = run([tool, "unpack", str(image)], check=False, capture=True, cwd=tmp_path)
        if proc.returncode not in (0, 2, 3):
            return False
        kernel = tmp_path / "kernel"
        if not kernel.exists() or kernel.stat().st_size == 0:
            return False
        version = parse_linux_version(kernel.read_bytes())
        if not version:
            warn("镜像含内核，但未能解析出版本号")
            return True
        info(f"内核版本: {version}")
        if not is_gki_kernel(version):
            warn(f"该内核为 {version}，属于非 GKI 内核"
                 "（KernelSU 的 LKM 模式要求内核 >= 5.10）。")
            warn("此类设备需改用 KernelSU 的内核内置方案（自行编译内核），"
                 "无法通过修补 boot/init_boot 获得 root。")
        return True


def infer_kmi_from_image(image: Path, arch: str) -> str | None:
    """当镜像不含内核时（如 init_boot），尝试从 ramdisk 内的 build.prop 反推 KMI。

    思路：读出 build.prop 里的 Android 大版本（如 15），在 ksud 支持的 KMI
    列表里找唯一匹配项；若有多个候选则返回 None，交由用户显式指定。
    """
    tool = magiskboot_bin()
    if not tool:
        return None

    image = image.resolve()
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        proc = run([tool, "unpack", str(image)], check=False, capture=True, cwd=tmp_path)
        if proc.returncode not in (0, 2, 3):
            return None
        ramdisk = tmp_path / "ramdisk.cpio"
        if not ramdisk.exists():
            return None

        extract_dir = tmp_path / "rd"
        extract_dir.mkdir()
        with open(ramdisk, "rb") as fh:
            subprocess.run(["cpio", "-idm", "--quiet"],
                           cwd=extract_dir, stdin=fh, capture_output=True)

        for prop_file in extract_dir.rglob("build.prop"):
            try:
                props = parse_build_prop(prop_file.read_text(errors="replace"))
            except OSError:
                continue
            android_version = android_version_from_props(props)
            if not android_version:
                continue
            info(f"{image.name} 的 build.prop 显示 Android {android_version}")

            supported = list_supported_kmis(arch)
            candidates = [k for k in kmi_candidates_for_android(android_version)
                          if not supported or k in supported]
            if len(candidates) == 1:
                return candidates[0]
            if candidates:
                warn(f"Android {android_version} 有多个候选 KMI: {', '.join(candidates)}")
                return None
            warn(f"没有与 Android {android_version} 匹配的 KMI")
            return None
    return None


def magiskboot_bin() -> str | None:
    """返回 magiskboot 的绝对路径。"""
    return find_tool("magiskboot")


def patch_one(
    ksud: str,
    image: Path,
    out_dir: Path,
    out_name: str,
    kmi: str | None,
    arch: str,
    extra_cmdline: str | None,
    allow_shell: bool,
    enable_adbd: bool,
) -> Path | None:
    """对单个镜像执行修补，返回产物路径。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / out_name
    if target.exists():
        target.unlink()  # 避免旧产物造成误判

    cmd = [ksud, "boot-patch", "-b", str(image), "-o", str(out_dir), "--out-name", out_name]
    if kmi:
        cmd += ["--kmi", kmi]
    cmd += ["--arch", arch]
    if extra_cmdline:
        cmd += ["--cmdline", extra_cmdline]
    if allow_shell:
        cmd += ["--allow-shell"]
    if enable_adbd:
        cmd += ["--enable-adbd"]

    proc = run(cmd, check=False, capture=True)

    if proc.returncode != 0 or not target.exists():
        error(f"修补失败: {image.name}")
        return None

    if target.read_bytes()[:8] not in (ANDROID_MAGIC, VENDOR_BOOT_MAGIC):
        warn(f"{target.name} 的 magic 异常")
    ok(f"{target.name} ({size_of(target)})")
    return target


def main() -> int:
    parser = argparse.ArgumentParser(description="用 KernelSU 修补镜像")
    parser.add_argument("--images", required=True, help="待修补镜像，逗号分隔")
    parser.add_argument("--out-dir", default="work/patched", help="产物目录")
    parser.add_argument("--kmi", default="", help="强制指定 KMI，如 android13-5.15")
    parser.add_argument("--arch", default="aarch64", choices=["aarch64", "x86_64"],
                        help="目标架构")
    parser.add_argument("--version", default="", help="KernelSU 版本，用于命名")
    parser.add_argument("--cmdline", default="", help="追加到 boot 头的额外 cmdline")
    parser.add_argument("--allow-shell", action="store_true", help="允许 shell 获得 root")
    parser.add_argument("--enable-adbd", action="store_true", help="强制开启 adbd 并关闭鉴权")
    parser.add_argument("--name-template", default="",
                        help="产物命名模板，可用 {partition} {version} {kmi}")
    args = parser.parse_args()

    images = [Path(p.strip()) for p in args.images.split(",") if p.strip()]
    images = [p for p in images if p.exists()]
    if not images:
        error("没有可用的待修补镜像")
        return 1

    ksud = ksud_path()
    info(f"ksud: {ksud}")
    run([ksud, "--version"], check=False, capture=True)

    out_dir = Path(args.out_dir)
    work = Path("work/raw")

    # 统一转 raw，并记录每个镜像的分区名
    with timed("预处理镜像"):
        tasks: list[tuple[str, Path]] = []
        detected_kmi = ""
        for img in images:
            part = guess_partition(img)
            raw = to_raw(img, work)
            tasks.append((part, raw))
            if not detected_kmi:
                kmi = detect_kmi_from_image(raw)
                if kmi:
                    info(f"探测到 KMI: {raw.name} => {kmi}")
                    detected_kmi = kmi

    # KMI 优先级：用户指定 > 内核探测 > ramdisk 反推
    global_kmi = args.kmi.strip() or detected_kmi
    if not global_kmi:
        info("镜像中未发现内核，尝试从 ramdisk 的 build.prop 反推 KMI ...")
        for _, raw in tasks:
            inferred = infer_kmi_from_image(raw, args.arch)
            if inferred:
                global_kmi = inferred
                ok(f"推断出 KMI: {inferred}")
                break
    if not global_kmi:
        error("无法自动确定 KMI。")
        has_kernel = False
        # 针对性诊断：判断镜像里的内核是否为 GKI
        for _, raw in tasks:
            if diagnose_kernel(raw):
                has_kernel = True
        if not has_kernel:
            error("镜像不含可识别的内核（多见于 init_boot），"
                  "请用 --kmi 手动指定，例如 --kmi android15-6.6")
        error("可用取值见: tools/ksud supported-kmis")
        return 1
    info(f"使用 KMI: {global_kmi}")

    results: list[dict[str, str]] = []
    failed: list[str] = []

    with timed("修补镜像"):
        for part, raw in tasks:
            if part not in PATCHABLE:
                warn(f"{raw.name} 推断为 '{part}'，不是常见可修补分区，仍尝试修补。")

            if args.name_template:
                out_name = args.name_template.format(
                    partition=part, version=args.version or "KernelSU", kmi=global_kmi
                )
            else:
                out_name = f"{part}-{args.version or 'KernelSU'}-patched.img"

            log("")
            with group(f"修补 {raw.name} -> {out_name}"):
                produced = patch_one(
                    ksud, raw, out_dir, out_name, global_kmi, args.arch,
                    args.cmdline or None, args.allow_shell, args.enable_adbd,
                )
            if produced:
                results.append({
                    "partition": part,
                    "file": str(produced),
                    "name": produced.name,
                    "size": size_of(produced),
                    "sha256": sha256_of(produced),
                    "kmi": global_kmi,
                })
            else:
                failed.append(raw.name)

    if not results:
        error("所有镜像修补失败")
        return 1

    log("")
    log("修补结果:")
    for item in results:
        log(f"  - {item['name']:<44} {item['size']:>10}  sha256={item['sha256'][:16]}...")
    if failed:
        warn(f"失败 {len(failed)} 个: {', '.join(failed)}")

    set_output(
        patched_json=json.dumps(results),
        patched_files="\n".join(item["file"] for item in results),
        patched_count=len(results),
        kmi=global_kmi,
        failed_count=len(failed),
    )

    rows = "\n".join(
        f"| `{item['name']}` | {item['partition']} | {item['size']} | `{item['sha256'][:16]}...` |"
        for item in results
    )
    summary(
        f"### KernelSU 修补结果\n\n"
        f"- KMI：`{global_kmi}`\n"
        f"- 架构：`{args.arch}`\n"
        f"- 成功：{len(results)}，失败：{len(failed)}\n\n"
        f"| 文件 | 分区 | 大小 | SHA256 |\n|---|---|---|---|\n{rows}\n"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
