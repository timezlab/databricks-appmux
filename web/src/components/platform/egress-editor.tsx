"use client";

import { useReducer, useState } from "react";

import type { EgressRuleRequest } from "@/lib/api";
import {
  createEgressDraft,
  egressDraftReducer,
  egressWarnings,
  hasRemoteEgressChange,
  isEgressDraftDirty,
  syncEgressDraft,
  validateEgressDraft,
} from "@/lib/platform/egress";
import type { EgressSource } from "@/lib/platform/egress";
import type { FormErrors } from "@/lib/platform/forms";
import { pluralize } from "@/lib/format";
import { Button } from "@/components/button";
import { Field } from "@/components/field";
import {
  FORM_STACK_CLASSES,
  FieldGrid,
  FormActions,
} from "@/components/form-layout";
import { Notice } from "@/components/notice";
import { Input } from "@/components/ui/input";
import {
  NativeSelect,
  NativeSelectOption,
} from "@/components/ui/native-select";
import { ActionResult } from "./action-result";
import { useWriteAction } from "./use-write-action";

export interface EgressEditorProps {
  /** Names the list in labels and messages ("Global egress rules", "Egress rules of sales"). */
  name: string;
  scope: "global" | "app";
  /** The rules as last polled, with the `apps` version they came with (`null` for the global list). */
  source: EgressSource;
  /** Stores the rules and answers with what the server now holds. `version` is the one the draft started from. */
  save: (
    rules: EgressRuleRequest[],
    version: number | null,
  ) => Promise<EgressSource>;
  /** The sentence shown after a save ("Saved the global egress rules"). */
  savedTitle: string;
}

/**
 * An ordered list of egress rules to edit as a draft and save as a whole.
 * Which rule wins, whether the draft is out of date and what a rule must look
 * like are decided in `lib/platform/egress.ts`; this renders them.
 */
