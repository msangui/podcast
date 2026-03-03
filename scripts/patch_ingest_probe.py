"""
Minimal probe — replaces Ingest node with a single fetch() test.
Returns success/error so we know if fetch() works in the Code node sandbox.

Run: python3 scripts/patch_ingest_probe.py
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

JS_PROBE = r"""
const results = [];

// Test 1: is fetch defined at all?
results.push({ test: 'fetch_defined', value: typeof fetch });

// Test 2: try to fetch a simple RSS feed
try {
  const resp = await fetch('https://hnrss.org/frontpage', {
    headers: { 'User-Agent': 'Mozilla/5.0', 'Accept': '*/*' },
    redirect: 'follow'
  });
  const text = await resp.text();
  results.push({
    test: 'hn_fetch',
    status: resp.status,
    ok: resp.ok,
    body_length: text.length,
    body_preview: text.substring(0, 300),
    item_count: (text.match(/<item>/g) || []).length
  });
} catch(e) {
  results.push({ test: 'hn_fetch', error: e.message, error_type: e.constructor.name });
}

// Test 3: try Ars Technica
try {
  const resp = await fetch('https://arstechnica.com/feed/', {
    headers: { 'User-Agent': 'Mozilla/5.0' },
    redirect: 'follow'
  });
  const text = await resp.text();
  results.push({
    test: 'ars_fetch',
    status: resp.status,
    ok: resp.ok,
    body_length: text.length,
    item_count: (text.match(/<item>/g) || []).length
  });
} catch(e) {
  results.push({ test: 'ars_fetch', error: e.message });
}

return [{ json: { probe_results: results } }];
"""

print(f"→ Fetching Workflow {WF_ID}...")
wf = n8n("GET", f"/workflows/{WF_ID}")

for node in wf["nodes"]:
    if node["name"] == "Ingest & Filter News":
        node["parameters"]["jsCode"] = JS_PROBE
        node["parameters"]["mode"]   = "runOnceForAllItems"
        print("  ✓ Patched with fetch() probe")
        break

n8n("PUT", f"/workflows/{WF_ID}", {
    "name": wf["name"], "nodes": wf["nodes"],
    "connections": wf["connections"], "settings": wf["settings"]
})
print("  ✓ Saved. Run the workflow, click 'Ingest & Filter News', paste probe_results here.")
