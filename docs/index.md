---
title: Make every AI tool call say what it does
template: home.html
hide:
  - navigation
  - toc
---

<section class="ws-hero" markdown="1">
<div class="ws-wrap" markdown="1">
<div markdown="1">

<p class="ws-kicker">Spec · Reference gateway · For MCP</p>

<h1>Make every AI tool call<br> <em>say what it does.</em></h1>

<p class="ws-lede">WebSpec gives each MCP tool its own URL, and each call an HTTP method that
matches what the tool does. A gateway in front of your unchanged MCP servers refuses any method
a tool's contract doesn't admit, and any change whose definer header doesn't match its method.</p>

<div class="ws-actions" markdown="1">
[Run it in ten minutes <span>→</span>](guide/index.md){ .ws-button .ws-button--solid }
[Read the spec](spec/index.md){ .ws-button }
</div>

</div>

<div class="ws-seal">
<figure class="ws-specimen" aria-label="A request to the reference gateway">
<div class="ws-specimen__band"><span>Request<span class="ws-wide"> · reference gateway</span></span><span class="ws-specimen__status">200 OK</span></div>
<pre><code><span><b class="ws-p ws-p--method">DELETE</b> <span class="ws-p ws-p--path">/delete_note</span>?<span class="ws-p ws-p--args">id=welcome</span></span><span><span class="ws-p--key">Host:</span> <span class="ws-p ws-p--host">notes.localhost</span></span><span><span class="ws-p--key">X-Gimme-Definer:</span> <span class="ws-p ws-p--definer">REMOVE</span></span></code></pre>
<ul class="ws-legend">
<li class="ws-p--host"><i></i>who</li>
<li class="ws-p--method"><i></i>what kind</li>
<li class="ws-p--path"><i></i>which act</li>
<li class="ws-p--args"><i></i>with what</li>
<li class="ws-p--definer"><i></i>the verb, said aloud</li>
</ul>
</figure>
</div>

</div>
</section>

<section markdown="1">
<div class="ws-wrap" markdown="1">

<p class="ws-kicker">The problem</p>

## To the web, every MCP call looks the same.

<p class="ws-intro">MCP's HTTP transport sends every tool call as a <code>POST</code> to one
endpoint. Since its 2026-07-28 revision a header names the tool, but the rules the web
applies by method, such as CORS preflights and method allow-lists, still treat a read, a send,
and a delete alike. WebSpec puts what a call does back on the request line, where
browsers, proxies, and logs already look.</p>

<div class="ws-versus">
<figure>
<figcaption><b>MCP over HTTP</b><span>every call is a POST to /mcp (abridged)</span></figcaption>
<pre><code><b class="ws-p ws-p--method">POST</b> /mcp
<span class="ws-dim">MCP-Protocol-Version: 2026-07-28</span>
<span class="ws-dim">Mcp-Method: tools/call</span>
<span class="ws-dim">Mcp-Name:</span> delete_note

<span class="ws-dim">{"jsonrpc": "2.0", "id": 1,
 "method": "tools/call",
 "params": {
  "name": "delete_note",
  "arguments": {"id": "welcome"}}}</span></code></pre>
</figure>
<figure>
<figcaption><b>Through a WebSpec gateway</b><span>one URL per tool</span></figcaption>
<pre><code><b class="ws-p ws-p--method">DELETE</b> <span class="ws-p ws-p--path">/delete_note</span>?<span class="ws-p ws-p--args">id=welcome</span>
<span class="ws-p--key">Host:</span> <span class="ws-p ws-p--host">notes.localhost</span>
<span class="ws-p--key">X-Gimme-Definer:</span> <span class="ws-p ws-p--definer">REMOVE</span></code></pre>
</figure>
</div>

</div>
</section>

<section markdown="1">
<div class="ws-wrap" markdown="1">

<p class="ws-kicker">Read it like a sentence</p>

## Every part says one thing, and the gateway checks them. { #every-part-says-one-thing-and-the-parts-must-agree }

<p class="ws-intro">The gateway learns what a tool does, its <em>contract</em>, from the
annotations its MCP server declares or from the operator, never from the request. It checks
the method against that contract and, on every change, the definer against the method.</p>

<div class="ws-anatomy">

<div class="ws-part">
<span class="ws-part__token ws-p--host">notes.localhost</span>
<p><span class="ws-part__role">Who handles it</span>The host names the destination: here, the
MCP server <code>notes</code>. Browsers treat each host as its own origin, and proxies route by
it.</p>
<p><span class="ws-refusal">A host the gateway doesn't serve, such as
<span class="ws-keep"><code>mail.localhost</code>:</span> <code>404 unknown_service</code></span></p>
</div>

