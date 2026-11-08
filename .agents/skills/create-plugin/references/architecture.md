# Athena architecture for plugin authors

Link-first. The authorities are:

- `ADR-0014` — server plugins: manifest, discovery, activation, loading, failure semantics.
- `ADR-0018` — `websearch`, the complete example of a plugin with one Tool.
- `GLOSSARY.md` — the domain vocabulary; use its terms, not synonyms.
- `plugin_api/__init__.py` — the only surface a plugin imports.
- `plugins/websearch/plugin.py` — HTTP archetype.
- `plugins/homeassistant/plugin.py` — MCP platform archetype.

## Athena in one page

Athena is one process with one asyncio loop (ADR-0005). It connects voice Clients to Gemini
Live and gives the model a small set of Tools. The codebase splits into a **Noyau** and
**adaptateurs** (ADR-0001): the kernel owns Sessions, memory, and event judgement without
I/O; every external system is an adapter.

The three Surfaces are:

- **Transport** — the WebSocket between the server and the Clients (desktop, mobile, web).
  It carries the shared Protobuf protocol (ADR-0002) and requires a `client` access token.
- **Webhook** — HTTP ingress for external Sources pushing Events (ADR-0011, ADR-0017).
  It requires a `source` access token.
- **Console web** — the observation surface (ADR-0003, ADR-0016): a static SPA, a JSON
  snapshot, and a WebSocket trace stream. It is not a Client; it requires a Console ticket.

The core concepts a plugin will meet:

- **Session** — a conversation visible to the Client. It starts with a wake word, a tap, or a
  proactive Announcement; it ends with a signal, a timeout, or a model-decided
  `end_session`.
- **Connexion Live** — the server ↔ Gemini Live link. It can exist without a Session.
- **Canal d'événements** — the single ordered channel through which adapter inputs and
  external Events enter the kernel. The kernel processes one item at a time and judges an
  Event only at **Repos** (ADR-0007, ADR-0010).
- **Memory** — a kernel component, not a plugin (ADR-0006). It owns the Journal, the
  Consolidation, and pinned facts.
- **Tool** — a function Gemini may call during a Session. A Tool belongs either to the kernel
  (`save_memory`, `forget_memory`, `end_session`) or to a plugin.

## Where a plugin sits

A server plugin is a directory under `plugins/<name>/` with a `plugin.json` manifest and an
entrypoint. It is loaded **at startup**, never hot. Dropping the directory executes nothing:
activation is an explicit list in the state file (`plugins.json` by default). A plugin is
trusted code running in the server process, with the server's privileges and no sandbox
(ADR-0014).

### Loading lifecycle

The `PluginLoader` owns all the I/O of loading:

1. Read the state file: `enabled` (ordered list of plugin names) and `config` (non-secret
   values per plugin). A missing file means "no plugin enabled", not an error.
2. For each enabled plugin, read and validate `plugin.json`: `api_version` must be `1`,
   `name` must match the directory, `platform` must be known, the entrypoint must exist.
3. Resolve the declared non-secret config. Types are `string` (non-empty text) and `url`
   (`http`/`https` with a host). A required key that is missing or empty is fatal. The loader
   does **not** apply defaults; the plugin does.
4. Resolve the declared secrets from the environment as
   `ATHENA_PLUGIN_<PLUGIN>_<SECRET>`. A required secret that is missing is fatal. The value is
   never written to a Trace or a log.
5. Import the entrypoint and call `create(context)`. The call may be sync or async.
6. Validate that `create` returned a `PluginBinding` whose `name` matches the plugin.
7. Register the binding in the `PluginRegistry`: each Tool is attributed to its plugin, and a
   name collision is fatal. The registry also composes the plugins' instructions in a
   deterministic order.
8. Record the plugin state (`activated`, `disabled`, `missing`, `failed`) in the registry and
   emit `PluginState`.

Plugins are loaded **before** the transport and before the `started` trace: a broken enabled
plugin prevents the server from serving anything.

