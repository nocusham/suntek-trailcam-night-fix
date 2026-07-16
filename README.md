# Fixing night-photo overexposure on Suntek / Novatek trail cameras

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

English | [中文](README.zh-CN.md)

This project analyzes and patches night/IR auto-exposure tables in selected
Suntek trail-camera firmware based on the Novatek `NVTPACK_FW_HDR2` platform.
The tool changes the firmware's `tab_ratio_ir` target curve, recalculates every
affected internal checksum, recalculates the outer firmware checksum, and then
verifies the written image byte-for-byte.

Version 2.1 understands two independent dimensions of the camera firmware:

- **runtime:** normal/remote operation and low-power/PIR wake-up operation;
- **sensor:** a single shared camera module, or separate day and night modules.

This matters on the HC-950Ultra: each runtime contains one AE configuration for
the IMX258M day camera and another for the SC223AP night camera. The tool now
recognizes and labels all four configurations instead of treating them as an
ambiguous error.

## Safety warning

Flashing modified firmware can permanently brick a camera. There is no warranty
and no guarantee that a firmware image is compatible with another hardware
revision, even when the model name looks identical.

- Keep an untouched copy of the exact original firmware.
- Run `--verify-only`, `--scan`, and `--dry-run` before writing or flashing.
- Compare the input SHA-256 with the verified-layout table below.
- Test with physical access to the camera and preferably a 3.3 V UART recovery
  connection.
- Never flash an output produced after an error or interrupted operation.

This repository contains tooling and documentation only. It does not distribute
manufacturer firmware.

## Tested and recognized firmware

Recognition and automatic patching are intentionally separate. A firmware can
be recognized and fully inspectable without having a recommended exposure
change.

| Layout/profile | Model/build | Original BIN SHA-256 | Camera design | Automatic action |
|---|---|---|---|---|
| `hc960-ae55` | HC-960Ultra, 2026-03-26 | `b391abec2bdf6ab1d48e357c94e0f56bb9e2703899b647609acec3faa30150fa` | Single camera, two runtimes | `110 x21` → `55 x21` in both runtimes |
| `hc940-ae58` | HC-940Ultra, 2025-04-23 | `9eb10ef5dd4057a891fb48a2b9cb9165e9ae3168a9b7e58aecc6299b90749c4a` | Single camera, two runtimes | `110..125` → calibrated `58..66` in both runtimes |
| `hc950-dual-camera` | HC-950Ultra / 950XFUltra, 2024-08-08 | `e4db261f9228af5793d5952b45f9b6e9e41b2a50e264ac8971e5145d8cc19370` | IMX258M day + SC223AP night, two runtimes | **Recognition only; no factory exposure change is recommended** |

The HC-940Ultra test curve is:

```text
58, 58, 58, 58, 58, 58, 58,
58, 58, 58, 58, 58, 58, 61,
63, 66, 66, 66, 66, 66, 66
```

It was derived from controlled comparison images after a flat value of 55
removed nearly all clipping while retaining usable subject detail. It is a
research/test calibration, not a vendor release.

List automatic profiles and all recognized layouts:

```bash
python3 patch_ae.py --list-profiles
```

## Firmware architecture

### Two runtimes

The examined HC-940Ultra, HC-950Ultra, and HC-960Ultra images contain:

| Runtime | Typical partition | Load address | Function |
|---|---:|---:|---|
| Normal runtime | ID 3 | `0x02700400` | menu, network, remote capture, Linux/4G host path |
| Low-power runtime | ID 9 | `0x00400400` | PIR wake-up, standalone/low-power capture |

Each runtime owns its own AE data. A patch applied only to partition 3 can fix
remote captures while PIR captures continue using the unmodified table in
partition 9.

### HC-950Ultra dual-camera layout

The verified HC-950Ultra firmware contains two AE configurations in each
runtime:

