#!/usr/bin/env python3
"""
Creates all Wetsoda call-center agent workflows in n8n via REST API.
Connects to mogala VOIP SAAS (JWT auth) + local Ollama LLM.
"""
import json, uuid, subprocess, sys

BASE = "http://localhost:5678"
COOKIES = "/tmp/n8n_cookies.txt"

def api(method, path, data=None, raw=False):
    cmd = ["curl", "-s", "-b", COOKIES, "-X", method]
    if data:
        cmd += ["-H", "Content-Type: application/json", "-d", json.dumps(data)]
    cmd.append(f"{BASE}{path}")
    r = subprocess.run(cmd, capture_output=True, text=True)
    if not r.stdout.strip():
        raise Exception(f"Empty response for {method} {path}: {r.stderr}")
    parsed = json.loads(r.stdout) if not raw else r.stdout
    if isinstance(parsed, dict) and parsed.get("status") == "error":
        raise Exception(f"API error {method} {path}: {parsed.get('message')}")
    return parsed

def uid(): return str(uuid.uuid4())

print("=== Wetsoda Call-Center Agent Builder ===\n")
print("Note: mogala config is read from docker env vars (MOGALA_API_URL etc.)\n")

# ── Ollama credential ─────────────────────────────────────────────────
print("Creating Ollama credential...")
existing = api("GET", "/rest/credentials?includeData=false")
existing_ollama = [c for c in existing.get("data", []) if c["type"] == "ollamaApi"]
if existing_ollama:
    OLLAMA_CRED_ID = existing_ollama[0]["id"]
    print(f"  ✓ Ollama credential already exists id: {OLLAMA_CRED_ID}")
else:
    ollama_cred = api("POST", "/rest/credentials", {
        "name": "Ollama Local",
        "type": "ollamaApi",
        "data": {"baseUrl": "http://ollama:11434"},
        "nodesAccess": []
    })
    OLLAMA_CRED_ID = ollama_cred["data"]["id"]
    print(f"  ✓ Ollama credential created id: {OLLAMA_CRED_ID}")

# ── Helper: Ollama model node ──────────────────────────────────────────
def ollama_node(node_id, x, y, parent_agent_id):
    return {
        "parameters": {"model": "llama3.2", "options": {"temperature": 0.3}},
        "id": node_id,
        "name": "Ollama LLM",
        "type": "@n8n/n8n-nodes-langchain.lmChatOllama",
        "typeVersion": 1,
        "position": [x, y],
        "credentials": {"ollamaApi": {"id": OLLAMA_CRED_ID, "name": "Ollama Local"}}
    }

# ── Helper: mogala auth sub-nodes (login → extract JWT) ───────────────
def mogala_auth_nodes(login_id, x, y):
    return [
        {
            "parameters": {
                "method": "POST",
                "url": "={{ $env.MOGALA_API_URL }}/auth/login",
                "sendBody": True,
                "bodyParameters": {"parameters": [
                    {"name": "email",    "value": "={{ $env.MOGALA_EMAIL }}"},
                    {"name": "password", "value": "={{ $env.MOGALA_PASSWORD }}"},
                    {"name": "domain",   "value": "={{ $env.MOGALA_DOMAIN }}"}
                ]},
                "options": {}
            },
            "id": login_id,
            "name": "Mogala Login",
            "type": "n8n-nodes-base.httpRequest",
            "typeVersion": 4.2,
            "position": [x, y]
        }
    ]

# ═══════════════════════════════════════════════════════════════════════
# WORKFLOW 1: Inbound Call Triage Agent
# ═══════════════════════════════════════════════════════════════════════
print("\nBuilding Workflow 1: Inbound Call Triage...")

