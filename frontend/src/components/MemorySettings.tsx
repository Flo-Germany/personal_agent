import { useEffect, useMemo, useState } from "react";
import {
  BrainCircuit,
  Check,
  FileText,
  Plus,
  RefreshCw,
  Sparkles,
  Trash2,
  TriangleAlert,
} from "lucide-react";

import {
  clearKnowledge,
  fetchKnowledge,
  saveKnowledge,
} from "../lib/settingsApi";
import {
  addKnowledgeFact,
  parseKnowledge,
  removeKnowledgeFact,
} from "../lib/knowledge";
import { shortModelName } from "../lib/display";
import type { Identity } from "../types";
import { AgentAvatar } from "./AgentAvatar";
import {
  Badge,
  DangerButton,
  PrimaryButton,
  QuietButton,
  SettingsCard,
  SettingsNote,
} from "./SettingsUi";

function IdentityCard({ identity }: { identity: Identity | null }) {
  const name = identity?.name ?? "Compa";
  return (
    <div className="flex items-center gap-3 rounded-[18px] border border-fg/[0.08] bg-fg/[0.028] p-4 shadow-[var(--compa-shadow)]">
      <AgentAvatar size="xl" />
      <div className="min-w-0 flex-1">
        <p className="truncate text-[14px] font-semibold text-fg/92">{name}</p>
        <p className="truncate text-[11px] text-fg/68" title={identity?.model}>
          {identity ? shortModelName(identity.model) : "Local model"}
        </p>
      </div>
      <Badge tone="positive">
        <Sparkles className="size-2.5" /> Local
      </Badge>
    </div>
  );
}

