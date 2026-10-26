from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Mapping

import pytest
from conftest import RecordingTraceSink

from server.adapters.plugins import PluginLoadError, PluginLoader
from server.app.secrets import EnvSecretResolver
from server.core.plugins import PluginRegistry

FIXTURE_NAME = "fixture"

FIXTURE_CODE = '''
from plugin_api import PluginBinding, ToolBinding, ToolSpec


async def handle(arguments):
    return {"status": "ok", "echo": arguments.get("value", "")}


def create(context):
    context.trace("PluginCreated", {"config": dict(context.config)})
    return PluginBinding(
        name="fixture",
        tools=(
            ToolBinding(
                spec=ToolSpec(
                    name="fixture__echo",
                    description="Écho du plugin",
                    parameters={
                        "type": "object",
                        "properties": {"value": {"type": "string"}},
                    },
                ),
                handler=handle,
            ),
        ),
        instruction="Utilise fixture__echo pour répéter.",
    )
'''

ASYNC_CODE = FIXTURE_CODE.replace(
    "def create(context):", "async def create(context):"
)

EXPLODING_CODE = 'raise RuntimeError("this plugin must never be imported")'


def _manifest(name: str = FIXTURE_NAME, **overrides: object) -> dict:
    manifest = {
        "api_version": 1,
        "name": name,
        "version": "0.1.0",
        "description": "Plugin de fixture",
        "platform": "server",
        "entrypoint": "plugin.py",
    }
    manifest.update(overrides)
    return manifest


