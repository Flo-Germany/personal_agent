export type QuickPrompt = {
  label: string;
  prompt: string;
};

export const QUICK_PROMPTS: QuickPrompt[] = [
  {
    label: "Newest files",
    prompt: "Search my Downloads folder for the newest 5 files.",
  },
  {
    label: "Folder summary",
    prompt: "What files are in my Documents folder? Give me a short overview.",
  },
  {
    label: "Make a plan",
    prompt: "Help me make a clear, prioritized plan for my day.",
  },
  {
    label: "Organize files",
    prompt:
      "Look at my Downloads folder and suggest how I could organize it.",
  },
];
