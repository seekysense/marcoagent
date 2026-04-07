const GaleneBaseUrl = 'https://api-gb10.elettra.ai';

// Configurazione:
// 1) n8n Variables (consigliato): $vars.GALENE_APIKEY / $vars.GALENE_MODEL
// 2) env instance n8n:            $env.GALENE_APIKEY / $env.GALENE_MODEL
// 3) fallback locale:             process.env.APIKEY / process.env.MODEL
const GaleneToken =
  ((typeof $vars !== 'undefined' && ($vars.GALENE_APIKEY || $vars.APIKEY)) || '')
  || ((typeof $env !== 'undefined' && ($env.GALENE_APIKEY || $env.APIKEY)) || '')
  || ((typeof process !== 'undefined' && process.env && (process.env.GALENE_APIKEY || process.env.APIKEY)) || '')
  || 'INSERISCI_QUI_IL_TOKEN_JWT';

const GaleneModel =
  ((typeof $vars !== 'undefined' && ($vars.GALENE_MODEL || $vars.MODEL)) || '')
  || ((typeof $env !== 'undefined' && ($env.GALENE_MODEL || $env.MODEL)) || '')
  || ((typeof process !== 'undefined' && process.env && (process.env.GALENE_MODEL || process.env.MODEL)) || '')
  || 'RedHatAI/Mistral-Small-3.1-24B';

if (!GaleneToken || GaleneToken.includes('INSERISCI_QUI')) {
  throw new Error('Configura GaleneToken dentro agent-selector.js prima di eseguire il nodo.');
}

async function galeneRequest(path, { method = 'GET', body } = {}) {
  const url = `${GaleneBaseUrl}${path}`;
  const headers = {
    Authorization: `Bearer ${GaleneToken}`,
  };

  if (body !== undefined) {
    headers['Content-Type'] = 'application/json';
  }

  let statusCode;
  let rawText;

  if (typeof fetch === 'function') {
    const response = await fetch(url, {
      method,
      headers,
      body: body !== undefined ? JSON.stringify(body) : undefined,
    });
    statusCode = response.status;
    rawText = await response.text();
  } else if (typeof require === 'function') {
    let axios = null;
    try {
      axios = require('axios');
    } catch (_) {
      axios = null;
    }

    if (!axios) {
      throw new Error(
        'N8N Code node: fetch non disponibile e modulo axios non trovato. '
        + 'In produzione usa HTTP Request node (consigliato), oppure abilita NODE_FUNCTION_ALLOW_EXTERNAL=axios '
        + 'sul Task Runner e installa axios nel container del runner.',
      );
    }

    const response = await axios({
      url,
      method,
      headers,
      data: body !== undefined ? body : undefined,
      validateStatus: () => true,
    });

    statusCode = response.status;
    rawText = typeof response.data === 'string' ? response.data : JSON.stringify(response.data);
  } else {
    throw new Error(
      'N8N Code node: fetch/require non disponibili. Usa HTTP Request node per chiamate API.',
    );
  }

  let payload = {};

  if (rawText) {
    try {
      payload = JSON.parse(rawText);
    } catch {
      throw new Error(`Risposta non JSON da ${method} ${path}: ${rawText.slice(0, 400)}`);
    }
  }

  if (statusCode < 200 || statusCode >= 300) {
    const apiMessage = payload && payload.message ? payload.message : rawText;
    throw new Error(`${method} ${path} fallita (${statusCode}): ${apiMessage || 'errore sconosciuto'}`);
  }

  return payload;
}

function extractUserRequest(item) {
  const data = item && item.json ? item.json : item;

  if (typeof data === 'string' && data.trim()) {
    return data.trim();
  }

  const candidates = [
    data && data.query && data.query.user_request,
    data && data.query && data.query.request,
    data && data.query && data.query.query,
    data && data.query && data.query.prompt,
    data && data.query && data.query.text,
    data && data.query && data.query.message,
    data && data.query && data.query.input,
    data && data.body && data.body.user_request,
    data && data.body && data.body.request,
    data && data.body && data.body.query,
    data && data.body && data.body.prompt,
    data && data.body && data.body.text,
    data && data.body && data.body.message,
    data && data.body && data.body.input,
    data && data.user_request,
    data && data.request,
    data && data.query,
    data && data.prompt,
    data && data.text,
    data && data.message,
    data && data.input,
  ];

  for (const value of candidates) {
    if (typeof value === 'string' && value.trim()) {
      return value.trim();
    }
  }

  throw new Error('Input testuale mancante. Usa uno dei campi: user_request, request, query, prompt, text, message, input.');
}

