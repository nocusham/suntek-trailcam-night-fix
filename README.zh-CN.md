# 修复 Suntek / Novatek 猎相机夜间照片过曝

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

[English](README.md) | 中文

本项目用于分析和修补部分采用 Novatek `NVTPACK_FW_HDR2` 平台的 Suntek
猎相机固件。工具会修改夜间红外自动曝光目标曲线 `tab_ratio_ir`，重新计算所有
受影响分区的内部校验和以及整个固件的外层校验和，并对写出的固件进行逐字节
回读验证。

版本 2.1 能同时理解固件中的两个维度：

- **运行时：**普通/远程运行时，以及低功耗/PIR 唤醒运行时；
- **传感器：**单个共用相机模块，或者相互独立的日间和夜间相机模块。

这对 HC-950Ultra 很重要：每个运行时中都包含一份 IMX258M 日间相机 AE 配置
和一份 SC223AP 夜间相机 AE 配置。工具现在会正确识别并标记全部四份配置，而
不会再把它们当作无法处理的歧义。

## 安全警告

刷写修改后的固件可能永久损坏相机。即使型号名称相同，也不能保证固件适用于
另一硬件版本。

- 保留该相机原始固件的完整、未修改副本。
- 写入或刷机前先运行 `--verify-only`、`--scan` 和 `--dry-run`。
- 将输入文件的 SHA-256 与下表中的已验证布局严格比较。
- 最好在可以直接接触相机的情况下测试，并准备 3.3 V UART 恢复连接。
- 出现错误或操作中断后，不要刷写输出文件。

本仓库只提供工具和文档，不分发厂商固件。

## 已测试和识别的固件

“识别固件”和“自动修改曝光”是两个独立概念。固件可以被完整识别、扫描和验证，
但不一定存在推荐的曝光补丁。

| 布局/配置 | 型号/版本 | 原始 BIN SHA-256 | 相机结构 | 自动操作 |
|---|---|---|---|---|
| `hc960-ae55` | HC-960Ultra，2026-03-26 | `b391abec2bdf6ab1d48e357c94e0f56bb9e2703899b647609acec3faa30150fa` | 单相机、两个运行时 | 两个运行时中的 `110 x21` → `55 x21` |
| `hc940-ae58` | HC-940Ultra，2025-04-23 | `9eb10ef5dd4057a891fb48a2b9cb9165e9ae3168a9b7e58aecc6299b90749c4a` | 单相机、两个运行时 | 两个运行时中的 `110..125` → 标定后的 `58..66` |
| `hc950-dual-camera` | HC-950Ultra / 950XFUltra，2024-08-08 | `e4db261f9228af5793d5952b45f9b6e9e41b2a50e264ac8971e5145d8cc19370` | IMX258M 日间 + SC223AP 夜间、两个运行时 | **只识别；不建议修改出厂曝光** |

HC-940Ultra 使用以下 21 项测试曲线：

```text
58, 58, 58, 58, 58, 58, 58,
58, 58, 58, 58, 58, 58, 61,
63, 66, 66, 66, 66, 66, 66
```

受控实拍对比表明，固定值 55 几乎消除了高光剪切，同时仍保留了可用的主体细节。
该 `58..66` 曲线保留厂商原曲线形状，属于研究/测试标定，并非厂商发布版本。

列出自动补丁配置和所有已识别固件布局：

```bash
python3 patch_ae.py --list-profiles
```

## 固件结构

### 两个运行时

已分析的 HC-940Ultra、HC-950Ultra 和 HC-960Ultra 固件包含：

| 运行时 | 常见分区 | 加载地址 | 功能 |
|---|---:|---:|---|
| 普通运行时 | ID 3 | `0x02700400` | 菜单、网络、远程拍照、Linux/4G 主机路径 |
| 低功耗运行时 | ID 9 | `0x00400400` | PIR 唤醒、独立/低功耗拍照 |

每个运行时都有自己的 AE 数据。只修改分区 3 可能修复远程照片，但 PIR 照片仍然
使用分区 9 中未修改的表。

