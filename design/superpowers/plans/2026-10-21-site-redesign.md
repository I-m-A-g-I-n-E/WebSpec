# Site redesign and editorial pass (2026-10-21)

Goal: a spec site that a newcomer understands in a minute and enjoys reading, ready to share
publicly on 2026-10-22. The October rewrite made the text correct; this pass makes it
legible, shorter, and distinctive.

## Where things stood

- 2026-10-05: spec rewritten as Spec (normative, rule IDs) and Guide tabs; walkthrough
  generated from a real run; CI builds the site strictly.
- 2026-10-06/07: deployment stack merged (PRs #15–#22). Open: #23 (pin every listed tool,
  TC-6) and #24 (macOS dry-run test).
- The site is stock MkDocs Material (deep purple). The landing page is a 2,000-word overview
  that mixes the pitch with normative detail; "WebSpec and MCP", the status table, and the
  security-dial table each appear twice.

## Design

Concept: security printing. A WebSpec request is held to a bank check's standard: the amount is
written twice (method and definer), it is signed (guard), numbered so it is paid once
(`Idempotency-Key`), cleared (clearance), and countersigned when it matters (level 4). The
visual language follows: paper and ink, a guilloché rosette, hairline rules, and five
"part colors" for host, method, path, definer, and arguments, used consistently on every page.

- Type: Instrument Serif (display), Instrument Sans (text), IBM Plex Mono (code).
- Light "paper" and dark "ink" schemes, following the system setting.
- Home: hero with the request as a specimen, "read it like a sentence" anatomy with each part's
  real refusal, an interactive level dial (0–4) whose headers are real demo-key signatures,
  where the gateway sits, what it stops and doesn't, status, and entry points.
- Rule IDs become linkable badges (build-time hook).

## Information architecture

- Home (`index.md`): short, visual.
- Guide: Quickstart, **How it works** (the overview's conceptual tour, tightened), walkthrough,
  annotating, approval, deploying, threat model.
- Spec: **About this spec** (conventions, conformance), addressing, methods, levels, audit and
  deployment, status.

## Editorial rules

- Each idea once, in its home; elsewhere link to it.
- Normative text keeps its meaning and rule IDs; edits there are for clarity only.
- Every status code quoted on Home and How it works is checked against the real gateway
  (`gateway/tests/test_overview.py`).
- Cut what does not help a reader use or evaluate WebSpec (the Apple parallel; the duplicate
  MCP comparison).

## Verification

`mkdocs build --strict`, `pytest docs/tests gateway/tests/test_overview.py
gateway/tests/test_examples.py`, screenshots at 1440 and 390 px in both schemes, and a
fresh-eyes review of the new copy against the spec.
