#!/usr/bin/env python3
"""Stand-in for `gh` in the publisher tests. Logs every call (args + which token) to $FAKE_GH_LOG and
answers from the JSON fixture in $FAKE_GH_FIXTURE: {"<args joined by spaces>": <json or string>}.
The longest fixture key that prefixes the call wins. Never touches the network."""
import json
import os
import sys

args = sys.argv[1:]
with open(os.environ["FAKE_GH_LOG"], "a", encoding="utf-8") as f:
    f.write(json.dumps({"args": args, "token": os.environ.get("GH_TOKEN", "")}) + "\n")
fixture = json.load(open(os.environ["FAKE_GH_FIXTURE"], encoding="utf-8"))
line = " ".join(args)
best = max((k for k in fixture if line.startswith(k)), key=len, default=None)
if best is None:
    print("", end="")
    sys.exit(0)
value = fixture[best]
print(value if isinstance(value, str) else json.dumps(value))