### HC-950Ultra 双相机布局

已验证的 HC-950Ultra 固件在每个运行时中都包含两份 AE 配置：

| 运行时 | 传感器 | 用途 | `tab_ratio_ir` 文件偏移 |
|---|---|---|---:|
| 普通/远程 | IMX258M | 日间相机 | `0x006c2c60` |
| 普通/远程 | SC223AP | 夜间相机 | `0x006c3904` |
| 低功耗/PIR | IMX258M | 日间相机 | `0x01893924` |
| 低功耗/PIR | SC223AP | 夜间相机 | `0x018945ec` |

四份原始曲线都是 `110 x21`。这个数值本身**不能**证明 HC-950Ultra 过曝：
其专用 SC223AP 夜间传感器、IQ 参数、镜头、红外照明以及 AE 边界参数均与单相机
型号不同。实际 HC-950Ultra 夜间照片曝光良好，因此版本 2.1 故意不提供自动
HC-950 曝光目标。

### AE 结构识别

一个候选配置包含连续的三组 21 项曲线：

```text
tab_ratio_mov
+0x54  tab_ratio_photo
+0xa8  tab_ratio_ir
```

在已分析的 SDK 布局中，特征性的 `over_exposure` 阈值表位于
`tab_ratio_ir + 0x25c`。对于已知固件哈希，工具还会验证精确分区、运行时角色、
表偏移、原始曲线，以及 HC-950Ultra 的传感器标识字符串。

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

该命令验证整个文件的校验和，以及所有可通过 Novatek `55 aa` 标记识别的内部分区
校验和。对于哈希完全匹配的已知固件，还会显示识别出的相机布局。

### 2. 扫描运行时、传感器和曲线

```bash
python3 patch_ae.py firmware.zip --scan
```

HC-940Ultra 或 HC-960Ultra 应显示两项：普通/远程和低功耗/PIR。已验证的
HC-950Ultra 应显示四项：两个运行时中的 IMX258M 日间配置和 SC223AP 夜间配置。

### 3. 预览自动补丁

对于哈希完全匹配的 HC-940Ultra 和 HC-960Ultra：

```bash
python3 patch_ae.py firmware.zip --dry-run
```

工具根据输入 SHA-256 选择配置，不写入文件。

对 HC-950Ultra 执行相同命令时，工具会故意停止并说明没有推荐的自动曝光修改。

### 4. 创建自动补丁固件

对于精确匹配的 HC-940Ultra 或 HC-960Ultra：

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

清单包含型号/布局识别、运行时、传感器、旧/新曲线、修改偏移、校验和、哈希和
验证结果。

### 5. 再次验证输出

```bash
python3 patch_ae.py firmware_patched.zip --verify-only
```

写入后工具会自动执行相同的回读验证。如果回读验证失败，输出文件会被删除。

## HC-950Ultra 用法

HC-950Ultra 可以被完整识别、扫描、选择、修改、重新计算校验和、生成清单并回读
验证。由于其出厂夜间图像已经良好，所以没有推荐的自动曝光补丁。

### 安全检查

```bash
python3 patch_ae.py 950XFUltra_20240808.zip --verify-only
python3 patch_ae.py 950XFUltra_20240808.zip --scan
```

### 实验性修改夜间传感器

只有在图像分析或 UART 数据明确表明需要修改时才应进行：

```bash
python3 patch_ae.py 950XFUltra_20240808.zip \
  --sensor night --ir-scale 0.95 --dry-run
```

对于精确识别的 HC-950Ultra 哈希，使用自定义曲线但不指定 `--sensor` 时，默认选择
两个运行时中的 **SC223AP 夜间传感器**：

```bash
python3 patch_ae.py 950XFUltra_20240808.zip --ir 109 --dry-run
```

等价的明确选择：

```bash
python3 patch_ae.py 950XFUltra_20240808.zip \
  --sensor sc223ap --ir 109 --dry-run
```

只选择 IMX258M 日间相机 AE 配置：

```bash
python3 patch_ae.py 950XFUltra_20240808.zip \
  --sensor day --ir 109 --dry-run
```

