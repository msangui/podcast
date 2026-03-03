"""
Debug version of Ingest — uses fetch(), returns per-source diagnostics
so we can see exactly why allStories ends up empty.

Run from repo root: python3 scripts/patch_ingest_debug2.py
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

JS_INGEST_DEBUG = r"""
async function fetchUrl(url) {
  const response = await fetch(url, {
    headers: {
      'User-Agent': 'Mozilla/5.0 (compatible; CircuitBreakers/1.0)',
      'Accept': 'application/rss+xml, application/xml, text/xml, */*'
    },
    redirect: 'follow'
  });
  if (!response.ok) throw new Error(`HTTP ${response.status} for ${url}`);
  return response.text();
}

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
      published: pubDate ? new Date(pubDate).getTime() : Date.now(),
      published_str: pubDate || '(none)',
      score: parseInt(get('score') || '0', 10)
    });
  }
  return items;
}

const sourcesData = $('Fetch sources.json').first().json;
const sources = sourcesData.sources.filter(s => s.active && s.type === 'rss');
const hnSource = sourcesData.sources.find(s => s.name === 'Hacker News');
const HN_KEYWORDS = hnSource?.keywords || [];
const MAX_AGE_MS = 36 * 3600 * 1000;
const now = Date.now();

const log = [];

for (const source of sources) {
  const entry = { source: source.name, url: source.rss, status: 'unknown',
                  raw_item_count: 0, after_age_filter: 0, after_hn_filter: 0,
                  sample_titles: [], sample_ages_h: [], error: null };
  try {
    const xml = await fetchUrl(source.rss);
    entry.body_length = xml.length;
    entry.body_preview = xml.substring(0, 200);

    let items = parseRss(xml);
    entry.raw_item_count = items.length;
    if (items.length > 0) {
      // Show age of first 3 items in hours
      entry.sample_titles   = items.slice(0, 3).map(i => i.title.substring(0, 60));
      entry.sample_ages_h   = items.slice(0, 3).map(i => Math.round((now - i.published) / 3600000));
      entry.sample_pub_strs = items.slice(0, 3).map(i => i.published_str);
    }

    const afterAge = items.filter(item => (now - item.published) <= MAX_AGE_MS);
    entry.after_age_filter = afterAge.length;

    let afterHn = afterAge;
    if (source.name === 'Hacker News') {
      afterHn = afterAge.filter(item => {
        if (item.score < 100) return false;
        const text = (item.title + ' ' + (item.description||'')).toLowerCase();
        return HN_KEYWORDS.some(kw => text.includes(kw.toLowerCase()));
      });
      entry.hn_score_samples = afterAge.slice(0, 5).map(i => ({ title: i.title.substring(0,50), score: i.score }));
    }
    entry.after_hn_filter = afterHn.length;
    entry.status = 'ok';
  } catch (err) {
    entry.status = 'error';
    entry.error  = err.message || String(err);
  }
  log.push(entry);
}

return [{ json: { debug_log: log, now_iso: new Date(now).toISOString(), max_age_h: 36 } }];
"""

print(f"→ Fetching Workflow {WF_ID}...")
wf = n8n("GET", f"/workflows/{WF_ID}")

for node in wf["nodes"]:
    if node["name"] == "Ingest & Filter News":
        node["parameters"]["jsCode"] = JS_INGEST_DEBUG
        node["parameters"]["mode"]   = "runOnceForAllItems"
        print("  ✓ Patched 'Ingest & Filter News' with debug2 version")
        break

n8n("PUT", f"/workflows/{WF_ID}", {
    "name": wf["name"], "nodes": wf["nodes"],
    "connections": wf["connections"], "settings": wf["settings"]
})
print("  ✓ Saved. Run the workflow and paste the Ingest node output JSON here.")
