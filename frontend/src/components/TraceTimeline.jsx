import { metaFor } from "@/lib/api";
import { ArrowRight, Wrench, Sparkles, MessageCircleQuestion, CircleDot, Cog, ShieldCheck, ShieldAlert } from "lucide-react";

function TraceIcon({ type, blocked }) {
  const map = {
    classify: Sparkles,
    handoff: ArrowRight,
    tool_call: Cog,
    reason: CircleDot,
    final: CircleDot,
    clarify: MessageCircleQuestion,
    guardrail: blocked ? ShieldAlert : ShieldCheck,
  };
  const Icon = map[type] || CircleDot;
  return <Icon size={12} strokeWidth={2} />;
}

export function TraceTimeline({ events = [], compact = false }) {
  if (!events?.length) return null;
  return (
    <div className={`border-l border-white/10 pl-4 ${compact ? "space-y-1.5" : "space-y-2"}`}
         data-testid="trace-timeline">
      {events.map((ev, i) => {
        const meta = metaFor(ev.agent);
        return (
          <div key={i} className="relative fade-up" style={{ animationDelay: `${i * 40}ms` }}
               data-testid={`trace-event-${i}`}>
            <div className="absolute -left-[21px] top-1.5 w-2.5 h-2.5 sharp"
                 style={{ background: ev.color || meta.color }} />
            <div className="flex items-start gap-2 font-mono text-[11px] text-zinc-300">
              <span className="text-zinc-500 shrink-0">
                {new Date((ev.ts || 0) * 1000).toLocaleTimeString([], { hour12: false })}
              </span>
              <span className="shrink-0" style={{ color: ev.color || meta.color }}>
                <TraceIcon type={ev.type} blocked={ev.blocked} />
              </span>
              <span className="uppercase tracking-[0.08em] text-zinc-500 shrink-0">
                {ev.agent}
              </span>
              <span className="text-zinc-200 truncate">{ev.label}</span>
              {ev.latency_ms != null && (
                <span className="ml-auto text-zinc-500 shrink-0">{ev.latency_ms}ms</span>
              )}
            </div>
            {ev.type === "guardrail" && ev.blocked && ev.reason && (
              <div className="mt-1 ml-6 font-mono text-[10.5px] text-red-300/80 bg-red-950/30 border border-red-500/20 sharp p-2 max-w-full">
                {ev.reason}
              </div>
            )}
            {ev.type === "tool_call" && ev.result && (
              <pre className="mt-1 ml-6 font-mono text-[10.5px] text-zinc-400 bg-zinc-950/60 border border-white/5 sharp p-2 overflow-x-auto max-w-full">
{JSON.stringify(ev.result, null, 2)}
              </pre>
            )}
          </div>
        );
      })}
    </div>
  );
}
