"use client";

import { useState } from "react";
import type { FormEvent } from "react";

import { putAdmins } from "@/lib/api";
import type { PlatformView } from "@/lib/api";
import { withoutError } from "@/lib/app/forms";
import { ADMIN_PRINCIPAL_TYPES } from "@/lib/app/sharing";
import type { PrincipalChoice } from "@/lib/app/sharing";
import { adminRows, parseAdminChange } from "@/lib/platform/admins";
import type { AdminChange, AdminRow, AdminViewer } from "@/lib/platform/admins";
import type { FormErrors } from "@/lib/platform/forms";
import { Button } from "@/components/button";
import { ConfirmButton } from "@/components/confirm-button";
import { DataTable } from "@/components/data-table";
import { Field } from "@/components/field";
import {
  FORM_STACK_CLASSES,
  FieldGrid,
  FormActions,
} from "@/components/form-layout";
import { PrincipalCombobox } from "@/components/principal-combobox";
import { Section } from "@/components/section";
import { Textarea } from "@/components/ui/textarea";
import { ActionResult } from "./action-result";
import { FormSummary } from "./form-summary";
import { useWriteAction } from "./use-write-action";

const CHOOSE_WHO_MESSAGE = "Choose a user or a group to add.";

/** A platform admin is a user or a group; the field offers nothing else, but narrow the type for the request. */
function adminType(choice: PrincipalChoice): "user" | "group" | null {
  return choice.type === "user" || choice.type === "group" ? choice.type : null;
}

/**
 * The platform admins, who may open this page. Each change sends the whole
 * list with a reason for the audit log. The list has no version, so when two
 * admins save at once the later save wins.
 */
export function AdminsSection({
  view,
  viewer,
}: {
  view: PlatformView;
  viewer: AdminViewer;
}) {
  const [reason, setReason] = useState("");
  const [newAdmin, setNewAdmin] = useState<PrincipalChoice | null>(null);
  const [errors, setErrors] = useState<FormErrors>({});
  const action = useWriteAction();

  const rows = adminRows(view.admins, viewer);

  function change(next: AdminChange): void {
    const parsed = parseAdminChange(view.admins, next, reason);
    if (!parsed.ok) {
      setErrors(parsed.errors);
      return;
    }
    setErrors({});
    const body = parsed.value;
    void action.run(async () => {
      await putAdmins(body);
      setReason("");
      if (next.kind === "add") setNewAdmin(null);
      return {
        tone: "success",
        title:
          next.kind === "add"
            ? `Added ${next.name.trim()} to the platform admins`
            : `Removed ${next.name} from the platform admins`,
      };
    });
  }

  function handleAdd(event: FormEvent<HTMLFormElement>): void {
    event.preventDefault();
    const type = newAdmin === null ? null : adminType(newAdmin);
    if (newAdmin === null || type === null) {
      setErrors({ name: CHOOSE_WHO_MESSAGE });
      return;
    }
    change({ kind: "add", type, name: newAdmin.name });
  }

  return (
    <Section title="Platform admins">
      <div className="flex flex-col gap-4">
        <DataTable<AdminRow>
          caption="Platform admins"
          isCaptionHidden
          rows={rows}
          rowKey={(row) => row.key}
          columns={[
            {
              key: "name",
              header: "Name",
              isRowHeader: true,
              cell: (row) => (
                <>
                  <span className="wrap-break-word">{row.name}</span>
                  {row.isYou ? (
                    <span className="text-muted-foreground"> (you)</span>
                  ) : null}
                </>
              ),
            },
            {
              key: "type",
              header: "Type",
              cell: (row) => row.typeLabel,
            },
            {
              key: "actions",
              header: "Actions",
              isLabelHiddenWhenStacked: true,
              cell: (row) =>
                row.canRemove ? (
                  <ConfirmButton
                    label="Remove"
                    size="small"
                    prompt={
                      row.losesYourAccess
                        ? `Remove ${row.name}? You would lose access to this page.`
                        : `Remove ${row.name} from the platform admins?`
                    }
                    confirmLabel={`Yes, remove ${row.name}`}
                    isBusy={action.isBusy}
                    onConfirm={() => {
                      change({
                        kind: "remove",
                        type: row.type,
                        name: row.name,
                      });
                    }}
                  />
                ) : (
                  <span className="text-muted-foreground">
                    {row.blockedReason}
                  </span>
                ),
            },
          ]}
        />

        <div className="flex flex-col gap-4">
          <FormSummary errors={errors} />
          <Field
            label="Reason for the next change"
            hint="Required for adding and for removing. Recorded in the audit log."
            error={errors.reason}
          >
            {(control) => (
              <Textarea
                {...control}
                value={reason}
                onChange={(event) => {
                  setReason(event.target.value);
                }}
              />
            )}
          </Field>
        </div>

        <form
          className={`${FORM_STACK_CLASSES} border-t pt-4`}
          onSubmit={handleAdd}
        >
          <h3>Add an admin</h3>
          <FieldGrid>
            <Field
              label="Who"
              hint="A user or a group. Type to search, or type it in full."
              error={errors.name}
              isRequired
            >
              {(control) => (
                <PrincipalCombobox
                  {...control}
                  value={newAdmin}
                  allowedTypes={ADMIN_PRINCIPAL_TYPES}
                  label="Who to make a platform admin"
                  onValueChange={(choice) => {
                    setNewAdmin(choice);
                    setErrors((current) => withoutError(current, "name"));
                  }}
                />
              )}
            </Field>
          </FieldGrid>
          <FormActions
            primary={
              <Button
                type="submit"
                variant="primary"
                isBusy={action.isBusy}
                busyLabel="Saving"
              >
                Add admin
              </Button>
            }
          />
        </form>
        <ActionResult
          action={action}
          context="Could not change the platform admins"
        />
      </div>
    </Section>
  );
}
