"""
Creates Workflow 01 — Daily Podcast Pipeline in n8n via REST API.
Run from repo root: python3 scripts/create_workflow_pipeline.py
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
TG_CRED_ID  = "rEjsdWsuMxLgKY8d"   # created during Concierge setup
CFO_WF_ID   = env.get("N8N_WORKFLOW_ID_CFO", "CiG0AWyoveYG76y5")

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

# ── JS: Ingest & Filter News ──────────────────────────────────────────────────
JS_INGEST = r"""
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

for (const source of sources) {
  try {
    const xml = await $helpers.httpRequest({ method: 'GET', url: source.rss });
    let items = parseRss(typeof xml === 'string' ? xml : '');
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
        title: item.title,
        url: item.link,
        description: item.description,
        published: new Date(item.published).toISOString(),
        source: source.name,
        source_tier: source.tier,
        hn_score: item.score || null
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

# ── JS: Build Curator Input ───────────────────────────────────────────────────
JS_BUILD_CURATOR = r"""
const stories    = $('Ingest & Filter News').first().json.stories;
const showFormat = $('Fetch show-format.json').first().json;
const prompt     = $('Fetch Curator Prompt').first().json.data;

const userMessage = [
  `DATE: ${new Date().toISOString().split('T')[0]}`,
  '',
  'SHOW FORMAT REFERENCE:',
  JSON.stringify({
    episode_length_minutes: showFormat.episode_length_minutes,
    segments: showFormat.segments
  }, null, 2),
  '',
  `RAW FEED (${stories.length} stories, sorted by tier then recency):`,
  JSON.stringify(stories, null, 2)
].join('\n');

return [{ json: { system_prompt: prompt, user_message: userMessage } }];
"""

# ── JS: Parse Curator Response ────────────────────────────────────────────────
JS_PARSE_CURATOR = r"""
const raw = $input.first().json.content[0].text.trim();
let jsonStr = raw;
const fence = raw.match(/^```(?:json)?\s*\n?([\s\S]*?)\n?```$/);
if (fence) jsonStr = fence[1].trim();
const brief = JSON.parse(jsonStr);
return [{ json: { brief } }];
"""

# ── JS: Build Writer Input ────────────────────────────────────────────────────
JS_BUILD_WRITER = r"""
const brief      = $('Parse Curator Response').first().json.brief;
const showFormat = $('Fetch show-format.json').first().json;
const prompt     = $('Fetch Writer Prompt').first().json.data;

const userMessage = [
  'CURATOR BRIEF:',
  JSON.stringify(brief, null, 2),
  '',
  'SHOW FORMAT:',
  JSON.stringify({
    hosts: showFormat.hosts,
    segments: showFormat.segments,
    target_word_count: showFormat.target_word_count,
    words_per_minute: showFormat.words_per_minute
  }, null, 2)
].join('\n');

return [{ json: { system_prompt: prompt, user_message: userMessage } }];
"""

# ── JS: Parse Writer Response ─────────────────────────────────────────────────
JS_PARSE_WRITER = r"""
const raw = $input.first().json.content[0].text;

// Find all JSON fence blocks; last one is the episode metadata
let metadata = {};
const fences = [...raw.matchAll(/```(?:json)?\s*\n([\s\S]*?)\n```/g)];
if (fences.length > 0) {
  try { metadata = JSON.parse(fences[fences.length - 1][1].trim()); } catch(e) {}
}

// Script is everything before the last fence block
const lastFenceIdx = fences.length > 0
  ? raw.lastIndexOf(fences[fences.length - 1][0])
  : raw.length;
const script = raw.substring(0, lastFenceIdx).trim();

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

# ── JS: Split Script into Lines (one item per TTS call) ──────────────────────
JS_SPLIT_LINES = r"""
const writerOut   = $('Parse Writer Response').first().json;
const showFormat  = $('Fetch show-format.json').first().json;
const hansVoiceId  = showFormat.hosts.host_1.voice_id;
const flintVoiceId = showFormat.hosts.host_2.voice_id;

const lines = writerOut.script
  .split('\n')
  .map(l => l.trim())
  .filter(l => l.startsWith('HANS:') || l.startsWith('FLINT:'));

return lines.map((line, index) => {
  const isHans = line.startsWith('HANS:');
  const text   = line.replace(/^(HANS|FLINT):\s*/, '').trim();
  return {
    json: {
      text,
      voice_id:      isHans ? hansVoiceId  : flintVoiceId,
      speaker:       isHans ? 'HANS'       : 'FLINT',
      line_index:    index,
      total_lines:   lines.length,
      episode_title: writerOut.episode_title
    }
  };
});
"""

# ── JS: Concatenate Audio (run once for all items) ────────────────────────────
JS_CONCAT_AUDIO = r"""
const items = $input.all();
const date  = new Date().toISOString().split('T')[0];
const fname = `circuit-breakers-${date}.mp3`;

const buffers = items.map(item => {
  const b64 = item.binary?.data?.data || '';
  return Buffer.from(b64, 'base64');
});

const combined = Buffer.concat(buffers);

return [{
  json: { file_name: fname, line_count: items.length },
  binary: {
    data: {
      data:     combined.toString('base64'),
      mimeType: 'audio/mpeg',
      fileName: fname,
      fileSize: combined.length
    }
  }
}];
"""

# ── JS: Write Run Log ─────────────────────────────────────────────────────────
JS_WRITE_LOG = r"""
const fs        = require('fs');
const writerOut = $('Parse Writer Response').first().json;
const buzzOut   = $('Upload to Buzzsprout').first().json;
const ingest    = $('Ingest & Filter News').first().json;
const today     = new Date().toISOString().split('T')[0];

const log = {
  date:                  today,
  run_at:                new Date().toISOString(),
  episode_title:         writerOut.episode_title,
  story_count:           writerOut.story_count,
  stories_ingested:      ingest.total,
  buzzsprout_episode_id: buzzOut.id,
  buzzsprout_audio_url:  buzzOut.audio_url,
  status:                'success'
};

fs.mkdirSync('/logs', { recursive: true });
fs.writeFileSync(`/logs/${today}.json`, JSON.stringify(log, null, 2));
return [{ json: log }];
"""

# ── Shared Anthropic headers ──────────────────────────────────────────────────
ANTHROPIC_HEADERS = {"parameters": [
    {"name": "x-api-key",         "value": "={{ $env.ANTHROPIC_API_KEY }}"},
    {"name": "anthropic-version", "value": "2023-06-01"},
    {"name": "content-type",      "value": "application/json"}
]}

# ── Anthropic body expressions ────────────────────────────────────────────────
CURATOR_BODY = (
    "={{ JSON.stringify({"
    " model: 'claude-sonnet-4-6',"
    " max_tokens: 4096,"
    " system: $('Build Curator Input').first().json.system_prompt,"
    " messages: [{ role: 'user', content: $('Build Curator Input').first().json.user_message }]"
    " }) }}"
)

WRITER_BODY = (
    "={{ JSON.stringify({"
    " model: 'claude-sonnet-4-6',"
    " max_tokens: 8192,"
    " system: $('Build Writer Input').first().json.system_prompt,"
    " messages: [{ role: 'user', content: $('Build Writer Input').first().json.user_message }]"
    " }) }}"
)

# ── ElevenLabs body expression ────────────────────────────────────────────────
ELEVENLABS_BODY = (
    "={{ JSON.stringify({"
    " text: $json.text,"
    " model_id: 'eleven_multilingual_v2',"
    " voice_settings: { stability: 0.5, similarity_boost: 0.75 }"
    " }) }}"
)

# ── Telegram digest text ──────────────────────────────────────────────────────
TG_DIGEST_TEXT = (
    "={{ `📻 *Circuit Breakers — ${$('Parse Writer Response').first().json.episode_title}*\\n\\n"
    "${$('Parse Writer Response').first().json.episode_description}\\n\\n"
    "📖 *Deep Dives:*\\n"
    "${$('Parse Writer Response').first().json.deep_dives.map(d => '• ' + d.tease + '\\n  ' + d.url).join('\\n')}\\n\\n"
    "🎧 ${$('Upload to Buzzsprout').first().json.audio_url || '(processing)'}` }}"
)

# ── Workflow definition ───────────────────────────────────────────────────────
workflow = {
    "name": "01 - Daily Podcast Pipeline",
    "nodes": [
        # 1 – Schedule Trigger: 3:00 AM daily
        {
            "parameters": {
                "rule": {"interval": [{"field": "cronExpression", "expression": "0 3 * * *"}]}
            },
            "id": "daily-schedule",
            "name": "Daily Schedule",
            "type": "n8n-nodes-base.scheduleTrigger",
            "typeVersion": 1.2,
            "position": [240, 300]
        },
        # 2 – Fetch sources.json from GitHub
        {
            "parameters": {
                "url": "={{ $env.GITHUB_RAW_BASE_URL }}/config/sources.json",
                "options": {}
            },
            "id": "fetch-sources",
            "name": "Fetch sources.json",
            "type": "n8n-nodes-base.httpRequest",
            "typeVersion": 4.2,
            "position": [460, 300]
        },
        # 3 – Fetch show-format.json from GitHub
        {
            "parameters": {
                "url": "={{ $env.GITHUB_RAW_BASE_URL }}/config/show-format.json",
                "options": {}
            },
            "id": "fetch-show-format",
            "name": "Fetch show-format.json",
            "type": "n8n-nodes-base.httpRequest",
            "typeVersion": 4.2,
            "position": [680, 300]
        },
        # 4 – Ingest & Filter News (Code, run once)
        {
            "parameters": {
                "jsCode": JS_INGEST,
                "mode": "runOnceForAllItems"
            },
            "id": "ingest-news",
            "name": "Ingest & Filter News",
            "type": "n8n-nodes-base.code",
            "typeVersion": 2,
            "position": [900, 300]
        },
        # 5 – Fetch Curator Prompt from GitHub
        {
            "parameters": {
                "url": "={{ $env.GITHUB_RAW_BASE_URL }}/prompts/curator-agent.md",
                "options": {"response": {"response": {"responseFormat": "text"}}}
            },
            "id": "fetch-curator-prompt",
            "name": "Fetch Curator Prompt",
            "type": "n8n-nodes-base.httpRequest",
            "typeVersion": 4.2,
            "position": [1120, 300]
        },
        # 6 – Build Curator Input
        {
            "parameters": {"jsCode": JS_BUILD_CURATOR},
            "id": "build-curator-input",
            "name": "Build Curator Input",
            "type": "n8n-nodes-base.code",
            "typeVersion": 2,
            "position": [1340, 300]
        },
        # 7 – Call Claude — Curator Agent
        {
            "parameters": {
                "method": "POST",
                "url": "https://api.anthropic.com/v1/messages",
                "sendHeaders": True,
                "headerParameters": ANTHROPIC_HEADERS,
                "sendBody": True,
                "specifyBody": "json",
                "jsonBody": CURATOR_BODY,
                "options": {}
            },
            "id": "call-claude-curator",
            "name": "Call Claude - Curator",
            "type": "n8n-nodes-base.httpRequest",
            "typeVersion": 4.2,
            "position": [1560, 300]
        },
        # 8 – Parse Curator Response
        {
            "parameters": {"jsCode": JS_PARSE_CURATOR},
            "id": "parse-curator",
            "name": "Parse Curator Response",
            "type": "n8n-nodes-base.code",
            "typeVersion": 2,
            "position": [1780, 300]
        },
        # 9 – Fetch Writer Prompt from GitHub
        {
            "parameters": {
                "url": "={{ $env.GITHUB_RAW_BASE_URL }}/prompts/writer-agent.md",
                "options": {"response": {"response": {"responseFormat": "text"}}}
            },
            "id": "fetch-writer-prompt",
            "name": "Fetch Writer Prompt",
            "type": "n8n-nodes-base.httpRequest",
            "typeVersion": 4.2,
            "position": [2000, 300]
        },
        # 10 – Build Writer Input
        {
            "parameters": {"jsCode": JS_BUILD_WRITER},
            "id": "build-writer-input",
            "name": "Build Writer Input",
            "type": "n8n-nodes-base.code",
            "typeVersion": 2,
            "position": [2220, 300]
        },
        # 11 – Call Claude — Writer Agent
        {
            "parameters": {
                "method": "POST",
                "url": "https://api.anthropic.com/v1/messages",
                "sendHeaders": True,
                "headerParameters": ANTHROPIC_HEADERS,
                "sendBody": True,
                "specifyBody": "json",
                "jsonBody": WRITER_BODY,
                "options": {}
            },
            "id": "call-claude-writer",
            "name": "Call Claude - Writer",
            "type": "n8n-nodes-base.httpRequest",
            "typeVersion": 4.2,
            "position": [2440, 300]
        },
        # 12 – Parse Writer Response
        {
            "parameters": {"jsCode": JS_PARSE_WRITER},
            "id": "parse-writer",
            "name": "Parse Writer Response",
            "type": "n8n-nodes-base.code",
            "typeVersion": 2,
            "position": [2660, 300]
        },
        # 13 – Split Script into Lines (one item per HANS/FLINT line)
        {
            "parameters": {"jsCode": JS_SPLIT_LINES},
            "id": "split-lines",
            "name": "Split Script into Lines",
            "type": "n8n-nodes-base.code",
            "typeVersion": 2,
            "position": [2880, 300]
        },
        # 14 – Generate Voice via ElevenLabs (processes each item → binary MP3)
        {
            "parameters": {
                "method": "POST",
                "url": "={{ 'https://api.elevenlabs.io/v1/text-to-speech/' + $json.voice_id }}",
                "sendHeaders": True,
                "headerParameters": {"parameters": [
                    {"name": "xi-api-key",   "value": "={{ $env.ELEVENLABS_API_KEY }}"},
                    {"name": "content-type", "value": "application/json"},
                    {"name": "Accept",       "value": "audio/mpeg"}
                ]},
                "sendBody": True,
                "specifyBody": "json",
                "jsonBody": ELEVENLABS_BODY,
                "options": {
                    "response": {
                        "response": {
                            "responseFormat": "file",
                            "outputPropertyName": "data"
                        }
                    }
                }
            },
            "id": "generate-voice",
            "name": "Generate Voice",
            "type": "n8n-nodes-base.httpRequest",
            "typeVersion": 4.2,
            "position": [3100, 300]
        },
        # 15 – Concatenate all MP3 binary chunks → single episode file
        {
            "parameters": {
                "jsCode": JS_CONCAT_AUDIO,
                "mode": "runOnceForAllItems"
            },
            "id": "concat-audio",
            "name": "Concatenate Audio",
            "type": "n8n-nodes-base.code",
            "typeVersion": 2,
            "position": [3320, 300]
        },
        # 16 – Upload episode to Buzzsprout (multipart POST)
        {
            "parameters": {
                "method": "POST",
                "url": "={{ 'https://www.buzzsprout.com/api/' + $env.BUZZSPROUT_PODCAST_ID + '/episodes.json' }}",
                "sendHeaders": True,
                "headerParameters": {"parameters": [
                    {"name": "Authorization",
                     "value": "=Token token={{ $env.BUZZSPROUT_API_KEY }}"}
                ]},
                "sendBody": True,
                "contentType": "multipart-form-data",
                "bodyParameters": {"parameters": [
                    {"parameterType": "formBinaryData",
                     "name": "audio_file",
                     "inputDataFieldName": "data"},
                    {"parameterType": "formData",
                     "name": "title",
                     "value": "={{ $('Parse Writer Response').first().json.episode_title }}"},
                    {"parameterType": "formData",
                     "name": "description",
                     "value": "={{ $('Parse Writer Response').first().json.episode_description }}"},
                    {"parameterType": "formData",
                     "name": "private",
                     "value": "false"}
                ]},
                "options": {}
            },
            "id": "upload-buzzsprout",
            "name": "Upload to Buzzsprout",
            "type": "n8n-nodes-base.httpRequest",
            "typeVersion": 4.2,
            "position": [3540, 300]
        },
        # 17 – Send Telegram digest with episode link + deep dive URLs
        {
            "parameters": {
                "chatId": "={{ $env.TELEGRAM_CHAT_ID }}",
                "text":   TG_DIGEST_TEXT,
                "additionalFields": {
                    "parse_mode": "Markdown",
                    "disable_web_page_preview": True
                }
            },
            "id": "send-tg-digest",
            "name": "Send Telegram Digest",
            "type": "n8n-nodes-base.telegram",
            "typeVersion": 1.2,
            "position": [3760, 300],
            "credentials": {
                "telegramApi": {"id": TG_CRED_ID, "name": "Telegram Bot - Circuit Breakers"}
            }
        },
        # 18 – Write run log to /logs/YYYY-MM-DD.json
        {
            "parameters": {
                "jsCode": JS_WRITE_LOG,
                "mode": "runOnceForAllItems"
            },
            "id": "write-log",
            "name": "Write Run Log",
            "type": "n8n-nodes-base.code",
            "typeVersion": 2,
            "position": [3980, 300]
        },
        # 19 – Fire CFO workflow (non-blocking)
        {
            "parameters": {
                "workflowId": {"value": CFO_WF_ID, "mode": "id"},
                "options":    {"waitForSubWorkflow": False}
            },
            "id": "exec-cfo",
            "name": "Execute CFO Workflow",
            "type": "n8n-nodes-base.executeWorkflow",
            "typeVersion": 1.1,
            "position": [4200, 300]
        }
    ],
    "connections": {
        "Daily Schedule":          {"main": [[{"node": "Fetch sources.json",      "type": "main", "index": 0}]]},
        "Fetch sources.json":      {"main": [[{"node": "Fetch show-format.json",  "type": "main", "index": 0}]]},
        "Fetch show-format.json":  {"main": [[{"node": "Ingest & Filter News",    "type": "main", "index": 0}]]},
        "Ingest & Filter News":    {"main": [[{"node": "Fetch Curator Prompt",    "type": "main", "index": 0}]]},
        "Fetch Curator Prompt":    {"main": [[{"node": "Build Curator Input",     "type": "main", "index": 0}]]},
        "Build Curator Input":     {"main": [[{"node": "Call Claude - Curator",   "type": "main", "index": 0}]]},
        "Call Claude - Curator":   {"main": [[{"node": "Parse Curator Response",  "type": "main", "index": 0}]]},
        "Parse Curator Response":  {"main": [[{"node": "Fetch Writer Prompt",     "type": "main", "index": 0}]]},
        "Fetch Writer Prompt":     {"main": [[{"node": "Build Writer Input",      "type": "main", "index": 0}]]},
        "Build Writer Input":      {"main": [[{"node": "Call Claude - Writer",    "type": "main", "index": 0}]]},
        "Call Claude - Writer":    {"main": [[{"node": "Parse Writer Response",   "type": "main", "index": 0}]]},
        "Parse Writer Response":   {"main": [[{"node": "Split Script into Lines", "type": "main", "index": 0}]]},
        "Split Script into Lines": {"main": [[{"node": "Generate Voice",          "type": "main", "index": 0}]]},
        "Generate Voice":          {"main": [[{"node": "Concatenate Audio",       "type": "main", "index": 0}]]},
        "Concatenate Audio":       {"main": [[{"node": "Upload to Buzzsprout",    "type": "main", "index": 0}]]},
        "Upload to Buzzsprout":    {"main": [[{"node": "Send Telegram Digest",    "type": "main", "index": 0}]]},
        "Send Telegram Digest":    {"main": [[{"node": "Write Run Log",           "type": "main", "index": 0}]]},
        "Write Run Log":           {"main": [[{"node": "Execute CFO Workflow",    "type": "main", "index": 0}]]}
    },
    "settings": {
        "executionOrder": "v1",
        "saveManualExecutions": True,
        "callerPolicy": "workflowsFromSameOwner"
    }
}

print("→ Creating Workflow 01 - Daily Podcast Pipeline...")
result = n8n("POST", "/workflows", workflow)
wf_id = result["id"]
print(f"  ✓ Workflow ID: {wf_id}")
print(f"  ✓ Open at: http://localhost:5678/workflow/{wf_id}")
print()
print(f"Add to .env:  N8N_WORKFLOW_ID_DAILY_PIPELINE={wf_id}")
