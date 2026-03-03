"""
Adds SAVE_LOCAL support to the Daily Pipeline workflow:

  - Concatenate Audio: writes episode to /episodes/ when SAVE_LOCAL=true
  - Upload to Buzzsprout: converted to Code node — skips upload when SAVE_LOCAL=true,
    otherwise does the same multipart POST as before
  - Write Run Log: records local_path when SAVE_LOCAL=true

Set SAVE_LOCAL=true in .env to generate episodes locally without publishing.
Set SAVE_LOCAL=false (or omit) to publish to Buzzsprout as normal.

Run from repo root: python3 scripts/patch_save_local.py
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
    `-filter_complex "[0:a]volume='if(lt(t,9),1,if(lt(t,12),0.8,if(lt(t,15),0.75-(t-12)/3*0.5,0.25)))':eval=frame[iv];` +
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

# ── Upload to Buzzsprout (Code node replacement) ──────────────────────────────
# Skips upload and returns a dummy response when SAVE_LOCAL=true.
# Otherwise performs the same multipart POST the original HTTP Request node did.
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
const fileName     = item.json.file_name || 'episode.mp3';

// Episode title from the writer output
const episodeTitle = $('Parse Writer Response').first().json.episode_title || fileName;

const apiKey    = $env.BUZZSPROUT_API_KEY;
const podcastId = $env.BUZZSPROUT_PODCAST_ID;
const boundary  = `----FormBoundary${Date.now()}`;

// Build multipart body
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
      'Authorization': `Token token="${apiKey}"`,
      'Content-Type':  `multipart/form-data; boundary=${boundary}`,
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

# ── Write Run Log ──────────────────────────────────────────────────────────────
# Adds local_path to the log entry when SAVE_LOCAL=true.
JS_WRITE_LOG = r"""
const fs        = require('fs');
const writerOut = $('Parse Writer Response').first().json;
const buzzOut   = $('Upload to Buzzsprout').first().json;
const ingest    = $('Ingest & Filter News').first().json;
const brief     = $('Parse Curator Response').first().json.brief;
const today     = new Date().toISOString().split('T')[0];

// Extract URLs of stories actually selected by the curator
const coveredUrls = (brief.stories || []).map(s => s.url).filter(Boolean);

const localPath = $env.SAVE_LOCAL === 'true'
  ? `/episodes/circuit-breakers-${today}.mp3`
  : null;

const log = {
  date:                  today,
  run_at:                new Date().toISOString(),
  episode_title:         writerOut.episode_title,
  story_count:           writerOut.story_count,
  covered_urls:          coveredUrls,
  stories_ingested:      ingest.total,
  buzzsprout_episode_id: buzzOut.id,
  buzzsprout_audio_url:  buzzOut.audio_url,
  local_path:            localPath,
  status:                'success'
};

fs.mkdirSync('/logs', { recursive: true });
fs.writeFileSync(`/logs/${today}.json`, JSON.stringify(log, null, 2));
return [{ json: log }];
"""

# ── Apply patches ──────────────────────────────────────────────────────────────
print(f"→ Fetching Workflow {WF_ID}...")
wf = n8n("GET", f"/workflows/{WF_ID}")

for node in wf["nodes"]:
    if node["name"] == "Concatenate Audio":
        node["parameters"]["jsCode"] = JS_CONCAT_AUDIO
        print("  ✓ Patched 'Concatenate Audio' — saves to /episodes/ when SAVE_LOCAL=true")

    elif node["name"] == "Upload to Buzzsprout":
        # Convert from HTTP Request node to Code node
        node["type"]        = "n8n-nodes-base.code"
        node["typeVersion"] = 2
        node["parameters"]  = {"mode": "runOnceForAllItems", "jsCode": JS_UPLOAD_BUZZSPROUT}
        node.pop("credentials", None)
        print("  ✓ Replaced 'Upload to Buzzsprout' with Code node — skips upload when SAVE_LOCAL=true")

    elif node["name"] == "Write Run Log":
        node["parameters"]["jsCode"] = JS_WRITE_LOG
        print("  ✓ Patched 'Write Run Log' — records local_path when SAVE_LOCAL=true")

n8n("PUT", f"/workflows/{WF_ID}", {
    "name": wf["name"], "nodes": wf["nodes"],
    "connections": wf["connections"], "settings": wf["settings"]
})
print("  ✓ Saved")
print()
print("Next steps:")
print("  1. mkdir -p episodes")
print("  2. docker compose up -d   (picks up new volume + SAVE_LOCAL env var)")
print("  3. Set SAVE_LOCAL=true in .env to generate locally, false to publish")
