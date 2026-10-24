# KernelSU / APatch 云修补

GitHub Actions 里跑完全程：**下载固件 → 解包提取分区 → 修补 → 发布 Release**，不用本地环境。

- **Build KernelSU Patched Images** — `ksud` LKM 方式，一般修 `boot` / `init_boot`
- **Build APatch Patched Images** — `kptools` 直接修内核，只认 `boot`

默认分支 `dev`（两套都有），`main` 只有 KernelSU。

## 用法

1. Fork 本仓库
2. **Actions** → 选工作流 → **Run workflow**

**KernelSU** 主要参数：

| 参数 | 说明 |
|---|---|
| `firmware_url` | **必填**，固件直链，可多个 |
| `partitions` | 要提取的分区，默认 `boot,init_boot` |
| `patch_partitions` | 要修补的分区，留空=同上 |
| `kernelsu_version` | `latest` 或指定版本 |
| `kmi` | 留空自动判定（如 `android15-6.6`） |

**APatch** 主要参数：

| 参数 | 说明 |
|---|---|
| `firmware_url` | **必填**，固件直链 |
| `partitions` | 默认 `boot`（`init_boot` 不含内核，修不了） |
| `superkey` | 可留空，见下 |
| `kernelpatch_version` | `auto` 跟随 APatch 上游，或写 `0.13.9` |

Release 的 tag 和名称默认自动生成；想自定义就填 `release_tag`（tag，同时决定发布地址）和
`release_name`（只改标题，留空=用 tag）。

构建完在 **Releases** / **Artifacts** 取 `patched/`（已 Root 镜像）和 `partitions/`（原分区）。

## 刷入

```bash
sha256sum -c SHA256SUMS.txt
adb reboot bootloader
fastboot flash init_boot init_boot-*-patched.img   # KernelSU
fastboot flash boot      boot-APatch-*-patched.img # APatch
fastboot reboot
```

> 刷前先备份原分区，固件版本必须和设备匹配。

## APatch 注意

- 只刷 `boot`，别用 `init_boot` / `vendor_boot`
- 内核必须开 `CONFIG_KALLSYMS`（建议再开 `CONFIG_KALLSYMS_ALL`），否则修了也起不来
- **SuperKey 可选**：留空就是「不校验」，开机直接可用 root；要设就 8~63 位、含字母和数字。
  仓库是公开的，建议放 Secret `APATCH_SUPERKEY` 而不是填输入框

## 本地调试

```bash
python3 scripts/setup_tools.py
python3 scripts/fetch_kernelsu.py      # ksud
python3 scripts/fetch_apatch.py        # kptools + kpimg
python3 tests/smoke_test.py
```

## 致谢

[tiann/KernelSU](https://github.com/tiann/KernelSU) ·
[bmax121/APatch](https://github.com/bmax121/APatch) ·
[bmax121/KernelPatch](https://github.com/bmax121/KernelPatch) ·
[topjohnwu/Magisk](https://github.com/topjohnwu/Magisk) ·
[ssut/payload-dumper-go](https://github.com/ssut/payload-dumper-go)

> 仅供学习与个人设备维护，刷机风险自负。