只有确实要测试全部四份配置时才使用：

```bash
python3 patch_ae.py 950XFUltra_20240808.zip \
  --sensor all --ir 109 --dry-run
```

运行时选择和传感器选择可以组合：

```bash
python3 patch_ae.py 950XFUltra_20240808.zip \
  --runtime pir --sensor night --ir 109 --dry-run
```

## 自定义曲线模式

未知固件没有自动目标。检查 `--scan` 结果后，必须明确选择一种模式。

### 固定曲线

```bash
python3 patch_ae.py firmware.bin --ir 55
```

### 按比例缩放原曲线

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

## 运行时、传感器和偏移选择

默认在两个运行时中修改所有已选择的传感器配置。运行时选择可重复使用：

```bash
python3 patch_ae.py firmware.bin --runtime normal --ir 55
python3 patch_ae.py firmware.bin --runtime pir --ir 55
python3 patch_ae.py firmware.bin --runtime pid:9 --ir-scale 0.50
```

传感器选择可重复使用：

```bash
--sensor single
--sensor day
--sensor night
--sensor imx258m
--sensor sc223ap
--sensor all
```

已弃用的 `--all` 表示所有运行时；在双相机固件中，只有确实要修改所有传感器
配置时才使用 `--sensor all`。

可以提供人工确认的 IR 表偏移。工具会验证其所属分区及周围 AE 结构：

```bash
python3 patch_ae.py firmware.bin --ir-offset 0x006c3904 --ir 109
```

重复 `--ir-offset` 可选择多个表。手动偏移不能与 `--runtime` 或 `--sensor` 组合。

对于未知固件，如果一个运行时中存在多个无法识别的 AE 结构，工具允许扫描，但
拒绝猜测传感器分组。人工分析后必须使用明确的 `--ir-offset`。

## ISO 上限：只允许明确偏移

自动搜索 ISO 字段已被禁用，因为该数据模式不够唯一。专家模式必须提供已验证的
偏移：

```bash
python3 patch_ae.py firmware.bin --ir 55 \
  --iso-cap 3200 --iso-offset 0x123456
```

偏移必须 4 字节对齐、位于已选择的运行时分区内、包含合理的 ISO 值，并且后面
紧跟 `iso_prv.l = 100`。对于双相机固件，必须独立确认每个 ISO 偏移属于目标
传感器配置；工具不会自动识别传感器专用 ISO 字段。

## HC-950Ultra 的第二个固件包

厂商升级流程包含两个不同阶段：

1. `950XFUltra_20240808` 中的 `FWHC940A.bin` 更新 Novatek 相机系统、两个图像
   传感器、两个相机运行时、UI、存储和网络主机软件。这是 `patch_ae.py` 唯一
   读取的固件包。
2. `16009.1047.00.01.29.05-update fw` 包含用于独立 4G 调制解调器的 `.pac`
   镜像和 `upgrade_tool`。它不是第二个图像传感器固件，不属于本工具范围。

不要把调制解调器 `.pac`、`upgrade_tool` 或调制解调器升级压缩包传给
`patch_ae.py`。

## 版本 2.1 的验证项目

修补前：

1. `NVTPACK_FW_HDR2` 版本标记。
2. 读取头部 `0x14` 的分区表指针和 `0x18` 的分区数量。
3. 按实际顺序 `{offset, size, partition_id}` 解析记录。
4. 检查分区边界和重叠。
5. 验证整个文件校验和。
6. 验证每个可识别的内部分区校验和。
7. 验证 AE 表结构及其分区归属。
8. 对已识别布局验证精确 SHA-256、偏移、运行时角色、原始曲线和必要的传感器
   标记。
9. 明确处理运行时和传感器选择，不猜测未知的多传感器布局。

修补后：

1. 每个被修改的分区都重新计算内部校验和。
2. 最后重新计算整个文件校验和。
3. 回读并精确比较目标曲线和可选 ISO 字段。
4. 字节白名单拒绝任何请求范围和校验和字段之外的改动。
5. 通过唯一临时文件写入，再原子重命名。
6. 从磁盘或 ZIP 中重新读取 BIN 并再次验证。

