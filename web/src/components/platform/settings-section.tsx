"use client";

import { useEffect, useId, useMemo, useReducer, useRef, useState } from "react";

import { putSetting } from "@/lib/api";
import type { PlatformView, SettingKey } from "@/lib/api";
import { pluralize } from "@/lib/format";
import { SETTING_GROUPS } from "@/lib/platform/sections";
import {
  INITIAL_SETTINGS_DRAFT,
  changedSettingKeys,
  planSettingsSave,
  runSettingsSave,
  saveSummary,
  savedValues,
  settingErrors,
  settingsDraftReducer,
} from "@/lib/platform/settings-draft";
import type { SaveStepResult } from "@/lib/platform/settings-draft";
import {
  SETTING_DESCRIPTORS,
  formatSettingValue,
  parseSettingValue,
  settingDraft,
  settingSaveWarning,
} from "@/lib/platform/settings";
import type { SettingDescriptor } from "@/lib/platform/settings";
import { toneMark } from "@/lib/status-presentation";
import { Button } from "@/components/button";
import { ConfirmButton } from "@/components/confirm-button";
import { describeError } from "@/components/error-message";
import { FieldError } from "@/components/field";
import { Notice } from "@/components/notice";
import { Section } from "@/components/section";
import { Badge } from "@/components/ui/badge";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Textarea } from "@/components/ui/textarea";
import { usePlatformContext } from "./platform-context";
import { useUnsavedChangesGuard } from "./use-unsaved-changes-guard";

/** What the row of a setting shows and does, from the page's state. */
interface RowState {
  draft: string;
  isChanged: boolean;
  /** Why the text is not acceptable, or the sentence of a failed save. */
  error: string | undefined;
  /** The stored value now, when another admin changed it while this row was being edited. */
  conflict: string | undefined;
}

const CONTROL_ATTRIBUTE = "data-setting-control";

/** One sentence for a failed call: what failed, the server's reason, what to do. */
function failureSentence(error: unknown): string {
  const message = describeError(error);
  return [message.heading, message.detail, message.hint]
    .filter((part): part is string => part !== null && part !== "")
    .map((part) => part.replace(/[.\s]+$/, ""))
    .join(". ")
    .concat(".");
}

function failuresOf(results: readonly SaveStepResult[]) {
  const failures: Partial<Record<SettingKey, string>> = {};
  for (const result of results) {
    if (result.status === "failed") {
      failures[result.key] = failureSentence(result.error);
    } else if (result.status === "skipped") {
      failures[result.key] =
        `Not sent: ${result.because} was not saved, and this setting depends on it.`;
    }
  }
  return failures;
}

/**
 * Every platform setting in groups, each a row (name, meaning and current value
 * on the left, the editor on the right), and one bar at the bottom that saves
 * what changed. The API takes one setting per call, so "Save changes" sends one
 * call per changed row and says which went through (`settings-draft.ts`).
 */
