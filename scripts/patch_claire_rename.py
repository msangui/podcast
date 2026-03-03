"""
Renames HANS → CLAIRE in the live Daily Pipeline workflow nodes:
  - Split Script into Lines: speaker tag, variable names, env var
  - Generate Voice (Sequential): no HANS refs, but verify

Run from repo root: python3 scripts/patch_claire_rename.py
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

JS_SPLIT = r"""
const writerOut   = $('Parse Writer Response').first().json;
const showFormat  = $('Fetch show-format.json').first().json;

// Env vars win for voice ID; show-format.json supplies voice_settings
const claireVoiceId  = $env.ELEVENLABS_CLAIRE_VOICE_ID || showFormat.hosts.host_1.voice_id;
const flintVoiceId   = $env.ELEVENLABS_FLINT_VOICE_ID  || showFormat.hosts.host_2.voice_id;
const claireSettings = showFormat.hosts.host_1.voice_settings
                    || { stability: 0.28, similarity_boost: 0.75, style: 0.40, use_speaker_boost: true };
const flintSettings  = showFormat.hosts.host_2.voice_settings
                    || { stability: 0.45, similarity_boost: 0.75, style: 0.20, use_speaker_boost: true };

const lines = writerOut.script
  .split('\n')
  .map(l => l.trim())
  .filter(l => l.startsWith('CLAIRE:') || l.startsWith('FLINT:'));

return lines.map((line, index) => {
  const isClaire = line.startsWith('CLAIRE:');
  const text     = line.replace(/^(CLAIRE|FLINT):\s*/, '').trim();
  return {
    json: {
      text,
      voice_id:       isClaire ? claireVoiceId  : flintVoiceId,
      voice_settings: isClaire ? claireSettings : flintSettings,
      speaker:        isClaire ? 'CLAIRE'       : 'FLINT',
      line_index:     index,
      total_lines:    lines.length,
      episode_title:  writerOut.episode_title
    }
  };
});
"""

print(f"→ Fetching Workflow {WF_ID}...")
wf = n8n("GET", f"/workflows/{WF_ID}")

for node in wf["nodes"]:
    if node["name"] == "Split Script into Lines":
        node["parameters"]["jsCode"] = JS_SPLIT
        print("  ✓ Patched 'Split Script into Lines' — HANS → CLAIRE")

n8n("PUT", f"/workflows/{WF_ID}", {
    "name": wf["name"], "nodes": wf["nodes"],
    "connections": wf["connections"], "settings": wf["settings"]
})
print("  ✓ Saved")
