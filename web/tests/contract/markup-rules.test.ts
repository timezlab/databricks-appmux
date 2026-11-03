/**
 * Static guard for the page rules of design 10.1 and ADR 0012, over the source
 * of every page and component. The edge serves the admin UI under a strict CSP
 * (`script-src 'self'` plus hashes; spike 19 ran with `style-src 'self'`,
 * `connect-src 'self'`, `img-src 'self'`), and the pages show strings from the
 * API. So:
 *  - no inline style attribute, no inline script, no inline event-handler string;
 *  - no raw HTML sink (`dangerouslySetInnerHTML`, `innerHTML`, `document.write`);
 *  - no dynamic code (`eval`, `new Function`, a string given to a timer);
 *  - no `window.confirm`/`alert`/`prompt` (the two-step ConfirmButton replaces them);
 *  - no request of its own (`fetch`, XHR, WebSocket, beacon): `lib/api/client.ts`, the one
 *    file that may call `fetch`, makes every call;
 *  - every `href`/`src` (and the other URL attributes) is a literal path on this
 *    origin, the output of `appLink` or `appPagePath`, or one of the few
 *    reviewed expressions of TRUSTED_EXPRESSIONS: never text built from API data,
 *    never a template or a concatenation, so `javascript:`, `data:` and
 *    cross-origin links cannot be assembled;
 *  - only the layout renders `<main>` (a second landmark splits the page for screen readers).
 * The scan reads the syntax tree, so a comment or a string that names a rule is not a violation.
 */
import assert from "node:assert/strict";
import { readFileSync, readdirSync } from "node:fs";
import path from "node:path";
import { describe, it } from "vitest";

import ts from "typescript";

import { appPagePath } from "@/lib/app/params";
import { launcherAppRows } from "@/lib/launcher/apps";
import { PLATFORM_INBOX_HREF } from "@/lib/launcher/requests";
import { appLink } from "@/lib/slug-link";
import { NAV_ITEMS } from "@/components/nav-items";
import { appNavItems } from "@/lib/app/sections";
import { PLATFORM_PAGES, SETTING_GROUPS } from "@/lib/platform/sections";
import { AppDetail } from "@/lib/api";
import { readExample } from "../support/contract-examples";

const SRC_DIR = path.resolve(import.meta.dirname, "../../src");
/**
 * Pages, components, hooks and the lib: every `.ts` and `.tsx` file counts (a
 * helper next to a component renders too, and a hook or a lib module can make
 * a request or build a URL as well).
 */
const SCANNED_DIRS = ["app", "components", "hooks", "lib"];
/** The one file that may render `<main>`. */
const LAYOUT_FILE = "app/layout.tsx";
/** The one file that may call `fetch`: every request of the admin UI goes through it. */
const CLIENT_FILE = "lib/api/client.ts";

const BANNED_CALLS = new Set([
  "eval",
  "Function",
  "confirm",
  "alert",
  "prompt",
  "fetch",
]);
const BANNED_MEMBER_CALLS = new Set([
  "eval",
  "confirm",
  "alert",
  "prompt",
  "fetch",
  "write",
  "writeln",
  "insertAdjacentHTML",
  "sendBeacon",
]);
const BANNED_CONSTRUCTORS = new Set([
  "Function",
  "XMLHttpRequest",
  "WebSocket",
  "EventSource",
]);
const BANNED_MEMBERS = new Set(["innerHTML", "outerHTML"]);
// `<base href>` would redirect every relative URL of the page.
const BANNED_ELEMENTS = new Set([
  "script",
  "iframe",
  "object",
  "embed",
  "base",
]);
const TIMER_FUNCTIONS = new Set(["setTimeout", "setInterval"]);

// Attributes that hold a URL, compared in lower case. On any element:
const URL_ATTRIBUTES = new Set([
  "href",
  "xlink:href",
  "src",
  "srcset",
  "poster",
  "ping",
  "cite",
  "formaction",
  // AppSidebar's link back to the list.
  "backhref",
]);
// On a plain HTML element only: on a component `action` or `data` is a prop of
// its own (a node to draw, rows to show), not a URL.
const HTML_ONLY_URL_ATTRIBUTES = new Set(["action", "data"]);
// A spread can carry an href or a src past the attribute check above.
const SPREAD_URL_ELEMENTS = new Set([
  "a",
  "area",
  "img",
  "source",
  "video",
  "audio",
  "track",
  "link",
  "form",
  "Link",
]);

const FORBIDDEN_URL = /^\s*(javascript:|data:|vbscript:|https?:|\/\/)/i;
/**
 * A path on this origin: `/` and then anything but a second `/` or a `\`
 * (browsers read `/\evil.example` as `//evil.example`), with no space, tab or
 * control character (browsers drop tabs and newlines inside a URL, so
 * `/<tab>/evil.example` is `//evil.example` too); or an anchor `#name`.
 */
