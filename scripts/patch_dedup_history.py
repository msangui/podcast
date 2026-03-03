"""
Implements cross-episode story deduplication:
  1. Adds 'Load Episode History' node (reads + auto-cleans /logs/*.json, 7-day window)
  2. Wires: Daily Schedule → Load Episode History → Build Curator Input
  3. Passes covered URLs to curator so it excludes already-aired stories
  4. Expands Write Run Log to save covered_urls in each log file

Run from repo root: python3 scripts/patch_dedup_history.py
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

# ── Part 1: Load Episode History node ────────────────────────────────────────
JS_LOAD_HISTORY = r"""
const fs   = require('fs');
const path = require('path');

const logsDir   = '/logs';
const cutoff    = new Date();
cutoff.setDate(cutoff.getDate() - 7);  // keep last 7 days

const coveredUrls = [];

try {
  const files = fs.readdirSync(logsDir)
    .filter(f => f.endsWith('.json'))
    .sort();  // ascending — oldest first for deletion pass

  for (const file of files) {
    const filePath = path.join(logsDir, file);
    const dateStr  = file.replace('.json', '');
    const fileDate = new Date(dateStr);

    if (isNaN(fileDate.getTime())) continue;  // skip non-date files

    if (fileDate < cutoff) {
      // Older than 7 days — delete to keep storage bounded
      try { fs.unlinkSync(filePath); } catch (e) {}
      continue;
    }

    try {
      const log = JSON.parse(fs.readFileSync(filePath, 'utf8'));
      if (Array.isArray(log.covered_urls)) {
        coveredUrls.push(...log.covered_urls);
      }
    } catch (e) {
      // Malformed log — skip
    }
  }
} catch (e) {
  // /logs doesn't exist yet (first ever run) — return empty list
}

return [{ json: { covered_urls: coveredUrls, count: coveredUrls.length } }];
"""

# ── Part 2: Updated Build Curator Input ──────────────────────────────────────
JS_BUILD_CURATOR = r"""
const stories    = $('Ingest & Filter News').first().json.stories;
const showFormat = $('Fetch show-format.json').first().json;
const prompt     = $('Fetch Curator Prompt').first().json.data;
const history    = $('Load Episode History').first().json;

const historySection = history.covered_urls.length > 0
  ? [
      '',
      `PREVIOUSLY COVERED URLS — do NOT select these (already aired in the last 7 days):`,
      history.covered_urls.join('\n')
    ].join('\n')
  : '';

const userMessage = [
  `DATE: ${new Date().toISOString().split('T')[0]}`,
  '',
  'SHOW FORMAT REFERENCE:',
  JSON.stringify({
    episode_length_minutes: showFormat.episode_length_minutes,
    segments: showFormat.segments
  }, null, 2),
  historySection,
  '',
  `RAW FEED (${stories.length} stories, sorted by tier then recency):`,
  JSON.stringify(stories, null, 2)
].join('\n');

return [{ json: { system_prompt: prompt, user_message: userMessage } }];
"""

# ── Part 3: Updated Write Run Log ─────────────────────────────────────────────
JS_WRITE_LOG = r"""
const fs        = require('fs');
const writerOut = $('Parse Writer Response').first().json;
const buzzOut   = $('Upload to Buzzsprout').first().json;
const ingest    = $('Ingest & Filter News').first().json;
const brief     = $('Parse Curator Response').first().json.brief;
const today     = new Date().toISOString().split('T')[0];

// Extract URLs of stories actually selected by the curator
const coveredUrls = (brief.stories || []).map(s => s.url).filter(Boolean);

const log = {
  date:                  today,
  run_at:                new Date().toISOString(),
  episode_title:         writerOut.episode_title,
  story_count:           writerOut.story_count,
  covered_urls:          coveredUrls,
  stories_ingested:      ingest.total,
  buzzsprout_episode_id: buzzOut.id,
  buzzsprout_audio_url:  buzzOut.audio_url,
  status:                'success'
};

fs.mkdirSync('/logs', { recursive: true });
fs.writeFileSync(`/logs/${today}.json`, JSON.stringify(log, null, 2));
return [{ json: log }];
"""

# ── Apply patches ─────────────────────────────────────────────────────────────
print(f"→ Fetching Workflow {WF_ID}...")
wf = n8n("GET", f"/workflows/{WF_ID}")
nodes       = wf["nodes"]
connections = wf["connections"]

# Locate reference nodes for positioning
schedule_node = next(n for n in nodes if n["name"] == "Daily Schedule")
curator_node  = next(n for n in nodes if n["name"] == "Build Curator Input")

# Add Load Episode History node if not already present
if not any(n["name"] == "Load Episode History" for n in nodes):
    hist_node = {
        "parameters": {"jsCode": JS_LOAD_HISTORY, "mode": "runOnceForAllItems"},
        "id":   "load-episode-history",
        "name": "Load Episode History",
        "type": "n8n-nodes-base.code",
        "typeVersion": 2,
        "position": [
            schedule_node["position"][0] + 220,
            schedule_node["position"][1] + 160
        ]
    }
    nodes.append(hist_node)
    print("  ✓ Added 'Load Episode History' node")
else:
    for n in nodes:
        if n["name"] == "Load Episode History":
            n["parameters"]["jsCode"] = JS_LOAD_HISTORY
    print("  ✓ Updated 'Load Episode History' node code")

# Wire Daily Schedule → Load Episode History
sched_main = connections.setdefault("Daily Schedule", {}).setdefault("main", [[]])
if not any(c["node"] == "Load Episode History" for c in sched_main[0]):
    sched_main[0].append({"node": "Load Episode History", "type": "main", "index": 0})
    print("  ✓ Daily Schedule → Load Episode History")

# Wire Load Episode History → Build Curator Input
connections["Load Episode History"] = {
    "main": [[{"node": "Build Curator Input", "type": "main", "index": 0}]]
}
print("  ✓ Load Episode History → Build Curator Input")

# Patch Build Curator Input
for n in nodes:
    if n["name"] == "Build Curator Input":
        n["parameters"]["jsCode"] = JS_BUILD_CURATOR
        print("  ✓ Patched 'Build Curator Input' — history section added")
        break

# Patch Write Run Log
for n in nodes:
    if n["name"] == "Write Run Log":
        n["parameters"]["jsCode"] = JS_WRITE_LOG
        print("  ✓ Patched 'Write Run Log' — covered_urls now saved")
        break

n8n("PUT", f"/workflows/{WF_ID}", {
    "name": wf["name"], "nodes": nodes,
    "connections": connections, "settings": wf["settings"]
})
print("  ✓ Saved")
