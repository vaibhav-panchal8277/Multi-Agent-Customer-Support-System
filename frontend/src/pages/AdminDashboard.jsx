import { useEffect, useState } from "react";
import { NavLink } from "react-router-dom";
import { listSessions, getTraces, metaFor } from "@/lib/api";
import { TraceTimeline } from "@/components/TraceTimeline";
import { AgentBadge } from "@/components/AgentBadge";
import { ArrowLeft, Activity, Zap, LayoutGrid } from "lucide-react";

export default function AdminDashboard() {
  const [sessions, setSessions] = useState([]);
  const [selected, setSelected] = useState(null);
  const [turns, setTurns] = useState([]);

  useEffect(() => {
    (async () => {
      const s = await listSessions();
      setSessions(s);
      if (s.length && !selected) {
        const firstId = s[0].id;
        setSelected(firstId);
        setTurns(await getTraces(firstId));
      }
    })();
    // eslint-disable-next-line
  }, []);

  useEffect(() => {
    if (!selected) return;
    (async () => setTurns(await getTraces(selected)))();
  }, [selected]);

  const agentDistribution = countBy(turns.map(t => t.agent).filter(Boolean));
  const totalToolCalls = turns.reduce((n, t) => n + (t.tool_calls?.length || 0), 0);
  const avgLatency = (() => {
    const lats = turns.flatMap(t => (t.trace_events || []).map(e => e.latency_ms).filter(Boolean));
    if (!lats.length) return 0;
    return Math.round(lats.reduce((a, b) => a + b, 0) / lats.length);
  })();

  return (
    <div className="grain min-h-screen relative">
      <div className="relative z-10">
        {/* Header */}
        <header className="backdrop-blur-2xl bg-zinc-950/70 border-b border-white/5 px-8 py-5 sticky top-0 z-20">
          <div className="flex items-center justify-between">
            <div className="flex items-center gap-6">
              <NavLink to="/" className="flex items-center gap-2 text-zinc-400 hover:text-white text-sm"
                       data-testid="back-to-chat">
                <ArrowLeft size={14} /> Chat
              </NavLink>
              <div>
                <div className="font-mono text-[10px] uppercase tracking-[0.18em] text-zinc-500">Admin</div>
                <div className="font-display text-2xl tracking-tight">Agent Command Center</div>
              </div>
            </div>
            <div className="flex items-center gap-6">
              <Stat icon={LayoutGrid} label="Sessions" value={sessions.length} />
              <Stat icon={Activity} label="Turns (selected)" value={turns.length} />
              <Stat icon={Zap} label="Avg LLM latency" value={`${avgLatency}ms`} />
            </div>
          </div>
        </header>

        <div className="grid grid-cols-1 md:grid-cols-4 lg:grid-cols-12 gap-6 p-6">
          {/* Sessions list */}
          <section className="col-span-12 md:col-span-4 lg:col-span-3 sharp border border-white/10 bg-[#0f0f11] p-5" style={{ borderRadius: 4 }}>
            <div className="font-mono text-[10px] uppercase tracking-[0.18em] text-zinc-500 mb-4">
              Conversations
            </div>
            <div className="space-y-1 max-h-[70vh] overflow-y-auto" data-testid="admin-sessions">
              {sessions.map(s => {
                const meta = metaFor(s.last_agent || "supervisor");
                const active = s.id === selected;
                return (
                  <button
                    key={s.id}
                    onClick={() => setSelected(s.id)}
                    data-testid={`admin-session-${s.id}`}
                    className={`w-full text-left p-3 sharp transition-colors ${active ? "bg-white/5 border-l-2" : "border-l-2 border-transparent hover:bg-white/[0.03]"}`}
                    style={{ borderRadius: 2, borderLeftColor: active ? meta.color : "transparent" }}
                  >
                    <div className="text-sm text-zinc-100 truncate">{s.title}</div>
                    <div className="flex items-center gap-2 mt-1">
                      <span className="font-mono text-[9.5px] uppercase tracking-[0.14em]" style={{ color: meta.color }}>
                        {meta.name}
                      </span>
                      <span className="font-mono text-[9.5px] text-zinc-600">·</span>
                      <span className="font-mono text-[9.5px] text-zinc-500">{s.message_count} msgs</span>
                      {s.status === "resolved" && (
                        <span className="ml-auto font-mono text-[9.5px] text-emerald-400 uppercase">resolved</span>
                      )}
                    </div>
                  </button>
                );
              })}
              {!sessions.length && <div className="text-zinc-500 text-xs">No sessions yet</div>}
            </div>
          </section>

          {/* Agent distribution */}
          <section className="col-span-12 md:col-span-4 lg:col-span-4 sharp border border-white/10 bg-[#0f0f11] p-5" style={{ borderRadius: 4 }}>
            <div className="font-mono text-[10px] uppercase tracking-[0.18em] text-zinc-500 mb-4">
              Agent activity (selected)
            </div>
            <AgentDistribution counts={agentDistribution} totalTurns={turns.length} />
            <div className="divider-h my-6" />
            <div className="font-mono text-[10px] uppercase tracking-[0.18em] text-zinc-500 mb-2">Tool calls</div>
            <div className="font-display text-4xl text-white">{totalToolCalls}</div>
            <div className="text-xs text-zinc-500 mt-1">mock APIs invoked across this conversation</div>
          </section>

          {/* Timeline of turns */}
          <section className="col-span-12 md:col-span-4 lg:col-span-5 sharp border border-white/10 bg-[#0f0f11] p-5 min-h-[400px]" style={{ borderRadius: 4 }}>
            <div className="font-mono text-[10px] uppercase tracking-[0.18em] text-zinc-500 mb-4">
              Trace timeline
            </div>
            <div className="space-y-6 max-h-[70vh] overflow-y-auto pr-2" data-testid="admin-traces">
              {turns.map((t, i) => {
                const meta = metaFor(t.agent);
                return (
                  <div key={t.id || i} className="fade-up" style={{ animationDelay: `${i * 60}ms` }}
                       data-testid={`turn-${i}`}>
                    <div className="flex items-center gap-3 mb-3">
                      <div className="font-mono text-[10px] text-zinc-600">
                        TURN {String(i + 1).padStart(2, "0")}
                      </div>
                      <AgentBadge agent={t.agent} size="sm" />
                      {t.intent && (
                        <span className="font-mono text-[10px] uppercase tracking-[0.12em]" style={{ color: meta.color }}>
                          {t.intent} · {Math.round((t.confidence || 0) * 100)}%
                        </span>
                      )}
                      <span className="ml-auto font-mono text-[10px] text-zinc-600">
                        {new Date(t.created_at).toLocaleTimeString([], { hour12: false })}
                      </span>
                    </div>
                    <div className="text-zinc-300 text-sm border-l-2 border-white/10 pl-3 mb-3">
                      <span className="text-zinc-500 mr-2 font-mono text-[10px]">USER</span>
                      {t.user_message}
                    </div>
                    <TraceTimeline events={t.trace_events || []} />
                    <div className="mt-3 text-sm text-zinc-200 border-l-2 pl-3" style={{ borderColor: meta.color }}>
                      <span className="text-zinc-500 mr-2 font-mono text-[10px]">REPLY</span>
                      {t.response}
                    </div>
                  </div>
                );
              })}
              {!turns.length && (
                <div className="text-zinc-500 text-xs">Select a conversation to inspect its trace</div>
              )}
            </div>
          </section>
        </div>
      </div>
    </div>
  );
}

