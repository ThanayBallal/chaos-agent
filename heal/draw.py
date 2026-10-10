"""Write the heal graph as Mermaid: python -m heal.draw -> docs/heal_graph.mmd"""
from pathlib import Path

from heal.graph import build

OUT = Path(__file__).resolve().parent.parent / "docs" / "heal_graph.mmd"


def draw(out=OUT):
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(build().get_graph().draw_mermaid(), encoding="utf-8")
    return out


if __name__ == "__main__":
    print(f"[heal] wrote {draw()}")
