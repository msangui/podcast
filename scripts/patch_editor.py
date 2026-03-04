"""
Adds an "Editor Review" agent to the Daily Pipeline workflow.

The editor runs after "Parse Writer Response" and before "Split Script into Lines".
It validates the episode script for:
  - Grammatical errors and awkward phrasing (auto-fixed)
  - Factual hallucinations (verified against ingested source stories; Telegram alert sent)
  - Conversational flow between hosts

Also updates "Concatenate Audio" to save episodes in a dated subfolder
(/episodes/{date}/) alongside a transcript.txt file (final edited script).

Run from repo root: python3 scripts/patch_editor.py
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

# ── Editor Review ──────────────────────────────────────────────────────────────
JS_EDITOR_REVIEW = r"""
const https = require('https');

const systemPrompt = $('Fetch Editor Prompt').first().json.data;
const writerOut    = $('Parse Writer Response').first().json;
const script       = writerOut.script || '';
const episodeTitle = writerOut.episode_title || 'Circuit Breakers Daily';
const ingestOut    = $('Ingest & Filter News').first().json;
const stories      = ingestOut.stories || [];

const sourceContext = stories.slice(0, 30)
  .map(s => `- ${s.title} (${s.source}): ${(s.description || '').slice(0, 200)}`)
  .join('\n');

const userMessage = `EPISODE TITLE: ${episodeTitle}

SOURCE STORIES (ground truth for fact-checking):
${sourceContext}

SCRIPT TO REVIEW:
${script}`;

const body = JSON.stringify({
  model:      'claude-sonnet-4-6',
  max_tokens: 10000,
  system:     systemPrompt,
  messages:   [{ role: 'user', content: userMessage }]
});

const result = await new Promise((resolve, reject) => {
  const req = https.request({
    hostname: 'api.anthropic.com',
    path:     '/v1/messages',
    method:   'POST',
    headers: {
      'x-api-key':         $env.ANTHROPIC_API_KEY,
      'anthropic-version': '2023-06-01',
      'Content-Type':      'application/json',
      'Content-Length':    Buffer.byteLength(body)
    }
  }, res => {
    let d = '';
    res.on('data', c => d += c);
    res.on('end', () => {
      if (res.statusCode >= 200 && res.statusCode < 300) resolve(JSON.parse(d));
      else reject(new Error(`Anthropic HTTP ${res.statusCode}: ${d}`));
    });
  });
  req.on('error', reject);
  req.write(body);
  req.end();
});

const text = result.content[0].text.trim();
let review;
try {
  const clean = text.replace(/^```json\s*/, '').replace(/\s*```$/, '');
  review = JSON.parse(clean);
} catch (e) {
  // Pass script through unchanged on parse error
  return [{ json: {
    ...writerOut,
    editorial_changes:    [`Editor parse error: ${e.message}`],
    hallucinations_found: [],
    editor_approved:      false
  } }];
}

// Send Telegram alert if hallucinations were found
if (review.hallucinations_found && review.hallucinations_found.length > 0) {
  const alertText = `⚠️ *Editor Alert — ${episodeTitle}*\n\nHallucinations detected:\n` +
    review.hallucinations_found.map(h => `• ${h}`).join('\n') +
    `\n\nThe episode will still be generated with corrections applied.`;

  const tgBody = JSON.stringify({
    chat_id:    $env.TELEGRAM_CHAT_ID,
    text:       alertText,
    parse_mode: 'Markdown'
  });

  await new Promise((resolve) => {
    const req = https.request({
      hostname: 'api.telegram.org',
      path:     `/bot${$env.TELEGRAM_BOT_TOKEN}/sendMessage`,
      method:   'POST',
      headers: { 'Content-Type': 'application/json', 'Content-Length': Buffer.byteLength(tgBody) }
    }, res => { res.on('data', () => {}); res.on('end', resolve); });
    req.on('error', resolve); // don't block pipeline on Telegram failure
    req.write(tgBody);
    req.end();
  });
}

return [{ json: {
  ...writerOut,
  script:               review.script || script,
  editorial_changes:    review.changes || [],
  hallucinations_found: review.hallucinations_found || [],
  editor_approved:      review.approved !== false
} }];
"""

# ── Concatenate Audio (updated: dated folder + transcript) ─────────────────────
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

const combined   = fs.readFileSync(outFile);
const episodeDir = `/episodes/${date}`;

if ($env.SAVE_LOCAL === 'true') {
  fs.mkdirSync(episodeDir, { recursive: true });
  fs.writeFileSync(`${episodeDir}/${fname}`, combined);

  // Save transcript — prefer Editor Review output (corrected script), fall back to writer
  let transcript = '';
  try {
    transcript = $('Editor Review').first().json.script || '';
  } catch (_) {
    try { transcript = $('Parse Writer Response').first().json.script || ''; } catch (_) {}
  }
  if (transcript) fs.writeFileSync(`${episodeDir}/transcript.txt`, transcript);
}

try { fs.rmSync(tmpDir, { recursive: true, force: true }); } catch (e) {}

return [{
  json: {
    file_name:    fname,
    episode_dir:  episodeDir,
    line_count:   items.length,
    has_intro:    fs.existsSync(introPath),
    combined_b64: combined.toString('base64')
  }
}];
"""

