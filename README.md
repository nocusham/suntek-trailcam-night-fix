# Fixing night-photo overexposure on Suntek / Novatek trail cameras

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

English | [中文](README.zh-CN.md)

`patch_ae.py` inspects and patches the night/IR auto-exposure tables in selected
Suntek trail-camera firmware based on the Novatek `NVTPACK_FW_HDR2` platform.
It understands separate normal/remote and low-power/PIR runtimes, supports
single- and dual-camera layouts, recalculates every affected internal checksum,
recalculates the outer firmware checksum, and verifies an exact changed-byte
whitelist before writing an output image.

Version 3 is designed for Suntek's frequent firmware updates. An unknown SHA-256
is no longer treated as either "probably compatible" or "single-camera" merely
because one plausible table was found. The tool now assigns one of four support
levels and fails closed when model, sensor, or runtime identity is uncertain.

## Safety warning

Flashing modified firmware can permanently brick a camera. A valid checksum does
not prove hardware compatibility.

- Keep an untouched copy of the exact original manufacturer firmware.
- Run `--verify-only`, `--compat-check`, `--scan`, and `--dry-run` first.
- Never reuse offsets from another firmware build.
- Keep physical access to the camera and preferably a 3.3 V UART recovery path.
- Do not trust a profile downloaded from an unknown source.

This repository contains tools, profiles, and documentation only. It does not
include or download manufacturer firmware.

## Support levels

| Level | Meaning | Automatic profile | Custom patch |
|---|---|---:|---:|
| `verified` | Exact BIN SHA-256 matches a trusted profile; partition, runtime, offset, curve, marker, and context validation passed | Allowed when the profile defines one | Allowed |
| `family-match` | Unknown SHA-256, but build markers, sensor markers, runtime counts, candidate counts, and relative SDK context match a known family | Blocked | Expert workflow only |
| `structural-match` | Valid NVTPACK and plausible AE structures, but model or sensor identity is not verified | Blocked | Explicit offsets and expert workflow only |
| `unsupported` | Required runtime/AE structure was not found or contradicted | Blocked | Blocked |

The most important safety rule is:

```text
Known SHA-256 + trusted profile
    -> automatic patch may be allowed

Unknown SHA-256 + probable family
    -> analyze and label, but do not write normally

Unknown model or ambiguous sensor layout
    -> require explicit offsets and a bound scan manifest
```

## Tested firmware and bundled profiles

Official release profiles are stored in `profiles/*.json`. The script also
contains conservative built-in fallbacks so checksum verification and the
currently tested layouts remain usable when only `patch_ae.py` was downloaded.

| Layout/profile | Model/build | Original BIN SHA-256 | Camera design | Automatic action |
|---|---|---|---|---|
| `hc960-ae55` | HC-960Ultra, 2026-03-26 | `b391abec2bdf6ab1d48e357c94e0f56bb9e2703899b647609acec3faa30150fa` | Single camera, two runtimes | `110 x21` -> `55 x21` in both runtimes |
| `hc940-ae58` | HC-940Ultra, 2025-04-23 | `9eb10ef5dd4057a891fb48a2b9cb9165e9ae3168a9b7e58aecc6299b90749c4a` | Single camera, two runtimes | `110..125` -> calibrated `58..66` in both runtimes |
| `hc950-dual-camera-2024` | HC-950Ultra / 950XFUltra, 2024-08-08 | `e4db261f9228af5793d5952b45f9b6e9e41b2a50e264ac8971e5145d8cc19370` | IMX258M day + SC223AP night, two runtimes | Recognition only |
| `hc950-dual-camera-2026` | HC-950Ultra / 950XFUltra, 2026-05-27 | `a6caf6be7e1a77dfe434ae78b959390b190f2e3b6e9b6e0cb5c8b29b2e6edf61` | IMX258M day + SC223AP night, two runtimes | Recognition only |

The HC-950Ultra factory night exposure was reported as good. Therefore neither
HC-950 profile defines an automatic exposure change. An explicit experimental
change defaults to the SC223AP night sensor only on an exact verified build.

