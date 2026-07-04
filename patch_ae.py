#!/usr/bin/env python3
"""
patch_ae.py - Fix night (IR) over-exposure on Suntek / Novatek (NA51023 / NT96670) trail cameras.

It lowers the AE night-luminance target `tab_ratio_ir` inside the firmware's uITRON partition and
re-computes the Novatek NVTPACK_FW_HDR2 checksums (uITRON partition checksum + whole-file CRC) so the
patched image boots and flashes normally.

Verified on HC-960Ultra-li (build FWHC940A / 20260326). Designed to generalize to HC-940/950Ultra,
newer builds, and other image sensors, because it:
  * auto-detects the main uITRON partition from the NVTPACK_FW_HDR2 partition table,
  * self-tests the checksum algorithm against the original stored values (aborts if it can't),
  * locates tab_ratio_ir by structure (a flat 21x uint32 array followed by over_exposure), not by a
    hard-coded offset.

USE AT YOUR OWN RISK. Modifying/flashing firmware can brick the camera. Keep the original firmware.

Examples
--------
  python3 patch_ae.py FWHC940A.bin                    # tab_ratio_ir -> 55, writes FWHC940A_patched.bin
  python3 patch_ae.py FW.bin -o out.bin --ir 45       # custom output and value (lower = darker night)
  python3 patch_ae.py FW.bin --iso-cap 3200           # also cap iso_prv.h (e.g. 12800 -> 3200)
  python3 patch_ae.py FW.bin --dry-run                # analyze + locate only, write nothing
  python3 patch_ae.py FW.bin --verify-only            # only check a file's checksums
  python3 patch_ae.py FW.bin --uit-off 0x1878 --uit-size 7240660   # manual override if auto-detect fails

License: MIT
"""

import argparse
import array
import os
import struct
import sys

# --- NVTPACK_FW_HDR2 layout constants (stable across HC-940/950/960; re-verified by self-test) ---
HDR2_VERSION_OFF = 0x10           # dword == 0x16071515 for NVTPACK_FW_HDR2
HDR2_VERSION = 0x16071515
PART_COUNT_OFF = 0x18             # dword: number of partitions
PART_TABLE_OFF = 0x88            # array of {id, offset, size} uint32 triples
FILE_CRC_OFF = 0x24              # whole-file 16-bit CRC (stored as dword, value in low 16 bits)
# ignoreCRCoffset candidates NTKFWinfo tries for data partitions (checksum sits after '55 aa'):
UIT_CKSUM_CANDIDATES = (0x6E, 0x16E, 0x26E, 0x36E, 0x46E)


