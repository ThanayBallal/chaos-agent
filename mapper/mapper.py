"""Build Robot Shop's dependency map from its Docker Compose file."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from pathlib import Path
from urllib.parse import urlsplit

import yaml


ROT = Path(__file__).resolve().parent.parent
CMP = ROT.parent / "robot-shop" / "docker-compose.yaml"

_VALID_KINDS = {"app", "db", "cache", "queue", "proxy"}


def _normalise_image(image: object) -> str:
    """Return an image reference without a digest/tag, suitable for matching."""
    value = str(image or "").lower().split("@", 1)[0]
    last_slash = value.rfind("/")
    last_colon = value.rfind(":")
    if last_colon > last_slash:
        value = value[:last_colon]
    return value


def _classify_kind(service_name: str, service: Mapping[str, object]) -> str:
    """Classify a service using its image name, with build/name fallbacks."""
    image = _normalise_image(service.get("image"))
    build = service.get("build")
    if isinstance(build, Mapping):
        build_hint = str(build.get("context", ""))
    else:
        build_hint = str(build or "")

    # Image is the primary signal. The fallbacks handle build-only Compose files.
    text = " ".join(part for part in (image, build_hint, service_name) if part).lower()
    tokens = set(re.findall(r"[a-z0-9]+", text))

    if tokens & {
        "mongo",
        "mongodb",
        "mysql",
        "mariadb",
        "postgres",
        "postgresql",
        "cassandra",
        "couchdb",
        "database",
        "db",
    }:
        return "db"
    if tokens & {"redis", "memcached", "cache"}:
        return "cache"
    if tokens & {
        "rabbitmq",
        "kafka",
        "nats",
        "activemq",
        "pulsar",
        "queue",
        "broker",
    }:
        return "queue"
    if tokens & {"nginx", "haproxy", "traefik", "envoy", "proxy", "gateway", "web"}:
        return "proxy"
    return "app"


def _depends_on(value: object) -> list[str]:
    """Normalise short- and long-form Compose depends_on declarations."""
    if value is None:
        return []
    if isinstance(value, Mapping):
        return [str(name) for name in value]
    if isinstance(value, list):
        return [str(name) for name in value]
    if isinstance(value, str):
        return [value]
    raise ValueError(f"unsupported depends_on value: {value!r}")


def _environment_items(value: object) -> list[tuple[str, str]]:
    """Normalise mapping- and list-form Compose environment variables."""
    if value is None:
        return []
    if isinstance(value, Mapping):
        return [
            (str(key), str(item))
            for key, item in value.items()
            if item is not None
        ]
    if isinstance(value, list):
        items: list[tuple[str, str]] = []
        for item in value:
            if isinstance(item, str) and "=" in item:
                key, raw_value = item.split("=", 1)
                items.append((key, raw_value))
        return items
    raise ValueError(f"unsupported environment value: {value!r}")


def _networks(value: object) -> set[str]:
    """Normalise short- and long-form Compose network declarations."""
    if value is None:
        return {"default"}
    if isinstance(value, Mapping):
        return {str(name) for name in value}
    if isinstance(value, list):
        return {str(name) for name in value}
    if isinstance(value, str):
        return {value}
    raise ValueError(f"unsupported networks value: {value!r}")


def _network_aliases(
    services: Mapping[str, Mapping[str, object]],
) -> dict[tuple[str, str], str]:
    """Map ``(network, alias)`` to the service represented by that alias."""
    aliases: dict[tuple[str, str], str] = {}
    for service_name, service in services.items():
        raw_networks = service.get("networks")
        if not isinstance(raw_networks, Mapping):
            continue
        for network_name, network_config in raw_networks.items():
            if not isinstance(network_config, Mapping):
                continue
            raw_aliases = network_config.get("aliases", [])
            if isinstance(raw_aliases, str):
                raw_aliases = [raw_aliases]
            if isinstance(raw_aliases, list):
                for alias in raw_aliases:
                    aliases[(str(network_name), str(alias))] = service_name
    return aliases


def _possible_hosts(value: str) -> set[str]:
    """Extract host-like values from hostnames, URLs and connection strings."""
    text = value.strip().strip("'\"")
    if not text:
        return set()

    hosts = {text}

    # Parse complete or comma/semicolon-separated URLs, including credentials.
    for match in re.finditer(r"[a-zA-Z][a-zA-Z0-9+.-]*://[^\s,;]+", text):
        try:
            hostname = urlsplit(match.group(0)).hostname
        except ValueError:
            hostname = None
        if hostname:
            hosts.add(hostname)

    # Also support HOST:PORT, host=HOST and ${VAR:-HOST} forms.
    for token in re.split(r"[\s,;]+", text):
        token = token.strip("'\"")
        if "=" in token and not token.startswith("${"):
            token = token.rsplit("=", 1)[-1]
        default_match = re.fullmatch(r"\$\{[^}:]+:-([^}]+)\}", token)
        if default_match:
            token = default_match.group(1)
        if "://" in token:
            continue
        token = token.rsplit("@", 1)[-1]
        token = token.split("/", 1)[0]
        token = token.split(":", 1)[0]
        if token:
            hosts.add(token)
    return hosts


def _referenced_services(
    source: str,
    value: str,
    service_names: set[str],
    service_networks: Mapping[str, set[str]],
    aliases: Mapping[tuple[str, str], str],
) -> list[str]:
    """Resolve service hostnames (and network aliases) found in an env value."""
    found: set[str] = set()
    possible_hosts = _possible_hosts(value)

    for host in possible_hosts:
        if host in service_names and host != source:
            found.add(host)

        for network_name in service_networks[source]:
            target = aliases.get((network_name, host))
            if target and target != source:
                found.add(target)

    return sorted(found)


def _edge_type(target_kind: str) -> str:
    """Use infrastructure kinds directly and HTTP for application calls."""
    return target_kind if target_kind in {"db", "cache", "queue"} else "http"


def _environment_evidence(name: str, value: str, target: str) -> str:
    """Describe env evidence without exposing URL credentials or query values."""
    simple_value = value.strip().strip("'\"")
    if re.fullmatch(r"[a-zA-Z0-9_.-]+(?::[0-9]+)?", simple_value):
        return f"env {name}={simple_value}"
    return f"env {name} references {target}"


def build_depmap(pth=CMP):
    """Return a dictionary matching the ``depmap.json`` team contract.

    Dependencies are discovered from explicit ``depends_on`` entries and from
    environment values that contain another Compose service hostname or a
    network alias. Merely sharing a network is not treated as a dependency.
    """
    compose_path = Path(pth)
    if not compose_path.is_file():
        raise FileNotFoundError(
            f"Compose file not found: {compose_path}. "
            "Clone Robot Shop as a sibling directory named 'robot-shop'."
        )

    try:
        document = yaml.safe_load(compose_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ValueError(f"invalid Compose YAML in {compose_path}: {exc}") from exc

    if not isinstance(document, Mapping) or not isinstance(document.get("services"), Mapping):
        raise ValueError(f"Compose file has no services mapping: {compose_path}")

    raw_services = document["services"]
    services: dict[str, Mapping[str, object]] = {}
    for name, config in raw_services.items():
        if config is None:
            config = {}
        if not isinstance(config, Mapping):
            raise ValueError(f"service {name!r} must be a mapping")
        services[str(name)] = config

    service_names = set(services)
    service_order = {name: index for index, name in enumerate(services)}
    kinds = {name: _classify_kind(name, config) for name, config in services.items()}
    if not set(kinds.values()) <= _VALID_KINDS:
        raise AssertionError("mapper produced an unsupported service kind")

    service_networks = {
        name: _networks(config.get("networks")) for name, config in services.items()
    }
    aliases = _network_aliases(services)

    # A single logical edge can have multiple independent pieces of evidence.
    edge_evidence: dict[tuple[str, str], list[str]] = {}

    def add_edge(source: str, target: str, evidence: str) -> None:
        if target not in service_names:
            raise ValueError(
                f"service {source!r} depends on unknown Compose service {target!r}"
            )
        key = (source, target)
        evidence_items = edge_evidence.setdefault(key, [])
        if evidence not in evidence_items:
            evidence_items.append(evidence)

    for source, config in services.items():
        for target in _depends_on(config.get("depends_on")):
            add_edge(source, target, "compose depends_on")

        for env_name, env_value in _environment_items(config.get("environment")):
            for target in _referenced_services(
                source,
                env_value,
                service_names,
                service_networks,
                aliases,
            ):
                add_edge(
                    source,
                    target,
                    _environment_evidence(env_name, env_value, target),
                )

    edges = []
    service_output = []
    for source in services:
        outgoing = [
            (target, evidence)
            for (edge_source, target), evidence in edge_evidence.items()
            if edge_source == source
        ]
        outgoing.sort(key=lambda item: service_order[item[0]])

        dependency_evidence: list[str] = []
        for target, evidence_items in outgoing:
            for item in evidence_items:
                if item not in dependency_evidence:
                    dependency_evidence.append(item)
            edges.append(
                {
                    "src": source,
                    "dst": target,
                    "type": _edge_type(kinds[target]),
                    "evidence": "; ".join(evidence_items),
                }
            )

        service_output.append(
            {
                "name": source,
                "kind": kinds[source],
                "depends_on": [target for target, _ in outgoing],
                "evidence": dependency_evidence,
            }
        )

    return {"app": "robot-shop", "services": service_output, "edges": edges}


def to_mermaid(dep):
    """Return a deterministic Mermaid ``graph LR`` representation of a depmap."""
    services = dep.get("services", [])
    edges = dep.get("edges", [])
    identifiers = {
        service["name"]: f"service_{index}" for index, service in enumerate(services)
    }

    lines = ["graph LR"]
    classes: dict[str, list[str]] = {kind: [] for kind in sorted(_VALID_KINDS)}

    for service in services:
        name = str(service["name"])
        kind = str(service["kind"])
        identifier = identifiers[name]
        label = f"{name} ({kind})".replace('"', "&quot;")
        lines.append(f'  {identifier}["{label}"]')
        if kind in classes:
            classes[kind].append(identifier)

    for edge in edges:
        source = identifiers.get(edge["src"])
        target = identifiers.get(edge["dst"])
        if source is None or target is None:
            raise ValueError(f"edge references an unknown service: {edge!r}")
        edge_kind = str(edge["type"]).replace('"', "&quot;")
        lines.append(f'  {source} -->|"{edge_kind}"| {target}')

    lines.extend(
        [
            "  classDef app fill:#dbeafe,stroke:#2563eb,color:#172554",
            "  classDef db fill:#dcfce7,stroke:#16a34a,color:#052e16",
            "  classDef cache fill:#fef3c7,stroke:#d97706,color:#451a03",
            "  classDef queue fill:#f3e8ff,stroke:#9333ea,color:#3b0764",
            "  classDef proxy fill:#fee2e2,stroke:#dc2626,color:#450a0a",
        ]
    )
    for kind, members in classes.items():
        if members:
            lines.append(f"  class {','.join(members)} {kind}")

    return "\n".join(lines) + "\n"


def _mock_differences(actual: Mapping[str, object], mock: Mapping[str, object]) -> list[str]:
    """Return readable structural differences; mock evidence is intentionally ignored."""
    actual_services = {item["name"]: item["kind"] for item in actual.get("services", [])}
    mock_services = {item["name"]: item["kind"] for item in mock.get("services", [])}
    actual_edges = {
        (item["src"], item["dst"], item["type"]) for item in actual.get("edges", [])
    }
    mock_edges = {
        (item["src"], item["dst"], item["type"]) for item in mock.get("edges", [])
    }

    differences: list[str] = []
    for name in sorted(actual_services.keys() - mock_services.keys()):
        differences.append(f"service only in generated map: {name} ({actual_services[name]})")
    for name in sorted(mock_services.keys() - actual_services.keys()):
        differences.append(f"service only in mock: {name} ({mock_services[name]})")
    for name in sorted(actual_services.keys() & mock_services.keys()):
        if actual_services[name] != mock_services[name]:
            differences.append(
                f"kind differs for {name}: generated={actual_services[name]}, "
                f"mock={mock_services[name]}"
            )
    for source, target, edge_kind in sorted(actual_edges - mock_edges):
        differences.append(f"edge only in generated map: {source} -> {target} ({edge_kind})")
    for source, target, edge_kind in sorted(mock_edges - actual_edges):
        differences.append(f"edge only in mock: {source} -> {target} ({edge_kind})")
    return differences


if __name__ == "__main__":
    dep = build_depmap()
    data_dir = ROT / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "depmap.json").write_text(
        json.dumps(dep, indent=2) + "\n", encoding="utf-8"
    )
    (data_dir / "depmap.mmd").write_text(to_mermaid(dep), encoding="utf-8")
    print(f"{len(dep['services'])} services, {len(dep['edges'])} edges")

    mock_path = data_dir / "mock_depmap.json"
    if mock_path.is_file():
        mock = json.loads(mock_path.read_text(encoding="utf-8"))
        differences = _mock_differences(dep, mock)
        if differences:
            print("Differences from data/mock_depmap.json:")
            for difference in differences:
                print(f"  - {difference}")
        else:
            print("Generated structure matches data/mock_depmap.json.")
