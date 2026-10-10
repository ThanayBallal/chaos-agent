PRJ = "robot-shop"
MAX_DUR = 60
FAULTS = {
    "kill": {"duration_s"},
    "pause": {"duration_s"},
    "latency": {"duration_s", "delay_ms"},
    "cpu_stress": {"duration_s", "cpus"},
}
ERR_MAX = 0.5
ERR_HOLD_S = 10
P95_MAX = 5000