W1 = {
    "name": "🎯 Inbound Call Triage Agent",
    "settings": {"executionOrder": "v1"},
    "active": False,
    "tags": [{"id": "AFB7zOhMECO42yKb", "name": "call-center"}],
    "nodes": [
        # Schedule: every 30 seconds
        {
            "parameters": {"rule": {"interval": [{"field": "seconds", "secondsInterval": 30}]}},
            "id": uid(), "name": "Every 30s",
            "type": "n8n-nodes-base.scheduleTrigger",
            "typeVersion": 1.2, "position": [0, 300]
        },
        # Mogala login
        *mogala_auth_nodes("w1-login", 220, 300),
        # Fetch recent call logs
        {
            "parameters": {
                "url": "={{ $env.MOGALA_API_URL }}/api/call-logs",
                "authentication": "genericCredentialType",
                "genericAuthType": "httpHeaderAuth",
                "sendHeaders": True,
                "headerParameters": {"parameters": [{
                    "name": "Authorization",
                    "value": "=Bearer {{ $('Mogala Login').item.json.token }}"
                }]},
                "options": {}
            },
            "id": uid(), "name": "Get Call Logs",
            "type": "n8n-nodes-base.httpRequest",
            "typeVersion": 4.2, "position": [440, 300]
        },
        # Filter: only new calls (started in last 35 seconds)
        {
            "parameters": {
                "jsCode": """
const now = Date.now();
const cutoff = now - 35000; // 35s window

return $input.all()
  .flatMap(item => item.json)
  .filter(call => {
    const started = new Date(call.started_at).getTime();
    return started > cutoff && call.status !== 'processed';
  })
  .map(call => ({ json: call }));
"""
            },
            "id": uid(), "name": "Filter New Calls",
            "type": "n8n-nodes-base.code",
            "typeVersion": 2, "position": [660, 300]
        },
        # AI Agent: classify intent
        {
            "parameters": {
                "text": """=Classify this inbound call and respond with ONLY a JSON object:

Call details:
- Caller: {{ $json.caller }}
- Called number: {{ $json.callee }}
- Time: {{ $json.started_at }}
- Duration: {{ $json.duration }}s
- Status: {{ $json.status }}

Respond with exactly this JSON structure:
{
  "intent": "support|sales|billing|emergency|other",
  "confidence": 0.0-1.0,
  "reason": "brief explanation",
  "priority": "high|medium|low",
  "caller": "{{ $json.caller }}",
  "callee": "{{ $json.callee }}"
}""",
                "options": {"systemMessage": "You are a call center routing AI. Classify inbound calls based on the called number and caller patterns. Respond only with valid JSON."}
            },
            "id": "w1-agent", "name": "Classify Intent",
            "type": "@n8n/n8n-nodes-langchain.chainLlm",
            "typeVersion": 1.4, "position": [880, 300]
        },
        # Ollama LLM for classifier
        {
            "parameters": {"model": "llama3.2", "options": {"temperature": 0.1}},
            "id": uid(), "name": "Ollama LLM",
            "type": "@n8n/n8n-nodes-langchain.lmChatOllama",
            "typeVersion": 1, "position": [880, 500],
            "credentials": {"ollamaApi": {"id": OLLAMA_CRED_ID, "name": "Ollama Local"}}
        },
        # Parse AI response
        {
            "parameters": {
                "jsCode": """
const raw = $input.item.json.text || $input.item.json.response || '';
let parsed;
try {
  const match = raw.match(/\\{[\\s\\S]*\\}/);
  parsed = JSON.parse(match ? match[0] : raw);
} catch(e) {
  parsed = { intent: 'other', confidence: 0, reason: 'parse error', priority: 'low', caller: '', callee: '' };
}
return [{ json: { ...parsed, raw_response: raw } }];
"""
            },
            "id": uid(), "name": "Parse Classification",
            "type": "n8n-nodes-base.code",
            "typeVersion": 2, "position": [1100, 300]
        },
        # Switch by intent
        {
            "parameters": {
                "mode": "rules",
                "rules": {"values": [
                    {"conditions": {"options": {"caseSensitive": False}, "conditions": [{"leftValue": "={{ $json.intent }}", "rightValue": "support", "operator": {"type": "string", "operation": "equals"}}]}},
                    {"conditions": {"options": {"caseSensitive": False}, "conditions": [{"leftValue": "={{ $json.intent }}", "rightValue": "sales", "operator": {"type": "string", "operation": "equals"}}]}},
                    {"conditions": {"options": {"caseSensitive": False}, "conditions": [{"leftValue": "={{ $json.intent }}", "rightValue": "billing", "operator": {"type": "string", "operation": "equals"}}]}},
                    {"conditions": {"options": {"caseSensitive": False}, "conditions": [{"leftValue": "={{ $json.intent }}", "rightValue": "emergency", "operator": {"type": "string", "operation": "equals"}}]}}
                ]},
                "fallbackOutput": "extra"
            },
            "id": uid(), "name": "Route by Intent",
            "type": "n8n-nodes-base.switch",
            "typeVersion": 3, "position": [1320, 300]
        },
        {"parameters": {"notice": "➡️ Route to Support Queue\nAdd: Assign to support agent, send Slack alert, create ticket"}, "id": uid(), "name": "→ Support",  "type": "n8n-nodes-base.stickyNote", "typeVersion": 1, "position": [1540, 160]},
        {"parameters": {"notice": "➡️ Route to Sales Queue\nAdd: Notify sales team, create lead in CRM"},                   "id": uid(), "name": "→ Sales",    "type": "n8n-nodes-base.stickyNote", "typeVersion": 1, "position": [1540, 280]},
        {"parameters": {"notice": "➡️ Route to Billing Queue\nAdd: Pull account info, assign billing agent"},               "id": uid(), "name": "→ Billing",  "type": "n8n-nodes-base.stickyNote", "typeVersion": 1, "position": [1540, 400]},
        {"parameters": {"notice": "🚨 Emergency Route\nAdd: Page on-call, immediate escalation"},                           "id": uid(), "name": "→ Emergency","type": "n8n-nodes-base.stickyNote", "typeVersion": 1, "position": [1540, 520]},
        {"parameters": {"notice": "❓ Fallback / Unknown\nAdd: IVR menu, general queue"},                                   "id": uid(), "name": "→ Other",    "type": "n8n-nodes-base.stickyNote", "typeVersion": 1, "position": [1540, 640]},
    ],
    "connections": {
        "Every 30s":           {"main": [[{"node": "Mogala Login",       "type": "main", "index": 0}]]},
        "Mogala Login":        {"main": [[{"node": "Get Call Logs",      "type": "main", "index": 0}]]},
        "Get Call Logs":       {"main": [[{"node": "Filter New Calls",   "type": "main", "index": 0}]]},
        "Filter New Calls":    {"main": [[{"node": "Classify Intent",    "type": "main", "index": 0}]]},
        "Ollama LLM":          {"ai_languageModel": [[{"node": "Classify Intent", "type": "ai_languageModel", "index": 0}]]},
        "Classify Intent":     {"main": [[{"node": "Parse Classification","type": "main", "index": 0}]]},
        "Parse Classification":{"main": [[{"node": "Route by Intent",    "type": "main", "index": 0}]]},
    }
}