function countBy(arr) {
  return arr.reduce((acc, v) => ({ ...acc, [v]: (acc[v] || 0) + 1 }), {});
}

function Stat({ icon: Icon, label, value }) {
  return (
    <div className="flex items-center gap-3">
      <div className="sharp border border-white/10 p-2" style={{ borderRadius: 2 }}>
        <Icon size={14} className="text-zinc-400" />
      </div>
      <div className="leading-tight">
        <div className="font-mono text-[9.5px] uppercase tracking-[0.14em] text-zinc-500">{label}</div>
        <div className="font-display text-lg text-white">{value}</div>
      </div>
    </div>
  );
}

function AgentDistribution({ counts, totalTurns }) {
  const agents = ["supervisor", "billing", "technical", "refunds"];
  const total = Math.max(1, totalTurns);
  return (
    <div className="space-y-3">
      {agents.map(a => {
        const m = metaFor(a);
        const c = counts[a] || 0;
        const pct = Math.round((c / total) * 100);
        return (
          <div key={a} data-testid={`agent-dist-${a}`}>
            <div className="flex items-center justify-between mb-1">
              <div className="flex items-center gap-2">
                <span className="w-2 h-2 sharp" style={{ background: m.color, borderRadius: 1 }} />
                <span className="text-sm text-zinc-200 font-display">{m.name}</span>
              </div>
              <div className="font-mono text-[11px] text-zinc-400">
                {c} · <span className="text-zinc-600">{pct}%</span>
              </div>
            </div>
            <div className="h-1 bg-white/5">
              <div className="h-full transition-[width]" style={{ width: `${pct}%`, background: m.color }} />
            </div>
          </div>
        );
      })}
    </div>
  );
}
