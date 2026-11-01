import Link from "next/link";

import { EmptyState } from "@/components/empty-state";
import { PageHeader } from "@/components/page-header";
import { Button } from "@/components/ui/button";

// Without this file Next prerenders its built-in 404 page, which carries an
// inline <style> element and style attributes: the CSP (style-src 'self')
// refuses both. tests/export/html.test.ts checks the built 404 pages.
export default function NotFound() {
  return (
    <>
      <PageHeader title="Page not found" />
      <EmptyState
        title="There is no such page in the admin UI"
        action={
          <Button asChild>
            <Link href="/">Back to the launcher</Link>
          </Button>
        }
      >
        Check the address, or go back to the launcher.
      </EmptyState>
    </>
  );
}