r1 = api("POST", "/rest/workflows", W1)
W1_ID = r1["data"]["id"]
print(f"  ✓ Created id={W1_ID}")

# ═══════════════════════════════════════════════════════════════════════
# WORKFLOW 2: Outbound Dialer Agent
# ═══════════════════════════════════════════════════════════════════════
print("Building Workflow 2: Outbound Dialer...")

W2 = {
    "name": "📞 Outbound Dialer Agent",
    "settings": {"executionOrder": "v1"},
    "active": False,
    "tags": [{"id": "AFB7zOhMECO42yKb", "name": "call-center"}],
    "nodes": [
        # Webhook trigger: POST a contact list to start dialing
        {
            "parameters": {
                "httpMethod": "POST", "path": "outbound-dialer",
                "responseMode": "responseNode", "options": {}
            },
            "id": uid(), "name": "Start Dialing Campaign",
            "type": "n8n-nodes-base.webhook",
            "typeVersion": 2, "position": [0, 300],
            "webhookId": "wetsoda-outbound-dialer"
        },
        # Parse campaign payload
        {
            "parameters": {
                "jsCode": """
// Expected payload: { contacts: [{phone, name, context}], script_goal: "..." }
const body = $input.item.json.body || $input.item.json;
const contacts = body.contacts || [];
const scriptGoal = body.script_goal || 'General outbound call';
return contacts.map(c => ({
  json: { phone: c.phone, name: c.name || 'Unknown', context: c.context || '', scriptGoal }
}));
"""
            },
            "id": uid(), "name": "Parse Contact List",
            "type": "n8n-nodes-base.code",
            "typeVersion": 2, "position": [220, 300]
        },
        # Auth
        *mogala_auth_nodes("w2-login", 440, 300),
        # Get available extensions (agents)
        {
            "parameters": {
                "url": "={{ $env.MOGALA_API_URL }}/api/extensions",
                "sendHeaders": True,
                "headerParameters": {"parameters": [{"name": "Authorization", "value": "=Bearer {{ $('Mogala Login').item.json.token }}"}]},
                "options": {}
            },
            "id": uid(), "name": "Get Agent Extensions",
            "type": "n8n-nodes-base.httpRequest",
            "typeVersion": 4.2, "position": [660, 300]
        },
        # AI Agent: generate personalised call script
        {
            "parameters": {
                "text": """=Generate a concise outbound call script for this contact.

Contact: {{ $json.name }} ({{ $json.phone }})
Context: {{ $json.context }}
Goal: {{ $json.scriptGoal }}
Available agent extensions: {{ $('Get Agent Extensions').item.json }}

Respond with JSON:
{
  "contact_phone": "{{ $json.phone }}",
  "contact_name": "{{ $json.name }}",
  "opening": "word-for-word opening line",
  "key_points": ["point1", "point2", "point3"],
  "objection_handlers": {"common objection": "response"},
  "closing": "closing line",
  "assigned_extension": "best extension number to use"
}""",
                "options": {"systemMessage": "You are an outbound call script writer for a call center. Write natural, conversational scripts. Respond only with valid JSON."}
            },
            "id": "w2-agent", "name": "Generate Call Script",
            "type": "@n8n/n8n-nodes-langchain.chainLlm",
            "typeVersion": 1.4, "position": [880, 300]
        },
        {
            "parameters": {"model": "llama3.2", "options": {"temperature": 0.5}},
            "id": uid(), "name": "Ollama LLM",
            "type": "@n8n/n8n-nodes-langchain.lmChatOllama",
            "typeVersion": 1, "position": [880, 500],
            "credentials": {"ollamaApi": {"id": OLLAMA_CRED_ID, "name": "Ollama Local"}}
        },
        # Parse script
        {
            "parameters": {
                "jsCode": """
const raw = $input.item.json.text || '';
let script;
try { script = JSON.parse(raw.match(/\\{[\\s\\S]*\\}/)[0]); } catch(e) { script = {error: 'parse failed', raw}; }
return [{ json: { ...script, generated_at: new Date().toISOString() } }];
"""
            },
            "id": uid(), "name": "Parse Script",
            "type": "n8n-nodes-base.code",
            "typeVersion": 2, "position": [1100, 300]
        },
        # Originate call via Asterisk AMI (stub — configure with AMI details)
        {
            "parameters": {
                "notice": "🔧 CONFIGURE: Add HTTP Request to Asterisk AMI or Kamailio API\nEndpoint: POST /ari/channels or AMI Originate\nPayload: { channel: 'SIP/{{ $json.assigned_extension }}', exten: '{{ $json.contact_phone }}' }\nAuth: Bearer from mogala login"
            },
            "id": uid(), "name": "⚙️ Originate Call (configure me)",
            "type": "n8n-nodes-base.stickyNote",
            "typeVersion": 1, "position": [1320, 200]
        },
        # Log the dialing event
        {
            "parameters": {
                "jsCode": """
const item = $input.item.json;
console.log('[Wetsoda Dialer]', JSON.stringify({
  contact: item.contact_name,
  phone: item.contact_phone,
  extension: item.assigned_extension,
  timestamp: new Date().toISOString()
}));
return [{ json: { status: 'queued', ...item } }];
"""
            },
            "id": uid(), "name": "Log Dial Event",
            "type": "n8n-nodes-base.code",
            "typeVersion": 2, "position": [1320, 380]
        },
        # Respond to webhook
        {
            "parameters": {
                "respondWith": "json",
                "responseBody": "={{ JSON.stringify({ status: 'ok', queued: $input.all().length }) }}"
            },
            "id": uid(), "name": "Campaign Response",
            "type": "n8n-nodes-base.respondToWebhook",
            "typeVersion": 1.1, "position": [1540, 380]
        },
    ],
    "connections": {
        "Start Dialing Campaign": {"main": [[{"node": "Parse Contact List",     "type": "main", "index": 0}]]},
        "Parse Contact List":    {"main": [[{"node": "Mogala Login",            "type": "main", "index": 0}]]},
        "Mogala Login":          {"main": [[{"node": "Get Agent Extensions",    "type": "main", "index": 0}]]},
        "Get Agent Extensions":  {"main": [[{"node": "Generate Call Script",    "type": "main", "index": 0}]]},
        "Ollama LLM":            {"ai_languageModel": [[{"node": "Generate Call Script", "type": "ai_languageModel", "index": 0}]]},
        "Generate Call Script":  {"main": [[{"node": "Parse Script",            "type": "main", "index": 0}]]},
        "Parse Script":          {"main": [[{"node": "Log Dial Event",          "type": "main", "index": 0}]]},
        "Log Dial Event":        {"main": [[{"node": "Campaign Response",       "type": "main", "index": 0}]]},
    }
}