export function SettingsGroups({
  view,
  navigate,
}: {
  view: PlatformView;
  /** Where "Leave and discard" goes; tests replace it because jsdom cannot navigate. */
  navigate?: (url: string) => void;
}) {
  const { refresh, announce } = usePlatformContext();
  const [state, dispatch] = useReducer(
    settingsDraftReducer,
    INITIAL_SETTINGS_DRAFT,
  );
  const [isSaving, setIsSaving] = useState(false);
  const isSavingRef = useRef(false);
  const rootRef = useRef<HTMLDivElement>(null);

  // What the server holds, plus what it accepted a moment ago and the poll has not shown yet.
  const stored = useMemo(
    () => ({ ...view.settings, ...state.confirmed }),
    [view.settings, state.confirmed],
  );

  // A new view may change what a row was edited from (another admin saved).
  // The poll builds a new object every time, so the content is what is compared.
  // Adjusting state during render, as React documents for a value that changed.
  const viewKey = JSON.stringify(view.settings);
  const [seenViewKey, setSeenViewKey] = useState(viewKey);
  if (seenViewKey !== viewKey) {
    setSeenViewKey(viewKey);
    dispatch({ type: "viewChanged", stored: view.settings });
  }

  const changedKeys = changedSettingKeys(state.drafts, stored);
  const errors = settingErrors(state.drafts, stored);
  const invalidKeys = changedKeys.filter((key) => errors[key] !== undefined);
  const failedKeys = changedKeys.filter(
    (key) => state.failures[key] !== undefined,
  );
  const isDirty = changedKeys.length > 0;
  const guard = useUnsavedChangesGuard(isDirty, navigate);

  function rowState(key: SettingKey): RowState {
    const draft = state.drafts[key] ?? settingDraft(key, stored);
    return {
      draft,
      isChanged: changedKeys.includes(key),
      error: errors[key] ?? state.failures[key],
      conflict: state.conflicts[key],
    };
  }

  function focusControl(key: SettingKey): void {
    const control = rootRef.current?.querySelector<HTMLElement>(
      `[${CONTROL_ATTRIBUTE}="${key}"]`,
    );
    control?.focus();
  }

  async function save(): Promise<void> {
    if (isSavingRef.current) return;
    const plan = planSettingsSave(state.drafts, stored);
    if (!plan.ok) {
      const first = changedKeys.find((key) => plan.errors[key] !== undefined);
      if (first !== undefined) focusControl(first);
      return;
    }
    if (plan.steps.length === 0) return;
    isSavingRef.current = true;
    setIsSaving(true);
    try {
      const results = await runSettingsSave(plan.steps, putSetting);
      dispatch({
        type: "saveFinished",
        results,
        saved: savedValues(plan.steps, results),
        failures: failuresOf(results),
      });
      const first = results.find((result) => result.status !== "saved");
      if (first !== undefined) focusControl(first.key);
      if (results.some((result) => result.status === "saved")) refresh();
      announce(saveSummary(results));
    } finally {
      isSavingRef.current = false;
      setIsSaving(false);
    }
  }

  // A value that needs a second look is saved through a confirm step.
  const warnings = changedKeys.flatMap((key) => {
    const parsed = parseSettingValue(key, state.drafts[key] ?? "");
    const warning = parsed.ok
      ? settingSaveWarning(key, parsed.value.value)
      : null;
    return warning === null ? [] : [warning];
  });

  return (
    <div ref={rootRef} className="flex min-w-0 flex-col gap-6">
      <p className="text-muted-foreground">
        Change as many settings as you need, then save them together with{" "}
        <strong>Save changes</strong> at the bottom. Every setting here reaches
        all children, not one app.
      </p>
      {state.lastSave !== null && !isDirty ? (
        <Notice tone="success" title={state.lastSave} live="polite" />
      ) : null}
      {SETTING_GROUPS.map((group) => (
        <Section key={group.id} id={group.id} title={group.title}>
          <p className="text-muted-foreground">{group.summary}</p>
          <ul className="flex flex-col divide-y rounded-md border">
            {group.keys.map((key) => (
              <SettingRow
                key={key}
                descriptor={SETTING_DESCRIPTORS[key]}
                nowText={formatSettingValue(key, stored)}
                row={rowState(key)}
                onChange={(text) => {
                  dispatch({ type: "edited", key, text, stored });
                }}
              />
            ))}
          </ul>
        </Section>
      ))}
      {isDirty || guard.pendingUrl !== null ? (
        <SaveBar
          changedCount={changedKeys.length}
          invalidKeys={invalidKeys}
          errors={errors}
          failedCount={failedKeys.length}
          lastSave={state.lastSave}
          isSaving={isSaving}
          warnings={warnings}
          pendingUrl={guard.pendingUrl}
          onSave={() => {
            void save();
          }}
          onDiscard={() => {
            dispatch({ type: "discarded" });
          }}
          onStay={guard.stay}
          onLeave={() => {
            dispatch({ type: "discarded" });
            guard.leave();
          }}
        />
      ) : null}
    </div>
  );
}

// --------------------------------------------------------------------- row

