"""
Inserts a 'TTS Rate Limit' Code node (1.2 s delay per item) between
'Split Lines' and 'Generate Voice' to avoid ElevenLabs 429 errors.

Run from repo root: python3 scripts/patch_tts_ratelimit.py
"""
import json, urllib.request, urllib.error

def load_env(path="./.env"):
    env = {}
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                env[k.strip()] = v.strip()
    return env

env = load_env()
N8N_URL     = "http://localhost:5678/api/v1"
N8N_API_KEY = env["N8N_API_KEY"]
WF_ID       = env["N8N_WORKFLOW_ID_DAILY_PIPELINE"]

def n8n(method, path, data=None):
    body = json.dumps(data).encode() if data else None
    req  = urllib.request.Request(
        f"{N8N_URL}{path}", data=body,
        headers={"X-N8N-API-KEY": N8N_API_KEY, "Content-Type": "application/json"},
        method=method
    )
    try:
        with urllib.request.urlopen(req) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        print(f"  ✗ HTTP {e.code}: {e.read().decode()}")
        raise

print(f"→ Fetching Workflow {WF_ID}...")
wf = n8n("GET", f"/workflows/{WF_ID}")

nodes       = wf["nodes"]
connections = wf["connections"]

# Find the positions of Split Lines and Generate Voice for layout
split_node  = next((n for n in nodes if n["name"] == "Split Script into Lines"), None)
voice_node  = next((n for n in nodes if n["name"] == "Generate Voice"), None)

if not split_node or not voice_node:
    print("  ✗ Could not find 'Split Script into Lines' or 'Generate Voice' nodes")
    raise SystemExit(1)

# Place the delay node halfway between them
mid_x = (split_node["position"][0] + voice_node["position"][0]) // 2
mid_y = (split_node["position"][1] + voice_node["position"][1]) // 2

DELAY_NODE = {
    "parameters": {
        "jsCode": (
            "// Throttle ElevenLabs to ~50 req/min (free tier: 2 concurrent)\n"
            "await new Promise(r => setTimeout(r, 1200));\n"
            "return items;"
        ),
        "mode": "runOnceForEachItem"
    },
    "id":   "tts-rate-limit",
    "name": "TTS Rate Limit",
    "type": "n8n-nodes-base.code",
    "typeVersion": 2,
    "position": [mid_x, mid_y]
}

# Only add if not already present
if not any(n["name"] == "TTS Rate Limit" for n in nodes):
    nodes.append(DELAY_NODE)
    print("  ✓ Added 'TTS Rate Limit' node")
else:
    print("  ✓ 'TTS Rate Limit' node already present — updating code only")
    for n in nodes:
        if n["name"] == "TTS Rate Limit":
            n["parameters"]["jsCode"] = DELAY_NODE["parameters"]["jsCode"]

# Re-wire: Split Script into Lines → TTS Rate Limit → Generate Voice
# Remove any direct Split Script into Lines → Generate Voice connection
split_conns = connections.get("Split Script into Lines", {}).get("main", [[]])
new_split_conns = []
for branch in split_conns:
    new_split_conns.append([c for c in branch if c["node"] != "Generate Voice"])
connections["Split Script into Lines"] = {"main": new_split_conns}

# Add Split Script into Lines → TTS Rate Limit (only if not already there)
already_linked = any(
    c["node"] == "TTS Rate Limit"
    for branch in connections["Split Script into Lines"]["main"]
    for c in branch
)
if not already_linked:
    if connections["Split Script into Lines"]["main"]:
        connections["Split Script into Lines"]["main"][0].append(
            {"node": "TTS Rate Limit", "type": "main", "index": 0}
        )
    else:
        connections["Split Script into Lines"]["main"] = [
            [{"node": "TTS Rate Limit", "type": "main", "index": 0}]
        ]

# Add TTS Rate Limit → Generate Voice
connections["TTS Rate Limit"] = {
    "main": [[{"node": "Generate Voice", "type": "main", "index": 0}]]
}
print("  ✓ Re-wired: Split Lines → TTS Rate Limit → Generate Voice")

n8n("PUT", f"/workflows/{WF_ID}", {
    "name": wf["name"], "nodes": nodes,
    "connections": connections, "settings": wf["settings"]
})
print("  ✓ Saved")
