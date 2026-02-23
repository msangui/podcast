"""
Creates Workflow 03 — CFO Agent in n8n via REST API.
Run from repo root: python3 scripts/create_workflow_cfo.py
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
TG_CRED_ID  = "rEjsdWsuMxLgKY8d"  # created during concierge setup

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

# ── Code node: build CFO input from fetched data ─────────────────────────────
JS_BUILD_INPUT = r"""
const prompt    = $('Fetch CFO Prompt').first().json.data;
const budget    = $('Fetch budget.json').first().json;
const trigger   = $('CFO Trigger').first().json;

const operatorCommand = trigger.operator_command || '';
const chatId          = String(trigger._chat_id || $env.TELEGRAM_CHAT_ID);

const userMessage = [
  operatorCommand ? `OPERATOR COMMAND: ${operatorCommand}` : 'TRIGGER: post-episode daily cost check',
  '',
  'BUDGET CONFIG:',
  JSON.stringify(budget, null, 2),
  '',
  'NOTE: Live API usage data pipeline not yet fully wired. Respond based on the operator command and budget thresholds. If no command, produce a daily status report with action: "none".'
].join('\n');

return [{ json: { user_message: userMessage, system_prompt: prompt, _chat_id: chatId } }];
"""

# ── Code node: parse Claude CFO response ─────────────────────────────────────
JS_PARSE = r"""
function extractJson(text) {
  const fences = [...text.matchAll(/```(?:json)?\s*\n([\s\S]*?)\n```/g)];
  if (fences.length > 0) return fences[fences.length - 1][1].trim();
  const bare = text.match(/(\{[\s\S]*\}|\[[\s\S]*\])/);
  if (bare) return bare[1].trim();
  return text.trim();
}
const rawText = $input.first().json.content[0].text;
const parsed  = JSON.parse(extractJson(rawText));
const chatId  = $('Build CFO Input').first().json._chat_id;

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

# ── JSON body expression for Anthropic ───────────────────────────────────────
JSON_BODY = (
    "={{ JSON.stringify({"
    " model: 'claude-sonnet-4-6',"
    " max_tokens: 2048,"
    " system: $('Build CFO Input').item.json.system_prompt,"
    " messages: [{ role: 'user', content: $('Build CFO Input').item.json.user_message }]"
    " }) }}"
)

# ── Shared headers for n8n API calls (pause/resume) ──────────────────────────
N8N_PATCH_HEADERS = {"parameters": [
    {"name": "X-N8N-API-KEY", "value": "={{ $env.N8N_API_KEY }}"},
    {"name": "content-type",  "value": "application/json"}
]}

def patch_workflow_node(node_id, name, wf_env_var, active, pos):
    """HTTP PATCH node to activate/deactivate a workflow via n8n API."""
    return {
        "parameters": {
            "method": "PATCH",
            "url": f"={{= 'http://localhost:5678/api/v1/workflows/' + $env.{wf_env_var} }}",
            "sendHeaders": True,
            "headerParameters": N8N_PATCH_HEADERS,
            "sendBody": True,
            "specifyBody": "json",
            "jsonBody": json.dumps({"active": active}),
            "options": {"response": {"response": {"responseFormat": "json"}}},
            "continueOnFail": True
        },
        "id": node_id,
        "name": name,
        "type": "n8n-nodes-base.httpRequest",
        "typeVersion": 4.2,
        "position": pos
    }

