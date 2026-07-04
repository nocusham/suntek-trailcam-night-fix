# 修复 Suntek / Novatek 红外相机夜间照片过曝

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

[English](README.md) | **中文**

一份完整、可复现的指南，用于**诊断并修复** Suntek/Novatek 平台红外相机（例如 **HC‑940Ultra、
HC‑950Ultra、HC‑960Ultra‑li** 及同类机型）长期存在的**夜间（红外）过曝**问题。方法是**在固件内部**
修正自动曝光（AE）调校参数，并重新计算固件校验和，使打补丁后的镜像能够正常启动与刷写。

> **本方法已在真实的 HC‑960Ultra‑li 上端到端验证：** 打补丁后的固件通过 SD 卡刷入、正常启动，
> 夜间照片中纯白（过曝）像素比例从约 22% 降到约 1%。

---

## ⚠️ 请先阅读 — 风险与免责声明

修改并刷写固件**可能导致相机变砖**。你需**自行承担风险**；本文不提供任何担保，作者不承担任何责任。
请务必降低风险：

- **保留你机型对应的原始未修改固件**，以便随时恢复。
- 只修改**数据字节**，绝不改变文件长度。
- 刷写前**务必运行校验和自检**（见下文）——错误的校验和会导致镜像无法启动。
- 尽量在**能物理接触相机**时测试（不要远程盲刷）。

好消息：在本平台的 SD 更新流程中，校验和**不正确**的镜像会在写入前被**拒绝**（相机保持可用），
而不是写到一半。这让谨慎的尝试相对安全——但仍要把“变砖”当作可能发生的事。

---

## 仓库内容

- [`patch_ae.py`](patch_ae.py) —— 补丁工具（自检、自动定位、打补丁、算校验和、验证）。
- [`README.md`](README.md) / [`README.zh-CN.md`](README.zh-CN.md) —— 本指南（英文 / 中文）。
- [`LICENSE`](LICENSE) —— MIT（仅涵盖工具与文档；不分发任何固件）。

> 本项目**不**包含任何厂商固件——固件需你自行提供。

---

## 1. 问题描述

夜间相机切换到红外模式。在受影响的机器上，AE 引擎的**目标亮度对红外场景设得过高**，于是不断拉高
ISO/曝光，导致**近距离目标（几米外的动物）被过曝成纯白**，而白天照片正常。厂商的建议（“把菜单
ISO 设为 100”）只能部分缓解，因为那只是粗暴地封顶；真正的根源是一个 AE 参数。

**根本原因：** AE 的夜间/红外亮度比例表 **`tab_ratio_ir`** 被设为 **110%**（高于白天最大值 100%）。
把它调低（例如调到 **55**）即可从源头修复过曝。

这**不是**红外灯功率问题：遮挡红外灯并没有用，因为 AE 只会提高增益去达到那个（过高的）目标。

---

## 2. 平台背景（内部结构）

- 主控：**Novatek NA51023**（市场名 **NT96670**），双 CPU、MIPS32。
  - **CPU1**：µITRON/eCos 实时系统 —— 相机管线、ISP、**AE**、屏幕菜单。*补丁作用于此。*
  - **CPU2**：Linux —— 4G/WiFi、云端。
- 启动流程：内部 ROM → 引导程序（`LD_NVT`）→ **u‑boot** → 把 **µITRON** 镜像拷到内存并启动。
  **u‑boot 每次启动都会校验 µITRON 分区的校验和**，所以打补丁的镜像必须带正确校验和，否则无法启动。
- 固件文件格式：**`NVTPACK_FW_HDR2`** —— 一个容器，包含文件头、分区表以及若干分区：两个 `MODELEXT`
  配置块、**主 µITRON** 镜像（AE 调校所在）、u‑boot、Linux `uImage`、`UBIFS` 根文件系统，以及
  **第二份 µITRON 副本**。
- AE/IQ/AWB 调校以命名库 **`AE_PARAM_<传感器>_EVB`** 的形式嵌在 µITRON 中
  （例如 SmartSens SC2210 传感器对应 `AE_PARAM_SC2210_EVB`）。它**没有 SD/运行时覆盖**通道
  （`A:\ntscript.txt` 脚本引擎虽然存在，但在正常启动中**不会**被自动执行），
  因此唯一可靠的持久修复方式就是给固件镜像打补丁。

