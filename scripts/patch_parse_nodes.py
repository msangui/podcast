"""
Patches all 'Parse *' Code nodes across workflows 01, 03, 05 to use a
robust JSON extractor that handles Claude preamble text before the fence.

Root cause: /^```...```$/ requires the fence to be the ENTIRE response.
Claude often adds "Here is the brief:" before the fence, so the regex
fails silently and JSON.parse gets the raw ```json text → SyntaxError.

Run from repo root: python3 scripts/patch_parse_nodes.py
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

def put_wf(wf_id, wf):
    n8n("PUT", f"/workflows/{wf_id}", {
        "name": wf["name"], "nodes": wf["nodes"],
        "connections": wf["connections"], "settings": wf["settings"]
    })

# ── Robust JSON extractor (shared helper embedded in each node) ───────────────
# Finds the LAST code fence in the response, or falls back to raw brace-match.
# Handles Claude preamble like "Here is the brief:\n```json\n{...}\n```"
EXTRACT_HELPER = r"""
function extractJson(text) {
  // Find all ```json or ``` fences — take the last one (metadata is last for writer)
  const fences = [...text.matchAll(/```(?:json)?\s*\n([\s\S]*?)\n```/g)];
  if (fences.length > 0) return fences[fences.length - 1][1].trim();
  // Fallback: first bare JSON object or array in the text
  const bare = text.match(/(\{[\s\S]*\}|\[[\s\S]*\])/);
  if (bare) return bare[1].trim();
  return text.trim();
}
"""

# ── New parse code per workflow ───────────────────────────────────────────────

JS_PARSE_CURATOR = EXTRACT_HELPER + r"""
const raw   = $input.first().json.content[0].text;
const brief = JSON.parse(extractJson(raw));
return [{ json: { brief } }];
"""

JS_PARSE_WRITER = EXTRACT_HELPER + r"""
const raw = $input.first().json.content[0].text;

// Writer returns script text followed by a ```json metadata block
// extractJson takes the LAST fence, which is the metadata
let metadata = {};
try { metadata = JSON.parse(extractJson(raw)); } catch(e) {}

// Script = everything before the last ``` block
const lastFenceIdx = raw.lastIndexOf('\x60\x60\x60');
const script = lastFenceIdx > 0 ? raw.substring(0, lastFenceIdx).trim() : raw.trim();

return [{
  json: {
    script,
    metadata,
    episode_title:       metadata.episode_title       || 'Circuit Breakers Daily',
    episode_description: metadata.episode_description || '',
    deep_dives:          metadata.deep_dives          || [],
    story_count:         metadata.story_count         || 0
  }
}];
"""

JS_PARSE_CFO = EXTRACT_HELPER + r"""
const raw    = $input.first().json.content[0].text;
const parsed = JSON.parse(extractJson(raw));
const chatId = $('Build CFO Input').first().json._chat_id;

return [{
  json: {
    action:           parsed.action           || 'none',
    telegram_message: parsed.telegram_message || parsed.whatsapp_message || '(no message)',
    cost_summary:     parsed.cost_summary     || {},
    anomaly_detected: parsed.anomaly_detected || false,
    anomaly_details:  parsed.anomaly_details  || null,
    _chat_id:         String(chatId)
  }
}];
"""

JS_PARSE_ROTATION = EXTRACT_HELPER + r"""
const raw    = $input.first().json.content[0].text;
const parsed = JSON.parse(extractJson(raw));
const chatId = $('Build Source Rotation Input').first().json._chat_id;

return [{
  json: {
    recommendation:    parsed.recommendation    || 'no_change',
    telegram_message:  parsed.telegram_message  || '(no message)',
    analysis:          parsed.analysis          || {},
    new_sources_array: parsed.new_sources_array || null,
    _chat_id:          String(chatId)
  }
}];
"""

# ── Apply patches ─────────────────────────────────────────────────────────────
patches = [
    (env["N8N_WORKFLOW_ID_DAILY_PIPELINE"], "Parse Curator Response", JS_PARSE_CURATOR),
    (env["N8N_WORKFLOW_ID_DAILY_PIPELINE"], "Parse Writer Response",  JS_PARSE_WRITER),
    (env["N8N_WORKFLOW_ID_CFO"],            "Parse CFO Response",     JS_PARSE_CFO),
    (env["N8N_WORKFLOW_ID_SOURCE_ROTATION"],"Parse Source Rotation Response", JS_PARSE_ROTATION),
]

wf_cache = {}

for wf_id, node_name, new_code in patches:
    if wf_id not in wf_cache:
        wf_cache[wf_id] = n8n("GET", f"/workflows/{wf_id}")
    wf = wf_cache[wf_id]
    for node in wf["nodes"]:
        if node["name"] == node_name:
            node["parameters"]["jsCode"] = new_code
            print(f"  ✓ [{wf['name']}] patched '{node_name}'")
            break
    else:
        print(f"  ✗ [{wf_id}] node '{node_name}' not found")

print("→ Saving workflows...")
for wf_id, wf in wf_cache.items():
    put_wf(wf_id, wf)
    print(f"  ✓ Saved: {wf['name']}")

print("Done.")
