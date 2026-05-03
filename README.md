<p align="center">
  <img src="./assets/banner.png" alt="umbra" width="100%"/>
</p>

# umbra

> **The de-facto MCP server for stealth browser automation.**
> Real Chrome, 0% creepjs detection, 31/31 sannysoft, 77 broad tools, multi-browser orchestration, encrypted sessions, prompt-injection signaling, and live human handoff over a Cloudflare tunnel — for AI agents that need to browse the web like a human, not a bot.

> *umbra — the darkest part of a shadow, where light is fully blocked.*

Built by merging the best parts of [obscura](https://github.com/h4ckf0r0day/obscura) (per-session fingerprint payload) + [fantoma](https://github.com/Huzy85/fantoma) (zero-mouse ARIA driver) + [stealth-browser-mcp](https://github.com/vibheksoni/stealth-browser-mcp) (nodriver + MCP surface) — and filling in the gaps each one had: the [`Page.enable()` injection bug](https://github.com/GabriWar/umbra/blob/main/src/umbra/stealth/inject.py), real-GPU headless via `--headless=new + ANGLE Vulkan`, dynamic UA-CH version pinning, mDNS-aware WebRTC SDP filter, MCP token-efficient minification, and the `_untrusted: true` cognitive-separation flag on every page-sourced tool response.

---

## 🪙 token efficiency

Counter-intuitively, umbra costs LESS context than minimal browser-MCPs (incl. playwright-mcp) on any real agent session — its 77-tool catalog adds ~13KB upfront, but per-call savings recover that within 3 calls and dominate after that.

**measured** (79-call e2e session against real Chrome + httpbin, all 77 tools exercised, see [`tests/test_token_audit.py`](https://github.com/GabriWar/umbra/blob/main/tests/test_token_audit.py)):

| | uncompressed (`set_verbosity='full'`) | compressed (default) | saved |
|---|---|---|---|
| total tokens (cl100k_base) | 136,285 | **33,357** | **75 %** |
| total bytes (JSON) | 420,648 | **71,282** | **83 %** |
| median per call | — | **14 tokens / 4 ms** | — |

Top per-tool wins: `clone_element` 97 %, `dom_query` 52 %, `tls_fetch` 40 %. The handful of zero-save tools (`screenshot*`, `aria_snapshot`, `inspect_element`) either ship base64 binaries (incompressible) or are already pre-RLE'd in the driver before `_compact` sees them.

Per-call wins come from:
- `extract_markdown` (clean MD via Mozilla Readability + markdownify) instead of raw HTML/innerText dumps
- `_compact()` everywhere: drops `None` only, columnar layout for 4+ homogeneous arrays w/ constant-column hoisting, word-boundary truncation w/ explicit `...[+Nc, raise max_str to see full]` markers
- `_untrusted: true` flag (8 bytes) instead of wrapping content in `<external>...</external>` tags
- columnar `dom_query`: `{"keys":["text","href","rect"],"rows":[...]}` vs `[{text,href,rect,id,cls,value,visible},...]` — 44 % smaller on real pages
- pagination markers (`{_truncated, shown, total, more_via}`) so callers see WHAT was truncated and HOW to lift the cap

Toggle off via `set_verbosity('full')` when you need raw byte-exact output. Lossless: zero failures, zero inflations across all 79 audit calls.

---

## ⚡ in numbers

| | umbra |
|---|---|
| **bot.sannysoft.com** | **31 / 31** ✓ (perfect) |
| **creepjs `headless`** | **0 %** (matches vanilla Chrome) |
| **creepjs `stealth`** | **0 %** |
| **CDP automation tells** stripped | `webdriver` `cdc_*` `$cdc_` `_phantom` `_selenium` `__webdriver_*` `__nightmare` ... |
| **MCP tools** | **77** (broad primitives + `batch` flagship, not 95 narrow ones) |
| **headless** | real GPU via `--headless=new + ANGLE Vulkan` (no SwiftShader tell) |
| **TLS / JA3** | real Chrome stack + optional `curl_cffi` for raw HTTP |
| **WebRTC** | mDNS-aware SDP filter (real-Chrome behavior, no LAN IP leak) |
| **handoff** | live remote-view via `cloudflared` Quick Tunnel (works VPS → home laptop) |
| **CDP schema drift** | resilient — survives Chrome field churn (e.g. dropped `sameParty`) without hangs |

---

## 🚀 install

**requirements:** Python 3.10+, a Chromium-based browser (Chrome / Chromium / Edge — auto-detected).

### 1. clone + install

```bash
git clone https://github.com/GabriWar/umbra.git
cd umbra
pip install -e ".[all]"
```

`[all]` pulls every optional dep — recommended. To pick & choose:

| extra | enables | install |
|---|---|---|
| (default) | core 50 tools, encrypted sessions | `pip install -e .` |
| `[markdown]` | `extract_markdown` (readability + markdownify) | `pip install -e ".[markdown]"` |
| `[tls]` | `tls_fetch` (curl_cffi w/ Chrome JA3+JA4) | `pip install -e ".[tls]"` |
| `[playwright]` | optional Playwright backend | `pip install -e ".[playwright]"` |
| `[test]` | pytest + asyncio for regression suite | `pip install -e ".[test]"` |
| `[all]` | everything above | `pip install -e ".[all]"` |

### 2. install cloudflared (recommended — for `handoff_*` tunnel)

The handoff tool exposes a live remote-view of the headless browser via a Cloudflare Quick Tunnel — open a URL on any device, click I'M DONE when done. Without `cloudflared` it falls back to `http://127.0.0.1:PORT` (localhost only).

```bash
# arch / cachyos
sudo pacman -S cloudflared
# debian / ubuntu
sudo apt install cloudflared
# macos
brew install cloudflared
# everywhere else: download the binary from
#   https://github.com/cloudflare/cloudflared/releases/latest
```

No signup, no auth, no account.

### 3. wire into Claude Code (or any MCP client)

```bash
claude mcp add-json umbra '{
  "type":"stdio",
  "command":"/full/path/to/your/python",
  "args":["-m","umbra.server"],
  "env":{
    "UMBRA_CONTAINER":"1",
    "PYTHONPATH":"/full/path/to/umbra/src"
  }
}'
```

Then restart Claude Code → `/mcp` should show `umbra` with 77 tools.

For Cursor / Claude Desktop / other MCP clients, edit their `mcp_servers` config with the same shape.

### 4. verify

```bash
python -m umbra.server  # ctrl+c after a few seconds — tools should register cleanly
pytest -m e2e -v -s     # full regression suite (boots real Chrome, ~60s)
```

### TODO (distribution)

- [ ] Submit to [Smithery.ai](https://smithery.ai) registry — add `smithery.yaml` and tag a release. Auto-indexes for Claude Desktop / Cursor / Cline users.
- [ ] Add `.claude-plugin/plugin.json` for Claude Code's plugin marketplace system.
- [ ] Optionally submit to Anthropic's official marketplace via `claude.ai/settings/plugins/submit`.

### TODO (features)

- [ ] **Proxy pool rotation** — currently `StealthOptions(proxy="...")` accepts one proxy per session. For high-volume scraping or geo-distributed scraping, add a `proxy_pool=[...]` option that round-robins (or rotates per-tab / per-N-requests / on-403). Pair w/ residential providers (smartproxy, iproyale, brightdata) for IP reputation. ~80 LoC + a per-tab proxy override via CDP `Network.setExtraHTTPHeaders` + `--proxy-server` per browser instance.

- [x] **Full request interception graph** — shipped as `route_add` / `route_add_many` / `route_remove` / `route_set_enabled` / `route_block_set` / `route_list` / `route_captures` + `har_record_start/stop/dump/clear` + `har_replay_load`. Match DSL: `url_pattern`, `url_regex`, `method`, `resource_type`, `header_match`, `status_min/max`. Actions: `block` (14 custom `error_reason`s; response-stage block synthesizes 5xx via fulfill), `fulfill` (status+headers+body|body_b64), `continue` (request rewrite: new_url/new_method/new_post_data/headers — headers MERGED w/ originals, not replaced), `modify` (response-stage `getResponseBody` → `body_replace=[[regex,repl],...]` or outright body/status/headers override), `tee` (pure spy: pass-through + capture body), `redirect` (synth 302 + Location). Per-rule `delay_ms` (latency injection), `times` (auto-disable after N hits), `priority` (higher fires first), `capture` (per-rule cross-stage body buffer for any action), `enabled` (pause without remove). HAR-1.2 record/replay (`loose` URL-only mode for query-string drift). Tracker/resource blocking from `StealthOptions(block_trackers=, block_resources=)` integrated into the same engine — single Fetch handler, no double-fire race. `dynamic_hook` kept as legacy thin wrapper. Engine: `src/umbra/driver/intercept.py`.

- [ ] **Battle-test the ARIA tree on edge cases** — fantoma-derived snapshot covers the 95% case (forms, lists, dialogs, nav) but real-world weirdness still exposes gaps: shadow-DOM-inside-iframe-inside-shadow-DOM, custom elements w/ delegated focus, `<canvas>`-rendered "trees" (Figma/Notion), virtual-scroll lists where ARIA indexes shift mid-snapshot, `aria-owns` cross-references, RTL/i18n role inflections. Need a regression corpus (gmail, github, notion, figma, linear, jira, gov forms) + property-based tests so we don't regress as nodriver/Chrome update. Playwright's accessibility tree has a decade of these baked in — ours is ~6 months.

---

## 🤖 use as an MCP server (the main use case)

```bash
claude mcp add-json umbra '{
  "type":"stdio",
  "command":"/home/you/.venv/bin/python",
  "args":["-m","umbra.server"],
  "env":{"UMBRA_CONTAINER":"1","PYTHONPATH":"/path/to/umbra/src"}
}'
```

Now your agent has 77 MCP tools for stealth Chrome automation. Cursor, Claude Desktop, Claude Code — anything MCP.

---

## 🧰 the 77 tools

```
                  ┌─ browser            spawn / close / list_browsers / close_browser /
                  │                     navigate / list_tabs / switch_tab / back / forward /
                  │                     reload / kill_all
                  │
                  ├─ ARIA               aria_snapshot / aria_click / aria_type / find_by_text
                  │  (zero mouse)       fill_form / current_state
                  │
                  ├─ input (CDP)        click_at / press_key / scroll / drag / hover /
                  │  humanized          paste_text / select_option / wait_for / wait_for_text
                  │
                  ├─ extraction         extract_text / extract_links / grep_text / dom_query /
                  │  _untrusted=true    inspect_element / extract_markdown / clone_element
                  │
                  ├─ visual             screenshot / screenshot_region
                  │
                  ├─ JS                 evaluate / inject_css
                  │
                  ├─ devtools           get_console_logs / get_network_requests / clear_logs /
                  │                     get_response_body / memory_metrics / get_cookies /
                  │                     set_cookies / clear_cookies
                  │
                  ├─ stealth ops        check_detection / warm_session / rotate_fingerprint /
                  │                     set_verbosity
                  │
                  ├─ network ctrl       block_urls / set_extra_headers / set_viewport /
                  │                     dynamic_hook
                  │
                  ├─ handoff            handoff_start / handoff_wait / request_user_input
                  │  (live remote view, cloudflared tunnel)
                  │
                  ├─ sessions           session_save / session_load / session_list /
                  │  (encrypted)        session_delete
                  │
                  ├─ files              upload_file / setup_downloads / wait_for_download
                  │
                  ├─ TLS                tls_fetch  (raw HTTP w/ Chrome JA3+JA4)
                  │
                  └─ batch ⭐ flagship  batch  (N tools in one round-trip; composes w/ dedup)
```

---

## 🎯 highlight tools

### `batch` ⭐ flagship — N tools in one MCP round-trip

```python
batch([
  {"tool": "navigate",        "args": {"tab_id": "t0", "url": "https://news.ycombinator.com"}},
  {"tool": "wait_for_text",   "args": {"tab_id": "t0", "text": "Hacker News"}},
  {"tool": "aria_snapshot",   "args": {"tab_id": "t0"}},
  {"tool": "extract_links",   "args": {"tab_id": "t0", "limit": 30}},
  {"tool": "extract_markdown","args": {"tab_id": "t0"}},
])
# → {"results":[...5 entries with ok/data/ms each...],
#    "elapsed_ms":1840, "ok_count":5, "fail_count":0}
```

Serial in declared order, single MCP round-trip. Saves protocol framing per call AND composes with cross-call dedup (identical re-calls inside the batch return `_unchanged_since` instead of full payloads). Use it whenever you have ≥2 calls in mind — it's almost always the right choice.

`stop_on_error=True` short-circuits the batch on first failure (default: keep going + report fail_count).

### `handoff_start` — when you hit a captcha, hand the wheel back

```
agent → handoff_start("t0", "solve recaptcha")
         → returns https://random.trycloudflare.com/h-XYZ/
agent → tells user: "open this URL"
user  → opens URL on phone/laptop, sees live page, clicks/types
user  → hits "I'M DONE"
agent → handoff_wait("t0")  blocks until done, returns post-handoff URL+title
agent → continues automation
```

Built on Cloudflare Quick Tunnels (no signup, no auth, instant).
URL contains a 192-bit auth token in the path → URL knowledge = auth.
Forces HTTP/2 protocol for sustained WebSocket reliability.

### `extract_markdown` — page → clean markdown (firecrawl-style)

```python
extract_markdown('t0')
# → {"_untrusted": True,
#    "title": "Web Scraping - Wikipedia",
#    "markdown": "# Web Scraping\n\nMethod of extracting data...",
#    "source_html_len": 87432}
```

Mozilla Readability + markdownify. Falls back to `<body>` for list pages (HN, reddit) where readability gives up.

### `session_save` / `session_load` — log in once

```python
# First time: log in manually via handoff
session_save('t0', 'github-me', passphrase='hunter2')
# → encrypted blob in ~/.local/share/umbra/sessions/github.com/github-me.fern

# Next time: skip login entirely
session_load('t0', 'github-me', passphrase='hunter2')
# → cookies + localStorage injected, you're logged in
```

Fernet (AES-128-CBC + HMAC-SHA256) + PBKDF2-HMAC-SHA256 200k iterations. Per-(domain, name) namespace, path-traversal-safe.

### `tls_fetch` — skip the DOM entirely for JSON APIs

```python
tls_fetch('https://api.example.com/users')
# → {"status": 200, "body": "{...}"}
```

curl_cffi pinned to the running Chrome version — JA3+JA4+HTTP/2 SETTINGS frames match Chrome exactly. ~50ms vs ~500ms via spawn+navigate.

### multi-browser

```python
spawn(url='...', browser_id='alice')
spawn(url='...', browser_id='bob')
# alice and bob have fully isolated cookies, profiles, identities
list_browsers()
# → [{"browser_id":"alice","tab_count":3}, {"browser_id":"bob","tab_count":1}]
```

---

## 🛡️ stealth coverage matrix

| detection vector | obscura | fantoma | sb-mcp | **umbra** |
|---|---|---|---|---|
| canvas / audio / WebGL fp | ✓ | partial | ✗ | ✓ (per-session noise, deterministic w/in session) |
| `navigator.webdriver` | ✓ | ✓ | ✓ | ✓ |
| `cdc_*` / `_phantom` / `_selenium` | ✗ | n/a | ✓ (nodriver) | ✓ delete-only (no `in` operator tell) |
| `event.isTrusted` | ✗ | ✓ (no synth events) | ✗ | ✓ (CDP `Input.dispatch*` only) |
| mouse / scroll behavioral fp | n/a | ✓ | ✗ | ✓ (ARIA driver default) |
| keystroke timing fp | n/a | ✓ key-pair | ✗ flat 50ms | ✓ key-pair + log-normal jitter |
| Cloudflare turnstile (passive) | ✗ | partial | ✓ | ✓ (real Chrome) |
| TLS / JA3 / JA4 | ✗ | ✗ | ✓ (real Chrome) | ✓ + `tls_fetch` for raw HTTP |
| WebGL real GPU in headless | ✗ no GL | ✗ | ✗ SwiftShader | ✓ ANGLE Vulkan |
| WebRTC outgoing SDP `host` | partial | ✗ | ✗ | ✓ (mDNS-aware filter, real-Chrome behavior) |
| UA-CH version mismatch | ✗ | ✗ | ✗ | ✓ (dynamic Chrome version + `setUserAgentOverride`) |
| iframe + shadow DOM piercing | ✗ | ✓ | ✗ | ✓ |
| tracker/fp-script blocking | ✓ (3520) | ✗ | ✗ | ✓ (3520 + dynamic hooks) |
| session warming (cookie age) | ✗ | ✗ | ✗ | ✓ (4 profiles) |
| live human handoff | ✗ | ✗ | ✗ | ✓ (cloudflared tunnel) |
| MCP tool surface | ✗ | ✗ | ✓ (95 narrow) | ✓ (77 broad) |
| prompt-injection signaling | ✗ | ✗ | ✗ | ✓ (`_untrusted: true` on all extraction) |

---

## 🐍 use as a python library

```python
import asyncio
from umbra import stealth_browser

async def main():
    async with stealth_browser(timezone="America/New_York", block_trackers=True) as b:
        tab = await b.new_tab("https://news.ycombinator.com")
        await asyncio.sleep(2)
        await tab.save_screenshot("hn.png")

asyncio.run(main())
```

```python
# ARIA driver — zero mouse coords
from umbra import stealth_browser, AriaDriver

async with stealth_browser() as b:
    tab = await b.new_tab("https://github.com/login")
    drv = AriaDriver(tab)
    await drv.snapshot()
    print(drv.render_tree())
    # [0] textbox "Login or email"
    # [1] textbox "Password"
    # [2] button "Sign in"
    await drv.type(0, "me@example.com")
    await drv.type(1, "...")
    await drv.click(2)
```

---

## 🔬 token efficiency

every MCP tool response goes through `_compact()`:

- `None` dropped (empty `[]` / `""` / `0` / `False` KEPT — they're informative)
- columnar layout for 4+ homogeneous-dict arrays: `{"_columnar":true,"keys":[...],"rows":[[...]]}`
- constant-column hoist: shared values factored to `_constant: {col: val}`
- word-boundary string truncation w/ explicit `...[+Nc, raise max_str to see full]` marker
- list truncation w/ `{_truncated, shown, total, more_via}` marker

**24% average wire-byte savings** on real-world pages (test data on HN/wikipedia). flip with `set_verbosity('full')` when you need raw.

### cross-call dedup ledger

Identical repeat calls return `{"_unchanged_since": "cN", "_hash": "..."}` instead of the full payload — the data is unchanged from call cN, so the agent reuses what it already has in context. Pass `force_refresh=True` to bypass.

```python
extract_text('t0')   # → call c5: full {text:"...",length:8421,...}
extract_text('t0')   # → call c6: {"_unchanged_since":"c5","_hash":"a7f2..."}  ← saved 8KB
```

Pairs perfectly with `batch` — you can blast `[snapshot, snapshot, snapshot]` after each interaction; only the deltas come back.

### ARIA pattern grouping (RLE for snapshots)

Long lists (HN comments, search results, file trees) with repeating `(role, name)` cycles get run-length-encoded losslessly:

```
[12-77] cycle×13 (period 5): link("Comments"), link("Permalink"), link("Save"), button("Vote"), text("user")
↳ 66 lines collapsed into 1 — agent still knows the exact range and what's in each cycle
```

Detects period 1–6 with ≥2 reps. Real HN comments page: ~70% smaller snapshot.

### URL footnoting (host dedup in `extract_links`)

Repeated hosts get factored out once:

```
{"_hosts": {"h1":"https://github.com", "h2":"https://news.ycombinator.com"},
 "links": [["h1","/user/foo"], ["h1","/issues/123"], ["h2","/item?id=456"], ...]}
```

~50% smaller on link-heavy pages. Reconstruct via `_hosts[h] + path`.

---

## 🩹 CDP schema resilience

nodriver's CDP parser hardcodes Chrome protocol field names — when Chrome changes the schema between releases, the parser KeyErrors. Worse, the listener task dies on the unhandled raise → every subsequent CDP call on that tab hangs forever (no awaiter ever wakes up).

umbra ships three monkey-patches in `umbra/nodriver_patch.py` to make this class of bug impossible:

1. **`Transaction.__call__`** — every parser exception becomes `future.set_exception(...)` so the awaiter gets a real error, never a hang.
2. **`Connection._listener`** — wraps the per-message dispatch so a single bad parse can't kill the listener task; future calls keep working.
3. **`Cookie.from_json`** — tolerant of Chrome 146+ dropping `sameParty` (matches the pattern already used in `CookieParam.from_json`; upstream inconsistency).

Patches are **idempotent** (per-class flag + module-level short-circuit, safe to call N times) and **partial-failure tolerant** (each patch runs in its own try/except — one failing doesn't block the others). Applied automatically at `umbra.browser` import — zero config.

---

## 🏗️ architecture

```
                  ┌────────────────────────────────────────────┐
                  │  FastMCP server  (umbra.server, 65 tools)  │
                  │  + _compact() minification                 │
                  │  + _untrusted prompt-injection signaling   │
                  └────────────────────────────────────────────┘
                                       │
       ┌─────────────────────┬─────────┴──────────┬─────────────────────┐
       ▼                     ▼                    ▼                     ▼
  ┌─────────┐          ┌──────────┐         ┌────────────┐        ┌──────────┐
  │ Browser │          │  Drivers │         │  Stealth   │        │  Misc    │
  │  multi  │          │  ARIA    │         │  payload   │        │  session │
  │  inst.  │          │  CDP     │         │  3520 list │        │  handoff │
  └────┬────┘          │ humanizer│         │  detection │        │    tls   │
       │               └────┬─────┘         └─────┬──────┘        └──────────┘
       ▼                    ▼                     ▼
  ┌──────────────────────────────────────────────────────────────────────┐
  │nodriver (real Chrome via CDP) + Page.addScriptToEvaluateOnNewDocument│
  │  --headless=new + --use-angle=vulkan + dynamic UA-CH version pinning │
  └──────────────────────────────────────────────────────────────────────┘
                                       │
                                       ▼
                              ┌─────────────────┐
                              │   real Chrome   │
                              │  146.0.7680.x   │
                              └─────────────────┘
```

---

## 🧪 regression tests

```bash
pip install -e ".[test]"
pytest -m e2e -v -s
```

runs `bot.sannysoft.com` + `creepjs` + UA-CH consistency + automation-tell checks. Catches drift if Chrome / nodriver update breaks something.

---

## 📜 license

**MIT + Attribution Requirement.** Free for any use (commercial, research, hobby) — but if you ship it in a product or publish research using it, please credit:

```markdown
Powered by [umbra](https://github.com/GabriWar/umbra) by Gabriel Duarte Guerra.
```

(in your README, docs, about page, or paper acknowledgements — anywhere a human reading your project can see it).

Third-party attributions in `LICENSE`:

- `stealth/payload.js` patterns from h4ckf0r0day/obscura (Apache-2.0)
- `stealth/tracker_domains.txt` from obscura (Peter Lowe ad/tracker host file)
- `driver/aria.py` + `humanizer.py` patterns from Huzy85/fantoma (MIT)
- MCP tool surface convention from vibheksoni/stealth-browser-mcp (MIT)
