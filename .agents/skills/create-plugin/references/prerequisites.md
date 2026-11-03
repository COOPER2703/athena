# Prerequisites

## Required skills

The workflow delegates the interview to `grill-with-docs` and the deliverables to `to-spec`
and `to-tickets`. They are installed together with their dependencies:

- `grill-with-docs` (which loads `grilling` and `domain-modeling`)
- `to-spec`
- `to-tickets`

Check that they appear in the available skill list. Do not inspect only `.agents/skills/`:
a global installation in `~/.agents/skills/` is equally valid.

## Install

Run from the repository root:

```sh
npx skills add mattpocock/skills \
  -s grill-with-docs \
  -s grilling \
  -s domain-modeling \
  -s to-spec \
  -s to-tickets \
  -y
```

This installs project-level copies into `.agents/skills/<id>/` and writes `skills-lock.json`.

## Restore

If the directories were removed but `skills-lock.json` is present:

```sh
npx skills experimental_install
```

## Versioning

- `skills-lock.json` is versioned: it pins the source and the content hash of every skill.
- The five third-party directories are ignored by git (see `.gitignore`).
- `.agents/skills/create-plugin/` is this repository's own skill and is versioned.

## Tracker

The spec and tickets are published through the tracker configured by
`/setup-matt-pocock-skills`:

- `docs/agents/issue-tracker.md`
- `docs/agents/triage-labels.md`

If either file is missing, tell the developer to run `/setup-matt-pocock-skills` and stop.
Never guess a tracker, a label, or a command.

## Failure messages

When a prerequisite is missing, stop with a message that names:

1. the missing skill or file;
2. the exact command that fixes it;
3. the fact that the workflow cannot continue without it.

Do not download skills automatically. Do not run a degraded grill.
