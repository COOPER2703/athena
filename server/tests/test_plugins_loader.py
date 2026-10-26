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
from server.core.plugins import PluginBinding, ToolBinding
from server.core.ports import ToolSpec


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
from server.core.plugins import PluginBinding, ToolBinding
from server.core.ports import ToolSpec


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
