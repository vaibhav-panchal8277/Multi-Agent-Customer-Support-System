import { act } from "react";
import { createRoot } from "react-dom/client";
import { MemoryRouter } from "react-router-dom";
import AdminDashboard from "@/pages/AdminDashboard";
import * as api from "@/lib/api";

jest.mock("@/lib/api", () => {
  let token = "";
  return {
    listAllSessions: jest.fn(), getTraces: jest.fn(),
    getAdminToken: jest.fn(() => token),
    setAdminToken: jest.fn((t) => { token = t; }),
    metaFor: () => ({ name: "Agent", color: "#ffffff" }),
  };
});

let container, root;
const $ = (id) => container.querySelector(`[data-testid="${id}"]`);
const flush = () => act(async () => { await new Promise(r => setTimeout(r, 0)); });

async function render() {
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  await act(async () => root.render(<MemoryRouter><AdminDashboard /></MemoryRouter>));
}

async function login(token) {
  await act(async () => {
    const input = $("admin-token-input");
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value").set.call(input, token);
    input.dispatchEvent(new Event("input", { bubbles: true }));
  });
  await act(async () => {
    $("admin-login").dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
  });
  await flush();
}

beforeEach(() => {
  jest.clearAllMocks();
  api.setAdminToken("");
  api.getTraces.mockResolvedValue([]);
});
afterEach(() => { act(() => root.unmount()); container.remove(); });

test("asks for the admin token and loads nothing without it", async () => {
  await render();
  expect($("admin-login")).not.toBeNull();
  expect(api.listAllSessions).not.toHaveBeenCalled();
});

test("valid token opens the dashboard", async () => {
  api.listAllSessions.mockResolvedValue([{ id: "s1", title: "First", message_count: 2 }]);
  await render();
  await login("good-token");
  expect(api.setAdminToken).toHaveBeenCalledWith("good-token");
  expect($("admin-login")).toBeNull();
  expect($("admin-session-s1")).not.toBeNull();
  expect(api.getTraces).toHaveBeenCalledWith("s1");
});

test("rejected token is cleared and the error shown", async () => {
  api.listAllSessions.mockRejectedValue({ response: { status: 401, data: { detail: "invalid or missing admin token" } } });
  await render();
  await login("bad-token");
  expect($("admin-login")).not.toBeNull();
  expect($("admin-error").textContent).toContain("invalid or missing admin token");
  expect(api.setAdminToken).toHaveBeenLastCalledWith("");
});