r2 = api("POST", "/rest/workflows", W2)
W2_ID = r2["data"]["id"]
print(f"  ✓ Created id={W2_ID}")

# ═══════════════════════════════════════════════════════════════════════
# WORKFLOW 3: Live Call Assistant
# ═══════════════════════════════════════════════════════════════════════
print("Building Workflow 3: Live Call Assistant...")

W3 = {
    "name": "🎧 Live Call Assistant",
    "settings": {"executionOrder": "v1"},
    "active": False,
    "tags": [{"id": "AFB7zOhMECO42yKb", "name": "call-center"}],
    "nodes": [
        {
            "parameters": {"rule": {"interval": [{"field": "seconds", "secondsInterval": 15}]}},
            "id": uid(), "name": "Every 15s",
            "type": "n8n-nodes-base.scheduleTrigger",
            "typeVersion": 1.2, "position": [0, 300]
        },
        *mogala_auth_nodes("w3-login", 220, 300),
        # Get live call logs
        {
            "parameters": {
                "url": "={{ $env.MOGALA_API_URL }}/api/call-logs",
                "sendHeaders": True,
                "headerParameters": {"parameters": [{"name": "Authorization", "value": "=Bearer {{ $('Mogala Login').item.json.token }}"}]},
                "options": {}
            },
            "id": uid(), "name": "Get Live Calls",
            "type": "n8n-nodes-base.httpRequest",
            "typeVersion": 4.2, "position": [440, 300]
        },
        # Filter: calls that started within last 20s (likely live/ringing)
        {
            "parameters": {
                "jsCode": """
const now = Date.now();
const liveCalls = $input.all()
  .flatMap(i => Array.isArray(i.json) ? i.json : [i.json])
  .filter(call => {
    const started = new Date(call.started_at).getTime();
    const ageMs = now - started;
    return ageMs < 20000 && (call.status === 'ringing' || call.status === 'active' || call.duration === 0);
  });

if (liveCalls.length === 0) return [];
return liveCalls.map(c => ({ json: c }));
"""
            },
            "id": uid(), "name": "Filter Live Calls",
            "type": "n8n-nodes-base.code",
            "typeVersion": 2, "position": [660, 300]
        },
        # Get caller's history from mogala
        {
            "parameters": {
                "url": "={{ $env.MOGALA_API_URL }}/api/call-logs?caller={{ encodeURIComponent($json.caller) }}",
                "sendHeaders": True,
                "headerParameters": {"parameters": [{"name": "Authorization", "value": "=Bearer {{ $('Mogala Login').item.json.token }}"}]},
                "options": {}
            },
            "id": uid(), "name": "Get Caller History",
            "type": "n8n-nodes-base.httpRequest",
            "typeVersion": 4.2, "position": [880, 300]
        },
        # AI: surface context for the agent
        {
            "parameters": {
                "text": """=A call is coming in RIGHT NOW. Brief the agent in 3 bullet points max.

Current call:
- Caller: {{ $('Filter Live Calls').item.json.caller }}
- Called: {{ $('Filter Live Calls').item.json.callee }}
- Started: {{ $('Filter Live Calls').item.json.started_at }}

Caller history (recent calls): {{ JSON.stringify($json) }}

Respond with JSON:
{
  "caller": "{{ $('Filter Live Calls').item.json.caller }}",
  "brief": ["bullet 1", "bullet 2", "bullet 3"],
  "suggested_greeting": "Hi [caller name], ...",
  "flags": ["VIP" | "Repeat caller" | "First time" | "Recent issue"],
  "recommended_action": "what the agent should do first"
}""",
                "options": {"systemMessage": "You are a real-time call center assistant. Give ultra-brief, actionable context. Respond only with valid JSON."}
            },
            "id": "w3-agent", "name": "Surface Call Context",
            "type": "@n8n/n8n-nodes-langchain.chainLlm",
            "typeVersion": 1.4, "position": [1100, 300]
        },
        {
            "parameters": {"model": "llama3.2", "options": {"temperature": 0.2}},
            "id": uid(), "name": "Ollama LLM",
            "type": "@n8n/n8n-nodes-langchain.lmChatOllama",
            "typeVersion": 1, "position": [1100, 500],
            "credentials": {"ollamaApi": {"id": OLLAMA_CRED_ID, "name": "Ollama Local"}}
        },
        # Format for dashboard
        {
            "parameters": {
                "jsCode": """
const raw = $input.item.json.text || '';
let context;
try { context = JSON.parse(raw.match(/\\{[\\s\\S]*\\}/)[0]); } catch(e) { context = { error: 'parse failed' }; }
const out = { ...context, timestamp: new Date().toISOString(), workflow: 'live-call-assistant' };
console.log('[LiveAssist]', JSON.stringify(out));
return [{ json: out }];
"""
            },
            "id": uid(), "name": "Format for Dashboard",
            "type": "n8n-nodes-base.code",
            "typeVersion": 2, "position": [1320, 300]
        },
        {
            "parameters": {"notice": "🔧 CONFIGURE: Push context to your agent dashboard\nOptions:\n• POST to your dashboard webhook\n• Write to a shared data table\n• Send Slack/Teams notification\n• WebSocket push via external service"},
            "id": uid(), "name": "⚙️ Push to Dashboard (configure me)",
            "type": "n8n-nodes-base.stickyNote",
            "typeVersion": 1, "position": [1540, 300]
        },
    ],
    "connections": {
        "Every 15s":         {"main": [[{"node": "Mogala Login",       "type": "main", "index": 0}]]},
        "Mogala Login":      {"main": [[{"node": "Get Live Calls",     "type": "main", "index": 0}]]},
        "Get Live Calls":    {"main": [[{"node": "Filter Live Calls",  "type": "main", "index": 0}]]},
        "Filter Live Calls": {"main": [[{"node": "Get Caller History", "type": "main", "index": 0}]]},
        "Get Caller History":{"main": [[{"node": "Surface Call Context","type": "main", "index": 0}]]},
        "Ollama LLM":        {"ai_languageModel": [[{"node": "Surface Call Context", "type": "ai_languageModel", "index": 0}]]},
        "Surface Call Context":{"main": [[{"node": "Format for Dashboard","type": "main", "index": 0}]]},
    }
}

