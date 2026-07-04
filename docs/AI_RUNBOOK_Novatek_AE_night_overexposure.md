# AI RUNBOOK — Novatek trail-camera AE night-overexposure diagnosis & firmware patch

> Audience: an AI agent (or future instance of me) that must re-derive and re-apply this fix on
> **any** Suntek/Novatek trail camera (HC-940Ultra, HC-950Ultra, HC-960Ultra-li, newer builds),
> **even with a different image sensor, ISP driver, or firmware version**.
> Optimized for machine reasoning: exact constants, exact algorithm, explicit decision points,
> and generalization rules. Never hardcode offsets that this document marks as "locate dynamically".

---

## 0. PROBLEM SIGNATURE (when this runbook applies)

- Symptom: **night / IR photos are overexposed**, especially **near subjects** (blown to white),
  while daytime photos are fine.
- Root-cause class: the **auto-exposure (AE) night luminance target is set too high** in the
  camera's embedded AE tuning table (not an IR-LED-power problem, not a sensor-hardware problem).
- Platform class: **Novatek NA51023 / NT96670** family SoC, firmware packaged as **`NVTPACK_FW_HDR2`**.
- If `ae aetdump 0` (UART) shows `tab_ratio_ir` values **> 100** (typically flat `110`) while
  `tab_ratio_mov`/`tab_ratio_photo` cap at `100`, the diagnosis is confirmed.

Confidence check before acting: covering the IR LEDs does NOT fix it (AE just raises gain) ⇒
confirms AE-target cause, not LED power.

---

## 1. ARCHITECTURE MODEL (generalized, stable across models)

- **Dual-CPU SoC** (Novatek NA51023 = marketing name NT96670), MIPS32 LE:
  - **CPU1** runs **µITRON/eCos RTOS** → camera pipeline, ISP, **AE**, UI/menu. *All fixes live here.*
  - **CPU2** runs **Linux** (BusyBox) → 4G/WiFi connectivity, cloud (MQTT `*.car-dv.com`).
- **Boot chain**: internal boot ROM → loader (`LD_NVT`) → **u-boot** (loads at `0x02000000`) →
  copies µITRON to RAM `0x02700000` and starts it. u-boot **verifies the µITRON partition checksum
  at every boot** (string: `uitron pat%d, res check sum fail.`). A wrong checksum ⇒ no boot (brick).
- **Firmware container = `NVTPACK_FW_HDR2`** (16-byte GUID at file offset 0x00 is a *constant*
  format identifier, NOT a content hash; version dword `0x16071515` at 0x10).
- **Partitions** (IDs, from the partition table at file offset 0x88, triples `{id,offset,size}`):
  - 2× `MODELEXT` (model config blobs, ~3 KB each)
  - **µITRON main** (largest "unknown partition", starts with load-addr word `0x027004xx`,
    header block `NT96670`… at partition-offset 0x50) — **AE tuning lives here**
  - u-boot; Linux `uImage`; `UBIFS` rootfs; a **2nd µITRON copy** (different load addr, e.g. `0x004004xx`)
- **AE/IQ/AWB tuning** is loaded at boot as an **embedded lib** named `AE_PARAM_<SENSOR>_EVB`
  (e.g. `AE_PARAM_SC2210_EVB`). Boot log: `[PQ] ae_libext_search--> search ae libext name = AE_PARAM_<SENSOR>_EVB`.
  It resides **inside the µITRON partition** as plaintext. **No SD/runtime override path exists**
  (the SD script engine `A:\ntscript.txt` / `UserCmd_RunScript` is present but is NOT auto-executed
  in normal boots — verified empirically across standby, 4G, and capture boots — do not rely on it).

---

## 2. TOOLS

- **NTKFWinfo** — `github.com/EgorKin/Novatek-FW-info` (clone via git; pure Python). Parses
  `NVTPACK_FW_HDR2`, lists partitions, computes/verifies CRCs, extracts/replaces partitions,
  `-fixCRC`. Confirmed working on NT96670. Use it to (a) confirm format, (b) list partitions +
  offsets, (c) independently verify the file CRC after patching.
  - `python3 NTKFWinfo.py -i <fw.bin>` → info + CRC check.
  - Its `MemCheck_CalcCheckSum16Bit()` is the authoritative checksum implementation (see §4).
