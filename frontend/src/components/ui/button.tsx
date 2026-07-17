import { Slot } from "@radix-ui/react-slot";
import { cva, type VariantProps } from "class-variance-authority";
import * as React from "react";

import { cn } from "@/lib/utils";

const buttonVariants = cva(
  "inline-flex min-h-10 shrink-0 items-center justify-center gap-2 border px-4 font-mono text-xs font-semibold tracking-[0.04em] transition-[color,background-color,border-color,transform] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent focus-visible:ring-offset-2 focus-visible:ring-offset-ink disabled:pointer-events-none disabled:opacity-40 active:translate-y-px",
  {
    variants: {
      variant: {
        primary: "border-accent bg-accent text-ink hover:bg-accent-strong",
        quiet: "border-line-strong bg-surface text-text hover:border-accent hover:text-accent",
        danger: "border-danger/70 bg-danger/10 text-danger hover:bg-danger/20",
        ghost: "border-transparent bg-transparent text-muted hover:border-line-strong hover:text-text",
      },
      size: {
        default: "h-10",
        compact: "h-8 min-h-8 px-3 text-[11px]",
        icon: "size-10 p-0",
      },
    },
    defaultVariants: {
      variant: "quiet",
      size: "default",
    },
  },
);

export interface ButtonProps
  extends React.ButtonHTMLAttributes<HTMLButtonElement>,
    VariantProps<typeof buttonVariants> {
  asChild?: boolean;
}

export const Button = React.forwardRef<HTMLButtonElement, ButtonProps>(
  ({ className, variant, size, asChild = false, ...props }, ref) => {
    const Component = asChild ? Slot : "button";
    return (
      <Component
        className={cn(buttonVariants({ variant, size }), className)}
        ref={ref}
        {...props}
      />
    );
  },
);
Button.displayName = "Button";
