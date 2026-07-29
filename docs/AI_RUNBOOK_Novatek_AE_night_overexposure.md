# AI RUNBOOK — Novatek trail-camera night-AE diagnosis, multi-runtime analysis, and safe firmware patching

> **Document status:** updated for `patch_ae.py` v3.0.0, 2026-07-29  
> **Audience:** an AI agent or firmware analyst who must inspect a Suntek/Novatek trail-camera firmware, determine whether night overexposure is caused by embedded AE tuning, and apply or review a safe, reproducible patch.  
> **Primary rule:** never infer compatibility from a model name or a visually similar firmware. Exact firmware SHA-256 and verified structure determine whether an automatic patch is allowed.

---

## 0. Scope, safety, and non-negotiable rules

This runbook applies to examined Suntek trail cameras based on the Novatek `NVTPACK_FW_HDR2` firmware format, including verified HC-940Ultra and HC-960Ultra builds and recognition-only HC-950Ultra builds.

Firmware modification can permanently brick a camera. A valid checksum proves only that the container is internally consistent; it does **not** prove that a patch is semantically correct or compatible with the hardware.

Always:

1. Keep the exact untouched manufacturer firmware and its SHA-256.
2. Inspect ZIP contents before use; the camera firmware is normally a `.bin` such as `FWHC940A.bin`.
3. Run `--verify-only`, `--compat-check`, `--scan`, and `--dry-run` before writing.
4. Patch every runtime that can perform the affected capture path.
5. Never reuse absolute offsets from another firmware build.
6. Keep every edit in-place and length-preserving.
7. Recalculate every modified partition checksum before the outer checksum.
8. Verify the exact changed-byte whitelist and reopen the written BIN/ZIP.
9. Treat unknown firmware as unverified even when its visible build name resembles a known build.
10. Keep physical access to the camera and preferably a known 3.3 V UART recovery path.

Do not upload or distribute manufacturer firmware through this project.

---

## 1. Critical corrections to the earlier runbook

The previous version contained assumptions that were disproved by analysis of HC-940Ultra, HC-950Ultra, and HC-960Ultra firmware.

### 1.1 Partition-table format

The partition table is **not** generically fixed at file offset `0x88`, and its records are **not** `{id, offset, size}`.

For the examined `NVTPACK_FW_HDR2` images:

- file `+0x14`: little-endian `u32` pointer to the partition table;
- file `+0x18`: little-endian `u32` partition count;
- each table record is 12 bytes in the order:

```text
{ offset, size, partition_id }
```

The examined images use a table pointer of `0x80`. Starting at `0x88` shifts every field and can create false partition IDs, omit the first partition, and invent a final record.

### 1.2 The second camera runtime is active and must not be treated as an unused copy

The examined HC-940Ultra and HC-960Ultra firmware contains two independent camera runtimes:

| Runtime role | Typical partition | Typical load address | Capture path |
|---|---:|---:|---|
| Normal/remote | ID 3 | `0x02700400` | menu, network/4G host, remote capture |
| Low-power/PIR | ID 9 | `0x00400400` | standalone PIR wake-up and capture |

Each runtime has its **own AE configuration**. Patching only the normal runtime can fix remote-triggered photos while PIR-triggered photos remain overexposed. This behavior was the central cause of the original incomplete fix.

Runtime identity must be derived from structure and load address, not from partition ID alone.

### 1.3 `tab_ratio_ir` is not always flat and adjacent tables are not always ramps

Observed valid curves include:

- HC-960Ultra: flat `110 × 21`;
- HC-940Ultra: `110 … 125` non-flat curve;
- HC-950Ultra: a mixture of flat and non-flat curves across sensors and runtimes.

Therefore, a locator must not require:

- `tab_ratio_mov` to be a ramp;
- `tab_ratio_photo` to be flat;
- `tab_ratio_ir` to be flat.

### 1.4 Covering IR LEDs is not a conclusive software/hardware discriminator

