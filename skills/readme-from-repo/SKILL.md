---
name: readme-from-repo
description: >
  Explore a repository read-only, then write or update its README.md using audience-matched
  templates and README best practice. Use when asked to write, create, generate, update, refresh, or
  review a README, or to "document this repo". Combines a thorough whole-repo explanation (purpose,
  architecture, entry points, data flow, operator touchpoints) with a proven README structure.
  Defaults to an Internal/team README; also supports Open Source, Personal, and Config types.
  Never invents facts: unverified items are marked TODO.
---

# README From Repo

Turn what the repo actually contains into a README that answers its reader's questions. The first
half is investigation (what the repo is and how it works); the second half is writing (structure,
tone, accuracy).

Template ideas adapted from the MIT-licensed `crafting-effective-readmes` skill
(https://github.com/softaworks/agent-toolkit/tree/main/skills/crafting-effective-readmes).

## Golden Rules

1. **Verified facts only.** Every command, path, entry point, env var, version, and URL must come from
   a file you actually read. If you cannot verify it, write `TODO: confirm` instead of guessing. Never
   invent setup steps.
2. **Read-only until approved.** Explore freely. Do not create or modify any file until the user has
   approved the outline and draft.
3. **Never overwrite wholesale.** If a README exists, switch to update mode (see Step 6).
4. **Write for the reader.** Always ask: who reads this, and what do they need to know?
5. **Lead with value.** The first 1-2 sentences say what it is and why it exists.

## Workflow

### Step 1 - Explore the repo (read-only)

Read enough to explain the whole codebase. Cover:

- **What it is and why it exists** - purpose and who uses it.
- **Architecture** - main components and how they fit together.
- **Entry points and build system** - how it is built, run, and tested; where execution starts.
- **Key modules and data/control flow** - how information moves between the important pieces.
- **Moving parts an operator/developer touches day to day** - configs, pipelines, scripts, envs.

Where to look: existing README/docs, build and package manifests (`Makefile`, `package.json`,
`pyproject.toml`, `go.mod`, `pom.xml`, `build.gradle`), CI config (`.github/workflows`,
`Jenkinsfile`, `bamboo-specs`, `azure-pipelines.yml`), IaC (`terraform`, `helm`, `Dockerfile`,
`docker-compose`), env/config samples, and top-level directory layout. Use the Explore subagent for
large repos.

### Step 2 - Pick the README type

| Type | Audience | Template |
|------|----------|----------|
| **Internal** (default) | Teammates, new hires, on-call; assume a senior DevOps/DevSecOps reader | [templates/internal.md](./templates/internal.md) |
| **Open Source** | Contributors and users worldwide | [templates/oss.md](./templates/oss.md) |
| **Personal** | Future you, portfolio viewers | [templates/personal.md](./templates/personal.md) |
| **Config** | Future you, confused (dotfiles, config/script dirs) | [templates/xdg-config.md](./templates/xdg-config.md) |

Default to **Internal**. Ask the user only when the repo gives no signal (for example a public
license, `CONTRIBUTING.md`, and published package suggest OSS; a dotfiles-style layout suggests
Config). State your recommendation with the reason.

Also ask, briefly: the one-sentence problem it solves, and the quickest path to "it works", if you
could not determine them from the repo.

### Step 3 - Propose the outline (approval gate)

Using [section-checklist.md](./section-checklist.md), show:

- The chosen type and why.
- The section list, with a one-line note on what each will contain and which files it comes from.
- Anything you could not verify (will become `TODO: confirm`).

Wait for approval or changes before drafting.

### Step 4 - Draft

Fill the template from verified facts, following [style-guide.md](./style-guide.md). Rules:

- Prefer real, copy-pasteable commands taken from the repo's own scripts/CI.
- Check every command against Makefile, package manifest, CI config, or similar where possible.
- Include **one** Mermaid architecture diagram only when 3 or more components interact; keep it small.
  Skip it for simple repos.
- Use headers, tables, and lists; avoid walls of text.
- Link to deeper docs instead of duplicating them.
- Drop template sections that do not apply rather than leaving empty placeholders.
- Mark unknowns as `TODO: confirm`.
- Add a "Last reviewed: YYYY-MM-DD" line for Internal and Config types.

Show the draft to the user and ask for approval.

### Step 5 - Always ask

After the draft: **"Anything else to highlight or include that I might have missed?"**

### Step 6 - Write

After approval, write `README.md` at the repo root.

**If a README already exists (update mode):**

1. Read it and compare against the actual project state.
2. Flag stale or wrong sections.
3. Propose specific edits as a diff-style summary and wait for approval.
4. Apply targeted edits that preserve existing content the user wants kept. Do not replace the file
   wholesale.

### Step 7 - Verify

Re-check the written README: commands and paths exist, links resolve, no leftover template
placeholders other than intentional `TODO: confirm` markers. Report any remaining TODOs to the user.

## Best-Practice Summary

- **Essential sections for every README:** Name, Description (what + why), Usage with examples.
- **Common mistakes:** missing install steps, no examples, wall of text, stale content, generic tone.
- **Keep it current:** date-stamp Internal and Config READMEs; refresh when capabilities change.
- **Standard-readme ordering** (when in doubt): title, short description, install, usage,
  contributing, license.