export function MemorySettings({ identity }: { identity: Identity | null }) {
  const [content, setContent] = useState<string | null>(null);
  const [saved, setSaved] = useState("");
  const [path, setPath] = useState("");
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [raw, setRaw] = useState(false);
  const [section, setSection] = useState("");
  const [fact, setFact] = useState("");
  const [resetArmed, setResetArmed] = useState(false);

  const load = async () => {
    setLoading(true);
    try {
      const data = await fetchKnowledge();
      setContent(data.content);
      setSaved(data.content);
      setPath(data.path);
      setError("");
    } catch (cause) {
      setError(
        cause instanceof Error ? cause.message : "Could not load the memory file",
      );
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    void load();
  }, []);

  useEffect(() => {
    if (!resetArmed) {
      return;
    }
    const timer = window.setTimeout(() => setResetArmed(false), 4000);
    return () => window.clearTimeout(timer);
  }, [resetArmed]);

  const parsed = useMemo(
    () => (content ? parseKnowledge(content) : { intro: "", sections: [] }),
    [content],
  );

  const factCount = parsed.sections.reduce(
    (total, item) => total + item.facts.length,
    0,
  );
  const dirty = content !== null && content !== saved;
  const activeSection =
    section && parsed.sections.some((item) => item.title === section)
      ? section
      : (parsed.sections[0]?.title ?? "");

  const save = async () => {
    if (content === null) {
      return;
    }
    setBusy(true);
    setError("");
    try {
      const result = await saveKnowledge(content);
      setSaved(content);
      setPath(result.path);
    } catch (cause) {
      setError(
        cause instanceof Error ? cause.message : "Could not save the memory file",
      );
    } finally {
      setBusy(false);
    }
  };

  const reset = async () => {
    if (!resetArmed) {
      setResetArmed(true);
      return;
    }
    setBusy(true);
    setError("");
    try {
      const result = await clearKnowledge();
      setContent(result.content);
      setSaved(result.content);
      setResetArmed(false);
    } catch (cause) {
      setError(
        cause instanceof Error ? cause.message : "Could not reset the memory",
      );
    } finally {
      setBusy(false);
    }
  };

  return (
    <>
      <IdentityCard identity={identity} />

      <SettingsCard
        title="Knowledge base"
        description="Facts Compa keeps about you. Everything stays in this local file."
        action={
          <Badge tone={dirty ? "warning" : "neutral"}>
            <FileText className="size-2.5" />
            {factCount} {factCount === 1 ? "fact" : "facts"}
          </Badge>
        }
      >
        {loading ? (
          <p className="flex items-center gap-2 text-[11px] text-fg/68">
            <RefreshCw className="size-3 animate-spin" /> Loading memory…
          </p>
        ) : content === null ? (
          <SettingsNote tone="danger">
            {error || "The memory file could not be read."}
          </SettingsNote>
        ) : (
          <>
            <div className="flex flex-wrap items-center gap-1.5">
              <QuietButton onClick={() => setRaw((value) => !value)}>
                {raw ? "Structured view" : "Edit as text"}
              </QuietButton>
              <QuietButton onClick={() => void load()} disabled={busy}>
                <span className="flex items-center gap-1.5">
                  <RefreshCw className="size-3" /> Reload
                </span>
              </QuietButton>
              {dirty && <Badge tone="warning">Unsaved changes</Badge>}
            </div>

            {raw ? (
              <textarea
                aria-label="Memory file"
                value={content}
                spellCheck={false}
                onChange={(event) => setContent(event.target.value)}
                className="field-focus min-h-[260px] w-full resize-y rounded-xl border border-fg/[0.1] bg-fg/[0.03] p-3 font-mono text-[11px] leading-5 text-fg/80 focus:outline-none"
              />
            ) : (
              <div className="grid grid-cols-1 gap-2.5 min-[600px]:grid-cols-2">
                <div className="space-y-2 rounded-xl border border-dashed border-fg/[0.12] p-2.5 min-[600px]:col-span-2">
                  <div className="flex flex-wrap gap-1.5">
                    {parsed.sections.map((item) => (
                      <button
                        key={item.title}
                        type="button"
                        onClick={() => setSection(item.title)}
                        className={`rounded-full border px-2 py-0.5 text-[9.5px] transition-colors duration-150 ${
                          item.title === activeSection
                            ? "border-accent/40 bg-accent/15 text-accent-ink"
                            : "border-fg/[0.09] text-fg/68 hover:text-fg/80"
                        }`}
                      >
                        {item.title}
                      </button>
                    ))}
                  </div>
                  <div className="flex items-center gap-1.5">
                    <input
                      aria-label="New fact"
                      value={fact}
                      placeholder="Add something Compa should remember?"
                      onChange={(event) => setFact(event.target.value)}
                      onKeyDown={(event) => {
                        if (event.key === "Enter" && fact.trim()) {
                          setContent(
                            addKnowledgeFact(content, activeSection, fact),
                          );
                          setFact("");
                        }
                      }}
                      className="field-focus min-w-0 flex-1 rounded-lg border border-fg/[0.1] bg-fg/[0.04] px-2.5 py-1.5 text-[11px] text-fg/85 placeholder:text-fg/75 focus:outline-none"
                    />
                    <button
                      type="button"
                      aria-label="Add fact"
                      disabled={!fact.trim() || !activeSection}
                      onClick={() => {
                        setContent(addKnowledgeFact(content, activeSection, fact));
                        setFact("");
                      }}
                      className="flex size-7 shrink-0 items-center justify-center rounded-lg bg-accent text-white transition-colors hover:bg-accent-strong disabled:opacity-30"
                    >
                      <Plus className="size-3.5" />
                    </button>
                  </div>
                </div>

                {parsed.sections.map((item) => (
                  <div
                    key={item.title}
                    className="rounded-xl border border-fg/[0.07] bg-fg/[0.02] p-2.5"
                  >
                    <div className="flex items-center gap-1.5">
                      <span className="size-1.5 rounded-full bg-accent" />
                      <p className="min-w-0 flex-1 truncate text-[10px] font-semibold tracking-[0.08em] text-fg/68 uppercase">
                        {item.title}
                      </p>
                    </div>
                    {item.facts.length === 0 ? (
                      <p className="mt-1 text-[10.5px] text-fg/68">No facts yet.</p>
                    ) : (
                      <ul className="mt-1.5 space-y-1">
                        {item.facts.map((entry) => (
                          <li
                            key={`${item.title}-${entry.index}`}
                            className="group flex items-start gap-2 rounded-lg px-1.5 py-1 transition-colors hover:bg-fg/[0.04]"
                          >
                            <span className="mt-[7px] size-1 shrink-0 rounded-full bg-fg/25" />
                            <span className="min-w-0 flex-1 break-words text-[11px] leading-4 text-fg/80">
                              {entry.text}
                            </span>
                            <button
                              type="button"
                              aria-label={`Delete fact: ${entry.text}`}
                              onClick={() =>
                                setContent(
                                  removeKnowledgeFact(content, entry),
                                )
                              }
                              className="flex size-6 shrink-0 items-center justify-center rounded-md text-fg/68 opacity-0 transition-all duration-150 ease-out group-hover:opacity-100 hover:bg-danger/15 hover:text-danger-ink focus-visible:opacity-100"
                            >
                              <Trash2 className="size-3" />
                            </button>
                          </li>
                        ))}
                      </ul>
                    )}
                  </div>
                ))}
              </div>
            )}

            <div className="flex flex-wrap items-center gap-2 border-t border-fg/[0.06] pt-3">
              <PrimaryButton onClick={() => void save()} disabled={!dirty || busy}>
                <span className="flex items-center gap-1.5">
                  <Check className="size-3" />
                  {busy ? "Saving…" : "Save changes"}
                </span>
              </PrimaryButton>
              <DangerButton onClick={() => void reset()} disabled={busy}>
                {resetArmed ? "Click again to reset" : "Reset memory"}
              </DangerButton>
              <span className="ml-auto flex items-center gap-1 text-[9.5px] text-fg/68">
                <BrainCircuit className="size-3" />
                {content.length.toLocaleString("en-US")} chars
              </span>
            </div>

            {path && (
              <p className="truncate font-mono text-[9.5px] text-fg/68" title={path}>
                {path}
              </p>
            )}
          </>
        )}

        {error && content !== null && (
          <p className="flex items-start gap-1.5 text-[10.5px] leading-4 text-danger-ink">
            <TriangleAlert className="mt-px size-3 shrink-0" />
            {error}
          </p>
        )}
      </SettingsCard>
    </>
  );
}
