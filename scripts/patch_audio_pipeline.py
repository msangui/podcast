"""
Fixes the audio pipeline binary data bug.

Root cause: n8n Code nodes store binary data in an internal binary store and
replace item.binary.fieldName.data with a store reference (UUID/path). When
Concatenate Audio reads item.binary?.data?.data it gets the reference, not
the audio — so Buffer.from(uuid, 'base64') produces garbage and the episode
contains only the intro.

Fix: move audio data into item.json (never rewritten by n8n) instead of binary.

  1. Generate Voice (Sequential): adds json.audio_b64
  2. Concatenate Audio: reads from json.audio_b64, adds json.combined_b64
  3. Upload to Buzzsprout: reads from json.combined_b64

Run from repo root: python3 scripts/patch_audio_pipeline.py
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

# ── Generate Voice (Sequential) ───────────────────────────────────────────────
# audio_b64 goes in json — always inline, never stored in binary store
JS_GENERATE_VOICE = r"""
const https = require('https');

function postElevenlabs(voiceId, text, voiceSettings, speed, apiKey) {
  return new Promise((resolve, reject) => {
    const bodyObj = {
      text,
      model_id: 'eleven_multilingual_v2',
      voice_settings: voiceSettings
    };
    if (speed) bodyObj.speed = speed;
    const body = JSON.stringify(bodyObj);
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
  const { text, voice_id, voice_settings, speed, speaker, line_index, total_lines, episode_title } = item.json;
  const audioBuffer = await postElevenlabs(voice_id, text, voice_settings, speed, apiKey);
  results.push({
    json: {
      speaker, line_index, total_lines, episode_title,
      bytes: audioBuffer.length,
      audio_b64: audioBuffer.toString('base64')
    }
  });
  if (line_index < total_lines - 1) {
    await new Promise(r => setTimeout(r, 300));
  }
}

return results;
"""

# ── Concatenate Audio ─────────────────────────────────────────────────────────
# Two-step process:
#   1. Concat all TTS segments into tts_all.mp3
#   2. Mix intro (ducked at t=12s) with TTS delayed by 12s — hosts come in
#      while the intro music is still playing, music fades out at ~19s
JS_CONCAT_AUDIO = r"""
const fs           = require('fs');
const { execSync } = require('child_process');
const items = $input.all();
const date  = new Date().toISOString().split('T')[0];
const fname = `circuit-breakers-${date}.mp3`;

const tmpDir = `/tmp/ep-${Date.now()}`;
fs.mkdirSync(tmpDir, { recursive: true });

// Write TTS segments
const segFiles = [];
items.forEach((item, i) => {
  const b64 = item.json.audio_b64 || '';
  if (!b64) return;
  const dest = `${tmpDir}/${String(i).padStart(4, '0')}-tts.mp3`;
  fs.writeFileSync(dest, Buffer.from(b64, 'base64'));
  segFiles.push(dest);
});

// Step 1: concat all TTS into one file
const listFile = `${tmpDir}/list.txt`;
fs.writeFileSync(listFile, segFiles.map(f => `file '${f}'`).join('\n'));
const ttsFile = `${tmpDir}/tts_all.mp3`;
execSync(
  `/usr/local/bin/ffmpeg -y -f concat -safe 0 -i "${listFile}" -acodec libmp3lame -q:a 4 "${ttsFile}"`,
  { timeout: 120000 }
);

// Step 2: mix intro (with duck at t=12s) + TTS delayed 12s; fallback to TTS only
const introPath = '/assets/intro.mp3';
const outFile   = `${tmpDir}/combined.mp3`;

if (fs.existsSync(introPath)) {
  execSync(
    `/usr/local/bin/ffmpeg -y -i "${introPath}" -i "${ttsFile}" ` +
    `-filter_complex "[0:a]volume='if(lt(t,12),1,if(lt(t,15),1-(t-12)/3*0.75,0.25))':eval=frame[iv];` +
    `[1:a]adelay=12000|12000[td];` +
    `[iv][td]amix=inputs=2:duration=longest:normalize=0[out]" ` +
    `-map "[out]" -acodec libmp3lame -q:a 4 "${outFile}"`,
    { timeout: 180000 }
  );
} else {
  fs.copyFileSync(ttsFile, outFile);
}

const combined = fs.readFileSync(outFile);

if ($env.SAVE_LOCAL === 'true') {
  fs.mkdirSync('/episodes', { recursive: true });
  fs.writeFileSync(`/episodes/${fname}`, combined);
}

try { fs.rmSync(tmpDir, { recursive: true, force: true }); } catch (e) {}

return [{
  json: {
    file_name:    fname,
    line_count:   items.length,
    has_intro:    fs.existsSync(introPath),
    combined_b64: combined.toString('base64')
  }
}];
"""

# ── Upload to Buzzsprout (Code node) ──────────────────────────────────────────
# Reads combined_b64 from json (reliable), skips upload when SAVE_LOCAL=true
JS_UPLOAD_BUZZSPROUT = r"""
const https = require('https');

// Skip upload when running in local-save mode
if ($env.SAVE_LOCAL === 'true') {
  const fname = $input.first().json.file_name || 'episode.mp3';
  return [{ json: { skipped: true, id: null, audio_url: null, title: fname } }];
}

const item        = $input.first();
const b64         = item.json.combined_b64 || '';
const audioBuffer = Buffer.from(b64, 'base64');
const fileName    = item.json.file_name || 'episode.mp3';

// Episode title from the writer output
const episodeTitle = $('Parse Writer Response').first().json.episode_title || fileName;

const apiKey    = $env.BUZZSPROUT_API_KEY;
const podcastId = $env.BUZZSPROUT_PODCAST_ID;
const boundary  = `----FormBoundary${Date.now()}`;

const parts = [
  Buffer.from(`--${boundary}\r\nContent-Disposition: form-data; name="title"\r\n\r\n${episodeTitle}\r\n`),
  Buffer.from(`--${boundary}\r\nContent-Disposition: form-data; name="audio_file"; filename="${fileName}"\r\nContent-Type: audio/mpeg\r\n\r\n`),
  audioBuffer,
  Buffer.from(`\r\n--${boundary}--\r\n`)
];
const body = Buffer.concat(parts);

const result = await new Promise((resolve, reject) => {
  const req = https.request({
    hostname: 'www.buzzsprout.com',
    path:     `/api/${podcastId}/episodes.json`,
    method:   'POST',
    headers: {
      'Authorization':  `Token token="${apiKey}"`,
      'Content-Type':   `multipart/form-data; boundary=${boundary}`,
      'Content-Length': body.length
    }
  }, (res) => {
    let data = '';
    res.on('data', c => data += c);
    res.on('end', () => {
      if (res.statusCode >= 200 && res.statusCode < 300) {
        resolve(JSON.parse(data));
      } else {
        reject(new Error(`Buzzsprout HTTP ${res.statusCode}: ${data}`));
      }
    });
    res.on('error', reject);
  });
  req.on('error', reject);
  req.write(body);
  req.end();
});

return [{ json: result }];
"""

# ── Apply patches ──────────────────────────────────────────────────────────────
print(f"→ Fetching Workflow {WF_ID}...")
wf = n8n("GET", f"/workflows/{WF_ID}")

patched = set()
for node in wf["nodes"]:
    if node["name"] == "Generate Voice (Sequential)":
        node["parameters"]["jsCode"] = JS_GENERATE_VOICE
        patched.add(node["name"])
        print("  ✓ Patched 'Generate Voice (Sequential)' — audio stored in json.audio_b64")
    elif node["name"] == "Concatenate Audio":
        node["parameters"]["jsCode"] = JS_CONCAT_AUDIO
        patched.add(node["name"])
        print("  ✓ Patched 'Concatenate Audio' — reads json.audio_b64, writes json.combined_b64")
    elif node["name"] == "Upload to Buzzsprout":
        node["parameters"]["jsCode"] = JS_UPLOAD_BUZZSPROUT
        patched.add(node["name"])
        print("  ✓ Patched 'Upload to Buzzsprout' — reads json.combined_b64")

missing = {"Generate Voice (Sequential)", "Concatenate Audio", "Upload to Buzzsprout"} - patched
if missing:
    print(f"  ✗ Nodes not found: {missing}")
    raise SystemExit(1)

n8n("PUT", f"/workflows/{WF_ID}", {
    "name": wf["name"], "nodes": wf["nodes"],
    "connections": wf["connections"], "settings": wf["settings"]
})
print("  ✓ Saved")
print()
print("Test: trigger the workflow with SAVE_LOCAL=true")
print("      → episodes/circuit-breakers-YYYY-MM-DD.mp3 should have intro + speech")