Covering the IR LEDs can reduce clipping because it reduces scene illumination. That observation alone does not distinguish excessive LED power from an AE target problem.

Stronger evidence for an AE-tuning cause is:

- the same hardware produces different exposure after only an AE-table change;
- a remote capture is corrected while PIR capture remains overexposed when only one runtime is patched;
- patching the corresponding AE table in both runtimes makes the two trigger paths converge.

### 1.5 Do not automatically locate or patch an ISO limit

A generic search for the first plausible `{ISO_high, 100}` pair can select an unrelated field. `patch_ae.py` v3 does not perform automatic ISO searching. `--iso-cap` is expert-only and requires manually verified `--iso-offset` values.

---

## 2. Confirmed architecture model

### 2.1 SoC and execution environments

The examined cameras use the Novatek NA51023 / NT96670 platform, MIPS32 little-endian.

- CPU1 executes the camera pipeline, ISP, AE, UI, and low-power logic under a Novatek RTOS environment such as µITRON/eCos.
- CPU2 Linux provides network/4G and cloud support in the normal host runtime.
- The low-power/PIR runtime can operate without CPU2 Linux.

The firmware may contain multiple complete camera runtimes, not merely backup copies.

### 2.2 Embedded tuning libraries

AE/IQ/AWB tuning is embedded in runtime partitions. Sensor-related strings can include:

```text
CMOS_<SENSOR>
AE_PARAM_<SENSOR>_EVB
IQ_PARAM_<SENSOR>_EVB
AWB_PARAM_<SENSOR>_EVB
```

These markers are useful evidence but are not sufficient by themselves to identify the active table.

### 2.3 HC-950Ultra dual-camera design

The examined HC-950Ultra / 950XFUltra firmware contains two physical camera modules:

- `IMX258M`: day camera;
- `SC223AP`: night camera.

Each sensor has a separate AE configuration in both runtimes, producing four relevant AE structures:

```text
normal/remote + IMX258M
normal/remote + SC223AP
low-power/PIR + IMX258M
low-power/PIR + SC223AP
```

The factory night exposure of the examined HC-950Ultra cameras is reported as good. HC-950 profiles are therefore recognition-only and do not define an automatic exposure patch.

### 2.4 Separate 4G modem update packages

A package containing files such as a `.pac`, `upgrade_tool`, `log4gfile.bin`, and `logfile.bin` is a separate cellular-modem update, not a second image-sensor firmware. Do not feed the modem package to `patch_ae.py`.

---

## 3. Supported and examined firmware

Official exact-build knowledge belongs in trusted JSON profiles under `profiles/`.

| Layout/profile | Model/build | Original BIN SHA-256 | Camera design | Default action |
|---|---|---|---|---|
| `hc960-ae55` | HC-960Ultra, 2026-03-26 | `b391abec2bdf6ab1d48e357c94e0f56bb9e2703899b647609acec3faa30150fa` | one camera, two runtimes | automatic `110 × 21` → `55 × 21` in both runtimes |
| `hc940-ae58` | HC-940Ultra, 2025-04-23 | `9eb10ef5dd4057a891fb48a2b9cb9165e9ae3168a9b7e58aecc6299b90749c4a` | one camera, two runtimes | automatic calibrated `110…125` → `58…66` in both runtimes |
| `hc950-dual-camera-2024` | HC-950Ultra / 950XFUltra, 2024-08-08 | `e4db261f9228af5793d5952b45f9b6e9e41b2a50e264ac8971e5145d8cc19370` | IMX258M day + SC223AP night, two runtimes | recognition only |
| `hc950-dual-camera-2026` | HC-950Ultra / 950XFUltra, 2026-05-27 | `a6caf6be7e1a77dfe434ae78b959390b190f2e3b6e9b6e0cb5c8b29b2e6edf61` | IMX258M day + SC223AP night, two runtimes | recognition only |

An exact model label with a different SHA-256 is a new, unverified build.

---

## 4. `patch_ae.py` v3 support levels

Every input is classified into one of four levels:

