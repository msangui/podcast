"""
Creates Workflow 05 — Source Rotation Admin in n8n via REST API.
Run from repo root: python3 scripts/create_workflow_source_rotation.py
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
TG_CRED_ID  = "rEjsdWsuMxLgKY8d"

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

# ── JS: Build Source Rotation Input ──────────────────────────────────────────
JS_BUILD_INPUT = r"""
const sourcesData = $('Fetch sources.json').first().json;
const prompt      = $('Fetch Source Rotation Prompt').first().json.data;
const trigger     = $('Source Rotation Trigger').first().json;

const operatorCommand = trigger.operator_command || '';
const chatId = String(trigger._chat_id || $env.TELEGRAM_CHAT_ID);

const userMessage = [
  operatorCommand
    ? `OPERATOR COMMAND: ${operatorCommand}`
    : 'TRIGGER: biweekly source rotation review',
  '',
  'CURRENT SOURCES CONFIG:',
  JSON.stringify(sourcesData, null, 2)
].join('\n');

return [{ json: { system_prompt: prompt, user_message: userMessage, _chat_id: chatId } }];
"""

# ── JS: Parse Source Rotation Response ───────────────────────────────────────
JS_PARSE = r"""
function extractJson(text) {
  const fences = [...text.matchAll(/```(?:json)?\s*\n([\s\S]*?)\n```/g)];
  if (fences.length > 0) return fences[fences.length - 1][1].trim();
  const bare = text.match(/(\{[\s\S]*\}|\[[\s\S]*\])/);
  if (bare) return bare[1].trim();
  return text.trim();
}
const parsed = JSON.parse(extractJson($input.first().json.content[0].text));
const chatId = $('Build Source Rotation Input').first().json._chat_id;

return [{
  json: {
    recommendation:   parsed.recommendation   || 'no_change',
    telegram_message: parsed.telegram_message || '(no message)',
    analysis:         parsed.analysis         || {},
    new_sources_array: parsed.new_sources_array || null,
    _chat_id:         String(chatId)
  }
}];
"""

# ── Anthropic body expression ─────────────────────────────────────────────────
CLAUDE_BODY = (
    "={{ JSON.stringify({"
    " model: 'claude-sonnet-4-6',"
    " max_tokens: 2048,"
    " system: $('Build Source Rotation Input').first().json.system_prompt,"
    " messages: [{ role: 'user', content: $('Build Source Rotation Input').first().json.user_message }]"
    " }) }}"
)

ANTHROPIC_HEADERS = {"parameters": [
    {"name": "x-api-key",         "value": "={{ $env.ANTHROPIC_API_KEY }}"},
    {"name": "anthropic-version", "value": "2023-06-01"},
    {"name": "content-type",      "value": "application/json"}
]}

# ── Workflow definition ───────────────────────────────────────────────────────
workflow = {
    "name": "05 - Source Rotation Admin",
    "nodes": [
        # 1 – Execute Workflow Trigger (called from Concierge)
        {
            "parameters": {},
            "id": "rotation-trigger",
            "name": "Source Rotation Trigger",
            "type": "n8n-nodes-base.executeWorkflowTrigger",
            "typeVersion": 1,
            "position": [240, 300]
        },
        # 2 – Fetch sources.json from GitHub
        {
            "parameters": {
                "url": "={{ $env.GITHUB_RAW_BASE_URL }}/config/sources.json",
                "options": {"response": {"response": {"responseFormat": "json"}}}
            },
            "id": "fetch-sources",
            "name": "Fetch sources.json",
            "type": "n8n-nodes-base.httpRequest",
            "typeVersion": 4.2,
            "position": [460, 200]
        },
        # 3 – Fetch Source Rotation Prompt from GitHub
        {
            "parameters": {
                "url": "={{ $env.GITHUB_RAW_BASE_URL }}/prompts/source-rotation-agent.md",
                "options": {"response": {"response": {"responseFormat": "text"}}}
            },
            "id": "fetch-rotation-prompt",
            "name": "Fetch Source Rotation Prompt",
            "type": "n8n-nodes-base.httpRequest",
            "typeVersion": 4.2,
            "position": [460, 400]
        },
        # 4 – Build input for Claude
        {
            "parameters": {"jsCode": JS_BUILD_INPUT},
            "id": "build-rotation-input",
            "name": "Build Source Rotation Input",
            "type": "n8n-nodes-base.code",
            "typeVersion": 2,
            "position": [680, 300]
        },
        # 5 – Call Claude — Source Rotation Agent
        {
            "parameters": {
                "method": "POST",
                "url": "https://api.anthropic.com/v1/messages",
                "sendHeaders": True,
                "headerParameters": ANTHROPIC_HEADERS,
                "sendBody": True,
                "specifyBody": "json",
                "jsonBody": CLAUDE_BODY,
                "options": {}
            },
            "id": "call-claude-rotation",
            "name": "Call Claude - Source Rotation",
            "type": "n8n-nodes-base.httpRequest",
            "typeVersion": 4.2,
            "position": [900, 300]
        },
        # 6 – Parse Claude response
        {
            "parameters": {"jsCode": JS_PARSE},
            "id": "parse-rotation",
            "name": "Parse Source Rotation Response",
            "type": "n8n-nodes-base.code",
            "typeVersion": 2,
            "position": [1120, 300]
        },
        # 7 – Send Telegram rotation report (always fires)
        {
            "parameters": {
                "chatId": "={{ $json._chat_id }}",
                "text":   "={{ $json.telegram_message }}",
                "additionalFields": {}
            },
            "id": "send-tg-rotation",
            "name": "Send Rotation Report",
            "type": "n8n-nodes-base.telegram",
            "typeVersion": 1.2,
            "position": [1340, 300],
            "credentials": {
                "telegramApi": {"id": TG_CRED_ID, "name": "Telegram Bot - Circuit Breakers"}
            }
        }
    ],
    "connections": {
        # Trigger fans out to both fetches in parallel
        "Source Rotation Trigger": {"main": [[
            {"node": "Fetch sources.json",           "type": "main", "index": 0},
            {"node": "Fetch Source Rotation Prompt", "type": "main", "index": 0}
        ]]},
        # Both feeds merge into Build Input
        "Fetch sources.json":           {"main": [[{"node": "Build Source Rotation Input", "type": "main", "index": 0}]]},
        "Fetch Source Rotation Prompt": {"main": [[{"node": "Build Source Rotation Input", "type": "main", "index": 0}]]},
        "Build Source Rotation Input":  {"main": [[{"node": "Call Claude - Source Rotation", "type": "main", "index": 0}]]},
        "Call Claude - Source Rotation":{"main": [[{"node": "Parse Source Rotation Response", "type": "main", "index": 0}]]},
        "Parse Source Rotation Response":{"main": [[{"node": "Send Rotation Report", "type": "main", "index": 0}]]}
    },
    "settings": {
        "executionOrder": "v1",
        "saveManualExecutions": True,
        "callerPolicy": "workflowsFromSameOwner"
    }
}

print("→ Creating Workflow 05 - Source Rotation Admin...")
result = n8n("POST", "/workflows", workflow)
wf_id = result["id"]
print(f"  ✓ Workflow ID: {wf_id}")
print(f"  ✓ Open at: http://localhost:5678/workflow/{wf_id}")
print()
print(f"Add to .env:  N8N_WORKFLOW_ID_SOURCE_ROTATION={wf_id}")
