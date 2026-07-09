#!/usr/bin/env python3
"""
patch_ae.py - Fix night (IR) over-exposure on Suntek / Novatek (NA51023 / NT96670) trail cameras.

It lowers the AE night-luminance target ``tab_ratio_ir`` inside the firmware's uITRON partition and
re-computes the Novatek ``NVTPACK_FW_HDR2`` checksums (uITRON partition checksum + whole-file CRC) so
the patched image boots and flashes normally.

Verified on HC-960Ultra-li (build FWHC940A / 20260326). Designed to generalise to HC-940/950Ultra,
newer builds, and other image sensors, because it:

* auto-detects the main uITRON partition from the partition table,
* self-tests the checksum algorithm against the original stored values (aborts if it cannot),
* locates ``tab_ratio_ir`` by structure (a monotonic ``tab_ratio_mov`` ramp whose following table is
  flat, followed later by the ``over_exposure`` block), not by a hard-coded offset.

Resilience: the tool validates its input, refuses ambiguous matches, verifies the result **in memory
before writing**, writes **atomically**, and re-checks the file **round-trip** from disk. It never
overwrites the input and never leaves a half-written output.

USE AT YOUR OWN RISK. Modifying/flashing firmware can brick the camera. Keep the original firmware.

Examples
--------
    python3 patch_ae.py FWHC940A.bin                    # tab_ratio_ir -> 55, writes *_patched.bin
    python3 patch_ae.py FW.bin -o out.bin --ir 45       # custom output and value (lower = darker)
    python3 patch_ae.py FW.bin --iso-cap 3200           # also cap iso_prv.h (e.g. 12800 -> 3200)
    python3 patch_ae.py FW.bin --dry-run                # analyse + locate only, write nothing
    python3 patch_ae.py FW.bin --verify-only            # only check a file's checksums
    python3 patch_ae.py FW.bin --uit-off 0x1878 --uit-size 7240660   # manual uITRON override
    python3 patch_ae.py FW.bin --ir-offset 0x6cb628     # manual tab_ratio_ir override
    python3 patch_ae.py FW.bin --force                  # proceed past non-fatal safety stops

License: MIT
"""

from __future__ import annotations

import argparse
import os
import struct
import sys
from array import array
from typing import List, Optional, Tuple

__version__ = "1.1.0"

# --- NVTPACK_FW_HDR2 layout (stable across HC-940/950/960; re-verified at runtime by self-test) ---
HDR2_VERSION_OFF = 0x10           # dword == HDR2_VERSION for a valid container
HDR2_VERSION = 0x16071515
PART_COUNT_OFF = 0x18            # dword: number of partitions
PART_TABLE_OFF = 0x88           # array of {id, offset, size} uint32 triples
FILE_CRC_OFF = 0x24            # whole-file 16-bit CRC (stored in the low word of a dword)

UIT_CKSUM_CANDIDATES = (0x6E, 0x16E, 0x26E, 0x36E, 0x46E)
UIT_LOAD_BASE = 0x02700000
UIT_LOAD_MASK = 0xFFF00000
MAGIC = b"\x55\xaa"

N = 21                            # AE tuning tables are 21 x uint32
TAB = N * 4                       # 0x54 bytes per table
MOV_TO_IR = 2 * TAB              # tab_ratio_mov -> tab_ratio_ir (skip mov[21] + photo[21]) = 0xA8
OE_SEARCH_WINDOW = 0x400          # how far after tab_ratio_ir to look for the over_exposure block

MIN_FW_SIZE = 0x100000            # 1 MiB - anything smaller is not a full camera firmware
RATIO_MIN, RATIO_MAX = 1, 255     # plausible tab_ratio_* value range
ISO_MIN, ISO_MAX = 100, 204800    # plausible ISO ceiling range


class PatchError(Exception):
    """A controlled, user-facing failure (prints as '[error] ...', non-zero exit)."""


def log(tag: str, msg: str) -> None:
    print("[%s] %s" % (tag, msg))


