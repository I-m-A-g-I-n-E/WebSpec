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

<p class="ws-lede">WebSpec gives each MCP tool an HTTP method and a path, taken from the
tool's name. A gateway in front of your unchanged MCP servers refuses any method the tool's
contract doesn't admit.</p>

<div class="ws-actions" markdown="1">
[Run it in ten minutes <span>→</span>](guide/index.md){ .ws-button .ws-button--solid }
[Read the spec](spec/index.md){ .ws-button }
</div>

</div>

<div class="ws-seal">
<figure class="ws-specimen" data-methods aria-label="Requests to the reference gateway">
<div class="ws-specimen__band"><span>Request<span class="ws-wide"> · reference gateway</span></span><span class="ws-specimen__status">200 OK</span><span class="ws-methods" role="tablist" aria-label="Method" hidden><button type="button" role="tab" id="ws-method-get" aria-controls="ws-request-get" aria-selected="false" tabindex="-1" data-method="get">GET</button><button type="button" role="tab" id="ws-method-post" aria-controls="ws-request-post" aria-selected="false" tabindex="-1" data-method="post">POST</button><button type="button" role="tab" id="ws-method-delete" aria-controls="ws-request-delete" aria-selected="true" data-method="delete">DELETE</button></span></div>
<pre id="ws-request-get" role="tabpanel" aria-labelledby="ws-method-get" data-method="get" hidden><code><span><b class="ws-p ws-p--method">GET</b> <span class="ws-p ws-p--path">/note</span>?<span class="ws-p ws-p--args">id=welcome</span></span><span><span class="ws-p--key">Host:</span> <span class="ws-p ws-p--host">notes.localhost</span></span></code></pre><pre id="ws-request-post" role="tabpanel" aria-labelledby="ws-method-post" data-method="post" hidden><code><span><b class="ws-p ws-p--method">POST</b> <span class="ws-p ws-p--path">/email</span></span><span><span class="ws-p--key">Host:</span> <span class="ws-p ws-p--host">notes.localhost</span></span><span><span class="ws-p--key">Content-Type:</span> application/json</span><span><span class="ws-p--key">WebSpec-Definer:</span> <span class="ws-p ws-p--definer">SEND</span></span><span> </span><span class="ws-p ws-p--args">{"id":"welcome","to":"ana@example.com"}</span></code></pre><pre id="ws-request-delete" role="tabpanel" aria-labelledby="ws-method-delete" data-method="delete"><code><span><b class="ws-p ws-p--method">DELETE</b> <span class="ws-p ws-p--path">/note</span>?<span class="ws-p ws-p--args">id=welcome</span></span><span><span class="ws-p--key">Host:</span> <span class="ws-p ws-p--host">notes.localhost</span></span></code></pre>
<ul class="ws-legend">
<li class="ws-p--host"><i></i>who</li>
<li class="ws-p--method"><i></i>what kind</li>
<li class="ws-p--path"><i></i>on what</li>
<li class="ws-p--args"><i></i>with what</li><li class="ws-p--definer" data-only="post" hidden><i></i>which action</li>
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
<figcaption><b>Through a WebSpec gateway</b><span>the method says what kind of act</span></figcaption>
<pre><code><b class="ws-p ws-p--method">DELETE</b> <span class="ws-p ws-p--path">/note</span>?<span class="ws-p ws-p--args">id=welcome</span>
<span class="ws-p--key">Host:</span> <span class="ws-p ws-p--host">notes.localhost</span>

<span class="ws-dim">→ 200 OK</span></code></pre>
</figure>
</div>

</div>
</section>

<section markdown="1">
<div class="ws-wrap" markdown="1">

<p class="ws-kicker" id="every-part-says-one-thing-and-the-parts-must-agree">Read it like a sentence</p>

## Every part says one thing, and the gateway checks them. { #every-part-says-one-thing }