| Runtime | Sensor | Purpose | `tab_ratio_ir` file offset |
|---|---|---|---:|
| Normal/remote | IMX258M | day camera | `0x006c2c60` |
| Normal/remote | SC223AP | night camera | `0x006c3904` |
| Low-power/PIR | IMX258M | day camera | `0x01893924` |
| Low-power/PIR | SC223AP | night camera | `0x018945ec` |

All four original curves are `110 x21`. That value alone does **not** prove
that the HC-950Ultra is overexposed: the dedicated SC223AP night sensor, sensor
IQ parameters, lens, IR illumination, and AE boundary settings differ from the
single-camera models. Real HC-950Ultra night images were reported as correctly
exposed, so version 2.1 deliberately has no automatic HC-950 patch target.

### AE structure used for detection

A candidate contains three consecutive 21-entry curves:

```text
tab_ratio_mov
+0x54  tab_ratio_photo
+0xa8  tab_ratio_ir
```

In the examined SDK layout, a characteristic `over_exposure` threshold pair is
located at `tab_ratio_ir + 0x25c`. Known firmware hashes additionally validate
the exact partition, runtime role, table offset, original curve, and—on the
HC-950Ultra—sensor identity strings.

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
that can be identified by its Novatek `55 aa` checksum marker. For an exact
known hash, it also prints the recognized camera layout.

### 2. Scan runtimes, sensors, and curves

```bash
python3 patch_ae.py firmware.zip --scan
```

A HC-940Ultra or HC-960Ultra scan should show two entries: one normal/remote and
one low-power/PIR entry. A verified HC-950Ultra scan should show four entries:
IMX258M day and SC223AP night in both runtimes.

### 3. Preview an automatic patch

For the exact HC-940Ultra and HC-960Ultra profiles:

```bash
python3 patch_ae.py firmware.zip --dry-run
```

The input SHA-256 selects the profile. Nothing is written.

The same command on the HC-950Ultra intentionally stops with a message that no
automatic exposure change is recommended.

### 4. Create an automatic patched image

For an exact HC-940Ultra or HC-960Ultra profile:

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

The manifest includes model/layout recognition, runtime, sensor, old/new curves,
changed offsets, checksums, hashes, and verification results.

### 5. Verify the produced image again

```bash
python3 patch_ae.py firmware_patched.zip --verify-only
```

The tool already performs this round-trip check after writing. A failed
round-trip check deletes the output.

## HC-950Ultra usage

The HC-950Ultra is fully recognized, scanned, selected, patched, checksummed,
manifested, and round-trip verified. It simply has no recommended automatic
exposure modification because its factory night image is already good.

### Safe inspection

```bash
python3 patch_ae.py 950XFUltra_20240808.zip --verify-only
python3 patch_ae.py 950XFUltra_20240808.zip --scan
```

### Experimental night-sensor change

Only perform this after image-based or UART-based evidence that a change is
actually needed:

```bash
python3 patch_ae.py 950XFUltra_20240808.zip \
  --sensor night --ir-scale 0.95 --dry-run
```

On the exact recognized HC-950Ultra hash, an explicit custom curve with no
`--sensor` option defaults to the **SC223AP night sensor** in both runtimes:

```bash
python3 patch_ae.py 950XFUltra_20240808.zip --ir 109 --dry-run
```

Equivalent explicit selection:

```bash
python3 patch_ae.py 950XFUltra_20240808.zip \
  --sensor sc223ap --ir 109 --dry-run
```

Select the IMX258M day-camera AE configurations only:

```bash
python3 patch_ae.py 950XFUltra_20240808.zip \
  --sensor day --ir 109 --dry-run
```

Select all four day/night × normal/PIR configurations only when that is the
intentional test:

```bash
python3 patch_ae.py 950XFUltra_20240808.zip \
  --sensor all --ir 109 --dry-run
```

A runtime selector can be combined with a sensor selector:

```bash
python3 patch_ae.py 950XFUltra_20240808.zip \
  --runtime pir --sensor night --ir 109 --dry-run
```

## Custom curve modes

Unknown firmware has no automatic target. After reviewing `--scan`, choose an
explicit mode.

### Flat curve

```bash
python3 patch_ae.py firmware.bin --ir 55
```

