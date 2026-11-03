# hermes-smart-search

A Hermes Agent web provider plugin that routes the native `web_search` and
`web_extract` tools through the local [smart-search](https://github.com/konbakuyomu/smartsearch) CLI.

Hermes pins **one** backend per capability. This plugin turns that one backend
into a fan-out: smart-search picks the right channel for each question among
every provider you configured there (Exa, Tavily, TinyFish, Firecrawl,
AnySearch, Jina, Zhipu, DeepSeek, Yuanbao, Context7, …), applies its own
same-capability fallback when one fails, and remembers a broken provider in a
cooldown file instead of retrying it every call.

Nothing is hard-coded on the plugin side: any provider smart-search supports
becomes available the moment you add its key in smart-search's own config — no
plugin change, no Hermes restart.

## Requirements

- Hermes Agent ≥ 0.22
- The `smart-search` CLI on the machine that runs Hermes:

  ```bash
  npm install -g @konbakuyomu/smart-search@latest
  smart-search doctor          # shows which providers are configured
  smart-search setup           # or: smart-search config set EXA_API_KEY ...
  ```

  If Hermes manages its own Node runtime (`~/.hermes/tools/node-*`), that prefix
  can be rebuilt by a Hermes update and silently drop globally installed npm
  packages. Install into a stable prefix instead, then link the bundled native
  binary (it needs no Node at runtime — swap `linux-arm64` for your platform):

  ```bash
  npm install -g --prefix /opt/smart-search @konbakuyomu/smart-search@latest
  ln -sf /opt/smart-search/lib/node_modules/@konbakuyomu/smart-search/node_modules/\
  @konbakuyomu/smart-search-linux-arm64/runtime/smart-search/smart-search \
  /usr/local/bin/smart-search
  ```

- At least one retrieval channel configured in smart-search. A main-search model
  (xAI / OpenAI-compatible) is optional — without it `search` returns sources
  instead of a synthesized answer, which is what an agent wants anyway.

## Install

```bash
hermes plugins install rays1d/hermes-smart-search --enable
```

Or drop this directory under `$HERMES_HOME/plugins/web/smartsearch/` and run
`hermes plugins enable web/smartsearch`.

## Configure

```bash
hermes config set web.search_backend smartsearch
hermes config set web.extract_backend smartsearch
```

`web_extract` runs up to three `smart-search fetch` calls in parallel; `web_search`
runs one `smart-search search`.

If the CLI fails, Hermes' one-shot keyless rescue still answers that call from
its built-in free-tier ring, so a broken key never takes search fully offline.

## Deep research tool

The plugin also registers `web_research` in the `web` toolset — a bounded,
multi-round research loop (search → read the discovered pages → fill evidence
gaps → cite):

```json
{"query": "…", "budget": "quick | standard | deep"}
```

| Budget | Rounds / channels per round | Rough wall time |
| --- | --- | --- |
| `quick` | 2 / 1 | ~45 s |
| `standard` (default) | 3 / 2 | ~2 min |
| `deep` | 10 / 10 | several minutes |

It returns `{answer, citations, evidence: {status, sufficient, gaps}, providers_used,
rounds, partial_success, warnings}`. When the evidence bar is not met but findings
exist, it returns `success: true` with `partial_success: true` and the reason in
`warnings` — the findings are still usable.

Research synthesis is opt-in inside smart-search: `SMART_SEARCH_JEV_SYNTHESIZE`
(`false` default, `auto`, `true`) decides whether a separate synthesis model writes
the final answer. With `auto` it synthesizes only when the evidence warrants it.

The tool appears in new sessions (`hermes gateway restart` or `/new`), since Hermes
freezes a session's tool list at start to protect prompt caching.

## Environment variables

| Variable | Default | Meaning |
| --- | --- | --- |
| `SMARTSEARCH_CLI` | auto (`PATH`, then `/usr/local/bin`, `~/.local/bin`, Homebrew) | Absolute path to the CLI |
| `SMARTSEARCH_TIMEOUT` | `90` | Per-call budget in seconds, clamped to 15–300 |

Provider keys are **not** read from the environment by this plugin — they live in
smart-search's config (`~/.config/smart-search/config.json`, or
`$SMART_SEARCH_CONFIG_DIR`), written by `smart-search setup` / `smart-search config set`.

## Notes

- Search results carry the serving channel in the description, e.g. `[tavily] …`.
- `smart-search fetch` returns markdown; the plugin uses the first `# heading`
  (or a YAML `title:`) as the page title.
- Windows: the npm shim is launched through `cmd /c` automatically.

## Test

```bash
python -m unittest discover -s tests -v
```

The tests stub the CLI and exercise result mapping only — no network, no keys.
