# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.0.1] - 2026-02-02

### Fixed
- Fixed internal `SyntaxError` by moving `from __future__` import to the top of the file.
- Improved library initialization and environment compatibility.

## [1.0.0] - 2026-02-01

### Added
- Initial release of PySlideSpeaker.
- Core functionality to generate MP4 videos from PDF slides and YAML scripts.
- Integration with `edge-tts` for voice synthesis.
- Integration with `PyMuPDF` for PDF image extraction.
- Integration with `moviepy` for video assembly.
- Hash-based caching system to speed up incremental builds.
- CLI interface with support for custom configuration via YAML.
- Auto-detection of PDF and YAML files in current directory.
- LLM prompt template for YAML script generation.