### The plugin contract

`PluginContext` is everything a plugin receives:

- `name` — the plugin name, also the owner of its Tools.
- `config` — the resolved non-secret config, as text.
- `secrets` — the resolved secret values, never traced.
- `trace(kind, payload, level=...)` — the observability sink, automatically prefixed with the
  plugin name and the `plugins` source.
- `push_event(source, content)` — the entry into the single Event channel.

`PluginBinding` is what the plugin returns:

- `name` — must equal `context.name`.
- `tools` — a tuple of `ToolBinding(spec, handler)`.
- `instruction` — a usage policy for Gemini, composed into the system prompt of every Session
  where the plugin's Tools are declared. It is not a schema. An Announcement Session carries
  the same instruction but declares no Tools (ADR-0017).

`ToolSpec(name, description, parameters)` is the declaration Gemini sees. `parameters` is a
JSON Schema mapping, passed as-is. The handler receives a `Mapping[str, object]` and returns a
`Mapping[str, object]`; it never raises.

### Tool round-trip

At the opening of each ordinary Live connection, the kernel declares the kernel Tools plus the
Tools of every activated plugin. An Announcement connection declares **no** Tools, so that
external content can never trigger an action (ADR-0017). Gemini calls a Tool; the kernel
executes the handler; the result goes back as a `FunctionResponse`. The plugin's handler is
the only code that leaves the server, towards the external platform. The kernel does not
rename or prefix platform Tools: their names are the platform's.

A plugin activated by a successful retry becomes visible to the **next** Live connection, not
the current one, because the Tool list is frozen when the connection opens.

### Failure semantics

- **`PluginUnavailable`** is the only retryable failure. It means the platform is unreachable
  or refused authentication. The loader records `failed`, emits a Trace, starts the server in
  a degraded mode, and the App retries every 5 seconds. A successful retry registers the
  plugin without restarting the server.
- **Any other exception** from `create()` is a plugin defect. It is fatal at startup and is
  logged and forgotten during a retry. A missing required config key, a missing required
  secret, an invalid manifest, or an import error is also fatal.
- **A Tool handler never raises.** A platform failure becomes an error mapping, usually
  `{"status": "error", "message": "..."}`, which the model can turn into a spoken sentence.
  The Session survives.

### Observability

`context.trace("Kind", {"key": value})` emits a Trace with source `plugins` and a `plugin` key.
Trace what an operator needs to diagnose the integration: the request, its parameters, the
duration, the result count, a cache hit or miss, credits consumed. Never trace a secret, a
whole page of content, or a prompt. The Console reads the plugin states and Tools from the
registry snapshot, and the Traces from the trace stream.

### Events

A plugin may push an Event with `context.push_event(source, content)`. The plugin does not
decide whether the Event is announced: the kernel judges it at Repos. A plugin that actively
polls a resource is a **Watcher**; an external application that pushes is a **Source**. The
manifest declares neither: a Source is named at the moment the Event is pushed.

## The two archetypes

- `plugins/websearch` — the simplest complete HTTP plugin. One required secret, five optional
  config keys, a `GET /usage` probe that validates the key without spending a credit, one Tool
  (`web_search`), a short cache, French error sentences, a degraded start when Tavily is
  unreachable or refuses the key.
- `plugins/homeassistant` — the MCP/platform plugin. `create` discovers the Assist Tools,
  exposes them as-is, fetches the Assist instruction, and uses 5-second discovery and
  10-second call budgets. A failed discovery is `PluginUnavailable`; a failed call is an error
  mapping.

## What stays out

- Client platforms (`desktop`, `android`, `ios`) are declared in manifests but never loaded by
  the server (ADR-0014).
- No hot activation: the state file is read at startup.
- No plugin catalog or marketplace: installing a plugin means making it present on the server.
- No plugin sandbox or subprocess: a plugin runs in the server process (ADR-0005).
