"""
Creates Workflow 02 — Telegram Concierge in n8n via REST API.
Run from repo root: python3 scripts/create_workflow_concierge.py
"""
import json, urllib.request, urllib.error

# ── Load .env ────────────────────────────────────────────────────────────────
def load_env(path="./"):
    env = {}
    with open(f"{path}.env") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                env[k.strip()] = v.strip()
    return env

env = load_env()
N8N_URL          = "http://localhost:5678/api/v1"
N8N_API_KEY      = env["N8N_API_KEY"]
TELEGRAM_TOKEN   = env["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = env["TELEGRAM_CHAT_ID"]

# ── HTTP helper ───────────────────────────────────────────────────────────────
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

# ── Step 1: Telegram credential ───────────────────────────────────────────────
print("→ Creating Telegram credential...")
tg = n8n("POST", "/credentials", {
    "name": "Telegram Bot - Circuit Breakers",
    "type": "telegramApi",
    "data": {"accessToken": TELEGRAM_TOKEN}
})
TG_ID = str(tg["id"])
print(f"  ✓ ID: {TG_ID}")

# ── Step 2: Workflow definition ───────────────────────────────────────────────
JS_PARSE = r"""
// Extract JSON from Claude's response
const claudeOutput = $input.first().json;
const rawText = claudeOutput.content[0].text.trim();

// Strip markdown code fences if Claude wrapped the JSON
let jsonStr = rawText;
const fenceMatch = jsonStr.match(/^```(?:json)?\n?([\s\S]*?)\n?```$/);
if (fenceMatch) jsonStr = fenceMatch[1].trim();

const parsed = JSON.parse(jsonStr);

// Attach Telegram context for downstream nodes
const tgMsg = $('Telegram Trigger').first().json.message;
return [{
  json: {
    route:                 parsed.route,
    action:                parsed.action || '',
    response:              parsed.response,
    requires_confirmation: parsed.requires_confirmation || false,
    _chat_id:              String(tgMsg.chat.id),
    _message_id:           tgMsg.message_id,
    _original_text:        tgMsg.text
  }
}];
"""

JS_PLACEHOLDER = r"""
// Placeholder — will be wired to sub-workflows once CFO/Curator/Writer/Growth are built
const { route, action } = $input.first().json;
console.log(`[Concierge] Sub-workflow needed → route: ${route} | action: ${action}`);
return $input.all();
"""

JSON_BODY_EXPR = (
    "={{ JSON.stringify({"
    " model: 'claude-sonnet-4-6',"
    " max_tokens: 1024,"
    " system: $('Fetch Concierge Prompt').item.json.data,"
    " messages: [{ role: 'user', content: $('Telegram Trigger').item.json.message.text }]"
    " }) }}"
)

workflow = {
    "name": "02 - Telegram Concierge",
    "nodes": [
        # 1 ─ Telegram Trigger
        {
            "parameters": {"updates": ["message"], "additionalFields": {}},
            "id": "tg-trigger-001",
            "name": "Telegram Trigger",
            "type": "n8n-nodes-base.telegramTrigger",
            "typeVersion": 1.1,
            "position": [240, 300],
            "webhookId": "circuit-breakers-concierge",
            "credentials": {"telegramApi": {"id": TG_ID, "name": "Telegram Bot - Circuit Breakers"}}
        },
        # 2 ─ Fetch prompt from GitHub
        {
            "parameters": {
                "url": "={{ $env.GITHUB_RAW_BASE_URL }}/prompts/concierge-agent.md",
                "options": {"response": {"response": {"responseFormat": "text"}}}
            },
            "id": "fetch-prompt-001",
            "name": "Fetch Concierge Prompt",
            "type": "n8n-nodes-base.httpRequest",
            "typeVersion": 4.2,
            "position": [460, 300]
        },
        # 3 ─ Call Claude
        {
            "parameters": {
                "method": "POST",
                "url": "https://api.anthropic.com/v1/messages",
                "sendHeaders": True,
                "headerParameters": {"parameters": [
                    {"name": "x-api-key",        "value": "={{ $env.ANTHROPIC_API_KEY }}"},
                    {"name": "anthropic-version", "value": "2023-06-01"},
                    {"name": "content-type",      "value": "application/json"}
                ]},
                "sendBody": True,
                "specifyBody": "json",
                "jsonBody": JSON_BODY_EXPR,
                "options": {}
            },
            "id": "call-claude-001",
            "name": "Call Claude - Concierge",
            "type": "n8n-nodes-base.httpRequest",
            "typeVersion": 4.2,
            "position": [680, 300]
        },
        # 4 ─ Parse Claude response
        {
            "parameters": {"jsCode": JS_PARSE},
            "id": "parse-response-001",
            "name": "Parse Claude Response",
            "type": "n8n-nodes-base.code",
            "typeVersion": 2,
            "position": [900, 300]
        },
        # 5 ─ Send Telegram reply (always, regardless of route)
        {
            "parameters": {
                "chatId": "={{ $json._chat_id }}",
                "text":   "={{ $json.response }}",
                "additionalFields": {}
            },
            "id": "send-reply-001",
            "name": "Send Telegram Reply",
            "type": "n8n-nodes-base.telegram",
            "typeVersion": 1.2,
            "position": [1120, 160],
            "credentials": {"telegramApi": {"id": TG_ID, "name": "Telegram Bot - Circuit Breakers"}}
        },
        # 6 ─ IF: needs sub-workflow?
        {
            "parameters": {
                "conditions": {
                    "options": {"caseSensitive": True, "leftValue": "", "typeValidation": "strict"},
                    "conditions": [{
                        "id": "route-not-self",
                        "leftValue":  "={{ $json.route }}",
                        "rightValue": "self",
                        "operator": {"type": "string", "operation": "notEquals"}
                    }],
                    "combinator": "and"
                },
                "options": {}
            },
            "id": "if-subworkflow-001",
            "name": "Needs Sub-workflow?",
            "type": "n8n-nodes-base.if",
            "typeVersion": 2.2,
            "position": [1120, 460]
        },
        # 7 ─ Sub-workflow placeholder
        {
            "parameters": {"jsCode": JS_PLACEHOLDER},
            "id": "placeholder-001",
            "name": "Sub-workflow Placeholder",
            "type": "n8n-nodes-base.code",
            "typeVersion": 2,
            "position": [1340, 360]
        }
    ],
    "connections": {
        "Telegram Trigger":       {"main": [[{"node": "Fetch Concierge Prompt",  "type": "main", "index": 0}]]},
        "Fetch Concierge Prompt": {"main": [[{"node": "Call Claude - Concierge", "type": "main", "index": 0}]]},
        "Call Claude - Concierge":{"main": [[{"node": "Parse Claude Response",   "type": "main", "index": 0}]]},
        "Parse Claude Response":  {"main": [[
            {"node": "Send Telegram Reply",    "type": "main", "index": 0},
            {"node": "Needs Sub-workflow?",    "type": "main", "index": 0}
        ]]},
        "Needs Sub-workflow?": {"main": [
            [{"node": "Sub-workflow Placeholder", "type": "main", "index": 0}],
            []
        ]}
    },
    "settings": {
        "executionOrder": "v1",
        "saveManualExecutions": True,
        "callerPolicy": "workflowsFromSameOwner"
    },
}

# ── Step 3: Create workflow ────────────────────────────────────────────────────
print("→ Creating Workflow 02 - Telegram Concierge...")
result = n8n("POST", "/workflows", workflow)
wf_id = result["id"]
print(f"  ✓ Workflow ID: {wf_id}")
print(f"  ✓ Open at: http://localhost:5678/workflow/{wf_id}")
print()
print("Next: activate the workflow in n8n UI to enable the Telegram webhook.")
