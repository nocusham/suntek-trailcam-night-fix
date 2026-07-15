# 修复 Suntek / Novatek 猎相机夜间照片过曝

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

[English](README.md) | 中文

本项目用于分析和修补部分采用 Novatek `NVTPACK_FW_HDR2` 平台的 Suntek
猎相机固件。补丁会修改夜间红外自动曝光曲线 `tab_ratio_ir`，并重新计算所有
受影响的校验和，使输出固件在结构上保持一致。

版本 2 修正了一个关键问题：已分析的固件中包含**两个独立的相机运行时**。
普通/网络运行时负责远程拍照，另一个低功耗运行时负责 PIR 唤醒拍照。两个
运行时各有一份 AE 参数。只修补第一份时，远程照片可能恢复正常，但 PIR
触发的照片仍然过曝。

## 安全警告

刷写修改后的固件可能永久损坏相机。即使型号名称相同，也不能保证固件适用于
另一硬件版本。

- 保留该相机原始固件的完整、未修改副本。
- 写入或刷机前先运行 `--verify-only` 和 `--dry-run`。
- 将输入文件的 SHA-256 与下表中的已验证配置严格比较。
- 最好在可以直接接触相机的情况下测试，并准备 3.3 V UART 恢复连接。
- 出现错误或操作中断后，不要刷写输出文件。

本仓库只提供工具和文档，不分发厂商固件。

## 已测试的固件配置

只有当 BIN 文件的 SHA-256 完全匹配时，工具才会自动选择配置。

| 配置 | 型号/版本 | 原始 BIN SHA-256 | 原始 IR 曲线 | 目标曲线 |
|---|---|---|---|---|
| `hc960-ae55` | HC-960Ultra，2026-03-26 | `b391abec2bdf6ab1d48e357c94e0f56bb9e2703899b647609acec3faa30150fa` | `110 x21` | `55 x21` |
| `hc940-ae58` | HC-940Ultra，2025-04-23 | `9eb10ef5dd4057a891fb48a2b9cb9165e9ae3168a9b7e58aecc6299b90749c4a` | `110..125` | `58..66` |

HC-940Ultra 使用以下 21 项测试曲线：

```text
58, 58, 58, 58, 58, 58, 58,
58, 58, 58, 58, 58, 58, 61,
63, 66, 66, 66, 66, 66, 66
```

实拍对比表明，固定值 55 几乎消除了高光剪切，同时仍保留了可用的主体细节。
`58..66` 曲线保留厂商原始 `110..125` 曲线的形状，并利用一部分剩余曝光余量。
它属于研究/测试标定，并非厂商发布版本。

查看所有配置和哈希：

```bash
python3 patch_ae.py --list-profiles
```

## 为什么必须修补两个运行时

已分析的 HC-940Ultra 和 HC-960Ultra 固件包含：

| 运行时 | 常见分区 | 加载地址 | 功能 |
|---|---:|---:|---|
| 普通运行时 | ID 3 | `0x02700400` | 菜单、网络、远程拍照、Linux/4G 主机路径 |
| 低功耗运行时 | ID 9 | `0x00400400` | PIR 唤醒、独立/低功耗拍照 |

每个运行时都包含一组独立的三个 21 项 AE 曲线：

```text
tab_ratio_mov
+0x54  tab_ratio_photo
+0xA8  tab_ratio_ir
```

在已分析的 SDK 布局中，特征性的 `over_exposure` 阈值表位于
`tab_ratio_ir + 0x25c`。版本 2 使用该结构、分区边界、运行时校验和以及
歧义检查，而不是依赖单一硬编码偏移。

## 要求

- Python 3.10 或更高版本。
- 厂商原始 `.bin`，或只包含一个固件 `.bin` 的 `.zip`。
- 不需要第三方 Python 包。

可选的独立检查工具：

