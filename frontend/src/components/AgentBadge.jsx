import { metaFor } from "@/lib/api";
import { Bot, Wrench, Receipt, Undo2, Compass, ShieldAlert } from "lucide-react";

const ICONS = {
  supervisor: Compass,
  billing: Receipt,
  technical: Wrench,
  refunds: Undo2,
  clarify: Compass,
  guardrail: ShieldAlert,
};

export function AgentBadge({ agent = "supervisor", size = "md", subtle = false, testId }) {
  const meta = metaFor(agent);
  const Icon = ICONS[agent] || Bot;
  const px = size === "sm" ? 24 : size === "lg" ? 44 : 32;
  const iconPx = size === "sm" ? 12 : size === "lg" ? 22 : 16;
  return (
    <div className="flex items-center gap-2" data-testid={testId || `agent-badge-${agent}`}>
      <div
        className={`flex items-center justify-center sharp ${subtle ? "" : meta.ring}`}
        style={{
          width: px, height: px,
          background: subtle ? "rgba(255,255,255,0.04)" : "#0f0f11",
          borderRadius: 4,
          color: meta.color,
        }}
      >
        <Icon size={iconPx} strokeWidth={1.75} />
      </div>
      {size !== "sm" && (
        <div className="leading-tight">
          <div className="text-xs uppercase tracking-[0.14em] text-zinc-500 font-mono">Agent</div>
          <div className="font-display text-sm text-zinc-100" style={{ color: meta.color === "#FFFFFF" ? "#FAFAFA" : meta.color }}>
            {meta.name}
          </div>
        </div>
      )}
    </div>
  );
}
