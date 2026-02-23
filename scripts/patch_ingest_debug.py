"""
Patches 'Ingest & Filter News' with diagnostic version that:
  1. Uses returnFullResponse:true so XML body is reliably extracted
  2. Captures per-source errors instead of silently swallowing them
  3. Returns both stories AND errors so we can see what went wrong

Run from repo root: python3 scripts/patch_ingest_debug.py
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
N8N_URL    = "http://localhost:5678/api/v1"
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

JS_INGEST_DEBUG = r"""
const sourcesData = $('Fetch sources.json').first().json;
const sources = sourcesData.sources.filter(s => s.active && s.type === 'rss');
const hnSource = sourcesData.sources.find(s => s.name === 'Hacker News');
const HN_KEYWORDS = hnSource?.keywords || [];
const MAX_AGE_MS = 36 * 3600 * 1000;
const now = Date.now();

function parseRss(xml) {
  const items = [];
  const itemRe = /<item>([\s\S]*?)<\/item>/g;
  let m;
  while ((m = itemRe.exec(xml)) !== null) {
    const block = m[1];
    const get = (tag) => {
      const r = new RegExp(
        `<${tag}[^>]*><!\\[CDATA\\[([\\s\\S]*?)\\]\\]></${tag}>|<${tag}[^>]*>([\\s\\S]*?)</${tag}>`
      ).exec(block);
      return r ? (r[1] || r[2] || '').trim() : '';
    };
    const pubDate = get('pubDate');
    const rawLink = get('link') || get('guid');
    const link = (rawLink.match(/https?:\/\/[^\s<"]+/) || [rawLink])[0];
    items.push({
      title: get('title'),
      link,
      description: get('description').replace(/<[^>]+>/g, '').substring(0, 400),
      published: pubDate ? new Date(pubDate).getTime() : now,
      score: parseInt(get('score') || '0', 10)
    });
  }
  return items;
}

const allStories = [];
const seenUrls = new Set();
const fetchLog = [];  // capture per-source results for debugging

for (const source of sources) {
  let logEntry = { source: source.name, url: source.rss, status: 'unknown', items_before_filter: 0, items_after_filter: 0 };
  try {
    // Use returnFullResponse:true to reliably get body as string for XML
    const resp = await $helpers.httpRequest({
      method: 'GET',
      url: source.rss,
      returnFullResponse: true
    });
    const statusCode = resp.statusCode || resp.status || '?';
    const body = typeof resp.body === 'string' ? resp.body : JSON.stringify(resp.body || '');
    logEntry.status = `HTTP ${statusCode}`;
    logEntry.body_length = body.length;
    logEntry.body_preview = body.substring(0, 100);

    let items = parseRss(body);
    logEntry.items_before_filter = items.length;

    items = items.filter(item => (now - item.published) <= MAX_AGE_MS);

    if (source.name === 'Hacker News') {
      items = items.filter(item => {
        if (item.score < 100) return false;
        const text = (item.title + ' ' + item.description).toLowerCase();
        return HN_KEYWORDS.some(kw => text.includes(kw.toLowerCase()));
      });
    }

    logEntry.items_after_filter = items.length;

    for (const item of items) {
      if (!item.link || seenUrls.has(item.link)) continue;
      seenUrls.add(item.link);
      allStories.push({
        title: item.title,
        url: item.link,
        description: item.description,
        published: new Date(item.published).toISOString(),
        source: source.name,
        source_tier: source.tier,
        hn_score: item.score || null
      });
    }
    logEntry.status = 'ok';
  } catch (err) {
    logEntry.status = 'error';
    logEntry.error = err.message || String(err);
  }
  fetchLog.push(logEntry);
}

allStories.sort((a, b) => {
  if (a.source_tier !== b.source_tier) return a.source_tier - b.source_tier;
  return new Date(b.published) - new Date(a.published);
});

return [{ json: { stories: allStories, total: allStories.length, fetch_log: fetchLog, fetched_at: new Date().toISOString() } }];
"""

print(f"→ Fetching Workflow {WF_ID}...")
wf = n8n("GET", f"/workflows/{WF_ID}")

for node in wf["nodes"]:
    if node["name"] == "Ingest & Filter News":
        node["parameters"]["jsCode"] = JS_INGEST_DEBUG
        print("  ✓ Patched 'Ingest & Filter News' with debug version")
        break

n8n("PUT", f"/workflows/{WF_ID}", {
    "name": wf["name"], "nodes": wf["nodes"],
    "connections": wf["connections"], "settings": wf["settings"]
})
print("  ✓ Saved. Re-run the workflow, then read fetch_log from the Ingest node output.")