<p class="ws-intro">The gateway learns what a tool does, its <em>contract</em>, from the
annotations its MCP server declares or from the operator, never from the request. The method
and the path select one tool, and on <code>POST</code> a header names the action.</p>

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
decides which methods may run it (<a href="spec/methods/#method-binding">MB-1</a>).
<code>delete_note</code> declares itself destructive and idempotent, so its contract admits
<code>DELETE</code> and not <code>GET</code>: HTTP defines <code>GET</code> as safe, and
crawlers follow every link.</p>
<p><span class="ws-refusal">A send by <span class="ws-keep"><code>GET /email</code>:</span>
<code>405 Method Not Allowed</code> with <code>Allow: HEAD, OPTIONS, POST</code></span></p>
</div>

<div class="ws-part">
<span class="ws-part__token ws-p--path">/note</span>
<p><span class="ws-part__role">On what</span>The route rule splits the tool's name at its first
underscore. The verb picks the method, and the rest is the path, so <code>delete_note</code> is
<code>DELETE /note</code> and <code>read_note</code> is <code>GET /note</code>
(<a href="spec/addressing/#the-route-rule">PA-4</a>). If the name starts with no verb of the
rule, or that verb's method isn't admissible (the contract must admit it, and a tool that isn't
idempotent never gets <code>PUT</code> or <code>DELETE</code>), the tool keeps its full name as
its path.</p>
<p><span class="ws-refusal">A method <code>/note</code> doesn't map,
<span class="ws-keep"><code>PUT /note</code>:</span> <code>405 Method Not Allowed</code> with
<code>Allow: HEAD, OPTIONS, GET, DELETE</code></span>
<span class="ws-refusal">A path that maps no tool: <code>404 tool_not_found</code></span></p>
</div>

<div class="ws-part">
<span class="ws-part__token ws-p--args">id=welcome</span>
<p><span class="ws-part__role">With what</span>Arguments travel in the query or in a JSON body.
Bodies must be strict JSON, and each query key may appear once.</p>
<p><span class="ws-refusal">A repeated key, <span class="ws-keep"><code>?id=welcome&amp;id=other</code>:</span>
<code>400 duplicate_query_key</code></span></p>
</div>

<div class="ws-part">
<span class="ws-part__token ws-p--definer">SEND</span>
<p><span class="ws-part__role">Which action, on POST</span><code>POST</code> covers many kinds
of act, so a <code>POST</code> names its action in one word, in the
<code>WebSpec-Definer</code> header: <code>send_email</code> is <code>POST /email</code> with
<code>SEND</code>. On other methods the definer is optional. A page on another origin can't
add this header without a CORS preflight, and by default the gateway refuses the
preflight.</p>
<p><span class="ws-refusal"><code>CREATE</code> on <span class="ws-keep"><code>POST /email</code>:</span>
<code>400 unknown_action</code></span>
<span class="ws-refusal">A form posted from another site, which can't carry the header:
<code>400 missing_definer</code></span></p>
</div>

</div>

<p class="ws-maxim">So a <code>GET</code> can reach only a tool whose contract says it is
read-only.</p>

<p class="ws-footnote">Every answer in this section is the reference gateway's, and its test suite
checks each one. <a href="guide/how-it-works/">How it works</a> explains each part in full.</p>

</div>
</section>

<section markdown="1">
<div class="ws-wrap" markdown="1">

<p class="ws-kicker">Turn the dial</p>

## Raise the level. The model's part never changes.

<p class="ws-intro">The operator sets each destination's security level, from 0 to 4. A bank
check must be filled in the way the bank expects, carries a signature, bears a number so it is
paid only once, clears before the money moves, and may need a second signature when the stakes
are high. Each level adds the next of those guarantees, and none takes one away. The model writes the same
request at every level. The shim, a small signing layer in the harness that runs the model,
adds the rest.</p>

