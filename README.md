# Fixing night-photo overexposure on Suntek / Novatek trail cameras

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

English | [中文](README.zh-CN.md)

This project analyzes and patches excessive night/IR auto-exposure in selected
Suntek trail-camera firmware based on the Novatek `NVTPACK_FW_HDR2` platform.
The patch changes the firmware's `tab_ratio_ir` target curve and recalculates all
affected checksums so that the image remains internally consistent.

Version 2 fixes an important architectural detail: the examined firmware images
contain **two independent camera runtimes**. The normal/network runtime handles
remote captures, while a second low-power runtime handles PIR wake-up captures.
Each runtime has its own AE table. Patching only the first one can fix remote
photos while PIR photos remain overexposed.

## Safety warning

Flashing modified firmware can permanently brick a camera. There is no warranty
and no guarantee that a firmware image is compatible with another hardware
revision, even when the model name looks identical.

- Keep an untouched copy of the exact original firmware.
- Run `--verify-only` and `--dry-run` before writing or flashing anything.
- Compare the SHA-256 hash with the verified profile table below.
- Test with physical access to the camera and preferably a 3.3 V UART recovery
  connection.
- Never flash an output after an error or interrupted operation.

This repository contains tooling and documentation only. It does not distribute
manufacturer firmware.

## Tested firmware profiles

Automatic profiles are selected only when the input BIN has an exact known
SHA-256 hash.

| Profile | Model/build | Original BIN SHA-256 | Original IR curve | Target curve |
|---|---|---|---|---|
| `hc960-ae55` | HC-960Ultra, 2026-03-26 | `b391abec2bdf6ab1d48e357c94e0f56bb9e2703899b647609acec3faa30150fa` | `110 x21` | `55 x21` |
| `hc940-ae58` | HC-940Ultra, 2025-04-23 | `9eb10ef5dd4057a891fb48a2b9cb9165e9ae3168a9b7e58aecc6299b90749c4a` | `110..125` | `58..66` |

The HC-940Ultra target is the following 21-entry test curve:

```text
58, 58, 58, 58, 58, 58, 58,
58, 58, 58, 58, 58, 58, 61,
63, 66, 66, 66, 66, 66, 66
```

It was derived from comparison images after a flat value of 55 removed nearly
all clipping while retaining usable subject detail. The `58..66` curve preserves
the shape of the manufacturer's original `110..125` curve and uses some of the
remaining exposure headroom. It is a research/test calibration, not a vendor
release.

Run this to print profiles and hashes:

```bash
python3 patch_ae.py --list-profiles
```

## Why both runtimes must be patched

The examined HC-940Ultra and HC-960Ultra images contain:

| Runtime | Typical partition | Load address | Function |
|---|---:|---:|---|
| Normal runtime | ID 3 | `0x02700400` | menu, network, remote capture, Linux/4G host path |
| Low-power runtime | ID 9 | `0x00400400` | PIR wake-up, standalone/low-power capture |

Both contain a separate sequence of three 21-entry AE curves:

```text
tab_ratio_mov
+0x54  tab_ratio_photo
+0xA8  tab_ratio_ir
```

In the examined SDK layout, a characteristic `over_exposure` threshold pair is
found at `tab_ratio_ir + 0x25c`. Version 2 uses this structure, partition
boundaries, runtime checksums, and ambiguity checks instead of relying on one
hard-coded table offset.

## Requirements

- Python 3.10 or newer.
- The original manufacturer `.bin`, or a `.zip` containing exactly one firmware
  `.bin`.
- No third-party Python packages are required.

Optional independent inspection:

