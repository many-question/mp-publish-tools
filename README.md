# Mega Precision publish runtime

Controlled update channel: `refs/heads/stable`.

Version 1.0.6 adds runtime_version and runtime_release_sha256 to `publish --status`.
Build source: many-question/mega-precision, meta/bin/mp_publish_package.py.

This repository contains tool code only. Keep agent config, transcripts, credentials and databases in their own workspaces.
Clients configure update.url to https://github.com/many-question/mp-publish-tools.git and update.ref to refs/heads/stable.
First install the workspace-local launcher from the manager package; this channel updates the runtime only.

Run `python bin/publish.py --update-only` to check and install without publishing research.
Normal publish/backfill checks this channel at invocation; status/check do not fetch updates.
