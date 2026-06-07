#!/usr/bin/env python3
"""
Tiny stdlib-only client for the local stack, so the Makefile doesn't depend on
curl (the slim service images don't ship curl, and hosts may not either).

Usage:
    python3 scripts/call.py ingest "artificial intelligence"
    python3 scripts/call.py research "What are the latest developments in AI?"
"""
import json
import sys
import urllib.parse
import urllib.request

ORCH = "http://localhost:8000"
RETRIEVAL = "http://localhost:8001"


def _post(url: str, body: dict | None = None, timeout: int = 180) -> dict:
    data = json.dumps(body).encode() if body is not None else b""
    req = urllib.request.Request(
        url, data=data, method="POST",
        headers={"content-type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")
        print(f"HTTP {e.code} from {url}:\n{detail}", file=sys.stderr)
        sys.exit(1)
    except urllib.error.URLError as e:
        print(f"Could not reach {url}: {e.reason}", file=sys.stderr)
        print("Is the stack up? Try: make ps", file=sys.stderr)
        sys.exit(1)


def main() -> None:
    if len(sys.argv) < 2:
        print("usage: call.py {ingest|research} [text]", file=sys.stderr)
        sys.exit(2)
    cmd = sys.argv[1]
    text = sys.argv[2] if len(sys.argv) > 2 else ""

    if cmd == "ingest":
        q = urllib.parse.quote(text or "artificial intelligence")
        out = _post(f"{RETRIEVAL}/ingest?query={q}&max_records=50")
    elif cmd == "research":
        if not text:
            print('Provide a question: make research Q="..."', file=sys.stderr)
            sys.exit(2)
        out = _post(f"{ORCH}/research", {"question": text})
    else:
        print(f"unknown command: {cmd}", file=sys.stderr)
        sys.exit(2)

    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
