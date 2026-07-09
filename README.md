# Fixing night-photo overexposure on Suntek / Novatek trail cameras

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

**English** | [中文](README.zh-CN.md)

A complete, reproducible guide to **diagnose and fix chronic night (IR) over‑exposure** on
Suntek/Novatek‑platform trail cameras (e.g. **HC‑940Ultra, HC‑950Ultra, HC‑960Ultra‑li** and
siblings), by correcting the auto‑exposure (AE) tuning **inside the firmware** and re‑computing the
firmware checksums so the patched image boots and flashes normally.

> **This has been verified end‑to‑end on a real HC‑960Ultra‑li:** the patched firmware flashed via
> SD card, booted, and reduced fully‑blown‑white pixels in night shots from ~22 % to ~1 %.

---

## ⚠️ Read this first — risk & disclaimer

Modifying and flashing firmware can **brick your camera**. You do this **at your own risk**; there is
no warranty and the authors accept no liability. Mitigate the risk:

- **Keep the original, unmodified firmware file** for your exact model to restore if needed.
- Only change **data bytes**; never change the file length.
- **Always run the checksum self‑test** (below) before flashing — a wrong checksum yields an image
  that fails to boot.
- Prefer to test with **physical access** to the camera (not remotely).

The good news: on the SD‑update path of this platform, an image with **incorrect** checksums is
**rejected before writing** (the camera keeps working) rather than half‑flashed. That makes a careful
attempt relatively safe — but treat bricking as possible anyway.

---

## Repository contents

- [`patch_ae.py`](patch_ae.py) — the patch tool (self-test, auto-locate, patch, checksum, verify).
- [`README.md`](README.md) / [`README.zh-CN.md`](README.zh-CN.md) — this guide (English / 中文).
- [`LICENSE`](LICENSE) — MIT (covers the tool & docs only; no firmware is distributed).

> This project does **not** include any manufacturer firmware — you supply your own model's file.

---

## 1. The problem

