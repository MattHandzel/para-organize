# Changelog

All notable changes to para-organize.nvim will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- Initial project structure
- Configuration module with validation and debug support
- Utility functions for file operations and frontmatter parsing
- Indexer for scanning and metadata extraction
- Search module with Telescope integration
- Suggestion engine with learning capabilities
- UI module with two-pane interface using nui.nvim
- Safe file operations with archive functionality
- Learning system that improves suggestions over time
- Documentation: README, MANUAL, and vimdoc help
- Comprehensive test infrastructure with busted
- Health checks for dependencies and configuration
- Development environment with NixOS flake
- GitHub Actions CI/CD workflows
- Debug command for troubleshooting
- Example configurations (minimal and full)

### Fixed
- Reserved keyword 'until' usage in search.lua (now using 'until_date')
- Reserved keyword 'then' usage in utils.lua (now using 'time_then')
- Tag normalization function to properly handle special characters
- String similarity test expectations
- Frontmatter parser to correctly handle complex YAML structures
  - Added support for nested structures with proper indentation handling
  - Fixed key pattern to support keys with dashes and underscores
  - Improved end delimiter detection for frontmatter
  - Enhanced list item parsing for complex nested structures

## [0.1.0] - 2025-09-14
- Initial release with core functionality