| Level | Meaning | Automatic profile | Custom patch |
|---|---|---:|---:|
| `verified` | Exact BIN SHA-256 matches a trusted profile and all layout assertions pass | allowed when the profile defines one | allowed |
| `family-match` | Unknown SHA-256, but model markers, sensor markers, runtime counts, candidate counts, and relative context match a known family | blocked | expert workflow only |
| `structural-match` | Valid container and plausible AE structures, but model/sensor identity is not verified | blocked | explicit offsets and expert workflow only |
| `unsupported` | Required container, runtime, or AE structure is absent or contradictory | blocked | blocked |

Fail-closed rule:

```text
exact trusted SHA-256 + validated profile
    -> automatic patch may be permitted

unknown SHA-256 + probable family
    -> analyze and label, but do not write normally

ambiguous sensor/runtime structure
    -> require explicit offsets and a scan-bound expert workflow
```

A single detected candidate in an unknown firmware must be labeled `unidentified`, not assumed to mean a single-camera design.

---

## 5. Recommended tool workflow

### 5.1 Verify container checksums

```bash
python3 patch_ae.py firmware.zip --verify-only
```

This checks the outer NVTPACK checksum and every detectable internal partition checksum. It does not prove that an exposure patch is appropriate.

### 5.2 Classify and scan

```bash
python3 patch_ae.py firmware.zip \
  --compat-check \
  --manifest firmware-scan.json
```

For a detailed candidate listing:

```bash
python3 patch_ae.py firmware.zip --scan --manifest firmware-scan.json
```

The scan manifest should record at least:

- exact input BIN SHA-256;
- archive member name for ZIP input;
- support level and probable family;
- partition map and checksums;
- runtime roles;
- every AE candidate and original curves;
- sensor labels only where justified;
- context fingerprints;
- whether automatic patching is allowed.

### 5.3 Compare an unknown build with a known layout

```bash
python3 patch_ae.py new-firmware.zip \
  --compare-layout hc950-dual-camera-2026
```

Absolute offset changes are expected between builds. Compare counts, curves, runtime roles, markers, and relative context—not only offsets.

### 5.4 Export an unverified profile candidate

```bash
python3 patch_ae.py new-firmware.zip \
  --export-layout candidate-profile.json
```

The exported profile must remain marked unverified until manually reviewed and regression-tested.

### 5.5 Preview an automatic patch on an exact verified HC-940/HC-960 build

```bash
python3 patch_ae.py firmware.zip --dry-run
```

### 5.6 Write an automatic patch

```bash
python3 patch_ae.py firmware.zip --manifest patch.json
```

ZIP input preserves the archive structure. BIN input writes a BIN. Existing output files require `--overwrite`.

---

## 6. Expert workflow for unknown firmware

Unknown firmware must never receive a known build's automatic profile.

### 6.1 Family-match dry run

An explicit original-curve assertion is mandatory:

```bash
CURVE=110,110,110,110,110,110,110,110,110,110,110,110,110,110,110,110,110,110,110,110,110

python3 patch_ae.py new-firmware.zip \
  --ir 109 \
  --allow-unverified \
  --expect-ir "$CURVE" \
  --dry-run \
  --manifest dry-run.json
```

### 6.2 Structural-match dry run

When sensor identity is unknown, provide every manually verified target offset and bind each expected curve to that offset:

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

### 6.3 Bind a real write to an earlier scan

```bash
python3 patch_ae.py new-firmware.zip \
  --ir 109 \
  --allow-unverified \
  --expect-ir "$CURVE" \
  --accept-scan-manifest firmware-scan.json \
  --manifest patch.json
```

The accepted scan must match the current input SHA-256 and contain every selected offset with the same original curve. Unverified outputs must be visibly named, for example:

```text
new-firmware_UNVERIFIED_PATCHED.zip
```

---

## 7. NVTPACK parsing

### 7.1 Header

Observed `NVTPACK_FW_HDR2` properties:

