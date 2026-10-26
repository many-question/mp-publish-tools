# Mega Precision publish runtime

Controlled update channel: `refs/heads/stable`.

Version 3.2.0 reads the report format from the workspace (`.mp-publish/profile.json`, written when a run is
set up) or from this release (`release.json` → `profile`, here the Mega Precision format), so other projects can
share the channel. Validation results for Mega Precision reports are unchanged.
Version 3.3.0 (projection skeleton-3) also collects a Claude desktop session family: the host session, the
execution sessions and forks it started, their subagents, the session registry and the shared notes folder.
Only the host session publishes formal reports; execution sessions hand their results back to the host.
Launchers installed before 3.3.0 accept only skeleton-1/2 and keep using their previous runtime (they print a
notice that the tool update failed); reinstall the launcher with `arp publish-package install --workspace <ws>`
to switch. See docs/publish/v3.3-multi-session.md in the build source.
Version 3.3.1 also finds the desktop session registry and shared notes in the newer Windows data folder
(%LOCALAPPDATA%\Claude-Data\<profile>\) as well as the older %APPDATA%\Claude.
Build source: many-question/agent-research-platform, `python arp.py --project <mega-precision> publish-package build`.

This repository contains tool code only. Keep agent config, transcripts, credentials and databases in their own workspaces.
Clients configure update.url to https://github.com/many-question/mp-publish-tools.git and update.ref to refs/heads/stable.
First install the workspace-local launcher from the manager package; this channel updates the runtime only.

Run `python bin/publish.py --update-only` to check and install without publishing research.
Normal publish/backfill checks this channel at invocation; status/check do not fetch updates.
