---
name: Create Plugin
description: Design a new Athena server plugin through a grill, then produce a spec and implementation tickets. Use when the developer wants to create a plugin, add an integration, or expose new Tools to Gemini. Does not write plugin code.
disable-model-invocation: true
---

# Create an Athena plugin

This skill turns a plugin idea into a reviewed spec and a set of implementation tickets. It
does **not** write plugin code, run tests, create branches, or assign work: the developer
implements afterwards.

## Read first

Read these four references before doing anything else:

- `references/prerequisites.md` — required skills, install and restore commands, tracker docs.
- `references/architecture.md` — Athena in one page, then where a plugin sits.
- `references/rules.md` — the design constraints a plugin must satisfy.
- `references/recipe.md` — the anatomy of a plugin, illustrative snippets, and what the spec
  must capture.

## Workflow

### 1. Check prerequisites

Confirm that `grill-with-docs`, `to-spec`, and `to-tickets` are in your available skills, and
that `docs/agents/issue-tracker.md` and `docs/agents/triage-labels.md` exist. If anything is
missing, stop and give the exact command from `references/prerequisites.md`. Do not download
anything yourself and do not fall back to a degraded interview.

### 2. Ask for the name and the purpose

Ask one round, two fields:

- **Name**: the plugin directory name, kebab-case, matching the manifest `name`.
- **Purpose**: one sentence, user-facing: "The user can now ...".

Check that `plugins/<name>/` does not exist. A plugin name must be unique; the loader also
requires the manifest name to match the directory.

Read `plugins/README.md` before going further: it states the mandatory shape of a plugin
folder and its amovibility rule. The spec must produce that shape — no test belongs in
`server/tests/`, and no plugin-specific document belongs in the root `docs/`.

Do not ask the rest of the plugin questions yourself. The grill owns them.

### 3. Run the grill

Load the `grill-with-docs` skill with the name and purpose in context. Let it run its rounds;
it will interview the developer, challenge the vocabulary against `GLOSSARY-MAP.md` and the
relevant glossaries, and write ADRs or glossary entries when a decision crystallises. Plugin
terms go into `plugins/<name>/GLOSSARY.md` and plugin decisions into
`plugins/<name>/docs/adr/`, numbered locally from `0001`; central ADRs are for the system.

Before the grill closes, list the proposed Tool names and check that none is already owned by
the kernel or another plugin (`server/core`, `plugins/*/plugin.py`). A Tool name collision is a
fatal loader error, so it must be resolved before the spec.

### 4. Produce the spec

When the grill has no open branches, ask: "Shall I turn this into a spec?" If the developer
says yes, load `to-spec`.

The spec must contain, inside *Implementation Decisions*:

- a **Plugin decisions** block: manifest, platform (`server`), Tools and schemas, non-secret
  config, secret names, startup probe, retryable vs fatal failures, degraded start, traces,
  events, activation, Console visibility;
- a **Plugin folder** block: the mandatory shape from `plugins/README.md` — `plugin.json`,
  `plugin.py`, `README.md` (activation and secrets), `GLOSSARY.md`, `tests/`, `docs/` with
  the smoke test and locally numbered ADRs — and the vocabulary or decisions it introduces;
- a **Tool contract** block per Tool: name, description, parameters, what the model must and
  must not do, and the error shape it returns.

If `to-spec` omits them, add them before publishing. Publish only after explicit confirmation.

### 5. Produce the tickets

After the spec is published, ask: "Shall I break this into tickets?" If the developer says
yes, load `to-tickets` and let it do its job: it owns the slicing, the blocking edges, and the
publication mechanics.

Do not pre-slice the work, do not assign tickets, and do not close the spec issue.

### 6. Stop

Summarise the spec and ticket links. State that implementation is out of scope for this skill.
Do not create a branch, write a plugin file, or run a test.

## Gates

- **No code.** No `plugin.json`, no `plugin.py`, no test, no branch.
- **No verification.** Do not run `pytest`, a smoke test, or a secret scan.
- **No automatic install.** Missing skills are the developer's install step.
- **No hardcoded tracker.** Read `docs/agents/` and defer to `to-spec` / `to-tickets`.
- **No publication without confirmation.** Ask before creating the spec and before creating
  the tickets.
- **No kernel change.** If the plugin needs a new kernel capability, stop: that is a kernel
  feature first, with its own spec and ADR.

## Language

Write the skill and its references in English. Reply to the developer in their language
(French in this repository). Keep every string that Athena will say to an end user in French.
