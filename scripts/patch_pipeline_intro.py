"""
Patches Workflow 01 — updates the Concatenate Audio node to prepend /assets/intro.mp3.
Run from repo root: python3 scripts/patch_pipeline_intro.py
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
WF_ID       = env.get("N8N_WORKFLOW_ID_DAILY_PIPELINE", "RF67xT0WExE8Xd2U")

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

JS_CONCAT_AUDIO_WITH_INTRO = r"""
const items = $input.all();
const date  = new Date().toISOString().split('T')[0];
const fname = `circuit-breakers-${date}.mp3`;

// Prepend intro if it exists on disk
const fs = require('fs');
const introPath = '/assets/intro.mp3';
const introBuffer = fs.existsSync(introPath) ? fs.readFileSync(introPath) : Buffer.alloc(0);

const ttsBuffers = items.map(item => {
  const b64 = item.binary?.data?.data || '';
  return Buffer.from(b64, 'base64');
});

const combined = Buffer.concat([introBuffer, ...ttsBuffers]);

return [{
  json: { file_name: fname, line_count: items.length, has_intro: introBuffer.length > 0 },
  binary: {
    data: {
      data:     combined.toString('base64'),
      mimeType: 'audio/mpeg',
      fileName: fname,
      fileSize: combined.length
    }
  }
}];
"""

print(f"→ Fetching Workflow {WF_ID}...")
wf = n8n("GET", f"/workflows/{WF_ID}")

# Find and update the Concatenate Audio node
updated = False
for node in wf["nodes"]:
    if node["name"] == "Concatenate Audio":
        node["parameters"]["jsCode"] = JS_CONCAT_AUDIO_WITH_INTRO
        updated = True
        print("  ✓ Found 'Concatenate Audio' node — patching jsCode")
        break

if not updated:
    print("  ✗ 'Concatenate Audio' node not found — check workflow")
    raise SystemExit(1)

# Build minimal PUT payload — only these four fields are accepted
payload = {
    "name":        wf["name"],
    "nodes":       wf["nodes"],
    "connections": wf["connections"],
    "settings":    wf["settings"],
}

print("→ Updating workflow...")
n8n("PUT", f"/workflows/{WF_ID}", payload)
print("  ✓ Done — Concatenate Audio will now prepend /assets/intro.mp3")