r3 = api("POST", "/rest/workflows", W3)
W3_ID = r3["data"]["id"]
print(f"  ✓ Created id={W3_ID}")

# ═══════════════════════════════════════════════════════════════════════
# WORKFLOW 4: Post-Call Summarizer
# ═══════════════════════════════════════════════════════════════════════
print("Building Workflow 4: Post-Call Summarizer...")

W4 = {
    "name": "📝 Post-Call Summarizer",
    "settings": {"executionOrder": "v1"},
    "active": False,
    "tags": [{"id": "AFB7zOhMECO42yKb", "name": "call-center"}],
    "nodes": [
        {
            "parameters": {"rule": {"interval": [{"field": "minutes", "minutesInterval": 2}]}},
            "id": uid(), "name": "Every 2 Minutes",
            "type": "n8n-nodes-base.scheduleTrigger",
            "typeVersion": 1.2, "position": [0, 300]
        },
        *mogala_auth_nodes("w4-login", 220, 300),
        # Get recent completed calls
        {
            "parameters": {
                "url": "={{ $env.MOGALA_API_URL }}/api/call-logs",
                "sendHeaders": True,
                "headerParameters": {"parameters": [{"name": "Authorization", "value": "=Bearer {{ $('Mogala Login').item.json.token }}"}]},
                "options": {}
            },
            "id": uid(), "name": "Get Completed Calls",
            "type": "n8n-nodes-base.httpRequest",
            "typeVersion": 4.2, "position": [440, 300]
        },
        # Filter: completed calls in last 3 minutes (> 0 duration)
        {
            "parameters": {
                "jsCode": """
const now = Date.now();
const windowMs = 3 * 60 * 1000;

// Track processed call IDs (stored in workflow static data)
const processed = $getWorkflowStaticData('global').processedCallIds || [];

const toSummarize = $input.all()
  .flatMap(i => Array.isArray(i.json) ? i.json : [i.json])
  .filter(call => {
    const started = new Date(call.started_at).getTime();
    const age = now - started;
    return call.duration > 0 &&
           age < windowMs &&
           age > 5000 && // call must be > 5s old (completed)
           !processed.includes(call.id);
  });

return toSummarize.map(c => ({ json: c }));
"""
            },
            "id": uid(), "name": "Filter Unsummarized",
            "type": "n8n-nodes-base.code",
            "typeVersion": 2, "position": [660, 300]
        },
        # AI: summarize the call
        {
            "parameters": {
                "text": """=Write a concise post-call summary for this completed call.

Call record:
- Caller: {{ $json.caller }}
- Agent/Extension: {{ $json.callee }}
- Duration: {{ $json.duration }} seconds ({{ Math.round($json.duration / 60) }} min)
- Started: {{ $json.started_at }}
- Ended: {{ $json.ended_at || 'N/A' }}
- Status: {{ $json.status }}

Respond with JSON:
{
  "call_id": "{{ $json.id }}",
  "caller": "{{ $json.caller }}",
  "agent": "{{ $json.callee }}",
  "duration_seconds": {{ $json.duration }},
  "summary": "2-3 sentence summary of what likely happened based on duration and routing",
  "outcome": "resolved|escalated|voicemail|abandoned|transferred",
  "follow_up_required": true|false,
  "follow_up_note": "what needs to happen next (if any)",
  "sentiment": "positive|neutral|negative",
  "tags": ["tag1", "tag2"]
}""",
                "options": {"systemMessage": "You are a post-call analyst. Generate concise, professional call summaries. Respond only with valid JSON."}
            },
            "id": "w4-agent", "name": "Summarize Call",
            "type": "@n8n/n8n-nodes-langchain.chainLlm",
            "typeVersion": 1.4, "position": [880, 300]
        },
        {
            "parameters": {"model": "llama3.2", "options": {"temperature": 0.3}},
            "id": uid(), "name": "Ollama LLM",
            "type": "@n8n/n8n-nodes-langchain.lmChatOllama",
            "typeVersion": 1, "position": [880, 500],
            "credentials": {"ollamaApi": {"id": OLLAMA_CRED_ID, "name": "Ollama Local"}}
        },
        # Parse summary
        {
            "parameters": {
                "jsCode": """
const raw = $input.item.json.text || '';
let summary;
try { summary = JSON.parse(raw.match(/\\{[\\s\\S]*\\}/)[0]); } catch(e) { summary = { error: 'parse failed', raw }; }
return [{ json: { ...summary, summarized_at: new Date().toISOString() } }];
"""
            },
            "id": uid(), "name": "Parse Summary",
            "type": "n8n-nodes-base.code",
            "typeVersion": 2, "position": [1100, 300]
        },
        # Mark as processed + log
        {
            "parameters": {
                "jsCode": """
const summary = $input.item.json;
// Persist processed call ID to avoid re-processing
const staticData = $getWorkflowStaticData('global');
if (!staticData.processedCallIds) staticData.processedCallIds = [];
staticData.processedCallIds.push(summary.call_id);
// Keep only last 500 processed IDs (rolling window)
if (staticData.processedCallIds.length > 500) {
  staticData.processedCallIds = staticData.processedCallIds.slice(-500);
}
console.log('[PostCallSummarizer] Logged:', summary.call_id, summary.outcome);
return [{ json: summary }];
"""
            },
            "id": uid(), "name": "Mark Processed",
            "type": "n8n-nodes-base.code",
            "typeVersion": 2, "position": [1320, 300]
        },
        # Push to CRM/logging
        {
            "parameters": {
                "notice": "🔧 CONFIGURE: Send summary to your CRM or logging system\nOptions:\n• POST to your CRM webhook (HubSpot, Salesforce, etc.)\n• Append to Google Sheets\n• Write to a database\n• Send to Slack #call-summaries channel\n• Store in n8n Data Table"
            },
            "id": uid(), "name": "⚙️ Push to CRM (configure me)",
            "type": "n8n-nodes-base.stickyNote",
            "typeVersion": 1, "position": [1540, 300]
        },
    ],
    "connections": {
        "Every 2 Minutes":     {"main": [[{"node": "Mogala Login",          "type": "main", "index": 0}]]},
        "Mogala Login":        {"main": [[{"node": "Get Completed Calls",   "type": "main", "index": 0}]]},
        "Get Completed Calls": {"main": [[{"node": "Filter Unsummarized",   "type": "main", "index": 0}]]},
        "Filter Unsummarized": {"main": [[{"node": "Summarize Call",        "type": "main", "index": 0}]]},
        "Ollama LLM":          {"ai_languageModel": [[{"node": "Summarize Call", "type": "ai_languageModel", "index": 0}]]},
        "Summarize Call":      {"main": [[{"node": "Parse Summary",         "type": "main", "index": 0}]]},
        "Parse Summary":       {"main": [[{"node": "Mark Processed",        "type": "main", "index": 0}]]},
    }
}

r4 = api("POST", "/rest/workflows", W4)
W4_ID = r4["data"]["id"]
print(f"  ✓ Created id={W4_ID}")

# ── Summary ────────────────────────────────────────────────────────────
print(f"""
╔══════════════════════════════════════════════════════════════╗
║         Wetsoda Call-Center Agents — READY                  ║
╠══════════════════════════════════════════════════════════════╣
║  🎯 Inbound Call Triage    id={W1_ID:<10}  ║
║  📞 Outbound Dialer        id={W2_ID:<10}  ║
║  🎧 Live Call Assistant    id={W3_ID:<10}  ║
║  📝 Post-Call Summarizer   id={W4_ID:<10}  ║
╠══════════════════════════════════════════════════════════════╣
║  ⚠️  BEFORE ACTIVATING — update in n8n Variables:           ║
║     MOGALA_API_URL  →  your mogala backend URL              ║
║     MOGALA_EMAIL    →  admin email                          ║
║     MOGALA_PASSWORD →  admin password                       ║
║     MOGALA_DOMAIN   →  your tenant domain                   ║
║                                                              ║
║  Open: http://localhost:5678                                 ║
╚══════════════════════════════════════════════════════════════╝
""")