### Scale the existing curve

```bash
python3 patch_ae.py firmware.bin --ir-scale 0.50
```

For example, `110,115,120,125` becomes approximately `55,58,60,63`.

### Explicit 21-entry curve

```bash
python3 patch_ae.py firmware.bin --ir-values \
  58,58,58,58,58,58,58,58,58,58,58,58,58,61,63,66,66,66,66,66,66
```

Values must be in `1..255` and monotonic non-decreasing.

## Runtime, sensor, and offset selection

All selected sensor configurations are patched in both runtimes by default.
Runtime selectors are repeatable:

```bash
python3 patch_ae.py firmware.bin --runtime normal --ir 55
python3 patch_ae.py firmware.bin --runtime pir --ir 55
python3 patch_ae.py firmware.bin --runtime pid:9 --ir-scale 0.50
```

Sensor selectors are repeatable:

```bash
--sensor single
--sensor day
--sensor night
--sensor imx258m
--sensor sc223ap
--sensor all
```

The deprecated `--all` option means all runtimes; on dual-camera firmware use
`--sensor all` only when all sensor configurations should be changed.

A manually confirmed IR-table offset can be supplied and is validated against
its containing partition and surrounding AE structure:

```bash
python3 patch_ae.py firmware.bin --ir-offset 0x006c3904 --ir 109
```

Repeat `--ir-offset` to patch multiple explicitly selected tables. Manual
offsets cannot be combined with `--runtime` or `--sensor`.

For unknown firmware with multiple unidentified AE structures in one runtime,
the tool permits scanning but refuses automatic sensor grouping. Use explicit
`--ir-offset` values after manual analysis.

## ISO cap: explicit offsets only

Automatic ISO-field searching is intentionally disabled because the pattern is
not structurally unique. The expert mode requires verified offsets:

```bash
python3 patch_ae.py firmware.bin --ir 55 \
  --iso-cap 3200 --iso-offset 0x123456
```

The offset must be aligned, inside a selected runtime partition, contain a
plausible ISO value, and be followed by `iso_prv.l = 100`. On dual-camera
firmware, independently confirm that each ISO offset belongs to the intended
sensor configuration; sensor-specific ISO offsets are not auto-detected.

## HC-950Ultra's second firmware package

The manufacturer update procedure supplies two different update stages:

1. `950XFUltra_20240808` with `FWHC940A.bin` updates the Novatek camera system,
   both image sensors, both camera runtimes, the UI, storage, and network host
   software. This is the only package that `patch_ae.py` reads.
2. `16009.1047.00.01.29.05-update fw` contains a `.pac` image and
   `upgrade_tool` for the separate 4G modem. It is not a second image-sensor
   firmware and is outside the scope of this patcher.

Do not pass the modem `.pac`, `upgrade_tool`, or modem update archive to
`patch_ae.py`.

## What version 2.1 validates

Before patching:

1. `NVTPACK_FW_HDR2` version marker.
2. Partition-table pointer from header offset `0x14` and count from `0x18`.
3. Records in the actual order `{offset, size, partition_id}`.
4. Partition bounds and overlap.
5. Whole-file checksum.
6. Every detectable internal partition checksum.
7. AE-table structure and partition ownership.
8. Exact SHA-256, offsets, runtime roles, original curves, and required sensor
   markers for recognized layouts.
9. Explicit runtime and sensor selection without guessing unknown multi-sensor
   layouts.

After patching:

1. Every changed partition receives a recalculated internal checksum.
2. The whole-file checksum is recalculated afterward.
3. Target curves and optional ISO fields are read back exactly.
4. A byte-level whitelist rejects changes outside requested data and checksum
   fields.
5. The output is written through a unique temporary file and atomically renamed.
6. The BIN is read back from disk or ZIP and verified again.

The Novatek checksum is a position-weighted, additive 16-bit two's-complement
checksum. Words are explicitly interpreted as little-endian and the outer field
is written as 16 bits.

## Diagnosing image quality