List the loaded profiles and their trust source:

```bash
python3 patch_ae.py --list-profiles
```

## Quick start for an exact verified build

### 1. Verify checksums

```bash
python3 patch_ae.py firmware.zip --verify-only
```

This verifies the outer NVTPACK checksum and every detectable internal
partition checksum. `--verify-only` intentionally does not perform the slower AE
scan on an unknown hash; use `--compat-check` for classification.

### 2. Scan the firmware

```bash
python3 patch_ae.py firmware.zip --scan --manifest scan.json
```

The manifest records the input SHA-256, support level, partition map, every
candidate offset, original curves, sensor/runtime labels, and context
fingerprints.

### 3. Preview an automatic patch

For the exact HC-940Ultra and HC-960Ultra profiles:

```bash
python3 patch_ae.py firmware.zip --dry-run
```

### 4. Write the patched image

```bash
python3 patch_ae.py firmware.zip --manifest patch.json
```

ZIP input preserves the archive structure and defaults to
`firmware_patched.zip`. BIN input defaults to `firmware_patched.bin`. Existing
files require `--overwrite`.

## Handling a new, unknown Suntek firmware

### Step 1: classify it

```bash
python3 patch_ae.py new-firmware.zip \
  --compat-check \
  --manifest new-firmware-scan.json
```

Example result:

```text
support level=family-match
model=HC-950Ultra / 950XFUltra
confidence=high
automatic_patch=no
```

A family match uses independent evidence, not only the visible model string:

- model/build prefix;
- required sensor and AE parameter markers;
- normal/remote and low-power/PIR runtime presence;
- the expected number of AE configurations in each runtime;
- relative AE-table structure and context fingerprints.

A firmware cannot receive `family-match` when a known dual-camera family is
missing one of its expected candidates. It falls back to `structural-match`
instead. This prevents a partly changed HC-950 build from being misclassified
as a single-camera image.

### Step 2: compare with a known generation

```bash
python3 patch_ae.py new-firmware.zip \
  --compare-layout hc950-dual-camera-2026
```

The comparison reports candidate counts, original-curve matches, absolute
offset changes, required markers, and relative context matches. Absolute offset
changes are expected between builds and must never be copied blindly.

### Step 3: export a profile candidate for review

```bash
python3 patch_ae.py new-firmware.zip \
  --export-layout candidate-profile.json
```

The exported file is marked `"status": "unverified"`. Review the sensor order,
curves, runtime roles, offsets, markers, and fingerprints before proposing it as
an official profile.

### Step 4: perform an unverified dry run

An unknown build requires explicit acknowledgment and an assertion of the
original curve.

For a probable HC-950 family match whose SC223AP night tables are both flat
`110 x21`:

```bash
CURVE=110,110,110,110,110,110,110,110,110,110,110,110,110,110,110,110,110,110,110,110,110

python3 patch_ae.py new-firmware.zip \
  --ir 109 \
  --allow-unverified \
  --expect-ir "$CURVE" \
  --dry-run \
  --manifest dry-run.json
```

For different original curves, bind each assertion to its file offset:

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

A `structural-match` cannot use guessed sensor names. Supply every target with
`--ir-offset` after manual analysis.

### Step 5: bind a real write to the earlier scan

```bash
python3 patch_ae.py new-firmware.zip \
  --ir 109 \
  --allow-unverified \
  --expect-ir "$CURVE" \
  --accept-scan-manifest new-firmware-scan.json \
  --manifest patch.json
```

The real write is refused unless the accepted scan manifest has the same input
SHA-256 and contains every selected offset with the same original curve.
Unverified outputs are visibly named:

```text
new-firmware_UNVERIFIED_PATCHED.zip
```

## Runtime and sensor selection

The examined firmware contains two independent runtime images:

| Runtime | Typical partition | Load address | Function |
|---|---:|---:|---|
| Normal/remote | ID 3 | `0x02700400` | menu, network, remote capture, Linux/4G host path |
| Low-power/PIR | ID 9 | `0x00400400` | PIR wake-up and standalone capture |

