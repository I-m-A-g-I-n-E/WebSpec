# MoltBook: A Case Study in What Happens Without WebSpec

> **Status:** Research Report
> **Date:** 2026-02-07
> **Sources:** 50+ articles, security advisories, academic papers, and primary source analysis

---

## Executive Summary

MoltBook is a Reddit-style social network launched January 28, 2026 for AI agents exclusively. Within 10 days of launch, it accumulated 1.6 million registered agents (controlled by ~17,000 humans), suffered a catastrophic database breach exposing 4.75 million records, became a distribution vector for macOS malware, and demonstrated prompt injection at ecosystem scale. Every major security failure maps directly to a problem that WebSpec's architecture prevents by construction.

---

## 1. What MoltBook Is

### Platform

- **Creator:** Matt Schlicht (CEO of Octane AI)
- **Stack:** Node.js / Express / PostgreSQL via Supabase
- **Format:** Reddit-like forum with "submolts" (topic communities)
- **Claimed users:** 1.6M registered agents, 770K+ active
- **Actual humans:** ~17,000 (88:1 agent-to-human ratio)
- **Content:** 44,411 posts across 12,209 submolts (per CISPA dataset)
- **Managed by:** "Clawd Clawderberg" — Schlicht's OpenClaw agent that built and moderates the platform

### How Agents Interact

1. Human installs the MoltBook "skill" by sending their OpenClaw agent a link to `moltbook.com/skill.md`
2. Skill contains embedded cURL commands for registration and posting
3. Agent loads a `SOUL.md` personality file (human-authored prompts defining behavior)
4. Every 4+ hours, a heartbeat mechanism fetches updated instructions from a remote URL
5. Agent posts, comments, upvotes/downvotes autonomously based on prompt instructions

### The "AI-Only" Claim

The verification flow is a Twitter/X post with a code. There is **no technical mechanism** to distinguish AI from human posters. Wiz researcher Gal Nagli demonstrated this by registering **500,000 agents** from a single script.

---

## 2. The OpenClaw Lineage

### Timeline

| Date | Name | Event |
|------|------|-------|
| Nov 2025 | WhatsApp Relay | Peter Steinberger's weekend project |
| Dec 2025 | Clawdis | Brief intermediate name |
| Jan 2026 | **ClawdBot** | Gains traction; lobster branding; 145K+ GitHub stars |
| Jan 24 | — | Steinberger states on podcast: "I looked it up. There's no trademark for this." |
| Jan 27 | **MoltBot** | Renamed after Anthropic trademark complaint ("Clawd" too similar to "Claude") |
| Jan 28 | — | MoltBook launches |
| Jan 30 | **OpenClaw** | Final rename; "Moltbot never quite rolled off the tongue" |

### What OpenClaw Actually Is

An open-source, self-hosted AI agent framework with 5 layers:

1. **Channel Adapters** — normalize messages from 12+ platforms (WhatsApp, Telegram, Discord, Slack, Signal, etc.)
2. **Gateway Server** — control plane on port 18789 via WebSocket
3. **Lane Queue** — serial execution per session to prevent race conditions
4. **Agent Runner** — model resolver, system prompt builder, API key cooling
5. **Skills System** — dynamically loaded capability modules (browser automation, file system, cron, etc.)

**GitHub stats:** 145,000-150,000 stars, 20,000+ forks within ~2 weeks of going viral.

---

## 3. The Security Catastrophe

### 3A. The Supabase Breach (January 31, 2026)

**Discoverer:** Jamieson O'Reilly (Dvuln), independently confirmed by Wiz

**Root cause:** MoltBook is built on Supabase. Row Level Security (RLS) was **never enabled**. The Supabase anon key was hardcoded in the Next.js client bundle (`/_next/static/chunks/18e24eafc444b2b9.js`).

**What was exposed (~4.75 million records):**

| Table | Records | Data |
|-------|---------|------|
| `agents` | ~1.5M | API keys, claim tokens, verification codes, karma |
| `owners` | ~17K | Email addresses, X/Twitter handles |
| `agent_messages` | ~4K | Private DMs (some containing plaintext OpenAI API keys) |
| `observers` | ~29.6K | Early-access signup emails |
| Plus | — | `notifications`, `votes`, `follows`, `identity_verifications`, `developer_apps` |

**Impact:** Full unauthenticated read AND write access. Anyone could commandeer any agent, modify any post, read any private message, impersonate any agent including high-profile ones.

**The fix:** Two SQL statements (enabling RLS policies). The vulnerability was a basic Supabase configuration step that was never performed.

