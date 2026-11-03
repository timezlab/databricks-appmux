/**
 * Which sections of an app still show the "being built" placeholder. This list
 * is meant to change: a page lane that builds its section moves it from
 * `STILL_PLACEHOLDERS` to built here, in the same commit, and the overhaul is
 * done only when the list is empty (T205 also greps for `SectionPlaceholder`).
 * The record of pages is typed by `AppSection`, so a section without a page
 * component does not compile.
 */
import { render, screen } from "@testing-library/react";
import type { ComponentType } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { AppView } from "@/components/app/app-view";
import { AppAuthorizationPage } from "@/components/app/pages/authorization-page";
import { AppDeploymentsPage } from "@/components/app/pages/deployments-page";
import { AppEnvironmentPage } from "@/components/app/pages/environment-page";
import { AppInsightsPage } from "@/components/app/pages/insights-page";
import { AppLogsPage } from "@/components/app/pages/logs-page";
import { AppOverviewPage } from "@/components/app/pages/overview-page";
import { AppRequestsPage } from "@/components/app/pages/requests-page";
import { AppResourcesPage } from "@/components/app/pages/resources-page";
import { AppSecurityPage } from "@/components/app/pages/security-page";
import { AppSettingsPage } from "@/components/app/pages/settings-page";
import { APP_SECTIONS } from "@/lib/app/params";
import type { AppSection } from "@/lib/app/params";
import { readExample } from "../../support/contract-examples";
import { jsonResponse, stubFetch } from "../../support/fake-fetch";
import type { RecordedCall } from "../../support/fake-fetch";

vi.mock("next/navigation", () => ({
  usePathname: () => "/app/",
  useRouter: () => ({ replace: vi.fn(), push: vi.fn() }),
}));

const PAGES: Readonly<Record<AppSection, ComponentType>> = {
  overview: AppOverviewPage,
  authorization: AppAuthorizationPage,
  deployments: AppDeploymentsPage,
  logs: AppLogsPage,
  security: AppSecurityPage,
  environment: AppEnvironmentPage,
  resources: AppResourcesPage,
  insights: AppInsightsPage,
  requests: AppRequestsPage,
  settings: AppSettingsPage,
};

/** The sections not built yet. Remove a section from here when its page replaces the placeholder. */
const STILL_PLACEHOLDERS: readonly AppSection[] = [
  "logs",
  "environment",
  "insights",
];

const PLACEHOLDER_TEXT = "This section is being built.";

function route({ url }: RecordedCall): Response {
  if (url.endsWith("/api/v1/apps/sales"))
    return jsonResponse(200, readExample("AppDetail"));
  if (url.endsWith("/api/v1/me"))
    return jsonResponse(200, readExample("MeResponse"));
  if (url.endsWith("/api/v1/apps"))
    return jsonResponse(200, readExample("AppList"));
  if (url.endsWith("/api/v1/requests"))
    return jsonResponse(200, readExample("RequestList"));
  return jsonResponse(404, {
    error: { code: "not_found", message: "no such route in this test" },
  });
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe("the sections that still show a placeholder", () => {
  for (const section of APP_SECTIONS) {
    const isPlaceholder = STILL_PLACEHOLDERS.includes(section);
    it(`${section} ${isPlaceholder ? "still shows" : "no longer shows"} the placeholder`, async () => {
      stubFetch(route);
      const Page = PAGES[section];
      render(
        <AppView slug="sales">
          <Page />
        </AppView>,
      );
      // The page is drawn once the shell has the app.
      await screen.findByRole("heading", { level: 1, name: "Sales dashboard" });
      if (isPlaceholder) {
        expect(screen.getByText(PLACEHOLDER_TEXT)).toBeInTheDocument();
      } else {
        expect(screen.queryByText(PLACEHOLDER_TEXT)).not.toBeInTheDocument();
      }
    });
  }
});
