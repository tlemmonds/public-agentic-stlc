# Agent instructions

All agent context for this repo lives in [CLAUDE.md](CLAUDE.md). It applies to
any coding agent (Claude Code, GitHub Copilot, Codex, Cursor, …).

If the user asks you to set this pipeline up for their application, follow the
**Setup playbook (new AUT)** section of CLAUDE.md step by step, and observe its
working rules: ask before any credential or login step, never commit secrets,
and never modify the application under test.
