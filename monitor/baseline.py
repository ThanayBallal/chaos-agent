"""P3: capture steady-state metrics under load. TODO(P3)."""

URL = "http://localhost:8080"


def sample(dur=1.0):
    """Drive traffic for dur seconds; return {"error_rate", "p95_ms", "health": {svc: state}}."""
    raise NotImplementedError("P3")


def capture(win=120):
    """Return a dict matching contracts.md baseline.json."""
    raise NotImplementedError("P3")
