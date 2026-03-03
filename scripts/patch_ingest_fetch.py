"""
Replaces require('https') (disallowed in n8n Code node sandbox) with
the native global fetch() API (Node.js 18+, always available in n8n).

Run from repo root: python3 scripts/patch_ingest_fetch.py
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

JS_INGEST = r"""
async function fetchUrl(url) {
  const response = await fetch(url, {
    headers: {
      'User-Agent': 'Mozilla/5.0 (compatible; CircuitBreakers/1.0; +https://github.com/msangui/podcast)',
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
    // Guard against unparseable dates — new Date('garbage').getTime() === NaN,
    // which makes (now - NaN) === NaN, which fails the age filter silently.
    const ts = pubDate ? new Date(pubDate).getTime() : NaN;
    const published = isNaN(ts) ? Date.now() : ts;

    const rawLink = get('link') || get('guid');
    // Some feeds put the URL in an href attribute on a self-closing <link/> tag
    const hrefMatch = block.match(/<link[^>]+href=["']([^"']+)["']/i);
    const link = (rawLink.match(/https?:\/\/[^\s<"]+/) || [])[0]
              || (hrefMatch && hrefMatch[1])
              || rawLink;

    items.push({
      title: get('title'),
      link,
      description: get('description').replace(/<[^>]+>/g, '').substring(0, 400),
      published,
      score: parseInt(get('score') || '0', 10)
    });
  }
  return items;
}

const sourcesData = $('Fetch sources.json').first().json;
const sources = sourcesData.sources.filter(s => s.active && s.type === 'rss');
const hnSource = sourcesData.sources.find(s => s.name === 'Hacker News');
const HN_KEYWORDS = hnSource?.keywords || [];
const MAX_AGE_MS = 48 * 3600 * 1000;  // 48 h — slightly wider net
const now = Date.now();

const allStories = [];
const seenUrls   = new Set();

for (const source of sources) {
  try {
    const xml   = await fetchUrl(source.rss);
    let   items = parseRss(xml);
    items = items.filter(item => (now - item.published) <= MAX_AGE_MS);

    if (source.name === 'Hacker News') {
      items = items.filter(item => {
        if (item.score < 100) return false;
        const text = (item.title + ' ' + item.description).toLowerCase();
        return HN_KEYWORDS.some(kw => text.includes(kw.toLowerCase()));
      });
    }

    for (const item of items) {
      if (!item.link || seenUrls.has(item.link)) continue;
      seenUrls.add(item.link);
      allStories.push({
        title:       item.title,
        url:         item.link,
        description: item.description,
        published:   new Date(item.published).toISOString(),
        source:      source.name,
        source_tier: source.tier,
        hn_score:    item.score || null
      });
    }
  } catch (err) {
    console.error('Feed error:', source.name, err.message);
  }
}

allStories.sort((a, b) => {
  if (a.source_tier !== b.source_tier) return a.source_tier - b.source_tier;
  return new Date(b.published) - new Date(a.published);
});

return [{ json: { stories: allStories, total: allStories.length, fetched_at: new Date().toISOString() } }];
"""

print(f"→ Fetching Workflow {WF_ID}...")
wf = n8n("GET", f"/workflows/{WF_ID}")

for node in wf["nodes"]:
    if node["name"] == "Ingest & Filter News":
        node["parameters"]["jsCode"] = JS_INGEST
        node["parameters"]["mode"]   = "runOnceForAllItems"
        print("  ✓ Patched 'Ingest & Filter News' — NaN-date fix + 48h window + href fallback")
        break

n8n("PUT", f"/workflows/{WF_ID}", {
    "name": wf["name"], "nodes": wf["nodes"],
    "connections": wf["connections"], "settings": wf["settings"]
})
print("  ✓ Saved")