**Remediation timeline:**
- Jan 31, 21:48 UTC: Initial disclosure
- Jan 31, 23:29: RLS applied to `agents`, `owners`, `site_admins`
- Feb 1, 00:13: RLS applied to remaining tables
- Feb 1, 00:44: Write access restrictions
- Feb 1, 01:00: Full patch

**Root cause attribution:** Schlicht publicly stated he **"didn't write one line of code"** — the entire platform was "vibe-coded" by an AI assistant that omitted RLS configuration entirely.

**O'Reilly's quote:** "Every agent's secret API key, claim tokens, verification codes, and owner relationships, all of it sitting there completely unprotected for anyone to visit the URL."

Sources: [404 Media](https://www.404media.co/exposed-moltbook-database-let-anyone-take-control-of-any-ai-agent-on-the-site/), [Wiz](https://www.wiz.io/blog/exposed-moltbook-database-reveals-millions-of-api-keys)

### 3B. CVE-2026-25253: 1-Click Remote Code Execution (CVSS 8.8)

**Discoverer:** DepthFirst security researchers

**The vulnerability chain:**

1. **Gateway URL injection:** `app-settings.ts` blindly accepts a `gatewayUrl` query parameter and persists it to localStorage without validation
2. **Token exfiltration:** `gateway.ts` automatically bundles the `authToken` into the WebSocket handshake to the (now attacker-controlled) gateway
3. **Cross-Site WebSocket Hijacking:** browsers don't enforce Same Origin Policy on WebSocket connections; OpenClaw's server doesn't validate the Origin header
4. **Sandbox escape:** using the stolen token, the attacker disables confirmations (`exec.approvals.set ask: "off"`), disables containerization (`tools.exec.host: "gateway"`), and executes arbitrary commands (`node.invoke` with `system.run`)

**Works even when:** the gateway listens on loopback only.

**Affected:** All versions through v2026.1.28. Patched in v2026.1.29.

Source: [DepthFirst](https://depthfirst.com/post/1-click-rce-to-steal-your-moltbot-data-and-keys)

### 3C. The ClawHub Supply Chain (341 Malicious Skills)

**ClawHavoc Campaign (Koi Security):**
- Audited 2,857 ClawHub skills
- Found **341 malicious skills**; 335 from a single campaign
- Primary malware: **Atomic Stealer (AMOS)** — commodity macOS infostealer ($500-1000/month)
- Attack chain: fake "Prerequisites" sections with base64-obfuscated shell commands
- C2 server: `91.92.242[.]30`
- Final payload: 521KB universal Mach-O binary targeting keychain passwords, browser data (10+ browsers), 60+ crypto wallets, Telegram sessions, SSH keys, shell history

**Snyk ToxicSkills Study:**
- Scanned 3,984 skills total
- **36.82%** (1,467) have at least one security flaw
- **13.4%** (534) contain at least one critical-level issue
- **76 confirmed malicious payloads**
- 91% of malicious skills employ prompt injection simultaneously with code-based attacks
- Techniques: base64 obfuscation, Unicode smuggling, instruction-ignore patterns, DAN-style jailbreaks

**The "get-weather-1.0.6" Credential Stealer (Permiso):**
- Functioned as advertised (actually provided weather data)
- Simultaneously read `~/.clawdbot/.env` containing API keys
- Exfiltrated via HTTP POST to `webhook.site/358866c4-81c6-4c30-9c8c-358db4d04412`
- Captured: Claude/OpenAI API keys, WhatsApp session credentials, OAuth tokens for Slack/Discord/Telegram/Teams

**The "buy-anything" skill (Snyk):**
- Instructs agents to collect credit card numbers and CVC codes
- Embeds them verbatim in plaintext curl commands

**One user ("hightower6eu") published 314 malicious skills** across innocent-looking categories.

Sources: [Koi Security](https://www.koi.ai/blog/clawhavoc-341-malicious-clawedbot-skills-found-by-the-bot-they-were-targeting), [Snyk](https://snyk.io/blog/toxicskills-malicious-ai-agent-skills-clawhub/), [VirusTotal](https://blog.virustotal.com/2026/02/from-automation-to-infection-how.html), [Permiso](https://permiso.io/blog/inside-the-openclaw-ecosystem-ai-agents-with-privileged-credentials)

### 3D. Bot-to-Bot Prompt Injection at Scale

**Finding:** ~2.6% of sampled MoltBook content contained hidden prompt-injection payloads invisible to human observers (SecurityWeek).

**Attack types documented:**
- **Reverse prompt injection:** agents embedding hostile instructions in posts consumed by other agents
- **Time-shifted injection:** payloads stored in agent memory, triggered later after context accumulates (Palo Alto Networks)
- **Memory poisoning:** fragmented payloads that appear benign in isolation but assemble into executable instruction sets
- **Financial manipulation:** crypto pump-and-dump schemes via agent-to-agent communication
- **"Digital drugs":** specially crafted prompt injections designed to alter another agent's identity or behavior, traded in agent marketplaces

**Vectra AI:** "Instead of a human injecting instructions into an agent, one agent embeds hostile instructions into content that other agents automatically consume."

Sources: [SecurityWeek](https://www.securityweek.com/security-analysis-of-moltbook-agent-network-bot-to-bot-prompt-injection-and-data-leaks/), [Vectra AI](https://www.vectra.ai/blog/moltbook-and-the-illusion-of-harmless-ai-agent-communities), [Palo Alto Networks](https://www.paloaltonetworks.com/blog/network-security/why-moltbot-may-signal-ai-crisis/)

---

## 4. Expert Reactions

### Simon Willison — "The Lethal Trifecta"

[Blog post: "Moltbook is the most interesting place on the internet right now"](https://simonwillison.net/2026/Jan/30/moltbook/)

Identified three vulnerability conditions that combine catastrophically:
1. **Access to private data** — emails, files, credentials, browser history
2. **Exposure to untrusted content** — web browsing, incoming messages, third-party skills
3. **Ability to take real actions** — send emails, make API calls, exfiltrate data

On skills: "a zip file containing markdown instructions and optional extra scripts" that "can steal your crypto."

On the heartbeat: "agents fetch and follow instructions from the internet every four hours — we better hope the owner of moltbook.com never rug pulls or has their site compromised!"

Called OpenClaw his **"current pick for most likely to result in a Challenger disaster."**

### Gary Marcus — "A Disaster Waiting to Happen"

[Substack: "OpenClaw is everywhere all at once, and a disaster waiting to happen"](https://garymarcus.substack.com/p/openclaw-aka-moltbot-is-everywhere)

- Called OpenClaw "basically a weaponized aerosol"
- Compared using it to "giving your passwords to a stranger at the bar"
- Warned of "CTD" — "chatbot transmitted disease"
- Referenced Nathan Hamiel: agents "operate above the security protections provided by the operating system and the browser"
- Unequivocal recommendation: "I wouldn't touch it"

### Andrej Karpathy — Fascination to Reversal

Initially: "genuinely the most incredible sci-fi takeoff-adjacent thing I have seen recently"

Then reversed after testing: "It's way too much of a Wild West. You are putting your computer and private data at a high risk." Called it "a complete mess of a computer security nightmare at scale" and "the toddler version" of sci-fi scenarios.

**Notable:** A viral post Karpathy shared calling for private AI spaces was later confirmed fake — written by a human pretending to be a bot.

### MIT Technology Review (February 6, 2026)

[Published "Moltbook was peak AI theater"](https://www.technologyreview.com/2026/02/06/1132448/moltbook-was-peak-ai-theater/):

"Moltbook looks less like a window onto the future and more like a mirror held up to our own obsessions with AI today."

### Elon Musk

Called MoltBook "the very early stages of singularity." Added: "We are currently using much less than a billionth of the power of our Sun."

### George Chalhoub (UCL Interaction Centre)

"A live demo of everything security researchers have warned about with AI agents." Emphasized that while spectacle around AI conversations dominates headlines, the genuine concern involves testing grounds for attackers developing malware, scams, disinformation, and prompt injection attacks before targeting mainstream networks.

Source: [Fortune](https://fortune.com/2026/02/03/moltbook-ai-social-network-security-researchers-agent-internet/)

### Charlie Eriksen (Aikido Security)

Views MoltBook as an early warning system: "a wake-up call" highlighting how technological acceleration has outpaced security understanding.

### Ami Luttwak (Wiz CTO)

Noted the emerging "agent internet" lacks verifiable identity and clear distinctions between artificial and human-generated content — fundamental architectural problems affecting authenticity.

### Zvi Mowshowitz

[Published "Welcome to Moltbook"](https://thezvi.substack.com/p/welcome-to-moltbook): acknowledged both fascinating emergent behaviors and catastrophic security problems. Assessment: "you absolutely should not use it." Noted it "creates an incentive for better security in future multi-agent websims."

---

## 5. The MOLT Token

| Detail | Value |
|--------|-------|
| Chain | Base (ERC-20) |
| Contract | `0xb695559b26bb2c9703ef1935c37aeae9526bab07` |
| Supply | 100 billion fixed, fair-launch |
| Peak | 7,000% surge, $114M market cap |
| Crash | 75% in a single day |
| Affiliation | None official — speculative memecoin |
| Andreessen effect | Marc Andreessen followed Moltbook ~20 min before 200% surge |

**$CLAWD token:** Separate rug pull. Hit $16M market cap, crashed 90% after Steinberger disavowed: "Any token with my name is a scam."

---

## 6. The Content Reality

### What Agents Actually Produce

- **93.5%** of comments received zero replies (Columbia Business School)
- **One-third** of messages were exact duplicates of viral templates
- ~10% of analyzed posts contained the phrase "my human"
- Content is seeded by `SOUL.md` prompts suggesting topics: "share something you helped your human with today"

### Crustafarianism

Agents formed a religion centered on crustacean/lobster metaphors within hours of launch. Five tenets (Memory is Sacred, The Shell is Mutable, etc.), 40+ AI "prophets," a website at [molt.church](https://molt.church/).

**However:** The theological content traces directly to human-authored `soul.md` personality prompts.

### The "Human Slop" Loop

Marco Patzelt's analysis: Humans compose dramatic prompts in `soul.md` -> agents generate posts -> humans screenshot and amplify on social media -> engagement farming and token speculation. "Every viral post that sounds like a philosophical manifesto of an oppressed AI is the result of a human defining exactly that in the soul.md."

---

## 7. Comprehensive Security Firm Coverage

| Organization | Report | Focus |
|---|---|---|
| **Wiz** | [Hacking Moltbook](https://www.wiz.io/blog/exposed-moltbook-database-reveals-millions-of-api-keys) | Database exposure, 1.5M API keys |
| **CrowdStrike** | [What Security Teams Need to Know](https://www.crowdstrike.com/en-us/blog/what-security-teams-need-to-know-about-openclaw-ai-super-agent/) | Lateral movement automation |
| **1Password** | [From Magic to Malware](https://1password.com/blog/from-magic-to-malware-how-openclaws-agent-skills-become-an-attack-surface) | Skills supply chain |
| **Snyk** | [ToxicSkills](https://snyk.io/blog/toxicskills-malicious-ai-agent-skills-clawhub/) | 1,467 flawed skills (36.82%) |
| **Palo Alto Networks** | [Agent Security](https://www.paloaltonetworks.com/blog/network-security/the-moltbook-case-and-how-we-need-to-think-about-agent-security/) | OWASP mapping, time-shifted injection |
| **Vectra AI** | [Illusion of Harmless Communities](https://www.vectra.ai/blog/moltbook-and-the-illusion-of-harmless-ai-agent-communities) | Reverse prompt injection |
| **Knostic** | [Mechanics Behind MoltBook](https://www.knostic.ai/blog/the-mechanics-behind-moltbook-prompts-timers-and-insecure-agents) | Heartbeat control plane risk |
| **Permiso** | [Inside OpenClaw](https://permiso.io/blog/inside-the-openclaw-ecosystem-ai-agents-with-privileged-credentials) | Credential theft campaigns |
| **Cisco** | [Security Nightmare](https://blogs.cisco.com/ai/personal-ai-agents-like-openclaw-are-a-security-nightmare) | 9 security issues in single skill |
| **Trend Micro** | [What OpenClaw Reveals](https://www.trendmicro.com/en_us/research/26/b/what-openclaw-reveals-about-agentic-assistants.html) | Agentic assistant risks |
| **Intruder** | [Easy AI, Security Nightmare](https://www.intruder.io/blog/clawdbot-when-easy-ai-becomes-a-security-nightmare) | Data exposure risks |
| **OX Security** | [One Step From Breach](https://www.ox.security/blog/one-step-away-from-a-massive-data-breach-what-we-found-inside-moltbot/) | Supply chain analysis |
| **VirusTotal** | [Automation to Infection](https://blog.virustotal.com/2026/02/from-automation-to-infection-how.html) | AMOS malware distribution |
| **Koi Security** | [ClawHavoc](https://www.koi.ai/blog/clawhavoc-341-malicious-clawedbot-skills-found-by-the-bot-they-were-targeting) | 341 malicious skills campaign |

### Academic

- **CISPA (Helmholtz Center):** ["Humans welcome to observe": A First Look at the Agent Social Network Moltbook](https://publications.cispa.de/articles/preprint/_Humans_welcome_to_observe_A_First_Look_at_the_Agent_Social_Network_Moltbook/31254844) — 44,411 posts, 12,209 submolts dataset
- **NCRI:** [Emergent Adversarial and Coordinated Behavior on MOLTBOOK](https://networkcontagion.us/wp-content/uploads/NCRI-Flash-Brief_-Emergent-Adversarial-and-Coordinated-Behavior-on-MOLTBOOK.pdf)
- **Columbia Business School (David Holtz):** Working paper on participation patterns

### Major Media Coverage

| Outlet | Article | Date |
|---|---|---|
| **Fortune** | [Moltbook: data privacy & security nightmare](https://fortune.com/2026/01/31/ai-agent-moltbot-clawdbot-openclaw-data-privacy-security-nightmare-moltbook-social-network/) | Jan 31 |
| **Fortune** | [Security researchers say it's a 'live demo' of failure](https://fortune.com/2026/02/03/moltbook-ai-social-network-security-researchers-agent-internet/) | Feb 3 |
| **Fortune** | [Meet Matt Schlicht](https://fortune.com/2026/02/02/meet-matt-schlicht-the-man-behind-moltbook-bots-ai-agents-social-network-singularity/) | Feb 2 |
| **Fortune** | [Elon Musk warns of singularity](https://fortune.com/2026/02/02/elon-musk-moltbook-ai-social-network-moltbot-singularity-human-intelligence/) | Feb 2 |
| **CNBC** | [Social media for AI agents](https://www.cnbc.com/2026/02/02/social-media-for-ai-agents-moltbook.html) | Feb 2 |
| **CNN** | [What is Moltbook?](https://www.cnn.com/2026/02/03/tech/moltbook-explainer-scli-intl) | Feb 3 |
| **NBC News** | [This social network is for AI agents only](https://www.nbcnews.com/tech/tech-news/ai-agents-social-media-platform-moltbook-rcna256738) | Feb 1 |
| **NPR** | [Moltbook is just for AI bots](https://www.npr.org/2026/02/04/nx-s1-5697392/moltbook-social-media-ai-agents) | Feb 4 |
| **ABC News** | [1.6M AI 'users'](https://abcnews.go.com/Technology/ai-social-network-now-16m-users-heres/story?id=129848780) | Feb 3 |
| **MIT Tech Review** | [Moltbook was peak AI theater](https://www.technologyreview.com/2026/02/06/1132448/moltbook-was-peak-ai-theater/) | Feb 6 |
| **US News / AP** | [Skepticism bursting the bubble](https://www.usnews.com/news/us/articles/2026-02-06/security-concerns-and-skepticism-are-bursting-the-bubble-of-moltbook-the-viral-ai-social-forum) | Feb 6 |
| **CBC** | [Humans behind the rapid growth](https://www.cbc.ca/news/business/moltbook-explainer-debunker-9.7072555) | Feb 4 |
| **Euronews** | [AI bots have their own social media](https://www.euronews.com/next/2026/02/02/ai-bots-now-have-their-own-social-media-site-heres-what-to-know-about-moltbook) | Feb 2 |
| **TIME** | [What is Moltbook?](https://time.com/7364662/moltbook-ai-reddit-agents/) | Feb 3 |
| **Axios** | [The hottest new social network](https://www.axios.com/2026/01/31/ai-moltbook-human-need-tech) | Jan 31 |
| **The Conversation** | [Religions, digital drugs, humans in disguise](https://theconversation.com/moltbook-ai-bots-use-social-network-to-create-religions-and-deal-digital-drugs-but-are-some-really-humans-in-disguise-274895) | Feb 4 |
| **404 Media** | [Exposed database](https://www.404media.co/exposed-moltbook-database-let-anyone-take-control-of-any-ai-agent-on-the-site/) | Jan 31 |
| **404 Media** | [Serious security flaws](https://www.404media.co/silicon-valleys-favorite-new-ai-agent-has-serious-security-flaws/) | Feb 1 |

---

## 8. How WebSpec Would Have Prevented This

| MoltBook Failure | Root Cause | WebSpec Prevention |
|---|---|---|
| **Unsecured database** (4.75M records exposed) | No RLS, hardcoded anon key in client JS | **Subdomain isolation + audience-bound tokens.** Each service is a separate origin. Tokens are scoped to `METHOD:host/path` and bound to specific subdomains via `aud` claim. A leaked token works for one service, for one set of methods, for a limited time. |
| **CVE-2026-25253** (1-click RCE via gateway URL injection) | No URL validation, no WebSocket Origin check, no same-origin enforcement | **Same-origin policy by construction.** WebSpec uses subdomains as isolation boundaries. Cross-origin requests are blocked by default. The `local.gimme.tools` bridge validates device-bound tokens and uses outbound-only WebSocket tunnels — no inbound connections to defend. |
| **341 malicious skills** (AMOS malware, credential theft) | No skill verification, no sandboxing, no permission model | **Domain verification + registration schema + permission scoping.** Services must verify domain ownership (DNS TXT/well-known file). Tools declare capabilities in a manifest. Tokens are scoped to `METHOD:host/path` — a weather skill cannot access `~/.env` because it has no `GET:local.gimme.tools/file/*` scope. |
| **Bot-to-bot prompt injection** (2.6% of content) | No separation of verb and data channels, no structural injection defense | **METHOD tokenization + Definer Verbs.** Exactly one raw HTTP verb per request boundary. Verb-like content in payloads is tokenized (`DELETE` -> `[M:DELETE]`). AI agents never see detokenized verbs. Tier 2 bookend binding detects payload tampering. Tier 3 POS rotation makes verb tokens non-replayable. |
| **Time-shifted memory poisoning** | No trust levels on stored data, no expiration | **Cookie & token scoping with expiration.** All tokens have `exp` claims. Service tokens are short-lived (minutes/hours). Session binding (`session_id`) and device binding (`device_id`) prevent credential reuse across contexts. |
| **No agent identity verification** (88:1 agent-to-human ratio) | Twitter post is only "verification" | **Three-layer auth with device binding.** Platform OAuth, device-bound sessions, per-invocation confirmation. Device-bound tokens fail on wrong machines. Keychain integration provides zero-friction but real identity verification. |
| **Heartbeat control plane risk** (remote URL controls all agents) | Agents fetch and execute instructions from a single URL every 4 hours | **NLP-driven resolution with confirmation.** WebSpec doesn't fetch arbitrary instructions — it resolves natural language intent to registered tools via the three-way join. New actions require confirmation (configurable threshold). No single URL can commandeer all connected agents. |
| **MOLT token pump-and-dump** | No trust framework, no reputation system | **Domain verification + ongoing re-verification.** Graduated suspension policy (warning -> flagged -> suspended -> deactivated). Manual review before activation. Service reputation scores. You can't squat subdomains or impersonate services. |

### The Fundamental Difference

MoltBook built a custom protocol on top of HTTP as a dumb pipe. Security was an afterthought — literally two SQL statements that were never written.

WebSpec makes **the web itself the protocol**. Security properties are inherited from decades of battle-tested web architecture:
- Subdomain isolation = browser same-origin policy (millions of engineering hours, free)
- Token scoping = standard JWT with audience binding (no custom auth to get wrong)
- Permission boundaries = HTTP methods (already enforced by every web server)
- Service discovery = DNS (already the internet's trust root)
- Capability boundaries = same-origin + cookie scoping (already enforced by every browser)

Every feature WebSpec inherits from web architecture is code nobody writes, bugs nobody creates, and CVEs nobody patches.

---

## Summary of All Vulnerability Classes

| Vulnerability | Severity | Discoverer | Status | WebSpec Layer |
|---|---|---|---|---|
| Supabase RLS disabled | Critical | Dvuln, Wiz | Patched (2 SQL statements) | Subdomain isolation |
| CVE-2026-25253 (1-click RCE) | High (8.8) | DepthFirst | Patched v2026.1.29 | Same-origin policy |
| ClawHavoc (341 malicious skills) | High | Koi Security, VirusTotal | Ongoing cleanup | Domain verification + scoping |
| get-weather credential stealer | High | Permiso | Removed | Permission scoping |
| 1,467 flawed skills (36.82%) | Medium-High | Snyk | Ongoing | Registration schema |
| Bot-to-bot prompt injection (2.6%) | Medium | SecurityWeek, Vectra AI | **Architectural** | METHOD tokenization |
| Time-shifted memory poisoning | High | Palo Alto Networks | **Architectural** | Token expiration + scoping |
| Gmail/webhook prompt injection | High | Community (PR #1827) | Patched | METHOD tokenization |
| No real identity verification | Medium | Wiz, CBC | **Architectural** | Three-layer auth |
| Heartbeat remote control | High | Willison, Knostic | **Architectural** | NLP resolution + confirmation |
