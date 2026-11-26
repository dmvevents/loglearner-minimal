"""The default source: Claude Code session JSONL under ~/.claude/projects/.

Thin registration wrapper — the actual scanning stays in session_scanner and
the v2 pipeline is untouched. Existing here so `--list-sources` truthfully
enumerates the default alongside the plugins (E1 definition of done).
"""

from __future__ import annotations

from . import register_source


@register_source("claude")
class ClaudeSessionSource:
    kind = "sessions"
    description = ("Claude Code session JSONL (~/.claude/projects, env "
                   "CLAUDE_PROJECTS_ROOT). Default; mined by the v1/v2 pipelines.")

    @staticmethod
    def run(args) -> int:
        print("`--source claude` is the default mining path — run without "
              "--source, or with --v2, exactly as before.")
        return 0
