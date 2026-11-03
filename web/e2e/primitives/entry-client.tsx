// Hydrates the HTML build.ts prerendered. A hydration mismatch goes to the
// console as an error, where csp.spec.ts counts it.
import { hydrateRoot } from "react-dom/client";

import "./primitives.css";
import { PrimitivesPage } from "./primitives-page.tsx";

const root = document.getElementById("root");
if (root === null) throw new Error("the primitives page has no #root");

hydrateRoot(root, <PrimitivesPage />, {
  onRecoverableError(error) {
    console.error("The primitives page did not hydrate cleanly:", error);
  },
});
