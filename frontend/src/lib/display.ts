/** Short, human model label: "Qwen3 4B Instruct 2507 (Q4_K_M) · llama.cpp · …" */
export function shortModelName(value: string): string {
  const first = value.split(" · ")[0] ?? value;
  const withoutQuant = first.replace(/\s*\([^)]*\)\s*$/, "").trim();
  return withoutQuant || value;
}
