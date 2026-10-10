"""P4: single LLM entry point, provider-agnostic. Config comes from .env (see agent/.env.example).

LLM_PROVIDER = anthropic | gemini | openai (default anthropic), LLM_API_KEY = key for that provider,
LLM_MODEL optionally overrides the default model, CHAOS_NO_LLM=1 forces callers onto their fallbacks.
Each ask_json call is capped at CAP seconds overall so a slow API cannot hang the demo.
Each SDK is imported only when its provider is selected.
"""
import json
import os
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENVS = [ROOT / ".env", ROOT / "agent" / ".env"]
MDL = {"anthropic": "claude-opus-5-5", "gemini": "gemini-2.5-pro", "openai": "gpt-4.1"}
PKG = {"anthropic": "anthropic", "gemini": "google-genai", "openai": "openai"}
CAP = 25.0


def _env():
    """Load KEY=VALUE lines from .env files without overriding variables already set."""
    for pth in ENVS:
        if not pth.exists():
            continue
        for lin in pth.read_text(encoding="utf-8").splitlines():
            key, sep, val = lin.strip().removeprefix("export ").partition("=")
            if sep and key and not key.startswith("#"):
                os.environ.setdefault(key.strip(), val.strip().strip("'\""))


def _anthropic(key, mdl, sys_prompt, usr_prompt, schema, tmo):
    import anthropic
    cfg = {"effort": "medium"}
    if schema:
        cfg["format"] = {"type": "json_schema", "schema": schema}
    cli = anthropic.Anthropic(api_key=key, timeout=tmo, max_retries=1)
    rsp = cli.beta.messages.create(
        model=mdl, max_tokens=16000, system=sys_prompt,
        messages=[{"role": "user", "content": usr_prompt}],
        output_config=cfg,
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
    )
    if rsp.stop_reason in ("refusal", "max_tokens"):
        raise RuntimeError(f"anthropic stopped with {rsp.stop_reason}")
    return "".join(blk.text for blk in rsp.content if blk.type == "text")


def _openai(key, mdl, sys_prompt, usr_prompt, schema, tmo):
    from openai import OpenAI
    cli = OpenAI(api_key=key, timeout=tmo, max_retries=1)
    rsp = cli.chat.completions.create(
        model=mdl,
        messages=[{"role": "system", "content": sys_prompt}, {"role": "user", "content": usr_prompt}],
        response_format={"type": "json_object"},
    )
    cho = rsp.choices[0]
    if cho.finish_reason != "stop":
        raise RuntimeError(f"openai stopped with {cho.finish_reason}")
    return cho.message.content or ""


def _gemini(key, mdl, sys_prompt, usr_prompt, schema, tmo):
    from google import genai
    cfg = {"system_instruction": sys_prompt, "response_mime_type": "application/json"}
    if mdl.startswith("gemini-3"):
        cfg["thinking_config"] = {"thinking_level": "low"}  # default is high: ~2x slower, blows the time cap
    cli = genai.Client(api_key=key, http_options={"timeout": int(tmo * 1000)})
    rsp = cli.models.generate_content(model=mdl, contents=usr_prompt, config=cfg)
    if not rsp.text:
        raise RuntimeError("gemini returned no text (blocked or empty)")
    return rsp.text


def _bounded(fnc, tmo, *arg):
    """Run fnc(*arg, tmo) in a daemon thread; raise TimeoutError if it is still running after tmo seconds."""
    box = {}

    def run():
        try:
            box["ok"] = fnc(*arg, tmo)
        except BaseException as exc:
            box["err"] = exc

    thr = threading.Thread(target=run, daemon=True)
    thr.start()
    thr.join(tmo)
    if thr.is_alive():
        raise TimeoutError(f"LLM call exceeded the {CAP:g}s cap")
    if "err" in box:
        raise box["err"]
    return box["ok"]


CALL = {"anthropic": _anthropic, "gemini": _gemini, "openai": _openai}


def config():
    """Return (provider, key, model) or raise RuntimeError explaining what is missing."""
    _env()
    if os.environ.get("CHAOS_NO_LLM"):
        raise RuntimeError("LLM disabled by CHAOS_NO_LLM")
    pvd = os.environ.get("LLM_PROVIDER", "anthropic").strip().lower()
    if pvd not in CALL:
        raise RuntimeError(f"LLM_PROVIDER '{pvd}' not one of {sorted(CALL)}")
    key = os.environ.get("LLM_API_KEY", "").strip()
    if not key:
        raise RuntimeError("LLM_API_KEY not set in .env (see agent/.env.example)")
    return pvd, key, os.environ.get("LLM_MODEL", "").strip() or MDL[pvd]


def _strip(txt):
    """Drop a ```json fence if the model added one."""
    txt = txt.strip()
    if txt.startswith("```"):
        txt = txt.split("\n", 1)[-1].rsplit("```", 1)[0]
    return txt


def ask_json(sys_prompt, usr_prompt, tries=3, schema=None, check=None):
    """Return parsed JSON from the LLM; retry on invalid JSON; raise after tries.

    schema: JSON Schema (object root); enforced by anthropic structured outputs, appended to the prompt otherwise.
    check:  fn(obj) -> cleaned obj; raising ValueError retries with the error fed back to the model.
    Config, import, API, refusal and timeout (CAP seconds across all tries) errors are raised so callers can fall back.
    """
    pvd, key, mdl = config()
    try:
        __import__({"gemini": "google.genai"}.get(pvd, pvd))
    except ImportError as err:
        raise RuntimeError(f"LLM_PROVIDER={pvd} needs `pip install {PKG[pvd]}` ({err})") from err
    if schema and pvd != "anthropic":
        sys_prompt = f"{sys_prompt}\n\nReply with a single JSON object matching this JSON Schema:\n{json.dumps(schema)}"
    msg, err, end = usr_prompt, None, time.monotonic() + CAP
    for _ in range(tries):
        rem = end - time.monotonic()
        if rem < 1:
            raise TimeoutError(f"LLM gave no valid JSON within the {CAP:g}s cap: {err}")
        txt = _bounded(CALL[pvd], rem, key, mdl, sys_prompt, msg, schema)
        try:
            obj = json.loads(_strip(txt))
            return check(obj) if check else obj
        except (ValueError, KeyError, TypeError) as exc:
            err = exc
            msg = (f"{usr_prompt}\n\nYour previous reply was rejected: {exc}\n"
                   "Reply again with corrected JSON only.")
    raise ValueError(f"no valid JSON from {pvd} after {tries} tries: {err}")
