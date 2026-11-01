// app/global-error.tsx renders no <style> element and no style attribute (#201): either
// would be refused or, for an element, need a nonce the page's CSP gives only to what Next
// renders itself (lib/csp.ts). E2E can't reach this page reliably (only a failing root
// layout shows it), so it is rendered here. Run with `node --test` (Node >= 22.18).
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { test } from "node:test";

import { createElement, type FunctionComponent } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import ts from "typescript";

type Props = { error: unknown; retry: () => void };

/** The page compiled as Next would, without its stylesheet import (only a bundler loads one). */
async function loadGlobalError(): Promise<FunctionComponent<Props>> {
  const source = await readFile(new URL("./global-error.tsx", import.meta.url), "utf8");
  const { outputText } = ts.transpileModule(source.replace(/^import "\.\/globals\.css";$/m, ""), {
    compilerOptions: { jsx: ts.JsxEmit.ReactJSX, module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 },
  });
  // A data: URL module can't resolve bare specifiers: point the JSX runtime at its file.
  const js = outputText.replaceAll('"react/jsx-runtime"', JSON.stringify(import.meta.resolve("react/jsx-runtime")));
  const mod = await import(`data:text/javascript,${encodeURIComponent(js)}`);
  return mod.default;
}

test("the global error page renders without inline styles", async () => {
  const GlobalError = await loadGlobalError();
  const html = renderToStaticMarkup(createElement(GlobalError, { error: new Error("boom"), retry: () => {} }));
  assert.match(html, /^<html lang="en">(<head><\/head>)?<body /, "it replaces the root layout");
  assert.match(html, /Something went wrong/);
  assert.doesNotMatch(html, /<style\b/i, "a <style> element");
  assert.doesNotMatch(html, /\sstyle=/i, "a style attribute");
  assert.doesNotMatch(html, /boom/, "the error's message is not shown");
});
