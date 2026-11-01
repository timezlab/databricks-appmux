import type { PlatformView } from "@/lib/api";
import { formatCount } from "@/lib/format";
import { memoryView, totalBudgetMb } from "@/lib/platform/memory";
import { CapacityStrip } from "@/components/capacity-strip";
import { Notice } from "@/components/notice";
import { Section } from "@/components/section";

/** The slot's memory against the container limit (`GET /platform`, `memory`). */
export function MemorySection({ view }: { view: PlatformView }) {
  const memory = memoryView(view.memory);
  const registeredMb = totalBudgetMb(view.apps);
  return (
    <Section title="Memory">
      <div className="flex flex-col gap-2">
        <p>
          <strong>{memory.headline}</strong>
        </p>
        <CapacityStrip
          label={memory.summary}
          segments={memory.segments}
          total={memory.limitMb}
          figure={`${memory.percentText} committed`}
          level={memory.level}
          levelText={memory.levelText}
        />
        {memory.note === null ? null : (
          <Notice tone="danger" title={memory.note} />
        )}
        <p className="text-muted-foreground">
          The container allows {formatCount(memory.limitMb)} MB. appmux keeps{" "}
          {formatCount(memory.reserveMb)} MB for itself (memory_reserve_mb), so
          the apps share {formatCount(memory.availableMb)} MB. Committed is the
          budget of each running app plus its per-child overhead and any update
          overlap. The {formatCount(view.apps.length)} registered apps hold{" "}
          {formatCount(registeredMb)} MB of budgets in total, running or not.
        </p>
      </div>
    </Section>
  );
}