Novatek 使用的是位置加权的 16 位二进制补码加法校验和。实现始终按小端解释
数据，并只写入外层 16 位字段。

## 评估夜间图像

保持相机和场景不变，分别拍摄远程触发和 PIR 触发照片。画面中应同时包含明亮的
近距离植物和较暗的远距离主体。

排除底部信息栏后的简单高光剪切测量：

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
索引，纯白像素也已经丢失原始亮度。

## 刷机

不同固件的升级文件名和过程可能不同。常见情况是 ZIP 中的 BIN 名为
`FWHC940A.bin`，并复制到 SD 卡根目录。刷机前必须确认本机型所需文件名和恢复
方法。

刷机后测试：

1. 正常启动和菜单操作；
2. 远程夜间拍照；
3. PIR 唤醒夜间拍照；
4. 连续多次 PIR 拍照；
5. 日间照片、视频、存储和网络功能；
6. 对 HC-950Ultra，测试两个物理相机模块以及日/夜切换。

静态分析和正确校验和不能证明固件适用于所有硬件版本。

## 故障排除

### 输入校验和不匹配

请使用未修改的厂商原始固件。版本 2 不提供宽泛的 `--force`，避免把损坏的输入
变成“校验和正确但内容未知”的输出。

### 已识别的 HC-950Ultra 没有自动配置

这是故意设计。其出厂夜间曝光良好。使用 `--scan` 检查即可。实验性修改必须
明确指定曲线模式；对于精确识别的布局，默认选择 SC223AP 夜间传感器。

### 没有自动配置

运行 `--scan`，比较固件版本和曲线，然后使用 `--ir-scale`、`--ir` 或
`--ir-values`。不要使用其他哈希对应的配置。

### 未知固件中存在多个 AE 结构

版本 2.1 已正式支持经过验证的 HC-950Ultra 双相机布局。未知多 AE 布局会由
`--scan` 显示，但修改必须使用人工确认的 `--ir-offset`。

### 只找到一个运行时

某些固件可能确实只有一个运行时。本仓库验证的三个固件版本都有普通/远程和
低功耗/PIR 运行时。在实际测试 PIR 照片前，不要假定 PIR 路径已经修复。

## 开发与回归测试

发布 `patch_ae.py` 修改前，至少运行：

```bash
python3 -m py_compile patch_ae.py
python3 patch_ae.py HC960-original.zip --dry-run
python3 patch_ae.py HC940-original.zip --dry-run
python3 patch_ae.py HC950-original.zip --scan
python3 patch_ae.py HC950-original.zip --ir 109 --dry-run
python3 patch_ae.py HC950-original.zip --sensor day --ir 109 --dry-run
python3 patch_ae.py HC950-original.zip --sensor all --ir 109 --dry-run
```

预期回归 BIN SHA-256：

```text
HC-960Ultra 自动配置：
a66190b5f418a2e54c09042f154411777bb2b3f7ec339023a1331442600c4667

HC-940Ultra 自动配置：
a0a7b94cc9e1c4e7da51b8ddf4c8b18a619d2acecf8b874247ca3669e5bf9a53

HC-950Ultra 仅用于测试的固定值 109，默认 SC223AP 夜间传感器：
a03b34e0b24f5dde27a3b39598d69f38c5ff064e75a98775c3b3c567fcadbb3c
```

HC-950 哈希只用于软件回归测试，**不是**推荐刷写的固件。

不要把厂商原始固件或补丁固件提交到本仓库。

## 致谢

- [Novatek-FW-info / NTKFWinfo](https://github.com/EgorKin/Novatek-FW-info)：
  提供公开的格式和校验和解析参考。
- 提供原始固件哈希、运行时/传感器分析和受控夜间照片对比的贡献者。

## 许可证

工具和文档采用 [MIT](LICENSE)。该许可证不包括厂商固件、Suntek/Novatek
商标或相机硬件。本项目按现状提供；刷机风险由使用者承担。
