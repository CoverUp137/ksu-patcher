#!/usr/bin/env python3
"""用 APatch（KernelPatch）的 kptools 修补 boot 镜像。

处理链路（与 APatch App 内置的 boot_patch.sh 一致）
-------------------------------------------------
1. sparse 镜像先用 simg2img 转成 raw
2. kptools unpack 解出 kernel，确认镜像里真的有内核
3. 检查 CONFIG_KALLSYMS（APatch 的硬性要求），并提示 KALLSYMS_ALL
4. kptools -p -i <boot.img> -S <superkey> -k kpimg -o <out>
   （-i 直接给 boot 镜像时，kptools 会自己解包内核、打补丁并重打包）
5. 重新解包产物，确认 patched=true，且 kpimg 版本与本次使用的一致

注意：APatch 修补的是内核，只有 `boot`（部分设备是 `recovery`）里带内核，
`init_boot` / `vendor_boot` 不含内核，无法用本工具修补。
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fetch_apatch import parse_ini  # noqa: E402
from patch import guess_partition, to_raw  # noqa: E402
from utils import (  # noqa: E402
    ANDROID_MAGIC,
    VENDOR_BOOT_MAGIC,
    error,
    find_tool,
    group,
    image_magic,
    info,
    log,
    ok,
    set_output,
    sha256_of,
    size_of,
    summary,
    timed,
    warn,
)

SUPERKEY_MIN = 8
SUPERKEY_MAX = 63
# APatch App 只允许修补带内核的镜像
KERNEL_PARTITIONS = ("boot", "recovery", "kernel")


# --------------------------------------------------------------------------- #
# 参数校验
# --------------------------------------------------------------------------- #


def validate_superkey(key: str) -> str | None:
    """按 APatch App 的规则校验 SuperKey：8~63 位，且同时含字母和数字。

    返回错误说明；合法返回 None。规则取自 APatch 源码
    `PatchesViewModel.checkSuperKeyValidation`。
    """
    if not key:
        return "SuperKey 不能为空"
    if any(ch.isspace() for ch in key):
        return "SuperKey 不能包含空白字符"
    if not (SUPERKEY_MIN <= len(key) <= SUPERKEY_MAX):
        return f"SuperKey 长度需在 {SUPERKEY_MIN}~{SUPERKEY_MAX} 位之间（当前 {len(key)} 位）"
    if not any(ch.isdigit() for ch in key):
        return "SuperKey 至少要包含一个数字"
    if not any(ch.isalpha() for ch in key):
        return "SuperKey 至少要包含一个字母"
    return None


def pick_images(images: list[Path], wanted: str) -> tuple[list[Path], list[str]]:
    """按分区名精确筛选待修补镜像，返回 (命中列表, 未命中名称列表)。

    必须精确匹配：`boot` 不能误匹配 `init_boot`（前者含内核，后者不含）。
    """
    names = [p.strip().lower() for p in wanted.split(",") if p.strip()]
    if not names:
        return images, []

    def keys(img: Path) -> set[str]:
        return {guess_partition(img), img.stem.lower()}

    picked = [img for img in images if keys(img) & set(names)]
    missing = [n for n in names if not any(n in keys(i) for i in images)]
    return picked, missing


# --------------------------------------------------------------------------- #
# kptools 封装
# --------------------------------------------------------------------------- #


def run_kptools(cmd: list[str | Path], *, cwd: str | Path | None = None,
                secret: str | None = None, echo: bool = True) -> subprocess.CompletedProcess:
    """执行 kptools；secret 会在日志中被替换成占位符，避免 SuperKey 泄漏。"""
    args = [str(c) for c in cmd]
    if echo:
        display = " ".join(args)
        if secret:
            display = display.replace(secret, "<superkey>")
        info(f"$ {display}")
    proc = subprocess.run(args, cwd=cwd, text=True, capture_output=True)
    if echo:
        if proc.stdout.strip():
            log(proc.stdout.rstrip())
        if proc.stderr.strip():
            log(proc.stderr.rstrip())
    return proc


def unpack_kernel(kptools: str, image: Path, work: Path) -> Path | None:
    """在 work 目录里解出内核；失败或没有内核时返回 None。

    kptools 在 work 目录下执行，所以镜像路径必须先转成绝对路径。
    """
    work.mkdir(parents=True, exist_ok=True)
    proc = run_kptools([kptools, "unpack", Path(image).resolve()], cwd=work)
    kernel = work / "kernel"
    if proc.returncode != 0:
        return None
    if not kernel.is_file() or kernel.stat().st_size == 0:
        return None
    return kernel


def kernel_info(kptools: str, kernel: Path) -> dict[str, dict[str, str]]:
    """读取内核补丁信息（banner / patched / kpimg 版本等）。"""
    proc = run_kptools([kptools, "-i", kernel, "-l"], echo=False)
    return parse_ini(proc.stdout)


def kernel_config(kptools: str, kernel: Path) -> dict[str, str] | None:
    """读取内核内嵌的 ikconfig；未开启 CONFIG_IKCONFIG 时返回 None。"""
    proc = run_kptools([kptools, "-i", kernel, "-f"], echo=False)
    if proc.returncode != 0 or "Cannot find kernel config blob" in proc.stdout + proc.stderr:
        return None

    config: dict[str, str] = {}
    for line in proc.stdout.splitlines():
        line = line.strip()
        if line.startswith("# ") and line.endswith(" is not set"):
            config[line[2:-len(" is not set")]] = "n"
        elif line.startswith("CONFIG_") and "=" in line:
            key, _, value = line.partition("=")
            config[key] = value.strip()
    return config or None


def check_kallsyms(config: dict[str, str] | None, allow_missing: bool) -> tuple[bool, str]:
    """检查 CONFIG_KALLSYMS；返回 (是否可继续, 结论说明)。"""
    if config is None:
        return (True,
                "内核未开启 CONFIG_IKCONFIG，无法读取内核配置，已跳过 KALLSYMS 检查"
                "（若设备无法开机，多半是缺少 CONFIG_KALLSYMS）")
    if config.get("CONFIG_KALLSYMS") != "y":
        if allow_missing:
            return True, "内核未开启 CONFIG_KALLSYMS，已按要求继续（APatch 极可能无法启动）"
        return False, "内核未开启 CONFIG_KALLSYMS，APatch 无法工作"
    if config.get("CONFIG_KALLSYMS_ALL") != "y":
        return True, "内核开启了 KALLSYMS，但未开启 CONFIG_KALLSYMS_ALL，可能无法正常启动"
    return True, "内核支持 KALLSYMS / KALLSYMS_ALL"


def default_out_name(partition: str, version: str) -> str:
    return f"{partition}-APatch-{version or 'kptools'}-patched.img"


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #


def main() -> int:
    parser = argparse.ArgumentParser(description="用 APatch（KernelPatch）修补镜像")
    parser.add_argument("--images", required=True, help="待修补镜像，逗号分隔")
    parser.add_argument("--patch-partitions", default="",
                        help="只修补这些分区（逗号分隔，留空=全部）")
    parser.add_argument("--out-dir", default="work/patched", help="产物目录")
    parser.add_argument("--superkey", default="",
                        help="APatch SuperKey（8~63 位，含字母和数字）")
    parser.add_argument("--version", default="", help="KernelPatch 版本，用于命名")
    parser.add_argument("--kpimg", default="", help="kpimg 路径（默认 tools/kpimg）")
    parser.add_argument("--kptools", default="", help="kptools 路径（默认 tools/kptools）")
    parser.add_argument("--name-template", default="",
                        help="产物命名模板，可用 {partition} {version}")
    parser.add_argument("--allow-no-kallsyms", action="store_true",
                        help="内核未开启 CONFIG_KALLSYMS 时仍然继续")
    args = parser.parse_args()

    key_error = validate_superkey(args.superkey)
    if key_error:
        error(f"SuperKey 无效：{key_error}")
        return 1

    kptools = args.kptools or find_tool("kptools")
    kpimg = args.kpimg or find_tool("kpimg")
    if not kptools:
        error("未找到 kptools，请先执行 scripts/fetch_apatch.py")
        return 1
    if not kpimg:
        error("未找到 kpimg，请先执行 scripts/fetch_apatch.py")
        return 1
    info(f"kptools: {kptools}")
    info(f"kpimg:   {kpimg}")

    kpimg_meta = run_kptools([kptools, "-l", "-k", kpimg], echo=False)
    kpimg_ver = parse_ini(kpimg_meta.stdout).get("kpimg", {}).get("version", "")
    if kpimg_ver:
        info(f"kpimg 版本: {kpimg_ver}")

    all_images = [Path(p.strip()) for p in args.images.split(",") if p.strip()]
    all_images = [p for p in all_images if p.exists()]
    if not all_images:
        error("没有可用的待修补镜像")
        return 1

    images, missing = pick_images(all_images, args.patch_partitions)
    for name in missing:
        warn(f"没有匹配到分区: {name}")
    if not images:
        error("请求修补的分区都不在提取结果里。")
        error(f"实际可用的镜像: {', '.join(p.name for p in all_images)}")
        return 1

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    raw_dir = Path("work/raw")

    results: list[dict[str, str]] = []
    failed: list[dict[str, str]] = []

    with timed("APatch 修补"):
        for img in images:
            part = guess_partition(img)
            if args.name_template:
                out_name = args.name_template.format(partition=part,
                                                     version=args.version or "kptools")
            else:
                out_name = default_out_name(part, args.version)
            target = out_dir / out_name
            if target.exists():
                target.unlink()  # 避免旧产物干扰校验

            log("")
            with group(f"修补 {img.name} -> {out_name}"):
                # 后续 kptools 都在临时目录里执行，路径必须是绝对的
                raw = to_raw(img.resolve(), raw_dir.resolve())
                magic = image_magic(raw)
                if magic == VENDOR_BOOT_MAGIC:
                    error("vendor_boot 不含内核，APatch 无法修补；请改用 boot 分区。")
                    failed.append({"image": img.name, "reason": "vendor_boot 不含内核"})
                    continue
                if magic != ANDROID_MAGIC:
                    error(f"不是 Android boot 镜像（magic={magic!r}），已跳过。")
                    failed.append({"image": img.name, "reason": "不是 boot 镜像"})
                    continue

                if part not in KERNEL_PARTITIONS:
                    warn(f"分区名推断为 '{part}'，APatch 通常只修补 boot / recovery，仍继续尝试。")

                with tempfile.TemporaryDirectory() as tmp:
                    tmp_path = Path(tmp)
                    kernel = unpack_kernel(kptools, raw, tmp_path / "unpack")
                    if kernel is None:
                        error("镜像里没有可识别的内核（init_boot / vendor_boot 等），"
                              "APatch 只能修补 boot 分区。")
                        failed.append({"image": img.name, "reason": "镜像不含内核"})
                        continue

                    config = kernel_config(kptools, kernel)
                    proceed, message = check_kallsyms(config, args.allow_no_kallsyms)
                    if not proceed:
                        error(message)
                        error("如确认要强刷（多半无法开机），可加 --allow-no-kallsyms。")
                        failed.append({"image": img.name, "reason": message})
                        continue
                    if config and config.get("CONFIG_KALLSYMS") == "y":
                        ok(message)
                    else:
                        warn(message)

                    before = kernel_info(kptools, kernel)
                    if before.get("kernel", {}).get("patched") == "true":
                        warn("该镜像已被 APatch 修补过，本次将更新补丁（可更换 SuperKey / kpimg）。")

                    log("")
                    out_tmp = tmp_path / "patched.img"
                    proc = run_kptools(
                        [kptools, "-p", "-i", raw, "-S", args.superkey,
                         "-k", kpimg, "-o", out_tmp],
                        cwd=tmp_path, secret=args.superkey,
                    )
                    if proc.returncode != 0 or not out_tmp.is_file():
                        error("kptools 修补失败")
                        failed.append({"image": img.name, "reason": "kptools 修补失败"})
                        continue

                    # 复核：重新解包产物，确认真的是 patched=true
                    check_kernel = unpack_kernel(kptools, out_tmp, tmp_path / "verify")
                    if check_kernel is None:
                        error("产物无法解包，修补结果不可信")
                        failed.append({"image": img.name, "reason": "产物不可解析"})
                        continue
                    after = kernel_info(kptools, check_kernel)
                    patched = after.get("kernel", {}).get("patched") == "true"
                    got_ver = after.get("kpimg", {}).get("version", "")
                    if not patched:
                        error("产物未显示 patched=true，修补失败")
                        failed.append({"image": img.name, "reason": "产物未打上补丁"})
                        continue
                    if kpimg_ver and got_ver and got_ver != kpimg_ver:
                        warn(f"产物内 kpimg 版本 {got_ver} 与本次使用的 {kpimg_ver} 不一致")

                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(out_tmp, target)

                if image_magic(target) != ANDROID_MAGIC:
                    error(f"{target.name} 的 magic 异常")
                    failed.append({"image": img.name, "reason": "产物 magic 异常"})
                    continue

                ok(f"{target.name} ({size_of(target)})")
                results.append({
                    "partition": part,
                    "file": str(target),
                    "name": target.name,
                    "size": size_of(target),
                    "bytes": str(target.stat().st_size),
                    "sha256": sha256_of(target),
                    "kpimg": got_ver or kpimg_ver,
                })

    if not results:
        error("所有镜像修补失败")
        for item in failed:
            error(f"  - {item['image']}: {item['reason']}")
        return 1

    log("")
    log("修补结果:")
    for item in results:
        log(f"  - {item['name']:<44} {item['size']:>10}  sha256={item['sha256'][:16]}...")
    if failed:
        warn(f"失败 {len(failed)} 个: " + ", ".join(f"{i['image']}({i['reason']})" for i in failed))

    set_output(
        patched_json=json.dumps(results, ensure_ascii=False),
        patched_files="\n".join(item["file"] for item in results),
        patched_count=len(results),
        failed_count=len(failed),
        kpimg_version=kpimg_ver,
    )

    rows = "\n".join(
        f"| `{item['name']}` | {item['partition']} | {item['size']} | `{item['sha256'][:16]}…` |"
        for item in results
    )
    summary(
        "### APatch 修补结果\n\n"
        f"- KernelPatch：`{args.version or 'auto'}`\n"
        f"- kpimg：`{kpimg_ver}`\n"
        f"- 成功：{len(results)}，失败：{len(failed)}\n\n"
        f"| 文件 | 分区 | 大小 | SHA256 |\n|---|---|---|---|\n{rows}\n"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
