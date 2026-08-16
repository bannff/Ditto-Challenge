# Source layout

This project uses the Python `src` layout so imports resolve from the installed package rather than accidentally from the repository root.

`self_improving_coding_agent/` contains autodev’s distributable implementation. Target applications, ticket fixtures, scripts, and tests live outside `src/` because they exercise the platform rather than form part of it.
