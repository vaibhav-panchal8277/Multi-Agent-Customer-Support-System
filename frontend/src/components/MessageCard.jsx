import { AgentBadge } from "./AgentBadge";
import { TraceTimeline } from "./TraceTimeline";
import { metaFor } from "@/lib/api";

export function MessageCard({ msg, index = 0, refunds = {}, onConfirmRefund }) {
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

        {onConfirmRefund && (
          <PendingRefunds toolCalls={msg.tool_calls} refunds={refunds} onConfirm={onConfirmRefund} />
        )}
      </div>
    </div>
  );
}

// A refund the Refunds agent proposed only executes when the customer clicks
// "Confirm refund" here (the server never lets the LLM move money itself).
function PendingRefunds({ toolCalls, refunds, onConfirm }) {
  const proposals = (toolCalls || [])
    .filter(tc => tc.tool === "request_refund")
    .map(tc => tc.result)
    .filter(r => r && r.status === "pending_confirmation" && r.confirmation_id);
  if (!proposals.length) return null;

  return (
    <div className="mt-4 space-y-2">
      {proposals.map(r => {
        const status = refunds[r.confirmation_id] || "pending";
        const amount = Number(r.amount);
        const label = (Number.isFinite(amount) ? "$" + amount.toFixed(2) : "refund") + " for " + r.invoice_id;
        if (status === "pending" || status === "processing") {
          return (
            <button
              key={r.confirmation_id}
              onClick={() => onConfirm(r.confirmation_id)}
              disabled={status === "processing"}
              data-testid={"confirm-refund-" + r.confirmation_id}
              className="sharp px-4 py-2 border border-amber-500/40 text-amber-400 hover:bg-amber-500/10 text-sm disabled:opacity-40"
              style={{ borderRadius: 2 }}
            >
              {status === "processing" ? "Processing…" : "Confirm refund · " + label}
            </button>
          );
        }
        return (
          <div key={r.confirmation_id} className="font-mono text-[11px] uppercase tracking-[0.12em] text-zinc-500">
            Refund {label} · {status}
          </div>
        );
      })}
    </div>
  );
}
