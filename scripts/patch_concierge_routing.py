"""
Patches Concierge workflow to add proper sub-workflow routing:
  Needs Sub-workflow? (IF) → Route Switch (IF) → CFO | Source Rotation

Also fixes the broken 'Sub-workflow Placeholder' connection.
Run from repo root: python3 scripts/patch_concierge_routing.py
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
N8N_URL          = "http://localhost:5678/api/v1"
N8N_API_KEY      = env["N8N_API_KEY"]
TG_CRED_ID       = "rEjsdWsuMxLgKY8d"
CONCIERGE_ID     = env["N8N_WORKFLOW_ID_CONCIERGE"]
CFO_WF_ID        = env["N8N_WORKFLOW_ID_CFO"]
ROTATION_WF_ID   = env["N8N_WORKFLOW_ID_SOURCE_ROTATION"]

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

print(f"→ Fetching Concierge workflow {CONCIERGE_ID}...")
wf = n8n("GET", f"/workflows/{CONCIERGE_ID}")

# ── Remove the old broken Execute CFO Workflow node (will be re-added) ────────
wf["nodes"] = [n for n in wf["nodes"] if n["name"] not in ("Execute CFO Workflow", "Sub-workflow Placeholder")]
print("  ✓ Removed old sub-workflow nodes")

# ── Add: Route Switch IF node ─────────────────────────────────────────────────
# Routes TRUE branch of "Needs Sub-workflow?" to CFO or Source Rotation
wf["nodes"].append({
    "parameters": {
        "conditions": {
            "options": {"caseSensitive": True, "leftValue": "", "typeValidation": "strict"},
            "conditions": [{
                "id": "is-cfo",
                "leftValue": "={{ $json.route }}",
                "rightValue": "cfo",
                "operator": {"type": "string", "operation": "equals"}
            }],
            "combinator": "and"
        },
        "options": {}
    },
    "id": "route-switch",
    "name": "Route Switch",
    "type": "n8n-nodes-base.if",
    "typeVersion": 2.2,
    "position": [1340, 440]
})

# ── Add: Execute CFO Workflow ──────────────────────────────────────────────────
wf["nodes"].append({
    "parameters": {
        "workflowId": {"value": CFO_WF_ID, "mode": "id"},
        "options": {"waitForSubWorkflow": False}
    },
    "id": "exec-cfo",
    "name": "Execute CFO Workflow",
    "type": "n8n-nodes-base.executeWorkflow",
    "typeVersion": 1.1,
    "position": [1560, 300]
})

# ── Add: Execute Source Rotation Workflow ──────────────────────────────────────
wf["nodes"].append({
    "parameters": {
        "workflowId": {"value": ROTATION_WF_ID, "mode": "id"},
        "options": {"waitForSubWorkflow": False}
    },
    "id": "exec-rotation",
    "name": "Execute Source Rotation",
    "type": "n8n-nodes-base.executeWorkflow",
    "typeVersion": 1.1,
    "position": [1560, 560]
})

print("  ✓ Added Route Switch, Execute CFO Workflow, Execute Source Rotation")

# ── Rebuild connections ────────────────────────────────────────────────────────
wf["connections"] = {
    "Telegram Trigger": {"main": [[
        {"node": "Fetch Concierge Prompt", "type": "main", "index": 0}
    ]]},
    "Fetch Concierge Prompt": {"main": [[
        {"node": "Call Claude - Concierge", "type": "main", "index": 0}
    ]]},
    "Call Claude - Concierge": {"main": [[
        {"node": "Parse Claude Response", "type": "main", "index": 0}
    ]]},
    # Parse fans out: always reply to Telegram + check if sub-workflow needed
    "Parse Claude Response": {"main": [[
        {"node": "Send Telegram Reply",   "type": "main", "index": 0},
        {"node": "Needs Sub-workflow?",   "type": "main", "index": 0}
    ]]},
    # Needs Sub-workflow? TRUE → Route Switch, FALSE → nothing
    "Needs Sub-workflow?": {"main": [
        [{"node": "Route Switch", "type": "main", "index": 0}],
        []
    ]},
    # Route Switch: TRUE (route==cfo) → Execute CFO, FALSE → Execute Source Rotation
    "Route Switch": {"main": [
        [{"node": "Execute CFO Workflow",    "type": "main", "index": 0}],
        [{"node": "Execute Source Rotation", "type": "main", "index": 0}]
    ]}
}

print("  ✓ Rebuilt connections")

# ── PUT minimal payload ────────────────────────────────────────────────────────
payload = {
    "name":        wf["name"],
    "nodes":       wf["nodes"],
    "connections": wf["connections"],
    "settings":    wf["settings"],
}

print("→ Updating Concierge workflow...")
n8n("PUT", f"/workflows/{CONCIERGE_ID}", payload)
print("  ✓ Done")
print()
print("Routing:")
print("  Parse Claude Response → Needs Sub-workflow? IF")
print("  Needs Sub-workflow? TRUE → Route Switch IF (route == 'cfo'?)")
print("  Route Switch TRUE  → Execute CFO Workflow")
print("  Route Switch FALSE → Execute Source Rotation")
