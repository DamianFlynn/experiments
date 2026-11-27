# Changelog

All notable changes to this module are documented in this file.

## [Unreleased]

### Added

- Variable `tags` on the private endpoint.

## [1.2.0] - 2026-10-14

### Added

- Variable `diagnostic_settings` to send logs to a workspace.
- Output `resource_id`.

### Deprecated

- Variable `legacy_network_rules`; use `network_rules` instead.

### Removed

- Output `primary_key`
  (read it from the resource instead).

### Notes

- The examples now pin the provider to `~> 4.0`.

## [1.1.0] - 2026-09-01

### Fixed

- Role assignments are no longer recreated on every plan.

[Unreleased]: https://github.com/example/terraform-module/compare/v1.2.0...HEAD
[1.2.0]: https://github.com/example/terraform-module/compare/v1.1.0...v1.2.0
