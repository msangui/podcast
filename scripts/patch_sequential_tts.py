"""
Replaces 'TTS Rate Limit' + 'Generate Voice' (HTTP Request) with a single
'Generate Voice (Sequential)' Code node that calls ElevenLabs one line at a
time using require('https'), eliminating the 429 rate-limit problem.

Run from repo root: python3 scripts/patch_sequential_tts.py
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

JS_SEQUENTIAL_TTS = r"""
const https = require('https');

function postElevenlabs(voiceId, text, apiKey) {
  return new Promise((resolve, reject) => {
    const body = JSON.stringify({
      text,
      model_id: 'eleven_multilingual_v2',
      voice_settings: { stability: 0.5, similarity_boost: 0.75 }
    });
    const req = https.request({
      hostname: 'api.elevenlabs.io',
      path: `/v1/text-to-speech/${voiceId}`,
      method: 'POST',
      headers: {
        'xi-api-key':     apiKey,
        'Content-Type':   'application/json',
        'Accept':         'audio/mpeg',
        'Content-Length': Buffer.byteLength(body)
      }
    }, (res) => {
      if (res.statusCode !== 200) {
        let errBody = '';
        res.on('data', c => errBody += c);
        res.on('end', () => reject(new Error(`ElevenLabs HTTP ${res.statusCode}: ${errBody}`)));
        return;
      }
      const chunks = [];
      res.on('data', c => chunks.push(Buffer.isBuffer(c) ? c : Buffer.from(c)));
      res.on('end',  () => resolve(Buffer.concat(chunks)));
      res.on('error', reject);
    });
    req.on('error', reject);
    req.write(body);
    req.end();
  });
}

const lines  = $input.all();
const apiKey = $env.ELEVENLABS_API_KEY;
const results = [];

for (const item of lines) {
  const { text, voice_id, speaker, line_index, total_lines, episode_title } = item.json;
  const audioBuffer = await postElevenlabs(voice_id, text, apiKey);
  results.push({
    json: { speaker, line_index, total_lines, episode_title, bytes: audioBuffer.length },
    binary: {
      data: {
        data:     audioBuffer.toString('base64'),
        mimeType: 'audio/mpeg',
        fileName: `line-${line_index}.mp3`,
        fileSize: audioBuffer.length
      }
    }
  });
  // Brief pause between calls — sequential by design, this is just extra courtesy
  if (line_index < total_lines - 1) {
    await new Promise(r => setTimeout(r, 300));
  }
}

return results;
"""

print(f"→ Fetching Workflow {WF_ID}...")
wf = n8n("GET", f"/workflows/{WF_ID}")

nodes       = wf["nodes"]
connections = wf["connections"]

# Find reference nodes for positioning
split_node  = next((n for n in nodes if n["name"] == "Split Script into Lines"), None)
concat_node = next((n for n in nodes if n["name"] == "Concatenate Audio"), None)

if not split_node or not concat_node:
    print("  ✗ Could not find 'Split Script into Lines' or 'Concatenate Audio'")
    raise SystemExit(1)

# Remove old TTS Rate Limit and Generate Voice nodes
REMOVE = {"TTS Rate Limit", "Generate Voice"}
nodes = [n for n in nodes if n["name"] not in REMOVE]
print(f"  ✓ Removed nodes: {REMOVE}")

# Place new node between split and concat
mid_x = (split_node["position"][0] + concat_node["position"][0]) // 2
mid_y = (split_node["position"][1] + concat_node["position"][1]) // 2

SEQ_NODE = {
    "parameters": {
        "jsCode": JS_SEQUENTIAL_TTS,
        "mode":   "runOnceForAllItems"
    },
    "id":   "generate-voice-sequential",
    "name": "Generate Voice (Sequential)",
    "type": "n8n-nodes-base.code",
    "typeVersion": 2,
    "position": [mid_x, mid_y]
}
nodes.append(SEQ_NODE)
print("  ✓ Added 'Generate Voice (Sequential)' node")

# Clean up old connections for removed nodes
for name in REMOVE:
    connections.pop(name, None)

# Re-wire: Split Script into Lines → Generate Voice (Sequential) → Concatenate Audio
connections["Split Script into Lines"] = {
    "main": [[{"node": "Generate Voice (Sequential)", "type": "main", "index": 0}]]
}
connections["Generate Voice (Sequential)"] = {
    "main": [[{"node": "Concatenate Audio", "type": "main", "index": 0}]]
}
print("  ✓ Wired: Split Script into Lines → Generate Voice (Sequential) → Concatenate Audio")

n8n("PUT", f"/workflows/{WF_ID}", {
    "name": wf["name"], "nodes": nodes,
    "connections": connections, "settings": wf["settings"]
})
print("  ✓ Saved")
