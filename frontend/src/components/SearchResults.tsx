import { memo, useCallback, useMemo, useState } from "react";
import type { ReactNode } from "react";
import {
  Check,
  ChevronRight,
  Copy,
  ExternalLink,
  Folder,
  FolderOpen,
  Search,
  TriangleAlert,
} from "lucide-react";

import { iconForExtension } from "../lib/fileIcons";
import {
  copyLocalPath,
  openLocalFile,
  revealLocalFile,
} from "../lib/tauriActions";
import type { SearchResultData, SearchTreeNode } from "../types";

type Filter = "all" | "folders" | "files";

function formatBytes(value?: number | null): string | null {
  if (value === null || value === undefined) {
    return null;
  }
  if (value < 1024) {
    return `${value} B`;
  }
  const units = ["KB", "MB", "GB", "TB"];
  let size = value / 1024;
  let index = 0;
  while (size >= 1024 && index < units.length - 1) {
    size /= 1024;
    index += 1;
  }
  return `${size.toFixed(size < 10 ? 1 : 0)} ${units[index]}`;
}

function relativeTime(value?: string | null): string | null {
  if (!value) {
    return null;
  }
  const then = Date.parse(value);
  if (Number.isNaN(then)) {
    return null;
  }
  const minutes = Math.round((Date.now() - then) / 60_000);
  if (minutes < 1) {
    return "just now";
  }
  if (minutes < 60) {
    return `${minutes}m ago`;
  }
  const hours = Math.round(minutes / 60);
  if (hours < 24) {
    return `${hours}h ago`;
  }
  const days = Math.round(hours / 24);
  if (days < 30) {
    return `${days}d ago`;
  }
  const months = Math.round(days / 30);
  if (months < 12) {
    return `${months}mo ago`;
  }
  return `${Math.round(months / 12)}y ago`;
}

/** Keep matches plus the folders that lead to them. */
function filterTree(node: SearchTreeNode, filter: Filter): SearchTreeNode | null {
  const children = (node.children ?? [])
    .map((child) => filterTree(child, filter))
    .filter((child): child is SearchTreeNode => child !== null);
  const selfMatches =
    filter === "all" ||
    node.type === (filter === "folders" ? "folder" : "file");
  const hasMatch = node.matched && selfMatches;

  if (!hasMatch && children.length === 0) {
    return null;
  }
  return { ...node, children };
}

function collectMatches(node: SearchTreeNode, into: SearchTreeNode[]): void {
  if (node.matched) {
    into.push(node);
  }
  for (const child of node.children ?? []) {
    collectMatches(child, into);
  }
}

function plural(count: number, singular: string): string {
  return `${count} ${singular}${count === 1 ? "" : "s"}`;
}

/** Emphasize the matched part of a name so long lists scan faster. */
function highlightName(name: string, needle: string | null | undefined) {
  const query = needle?.trim();
  if (!query || query.includes("*") || query.includes("?")) {
    return name;
  }
  const index = name.toLocaleLowerCase().indexOf(query.toLocaleLowerCase());
  if (index < 0) {
    return name;
  }
  return (
    <>
      {name.slice(0, index)}
      <span className="rounded-[2px] bg-violet-300/10 text-violet-100/95">
        {name.slice(index, index + query.length)}
      </span>
      {name.slice(index + query.length)}
    </>
  );
}

function RowAction({
  label,
  onClick,
  children,
}: {
  label: string;
  onClick: () => void;
  children: ReactNode;
}) {
  return (
    <button
      type="button"
      onClick={(event) => {
        event.preventDefault();
        event.stopPropagation();
        onClick();
      }}
      aria-label={label}
      title={label}
      className="flex size-5 items-center justify-center rounded text-white/30 transition-colors duration-150 ease-out hover:bg-white/[0.08] hover:text-white/80 focus:outline-none focus-visible:ring-2 focus-visible:ring-white/20"
    >
      {children}
    </button>
  );
}

type SearchResultsProps = {
  data: SearchResultData;
};

/**
 * Chat card for `search_system` results.
 *
 * Renders the backend's ready-made tree so a search answers "where is it?"
 * visually: folders and files keep their hierarchy, matches are highlighted,
 * and every row can be opened, revealed in Explorer, or copied.
 */