---

## 3. 前置条件

**软件**
- **Python 3**（自带 `struct`、`array` 模块）。
- **NTKFWinfo** —— EgorKin 的 Novatek 固件工具：
  `git clone https://github.com/EgorKin/Novatek-FW-info`（仓库亦称 *NTKFWinfo*）。它能解析
  `NVTPACK_FW_HDR2`、列出分区并校验 CRC。我们用它确认格式，并独立验证打补丁后的文件。
- **Pillow**（`pip install pillow`）—— 可选，用于测量测试 JPG 的过曝程度。

**硬件**
- 一张 **SD 卡**（相机在启动时从卡根目录刷写固件）。
- **可选但很有用：一个 3.3V 的 USB‑UART 适配器**，用于连接相机的串口控制台。
  它能让你用 `ae aetdump 0` 读取实时 AE 表并确认数值。（接触 UART 焊点通常需要拆开相机。）

**文件**
- 你机型对应的**精确固件**（相机自身的 SD 更新 `.bin`，或厂商提供）。请保留一份原始副本。

---

## 4. 步骤 1 —— 用 NTKFWinfo 检查固件

```bash
git clone https://github.com/EgorKin/Novatek-FW-info
cd Novatek-FW-info
python3 NTKFWinfo.py -i /path/to/FWHC940A.bin
```

你应当看到类似输出：

```
NVTPACK_FW_HDR2 found
Found 7 partitions
Firmware file ORIG_CRC:0x4044  CALC_CRC:0x4044          <- 格式与 CRC 已确认
 ID   START_OFFSET   END_OFFSET        SIZE      ORIG_CRC  CALC_CRC   TYPE
  1   0x000000D4  - 0x00000CB8         3,044     0xC55C    0xC55C     MODELEXT INFO: Chip:NT96670 ...
  2   0x00000CB8  - 0x00001878         3,008     0xFEF8    0xFEF8     MODELEXT INFO: Chip:NT96670 ...
  3   0x00001878  - 0x006E944C     7,240,660     0x0000    0x0000     unknown partition   <- µITRON（主）
  4   ...                                                              (u-boot)
  6   ...                                                              uImage (Linux)
  7   ...                                                              CKSM UBIFS  (rootfs)
  9   0x015B7D1C  - 0x01E2B004     8,860,392     0x0000    0x0000     unknown partition   <- 第二份 µITRON 副本
```

**记下**那一行**主 µITRON 分区**（体积较大的 *unknown partition*，其数据以 `0x027004xx` 载入地址开头）。
记录它的 **START_OFFSET** 与 **SIZE**——后面会用到。这些数值**因机型和固件版本而异**，所以切勿臆测，
一律在这里读取。

> 文件行的 `ORIG_CRC == CALC_CRC` 证明了容器格式，也证明下文使用的校验和族是正确的。

---

## 5. 步骤 2 —— 诊断 AE 参数

### 方案 A —— UART 控制台（最佳）
连接 UART，给相机上电，输入：

```
ae aetdump 0
```

查看 `expect_lum` 部分。故障机会显示：

```
data.tExpectLum.expect_lum.tab_ratio_mov = { 44, 48, 52, ... 100, 100 }   (白天：封顶 100)
data.tExpectLum.expect_lum.tab_ratio_ir  = { 110, 110, ... 110 }          (夜间：110 = 过高)
...
data.tBoundary.proc_boundary.iso_prv.h   = 12800                          (ISO 上限)
```

`tab_ratio_ir > 100`（通常是恒定的 **110**）即为确诊。另外注意只有一个 AE 实例
（输入 `ae dumpcurve 1` 会触发 CPU 异常），因此这一个值同时控制预览与拍照。

### 方案 B —— 无 UART（图像分析）
拍一张夜间照片，测量有多少被过曝成白色：

```python
from PIL import Image
im = Image.open("night.jpg").convert("L")
h = im.histogram(); tot = sum(h)
print("纯白 (255): %.1f%%" % (h[255]/tot*100))
```

若 255 处占比很大（约 20% 以上），说明严重过曝。修复后用同样的方法再测一次以确认效果。

