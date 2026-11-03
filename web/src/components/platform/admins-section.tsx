"use client";

import { useState } from "react";
import type { FormEvent } from "react";

import { putAdmins } from "@/lib/api";
import type { PlatformView } from "@/lib/api";
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
import { Section } from "@/components/section";
import { Input } from "@/components/ui/input";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Textarea } from "@/components/ui/textarea";
import { ActionResult } from "./action-result";
import { FormSummary } from "./form-summary";
import { useWriteAction } from "./use-write-action";

type NewAdminType = "user" | "group";

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
  const [newType, setNewType] = useState<NewAdminType>("user");
  const [newName, setNewName] = useState("");
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
      if (next.kind === "add") setNewName("");
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
    change({ kind: "add", type: newType, name: newName });
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

        <div className="flex max-w-140 flex-col gap-4">
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
          className={`${FORM_STACK_CLASSES} max-w-140 border-t pt-4`}
          onSubmit={handleAdd}
        >
          <h3>Add an admin</h3>
          <FieldGrid>
            <Field label="Type">
              {(control) => (
                <Select
                  value={newType}
                  onValueChange={(value) => {
                    setNewType(value === "group" ? "group" : "user");
                  }}
                >
                  <SelectTrigger {...control} className="w-full">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent label="Type">
                    <SelectItem value="user">User</SelectItem>
                    <SelectItem value="group">Group</SelectItem>
                  </SelectContent>
                </Select>
              )}
            </Field>
            <Field
              label={newType === "user" ? "Email address" : "Group name"}
              hint={
                newType === "user"
                  ? "For example user@example.com."
                  : "The name of a workspace group."
              }
              error={errors.name}
            >
              {(control) => (
                <Input
                  {...control}
                  type="text"
                  autoComplete="off"
                  spellCheck={false}
                  value={newName}
                  onChange={(event) => {
                    setNewName(event.target.value);
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