# --------------------------------------------------------------------------------------------------
# checksum
# --------------------------------------------------------------------------------------------------
def ntk_cksum16(buf: bytes, off: int, length: int, ignore_off: int) -> int:
    """Novatek position-weighted 16-bit two's-complement checksum
    (equivalent to NTKFWinfo's ``MemCheck_CalcCheckSum16Bit``)."""
    n = length // 2
    if off < 0 or off + n * 2 > len(buf) or not (0 <= ignore_off < n * 2):
        raise PatchError("checksum range out of bounds (off=0x%x len=%d)" % (off, length))
    a = array("h")                                 # signed 16-bit little-endian words
    a.frombytes(bytes(buf[off:off + n * 2]))
    a[ignore_off // 2] = 0                          # zero the word that stores the checksum itself
    s = (sum(a) + (n - 1) * n // 2) & 0xFFFF        # sum of words + triangular number n*(n-1)/2
    return ((~s & 0xFFFF) + 1) & 0xFFFF             # two's complement (negate)


# --------------------------------------------------------------------------------------------------
# structural predicates on 21 x uint32 tables (bounds-safe)
# --------------------------------------------------------------------------------------------------
def _table21(buf: bytes, o: int) -> Optional[Tuple[int, ...]]:
    if o < 0 or o + TAB > len(buf):
        return None
    return struct.unpack_from("<%dI" % N, buf, o)


def _is_flat(buf: bytes, o: int) -> bool:
    a = _table21(buf, o)
    return a is not None and len(set(a)) == 1


def _flat_in(buf: bytes, o: int, lo: int, hi: int) -> bool:
    """Flat 21x table whose (single) value is a plausible AE ratio in [lo, hi]."""
    a = _table21(buf, o)
    return a is not None and len(set(a)) == 1 and lo <= a[0] <= hi


def _is_ramp(buf: bytes, o: int) -> bool:
    """Monotonic non-decreasing and NOT flat, with plausible values."""
    a = _table21(buf, o)
    if a is None or a[0] == a[-1] or not all(0 <= x <= 4096 for x in a):
        return False
    return all(a[i] <= a[i + 1] for i in range(N - 1))


# --------------------------------------------------------------------------------------------------
# firmware container
# --------------------------------------------------------------------------------------------------
class NovatekFW:
    """A mutable NVTPACK_FW_HDR2 firmware image with the operations this tool needs."""

    def __init__(self, data: bytearray) -> None:
        self.d = data

    # -- basic accessors (bounds-checked) ----------------------------------------------------------
    def u32(self, o: int) -> int:
        if o < 0 or o + 4 > len(self.d):
            raise PatchError("read past end of file at 0x%x" % o)
        return struct.unpack_from("<I", self.d, o)[0]

    def u16(self, o: int) -> int:
        if o < 0 or o + 2 > len(self.d):
            raise PatchError("read past end of file at 0x%x" % o)
        return struct.unpack_from("<H", self.d, o)[0]

    def set_u16(self, o: int, v: int) -> None:
        struct.pack_into("<H", self.d, o, v & 0xFFFF)

    def set_u32(self, o: int, v: int) -> None:
        struct.pack_into("<I", self.d, o, v & 0xFFFFFFFF)

    def write_table21(self, o: int, value: int) -> None:
        struct.pack_into("<%dI" % N, self.d, o, *([value] * N))

    def is_container(self) -> bool:
        return len(self.d) >= HDR2_VERSION_OFF + 4 and self.u32(HDR2_VERSION_OFF) == HDR2_VERSION

    # -- partition table ---------------------------------------------------------------------------
    def partitions(self) -> List[Tuple[int, int, int]]:
        count = self.u32(PART_COUNT_OFF) if len(self.d) >= PART_COUNT_OFF + 4 else 0
        if not (0 < count < 32):
            count = 16
        parts, o = [], PART_TABLE_OFF
        for _ in range(count):
            if o + 12 > len(self.d):
                break
            pid, off, size = struct.unpack_from("<III", self.d, o)
            if pid > 64 or off >= len(self.d) or size == 0 or off + size > len(self.d):
                break
            parts.append((pid, off, size))
            o += 12
        return parts

    def find_uitron(self) -> Tuple[Optional[Tuple[int, int]], bool]:
        """Return ((offset, size), is_fallback). Main uITRON = partition whose first dword is a load
        address ~0x027004xx and that carries the '55 aa' magic; fallback = any large NT96670 image
        with the magic."""
        fallback = None
        for _pid, off, size in self.partitions():
            first = struct.unpack_from("<I", self.d, off)[0]
            has_magic = self.d.find(MAGIC, off, off + 0x90) > 0
            if (first & UIT_LOAD_MASK) == UIT_LOAD_BASE and has_magic:
                return (off, size), False
            if fallback is None and has_magic and size > MIN_FW_SIZE and b"NT96670" in self.d[off:off + 0x80]:
                fallback = (off, size)
        return fallback, True

    # -- checksums ---------------------------------------------------------------------------------
    def file_crc(self) -> int:
        return ntk_cksum16(self.d, 0, len(self.d), FILE_CRC_OFF)

    def file_crc_stored(self) -> int:
        return self.u32(FILE_CRC_OFF) & 0xFFFF

    def partition_crc(self, off: int, size: int, ic: int) -> int:
        return ntk_cksum16(self.d, off, size, ic)

    def detect_uit_cksum_off(self, uit_off: int, uit_size: int) -> Optional[Tuple[int, int]]:
        """Return (ignoreCRCoffset, stored) whose stored checksum our algorithm reproduces."""
        if uit_off < 0 or uit_off + uit_size > len(self.d) or uit_size < TAB:
            raise PatchError("uITRON range invalid (off=0x%x size=%d)" % (uit_off, uit_size))
        candidates = list(UIT_CKSUM_CANDIDATES)
        m = self.d.find(MAGIC, uit_off, uit_off + 0x400)
        if m > 0:
            candidates.append((m + 2) - uit_off)
        for ic in candidates:
            if uit_off + ic + 2 > len(self.d) or self.d[uit_off + ic - 2:uit_off + ic] != MAGIC:
                continue
            stored = self.u16(uit_off + ic)
            if self.partition_crc(uit_off, uit_size, ic) == stored:
                return ic, stored
        return None

    # -- parameter location ------------------------------------------------------------------------
    def _over_exposure_follows(self, ir_off: int, end: int) -> bool:
        """Confirm the over_exposure block is nearby: two consecutive ramps (tab_thr_mov, tab_thr_ir)
        within a short window after tab_ratio_ir."""
        hi = min(ir_off + OE_SEARCH_WINDOW, end - 2 * TAB)
        o = ir_off + TAB
        while o <= hi:
            if _is_ramp(self.d, o) and _is_ramp(self.d, o + TAB):
                return True
            o += 4
        return False

    def find_tab_ratio_ir(self, off: int, size: int) -> List[int]:
        """Return ALL plausible tab_ratio_ir offsets. Anchor: tab_ratio_mov = ramp whose next table
        is flat (excludes over_exposure.tab_thr_mov); then tab_ratio_ir = mov + 0xA8; then require
        the over_exposure block to follow. Normally exactly one match."""
        end = min(off + size, len(self.d))
        out, o = [], off
        while o + MOV_TO_IR + TAB <= end:
            if (_is_ramp(self.d, o)
                    and _flat_in(self.d, o + TAB, RATIO_MIN, RATIO_MAX)
                    and _flat_in(self.d, o + MOV_TO_IR, RATIO_MIN, RATIO_MAX)):
                ir = o + MOV_TO_IR
                if self._over_exposure_follows(ir, end):
                    out.append(ir)
            o += 4
        return out

    def find_iso_prv_h(self, ir_off: int, limit: int) -> int:
        """iso_prv = {iso_prv.h, iso_prv.l(=100)}. Search forward from tab_ratio_ir for {H, 100}
        with H a plausible ISO ceiling. Best-effort. Returns offset of iso_prv.h, or -1."""
        limit = min(limit, len(self.d))
        o = ir_off
        while o + 8 <= limit:
            h, l = struct.unpack_from("<2I", self.d, o)
            if l == 100 and 400 <= h <= ISO_MAX and h % 100 == 0 and h != 100:
                return o
            o += 4
        return -1

    # -- edits -------------------------------------------------------------------------------------
    def fix_checksums(self, uit_off: int, uit_size: int, uit_ic: int) -> Tuple[int, int]:
        """Partition checksum first, then the whole-file CRC (which covers it)."""
        new_uit = self.partition_crc(uit_off, uit_size, uit_ic)
        self.set_u16(uit_off + uit_ic, new_uit)
        new_file = self.file_crc()
        self.set_u32(FILE_CRC_OFF, new_file)
        return new_uit, new_file

    def verify(self, uit_off: int, uit_size: int, uit_ic: int) -> Tuple[bool, bool]:
        ok_uit = self.u16(uit_off + uit_ic) == self.partition_crc(uit_off, uit_size, uit_ic)
        ok_file = self.file_crc_stored() == self.file_crc()
        return ok_uit, ok_file


# --------------------------------------------------------------------------------------------------
# I/O helpers (resilient)
# --------------------------------------------------------------------------------------------------
def read_firmware(path: str) -> bytearray:
    if not os.path.isfile(path):
        raise PatchError("input file not found: %s" % path)
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError as e:
        raise PatchError("cannot read %s: %s" % (path, e))
    if len(data) < max(MIN_FW_SIZE, PART_TABLE_OFF + 12):
        raise PatchError("file too small to be a firmware image (%d bytes)" % len(data))
    if len(data) % 2:
        log("warn", "file has an odd byte length; the last byte is ignored by the 16-bit checksum")
    return bytearray(data)


def atomic_write(path: str, data: bytes) -> None:
    """Write to a temporary file then atomically replace, so a crash never leaves a partial output."""
    tmp = path + ".tmp"
    try:
        with open(tmp, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except OSError as e:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass
        raise PatchError("cannot write %s: %s" % (path, e))


def same_path(a: str, b: str) -> bool:
    try:
        return os.path.exists(a) and os.path.exists(b) and os.path.samefile(a, b)
    except OSError:
        return os.path.abspath(a) == os.path.abspath(b)


# --------------------------------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------------------------------
def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Patch Novatek trail-cam night over-exposure (tab_ratio_ir).")
    ap.add_argument("--version", action="version", version="%(prog)s " + __version__)
    ap.add_argument("input", help="original firmware .bin")
    ap.add_argument("-o", "--output", help="output file (default: <input>_patched.bin)")
    ap.add_argument("--ir", type=int, default=55, help="new tab_ratio_ir value (default 55; lower = darker night)")
    ap.add_argument("--iso-cap", type=int, default=None, help="set iso_prv.h to this (e.g. 3200); original often 12800")
    ap.add_argument("--uit-off", type=lambda x: int(x, 0), default=None, help="manual uITRON start offset (hex ok)")
    ap.add_argument("--uit-size", type=lambda x: int(x, 0), default=None, help="manual uITRON size")
    ap.add_argument("--ir-offset", type=lambda x: int(x, 0), default=None, help="manual tab_ratio_ir file offset")
    ap.add_argument("--dry-run", action="store_true", help="analyse and locate only; write nothing")
    ap.add_argument("--verify-only", action="store_true", help="only verify the checksums of the input file")
    ap.add_argument("--all", action="store_true", help="patch ALL tab_ratio_ir tables when several are found")
    ap.add_argument("--force", action="store_true", help="proceed past non-fatal safety stops")
    return ap.parse_args(argv)


def resolve_uitron(fw: NovatekFW, args: argparse.Namespace) -> Tuple[int, int]:
    if (args.uit_off is None) != (args.uit_size is None):
        raise PatchError("--uit-off and --uit-size must be given together")
    if args.uit_off is not None:
        if args.uit_off < 0 or args.uit_off + args.uit_size > len(fw.d):
            raise PatchError("manual uITRON range is outside the file")
        return args.uit_off, args.uit_size
    found, is_fallback = fw.find_uitron()
    if not found:
        raise PatchError("could not auto-detect uITRON; pass --uit-off/--uit-size (from NTKFWinfo -i)")
    if is_fallback:
        log("warn", "using a fallback uITRON match; verify offsets with NTKFWinfo -i if results look wrong")
    return found


def resolve_ir_targets(fw: NovatekFW, args: argparse.Namespace, uit_off: int, uit_size: int) -> List[int]:
    """Return the tab_ratio_ir offsets to patch. Default: the first (active) candidate; --all patches
    every candidate; --ir-offset selects one explicitly."""
    if args.ir_offset is not None:
        o = args.ir_offset
        if not _is_flat(fw.d, o):
            raise PatchError("--ir-offset 0x%x is not a flat 21x uint32 table" % o)
        return [o]
    cands = fw.find_tab_ratio_ir(uit_off, uit_size)
    if not cands:
        raise PatchError("could not locate tab_ratio_ir; read `ae aetdump 0` over UART and pass --ir-offset")
    if len(cands) == 1:
        return cands
    listing = ", ".join("0x%x (=%d)" % (c, fw.u32(c)) for c in cands)
    if args.all:
        log("i", "patching all %d tab_ratio_ir tables: %s" % (len(cands), listing))
        return cands
    log("warn", "%d tab_ratio_ir candidates: %s" % (len(cands), listing))
    log("warn", "patching only the first (active config); use --all for all, or --ir-offset to choose")
    return [cands[0]]


def do_patch(fw: NovatekFW, args: argparse.Namespace) -> int:
    if not fw.is_container():
        msg = "NVTPACK_FW_HDR2 marker (0x16071515) not found at 0x10 - wrong file?"
        if not args.force:
            raise PatchError(msg + " (use --force to try anyway with --uit-off/--uit-size)")
        log("warn", msg + " continuing due to --force")

    uit_off, uit_size = resolve_uitron(fw, args)
    log("i", "uITRON partition: off=0x%x size=%d (0x%x)" % (uit_off, uit_size, uit_size))

    detected = fw.detect_uit_cksum_off(uit_off, uit_size)
    if detected is None:
        raise PatchError("checksum self-test failed for every candidate offset - unknown container variant")
    uit_ic, uit_stored = detected
    file_stored, file_calc = fw.file_crc_stored(), fw.file_crc()
    log("ok", "self-test: uITRON cksum @+0x%x=0x%04x reproduced; file CRC @0x24=0x%04x calc=0x%04x"
        % (uit_ic, uit_stored, file_stored, file_calc))
    if file_stored != file_calc:
        if not args.force:
            raise PatchError("original file CRC mismatch - input may already be modified (use --force)")
        log("warn", "original file CRC mismatch; continuing due to --force")

    if args.verify_only:
        ok_uit, ok_file = fw.verify(uit_off, uit_size, uit_ic)
        log("verify", "uITRON checksum: %s   file CRC: %s"
            % ("OK" if ok_uit else "MISMATCH", "OK" if ok_file else "MISMATCH"))
        return 0 if (ok_uit and ok_file) else 1

    # value sanity
    if not (RATIO_MIN <= args.ir <= RATIO_MAX):
        raise PatchError("--ir %d out of range [%d..%d]" % (args.ir, RATIO_MIN, RATIO_MAX))
    if not (20 <= args.ir <= 110):
        log("warn", "--ir %d is outside the usual 20..110 range" % args.ir)
    if args.iso_cap is not None and not (ISO_MIN <= args.iso_cap <= ISO_MAX):
        raise PatchError("--iso-cap %d out of range [%d..%d]" % (args.iso_cap, ISO_MIN, ISO_MAX))

    targets = resolve_ir_targets(fw, args, uit_off, uit_size)
    for t in targets:
        val = fw.u32(t)
        log("i", "tab_ratio_ir @ file 0x%x = %d x21" % (t, val))
        if not (RATIO_MIN <= val <= RATIO_MAX) and not args.force:
            raise PatchError("value %d at 0x%x is not a plausible tab_ratio (use --ir-offset/--force)" % (val, t))

    if args.dry_run:
        log("dry-run", "would set tab_ratio_ir -> %d at %s; no file written"
            % (args.ir, ", ".join("0x%x" % t for t in targets)))
        return 0

    # decide output path early so we fail before editing if it collides with the input
    out = args.output or (os.path.splitext(args.input)[0] + "_patched.bin")
    if same_path(out, args.input):
        raise PatchError("refusing to overwrite the input file; choose a different --output")

    # ---- edit in memory ----
    original = bytes(fw.d)
    for t in targets:
        prev = fw.u32(t)
        fw.write_table21(t, args.ir)
        log("patch", "tab_ratio_ir @0x%x %d -> %d" % (t, prev, args.ir))

    if args.iso_cap is not None:
        hit = fw.find_iso_prv_h(targets[0], uit_off + uit_size - 8)
        if hit > 0:
            log("patch", "iso_prv.h @0x%x %d -> %d" % (hit, fw.u32(hit), args.iso_cap))
            fw.set_u32(hit, args.iso_cap)
        else:
            log("warn", "iso_prv.h ({H,100}) not found near AE struct; --iso-cap skipped")

    new_uit, new_file = fw.fix_checksums(uit_off, uit_size, uit_ic)
    log("cksum", "uITRON 0x%04x->0x%04x   file CRC 0x%04x->0x%04x"
        % (uit_stored, new_uit, file_stored, new_file))

    # ---- verify IN MEMORY before writing anything ----
    if len(fw.d) != len(original):
        raise PatchError("internal error: file size changed during patch")
    ok_uit, ok_file = fw.verify(uit_off, uit_size, uit_ic)
    if not (ok_uit and ok_file):
        raise PatchError("in-memory verification failed (uITRON=%s file=%s) - nothing written"
                         % (ok_uit, ok_file))

    # ---- atomic write, then round-trip verify from disk ----
    atomic_write(out, fw.d)
    rb = NovatekFW(read_firmware(out))
    ok_uit, ok_file = rb.verify(uit_off, uit_size, uit_ic)
    if not (ok_uit and ok_file):
        raise PatchError("round-trip verification of %s failed - do NOT flash it" % out)

    changed = sum(1 for i in range(len(original)) if original[i] != fw.d[i])
    log("verify", "on-disk uITRON checksum: OK   file CRC: OK")
    log("done", "wrote %s  (bytes changed: %d)" % (out, changed))
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    fw = NovatekFW(read_firmware(args.input))
    return do_patch(fw, args)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except PatchError as e:
        print("[error] %s" % e, file=sys.stderr)
        sys.exit(2)
    except KeyboardInterrupt:
        print("\n[error] interrupted", file=sys.stderr)
        sys.exit(130)