def ntk_cksum16(buf, off, length, ignore_off):
    """Novatek position-weighted 16-bit two's-complement checksum
    (equivalent to NTKFWinfo's MemCheck_CalcCheckSum16Bit)."""
    n = length // 2
    a = array.array('h')                         # signed 16-bit little-endian words
    a.frombytes(bytes(buf[off:off + n * 2]))
    a[ignore_off // 2] = 0                        # zero the word that stores the checksum itself
    s = (sum(a) + (n - 1) * n // 2) & 0xFFFF      # sum of words + triangular number n*(n-1)/2
    return ((~s & 0xFFFF) + 1) & 0xFFFF           # two's complement (negate)


def parse_partitions(d):
    """Return list of (id, offset, size) from the NVTPACK_FW_HDR2 partition table."""
    count = struct.unpack_from('<I', d, PART_COUNT_OFF)[0]
    if not (0 < count < 32):
        count = 16
    parts, o = [], PART_TABLE_OFF
    for _ in range(count):
        pid, off, size = struct.unpack_from('<III', d, o)
        if pid > 64 or off >= len(d) or size == 0 or off + size > len(d):
            break
        parts.append((pid, off, size))
        o += 12
    return parts


def find_uitron(d, parts):
    """Main uITRON = the partition whose first dword is a load address ~0x027004xx and that has the
    '55 aa' magic in its header. (The active image loads to RAM 0x02700000.)"""
    fallback = None
    for pid, off, size in parts:
        first = struct.unpack_from('<I', d, off)[0]
        has_magic = d.find(b'\x55\xaa', off, off + 0x90) > 0
        if (first & 0xFFF00000) == 0x02700000 and has_magic:
            return off, size
        if fallback is None and has_magic and size > 1_000_000 and b'NT96670' in d[off:off + 0x80]:
            fallback = (off, size)
    return fallback


def detect_uit_cksum_off(d, uit_off, uit_size):
    """Find the ignoreCRCoffset whose stored checksum our algorithm reproduces (self-test)."""
    # try the known fixed candidates first
    for ic in UIT_CKSUM_CANDIDATES:
        if d[uit_off + ic - 2:uit_off + ic] != b'\x55\xaa':
            continue
        stored = struct.unpack_from('<H', d, uit_off + ic)[0]
        if ntk_cksum16(d, uit_off, uit_size, ic) == stored:
            return ic, stored
    # otherwise locate '55 aa' dynamically in the header and test that offset
    m = d.find(b'\x55\xaa', uit_off, uit_off + 0x400)
    if m > 0:
        ic = (m + 2) - uit_off
        stored = struct.unpack_from('<H', d, m + 2)[0]
        if ntk_cksum16(d, uit_off, uit_size, ic) == stored:
            return ic, stored
    return None, None


def _flat21(d, o):
    return len(set(struct.unpack_from('<21I', d, o))) == 1


def _mono_ramp21(d, o):
    a = struct.unpack_from('<21I', d, o)
    if not all(0 <= x <= 4096 for x in a) or a[0] == a[-1]:   # in-range and NOT flat
        return False
    return all(a[i] <= a[i + 1] for i in range(20))           # non-decreasing ramp


def find_tab_ratio_ir(d, off, size):
    """Structure-anchored, sensor-independent locator.
    expect_lum layout: ..., tab_ratio_mov[21] (ramp rising to ~100), tab_ratio_photo[21] (flat),
    tab_ratio_ir[21] (flat, the night/IR target we want).
    Anchor on tab_ratio_mov = a monotonic non-decreasing 21x array whose NEXT 21x array is FLAT.
    This uniquely picks tab_ratio_mov and excludes over_exposure.tab_thr_mov (whose next array,
    tab_thr_ir, is itself a ramp). Then tab_ratio_ir_off = mov_off + 0xA8 (skip mov[21] + photo[21])."""
    end = off + size
    o = off
    while o + 0xA8 + 0x54 <= end:
        if _mono_ramp21(d, o) and _flat21(d, o + 0x54) and _flat21(d, o + 0xA8):
            return o + 0xA8
        o += 4
    return -1


def find_iso_prv_h(d, ir_off, limit):
    """iso_prv = {iso_prv.h (max ISO), iso_prv.l (min ISO, typically 100)}. Search forward from
    tab_ratio_ir for {H, 100} where H is a plausible ISO ceiling. Best-effort: verify via UART
    `ae aetdump 0` (proc_boundary.iso_prv.h) afterwards."""
    o = ir_off
    while o + 8 <= limit:
        h, l = struct.unpack_from('<2I', d, o)
        if l == 100 and 400 <= h <= 204800 and h % 100 == 0 and h != 100:
            return o
        o += 4
    return -1


def check(d, uit_off, uit_size, uit_ic):
    ok_uit = struct.unpack_from('<H', d, uit_off + uit_ic)[0] == ntk_cksum16(d, uit_off, uit_size, uit_ic)
    ok_file = (struct.unpack_from('<I', d, FILE_CRC_OFF)[0] & 0xFFFF) == ntk_cksum16(d, 0, len(d), FILE_CRC_OFF)
    return ok_uit, ok_file


def main():
    ap = argparse.ArgumentParser(description='Patch Novatek trail-cam night over-exposure (tab_ratio_ir).')
    ap.add_argument('input', help='original firmware .bin')
    ap.add_argument('-o', '--output', help='output file (default: <input>_patched.bin)')
    ap.add_argument('--ir', type=int, default=55, help='new tab_ratio_ir value (default 55; lower = darker night)')
    ap.add_argument('--iso-cap', type=int, default=None, help='set iso_prv.h to this (e.g. 3200); original often 12800')
    ap.add_argument('--uit-off', type=lambda x: int(x, 0), default=None, help='manual uITRON start offset (hex ok)')
    ap.add_argument('--uit-size', type=lambda x: int(x, 0), default=None, help='manual uITRON size')
    ap.add_argument('--dry-run', action='store_true', help='analyze and locate only; write nothing')
    ap.add_argument('--verify-only', action='store_true', help='only verify the checksums of the input file')
    args = ap.parse_args()

    d = bytearray(open(args.input, 'rb').read())
    if struct.unpack_from('<I', d, HDR2_VERSION_OFF)[0] != HDR2_VERSION:
        print('[warn] NVTPACK_FW_HDR2 version marker (0x16071515) not found at 0x10 - is this the right file?')

    # locate the main uITRON partition
    if args.uit_off is not None and args.uit_size is not None:
        uit_off, uit_size = args.uit_off, args.uit_size
    else:
        found = find_uitron(d, parse_partitions(d))
        if not found:
            sys.exit('[error] could not auto-detect uITRON; pass --uit-off/--uit-size (from NTKFWinfo -i)')
        uit_off, uit_size = found
    print('[i] uITRON partition: off=0x%x size=%d (0x%x)' % (uit_off, uit_size, uit_size))

    # self-test the checksum algorithm/offsets against the original file
    uit_ic, uit_stored = detect_uit_cksum_off(d, uit_off, uit_size)
    if uit_ic is None:
        sys.exit('[error] checksum self-test failed for every candidate offset - unknown container variant')
    file_stored = struct.unpack_from('<I', d, FILE_CRC_OFF)[0] & 0xFFFF
    file_calc = ntk_cksum16(d, 0, len(d), FILE_CRC_OFF)
    print('[ok] self-test: uITRON cksum @+0x%x=0x%04x reproduced; file CRC @0x24=0x%04x calc=0x%04x'
          % (uit_ic, uit_stored, file_stored, file_calc))
    if file_stored != file_calc:
        print('[warn] original file CRC mismatch - the input may already be modified')

    if args.verify_only:
        ok_uit, ok_file = check(d, uit_off, uit_size, uit_ic)
        print('[verify] uITRON checksum: %s   file CRC: %s'
              % ('OK' if ok_uit else 'MISMATCH', 'OK' if ok_file else 'MISMATCH'))
        return

    # locate tab_ratio_ir
    ir = find_tab_ratio_ir(d, uit_off, uit_size)
    if ir < 0:
        sys.exit('[error] could not locate tab_ratio_ir; read values via UART `ae aetdump 0` and locate manually')
    old = struct.unpack_from('<21I', d, ir)
    print('[i] tab_ratio_ir @ file 0x%x = %d x21' % (ir, old[0]))

    if args.dry_run:
        print('[dry-run] would set tab_ratio_ir -> %d; no file written' % args.ir)
        return

    orig = bytes(d)
    struct.pack_into('<21I', d, ir, *([args.ir] * 21))
    print('[patch] tab_ratio_ir %d -> %d' % (old[0], args.ir))

    if args.iso_cap is not None:
        hit = find_iso_prv_h(d, ir, uit_off + uit_size - 20)
        if hit > 0:
            struct.pack_into('<I', d, hit, args.iso_cap)
            print('[patch] iso_prv.h @0x%x 12800 -> %d' % (hit, args.iso_cap))
        else:
            print('[warn] iso_prv.h (12800,100,...) not found near AE struct; --iso-cap skipped')

    # fix checksums: partition first, whole-file CRC last (it covers the partition)
    new_uit = ntk_cksum16(d, uit_off, uit_size, uit_ic)
    struct.pack_into('<H', d, uit_off + uit_ic, new_uit)
    new_file = ntk_cksum16(d, 0, len(d), FILE_CRC_OFF)
    struct.pack_into('<I', d, FILE_CRC_OFF, new_file)
    print('[cksum] uITRON 0x%04x->0x%04x   file CRC 0x%04x->0x%04x'
          % (uit_stored, new_uit, file_stored, new_file))

    out = args.output or (os.path.splitext(args.input)[0] + '_patched.bin')
    open(out, 'wb').write(d)

    ok_uit, ok_file = check(d, uit_off, uit_size, uit_ic)
    assert len(d) == len(orig), 'file size changed!'
    diffs = sum(1 for i in range(len(orig)) if orig[i] != d[i])
    print('[verify] uITRON checksum: %s   file CRC: %s'
          % ('OK' if ok_uit else 'MISMATCH', 'OK' if ok_file else 'MISMATCH'))
    print('[done] wrote %s  (bytes changed: %d)' % (out, diffs))
    if not (ok_uit and ok_file):
        sys.exit('[error] verification FAILED - do NOT flash this file')


if __name__ == '__main__':
    main()