<div class="ws-part">
<span class="ws-part__token ws-p--method">DELETE</span>
<p><span class="ws-part__role">What kind of act</span>The tool's contract, not the caller,
decides which methods may run it. <code>delete_note</code> declares itself destructive and
idempotent, so it takes <code>DELETE</code> or <code>PUT</code>, never <code>GET</code>:
HTTP defines <code>GET</code> as safe, and crawlers follow every link.</p>
<p><span class="ws-refusal"><span class="ws-keep"><code>GET /delete_note</code>:</span> <code>405 Method Not Allowed</code>
with <code>Allow: HEAD, OPTIONS, PUT, DELETE</code></span></p>
</div>

<div class="ws-part">
<span class="ws-part__token ws-p--path">/delete_note</span>
<p><span class="ws-part__role">Which act</span>The path names one MCP tool on that server.
Logs record it, and a reverse proxy can match it.</p>
<p><span class="ws-refusal">A path that names no tool: <code>404 tool_not_found</code></span></p>
</div>

<div class="ws-part">
<span class="ws-part__token ws-p--args">id=welcome</span>
<p><span class="ws-part__role">With what</span>Arguments travel in the query or in a JSON body.
Bodies must be strict JSON, and each query key may appear once.</p>
<p><span class="ws-refusal">A repeated key, <span class="ws-keep"><code>?id=welcome&amp;id=other</code>:</span>
<code>400 duplicate_query_key</code></span></p>
</div>

<div class="ws-part">
<span class="ws-part__token ws-p--definer">REMOVE</span>
<p><span class="ws-part__role">The verb, said aloud</span>On every change, the definer
restates the method in one word, like the amount in words on a check, and the two must agree.
A page on another origin can't add this header without a CORS preflight, and by default the gateway refuses the
preflight.</p>
<p><span class="ws-refusal"><code>REMOVE</code> on a <span class="ws-keep"><code>POST</code>:</span>
<code>400 definer_family_mismatch</code></span>
<span class="ws-refusal">A form posted from another site, which can't carry the header:
<code>400 missing_definer</code></span></p>
</div>

</div>

<p class="ws-maxim">So a <code>GET</code> can reach only a tool whose contract says it is
read-only.</p>

<p class="ws-footnote">Every answer on this page is the reference gateway's, and its test suite
checks each one. <a href="guide/how-it-works/">How it works</a> explains each part in full.</p>

</div>
</section>

<section markdown="1">
<div class="ws-wrap" markdown="1">

<p class="ws-kicker">Turn the dial</p>

## Raise the level. The model's part never changes.

<p class="ws-intro">The operator sets each destination's security level, from 0 to 4. A bank
check states its amount twice, carries a signature, bears a number so it is paid only once,
clears before the money moves, and may need a second signature when the stakes are high. Each
level adds the next of those guarantees, and none takes one away. The model writes the same
request at every level. The shim, a small signing layer in the harness that runs the model,
adds the rest.</p>

<div class="ws-dial">
<div class="ws-levels" role="tablist" aria-label="Security level">
<button class="ws-level" role="tab" data-level="0" aria-selected="false" aria-controls="ws-dial-panel"><span class="ws-level__n">0</span><span class="ws-level__name">local</span><span class="ws-level__like">Amount in figures and words</span></button>
<button class="ws-level" role="tab" data-level="1" aria-selected="false" aria-controls="ws-dial-panel"><span class="ws-level__n">1</span><span class="ws-level__name">signed</span><span class="ws-level__like">Carries a signature</span></button>
<button class="ws-level" role="tab" data-level="2" aria-selected="false" aria-controls="ws-dial-panel"><span class="ws-level__n">2</span><span class="ws-level__name">bound</span><span class="ws-level__like">Numbered, paid once</span></button>
<button class="ws-level" role="tab" data-level="3" aria-selected="false" aria-controls="ws-dial-panel"><span class="ws-level__n">3</span><span class="ws-level__name">cleared</span><span class="ws-level__like">Clears before money moves</span></button>
<button class="ws-level" role="tab" data-level="4" aria-selected="true" aria-controls="ws-dial-panel"><span class="ws-level__n">4</span><span class="ws-level__name">witnessed</span><span class="ws-level__like">Countersigned</span></button>
</div>

