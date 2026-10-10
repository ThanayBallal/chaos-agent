"""P2: build depmap.json from robot-shop's docker-compose.yaml. TODO(P2)."""
import json
from pathlib import Path

ROT = Path(__file__).resolve().parent.parent
CMP = ROT.parent / "robot-shop" / "docker-compose.yaml"


def build_depmap(pth=CMP):
    """Return a dict matching contracts.md depmap.json. Every edge needs evidence."""
    raise NotImplementedError("P2: parse compose services, depends_on, env hostnames")


def to_mermaid(dep):
    """Return Mermaid 'graph LR' text for dep."""
    raise NotImplementedError("P2")


if __name__ == "__main__":
    dep = build_depmap()
    (ROT / "data" / "depmap.json").write_text(json.dumps(dep, indent=2))
    (ROT / "data" / "depmap.mmd").write_text(to_mermaid(dep))
    print(f"{len(dep['services'])} services, {len(dep['edges'])} edges")
