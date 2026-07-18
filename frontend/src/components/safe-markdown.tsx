import { compiler, type MarkdownToJSX } from "markdown-to-jsx";
import type { AnchorHTMLAttributes, HTMLAttributes, ReactNode } from "react";

import { cn } from "@/lib/utils";

function safeMarkdownUrl(value: string, _tag: string, attribute: string): string | null {
  if (attribute === "src") return null;
  if (attribute !== "href") return value;

  const candidate = value.trim();
  if (candidate.startsWith("#")) return candidate;
  if (candidate.startsWith("/") && !candidate.startsWith("//")) return candidate;
  if (!/^https?:\/\//i.test(candidate)) return null;
  try {
    const url = new URL(candidate);
    return ["http:", "https:"].includes(url.protocol) ? candidate : null;
  } catch {
    return null;
  }
}

function SafeLink({ href, children, ...props }: AnchorHTMLAttributes<HTMLAnchorElement>) {
  if (!href) return <span className="markdown-link-disabled">{children}</span>;
  const external = /^https?:\/\//i.test(href);
  return (
    <a
      {...props}
      href={href}
      target={external ? "_blank" : undefined}
      rel={external ? "noopener noreferrer" : undefined}
    >
      {children}
      {external ? <span className="sr-only"> (opens in new tab)</span> : null}
    </a>
  );
}

function MarkdownHeading({ children, ...props }: HTMLAttributes<HTMLHeadingElement>) {
  const safeProps = { ...props };
  delete safeProps.id;
  return <h4 {...safeProps}>{children}</h4>;
}

function OmittedImage({ alt }: { alt?: string }) {
  return alt ? <span className="markdown-omitted">[Image omitted: {alt}]</span> : null;
}

const options: MarkdownToJSX.Options = {
  disableAutoLink: true,
  disableParsingRawHTML: true,
  enforceAtxHeadings: true,
  forceBlock: true,
  sanitizer: safeMarkdownUrl,
  overrides: {
    a: SafeLink,
    h1: MarkdownHeading,
    h2: MarkdownHeading,
    h3: MarkdownHeading,
    h4: MarkdownHeading,
    h5: MarkdownHeading,
    h6: MarkdownHeading,
    img: OmittedImage,
    input: () => null,
  },
};

export function SafeMarkdown({
  children,
  className,
  id,
}: {
  children?: string | null;
  className?: string;
  id?: string;
}) {
  const source = children?.trim() || "";
  let rendered: ReactNode;
  try {
    rendered = compiler(source, options);
  } catch {
    rendered = <p data-markdown-fallback>{source}</p>;
  }
  return <div id={id} className={cn("markdown-content", className)}>{rendered}</div>;
}

export { safeMarkdownUrl };
