"""P3: fault injection on compose services. Every inject returns a revert function. TODO(P3)."""

PRJ = "robot-shop"


def container(svc):
    """Return the docker container for compose service svc in project robot-shop."""
    raise NotImplementedError("P3")


def inject(exp):
    """Apply exp (plan.json item). Return a zero-arg revert() that undoes it."""
    raise NotImplementedError("P3: kill / pause / latency / cpu_stress")
