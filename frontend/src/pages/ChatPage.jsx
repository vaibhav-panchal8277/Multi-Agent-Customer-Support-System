import { useEffect, useRef, useState } from "react";
import { NavLink, useNavigate, useParams } from "react-router-dom";
import { Toaster, toast } from "sonner";
import { Send, Plus, Trash2, CheckCircle2, ShieldCheck } from "lucide-react";
import {
  createSession, listSessions, getSession, sendChat, resolveSession, deleteSession, metaFor,
} from "@/lib/api";
import { AgentBadge } from "@/components/AgentBadge";
import { MessageCard } from "@/components/MessageCard";

const SUGGESTIONS = [
  "I was double-charged for invoice INV-1002",
  "The OAuth login keeps redirecting in a loop",
  "Please refund invoice INV-1001, I cancelled last week",
  "Hey, can you help me?",
];

export default function ChatPage() {
  const { sid } = useParams();
  const nav = useNavigate();

  const [sessions, setSessions] = useState([]);
  const [session, setSession] = useState(null);
  const [messages, setMessages] = useState([]);
  const [input, setInput] = useState("");
  const [sending, setSending] = useState(false);
  const [activeAgent, setActiveAgent] = useState(null);
  const scrollerRef = useRef(null);

  const refreshSessions = async () => setSessions(await listSessions());

  useEffect(() => { refreshSessions(); }, []);

  useEffect(() => {
    (async () => {
      if (!sid) { setSession(null); setMessages([]); return; }
      const { session, messages } = await getSession(sid);
      setSession(session); setMessages(messages);
    })();
  }, [sid]);

  useEffect(() => {
    scrollerRef.current?.scrollTo({ top: scrollerRef.current.scrollHeight, behavior: "smooth" });
  }, [messages, sending]);

  const onNew = async () => {
    const s = await createSession({ title: "New conversation" });
    await refreshSessions();
    nav(`/c/${s.id}`);
  };

  const onSend = async (text) => {
    const msg = (text ?? input).trim();
    if (!msg) return;
    let currentSid = sid;
    if (!currentSid) {
      const s = await createSession({ title: msg.slice(0, 40) });
      currentSid = s.id;
      nav(`/c/${currentSid}`);
    }
    setInput("");
    setSending(true);
    setActiveAgent("supervisor");
    // optimistic user message
    const now = new Date().toISOString();
    setMessages(m => [...m, { role: "user", content: msg, created_at: now }]);

    try {
      const res = await sendChat(currentSid, msg);
      setActiveAgent(res.agent);
      const asst = {
        role: "assistant",
        content: res.response,
        agent: res.agent,
        intent: res.intent,
        confidence: res.confidence,
        trace_events: res.trace_events,
        tool_calls: res.tool_calls,
        created_at: new Date().toISOString(),
      };
      setMessages(m => [...m, asst]);
      refreshSessions();
    } catch (e) {
      toast.error("Failed to reach the support graph", { description: e?.message });
    } finally {
      setSending(false);
      setTimeout(() => setActiveAgent(null), 1200);
    }
  };

  const onResolve = async () => {
    if (!sid) return;
    await resolveSession(sid);
    toast.success("Ticket marked resolved");
    refreshSessions();
    const { session } = await getSession(sid);
    setSession(session);
  };

  const onDelete = async (id) => {
    await deleteSession(id);
    if (id === sid) nav("/");
    refreshSessions();
  };

  return (
    <div className="grain min-h-screen text-zinc-100 relative">
      <Toaster theme="dark" position="top-right" />
      <div className="relative z-10 grid grid-cols-12 min-h-screen">
        {/* Sidebar */}
        <aside className="col-span-12 md:col-span-3 lg:col-span-3 xl:col-span-2 border-r border-white/5 flex flex-col">
          <div className="px-5 py-5 border-b border-white/5">
            <div className="font-display text-lg tracking-tight text-white">MESH<span style={{ color: "#0ea5e9" }}>·</span>Support</div>
            <div className="font-mono text-[10px] uppercase tracking-[0.18em] text-zinc-500 mt-1">
              Multi-Agent · GPT-OSS 120B
            </div>
          </div>
          <div className="px-3 pt-3 flex items-center gap-2">
            <button
              onClick={onNew}
              data-testid="new-conversation-btn"
              className="flex-1 flex items-center justify-center gap-2 sharp bg-white text-zinc-900 px-3 py-2 text-sm font-medium hover:-translate-y-[1px] transition-transform"
              style={{ borderRadius: 2 }}
            >
              <Plus size={14} /> New chat
            </button>
            <NavLink to="/admin" data-testid="open-admin-link"
              className="sharp px-3 py-2 text-sm border border-white/10 text-zinc-300 hover:text-white hover:border-white/20 transition-colors"
              style={{ borderRadius: 2 }}>
              <ShieldCheck size={14} />
            </NavLink>
          </div>

          <div className="mt-4 flex-1 overflow-y-auto px-2 pb-4" data-testid="sessions-list">
            {sessions.length === 0 && (
              <div className="px-3 py-8 text-center text-zinc-500 text-xs">No conversations yet</div>
            )}
            {sessions.map(s => (
              <NavLink
                key={s.id}
                to={`/c/${s.id}`}
                data-testid={`session-item-${s.id}`}
                className={({ isActive }) =>
                  `group block px-3 py-3 border-l-2 mb-0.5 transition-colors ${
                    isActive ? "bg-white/5 border-white" : "border-transparent hover:bg-white/[0.03]"
                  }`
                }
              >
                <div className="flex items-center justify-between gap-2">
                  <div className="min-w-0">
                    <div className="text-sm text-zinc-100 truncate">{s.title}</div>
                    <div className="flex items-center gap-2 mt-1">
                      {s.last_agent && (
                        <span className="font-mono text-[9.5px] uppercase tracking-[0.14em]"
                              style={{ color: metaFor(s.last_agent).color }}>
                          {metaFor(s.last_agent).name}
                        </span>
                      )}
                      <span className="font-mono text-[9.5px] text-zinc-600">
                        {s.message_count} msgs
                      </span>
                      {s.status === "resolved" && (
                        <span className="font-mono text-[9.5px] text-emerald-400 uppercase">resolved</span>
                      )}
                    </div>
                  </div>
                  <button
                    onClick={(e) => { e.preventDefault(); onDelete(s.id); }}
                    data-testid={`delete-session-${s.id}`}
                    className="opacity-0 group-hover:opacity-100 text-zinc-500 hover:text-red-400 transition-opacity"
                    title="Delete"
                  >
                    <Trash2 size={13} />
                  </button>
                </div>
              </NavLink>
            ))}
          </div>
        </aside>

        {/* Main chat */}
        <main className="col-span-12 md:col-span-9 lg:col-span-9 xl:col-span-10 flex flex-col min-h-screen">
          {/* Header */}
          <header className="sticky top-0 z-20 backdrop-blur-2xl bg-zinc-950/70 border-b border-white/5 px-8 py-4 flex items-center justify-between">
            <div className="flex items-center gap-4">
              <AgentBadge agent={activeAgent || session?.last_agent || "supervisor"} size="md" testId="active-agent-badge" />
              {sending && (
                <div className="font-mono text-[11px] text-zinc-400 flex items-center gap-2">
                  <span className="w-1.5 h-1.5 sharp pulse-dot" style={{ background: metaFor(activeAgent || "supervisor").color, borderRadius: 1 }} />
                  <span className="seq-word">Analyzing intent</span>
                  <span className="seq-word">Routing</span>
                  <span className="seq-word">Composing reply</span>
                </div>
              )}
            </div>
            <div className="flex items-center gap-3">
              {session && (
                <div className="font-mono text-[10px] uppercase tracking-[0.14em] text-zinc-500">
                  {session.status === "resolved" ? "TICKET · RESOLVED" : "TICKET · OPEN"}
                </div>
              )}
              {session && session.status !== "resolved" && (
                <button
                  onClick={onResolve}
                  data-testid="resolve-btn"
                  className="sharp flex items-center gap-2 px-3 py-1.5 border border-emerald-500/30 text-emerald-400 hover:bg-emerald-500/10 text-xs"
                  style={{ borderRadius: 2 }}
                >
                  <CheckCircle2 size={13} /> Mark resolved
                </button>
              )}
            </div>
          </header>

          {/* Messages */}
          <div ref={scrollerRef} className="flex-1 overflow-y-auto px-6 md:px-12 py-10">
            <div className="max-w-3xl mx-auto space-y-8">
              {messages.length === 0 && !sending && (
                <EmptyState onPick={onSend} />
              )}
              {messages.map((m, i) => (
                <MessageCard key={i} msg={m} index={i} />
              ))}
              {sending && <SkeletonAgentCard agent={activeAgent || "supervisor"} />}
            </div>
          </div>

          {/* Composer */}
          <div className="sticky bottom-0 backdrop-blur-2xl bg-zinc-950/70 border-t border-white/5 px-6 md:px-12 py-4">
            <div className="max-w-3xl mx-auto flex items-end gap-3">
              <textarea
                data-testid="chat-input"
                value={input}
                onChange={e => setInput(e.target.value)}
                onKeyDown={e => {
                  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); onSend(); }
                }}
                rows={1}
                placeholder="Ask about a bill, a bug, or a refund…"
                className="flex-1 resize-none bg-zinc-950 border border-white/10 sharp px-4 py-3 text-zinc-100 placeholder:text-zinc-600 focus:outline-none focus:border-white/30 font-sans text-[15px]"
                style={{ borderRadius: 2, maxHeight: 200 }}
              />
              <button
                onClick={() => onSend()}
                disabled={sending || !input.trim()}
                data-testid="send-btn"
                className="sharp px-5 py-3 bg-white text-zinc-900 font-medium disabled:opacity-30 hover:-translate-y-[1px] transition-transform flex items-center gap-2"
                style={{ borderRadius: 2 }}
              >
                <Send size={14} /> Send
              </button>
            </div>
          </div>
        </main>
      </div>
    </div>
  );
}

