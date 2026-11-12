import axios from "axios";

const BACKEND_URL = process.env.REACT_APP_BACKEND_URL;
export const API = `${BACKEND_URL}/api`;

// Optional shared API token. NOTE: anything in REACT_APP_* is compiled into the
// public JS bundle, so this is only a casual-traffic gate, not a secret. Access
// to a conversation is protected by its per-session key (below); admin views use
// a separate ADMIN token that is typed in at runtime and never built in.
const API_TOKEN = process.env.REACT_APP_API_TOKEN;

export const api = axios.create({ baseURL: API, timeout: 60000 });

if (API_TOKEN) {
  api.defaults.headers.common["Authorization"] = `Bearer ${API_TOKEN}`;
}

// ---------- Per-session keys (this browser's conversations) ----------
// The server returns a session's key once, on creation, and stores only its
// hash. We keep the keys in localStorage so this browser can reopen its own
// conversations; without the key a conversation can't be read or changed.
const KEYS_STORAGE = "mesh.sessionKeys";

function readKeys() {
  try {
    return JSON.parse(localStorage.getItem(KEYS_STORAGE) || "{}") || {};
  } catch {
    return {};
  }
}

function writeKeys(keys) {
  try {
    localStorage.setItem(KEYS_STORAGE, JSON.stringify(keys));
  } catch {
    /* storage unavailable (private mode): keys live for this page only */
  }
}

let sessionKeys = readKeys();

function rememberKey(sid, key) {
  sessionKeys = { ...sessionKeys, [sid]: key };
  writeKeys(sessionKeys);
}

function forgetKey(sid) {
  const { [sid]: _drop, ...rest } = sessionKeys;
  sessionKeys = rest;
  writeKeys(sessionKeys);
}

function sessionHeaders(sid) {
  const key = sessionKeys[sid];
  return key ? { "X-Session-Key": key } : {};
}

export async function createSession(payload = {}) {
  const { data } = await api.post("/sessions", { title: payload.title });
  rememberKey(data.id, data.session_key);
  const { session_key: _k, ...session } = data;
  return session;
}

// Only the conversations this browser holds keys for.
export async function listMySessions() {
  const refs = Object.entries(sessionKeys).map(([id, key]) => ({ id, key }));
  if (!refs.length) return [];
  const { data } = await api.post("/sessions/mine", { sessions: refs.slice(-100) });
  return data;
}

export async function getSession(sid) {
  const { data } = await api.get(`/sessions/${sid}`, { headers: sessionHeaders(sid) });
  return data;
}

export async function sendChat(sid, message) {
  const { data } = await api.post("/chat", { session_id: sid, message },
                                  { headers: sessionHeaders(sid) });
  return data;
}

export async function verifyAccount(sid, email, pin) {
  const { data } = await api.post(`/sessions/${sid}/verify`, { email, pin },
                                  { headers: sessionHeaders(sid) });
  return data;
}

export async function confirmRefund(sid, confirmationId) {
  const { data } = await api.post(
    `/sessions/${sid}/refunds/${encodeURIComponent(confirmationId)}/confirm`, {},
    { headers: sessionHeaders(sid) });
  return data;
}

export async function resolveSession(sid) {
  const { data } = await api.post(`/sessions/${sid}/resolve`, {},
                                  { headers: sessionHeaders(sid) });
  return data;
}

export async function reopenSession(sid) {
  const { data } = await api.post(`/sessions/${sid}/reopen`, {},
                                  { headers: sessionHeaders(sid) });
  return data;
}

export async function deleteSession(sid) {
  const { data } = await api.delete(`/sessions/${sid}`, { headers: sessionHeaders(sid) });
  forgetKey(sid);
  return data;
}

// ---------- Admin (X-Admin-Token, entered at runtime) ----------
const ADMIN_STORAGE = "mesh.adminToken";

export function getAdminToken() {
  try {
    return sessionStorage.getItem(ADMIN_STORAGE) || "";
  } catch {
    return "";
  }
}

export function setAdminToken(token) {
  try {
    if (token) sessionStorage.setItem(ADMIN_STORAGE, token);
    else sessionStorage.removeItem(ADMIN_STORAGE);
  } catch {
    /* ignore */
  }
}

function adminHeaders() {
  const t = getAdminToken();
  return t ? { "X-Admin-Token": t } : {};
}

export async function listAllSessions() {
  const { data } = await api.get("/sessions", { headers: adminHeaders() });
  return data;
}

export async function getTraces(sid) {
  const { data } = await api.get(`/sessions/${sid}/traces`, { headers: adminHeaders() });
  return data.turns;
}

export const AGENT_META = {
  supervisor: { name: "Supervisor", color: "#FFFFFF",  ring: "ring-supervisor" },
  billing:    { name: "Billing",    color: "#10b981",  ring: "ring-billing" },
  technical:  { name: "Technical",  color: "#0ea5e9",  ring: "ring-technical" },
  refunds:    { name: "Refunds",    color: "#f59e0b",  ring: "ring-refunds" },
  clarify:    { name: "Supervisor", color: "#a78bfa",  ring: "ring-clarify" },
  guardrail:  { name: "Guardrail",  color: "#ef4444",  ring: "ring-guardrail" },
};

export function metaFor(agent) {
  return AGENT_META[agent] || AGENT_META.supervisor;
}
