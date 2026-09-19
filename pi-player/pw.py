"""Thin wrappers over PipeWire's command-line tools (slow - keep off the UI thread)."""

from __future__ import annotations

import json
import logging
import subprocess

log = logging.getLogger(__name__)
_reported: set[str] = set()


def _report(message: str) -> None:
    # Once each, so a retry loop can't flood the journal.
    if message not in _reported:
        _reported.add(message)
        log.warning("%s", message)


def _run(args: list[str], timeout: float = 5.0) -> str | None:
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        _report(f"{args[0]} unavailable: {exc}")
        return None
    if result.returncode != 0:
        _report(f"{' '.join(args[:2])} failed ({result.returncode}): {result.stderr.strip()}")
        return None
    return result.stdout


def nodes() -> list[dict]:
    """Every node as {id, name, description, media_class}."""
    out = _run(["pw-dump"])
    if not out:
        return []
    try:
        objects = json.loads(out)
    except ValueError:
        return []
    found = []
    for obj in objects:
        if obj.get("type") != "PipeWire:Interface:Node":
            continue
        props = (obj.get("info") or {}).get("props") or {}
        found.append({
            "id": obj.get("id"),
            "name": props.get("node.name", ""),
            "description": props.get("node.description", ""),
            "media_class": props.get("media.class", ""),
        })
    return found


def find_node(name: str, among: list[dict] | None = None) -> dict | None:
    return next((n for n in (nodes() if among is None else among) if n["name"] == name), None)


def sinks(among: list[dict] | None = None) -> list[dict]:
    return [n for n in (nodes() if among is None else among) if n["media_class"] == "Audio/Sink"]


def set_props(node_id: int, params: dict[str, float]) -> bool:
    """Set filter-chain controls, e.g. {"eq_band_1:Gain": 3.0}."""
    flat: list = []
    for key, value in params.items():
        flat += [key, float(value)]
    payload = json.dumps({"params": flat})
    return _run(["pw-cli", "set-param", str(node_id), "Props", payload]) is not None


def set_default_sink(node_id: int) -> bool:
    return _run(["wpctl", "set-default", str(node_id)]) is not None


def set_target(node_id: int, sink: dict) -> bool:
    """Route a stream to a sink (target.object, plus the older target.node)."""
    ok = _run(["pw-metadata", str(node_id), "target.object", sink["name"]]) is not None
    _run(["pw-metadata", str(node_id), "target.node", str(sink["id"])])
    return ok