```text
+0x10  u32 LE  format version, observed 0x16071515
+0x14  u32 LE  partition-table offset
+0x18  u32 LE  partition count
+0x24  u16 LE  outer checksum in the low 16-bit field
```

The 16-byte value at file offset `0x00` is a format identifier in the examined images, not a content hash.

### 7.2 Partition records

Read exactly `partition_count` records from `partition_table_offset`:

```text
u32 LE offset
u32 LE size
u32 LE partition_id
```

Before using any record, verify:

- `offset + size <= file_size`;
- no partition overlap;
- table itself lies within the file;
- count is plausible;
- the partition header and load address are consistent with its inferred role.

Do not silently fall back to an arbitrary record count after a malformed header.

### 7.3 Runtime identification

Observed load-address families:

```text
0x027xxxxx -> normal/remote runtime
0x004xxxxx -> low-power/PIR runtime
```

Treat partition IDs 3 and 9 as known-build profile evidence, not universal constants.

---

## 8. Checksum algorithm and verification

The examined images use a position-weighted 16-bit two's-complement checksum. Some tools and legacy messages call the outer value a CRC, but it is not a polynomial CRC.

### 8.1 Safe explicit little-endian implementation

```python
import struct


def ntk_checksum16(data: bytes | bytearray,
                   start: int,
                   length: int,
                   ignore_relative: int) -> int:
    word_count = length // 2
    even_length = word_count * 2

    if start < 0 or start + even_length > len(data):
        raise ValueError("checksum range outside file")
    if ignore_relative < 0 or ignore_relative + 2 > even_length:
        raise ValueError("checksum field outside range")
    if ignore_relative & 1:
        raise ValueError("checksum field is not 16-bit aligned")

    total = 0
    for index, (word,) in enumerate(
        struct.iter_unpack("<h", data[start:start + even_length])
    ):
        if index == ignore_relative // 2:
            word = 0
        total += word

    total = (total + (word_count - 1) * word_count // 2) & 0xFFFF
    return (-total) & 0xFFFF
```

An implementation using `array('h')` must explicitly byteswap on big-endian hosts. Do not assume native byte order is little-endian.

### 8.2 Checksum fields

In the examined builds:

- outer checksum: file `+0x24`, write only the 16-bit field;
- MODELEXT checksum: often partition `+0x36`;
- camera-runtime checksum: often partition `+0x6e`.

The internal checksum field is associated with a `55 aa` marker near the beginning of the partition. Locate and verify it per partition rather than assuming every future build uses the same relative offset.

### 8.3 Mandatory checks

Before patching:

1. Verify the outer checksum.
2. Verify **all detectable internal partition checksums**, not only the selected runtime.
3. Abort on any mismatch unless the task is explicitly forensic analysis of a damaged image.

After patching:

1. Recalculate each modified partition checksum.
2. Recalculate the outer checksum last.
3. Reopen the written output and verify all checksums again.

A valid outer checksum does not prove that every internal partition checksum is valid.

---

## 9. Locating AE structures safely

### 9.1 Observed table layout

The examined SDK stores three consecutive 21-entry little-endian `u32` curves:

```text
tab_ratio_mov
+0x54  tab_ratio_photo
+0xa8  tab_ratio_ir
```

Each table is `21 × 4 = 0x54` bytes.

A characteristic over-exposure structure is normally found around:

```text
tab_ratio_ir + 0x25c
```

The `0x25c` relation is strong evidence for the examined SDK generation, not a universal constant.

### 9.2 Candidate validation

A candidate should satisfy multiple independent checks:

- all three arrays are fully inside one partition;
- all contain 21 plausible `u32` ratios;
- curves are flat or monotonic/plausible, without requiring a specific shape;
- neighboring AE fields are structurally consistent;
- the over-exposure block or nearby context fingerprint matches;
- the same runtime/sensor pattern is present where the model architecture requires it;
- an exact profile's expected original curve matches byte-for-byte.

Reject or downgrade confidence when:

