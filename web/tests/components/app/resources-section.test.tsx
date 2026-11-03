import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ResourceArea } from "@/components/app/resource-area";
import { ResourcesSection } from "@/components/app/resources-section";
import type { ResourceTypesState } from "@/components/app/resources/use-resource-types";
import { useAppActions } from "@/components/app/use-app-actions";
import { ApiError, ResourceTypeList, ResourceView } from "@/lib/api";
import type { AppDetail } from "@/lib/api";
import type { Actor } from "@/lib/app/actions";
import { emptyAttachDraft } from "@/lib/app/resources";
import type { AttachDraft } from "@/lib/app/resources";
import { stubFetch } from "../../support/fake-fetch";
import {
  APP,
  appWith,
  describeCall,
  jsonResponse,
  readExample,
  renderWithActions,
  stubApi,
} from "./form-test-harness";

afterEach(() => {
  vi.restoreAllMocks();
});

const MANAGER: Actor = {
  email: "alice@example.com",
  isPlatformAdmin: false,
  permission: "CAN_MANAGE",
  canManage: true,
};
const VISITOR: Actor = { ...MANAGER, permission: "CAN_USE", canManage: false };

const READY: ResourceTypesState = {
  status: "ready",
  catalogue: ResourceTypeList.parse(readExample("ResourceTypeList")).items,
};

function renderSection({
  actor = MANAGER,
  draft = null,
  types = READY,
  app = APP,
}: {
  actor?: Actor;
  draft?: AttachDraft | null;
  types?: ResourceTypesState;
  app?: AppDetail;
} = {}) {
  const onOpenPanel = vi.fn();
  const onClosePanel = vi.fn();
  const onRetryTypes = vi.fn();
  const view = renderWithActions((actions) => (
    <ResourcesSection
      app={app}
      actor={actor}
      actions={actions}
      onReload={vi.fn()}
      types={types}
      onRetryTypes={onRetryTypes}
      draft={draft}
      draftId={1}
      onOpenPanel={onOpenPanel}
      onClosePanel={onClosePanel}
    />
  ));
  return {
    ...view,
    onOpenPanel,
    onClosePanel,
    onRetryTypes,
    row: (name: RegExp) =>
      within(
        screen.getByRole("table", { name: "Resources attached to this app" }),
      ).getByRole("row", { name }),
    user: userEvent.setup(),
  };
}

describe("ResourcesSection", () => {
  it("lists each resource's type, target, permission, access mode and check, in words", () => {
    const { row } = renderSection();

    const warehouse = row(/^sql-warehouse/);
    expect(warehouse).toHaveTextContent("SQL warehouse");
    expect(warehouse).toHaveTextContent("a1b2c3d4e5f67890");
    expect(warehouse).toHaveTextContent("Can use");
    expect(warehouse).toHaveTextContent("As the visitor (OBO)");
    expect(warehouse).toHaveTextContent("Verified");

    // A type the catalogue does not list shows by its wire name.
    const model = row(/^forecast-model/);
    expect(model).toHaveTextContent("serving_endpoint");
    expect(model).toHaveTextContent("Not verified");
  });

  it("shows a secret as its scope/key reference with no access mode, never as a value", () => {
    const { row } = renderSection();

    const secret = row(/^openai-api-key/);
    expect(secret).toHaveTextContent("Secret");
    expect(secret).toHaveTextContent("sales-secrets/openai-api-key");
    expect(secret).toHaveTextContent("Read by appmux when the app starts");
  });

  it("says when the active release does not refer to a resource", () => {
    const { row } = renderSection();
    expect(row(/^forecast-model/)).toHaveTextContent(
      "Release 8 does not refer to it.",
    );
    // The own service principal's keys are read by appmux, not the release.
    expect(row(/^sp-client-id/)).not.toHaveTextContent("does not refer");
  });

  it("asks before detaching, names what still reads the resource, then deletes it by key", async () => {
    const calls = stubApi(() => new Response(null, { status: 204 }));
    const { row, user, onChanged } = renderSection();

    await user.click(
      within(row(/^sql-warehouse/)).getByRole("button", { name: "Detach" }),
    );
    expect(calls).toHaveLength(0);
    expect(
      screen.getByRole("group", {
        name: /Detach sql-warehouse\? Still read by the env entry WAREHOUSE\./,
      }),
    ).toBeInTheDocument();

    await user.click(
      screen.getByRole("button", { name: "Yes, detach sql-warehouse" }),
    );

    await waitFor(() => {
      expect(onChanged).toHaveBeenCalledTimes(1);
    });
    expect(describeCall(calls[0])).toEqual({
      method: "DELETE",
      path: "/apps/sales/resources/sql-warehouse",
      body: undefined,
    });
  });

  it("opens and closes the Add panel with one button that says which it will do", async () => {
    const closed = renderSection();
    const openButton = screen.getByRole("button", { name: "Add resource" });
    expect(openButton).toHaveAttribute("aria-expanded", "false");
    await closed.user.click(openButton);
    expect(closed.onOpenPanel).toHaveBeenCalledTimes(1);
    closed.unmount();

    const open = renderSection({ draft: emptyAttachDraft() });
    expect(
      screen.getByRole("form", { name: "Add a resource" }),
    ).toBeInTheDocument();
    const closeButton = screen.getByRole("button", { name: "Close" });
    expect(closeButton).toHaveAttribute("aria-expanded", "true");
    await open.user.click(closeButton);
    expect(open.onClosePanel).toHaveBeenCalledTimes(1);
  });

  it("holds the panel's place while the types load, and offers a retry when they could not be read", async () => {
    const loading = renderSection({
      draft: emptyAttachDraft(),
      types: { status: "loading" },
    });
    expect(screen.getByRole("status")).toHaveTextContent(
      "Loading the resource types",
    );
    loading.unmount();

    const failed = renderSection({
      draft: emptyAttachDraft(),
      types: {
        status: "failed",
        error: new ApiError(503, "unavailable", "the server is starting"),
      },
    });
    expect(screen.getByRole("alert")).toHaveTextContent(
      "Could not read the resource types",
    );
    await failed.user.click(screen.getByRole("button", { name: /Try again/ }));
    expect(failed.onRetryTypes).toHaveBeenCalledTimes(1);
  });

  it("offers neither Add nor Detach to a visitor, and says why", () => {
    const { row } = renderSection({ actor: VISITOR });

    expect(
      screen.queryByRole("button", { name: "Add resource" }),
    ).not.toBeInTheDocument();
    expect(
      within(row(/^sql-warehouse/)).queryByRole("button", { name: "Detach" }),
    ).not.toBeInTheDocument();
    expect(
      screen.getByText("Read only: Only an admin of this app can do this."),
    ).toBeInTheDocument();
  });

  it("says what to do when nothing is attached", () => {
    const view = renderSection({ app: appWith({ resources: [] }) });
    expect(view.container).toHaveTextContent("No resources attached");
    expect(screen.queryByRole("table")).toBeNull();
  });
});

