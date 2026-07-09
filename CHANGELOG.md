# Changelog

All notable changes to this project are documented here.
The format is based on [Keep a Changelog](https://keepachangelog.com/), and this project adheres to
[Semantic Versioning](https://semver.org/).

## [1.1.0] - 2026-07-09

### Added
- `--ir-offset 0x<offset>` to select a specific `tab_ratio_ir` table explicitly.
- `--all` to patch **every** `tab_ratio_ir` table when more than one is present.
- `--force` to proceed past non-fatal safety stops (already-modified input, implausible value, missing
  container marker).
- `--version` flag; the script now exposes `__version__`.
- **Troubleshooting** section in the README (English + 中文).

### Changed
- Refactored the tool into a `NovatekFW` class with type hints and clearer separation of concerns.
- The `tab_ratio_ir` locator now anchors on the `tab_ratio_mov` ramp **and requires plausible ratio
  values**, which removes false positives (e.g. all-zero tables) and produces a single, correct match.

### Hardened (resilience)
- Verification now runs **in memory before anything is written**; the output is written **atomically**
  (temp file + `os.replace`, with `fsync`) and then **re-verified round-trip** from disk. The tool
  never leaves a half-written or invalid image.
- Input validation: missing, too-small, or non-`NVTPACK_FW_HDR2` files fail cleanly with a clear
  message instead of a traceback.
- All reads are bounds-checked; the tool refuses to overwrite the input file; controlled errors use a
  non-zero exit code (`2`).

### Compatibility
- **Default behaviour is unchanged.** For the verified HC-960Ultra-li case the output is
  **bit-identical** to v1.0.0 (23 bytes changed, identical checksums). Existing commands keep working.

## [1.0.0] - 2026-07-03

### Added
- Initial release.
- `patch_ae.py`: diagnoses and fixes night/IR over-exposure by lowering the AE `tab_ratio_ir`
  night-luminance target inside the µITRON partition and recomputing the Novatek
  `NVTPACK_FW_HDR2` checksums (µITRON partition checksum + whole-file CRC) so the image boots and
  flashes normally.
- Documentation in English and 简体中文, an AI/maintainer runbook, `LICENSE` (MIT), `SECURITY.md`,
  and a GitHub issue template.
- Verified end-to-end on a real HC-960Ultra-li (night clipping reduced from ~22 % to ~1 %).

[1.1.0]: https://github.com/nocusham/suntek-trailcam-night-fix/releases/tag/v1.1.0
[1.0.0]: https://github.com/nocusham/suntek-trailcam-night-fix/releases/tag/v1.0.0