- candidates overlap;
- one runtime has fewer candidates than required by a known dual-camera family;
- sensor ordering differs between runtimes;
- a known build's expected curve or context hash does not match;
- more than one candidate is equally plausible and no verified sensor identity exists.

### 9.3 UART evidence

When available, `ae aetdump 0` can provide the exact live arrays and adjacent fields. Use it to strengthen identification, not to bypass container and checksum verification.

Volatile `ae aetset` changes can test a value but do not survive the per-trigger boot cycle and are not a persistent deployment method.

---

## 10. Runtime and sensor selection

### 10.1 Single-camera HC-940/HC-960 layouts

Patch the corresponding AE table in both:

```text
normal/remote runtime
low-power/PIR runtime
```

Patching only one runtime intentionally creates different exposure behavior for remote and PIR captures and is normally incorrect.

### 10.2 Dual-camera HC-950 layout

On exact verified builds, sensor selection can distinguish:

```bash
--sensor day
--sensor night
--sensor imx258m
--sensor sc223ap
--sensor all
```

An explicit HC-950 experiment without `--sensor` defaults only to the SC223AP night sensor in both runtimes. The IMX258M day-camera AE tables remain unchanged unless selected explicitly.

Because factory night exposure is reported as good, no automatic HC-950 patch is recommended.

### 10.3 Runtime filters

A runtime can be selected explicitly, for example:

```bash
python3 patch_ae.py firmware.zip \
  --runtime pir \
  --ir-scale 0.9 \
  --dry-run
```

Use this only for diagnosis or a deliberate asymmetric experiment. Production fixes should normally keep remote and PIR paths aligned.

---

## 11. Choosing a target curve

A finished JPEG cannot reveal which of the 21 entries was active. Clipped pixels also lose information above the JPEG maximum. Image analysis can narrow a sensible range but cannot uniquely reconstruct all 21 points.

### 11.1 Patch modes

Flat curve:

```bash
python3 patch_ae.py firmware.zip --ir 55 --dry-run
```

Curve-preserving scaling:

```bash
python3 patch_ae.py firmware.zip --ir-scale 0.50 --dry-run
```

Explicit 21-point curve:

```bash
python3 patch_ae.py firmware.zip \
  --ir-values 58,58,58,58,58,58,58,58,58,58,58,58,58,61,63,66,66,66,66,66,66 \
  --dry-run
```

### 11.2 HC-960Ultra verified target

Original:

```text
110 × 21
```

Verified profile target:

```text
55 × 21
```

This must be applied to both normal/remote and low-power/PIR runtimes.

### 11.3 HC-940Ultra image-calibrated target

Original:

```text
110,110,110,110,110,110,110,
110,110,110,110,110,110,115,
120,125,125,125,125,125,125
```

Calibrated target preserving the curve shape:

```text
58,58,58,58,58,58,58,
58,58,58,58,58,58,61,
63,66,66,66,66,66,66
```

A test with a flat value of 55 nearly eliminated clipping and retained useful detail. The `58…66` curve uses part of the remaining headroom while preserving the manufacturer curve shape.

### 11.4 HC-950Ultra

Do not infer a need to patch from the numeric value `110` alone. Sensor sensitivity, optics, IR illumination, ISP tuning, and the dual-camera design change the result. The examined HC-950Ultra night exposure is already good, so exact profiles are recognition-only.

---

## 12. Patch procedure and required order

For a manually implemented patch or a code review of `patch_ae.py`:

1. Read the original into a new mutable buffer.
2. Record the input SHA-256 and every selected original curve.
3. Resolve each target to exactly one containing partition.
4. Apply only the selected length-preserving curve or explicitly verified ISO field.
5. Recalculate the checksum of every modified partition.
6. Recalculate the outer 16-bit checksum at file `+0x24` last.
7. Build an exact whitelist of permitted changed byte ranges:
   - selected AE curve bytes;
   - explicitly selected ISO fields, if any;
   - checksum fields of modified partitions;
   - outer checksum field.
