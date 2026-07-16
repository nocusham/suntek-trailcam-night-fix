# 修复 Suntek / Novatek 野外相机夜间照片过曝

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

[English](README.md) | 中文

`patch_ae.py` 用于分析和修改基于 Novatek `NVTPACK_FW_HDR2` 平台的部分
Suntek 野外相机固件中的夜间/红外自动曝光表。工具能够识别普通/远程运行时与
低功耗/PIR 运行时，也支持单摄像头和双摄像头布局。写入固件前后会重新计算所有
受影响分区的内部校验和、外层固件校验和，并用精确的字节白名单验证修改范围。

版本 3 专门改进了对 Suntek 频繁发布的新固件的处理。未知 SHA-256 不会仅仅因为
找到一张看似合理的表就被判定为“兼容”或“单摄像头”。工具现在会给出四个支持
等级；当型号、传感器或运行时身份不确定时，默认拒绝写入。

## 安全警告

刷写修改后的固件可能永久损坏相机。校验和正确并不代表硬件兼容。

- 保留与相机完全对应的原始厂家固件副本。
- 先运行 `--verify-only`、`--compat-check`、`--scan` 和 `--dry-run`。
- 不要复制其他固件版本中的固定偏移量。
- 测试时应能直接接触相机，最好准备 3.3 V UART 恢复通道。
- 不要信任来源不明的配置文件。

本仓库只包含工具、配置文件和文档，不包含也不会下载厂家固件。

## 支持等级

| 等级 | 含义 | 自动配置 | 自定义修改 |
|---|---|---:|---:|
| `verified` | BIN 的 SHA-256 与受信任配置完全一致，且分区、运行时、偏移、原始曲线、标记字符串和上下文指纹均验证通过 | 配置中定义时允许 | 允许 |
| `family-match` | SHA-256 未知，但构建标记、传感器标记、运行时数量、候选表数量和相对 SDK 结构符合已知型号系列 | 禁止 | 仅专家流程 |
| `structural-match` | NVTPACK 和 AE 结构合理，但型号或传感器身份无法确认 | 禁止 | 必须显式偏移并使用专家流程 |
| `unsupported` | 未找到完整支持的运行时/AE 结构，或结构相互矛盾 | 禁止 | 禁止 |

核心原则：

```text
已知 SHA-256 + 受信任配置
    -> 可允许自动修改

未知 SHA-256 + 高置信度型号系列匹配
    -> 自动分析和标记，但普通模式不写入

未知型号或传感器布局不明确
    -> 必须显式给出偏移，并绑定之前的扫描清单
```

## 已测试固件和内置配置

官方发布配置位于 `profiles/*.json`。脚本仍保留保守的内置后备数据，因此只下载
`patch_ae.py` 时，当前已测试固件的校验和验证和基本识别仍可使用。

| 布局/配置 | 型号/构建 | 原始 BIN SHA-256 | 摄像头设计 | 自动动作 |
|---|---|---|---|---|
| `hc960-ae55` | HC-960Ultra，2026-03-26 | `b391abec2bdf6ab1d48e357c94e0f56bb9e2703899b647609acec3faa30150fa` | 单摄像头，两个运行时 | 两个运行时均由 `110 x21` 改为 `55 x21` |
| `hc940-ae58` | HC-940Ultra，2025-04-23 | `9eb10ef5dd4057a891fb48a2b9cb9165e9ae3168a9b7e58aecc6299b90749c4a` | 单摄像头，两个运行时 | 两个运行时均由 `110..125` 改为校准后的 `58..66` |
| `hc950-dual-camera-2024` | HC-950Ultra / 950XFUltra，2024-08-08 | `e4db261f9228af5793d5952b45f9b6e9e41b2a50e264ac8971e5145d8cc19370` | IMX258M 日间 + SC223AP 夜间，两个运行时 | 仅识别 |
| `hc950-dual-camera-2026` | HC-950Ultra / 950XFUltra，2026-05-27 | `a6caf6be7e1a77dfe434ae78b959390b190f2e3b6e9b6e0cb5c8b29b2e6edf61` | IMX258M 日间 + SC223AP 夜间，两个运行时 | 仅识别 |

HC-950Ultra 的原厂夜间曝光实测良好，因此两个 HC-950 配置都不包含自动曝光修改。
只有在精确验证的固件上进行显式实验修改时，默认才会选择 SC223AP 夜间传感器。

查看已加载配置及其信任来源：

```bash
python3 patch_ae.py --list-profiles
```