- [Novatek-FW-info / NTKFWinfo](https://github.com/EgorKin/Novatek-FW-info)
- 用于控制台诊断和恢复的 3.3 V USB-UART 适配器。

## 快速开始

### 1. 验证原始固件

```bash
python3 patch_ae.py firmware.zip --verify-only
```

该命令验证整个文件的校验和，以及所有可通过 Novatek `55 aa` 标记识别的
内部分区校验和。在已测试固件中，这包括两个配置分区、两个相机运行时和
bootloader。

### 2. 扫描运行时和原始曲线

```bash
python3 patch_ae.py firmware.zip --scan
```

正常情况下应显示一个普通/远程运行时和一个低功耗/PIR 运行时。如果结果存在
歧义或与预期固件版本不符，请停止操作。

### 3. 预览自动补丁

对于上表中 SHA-256 完全匹配的固件：

```bash
python3 patch_ae.py firmware.zip --dry-run
```

工具根据输入哈希选择配置，不写入文件。

### 4. 创建补丁固件

```bash
python3 patch_ae.py firmware.zip
```

- ZIP 输入默认生成 `firmware_patched.zip`，并保留压缩包目录结构。
- BIN 输入默认生成 `firmware_patched.bin`。
- 工具绝不覆盖输入文件。
- 覆盖已有输出必须显式使用 `--overwrite`。

同时创建 JSON 审计清单：

```bash
python3 patch_ae.py firmware.zip --manifest patch-manifest.json
```

### 5. 再次验证输出

```bash
python3 patch_ae.py firmware_patched.zip --verify-only
```

写入后工具会自动执行同样的回读验证。如果回读验证失败，输出文件会被删除。

## 自定义曲线模式

未知固件没有自动目标值。检查 `--scan` 结果后，必须显式选择一种模式。

### 固定曲线

将全部 21 项设为同一个值，兼容旧版行为：

```bash
python3 patch_ae.py firmware.bin --ir 55
```

### 按比例缩放原曲线

保留曲线形状：

```bash
python3 patch_ae.py firmware.bin --ir-scale 0.50
```

例如，`110,115,120,125` 约变为 `55,58,60,63`。

### 明确指定 21 项曲线

```bash
python3 patch_ae.py firmware.bin --ir-values \
  58,58,58,58,58,58,58,58,58,58,58,58,58,61,63,66,66,66,66,66,66
```

所有值必须在 `1..255` 范围内，并且单调不下降。

## 选择运行时和偏移

默认修补所有检测到的运行时。限制到单个运行时属于专家操作，可能故意让另一
触发路径保持未修补状态。

```bash
python3 patch_ae.py firmware.bin --runtime normal --ir 55
python3 patch_ae.py firmware.bin --runtime pir --ir 55
python3 patch_ae.py firmware.bin --runtime pid:9 --ir-scale 0.50
```

`--runtime` 可重复使用。已弃用的 `--all` 仍可接受，但不再改变默认行为。

可以提供人工确认的 IR 表偏移。工具会验证该偏移所属分区及周围 AE 结构：

```bash
python3 patch_ae.py firmware.bin --ir-offset 0x006cb628 --ir 55
```

重复 `--ir-offset` 可选择多个表。

## ISO 上限：只允许明确偏移

版本 1 会搜索第一个看似合理的 `{iso_prv.h, 100}`。该模式不够唯一，无法安全
自动定位。版本 2 必须提供人工确认的偏移：

```bash
python3 patch_ae.py firmware.bin --ir 55 \
  --iso-cap 3200 --iso-offset 0x123456
```

偏移必须 4 字节对齐、位于已选择的 AE 运行时内、包含合理的 ISO 值，并且后面
紧跟 `iso_prv.l = 100`。如果两个运行时都有独立字段，应重复提供偏移。

## 版本 2 的验证项目

修补前：

1. `NVTPACK_FW_HDR2` 版本标记。
2. 读取头部 `0x14` 的分区表指针和 `0x18` 的分区数量。
3. 按实际顺序 `{offset, size, partition_id}` 解析记录。
4. 检查分区边界和重叠。
5. 验证整个文件校验和。
6. 验证每个可识别的内部分区校验和。
7. 每个选中运行时必须只有一个明确的 AE 结构。
8. 使用配置时，验证 SHA-256 和预期原始曲线。

修补后：

1. 每个被修改的运行时都重新计算内部校验和。
2. 最后重新计算整个文件校验和。
3. 回读并精确比较目标曲线和可选 ISO 字段。
4. 字节白名单拒绝任何请求范围和校验和字段之外的改动。
5. 通过唯一临时文件写入，再原子重命名。
6. 从磁盘或 ZIP 中重新读取 BIN 并再次验证。

Novatek 使用的是位置加权的 16 位二进制补码加法校验和，而不是传统 CRC。实现
在所有主机平台上都显式按小端解释数据，并只写入外层 16 位字段。

## 评估夜间图像

建议保持相机和场景不变，分别拍摄远程触发和 PIR 触发照片。画面中应同时包含
明亮的近距离植物和较暗的远距离主体。

排除底部相机信息栏后的简单高光剪切测量：

```python
from PIL import Image

image = Image.open("night.jpg").convert("L")
image = image.crop((0, 0, image.width, image.height - 100))
histogram = image.histogram()
total = sum(histogram)
print("pixels >= 250: %.2f%%" % (sum(histogram[250:]) / total * 100))
print("pixels == 255: %.2f%%" % (histogram[255] / total * 100))
```

只有此可选图像分析需要 Pillow：

```bash
python3 -m pip install pillow
```

仅凭已经剪切的 JPEG 无法唯一重建 21 项曲线。照片不会显示拍摄时使用了哪个 AE
索引，纯白像素也已经丢失原始亮度。确定最终曲线前，应进行多组受控测试。

## 刷机

不同固件的升级文件名和过程可能不同。常见情况是 ZIP 中的 BIN 名为
`FWHC940A.bin`，并复制到 SD 卡根目录。刷机前必须确认本机型所需文件名和恢复
方法。

刷机后测试：

1. 正常启动和菜单操作。
2. 远程夜间拍照。
3. PIR 唤醒夜间拍照。
4. 连续多次 PIR 拍照，检查 AE 收敛差异。
5. 日间照片、视频、存储和网络功能。

静态分析和正确校验和不能证明固件适用于所有硬件版本。

## 故障排除

### 输入校验和不匹配

请使用未修改的厂商原始固件。版本 2 不再提供宽泛的 `--force`，避免把损坏的
输入变成“校验和正确但内容未知”的输出。

### 没有自动配置

运行 `--scan`，比较固件版本和曲线，然后使用 `--ir-scale`、`--ir` 或
`--ir-values`。不要把其他哈希对应的型号配置强制应用到当前文件。

### 找不到 AE 运行时

固件可能使用了不同 SDK 结构。通过 UART 获取 `ae aetdump 0` 等信息，人工分析
固件，并只在确认表和所属分区后使用 `--ir-offset`。

### 找到多个 AE 结构

工具会停止而不是猜测。请使用人工确认的 `--ir-offset`，或为新布局增加带回归
测试的正式支持。

### 只找到一个运行时

某些固件可能确实只有一个相机运行时。已分析的 HC-940Ultra 和 HC-960Ultra
版本应有两个。在实际测试 PIR 照片前，不要假定 PIR 路径已经修复。

## 开发与回归测试

发布 `patch_ae.py` 修改前，至少运行：

```bash
python3 -m py_compile patch_ae.py
python3 patch_ae.py HC960-original.zip --dry-run
python3 patch_ae.py HC940-original.zip --dry-run
python3 patch_ae.py HC960-original.zip --verify-only
python3 patch_ae.py HC940-original.zip --verify-only
```

自动配置的预期 BIN SHA-256：

```text
HC-960Ultra: a66190b5f418a2e54c09042f154411777bb2b3f7ec339023a1331442600c4667
HC-940Ultra: a0a7b94cc9e1c4e7da51b8ddf4c8b18a619d2acecf8b874247ca3669e5bf9a53
```

不要把厂商原始固件或补丁固件提交到本仓库。

## 致谢

- [Novatek-FW-info / NTKFWinfo](https://github.com/EgorKin/Novatek-FW-info)：
  提供公开的格式和校验和解析参考。
- 提供原始固件哈希、运行时分析和受控夜间照片对比的贡献者。

## 许可证

工具和文档采用 [MIT](LICENSE)。该许可证不包括厂商固件、Suntek/Novatek
商标或相机硬件。本项目按现状提供；刷机风险由使用者承担。