export function EgressEditor({
  name,
  scope,
  source,
  save,
  savedTitle,
}: EgressEditorProps) {
  const [draft, dispatch] = useReducer(
    egressDraftReducer,
    source,
    createEgressDraft,
  );
  // Derived on every render (no effect): the draft the person sees, given the latest poll.
  const shown = syncEgressDraft(draft, source);
  // Messages of the rules stay hidden until the first Save, so a fresh row is not red.
  const [isChecking, setIsChecking] = useState(false);
  const action = useWriteAction();

  const isDirty = isEgressDraftDirty(shown);
  const hasRemoteChange = hasRemoteEgressChange(shown, source);
  const validation = validateEgressDraft(shown.rules);
  const errorsByRule: Readonly<Record<string, FormErrors>> =
    isChecking && !validation.ok ? validation.errors : {};
  const warnings = egressWarnings(shown.rules, scope);

  function handleSave(): void {
    setIsChecking(true);
    if (!validation.ok) return;
    const { rules } = validation;
    void action.run(async () => {
      const stored = await save(rules, shown.version);
      dispatch({ type: "saved", rules: stored.rules, version: stored.version });
      setIsChecking(false);
      return {
        tone: "success",
        title: savedTitle,
        detail: `${pluralize(stored.rules.length, "rule")} stored.`,
      };
    });
  }

  function handleDiscard(): void {
    dispatch({ type: "discard", source });
    setIsChecking(false);
    action.clear();
  }

  return (
    <div className={FORM_STACK_CLASSES}>
      <p className="text-muted-foreground">
        {scope === "global"
          ? "These rules apply to every app, after the app's own rules. "
          : "These rules are checked first for this app; the global rules come next. "}
        The first rule that matches decides; traffic no rule mentions is
        allowed. The container&apos;s internal network 10.0.2.0/24 is always
        allowed. A new deny stalls connections that are already open instead of
        resetting them.
      </p>

      {hasRemoteChange ? (
        <Notice
          tone="warning"
          title="These rules changed while you were editing"
        >
          <p>
            Another admin (or another tab) stored different rules. Discard your
            changes to see theirs; saving is off until then so theirs are not
            overwritten.
          </p>
        </Notice>
      ) : null}

      {shown.rules.length === 0 ? (
        <p className="text-muted-foreground">
          {name} is empty
          {scope === "app"
            ? ": only the global rules apply."
            : ": nothing is blocked by the global list."}
        </p>
      ) : (
        shown.rules.map((rule, index) => {
          const errors = errorsByRule[String(rule.id)] ?? {};
          const number = index + 1;
          return (
            <fieldset key={rule.id}>
              <legend>Rule {number}</legend>
              <div className={FORM_STACK_CLASSES}>
                <FieldGrid>
                  <Field label="Action" error={errors.action}>
                    {(control) => (
                      <NativeSelect
                        {...control}
                        value={rule.action}
                        onChange={(event) => {
                          dispatch({
                            type: "edit",
                            id: rule.id,
                            field: "action",
                            value: event.target.value,
                            source,
                          });
                        }}
                      >
                        <NativeSelectOption value="allow">
                          allow
                        </NativeSelectOption>
                        <NativeSelectOption value="deny">
                          deny
                        </NativeSelectOption>
                      </NativeSelect>
                    )}
                  </Field>
                  <Field
                    label="Address (CIDR)"
                    hint="For example 10.20.0.0/16, or @container_ip/32"
                    error={errors.cidr}
                  >
                    {(control) => (
                      <Input
                        {...control}
                        type="text"
                        autoComplete="off"
                        spellCheck={false}
                        value={rule.cidr}
                        onChange={(event) => {
                          dispatch({
                            type: "edit",
                            id: rule.id,
                            field: "cidr",
                            value: event.target.value,
                            source,
                          });
                        }}
                      />
                    )}
                  </Field>
                  <Field
                    label="Port"
                    hint="any, a port from 1 to 65535, or @appmux_port"
                    error={errors.port}
                  >
                    {(control) => (
                      <Input
                        {...control}
                        type="text"
                        autoComplete="off"
                        spellCheck={false}
                        value={rule.port}
                        onChange={(event) => {
                          dispatch({
                            type: "edit",
                            id: rule.id,
                            field: "port",
                            value: event.target.value,
                            source,
                          });
                        }}
                      />
                    )}
                  </Field>
                  <Field label="Comment" error={errors.comment}>
                    {(control) => (
                      <Input
                        {...control}
                        type="text"
                        autoComplete="off"
                        value={rule.comment}
                        onChange={(event) => {
                          dispatch({
                            type: "edit",
                            id: rule.id,
                            field: "comment",
                            value: event.target.value,
                            source,
                          });
                        }}
                      />
                    )}
                  </Field>
                </FieldGrid>
                <div className="flex flex-wrap items-center gap-2">
                  <Button
                    size="small"
                    aria-label={`Move rule ${number} up`}
                    disabled={index === 0}
                    onClick={() => {
                      dispatch({
                        type: "move",
                        id: rule.id,
                        direction: "up",
                        source,
                      });
                    }}
                  >
                    Move up
                  </Button>
                  <Button
                    size="small"
                    aria-label={`Move rule ${number} down`}
                    disabled={index === shown.rules.length - 1}
                    onClick={() => {
                      dispatch({
                        type: "move",
                        id: rule.id,
                        direction: "down",
                        source,
                      });
                    }}
                  >
                    Move down
                  </Button>
                  <Button
                    size="small"
                    variant="danger"
                    aria-label={`Remove rule ${number}`}
                    onClick={() => {
                      dispatch({ type: "remove", id: rule.id, source });
                    }}
                  >
                    Remove
                  </Button>
                </div>
              </div>
            </fieldset>
          );
        })
      )}

      {warnings.length === 0 ? null : (
        <Notice tone="warning" title="Check these rules before saving">
          <ul className="list-disc pl-5">
            {warnings.map((warning) => (
              <li key={warning}>{warning}</li>
            ))}
          </ul>
        </Notice>
      )}

      <FormActions
        primary={
          <Button
            variant="primary"
            isBusy={action.isBusy}
            busyLabel="Saving"
            disabled={!isDirty || hasRemoteChange}
            onClick={handleSave}
          >
            Save rules
          </Button>
        }
      >
        <Button
          onClick={() => {
            dispatch({ type: "add", source });
          }}
        >
          Add rule
        </Button>
        <Button variant="ghost" disabled={!isDirty} onClick={handleDiscard}>
          Discard changes
        </Button>
      </FormActions>
      {isChecking && !validation.ok ? (
        <Notice
          tone="danger"
          live="assertive"
          title="Some rules need attention; the messages are under their fields"
        />
      ) : null}
      <ActionResult action={action} context={`Could not save ${name}`} />
    </div>
  );
}