## 已验证固件的快速使用

### 1. 验证校验和

```bash
python3 patch_ae.py firmware.zip --verify-only
```

该命令验证外层 NVTPACK 校验和和所有可识别的内部分区校验和。对于未知哈希，
`--verify-only` 不执行较慢的 AE 扫描；需要兼容性分类时请使用 `--compat-check`。

### 2. 扫描运行时、传感器和曲线

```bash
python3 patch_ae.py firmware.zip --scan --manifest scan.json
```

清单中会记录输入 SHA-256、支持等级、分区表、所有候选偏移、原始曲线、运行时/
传感器标签和上下文指纹。

### 3. 预览自动修改

仅适用于精确匹配的 HC-940Ultra 和 HC-960Ultra：

```bash
python3 patch_ae.py firmware.zip --dry-run
```

### 4. 写入修改后的固件

```bash
python3 patch_ae.py firmware.zip --manifest patch.json
```

ZIP 输入会保留原归档结构，默认输出 `firmware_patched.zip`；BIN 输入默认输出
`firmware_patched.bin`。覆盖已有文件需要 `--overwrite`。

## 处理新的未知 Suntek 固件

### 第一步：兼容性分类

```bash
python3 patch_ae.py new-firmware.zip \
  --compat-check \
  --manifest new-firmware-scan.json
```

示例结果：

```text
support level=family-match
model=HC-950Ultra / 950XFUltra
confidence=high
automatic_patch=no
```

型号系列匹配会综合多个独立证据，而不是只看型号字符串：

- 型号/构建前缀；
- 必需的传感器和 AE 参数标记；
- 普通/远程与低功耗/PIR 两个运行时；
- 每个运行时中预期的 AE 候选数量；
- AE 表的相对结构和上下文指纹。

如果已知双摄像头系列缺少任何一个预期候选表，就不能获得 `family-match`，而会
降级为 `structural-match`。这样可以避免把结构部分变化的 HC-950 固件误判为
单摄像头固件。

### 第二步：与已知布局比较

```bash
python3 patch_ae.py new-firmware.zip \
  --compare-layout hc950-dual-camera-2026
```

比较结果包括候选数量、原始曲线、绝对偏移、必需标记和相对上下文。新版本中的
绝对偏移变化很常见，绝不能直接复制旧版本偏移。

### 第三步：导出待审核配置

```bash
python3 patch_ae.py new-firmware.zip \
  --export-layout candidate-profile.json
```

导出的 JSON 会标记为 `"status": "unverified"`。在提交为官方配置前，必须人工
核对传感器顺序、曲线、运行时、偏移、标记字符串和指纹。

### 第四步：进行未验证的 dry-run

未知固件必须显式确认风险，并断言每个目标表的原始曲线。

假设 HC-950 系列的新固件中两个 SC223AP 夜间表都是 `110 x21`：

```bash
CURVE=110,110,110,110,110,110,110,110,110,110,110,110,110,110,110,110,110,110,110,110,110

python3 patch_ae.py new-firmware.zip \
  --ir 109 \
  --allow-unverified \
  --expect-ir "$CURVE" \
  --dry-run \
  --manifest dry-run.json
```

如果不同偏移的原始曲线不同，可分别绑定：

```bash
python3 patch_ae.py new-firmware.zip \
  --ir-scale 0.95 \
  --allow-unverified \
  --ir-offset 0x006c3a88 \
  --ir-offset 0x018353d8 \
  --expect-ir 0x006c3a88=110,110,110,110,110,110,110,110,110,110,110,110,110,110,110,110,110,110,110,110,110 \
  --expect-ir 0x018353d8=110,110,110,110,110,110,110,110,110,110,110,110,110,110,110,110,110,110,110,110,110 \
  --dry-run
```

`structural-match` 不能使用推测出的传感器名称。人工分析后必须通过 `--ir-offset`
显式指定每个目标。

### 第五步：将实际写入绑定到之前的扫描清单

```bash
python3 patch_ae.py new-firmware.zip \
  --ir 109 \
  --allow-unverified \
  --expect-ir "$CURVE" \
  --accept-scan-manifest new-firmware-scan.json \
  --manifest patch.json
```

只有当扫描清单中的输入 SHA-256、目标偏移和原始曲线与当前固件完全一致时，实际
写入才会继续。未验证输出会使用明显的文件名：

```text
new-firmware_UNVERIFIED_PATCHED.zip
```

## 运行时和传感器选择

已分析固件包含两个独立的运行时镜像：

