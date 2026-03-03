"""
Adds Google Drive upload support to the Daily Pipeline workflow.

When SAVE_LOCAL=true a new "Upload to Google Drive" Code node runs after
"Upload to Buzzsprout" and uploads the episode MP3 to the configured Drive
folder using a service account JSON key mounted at /config/google-service-account.json.

Also updates "Write Run Log" to record the Drive file URL in the log entry.

Prerequisites:
  1. Create a Google Cloud service account with Drive API enabled
  2. Download the JSON key → save as config/google-service-account.json
  3. Share the Drive folder with the service account email (editor access)
  4. docker compose up -d  (picks up new volume + env vars from docker-compose.yml)

Run from repo root: python3 scripts/patch_google_drive.py
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

# ── Upload to Google Drive ─────────────────────────────────────────────────────
# Runs only when SAVE_LOCAL=true. Exchanges the OAuth2 refresh token stored in
# GOOGLE_DRIVE_REFRESH_TOKEN for a short-lived access token, then uploads the
# episode MP3 to the personal Drive folder via the multipart upload endpoint.
JS_UPLOAD_GOOGLE_DRIVE = r"""
if ($env.SAVE_LOCAL !== 'true') {
  return [{ json: { skipped: true, fileId: null, webViewLink: null } }];
}

const https = require('https');

// Exchange refresh token for a short-lived access token
function post(hostname, path, headers, body) {
  return new Promise((resolve, reject) => {
    const req = https.request({ hostname, path, method: 'POST', headers }, res => {
      let d = '';
      res.on('data', c => d += c);
      res.on('end', () => resolve({ status: res.statusCode, body: d }));
    });
    req.on('error', reject);
    req.write(body);
    req.end();
  });
}

const tokenBody = [
  'grant_type=refresh_token',
  `refresh_token=${encodeURIComponent($env.GOOGLE_DRIVE_REFRESH_TOKEN)}`,
  `client_id=${encodeURIComponent($env.GOOGLE_OAUTH_CLIENT_ID)}`,
  `client_secret=${encodeURIComponent($env.GOOGLE_OAUTH_CLIENT_SECRET)}`
].join('&');

const tokenRes = await post(
  'oauth2.googleapis.com', '/token',
  { 'Content-Type': 'application/x-www-form-urlencoded', 'Content-Length': Buffer.byteLength(tokenBody) },
  tokenBody
);
const { access_token: token } = JSON.parse(tokenRes.body);
if (!token) throw new Error(`Token refresh failed: ${tokenRes.body}`);

// Build multipart/related body: JSON metadata + MP3 binary
const concatOut = $('Concatenate Audio').first().json;
const audio     = Buffer.from(concatOut.combined_b64, 'base64');
const fileName  = concatOut.file_name;
const folderId  = $env.GOOGLE_DRIVE_FOLDER_ID;
const boundary  = `DriveUpload${Date.now()}`;
const meta      = JSON.stringify({ name: fileName, parents: [folderId] });

const uploadBody = Buffer.concat([
  Buffer.from(`--${boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n${meta}\r\n`),
  Buffer.from(`--${boundary}\r\nContent-Type: audio/mpeg\r\n\r\n`),
  audio,
  Buffer.from(`\r\n--${boundary}--`)
]);

// Upload via Drive API multipart endpoint
const uploadRes = await new Promise((resolve, reject) => {
  const req = https.request({
    hostname: 'www.googleapis.com',
    path:     '/upload/drive/v3/files?uploadType=multipart&fields=id,webViewLink',
    method:   'POST',
    headers: {
      'Authorization': `Bearer ${token}`,
      'Content-Type':  `multipart/related; boundary=${boundary}`,
      'Content-Length': uploadBody.length
    }
  }, res => {
    let d = '';
    res.on('data', c => d += c);
    res.on('end', () => {
      const parsed = JSON.parse(d);
      if (res.statusCode >= 200 && res.statusCode < 300) resolve(parsed);
      else reject(new Error(`Drive upload HTTP ${res.statusCode}: ${d}`));
    });
  });
  req.on('error', reject);
  req.write(uploadBody);
  req.end();
});

return [{ json: { fileId: uploadRes.id, webViewLink: uploadRes.webViewLink, fileName } }];
"""

# ── Write Run Log (updated to capture Drive URL) ───────────────────────────────
JS_WRITE_LOG = r"""
const fs        = require('fs');
const writerOut = $('Parse Writer Response').first().json;
const buzzOut   = $('Upload to Buzzsprout').first().json;
const driveOut  = $('Upload to Google Drive').first().json;
const ingest    = $('Ingest & Filter News').first().json;
const brief     = $('Parse Curator Response').first().json.brief;
const today     = new Date().toISOString().split('T')[0];

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

# Find the "Upload to Buzzsprout" node to get its canvas position
buzzsprout_node = next((n for n in nodes if n["name"] == "Upload to Buzzsprout"), None)
if not buzzsprout_node:
    print("  ✗ 'Upload to Buzzsprout' node not found")
    raise SystemExit(1)

pos_x = buzzsprout_node["position"][0] + 220
pos_y = buzzsprout_node["position"][1]

# Check if "Upload to Google Drive" already exists (idempotent)
if any(n["name"] == "Upload to Google Drive" for n in nodes):
    print("  ℹ 'Upload to Google Drive' already exists — updating code only")
    for node in nodes:
        if node["name"] == "Upload to Google Drive":
            node["parameters"]["jsCode"] = JS_UPLOAD_GOOGLE_DRIVE
else:
    # Find what "Upload to Buzzsprout" currently connects to
    buzz_targets = connections.get("Upload to Buzzsprout", {}).get("main", [[]])[0]

    # Add the new node
    new_node = {
        "name":        "Upload to Google Drive",
        "type":        "n8n-nodes-base.code",
        "typeVersion": 2,
        "position":    [pos_x, pos_y],
        "parameters":  {"mode": "runOnceForAllItems", "jsCode": JS_UPLOAD_GOOGLE_DRIVE}
    }
    nodes.append(new_node)

    # Re-wire: Buzzsprout → Drive → (previous Buzzsprout targets)
    connections["Upload to Buzzsprout"] = {
        "main": [[{"node": "Upload to Google Drive", "type": "main", "index": 0}]]
    }
    connections["Upload to Google Drive"] = {
        "main": [buzz_targets] if buzz_targets else [[]]
    }
    print("  ✓ Added 'Upload to Google Drive' node and rewired connections")

# Update Write Run Log
for node in nodes:
    if node["name"] == "Write Run Log":
        node["parameters"]["jsCode"] = JS_WRITE_LOG
        print("  ✓ Patched 'Write Run Log' — records google_drive_url")

n8n("PUT", f"/workflows/{WF_ID}", {
    "name": wf["name"], "nodes": nodes,
    "connections": connections, "settings": wf["settings"]
})
print("  ✓ Saved")
print()
print("Next steps:")
print("  1. Place service account JSON at config/google-service-account.json")
print("  2. docker compose up -d   (picks up new /config volume + GOOGLE_DRIVE_FOLDER_ID)")
print("  3. Trigger workflow with SAVE_LOCAL=true")
print("  4. Check logs/YYYY-MM-DD.json for google_drive_url")