export const SearchResults = memo(function SearchResults({
  data,
}: SearchResultsProps) {
  const [filter, setFilter] = useState<Filter>("all");
  const [collapsed, setCollapsed] = useState<Set<string>>(() => new Set());
  const [copied, setCopied] = useState(false);

  const summary = data.summary ?? {};
  const files =
    summary.files ??
    data.results.filter((item) => item.type === "file").length;
  const folders =
    summary.folders ??
    data.results.filter((item) => item.type === "folder").length;
  const total = summary.total ?? files + folders;
  const matchedNodes = useMemo(() => {
    const into: SearchTreeNode[] = [];
    for (const node of data.tree ?? []) {
      collectMatches(node, into);
    }
    return into;
  }, [data.tree]);
  const nodes = useMemo(
    () =>
      (data.tree ?? [])
        .map((node) => filterTree(node, filter))
        .filter((node): node is SearchTreeNode => node !== null),
    [data.tree, filter],
  );

  const roots = data.roots ?? [];
  const rootLabel =
    roots.length === 0
      ? "local files"
      : roots.length === 1
        ? roots[0]
        : `${roots.length} drives`;
  const queryLabel = data.query?.name_contains
    ? data.query.extension
      ? `"${data.query.name_contains}" · ${data.query.extension}`
      : `"${data.query.name_contains}"`
    : data.query?.extension
      ? `${data.query.extension} files`
      : null;
  const duration =
    typeof data.duration_seconds === "number"
      ? `${data.duration_seconds}s`
      : null;
  const newest = data.summary?.newest_path;
  const newestAge = relativeTime(data.summary?.newest_modified);

  const toggle = useCallback((path: string) => {
    setCollapsed((current) => {
      const next = new Set(current);
      if (next.has(path)) {
        next.delete(path);
      } else {
        next.add(path);
      }
      return next;
    });
  }, []);

  const copyAll = useCallback(async () => {
    const paths = matchedNodes.map((node) => node.path);
    const ok = await copyLocalPath(paths.join("\n"));
    if (!ok) {
      return;
    }
    setCopied(true);
    window.setTimeout(() => setCopied(false), 1500);
  }, [matchedNodes]);

  const renderNode = (node: SearchTreeNode, depth: number) => {
    const children = node.children ?? [];
    const isFolder = node.type === "folder";
    const hasChildren = children.length > 0;
    const isOpen = !collapsed.has(node.path);
    const isMatch =
      node.matched &&
      (filter === "all" ||
        node.type === (filter === "folders" ? "folder" : "file"));
    const Icon = isFolder
      ? hasChildren && isOpen
        ? FolderOpen
        : Folder
      : iconForExtension((node.extension ?? "").replace(/^\./, ""));
    const size = isFolder ? null : formatBytes(node.size);
    const age = relativeTime(node.modified);

    return (
      <li
        key={node.path}
        role="treeitem"
        aria-expanded={isFolder && hasChildren ? isOpen : undefined}
      >
        <div
          className="group flex items-center gap-1 rounded-md pr-1 transition-colors duration-150 ease-out hover:bg-white/[0.03]"
          style={{ paddingLeft: `${depth * 14 + 4}px` }}
        >
          {isFolder && hasChildren ? (
            <button
              type="button"
              onClick={() => toggle(node.path)}
              aria-label={isOpen ? `Collapse ${node.name}` : `Expand ${node.name}`}
              className="flex size-4 shrink-0 items-center justify-center rounded text-white/30 transition-colors duration-150 ease-out hover:text-white/70 focus:outline-none focus-visible:ring-2 focus-visible:ring-white/20"
            >
              <ChevronRight
                className={`size-3 transition-transform duration-150 ease-out ${
                  isOpen ? "rotate-90" : ""
                }`}
              />
            </button>
          ) : (
            <span className="size-4 shrink-0" />
          )}

          <button
            type="button"
            onClick={() => void openLocalFile(node.path)}
            title={node.path}
            className="flex min-w-0 flex-1 items-center gap-1.5 py-[3px] text-left focus:outline-none focus-visible:ring-2 focus-visible:ring-violet-300/40"
          >
            <Icon
              className={`size-3.5 shrink-0 ${
                isMatch
                  ? isFolder
                    ? "text-amber-200/70"
                    : "text-violet-200/60"
                  : "text-white/25"
              }`}
            />
            <span
              className={`truncate text-[11px] leading-4 ${
                isMatch ? "text-white/85" : "text-white/40"
              }`}
            >
              {isMatch
                ? highlightName(node.name, data.query?.name_contains)
                : node.name}
            </span>
            {isFolder && node.children_count ? (
              <span className="shrink-0 text-[9px] tabular-nums text-white/25">
                {plural(node.children_count, "item")}
              </span>
            ) : null}
            {size ? (
              <span className="hidden shrink-0 text-[9px] tabular-nums text-white/25 sm:inline">
                {size}
              </span>
            ) : null}
            {age && isMatch ? (
              <span className="hidden shrink-0 text-[9px] tabular-nums text-white/20 md:inline">
                {age}
              </span>
            ) : null}
          </button>

          <span className="flex shrink-0 items-center gap-0.5 opacity-0 transition-opacity duration-150 ease-out group-hover:opacity-100 group-focus-within:opacity-100">
            <RowAction
              label={isFolder ? "Open folder" : "Open file"}
              onClick={() => void openLocalFile(node.path)}
            >
              <ExternalLink className="size-3" />
            </RowAction>
            <RowAction
              label="Show in Explorer"
              onClick={() => void revealLocalFile(node.path)}
            >
              <FolderOpen className="size-3" />
            </RowAction>
            <RowAction
              label="Copy path"
              onClick={() => void copyLocalPath(node.path)}
            >
              <Copy className="size-3" />
            </RowAction>
          </span>
        </div>

        {isFolder && hasChildren && isOpen ? (
          <ul role="group">{children.map((child) => renderNode(child, depth + 1))}</ul>
        ) : null}
      </li>
    );
  };

  const chip = (value: Filter, label: string, count: number) => (
    <button
      key={value}
      type="button"
      onClick={() => setFilter(value)}
      aria-pressed={filter === value}
      className={`rounded-full border px-1.5 py-[1px] text-[9.5px] leading-4 transition-colors duration-150 ease-out focus:outline-none focus-visible:ring-2 focus-visible:ring-white/20 ${
        filter === value
          ? "border-violet-300/25 bg-violet-300/10 text-violet-100/85"
          : "border-white/[0.06] bg-white/[0.02] text-white/35 hover:text-white/60"
      }`}
    >
      {label}
      <span className="ml-1 tabular-nums text-white/30">{count}</span>
    </button>
  );

  return (
    <div className="message-in">
      <div className="overflow-hidden rounded-xl border border-white/[0.07] bg-white/[0.018]">
        <div className="flex flex-wrap items-center gap-2 border-b border-white/[0.05] px-2.5 py-1.5">
          <span className="flex size-6 shrink-0 items-center justify-center rounded-full border border-white/10 bg-white/[0.04] text-white/45">
            <Search className="size-3" />
          </span>
          <div className="min-w-0 flex-1">
            <p className="truncate text-[11.5px] leading-4 font-medium text-white/80">
              {queryLabel ? `Search ${queryLabel}` : "Search results"}
              <span className="ml-1.5 font-normal text-white/35">
                {plural(folders, "folder")} · {plural(files, "file")}
              </span>
            </p>
            <p className="mt-0.5 flex min-w-0 items-center gap-1.5 text-[9.5px] leading-3 text-white/30">
              <span className="truncate">{rootLabel}</span>
              {duration ? <span className="shrink-0">· {duration}</span> : null}
              {data.truncated ? (
                <span
                  className="flex shrink-0 items-center gap-0.5 rounded-full bg-amber-300/10 px-1 text-amber-100/70"
                  title="The search hit its time, scan, or result cap. Narrow the root or filters to see more."
                >
                  <TriangleAlert className="size-2.5" />
                  capped
                </span>
              ) : null}
            </p>
          </div>
          <div className="flex shrink-0 items-center gap-1">
            {chip("all", "All", total)}
            {chip("folders", "Folders", folders)}
            {chip("files", "Files", files)}
            <button
              type="button"
              onClick={() => void copyAll()}
              aria-label="Copy all matching paths"
              title="Copy all matching paths"
              className="flex size-5 items-center justify-center rounded text-white/30 transition-colors duration-150 ease-out hover:bg-white/[0.08] hover:text-white/80 focus:outline-none focus-visible:ring-2 focus-visible:ring-white/20"
            >
              {copied ? (
                <Check className="size-3 text-emerald-300" />
              ) : (
                <Copy className="size-3" />
              )}
            </button>
          </div>
        </div>

        <div className="max-h-[420px] overflow-y-auto px-1 py-1">
          {nodes.length > 0 ? (
            <ul role="tree" aria-label="Search results">
              {nodes.map((node) => renderNode(node, 0))}
            </ul>
          ) : (
            <p className="px-2.5 py-2 text-[10.5px] text-white/35">
              No {filter === "folders" ? "folders" : "files"} in these results.
            </p>
          )}
        </div>

        {(newest || summary.total_size) && (
          <div className="flex items-center justify-between gap-3 border-t border-white/[0.05] px-2.5 py-1 text-[9px] text-white/25">
            <span className="truncate">
              {newest
                ? `Newest: ${newest.split(/[\\/]/).pop()}${newestAge ? ` · ${newestAge}` : ""}`
                : ""}
            </span>
            <span className="shrink-0 tabular-nums">
              {summary.total_size
                ? `${formatBytes(summary.total_size)} total`
                : ""}
            </span>
          </div>
        )}
      </div>
    </div>
  );
});