At night the camera switches to IR mode. On affected units the AE engine targets a **luminance that
is too high for IR**, so it pushes ISO/exposure until **near subjects (an animal a few metres away)
are blown to pure white**, while daytime photos are fine. Vendor advice ("set ISO to 100 in the
menu") only partially helps because it is a blunt cap; the real cause is one AE parameter.

**Root cause:** the AE night/IR luminance‑ratio table **`tab_ratio_ir`** is set to **110 %** (higher
than the daytime maximum of 100 %). Lowering it (e.g. to **55**) fixes the over‑exposure at its source.

This is **not** an IR‑LED‑power problem: covering the LEDs does not help, because the AE simply raises
gain to reach its (too‑high) target.

---

## 2. Platform background (what's inside)

- SoC: **Novatek NA51023** (marketing name **NT96670**), dual‑CPU MIPS32.
  - **CPU1**: µITRON/eCos RTOS — camera pipeline, ISP, **AE**, on‑screen menu. *The fix lives here.*
  - **CPU2**: Linux — 4G/WiFi, cloud.
- Boot: internal ROM → loader (`LD_NVT`) → **u‑boot** → copies the **µITRON** image to RAM and starts
  it. **u‑boot checks the µITRON partition checksum on every boot**, so a patched image must carry a
  correct checksum or it will not boot.
- Firmware file format: **`NVTPACK_FW_HDR2`** — a container with a header, a partition table, and
  several partitions: two `MODELEXT` config blobs, the **main µITRON** image (where AE tuning lives),
  u‑boot, a Linux `uImage`, a `UBIFS` root filesystem, and a **second µITRON copy**.
- AE/IQ/AWB tuning is embedded in the µITRON as a named lib **`AE_PARAM_<SENSOR>_EVB`**
  (e.g. `AE_PARAM_SC2210_EVB` for the SmartSens SC2210 sensor). There is **no SD/runtime override**
  for it (the `A:\ntscript.txt` script engine exists but is **not** auto‑executed on normal boots),
  so the only reliable persistent fix is to patch the firmware image.

---

## 3. Prerequisites

**Software**
- **Python 3** (with the standard `struct`, `array` modules).
- **NTKFWinfo** — the Novatek firmware toolkit by EgorKin:
  `git clone https://github.com/EgorKin/Novatek-FW-info` (repo also called *NTKFWinfo*). It parses
  `NVTPACK_FW_HDR2`, lists partitions, and verifies CRCs. We use it to confirm the format and to
  independently verify our patched file.
- **Pillow** (`pip install pillow`) — optional, for measuring over‑exposure in test JPGs.

**Hardware**
- An **SD card** (the camera flashes firmware from the card root on boot).
- **Optional but very helpful: a 3.3 V USB‑UART adapter** to reach the camera's serial console.
  It lets you read the live AE table with `ae aetdump 0` and confirm values. (Reaching the UART pads
  usually requires opening the camera.)

**Files**
- The **exact firmware** for your model (the camera's own SD‑update `.bin`, or from the vendor).
  Keep a pristine copy.

---

## 4. Step 1 — Inspect the firmware with NTKFWinfo

```bash
git clone https://github.com/EgorKin/Novatek-FW-info
cd Novatek-FW-info
python3 NTKFWinfo.py -i /path/to/FWHC940A.bin
```

You should see something like:

```
NVTPACK_FW_HDR2 found
Found 7 partitions
Firmware file ORIG_CRC:0x4044  CALC_CRC:0x4044          <- format & CRC confirmed
 ID   START_OFFSET   END_OFFSET        SIZE      ORIG_CRC  CALC_CRC   TYPE
  1   0x000000D4  - 0x00000CB8         3,044     0xC55C    0xC55C     MODELEXT INFO: Chip:NT96670 ...
  2   0x00000CB8  - 0x00001878         3,008     0xFEF8    0xFEF8     MODELEXT INFO: Chip:NT96670 ...
  3   0x00001878  - 0x006E944C     7,240,660     0x0000    0x0000     unknown partition   <- µITRON (main)
  4   ...                                                              (u-boot)
  6   ...                                                              uImage (Linux)
  7   ...                                                              CKSM UBIFS  (rootfs)
  9   0x015B7D1C  - 0x01E2B004     8,860,392     0x0000    0x0000     unknown partition   <- 2nd µITRON copy
```

**Write down** the **main µITRON partition** row (the large *unknown partition* whose data starts with
a `0x027004xx` load address). Note its **START_OFFSET** and **SIZE** — you'll need them. These values
**differ between models and firmware versions**, so never assume; always read them here.

> `ORIG_CRC == CALC_CRC` on the file line confirms the container format and that the checksum family
> used below is correct.

---

## 5. Step 2 — Diagnose the AE parameter

### Option A — UART console (best)
Connect the UART, power the camera, and type:

```
ae aetdump 0
```

Look at the `expect_lum` block. A faulty unit shows:

```
data.tExpectLum.expect_lum.tab_ratio_mov = { 44, 48, 52, ... 100, 100 }   (day: caps at 100)
data.tExpectLum.expect_lum.tab_ratio_ir  = { 110, 110, ... 110 }          (night: 110 = TOO HIGH)
...
data.tBoundary.proc_boundary.iso_prv.h   = 12800                          (ISO ceiling)
```

`tab_ratio_ir > 100` (typically flat **110**) is the confirmed fault. Also note there is a single AE
instance (typing `ae dumpcurve 1` causes a CPU exception), so this one value controls both preview and
capture.

### Option B — No UART (image analysis)
Take a night photo, then measure how much of it is blown to white:

```python
from PIL import Image
im = Image.open("night.jpg").convert("L")
h = im.histogram(); tot = sum(h)
print("pure white (255): %.1f%%" % (h[255]/tot*100))
```

A large fraction (~20 %+) at 255 indicates severe clipping. Use the same measure later to confirm the fix.

---

## 6. Step 3 — Understand the two checksums

After editing the µITRON you must fix **two** checksums, or the camera will reject/not boot the image:

1. **µITRON partition internal checksum** — u‑boot checks this every boot. It is a 16‑bit value stored
   right after the magic bytes `55 aa` inside the partition header, at **partition offset `0x6E`**.
2. **Whole‑file CRC** — the updater's file check. A 16‑bit value stored at **file offset `0x24`**.

Both use the **same algorithm**: a position‑weighted 16‑bit two's‑complement sum. Reference
implementation (equivalent to NTKFWinfo's `MemCheck_CalcCheckSum16Bit`):

```python
import array
def ntk_cksum16(buf, off, length, ignore_off):
    n = length // 2
    a = array.array('h')                        # signed 16-bit little-endian words
    a.frombytes(bytes(buf[off:off + n*2]))
    a[ignore_off // 2] = 0                       # zero the word that holds the checksum itself
    s = (sum(a) + (n - 1) * n // 2) & 0xFFFF     # sum of words + triangular number n*(n-1)/2
    return ((~s & 0xFFFF) + 1) & 0xFFFF          # two's complement (negate)
```

- MODELEXT partitions use `ignore_off = 0x36`; the µITRON uses `ignore_off = 0x6E`; the file uses
  `ignore_off = 0x24`.
- The **range** is the whole partition for a partition checksum, and the whole file for the file CRC.

---

## 7. Step 4 — Patch with `patch_ae.py`

The repository ships a ready-to-run tool, [`patch_ae.py`](patch_ae.py). It:

1. **self-tests** the checksum algorithm against your firmware (and aborts if the offsets differ),
2. **auto-detects** the main uITRON partition from the partition table,
3. **locates `tab_ratio_ir` by structure** — sensor-independent: it anchors on the `tab_ratio_mov`
   ramp (a monotonic array whose next array is flat), never a hard-coded offset,
4. sets it to your value and fixes the **uITRON partition checksum** and the **whole-file CRC**
   in the correct order,
5. **verifies** the result **in memory before writing**, writes the file **atomically**, then
   re-checks it **round-trip** from disk — so it never leaves a half-written or invalid image.

```bash
python3 patch_ae.py FWHC940A.bin                 # tab_ratio_ir -> 55, writes FWHC940A_patched.bin
python3 patch_ae.py FWHC940A.bin -o out.bin --ir 45   # custom output + value (lower = darker night)
python3 patch_ae.py FWHC940A.bin --iso-cap 3200       # also cap iso_prv.h (e.g. 12800 -> 3200)
python3 patch_ae.py FWHC940A.bin --dry-run            # analyse & locate only, write nothing
python3 patch_ae.py FWHC940A.bin --verify-only        # only check a file's checksums
python3 patch_ae.py FWHC940A.bin --uit-off 0x1878 --uit-size 7240660   # manual override
python3 patch_ae.py FWHC940A.bin --ir-offset 0x6cb628                 # choose one table explicitly
python3 patch_ae.py FWHC940A.bin --all               # patch ALL tab_ratio_ir tables if several exist
python3 patch_ae.py FWHC940A.bin --version           # print the tool version
```

Verified output on HC-960Ultra-li:

```
[i] uITRON partition: off=0x1878 size=7240660 (0x6e7bd4)
[ok] self-test: uITRON cksum @+0x6e=0x1a2d reproduced; file CRC @0x24=0x4044 calc=0x4044
[i] tab_ratio_ir @ file 0x6cb628 = 110 x21
[patch] tab_ratio_ir @0x6cb628 110 -> 55
[cksum] uITRON 0x1a2d->0x1eb0   file CRC 0x4044->0x4044
[verify] on-disk uITRON checksum: OK   file CRC: OK
[done] wrote FWHC940A_patched.bin  (bytes changed: 23)
```

If the **self-test fails**, your build uses different offsets — see section 11 (Generalizing).

### Troubleshooting

- **"N tab_ratio_ir candidates" / wrong table patched** — pass `--ir-offset 0x<offset>` to choose
  one (the offsets are printed), or `--all` to patch every table.
- **"checksum self-test failed for every candidate offset"** — a newer container variant; find the
  right offsets with `NTKFWinfo -i` and pass `--uit-off/--uit-size`.
- **"could not auto-detect uITRON"** — pass `--uit-off/--uit-size` from `NTKFWinfo -i`.
- **"original file CRC mismatch"** — the input looks already modified; re-extract a clean firmware
  (or add `--force` if you understand the risk).
- Nothing is written unless **both checksums verify**; on any failure the tool exits non-zero and
  writes no file.

## 8. Step 5 — Independently verify with NTKFWinfo

```bash
python3 NTKFWinfo.py -i FWHC940A_patched.bin
```

Confirm `Firmware file ORIG_CRC == CALC_CRC` (green) and that all recognized partitions are still
valid. (NTKFWinfo treats the µITRON as "unknown"/CRC 0x0000 — that's expected; its internal checksum
is the one your script fixed and verified.)

---

## 9. Step 6 — Flash and confirm

1. Rename the patched file to the update name your camera expects (commonly **`FWHC940A.bin`**) and
   copy it to the **root of the SD card**. Keep the original elsewhere for restore.
2. Insert the card and power the camera; it flashes on boot. (In the UART log you'll see
   `uiFWUpdate…`, `upd_src_size=…`, then a normal boot.)
3. Confirm the change:
   - UART: `ae aetdump 0` should now show `tab_ratio_ir = { 55, … }`.
   - Real night photo: re‑run the histogram measurement — the pure‑white fraction should drop sharply
     (in the verified case, ~22 % → ~1 %).

---

## 10. Tuning & options

- **`tab_ratio_ir` value**: `55` is a good starting point (≈ −1 stop of night target). For stronger
  taming of very close, bright subjects use `45` or `40`; for a brighter night image use `60`–`70`.
  Because flashing works reliably once the checksum is correct, you can iterate: change the value,
  re‑run the script, re‑flash, compare a night photo.
- **ISO ceiling** (`iso_prv.h`, e.g. `12800 → 3200`): set `NEW_ISO_PRV_H` in the script to cap ISO in
  Auto mode without lowering the general night target. Useful against ISO run‑away on near subjects.
- **Menu ISO options**: on some models the menu shows only `Auto/100/200/400` because only those
  option **strings** exist in the firmware — the AE can still use higher ISO internally. Prefer the
  `iso_prv.h` patch over trying to add menu entries (which requires far riskier UI/table surgery).

---

## 11. Generalizing to other models / sensors / firmware versions

- **Different sensor/driver** (not SC2210): the AE lib is `AE_PARAM_<SENSOR>_EVB`; the AE **struct
  layout is identical**. Diagnose with `ae aetdump 0` and locate `tab_ratio_ir` by content — the
  script's anchor logic (flat 21‑array followed by `over_exposure`) is sensor‑independent.
- **Different model / newer firmware**: partition offsets and the AE‑struct file offset **change**.
  Always re‑read them with `NTKFWinfo -i`, set the CONFIG at the top of the script, and rely on the
  **self‑test** to confirm the checksum offsets (`0x24`/`0x36`/`0x6E`) are still correct.
- **If the self‑test fails**: the container may be a newer variant. NTKFWinfo tries partition
  `ignoreCRCoffset` values `{0x6E, 0x16E, 0x26E, 0x36E, 0x46E}` and file `0x24`; sweep these until
  `ntk_cksum16` reproduces the stored value, then use that offset.

---

## 12. Troubleshooting / fallback

- **Update seems ignored / rejected**: the camera keeps the old firmware (safe). Re‑check the file
  name and that checksums are correct. If the model's SD path adds a signature check, use the u‑boot
  console instead: interrupt boot, then (addresses/lengths from your partition table)
  `fatload mmc 0 <ram_addr> <part>.bin; sf erase <flash_off> <len>; sf write <ram_addr> <flash_off> <len>; reset`.
  This bypasses the updater; only u‑boot's boot‑time µITRON checksum matters (which you fixed).
  The same SoC and console commands are documented in `github.com/hn/reolink-camera`.
- **Camera doesn't boot after flashing**: re‑flash the pristine original firmware.

---

## Credits & references

- **NTKFWinfo** — `github.com/EgorKin/Novatek-FW-info` — Novatek `NVTPACK_FW_HDR2` parser and CRC
  logic (the checksum algorithm here is derived from its `MemCheck_CalcCheckSum16Bit`).
- **hn/reolink-camera** — `github.com/hn/reolink-camera` — NA51023 boot process and u‑boot flashing
  commands reference.

## License

[MIT](LICENSE) — for the **tooling and documentation in this repository only**. It does **not**
cover, and this repository does **not** distribute, any manufacturer firmware; you supply your own.
"Novatek", "Suntek" and other names are trademarks of their respective owners. Provided **as-is,
without warranty**; flashing firmware is at your own risk (see the warning at the top).
