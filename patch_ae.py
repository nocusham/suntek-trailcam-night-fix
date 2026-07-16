#!/usr/bin/env python3
"""Patch night/IR auto-exposure tables in Suntek/Novatek trail-camera firmware.

Version 3 adds fail-closed support levels for frequently changing Suntek firmware:
exact verified builds, probable model-family matches, structural matches, and
unsupported images. Unknown builds can be inspected and compared safely; writing
an experimental patch requires an explicit expert workflow and a bound scan manifest.

The tool accepts a raw ``.bin`` image or a manufacturer ``.zip`` containing one
``.bin`` file. It validates the NVTPACK container, all detectable internal
partition checksums, the whole-file checksum, AE-table structure, and an exact
post-patch byte whitelist before writing an output file.

Tested firmware layouts:
  * HC-960Ultra, build 2026-03-26
  * HC-940Ultra, build 2025-04-23
  * HC-950Ultra / 950XFUltra, builds 2024-08-08 and 2026-05-27
    (dual camera; recognition only, no exposure change is recommended by default)

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
import re
import struct
import sys
import tempfile
import zipfile
from array import array
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

__version__ = "3.0.0"

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

SENSOR_SINGLE = "single"
SENSOR_DAY = "day"
SENSOR_NIGHT = "night"
SENSOR_UNKNOWN = "unknown"

SUPPORT_VERIFIED = "verified"
SUPPORT_FAMILY = "family-match"
SUPPORT_STRUCTURAL = "structural-match"
SUPPORT_UNSUPPORTED = "unsupported"
SUPPORT_NOT_CLASSIFIED = "not-classified"

HC960_SHA256 = "b391abec2bdf6ab1d48e357c94e0f56bb9e2703899b647609acec3faa30150fa"
HC940_SHA256 = "9eb10ef5dd4057a891fb48a2b9cb9165e9ae3168a9b7e58aecc6299b90749c4a"
HC950_20240808_SHA256 = "e4db261f9228af5793d5952b45f9b6e9e41b2a50e264ac8971e5145d8cc19370"
HC950_20260527_SHA256 = "a6caf6be7e1a77dfe434ae78b959390b190f2e3b6e9b6e0cb5c8b29b2e6edf61"

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
    sensor_key: str = SENSOR_UNKNOWN
    sensor_model: Optional[str] = None
    sensor_role: str = SENSOR_UNKNOWN


@dataclass(frozen=True)
class CandidateIdentity:
    partition_id: int
    runtime_role: str
    ir_offset: int
    sensor_key: str
    sensor_model: Optional[str]
    sensor_role: str
    expected_curve: Tuple[int, ...]
    context_after_sha256: Optional[str] = None
    over_exposure_sha256: Optional[str] = None


@dataclass(frozen=True)
class FirmwareLayout:
    name: str
    model: str
    build: str
    sha256: str
    camera_design: str
    candidates: Tuple[CandidateIdentity, ...]
    required_strings: Tuple[bytes, ...] = ()
    default_patch_sensor: Optional[str] = None
    note: str = ""
    family: str = ""
    source: str = "builtin"
    trusted: bool = True


@dataclass(frozen=True)
class Profile:
    name: str
    model: str
    build: str
    sha256: str
    expected_curve: Tuple[int, ...]
    target_curve: Tuple[int, ...]
    note: str
    source: str = "builtin"
    trusted: bool = True


@dataclass(frozen=True)
class FamilyRule:
    key: str
    model: str
    build_prefix: bytes
    camera_design: str
    required_markers: Tuple[bytes, ...]
    optional_markers: Tuple[bytes, ...]
    candidates_per_runtime: int
    default_patch_sensor: Optional[str]


@dataclass(frozen=True)
class SupportAssessment:
    level: str
    probable_family: Optional[str]
    probable_model: Optional[str]
    confidence: str
    score: int
    maximum_score: int
    reasons: Tuple[str, ...]
    warnings: Tuple[str, ...]
    build_strings: Tuple[str, ...]
    sensor_markers: Tuple[str, ...]
    automatic_patch_allowed: bool
    layout_source: Optional[str] = None


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


FIRMWARE_LAYOUTS: Dict[str, FirmwareLayout] = {
    HC960_SHA256: FirmwareLayout(
        name="hc960-single-camera",
        family="hc960",
        model="HC-960Ultra",
        build="2026-03-26",
        sha256=HC960_SHA256,
        camera_design="single camera module",
        candidates=(
            CandidateIdentity(3, ROLE_NORMAL, 0x006CB628, SENSOR_SINGLE, None, SENSOR_SINGLE, HC960_ORIGINAL),
            CandidateIdentity(9, ROLE_PIR, 0x0182DFC0, SENSOR_SINGLE, None, SENSOR_SINGLE, HC960_ORIGINAL),
        ),
        default_patch_sensor="all",
        note="One AE configuration per runtime.",
    ),
    HC940_SHA256: FirmwareLayout(
        name="hc940-single-camera",
        family="hc940",
        model="HC-940Ultra",
        build="2025-04-23",
        sha256=HC940_SHA256,
        camera_design="single camera module",
        candidates=(
            CandidateIdentity(3, ROLE_NORMAL, 0x006CB8CC, SENSOR_SINGLE, None, SENSOR_SINGLE, HC940_ORIGINAL),
            CandidateIdentity(9, ROLE_PIR, 0x0188EDE4, SENSOR_SINGLE, None, SENSOR_SINGLE, HC940_ORIGINAL),
        ),
        default_patch_sensor="all",
        note="One AE configuration per runtime.",
    ),
    HC950_20240808_SHA256: FirmwareLayout(
        name="hc950-dual-camera-2024",
        family="hc950",
        model="HC-950Ultra / 950XFUltra",
        build="2024-08-08",
        sha256=HC950_20240808_SHA256,
        camera_design="dual camera modules: IMX258M day + SC223AP night",
        candidates=(
            CandidateIdentity(3, ROLE_NORMAL, 0x006C2C60, "imx258m", "IMX258M", SENSOR_DAY, HC960_ORIGINAL),
            CandidateIdentity(3, ROLE_NORMAL, 0x006C3904, "sc223ap", "SC223AP", SENSOR_NIGHT, HC960_ORIGINAL),
            CandidateIdentity(9, ROLE_PIR, 0x01893924, "imx258m", "IMX258M", SENSOR_DAY, HC960_ORIGINAL),
            CandidateIdentity(9, ROLE_PIR, 0x018945EC, "sc223ap", "SC223AP", SENSOR_NIGHT, HC960_ORIGINAL),
        ),
        required_strings=(
            b"CMOS_IMX258M",
            b"CMOS_SC223AP",
            b"AE_PARAM_IMX258_EVB",
            b"AE_PARAM_SC223A_EVB",
            b"950XFUltra_20240808",
        ),
        default_patch_sensor=SENSOR_NIGHT,
        note=(
            "Factory night exposure is reported as good. The firmware is recognized "
            "and all four AE configurations can be selected, but there is no automatic "
            "HC-950 exposure target."
        ),
    ),
    HC950_20260527_SHA256: FirmwareLayout(
        name="hc950-dual-camera-2026",
        family="hc950",
        model="HC-950Ultra / 950XFUltra",
        build="2026-05-27",
        sha256=HC950_20260527_SHA256,
        camera_design="dual camera modules: IMX258M day + SC223AP night",
        candidates=(
            CandidateIdentity(3, ROLE_NORMAL, 0x006C2DE4, "imx258m", "IMX258M", SENSOR_DAY, HC940_ORIGINAL),
            CandidateIdentity(3, ROLE_NORMAL, 0x006C3A88, "sc223ap", "SC223AP", SENSOR_NIGHT, HC960_ORIGINAL),
            CandidateIdentity(9, ROLE_PIR, 0x01834710, "imx258m", "IMX258M", SENSOR_DAY, HC960_ORIGINAL),
            CandidateIdentity(9, ROLE_PIR, 0x018353D8, "sc223ap", "SC223AP", SENSOR_NIGHT, HC960_ORIGINAL),
        ),
        required_strings=(
            b"CMOS_IMX258M",
            b"CMOS_SC223AP",
            b"AE_PARAM_IMX258_EVB",
            b"AE_PARAM_SC223A_EVB",
            b"950XFUltra_20260527",
        ),
        default_patch_sensor=SENSOR_NIGHT,
        note=(
            "Factory night exposure is reported as good. This newer build is fully "
            "recognized, including its mixed original day-camera curves, but there is "
            "still no automatic HC-950 exposure target."
        ),
    ),
}


FAMILY_RULES: Tuple[FamilyRule, ...] = (
    FamilyRule(
        key="hc950",
        model="HC-950Ultra / 950XFUltra",
        build_prefix=b"950XFUltra_",
        camera_design="dual camera modules: IMX258M day + SC223AP night",
        required_markers=(
            b"CMOS_IMX258M",
            b"CMOS_SC223AP",
            b"AE_PARAM_IMX258_EVB",
            b"AE_PARAM_SC223A_EVB",
        ),
        optional_markers=(b"IQ_PARAM_IMX258_EVB", b"IQ_PARAM_SC223A_EVB"),
        candidates_per_runtime=2,
        default_patch_sensor=SENSOR_NIGHT,
    ),
    FamilyRule(
        key="hc960",
        model="HC-960Ultra",
        build_prefix=b"960RPFUltra_",
        camera_design="single camera module with separate normal and PIR runtimes",
        required_markers=(b"HC960Ultra",),
        optional_markers=(),
        candidates_per_runtime=1,
        default_patch_sensor="all",
    ),
    FamilyRule(
        key="hc940",
        model="HC-940Ultra",
        build_prefix=b"940FUltra_",
        camera_design="single camera module with separate normal and PIR runtimes",
        required_markers=(),
        optional_markers=(),
        candidates_per_runtime=1,
        default_patch_sensor="all",
    ),
)

BUILD_RE = re.compile(rb"(?:940FUltra|950XFUltra|960RPFUltra)_[0-9]{8}")
COMMON_CONTEXT_AFTER_SHA256 = (
    "eb9bd792977a60f88ffbfa49697a768b2dfe0380d1dcfb3b992ab8ecd33a1d44"
)
COMMON_OVER_EXPOSURE_SHA256 = (
    "8d5d9268d1029fc2e4cb9521471b2cdcdfd227a262c836659fac381a273fac6c"
)


def _curve_from_json(value: Any, label: str) -> Tuple[int, ...]:
    if not isinstance(value, list) or len(value) != AE_ENTRIES:
        raise PatchError(f"{label} must be a JSON array with {AE_ENTRIES} integers")
    try:
        curve = tuple(int(item) for item in value)
    except (TypeError, ValueError) as exc:
        raise PatchError(f"{label} contains a non-integer value") from exc
    if not is_ratio_curve(curve):
        raise PatchError(f"{label} is not a plausible monotonic AE curve")
    return curve


def _candidate_identity_from_json(value: Mapping[str, Any], label: str) -> CandidateIdentity:
    try:
        raw_offset = value["ir_offset"]
        offset = int(raw_offset, 0) if isinstance(raw_offset, str) else int(raw_offset)
        return CandidateIdentity(
            partition_id=int(value["partition_id"]),
            runtime_role=str(value["runtime_role"]),
            ir_offset=offset,
            sensor_key=str(value["sensor_key"]),
            sensor_model=(str(value["sensor_model"]) if value.get("sensor_model") else None),
            sensor_role=str(value["sensor_role"]),
            expected_curve=_curve_from_json(value["expected_curve"], f"{label}.expected_curve"),
            context_after_sha256=(
                str(value["context_after_sha256"]).lower()
                if value.get("context_after_sha256")
                else None
            ),
            over_exposure_sha256=(
                str(value["over_exposure_sha256"]).lower()
                if value.get("over_exposure_sha256")
                else None
            ),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise PatchError(f"invalid candidate in {label}: {exc}") from exc


def load_profile_file(path: Path, *, trusted: bool) -> None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PatchError(f"cannot load profile file {path}: {exc}") from exc
    if payload.get("schema") != 1:
        raise PatchError(f"unsupported profile schema in {path}; expected 1")
    try:
        layout_data = payload["layout"]
        digest = str(layout_data["sha256"]).lower()
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise PatchError(f"invalid SHA-256 in {path}")
        layout = FirmwareLayout(
            name=str(layout_data["name"]),
            model=str(layout_data["model"]),
            build=str(layout_data["build"]),
            sha256=digest,
            camera_design=str(layout_data["camera_design"]),
            candidates=tuple(
                _candidate_identity_from_json(item, f"{path}:candidate[{index}]")
                for index, item in enumerate(layout_data["candidates"])
            ),
            required_strings=tuple(
                str(item).encode("ascii") for item in layout_data.get("required_strings", [])
            ),
            default_patch_sensor=layout_data.get("default_patch_sensor"),
            note=str(layout_data.get("note", "")),
            family=str(layout_data.get("family", "")),
            source=str(path),
            trusted=trusted,
        )
    except (KeyError, TypeError, UnicodeEncodeError) as exc:
        raise PatchError(f"invalid layout in profile file {path}: {exc}") from exc

    existing = FIRMWARE_LAYOUTS.get(digest)
    if existing is not None:
        if existing.trusted and existing.source != "builtin":
            raise PatchError(
                f"profile {path} may not override trusted bundled layout "
                f"{existing.name} from {existing.source}"
            )
        if existing.name != layout.name and existing.source != "builtin":
            raise PatchError(
                f"profile collision for SHA-256 {digest}: {existing.source} vs {path}"
            )
    FIRMWARE_LAYOUTS[digest] = layout

    automatic = payload.get("automatic_patch")
    if automatic is not None:
        try:
            profile = Profile(
                name=str(automatic["name"]),
                model=layout.model,
                build=layout.build,
                sha256=digest,
                expected_curve=_curve_from_json(
                    automatic["expected_curve"], f"{path}:automatic.expected_curve"
                ),
                target_curve=_curve_from_json(
                    automatic["target_curve"], f"{path}:automatic.target_curve"
                ),
                note=str(automatic.get("note", "")),
                source=str(path),
                trusted=trusted,
            )
        except (KeyError, TypeError) as exc:
            raise PatchError(f"invalid automatic patch in {path}: {exc}") from exc
        existing_profile = PROFILES.get(profile.name)
        if (
            existing_profile is not None
            and existing_profile.trusted
            and existing_profile.source != "builtin"
        ):
            raise PatchError(
                f"profile {path} may not override trusted bundled automatic profile "
                f"{profile.name} from {existing_profile.source}"
            )
        PROFILES[profile.name] = profile


def load_profile_registry(
    extra_directories: Sequence[Path], *, trust_external: bool
) -> None:
    """Load bundled official profiles and optional user-provided profile directories.

    The directory next to this script is trusted as release content. Extra
    directories are recognition-only unless --trust-external-profiles is used.
    """
    bundled = Path(__file__).resolve().with_name("profiles")
    directories: List[Tuple[Path, bool]] = []
    if bundled.is_dir():
        directories.append((bundled, True))
    directories.extend((path, trust_external) for path in extra_directories)
    seen: set[Path] = set()
    for directory, trusted in directories:
        resolved = directory.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        if not resolved.is_dir():
            raise PatchError(f"profile directory not found: {directory}")
        for path in sorted(resolved.glob("*.json")):
            load_profile_file(path, trusted=trusted)
    global PROFILE_BY_SHA
    PROFILE_BY_SHA = {
        profile.sha256: profile
        for profile in PROFILES.values()
        if profile.trusted
    }


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
    """Find every structurally valid AE configuration in every runtime.

    Multiple exact matches in one runtime are valid on dual-camera firmware. The
    identity/selection layer decides whether they are a verified sensor layout or
    an unknown ambiguous layout.
    """
    exact: List[AECandidate] = []
    for part in partitions:
        exact.extend(find_ae_candidates_in_partition(data, part, exact_only=True))
    if exact:
        return sorted(exact, key=lambda candidate: candidate.ir_offset)

    # Conservative fallback for a related SDK layout where over_exposure moved.
    fallback: List[AECandidate] = []
    for part in partitions:
        hits = find_ae_candidates_in_partition(data, part, exact_only=False)
        hits = [
            hit
            for hit in hits
            if all(
                hit.mov_curve[i] <= hit.photo_curve[i] <= hit.ir_curve[i]
                for i in range(AE_ENTRIES)
            )
            and hit.ir_curve != hit.photo_curve
        ]
        fallback.extend(hits)
    if len(fallback) > 16:
        raise PatchError(
            f"fallback AE search produced {len(fallback)} candidates; use "
            "--ir-offset only after manual analysis"
        )
    return sorted(fallback, key=lambda candidate: candidate.ir_offset)


def candidates_from_layout(
    data: bytes | bytearray,
    partitions: Sequence[Partition],
    layout: FirmwareLayout,
) -> List[AECandidate]:
    """Resolve a verified layout directly without scanning entire runtime images."""
    by_pid: Dict[int, List[Partition]] = {}
    for partition in partitions:
        by_pid.setdefault(partition.pid, []).append(partition)
    candidates: List[AECandidate] = []
    for identity in layout.candidates:
        matching = [
            partition
            for partition in by_pid.get(identity.partition_id, [])
            if partition.role == identity.runtime_role
        ]
        if len(matching) != 1:
            raise PatchError(
                f"{layout.model} expected exactly one partition id "
                f"{identity.partition_id} with role {identity.runtime_role}"
            )
        candidate = candidate_at_ir_offset(
            data, matching[0], identity.ir_offset, exact_only=True
        )
        if candidate is None:
            raise PatchError(
                f"{layout.model} expected an AE structure at "
                f"0x{identity.ir_offset:x} in partition id {identity.partition_id}"
            )
        candidates.append(candidate)
    return sorted(candidates, key=lambda candidate: candidate.ir_offset)


def candidate_context_hashes(
    data: bytes | bytearray, candidate: AECandidate
) -> Tuple[str, str]:
    after_start = candidate.ir_offset + AE_TABLE_BYTES
    after_end = after_start + 64
    over_start = candidate.ir_offset + candidate.over_exposure_delta
    over_end = over_start + 2 * AE_TABLE_BYTES
    if after_end > candidate.partition.end or over_end > candidate.partition.end:
        raise PatchError(
            f"AE context outside partition at 0x{candidate.ir_offset:x}"
        )
    return (
        hashlib.sha256(bytes(data[after_start:after_end])).hexdigest(),
        hashlib.sha256(bytes(data[over_start:over_end])).hexdigest(),
    )


def extract_build_strings(data: bytes | bytearray) -> Tuple[str, ...]:
    return tuple(
        sorted({match.group(0).decode("ascii") for match in BUILD_RE.finditer(bytes(data))})
    )


def detect_sensor_markers(data: bytes | bytearray) -> Tuple[str, ...]:
    markers = (
        b"CMOS_IMX258M",
        b"CMOS_SC223AP",
        b"AE_PARAM_IMX258_EVB",
        b"AE_PARAM_SC223A_EVB",
        b"IQ_PARAM_IMX258_EVB",
        b"IQ_PARAM_SC223A_EVB",
    )
    return tuple(marker.decode("ascii") for marker in markers if marker in data)


def identify_candidates(
    data: bytes | bytearray,
    digest: str,
    candidates: Sequence[AECandidate],
    *,
    require_complete_layout: bool,
) -> Tuple[List[AECandidate], Optional[FirmwareLayout]]:
    """Attach exact verified identities or conservative unknown labels.

    Unknown firmware is never classified as a single-camera design merely
    because only one table was found. Family classification is handled later by
    assess_support(), which requires independent model markers and runtime/count
    consistency.
    """
    layout = FIRMWARE_LAYOUTS.get(digest)
    if layout is not None:
        for marker in layout.required_strings:
            if marker not in data:
                raise PatchError(
                    f"recognized {layout.model} hash but required marker "
                    f"{marker.decode('ascii', 'replace')!r} is missing"
                )
        identity_by_key = {
            (identity.partition_id, identity.ir_offset): identity
            for identity in layout.candidates
        }
        candidate_by_key = {
            (candidate.partition.pid, candidate.ir_offset): candidate
            for candidate in candidates
        }
        if require_complete_layout and set(candidate_by_key) != set(identity_by_key):
            expected = ", ".join(
                f"pid {pid}:0x{offset:x}" for pid, offset in sorted(identity_by_key)
            )
            actual = ", ".join(
                f"pid {pid}:0x{offset:x}" for pid, offset in sorted(candidate_by_key)
            ) or "none"
            raise PatchError(
                f"{layout.model} layout mismatch; expected [{expected}], found [{actual}]"
            )
        identified: List[AECandidate] = []
        for candidate in candidates:
            identity = identity_by_key.get((candidate.partition.pid, candidate.ir_offset))
            if identity is None:
                raise PatchError(
                    f"unexpected AE table in recognized {layout.model} image: "
                    f"pid {candidate.partition.pid} offset 0x{candidate.ir_offset:x}"
                )
            if candidate.partition.role != identity.runtime_role:
                raise PatchError(
                    f"runtime-role mismatch at 0x{candidate.ir_offset:x}: "
                    f"{candidate.partition.role} != {identity.runtime_role}"
                )
            if candidate.ir_curve != identity.expected_curve:
                raise PatchError(
                    f"original curve mismatch for {layout.model} {identity.sensor_key} "
                    f"at 0x{candidate.ir_offset:x}: "
                    f"{format_curve(candidate.ir_curve)} != "
                    f"{format_curve(identity.expected_curve)}"
                )
            context_after, over_hash = candidate_context_hashes(data, candidate)
            if (
                identity.context_after_sha256
                and context_after != identity.context_after_sha256
            ):
                raise PatchError(
                    f"AE context fingerprint mismatch at 0x{candidate.ir_offset:x}"
                )
            if (
                identity.over_exposure_sha256
                and over_hash != identity.over_exposure_sha256
            ):
                raise PatchError(
                    f"over_exposure fingerprint mismatch at 0x{candidate.ir_offset:x}"
                )
            identified.append(
                replace(
                    candidate,
                    sensor_key=identity.sensor_key,
                    sensor_model=identity.sensor_model,
                    sensor_role=identity.sensor_role,
                )
            )
        return sorted(identified, key=lambda candidate: candidate.ir_offset), layout

    groups: Dict[int, List[AECandidate]] = {}
    for candidate in candidates:
        groups.setdefault(candidate.partition.index, []).append(candidate)
    identified: List[AECandidate] = []
    for group in groups.values():
        ordered = sorted(group, key=lambda candidate: candidate.ir_offset)
        for index, candidate in enumerate(ordered, 1):
            suffix = str(index) if len(ordered) > 1 else ""
            identified.append(
                replace(
                    candidate,
                    sensor_key=f"unidentified{('-' + suffix) if suffix else ''}",
                    sensor_model=None,
                    sensor_role=SENSOR_UNKNOWN,
                )
            )
    return sorted(identified, key=lambda candidate: candidate.ir_offset), None


def _runtime_candidate_groups(
    candidates: Sequence[AECandidate],
) -> Dict[str, List[AECandidate]]:
    groups: Dict[str, List[AECandidate]] = {ROLE_NORMAL: [], ROLE_PIR: []}
    for candidate in candidates:
        if candidate.partition.role in groups:
            groups[candidate.partition.role].append(candidate)
    for role in groups:
        groups[role].sort(key=lambda candidate: candidate.ir_offset)
    return groups


def _family_score(
    data: bytes | bytearray,
    candidates: Sequence[AECandidate],
    rule: FamilyRule,
) -> Tuple[int, int, List[str], List[str]]:
    score = 0
    maximum = 100
    reasons: List[str] = []
    warnings: List[str] = []
    builds = extract_build_strings(data)
    matching_builds = [
        build for build in builds if build.encode("ascii").startswith(rule.build_prefix)
    ]
    if matching_builds:
        score += 25
        reasons.append(f"build marker starts with {rule.build_prefix.decode('ascii')}")
        if len(matching_builds) > 1:
            warnings.append(
                "multiple build markers for the same family were found: "
                + ", ".join(matching_builds)
            )
    else:
        warnings.append("family build-prefix marker is missing")

    if rule.required_markers:
        present = sum(marker in data for marker in rule.required_markers)
        marker_score = round(25 * present / len(rule.required_markers))
        score += marker_score
        reasons.append(
            f"{present}/{len(rule.required_markers)} required family markers present"
        )
        if present != len(rule.required_markers):
            warnings.append("not all required family markers are present")
    else:
        score += 25
        reasons.append("family has no additional required sensor markers")

    groups = _runtime_candidate_groups(candidates)
    runtime_present = sum(bool(groups[role]) for role in (ROLE_NORMAL, ROLE_PIR))
    score += 10 * runtime_present
    reasons.append(f"{runtime_present}/2 camera runtimes contain AE candidates")
    if runtime_present != 2:
        warnings.append("normal and PIR runtimes were not both found")

    counts_ok = all(
        len(groups[role]) == rule.candidates_per_runtime
        for role in (ROLE_NORMAL, ROLE_PIR)
    )
    if counts_ok:
        score += 20
        reasons.append(
            f"candidate count matches {rule.candidates_per_runtime} per runtime"
        )
    else:
        warnings.append(
            "candidate count does not match the known family architecture"
        )

    context_matches = 0
    for candidate in candidates:
        after_hash, over_hash = candidate_context_hashes(data, candidate)
        if (
            after_hash == COMMON_CONTEXT_AFTER_SHA256
            and over_hash == COMMON_OVER_EXPOSURE_SHA256
        ):
            context_matches += 1
    if candidates and context_matches == len(candidates):
        score += 10
        reasons.append("all AE candidates match the known relative SDK context")
    elif context_matches:
        score += 5
        warnings.append("only some AE candidates match the known SDK context")
    else:
        warnings.append("no known relative AE context fingerprint matched")
    return score, maximum, reasons, warnings


def _apply_family_identities(
    data: bytes | bytearray,
    candidates: Sequence[AECandidate],
    rule: FamilyRule,
) -> Tuple[List[AECandidate], FirmwareLayout]:
    groups = _runtime_candidate_groups(candidates)
    identified: List[AECandidate] = []
    identities: List[CandidateIdentity] = []
    if rule.key == "hc950":
        for role in (ROLE_NORMAL, ROLE_PIR):
            ordered = groups[role]
            if len(ordered) != 2:
                raise PatchError("cannot assign HC-950 sensor identities safely")
            sensor_data = (
                ("imx258m", "IMX258M", SENSOR_DAY),
                ("sc223ap", "SC223AP", SENSOR_NIGHT),
            )
            for candidate, (key, model, sensor_role) in zip(ordered, sensor_data):
                updated = replace(
                    candidate,
                    sensor_key=key,
                    sensor_model=model,
                    sensor_role=sensor_role,
                )
                identified.append(updated)
                after_hash, over_hash = candidate_context_hashes(data, candidate)
                identities.append(
                    CandidateIdentity(
                        candidate.partition.pid,
                        role,
                        candidate.ir_offset,
                        key,
                        model,
                        sensor_role,
                        candidate.ir_curve,
                        after_hash,
                        over_hash,
                    )
                )
    else:
        for role in (ROLE_NORMAL, ROLE_PIR):
            ordered = groups[role]
            if len(ordered) != 1:
                raise PatchError(f"cannot assign {rule.model} single-camera identity safely")
            candidate = ordered[0]
            updated = replace(
                candidate,
                sensor_key=SENSOR_SINGLE,
                sensor_model=None,
                sensor_role=SENSOR_SINGLE,
            )
            identified.append(updated)
            after_hash, over_hash = candidate_context_hashes(data, candidate)
            identities.append(
                CandidateIdentity(
                    candidate.partition.pid,
                    role,
                    candidate.ir_offset,
                    SENSOR_SINGLE,
                    None,
                    SENSOR_SINGLE,
                    candidate.ir_curve,
                    after_hash,
                    over_hash,
                )
            )
    builds = extract_build_strings(data)
    matching_builds = [
        value for value in builds if value.encode("ascii").startswith(rule.build_prefix)
    ]
    build = max(matching_builds).split("_", 1)[1] if matching_builds else "unknown"
    layout = FirmwareLayout(
        name=f"probable-{rule.key}-{build}",
        model=rule.model,
        build=build,
        sha256=hashlib.sha256(bytes(data)).hexdigest(),
        camera_design=rule.camera_design,
        candidates=tuple(identities),
        required_strings=(rule.build_prefix, *rule.required_markers),
        default_patch_sensor=rule.default_patch_sensor,
        note="Probable family match only; this build is not an exact verified profile.",
        family=rule.key,
        source="family-heuristic",
        trusted=False,
    )
    return sorted(identified, key=lambda candidate: candidate.ir_offset), layout


def assess_support(
    data: bytes | bytearray,
    digest: str,
    candidates: Sequence[AECandidate],
    exact_layout: Optional[FirmwareLayout],
) -> Tuple[SupportAssessment, List[AECandidate], Optional[FirmwareLayout]]:
    builds = extract_build_strings(data)
    markers = detect_sensor_markers(data)
    if exact_layout is not None and exact_layout.trusted:
        assessment = SupportAssessment(
            level=SUPPORT_VERIFIED,
            probable_family=exact_layout.family or None,
            probable_model=exact_layout.model,
            confidence="exact",
            score=100,
            maximum_score=100,
            reasons=(
                "exact BIN SHA-256 matches a trusted release profile",
                "partition/runtime/offset/original-curve validation passed",
            ),
            warnings=(),
            build_strings=builds,
            sensor_markers=markers,
            automatic_patch_allowed=digest in PROFILE_BY_SHA,
            layout_source=exact_layout.source,
        )
        return assessment, list(candidates), exact_layout

    if exact_layout is not None and not exact_layout.trusted:
        assessment = SupportAssessment(
            level=SUPPORT_FAMILY,
            probable_family=exact_layout.family or None,
            probable_model=exact_layout.model,
            confidence="external-profile-untrusted",
            score=95,
            maximum_score=100,
            reasons=("exact SHA-256 matches an external profile",),
            warnings=(
                "external profiles are recognition-only unless explicitly trusted",
            ),
            build_strings=builds,
            sensor_markers=markers,
            automatic_patch_allowed=False,
            layout_source=exact_layout.source,
        )
        return assessment, list(candidates), exact_layout

    scored: List[Tuple[int, FamilyRule, int, List[str], List[str]]] = []
    for rule in FAMILY_RULES:
        score, maximum, reasons, warnings = _family_score(data, candidates, rule)
        scored.append((score, rule, maximum, reasons, warnings))
    scored.sort(key=lambda item: item[0], reverse=True)
    best_score, best_rule, maximum, reasons, warnings = scored[0]
    second_score = scored[1][0] if len(scored) > 1 else 0
    best_groups = _runtime_candidate_groups(candidates)
    architecture_matches = all(
        len(best_groups[role]) == best_rule.candidates_per_runtime
        for role in (ROLE_NORMAL, ROLE_PIR)
    )
    prefix_present = best_rule.build_prefix in data
    if (
        best_score >= 75
        and best_score - second_score >= 15
        and prefix_present
        and architecture_matches
    ):
        identified, family_layout = _apply_family_identities(data, candidates, best_rule)
        confidence = "high" if best_score >= 90 else "medium"
        assessment = SupportAssessment(
            level=SUPPORT_FAMILY,
            probable_family=best_rule.key,
            probable_model=best_rule.model,
            confidence=confidence,
            score=best_score,
            maximum_score=maximum,
            reasons=tuple(reasons),
            warnings=tuple(warnings) + (
                "unknown SHA-256: automatic profiles and normal write mode are disabled",
            ),
            build_strings=builds,
            sensor_markers=markers,
            automatic_patch_allowed=False,
            layout_source="family-heuristic",
        )
        return assessment, identified, family_layout

    runtime_roles = {candidate.partition.role for candidate in candidates}
    if candidates and runtime_roles & {ROLE_NORMAL, ROLE_PIR}:
        assessment = SupportAssessment(
            level=SUPPORT_STRUCTURAL,
            probable_family=None,
            probable_model=None,
            confidence="structural-only",
            score=max(0, best_score),
            maximum_score=maximum,
            reasons=(
                f"found {len(candidates)} structurally plausible AE configuration(s)",
                "container and all required checksums are valid",
            ),
            warnings=(
                "model and sensor identities are not verified",
                "patching requires explicit offsets and the unverified expert workflow",
            ),
            build_strings=builds,
            sensor_markers=markers,
            automatic_patch_allowed=False,
            layout_source=None,
        )
        return assessment, list(candidates), None

    assessment = SupportAssessment(
        level=SUPPORT_UNSUPPORTED,
        probable_family=None,
        probable_model=None,
        confidence="none",
        score=max(0, best_score),
        maximum_score=maximum,
        reasons=("no complete supported AE runtime structure was found",),
        warnings=("firmware patching is disabled",),
        build_strings=builds,
        sensor_markers=markers,
        automatic_patch_allowed=False,
        layout_source=None,
    )
    return assessment, list(candidates), None


def print_support(assessment: SupportAssessment) -> None:
    model = assessment.probable_model or "unknown"
    log(
        "support",
        f"level={assessment.level}, model={model}, confidence={assessment.confidence}, "
        f"score={assessment.score}/{assessment.maximum_score}, "
        f"automatic_patch={'yes' if assessment.automatic_patch_allowed else 'no'}",
    )
    for reason in assessment.reasons:
        log("support", f"reason: {reason}")
    for warning in assessment.warnings:
        log("warn", warning)

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


def _runtime_matches(candidate: AECandidate, selector: str) -> bool:
    normalized = selector.lower()
    if normalized in ("normal", "remote"):
        return candidate.partition.role == ROLE_NORMAL
    if normalized in ("pir", "low-power", "lowpower"):
        return candidate.partition.role == ROLE_PIR
    if normalized.startswith("pid:"):
        try:
            return candidate.partition.pid == int(normalized.split(":", 1)[1], 0)
        except ValueError as exc:
            raise PatchError(f"invalid runtime selector: {selector}") from exc
    if normalized.isdigit():
        return candidate.partition.pid == int(normalized)
    if normalized == "all":
        return True
    raise PatchError(f"unknown runtime selector: {selector}")


def _sensor_matches(candidate: AECandidate, selector: str) -> bool:
    normalized = selector.lower().replace("_", "-")
    compact = normalized.replace("-", "")
    if normalized == "all":
        return True
    if normalized in (SENSOR_DAY, SENSOR_NIGHT, SENSOR_SINGLE):
        return candidate.sensor_role == normalized or candidate.sensor_key == normalized
    if compact in ("imx258", "imx258m"):
        return candidate.sensor_key == "imx258m"
    if compact in ("sc223a", "sc223ap"):
        return candidate.sensor_key == "sc223ap"
    return candidate.sensor_key.lower() == normalized


def select_candidates(
    candidates: Sequence[AECandidate],
    runtime_selectors: Sequence[str],
    sensor_selectors: Sequence[str],
    layout: Optional[FirmwareLayout],
) -> List[AECandidate]:
    if not candidates:
        raise PatchError(
            "no AE runtime found; use --ir-offset only after manually confirming "
            "the table and partition"
        )

    selected = list(candidates)
    if any(candidate.sensor_role == SENSOR_UNKNOWN for candidate in selected):
        raise PatchError(
            "multiple unidentified AE configurations exist in at least one runtime; "
            "use --ir-offset for every manually verified table"
        )

    if runtime_selectors and "all" not in [value.lower() for value in runtime_selectors]:
        selected = [
            candidate
            for candidate in selected
            if any(_runtime_matches(candidate, selector) for selector in runtime_selectors)
        ]
        if not selected:
            raise PatchError("--runtime selection matched no detected AE configuration")

    if sensor_selectors:
        selected = [
            candidate
            for candidate in selected
            if any(_sensor_matches(candidate, selector) for selector in sensor_selectors)
        ]
        if not selected:
            raise PatchError("--sensor selection matched no detected AE configuration")
    elif layout is not None and layout.default_patch_sensor not in (None, "all"):
        selected = [
            candidate
            for candidate in selected
            if _sensor_matches(candidate, layout.default_patch_sensor)
        ]

    return sorted(selected, key=lambda candidate: candidate.ir_offset)


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



@dataclass(frozen=True)
class ExpectedCurveRule:
    offset: Optional[int]
    curve: Tuple[int, ...]


def parse_expected_curve(text: str) -> ExpectedCurveRule:
    """Parse CURVE or OFFSET=CURVE for --expect-ir."""
    offset: Optional[int] = None
    curve_text = text
    if "=" in text:
        offset_text, curve_text = text.split("=", 1)
        try:
            offset = int(offset_text.strip(), 0)
        except ValueError as exc:
            raise argparse.ArgumentTypeError(
                "--expect-ir offset must be decimal or 0x-prefixed"
            ) from exc
    curve = parse_curve(curve_text)
    return ExpectedCurveRule(offset, curve)


def validate_expected_curves(
    candidates: Sequence[AECandidate], rules: Sequence[ExpectedCurveRule]
) -> None:
    if not rules:
        raise PatchError(
            "unverified firmware requires --expect-ir. Use either one 21-value "
            "curve for every selected target or OFFSET=V1,...,V21 per target"
        )
    global_rules = [rule for rule in rules if rule.offset is None]
    offset_rules = {rule.offset: rule for rule in rules if rule.offset is not None}
    if len(global_rules) > 1:
        raise PatchError("only one global --expect-ir curve may be supplied")
    unknown_offsets = set(offset_rules) - {candidate.ir_offset for candidate in candidates}
    if unknown_offsets:
        raise PatchError(
            "--expect-ir references unselected offsets: "
            + ", ".join(f"0x{offset:x}" for offset in sorted(unknown_offsets))
        )
    for candidate in candidates:
        rule = offset_rules.get(candidate.ir_offset)
        if rule is None and global_rules:
            rule = global_rules[0]
        if rule is None:
            raise PatchError(
                f"missing --expect-ir for selected offset 0x{candidate.ir_offset:x}"
            )
        if candidate.ir_curve != rule.curve:
            raise PatchError(
                f"expected original IR curve mismatch at 0x{candidate.ir_offset:x}: "
                f"found {format_curve(candidate.ir_curve)}, expected {format_curve(rule.curve)}"
            )


def read_json_file(path: Path, label: str) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PatchError(f"cannot read {label} {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise PatchError(f"{label} must contain a JSON object")
    return value


def validate_scan_manifest(
    path: Path,
    input_sha: str,
    selected: Sequence[AECandidate],
) -> None:
    manifest = read_json_file(path, "scan manifest")
    if manifest.get("manifest_type") not in ("analysis", "compatibility"):
        raise PatchError(
            "--accept-scan-manifest requires a manifest created by --scan or --compat-check"
        )
    if manifest.get("input", {}).get("sha256") != input_sha:
        raise PatchError("scan manifest SHA-256 does not match the input firmware")
    scanned: Dict[int, Tuple[int, ...]] = {}
    for item in manifest.get("candidates", []):
        try:
            offset = int(str(item["ir_offset"]), 0)
            curve = tuple(int(value) for value in item["ir_curve"])
        except (KeyError, TypeError, ValueError) as exc:
            raise PatchError("scan manifest contains an invalid candidate") from exc
        scanned[offset] = curve
    for candidate in selected:
        curve = scanned.get(candidate.ir_offset)
        if curve is None:
            raise PatchError(
                f"selected offset 0x{candidate.ir_offset:x} is absent from scan manifest"
            )
        if curve != candidate.ir_curve:
            raise PatchError(
                f"original curve at 0x{candidate.ir_offset:x} changed since scan manifest"
            )


def enforce_patch_policy(
    args: argparse.Namespace,
    assessment: SupportAssessment,
    input_sha: str,
    selected: Sequence[AECandidate],
) -> None:
    if assessment.level == SUPPORT_UNSUPPORTED:
        raise PatchError("unsupported firmware cannot be patched")
    if assessment.level == SUPPORT_VERIFIED:
        return
    if not args.allow_unverified:
        raise PatchError(
            f"support level is {assessment.level}; add --allow-unverified only after "
            "reviewing --compat-check and --scan output"
        )
    validate_expected_curves(selected, args.expect_ir)
    if not args.dry_run:
        if args.accept_scan_manifest is None:
            raise PatchError(
                "writing unverified firmware requires --accept-scan-manifest from a "
                "previous --scan --manifest run"
            )
        validate_scan_manifest(args.accept_scan_manifest, input_sha, selected)

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


def resolve_profile(
    args: argparse.Namespace,
    digest: str,
    layout: Optional[FirmwareLayout],
) -> Optional[Profile]:
    if args.profile and args.profile != "auto":
        profile = PROFILES.get(args.profile)
        if profile is None:
            raise PatchError(
                f"unknown profile {args.profile!r}; use --list-profiles"
            )
        if not profile.trusted:
            raise PatchError(
                f"profile {profile.name} came from an untrusted external directory; "
                "review it and use --trust-external-profiles to enable it"
            )
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
            if layout is not None:
                raise PatchError(
                    f"recognized {layout.model} build {layout.build}, but no automatic "
                    "exposure patch is recommended. Run --scan; to experiment, choose "
                    "--ir, --ir-scale, or --ir-values. On recognized HC-950 layouts the "
                    "verified default selection is the SC223AP night sensor."
                )
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
    layout: Optional[FirmwareLayout],
) -> Tuple[List[PatchTarget], str, Optional[Profile]]:
    profile = resolve_profile(args, digest, layout)
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


def default_output_path(source: FirmwareSource, *, unverified: bool = False) -> Path:
    suffix = ".zip" if source.is_zip else ".bin"
    marker = "_UNVERIFIED_PATCHED" if unverified else "_patched"
    return source.input_path.with_name(source.input_path.stem + marker + suffix)


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



def support_to_dict(assessment: SupportAssessment) -> dict:
    return {
        "level": assessment.level,
        "probable_family": assessment.probable_family,
        "probable_model": assessment.probable_model,
        "confidence": assessment.confidence,
        "score": assessment.score,
        "maximum_score": assessment.maximum_score,
        "reasons": list(assessment.reasons),
        "warnings": list(assessment.warnings),
        "build_strings": list(assessment.build_strings),
        "sensor_markers": list(assessment.sensor_markers),
        "automatic_patch_allowed": assessment.automatic_patch_allowed,
        "layout_source": assessment.layout_source,
    }


def candidate_to_dict(
    data: bytes | bytearray, candidate: AECandidate
) -> dict:
    after_hash, over_hash = candidate_context_hashes(data, candidate)
    return {
        "partition_id": candidate.partition.pid,
        "runtime_role": candidate.partition.role,
        "load_address": f"0x{candidate.partition.load_address:08x}",
        "mov_offset": f"0x{candidate.mov_offset:08x}",
        "photo_offset": f"0x{candidate.photo_offset:08x}",
        "ir_offset": f"0x{candidate.ir_offset:08x}",
        "sensor_key": candidate.sensor_key,
        "sensor_model": candidate.sensor_model,
        "sensor_role": candidate.sensor_role,
        "mov_curve": list(candidate.mov_curve),
        "photo_curve": list(candidate.photo_curve),
        "ir_curve": list(candidate.ir_curve),
        "over_exposure_delta": f"0x{candidate.over_exposure_delta:x}",
        "context_after_sha256": after_hash,
        "over_exposure_sha256": over_hash,
    }


def build_analysis_manifest(
    *,
    source: FirmwareSource,
    input_sha: str,
    partitions: Sequence[Partition],
    candidates: Sequence[AECandidate],
    assessment: SupportAssessment,
    mode: str,
) -> dict:
    return {
        "manifest_type": "compatibility" if mode == "compat-check" else "analysis",
        "schema": 1,
        "tool": {"name": "patch_ae.py", "version": __version__},
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "mode": mode,
        "input": {
            "path": str(source.input_path),
            "bin_member": source.bin_member,
            "size": len(source.payload),
            "sha256": input_sha,
        },
        "support": support_to_dict(assessment),
        "candidates": [candidate_to_dict(source.payload, item) for item in candidates],
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
                "checksum_valid": part.checksum_valid,
            }
            for part in partitions
        ],
    }


def export_layout_candidate(
    path: Path,
    source: FirmwareSource,
    input_sha: str,
    candidates: Sequence[AECandidate],
    assessment: SupportAssessment,
    overwrite: bool,
) -> None:
    family = assessment.probable_family or "unknown"
    build = assessment.build_strings[0] if assessment.build_strings else "unknown"
    payload = {
        "schema": 1,
        "status": "unverified",
        "generated_by": {"name": "patch_ae.py", "version": __version__},
        "support": support_to_dict(assessment),
        "layout": {
            "name": f"candidate-{family}-{build}",
            "family": family,
            "model": assessment.probable_model or "unknown",
            "build": build,
            "sha256": input_sha,
            "camera_design": "unverified; review manually",
            "required_strings": list(assessment.build_strings)
            + list(assessment.sensor_markers),
            "default_patch_sensor": None,
            "note": "Generated layout candidate. Do not mark trusted before manual review.",
            "candidates": [
                {
                    "partition_id": candidate.partition.pid,
                    "runtime_role": candidate.partition.role,
                    "ir_offset": f"0x{candidate.ir_offset:08x}",
                    "sensor_key": candidate.sensor_key,
                    "sensor_model": candidate.sensor_model,
                    "sensor_role": candidate.sensor_role,
                    "expected_curve": list(candidate.ir_curve),
                    "context_after_sha256": candidate_context_hashes(
                        source.payload, candidate
                    )[0],
                    "over_exposure_sha256": candidate_context_hashes(
                        source.payload, candidate
                    )[1],
                }
                for candidate in candidates
            ],
        },
        "automatic_patch": None,
    }
    atomic_write_bytes(
        path,
        (json.dumps(payload, indent=2, ensure_ascii=False) + "\n").encode("utf-8"),
        overwrite,
    )


def find_layout_by_name(name: str) -> FirmwareLayout:
    matches = [layout for layout in FIRMWARE_LAYOUTS.values() if layout.name == name]
    if len(matches) != 1:
        available = ", ".join(sorted({layout.name for layout in FIRMWARE_LAYOUTS.values()}))
        raise PatchError(f"unknown layout {name!r}; available: {available}")
    return matches[0]


def compare_layout(
    data: bytes | bytearray,
    input_sha: str,
    candidates: Sequence[AECandidate],
    reference: FirmwareLayout,
) -> dict:
    current_by_role: Dict[str, List[AECandidate]] = _runtime_candidate_groups(candidates)
    reference_by_role: Dict[str, List[CandidateIdentity]] = {ROLE_NORMAL: [], ROLE_PIR: []}
    for identity in reference.candidates:
        if identity.runtime_role in reference_by_role:
            reference_by_role[identity.runtime_role].append(identity)
    for role in reference_by_role:
        reference_by_role[role].sort(key=lambda item: item.ir_offset)
    comparisons: List[dict] = []
    for role in (ROLE_NORMAL, ROLE_PIR):
        actual = current_by_role[role]
        expected = reference_by_role[role]
        comparisons.append(
            {
                "runtime": role,
                "candidate_count_same": len(actual) == len(expected),
                "actual_count": len(actual),
                "reference_count": len(expected),
                "curves_same_by_order": [
                    actual[index].ir_curve == expected[index].expected_curve
                    for index in range(min(len(actual), len(expected)))
                ],
                "absolute_offsets_same_by_order": [
                    actual[index].ir_offset == expected[index].ir_offset
                    for index in range(min(len(actual), len(expected)))
                ],
                "context_matches_by_order": [
                    (
                        candidate_context_hashes(data, actual[index])[0]
                        == (expected[index].context_after_sha256 or COMMON_CONTEXT_AFTER_SHA256)
                        and candidate_context_hashes(data, actual[index])[1]
                        == (expected[index].over_exposure_sha256 or COMMON_OVER_EXPOSURE_SHA256)
                    )
                    for index in range(min(len(actual), len(expected)))
                ],
            }
        )
    return {
        "reference_layout": reference.name,
        "reference_sha256": reference.sha256,
        "input_sha256": input_sha,
        "exact_hash_match": input_sha == reference.sha256,
        "required_markers": {
            marker.decode("ascii", "replace"): marker in data
            for marker in reference.required_strings
        },
        "runtime_comparisons": comparisons,
    }


def print_layout_comparison(result: Mapping[str, Any]) -> None:
    log(
        "compare",
        f"reference={result['reference_layout']}, exact_hash_match={result['exact_hash_match']}",
    )
    for marker, present in result["required_markers"].items():
        log("compare", f"marker {marker!r}: {'present' if present else 'missing'}")
    for item in result["runtime_comparisons"]:
        log(
            "compare",
            f"{item['runtime']}: candidates {item['actual_count']}/{item['reference_count']}, "
            f"curves={item['curves_same_by_order']}, "
            f"offsets={item['absolute_offsets_same_by_order']}, "
            f"context={item['context_matches_by_order']}",
        )

def build_manifest(
    *,
    source: FirmwareSource,
    input_sha: str,
    output_sha: str,
    output_path: Optional[Path],
    mode: str,
    profile: Optional[Profile],
    layout: Optional[FirmwareLayout],
    assessment: SupportAssessment,
    partitions: Sequence[Partition],
    targets: Sequence[PatchTarget],
    checksum_changes: Sequence[ChecksumChange],
    changed_ranges: Sequence[Tuple[int, int]],
    dry_run: bool,
) -> dict:
    return {
        "manifest_type": "patch",
        "schema": 1,
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
        "support": support_to_dict(assessment),
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
        "recognized_layout": (
            {
                "name": layout.name,
                "model": layout.model,
                "build": layout.build,
                "camera_design": layout.camera_design,
                "note": layout.note,
            }
            if layout
            else None
        ),
        "targets": [
            {
                "partition_id": target.candidate.partition.pid,
                "role": target.candidate.partition.role,
                "load_address": f"0x{target.candidate.partition.load_address:08x}",
                "ir_offset": f"0x{target.candidate.ir_offset:08x}",
                "sensor_key": target.candidate.sensor_key,
                "sensor_model": target.candidate.sensor_model,
                "sensor_role": target.candidate.sensor_role,
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
        description=(
            "Inspect and patch Suntek/Novatek night-AE runtimes with fail-closed "
            "firmware compatibility levels."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Support levels:\n"
            "  verified         exact trusted SHA-256 profile\n"
            "  family-match     probable known model family, unknown build\n"
            "  structural-match plausible AE structure, unknown model/sensors\n"
            "  unsupported      patching disabled\n\n"
            "Unknown builds require --allow-unverified, --expect-ir, and for a real\n"
            "write an accepted manifest from a previous --scan run."
        ),
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("input", nargs="?", type=Path, help="manufacturer .bin or .zip")
    parser.add_argument("-o", "--output", type=Path, help="output .bin or .zip")
    parser.add_argument("--overwrite", action="store_true", help="allow replacing an existing output")

    registry = parser.add_argument_group("profile registry")
    registry.add_argument(
        "--profile-dir",
        action="append",
        type=Path,
        default=[],
        help="load additional JSON profile directory; repeatable",
    )
    registry.add_argument(
        "--trust-external-profiles",
        action="store_true",
        help="treat --profile-dir content as trusted (dangerous; review files first)",
    )
    registry.add_argument(
        "--list-profiles",
        action="store_true",
        help="print automatic profiles and recognized layouts, then exit",
    )

    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--profile",
        help="verified automatic profile name, or 'auto' (default by exact SHA-256)",
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
        "--sensor",
        action="append",
        default=[],
        metavar="all|day|night|single|MODEL",
        help="select a verified/probable sensor identity; repeatable",
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
        help="deprecated alias; all runtimes are already the default",
    )

    expert = parser.add_argument_group("unverified-build expert workflow")
    expert.add_argument(
        "--allow-unverified",
        action="store_true",
        help="permit a dry-run/patch on family or structural matches",
    )
    expert.add_argument(
        "--expect-ir",
        action="append",
        type=parse_expected_curve,
        default=[],
        metavar="[OFFSET=]V1,...,V21",
        help="required original curve assertion for every unverified target",
    )
    expert.add_argument(
        "--accept-scan-manifest",
        type=Path,
        help="bind an unverified real write to a previous --scan manifest",
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

    inspect = parser.add_argument_group("inspection and compatibility")
    inspect.add_argument("--dry-run", action="store_true", help="validate a patch without writing firmware")
    inspect.add_argument("--scan", action="store_true", help="locate runtimes, sensors, and curves")
    inspect.add_argument(
        "--compat-check",
        action="store_true",
        help="classify support level and explain the evidence without patching",
    )
    inspect.add_argument(
        "--verify-only",
        action="store_true",
        help="verify outer and all detectable internal checksums only",
    )
    inspect.add_argument(
        "--compare-layout",
        metavar="LAYOUT",
        help="compare the discovered runtime/sensor structure with a known layout",
    )
    inspect.add_argument(
        "--export-layout",
        type=Path,
        help="write an unverified JSON layout candidate for manual review",
    )
    inspect.add_argument(
        "--manifest",
        type=Path,
        help="write a JSON analysis, compatibility, verification, or patch manifest",
    )
    return parser.parse_args(argv)


def print_profiles() -> None:
    print("Automatic patch profiles:")
    for profile in sorted(PROFILES.values(), key=lambda item: item.name):
        trust = "trusted" if profile.trusted else "external/untrusted"
        print(
            f"{profile.name}: {profile.model}, build {profile.build}, "
            f"SHA-256 {profile.sha256} [{trust}]\n"
            f"  original: {format_curve(profile.expected_curve)}\n"
            f"  target  : {format_curve(profile.target_curve)}\n"
            f"  source  : {profile.source}"
        )
    print("\nRecognized firmware layouts:")
    seen: set[Tuple[str, str]] = set()
    for layout in sorted(FIRMWARE_LAYOUTS.values(), key=lambda item: (item.name, item.sha256)):
        key = (layout.name, layout.sha256)
        if key in seen:
            continue
        seen.add(key)
        automatic = next(
            (
                profile.name
                for profile in PROFILES.values()
                if profile.sha256 == layout.sha256 and profile.trusted
            ),
            "none (recognition/explicit patch only)",
        )
        trust = "trusted" if layout.trusted else "external/untrusted"
        print(
            f"{layout.name}: {layout.model}, build {layout.build}, "
            f"SHA-256 {layout.sha256} [{trust}]\n"
            f"  family  : {layout.family or 'unspecified'}\n"
            f"  design  : {layout.camera_design}\n"
            f"  auto    : {automatic}\n"
            f"  source  : {layout.source}\n"
            f"  note    : {layout.note}"
        )


def _write_json_manifest(path: Path, payload: Mapping[str, Any], overwrite: bool) -> None:
    atomic_write_bytes(
        path,
        (json.dumps(payload, indent=2, ensure_ascii=False) + "\n").encode("utf-8"),
        overwrite,
    )
    log("done", f"wrote manifest {path}")


def _lightweight_verification_assessment(
    input_sha: str, data: bytes | bytearray
) -> SupportAssessment:
    layout = FIRMWARE_LAYOUTS.get(input_sha)
    if layout is not None and layout.trusted:
        return SupportAssessment(
            level=SUPPORT_VERIFIED,
            probable_family=layout.family or None,
            probable_model=layout.model,
            confidence="exact",
            score=100,
            maximum_score=100,
            reasons=("exact trusted SHA-256 profile found",),
            warnings=("AE structures were not scanned in --verify-only mode",),
            build_strings=extract_build_strings(data),
            sensor_markers=detect_sensor_markers(data),
            automatic_patch_allowed=input_sha in PROFILE_BY_SHA,
            layout_source=layout.source,
        )
    return SupportAssessment(
        level=SUPPORT_NOT_CLASSIFIED,
        probable_family=None,
        probable_model=None,
        confidence="not-scanned",
        score=0,
        maximum_score=100,
        reasons=("checksum verification only; run --compat-check for classification",),
        warnings=("unknown SHA-256 was not structurally scanned",),
        build_strings=extract_build_strings(data),
        sensor_markers=detect_sensor_markers(data),
        automatic_patch_allowed=False,
        layout_source=None,
    )


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    load_profile_registry(
        args.profile_dir, trust_external=args.trust_external_profiles
    )
    if args.list_profiles:
        print_profiles()
        return 0
    if args.input is None:
        raise PatchError("an input .bin or .zip is required")
    if args.all:
        log("warn", "--all is deprecated; version 3 patches all detected runtimes by default")

    patch_requested = any(
        value is not None for value in (args.ir, args.ir_scale, args.ir_values, args.profile)
    )
    inspection_requested = any(
        (args.scan, args.compat_check, args.verify_only, args.compare_layout, args.export_layout)
    )
    if args.verify_only and (patch_requested or any((args.scan, args.compat_check, args.compare_layout, args.export_layout))):
        raise PatchError("--verify-only cannot be combined with patch or structural inspection modes")
    if (args.scan or args.compat_check or args.compare_layout or args.export_layout) and patch_requested:
        raise PatchError("inspection/export modes cannot be combined with patch modes")
    if args.ir_offset and (args.runtime or args.sensor):
        raise PatchError("use either --ir-offset or --runtime/--sensor selection, not both")
    if (args.iso_cap is None) != (not args.iso_offset):
        raise PatchError("--iso-cap and at least one --iso-offset must be used together")
    if args.expect_ir and not args.allow_unverified:
        raise PatchError("--expect-ir is meaningful only with --allow-unverified")
    if args.accept_scan_manifest and not args.allow_unverified:
        raise PatchError("--accept-scan-manifest requires --allow-unverified")

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
        assessment = _lightweight_verification_assessment(input_sha, original)
        print_support(assessment)
        if args.manifest:
            manifest = build_analysis_manifest(
                source=source,
                input_sha=input_sha,
                partitions=partitions,
                candidates=(),
                assessment=assessment,
                mode="verify-only",
            )
            manifest["manifest_type"] = "verification"
            _write_json_manifest(args.manifest, manifest, args.overwrite)
        log("done", f"all verifiable checksums are valid; SHA-256 {input_sha}")
        return 0

    exact_layout = FIRMWARE_LAYOUTS.get(input_sha)
    if exact_layout is not None:
        raw_candidates = candidates_from_layout(original, partitions, exact_layout)
    else:
        raw_candidates = find_all_ae_candidates(original, partitions)
    identified, exact_layout = identify_candidates(
        original,
        input_sha,
        raw_candidates,
        require_complete_layout=True,
    )
    assessment, candidates, layout = assess_support(
        original, input_sha, identified, exact_layout
    )
    if args.ir_offset and inspection_requested:
        manual_scan = resolve_manual_candidates(original, partitions, args.ir_offset)
        existing_offsets = {candidate.ir_offset for candidate in candidates}
        for candidate in manual_scan:
            if candidate.ir_offset not in existing_offsets:
                candidates.append(
                    replace(
                        candidate,
                        sensor_key="manual-offset",
                        sensor_model=None,
                        sensor_role=SENSOR_UNKNOWN,
                    )
                )
        candidates.sort(key=lambda item: item.ir_offset)
    print_support(assessment)
    if layout is not None:
        log(
            "model",
            f"{layout.model}, build {layout.build}; {layout.camera_design}",
        )
        if layout.note:
            log("model", layout.note)

    for candidate in sorted(candidates, key=lambda item: item.ir_offset):
        log(
            "scan",
            f"partition id {candidate.partition.pid} ({candidate.partition.role}), "
            f"load=0x{candidate.partition.load_address:08x}, "
            f"sensor={candidate.sensor_key} ({candidate.sensor_role}), "
            f"tab_ratio_ir=0x{candidate.ir_offset:08x}, "
            f"curve={format_curve(candidate.ir_curve)}, "
            f"over_exposure=IR+0x{candidate.over_exposure_delta:x}",
        )

    comparison: Optional[dict] = None
    if args.compare_layout:
        reference = find_layout_by_name(args.compare_layout)
        comparison = compare_layout(original, input_sha, candidates, reference)
        print_layout_comparison(comparison)
    if args.export_layout:
        export_layout_candidate(
            args.export_layout,
            source,
            input_sha,
            candidates,
            assessment,
            args.overwrite,
        )
        log("done", f"wrote unverified layout candidate {args.export_layout}")

    if inspection_requested:
        if args.manifest:
            analysis_mode = "compat-check" if args.compat_check else "scan"
            manifest = build_analysis_manifest(
                source=source,
                input_sha=input_sha,
                partitions=partitions,
                candidates=candidates,
                assessment=assessment,
                mode=analysis_mode,
            )
            if comparison is not None:
                manifest["layout_comparison"] = comparison
            _write_json_manifest(args.manifest, manifest, args.overwrite)
        log("done", f"inspection complete; input SHA-256 {input_sha}")
        return 0

    if args.ir_offset:
        manual = resolve_manual_candidates(original, partitions, args.ir_offset)
        known_by_offset = {candidate.ir_offset: candidate for candidate in candidates}
        selected = [known_by_offset.get(item.ir_offset, item) for item in manual]
    else:
        selected = select_candidates(candidates, args.runtime, args.sensor, layout)

    enforce_patch_policy(args, assessment, input_sha, selected)
    targets, mode, profile = build_targets(args, input_sha, selected, layout)
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

    unverified = assessment.level != SUPPORT_VERIFIED
    destination = args.output or default_output_path(source, unverified=unverified)
    manifest = build_manifest(
        source=source,
        input_sha=input_sha,
        output_sha=output_sha,
        output_path=None if args.dry_run else destination,
        mode=mode,
        profile=profile,
        layout=layout,
        assessment=assessment,
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
        _write_json_manifest(args.manifest, manifest, args.overwrite)
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
