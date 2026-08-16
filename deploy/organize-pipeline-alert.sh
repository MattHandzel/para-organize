#!/usr/bin/env bash
# organize pipeline failure alerter (spec 06 §5).
#
# Invoked by organize-pipeline-failure@.service with the failed unit's name.
# Installed as ~/.local/bin/organize-pipeline-alert (see deploy/README.md).
#
# Contract:
#   - ALWAYS writes a journal marker tagged `organize-pipeline-alert`
#     (the unit sets SyslogIdentifier), including the failed unit's result,
#     exit status, and the tail of its own journal;
#   - ALSO raises a desktop notification when a session bus is reachable;
#   - exits 0 even when notify-send is unavailable, so a missing desktop
#     does not turn one failure into two confusing ones. The journal marker
#     is the channel you can build a check on.
#
# Note the asymmetry with organize-pipeline.service, which must NEVER
# swallow a nonzero exit: this script is the reporter, not the work.

set -uo pipefail

unit="${1:-organize-pipeline.service}"
when="$(date -Is 2>/dev/null || date)"

result="$(systemctl --user show -p Result --value "$unit" 2>/dev/null || echo unknown)"
status="$(systemctl --user show -p ExecMainStatus --value "$unit" 2>/dev/null || echo '?')"

{
    printf 'ORGANIZE PIPELINE FAILED\n'
    printf '  unit:    %s\n' "$unit"
    printf '  result:  %s (exit status %s)\n' "$result" "$status"
    printf '  when:    %s\n' "$when"
    printf '  inspect: journalctl --user -u %s -n 50 --no-pager\n' "$unit"
    printf '  --- last 20 journal lines from %s ---\n' "$unit"
} >&2

journalctl --user -u "$unit" -n 20 --no-pager -o cat >&2 || \
    printf '  (journal unavailable)\n' >&2

if command -v notify-send >/dev/null 2>&1; then
    notify-send \
        --urgency=critical \
        --app-name=organize \
        "organize pipeline failed" \
        "$unit — $result (exit $status). journalctl --user -u $unit" \
        >/dev/null 2>&1 || \
        printf '  (notify-send present but could not reach a session bus)\n' >&2
else
    printf '  (notify-send not installed — journal marker only)\n' >&2
fi

exit 0
