#!/usr/bin/env python3
"""Submit a file to the Talky hackathon and wait for the result (official flow).

    read -rs HK_TOKEN && export HK_TOKEN
    .venv/bin/python scripts/submit.py LIVE out/sub_live.csv.gz
    .venv/bin/python scripts/submit.py FINAL out/sub_final.csv.gz --i-know-final-is-one-shot

Limits: 1 submission / 180 s per phase, 20 / day per phase. Requesting the upload URL already consumes a slot.
"""
import json
import os
import sys
import time

import requests

API = "https://hackathon.usetalky.com/api"
DONE = {"scored", "received", "invalid", "error"}


def main(phase: str, path: str) -> None:
    headers = {"Authorization": f"Bearer {os.environ['HK_TOKEN']}"}
    name = os.path.basename(path)
    r = requests.post(f"{API}/submissions/upload-url", headers=headers, json={"phase": phase, "filename": name}, timeout=30)
    if r.status_code != 201:
        sys.exit(f"upload-url failed: {r.status_code} {r.text}")
    up = r.json()
    sub_id = up["sub_id"]
    with open(path, "rb") as f:
        s3 = requests.post(up["upload"]["url"], data=up["upload"]["fields"], files={"file": (name, f)}, timeout=900)
    if s3.status_code not in (200, 201, 204):
        sys.exit(f"S3 upload failed: {s3.status_code} {s3.text[:500]}")
    r = requests.post(f"{API}/submissions/{sub_id}/score", headers=headers, json={}, timeout=30)
    if r.status_code >= 400:
        sys.exit(f"score failed: {r.status_code} {r.text}")
    sub = r.json()
    deadline = time.time() + 600
    while sub.get("status") not in DONE and time.time() < deadline:
        time.sleep(5)  # the whole venue shares one IP: never poll faster
        sub = requests.get(f"{API}/submissions/{sub_id}", headers=headers, timeout=30).json()
    os.makedirs("out/submissions", exist_ok=True)
    with open(f"out/submissions/{sub_id}.json", "w") as fh:
        json.dump(sub, fh, indent=2, ensure_ascii=False)
    m = sub.get("metrics") or sub.get("result", {}).get("metrics")
    print(json.dumps({k: sub.get(k) for k in ("sub_id", "phase", "status", "score")}, ensure_ascii=False))
    if m:
        print(json.dumps(m, indent=1))
    if sub.get("status") == "invalid":
        print(json.dumps(sub, indent=1, ensure_ascii=False)[:3000])


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if len(args) != 2 or args[0] not in ("LIVE", "FINAL"):
        sys.exit("usage: submit.py LIVE|FINAL file.csv.gz  (HK_TOKEN exported)")
    if args[0] == "FINAL" and "--i-know-final-is-one-shot" not in sys.argv:
        sys.exit("FINAL is ONE shot. Validate first (scripts/run.py prints VALID) and add --i-know-final-is-one-shot")
    if "HK_TOKEN" not in os.environ:
        sys.exit("export HK_TOKEN first (read -rs HK_TOKEN && export HK_TOKEN)")
    main(*args)
