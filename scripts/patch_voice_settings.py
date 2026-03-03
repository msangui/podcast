"""
Patches two workflow nodes to support per-host ElevenLabs voice settings:
  - Split Script into Lines: passes voice_settings from show-format.json per item
  - Generate Voice (Sequential): uses per-item voice_settings instead of hardcoded values

Run from repo root: python3 scripts/patch_voice_settings.py
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
const claireVoiceId   = $env.ELEVENLABS_CLAIRE_VOICE_ID  || showFormat.hosts.host_1.voice_id;
const flintVoiceId  = $env.ELEVENLABS_FLINT_VOICE_ID || showFormat.hosts.host_2.voice_id;
const claireSettings  = showFormat.hosts.host_1.voice_settings
                   || { stability: 0.28, similarity_boost: 0.75, style: 0.40, use_speaker_boost: true };
const flintSettings = showFormat.hosts.host_2.voice_settings
                   || { stability: 0.45, similarity_boost: 0.75, style: 0.20, use_speaker_boost: true };

const lines = writerOut.script
  .split('\n')
  .map(l => l.trim())
  .filter(l => l.startsWith('CLAIRE:') || l.startsWith('FLINT:'));

return lines.map((line, index) => {
  const isClaire = line.startsWith('CLAIRE:');
  const text   = line.replace(/^(CLAIRE|FLINT):\s*/, '').trim();
  return {
    json: {
      text,
      voice_id:      isClaire ? claireVoiceId   : flintVoiceId,
      voice_settings: isClaire ? claireSettings  : flintSettings,
      speaker:       isClaire ? 'CLAIRE'        : 'FLINT',
      line_index:    index,
      total_lines:   lines.length,
      episode_title: writerOut.episode_title
    }
  };
});
"""

JS_GENERATE_VOICE = r"""
const https = require('https');

function postElevenlabs(voiceId, text, voiceSettings, apiKey) {
  return new Promise((resolve, reject) => {
    const body = JSON.stringify({
      text,
      model_id: 'eleven_multilingual_v2',
      voice_settings: voiceSettings
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
  const { text, voice_id, voice_settings, speaker, line_index, total_lines, episode_title } = item.json;
  const audioBuffer = await postElevenlabs(voice_id, text, voice_settings, apiKey);
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
  if (line_index < total_lines - 1) {
    await new Promise(r => setTimeout(r, 300));
  }
}

return results;
"""

print(f"→ Fetching Workflow {WF_ID}...")
wf = n8n("GET", f"/workflows/{WF_ID}")

for node in wf["nodes"]:
    if node["name"] == "Split Script into Lines":
        node["parameters"]["jsCode"] = JS_SPLIT
        print("  ✓ Patched 'Split Script into Lines' — passes voice_settings per item")
    elif node["name"] == "Generate Voice (Sequential)":
        node["parameters"]["jsCode"] = JS_GENERATE_VOICE
        print("  ✓ Patched 'Generate Voice (Sequential)' — uses per-item voice_settings")

n8n("PUT", f"/workflows/{WF_ID}", {
    "name": wf["name"], "nodes": wf["nodes"],
    "connections": wf["connections"], "settings": wf["settings"]
})
print("  ✓ Saved")
