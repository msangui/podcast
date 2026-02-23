"""
Patches Workflow 01 — sets responseFormat: json on all HTTP nodes that
fetch .json files from GitHub (served as text/plain, so auto-detect fails).
Run from repo root: python3 scripts/patch_json_response_format.py
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
N8N_URL = "http://localhost:5678/api/v1"
N8N_API_KEY = env["N8N_API_KEY"]
WF_ID = env.get("N8N_WORKFLOW_ID_DAILY_PIPELINE", "RF67xT0WExE8Xd2U")

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

# Nodes that fetch .json config files — GitHub serves them as text/plain
# so n8n won't auto-parse; force responseFormat: json
JSON_FETCH_NODES = {"Fetch sources.json", "Fetch show-format.json"}

print(f"→ Fetching Workflow {WF_ID}...")
wf = n8n("GET", f"/workflows/{WF_ID}")

for node in wf["nodes"]:
    if node["name"] in JSON_FETCH_NODES:
        node["parameters"]["options"] = {
            "response": {"response": {"responseFormat": "json"}}
        }
        print(f"  ✓ Patched '{node['name']}' → responseFormat: json")

payload = {
    "name":        wf["name"],
    "nodes":       wf["nodes"],
    "connections": wf["connections"],
    "settings":    wf["settings"],
}

print("→ Updating workflow...")
n8n("PUT", f"/workflows/{WF_ID}", payload)
print("  ✓ Done")