<div class="ws-dial">
<div class="ws-levels" role="tablist" aria-label="Security level">
<button class="ws-level" role="tab" data-level="0" aria-selected="false" aria-controls="ws-dial-panel"><span class="ws-level__n">0</span><span class="ws-level__name">local</span><span class="ws-level__like">Filled in as the bank expects</span></button>
<button class="ws-level" role="tab" data-level="1" aria-selected="false" aria-controls="ws-dial-panel"><span class="ws-level__n">1</span><span class="ws-level__name">signed</span><span class="ws-level__like">Carries a signature</span></button>
<button class="ws-level" role="tab" data-level="2" aria-selected="false" aria-controls="ws-dial-panel"><span class="ws-level__n">2</span><span class="ws-level__name">bound</span><span class="ws-level__like">Numbered, paid once</span></button>
<button class="ws-level" role="tab" data-level="3" aria-selected="false" aria-controls="ws-dial-panel"><span class="ws-level__n">3</span><span class="ws-level__name">cleared</span><span class="ws-level__like">Clears before money moves</span></button>
<button class="ws-level" role="tab" data-level="4" aria-selected="true" aria-controls="ws-dial-panel"><span class="ws-level__n">4</span><span class="ws-level__name">witnessed</span><span class="ws-level__like">Countersigned</span></button>
</div>

<div class="ws-dial__panel" id="ws-dial-panel" role="tabpanel">
<div class="ws-dial__card">
<div class="ws-dial__head"><span><code>send_email</code>, a send that reaches outside systems (open-world)</span><span class="ws-key"><span class="ws-key--model">written by the model</span><span class="ws-key--shim">added by the shim</span></span></div>
<div class="ws-req"><span><b class="ws-p ws-p--method">POST</b> <span class="ws-p ws-p--path">/email</span></span><span><span class="ws-p--key">Host:</span> <span class="ws-p ws-p--host">notes.localhost</span></span><span><span class="ws-p--key">Content-Type:</span> application/json</span><span><span class="ws-p--key">WebSpec-Definer:</span> <span class="ws-p ws-p--definer">SEND</span></span><span class="ws-shim" data-from="2">Idempotency-Key: <b>k-91c2</b></span><span class="ws-shim" data-from="3">WebSpec-Clearance: <b>ff3ea9ba:1792704267</b></span><span class="ws-shim" data-from="4">WebSpec-Approval: <b>8b6ef55bac3e5e620082df6b…</b></span><span class="ws-shim" data-from="1" data-to="1" hidden>WebSpec-Nonce: <b>da8f10cf082184de0fe1160fe9b0c49d</b></span><span class="ws-shim" data-from="1" data-to="1" hidden>WebSpec-Guard: <b>b1bc2809</b></span><span class="ws-shim" data-from="2" data-to="2" hidden>WebSpec-Nonce: <b>7c55dc4484a2f858ecc381a99a5f2c9e</b></span><span class="ws-shim" data-from="2" data-to="2" hidden>WebSpec-Guard: <b>8efe6700</b></span><span class="ws-shim" data-from="3" data-to="3" hidden>WebSpec-Nonce: <b>965e2e49e86446f3d66f5b05321ce4ee</b></span><span class="ws-shim" data-from="3" data-to="3" hidden>WebSpec-Guard: <b>7b36f644</b></span><span class="ws-shim" data-from="4">WebSpec-Nonce: <b>85e7e0a121d79d6325591680d1fd9172</b></span><span class="ws-shim" data-from="4">WebSpec-Guard: <b>1b637e7d</b></span><span> </span><span class="ws-p ws-p--args">{"id":"welcome","to":"ana@example.com"}</span></div>
</div>