Select one runtime:

```bash
python3 patch_ae.py firmware.zip --runtime pir --ir-scale 0.9 --dry-run
```

On an exact or high-confidence HC-950 layout:

```bash
python3 patch_ae.py firmware.zip --sensor night --ir 109 --dry-run
python3 patch_ae.py firmware.zip --sensor sc223ap --ir 109 --dry-run
python3 patch_ae.py firmware.zip --sensor day --ir-scale 0.95 --dry-run
python3 patch_ae.py firmware.zip --sensor all --ir 109 --dry-run
```

Patching only one runtime is normally undesirable because remote and PIR images
would then use different exposure curves.

## Profile registry

The bundled JSON schema separates firmware knowledge from patching code. A new
exact build can be added without changing the parser or checksum implementation.
A profile contains:

- exact BIN SHA-256;
- model family and build;
- required marker strings;
- partition ID and runtime role for each candidate;
- exact `tab_ratio_ir` offsets and original curves;
- sensor identity;
- relative context fingerprints;
- an optional automatic target curve.

Load additional recognition profiles:

```bash
python3 patch_ae.py firmware.zip \
  --profile-dir ./candidate-profiles \
  --compat-check
```

External profiles are recognition-only by default. They cannot override trusted
bundled profiles and cannot enable an automatic patch. After independently
reviewing every profile file, an expert may opt in explicitly:

```bash
python3 patch_ae.py firmware.zip \
  --profile-dir ./reviewed-profiles \
  --trust-external-profiles \
  --dry-run
```

## AE structure and relative fingerprints

The examined SDK stores three consecutive 21-entry curves:

```text
tab_ratio_mov
+0x54  tab_ratio_photo
+0xa8  tab_ratio_ir
```

A characteristic over-exposure threshold pair is normally found at
`tab_ratio_ir + 0x25c`. Version 3 also hashes stable context regions after the
IR curve and around the threshold block. These fingerprints help recognize a
moved structure in a new build without treating a short byte pattern as proof
of sensor identity.

## Patch modes

Flat curve:

```bash
python3 patch_ae.py firmware.zip --ir 55 --dry-run
```

Curve-preserving scaling:

```bash
python3 patch_ae.py firmware.zip --ir-scale 0.50 --dry-run
```

Explicit curve:

```bash
python3 patch_ae.py firmware.zip \
  --ir-values 58,58,58,58,58,58,58,58,58,58,58,58,58,61,63,66,66,66,66,66,66 \
  --dry-run
```

`--iso-cap` remains an expert-only feature and requires explicit, manually
verified `--iso-offset` values. No automatic ISO search is performed.

## What every write verifies

Before and after writing, the tool checks:

- NVTPACK header and partition-table bounds;
- partition overlaps and file bounds;
- all detectable internal partition checksums;
- outer NVTPACK checksum;
- AE table structure and expected original curves;
- target curves after patching;
- checksum fields of every modified partition;
- exact changed-byte whitelist;
- output BIN/ZIP round trip.

A failed round-trip removes the output file.

## Requirements and tests

- Python 3.10 or newer.
- No third-party Python packages.
- Manufacturer `.bin`, or a `.zip` containing exactly one firmware `.bin`.

Run repository smoke tests:

```bash
python3 -m unittest discover -s tests -v
```

The four manufacturer images used for integration regression testing are not
included in this repository.

## Limitations

- Family recognition is evidence-based, not proof of hardware compatibility.
- Sensor assignment in an unknown HC-950 family build depends on the verified
  two-candidate ordering being consistent in both runtimes. Any count mismatch
  disables family assignment.
- A new SDK may move or redesign the AE structure beyond the current scanner.
- JPEG appearance alone cannot identify which of the 21 entries was active.
- Valid checksums cannot prevent a semantically wrong but structurally valid
  patch from damaging a camera.

## License

MIT. See `LICENSE` in the repository.