const SAFE_PATH = /^(\/(?![/\\])[^\s\\]*|#[\w-]*)$/;
const LAST_CONTROL_CHARACTER = 0x1f;
const DELETE_CHARACTER = 0x7f;

/**
 * Helpers whose output is always a checked path or `null`, and the module each
 * must be imported from: a path from `src/`, without extension. An import
 * counts when its specifier resolves to exactly that module (see
 * `resolveImportedModule`), so a look-alike file name elsewhere is not trusted.
 */
const TRUSTED_URL_HELPERS: Record<string, string> = {
  appLink: "lib/slug-link",
  appPagePath: "lib/app/params",
};

/**
 * Expressions that are not literals and not helper calls but were reviewed: the
 * file, the exact text, and why it is safe. "vetted sources" below checks that
 * each source really yields only safe paths, and that every entry is still used.
 * A new dynamic `href` is added here on purpose, with its reason, or it fails.
 */
const TRUSTED_EXPRESSIONS: readonly {
  file: string;
  text: string;
  why: string;
}[] = [
  {
    file: "components/main-nav.tsx",
    text: "item.href",
    why: "NAV_ITEMS holds literal paths only",
  },
  {
    file: "components/sub-nav.tsx",
    text: "item.href",
    why: "PLATFORM_PAGES holds literal paths, and appNavItems builds its hrefs with appPagePath",
  },
  {
    file: "components/jump-links.tsx",
    text: "item.href",
    why: "SETTING_GROUPS holds literal #anchors only",
  },
  {
    file: "components/launcher/app-list.tsx",
    text: "row.manageHref",
    why: "launcherAppRows sets it from appPagePath, which checks the slug, or null",
  },
  {
    file: "components/launcher/launcher.tsx",
    text: "PLATFORM_INBOX_HREF",
    why: "a constant path of lib/launcher/requests.ts",
  },
  {
    file: "components/route-strip.tsx",
    text: "href",
    why: "a prop: the scan checks the href each caller passes to <RouteStrip> (appLink(slug))",
  },
  {
    file: "components/app-sidebar.tsx",
    text: "href",
    why: "SidebarItemData.href, a literal path or appPagePath; review each caller's item list as for SubNav",
  },
  {
    file: "components/app-sidebar.tsx",
    text: "backHref",
    why: "a prop: the scan checks the backHref each caller passes to <AppSidebar>",
  },
];

const MAX_CONST_DEPTH = 5;

const URL_RULE_SCRIPT_OR_CROSS_ORIGIN = "script or cross-origin URL";
const URL_RULE_UNVETTED = "URL not a literal path or vetted helper";

interface Violation {
  rule: string;
  line: number;
  detail?: string;
}

interface ScanOptions {
  /** Path from `src/`, with `/`; decides whether `<main>` and the TRUSTED_EXPRESSIONS apply. */
  relativePath?: string;
  /** Receives each TRUSTED_EXPRESSIONS entry the scan relied on. */
  onTrustedExpressionUsed?: (entry: { file: string; text: string }) => void;
}

/** The text of a string literal, in braces or not; `null` for any other expression. */
function literalText(node: ts.Node | undefined): string | null {
  if (node === undefined) return null;
  if (ts.isStringLiteral(node) || ts.isNoSubstitutionTemplateLiteral(node)) {
    return node.text;
  }
  if (ts.isJsxExpression(node)) return literalText(node.expression);
  return null;
}

function hasControlCharacter(text: string): boolean {
  return [...text].some((character) => {
    const code = character.charCodeAt(0);
    return code <= LAST_CONTROL_CHARACTER || code === DELETE_CHARACTER;
  });
}

function isSafePathLiteral(text: string): boolean {
  return (
    SAFE_PATH.test(text) &&
    !hasControlCharacter(text) &&
    !FORBIDDEN_URL.test(text)
  );
}

/** `(x)` and `x as const` are `x`. */
function unwrap(node: ts.Expression, file: ts.SourceFile): ts.Expression {
  let current = node;
  for (;;) {
    if (ts.isParenthesizedExpression(current)) {
      current = current.expression;
    } else if (
      ts.isAsExpression(current) &&
      current.type.getText(file) === "const"
    ) {
      current = current.expression;
    } else {
      return current;
    }
  }
}

/** The module `localName` is imported from as a named import without renaming, or `null`. */
function importedFrom(file: ts.SourceFile, localName: string): string | null {
  for (const statement of file.statements) {
    if (!ts.isImportDeclaration(statement)) continue;
    const bindings = statement.importClause?.namedBindings;
    if (bindings === undefined || !ts.isNamedImports(bindings)) continue;
    for (const element of bindings.elements) {
      const importedName = (element.propertyName ?? element.name).text;
      if (element.name.text === localName && importedName === localName) {
        return ts.isStringLiteral(statement.moduleSpecifier)
          ? statement.moduleSpecifier.text
          : null;
      }
    }
  }
  return null;
}

/**
 * The module an import specifier names, as a path from `src/` without
 * extension: `@/lib/slug-link` and `@/lib/slug-link.ts` are `lib/slug-link`;
 * a relative specifier is resolved against the importing file. `null` for a
 * package, or for a relative specifier that leaves `src/`.
 */
function resolveImportedModule(
  specifier: string,
  importerPath: string,
): string | null {
  const withoutExtension = specifier.replace(/\.tsx?$/, "");
  if (withoutExtension.startsWith("@/")) return withoutExtension.slice(2);
  if (!withoutExtension.startsWith(".")) return null;
  const resolved = path.posix.join(
    path.posix.dirname(importerPath),
    withoutExtension,
  );
  return resolved.startsWith("..") ? null : resolved;
}

/**
 * The initializer of `name` when the file declares it exactly once and as a
 * `const`; `null` for a parameter, an import, a destructured name, a `let`, or
 * a name declared twice in different scopes (the scan does not follow scopes).
 */
function constInitializer(
  file: ts.SourceFile,
  name: string,
): ts.Expression | null {
  const declarations: ts.Node[] = [];
  const collect = (node: ts.Node): void => {
    const isNamed =
      ((ts.isVariableDeclaration(node) ||
        ts.isParameter(node) ||
        ts.isBindingElement(node)) &&
        ts.isIdentifier(node.name) &&
        node.name.text === name) ||
      ((ts.isFunctionDeclaration(node) || ts.isClassDeclaration(node)) &&
        node.name?.text === name) ||
      (ts.isImportSpecifier(node) && node.name.text === name) ||
      (ts.isImportClause(node) && node.name?.text === name) ||
      (ts.isNamespaceImport(node) && node.name.text === name);
    if (isNamed) declarations.push(node);
    ts.forEachChild(node, collect);
  };
  collect(file);

  const [only] = declarations;
  if (declarations.length !== 1 || only === undefined) return null;
  if (!ts.isVariableDeclaration(only) || only.initializer === undefined) {
    return null;
  }
  const isConst = (only.parent.flags & ts.NodeFlags.Const) !== 0;
  return isConst ? only.initializer : null;
}

/** The rule a URL value breaks, or `null` when it is safe. */
function urlProblem(
  node: ts.Expression,
  file: ts.SourceFile,
  options: ScanOptions,
  depth = 0,
): { rule: string; detail: string } | null {
  const expression = unwrap(node, file);
  const text = expression.getText(file);
  const unvetted = (detail: string) => ({
    rule: URL_RULE_UNVETTED,
    detail,
  });

  if (
    ts.isStringLiteral(expression) ||
    ts.isNoSubstitutionTemplateLiteral(expression)
  ) {
    if (FORBIDDEN_URL.test(expression.text)) {
      return {
        rule: URL_RULE_SCRIPT_OR_CROSS_ORIGIN,
        detail: expression.getText(file),
      };
    }
    return isSafePathLiteral(expression.text)
      ? null
      : unvetted(`${text} is not a path on this origin`);
  }
  // No attribute at all is safe.
  if (
    expression.kind === ts.SyntaxKind.NullKeyword ||
    (ts.isIdentifier(expression) && expression.text === "undefined")
  ) {
    return null;
  }
  if (ts.isConditionalExpression(expression)) {
    return (
      urlProblem(expression.whenTrue, file, options, depth) ??
      urlProblem(expression.whenFalse, file, options, depth)
    );
  }
  if (ts.isCallExpression(expression)) {
    const callee = expression.expression;
    if (ts.isIdentifier(callee)) {
      const module = Object.hasOwn(TRUSTED_URL_HELPERS, callee.text)
        ? TRUSTED_URL_HELPERS[callee.text]
        : undefined;
      const source = importedFrom(file, callee.text);
      if (
        module !== undefined &&
        source !== null &&
        resolveImportedModule(source, options.relativePath ?? "fixture.tsx") ===
          module
      ) {
        return null;
      }
    }
    return unvetted(
      `${text} is not a call of a vetted helper (${Object.keys(TRUSTED_URL_HELPERS).join(", ")}) imported from its module`,
    );
  }

  const relativePath = options.relativePath;
  const trusted = TRUSTED_EXPRESSIONS.find(
    (entry) => entry.file === relativePath && entry.text === text,
  );
  if (trusted !== undefined) {
    options.onTrustedExpressionUsed?.(trusted);
    return null;
  }
  if (ts.isIdentifier(expression) && depth < MAX_CONST_DEPTH) {
    const initializer = constInitializer(file, expression.text);
    if (initializer !== null) {
      return urlProblem(initializer, file, options, depth + 1);
    }
  }
  return unvetted(
    `${text} is neither a literal path, a vetted helper call, a const of those, nor in TRUSTED_EXPRESSIONS`,
  );
}

function elementOf(attribute: ts.Node): ts.JsxOpeningLikeElement | null {
  const element = attribute.parent.parent;
  return ts.isJsxOpeningElement(element) || ts.isJsxSelfClosingElement(element)
    ? element
    : null;
}

function findViolations(
  source: string,
  options: ScanOptions = {},
): Violation[] {
  const file = ts.createSourceFile(
    options.relativePath ?? "fixture.tsx",
    source,
    ts.ScriptTarget.Latest,
    true,
    ts.ScriptKind.TSX,
  );
  const mayRenderMain = options.relativePath === LAYOUT_FILE;
  const mayCallFetch = options.relativePath === CLIENT_FILE;
  const found: Violation[] = [];
  const report = (node: ts.Node, rule: string, detail?: string) => {
    const { line } = file.getLineAndCharacterOfPosition(node.getStart(file));
    found.push(
      detail === undefined
        ? { rule, line: line + 1 }
        : { rule, line: line + 1, detail },
    );
  };

  const visit = (node: ts.Node): void => {
    if (ts.isJsxAttribute(node)) {
      const name = node.name.getText(file);
      const lowerName = name.toLowerCase();
      if (name === "style") report(node, "style attribute");
      if (name === "dangerouslySetInnerHTML") report(node, "raw HTML sink");
      if (/^on/i.test(name) && literalText(node.initializer) !== null) {
        report(node, "inline event handler string");
      }
      if (name === "role" && literalText(node.initializer) === "main") {
        report(node, 'role="main" (only the layout has a main landmark)');
      }

      const tag = elementOf(node)?.tagName.getText(file) ?? "";
      const isHtmlElement = /^[a-z]/.test(tag);
      if (
        URL_ATTRIBUTES.has(lowerName) ||
        (isHtmlElement && HTML_ONLY_URL_ATTRIBUTES.has(lowerName))
      ) {
        const value =
          node.initializer !== undefined && ts.isJsxExpression(node.initializer)
            ? node.initializer.expression
            : node.initializer;
        const problem =
          value === undefined
            ? {
                rule: URL_RULE_UNVETTED,
                detail: `${name} has no value`,
              }
            : urlProblem(value, file, options);
        if (problem !== null) report(node, problem.rule, problem.detail);
      }
    } else if (ts.isJsxSpreadAttribute(node)) {
      const tag = elementOf(node)?.tagName.getText(file) ?? "";
      if (SPREAD_URL_ELEMENTS.has(tag)) {
        report(
          node,
          "spread on an element with URL attributes",
          `{...} on <${tag}> can carry an href or src; write the attributes out`,
        );
      }
    } else if (
      ts.isJsxOpeningElement(node) ||
      ts.isJsxSelfClosingElement(node)
    ) {
      const tag = node.tagName.getText(file);
      if (BANNED_ELEMENTS.has(tag)) report(node, `<${tag}> element`);
      if (tag === "main" && !mayRenderMain) {
        report(node, "<main> element outside the layout");
      }
    } else if (ts.isCallExpression(node)) {
      const callee = node.expression;
      if (ts.isIdentifier(callee)) {
        if (
          BANNED_CALLS.has(callee.text) &&
          !(callee.text === "fetch" && mayCallFetch)
        ) {
          report(node, `${callee.text}()`);
        }
        if (
          TIMER_FUNCTIONS.has(callee.text) &&
          literalText(node.arguments[0]) !== null
        ) {
          report(node, "code string given to a timer");
        }
      } else if (
        ts.isPropertyAccessExpression(callee) &&
        BANNED_MEMBER_CALLS.has(callee.name.text)
      ) {
        report(node, `.${callee.name.text}()`);
      }
    } else if (ts.isNewExpression(node)) {
      const callee = node.expression;
      if (ts.isIdentifier(callee) && BANNED_CONSTRUCTORS.has(callee.text)) {
        report(node, `new ${callee.text}`);
      }
    } else if (
      ts.isPropertyAccessExpression(node) &&
      BANNED_MEMBERS.has(node.name.text)
    ) {
      report(node, `.${node.name.text}`);
    }
    ts.forEachChild(node, visit);
  };
  visit(file);
  return found;
}

function sourceFiles(
  dir: string,
  isWanted: (name: string) => boolean,
): string[] {
  return readdirSync(dir, { withFileTypes: true }).flatMap((entry) => {
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) return sourceFiles(full, isWanted);
    return isWanted(entry.name) ? [full] : [];
  });
}