<div class="ws-dial__panel" id="ws-dial-panel" role="tabpanel">
<div class="ws-dial__card">
<div class="ws-dial__head"><span><code>send_note</code>, a send that reaches outside systems (open-world)</span><span class="ws-key"><span class="ws-key--model">written by the model</span><span class="ws-key--shim">added by the shim</span></span></div>
<div class="ws-req"><span><b class="ws-p ws-p--method">POST</b> <span class="ws-p ws-p--path">/send_note</span></span><span><span class="ws-p--key">Host:</span> <span class="ws-p ws-p--host">notes.localhost</span></span><span><span class="ws-p--key">Content-Type:</span> application/json</span><span><span class="ws-p--key">X-Gimme-Definer:</span> <span class="ws-p ws-p--definer">SEND</span><span class="ws-tail" data-from="2">:8a9975c7</span></span><span class="ws-shim" data-from="2">Idempotency-Key: <b>k-91c2</b></span><span class="ws-shim" data-from="3">X-UFO-Clearance: <b>08db7b72:1791244639</b></span><span class="ws-shim" data-from="4">X-WebSpec-Approval: <b>e6bcb018cdffb4cc91c2a460…</b></span><span class="ws-shim" data-from="1">X-WebSpec-Nonce: <b>b712879c3c9d7b92a014bfc62aa34653</b></span><span class="ws-shim" data-from="1" data-to="1" hidden>X-WebSpec-Guard: <b>ebeefe8b</b></span><span class="ws-shim" data-from="2">X-WebSpec-Guard: <b>b85db549</b></span><span> </span><span class="ws-p ws-p--args">{"id":"welcome","to":"ana@example.com"}</span></div>
</div>

<dl class="ws-dial__facts">
<div data-level-fact="0"><dt>Adds</dt><dd>Method binding, definer verbs, and strictly parsed arguments. Served only on loopback names.</dd></div>
<div data-level-fact="0" class="ws-without"><dt>Without it</dt><dd>No definer: <code>400 missing_definer</code></dd></div>
<div data-level-fact="1"><dt>Adds</dt><dd>The guard: an HMAC over the method, host, path, query, body, definer, and any idempotency key, with a single-use nonce. The shim and the gateway share its key. Required on a public domain.</dd></div>
<div data-level-fact="1" class="ws-without"><dt>Without it</dt><dd><code>401 guard_missing</code></dd></div>
<div data-level-fact="2"><dt>Adds</dt><dd>An <code>Idempotency-Key</code> for tools that aren't idempotent, and on every unsafe method a bookend, the short tag after the verb. Today the bookend adds nothing over the guard.</dd></div>
<div data-level-fact="2" class="ws-without"><dt>Without it</dt><dd><code>400 idempotency_key_required</code></dd></div>
<div data-level-fact="3"><dt>Adds</dt><dd>A single-use clearance from the harness's policy, bound to this destination, method, tool, and arguments, for every change and for reads marked <code>sensitive</code> or <code>dangerous</code>.</dd></div>
<div data-level-fact="3" class="ws-without"><dt>Without it</dt><dd><code>403 clearance_missing</code></dd></div>
<div data-level-fact="4"><dt>Adds</dt><dd>A person's signature over the request's method, destination, path, arguments, and body, for destructive tools, tools marked <code>dangerous</code> (the highest risk tier), and open-world changes.</dd></div>
<div data-level-fact="4" class="ws-without"><dt>Without it</dt><dd><code>428 approval_required</code>, with a summary for a person to sign</dd></div>
</dl>
</div>
</div>

<p class="ws-footnote">The guard, bookend, and clearance are valid for this request under the
<a href="guide/walkthrough/">walkthrough's</a> demo key. At level 4 the request is the second
attempt, which carries a person's approval. <a href="spec/levels/">Security levels</a> has the
rules.</p>

</div>
</section>

<section markdown="1">
<div class="ws-wrap" markdown="1">

<p class="ws-kicker">Where it sits</p>

## An HTTP face for the MCP servers you already run.

<div class="ws-flow">
<div class="ws-node"><span class="ws-node__name">Model</span><p>Writes the method, path, arguments, and one verb. Needs no key.</p></div>
<div class="ws-wire">request</div>
<div class="ws-node"><span class="ws-node__name">Harness shim</span><p>Adds the nonce, signatures, idempotency key, clearance, and approval the level calls for. Learns what to add from <code>OPTIONS</code>.</p></div>
<div class="ws-wire">HTTP</div>
<div class="ws-node ws-node--gateway"><span class="ws-node__name">WebSpec gateway</span><ul><li>Checks the request against the tool's contract, pinned so a server can't loosen it while the gateway runs</li><li>Runs the checks the destination's level requires</li><li>Keeps a hash-chained audit log</li></ul></div>
<div class="ws-wire">MCP</div>
<div class="ws-node"><span class="ws-node__name">MCP servers</span><p>Unchanged, over stdio or HTTP. Their annotations become each tool's contract.</p></div>
</div>

