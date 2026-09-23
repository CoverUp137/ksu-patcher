#!/usr/bin/env python3
"""端到端冒烟测试：不依赖外网，用本地生成的镜像验证整条链路。

覆盖
----
1. utils 的 KMI 探测 / sparse 判定 / 体积格式化
2. unpack.py 对 zip 固件包的提取
3. patch.py 的修补（需要 tools/ksud 与一个真实 boot.img）
4. verify.py 的校验
5. release.py 的产物整理
6. gen_notes.py 的说明生成

用法
----
    python3 tests/smoke_test.py

前提
----
- tools/ksud、tools/magiskboot 已由 setup_tools.py 准备好
- simg2img 可用（apt: android-sdk-libsparse-utils）
- 若需测试完整修补流程，请放置一个 GKI boot 镜像到
  tests/fixtures/boot.img（否则自动跳过 3~6 步）
"""
from __future__ import annotations

import os
import shutil
import struct
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from utils import detect_kmi_from_image, human_size, is_sparse  # noqa: E402

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    (PASS if condition else FAIL).append(name)
    mark = "\033[32mPASS\033[0m" if condition else "\033[31mFAIL\033[0m"
    print(f"  [{mark}] {name}" + (f"  {detail}" if detail else ""))


def run_step(label: str, cmd: list[str], cwd: Path) -> subprocess.CompletedProcess:
    print(f"\n--- {label} ---")
    env = {**os.environ, "GITHUB_OUTPUT": str(cwd / "outputs"), "NO_COLOR": "1"}
    return subprocess.run(cmd, cwd=cwd, env=env, text=True, capture_output=True)


