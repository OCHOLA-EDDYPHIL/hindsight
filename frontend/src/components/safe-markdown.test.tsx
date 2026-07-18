import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { SafeMarkdown, safeMarkdownUrl } from "@/components/safe-markdown";

describe("SafeMarkdown", () => {
  it("renders model formatting as semantic cockpit content", () => {
    render(
      <SafeMarkdown>{`## Verify first

- Inspect **queue depth**
- Compare \`retry_rate\`

\`\`\`text
hold scaling
\`\`\``}</SafeMarkdown>,
    );

    expect(screen.getByRole("heading", { name: "Verify first", level: 4 })).toBeVisible();
    expect(screen.getByRole("list")).toBeVisible();
    expect(screen.getByText("queue depth").tagName).toBe("STRONG");
    expect(screen.getByText("retry_rate").tagName).toBe("CODE");
    expect(screen.getByText("hold scaling").tagName).toBe("CODE");
  });

  it("does not create raw HTML, remote images, or task inputs", () => {
    const { container } = render(
      <SafeMarkdown>{`<script>window.bad = true</script>

<img src="https://unsafe.example/pixel" onerror="window.bad = true">

![diagnostic chart](https://unsafe.example/chart.png)

- [x] mutate state`}</SafeMarkdown>,
    );

    expect(container.querySelector("script")).toBeNull();
    expect(container.querySelector("img")).toBeNull();
    expect(container.querySelector("input")).toBeNull();
    expect(screen.getByText(/Image omitted: diagnostic chart/)).toBeVisible();
  });

  it("keeps approved links actionable and strips unsafe targets", () => {
    render(
      <SafeMarkdown>{`[Evidence](https://example.com/a) [History](/history) [Anchor](#trace)

[Script](javascript:alert(1)) [Data](data:text/html,bad) [Relative](//unsafe.example)`}</SafeMarkdown>,
    );

    expect(screen.getByRole("link", { name: /Evidence/ })).toHaveAttribute(
      "href",
      "https://example.com/a",
    );
    expect(screen.getByRole("link", { name: /Evidence/ })).toHaveAttribute(
      "rel",
      "noopener noreferrer",
    );
    expect(screen.getByRole("link", { name: "History" })).toHaveAttribute("href", "/history");
    expect(screen.getByRole("link", { name: "Anchor" })).toHaveAttribute("href", "#trace");
    expect(screen.queryByRole("link", { name: "Script" })).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Data" })).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Relative" })).not.toBeInTheDocument();
    expect(safeMarkdownUrl("file:///tmp/secret", "a", "href")).toBeNull();
  });

  it("leaves malformed and plain text readable", () => {
    render(<SafeMarkdown>{"Investigate **unfinished emphasis and keep the text"}</SafeMarkdown>);
    expect(screen.getByText(/Investigate/)).toHaveTextContent("unfinished emphasis");
  });
});
