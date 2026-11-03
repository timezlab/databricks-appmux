import { Notice } from "@/components/notice";
import { Section } from "@/components/section";

export interface SectionPlaceholderProps {
  /** The section's name, as in the sidebar. */
  title: string;
  /** What the section will hold, in one sentence. */
  description: string;
}

/**
 * A section of one app whose page is not built yet: its name, what it will
 * hold, and a notice saying so, so its route exists and the sidebar never
 * leads nowhere. The app's name above is the page's one `<h1>`, so the title
 * here is a section heading. Each page lane replaces its placeholder; none may
 * remain when the overhaul is done (T205).
 */
export function SectionPlaceholder({
  title,
  description,
}: SectionPlaceholderProps) {
  return (
    <Section title={title}>
      <p className="text-muted-foreground">{description}</p>
      <Notice tone="info" title="This section is being built." />
    </Section>
  );
}
