"""Chaos Agent dashboard: python -m dashboard.server  ->  http://127.0.0.1:8090

Runs `python -m heal.run ...` as a child process and streams its output to the browser (Server-Sent Events), parsed
into steps by dashboard/parse.py. Buttons: Run, Heal now (sends Enter to the pipeline's hold step), Kill switch
(creates the STOP file the safety guard watches). /api/status polls Docker and the storefront for the live service map.
Replay mode streams a recorded run (dashboard/sample_campaign.log) without Docker: a demo backup.
Binds to 127.0.0.1 only. heal/ is never imported or modified; the UI only reads its output and data/*.json.
"""
import argparse
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import requests
import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from dashboard.parse import Parser

ROT = Path(__file__).resolve().parent.parent
DAT = ROT / "data"
STP = ROT / "STOP"
WEB = Path(__file__).resolve().parent / "static"
REC = Path(__file__).resolve().parent / "sample_campaign.log"
PRJ = "robot-shop"
URL = os.environ.get("ROBOT_SHOP_URL", "http://localhost:8080")
FILES = {"campaign": "heal_campaign.json", "report": "heal_report.json", "code_review": "code_review.json",
         "depmap": "depmap.json"}


class RunReq(BaseModel):
    mode: str = "campaign"  # campaign | auto | prompt | replay
    prompt: str = ""
    pause: bool = True
    max: int = 4
    stub: bool = False


def command(req):
    """heal.run argv for a request (validated; never a shell string)."""
    arg = [sys.executable, "-u", "-m", "heal.run"]
    if req.mode == "prompt":
        txt = req.prompt.strip()
        if not txt or len(txt) > 300:
            raise ValueError("prompt must be 1..300 characters")
        arg.append(txt)
    elif req.mode in ("auto", "campaign"):
        arg.append("--auto")
        if req.mode == "campaign":
            arg += ["--all", "--max", str(max(1, min(int(req.max), 8)))]
    else:
        raise ValueError(f"unknown mode {req.mode!r}")
    arg.append("--stub" if req.stub else "--live")
    if req.pause and not req.stub:
        arg.append("--pause-first" if req.mode == "campaign" else "--pause-before-heal")
    return arg