def make_fake_boot(path: Path, kernel_size: int, ramdisk_size: int = 0) -> None:
    """构造最小的 boot header v4 镜像，用于测试头部解析。"""
    header = bytearray(4096)
    header[0:8] = b"ANDROID!"
    struct.pack_into("<I", header, 8, kernel_size)    # kernel_size
    struct.pack_into("<I", header, 12, ramdisk_size)  # ramdisk_size
    struct.pack_into("<I", header, 20, 1580)          # header_size (v4)
    struct.pack_into("<I", header, 40, 4)             # header_version
    path.write_bytes(bytes(header))


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        print(f"工作目录: {work}")

        # ---------------- 1. 基础函数 ----------------
        print("\n=== 1. 基础工具函数 ===")
        check("human_size(1024) == 1.0KB", human_size(1024) == "1.0KB", human_size(1024))
        check("human_size(0) == 0B", human_size(0) == "0B", human_size(0))

        fake = work / "fake_boot.img"
        make_fake_boot(fake, kernel_size=0)
        check("无内核镜像探测 KMI 返回 None", detect_kmi_from_image(fake) is None)
        check("普通文件不是 sparse", is_sparse(fake) is False)

        kernel_like = work / "kernel_like.bin"
        kernel_like.write_bytes(b"\0" * 64 + b"5.15.104-android13-8-gabcd" + b"\0" * 64)
        detected = detect_kmi_from_image(kernel_like)
        check("能从内核数据探测到 android13-5.15",
              detected == "android13-5.15", str(detected))

        # ---------------- 2. 依赖工具 ----------------
        print("\n=== 2. 依赖工具 ===")
        ksud = ROOT / "tools" / "ksud"
        magiskboot = ROOT / "tools" / "magiskboot"
        check("tools/ksud 存在", ksud.is_file(), str(ksud))
        check("tools/magiskboot 存在", magiskboot.is_file(), str(magiskboot))
        check("simg2img 可用", shutil.which("simg2img") is not None)

        if not ksud.is_file():
            print("\n缺少 tools/ksud。请先执行：")
            print("  python3 scripts/setup_tools.py")
            print("  python3 scripts/fetch_kernelsu.py")
            return 1

        # ---------------- 3. unpack ----------------
        print("\n=== 3. unpack.py ===")
        pkg_dir = work / "pkg"
        pkg_dir.mkdir()
        real_boot = ROOT / "tests" / "fixtures" / "boot.img"
        if real_boot.is_file():
            shutil.copy(real_boot, pkg_dir / "boot.img")
        else:
            make_fake_boot(pkg_dir / "boot.img", kernel_size=1024)

        pkg_zip = work / "factory.zip"
        with zipfile.ZipFile(pkg_zip, "w") as zf:
            zf.write(pkg_dir / "boot.img", "boot.img")

        r = run_step("unpack", [
            sys.executable, str(ROOT / "scripts" / "unpack.py"),
            "--input", str(pkg_zip), "--out-dir", str(work / "unpacked"),
            "--partitions", "boot",
        ], work)
        check("unpack.py 退出码 0", r.returncode == 0,
              (r.stderr or "")[-300:] if r.returncode else "")
        outputs = (work / "outputs").read_text() if (work / "outputs").exists() else ""
        check("unpack 输出 partitions_json", "partitions_json=" in outputs)

        # ---------- 针对线上故障的回归用例 ----------
        print("\n=== 3b. OTA 包处理（回归用例）===")
        sys.path.insert(0, str(ROOT / "scripts"))
        from unpack import index_images, list_archive  # noqa: E402

        # 用例 1：payload.bin 位于压缩包第一个条目时也必须被识别
        # （旧实现用 names[1:] 跳过"首行"，会丢掉第一个文件）
        ota_first = work / "ota_payload_first.zip"
        with zipfile.ZipFile(ota_first, "w", zipfile.ZIP_STORED) as zf:
            zf.writestr("payload.bin", b"P" * 2048)
            zf.writestr("META-INF/com/android/metadata", b"m" * 64)
        entries = list_archive(ota_first)
        check("payload.bin 在首位时能被列出",
              "payload.bin" in entries, str(entries[:3]))
        check("压缩包条目数完整（未丢失首个文件）",
              len(entries) == 2, f"期望 2, 实际 {len(entries)}")

        # 用例 2：payload.bin 不能被当成名为 payload 的分区
        idx = index_images([Path("x/payload.bin")])
        check("payload.bin 不被索引为分区",
              "payload" not in idx, str(idx))
        idx2 = index_images([Path("x/boot.img"), Path("x/init_boot.img")])
        check("正常分区仍能正确索引",
              set(idx2) == {"boot", "init_boot"}, str(list(idx2)))

        # 用例 3：请求不存在的分区不应崩溃
        ota_mix = work / "ota_mix.zip"
        with zipfile.ZipFile(ota_mix, "w", zipfile.ZIP_STORED) as zf:
            zf.write(pkg_dir / "boot.img", "boot.img")
        r = run_step("unpack-missing", [
            sys.executable, str(ROOT / "scripts" / "unpack.py"),
            "--input", str(ota_mix), "--out-dir", str(work / "unpacked2"),
            "--partitions", "boot,不存在的分区XYZ",
        ], work)
        check("请求不存在的分区时仍能成功退出",
              r.returncode == 0, (r.stderr or "")[-300:] if r.returncode else "")

        # ---------------- 4~6. 需要真实镜像 ----------------
        if not real_boot.is_file():
            print("\n=== 4~6. 跳过（未提供 tests/fixtures/boot.img）===")
            print("    放入一个 GKI boot 镜像后可跑通完整修补链路。")
        else:
            print("\n=== 4. patch.py ===")
            r = run_step("patch", [
                sys.executable, str(ROOT / "scripts" / "patch.py"),
                "--images", str(real_boot),
                "--out-dir", str(work / "patched"),
                "--version", "vTest",
            ], work)
            check("patch.py 退出码 0", r.returncode == 0,
                  (r.stderr or "")[-300:] if r.returncode else "")
            produced = sorted((work / "patched").glob("*.img")) if (work / "patched").exists() else []
            check("生成了修补后的镜像", len(produced) > 0, str(produced))

            if produced:
                print("\n=== 5. verify.py ===")
                r = run_step("verify", [
                    sys.executable, str(ROOT / "scripts" / "verify.py"),
                    "--patched", str(produced[0]),
                ], work)
                check("verify.py 退出码 0", r.returncode == 0,
                      (r.stderr or "")[-300:] if r.returncode else "")
                check("校验输出含 kernelsu.ko", "kernelsu.ko" in r.stdout)

                print("\n=== 6. release.py + gen_notes.py ===")
                r = run_step("release", [
                    sys.executable, str(ROOT / "scripts" / "release.py"),
                    "--patched", str(produced[0]),
                    "--out-dir", str(work / "release"),
                    "--version", "vTest", "--kmi", "android13-5.15",
                    "--firmware", str(pkg_zip),
                ], work)
                check("release.py 退出码 0", r.returncode == 0,
                      (r.stderr or "")[-300:] if r.returncode else "")
                check("生成 SHA256SUMS.txt", (work / "release" / "SHA256SUMS.txt").is_file())
                check("生成 manifest.json", (work / "release" / "manifest.json").is_file())

                r = run_step("notes", [
                    sys.executable, str(ROOT / "scripts" / "gen_notes.py"),
                    "--manifest", str(work / "release" / "manifest.json"),
                    "--tag", "test", "--ksu", "vTest", "--kmi", "android13-5.15",
                    "--arch", "aarch64", "--firmware", str(pkg_zip),
                    "--out", str(work / "release" / "RELEASE_NOTES.md"),
                ], work)
                check("gen_notes.py 退出码 0", r.returncode == 0,
                      (r.stderr or "")[-300:] if r.returncode else "")
                notes = work / "release" / "RELEASE_NOTES.md"
                check("Release 说明非空", notes.is_file() and notes.stat().st_size > 100)

    print("\n" + "=" * 60)
    print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    for name in FAIL:
        print(f"  失败: {name}")
    print("=" * 60)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
