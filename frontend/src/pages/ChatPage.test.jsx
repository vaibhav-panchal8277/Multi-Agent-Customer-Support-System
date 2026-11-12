import { act } from "react";
import { createRoot } from "react-dom/client";
import { MemoryRouter, Route, Routes, useNavigate } from "react-router-dom";
import ChatPage from "@/pages/ChatPage";
import * as api from "@/lib/api";

jest.mock("@/lib/api", () => ({
  listMySessions: jest.fn(), createSession: jest.fn(), getSession: jest.fn(),
  sendChat: jest.fn(), resolveSession: jest.fn(), deleteSession: jest.fn(),
  verifyAccount: jest.fn(), confirmRefund: jest.fn(), reopenSession: jest.fn(),
  metaFor: () => ({ name: "Agent", color: "#ffffff" }),
}));

const SESSION = { id: "s1", title: "t", status: "open", customer_email: null,
                  message_count: 0, created_at: "", updated_at: "" };

function deferred() {
  let resolve, reject;
  const promise = new Promise((res, rej) => { resolve = res; reject = rej; });
  return { promise, resolve, reject };
}

let container, root, navigate;
function Grab() { navigate = useNavigate(); return null; }

async function render(path) {
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  await act(async () => {
    root.render(
      <MemoryRouter initialEntries={[path]}>
        <Grab />
        <Routes>
          <Route path="/" element={<ChatPage />} />
          <Route path="/c/:sid" element={<ChatPage />} />
        </Routes>
      </MemoryRouter>,
    );
  });
}

const $ = (id) => container.querySelector(`[data-testid="${id}"]`);
const flush = () => act(async () => { await new Promise(r => setTimeout(r, 0)); });

function setValue(el, value) {
  const proto = el.tagName === "TEXTAREA" ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
  Object.getOwnPropertyDescriptor(proto, "value").set.call(el, value);
  el.dispatchEvent(new Event("input", { bubbles: true }));
}

async function type(text) { await act(async () => setValue($("chat-input"), text)); }
async function click(el) { await act(async () => { el.click(); }); }
async function enter() {
  await act(async () => {
    $("chat-input").dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
  });
}

beforeEach(() => {
  jest.clearAllMocks();
  api.listMySessions.mockResolvedValue([]);
  api.getSession.mockResolvedValue({ session: SESSION, messages: [], refunds: {} });
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
});

test("first message in a new conversation is not wiped by the session load", async () => {
  api.createSession.mockResolvedValue({ ...SESSION });
  const reply = deferred();
  api.sendChat.mockReturnValue(reply.promise);
  await render("/");
  await type("hello there");
  await click($("send-btn"));
  await flush();
  expect(container.textContent).toContain("hello there");
  await act(async () => reply.resolve({ agent: "billing", response: "Answer!", tool_calls: [],
                                         trace_events: [] }));
  await flush();
  expect(container.textContent).toContain("hello there");
  expect(container.textContent).toContain("Answer!");
  expect(api.getSession).not.toHaveBeenCalledWith("s1");  // no refetch that could wipe it
});

test("Enter while a reply is pending does not send twice", async () => {
  const reply = deferred();
  api.sendChat.mockReturnValue(reply.promise);
  await render("/c/s1");
  await type("once");
  await enter();
  await type("once");
  await enter();
  expect(api.sendChat).toHaveBeenCalledTimes(1);
  await act(async () => reply.resolve({ agent: "billing", response: "ok" }));
});

test("rejected message is removed and given back to the composer", async () => {
  api.sendChat.mockRejectedValue({ response: { status: 429, data: { detail: "too many messages" } } });
  await render("/c/s1");
  await type("spam");
  await click($("send-btn"));
  await flush();
  expect($("msg-user-0")).toBeNull();
  expect($("chat-input").value).toBe("spam");
});

test("resolved ticket: composer disabled until reopened", async () => {
  api.getSession.mockResolvedValue({ session: { ...SESSION, status: "resolved" },
                                     messages: [], refunds: {} });
  api.reopenSession.mockResolvedValue({ ...SESSION, status: "open" });
  await render("/c/s1");
  expect($("chat-input").disabled).toBe(true);
  expect($("resolve-btn")).toBeNull();
  await click($("reopen-btn"));
  expect(api.reopenSession).toHaveBeenCalledWith("s1");
  expect($("chat-input").disabled).toBe(false);
  expect($("reopen-btn")).toBeNull();
});