8. Compare original and output byte-by-byte; abort on any change outside the whitelist.
9. Write via a unique temporary file in the destination directory.
10. Flush and atomically replace the destination.
11. Reopen the BIN or ZIP from disk and repeat structural, checksum, target-curve, and whitelist verification.
12. Delete the output if round-trip verification fails.

Do not overwrite an existing output unless the user explicitly selected `--overwrite`.

The outer checksum often remains numerically unchanged after internal checksums are repaired. That is a useful observation, not a requirement.

---

## 13. Verification checklist

A patch is acceptable only when all items pass:

- input SHA-256 recorded;
- input outer checksum valid;
- all detectable input partition checksums valid;
- exact or explicitly accepted support level recorded;
- all target runtimes identified;
- all target sensors identified or explicit offsets supplied;
- original curves match the profile or `--expect-ir` assertions;
- target curves match after patching;
- every modified partition checksum valid;
- output outer checksum valid;
- output file size unchanged;
- no partition overlap or bounds violation;
- exact changed-byte whitelist valid;
- disk/ZIP round trip valid;
- output SHA-256 and manifest generated.

Example desired report:

```text
Partition 3  normal/remote  checksum OK
Partition 9  low-power/PIR checksum OK
NVTPACK                         checksum OK
Target curves                   OK
Changed-byte whitelist          OK
Round trip                      OK
Result                          VALIDATED OUTPUT
```

Do not describe an output as hardware-tested unless it was actually flashed and tested on the exact camera model/build.

---

## 14. Deployment and fallback

1. Use the manufacturer-documented SD update path for the exact model.
2. Keep the expected on-card filename, commonly `FWHC940A.bin`, but verify the vendor instructions.
3. Remove unrelated update files from the SD card.
4. Keep the original firmware available separately.
5. After update, test both:
   - a remote-triggered night photo, when supported;
   - a PIR-triggered night photo.
6. Compare clipping, near-subject detail, distant subject visibility, and overall noise.
7. Restore the pristine manufacturer firmware if behavior is unstable or the result is too dark.

Do not include generic raw-flash or bootloader write commands in a public runbook unless they have been independently validated for that exact hardware and a full recovery procedure is available.

---

## 15. Image-analysis guidance

When comparing before/after JPEGs:

- exclude the timestamp/status overlay;
- measure the whole scene and a fixed IR hotspot region separately;
- report at least:
  - fraction of pixels `>= 250`;
  - fraction exactly `255`;
  - median luminance;
  - 95th and 99th percentile;
- compare the same camera, viewpoint, scene, and trigger path where possible;
- do not trust EXIF exposure or ISO blindly; embedded values may be static placeholders;
- do not infer all 21 curve entries from one or a few JPEGs.

A global AE reduction can reduce a central IR hotspot but cannot independently optimize bright near vegetation and a dark distant background. If near subjects still clip while the background becomes too dark, IR LED power or illumination geometry may require separate investigation.

---

## 16. Known exact layouts — reference offsets only

These offsets are valid only for the exact SHA-256 listed in Section 3. They are documentation and regression anchors, not generic search constants.

### HC-960Ultra, build 2026-03-26

| Runtime | `tab_ratio_ir` file offset |
|---|---:|
| normal/remote | `0x006cb628` |
| low-power/PIR | `0x0182dfc0` |

Both original curves are `110 × 21`; profile target is `55 × 21`.

### HC-940Ultra, build 2025-04-23

| Runtime | `tab_ratio_ir` file offset |
|---|---:|
| normal/remote | `0x006cb8cc` |
| low-power/PIR | `0x0188ede4` |

Both original curves are `110…125`; profile target is `58…66`.

### HC-950Ultra, build 2024-08-08

| Runtime | Sensor | Role | `tab_ratio_ir` file offset |
|---|---|---|---:|
| normal/remote | IMX258M | day | `0x006c2c60` |
| normal/remote | SC223AP | night | `0x006c3904` |
| low-power/PIR | IMX258M | day | `0x01893924` |
| low-power/PIR | SC223AP | night | `0x018945ec` |

