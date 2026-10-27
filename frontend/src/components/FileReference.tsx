import {
  memo,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { createPortal } from "react-dom";
import type { KeyboardEvent, MouseEvent, ReactNode } from "react";
import {
  Check,
  Copy,
  ExternalLink,
  Eye,
  FolderOpen,
  X,
} from "lucide-react";

import { extensionLabel, iconForExtension } from "../lib/fileIcons";
import {
  fileExtension,
  fileName,
  isImagePath,
  localFileSrc,
} from "../lib/localFiles";
import {
  copyLocalPath,
  openLocalFile,
  revealLocalFile,
} from "../lib/tauriActions";

type ActionButtonProps = {
  label: string;
  onClick: () => void;
  children: ReactNode;
  overlay?: boolean;
};

function ActionButton({
  label,
  onClick,
  children,
  overlay = false,
}: ActionButtonProps) {
  return (
    <button
      type="button"
      onClick={(event: MouseEvent<HTMLButtonElement>) => {
        event.preventDefault();
        event.stopPropagation();
        onClick();
      }}
      aria-label={label}
      title={label}
      className={`flex size-6 items-center justify-center rounded-md transition-all duration-200 ease-out ${
        overlay
          ? "text-fg/80 hover:bg-fg/15 hover:text-fg"
          : "text-fg/68 hover:bg-fg/[0.08] hover:text-fg/85"
      }`}
    >
      {children}
    </button>
  );
}

type FileReferenceProps = {
  path: string;
};

export const FileReference = memo(function FileReference({
  path,
}: FileReferenceProps) {
  const [imageFailed, setImageFailed] = useState(false);
  const [previewOpen, setPreviewOpen] = useState(false);
  const [copied, setCopied] = useState(false);
  const copyTimer = useRef<number | null>(null);

  useEffect(() => {
    return () => {
      if (copyTimer.current !== null) {
        window.clearTimeout(copyTimer.current);
      }
    };
  }, []);

  useEffect(() => {
    if (!previewOpen) {
      return;
    }

    const handleKeyDown = (event: globalThis.KeyboardEvent) => {
      if (event.key === "Escape") {
        setPreviewOpen(false);
      }
    };

    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [previewOpen]);

  const name = useMemo(() => fileName(path), [path]);
  const extension = useMemo(() => fileExtension(path), [path]);
  const FileIcon = useMemo(() => iconForExtension(extension), [extension]);
  const source = useMemo(() => localFileSrc(path), [path]);

  const openFile = useCallback(() => {
    void openLocalFile(path);
  }, [path]);

  const revealFile = useCallback(() => {
    void revealLocalFile(path);
  }, [path]);

  const handleKeyDown = useCallback(
    (event: KeyboardEvent<HTMLSpanElement>) => {
      if (event.target !== event.currentTarget) {
        return;
      }

      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        openFile();
      }
    },
    [openFile],
  );

  const copyPath = useCallback(async () => {
    const ok = await copyLocalPath(path);
    if (!ok) {
      return;
    }

    setCopied(true);
    if (copyTimer.current !== null) {
      window.clearTimeout(copyTimer.current);
    }
    copyTimer.current = window.setTimeout(() => setCopied(false), 1500);
  }, [path]);

  const actions = (
    <>
      <ActionButton label="Open file" onClick={openFile} overlay={false}>
        <ExternalLink className="size-3.5" />
      </ActionButton>
      <ActionButton
        label="Show in folder"
        onClick={revealFile}
        overlay={false}
      >
        <FolderOpen className="size-3.5" />
      </ActionButton>
      <ActionButton label="Copy path" onClick={copyPath} overlay={false}>
        {copied ? (
          <Check className="size-3.5 text-positive-ink" />
        ) : (
          <Copy className="size-3.5" />
        )}
      </ActionButton>
    </>
  );

  const previewDialog = previewOpen
    ? createPortal(
        <div
          className="fixed inset-0 z-[100] flex items-center justify-center bg-black/85 p-4 backdrop-blur-sm"
          onClick={() => setPreviewOpen(false)}
        >
          <div
            role="dialog"
            aria-modal="true"
            aria-label={`Preview ${name}`}
            onClick={(event) => event.stopPropagation()}
            className="flex max-h-full w-full max-w-2xl flex-col overflow-hidden rounded-2xl border border-fg/10 bg-surface shadow-[0_30px_90px_var(--compa-shadow-soft)]"
          >
            <div className="flex items-center gap-2 border-b border-fg/[0.07] px-3 py-2">
              <span className="min-w-0 flex-1 truncate text-xs font-medium text-fg/80">
                {name}
              </span>
              <span className="flex shrink-0 items-center gap-0.5">
                <ActionButton label="Open file" onClick={openFile} overlay>
                  <ExternalLink className="size-3.5" />
                </ActionButton>
                <ActionButton
                  label="Show in folder"
                  onClick={revealFile}
                  overlay
                >
                  <FolderOpen className="size-3.5" />
                </ActionButton>
                <ActionButton label="Copy path" onClick={copyPath} overlay>
                  {copied ? (
                    <Check className="size-3.5 text-positive-ink" />
                  ) : (
                    <Copy className="size-3.5" />
                  )}
                </ActionButton>
                <ActionButton
                  label="Close preview"
                  onClick={() => setPreviewOpen(false)}
                  overlay
                >
                  <X className="size-4" />
                </ActionButton>
              </span>
            </div>
            <div className="flex min-h-0 flex-1 items-center justify-center bg-black/25 p-2">
              <img
                src={source}
                alt={name}
                className="max-h-[72vh] w-auto max-w-full rounded-lg object-contain"
              />
            </div>
          </div>
        </div>,
        document.body,
      )
    : null;

  if (isImagePath(path) && !imageFailed) {
    return (
      <>
        <span
          role="button"
          tabIndex={0}
          onClick={() => setPreviewOpen(true)}
          onKeyDown={handleKeyDown}
          aria-label={`Preview ${name}`}
          className="file-ref-image group relative block w-44 max-w-full cursor-pointer overflow-hidden rounded-xl border border-fg/[0.08] bg-gradient-to-br from-raised to-surface text-left transition-all duration-200 ease-out hover:border-fg/[0.18] hover:shadow-[0_12px_34px_-14px_var(--compa-shadow-strong)] focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/50"
        >
          <span className="block aspect-[4/3] overflow-hidden bg-black/30">
            <img
              src={source}
              alt={name}
              loading="lazy"
              draggable={false}
              onError={() => setImageFailed(true)}
              className="h-full w-full object-cover transition-transform duration-200 ease-out group-hover:scale-[1.04]"
            />
          </span>

          <span className="pointer-events-none absolute inset-x-0 bottom-0 bg-gradient-to-t from-black/85 via-black/35 to-transparent px-2 pt-6 pb-1.5">
            <span className="block truncate text-[11px] font-medium text-fg/90">
              {name}
            </span>
          </span>

          <span className="absolute top-1.5 right-1.5 flex items-center gap-0.5 rounded-lg border border-fg/10 bg-black/65 p-0.5 opacity-0 backdrop-blur-md transition-opacity duration-200 ease-out pointer-events-none group-hover:pointer-events-auto group-hover:opacity-100 group-focus-within:pointer-events-auto group-focus-within:opacity-100">
            <ActionButton
              label="Preview image"
              onClick={() => setPreviewOpen(true)}
              overlay
            >
              <Eye className="size-3.5" />
            </ActionButton>
            <ActionButton label="Open file" onClick={openFile} overlay>
              <ExternalLink className="size-3.5" />
            </ActionButton>
            <ActionButton
              label="Show in folder"
              onClick={revealFile}
              overlay
            >
              <FolderOpen className="size-3.5" />
            </ActionButton>
            <ActionButton label="Copy path" onClick={copyPath} overlay>
              {copied ? (
                <Check className="size-3.5 text-positive-ink" />
              ) : (
                <Copy className="size-3.5" />
              )}
            </ActionButton>
          </span>
        </span>
        {previewDialog}
      </>
    );
  }

  return (
    <span
      role="button"
      tabIndex={0}
      onClick={revealFile}
      onKeyDown={handleKeyDown}
      aria-label={`Show ${name} in its folder`}
      title="Show in folder"
      className="file-ref-card group flex w-full max-w-md min-w-0 cursor-pointer items-center gap-2.5 rounded-xl border border-fg/[0.08] bg-gradient-to-br from-raised to-surface px-3 py-2.5 text-left transition-all duration-200 ease-out hover:border-fg/[0.18] hover:shadow-[0_10px_26px_-18px_var(--compa-shadow-strong)] focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/50"
    >
      <span className="flex size-9 shrink-0 items-center justify-center rounded-lg border border-fg/[0.07] bg-fg/[0.045] text-fg/68 transition-colors duration-200 ease-out group-hover:border-accent/15 group-hover:text-accent-ink">
        <FileIcon className="size-4" />
      </span>

      <span className="min-w-0 flex-1">
        <span className="block truncate text-[12.5px] font-medium text-fg/90">
          {name}
        </span>
        <span className="mt-0.5 block text-[10px] tracking-wide text-fg/68 uppercase">
          {extensionLabel(extension)}
        </span>
      </span>

      <span className="flex shrink-0 items-center gap-0.5 opacity-0 transition-opacity duration-200 ease-out pointer-events-none group-hover:pointer-events-auto group-hover:opacity-100 group-focus-within:pointer-events-auto group-focus-within:opacity-100">
        {actions}
      </span>
    </span>
  );
});
