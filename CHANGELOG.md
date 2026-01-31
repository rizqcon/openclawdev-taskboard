# Changelog

All notable changes to this project will be documented in this file.

## [1.1.0] - 2026-01-31

### Added
- **Identity emoji support**: Command bar icon now uses `MAIN_AGENT_EMOJI` from environment variable (defaults to 🛡️)
- **Multi-line input**: Chat input is now a textarea supporting Shift+Enter for new lines
- **Graceful WebSocket reconnect**: Shows glowing indicator during reconnection instead of error messages
- **Retry logic**: API calls retry up to 3 times with exponential backoff for transient failures

### Changed
- **Scrollbar styling**: Thin, styled scrollbar (6px) that doesn't overlap borders
- **Textarea auto-resize**: Input grows with content up to 150px max height
- **Textarea reset**: Input resets to single line after sending message
- **Thinking indicator**: Moved from input area to header as glowing shield icon
- **Placeholder text**: Shortened to "Ctrl+V to paste images · Shift+Enter for new line"

### Fixed
- **Button alignment**: Attach, input, and send buttons now properly aligned at 44px height
- **OCD-compliant symmetry**: All input row elements use consistent sizing and box-sizing

## [1.0.0] - 2026-01-28

### Added
- Initial release
- Kanban board with Backlog, In Progress, Review, Done, Blocked columns
- Agent assignment and management
- Real-time WebSocket updates
- Agent chat integration via OpenClaw
- Task comments and action items
- Priority levels (Critical, High, Medium, Low)
- Agent work indicators
