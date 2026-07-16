# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project uses
[Semantic Versioning](https://semver.org/).

## [2.1.0] - 2026-07-16

### Added

- Verified recognition for the HC-950Ultra / `950XFUltra_20240808` manufacturer
  firmware with BIN SHA-256
  `e4db261f9228af5793d5952b45f9b6e9e41b2a50e264ac8971e5145d8cc19370`.
- Dual-camera sensor identities for the HC-950Ultra:
  - IMX258M day camera;
  - SC223AP night camera.
- Detection and labeling of all four HC-950Ultra AE configurations: day and
  night sensors in both the normal/remote and low-power/PIR runtimes.
- Repeatable `--sensor` selection with `single`, `day`, `night`, `imx258m`,
  `sc223ap`, and `all` selectors.
- Sensor and recognized-layout metadata in JSON manifests.
- A recognized-layout registry that validates exact partition IDs, runtime
  roles, AE offsets, original curves, and required sensor marker strings.
- Documentation for the HC-950Ultra two-stage update: `FWHC940A.bin` is the
  Novatek camera-system firmware handled by this tool; the separate `.pac` plus
  `upgrade_tool` package updates the 4G modem and is out of scope.

### Changed

- Known firmware layouts are now resolved directly from verified offsets instead
  of scanning every 4-byte position in multi-megabyte runtime partitions. This
  significantly speeds up `--scan`, profile patching, and dry runs on all three
  verified builds while retaining full structural validation.
- On the exact HC-950Ultra layout, an explicit custom curve without `--sensor`
  defaults to the SC223AP night sensor in both runtimes. The IMX258M day tables
  remain unchanged unless `--sensor day`, `--sensor imx258m`, or `--sensor all`
  is explicitly selected.
- `--list-profiles` now separates automatic exposure profiles from recognized
  firmware layouts.
- Scan output now includes sensor key and sensor role for every AE candidate.
- Documentation now distinguishes firmware recognition from a recommended
  exposure patch.

### Safety

- There is deliberately **no automatic HC-950Ultra exposure profile**. Supplied
  night images were correctly exposed, so invoking the tool without an explicit
  curve mode stops with an explanatory message rather than creating a patch.
- Unknown firmware with multiple AE structures in one runtime can still be
  scanned, but cannot be patched through guessed sensor grouping. Each target
  must be supplied with a manually verified `--ir-offset`.
- Manual `--ir-offset` selection cannot be combined with `--runtime` or
  `--sensor`, preventing conflicting target descriptions.
- ISO offsets remain expert-only and are not automatically associated with a
  specific sensor on dual-camera firmware.

### Validation notes

- HC-960Ultra automatic-profile output remains
  `a66190b5f418a2e54c09042f154411777bb2b3f7ec339023a1331442600c4667`.
- HC-940Ultra automatic-profile output remains
  `a0a7b94cc9e1c4e7da51b8ddf4c8b18a619d2acecf8b874247ca3669e5bf9a53`.
- HC-950Ultra test-only `--ir 109 --dry-run` defaults to the two SC223AP night
  tables and produces the regression BIN SHA-256
  `a03b34e0b24f5dde27a3b39598d69f38c5ff064e75a98775c3b3c567fcadbb3c`.
  This hash is a software test vector, not a recommended firmware image.
- HC-950Ultra `--sensor day --ir 109 --dry-run` selects exactly the two IMX258M
  tables; `--sensor all` selects all four tables.
- All original detectable partition checksums and the outer NVTPACK checksum
  reproduce before patching, and every test output passes in-memory and
  round-trip verification.

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
