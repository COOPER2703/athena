# Plugin design constraints

These are constraints for the spec, not a checklist to execute. Each one points to the
authority that decides the details.

1. **No kernel change.** A plugin is an external integration, not an extension of the kernel.
   If the idea requires a new kernel capability — a new kernel Tool, a new Event, a new
   Surface, a change to the Session lifecycle — stop: that is a kernel feature first, with its
   own spec and ADR. Authority: ADR-0001, `GLOSSARY.md` ("Noyau").

2. **Import only `plugin_api`.** A plugin never imports `server` or any of its internals. The
   public contract is the only surface it knows. Authority: ADR-0014,
   `server/tests/test_plugin_contract.py`.

3. **The manifest is declarative, never secret.** It carries the identity (`name`, `version`,
   `description`), the platform (`server`), the entrypoint, the non-secret config fields, and
   the **names** of the required secrets. It never contains a secret value, never lists the
   Tools (they are discovered at runtime), and never declares the Sources. Authority:
   ADR-0014, `plugin_api.PluginManifest`.

4. **`create(context)` returns a `PluginBinding` named like the plugin.** The plugin never
   writes to the registry itself; the kernel aggregates, attributes each Tool, and detects
   name collisions. Authority: `plugin_api.PluginBinding`, `server/core/plugins.py`.

5. **Only `PluginUnavailable` is retryable.** An unreachable platform or a refused
   authentication raises it: the loader records `failed`, starts degraded, and the App retries
   every 5 seconds. Any other exception from `create()` is a plugin defect and is fatal.
   Authority: ADR-0014, `server/adapters/plugins/loader.py`.

6. **A Tool handler never raises.** It returns a mapping. A platform failure becomes a French
   sentence the model can say, and the Session survives. Authority:
   `plugins/websearch/docs/adr/0001-recherche-web-par-plugin.md`, `plugins/websearch/plugin.py`,
   `plugins/homeassistant/plugin.py`.

7. **Secrets never leak.** They are resolved by the loader from
   `ATHENA_PLUGIN_<PLUGIN>_<SECRET>` and passed in `context.secrets`. They must never appear in
   a Trace, a log, a snapshot, an error message, or a test fixture. Authority: ADR-0014,
   ADR-0017.

8. **Trace what an operator needs.** State, Tool call, duration, result, cache hit or miss,
   credits. The loader prefixes the plugin name and the `plugins` source. Never trace a
   secret, a whole page of content, or a prompt. Authority: ADR-0004,
   `PluginContext.trace`.

9. **Tests prove observable behaviour.** Fake the external platform locally; exercise the full
   path App → Gemini → registry → plugin → platform; assert on the requests emitted, the
   payload returned to the model, and the Traces. Do not test private helpers directly.
   Authority: `plugins/websearch/docs/adr/0001-recherche-web-par-plugin.md`,
   `plugins/homeassistant/tests/test_plugins_homeassistant.py`,
   `plugins/websearch/tests/test_plugins_websearch.py`.

10. **The contract test must stay green.** No `server` import, an exact manifest, and a pure
    `plugin_api`. Authority: `server/tests/test_plugin_contract.py`.

11. **Activation is a file operation.** The directory, its name in `plugins.json`, and its
    secrets in the environment. No hot activation: the server is restarted, and the Tools are
    declared at the next Live connection. `plugins.json` is not versioned. Authority:
    ADR-0014.

12. **Document the domain.** A term that belongs only to a plugin goes into that plugin's
    `GLOSSARY.md`; a durable, surprising, hard-to-reverse trade-off of the plugin goes into
    its `docs/adr/`, numbered locally from `0001` (central ADRs are for the system only); a
    manual end-to-end path goes into the plugin's `docs/smoke-test.md`. Terms of the contract
    and the kernel stay in the root `GLOSSARY.md`, and system decisions in `docs/adr/`.
    User-facing strings are French and use the glossary vocabulary. Authority:
    `plugins/README.md`, `docs/adr/README.md`, `docs/agents/domain.md`, the `domain-modeling`
    skill, `GLOSSARY-MAP.md`.
