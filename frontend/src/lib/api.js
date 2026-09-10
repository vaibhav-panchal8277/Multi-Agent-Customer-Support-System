import axios from "axios";

const BACKEND_URL = process.env.REACT_APP_BACKEND_URL;
export const API = `${BACKEND_URL}/api`;

// Optional shared API token (issue C3). When REACT_APP_API_TOKEN is set, every
// request carries `Authorization: Bearer <token>`; when unset, requests go out
// unauthenticated so local dev against a tokenless backend still works.
const API_TOKEN = process.env.REACT_APP_API_TOKEN;

export const api = axios.create({ baseURL: API, timeout: 60000 });

if (API_TOKEN) {
  api.defaults.headers.common["Authorization"] = `Bearer ${API_TOKEN}`;
}

export async function listAgents() {
  const { data } = await api.get("/agents");
  return data.agents;
}

export async function createSession(payload = {}) {
  const { data } = await api.post("/sessions", payload);
  return data;
}

export async function listSessions() {
  const { data } = await api.get("/sessions");
  return data;
}

export async function getSession(sid) {
  const { data } = await api.get(`/sessions/${sid}`);
  return data;
}

export async function getTraces(sid) {
  const { data } = await api.get(`/sessions/${sid}/traces`);
  return data.turns;
}

export async function sendChat(sid, message) {
  const { data } = await api.post("/chat", { session_id: sid, message });
  return data;
}

export async function resolveSession(sid) {
  const { data } = await api.post(`/sessions/${sid}/resolve`);
  return data;
}

export async function deleteSession(sid) {
  const { data } = await api.delete(`/sessions/${sid}`);
  return data;
}

export const AGENT_META = {
  supervisor: { name: "Supervisor", color: "#FFFFFF",  ring: "ring-supervisor", cssVar: "--supervisor" },
  billing:    { name: "Billing",    color: "#10b981",  ring: "ring-billing",    cssVar: "--billing" },
  technical:  { name: "Technical",  color: "#0ea5e9",  ring: "ring-technical",  cssVar: "--technical" },
  refunds:    { name: "Refunds",    color: "#f59e0b",  ring: "ring-refunds",    cssVar: "--refunds" },
  clarify:    { name: "Supervisor", color: "#a78bfa",  ring: "ring-clarify",    cssVar: "--clarify" },
  guardrail:  { name: "Guardrail",  color: "#ef4444",  ring: "ring-guardrail",  cssVar: "--guardrail" },
};

export function metaFor(agent) {
  return AGENT_META[agent] || AGENT_META.supervisor;
}