def _write_plugin(
    root: Path,
    name: str = FIXTURE_NAME,
    *,
    manifest: dict | None = None,
    code: str = FIXTURE_CODE,
    entrypoint: str = "plugin.py",
) -> Path:
    directory = root / "plugins" / name
    directory.mkdir(parents=True, exist_ok=True)
    payload = _manifest(name) if manifest is None else manifest
    (directory / "plugin.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )
    (directory / entrypoint).write_text(code, encoding="utf-8")
    return directory


def _named_code(name: str, code: str = FIXTURE_CODE) -> str:
    """Point d'entrée de fixture lié au nom du plugin (nom et Tools suivent)."""
    return (
        code.replace('name="fixture"', f'name="{name}"')
        .replace("fixture__echo", f"{name}__echo")
        .replace("fixture__ping", f"{name}__ping")
    )


def _write_state(
    root: Path,
    *,
    enabled: list[str] | None = None,
    config: Mapping[str, Mapping[str, str]] | None = None,
) -> Path:
    state_path = root / "plugins.json"
    state_path.write_text(
        json.dumps({"enabled": enabled or [], "config": dict(config or {})}),
        encoding="utf-8",
    )
    return state_path


def _loader(root: Path, sink: RecordingTraceSink, env: dict[str, str] | None = None):
    return PluginLoader(
        directory=root / "plugins",
        state_path=root / "plugins.json",
        secrets=EnvSecretResolver(env or {}),
        trace_sink=sink,
    )


def _load(
    loader: PluginLoader,
    registry: PluginRegistry | None = None,
    pushes: list[tuple[str, str]] | None = None,
) -> PluginRegistry:
    registry = registry if registry is not None else PluginRegistry()
    pushes = pushes if pushes is not None else []
    asyncio.run(
        loader.load(registry, push_event=lambda source, content: pushes.append(
            (source, content)
        ))
    )
    return registry


def _states(sink: RecordingTraceSink) -> list[tuple[str, str]]:
    return [
        (str(entry.payload["plugin"]), str(entry.payload["state"]))
        for entry in sink.of_kind("PluginState")
    ]


def test_enabled_plugin_is_activated_with_its_tools(tmp_path: Path) -> None:
    _write_plugin(tmp_path)
    _write_state(tmp_path, enabled=[FIXTURE_NAME])
    sink = RecordingTraceSink()

    registry = _load(_loader(tmp_path, sink))

    assert {spec.name for spec in registry.tools.specs()} == {"fixture__echo"}
    assert registry.tools.owner_of("fixture__echo") == FIXTURE_NAME
    assert registry.instruction() == "Utilise fixture__echo pour répéter."
    [entry] = sink.of_kind("PluginState")
    assert entry.source == "plugins"
    assert entry.payload["plugin"] == FIXTURE_NAME
    assert entry.payload["state"] == "activated"


def test_plugin_present_but_not_enabled_is_never_imported(tmp_path: Path) -> None:
    _write_plugin(tmp_path, code=EXPLODING_CODE)
    _write_state(tmp_path, enabled=[])
    sink = RecordingTraceSink()

    registry = _load(_loader(tmp_path, sink))

    assert registry.tools.specs() == ()
    assert _states(sink) == [(FIXTURE_NAME, "disabled")]


def test_enabled_plugin_without_a_directory_is_traced_missing(tmp_path: Path) -> None:
    _write_state(tmp_path, enabled=[FIXTURE_NAME])
    sink = RecordingTraceSink()

    registry = _load(_loader(tmp_path, sink))

    assert registry.tools.specs() == ()
    assert _states(sink) == [(FIXTURE_NAME, "missing")]


def test_missing_directory_and_state_mean_no_plugins(tmp_path: Path) -> None:
    sink = RecordingTraceSink()

    registry = _load(_loader(tmp_path, sink))

    assert registry.tools.specs() == ()
    assert sink.of_kind("PluginState") == []


def test_an_async_create_is_supported(tmp_path: Path) -> None:
    _write_plugin(tmp_path, code=ASYNC_CODE)
    _write_state(tmp_path, enabled=[FIXTURE_NAME])

    registry = _load(_loader(tmp_path, RecordingTraceSink()))

    assert registry.tools.owner_of("fixture__echo") == FIXTURE_NAME


def test_plugin_declared_for_a_client_platform_is_skipped(tmp_path: Path) -> None:
    _write_plugin(tmp_path, code=EXPLODING_CODE, manifest=_manifest(platform="desktop"))
    _write_state(tmp_path, enabled=[FIXTURE_NAME])
    sink = RecordingTraceSink()

    registry = _load(_loader(tmp_path, sink))

    assert registry.tools.specs() == ()
    assert _states(sink) == [(FIXTURE_NAME, "disabled")]
    assert sink.of_kind("PluginState")[0].payload["platform"] == "desktop"


def test_unknown_api_version_fails_the_startup(tmp_path: Path) -> None:
    _write_plugin(tmp_path, manifest=_manifest(api_version=2))
    _write_state(tmp_path, enabled=[FIXTURE_NAME])

    with pytest.raises(PluginLoadError) as excinfo:
        _load(_loader(tmp_path, RecordingTraceSink()))

    message = str(excinfo.value)
    assert FIXTURE_NAME in message
    assert "api_version" in message


def test_manifest_without_a_required_field_fails_the_startup(tmp_path: Path) -> None:
    manifest = _manifest()
    del manifest["version"]
    _write_plugin(tmp_path, manifest=manifest)
    _write_state(tmp_path, enabled=[FIXTURE_NAME])

    with pytest.raises(PluginLoadError) as excinfo:
        _load(_loader(tmp_path, RecordingTraceSink()))

    message = str(excinfo.value)
    assert FIXTURE_NAME in message
    assert "version" in message


def test_missing_entrypoint_file_fails_the_startup(tmp_path: Path) -> None:
    _write_plugin(tmp_path, manifest=_manifest(entrypoint="missing.py"))
    _write_state(tmp_path, enabled=[FIXTURE_NAME])

    with pytest.raises(PluginLoadError) as excinfo:
        _load(_loader(tmp_path, RecordingTraceSink()))

    message = str(excinfo.value)
    assert FIXTURE_NAME in message
    assert "missing.py" in message


def test_missing_required_config_key_fails_the_startup(tmp_path: Path) -> None:
    _write_plugin(
        tmp_path,
        manifest=_manifest(config=[{"key": "url", "type": "url", "required": True}]),
    )
    _write_state(tmp_path, enabled=[FIXTURE_NAME])

    with pytest.raises(PluginLoadError) as excinfo:
        _load(_loader(tmp_path, RecordingTraceSink()))

    message = str(excinfo.value)
    assert FIXTURE_NAME in message
    assert "url" in message


SECRET_CODE = '''
from plugin_api import PluginBinding, ToolBinding, ToolSpec


async def handle(arguments):
    return {"status": "ok"}


def create(context):
    context.trace(
        "PluginCreated",
        {"token_present": "token" in context.secrets},
    )
    return PluginBinding(
        name="fixture",
        tools=(
            ToolBinding(
                spec=ToolSpec(name="fixture__ping", description="ping", parameters={}),
                handler=handle,
            ),
        ),
    )
'''


def test_missing_required_secret_fails_the_startup(tmp_path: Path) -> None:
    _write_plugin(
        tmp_path,
        code=SECRET_CODE,
        manifest=_manifest(secrets=[{"key": "token", "required": True}]),
    )
    _write_state(tmp_path, enabled=[FIXTURE_NAME])

    with pytest.raises(PluginLoadError) as excinfo:
        _load(_loader(tmp_path, RecordingTraceSink()))

    message = str(excinfo.value)
    assert FIXTURE_NAME in message
    assert "ATHENA_PLUGIN_FIXTURE_TOKEN" in message


def test_secret_is_resolved_for_the_plugin_and_never_traced(tmp_path: Path) -> None:
    secret = "s3cr3t-token-value"
    _write_plugin(
        tmp_path,
        code=SECRET_CODE,
        manifest=_manifest(secrets=[{"key": "token", "required": True}]),
    )
    _write_state(tmp_path, enabled=[FIXTURE_NAME])
    sink = RecordingTraceSink()

    registry = _load(
        _loader(tmp_path, sink, env={"ATHENA_PLUGIN_FIXTURE_TOKEN": secret})
    )

    created = sink.of_kind("PluginCreated")[0]
    assert created.payload["token_present"] is True
    assert all(
        secret not in json.dumps(entry.payload) for entry in sink.entries
    )
    assert registry.tools.owner_of("fixture__ping") == FIXTURE_NAME


def test_required_config_reaches_the_plugin(tmp_path: Path) -> None:
    _write_plugin(
        tmp_path,
        manifest=_manifest(config=[{"key": "url", "type": "url", "required": True}]),
    )
    _write_state(
        tmp_path,
        enabled=[FIXTURE_NAME],
        config={FIXTURE_NAME: {"url": "http://homeassistant.local:8123"}},
    )
    sink = RecordingTraceSink()

    _load(_loader(tmp_path, sink))

    created = sink.of_kind("PluginCreated")[0]
    assert created.payload["config"] == {"url": "http://homeassistant.local:8123"}


def test_two_plugins_claiming_the_same_tool_fail_the_startup(tmp_path: Path) -> None:
    _write_plugin(tmp_path, "alpha", manifest=_manifest("alpha"))
    _write_plugin(tmp_path, "beta", manifest=_manifest("beta"))
    # Les deux points d'entrée viennent de la fixture : elle déclare toujours
    # le même Tool et se lie au nom du dossier.
    for name in ("alpha", "beta"):
        code = FIXTURE_CODE.replace('name="fixture"', f'name="{name}"')
        (tmp_path / "plugins" / name / "plugin.py").write_text(code, encoding="utf-8")
    _write_state(tmp_path, enabled=["alpha", "beta"])

    with pytest.raises(PluginLoadError) as excinfo:
        _load(_loader(tmp_path, RecordingTraceSink()))

    message = str(excinfo.value)
    assert "alpha" in message
    assert "beta" in message


def test_malformed_manifest_fails_the_startup(tmp_path: Path) -> None:
    directory = tmp_path / "plugins" / FIXTURE_NAME
    directory.mkdir(parents=True)
    (directory / "plugin.json").write_text("{not json", encoding="utf-8")
    _write_state(tmp_path, enabled=[FIXTURE_NAME])

    with pytest.raises(PluginLoadError) as excinfo:
        _load(_loader(tmp_path, RecordingTraceSink()))

    assert FIXTURE_NAME in str(excinfo.value)


# -- Pannes retentables (ticket #57) -----------------------------------------

# Un plugin dont ``create`` échoue la première fois puis réussit : le cas d'une
# plateforme injoignable au démarrage, qui se répare sans redémarrer Athena.
FLAKY_CODE = '''
from plugin_api import PluginBinding, ToolBinding, ToolSpec

attempts = 0


async def handle(arguments):
    return {"status": "ok"}


def create(context):
    global attempts
    attempts += 1
    if attempts == 1:
        raise ConnectionError("home assistant unreachable")
    return PluginBinding(
        name="fixture",
        tools=(
            ToolBinding(
                spec=ToolSpec(name="fixture__ping", description="ping", parameters={}),
                handler=handle,
            ),
        ),
    )
'''

ALWAYS_FAILING_CODE = '''
def create(context):
    raise ConnectionError("home assistant toujours injoignable")
'''


WRONG_BINDING_CODE = '''
from plugin_api import PluginBinding, ToolBinding, ToolSpec

attempts = 0


async def handle(arguments):
    return {"status": "ok"}


def create(context):
    global attempts
    attempts += 1
    if attempts == 1:
        raise ConnectionError("home assistant unreachable")
    return PluginBinding(
        name="un-autre-nom",
        tools=(
            ToolBinding(
                spec=ToolSpec(name="fixture__ping", description="ping", parameters={}),
                handler=handle,
            ),
        ),
    )
'''


def test_a_failing_create_is_traced_failed_and_kept_for_retry(tmp_path: Path) -> None:
    _write_plugin(tmp_path, code=FLAKY_CODE)
    _write_state(tmp_path, enabled=[FIXTURE_NAME])
    sink = RecordingTraceSink()
    loader = _loader(tmp_path, sink)

    registry = _load(loader)

    # Le démarrage n'échoue pas : le plugin est seulement en échec.
    assert registry.tools.specs() == ()
    [failed] = sink.of_kind("PluginState")
    assert failed.payload["plugin"] == FIXTURE_NAME
    assert failed.payload["state"] == "failed"
    assert "ConnectionError" in failed.payload["reason"]
    assert "unreachable" in failed.payload["reason"]
    assert loader.pending == (FIXTURE_NAME,)

    remaining = asyncio.run(
        loader.retry_pending(registry, push_event=lambda source, content: None)
    )

    assert remaining == ()
    assert loader.pending == ()
    assert registry.tools.owner_of("fixture__ping") == FIXTURE_NAME
    states = [entry.payload["state"] for entry in sink.of_kind("PluginState")]
    assert states == ["failed", "activated"]


def test_a_retry_that_fails_again_is_traced_and_kept(tmp_path: Path) -> None:
    _write_plugin(tmp_path, code=ALWAYS_FAILING_CODE)
    _write_state(tmp_path, enabled=[FIXTURE_NAME])
    sink = RecordingTraceSink()
    loader = _loader(tmp_path, sink)
    registry = _load(loader)

    remaining = asyncio.run(
        loader.retry_pending(registry, push_event=lambda source, content: None)
    )

    assert remaining == (FIXTURE_NAME,)
    states = [entry.payload["state"] for entry in sink.of_kind("PluginState")]
    assert states == ["failed", "failed"]
    assert "toujours injoignable" in sink.of_kind("PluginState")[-1].payload["reason"]


def test_a_retry_never_retries_a_structural_defect(tmp_path: Path) -> None:
    """Un nom de binding faux n'est pas une panne passagère : la retente s'arrête."""
    _write_plugin(tmp_path, code=WRONG_BINDING_CODE)
    _write_state(tmp_path, enabled=[FIXTURE_NAME])
    sink = RecordingTraceSink()
    loader = _loader(tmp_path, sink)
    registry = _load(loader)

    remaining = asyncio.run(
        loader.retry_pending(registry, push_event=lambda source, content: None)
    )

    assert remaining == ()
    assert loader.pending == ()
    assert registry.tools.specs() == ()
    [failed] = sink.of_kind("PluginState")[1:]
    assert failed.payload["state"] == "failed"
    assert "un-autre-nom" in failed.payload["reason"]


def test_a_retry_after_a_create_failure_still_refuses_a_tool_collision(
    tmp_path: Path,
) -> None:
    _write_plugin(
        tmp_path,
        "alpha",
        manifest=_manifest("alpha"),
        code=_named_code("alpha"),
    )
    _write_plugin(
        tmp_path,
        "beta",
        manifest=_manifest("beta"),
        code=FLAKY_CODE.replace('name="fixture"', 'name="beta"').replace(
            "fixture__ping", "alpha__echo"
        ),
    )
    _write_state(tmp_path, enabled=["alpha", "beta"])
    sink = RecordingTraceSink()
    loader = _loader(tmp_path, sink)
    registry = _load(loader)

    remaining = asyncio.run(
        loader.retry_pending(registry, push_event=lambda source, content: None)
    )

    # La collision reste fatale : elle est tracée et n'entre jamais en retente,
    # et aucun Tool du plugin refusé n'entre dans le registre.
    assert remaining == ()
    assert registry.tools.owner_of("alpha__echo") == "alpha"
    failed = sink.of_kind("PluginState")[-1]
    assert failed.payload["plugin"] == "beta"
    assert failed.payload["state"] == "failed"
    assert "alpha" in failed.payload["reason"]


def test_entrypoint_raising_at_import_fails_the_startup(tmp_path: Path) -> None:
    _write_plugin(tmp_path, code='raise RuntimeError("boom à l\'import")')
    _write_state(tmp_path, enabled=[FIXTURE_NAME])

    with pytest.raises(PluginLoadError) as excinfo:
        _load(_loader(tmp_path, RecordingTraceSink()))

    message = str(excinfo.value)
    assert FIXTURE_NAME in message
    assert "boom à l'import" in message


LEAKY_CODE = '''
def create(context):
    raise RuntimeError(f"refusé pour {context.secrets['token']}")
'''


def test_a_failed_plugin_never_leaks_a_secret_in_its_reason(tmp_path: Path) -> None:
    secret = "s3cr3t-token-value"
    _write_plugin(
        tmp_path,
        code=LEAKY_CODE,
        manifest=_manifest(secrets=[{"key": "token", "required": True}]),
    )
    _write_state(tmp_path, enabled=[FIXTURE_NAME])
    sink = RecordingTraceSink()

    _load(_loader(tmp_path, sink, env={"ATHENA_PLUGIN_FIXTURE_TOKEN": secret}))

    [failed] = sink.of_kind("PluginState")
    assert failed.payload["state"] == "failed"
    assert secret not in json.dumps([entry.payload for entry in sink.entries])
    assert "***" in failed.payload["reason"]


def test_startup_traces_every_plugin_state_in_one_deterministic_pass(
    tmp_path: Path,
) -> None:
    for name in ("alpha", "gamma", "delta"):
        _write_plugin(tmp_path, name, manifest=_manifest(name), code=_named_code(name))
    _write_plugin(tmp_path, "beta", manifest=_manifest("beta"), code=ALWAYS_FAILING_CODE)
    _write_state(tmp_path, enabled=["alpha", "disparu", "gamma", "beta"])

    sink = RecordingTraceSink()
    _load(_loader(tmp_path, sink))

    # Activés dans l'ordre du fichier d'état (activé, manquant, activé, en
    # échec), puis les dossiers présents non listés par ordre de nom.
    assert _states(sink) == [
        ("alpha", "activated"),
        ("disparu", "missing"),
        ("gamma", "activated"),
        ("beta", "failed"),
        ("delta", "disabled"),
    ]
