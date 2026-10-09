"""
Phase A smoke test — proves the D2E endpoint works AND captures the real wire contract.

Run this FIRST. It:
  1. Acquires one token (static paste, device-code, or ROPC — per config).
  2. Starts one conversation, sends one generative prompt.
  3. Dumps the RAW response(s) to ./capture/ so we can see the true format
     (SSE vs JSON) and, if we deliberately over-send, the throttle response shape.

Usage:
  python smoke_test.py                 # uses config.json
  python smoke_test.py --config x.json
  python smoke_test.py --token "<bearer>"   # fastest: skip app reg entirely
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys
import time

from d2e_client import D2EClient
from token_provider import (DeviceCodeProvider, RopcProvider, StaticTokenProvider)

HERE = pathlib.Path(__file__).parent
CAP = HERE / "capture"


def load_config(path: str) -> dict:
    p = pathlib.Path(path)
    if not p.exists():
        print(f"[!] {path} not found. Copy config.example.json -> config.json and fill it in,\n"
              f"    or pass --token '<bearer>' for a zero-setup smoke test.", file=sys.stderr)
        sys.exit(2)
    return json.loads(p.read_text())


def make_provider(cfg: dict, token_override: str | None):
    if token_override:
        return StaticTokenProvider(token_override)
    mode = cfg.get("auth_mode", "devicecode").lower()
    if mode == "static":
        return StaticTokenProvider(cfg["static_token"])
    if mode == "ropc":
        a = cfg["accounts"][0]
        return RopcProvider(cfg["tenant_id"], cfg["client_id"], a["username"], a["password"])
    return DeviceCodeProvider(cfg["tenant_id"], cfg["client_id"])


def dump(name: str, result) -> None:
    CAP.mkdir(exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    path = CAP / f"{stamp}-{name}.txt"
    path.write_text(
        f"ok={result.ok} status={result.status} latency_ms={result.latency_ms:.0f} "
        f"throttled={result.throttled} conv={result.conversation_id}\n"
        f"error={result.error}\n"
        f"bot_texts={json.dumps(result.bot_texts, indent=2)}\n"
        f"--- RAW RESPONSE ---\n{result.raw}\n",
        encoding="utf-8",
    )
    print(f"    captured -> {path.name}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=str(HERE / "config.json"))
    ap.add_argument("--token", default=None, help="Static bearer token (skips app reg)")
    ap.add_argument("--overload", type=int, default=0,
                    help="Send N rapid generative turns to try to trigger throttling")
    args = ap.parse_args()

    cfg = {} if args.token else load_config(args.config)
    if args.token:
        # minimal config when only a token is supplied
        cfg = {
            "host": os.environ.get("D2E_HOST",
                "ea32dda382d5e9c4a499d7d7f2d31b.f3.environment.api.powerplatform.com"),
            "schema": os.environ.get("D2E_SCHEMA", "new_SOPStandardAgent"),
            "prompts": ["What is the standard operating procedure for onboarding?"],
        }

    provider = make_provider(cfg, args.token)
    client = D2EClient(cfg["host"], cfg["schema"],
                       cfg.get("api_version", "2022-03-01-preview"))

    print("[1] Acquiring token ...")
    token = provider.get_token()
    print(f"    token acquired (len={len(token)}).")

    print("[2] Starting conversation ...")
    start = client.start_conversation(token)
    dump("start", start)
    print(f"    start: ok={start.ok} status={start.status} "
          f"latency={start.latency_ms:.0f}ms conv={start.conversation_id}")
    if not start.conversation_id:
        print("[!] No conversationId returned — inspect the capture file for the real "
              "start-conversation contract before proceeding.", file=sys.stderr)
        return

    prompt = cfg.get("prompts", ["Hello"])[0]
    print(f"[3] Sending prompt: {prompt!r}")
    turn = client.send_message(token, start.conversation_id, prompt)
    dump("turn", turn)
    print(f"    turn: ok={turn.ok} status={turn.status} latency={turn.latency_ms:.0f}ms "
          f"throttled={turn.throttled}")
    for t in turn.bot_texts:
        print(f"    bot> {t[:300]}")

    if args.overload:
        print(f"[4] Overload: firing {args.overload} rapid turns to probe throttling ...")
        throttled_at = None
        for i in range(args.overload):
            r = client.send_message(token, start.conversation_id, prompt)
            flag = "THROTTLED" if r.throttled else ("ERR" if not r.ok else "ok")
            print(f"    #{i+1:03d} status={r.status} {flag} {r.latency_ms:.0f}ms")
            if r.throttled and throttled_at is None:
                throttled_at = i + 1
                dump(f"throttle-{i+1}", r)
        if throttled_at:
            print(f"[=] First throttle at rapid turn #{throttled_at}")
        else:
            print("[=] No throttling observed in this burst.")

    print("\nDone. Inspect ./capture/*.txt for raw wire format and throttle shape.")


if __name__ == "__main__":
    main()
