import { AgentBadge } from "./AgentBadge";
import { TraceTimeline } from "./TraceTimeline";
import { metaFor } from "@/lib/api";

export function MessageCard({ msg, index = 0 }) {
  const isUser = msg.role === "user";
  if (isUser) {
    return (
      <div className="fade-up" style={{ animationDelay: `${index * 40}ms` }}
           data-testid={`msg-user-${index}`}>
        <div className="flex items-baseline gap-3 mb-1">
          <span className="font-mono text-[10px] uppercase tracking-[0.14em] text-zinc-500">Customer</span>
          <span className="font-mono text-[10px] text-zinc-600">
            {new Date(msg.created_at).toLocaleTimeString([], { hour12: false })}
          </span>
        </div>
        <div className="text-zinc-100 text-[15px] leading-relaxed pl-0 border-l-2 border-zinc-700 pl-4 py-1">
          {msg.content}
        </div>
      </div>
    );
  }

  const agent = msg.agent || "supervisor";
  const meta = metaFor(agent);
  return (
    <div className="fade-up" style={{ animationDelay: `${index * 40}ms` }}
         data-testid={`msg-agent-${index}`}>
      <div
        className="sharp p-6 relative"
        style={{
          background: "#0f0f11",
          border: `1px solid rgba(255,255,255,0.08)`,
          borderLeft: `2px solid ${meta.color}`,
          borderRadius: 4,
        }}
      >
        <div className="flex items-start justify-between gap-4 mb-4">
          <AgentBadge agent={agent} testId={`msg-agent-badge-${index}`} />
          <div className="text-right">
            {msg.intent && (
              <div className="font-mono text-[10px] uppercase tracking-[0.14em] text-zinc-500">
                intent · {msg.intent}
                {msg.confidence != null && (
                  <span className="ml-2" style={{ color: meta.color }}>
                    {Math.round(msg.confidence * 100)}%
                  </span>
                )}
              </div>
            )}
            <div className="font-mono text-[10px] text-zinc-600">
              {new Date(msg.created_at).toLocaleTimeString([], { hour12: false })}
            </div>
          </div>
        </div>

        {msg.trace_events?.length > 0 && (
          <div className="mb-4">
            <div className="font-mono text-[10px] uppercase tracking-[0.14em] text-zinc-500 mb-2">
              Activity
            </div>
            <TraceTimeline events={msg.trace_events} compact />
          </div>
        )}

        <div className="text-[15px] leading-relaxed text-zinc-100 whitespace-pre-wrap">
          {msg.content}
        </div>
      </div>
    </div>
  );
}
