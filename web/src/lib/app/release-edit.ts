/**
 * Renaming and tagging a release. `PUT /apps/{app}/releases/{id}` replaces the
 * whole value of name and tags (admin-api.md), so the form always sends both.
 */
import { UpdateReleaseRequest } from "@/lib/api";
import type { ReleaseSummary } from "@/lib/api";
import { errorsFromZod, formFailure } from "./forms";
import type { FormErrors, FormResult } from "./forms";

// admin-api.md, UpdateReleaseRequest; lib/api/schemas keeps its copies private,
// and the test checks both edges of each against the schema.
export const RELEASE_NAME_MAX_LENGTH = 80;
export const RELEASE_TAG_MAX_LENGTH = 32;
export const RELEASE_TAGS_MAX = 10;

/** What the inputs hold: text, as typed. Tags are one text, separated by commas or spaces. */
export interface ReleaseEditValues {
  name: string;
  tags: string;
}

export function releaseEditDefaults(
  release: ReleaseSummary,
): ReleaseEditValues {
  return { name: release.name ?? "", tags: release.tags.join(", ") };
}

/** Tags are lower case in the contract, so what is typed is lower-cased; repeats are dropped. */
export function parseTags(text: string): string[] {
  const tags = text
    .split(/[\s,]+/)
    .map((tag) => tag.trim().toLowerCase())
    .filter((tag) => tag !== "");
  return [...new Set(tags)];
}

export function buildReleaseUpdate(
  values: ReleaseEditValues,
): FormResult<UpdateReleaseRequest> {
  const name = values.name.trim();
  const tags = parseTags(values.tags);

  const ownErrors: FormErrors = {};
  if (tags.some((tag) => tag.length > RELEASE_TAG_MAX_LENGTH)) {
    ownErrors.tags = `Each tag has at most ${RELEASE_TAG_MAX_LENGTH} characters.`;
  } else if (tags.length > RELEASE_TAGS_MAX) {
    ownErrors.tags = `At most ${RELEASE_TAGS_MAX} tags.`;
  }

  const parsed = UpdateReleaseRequest.safeParse({
    name: name === "" ? null : name,
    tags,
  });
  if (parsed.success && Object.keys(ownErrors).length === 0) {
    return { ok: true, body: parsed.data };
  }
  const schemaErrors = parsed.success
    ? {}
    : errorsFromZod(parsed.error, { dropIndexes: true });
  return formFailure({ ...schemaErrors, ...ownErrors });
}

/** Whether the form differs from the release, so Save can stay off until it does. */
export function isReleaseEditChanged(
  values: ReleaseEditValues,
  release: ReleaseSummary,
): boolean {
  const name = values.name.trim();
  const tags = parseTags(values.tags);
  return (
    name !== (release.name ?? "") ||
    tags.length !== release.tags.length ||
    tags.some((tag, index) => tag !== release.tags[index])
  );
}