const isSourceFile = (name: string) => /\.tsx?$/.test(name);
const isComponentFile = (name: string) =>
  name.endsWith(".tsx") && !name.endsWith(".test.tsx");

/** The scanned directories, plus any other `.tsx` file of `src/` (a component under another folder draws too). */
const FILES = [
  ...new Set([
    ...SCANNED_DIRS.flatMap((dir) =>
      sourceFiles(path.join(SRC_DIR, dir), isSourceFile),
    ),
    ...sourceFiles(SRC_DIR, isComponentFile),
  ]),
].sort();

function relativeSourcePath(file: string): string {
  return path.relative(SRC_DIR, file).split(path.sep).join("/");
}

describe("the guard itself (each rule is caught, nothing else is)", () => {
  const bad: [rule: string, code: string][] = [
    ["style attribute", `const a = <div style={{ width: 3 }} />;`],
    [
      "raw HTML sink",
      `const a = <div dangerouslySetInnerHTML={{ __html: x }} />;`,
    ],
    ["inline event handler string", `const a = <button onClick="go()" />;`],
    ["inline event handler string", `const a = <button onclick={"go()"} />;`],
    [
      URL_RULE_SCRIPT_OR_CROSS_ORIGIN,
      `const a = <a href="javascript:alert(1)">x</a>;`,
    ],
    [
      URL_RULE_SCRIPT_OR_CROSS_ORIGIN,
      `const a = <a href="https://evil.example/">x</a>;`,
    ],
    [
      URL_RULE_SCRIPT_OR_CROSS_ORIGIN,
      `const a = <img src="//evil.example/x.png" />;`,
    ],
    [
      URL_RULE_SCRIPT_OR_CROSS_ORIGIN,
      `const a = <a href={"data:text/html,x"}>x</a>;`,
    ],
    [
      URL_RULE_SCRIPT_OR_CROSS_ORIGIN,
      `const a = <a href={\`javascript:alert(1)\`}>x</a>;`,
    ],
    ["<script> element", `const a = <script>run()</script>;`],
    ["<iframe> element", `const a = <iframe title="t" />;`],
    ["<base> element", `const a = <base href="/x/" />;`],
    ["<main> element outside the layout", `const a = <main>x</main>;`],
    ["<main> element outside the layout", `const a = <main />;`],
    [
      'role="main" (only the layout has a main landmark)',
      `const a = <div role="main">x</div>;`,
    ],
    ["eval()", `eval("1");`],
    ["Function()", `Function("return 1")();`],
    ["new Function", `const f = new Function("return 1");`],
    ["confirm()", `if (confirm("sure?")) go();`],
    [".confirm()", `if (window.confirm("sure?")) go();`],
    [".alert()", `window.alert("hi");`],
    ["fetch()", `void fetch("/_appmux/api/v1/me");`],
    [".fetch()", `void window.fetch("/x");`],
    ["new XMLHttpRequest", `const x = new XMLHttpRequest();`],
    ["new WebSocket", `const s = new WebSocket("wss://x");`],
    [".sendBeacon()", `navigator.sendBeacon("/x");`],
    [".write()", `document.write("<b>x</b>");`],
    [".innerHTML", `el.innerHTML = x;`],
    [".insertAdjacentHTML()", `el.insertAdjacentHTML("beforeend", x);`],
    ["code string given to a timer", `setTimeout("go()", 10);`],
  ];
  for (const [rule, code] of bad) {
    it(`catches ${rule}: ${code}`, () => {
      assert.ok(
        findViolations(code).some((violation) => violation.rule === rule),
        `expected ${rule}; got ${JSON.stringify(findViolations(code))}`,
      );
    });
  }

  // Every way to put something other than a checked path into a URL attribute.
  // Each of these passed the earlier guard, which looked at string literals only.
  const unvettedUrls: [why: string, code: string][] = [
    [
      "a value from props or API data",
      `function F({ url }) { return <a href={url}>x</a>; }`,
    ],
    ["a member of API data", `const a = <a href={app.url}>x</a>;`],
    [
      "a template literal with a slug",
      "const a = <a href={`/${slug}/`}>x</a>;",
    ],
    [
      "a template literal that builds javascript:",
      "const a = <a href={`javascript:${slug}`}>x</a>;",
    ],
    [
      "a concatenation that builds javascript:",
      `const a = <a href={"java" + "script:alert(1)"}>x</a>;`,
    ],
    [
      "a concatenation of a path and a slug",
      `const a = <a href={"/" + slug + "/"}>x</a>;`,
    ],
    [
      "a const that is a template literal",
      "const href = `/${slug}/`;\nconst a = <a href={href}>x</a>;",
    ],
    [
      "a const that is a chain ending in a parameter",
      "function F({ url }) { const first = url; const second = first; return <a href={second}>x</a>; }",
    ],
    ["a let variable", `let href = "/a/";\nconst a = <a href={href}>x</a>;`],
    [
      "appLink that is not imported from slug-link",
      `import { appLink } from "./elsewhere.ts";\nconst a = <a href={appLink(slug)}>x</a>;`,
    ],
    [
      "appLink written in the file itself",
      `function appLink(s) { return s; }\nconst a = <a href={appLink(slug)}>x</a>;`,
    ],
    [
      "appLink imported under another name",
      `import { other as appLink } from "@/lib/slug-link";\nconst a = <a href={appLink(slug)}>x</a>;`,
    ],
    [
      "appLink imported from a look-alike alias path",
      `import { appLink } from "@/lib/evil/slug-link";\nconst a = <a href={appLink(slug)}>x</a>;`,
    ],
    [
      "appLink imported from a look-alike relative path",
      `import { appLink } from "./evil/slug-link.ts";\nconst a = <a href={appLink(slug)}>x</a>;`,
    ],
    [
      "a member call named like a helper",
      `const a = <a href={api.appLink(slug)}>x</a>;`,
    ],
    [
      "a call of any other function",
      `const a = <a href={buildUrl(slug)}>x</a>;`,
    ],
    [
      "a branch of a conditional",
      `const a = <a href={ok ? "/a/" : url}>x</a>;`,
    ],
    ["a next/link href from data", `const a = <Link href={row.href}>x</Link>;`],
    ["an image source from data", `const a = <img src={item.icon} alt="" />;`],
    [
      "an image source built from an id",
      'const a = <img src={`/x/${id}.png`} alt="" />;',
    ],
    ["a srcSet from data", `const a = <img srcSet={set} alt="" />;`],
    [
      "a form action that is a function",
      `const a = <form action={submit}>x</form>;`,
    ],
    [
      "a form action built from an id",
      `const a = <form action={"/x/" + id} />;`,
    ],
    [
      "a formAction on a component",
      `const a = <Button formAction={url}>x</Button>;`,
    ],
    ["an attribute without a value", `const a = <a href>x</a>;`],
    [
      "a path with a backslash (read as //)",
      `const a = <a href={"/\\\\evil.example"}>x</a>;`,
    ],
    [
      "a path with a tab (the tab is dropped, giving //)",
      `const a = <a href={"/\\t/evil.example"}>x</a>;`,
    ],
    ["a path with a space", `const a = <a href="/a b/">x</a>;`],
    ["a relative path", `const a = <a href="sales/">x</a>;`],
    ["an empty value", `const a = <a href="">x</a>;`],
    [
      "an expression that is a literal in disguise",
      `const a = <a href={x as string}>x</a>;`,
    ],
    [
      "an item.href outside the file that was reviewed",
      `const a = <Link href={item.href}>x</Link>;`,
    ],
  ];
  for (const [why, code] of unvettedUrls) {
    it(`rejects a URL attribute with ${why}`, () => {
      const rules = findViolations(code).map((violation) => violation.rule);
      assert.ok(
        rules.includes(URL_RULE_UNVETTED) ||
          rules.includes(URL_RULE_SCRIPT_OR_CROSS_ORIGIN),
        `expected a URL violation; got ${JSON.stringify(rules)}`,
      );
    });
  }

  const spreads: [string, string][] = [
    ["a", `const a = <a {...props}>x</a>;`],
    ["img", `const a = <img {...props} alt="" />;`],
    ["form", `const a = <form {...props}>x</form>;`],
    ["Link", `const a = <Link {...props}>x</Link>;`],
  ];
  for (const [tag, code] of spreads) {
    it(`rejects a spread on <${tag}>, which can carry an href`, () => {
      assert.ok(
        findViolations(code).some(
          (violation) =>
            violation.rule === "spread on an element with URL attributes",
        ),
      );
    });
  }

  it("lets a reviewed expression through only in the file it was reviewed for", () => {
    const code = `const a = <Link href={item.href}>x</Link>;`;
    assert.deepEqual(
      findViolations(code, { relativePath: "components/main-nav.tsx" }),
      [],
    );
    assert.equal(
      findViolations(code, { relativePath: "components/other.tsx" }).length,
      1,
    );
  });

  it("lets <main> through only in the layout", () => {
    const code = `const a = <main id="main">x</main>;`;
    assert.deepEqual(findViolations(code, { relativePath: LAYOUT_FILE }), []);
    assert.equal(
      findViolations(code, { relativePath: "app/page.tsx" }).length,
      1,
    );
  });

  it("lets fetch() through only in the client, and no other request API", () => {
    const code = `void fetch("/_appmux/api/v1/me");`;
    assert.deepEqual(findViolations(code, { relativePath: CLIENT_FILE }), []);
    assert.equal(
      findViolations(code, { relativePath: "lib/api/apps.ts" }).length,
      1,
    );
    for (const other of [
      `void window.fetch("/x");`,
      `const s = new WebSocket("wss://x");`,
      `navigator.sendBeacon("/x");`,
    ]) {
      assert.equal(
        findViolations(other, { relativePath: CLIENT_FILE }).length,
        1,
        other,
      );
    }
  });

  it("reports the line and says what to do for a rejected URL", () => {
    const [violation] = findViolations(
      "const x = 1;\nconst a = <a href={url}>x</a>;",
    );
    assert.equal(violation?.line, 2);
    assert.match(violation?.detail ?? "", /url/);
  });

  it("lets through what the rules allow, and words in comments and strings", () => {
    const clean = `
      import { appLink } from "@/lib/slug-link";
      import { appPagePath } from "@/lib/app/params";
      // never use dangerouslySetInnerHTML, innerHTML, eval or window.confirm
      /* style={{}} fetch("x") <main> href={url} */
      const note = "style={{}} eval( fetch( <script> <main> javascript:";
      const SUPPORT_PATH = "/support/";
      const href = appLink(slug);
      const a = (
        <a href="/sales/" data-tone="info" className="am-button" onClick={go}>
          {note}
        </a>
      );
      const b = <input aria-invalid="true" value={v} onChange={(e) => set(e.target.value)} />;
      const c = <a href="#main">skip</a>;
      const d = <Link href="/platform/#inbox">inbox</Link>;
      const e = <a href={appLink(slug)}>open</a>;
      const f = <a href={href}>open</a>;
      const h = <a href={ok ? "/a/" : "/b/"}>x</a>;
      const i = <a href={SUPPORT_PATH}>x</a>;
      const j = <a href={("/a/" as const)}>x</a>;
      const k = <a href={ok ? "/a/" : undefined}>x</a>;
      const l = <input {...control} />;
      const m = <form onSubmit={submit}>x</form>;
      const n = <Button {...rest}>x</Button>;
      const o = <EmptyState action={<Link href="/">Back</Link>} />;
      const p = <Table data={rows} />;
      setTimeout(() => go(), 10);
    `;
    assert.deepEqual(findViolations(clean), []);
  });

  it("does not accept a nullish fallback, whose right side could be anything", () => {
    const code = `
      import { appPagePath } from "@/lib/app/params";
      const g = <Link href={appPagePath(slug) ?? other}>x</Link>;
    `;
    assert.equal(findViolations(code).length, 1);
  });

  // The module is matched by what the specifier resolves to, so each spelling
  // of the same file is accepted and a file that only looks like it is not.
  const trustedSpellings: [
    helper: string,
    importer: string,
    specifier: string,
  ][] = [
    ["appLink", "components/app-link.tsx", "@/lib/slug-link"],
    ["appLink", "components/app-link.tsx", "@/lib/slug-link.ts"],
    ["appLink", "components/app-link.tsx", "../lib/slug-link"],
    ["appLink", "components/app-link.tsx", "../lib/slug-link.ts"],
    ["appLink", "lib/launcher/open.tsx", "../slug-link"],
    ["appPagePath", "components/app-link.tsx", "@/lib/app/params"],
    ["appPagePath", "components/app-link.tsx", "../lib/app/params.ts"],
    ["appPagePath", "lib/launcher/apps.tsx", "../app/params"],
  ];
  for (const [helper, importer, specifier] of trustedSpellings) {
    it(`trusts ${helper} imported as "${specifier}" from ${importer}`, () => {
      const code = `import { ${helper} } from "${specifier}";\nconst a = <Link href={${helper}(slug)}>x</Link>;`;
      assert.deepEqual(findViolations(code, { relativePath: importer }), []);
    });
  }
});

