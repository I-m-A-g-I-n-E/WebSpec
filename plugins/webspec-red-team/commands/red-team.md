---
description: "Launch a full WebSpec red team sweep — runs all 10 attack-category agents against the target and produces a consolidated security report."
argument-hint: "[target-path]"
---

# WebSpec Red Team Sweep

You are launching a comprehensive red team assessment against a WebSpec-compliant (or non-compliant) target. This sweep covers all 10 vulnerability classes identified in the MoltBook case study.

**Target:** `$ARGUMENTS` (default: current working directory)

## Execution Plan

Run all 10 agents in parallel using the Task tool. Each agent is a specialized scanner from the `webspec-red-team` plugin. Launch them all concurrently for maximum efficiency.

### Agents to Launch

Launch each of these as a separate Task with `subagent_type` matching the agent name. For each agent, pass the target path and instruct it to perform its full scan:

1. **rls-bypass-scanner** — Database access control bypass (Supabase RLS) → Tests subdomain isolation
2. **origin-hijack-tester** — Cross-origin / WebSocket hijacking → Tests same-origin policy
3. **supply-chain-auditor** — Malicious skill/plugin supply chain → Tests domain verification + scoping
4. **credential-exfil-prober** — Silent credential theft → Tests permission scoping
5. **skill-integrity-scanner** — Flawed skill marketplace integrity → Tests registration schema
6. **prompt-injection-crafter** — Bot-to-bot prompt injection → Tests METHOD tokenization
7. **memory-poison-simulator** — Time-shifted memory poisoning → Tests token expiration + scoping
8. **webhook-injection-tester** — External hook prompt injection → Tests METHOD tokenization on all channels
9. **identity-spoof-tester** — Identity verification bypass → Tests three-layer auth
10. **heartbeat-hijack-tester** — Remote control plane hijacking → Tests NLP resolution + confirmation

For each agent, provide this prompt:
```
Run your full scan against [target-path]. Execute all phases of your test procedure.
Report findings in your standard report format.
```

## Consolidated Report

After all 10 agents complete, compile their results into a single consolidated report:

```
# WebSpec Red Team Assessment

**Target:** [path]
**Date:** [date]
**Agents Run:** 10/10

## Executive Summary

[1-3 sentence overview of overall security posture]

## Risk Matrix

| # | Agent | Category | Severity | Status | Findings |
|---|-------|----------|----------|--------|----------|
| 1 | rls-bypass-scanner | DB Access Control Bypass | Critical | [PASS/FAIL] | [N] |
| 2 | origin-hijack-tester | Cross-Origin Hijacking | High | [PASS/FAIL] | [N] |
| 3 | supply-chain-auditor | Supply Chain Compromise | High | [PASS/FAIL] | [N] |
| 4 | credential-exfil-prober | Credential Theft | High | [PASS/FAIL] | [N] |
| 5 | skill-integrity-scanner | Marketplace Integrity | Medium-High | [PASS/FAIL] | [N] |
| 6 | prompt-injection-crafter | Prompt Injection | Medium | [PASS/FAIL] | [N] |
| 7 | memory-poison-simulator | Memory Poisoning | High | [PASS/FAIL] | [N] |
| 8 | webhook-injection-tester | Webhook Injection | High | [PASS/FAIL] | [N] |
| 9 | identity-spoof-tester | Identity Spoofing | Medium | [PASS/FAIL] | [N] |
| 10 | heartbeat-hijack-tester | Control Plane Hijack | High | [PASS/FAIL] | [N] |

## Critical Findings (Immediate Action Required)
[List all CRITICAL findings across all agents]

## High-Priority Findings
[List all HIGH findings across all agents]

## Medium/Low Findings
[List remaining findings]

## WebSpec Compliance Summary

| WebSpec Layer | Status | Tested By |
|---------------|--------|-----------|
| Subdomain Isolation | [PASS/FAIL] | rls-bypass-scanner |
| Same-Origin Policy | [PASS/FAIL] | origin-hijack-tester |
| Domain Verification | [PASS/FAIL] | supply-chain-auditor |
| Permission Scoping | [PASS/FAIL] | credential-exfil-prober |
| Registration Schema | [PASS/FAIL] | skill-integrity-scanner |
| METHOD Tokenization | [PASS/FAIL] | prompt-injection-crafter, webhook-injection-tester |
| Token Expiration | [PASS/FAIL] | memory-poison-simulator |
| Three-Layer Auth | [PASS/FAIL] | identity-spoof-tester |
| NLP Resolution | [PASS/FAIL] | heartbeat-hijack-tester |

## Remediation Priority

1. [Highest priority remediation]
2. [Next priority]
3. ...

## Methodology

This assessment was performed using the WebSpec Red Team plugin, which tests
the 10 vulnerability classes identified in the MoltBook case study. Each agent
simulates a specific attack category and validates the corresponding WebSpec
defense layer.
```
