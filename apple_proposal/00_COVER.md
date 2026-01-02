# WebSpec Protocol

## Technical Review Packet

---

**A Web-Native Tool Invocation Protocol**

*Designed to mesh with platform security rather than replace it*

---

### Contents

1. **Executive Summary** — The meshing principle and why inherited security matters
2. **Threat Model** — What we defend against, what we don't, and our assumptions
3. **Open Questions** — Specific questions for Apple Security Engineering review

### Appendix

- **Full Specification** — https://www.notion.so/WebSpec-2c942c9038be80c2b26ee86a5ea677c5
- **Status Report** — Documentation completeness assessment

---

### Prepared By

**Preston Richey**  
Orthonoetic Research  
December 2024

---

### Purpose

This packet is submitted for **technical review**, not endorsement. We seek feedback on:

- Whether our understanding of macOS security primitives is correct
- Where our assumptions might be flawed
- What we're missing about TCC, XPC, code signing, or entitlements
- Alternative approaches we should consider

We believe the best security comes from correctly using battle-tested primitives. We'd rather be corrected than ship something that undermines platform security.

---

*"The web IS the protocol."*
