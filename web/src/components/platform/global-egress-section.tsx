import { putPlatformEgress } from "@/lib/api";
import type { PlatformView } from "@/lib/api";
import { Section } from "@/components/section";
import { EgressEditor } from "./egress-editor";

/** The rules every app is checked against after its own (design 6.3). */
export function GlobalEgressSection({ view }: { view: PlatformView }) {
  return (
    <Section title="Global egress rules">
      <EgressEditor
        name="The global egress rules"
        scope="global"
        source={{ rules: view.egress_global, version: null }}
        save={async (rules) => {
          const stored = await putPlatformEgress({ rules });
          return { rules: stored.items, version: null };
        }}
        savedTitle="Saved the global egress rules"
      />
    </Section>
  );
}
