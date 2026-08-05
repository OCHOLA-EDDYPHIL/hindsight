import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");
const styles = fs.readFileSync(path.join(root, "src/styles.css"), "utf8");
const visibleSources = [
  "src/App.tsx",
  "src/components/cockpit.tsx",
  "src/components/operator-console.tsx",
  "src/components/safe-markdown.tsx",
].map((file) => fs.readFileSync(path.join(root, file), "utf8"));

describe("responsive and motion design contracts", () => {
  it("defines explicit mobile collapse and overflow containment", () => {
    expect(styles).toContain("overflow-x: hidden");
    expect(styles).toContain("@media (max-width: 800px)");
    expect(styles).toMatch(/\.outcome-grid,[\s\S]*grid-template-columns: minmax\(0, 1fr\)/);
    expect(styles).toMatch(/\.operator-walkthrough \{[\s\S]*grid-template-columns: minmax\(0, 1fr\)/);
    expect(styles).toContain("overflow-wrap: anywhere");
  });

  it("removes chronology motion for reduced-motion users", () => {
    expect(styles).toContain("@media (prefers-reduced-motion: reduce)");
    expect(styles).toContain("animation-duration: 0.01ms !important");
    expect(styles).toMatch(/\.causal-rail li \{[\s\S]*opacity: 1;[\s\S]*transform: none;/);
  });

  it("keeps visible product copy free of decorative long dashes", () => {
    for (const source of visibleSources) {
      expect(source).not.toContain("—");
      expect(source).not.toContain("–");
    }
  });
});
