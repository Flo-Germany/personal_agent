import {
  Children,
  isValidElement,
  useEffect,
  useRef,
  useState,
} from "react";
import type { ReactNode } from "react";
import ReactMarkdown from "react-markdown";
import type { Components } from "react-markdown";
import remarkGfm from "remark-gfm";
import { PrismLight as SyntaxHighlighter } from "react-syntax-highlighter";
import { oneDark } from "react-syntax-highlighter/dist/esm/styles/prism";
import bash from "react-syntax-highlighter/dist/esm/languages/prism/bash";
import css from "react-syntax-highlighter/dist/esm/languages/prism/css";
import diff from "react-syntax-highlighter/dist/esm/languages/prism/diff";
import ini from "react-syntax-highlighter/dist/esm/languages/prism/ini";
import javascript from "react-syntax-highlighter/dist/esm/languages/prism/javascript";
import json from "react-syntax-highlighter/dist/esm/languages/prism/json";
import jsx from "react-syntax-highlighter/dist/esm/languages/prism/jsx";
import markdown from "react-syntax-highlighter/dist/esm/languages/prism/markdown";
import markup from "react-syntax-highlighter/dist/esm/languages/prism/markup";
import powershell from "react-syntax-highlighter/dist/esm/languages/prism/powershell";
import python from "react-syntax-highlighter/dist/esm/languages/prism/python";
import sql from "react-syntax-highlighter/dist/esm/languages/prism/sql";
import tsx from "react-syntax-highlighter/dist/esm/languages/prism/tsx";
import typescript from "react-syntax-highlighter/dist/esm/languages/prism/typescript";
import yaml from "react-syntax-highlighter/dist/esm/languages/prism/yaml";

import { CheckIcon, CopyIcon } from "./icons";

const LANGUAGES: Record<string, unknown> = {
  bash,
  css,
  diff,
  ini,
  javascript,
  json,
  jsx,
  markdown,
  markup,
  powershell,
  python,
  sql,
  tsx,
  typescript,
  yaml,
};

for (const [name, language] of Object.entries(LANGUAGES)) {
  SyntaxHighlighter.registerLanguage(name, language);
}

const LANGUAGE_ALIASES: Record<string, string> = {
  js: "javascript",
  ts: "typescript",
  py: "python",
  sh: "bash",
  shell: "bash",
  zsh: "bash",
  yml: "yaml",
  md: "markdown",
  ps1: "powershell",
  html: "markup",
  xml: "markup",
};

function resolveLanguage(raw: string | undefined): string | null {
  if (!raw) {
    return null;
  }
  const name = raw.toLowerCase();
  const resolved = LANGUAGE_ALIASES[name] ?? name;
  return resolved in LANGUAGES ? resolved : null;
}

const CODE_FONT =
  "'JetBrains Mono', 'SFMono-Regular', ui-monospace, Menlo, monospace";

function CopyButton({ text }: { text: string }) {
  const [copied, setCopied] = useState(false);
  const timeoutRef = useRef<number | null>(null);

  useEffect(() => {
    return () => {
      if (timeoutRef.current !== null) {
        window.clearTimeout(timeoutRef.current);
      }
    };
  }, []);

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
      if (timeoutRef.current !== null) {
        window.clearTimeout(timeoutRef.current);
      }
      timeoutRef.current = window.setTimeout(() => setCopied(false), 1400);
    } catch {
      // Clipboard access can be denied; keep the button quiet.
    }
  };

  return (
    <button
      type="button"
      onClick={() => void copy()}
      aria-label={copied ? "Copied" : "Copy code"}
      title={copied ? "Copied" : "Copy code"}
      className="flex items-center gap-1 rounded-md px-1.5 py-0.5 text-[10px] text-white/35 transition-all duration-200 ease-out hover:bg-white/5 hover:text-white/70"
    >
      {copied ? (
        <CheckIcon className="size-3" />
      ) : (
        <CopyIcon className="size-3" />
      )}
    </button>
  );
}

function CodeBlock({ code, language }: { code: string; language: string | null }) {
  return (
    <div className="my-3 overflow-hidden rounded-xl border border-white/[0.08] bg-[#0d0d0f]">
      <div className="flex items-center justify-between border-b border-white/[0.06] px-3 py-1.5">
        <span className="font-mono text-[10px] tracking-wider text-white/35 uppercase">
          {language ?? "code"}
        </span>
        <CopyButton text={code} />
      </div>
      <SyntaxHighlighter
        language={language ?? undefined}
        style={oneDark}
        PreTag="div"
        customStyle={{
          margin: 0,
          padding: "0.875rem 1rem",
          background: "transparent",
          fontSize: "0.78rem",
          lineHeight: 1.65,
          overflowX: "auto",
        }}
        codeTagProps={{
          style: {
            fontFamily: CODE_FONT,
            background: "transparent",
          },
        }}
        wrapLongLines
      >
        {code}
      </SyntaxHighlighter>
    </div>
  );
}

function readCodeChild(children: ReactNode): {
  code: string;
  language: string | null;
} | null {
  const child = Children.toArray(children)[0];

  if (!isValidElement(child)) {
    return null;
  }

  const props = child.props as {
    className?: string;
    children?: ReactNode;
  };

  const match = /language-([\w+-]+)/.exec(props.className ?? "");
  const code = String(props.children ?? "").replace(/\n$/, "");

  return { code, language: resolveLanguage(match?.[1]) };
}

const components: Components = {
  a: ({ children, ...props }) => (
    <a {...props} target="_blank" rel="noreferrer noopener">
      {children}
    </a>
  ),
  pre: ({ children }) => {
    const block = readCodeChild(children);
    if (!block) {
      return <pre>{children}</pre>;
    }
    return <CodeBlock code={block.code} language={block.language} />;
  },
  code: ({ children, className }) => {
    const match = /language-([\w+-]+)/.exec(className ?? "");
    if (match) {
      return <code className={className}>{children}</code>;
    }
    return <code className="md-inline-code">{children}</code>;
  },
  table: ({ children }) => (
    <div className="md-table-wrap">
      <table>{children}</table>
    </div>
  ),
};

type MarkdownContentProps = {
  content: string;
  className?: string;
};

export function MarkdownContent({
  content,
  className = "",
}: MarkdownContentProps) {
  return (
    <div className={`markdown-body ${className}`.trim()}>
      <ReactMarkdown remarkPlugins={[remarkGfm]} components={components}>
        {content}
      </ReactMarkdown>
    </div>
  );
}