- **Python 3** (`struct`, `array`) for locating the parameter and computing checksums directly.
- **UART console** (optional but ideal for diagnosis): `<module> <action> [args]` syntax; the `ae`
  module runs on CPU1. Key commands: `ae aetdump 0` (dump AE tuning), `ae aetset <cmd> <val>`
  (set, RAM-only/volatile), `ae dumpcurve 0`, `ae aetuart 0 1` (live AE msgs), `ae isomax 0 <v>`,
  `ae uiiso 0 <v>`.
- **PIL/Pillow** (or any) for clipping analysis of test JPGs when EXIF ISO is absent.

---

## 3. PHASE A — ACQUIRE & PARSE

1. Obtain the **exact** firmware for the target model (camera's own SD-update `.bin`, or vendor).
   Keep a pristine copy as the restore image. The camera's update filename is typically
   `FWHC940A.bin` (verify what the model expects; it is the on-SD update name, not the model name).
2. `python3 NTKFWinfo.py -i fw.bin`. Record:
   - `Firmware file ORIG_CRC == CALC_CRC` (confirms `NVTPACK_FW_HDR2` + our algorithm class).
   - Partition table: note the **µITRON main partition** = the large "unknown partition" whose
     first dword is a `0x027004xx` load address and whose header at `+0x50` contains `NT96670`.
     Record its `start_offset` and `size`. (These are **version/model specific — never hardcode**.)
   - Note the **2nd µITRON copy** (another "unknown partition", first dword `0x004004xx`).

---

## 4. PHASE B — CHECKSUM ALGORITHM (authoritative; verify before use)

Two independent checksums must be kept valid after any edit:

- **Per-partition internal checksum** (u-boot verifies µITRON's at boot):
  - stored as 16-bit LE immediately after magic bytes `55 aa`.
  - location within partition (`ignoreCRCoffset`): **`0x36`** for MODELEXT, **`0x6E`** for µITRON.
    (Confirm by locating `55 aa` in the first 0x90 bytes of the partition; checksum is the 2 bytes after.)
  - range = the **whole partition** (its size).
- **Whole-file CRC** (updater `FW bin chk`): stored at **file offset `0x24`** (4 bytes; value in low 16 bits).
  - `ignoreCRCoffset = 0x24`, range = **whole file**.

Algorithm (position-weighted 16-bit two's-complement sum; from NTKFWinfo `MemCheck_CalcCheckSum16Bit`):

```python
import array
def ntk_cksum16(buf, off, length, ignore_off):
    n = length // 2
    a = array.array('h')                      # signed 16-bit LE words
    a.frombytes(bytes(buf[off:off + n*2]))
    a[ignore_off // 2] = 0                     # zero the stored-checksum word
    s = (sum(a) + (n - 1) * n // 2) & 0xFFFF   # + triangular number  n*(n-1)/2  (positional term)
    return ((~s & 0xFFFF) + 1) & 0xFFFF        # two's complement (negate)
```

**MANDATORY self-test before patching**: compute `ntk_cksum16` over the *unmodified* µITRON
partition (`ignore_off=0x6E`) and over the *unmodified* whole file (`ignore_off=0x24`); both must
equal the stored values. If not, the format/offsets differ on this build — re-derive before editing.

Constants table (stable for NVTPACK_FW_HDR2 / NA51023 seen so far; re-verify per build):

| item | offset | ignoreCRCoffset | range |
|---|---|---|---|
| file CRC | file 0x24 (4B) | 0x24 | whole file |
| MODELEXT partition cksum | part +0x36 (2B, after `55 aa`) | 0x36 | whole partition |
| µITRON partition cksum | part +0x6E (2B, after `55 aa`) | 0x6E | whole partition |

---

## 5. PHASE C — DIAGNOSE & LOCATE THE PARAMETER

### C1. Diagnose (prefer UART; else image analysis)
- UART: `ae aetdump 0`. Inspect `data.tExpectLum.expect_lum.tab_ratio_ir` (night target, ×21),
  `tab_ratio_mov`/`tab_ratio_photo` (day), `lum_mov`, `tOverExposure.over_expoure.lum`,
  `tBoundary.proc_boundary.iso_prv.h` (ISO ceiling). Diagnosis = `tab_ratio_ir` > 100.
- Single-AE check: `ae dumpcurve 1` → CPU exception ⇒ only AE id 0 exists ⇒ `tab_ratio_ir`
  governs BOTH preview and capture.
- Image analysis (no UART): grayscale histogram of overexposed night JPG; large fraction at 255
  (e.g. ~20%+) confirms clipping. Use as before/after metric when EXIF ISO is missing.

### C2. Locate `tab_ratio_ir` in the µITRON partition (plaintext, uint32×21)
Struct field order (Novatek AE format; same regardless of sensor):
```
expect_lum   : lum_mov(u32), lum_photo(u32), tab_ratio_mov[21], tab_ratio_photo[21], tab_ratio_ir[21]
over_exposure: enable(u32), lum(u32), tab_ratio[21], tab_thr_mov[21], tab_thr_ir[21]
histogram    : mode(u32), lum(u32), tab_ratio[21]
boundary     : iso_prv.h(u32), iso_prv.l(u32), ...
```
Location strategy (robust across versions — DO NOT hardcode the offset):
- Read the exact arrays from `ae aetdump 0`; search the µITRON partition for the matching byte
  sequences.
- Best anchors (distinctive constants): `over_exposure.lum` (e.g. `869`) and `histogram.lum`
  (e.g. `486`); `tab_ratio_mov` ramp (e.g. `44,48,52,58,62,69,…`). From `over_exposure` you can
  walk back: `tab_ratio_ir` is the 21-u32 array ending immediately before `{enable, lum}` of
  `over_exposure`.
- Validate the candidate: it should be a 21-element u32 array, preceded by `tab_ratio_photo`
  (often flat 100) and by `lum_photo`, and followed by `over_exposure`. Cross-check against aetdump.
- **2nd µITRON copy**: a similar 21×value array may exist there but at a *different relative offset*
  and possibly a *different structure*. Verify its neighbors match the AE struct BEFORE patching it.
  The **active** image is the main µITRON (loads to `0x02700000`); patching only it is sufficient
  and was empirically confirmed to fix the live camera. Patch the copy only if verified as the same field.

### C3. Optional secondary parameter
- `iso_prv.h` (ISO ceiling, e.g. `12800`): lower it (e.g. → `3200`) to cap runaway ISO in Auto mode.
  Locate via aetdump value near the AE struct; same patch+checksum procedure.

---

## 6. PHASE D — PATCH (exact procedure & ORDER)

1. Copy original → working file (`bytearray`).
2. Overwrite `tab_ratio_ir` (21× u32) with the target value. **Start value = 55** (down from 110).
   - Stronger near-subject taming: 40–45. Milder: 60–70. (Value is empirical; iterate via re-flash.)
   - Optional: overwrite `iso_prv.h` (u32) with the ISO cap.
3. Recompute the **µITRON partition internal checksum**: `ntk_cksum16(buf, uit_off, uit_size, 0x6E)`;
   write 2 bytes LE at `uit_off + 0x6E`.
   - If (and only if) the 2nd µITRON copy was also patched, fix its checksum the same way
     (`ignore_off = 0x6E` at that partition).
4. Recompute the **whole-file CRC** LAST (it covers the just-fixed partition):
   `ntk_cksum16(buf, 0, len(buf), 0x24)`; write as `<I` (4 bytes) at file `0x24`.
5. Write file. Keep size **unchanged** (in-place edits only; do not change lengths → `FW len chk` stays valid).

**Order is mandatory: data → partition checksum(s) → file CRC.**
Consistency note (sanity signal, not a requirement): because the partition checksum is a
two's-complement of a positional sum, re-fixing it makes the partition's contribution to the
file sum invariant; the file CRC therefore often ends up **unchanged** from the original. If the
file CRC changes wildly, re-check the partition checksum step.

---

## 7. PHASE E — VERIFY (do not skip)

- Recompute both checksums from the **freshly-written** file; both must match the newly-stored values.
- `python3 NTKFWinfo.py -i patched.bin` → `Firmware file ORIG_CRC == CALC_CRC` (green) and all
  recognized partitions still valid.
- Byte-diff original vs patched: expect ONLY (a) the target array bytes, (b) the µITRON checksum
  (2 B), and (possibly) (c) the file CRC (4 B). Any other diff = abort.

---

## 8. PHASE F — DEPLOY & FALLBACK

- Rename patched file to the model's update name (e.g. `FWHC940A.bin`), place on **SD card root**.
- Keep the pristine original on hand to restore.
- The camera flashes on boot. **Empirically verified on HC-960Ultra-li**: the SD updater accepts a
  correctly-checksummed image — no signature / `Invalid Key` / `PKCS7` block on the SD path
  (those strings pertain to USB/MSDC or other paths). u-boot's boot-time µITRON checksum passes
  because we fixed it.
- Verify the fix: `ae aetdump 0` shows the new `tab_ratio_ir`; a real night photo shows reduced
  clipping (histogram) / lower ISO.
- **Fallbacks**:
  - Update rejected ⇒ no change (safe). Investigate whether the model's SD path adds a signature;
    otherwise use the u-boot console: `fatload mmc 0 <addr> <part>.bin; sf erase <off> <len>;
    sf write <addr> <off> <len>; reset` (documented for NA51023 in `github.com/hn/reolink-camera`).
  - Does not boot ⇒ re-flash pristine original.

---

## 9. GENERALIZATION RULES (other models / sensors / firmware versions)

1. **Different sensor/driver** (e.g. not SC2210): the AE lib is `AE_PARAM_<SENSOR>_EVB`; the AE
   **struct layout is identical** (Novatek AE format). Read field values from `ae aetdump 0` and
   locate by content, not by sensor. `tab_ratio_ir` is always the night/IR target.
2. **Different model / newer firmware**: partition offsets and the AE-struct file offset **change**.
   ALWAYS: parse with NTKFWinfo, self-test the checksum algorithm against stored values, and locate
   the array by anchors/aetdump content. The **checksum algorithm and the offsets 0x24 / 0x36 / 0x6E**
   have been constant across HC-940/950/960 (NVTPACK_FW_HDR2) but must be re-verified each time
   (the self-test in §4 does this automatically).
3. **If the checksum self-test fails** on a new build: the container may be a newer NVTPACK variant.
   Re-read `MemCheck_CalcCheckSum16Bit` and the call sites in the current NTKFWinfo; the
   `ignoreCRCoffset` set it tries is `{0x6E,0x16E,0x26E,0x36E,0x46E}` for data partitions and
   `0x24` for the file — sweep these and pick the one reproducing the stored value.
4. **Never** rely on `A:\ntscript.txt` for a persistent fix on this platform (auto-run not triggered
   in normal boots). The embedded firmware patch is the only reliable persistent method.
5. Keep every edit **in-place / length-preserving** to avoid touching the partition table and lengths.

---

## 10. REFERENCE (HC-960Ultra-li, firmware FWHC940A / build 20260326) — worked example

- File size 31,633,412; file CRC `0x4044` @0x24.
- µITRON main = partition ID 3, `start 0x1878`, `size 7,240,660`, internal cksum `0x1a2d` @+0x6E.
- `tab_ratio_ir` @ file `0x6cb628` = `{110}×21`. Patched → `{55}×21`. New µITRON cksum `0x1eb0`.
  File CRC unchanged (`0x4044`). Total bytes changed: 23 (84B array-lows + 2B cksum; file CRC same).
- Result: night clipping (pure-white pixels) dropped from ~22% to ~1% (test) / ~7% (near white box).
- 2nd µITRON copy (ID 9) had a 110×21 array that was NOT `tab_ratio_ir` (different neighbors) → not patched.
- Sensor SC2210 → lib `AE_PARAM_SC2210_EVB`. Single AE (id 1 crashes).

---

## 11. PITFALLS

- Do NOT trust the debug help text "ISO … index 0~5" as the user menu; the menu options are limited
  by which option **strings** exist in the µITRON (e.g. only `100/200/400` present ⇒ menu shows only those).
- Do NOT change file length.
- Do NOT skip the checksum self-test; a wrong algorithm silently produces a brick-on-boot image.
- The 16-byte file header GUID is constant — do not treat it as a hash and do not modify it.
- Volatile UART `ae aetset` values are lost on the per-trigger cold boot; they can validate a value
  but cannot be the deployed fix.
