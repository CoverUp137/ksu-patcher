#!/usr/bin/env python3
"""公共工具：日志、命令执行、GitHub Actions 输出、文件检索。"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

# --------------------------------------------------------------------------- #
# 日志
# --------------------------------------------------------------------------- #

_COLOR = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None


def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _COLOR else text


def log(msg: str = "") -> None:
    print(msg, flush=True)


def info(msg: str) -> None:
    log(f"{_c('36', '[INFO]')} {msg}")


def warn(msg: str) -> None:
    log(f"{_c('33', '[WARN]')} {msg}")


def error(msg: str) -> None:
    log(f"{_c('31', '[FAIL]')} {msg}")


def ok(msg: str) -> None:
    log(f"{_c('32', '[ OK ]')} {msg}")


def group(title: str):
    """以 GitHub Actions 折叠分组的形式输出一段日志。"""
    return _Group(title)


class _Group:
    def __init__(self, title: str) -> None:
        self.title = title

    def __enter__(self):
        log(f"::group::{self.title}")
        return self

    def __exit__(self, *exc) -> bool:
        log("::endgroup::")
        return False


def mask(value: str) -> None:
    """在日志中遮蔽敏感字符串（如带签名的下载链接）。"""
    if value:
        log(f"::add-mask::{value}")


# --------------------------------------------------------------------------- #
# 命令执行
# --------------------------------------------------------------------------- #


class CommandError(RuntimeError):
    def __init__(self, cmd: list[str], code: int) -> None:
        self.cmd = cmd
        self.code = code
        super().__init__(f"命令执行失败 (exit={code}): {' '.join(cmd)}")


def run(
    cmd: list[str],
    *,
    check: bool = True,
    capture: bool = False,
    env: dict[str, str] | None = None,
    cwd: str | Path | None = None,
    timeout: int | None = None,
) -> subprocess.CompletedProcess:
    """执行命令；capture=True 时返回并打印输出，否则把输出直接透传到日志。"""
    printable = " ".join(str(c) for c in cmd)
    info(f"$ {printable}")
    full_env = {**os.environ, **(env or {})}

    if capture:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, env=full_env, cwd=cwd, timeout=timeout
        )
        if proc.stdout:
            log(proc.stdout.rstrip())
        if proc.stderr:
            log(proc.stderr.rstrip())
    else:
        proc = subprocess.run(cmd, text=True, env=full_env, cwd=cwd, timeout=timeout)
        proc.stdout = proc.stderr = ""  # type: ignore[assignment]

    if check and proc.returncode != 0:
        raise CommandError([str(c) for c in cmd], proc.returncode)
    return proc


def which(tool: str) -> str | None:
    return shutil.which(tool)


def require(tool: str) -> str:
    path = which(tool)
    if not path:
        raise SystemExit(f"缺少必需的工具: {tool}")
    return path


# --------------------------------------------------------------------------- #
# 工具定位
# --------------------------------------------------------------------------- #

# 仓库根目录（scripts/ 的上一级）
REPO_ROOT = Path(__file__).resolve().parent.parent


def find_tool(name: str) -> str | None:
    """查找可执行工具，依次尝试：仓库 tools/ 目录、当前目录 tools/、PATH。

    使用绝对路径可避免在切换工作目录后失效。
    """
    for base in (REPO_ROOT, Path.cwd()):
        candidate = base / "tools" / name
        if candidate.is_file():
            return str(candidate.resolve())
    found = which(name)
    return str(Path(found).resolve()) if found else None


# --------------------------------------------------------------------------- #
# 文件工具
# --------------------------------------------------------------------------- #

# 与 ksud 内部探测逻辑保持一致：匹配 "android13-5.15" 这类 KMI 字符串
KMI_RE = re.compile(rb"(\d+\.\d+)(?:\S+)?(android\d+)")


def human_size(num: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(num) < 1024.0:
            return f"{num:.1f}{unit}" if unit != "B" else f"{int(num)}B"
        num /= 1024.0
    return f"{num:.1f}PB"


def size_of(path: str | Path) -> str:
    return human_size(Path(path).stat().st_size)


def find_files(root: str | Path, patterns: list[str]) -> list[Path]:
    """在 root 下递归查找匹配任一 glob 的文件（大小写不敏感）。"""
    root = Path(root)
    found: dict[Path, None] = {}
    lowered = [p.lower() for p in patterns]
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        name = path.name.lower()
        for pattern in lowered:
            if path.name.lower().endswith(pattern) or path.match(pattern):
                found[path] = None
                break
    return sorted(found)


def sha256_of(path: str | Path, chunk_size: int = 1 << 20) -> str:
    import hashlib

    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        while block := fh.read(chunk_size):
            digest.update(block)
    return digest.hexdigest()


# Android sparse image 的 magic 为 0x3aff26ed，小端写入文件即 b"\x3a\xff\x26\xed"
SPARSE_MAGIC = b"\x3a\xff\x26\xed"
ANDROID_MAGIC = b"ANDROID!"
VENDOR_BOOT_MAGIC = b"VNDRBOOT"


def is_sparse(path: str | Path) -> bool:
    """判断是否为 Android sparse image。"""
    try:
        with open(path, "rb") as fh:
            return fh.read(4) == SPARSE_MAGIC
    except OSError:
        return False


def image_magic(path: str | Path) -> bytes:
    try:
        with open(path, "rb") as fh:
            return fh.read(8)
    except OSError:
        return b""


def detect_kmi_from_image(path: str | Path) -> str | None:
    """从 kernel 镜像中探测 KMI，例如 android13-5.15。

    ksud 也是用同样的方式扫描镜像内的版本字符串。
    """
    try:
        data = Path(path).read_bytes()
    except OSError:
        return None
    match = KMI_RE.search(data)
    if not match:
        return None
    kernel_version = match.group(1).decode()
    android_version = match.group(2).decode()
    return f"{android_version}-{kernel_version}"


# --------------------------------------------------------------------------- #
# GitHub Actions 交互
# --------------------------------------------------------------------------- #


def set_output(**kwargs: object) -> None:
    """写入 GITHUB_OUTPUT；本地运行时打印到日志方便调试。"""
    target = os.environ.get("GITHUB_OUTPUT")
    lines: list[str] = []
    for key, value in kwargs.items():
        if isinstance(value, (list, tuple)):
            text = "\n".join(str(v) for v in value)
        else:
            text = str(value)
        if "\n" in text:
            lines.append(f"{key}<<__EOF__\n{text}\n__EOF__")
        else:
            lines.append(f"{key}={text}")
    payload = "\n".join(lines)
    if target:
        with open(target, "a", encoding="utf-8") as fh:
            fh.write(payload + "\n")
    else:
        log(f"[output] {payload}")


def export_env(**kwargs: object) -> None:
    """把变量写入 GITHUB_ENV，供后续步骤使用。"""
    target = os.environ.get("GITHUB_ENV")
    if not target:
        log(f"[env] {kwargs}")
        return
    with open(target, "a", encoding="utf-8") as fh:
        for key, value in kwargs.items():
            text = value if isinstance(value, str) else "\n".join(map(str, value))  # type: ignore[arg-type]
            if "\n" in text:
                fh.write(f"{key}<<__EOF__\n{text}\n__EOF__\n")
            else:
                fh.write(f"{key}={text}\n")


def summary(markdown: str) -> None:
    """写入 GitHub Actions Step Summary。"""
    target = os.environ.get("GITHUB_STEP_SUMMARY")
    if not target:
        return
    with open(target, "a", encoding="utf-8") as fh:
        fh.write(markdown.rstrip() + "\n")


def timed(label: str):
    """上下文管理器：统计某步骤耗时。"""
    return _Timer(label)


class _Timer:
    def __init__(self, label: str) -> None:
        self.label = label
        self.start = 0.0

    def __enter__(self):
        self.start = time.monotonic()
        return self

    def __exit__(self, *exc) -> bool:
        ok(f"{self.label} 完成，耗时 {time.monotonic() - self.start:.1f}s")
        return False

# --------------------------------------------------------------------------- #
# 内核版本分析
# --------------------------------------------------------------------------- #

# Linux 内核横幅，例如 "Linux version 5.4.302-qgki-gabd4ede9ec64"
LINUX_VERSION_RE = re.compile(rb"Linux version (\d+\.\d+\.\d+)")
# GKI 内核的版本串形如 "6.6.66-android15-8-...-4k"
GKI_SUFFIX_RE = re.compile(rb"(\d+\.\d+)\.\d+-android(\d+)")

# GKI（通用内核镜像）从 5.10 起引入；更早的内核无法使用 KernelSU LKM 模式
MIN_GKI_KERNEL = (5, 10)


def parse_linux_version(data: bytes) -> str | None:
    """从内核镜像数据中提取 Linux 版本号，如 "5.4.302"。"""
    match = LINUX_VERSION_RE.search(data)
    return match.group(1).decode() if match else None


def is_gki_kernel(version: str | None) -> bool:
    """判断内核版本是否为 GKI（>= 5.10）。"""
    if not version:
        return False
    try:
        major, minor = (int(x) for x in version.split(".")[:2])
    except (ValueError, IndexError):
        return False
    return (major, minor) >= MIN_GKI_KERNEL


# --------------------------------------------------------------------------- #
# KMI 推断辅助
# --------------------------------------------------------------------------- #

# 各 KMI 对应的 Android 大版本（用于在缺少内核时反推）
ANDROID_VERSION_TO_KMI = {
    "12": ["android12-5.10"],
    "13": ["android13-5.10", "android13-5.15"],
    "14": ["android14-5.15", "android14-6.1"],
    "15": ["android15-6.6"],
    "16": ["android16-6.12"],
    "17": ["android17-6.18"],
}


def kmi_candidates_for_android(android_version: str) -> list[str]:
    """给出某个 Android 大版本对应的候选 KMI 列表。"""
    return ANDROID_VERSION_TO_KMI.get(android_version.strip(), [])


def parse_build_prop(text: str) -> dict[str, str]:
    """解析 build.prop 文本为字典。"""
    props: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        props[key.strip()] = value.strip()
    return props


def android_version_from_props(props: dict[str, str]) -> str | None:
    """从 build.prop 属性中取出 Android 大版本号。"""
    for key in (
        "ro.bootimage.build.version.release",
        "ro.build.version.release",
        "ro.system.build.version.release",
        "ro.build.version.release_or_codename",
    ):
        value = props.get(key, "").strip()
        if value:
            # 形如 "15" 或 "15.0.0"，取主版本号
            major = value.split(".")[0]
            if major.isdigit():
                return major
    # 退而求其次：从 fingerprint 里取 ":15/" 这样的段
    fingerprint = props.get("ro.bootimage.build.fingerprint", "") or props.get(
        "ro.build.fingerprint", ""
    )
    match = re.search(r":(\d{2})/", fingerprint)
    if match:
        return match.group(1)
    return None


def list_supported_kmis(arch: str = "aarch64") -> list[str]:
    """调用 ksud supported-kmis 得到内置 KMI 列表（不带 arch 前缀）。"""
    ksud = Path(__file__).resolve().parent.parent / "tools" / "ksud"
    if not ksud.is_file():
        found = which("ksud")
        if not found:
            return []
        ksud = Path(found)
    proc = subprocess.run(
        [str(ksud), "supported-kmis"], capture_output=True, text=True, timeout=60
    )
    result = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        if "/" in line:
            a, k = line.split("/", 1)
            if a == arch:
                result.append(k)
        else:
            result.append(line)
    return result
