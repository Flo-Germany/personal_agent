import {
  Children,
  isValidElement,
  memo,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import type {
  ComponentPropsWithoutRef,
  ReactNode,
} from "react";
import ReactMarkdown, { defaultUrlTransform } from "react-markdown";
import type { Components } from "react-markdown";
import { Files, Images } from "lucide-react";
import remarkGfm from "remark-gfm";

import { FileReference } from "./FileReference";
import {
  isImagePath,
  localPathFromHref,
  localPathFromText,
  toFileUrl,
} from "../lib/localFiles";
import { remarkLocalFilePaths } from "../lib/remarkLocalFilePaths";
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

const REMARK_PLUGINS = [remarkGfm, remarkLocalFilePaths];

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
      className="flex items-center gap-1 rounded-md px-1.5 py-0.5 text-[10px] text-white/56 transition-all duration-200 ease-out hover:bg-white/5 hover:text-white/70"
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
        <span className="font-mono text-[10px] tracking-wider text-white/56 uppercase">
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

type AnchorProps = ComponentPropsWithoutRef<"a"> & { node?: unknown };

function textFromChildren(children: ReactNode): string {
  return Children.toArray(children)
    .map((child) => {
      if (typeof child === "string" || typeof child === "number") {
        return String(child);
      }
      if (isValidElement(child)) {
        const props = child.props as { children?: ReactNode };
        return textFromChildren(props.children);
      }
      return "";
    })
    .join("");
}

function resolvePathFromAnchor(
  href: string | undefined,
  children: ReactNode,
): string | null {
  return (
    localPathFromHref(href) ??
    localPathFromText(textFromChildren(children))
  );
}

function LocalFileAnchor({ href, children, title }: AnchorProps) {
  const path = resolvePathFromAnchor(href, children);

  if (path) {
    return <FileReference path={path} />;
  }

  return (
    <a href={href} title={title} target="_blank" rel="noreferrer noopener">
      {children}
    </a>
  );
}

type ImageProps = ComponentPropsWithoutRef<"img"> & { node?: unknown };

function LocalFileImage({ src, alt }: ImageProps) {
  const path = src ? localPathFromHref(src) : null;

  if (path) {
    return <FileReference path={path} />;
  }

  return (
    <img src={src} alt={alt ?? ""} loading="lazy" draggable={false} />
  );
}

function resolveFileElement(node: ReactNode): string | null {
  if (!isValidElement(node)) {
    return null;
  }

  if (node.type === LocalFileAnchor) {
    const props = node.props as { href?: string; children?: ReactNode };
    return resolvePathFromAnchor(props.href, props.children);
  }

  if (node.type === LocalFileImage) {
    const props = node.props as { src?: string };
    return props.src ? localPathFromHref(props.src) : null;
  }

  return null;
}

function isIgnorableNode(node: ReactNode): boolean {
  if (typeof node === "string") {
    return node.trim() === "";
  }
  return isValidElement(node) && node.type === "br";
}

function collectFileInfo(children: ReactNode): {
  count: number;
  images: number;
} {
  let count = 0;
  let images = 0;

  const visit = (nodes: ReactNode) => {
    for (const node of Children.toArray(nodes)) {
      const path = resolveFileElement(node);
      if (path) {
        count += 1;
        if (isImagePath(path)) {
          images += 1;
        }
        continue;
      }

      if (isValidElement(node)) {
        const props = node.props as { children?: ReactNode };
        if (props.children !== undefined) {
          visit(props.children);
        }
      }
    }
  };

  visit(children);
  return { count, images };
}

function FileAwareParagraph({ children }: { children?: ReactNode }) {
  const nodes = Children.toArray(children);
  const paths = nodes
    .map(resolveFileElement)
    .filter((path): path is string => path !== null);

  if (paths.length === 0) {
    return <p>{children}</p>;
  }

  const onlyFiles = nodes.every(
    (node) => isIgnorableNode(node) || resolveFileElement(node) !== null,
  );

  if (onlyFiles) {
    const allImages = paths.every((path) => isImagePath(path));
    const gallery = (
      <div className={allImages ? "image-gallery" : "file-grid"}>
        {children}
      </div>
    );

    return paths.length > 1 ? (
      <GalleryFrame
        kind={allImages ? "images" : "files"}
        count={paths.length}
      >
        {gallery}
      </GalleryFrame>
    ) : (
      gallery
    );
  }

  return <div className="file-mixed">{children}</div>;
}

function GalleryFrame({
  kind,
  count,
  children,
}: {
  kind: "images" | "files";
  count: number;
  children: ReactNode;
}) {
  const Icon = kind === "images" ? Images : Files;
  const label =
    kind === "images"
      ? count === 1
        ? "image"
        : "images"
      : count === 1
        ? "file"
        : "files";

  return (
    <div className="file-gallery-panel">
      <div className="file-gallery-heading">
        <Icon className="size-3" />
        <span>
          {count} {label}
        </span>
      </div>
      {children}
    </div>
  );
}

function fileListInfo(
  children: ReactNode,
): { allImages: boolean; count: number } | null {
  const items = Children.toArray(children).filter(isValidElement);

  if (items.length === 0 || !items.every((item) => item.type === "li")) {
    return null;
  }

  let total = 0;
  let images = 0;

  for (const item of items) {
    const props = item.props as { children?: ReactNode };
    const info = collectFileInfo(props.children);

    if (info.count === 0) {
      return null;
    }

    total += info.count;
    images += info.images;
  }

  return { allImages: total > 0 && images === total, count: total };
}

function FileAwareUnorderedList({
  children,
  className,
}: {
  children?: ReactNode;
  className?: string;
}) {
  const info = fileListInfo(children);

  if (info) {
    const list = (
      <ul
        className={`${className ?? ""} ${
          info.allImages ? "image-gallery" : "file-grid"
        }`.trim()}
      >
        {children}
      </ul>
    );

    return info.count > 1 ? (
      <GalleryFrame
        kind={info.allImages ? "images" : "files"}
        count={info.count}
      >
        {list}
      </GalleryFrame>
    ) : (
      list
    );
  }

  return <ul className={className}>{children}</ul>;
}

function FileAwareOrderedList({
  children,
  className,
}: {
  children?: ReactNode;
  className?: string;
}) {
  const info = fileListInfo(children);

  if (info) {
    const list = (
      <ol
        className={`${className ?? ""} ${
          info.allImages ? "image-gallery" : "file-grid"
        }`.trim()}
      >
        {children}
      </ol>
    );

    return info.count > 1 ? (
      <GalleryFrame
        kind={info.allImages ? "images" : "files"}
        count={info.count}
      >
        {list}
      </GalleryFrame>
    ) : (
      list
    );
  }

  return <ol className={className}>{children}</ol>;
}

const components: Components = {
  a: LocalFileAnchor,
  img: LocalFileImage,
  p: ({ children }) => <FileAwareParagraph>{children}</FileAwareParagraph>,
  ul: ({ children, className }) => (
    <FileAwareUnorderedList className={className}>
      {children}
    </FileAwareUnorderedList>
  ),
  ol: ({ children, className }) => (
    <FileAwareOrderedList className={className}>{children}</FileAwareOrderedList>
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

function preserveLocalFileUrls(url: string): string {
  if (/^(?:file:|[A-Za-z]:[\\/]|\\\\)/i.test(url)) {
    return url;
  }
  return defaultUrlTransform(url);
}

/**
 * Repair raw Windows paths inside markdown link destinations before parsing.
 * ReactMarkdown would otherwise drop destinations like `](C:\\path\\file.png)`.
 */
function repairLocalLinkDestinations(content: string): string {
  return content.replace(
    /\]\(([A-Za-z]:[\\/][^)\r\n]*)\)/g,
    (match, destination: string) => {
      const path = localPathFromText(destination);
      return path ? `](${toFileUrl(path)})` : match;
    },
  );
}

type MarkdownContentProps = {
  content: string;
  className?: string;
};

export const MarkdownContent = memo(function MarkdownContent({
  content,
  className = "",
}: MarkdownContentProps) {
  const repaired = useMemo(() => repairLocalLinkDestinations(content), [content]);
  const wrapperClass = useMemo(
    () => `markdown-body ${className}`.trim(),
    [className],
  );

  return (
    <div className={wrapperClass}>
      <ReactMarkdown
        remarkPlugins={REMARK_PLUGINS}
        components={components}
        urlTransform={preserveLocalFileUrls}
      >
        {repaired}
      </ReactMarkdown>
    </div>
  );
});