describe("ResourceArea", () => {
  const NEW_RESOURCE: ResourceView = {
    ...ResourceView.parse(readExample("ResourceView")),
    key: "shared-serverless",
    target: "1234567890abcdef",
  };
  const onChanged = vi.fn();
  const onNotice = vi.fn();
  const onReload = vi.fn();

  /** The page around the area: the app arrives as a prop, so a reload is a rerender with the new one. */
  function AreaHost({ app }: { app: AppDetail }) {
    const actions = useAppActions({ onChanged, onNotice });
    return (
      <ResourceArea
        app={app}
        actor={MANAGER}
        actions={actions}
        onReload={onReload}
      />
    );
  }

  it("moves the keyboard to the new row once the reloaded app has it", async () => {
    stubFetch((call) => {
      if (call.url.includes("/resources/types")) {
        return jsonResponse(200, readExample("ResourceTypeList"));
      }
      if (call.url.includes("/resources/suggestions")) {
        return jsonResponse(200, readExample("ResourceSuggestionList"));
      }
      return jsonResponse(200, NEW_RESOURCE);
    });
    const user = userEvent.setup();
    const view = render(<AreaHost app={APP} />);

    await user.click(screen.getByRole("button", { name: "Add resource" }));
    const type = await screen.findByRole("combobox", { name: /^Type/ });
    expect(type).toHaveFocus();
    await user.click(type);
    await user.click(screen.getByRole("option", { name: /^SQL warehouse/ }));
    const object = screen.getByRole("combobox", { name: /^SQL warehouse/ });
    await waitFor(() => {
      expect(object).toBeEnabled();
    });
    await user.click(object);
    await user.click(screen.getByRole("option", { name: "Shared Serverless" }));
    await user.click(screen.getByRole("combobox", { name: /^Access mode/ }));
    await user.click(screen.getByRole("option", { name: /^As the visitor/ }));
    await user.click(screen.getByRole("button", { name: "Add" }));

    await waitFor(() => {
      expect(onChanged).toHaveBeenCalledTimes(1);
    });
    expect(
      screen.queryByRole("form", { name: "Add a resource" }),
    ).not.toBeInTheDocument();

    // The page reloads the app; the new row arrives and takes the focus.
    view.rerender(
      <AreaHost
        app={{ ...APP, resources: [...APP.resources, NEW_RESOURCE] }}
      />,
    );
    await waitFor(() => {
      expect(screen.getByText("shared-serverless")).toHaveFocus();
    });
  });
});