<dl class="ws-dial__facts">
<div data-level-fact="0"><dt>Adds</dt><dd>Method binding, the definer on <code>POST</code>, and strictly parsed arguments. Served only on loopback names.</dd></div>
<div data-level-fact="0" class="ws-without"><dt>Without it</dt><dd>No definer: <code>400 missing_definer</code></dd></div>
<div data-level-fact="1"><dt>Adds</dt><dd>The guard: an HMAC over the method, host, path, query, body, definer, and any idempotency key, with a single-use nonce. The shim and the gateway share its key. Required on a public domain.</dd></div>
<div data-level-fact="1" class="ws-without"><dt>Without it</dt><dd><code>401 guard_missing</code></dd></div>
<div data-level-fact="2"><dt>Adds</dt><dd>An <code>Idempotency-Key</code> for tools that aren't idempotent, so a retry with the same key gets the stored answer instead of running the tool again.</dd></div>
<div data-level-fact="2" class="ws-without"><dt>Without it</dt><dd><code>400 idempotency_key_required</code></dd></div>
<div data-level-fact="3"><dt>Adds</dt><dd>A single-use clearance from the harness's policy, bound to this destination, method, tool, and arguments, for every change and for reads marked <code>sensitive</code> or <code>dangerous</code>.</dd></div>
<div data-level-fact="3" class="ws-without"><dt>Without it</dt><dd><code>403 clearance_missing</code></dd></div>
<div data-level-fact="4"><dt>Adds</dt><dd>A person's signature over the request's method, destination, path, arguments, and body, for destructive tools, tools marked <code>dangerous</code> (the highest risk tier), and open-world changes.</dd></div>
<div data-level-fact="4" class="ws-without"><dt>Without it</dt><dd><code>428 approval_required</code>, with a summary for a person to sign</dd></div>
</dl>
</div>
</div>

<p class="ws-footnote">At each level this is a real request that the reference shim sent to the
demo server at that level, under the <a href="guide/walkthrough/">walkthrough's</a> demo key,
and the gateway answered <code>200 OK</code>. At level 4 it is the second attempt, which
carries a person's approval, shortened here. <a href="spec/levels/">Security levels</a> has the
rules.</p>

</div>
</section>

<section markdown="1">
<div class="ws-wrap" markdown="1">

<p class="ws-kicker">Where it sits</p>

## An HTTP face for the MCP servers you already run.

<div class="ws-flow">
<div class="ws-node"><span class="ws-node__name">Model</span><p>Writes the method, path, arguments, and on <code>POST</code> one verb. Needs no key.</p></div>
<div class="ws-wire">request</div>
<div class="ws-node"><span class="ws-node__name">Harness shim</span><p>Adds the nonce, signatures, idempotency key, clearance, and approval the level calls for. Learns what to add from <code>OPTIONS</code>.</p></div>
<div class="ws-wire">HTTP</div>
<div class="ws-node ws-node--gateway"><span class="ws-node__name">WebSpec gateway</span><ul><li>Routes each request to one tool, and checks its method against the tool's contract, pinned so a server can't loosen it while the gateway runs</li><li>Runs the checks the destination's level requires</li><li>Keeps a hash-chained audit log</li></ul></div>
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
<li><span class="ws-lv">L4</span><span>Destructive or <code>dangerous</code> calls, and open-world changes, that no person signed, even from a harness that can't sign with an approver's key</span></li>
</ul>
</div>
<div class="ws-ledger--no">
<h3>Doesn't</h3>
<ul>
<li><span class="ws-lv">—</span><span>Judge intent. A well-formed, signed call that does the wrong thing passes levels 0 to 3.</span></li>
<li><span class="ws-lv">—</span><span>Check arguments against the tool's schema. They're checked for form only: strict JSON and no repeated query keys.</span></li>
<li><span class="ws-lv">—</span><span>Gate open-world reads. A URL fetcher can carry data out in its arguments unless you raise it to <code>dangerous</code>.</span></li>
<li><span class="ws-lv">—</span><span>Survive a stolen guard key below level 4. One key signs everything levels 1 to 3 require.</span></li>
<li><span class="ws-lv">—</span><span>Resist guessing on its own. Guard and clearance tags are 32 bits today, so rate-limit at the edge.</span></li>
<li><span class="ws-lv">—</span><span>Remember across a restart. Nonces, idempotency records, and contract and binding pins live in memory, and a stdio server, which runs as the gateway's user, can force a restart.</span></li>
<li><span class="ws-lv">—</span><span>Recognize a renamed tool. A tool listed under a new name, on a route no tool held, gets the contract its listing claims.</span></li>
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
<div class="ws-status--built"><span class="ws-status__label">Implemented</span><p>Host routing, the route rule and binding pins, tool contracts and pinning, method binding, levels 0 to 4, a hash-chained audit log, and reference deployments for Linux, macOS, and Docker. CI runs the gateway against real MCP servers.</p></div>
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