- [Novatek-FW-info / NTKFWinfo](https://github.com/EgorKin/Novatek-FW-info)
- A 3.3 V USB-UART adapter for console diagnosis and recovery.

## Quick start

### 1. Verify the original image

```bash
python3 patch_ae.py firmware.zip --verify-only
```

The command checks the whole-file checksum and every internal partition checksum
that can be identified by its Novatek `55 aa` checksum marker. On the tested
images this includes the two configuration partitions, both camera runtimes,
and the bootloader.

### 2. Scan the runtimes and original curves

```bash
python3 patch_ae.py firmware.zip --scan
```

Expected output includes one normal/remote runtime and one low-power/PIR runtime.
Stop if the result is ambiguous or does not match the expected camera build.

### 3. Preview the automatic patch

For either exact profile listed above:

```bash
python3 patch_ae.py firmware.zip --dry-run
```

The input SHA-256 selects the appropriate profile. Nothing is written.

### 4. Create the patched image

```bash
python3 patch_ae.py firmware.zip
```

- ZIP input defaults to `firmware_patched.zip` and preserves the archive layout.
- BIN input defaults to `firmware_patched.bin`.
- The input file is never overwritten.
- Existing output files require `--overwrite`.

Create a JSON audit manifest at the same time:

```bash
python3 patch_ae.py firmware.zip --manifest patch-manifest.json
```

### 5. Verify the produced image again

```bash
python3 patch_ae.py firmware_patched.zip --verify-only
```

The tool also performs this check automatically after writing. A failed
round-trip check deletes the output.

## Custom curve modes

Unknown firmware has no automatic target. After reviewing `--scan`, select one
of these explicit modes.

### Flat curve

Backward-compatible behavior, setting all 21 entries to the same value:

```bash
python3 patch_ae.py firmware.bin --ir 55
```

### Scale the existing curve

Preserves the curve shape:

```bash
python3 patch_ae.py firmware.bin --ir-scale 0.50
```

For example, `110,115,120,125` becomes approximately `55,58,60,63`.

### Explicit 21-entry curve

```bash
python3 patch_ae.py firmware.bin --ir-values \
  58,58,58,58,58,58,58,58,58,58,58,58,58,61,63,66,66,66,66,66,66
```

The values must be in `1..255` and monotonic non-decreasing.

## Runtime and offset selection

All detected runtimes are patched by default. Limiting the patch is an expert
operation and can intentionally leave one trigger path unchanged.

```bash
python3 patch_ae.py firmware.bin --runtime normal --ir 55
python3 patch_ae.py firmware.bin --runtime pir --ir 55
python3 patch_ae.py firmware.bin --runtime pid:9 --ir-scale 0.50
```

Selectors are repeatable. The deprecated `--all` option is accepted but no
longer changes behavior.

A manually confirmed IR-table offset can be supplied and is validated against
its containing partition and surrounding AE structure:

```bash
python3 patch_ae.py firmware.bin --ir-offset 0x006cb628 --ir 55
```

Repeat `--ir-offset` to patch multiple explicitly selected tables.

## ISO cap: explicit offsets only

Version 1 searched for the first plausible `{iso_prv.h, 100}` pair. That pattern
is not unique enough for safe automatic patching. Version 2 requires a manually
verified offset:

```bash
python3 patch_ae.py firmware.bin --ir 55 \
  --iso-cap 3200 --iso-offset 0x123456
```

The offset must be aligned, inside a selected AE runtime, contain a plausible ISO
value, and be followed by `iso_prv.l = 100`. Repeat `--iso-offset` when separate
runtime copies must both be changed.

## What version 2 validates

Before patching:

1. `NVTPACK_FW_HDR2` version marker.
2. Partition-table pointer from header offset `0x14` and count from `0x18`.
3. Records in the actual order `{offset, size, partition_id}`.
4. Partition bounds and overlap.
5. Whole-file checksum.
6. Every detectable internal partition checksum.
7. Exactly one unambiguous AE structure per selected runtime.
8. Profile SHA-256 and expected original curve, when a profile is used.

After patching:

1. Every changed runtime receives a recalculated internal checksum.
2. The whole-file checksum is recalculated afterward.
3. Target curves and optional ISO fields are read back exactly.
4. A byte-level whitelist rejects changes outside requested data and checksum
   fields.
5. The output is written through a unique temporary file and atomically renamed.
6. The BIN is read back from disk or ZIP and verified again.

The Novatek checksum is a position-weighted, additive 16-bit two's-complement
checksum. The implementation explicitly interprets words as little-endian and
writes the outer field as 16 bits.

## Diagnosing image quality

A useful test sequence keeps the camera and scene unchanged and captures both
remote and PIR images. Include bright foreground vegetation and a darker subject
farther away.

A simple clipping measurement, excluding the camera's bottom information bar:

```python
from PIL import Image

image = Image.open("night.jpg").convert("L")
image = image.crop((0, 0, image.width, image.height - 100))
histogram = image.histogram()
total = sum(histogram)
print("pixels >= 250: %.2f%%" % (sum(histogram[250:]) / total * 100))
print("pixels == 255: %.2f%%" % (histogram[255] / total * 100))
```

Pillow is needed only for this optional analysis:

```bash
python3 -m pip install pillow
```

A curve cannot be uniquely reconstructed from clipped JPEGs alone. The images do
not reveal which of the 21 AE indices was active, and pure-white pixels have
already lost their original brightness. Use several controlled test captures
before deciding that a curve is final.

## Flashing

The exact update filename and procedure can vary by build. Commonly the patched
BIN inside the ZIP is named `FWHC940A.bin` and is copied to the SD-card root.
Confirm the expected name and recovery method for your camera before flashing.

After flashing, test:

1. Normal boot and menu operation.
2. Remote night capture.
3. PIR wake-up night capture.
4. Several consecutive PIR captures to detect AE settling differences.
5. Day images, video, storage, and network functions.

Static analysis and valid checksums do not prove compatibility with every camera
revision.

## Troubleshooting

### Input checksum mismatch

Use a pristine manufacturer image. Version 2 deliberately has no broad `--force`
option that can turn a damaged input into a checksum-consistent but unknown
output.

### No automatic profile

Run `--scan`, compare the firmware build and curves, then use `--ir-scale`,
`--ir`, or `--ir-values`. Do not select a model profile for a different hash.

### No AE runtime found

The firmware may use a different SDK structure. Obtain UART output such as
`ae aetdump 0`, inspect the image manually, and use `--ir-offset` only after the
table and containing partition are confirmed.

### Multiple AE structures found

The tool stops rather than guessing. Use a manually verified `--ir-offset`, or
add support for the new layout with test firmware and regression checks.

### Only one runtime found

Some firmware may genuinely contain one camera runtime. On the examined
HC-940Ultra and HC-960Ultra builds, two are expected. Do not assume PIR is fixed
until a PIR image has been tested.

## Development and regression checks

Before publishing a change to `patch_ae.py`, test at least:

```bash
python3 -m py_compile patch_ae.py
python3 patch_ae.py HC960-original.zip --dry-run
python3 patch_ae.py HC940-original.zip --dry-run
python3 patch_ae.py HC960-original.zip --verify-only
python3 patch_ae.py HC940-original.zip --verify-only
```

Expected automatic-profile BIN hashes:

```text
HC-960Ultra: a66190b5f418a2e54c09042f154411777bb2b3f7ec339023a1331442600c4667
HC-940Ultra: a0a7b94cc9e1c4e7da51b8ddf4c8b18a619d2acecf8b874247ca3669e5bf9a53
```

No manufacturer firmware or patched firmware should be committed to this
repository.

## Credits

- [Novatek-FW-info / NTKFWinfo](https://github.com/EgorKin/Novatek-FW-info)
  for public documentation and checksum parsing references.
- Contributors who supplied original firmware hashes, runtime analysis, and
  controlled night-image comparisons.

## License

[MIT](LICENSE) for the tool and documentation only. Manufacturer firmware,
Suntek and Novatek trademarks, and camera hardware are not covered by this
license. Provided as-is; flashing is at your own risk.
