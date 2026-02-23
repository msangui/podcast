"""
Replaces $helpers.httpRequest (not available in this n8n version) with
native Node.js https/http modules for RSS fetching in 'Ingest & Filter News'.

Run from repo root: python3 scripts/patch_ingest_https.py
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
const https = require('https');
const http  = require('http');
const zlib  = require('zlib');

// Fetch a URL following redirects, handles gzip, returns text
function fetchUrl(url, maxRedirects) {
  if (maxRedirects === undefined) maxRedirects = 5;
  return new Promise((resolve, reject) => {
    if (maxRedirects < 0) return reject(new Error('Too many redirects'));
    const lib = url.startsWith('https') ? https : http;
    const opts = {
      headers: {
        'User-Agent': 'Mozilla/5.0 (compatible; CircuitBreakers/1.0; +https://github.com/msangui/podcast)',
        'Accept': 'application/rss+xml, application/xml, text/xml, */*',
        'Accept-Encoding': 'gzip, deflate'
      },
      timeout: 15000
    };
    const req = lib.get(url, opts, (res) => {
      if (res.statusCode >= 300 && res.statusCode < 400 && res.headers.location) {
        return fetchUrl(res.headers.location, maxRedirects - 1).then(resolve).catch(reject);
      }
      if (res.statusCode !== 200) {
        return reject(new Error(`HTTP ${res.statusCode}`));
      }
      const chunks = [];
      let stream = res;
      const enc = (res.headers['content-encoding'] || '').toLowerCase();
      if (enc === 'gzip')    stream = res.pipe(zlib.createGunzip());
      if (enc === 'deflate') stream = res.pipe(zlib.createInflate());
      stream.on('data', chunk => chunks.push(Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk)));
      stream.on('end',  () => resolve(Buffer.concat(chunks).toString('utf8')));
      stream.on('error', reject);
    });
    req.on('error', reject);
    req.on('timeout', () => { req.destroy(); reject(new Error('Request timed out')); });
  });
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
      description: get('description').replace(/<[^>]+>/g, '').substring(0, 400),
      published: pubDate ? new Date(pubDate).getTime() : Date.now(),
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
    // Degrade gracefully — one bad feed must not kill the pipeline
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
        print("  ✓ Patched 'Ingest & Filter News' — now uses Node.js https module")
        break

n8n("PUT", f"/workflows/{WF_ID}", {
    "name": wf["name"], "nodes": wf["nodes"],
    "connections": wf["connections"], "settings": wf["settings"]
})
print("  ✓ Saved")