class Run:
    """One run at a time: the child process (or replay thread) and every output line with its parsed events."""

    def __init__(self):
        self.lck = threading.Lock()
        self.evs = []  # [{"i", "line", "events"}]
        self.prc = None
        self.mod = None
        self.sta = "idle"  # idle | running | done | error
        self.cod = None
        self.par = Parser()
        self.ent = threading.Event()  # replay: "heal now" pressed
        self.gen = 0  # run token: a stale replay thread from an earlier run never writes into a new one

    def busy(self):
        return self.sta == "running"

    def _push(self, lin, gen=None):
        with self.lck:
            if gen is not None and gen != self.gen:
                return False
            self.evs.append({"i": len(self.evs), "line": lin, "events": self.par.feed(lin)})
            return True

    def _reset(self, mod):
        with self.lck:
            self.gen += 1
            self.evs, self.par, self.cod, self.mod, self.sta = [], Parser(), None, mod, "running"
            self.ent.set()  # release a stale replay waiting at its hold
            self.ent = threading.Event()
        return self.gen

    def start(self, req):
        if self.busy():
            raise RuntimeError("a run is already in progress")
        if req.mode == "replay":
            gen = self._reset("replay")
            threading.Thread(target=self._replay, args=(gen, self.ent), daemon=True).start()
            return ["replay", REC.name]
        arg = command(req)
        gen = self._reset(req.mode)
        env = {**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8"}
        self.prc = subprocess.Popen(arg, cwd=ROT, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
                                    bufsize=1, env=env)
        threading.Thread(target=self._pump, args=(gen, self.prc), daemon=True).start()
        return arg[2:]

    def _pump(self, gen, prc):
        for lin in prc.stdout:
            self._push(lin, gen)
        cod = prc.wait()
        if gen == self.gen:
            self.cod, self.sta = cod, ("done" if cod == 0 else "error")
            self._push(f"[dashboard] run finished (exit code {cod})", gen)

    def _replay(self, gen, ent):
        for lin in REC.read_text(encoding="utf-8").splitlines():
            if self.sta != "running" or gen != self.gen:
                return
            self._push(lin, gen)
            if "Press Enter to start healing" in lin:
                ent.wait(30)
            elif lin.startswith(("=== Cycle", "[heal] inject", "[heal] verify")):
                time.sleep(1.2)
            elif lin.startswith(("[heal]", "== ")):
                time.sleep(0.25)
            else:
                time.sleep(0.05)
        if gen == self.gen:
            self.cod, self.sta = 0, "done"
            self._push("[dashboard] replay finished", gen)

    def heal_now(self):
        if self.mod == "replay":
            self.ent.set()
            return True
        if self.prc and self.prc.poll() is None and self.prc.stdin:
            try:
                self.prc.stdin.write("\n")
                self.prc.stdin.flush()
                return True
            except OSError:
                return False
        return False

    def since(self, idx):
        with self.lck:
            return self.evs[idx:]


RUN = Run()
app = FastAPI(title="Chaos Agent dashboard")


@app.get("/")
def index():
    return FileResponse(WEB / "index.html")


@app.post("/api/run")
def run(req: RunReq):
    try:
        arg = RUN.start(req)
    except (ValueError, RuntimeError) as err:
        raise HTTPException(400, str(err)) from err
    return {"ok": True, "command": " ".join(arg)}


@app.post("/api/heal")
def heal():
    return {"ok": RUN.heal_now()}


@app.post("/api/stop")
def stop():
    """Kill switch: create the STOP file. The pipeline aborts and reverts the active fault on its own."""
    if RUN.mod == "replay":
        RUN.sta = "done"
        return {"ok": True, "replay": True}
    STP.touch()
    return {"ok": True, "stop_file": str(STP)}


@app.get("/api/state")
def state():
    return {"status": RUN.sta, "mode": RUN.mod, "exit_code": RUN.cod, "lines": len(RUN.evs),
            "stop_file": STP.exists()}


@app.get("/api/events")
def events(since: int = 0):
    """SSE: every output line with its parsed events, from index `since` (reconnect-safe)."""

    def gen():
        idx, idl = since, 0
        while True:
            new = RUN.since(idx)
            for evt in new:
                yield f"data: {json.dumps(evt)}\n\n"
            idx += len(new)
            if new:
                idl = 0
            else:
                idl += 1
                if idl % 30 == 0:
                    yield ": keep-alive\n\n"
            time.sleep(0.15)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


def _containers():
    try:
        import docker
        cli = docker.from_env()
        out = {}
        for con in cli.containers.list(all=True, filters={"label": f"com.docker.compose.project={PRJ}"}):
            out[con.labels.get("com.docker.compose.service", con.name)] = con.status
        return out, None
    except Exception as err:
        return {}, f"{type(err).__name__}: {err}"[:160]


@app.get("/api/status")
def status():
    """Live service map: container state per compose service, plus the storefront's HTTP status."""
    if RUN.mod == "replay" and RUN.busy():
        return {"replay": True}
    sts, err = _containers()
    try:
        t_0 = time.monotonic()
        rsp = requests.get(URL, timeout=2)
        web = {"up": rsp.status_code == 200, "code": rsp.status_code, "ms": int((time.monotonic() - t_0) * 1000)}
    except requests.RequestException as exc:
        web = {"up": False, "code": None, "error": type(exc).__name__}
    return {"services": sts, "docker_error": err, "storefront": web, "url": URL}


@app.get("/api/data/{name}")
def data(name: str):
    if name not in FILES:
        raise HTTPException(404, "unknown file")
    pth = DAT / FILES[name]
    if not pth.exists():
        return {"missing": True}
    try:
        return json.loads(pth.read_text(encoding="utf-8"))
    except (OSError, ValueError) as err:
        return {"missing": True, "error": str(err)}


def main():
    arg = argparse.ArgumentParser(prog="python -m dashboard.server")
    arg.add_argument("--port", type=int, default=8090)
    opt = arg.parse_args()
    print(f"Chaos Agent dashboard: http://127.0.0.1:{opt.port}")
    uvicorn.run(app, host="127.0.0.1", port=opt.port, log_level="warning")


if __name__ == "__main__":
    main()