---

## 6. 步骤 3 —— 理解两个校验和

编辑 µITRON 之后，必须修正**两个**校验和，否则相机会拒绝该镜像或无法启动：

1. **µITRON 分区内部校验和** —— u‑boot 每次启动都会检查。它是一个 16 位值，存放在分区头部魔数
   `55 aa` 紧后面，即**分区偏移 `0x6E`**。
2. **整文件 CRC** —— 更新程序的文件校验。一个 16 位值，存放在**文件偏移 `0x24`**。

两者使用**相同算法**：位置加权的 16 位二进制补码求和。参考实现（等价于 NTKFWinfo 的
`MemCheck_CalcCheckSum16Bit`）：

```python
import array
def ntk_cksum16(buf, off, length, ignore_off):
    n = length // 2
    a = array.array('h')                        # 有符号 16 位小端字
    a.frombytes(bytes(buf[off:off + n*2]))
    a[ignore_off // 2] = 0                       # 将存放校验和自身的那个字清零
    s = (sum(a) + (n - 1) * n // 2) & 0xFFFF     # 各字之和 + 三角数 n*(n-1)/2
    return ((~s & 0xFFFF) + 1) & 0xFFFF          # 二进制补码（取负）
```

- MODELEXT 分区用 `ignore_off = 0x36`；µITRON 用 `ignore_off = 0x6E`；整文件用 `ignore_off = 0x24`。
- **范围**：分区校验和覆盖整个分区，整文件 CRC 覆盖整个文件。

---

## 7. 步骤 4 —— 用 `patch_ae.py` 打补丁

仓库提供了开箱即用的工具 [`patch_ae.py`](patch_ae.py)。它会：

1. 用你的固件对校验和算法做**自检**（若偏移不同则中止），
2. 从分区表**自动识别**主 uITRON 分区，
3. **按结构定位 `tab_ratio_ir`**（与传感器无关：以 `tab_ratio_mov` 斜坡为锚点——一个其后数组为
   平坦数组的单调数组——绝不依赖硬编码偏移），
4. 将其设为你的目标值，并按正确顺序修正 **uITRON 分区校验和**与**整文件 CRC**，
5. **验证**结果并报告改动的字节数。

```bash
python3 patch_ae.py FWHC940A.bin                 # tab_ratio_ir -> 55，输出 FWHC940A_patched.bin
python3 patch_ae.py FWHC940A.bin -o out.bin --ir 45   # 自定义输出与取值（越小夜间越暗）
python3 patch_ae.py FWHC940A.bin --iso-cap 3200       # 同时封顶 iso_prv.h（例如 12800 -> 3200）
python3 patch_ae.py FWHC940A.bin --dry-run            # 仅分析与定位，不写文件
python3 patch_ae.py FWHC940A.bin --verify-only        # 仅校验某文件的校验和
python3 patch_ae.py FWHC940A.bin --uit-off 0x1878 --uit-size 7240660   # 自动识别失败时手动指定
```

在 HC-960Ultra-li 上验证过的输出：

```
[i] uITRON partition: off=0x1878 size=7240660 (0x6e7bd4)
[ok] self-test: uITRON cksum @+0x6e=0x1a2d reproduced; file CRC @0x24=0x4044 calc=0x4044
[i] tab_ratio_ir @ file 0x6cb628 = 110 x21
[patch] tab_ratio_ir 110 -> 55
[cksum] uITRON 0x1a2d->0x1eb0   file CRC 0x4044->0x4044
[verify] uITRON checksum: OK   file CRC: OK
[done] wrote FWHC940A_patched.bin  (bytes changed: 23)
```

如果**自检失败**，说明你的固件使用了不同偏移——见第 11 节（推广到其他机型）。

## 8. 步骤 5 —— 用 NTKFWinfo 独立验证

```bash
python3 NTKFWinfo.py -i FWHC940A_patched.bin
```

确认 `Firmware file ORIG_CRC == CALC_CRC`（绿色），且所有可识别分区仍然有效。
（NTKFWinfo 会把 µITRON 视为 “unknown”/CRC 0x0000，这是正常的——它的内部校验和由你的脚本修正并验证。）

---

## 9. 步骤 6 —— 刷写并确认