describe("pages and components follow the rules", () => {
  it("finds the sources to scan", () => {
    assert.ok(FILES.length > 0);
    assert.ok(FILES.some((file) => file.endsWith("layout.tsx")));
    assert.ok(
      FILES.some(
        (file) => relativeSourcePath(file) === "components/app-link.tsx",
      ),
    );
  });

  const usedTrusted = new Set<string>();
  for (const file of FILES) {
    const relative = relativeSourcePath(file);
    it(relative, () => {
      const violations = findViolations(readFileSync(file, "utf8"), {
        relativePath: relative,
        onTrustedExpressionUsed: (entry) => {
          usedTrusted.add(`${entry.file}\n${entry.text}`);
        },
      });
      assert.deepEqual(
        violations,
        [],
        `${relative}: ${violations
          .map(
            (v) =>
              `line ${v.line}: ${v.rule}${v.detail === undefined ? "" : ` (${v.detail})`}`,
          )
          .join("; ")}`,
      );
    });
  }

  it("relies on every reviewed expression it lists, so a stale entry cannot linger", () => {
    const unused = TRUSTED_EXPRESSIONS.filter(
      (entry) => !usedTrusted.has(`${entry.file}\n${entry.text}`),
    ).map((entry) => `${entry.file}: ${entry.text}`);
    assert.deepEqual(unused, []);
  });

  it("the layout does render the one <main>, so the rule is not vacuous", () => {
    const layout = FILES.find(
      (file) => relativeSourcePath(file) === LAYOUT_FILE,
    );
    assert.ok(layout !== undefined);
    assert.match(readFileSync(layout, "utf8"), /<main\b/);
  });

  it("the client does call fetch, so its exemption is not vacuous", () => {
    const client = FILES.find(
      (file) => relativeSourcePath(file) === CLIENT_FILE,
    );
    assert.ok(client !== undefined);
    assert.match(readFileSync(client, "utf8"), /\bfetch\(/);
  });
});

describe("the sources of the reviewed expressions are safe", () => {
  const hostileSlugs = [
    "../admin",
    "//evil.example",
    "javascript:alert(1)",
    "_appmux",
    "sales/../x",
    "Sales",
    "sales\n",
    "",
    "a b",
    "sales?x=1",
  ];

  it("every NAV_ITEMS href is a safe path (main-nav.tsx: item.href)", () => {
    assert.ok(NAV_ITEMS.length > 0);
    for (const item of NAV_ITEMS) {
      assert.ok(isSafePathLiteral(item.href), item.href);
    }
  });

  it("every platform tab is a safe path (sub-nav.tsx: item.href)", () => {
    assert.ok(PLATFORM_PAGES.length > 0);
    for (const page of PLATFORM_PAGES) {
      assert.ok(isSafePathLiteral(page.href), page.href);
    }
  });

  it("every tab of an app is a safe path, and a hostile slug leaves no tab (sub-nav.tsx: item.href)", () => {
    const app = AppDetail.parse(readExample("AppDetail"));
    const items = appNavItems(app, []);
    assert.ok(items.length > 0);
    for (const item of items) {
      assert.ok(isSafePathLiteral(item.href), item.href);
    }
    for (const hostile of hostileSlugs) {
      assert.deepEqual(appNavItems({ ...app, slug: hostile }, []), [], hostile);
    }
  });

  it("every settings jump link is a plain #anchor (jump-links.tsx: item.href)", () => {
    assert.ok(SETTING_GROUPS.length > 0);
    for (const group of SETTING_GROUPS) {
      assert.match(group.href, /^#[\w-]+$/);
      assert.ok(isSafePathLiteral(group.href), group.href);
    }
  });

  it("PLATFORM_INBOX_HREF is a safe path (launcher.tsx)", () => {
    assert.ok(isSafePathLiteral(PLATFORM_INBOX_HREF), PLATFORM_INBOX_HREF);
  });

  it("appPagePath gives a safe path for a slug and null for anything else", () => {
    const link = appPagePath("sales");
    assert.ok(link !== null && isSafePathLiteral(link), String(link));
    for (const hostile of hostileSlugs) {
      assert.equal(appPagePath(hostile), null, JSON.stringify(hostile));
    }
  });

  it("the launcher's manageHref is a safe path or null, whatever slug the API sent (app-list.tsx: row.manageHref)", () => {
    const rowFor = (slug: string, permission: "CAN_MANAGE" | "CAN_USE") => {
      const [row] = launcherAppRows([
        {
          slug,
          display_name: "Sales",
          state: "running",
          permission,
          open_modes: [],
        },
      ]);
      return row;
    };
    const href = rowFor("sales", "CAN_MANAGE")?.manageHref ?? null;
    assert.ok(href !== null && isSafePathLiteral(href), String(href));
    assert.equal(rowFor("sales", "CAN_USE")?.manageHref, null);
    for (const hostile of hostileSlugs) {
      assert.equal(
        rowFor(hostile, "CAN_MANAGE")?.manageHref,
        null,
        JSON.stringify(hostile),
      );
    }
  });

  it("appLink gives a safe path for a slug and null for anything else", () => {
    const link = appLink("sales");
    assert.ok(link !== null && isSafePathLiteral(link), String(link));
    for (const hostile of hostileSlugs) {
      assert.equal(appLink(hostile), null, JSON.stringify(hostile));
    }
  });

  it("the path rule itself refuses what a browser reads as another origin", () => {
    for (const bad of [
      "//evil.example",
      "/\\evil.example",
      "/\t/evil.example",
      "/\n/evil.example",
      "/\u0001/x",
      "/\u007f",
      "https://evil.example/",
      "javascript:alert(1)",
      "data:text/html,x",
      "sales/",
      "",
      " /a/",
      "/a b/",
    ]) {
      assert.equal(isSafePathLiteral(bad), false, JSON.stringify(bad));
    }
    for (const good of [
      "/",
      "/sales/",
      "/app/?slug=sales",
      "/platform/#inbox",
      "#main",
      "#",
    ]) {
      assert.equal(isSafePathLiteral(good), true, good);
    }
  });
});