Keep the camera and scene unchanged and capture both remote and PIR images.
Include bright foreground vegetation and a darker subject farther away.

A simple clipping measurement, excluding the bottom information bar:

```python
from PIL import Image

image = Image.open("night.jpg").convert("L")
image = image.crop((0, 0, image.width, image.height - 100))
histogram = image.histogram()
total = sum(histogram)
print("pixels >= 250: %.2f%%" % (sum(histogram[250:]) / total * 100))
print("pixels == 255: %.2f%%" % (histogram[255] / total * 100))
```

Only this optional image analysis requires Pillow:

```bash
python3 -m pip install pillow
```

A 21-entry curve cannot be uniquely reconstructed from clipped JPEGs alone.
Images do not reveal the active AE index, and pure-white pixels have already
lost their original brightness.

## Flashing

The exact update filename and procedure varies by build. Commonly the patched
BIN inside the ZIP is named `FWHC940A.bin` and is copied to the SD-card root.
Confirm the expected name and recovery method for the exact camera before
flashing.

After flashing, test:

1. normal boot and menu operation;
2. remote night capture;
3. PIR wake-up night capture;
4. several consecutive PIR captures;
5. day images, video, storage, and network functions;
6. on the HC-950Ultra, both physical camera modules and day/night switching.

Static analysis and valid checksums do not prove compatibility with every
hardware revision.

## Troubleshooting

### Input checksum mismatch

Use a pristine manufacturer image. Version 2 has no broad `--force` option that
can turn a damaged input into a checksum-consistent but unknown output.

### Recognized HC-950Ultra has no automatic profile

This is intentional. Its factory night exposure is good. Use `--scan` for
inspection. An experimental change requires an explicit curve mode; the exact
known layout then defaults to the SC223AP night sensor.

### No automatic profile

Run `--scan`, compare the firmware build and curves, then use `--ir-scale`,
`--ir`, or `--ir-values`. Never select a profile for a different hash.

### Multiple AE structures on unknown firmware

Version 2.1 supports the verified HC-950Ultra dual-camera layout. Unknown
multi-AE layouts are displayed by `--scan`, but patching requires manually
verified `--ir-offset` values.

### Only one runtime found

Some firmware may genuinely contain one runtime. The three verified builds in
this repository contain normal/remote and low-power/PIR runtimes. Do not assume
PIR is fixed until a PIR image has been tested.

## Development and regression checks

Before publishing a change to `patch_ae.py`, test at least:

```bash
python3 -m py_compile patch_ae.py
python3 patch_ae.py HC960-original.zip --dry-run
python3 patch_ae.py HC940-original.zip --dry-run
python3 patch_ae.py HC950-original.zip --scan
python3 patch_ae.py HC950-original.zip --ir 109 --dry-run
python3 patch_ae.py HC950-original.zip --sensor day --ir 109 --dry-run
python3 patch_ae.py HC950-original.zip --sensor all --ir 109 --dry-run
```

Expected regression BIN hashes:

```text
HC-960Ultra automatic profile:
a66190b5f418a2e54c09042f154411777bb2b3f7ec339023a1331442600c4667

HC-940Ultra automatic profile:
a0a7b94cc9e1c4e7da51b8ddf4c8b18a619d2acecf8b874247ca3669e5bf9a53

HC-950Ultra test-only flat 109, default SC223AP night sensor:
a03b34e0b24f5dde27a3b39598d69f38c5ff064e75a98775c3b3c567fcadbb3c
```

The HC-950 hash is a software regression vector, **not** a recommended firmware
to flash.

No manufacturer or patched firmware should be committed to this repository.

## Credits

- [Novatek-FW-info / NTKFWinfo](https://github.com/EgorKin/Novatek-FW-info)
  for public format and checksum parsing references.
- Contributors who supplied original firmware hashes, runtime/sensor analysis,
  and controlled night-image comparisons.

## License

[MIT](LICENSE) for the tool and documentation only. Manufacturer firmware,
Suntek and Novatek trademarks, and camera hardware are not covered by this
license. Provided as-is; flashing is at your own risk.
