# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project uses
[Semantic Versioning](https://semver.org/).


## [3.0.0] - 2026-07-16

### Added

- Four explicit firmware support levels:
  - `verified` for an exact trusted BIN SHA-256 profile;
  - `family-match` for a high-confidence known model family with an unknown
    build hash;
  - `structural-match` for a valid container and plausible AE structures whose
    model or sensor identity is not verified;
  - `unsupported` when required structures are missing or contradictory.
- `--compat-check` to classify an image and print the evidence, confidence,
  score, warnings, detected build strings, sensor markers, runtimes, and AE
  candidates without patching.
- Analysis/compatibility manifests from `--scan --manifest` and
  `--compat-check --manifest`, including candidate curves and relative context
  fingerprints. Scan manifests can now be used as a cryptographic safety gate
  for later writes.
- `--compare-layout LAYOUT` to compare an unknown build with a known generation:
  candidate counts, original curves, absolute offsets, required markers, and
  relative SDK context are reported separately.
- `--export-layout FILE` to create an explicitly unverified JSON profile
  candidate for manual review and pull requests.
- A fail-closed expert workflow for unknown builds:
  - `--allow-unverified` acknowledges the risk;
  - `--expect-ir [OFFSET=]V1,...,V21` asserts every selected original curve;
  - `--accept-scan-manifest FILE` binds a real write to a previous scan with the
    same input SHA-256, offsets, and original curves.
- Data-driven profile loading from bundled `profiles/*.json` files and optional
  repeatable `--profile-dir` directories.
- `--trust-external-profiles` as an explicit opt-in after independent review.
  External profiles are recognition-only by default and cannot override trusted
  bundled profiles.
- Relative AE context fingerprints. Profiles can validate stable data after
  `tab_ratio_ir` and the characteristic over-exposure threshold block instead
  of relying only on absolute offsets or short byte signatures.
- Repository smoke tests for profile schemas, registry loading, versioning, and
  original-curve assertion parsing.

### Changed

- **Breaking safety change:** unknown firmware is never labeled `single` merely
  because one candidate was found in a runtime. Without independent family
  evidence, all candidates remain `unidentified`.
- **Breaking safety change:** a probable model-family match may be scanned and
  sensor-labeled, but it cannot use an automatic profile or ordinary write mode.
- **Breaking safety change:** a `structural-match` requires explicit
  `--ir-offset` targets; guessed sensor selection is not accepted.
- **Breaking safety change:** a real write on any non-`verified` image requires
  a previously generated and accepted scan manifest. Default output names
  contain `_UNVERIFIED_PATCHED`.
- HC-940Ultra, HC-950Ultra, and HC-960Ultra family detection now combines build
  prefixes, required markers, runtime presence, exact candidate counts, and
  relative SDK context. Candidate-count mismatches disable family assignment.
- Known firmware knowledge is separated from the parser/checksum code through
  schema-versioned JSON profiles. Built-in conservative fallbacks remain for
  standalone-script use.
- `--verify-only` remains a fast checksum-only operation. Unknown hashes are not
  structurally classified until `--compat-check` or `--scan` is run.
- `--list-profiles` now prints trust state and source path for every profile and
  layout.
- Patch and analysis manifests now include a schema, manifest type, support
  assessment, confidence, reasons, warnings, build markers, and profile source.

### Safety

- Unknown dual-camera firmware cannot be silently treated as single-camera when
  one sensor's AE structure changes or becomes temporarily undetectable.
- HC-950 family sensor labels are assigned only when both normal/remote and
  low-power/PIR runtimes each contain exactly two candidates and all other
  family checks pass. Any missing or extra candidate falls back to
  `structural-match`.
- Untrusted profile directories cannot replace a trusted official layout or
  automatic profile unless the user explicitly enables
  `--trust-external-profiles`.
- Original-curve assertions are checked before any checksum or firmware write.
- Accepted scan manifests are checked against the current input SHA-256, every
  selected offset, and every selected original curve.

### Validation notes

- Python compilation and all bundled unit tests pass on Python 3.13.
- Exact HC-960Ultra automatic-profile output remains:
  `a66190b5f418a2e54c09042f154411777bb2b3f7ec339023a1331442600c4667`.
- Exact HC-940Ultra automatic-profile output remains:
  `a0a7b94cc9e1c4e7da51b8ddf4c8b18a619d2acecf8b874247ca3669e5bf9a53`.
- Exact HC-950Ultra 2026 test-only `--ir 109` output remains:
  `da957f032d0d06c83a07a2f9791acc1c3d02e83642c8db3332cf8f1e1f63ee2a`.
- A checksum-valid synthetic unknown HC-950 build was classified as
  `family-match`, selected only the probable SC223AP night tables, rejected a
  dry run without `--allow-unverified`, rejected a dry run without
  `--expect-ir`, and rejected a real write without an accepted scan manifest.
- The same synthetic image with one deliberately unrecognizable AE candidate
  was downgraded to `structural-match`; it was not mislabeled as a single-camera
  firmware.
- An accepted unverified write used the visible `_UNVERIFIED_PATCHED` name and
  passed all internal checksums, the outer checksum, target-curve verification,
  changed-byte whitelisting, and disk round-trip verification.
- Synthetic images and hashes are test artifacts only and are not distributed
  or recommended for flashing.

## [2.2.0] - 2026-07-16

### Added

- Verified recognition for the newer HC-950Ultra / `950XFUltra_20260527`
  manufacturer firmware with BIN SHA-256
  `a6caf6be7e1a77dfe434ae78b959390b190f2e3b6e9b6e0cb5c8b29b2e6edf61`.
- A second strict HC-950 dual-camera layout containing all four sensor/runtime
  identities:
  - normal/remote IMX258M day camera at `0x006c2de4`;
  - normal/remote SC223AP night camera at `0x006c3a88`;
  - low-power/PIR IMX258M day camera at `0x01834710`;
  - low-power/PIR SC223AP night camera at `0x018353d8`.
- Build-string validation for both known HC-950 images:
  `950XFUltra_20240808` and `950XFUltra_20260527`.
- Documentation and regression vectors for both verified HC-950 generations.

### Changed

- The HC-950 layout names now include their generation:
  `hc950-dual-camera-2024` and `hc950-dual-camera-2026`.
- `--list-profiles` now lists both recognized HC-950 builds separately while
  keeping them distinct from automatic exposure profiles.
- The 2026 HC-950 layout validates its mixed original curves exactly: the
  normal/remote IMX258M table uses `110..125`, while the other three tables use
  `110 x21`.
- README examples now cover verification, scanning, sensor selection, runtime
  selection, and fail-closed offset handling on the 2026 firmware.

### Safety

- There is still **no automatic HC-950Ultra exposure profile** for either
  firmware generation. Factory night exposure was reported as good, so a patch
  requires an explicit `--ir`, `--ir-scale`, or `--ir-values` request.
- Explicit HC-950 custom changes continue to default to the SC223AP night sensor
  in both runtimes. The IMX258M day tables remain untouched unless selected.
- Offsets are never shared between the 2024 and 2026 images. Exact SHA-256,
  runtime role, table offset, expected original curve, and sensor/build markers
  must all match before sensor-aware selection is enabled.

### Validation notes

- All detectable internal checksums and the outer NVTPACK checksum reproduce on
  the original `950XFUltra_20260527` image.
- A test-only `--ir 109 --dry-run` selects only the two SC223AP night tables and
  produces BIN SHA-256
  `da957f032d0d06c83a07a2f9791acc1c3d02e83642c8db3332cf8f1e1f63ee2a`.
- `--sensor day --ir 109 --dry-run` produces
  `31f159f0ff5fda2851bf4639e8aabaa64efc7c3239871deed6b38575c5f6ba27`.
- `--sensor all --ir 109 --dry-run` produces
  `a38629736a38416bbeb102b7318ad9fd7c5008a69af96acf529d56a333e133cf`.
- A real ZIP write with the default SC223AP test selection passed output ZIP
  preservation, JSON manifest generation, byte whitelist, checksum updates,
  and disk/ZIP round-trip verification.
- HC-960Ultra, HC-940Ultra, and HC-950Ultra 2024 regression hashes remain
  unchanged from version 2.1.0.
- The HC-950 hashes above are software test vectors, not recommended firmware
  images.

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
