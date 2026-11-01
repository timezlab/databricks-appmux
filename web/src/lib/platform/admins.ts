/**
 * Platform admins on the platform page (admin-api `PUT /platform/admins`,
 * data model 4.1 `platform_admins`). The route replaces the whole list and
 * refuses a list with no `user` entry, so every change here is the current list
 * plus or minus one entry, checked before it is sent.
 *
 * The list has no version (last write wins): two admins editing it at once can
 * overwrite each other. The page polls the list every few seconds and builds
 * each change from the latest one, which narrows the window; it cannot close it.
 */
import { PutAdminsRequest } from "@/lib/api/schemas";
import type { Principal } from "@/lib/api/schemas";
import { FORM_ERROR_KEY, issueMessage } from "./forms";
import type { FormParse } from "./forms";

const LAST_USER_MESSAGE = "At least one user must remain a platform admin.";

const TYPE_LABEL: Record<Principal["type"], string> = {
  user: "User",
  group: "Group",
  everyone: "Everyone",
};

export interface AdminViewer {
  email: string;
  groups: readonly string[];
}

export interface AdminRow {
  /** Unique within the list, for a React key. */
  key: string;
  type: Principal["type"];
  typeLabel: string;
  name: string;
  isYou: boolean;
  canRemove: boolean;
  /** Why `canRemove` is false. */
  blockedReason: string | null;
  /** Removing this entry would leave the signed-in person without admin access. */
  losesYourAccess: boolean;
}

function sameEmail(first: string, second: string): boolean {
  return first.toLowerCase() === second.toLowerCase();
}

function holdsAccess(
  admins: readonly Principal[],
  viewer: AdminViewer,
): boolean {
  return admins.some(
    (admin) =>
      (admin.type === "user" && sameEmail(admin.name, viewer.email)) ||
      (admin.type === "group" && viewer.groups.includes(admin.name)),
  );
}

function userCount(admins: readonly Principal[]): number {
  return admins.filter((admin) => admin.type === "user").length;
}

export function adminRows(
  admins: readonly Principal[],
  viewer: AdminViewer,
): AdminRow[] {
  const users = userCount(admins);
  return admins.map((admin, index) => {
    const isLastUser = admin.type === "user" && users <= 1;
    const remaining = admins.filter((_, other) => other !== index);
    return {
      key: `${admin.type}:${admin.name}`,
      type: admin.type,
      typeLabel: TYPE_LABEL[admin.type],
      name: admin.name,
      isYou: admin.type === "user" && sameEmail(admin.name, viewer.email),
      canRemove: !isLastUser,
      blockedReason: isLastUser ? LAST_USER_MESSAGE : null,
      losesYourAccess: !holdsAccess(remaining, viewer),
    };
  });
}

export type AdminChange =
  | { kind: "add"; type: "user" | "group"; name: string }
  | { kind: "remove"; type: Principal["type"]; name: string };

function isSameEntry(admin: Principal, type: string, name: string): boolean {
  return admin.type === type && admin.name === name;
}

/** The body of `PUT /platform/admins`: `current` with `change` applied, and the reason. */
export function parseAdminChange(
  current: readonly Principal[],
  change: AdminChange,
  reason: string,
): FormParse<PutAdminsRequest> {
  const name = change.name.trim();

  let next: Principal[];
  let newIndex = -1;
  // The server compares type and lower-cased name, so a group typed in another case is listed already.
  const isAlreadyListed =
    change.kind === "add" &&
    current.some(
      (admin) =>
        admin.type === change.type &&
        admin.name.toLowerCase() === name.toLowerCase(),
    );
  if (change.kind === "add") {
    // An entry that is listed already changes nothing: the rest of the form is still checked.
    next = isAlreadyListed
      ? [...current]
      : [...current, { type: change.type, name }];
    newIndex = isAlreadyListed ? -1 : next.length - 1;
  } else {
    if (!current.some((admin) => isSameEntry(admin, change.type, name))) {
      return {
        ok: false,
        errors: {
          [FORM_ERROR_KEY]: `${name} is no longer in the list; the admins changed, so reload the page.`,
        },
      };
    }
    next = current.filter((admin) => !isSameEntry(admin, change.type, name));
    if (userCount(next) === 0) {
      return { ok: false, errors: { [FORM_ERROR_KEY]: LAST_USER_MESSAGE } };
    }
  }

  if (next.some((admin) => admin.type === "everyone")) {
    return {
      ok: false,
      errors: {
        [FORM_ERROR_KEY]:
          "The list holds an 'everyone' entry, which a platform admin list cannot hold. Remove that entry first.",
      },
    };
  }

  const errors: Record<string, string> = {};
  const parsed = PutAdminsRequest.safeParse({
    admins: next,
    reason: reason.trim(),
  });
  if (!parsed.success) {
    for (const issue of parsed.error.issues) {
      const [first, second] = issue.path;
      let key: string = FORM_ERROR_KEY;
      if (first === "reason") key = "reason";
      else if (first === "admins" && second === newIndex) key = "name";
      errors[key] ??= issueMessage(issue);
    }
  }
  if (isAlreadyListed) {
    errors.name = "is already a platform admin";
  }

  if (!parsed.success || Object.keys(errors).length > 0) {
    return { ok: false, errors };
  }
  return { ok: true, value: parsed.data };
}
