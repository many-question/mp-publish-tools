# Mega Precision publish runtime

Controlled update channel: `refs/heads/stable`.

Version 3.2.0 reads the report format from the workspace (`.mp-publish/profile.json`, written when a run is
set up) or from this release (`release.json` → `profile`, here the Mega Precision format), so other projects can
share the channel. Validation results for Mega Precision reports are unchanged.
Build source: many-question/agent-research-platform, `python arp.py --project <mega-precision> publish-package build`.

This repository contains tool code only. Keep agent config, transcripts, credentials and databases in their own workspaces.
Clients configure update.url to https://github.com/many-question/mp-publish-tools.git and update.ref to refs/heads/stable.
First install the workspace-local launcher from the manager package; this channel updates the runtime only.

Run `python bin/publish.py --update-only` to check and install without publishing research.
Normal publish/backfill checks this channel at invocation; status/check do not fetch updates.
