"use client";

import Link from "next/link";
import { useId, useRef, useState } from "react";

import { getMe, listApps, listRequests } from "@/lib/api";
import type { RequestRow } from "@/lib/api";
import {
  ME_POLL_INTERVAL_MS,
  accessBlockingError,
  pollData,
  registerFormContext,
} from "@/lib/launcher/page";
import {
  PLATFORM_INBOX_HREF,
  adminPendingCount,
  describeRequest,
  ownRequestsSection,
  pendingBannerText,
  shouldShowOwnRequests,
  withdrawnMessage,
} from "@/lib/launcher/requests";
import { usePoll } from "@/hooks/use-poll";
import { Button } from "@/components/button";
import { ErrorNotice } from "@/components/error-notice";
import { LiveRegion } from "@/components/live-region";
import { Notice } from "@/components/notice";
import { PageHeader } from "@/components/page-header";
import { Section } from "@/components/section";
import { AppsSection } from "./app-list";
import { OwnRequests } from "./own-requests";
import { RegisterForm } from "./register-form";
import { useAnsweredRequests } from "./use-answered-requests";

/**
 * The launcher (design 10.1): the apps the visitor may open, a form to ask for
 * a new one, and, only for a person who has asked for something, their own
 * requests. Three polls feed it; what each part shows and decides is in
 * `lib/launcher/*.ts`.
 */
export function Launcher() {
  const apps = usePoll((signal) => listApps({ signal }));
  const requests = usePoll((signal) => listRequests({ signal }));
  const me = usePoll((signal) => getMe({ signal }), {
    intervalMs: ME_POLL_INTERVAL_MS,
  });

  const [isFormOpen, setIsFormOpen] = useState(false);
  // What the last action did ("Sent your request ..."): shown and announced.
  const [actionMessage, setActionMessage] = useState<string | null>(null);
  const formId = useId();
  const formToggleRef = useRef<HTMLButtonElement>(null);

  const ownSection = ownRequestsSection(me, requests);
  const answeredMessage = useAnsweredRequests(ownSection);

  function refreshAll(): void {
    apps.refresh();
    requests.refresh();
    me.refresh();
  }

  const blockingError = accessBlockingError(apps);
  if (blockingError !== null) {
    return (
      <div className="flex flex-col gap-4">
        <PageHeader title="Apps" />
        <ErrorNotice error={blockingError} context="Could not load your apps" />
      </div>
    );
  }

  function closeForm(): void {
    setIsFormOpen(false);
    // The form's own buttons are about to disappear; keep the keyboard here.
    formToggleRef.current?.focus();
  }

  function handleSent(request: RequestRow): void {
    setActionMessage(
      `Sent your request for ${describeRequest(request).shortLabel}. A platform admin will answer it here.`,
    );
    requests.refresh();
    closeForm();
  }

  function handleWithdrawn(request: RequestRow): void {
    setActionMessage(withdrawnMessage(request));
    requests.refresh();
  }

  const bannerText = pendingBannerText(adminPendingCount(me, requests));
  const meData = pollData(me);

  return (
    <div className="flex flex-col gap-6">
      <PageHeader
        title="Apps"
        description="Open an app you can use, or ask for a new one."
        actions={
          <>
            <Button variant="ghost" onClick={refreshAll}>
              Refresh
            </Button>
            {/* A quiet secondary action: each app's Open button is the main one here. */}
            <Button
              ref={formToggleRef}
              aria-expanded={isFormOpen}
              aria-controls={formId}
              onClick={() => {
                setActionMessage(null);
                setIsFormOpen((current) => !current);
              }}
            >
              Request a new app
            </Button>
          </>
        }
      />
      <LiveRegion message={actionMessage} />
      <LiveRegion message={answeredMessage} />

      {actionMessage === null ? null : (
        <Notice tone="success" title={actionMessage} />
      )}
      {bannerText === null ? null : (
        <Notice tone="info" title={bannerText}>
          <p>
            <Link href={PLATFORM_INBOX_HREF}>Open the inbox</Link> on the
            Platform page.
          </p>
        </Notice>
      )}

      {/* `contents`: while the form is closed this wrapper has no box, so it adds no gap of its own. */}
      <div id={formId} className="contents">
        {isFormOpen ? (
          <Section title="Request a new app">
            <RegisterForm
              context={registerFormContext(apps, ownSection)}
              onSent={handleSent}
              onCancel={closeForm}
              onReload={refreshAll}
            />
          </Section>
        ) : null}
      </div>

      <AppsSection
        apps={apps}
        isPlatformAdmin={meData?.is_platform_admin ?? null}
        onReload={apps.refresh}
      />

      {shouldShowOwnRequests(ownSection) ? (
        <Section title="Your requests">
          <OwnRequests
            section={ownSection}
            onWithdrawn={handleWithdrawn}
            onReload={requests.refresh}
          />
        </Section>
      ) : null}
    </div>
  );
}
