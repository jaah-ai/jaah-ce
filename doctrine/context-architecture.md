# Context architecture

Keep a repository's files — code, docs and agent instructions — easy to trust and cheap to read.

## One fact, one file
Every fact lives in one file, its home. Anything else that needs it points there (a link or a path) instead
of restating it: copies drift. When you find a copy, collapse it into the home and leave a pointer. Agent
instruction files (`CLAUDE.md`, `AGENTS.md`) stay short and route to the detail.

## Secrets and instance data stay out of reusable files
Never put a credential, token, key or connection string in a file; name the variable, not its value. Values
of one deployment, machine or person (hostnames, account ids, local paths) belong in configuration or the
environment, not in reusable docs, templates or code.

## Docs describe the present
Write what is true now: no history ("we used to…") and no dated status in a living doc.

## Delete what is retired
Delete a working file — plan, script, note, doc — once it is no longer used, rather than archive it or mark it
obsolete: git history is the archive. Remove the pointers to it in the same change.