# ── Workflow definition ───────────────────────────────────────────────────────
workflow = {
    "name": "03 - CFO Agent",
    "nodes": [
        # 1 ─ Trigger (called by Concierge or Daily Pipeline)
        {
            "parameters": {},
            "id": "cfo-trigger",
            "name": "CFO Trigger",
            "type": "n8n-nodes-base.executeWorkflowTrigger",
            "typeVersion": 1,
            "position": [240, 300]
        },
        # 2 ─ Fetch CFO prompt from GitHub
        {
            "parameters": {
                "url": "={{ $env.GITHUB_RAW_BASE_URL }}/prompts/cfo-agent.md",
                "options": {"response": {"response": {"responseFormat": "text"}}}
            },
            "id": "fetch-cfo-prompt",
            "name": "Fetch CFO Prompt",
            "type": "n8n-nodes-base.httpRequest",
            "typeVersion": 4.2,
            "position": [460, 200]
        },
        # 3 ─ Fetch budget.json from GitHub
        {
            "parameters": {
                "url": "={{ $env.GITHUB_RAW_BASE_URL }}/config/budget.json",
                "options": {"response": {"response": {"responseFormat": "json"}}}
            },
            "id": "fetch-budget",
            "name": "Fetch budget.json",
            "type": "n8n-nodes-base.httpRequest",
            "typeVersion": 4.2,
            "position": [460, 400]
        },
        # 4 ─ Merge + build Claude input
        {
            "parameters": {"jsCode": JS_BUILD_INPUT},
            "id": "build-input",
            "name": "Build CFO Input",
            "type": "n8n-nodes-base.code",
            "typeVersion": 2,
            "position": [680, 300]
        },
        # 5 ─ Call Claude CFO
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
                "jsonBody": JSON_BODY,
                "options": {}
            },
            "id": "call-claude-cfo",
            "name": "Call Claude - CFO",
            "type": "n8n-nodes-base.httpRequest",
            "typeVersion": 4.2,
            "position": [900, 300]
        },
        # 6 ─ Parse CFO response
        {
            "parameters": {"jsCode": JS_PARSE},
            "id": "parse-cfo",
            "name": "Parse CFO Response",
            "type": "n8n-nodes-base.code",
            "typeVersion": 2,
            "position": [1120, 300]
        },
        # 7 ─ Send Telegram (always fires)
        {
            "parameters": {
                "chatId": "={{ $json._chat_id }}",
                "text":   "={{ $json.telegram_message }}",
                "additionalFields": {}
            },
            "id": "send-tg-cfo",
            "name": "Send CFO Telegram",
            "type": "n8n-nodes-base.telegram",
            "typeVersion": 1.2,
            "position": [1340, 160],
            "credentials": {"telegramApi": {"id": TG_CRED_ID, "name": "Telegram Bot - Circuit Breakers"}}
        },
        # 8 ─ Switch on action
        {
            "parameters": {
                "conditions": {
                    "options": {"caseSensitive": True, "leftValue": "", "typeValidation": "strict"},
                    "conditions": [
                        {"id": "is-pause",  "leftValue": "={{ $json.action }}", "rightValue": "pause",
                         "operator": {"type": "string", "operation": "equals"}},
                        {"id": "is-resume", "leftValue": "={{ $json.action }}", "rightValue": "resume",
                         "operator": {"type": "string", "operation": "equals"}}
                    ],
                    "combinator": "or"
                },
                "options": {}
            },
            "id": "action-switch",
            "name": "Pause or Resume?",
            "type": "n8n-nodes-base.if",
            "typeVersion": 2.2,
            "position": [1340, 440]
        },
        # 9-11 ─ Pause path: deactivate workflows
        patch_workflow_node("pause-pipeline", "Pause Daily Pipeline",
                            "N8N_WORKFLOW_ID_DAILY_PIPELINE", False, [1560, 300]),
        patch_workflow_node("pause-concierge", "Pause Concierge",
                            "N8N_WORKFLOW_ID_CONCIERGE",      False, [1780, 300]),
        patch_workflow_node("pause-growth", "Pause Growth",
                            "N8N_WORKFLOW_ID_GROWTH",         False, [2000, 300]),
        # 12-14 ─ Resume path: reactivate workflows
        patch_workflow_node("resume-pipeline", "Resume Daily Pipeline",
                            "N8N_WORKFLOW_ID_DAILY_PIPELINE", True, [1560, 560]),
        patch_workflow_node("resume-concierge", "Resume Concierge",
                            "N8N_WORKFLOW_ID_CONCIERGE",      True, [1780, 560]),
        patch_workflow_node("resume-growth", "Resume Growth",
                            "N8N_WORKFLOW_ID_GROWTH",         True, [2000, 560]),
    ],
    "connections": {
        # Trigger fans out to both fetches in parallel
        "CFO Trigger": {"main": [[
            {"node": "Fetch CFO Prompt",  "type": "main", "index": 0},
            {"node": "Fetch budget.json", "type": "main", "index": 0}
        ]]},
        # Both fetches feed into build input (n8n merges first-item from each)
        "Fetch CFO Prompt":  {"main": [[{"node": "Build CFO Input", "type": "main", "index": 0}]]},
        "Fetch budget.json": {"main": [[{"node": "Build CFO Input", "type": "main", "index": 0}]]},
        "Build CFO Input":   {"main": [[{"node": "Call Claude - CFO", "type": "main", "index": 0}]]},
        "Call Claude - CFO": {"main": [[{"node": "Parse CFO Response", "type": "main", "index": 0}]]},
        # Parse fans out: always send Telegram + check action
        "Parse CFO Response": {"main": [[
            {"node": "Send CFO Telegram", "type": "main", "index": 0},
            {"node": "Pause or Resume?",  "type": "main", "index": 0}
        ]]},
        # Action switch: true=pause path, false=resume path
        "Pause or Resume?": {"main": [
            [{"node": "Pause Daily Pipeline",  "type": "main", "index": 0}],
            [{"node": "Resume Daily Pipeline", "type": "main", "index": 0}]
        ]},
        # Pause chain
        "Pause Daily Pipeline":  {"main": [[{"node": "Pause Concierge",  "type": "main", "index": 0}]]},
        "Pause Concierge":       {"main": [[{"node": "Pause Growth",     "type": "main", "index": 0}]]},
        # Resume chain
        "Resume Daily Pipeline": {"main": [[{"node": "Resume Concierge", "type": "main", "index": 0}]]},
        "Resume Concierge":      {"main": [[{"node": "Resume Growth",    "type": "main", "index": 0}]]},
    },
    "settings": {
        "executionOrder": "v1",
        "saveManualExecutions": True,
        "callerPolicy": "workflowsFromSameOwner"
    }
}

print("→ Creating Workflow 03 - CFO Agent...")
result = n8n("POST", "/workflows", workflow)
wf_id = result["id"]
print(f"  ✓ Workflow ID: {wf_id}")
print(f"  ✓ Open at: http://localhost:5678/workflow/{wf_id}")
print()
print(f"Add to .env:  N8N_WORKFLOW_ID_CFO={wf_id}")