# ── Write Run Log (updated: new paths + editorial info) ───────────────────────
JS_WRITE_LOG = r"""
const fs        = require('fs');
const writerOut = $('Parse Writer Response').first().json;
const buzzOut   = $('Upload to Buzzsprout').first().json;
const driveOut  = $('Upload to Google Drive').first().json;
const ingest    = $('Ingest & Filter News').first().json;
const brief     = $('Parse Curator Response').first().json.brief;
const concatOut = $('Concatenate Audio').first().json;
const today     = new Date().toISOString().split('T')[0];

const coveredUrls = (brief.stories || []).map(s => s.url).filter(Boolean);

const episodeDir     = concatOut.episode_dir || `/episodes/${today}`;
const localPath      = $env.SAVE_LOCAL === 'true' ? `${episodeDir}/${concatOut.file_name}` : null;
const transcriptPath = $env.SAVE_LOCAL === 'true' ? `${episodeDir}/transcript.txt` : null;

let editorialChanges    = [];
let hallucinationsFound = [];
let editorApproved      = null;
try {
  const editorOut      = $('Editor Review').first().json;
  editorialChanges     = editorOut.editorial_changes    || [];
  hallucinationsFound  = editorOut.hallucinations_found || [];
  editorApproved       = editorOut.editor_approved ?? true;
} catch (e) {}

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
  transcript_path:       transcriptPath,
  editorial_changes:     editorialChanges,
  hallucinations_found:  hallucinationsFound,
  editor_approved:       editorApproved,
  google_drive_url:      driveOut.webViewLink || null,
  status:                'success'
};

fs.mkdirSync('/logs', { recursive: true });
fs.writeFileSync(`/logs/${today}.json`, JSON.stringify(log, null, 2));
return [{ json: log }];
"""

# ── Apply patches ──────────────────────────────────────────────────────────────
print(f"→ Fetching Workflow {WF_ID}...")
wf = n8n("GET", f"/workflows/{WF_ID}")

nodes       = wf["nodes"]
connections = wf["connections"]

# Find "Parse Writer Response" to get position and current targets
writer_node = next((n for n in nodes if n["name"] == "Parse Writer Response"), None)
if not writer_node:
    print("  ✗ 'Parse Writer Response' node not found")
    raise SystemExit(1)

pos_x = writer_node["position"][0] + 220
pos_y = writer_node["position"][1]

EDITOR_PROMPT_URL = "={{ $env.GITHUB_RAW_BASE_URL }}/prompts/editor-agent.md"

# ── Fetch Editor Prompt (HTTP Request node) ────────────────────────────────────
if any(n["name"] == "Fetch Editor Prompt" for n in nodes):
    print("  ℹ 'Fetch Editor Prompt' already exists — skipping")
else:
    fetch_node = {
        "name":        "Fetch Editor Prompt",
        "type":        "n8n-nodes-base.httpRequest",
        "typeVersion": 4,
        "position":    [pos_x, pos_y],
        "parameters":  {
            "url": EDITOR_PROMPT_URL,
            "options": {"response": {"response": {"responseFormat": "text"}}}
        }
    }
    nodes.append(fetch_node)

    # Parse Writer Response → Fetch Editor Prompt
    writer_targets = connections.get("Parse Writer Response", {}).get("main", [[]])[0]
    connections["Parse Writer Response"] = {
        "main": [[{"node": "Fetch Editor Prompt", "type": "main", "index": 0}]]
    }
    # Fetch Editor Prompt will chain to Editor Review below
    # Store writer_targets for the Editor Review wiring
    connections["_editor_prev_targets"] = writer_targets  # temp key, removed before save
    print("  ✓ Added 'Fetch Editor Prompt' node")

# ── Editor Review (Code node) ──────────────────────────────────────────────────
prev_targets = connections.pop("_editor_prev_targets", None)

if any(n["name"] == "Editor Review" for n in nodes):
    print("  ℹ 'Editor Review' already exists — updating code only")
    for node in nodes:
        if node["name"] == "Editor Review":
            node["parameters"]["jsCode"] = JS_EDITOR_REVIEW
else:
    review_node = {
        "name":        "Editor Review",
        "type":        "n8n-nodes-base.code",
        "typeVersion": 2,
        "position":    [pos_x + 220, pos_y],
        "parameters":  {"mode": "runOnceForAllItems", "jsCode": JS_EDITOR_REVIEW}
    }
    nodes.append(review_node)
    if prev_targets is not None:
        connections["Editor Review"] = {
            "main": [prev_targets] if prev_targets else [[]]
        }
    print("  ✓ Added 'Editor Review' node")

# Always ensure Fetch Editor Prompt → Editor Review is wired
connections["Fetch Editor Prompt"] = {
    "main": [[{"node": "Editor Review", "type": "main", "index": 0}]]
}
print("  ✓ Wired Fetch Editor Prompt → Editor Review")

# Update Concatenate Audio
for node in nodes:
    if node["name"] == "Concatenate Audio":
        node["parameters"]["jsCode"] = JS_CONCAT_AUDIO
        print("  ✓ Patched 'Concatenate Audio' — saves to /episodes/{date}/ + transcript.txt")

# Update Write Run Log
for node in nodes:
    if node["name"] == "Write Run Log":
        node["parameters"]["jsCode"] = JS_WRITE_LOG
        print("  ✓ Patched 'Write Run Log' — records transcript_path + editorial info")

n8n("PUT", f"/workflows/{WF_ID}", {
    "name": wf["name"], "nodes": nodes,
    "connections": connections, "settings": wf["settings"]
})
print("  ✓ Saved")
print()
print("Verification:")
print("  1. Trigger workflow with SAVE_LOCAL=true")
print("  2. Check /episodes/{date}/ for circuit-breakers-{date}.mp3 + transcript.txt")
print("  3. Check /logs/{date}.json for editorial_changes + hallucinations_found")