<p class="ws-footnote"><a href="guide/how-it-works/#webspec-and-mcp">WebSpec and MCP</a>
compares what each provides.</p>

</div>
</section>

<section markdown="1">
<div class="ws-wrap" markdown="1">

<p class="ws-kicker">Scope</p>

## What it stops, and what it doesn't.

<div class="ws-ledger">
<div>
<h3>Stops</h3>
<ul>
<li><span class="ws-lv">L0</span><span>Any method the tool's contract doesn't admit, such as a <code>GET</code> to a tool not marked read-only</span></li>
<li><span class="ws-lv">L0</span><span>Forms and scripts on other sites sending unsafe requests</span></li>
<li><span class="ws-lv">L1</span><span>Forged, altered, or replayed requests</span></li>
<li><span class="ws-lv">L2</span><span>A retry that would run a side effect twice (best effort under load)</span></li>
<li><span class="ws-lv">L3</span><span>Changes, and reads marked <code>sensitive</code> or <code>dangerous</code>, that the harness's policy didn't vouch for</span></li>
<li><span class="ws-lv">L4</span><span>Destructive or <code>dangerous</code> calls, and open-world changes, that no person signed, even from a compromised harness</span></li>
</ul>
</div>
<div class="ws-ledger--no">
<h3>Doesn't</h3>
<ul>
<li><span class="ws-lv">—</span><span>Judge intent. A well-formed, signed call that does the wrong thing passes levels 0 to 3.</span></li>
<li><span class="ws-lv">—</span><span>Check arguments against the tool's schema. They're checked for form only: strict JSON and unique query keys.</span></li>
<li><span class="ws-lv">—</span><span>Gate open-world reads. A URL fetcher can carry data out in its arguments unless you raise it to <code>dangerous</code>.</span></li>
<li><span class="ws-lv">—</span><span>Survive a stolen guard key below level 4. One key signs everything levels 1 to 3 require.</span></li>
<li><span class="ws-lv">—</span><span>Resist guessing on its own. Guard, bookend, and clearance tags are 32 bits today, so rate-limit at the edge.</span></li>
<li><span class="ws-lv">—</span><span>Remember across a restart. Nonces, idempotency records, and contract pins live in memory, and a stdio server, which runs as the gateway's user, can force a restart.</span></li>
<li><span class="ws-lv">—</span><span>Help if traffic can route around it. The gateway must be the only way out.</span></li>
</ul>
</div>
</div>

<p class="ws-footnote"><a href="guide/threat-model/">Threat model</a> ·
<a href="spec/status/#known-limits">Known limits</a></p>

</div>
</section>

<section markdown="1">
<div class="ws-wrap" markdown="1">

<p class="ws-kicker">Where it stands</p>

## Built and tested, with the rest written down.

<div class="ws-status">
<div class="ws-status--built"><span class="ws-status__label">Implemented</span><p>Host routing, tool contracts and pinning, method binding, levels 0 to 4, a hash-chained audit log, and reference deployments for Linux, macOS, and Docker. CI runs the gateway against real MCP servers.</p></div>
<div class="ws-status--next"><span class="ws-status__label">Proposed</span><p>Independent signers per level (RFC 9421 signatures), durable state, schema validation, scoped tokens, and intent checks. See the <a href="spec/status/#roadmap">roadmap</a>.</p></div>
</div>

<p class="ws-kicker">Where to start</p>

<div class="ws-start" markdown="1">
[<span class="ws-card__n">01</span><span class="ws-card__title">Quickstart</span><span class="ws-card__text">Run the gateway in front of a demo server and call it with curl.</span>](guide/index.md){ .ws-card }
[<span class="ws-card__n">02</span><span class="ws-card__title">How it works</span><span class="ws-card__text">Why each part of a request matters, and where its authority comes from.</span>](guide/how-it-works.md){ .ws-card }
[<span class="ws-card__n">03</span><span class="ws-card__title">A request at every level</span><span class="ws-card__text">Real transcripts of reads, sends, and deletes across levels 0 to 4.</span>](guide/walkthrough.md){ .ws-card }
[<span class="ws-card__n">04</span><span class="ws-card__title">The spec</span><span class="ws-card__text">Normative rules, each with a stable ID you can cite.</span>](spec/index.md){ .ws-card }
</div>

</div>
</section>