function EmptyState({ onPick }) {
  return (
    <div className="pt-6" data-testid="empty-state">
      <div className="font-mono text-[10px] uppercase tracking-[0.18em] text-zinc-500 mb-3">
        Start a conversation
      </div>
      <h1 className="font-display text-4xl sm:text-5xl lg:text-6xl tracking-tight text-white leading-tight">
        Talk to the <span style={{ color: "#ffffff" }}>Supervisor</span>.
        <br/>
        <span className="text-zinc-500">It routes you to the right specialist.</span>
      </h1>
      <div className="mt-8 grid grid-cols-1 md:grid-cols-2 gap-3">
        {SUGGESTIONS.map((s, i) => (
          <button
            key={i}
            onClick={() => onPick(s)}
            data-testid={`suggestion-${i}`}
            className="text-left p-4 sharp border border-white/10 hover:border-white/25 hover:bg-white/[0.02] text-zinc-300 text-sm"
            style={{ borderRadius: 2 }}
          >
            <span className="font-mono text-[10px] text-zinc-600 mr-2">0{i+1}</span>
            {s}
          </button>
        ))}
      </div>
    </div>
  );
}

function SkeletonAgentCard({ agent }) {
  const meta = metaFor(agent);
  return (
    <div className="beam sharp p-6" style={{ "--beam-color": meta.color, borderRadius: 4 }}
         data-testid="thinking-skeleton">
      <div className="flex items-center gap-3">
        <AgentBadge agent={agent} size="md" />
        <div className="font-mono text-[11px] text-zinc-400 flex items-center gap-2">
          <span className="seq-word">Reasoning</span>
          <span className="seq-word">Calling tools</span>
          <span className="seq-word">Drafting</span>
        </div>
      </div>
    </div>
  );
}