test("a slow load for a previous conversation never overwrites the current one", async () => {
  const slow = deferred();
  api.getSession.mockImplementation((sid) => sid === "s1" ? slow.promise : Promise.resolve({
    session: { ...SESSION, id: "s2" }, refunds: {},
    messages: [{ role: "user", content: "from s2", created_at: "2026-01-01T00:00:00Z" }],
  }));
  await render("/c/s1");
  await act(async () => navigate("/c/s2"));
  await flush();
  await act(async () => slow.resolve({
    session: SESSION, refunds: {},
    messages: [{ role: "user", content: "from s1", created_at: "2026-01-01T00:00:00Z" }],
  }));
  await flush();
  expect(container.textContent).toContain("from s2");
  expect(container.textContent).not.toContain("from s1");
});

test("refund executes only from the customer's confirm click", async () => {
  api.getSession.mockResolvedValue({
    session: { ...SESSION, customer_email: "acme@example.com" }, refunds: {},
    messages: [{
      role: "assistant", agent: "refunds", content: "Ready for your confirmation.",
      created_at: "2026-01-01T00:00:00Z",
      tool_calls: [{ tool: "request_refund", result: {
        status: "pending_confirmation", confirmation_id: "RFC-1", amount: 79, invoice_id: "INV-1001" } }],
    }],
  });
  api.confirmRefund.mockResolvedValue({
    ok: true, status: "confirmed",
    message: { role: "assistant", agent: "refunds", content: "Refund REF-9 processed.",
               created_at: "2026-01-01T00:00:01Z" },
  });
  await render("/c/s1");
  const btn = $("confirm-refund-RFC-1");
  expect(btn.textContent).toContain("$79.00 for INV-1001");
  expect(api.confirmRefund).not.toHaveBeenCalled();
  await click(btn);
  await flush();
  expect(api.confirmRefund).toHaveBeenCalledWith("s1", "RFC-1");
  expect($("confirm-refund-RFC-1")).toBeNull();
  expect(container.textContent).toContain("Refund REF-9 processed.");
  expect(container.textContent).toContain("confirmed");
});

test("verify account binds the identity", async () => {
  api.verifyAccount.mockResolvedValue({ ...SESSION, customer_email: "acme@example.com" });
  await render("/c/s1");
  await click($("verify-account-btn"));
  await act(async () => {
    setValue($("verify-email"), "acme@example.com");
    setValue($("verify-pin"), "4821");
  });
  await act(async () => {
    $("verify-form").dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
  });
  await flush();
  expect(api.verifyAccount).toHaveBeenCalledWith("s1", "acme@example.com", "4821");
  expect($("verified-badge").textContent).toContain("acme@example.com");
  expect($("verify-form")).toBeNull();
});

test("verify form opens focused, in a fixed-height column that can't scroll it away", async () => {
  // Regression: the whole page used to scroll, so the form opened above the
  // viewport when the chat was long and the button looked broken.
  await render("/c/s1");
  await click($("verify-account-btn"));
  expect($("verify-form")).not.toBeNull();
  expect(document.activeElement).toBe($("verify-email"));
  const column = $("chat-column").className.split(/\s+/);
  expect(column).toContain("h-screen");
  expect(column).not.toContain("min-h-screen");
  expect($("message-scroller").className.split(/\s+/)).toEqual(
    expect.arrayContaining(["flex-1", "min-h-0", "overflow-y-auto"]));
  // the form sits between the header and the scroller, outside the scrolled area
  expect($("message-scroller").contains($("verify-form"))).toBe(false);
  await click($("verify-account-btn"));
  expect($("verify-form")).toBeNull();  // toggles closed
});

test("wrong PIN shows the server's error", async () => {
  api.verifyAccount.mockRejectedValue({ response: { status: 401, data: { detail: "email or PIN not recognised" } } });
  await render("/c/s1");
  await click($("verify-account-btn"));
  await act(async () => {
    setValue($("verify-email"), "acme@example.com");
    setValue($("verify-pin"), "0000");
  });
  await act(async () => {
    $("verify-form").dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
  });
  await flush();
  expect($("verify-error").textContent).toBe("email or PIN not recognised");
});

test("another browser's conversation shows an access error", async () => {
  api.getSession.mockRejectedValue({ response: { status: 403, data: { detail: "nope" } } });
  await render("/c/s1");
  await flush();
  expect($("access-error").textContent).toContain("another browser");
});