function tryParseJson(text) {
  if (!text || typeof text !== 'string') {
    return null;
  }

  const trimmed = text.trim();

  try {
    return JSON.parse(trimmed);
  } catch {}

  const fenced = trimmed.match(/```(?:json)?\s*([\s\S]*?)\s*```/i);
  if (fenced && fenced[1]) {
    try {
      return JSON.parse(fenced[1]);
    } catch {}
  }

  const objectMatch = trimmed.match(/\{[\s\S]*\}/);
  if (objectMatch && objectMatch[0]) {
    try {
      return JSON.parse(objectMatch[0]);
    } catch {}
  }

  return null;
}

function normalizeDecision(modelContent, agentsById) {
  const parsed = tryParseJson(modelContent);
  if (!parsed || typeof parsed !== 'object') {
    return {
      tool_type: 'standard',
      agent_id: null,
      agent_title: null,
    };
  }

  const toolType = typeof parsed.tool_type === 'string' ? parsed.tool_type.toLowerCase() : '';
  const candidateAgentId = typeof parsed.agent_id === 'string' ? parsed.agent_id.trim() : '';

  if (toolType === 'agent' && candidateAgentId && agentsById.has(candidateAgentId)) {
    const agent = agentsById.get(candidateAgentId);
    return {
      tool_type: 'agent',
      agent_id: agent.agent_id,
      agent_title: agent.agent_title,
    };
  }

  return {
    tool_type: 'standard',
    agent_id: null,
    agent_title: null,
  };
}

async function chooseToolWithLlm(userRequest, readyAgents) {
  if (!readyAgents.length) {
    return {
      tool_type: 'standard',
      agent_id: null,
      agent_title: null,
    };
  }

  const agentsById = new Map(readyAgents.map((a) => [a.agent_id, a]));

  const messages = [
    {
      role: 'system',
      content:
        'Sei un selettore di agenti. Confronta richiesta utente e descrizioni agenti. '
        + 'Rispondi SOLO con JSON valido nel formato: '
        + '{"tool_type":"agent|standard","agent_id":"<id oppure null>"}. '
        + 'Scegli "agent" solo se un agente e\' davvero utile e pertinente. '
        + 'Se nessun agente e\' adatto, usa "standard" e agent_id null.',
    },
    {
      role: 'user',
      content:
        `Richiesta utente:\n${userRequest}\n\n` +
        `Agenti disponibili (usa solo questi agent_id):\n${JSON.stringify(readyAgents)}`,
    },
  ];

  const completion = await galeneRequest('/v1/chat/completions', {
    method: 'POST',
    body: {
      model: GaleneModel,
      stream: false,
      temperature: 0,
      messages,
    },
  });

  const modelContent =
    completion
    && Array.isArray(completion.choices)
    && completion.choices[0]
    && completion.choices[0].message
    && typeof completion.choices[0].message.content === 'string'
      ? completion.choices[0].message.content
      : '';

  return normalizeDecision(modelContent, agentsById);
}

async function initConversation() {
  const initResponse = await galeneRequest('/conversation/init', { method: 'GET' });
  const conversationId = initResponse && initResponse.result ? initResponse.result.conversation_id : null;

  if (!conversationId) {
    throw new Error('conversation_id non trovato nella risposta di /conversation/init');
  }

  return conversationId;
}

async function assignAgentToConversation(conversationId, agentId) {
  await galeneRequest(`/conversation/${encodeURIComponent(conversationId)}`, {
    method: 'PATCH',
    body: {
      agent_id: agentId,
    },
  });
}

const inputItems = $input.all();
if (!inputItems.length) {
  throw new Error('Nessun item in input.');
}

const agentsResponse = await galeneRequest('/agents', { method: 'GET' });
const agentsList = Array.isArray(agentsResponse && agentsResponse.result) ? agentsResponse.result : [];

const readyAgents = agentsList
  .filter((agent) => agent && agent.ready === true && agent.agent_id)
  .map((agent) => ({
    agent_id: String(agent.agent_id),
    agent_title: typeof agent.name === 'string' ? agent.name : '',
    description: typeof agent.description === 'string' ? agent.description : '',
  }));

const outputItems = [];

for (const item of inputItems) {
  const userRequest = extractUserRequest(item);
  const conversationId = await initConversation();

  const decision = await chooseToolWithLlm(userRequest, readyAgents);

  if (decision.tool_type === 'agent' && decision.agent_id) {
    await assignAgentToConversation(conversationId, decision.agent_id);
  }

  outputItems.push({
    json: {
      tool_type: decision.tool_type,
      conversation_id: conversationId,
      agent_id: decision.agent_id,
      agent_title: decision.agent_title,
    },
  });
}

return outputItems;