Recognition only; no automatic patch.

### HC-950Ultra, build 2026-05-27

| Runtime | Sensor | Role | `tab_ratio_ir` file offset |
|---|---|---|---:|
| normal/remote | IMX258M | day | `0x006c2de4` |
| normal/remote | SC223AP | night | `0x006c3a88` |
| low-power/PIR | IMX258M | day | `0x01834710` |
| low-power/PIR | SC223AP | night | `0x018353d8` |

Recognition only; no automatic patch.

---

## 17. AI-agent decision tree

```text
START
  |
  +-- Is input a BIN or a ZIP containing exactly one firmware BIN?
  |      no -> UNSUPPORTED / inspect package manually
  |
  +-- Does NVTPACK header, partition pointer, count, and bounds validate?
  |      no -> UNSUPPORTED
  |
  +-- Do outer and all detectable inner checksums validate?
  |      no -> STOP; forensic analysis only
  |
  +-- Exact SHA-256 matches trusted profile?
  |      yes -> validate every profile assertion
  |               |
  |               +-- all pass -> VERIFIED
  |               +-- any fail -> STOP; profile contradiction
  |
  +-- No exact match: run structural scan and family rules
         |
         +-- complete known-family architecture and fingerprints?
         |      yes -> FAMILY-MATCH; no automatic profile
         |
         +-- plausible AE structures but identity ambiguous?
         |      yes -> STRUCTURAL-MATCH; explicit offsets required
         |
         +-- missing/contradictory structure -> UNSUPPORTED

PATCH REQUEST
  |
  +-- VERIFIED with trusted automatic profile?
  |      yes -> dry-run, then write if requested
  |
  +-- unknown build?
         |
         +-- --allow-unverified present?
         +-- exact --expect-ir assertion for every target?
         +-- explicit offsets when sensor identity is ambiguous?
         +-- real write bound to accepted scan manifest?
                all yes -> experimental UNVERIFIED output
                any no  -> refuse

AFTER PATCH
  data -> modified partition checksum(s) -> outer checksum
       -> target verification -> exact byte whitelist
       -> atomic write -> reopen -> full round-trip verification
```

---

## 18. Pitfalls

- Do not parse records as `{id, offset, size}`.
- Do not start reading the partition table at `0x88` without reading the header pointer.
- Do not assume the second runtime is a backup or inactive image.
- Do not interpret `--all` as “all runtimes” unless the implementation explicitly does so.
- Do not patch the first candidate when multiple candidates are plausible.
- Do not assume a flat table is required.
- Do not replace a non-flat manufacturer curve with a flat curve unless that is a deliberate, tested choice.
- Do not patch an offset without mapping it to its containing partition.
- Do not repair only the outer checksum.
- Do not verify only the normal runtime.
- Do not write the outer 16-bit checksum as a 32-bit field if the upper bytes may be reserved.
- Do not use a short byte signature as proof of model or sensor identity.
- Do not allow an external profile to silently override a trusted bundled profile.
- Do not call a family match “verified.”
- Do not call a statically validated firmware “hardware-tested.”
- Do not confuse the HC-950 camera-system BIN with its separate 4G modem PAC update.
- Do not rely on volatile UART settings as a persistent fix.
- Do not change file length.

---

## 19. Repository-maintenance rules

When adding a newly examined firmware:

1. Preserve the original BIN SHA-256.
2. Run full checksum and structural analysis.
3. Determine the complete runtime count.
4. Determine camera-module count and sensor identity from multiple markers.
5. Confirm every AE candidate's original curves and context fingerprints.
6. Add a separate JSON profile; do not reuse another build's offsets.
7. Define an automatic target only when real images demonstrate a problem and a tested improvement.
8. For a camera whose factory exposure is good, add recognition only.
9. Add regression tests for profile schema, hashes, candidate counts, curves, and selection behavior.
10. Keep manufacturer firmware out of the repository.

