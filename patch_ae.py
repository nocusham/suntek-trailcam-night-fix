#!/usr/bin/env python3
"""Patch night/IR auto-exposure tables in Suntek/Novatek trail-camera firmware.

Version 2 patches every detected camera runtime by default. This is important on
firmware that contains a normal/remote runtime and a separate low-power/PIR
runtime, each with its own ``tab_ratio_ir`` table.

The tool accepts a raw ``.bin`` image or a manufacturer ``.zip`` containing one
``.bin`` file. It validates the NVTPACK container, all detectable internal
partition checksums, the whole-file checksum, AE-table structure, and an exact
post-patch byte whitelist before writing an output file.

Tested firmware layouts:
  * HC-960Ultra, build 2026-03-26
  * HC-940Ultra, build 2025-04-23

Firmware flashing can permanently brick hardware. Keep the exact original
firmware and use this tool at your own risk. No manufacturer firmware is
included or downloaded by this program.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import struct
import sys
import tempfile
import zipfile
from array import array
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

__version__ = "2.0.0"

HDR2_VERSION_OFF = 0x10
HDR2_VERSION = 0x16071515
PART_TABLE_PTR_OFF = 0x14
PART_COUNT_OFF = 0x18
FILE_CHECKSUM_OFF = 0x24

MAGIC = b"\x55\xaa"
CHECKSUM_SCAN_LIMIT = 0x800
MIN_RUNTIME_SIZE = 0x100000
MIN_FW_SIZE = 0x100000

AE_ENTRIES = 21
AE_TABLE_BYTES = AE_ENTRIES * 4
MOV_TO_PHOTO = AE_TABLE_BYTES
MOV_TO_IR = 2 * AE_TABLE_BYTES
OVER_EXPOSURE_DELTA = 0x25C
OVER_EXPOSURE_SEARCH = 0x400
RATIO_MIN = 1
RATIO_MAX = 255
ISO_MIN = 100
ISO_MAX = 204800

ROLE_NORMAL = "normal/remote"
ROLE_PIR = "low-power/PIR"
ROLE_OTHER = "camera-runtime"

HC960_SHA256 = "b391abec2bdf6ab1d48e357c94e0f56bb9e2703899b647609acec3faa30150fa"
HC940_SHA256 = "9eb10ef5dd4057a891fb48a2b9cb9165e9ae3168a9b7e58aecc6299b90749c4a"

HC960_ORIGINAL = (110,) * AE_ENTRIES
HC960_AE55 = (55,) * AE_ENTRIES
HC940_ORIGINAL = (
    110, 110, 110, 110, 110, 110, 110,
    110, 110, 110, 110, 110, 110, 115,
    120, 125, 125, 125, 125, 125, 125,
)
HC940_AE58 = (
    58, 58, 58, 58, 58, 58, 58,
    58, 58, 58, 58, 58, 58, 61,
    63, 66, 66, 66, 66, 66, 66,
)


class PatchError(Exception):
    """Controlled user-facing failure."""


def log(tag: str, message: str) -> None:
    print(f"[{tag}] {message}")


def u16(data: bytes | bytearray, offset: int) -> int:
    if offset < 0 or offset + 2 > len(data):
        raise PatchError(f"16-bit read outside file at 0x{offset:x}")
    return struct.unpack_from("<H", data, offset)[0]


def u32(data: bytes | bytearray, offset: int) -> int:
    if offset < 0 or offset + 4 > len(data):
        raise PatchError(f"32-bit read outside file at 0x{offset:x}")
    return struct.unpack_from("<I", data, offset)[0]


def set_u16(data: bytearray, offset: int, value: int) -> None:
    if offset < 0 or offset + 2 > len(data):
        raise PatchError(f"16-bit write outside file at 0x{offset:x}")
    struct.pack_into("<H", data, offset, value & 0xFFFF)


def set_u32(data: bytearray, offset: int, value: int) -> None:
    if offset < 0 or offset + 4 > len(data):
        raise PatchError(f"32-bit write outside file at 0x{offset:x}")
    struct.pack_into("<I", data, offset, value & 0xFFFFFFFF)


def ntk_checksum16(
    data: bytes | bytearray,
    start: int,
    length: int,
    ignore_relative: int,
) -> int:
    """Novatek position-weighted signed-word two's-complement checksum.

    Words are explicitly interpreted as little-endian. An odd final byte is not
    part of the 16-bit checksum, matching the vendor/NTKFWinfo algorithm.
    """
    word_count = length // 2
    even_length = word_count * 2
    if start < 0 or start + even_length > len(data) or word_count == 0:
        raise PatchError(
            f"checksum range outside file: start=0x{start:x}, length={length}"
        )
    if (
        ignore_relative < 0
        or ignore_relative + 2 > even_length
        or ignore_relative % 2
    ):
        raise PatchError(
            f"invalid checksum-field offset +0x{ignore_relative:x} "
            f"for range length {length}"
        )

    words = array("h")
    if words.itemsize != 2:
        raise PatchError("Python array('h') is not 16-bit on this platform")
    words.frombytes(bytes(data[start : start + even_length]))
    if sys.byteorder != "little":
        words.byteswap()
    words[ignore_relative // 2] = 0
    total = (sum(words) + (word_count - 1) * word_count // 2) & 0xFFFF
    return (-total) & 0xFFFF


@dataclass(frozen=True)
class Partition:
    index: int
    pid: int
    offset: int
    size: int
    load_address: int
    checksum_relative: Optional[int] = None
    checksum_stored: Optional[int] = None
    checksum_calculated: Optional[int] = None

    @property
    def end(self) -> int:
        return self.offset + self.size

    @property
    def checksum_valid(self) -> Optional[bool]:
        if self.checksum_relative is None:
            return None
        return self.checksum_stored == self.checksum_calculated

    @property
    def role(self) -> str:
        if (self.load_address & 0xFFF00000) == 0x02700000:
            return ROLE_NORMAL
        if (self.load_address & 0xFFF00000) == 0x00400000:
            return ROLE_PIR
        return ROLE_OTHER


@dataclass(frozen=True)
class AECandidate:
    partition: Partition
    mov_offset: int
    photo_offset: int
    ir_offset: int
    mov_curve: Tuple[int, ...]
    photo_curve: Tuple[int, ...]
    ir_curve: Tuple[int, ...]
    over_exposure_delta: int


@dataclass(frozen=True)
class Profile:
    name: str
    model: str
    build: str
    sha256: str
    expected_curve: Tuple[int, ...]
    target_curve: Tuple[int, ...]
    note: str


PROFILES: Dict[str, Profile] = {
    "hc960-ae55": Profile(
        name="hc960-ae55",
        model="HC-960Ultra",
        build="2026-03-26",
        sha256=HC960_SHA256,
        expected_curve=HC960_ORIGINAL,
        target_curve=HC960_AE55,
        note="Flat 110 -> flat 55 in normal and PIR runtimes.",
    ),
    "hc940-ae58": Profile(
        name="hc940-ae58",
        model="HC-940Ultra",
        build="2025-04-23",
        sha256=HC940_SHA256,
        expected_curve=HC940_ORIGINAL,
        target_curve=HC940_AE58,
        note="Image-calibrated, curve-preserving 58..66 target in both runtimes.",
    ),
}
PROFILE_BY_SHA = {profile.sha256: profile for profile in PROFILES.values()}


@dataclass
class FirmwareSource:
    payload: bytes
    input_path: Path
    bin_member: str
    archive_entries: Optional[List[Tuple[zipfile.ZipInfo, bytes]]]

    @property
    def is_zip(self) -> bool:
        return self.archive_entries is not None


@dataclass
class ChecksumChange:
    label: str
    offset: int
    old: int
    new: int


@dataclass
class PatchTarget:
    candidate: AECandidate
    new_curve: Tuple[int, ...]


def read_source(path: Path) -> FirmwareSource:
    if not path.is_file():
        raise PatchError(f"input file not found: {path}")
    if path.suffix.lower() != ".zip":
        try:
            payload = path.read_bytes()
        except OSError as exc:
            raise PatchError(f"cannot read {path}: {exc}") from exc
        return FirmwareSource(payload, path, path.name, None)

    try:
        entries: List[Tuple[zipfile.ZipInfo, bytes]] = []
        with zipfile.ZipFile(path, "r") as archive:
            infos = archive.infolist()
            bin_infos = [
                info
                for info in infos
                if not info.is_dir() and info.filename.lower().endswith(".bin")
            ]
            if len(bin_infos) != 1:
                raise PatchError(
                    f"ZIP must contain exactly one .bin file; found {len(bin_infos)}"
                )
            for info in infos:
                entries.append((info, b"" if info.is_dir() else archive.read(info)))
            bin_info = bin_infos[0]
            payload = next(data for info, data in entries if info is bin_info)
            return FirmwareSource(payload, path, bin_info.filename, entries)
    except zipfile.BadZipFile as exc:
        raise PatchError(f"invalid ZIP file: {path}") from exc
    except OSError as exc:
        raise PatchError(f"cannot read ZIP {path}: {exc}") from exc


def validate_container_header(data: bytes | bytearray) -> Tuple[int, int]:
    if len(data) < MIN_FW_SIZE:
        raise PatchError(f"file is too small to be a full firmware image ({len(data)} bytes)")
    if u32(data, HDR2_VERSION_OFF) != HDR2_VERSION:
        raise PatchError(
            "NVTPACK_FW_HDR2 version marker 0x16071515 not found at file offset 0x10"
        )
    table_offset = u32(data, PART_TABLE_PTR_OFF)
    count = u32(data, PART_COUNT_OFF)
    if table_offset < 0x40 or table_offset % 4:
        raise PatchError(f"invalid partition-table pointer 0x{table_offset:x}")
    if count < 1 or count > 64:
        raise PatchError(f"implausible partition count: {count}")
    if table_offset + count * 12 > len(data):
        raise PatchError("partition table extends beyond the file")
    return table_offset, count


def detect_partition_checksum(
    data: bytes | bytearray, offset: int, size: int
) -> Tuple[Optional[int], Optional[int], Optional[int]]:
    """Detect a checksum field by its 55 aa marker and reproduce its value."""
    scan_end = min(offset + size, offset + CHECKSUM_SCAN_LIMIT)
    fields: List[Tuple[int, int, int]] = []
    cursor = offset
    while cursor + 4 <= scan_end:
        marker = data.find(MAGIC, cursor, scan_end)
        if marker < 0 or marker + 4 > offset + size:
            break
        relative = marker + 2 - offset
        if relative % 2 == 0:
            stored = u16(data, offset + relative)
            calculated = ntk_checksum16(data, offset, size, relative)
            fields.append((relative, stored, calculated))
        cursor = marker + 2

    matches = [field for field in fields if field[1] == field[2]]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        details = ", ".join(f"+0x{relative:x}" for relative, _, _ in matches)
        raise PatchError(
            f"ambiguous matching checksum fields in partition at 0x{offset:x}: {details}"
        )
    # A corrupt input no longer reproduces its stored value. A single marker is
    # still enough to identify and report the field as MISMATCH.
    if len(fields) == 1:
        return fields[0]
    if len(fields) > 1:
        details = ", ".join(f"+0x{relative:x}" for relative, _, _ in fields)
        raise PatchError(
            f"ambiguous checksum markers in partition at 0x{offset:x}: {details}"
        )
    return None, None, None


def parse_partitions(data: bytes | bytearray) -> List[Partition]:
    table_offset, count = validate_container_header(data)
    table_end = table_offset + count * 12
    raw: List[Partition] = []
    for index in range(count):
        record = table_offset + index * 12
        # NVTPACK_FW_HDR2 stores {offset, size, partition_id}.
        offset, size, pid = struct.unpack_from("<III", data, record)
        if size == 0:
            raise PatchError(f"partition #{index} (id {pid}) has zero size")
        if offset < table_end or offset + size > len(data):
            raise PatchError(
                f"partition #{index} (id {pid}) is outside the file: "
                f"off=0x{offset:x}, size=0x{size:x}"
            )
        load_address = u32(data, offset)
        relative, stored, calculated = detect_partition_checksum(data, offset, size)
        raw.append(
            Partition(
                index=index,
                pid=pid,
                offset=offset,
                size=size,
                load_address=load_address,
                checksum_relative=relative,
                checksum_stored=stored,
                checksum_calculated=calculated,
            )
        )

    by_offset = sorted(raw, key=lambda part: part.offset)
    for previous, current in zip(by_offset, by_offset[1:]):
        if previous.end > current.offset:
            raise PatchError(
                f"partitions overlap: id {previous.pid} ends at 0x{previous.end:x}, "
                f"id {current.pid} begins at 0x{current.offset:x}"
            )
    return raw


def verify_checksums(
    data: bytes | bytearray,
    partitions: Sequence[Partition],
    *,
    require_runtime_checksums: bool = True,
) -> Tuple[bool, List[str]]:
    messages: List[str] = []
    all_ok = True

    stored_file = u16(data, FILE_CHECKSUM_OFF)
    calculated_file = ntk_checksum16(data, 0, len(data), FILE_CHECKSUM_OFF)
    file_ok = stored_file == calculated_file
    all_ok &= file_ok
    messages.append(
        f"NVTPACK checksum @0x{FILE_CHECKSUM_OFF:x}: "
        f"stored=0x{stored_file:04x}, calculated=0x{calculated_file:04x} "
        f"({'OK' if file_ok else 'MISMATCH'})"
    )

    for part in partitions:
        if part.checksum_relative is None:
            messages.append(
                f"partition id {part.pid} @0x{part.offset:x}: "
                "no recognized internal checksum"
            )
            continue
        calculated = ntk_checksum16(
            data, part.offset, part.size, part.checksum_relative
        )
        stored = u16(data, part.offset + part.checksum_relative)
        ok = stored == calculated
        all_ok &= ok
        messages.append(
            f"partition id {part.pid} @0x{part.offset:x} "
            f"checksum +0x{part.checksum_relative:x}: "
            f"stored=0x{stored:04x}, calculated=0x{calculated:04x} "
            f"({'OK' if ok else 'MISMATCH'})"
        )

    if require_runtime_checksums:
        for part in partitions:
            if part.role in (ROLE_NORMAL, ROLE_PIR) and part.checksum_relative is None:
                all_ok = False
                messages.append(
                    f"partition id {part.pid} ({part.role}) has no verifiable "
                    "internal checksum (INVALID)"
                )
    return all_ok, messages


def table21(data: bytes | bytearray, offset: int) -> Optional[Tuple[int, ...]]:
    if offset < 0 or offset + AE_TABLE_BYTES > len(data):
        return None
    return struct.unpack_from(f"<{AE_ENTRIES}I", data, offset)


def is_ratio_curve(values: Optional[Tuple[int, ...]]) -> bool:
    return bool(
        values is not None
        and all(RATIO_MIN <= value <= RATIO_MAX for value in values)
        and all(values[index] <= values[index + 1] for index in range(AE_ENTRIES - 1))
    )


def is_threshold_ramp(data: bytes | bytearray, offset: int) -> bool:
    values = table21(data, offset)
    return bool(
        values is not None
        and values[0] != values[-1]
        and all(0 <= value <= 4096 for value in values)
        and all(values[index] <= values[index + 1] for index in range(AE_ENTRIES - 1))
    )


def over_exposure_delta(
    data: bytes | bytearray,
    ir_offset: int,
    partition_end: int,
    *,
    exact_only: bool,
) -> Optional[int]:
    exact = ir_offset + OVER_EXPOSURE_DELTA
    if (
        exact + 2 * AE_TABLE_BYTES <= partition_end
        and is_threshold_ramp(data, exact)
        and is_threshold_ramp(data, exact + AE_TABLE_BYTES)
    ):
        return OVER_EXPOSURE_DELTA
    if exact_only:
        return None

    last = min(ir_offset + OVER_EXPOSURE_SEARCH, partition_end - 2 * AE_TABLE_BYTES)
    for offset in range(ir_offset + AE_TABLE_BYTES, last + 1, 4):
        if is_threshold_ramp(data, offset) and is_threshold_ramp(
            data, offset + AE_TABLE_BYTES
        ):
            return offset - ir_offset
    return None


def candidate_at_ir_offset(
    data: bytes | bytearray,
    partition: Partition,
    ir_offset: int,
    *,
    exact_only: bool,
) -> Optional[AECandidate]:
    if ir_offset % 4:
        return None
    mov_offset = ir_offset - MOV_TO_IR
    photo_offset = mov_offset + MOV_TO_PHOTO
    if mov_offset < partition.offset or ir_offset + AE_TABLE_BYTES > partition.end:
        return None
    mov = table21(data, mov_offset)
    photo = table21(data, photo_offset)
    ir = table21(data, ir_offset)
    if not (is_ratio_curve(mov) and is_ratio_curve(photo) and is_ratio_curve(ir)):
        return None
    assert mov is not None and photo is not None and ir is not None
    if mov == photo == ir:
        return None
    delta = over_exposure_delta(
        data, ir_offset, partition.end, exact_only=exact_only
    )
    if delta is None:
        return None
    return AECandidate(
        partition=partition,
        mov_offset=mov_offset,
        photo_offset=photo_offset,
        ir_offset=ir_offset,
        mov_curve=mov,
        photo_curve=photo,
        ir_curve=ir,
        over_exposure_delta=delta,
    )


def find_ae_candidates_in_partition(
    data: bytes | bytearray,
    partition: Partition,
    *,
    exact_only: bool = True,
) -> List[AECandidate]:
    if partition.size < MIN_RUNTIME_SIZE or partition.checksum_relative is None:
        return []
    candidates: List[AECandidate] = []
    last_mov = partition.end - 3 * AE_TABLE_BYTES
    for mov_offset in range(partition.offset, last_mov + 1, 4):
        # Fast reject: first values of all three curves must be plausible.
        try:
            first_mov = u32(data, mov_offset)
            first_photo = u32(data, mov_offset + MOV_TO_PHOTO)
            first_ir = u32(data, mov_offset + MOV_TO_IR)
        except PatchError:
            break
        if not (
            RATIO_MIN <= first_mov <= RATIO_MAX
            and RATIO_MIN <= first_photo <= RATIO_MAX
            and RATIO_MIN <= first_ir <= RATIO_MAX
        ):
            continue
        candidate = candidate_at_ir_offset(
            data,
            partition,
            mov_offset + MOV_TO_IR,
            exact_only=exact_only,
        )
        if candidate is not None:
            candidates.append(candidate)
    return candidates


def find_all_ae_candidates(
    data: bytes | bytearray,
    partitions: Sequence[Partition],
) -> List[AECandidate]:
    exact: List[AECandidate] = []
    for part in partitions:
        hits = find_ae_candidates_in_partition(data, part, exact_only=True)
        if len(hits) > 1:
            details = ", ".join(f"0x{hit.ir_offset:x}" for hit in hits)
            raise PatchError(
                f"multiple exact AE structures in partition id {part.pid}: {details}; "
                "use --ir-offset to select explicitly"
            )
        exact.extend(hits)
    if exact:
        return exact

    # Conservative fallback for a related SDK layout where over_exposure moved.
    fallback: List[AECandidate] = []
    for part in partitions:
        hits = find_ae_candidates_in_partition(data, part, exact_only=False)
        # Reduce generic false positives by requiring the original night curve to
        # be pointwise at least as high as the photo curve.
        hits = [
            hit
            for hit in hits
            if all(
                hit.mov_curve[i] <= hit.photo_curve[i] <= hit.ir_curve[i]
                for i in range(AE_ENTRIES)
            )
            and hit.ir_curve != hit.photo_curve
        ]
        if len(hits) > 1:
            details = ", ".join(
                f"0x{hit.ir_offset:x}(OE+0x{hit.over_exposure_delta:x})"
                for hit in hits
            )
            raise PatchError(
                f"ambiguous fallback AE structures in partition id {part.pid}: "
                f"{details}; use --ir-offset"
            )
        fallback.extend(hits)
    return fallback


def partition_containing(
    partitions: Sequence[Partition], offset: int, length: int
) -> Partition:
    hits = [
        part
        for part in partitions
        if part.offset <= offset and offset + length <= part.end
    ]
    if len(hits) != 1:
        raise PatchError(
            f"offset range 0x{offset:x}..0x{offset + length:x} is not inside "
            "exactly one partition"
        )
    return hits[0]


def resolve_manual_candidates(
    data: bytes | bytearray,
    partitions: Sequence[Partition],
    offsets: Sequence[int],
) -> List[AECandidate]:
    candidates: List[AECandidate] = []
    for offset in offsets:
        partition = partition_containing(partitions, offset, AE_TABLE_BYTES)
        candidate = candidate_at_ir_offset(
            data, partition, offset, exact_only=False
        )
        if candidate is None:
            raise PatchError(
                f"--ir-offset 0x{offset:x} is not a structurally valid "
                "tab_ratio_ir table"
            )
        candidates.append(candidate)
    if len({candidate.ir_offset for candidate in candidates}) != len(candidates):
        raise PatchError("duplicate --ir-offset value")
    return candidates


def select_candidates(
    candidates: Sequence[AECandidate], selectors: Sequence[str]
) -> List[AECandidate]:
    if not candidates:
        raise PatchError(
            "no AE runtime found; use --ir-offset only after manually confirming "
            "the table and partition"
        )
    if not selectors or "all" in selectors:
        return list(candidates)

    selected: List[AECandidate] = []
    for candidate in candidates:
        wanted = False
        for selector in selectors:
            normalized = selector.lower()
            if normalized in ("normal", "remote") and candidate.partition.role == ROLE_NORMAL:
                wanted = True
            elif normalized in ("pir", "low-power", "lowpower") and candidate.partition.role == ROLE_PIR:
                wanted = True
            elif normalized.startswith("pid:"):
                try:
                    wanted |= candidate.partition.pid == int(normalized.split(":", 1)[1], 0)
                except ValueError as exc:
                    raise PatchError(f"invalid runtime selector: {selector}") from exc
            elif normalized.isdigit():
                wanted |= candidate.partition.pid == int(normalized)
            elif normalized not in (
                "normal",
                "remote",
                "pir",
                "low-power",
                "lowpower",
            ) and not normalized.startswith("pid:"):
                raise PatchError(f"unknown runtime selector: {selector}")
        if wanted:
            selected.append(candidate)
    if not selected:
        raise PatchError("--runtime selection matched no detected AE runtime")
    return selected


def parse_curve(text: str) -> Tuple[int, ...]:
    try:
        values = tuple(int(part.strip(), 0) for part in text.split(","))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("curve must contain comma-separated integers") from exc
    if len(values) != AE_ENTRIES:
        raise argparse.ArgumentTypeError(
            f"curve must contain exactly {AE_ENTRIES} integers (got {len(values)})"
        )
    if not all(RATIO_MIN <= value <= RATIO_MAX for value in values):
        raise argparse.ArgumentTypeError(
            f"curve values must be in {RATIO_MIN}..{RATIO_MAX}"
        )
    if not all(values[index] <= values[index + 1] for index in range(AE_ENTRIES - 1)):
        raise argparse.ArgumentTypeError("curve must be monotonic non-decreasing")
    return values


def scale_curve(values: Sequence[int], factor: float) -> Tuple[int, ...]:
    if not (0.05 <= factor <= 2.0):
        raise PatchError("--ir-scale must be in the range 0.05..2.0")
    scaled = tuple(
        max(RATIO_MIN, min(RATIO_MAX, int(value * factor + 0.5)))
        for value in values
    )
    if not all(scaled[index] <= scaled[index + 1] for index in range(AE_ENTRIES - 1)):
        raise PatchError("scaled curve is unexpectedly non-monotonic")
    return scaled


def resolve_profile(args: argparse.Namespace, digest: str) -> Optional[Profile]:
    if args.profile and args.profile != "auto":
        profile = PROFILES[args.profile]
        if digest != profile.sha256:
            raise PatchError(
                f"profile {profile.name} is restricted to the verified {profile.model} "
                f"{profile.build} image with SHA-256 {profile.sha256}; input is {digest}"
            )
        return profile
    if args.profile == "auto" or not any(
        value is not None for value in (args.ir, args.ir_scale, args.ir_values)
    ):
        profile = PROFILE_BY_SHA.get(digest)
        if profile is None:
            raise PatchError(
                "input SHA-256 has no automatic profile; choose --ir, --ir-scale, "
                "or --ir-values after reviewing the detected original curves"
            )
        return profile
    return None


def build_targets(
    args: argparse.Namespace,
    digest: str,
    candidates: Sequence[AECandidate],
) -> Tuple[List[PatchTarget], str, Optional[Profile]]:
    profile = resolve_profile(args, digest)
    targets: List[PatchTarget] = []
    if profile is not None:
        if len(candidates) != 2 or {c.partition.role for c in candidates} != {
            ROLE_NORMAL,
            ROLE_PIR,
        }:
            raise PatchError(
                f"profile {profile.name} expects exactly normal/remote and "
                "low-power/PIR runtimes"
            )
        for candidate in candidates:
            if candidate.ir_curve != profile.expected_curve:
                raise PatchError(
                    f"profile {profile.name} expected IR curve "
                    f"{format_curve(profile.expected_curve)}, but partition id "
                    f"{candidate.partition.pid} contains {format_curve(candidate.ir_curve)}"
                )
            targets.append(PatchTarget(candidate, profile.target_curve))
        return targets, f"profile:{profile.name}", profile

    if args.ir is not None:
        if not (RATIO_MIN <= args.ir <= RATIO_MAX):
            raise PatchError(f"--ir must be in {RATIO_MIN}..{RATIO_MAX}")
        curve = (args.ir,) * AE_ENTRIES
        targets = [PatchTarget(candidate, curve) for candidate in candidates]
        return targets, f"flat:{args.ir}", None

    if args.ir_values is not None:
        targets = [PatchTarget(candidate, args.ir_values) for candidate in candidates]
        return targets, "explicit-curve", None

    if args.ir_scale is not None:
        targets = [
            PatchTarget(candidate, scale_curve(candidate.ir_curve, args.ir_scale))
            for candidate in candidates
        ]
        return targets, f"scale:{args.ir_scale:g}", None

    raise PatchError("no patch mode selected")


def format_curve(values: Sequence[int]) -> str:
    if len(set(values)) == 1:
        return f"{values[0]} x{len(values)}"
    return ",".join(str(value) for value in values)


def write_curve(data: bytearray, offset: int, values: Sequence[int]) -> None:
    if len(values) != AE_ENTRIES:
        raise PatchError("internal error: AE curve has wrong length")
    struct.pack_into(f"<{AE_ENTRIES}I", data, offset, *values)


def contiguous_ranges(indices: Iterable[int]) -> List[Tuple[int, int]]:
    ordered = sorted(set(indices))
    if not ordered:
        return []
    ranges: List[Tuple[int, int]] = []
    start = previous = ordered[0]
    for index in ordered[1:]:
        if index != previous + 1:
            ranges.append((start, previous + 1))
            start = index
        previous = index
    ranges.append((start, previous + 1))
    return ranges


def allowed_indices_for_ranges(ranges: Iterable[Tuple[int, int]]) -> set[int]:
    allowed: set[int] = set()
    for start, length in ranges:
        allowed.update(range(start, start + length))
    return allowed


def patch_firmware(
    original: bytes,
    partitions: Sequence[Partition],
    targets: Sequence[PatchTarget],
    *,
    iso_cap: Optional[int],
    iso_offsets: Sequence[int],
) -> Tuple[bytes, List[ChecksumChange], List[Tuple[int, int]], Dict[int, Tuple[int, int]]]:
    data = bytearray(original)
    allowed_ranges: List[Tuple[int, int]] = []
    affected: Dict[int, Partition] = {}

    for target in targets:
        write_curve(data, target.candidate.ir_offset, target.new_curve)
        allowed_ranges.append((target.candidate.ir_offset, AE_TABLE_BYTES))
        affected[target.candidate.partition.index] = target.candidate.partition

    if (iso_cap is None) != (not iso_offsets):
        raise PatchError("--iso-cap and at least one --iso-offset must be used together")
    if iso_cap is not None:
        if not (ISO_MIN <= iso_cap <= ISO_MAX):
            raise PatchError(f"--iso-cap must be in {ISO_MIN}..{ISO_MAX}")
        for offset in iso_offsets:
            if offset % 4:
                raise PatchError(f"--iso-offset 0x{offset:x} is not 4-byte aligned")
            part = partition_containing(partitions, offset, 8)
            selected_partition_indices = {target.candidate.partition.index for target in targets}
            if part.index not in selected_partition_indices:
                raise PatchError(
                    f"--iso-offset 0x{offset:x} is not inside a selected AE runtime"
                )
            if u32(data, offset + 4) != 100:
                raise PatchError(
                    f"--iso-offset 0x{offset:x} is not followed by iso_prv.l=100"
                )
            old = u32(data, offset)
            if not (ISO_MIN <= old <= ISO_MAX):
                raise PatchError(
                    f"--iso-offset 0x{offset:x} contains implausible ISO value {old}"
                )
            set_u32(data, offset, iso_cap)
            allowed_ranges.append((offset, 4))
            affected[part.index] = part

    checksum_changes: List[ChecksumChange] = []
    partition_checksum_changes: Dict[int, Tuple[int, int]] = {}
    for part in affected.values():
        if part.checksum_relative is None:
            raise PatchError(
                f"changed partition id {part.pid} has no recognized internal checksum"
            )
        checksum_offset = part.offset + part.checksum_relative
        old = u16(data, checksum_offset)
        new = ntk_checksum16(data, part.offset, part.size, part.checksum_relative)
        set_u16(data, checksum_offset, new)
        allowed_ranges.append((checksum_offset, 2))
        checksum_changes.append(
            ChecksumChange(f"partition id {part.pid}", checksum_offset, old, new)
        )
        partition_checksum_changes[part.pid] = (old, new)

    old_file = u16(data, FILE_CHECKSUM_OFF)
    new_file = ntk_checksum16(data, 0, len(data), FILE_CHECKSUM_OFF)
    set_u16(data, FILE_CHECKSUM_OFF, new_file)
    allowed_ranges.append((FILE_CHECKSUM_OFF, 2))
    checksum_changes.append(
        ChecksumChange("NVTPACK", FILE_CHECKSUM_OFF, old_file, new_file)
    )

    if len(data) != len(original):
        raise PatchError("internal error: firmware size changed")

    changed_indices = {
        index for index, (before, after) in enumerate(zip(original, data)) if before != after
    }
    allowed_indices = allowed_indices_for_ranges(allowed_ranges)
    unexpected = changed_indices - allowed_indices
    if unexpected:
        preview = ", ".join(f"0x{index:x}" for index in sorted(unexpected)[:10])
        raise PatchError(f"unexpected byte changes outside whitelist: {preview}")

    output = bytes(data)
    output_partitions = parse_partitions(output)
    valid, messages = verify_checksums(output, output_partitions)
    if not valid:
        raise PatchError("post-patch checksum verification failed:\n" + "\n".join(messages))
    for target in targets:
        actual = table21(output, target.candidate.ir_offset)
        if actual != target.new_curve:
            raise PatchError(
                f"post-patch curve verification failed at 0x{target.candidate.ir_offset:x}"
            )
    if iso_cap is not None:
        for offset in iso_offsets:
            if u32(output, offset) != iso_cap:
                raise PatchError(f"post-patch ISO verification failed at 0x{offset:x}")

    return (
        output,
        checksum_changes,
        contiguous_ranges(changed_indices),
        partition_checksum_changes,
    )


def unique_temp_path(destination: Path) -> Tuple[int, Path]:
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(
        prefix=destination.name + ".",
        suffix=".tmp",
        dir=str(destination.parent),
    )
    return fd, Path(name)


def fsync_directory(path: Path) -> None:
    try:
        fd = os.open(str(path), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_write_bytes(destination: Path, payload: bytes, overwrite: bool) -> None:
    if destination.exists() and not overwrite:
        raise PatchError(f"refusing to overwrite existing output: {destination}")
    fd, temporary = unique_temp_path(destination)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
        fsync_directory(destination.parent)
    except Exception:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise


def make_zip_bytes(source: FirmwareSource, patched: bytes) -> bytes:
    import io

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as output:
        if source.archive_entries is None:
            output.writestr(
                source.bin_member,
                patched,
                compress_type=zipfile.ZIP_DEFLATED,
                compresslevel=9,
            )
        else:
            replaced = False
            for info, payload in source.archive_entries:
                clone = copy.copy(info)
                if info.is_dir():
                    output.writestr(clone, b"")
                elif info.filename == source.bin_member:
                    output.writestr(clone, patched, compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)
                    replaced = True
                else:
                    output.writestr(clone, payload)
            if not replaced:
                raise PatchError("internal error: source ZIP member disappeared")
    return buffer.getvalue()


def default_output_path(source: FirmwareSource) -> Path:
    suffix = ".zip" if source.is_zip else ".bin"
    return source.input_path.with_name(source.input_path.stem + "_patched" + suffix)


def output_payload(source: FirmwareSource, patched: bytes, destination: Path) -> bytes:
    if destination.suffix.lower() == ".zip":
        return make_zip_bytes(source, patched)
    if destination.suffix.lower() != ".bin":
        raise PatchError("output filename must end in .bin or .zip")
    return patched


def read_output_bin(destination: Path, expected_member: str) -> bytes:
    if destination.suffix.lower() != ".zip":
        return destination.read_bytes()
    with zipfile.ZipFile(destination, "r") as archive:
        bins = [
            info.filename
            for info in archive.infolist()
            if not info.is_dir() and info.filename.lower().endswith(".bin")
        ]
        if bins != [expected_member]:
            raise PatchError(
                f"round-trip ZIP contains unexpected BIN members: {bins}"
            )
        return archive.read(expected_member)


def build_manifest(
    *,
    source: FirmwareSource,
    input_sha: str,
    output_sha: str,
    output_path: Optional[Path],
    mode: str,
    profile: Optional[Profile],
    partitions: Sequence[Partition],
    targets: Sequence[PatchTarget],
    checksum_changes: Sequence[ChecksumChange],
    changed_ranges: Sequence[Tuple[int, int]],
    dry_run: bool,
) -> dict:
    return {
        "tool": {"name": "patch_ae.py", "version": __version__},
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "dry_run": dry_run,
        "input": {
            "path": str(source.input_path),
            "bin_member": source.bin_member,
            "size": len(source.payload),
            "sha256": input_sha,
        },
        "output": {
            "path": str(output_path) if output_path else None,
            "size": len(source.payload),
            "sha256": output_sha,
        },
        "patch_mode": mode,
        "profile": (
            {
                "name": profile.name,
                "model": profile.model,
                "build": profile.build,
                "note": profile.note,
            }
            if profile
            else None
        ),
        "targets": [
            {
                "partition_id": target.candidate.partition.pid,
                "role": target.candidate.partition.role,
                "load_address": f"0x{target.candidate.partition.load_address:08x}",
                "ir_offset": f"0x{target.candidate.ir_offset:08x}",
                "original_curve": list(target.candidate.ir_curve),
                "new_curve": list(target.new_curve),
            }
            for target in targets
        ],
        "checksums": [
            {
                "label": change.label,
                "offset": f"0x{change.offset:08x}",
                "old": f"0x{change.old:04x}",
                "new": f"0x{change.new:04x}",
            }
            for change in checksum_changes
        ],
        "changed_ranges": [
            {"start": f"0x{start:08x}", "end_exclusive": f"0x{end:08x}"}
            for start, end in changed_ranges
        ],
        "changed_byte_count": sum(end - start for start, end in changed_ranges),
        "verification": {
            "container_header": True,
            "all_detectable_partition_checksums": True,
            "outer_checksum": True,
            "target_curves": True,
            "changed_bytes_whitelisted": True,
            "round_trip": not dry_run,
        },
        "partitions": [
            {
                "id": part.pid,
                "offset": f"0x{part.offset:08x}",
                "size": part.size,
                "load_address": f"0x{part.load_address:08x}",
                "role": part.role,
                "checksum_relative": (
                    f"0x{part.checksum_relative:x}"
                    if part.checksum_relative is not None
                    else None
                ),
            }
            for part in partitions
        ],
    }


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Patch all detected Suntek/Novatek night-AE runtimes safely.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Automatic profiles are available only for exact verified firmware SHA-256 values.\n"
            "Unknown firmware requires --ir, --ir-scale, or --ir-values."
        ),
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("input", nargs="?", type=Path, help="manufacturer .bin or .zip")
    parser.add_argument("-o", "--output", type=Path, help="output .bin or .zip")
    parser.add_argument("--overwrite", action="store_true", help="allow replacing an existing output")

    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--profile",
        choices=["auto", *sorted(PROFILES)],
        help="verified firmware profile (default: auto by SHA-256)",
    )
    mode.add_argument("--ir", type=int, help="write one flat value to all 21 entries")
    mode.add_argument(
        "--ir-scale",
        type=float,
        help="multiply each existing IR-curve entry, preserving its shape",
    )
    mode.add_argument(
        "--ir-values",
        type=parse_curve,
        metavar="V1,...,V21",
        help="write an explicit monotonic 21-entry curve",
    )

    parser.add_argument(
        "--runtime",
        action="append",
        default=[],
        metavar="all|normal|pir|PID",
        help="select runtimes; repeatable (default: all detected runtimes)",
    )
    parser.add_argument(
        "--ir-offset",
        action="append",
        type=lambda text: int(text, 0),
        default=[],
        help="expert override: explicit tab_ratio_ir file offset; repeatable",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="deprecated compatibility alias; all runtimes are already the default",
    )

    parser.add_argument(
        "--iso-cap",
        type=int,
        help="expert option: new iso_prv.h value; requires --iso-offset",
    )
    parser.add_argument(
        "--iso-offset",
        action="append",
        type=lambda text: int(text, 0),
        default=[],
        help="expert option: verified iso_prv.h file offset; repeatable",
    )

    parser.add_argument("--dry-run", action="store_true", help="scan and validate without writing firmware")
    parser.add_argument("--scan", action="store_true", help="locate runtimes and curves without selecting a patch")
    parser.add_argument(
        "--verify-only",
        action="store_true",
        help="verify outer and all detectable internal checksums only",
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        help="write a JSON patch/verification manifest",
    )
    parser.add_argument(
        "--list-profiles",
        action="store_true",
        help="print verified profiles and exit",
    )
    return parser.parse_args(argv)


def print_profiles() -> None:
    for profile in PROFILES.values():
        print(
            f"{profile.name}: {profile.model}, build {profile.build}, "
            f"SHA-256 {profile.sha256}\n"
            f"  original: {format_curve(profile.expected_curve)}\n"
            f"  target  : {format_curve(profile.target_curve)}"
        )


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    if args.list_profiles:
        print_profiles()
        return 0
    if args.input is None:
        raise PatchError("an input .bin or .zip is required")
    if args.all:
        log("warn", "--all is deprecated; version 2 patches all detected runtimes by default")
    if args.verify_only and any(
        value is not None for value in (args.ir, args.ir_scale, args.ir_values, args.profile)
    ):
        raise PatchError("patch-mode options cannot be combined with --verify-only")
    if args.ir_offset and args.runtime:
        raise PatchError("use either --ir-offset or --runtime selection, not both")
    if (args.iso_cap is None) != (not args.iso_offset):
        raise PatchError("--iso-cap and at least one --iso-offset must be used together")

    source = read_source(args.input)
    original = source.payload
    input_sha = hashlib.sha256(original).hexdigest()
    partitions = parse_partitions(original)
    valid, checksum_messages = verify_checksums(original, partitions)
    for message in checksum_messages:
        log("verify", message)
    if not valid:
        raise PatchError("input checksum verification failed; use a pristine firmware image")

    if args.verify_only:
        log("done", f"all verifiable checksums are valid; SHA-256 {input_sha}")
        return 0

    if args.ir_offset:
        candidates = resolve_manual_candidates(original, partitions, args.ir_offset)
    else:
        candidates = find_all_ae_candidates(original, partitions)
        candidates = select_candidates(candidates, args.runtime)

    for candidate in sorted(candidates, key=lambda item: item.ir_offset):
        log(
            "scan",
            f"partition id {candidate.partition.pid} ({candidate.partition.role}), "
            f"load=0x{candidate.partition.load_address:08x}, "
            f"tab_ratio_ir=0x{candidate.ir_offset:08x}, "
            f"curve={format_curve(candidate.ir_curve)}, "
            f"over_exposure=IR+0x{candidate.over_exposure_delta:x}",
        )

    if args.scan:
        log("done", f"scan complete; input SHA-256 {input_sha}")
        return 0

    targets, mode, profile = build_targets(args, input_sha, candidates)
    for target in targets:
        log(
            "plan",
            f"0x{target.candidate.ir_offset:08x}: "
            f"{format_curve(target.candidate.ir_curve)} -> {format_curve(target.new_curve)}",
        )

    patched, checksum_changes, changed_ranges, _ = patch_firmware(
        original,
        partitions,
        targets,
        iso_cap=args.iso_cap,
        iso_offsets=args.iso_offset,
    )
    output_sha = hashlib.sha256(patched).hexdigest()
    for change in checksum_changes:
        log(
            "cksum",
            f"{change.label} @0x{change.offset:x}: "
            f"0x{change.old:04x} -> 0x{change.new:04x}",
        )
    log(
        "verify",
        f"in-memory checksums, target curves and byte whitelist OK; "
        f"changed bytes={sum(end - start for start, end in changed_ranges)}",
    )

    destination = args.output or default_output_path(source)
    manifest = build_manifest(
        source=source,
        input_sha=input_sha,
        output_sha=output_sha,
        output_path=None if args.dry_run else destination,
        mode=mode,
        profile=profile,
        partitions=partitions,
        targets=targets,
        checksum_changes=checksum_changes,
        changed_ranges=changed_ranges,
        dry_run=args.dry_run,
    )

    if args.dry_run:
        log("dry-run", f"output SHA-256 would be {output_sha}; no firmware written")
    else:
        if destination.resolve() == source.input_path.resolve():
            raise PatchError("refusing to overwrite the input path")
        payload = output_payload(source, patched, destination)
        atomic_write_bytes(destination, payload, args.overwrite)
        try:
            round_trip = read_output_bin(destination, source.bin_member)
            if round_trip != patched:
                raise PatchError("round-trip output bytes differ from in-memory firmware")
            round_partitions = parse_partitions(round_trip)
            round_valid, round_messages = verify_checksums(round_trip, round_partitions)
            if not round_valid:
                raise PatchError(
                    "round-trip checksum verification failed:\n" + "\n".join(round_messages)
                )
            for target in targets:
                if table21(round_trip, target.candidate.ir_offset) != target.new_curve:
                    raise PatchError(
                        f"round-trip curve mismatch at 0x{target.candidate.ir_offset:x}"
                    )
        except Exception:
            try:
                destination.unlink()
            except FileNotFoundError:
                pass
            raise
        log("done", f"wrote {destination}")
        log("done", f"output SHA-256 {output_sha}")

    if args.manifest:
        manifest_payload = (json.dumps(manifest, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
        atomic_write_bytes(args.manifest, manifest_payload, args.overwrite)
        log("done", f"wrote manifest {args.manifest}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except PatchError as exc:
        print(f"[error] {exc}", file=sys.stderr)
        raise SystemExit(2)
    except KeyboardInterrupt:
        print("\n[error] interrupted", file=sys.stderr)
        raise SystemExit(130)