1. 把补丁文件改名为相机期望的更新名（通常为 **`FWHC940A.bin`**），复制到 **SD 卡根目录**。
   把原始文件另存以备恢复。
2. 插卡上电；相机在启动时刷写。（在 UART 日志中会看到 `uiFWUpdate…`、`upd_src_size=…`，随后正常启动。）
3. 确认改动：
   - UART：`ae aetdump 0` 现在应显示 `tab_ratio_ir = { 55, … }`。
   - 真实夜间照片：重新运行直方图测量——纯白占比应大幅下降
     （在已验证的案例中，约 22% → 约 1%）。

---

## 10. 调优与选项

- **`tab_ratio_ir` 取值**：`55` 是不错的起点（约相当于把夜间目标降 1 挡）。若要更强地压制很近、很亮的
  目标，用 `45` 或 `40`；若想夜间更亮，用 `60`–`70`。由于校验和正确后刷写可靠，你可以反复迭代：
  改值、重跑脚本、重刷、对比夜间照片。
- **ISO 上限**（`iso_prv.h`，例如 `12800 → 3200`）：在脚本中设置 `NEW_ISO_PRV_H`，即可在不降低整体
  夜间目标的前提下封顶 Auto 模式的 ISO。对近距离目标的 ISO 失控很有用。
- **菜单 ISO 选项**：某些机型菜单只显示 `Auto/100/200/400`，因为固件里只存在这几个选项**字符串**——
  AE 内部其实能用更高的 ISO。相比给菜单加条目（需要风险大得多的 UI/表结构改动），更推荐用
  `iso_prv.h` 补丁。

---

## 11. 推广到其他机型 / 传感器 / 固件版本

- **不同传感器/驱动**（非 SC2210）：AE 库名为 `AE_PARAM_<传感器>_EVB`；AE **结构布局完全相同**。
  用 `ae aetdump 0` 诊断，并按内容定位 `tab_ratio_ir`——脚本的锚点逻辑（恒定 21 元素数组，其后紧跟
  `over_exposure`）与传感器无关。
- **不同机型 / 更新的固件**：分区偏移与 AE 结构的文件偏移会**变化**。务必用 `NTKFWinfo -i` 重新读取，
  设置脚本顶部的 CONFIG，并依赖**自检**来确认校验和偏移（`0x24`/`0x36`/`0x6E`）仍然正确。
- **若自检失败**：容器可能是较新的变体。NTKFWinfo 会尝试分区 `ignoreCRCoffset` 的取值
  `{0x6E, 0x16E, 0x26E, 0x36E, 0x46E}` 以及文件 `0x24`；逐个尝试，直到 `ntk_cksum16` 能复现存储值，
  然后使用该偏移。

---

## 12. 疑难排解 / 退路

- **更新似乎被忽略/拒绝**：相机保留旧固件（安全）。请重新检查文件名以及校验和是否正确。
  如果该机型的 SD 流程加了签名校验，改用 u‑boot 控制台：中断启动，然后（地址/长度取自你的分区表）
  `fatload mmc 0 <ram_addr> <part>.bin; sf erase <flash_off> <len>; sf write <ram_addr> <flash_off> <len>; reset`。
  这会绕过更新程序；只有 u‑boot 启动时的 µITRON 校验和才起作用（你已修正）。
  同一 SoC 与控制台命令可参考 `github.com/hn/reolink-camera`。
- **刷写后无法启动**：重新刷入原始固件。

---

## 致谢与参考

- **NTKFWinfo** —— `github.com/EgorKin/Novatek-FW-info` —— Novatek `NVTPACK_FW_HDR2` 解析与 CRC 逻辑
  （本文的校验和算法源自其 `MemCheck_CalcCheckSum16Bit`）。
- **hn/reolink-camera** —— `github.com/hn/reolink-camera` —— NA51023 启动流程与 u‑boot 刷写命令参考。

## 许可

[MIT](LICENSE) —— 仅适用于**本仓库中的工具与文档**。它**不**涵盖、且本仓库**不**分发任何厂商固件；
固件需你自行提供。"Novatek"、"Suntek" 等名称为其各自所有者的商标。按**现状提供，不作任何担保**；
刷写固件风险自负（见顶部警告）。
