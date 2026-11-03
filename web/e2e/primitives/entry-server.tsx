// What build.ts prerenders, as the static export would: the primitives page,
// and the negative control (Radix's own Tabs and ToggleGroup, which write
// `style` attributes into server HTML; ui/tabs.tsx and ui/toggle-group.tsx
// exist because of it).
import { Tabs, ToggleGroup } from "radix-ui";
import { renderToString } from "react-dom/server";

import { PrimitivesPage } from "./primitives-page.tsx";

export function renderPrimitivesPage(): string {
  return renderToString(<PrimitivesPage />);
}

export function renderRadixControl(): string {
  return renderToString(
    <main>
      <h1>Control: Radix as shipped</h1>
      <Tabs.Root defaultValue="overview">
        <Tabs.List aria-label="Sections">
          <Tabs.Trigger value="overview">Overview</Tabs.Trigger>
          <Tabs.Trigger value="logs">Logs</Tabs.Trigger>
        </Tabs.List>
        <Tabs.Content value="overview">Overview panel</Tabs.Content>
        <Tabs.Content value="logs">Logs panel</Tabs.Content>
      </Tabs.Root>
      <ToggleGroup.Root type="single" defaultValue="1h" aria-label="Window">
        <ToggleGroup.Item value="15m">15 min</ToggleGroup.Item>
        <ToggleGroup.Item value="1h">1 hour</ToggleGroup.Item>
      </ToggleGroup.Root>
    </main>,
  );
}
