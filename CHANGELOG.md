# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project uses
[Semantic Versioning](https://semver.org/).

## [2.0.0] - 2026-07-15

### Added

- Dual-runtime discovery and patching. The tool now finds and patches both the
  normal/remote camera runtime and the separate low-power/PIR runtime when both
  are present.
- Verified automatic profiles for exact manufacturer images:
  - `hc960-ae55`: HC-960Ultra build 2026-03-26, flat `110 x21` to `55 x21`.
  - `hc940-ae58`: HC-940Ultra build 2025-04-23, curve-preserving
    `110..125` to the image-calibrated `58..66` test curve.
- Direct input and output support for manufacturer ZIP archives containing one
  firmware BIN file.
- Curve-preserving patch modes:
  - `--ir-scale FACTOR`
  - `--ir-values V1,...,V21`
  - existing flat mode `--ir VALUE`
- Runtime selection with repeatable `--runtime all|normal|pir|PID`.
- Repeatable, structurally validated `--ir-offset` overrides.
- JSON patch manifests with hashes, runtime roles, old/new curves, checksum
  changes, changed ranges, and verification results.
- `--scan` and `--list-profiles` commands.
- Manual ISO-cap patching with explicit, validated `--iso-offset` values.

### Fixed

- Corrected the `NVTPACK_FW_HDR2` partition-table parser. The table location is
  read from header offset `0x14` (normally `0x80`), and records are parsed as
  `{offset, size, partition_id}`. Version 1.1.0 started at `0x88` and interpreted
  the record fields in the wrong order.
- Fixed the root cause of "remote image fixed, PIR image still overexposed":
  version 1.x patched only the main runtime at load address `0x02700400`; the
  second runtime at `0x00400400` retained its own unmodified AE table.
- AE-table detection now supports both flat and non-flat monotonic curves. This
  allows the HC-940Ultra layout, whose `tab_ratio_mov`, `tab_ratio_photo`, and
  `tab_ratio_ir` shapes differ from the HC-960Ultra layout.
- `--verify-only` now verifies the outer checksum and every detectable internal
  partition checksum, including the PIR runtime and bootloader checksum.
- Manual offsets are assigned to their containing partition, and every changed
  partition receives its own recalculated checksum.
- The checksum implementation is explicitly little-endian on every host
  platform.
- The outer 16-bit checksum is now written as 16 bits instead of clearing the
  complete 32-bit header field.
- Ambiguous AE matches now stop with an error instead of silently patching the
  first candidate.
- Round-trip failure removes the output file; temporary files are unique, output
  replacement is opt-in, and the destination directory is synchronized.
- Added an exact changed-byte whitelist so a valid checksum alone cannot hide an
  accidental write outside the requested tables, ISO fields, or checksum fields.

### Changed

- **Breaking:** all detected camera runtimes are patched by default. `--all` is
  retained only as a deprecated compatibility alias.
- **Breaking:** firmware without an exact automatic profile must use `--ir`,
  `--ir-scale`, or `--ir-values`; the tool no longer guesses a universal target.
- **Breaking:** automatic `iso_prv.h` searching was removed because the first
  `{H,100}` pair is not structurally unique. `--iso-cap` now requires one or more
  explicit `--iso-offset` values.
- **Breaking:** unsafe `--uit-off`, `--uit-size`, and broad `--force` paths were
  removed. Partition boundaries must come from a valid container table.
- Terminology now uses "checksum" rather than "CRC" for the Novatek
  position-weighted additive checksum.
- Documentation now lists only the HC-940Ultra and HC-960Ultra, the models/builds
  actually examined in this project.

### Validation notes

- Both supplied manufacturer images reproduce all original inner and outer
  checksums before patching.
- The HC-960Ultra automatic profile produces the previously verified dual-table
  output SHA-256
  `a66190b5f418a2e54c09042f154411777bb2b3f7ec339023a1331442600c4667`.
- The HC-940Ultra automatic profile produces the image-calibrated dual-table
  output SHA-256
  `a0a7b94cc9e1c4e7da51b8ddf4c8b18a619d2acecf8b874247ca3669e5bf9a53`.
- Static checks, hashes, exact byte ranges, and checksum round trips do not
  guarantee safe flashing on every hardware revision. Keep the original image
  and a recovery path.

## [1.1.0] - 2026-07-09

### Added

- `--ir-offset 0x<offset>` to select a specific `tab_ratio_ir` table.
- `--all` to patch multiple tables within the selected runtime.
- `--force`, `--version`, and additional troubleshooting documentation.

### Changed

- Refactored the tool into a `NovatekFW` class.
- Improved the original single-runtime AE-table locator and write verification.

### Known limitations corrected in 2.0.0

- Parsed the partition table from the wrong offset and field order.
- Selected only the main runtime, leaving the PIR runtime unpatched.
- Required a ramp/flat/flat table shape that did not match HC-940Ultra firmware.
- Verified only the selected runtime plus the outer checksum.

## [1.0.0] - 2026-07-03

### Added

- Initial `patch_ae.py` release.
- English and Simplified Chinese documentation, MIT license, and safety guidance.
- End-to-end HC-960Ultra normal/remote-path test reducing severe night clipping.