| 运行时 | 常见分区 | 加载地址 | 功能 |
|---|---:|---:|---|
| 普通/远程 | ID 3 | `0x02700400` | 菜单、网络、远程拍摄、Linux/4G 主路径 |
| 低功耗/PIR | ID 9 | `0x00400400` | PIR 唤醒和独立拍摄 |

选择一个运行时：

```bash
python3 patch_ae.py firmware.zip --runtime pir --ir-scale 0.9 --dry-run
```

对于精确验证或高置信度 HC-950 布局：

```bash
python3 patch_ae.py firmware.zip --sensor night --ir 109 --dry-run
python3 patch_ae.py firmware.zip --sensor sc223ap --ir 109 --dry-run
python3 patch_ae.py firmware.zip --sensor day --ir-scale 0.95 --dry-run
python3 patch_ae.py firmware.zip --sensor all --ir 109 --dry-run
```

通常不应只修改一个运行时，否则远程拍摄和 PIR 拍摄会使用不同的曝光曲线。

## 配置文件注册表

JSON 配置把具体固件知识与解析/校验代码分离。新增精确固件版本时，不需要修改
分区解析器或校验和算法。配置包含：

- 精确 BIN SHA-256；
- 型号系列和构建日期；
- 必需的标记字符串；
- 每个候选表的分区 ID 和运行时角色；
- 精确 `tab_ratio_ir` 偏移和原始曲线；
- 传感器身份；
- 相对上下文指纹；
- 可选的自动目标曲线。

加载额外的识别配置：

```bash
python3 patch_ae.py firmware.zip \
  --profile-dir ./candidate-profiles \
  --compat-check
```

外部配置默认只用于识别，不能覆盖受信任的内置配置，也不能启用自动修改。只有在
独立审核所有文件后，专家才应显式信任：

```bash
python3 patch_ae.py firmware.zip \
  --profile-dir ./reviewed-profiles \
  --trust-external-profiles \
  --dry-run
```

## AE 结构和相对指纹

已分析 SDK 中三张 21 项曲线连续存放：

```text
tab_ratio_mov
+0x54  tab_ratio_photo
+0xa8  tab_ratio_ir
```

典型的过曝阈值块位于 `tab_ratio_ir + 0x25c`。版本 3 还会对 IR 曲线后的稳定区域
和阈值块进行哈希。这样可以在新固件中识别移动后的结构，同时不会把短字节序列
误当作传感器身份的证明。

## 修改模式

固定值：

```bash
python3 patch_ae.py firmware.zip --ir 55 --dry-run
```

保持曲线形状的比例缩放：

```bash
python3 patch_ae.py firmware.zip --ir-scale 0.50 --dry-run
```

显式 21 项曲线：

```bash
python3 patch_ae.py firmware.zip \
  --ir-values 58,58,58,58,58,58,58,58,58,58,58,58,58,61,63,66,66,66,66,66,66 \
  --dry-run
```

`--iso-cap` 仍是专家功能，必须同时提供人工确认的 `--iso-offset`。工具不会自动
搜索 ISO 字段。

## 每次写入都会验证的内容

- NVTPACK 头和分区表边界；
- 分区重叠和文件边界；
- 所有可识别的内部分区校验和；
- 外层 NVTPACK 校验和；
- AE 表结构和断言的原始曲线；
- 修改后的目标曲线；
- 所有被修改分区的校验和字段；
- 精确的修改字节白名单；
- 输出 BIN/ZIP 的重新读取和往返验证。

往返验证失败时会删除输出文件。

## 要求和测试

- Python 3.10 或更高版本。
- 不需要第三方 Python 包。
- 厂家 `.bin`，或只包含一个固件 `.bin` 的 `.zip`。

运行不需要厂家固件的仓库测试：

```bash
python3 -m unittest discover -s tests -v
```

用于集成回归测试的四个厂家固件镜像不会包含在本仓库中。

## 限制

- 型号系列识别是基于证据的高置信度判断，不等于硬件兼容证明。
- 未知 HC-950 固件中的传感器分配依赖两个运行时都保持已验证的双候选顺序；任何
  数量不一致都会禁用型号系列传感器分配。
- 新 SDK 可能彻底改变 AE 数据结构，使当前扫描器无法识别。
- 仅凭 JPEG 无法确定拍摄时使用了 21 项曲线中的哪一项。
- 校验和正确不能阻止语义上错误但结构上有效的修改损坏相机。

## 许可证

MIT。请参见仓库中的 `LICENSE`。
