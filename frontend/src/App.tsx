import type { FormEvent } from "react";

function App() {
  const handleSubmit = (event: FormEvent) => {
    event.preventDefault();
  };

  return (
    <div className="flex h-screen flex-col bg-[#0b0f14] text-slate-100">
      <header className="flex items-center gap-3 border-b border-white/10 px-4 py-3">
        <div className="flex size-9 items-center justify-center rounded-xl bg-emerald-400/15 text-sm font-semibold tracking-tight text-emerald-300 ring-1 ring-emerald-400/30">
          PA
        </div>
        <div className="min-w-0 flex-1">
          <h1 className="text-sm font-semibold tracking-tight">Personal Agent</h1>
          <p className="text-[11px] text-slate-400">Local · Private · Offline-ready</p>
        </div>
        <span className="flex items-center gap-1.5 rounded-full border border-emerald-400/20 bg-emerald-400/10 px-2 py-1 text-[10px] font-medium text-emerald-300">
          <span className="size-1.5 rounded-full bg-emerald-400" />
          Setup
        </span>
      </header>

      <main className="flex flex-1 flex-col items-center justify-center px-8 text-center">
        <div className="mb-5 flex size-14 items-center justify-center rounded-2xl border border-white/10 bg-white/5">
          <svg
            aria-hidden="true"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            strokeWidth="1.5"
            className="size-7 text-emerald-300"
          >
            <path
              strokeLinecap="round"
              strokeLinejoin="round"
              d="M8 9h8M8 13h5m-7 7 2.4-2.4A8 8 0 1 0 4 12.5V20Z"
            />
          </svg>
        </div>
        <h2 className="text-lg font-semibold tracking-tight">Foundation ready</h2>
        <p className="mt-2 max-w-xs text-sm leading-6 text-slate-400">
          The desktop shell and local backend are in place. Chat, tool calling,
          and Ollama wiring come next.
        </p>
        <div className="mt-6 flex flex-wrap justify-center gap-2 text-[10px] text-slate-500">
          <span className="rounded-md border border-white/10 px-2 py-1">Tauri v2</span>
          <span className="rounded-md border border-white/10 px-2 py-1">React</span>
          <span className="rounded-md border border-white/10 px-2 py-1">FastAPI</span>
        </div>
      </main>

      <footer className="border-t border-white/10 p-4">
        <form onSubmit={handleSubmit} className="flex items-end gap-2">
          <input
            type="text"
            disabled
            placeholder="Chat will be enabled next"
            aria-label="Message"
            className="h-11 min-w-0 flex-1 rounded-xl border border-white/10 bg-white/5 px-3 text-sm text-slate-200 placeholder:text-slate-500 focus:outline-none disabled:cursor-not-allowed"
          />
          <button
            type="submit"
            disabled
            aria-label="Send message"
            className="flex size-11 shrink-0 items-center justify-center rounded-xl bg-emerald-400 text-slate-950 disabled:cursor-not-allowed disabled:opacity-40"
          >
            <svg
              aria-hidden="true"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              strokeWidth="2"
              className="size-5"
            >
              <path strokeLinecap="round" strokeLinejoin="round" d="m5 12 14-7-4 14-3-6-7-1Z" />
            </svg>
          </button>
        </form>
        <p className="mt-2 text-center text-[10px] text-slate-600">
          Backend connection is not wired up yet
        </p>
      </footer>
    </div>
  );
}

export default App;
