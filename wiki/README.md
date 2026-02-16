# WebSpec

**A protocol specification for tool invocation that meshes with web architecture rather than building on top of it.**

## What is WebSpec?

WebSpec maps every protocol primitive directly to an existing web primitive — DNS for discovery, subdomains for isolation, HTTP methods for verbs, paths for nouns, and browser security for capability boundaries. The web IS the protocol.

## Reading the Spec

The specification is organized as a wiki under `docs/`. You can browse it directly on GitHub, or serve it locally with [MkDocs Material](https://squidfunk.github.io/mkdocs-material/):

```bash
pip install mkdocs-material
mkdocs serve
```

Then open `http://localhost:8000`.

## Structure

```
docs/
├── index.md                          # Overview & meshing principle
├── philosophy/                       # Non-normative conceptual foundations
│   ├── permissions-as-love-grammar.md
│   └── trust-gradients-philia-model.md
├── url-grammar/                      # URL structure & formal grammar
│   ├── core-syntax.md
│   ├── object-type-system.md
│   ├── type-resolution-inference.md
│   └── complete-grammar-ebnf.md
├── http-methods/                     # Verbs, permissions, security
│   ├── method-semantics.md
│   ├── permission-scoping.md
│   ├── head-options-discovery.md
│   ├── method-tokenization.md
│   └── definer-verbs.md             # NEW: Definer Verbs & Payload Binding
├── subdomain-architecture/           # Isolation & local bridge
│   ├── isolation-model.md
│   ├── cookie-token-scoping.md
│   ├── local-bridge.md
│   └── local-service-architecture.md
├── auth/                             # OAuth, keychain, tokens
│   ├── auth-flow-overview.md
│   ├── keychain-integration.md
│   ├── onboarding-subdomain-provisioning.md
│   └── token-structure.md
├── discovery/                        # NLP resolution & embeddings
│   ├── nlp-driven-resolution.md
│   ├── embedding-schema.md
│   └── three-way-join.md
├── registration/                     # Service registration & verification
│   ├── registration-schema.md
│   └── domain-verification.md
└── reference/                        # Comparisons & appendices
    └── comparison-with-mcp.md
```

## Status

All sections are in **Draft** status. The specification is under active development.

## Authors

- Preston Richey
- Claude (Anthropic)

## License

TBD
