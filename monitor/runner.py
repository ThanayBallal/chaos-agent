"""P3: run one experiment end to end. TODO(P3)."""


def run(exp, bas, grd):
    """inject -> guard.register(revert) -> sample each 1s -> grd.check(smp, bas, exp['target'])
    -> revert on timeout or abort -> measure recovery_s -> return dict matching result.json."""
    raise NotImplementedError("P3")