function SettingRow({
  descriptor,
  nowText,
  row,
  onChange,
}: {
  descriptor: SettingDescriptor;
  nowText: string;
  row: RowState;
  onChange: (text: string) => void;
}) {
  const id = useId();
  const meaningId = `${id}-meaning`;
  const hintId = `${id}-hint`;
  const errorId = `${id}-error`;
  const hint =
    descriptor.editor.kind === "list" ? "One folder per line." : undefined;
  const describedBy = [
    meaningId,
    hint === undefined ? null : hintId,
    row.error === undefined ? null : errorId,
  ]
    .filter((part) => part !== null)
    .join(" ");

  return (
    <li
      data-setting-row={descriptor.key}
      className={`grid gap-x-6 gap-y-4 border-l-4 px-4 py-4 lg:grid-cols-[minmax(0,1fr)_minmax(0,22rem)] ${
        row.isChanged
          ? "border-l-tone-info-border bg-card"
          : "border-l-transparent"
      }`}
    >
      <div className="flex min-w-0 flex-col gap-1">
        <div className="flex flex-wrap items-center gap-2">
          <Label
            htmlFor={id}
            className="font-mono text-base leading-normal font-semibold wrap-anywhere"
          >
            {descriptor.label}
          </Label>
          {row.isChanged ? (
            <Badge variant="info">
              <span aria-hidden="true">{toneMark("info")}</span>
              Changed
            </Badge>
          ) : null}
        </div>
        <p id={meaningId} className="text-muted-foreground">
          {descriptor.meaning}
        </p>
        <p className="text-sm">
          Now: <strong>{nowText}</strong>, reaches all children
        </p>
      </div>
      <div className="flex min-w-0 flex-col gap-1">
        <SettingControl
          descriptor={descriptor}
          control={{
            id,
            "aria-describedby": describedBy,
            "aria-invalid": row.error === undefined ? undefined : true,
            [CONTROL_ATTRIBUTE]: descriptor.key,
          }}
          draft={row.draft}
          onChange={onChange}
        />
        {hint === undefined ? null : (
          <p id={hintId} className="text-sm text-muted-foreground">
            {hint}
          </p>
        )}
        {row.error === undefined ? null : (
          <FieldError id={errorId}>{row.error}</FieldError>
        )}
        {row.conflict === undefined ? null : (
          <p className="text-sm text-tone-warning-text">
            <span aria-hidden="true">{toneMark("warning")} </span>
            Another admin changed this to{" "}
            <strong>
              {row.conflict === ""
                ? "(empty)"
                : row.conflict.replaceAll("\n", ", ")}
            </strong>{" "}
            while you were editing. Saving replaces it with your value.
          </p>
        )}
      </div>
    </li>
  );
}

interface ControlProps {
  id: string;
  "aria-describedby": string;
  "aria-invalid": true | undefined;
  [CONTROL_ATTRIBUTE]: SettingKey;
}

