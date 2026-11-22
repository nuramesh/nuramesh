import { Check, ShieldAlert, ShieldQuestion } from "lucide-react";

import { Chip } from "@/components/ui/chip";

import type { ChainSummary } from "./anchor-words";

export function ChainChip({ summary, className }: { summary: ChainSummary; className?: string }) {
  const Icon = summary.verified ? Check : summary.tone === "danger" ? ShieldAlert : ShieldQuestion;
  return (
    <Chip tone={summary.tone} className={className}>
      <Icon aria-hidden="true" className="size-4" />
      {summary.headline}
    </Chip>
  );
}
