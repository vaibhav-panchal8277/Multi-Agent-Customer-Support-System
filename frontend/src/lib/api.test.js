// api.js: per-session keys and the admin token are sent only where they belong.
jest.mock("axios", () => {
  const instance = {
    get: jest.fn(), post: jest.fn(), delete: jest.fn(),
    defaults: { headers: { common: {} } },
  };
  return { __esModule: true, default: { create: () => instance }, instance };
});

const { instance: http } = require("axios");

function loadApi() {
  let mod;
  jest.isolateModules(() => { mod = require("./api"); });
  return mod;
}

beforeEach(() => {
  localStorage.clear();
  sessionStorage.clear();
  http.get.mockReset(); http.post.mockReset(); http.delete.mockReset();
});

test("createSession stores the key and never returns it", async () => {
  const api = loadApi();
  http.post.mockResolvedValueOnce({ data: { id: "s1", title: "t", session_key: "k1" } });
  const s = await api.createSession({ title: "t", customer_email: "evil@x.com" });
  expect(s).toEqual({ id: "s1", title: "t" });
  expect(http.post).toHaveBeenCalledWith("/sessions", { title: "t" }); // no self-asserted email
  expect(JSON.parse(localStorage.getItem("mesh.sessionKeys"))).toEqual({ s1: "k1" });
});

test("session calls send that session's key, and only that one", async () => {
  localStorage.setItem("mesh.sessionKeys", JSON.stringify({ s1: "k1", s2: "k2" }));
  const api = loadApi();
  http.get.mockResolvedValue({ data: {} });
  http.post.mockResolvedValue({ data: {} });
  await api.getSession("s2");
  expect(http.get).toHaveBeenCalledWith("/sessions/s2", { headers: { "X-Session-Key": "k2" } });
  await api.sendChat("s1", "hi");
  expect(http.post).toHaveBeenLastCalledWith("/chat", { session_id: "s1", message: "hi" },
                                             { headers: { "X-Session-Key": "k1" } });
  await api.confirmRefund("s1", "RFC-1/x");
  expect(http.post).toHaveBeenLastCalledWith("/sessions/s1/refunds/RFC-1%2Fx/confirm", {},
                                             { headers: { "X-Session-Key": "k1" } });
  await api.getSession("unknown");
  expect(http.get).toHaveBeenLastCalledWith("/sessions/unknown", { headers: {} });
});

test("listMySessions posts only locally held keys", async () => {
  const api0 = loadApi();
  expect(await api0.listMySessions()).toEqual([]);
  expect(http.post).not.toHaveBeenCalled();
  localStorage.setItem("mesh.sessionKeys", JSON.stringify({ s1: "k1" }));
  const api = loadApi();
  http.post.mockResolvedValueOnce({ data: [{ id: "s1" }] });
  expect(await api.listMySessions()).toEqual([{ id: "s1" }]);
  expect(http.post).toHaveBeenCalledWith("/sessions/mine", { sessions: [{ id: "s1", key: "k1" }] });
});

test("deleteSession forgets the key", async () => {
  localStorage.setItem("mesh.sessionKeys", JSON.stringify({ s1: "k1", s2: "k2" }));
  const api = loadApi();
  http.delete.mockResolvedValueOnce({ data: { ok: true } });
  await api.deleteSession("s1");
  expect(JSON.parse(localStorage.getItem("mesh.sessionKeys"))).toEqual({ s2: "k2" });
});

test("admin token goes only to admin calls and lives in sessionStorage", async () => {
  const api = loadApi();
  api.setAdminToken("adm");
  expect(sessionStorage.getItem("mesh.adminToken")).toBe("adm");
  http.get.mockResolvedValue({ data: { turns: [] } });
  await api.listAllSessions();
  expect(http.get).toHaveBeenLastCalledWith("/sessions", { headers: { "X-Admin-Token": "adm" } });
  await api.getTraces("s1");
  expect(http.get).toHaveBeenLastCalledWith("/sessions/s1/traces",
                                            { headers: { "X-Admin-Token": "adm" } });
  await api.getSession("s1");
  expect(http.get.mock.calls.at(-1)[1].headers).not.toHaveProperty("X-Admin-Token");
  api.setAdminToken("");
  expect(sessionStorage.getItem("mesh.adminToken")).toBeNull();
});

test("corrupt storage doesn't crash and is treated as empty", async () => {
  localStorage.setItem("mesh.sessionKeys", "{not json");
  const api = loadApi();
  http.get.mockResolvedValue({ data: {} });
  await api.getSession("s1");
  expect(http.get).toHaveBeenLastCalledWith("/sessions/s1", { headers: {} });
  expect(await api.listMySessions()).toEqual([]);
});
