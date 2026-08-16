# Kiro hooks

This folder is reserved for optional Kiro lifecycle hooks: commands or integrations triggered by editor or agent events.

There are currently no hooks in this project. If one is added, keep it narrow, deterministic, and safe to run repeatedly. A hook must not bypass autodev’s worktree boundary, quality gates, or environment-based secret handling.