function SettingControl({
  descriptor,
  control,
  draft,
  onChange,
}: {
  descriptor: SettingDescriptor;
  control: ControlProps;
  draft: string;
  onChange: (text: string) => void;
}) {
  const editor = descriptor.editor;
  switch (editor.kind) {
    case "integer":
      return (
        <div className="flex items-center gap-2">
          <Input
            {...control}
            type="text"
            inputMode="numeric"
            autoComplete="off"
            className="w-40"
            value={draft}
            onChange={(event) => {
              onChange(event.target.value);
            }}
          />
          {editor.unit === null ? null : (
            <span className="text-muted-foreground">{editor.unit}</span>
          )}
        </div>
      );
    case "boolean":
      return (
        <Select value={draft} onValueChange={onChange}>
          <SelectTrigger {...control} className="w-full max-w-40">
            <SelectValue />
          </SelectTrigger>
          <SelectContent label={descriptor.label}>
            <SelectItem value="true">On</SelectItem>
            <SelectItem value="false">Off</SelectItem>
          </SelectContent>
        </Select>
      );
    case "choice":
      return (
        <Select value={draft} onValueChange={onChange}>
          <SelectTrigger {...control} className="w-full max-w-64">
            <SelectValue />
          </SelectTrigger>
          <SelectContent label={descriptor.label}>
            {editor.options.map((option) => (
              <SelectItem key={option} value={option}>
                {option}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      );
    case "list":
      return (
        <Textarea
          {...control}
          rows={3}
          spellCheck={false}
          value={draft}
          onChange={(event) => {
            onChange(event.target.value);
          }}
        />
      );
    case "optional-text":
      return (
        <Input
          {...control}
          type="text"
          autoComplete="off"
          spellCheck={false}
          value={draft}
          onChange={(event) => {
            onChange(event.target.value);
          }}
        />
      );
  }
}

// ---------------------------------------------------------------- save bar

function SaveBar({
  changedCount,
  invalidKeys,
  errors,
  failedCount,
  lastSave,
  isSaving,
  warnings,
  pendingUrl,
  onSave,
  onDiscard,
  onStay,
  onLeave,
}: {
  changedCount: number;
  invalidKeys: readonly SettingKey[];
  errors: Partial<Record<SettingKey, string>>;
  failedCount: number;
  lastSave: string | null;
  isSaving: boolean;
  warnings: readonly string[];
  pendingUrl: string | null;
  onSave: () => void;
  onDiscard: () => void;
  onStay: () => void;
  onLeave: () => void;
}) {
  const stayRef = useRef<HTMLButtonElement>(null);
  const isAskingToLeave = pendingUrl !== null;
  useEffect(() => {
    if (isAskingToLeave) stayRef.current?.focus();
  }, [isAskingToLeave]);

  const firstInvalid = invalidKeys[0];
  const firstProblem =
    firstInvalid === undefined
      ? null
      : `${firstInvalid} ${errors[firstInvalid] ?? ""}`;
  const countText = `${pluralize(changedCount, "setting")} changed, not saved yet`;

  return (
    <div
      role="region"
      aria-label="Unsaved changes"
      className="sticky bottom-0 z-10 flex flex-col gap-4 border-t-2 border-t-tone-info-border bg-background py-4 sm:flex-row sm:items-center sm:justify-between"
    >
      <div
        className="flex min-w-0 flex-1 flex-col gap-1 sm:min-w-64"
        role="status"
      >
        {isAskingToLeave ? (
          <p className="font-semibold">
            Leave this page? {pluralize(changedCount, "unsaved change")} will be
            lost.
          </p>
        ) : (
          <>
            <p className="font-semibold">
              {isSaving
                ? `Saving ${pluralize(changedCount, "setting")}`
                : countText}
            </p>
            {lastSave !== null && failedCount > 0 ? (
              <p className="text-tone-danger-text">
                <span aria-hidden="true">{toneMark("danger")} </span>
                {lastSave}. The rows that failed are marked below and still hold
                your value.
              </p>
            ) : null}
            {firstProblem === null ? null : (
              <p className="text-tone-danger-text">
                <span aria-hidden="true">{toneMark("danger")} </span>
                {pluralize(invalidKeys.length, "setting")}{" "}
                {invalidKeys.length === 1 ? "needs" : "need"} fixing before you
                can save. First: {firstProblem}
              </p>
            )}
          </>
        )}
      </div>
      <div className="flex flex-wrap items-center gap-2 sm:justify-end">
        {isAskingToLeave ? (
          <>
            <Button ref={stayRef} variant="secondary" onClick={onStay}>
              Stay on this page
            </Button>
            <Button variant="danger" onClick={onLeave}>
              Leave and discard
            </Button>
          </>
        ) : (
          <>
            <Button variant="secondary" onClick={onDiscard} disabled={isSaving}>
              Discard
            </Button>
            {warnings.length > 0 && invalidKeys.length === 0 ? (
              <ConfirmButton
                label="Save changes"
                variant="primary"
                prompt={warnings.join(" ")}
                confirmLabel={`Yes, save ${pluralize(changedCount, "change")}`}
                confirmVariant="danger-filled"
                isBusy={isSaving}
                busyLabel="Saving"
                onConfirm={onSave}
              />
            ) : (
              <Button
                variant="primary"
                isBusy={isSaving}
                busyLabel="Saving"
                onClick={onSave}
              >
                Save changes
              </Button>
            )}
          </>
        )}
      </div>
    </div>
  );
}
