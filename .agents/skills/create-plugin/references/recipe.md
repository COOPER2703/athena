# Plugin anatomy and spec inputs

The snippets below are illustrative, not files to copy. The skill never writes plugin code.

## Directory

```
plugins/<name>/
├── plugin.json
└── plugin.py
```

Tests live in `server/tests/test_plugins_<name>.py`. A manual end-to-end path belongs in a
`docs/smoke-test-*.md` document.

## Manifest

```json
{
  "api_version": 1,
  "name": "<name>",
  "version": "0.1.0",
  "description": "<one sentence>",
  "platform": "server",
  "entrypoint": "plugin.py",
  "config": [{ "key": "base_url", "type": "url", "required": false }],
  "secrets": [{ "key": "api_key", "required": true }]
}
```

Config types are `string` (non-empty text) and `url` (`http`/`https` with a host). The loader
validates the declared values but does not carry defaults: the plugin applies its own.

## Entry point

```python
from plugin_api import (
    PluginBinding,
    PluginContext,
    PluginUnavailable,
    ToolBinding,
    ToolSpec,
)


async def create(context: PluginContext) -> PluginBinding:
    # Probe the platform here if it can be unreachable.
    # Raise PluginUnavailable on an unreachable platform or a refused key.
    return PluginBinding(
        name=context.name,
        tools=(ToolBinding(spec=..., handler=...),),
        instruction="...",
    )
```

`create` may be synchronous or asynchronous. It must return a `PluginBinding` whose `name`
equals `context.name`.

## Tool

`ToolSpec(name, description, parameters)` declares the Tool. `parameters` is a JSON Schema
mapping, passed as-is to Gemini. The handler receives a `Mapping[str, object]` and returns a
`Mapping[str, object]`.

The error shape used by both examples is:

```python
return {"status": "error", "message": "La recherche web n'a pas abouti : ..."}
```

The message is French because the model may say it aloud.

## Config and secrets

- Non-secret config: `context.config`, already validated, text values.
- Secrets: `context.secrets`, already resolved, never traced.
- A missing required config key or secret is fatal before `create`; the plugin does not need to
  check it.

## Startup probe and degraded start

A platform that can be unreachable should be probed in `create`. Raise `PluginUnavailable` on
an unreachable platform or a refused key: the loader records `failed`, the server starts
degraded, and the App retries every 5 seconds. Any other exception is a defect: fatal.

A platform that answers but is temporarily unhealthy (for example a 5xx on a health endpoint)
should not necessarily raise: trace the degradation and let the Tool calls surface the truth.

## Traces

`context.trace("Kind", {"key": value}, level="info")` emits a Trace with source `plugins` and a
`plugin` key. Trace the request, its parameters, the duration, the result count, a cache hit
or miss, and credits consumed. Never trace the secret.

## Events

Optional: `context.push_event(source, content)` pushes an Event into the single channel. The
plugin does not decide whether it is announced; the kernel judges it at Repos.

## Instruction

`PluginBinding.instruction` is composed into the system prompt of every Session where the
plugin's Tools are declared. It is a usage policy, not a schema: when to call, when not to,
what to say before calling, how to phrase a failure. An Announcement Session carries the same
instruction but declares no Tools (ADR-0017).

## Tests

- Fake the external platform locally: an HTTP stub or an MCP stub, injected through a config
  key such as `base_url`, or through the existing stub pattern.
- Exercise the full path: App → Gemini fake → registry → plugin → platform.
- Assert the requests emitted, the payload returned to the model, the Traces, the degraded
  start and retry, the secret redaction, and the exact manifest.
- The contract test (`server/tests/test_plugin_contract.py`) picks up the new plugin
  automatically; it must stay green.

## Activation

```json
{
  "enabled": ["<name>"],
  "config": {
    "<name>": { "base_url": "https://example.test" }
  }
}
```

`plugins.json` is not versioned. Secrets go into the environment or `.env` as
`ATHENA_PLUGIN_<PLUGIN>_<SECRET>`. No hot activation: restart the server, and the Tools are
declared at the next Live connection.

## What the spec must capture

Inside `to-spec`'s *Implementation Decisions*:

### Plugin decisions

- manifest: name, version, description, platform `server`, entrypoint;
- config keys: type, required or optional, default applied by the plugin;
- secrets: names, required or optional, source;
- startup: probe, retryable versus fatal failures, degraded state;
- runtime failures: error shape, French sentence;
- traces: kinds and payloads, what must never leak;
- events: source name and content, or "none";
- activation: `plugins.json` entry and environment variables;
- Console: Tool visibility and plugin state.

### Tool contract (one per Tool)

- name, as the platform exposes it, with no prefix;
- description;
- parameters (JSON Schema);
- when the model must and must not call it;
- error shape.

## Reference implementations

- `plugins/websearch/plugin.py` — HTTP API, one Tool, cache, degraded start.
- `plugins/homeassistant/plugin.py` — MCP platform, discovered Tools, instruction.
