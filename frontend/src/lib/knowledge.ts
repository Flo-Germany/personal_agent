/** Small helpers for the knowledge-base manager (section/bullet editing). */

export type KnowledgeFact = {
  /** Stable index of the line inside the document. */
  index: number;
  text: string;
};

export type KnowledgeSection = {
  title: string;
  facts: KnowledgeFact[];
};

const HEADING = /^##\s+(.+?)\s*$/;

export function parseKnowledge(content: string): {
  intro: string;
  sections: KnowledgeSection[];
} {
  const lines = content.split(/\r?\n/);
  const sections: KnowledgeSection[] = [];
  let intro: string[] = [];
  let current: KnowledgeSection | null = null;

  for (let index = 0; index < lines.length; index += 1) {
    const line = lines[index];
    const heading = HEADING.exec(line);
    if (heading) {
      current = { title: heading[1], facts: [] };
      sections.push(current);
      continue;
    }
    if (!current) {
      intro.push(line);
      continue;
    }
    if (/^\s*[-*]\s+/.test(line)) {
      current.facts.push({
        index,
        text: line.replace(/^\s*[-*]\s+/, "").trim(),
      });
    }
  }

  return { intro: intro.join("\n").trim(), sections };
}

/** Remove one bullet without touching the rest of the document. */
export function removeKnowledgeFact(content: string, fact: KnowledgeFact): string {
  const lines = content.split(/\r?\n/);
  if (fact.index < 0 || fact.index >= lines.length) {
    return content;
  }
  lines.splice(fact.index, 1);
  return lines.join("\n");
}

/** Append a bullet to an existing section (or create the section). */
export function addKnowledgeFact(
  content: string,
  sectionTitle: string,
  fact: string,
): string {
  const clean = fact.replace(/\s+/g, " ").trim();
  if (!clean) {
    return content;
  }
  const lines = content.split(/\r?\n/);
  const heading = `## ${sectionTitle}`;
  let headingIndex = lines.findIndex((line) => line.trim() === heading);

  if (headingIndex === -1) {
    const trimmed = content.replace(/\s+$/, "");
    return `${trimmed}\n\n${heading}\n\n- ${clean}\n`;
  }

  // Insert after the last non-empty line of the section, before the next
  // heading, keeping exactly one blank line as a separator.
  let lastContent = headingIndex;
  for (let index = headingIndex + 1; index < lines.length; index += 1) {
    if (HEADING.test(lines[index])) {
      break;
    }
    if (lines[index].trim()) {
      lastContent = index;
    }
  }
  const insertAt = lastContent + 1;
  const alreadyBlank = insertAt < lines.length && !lines[insertAt].trim();
  lines.splice(
    insertAt,
    0,
    ...(alreadyBlank
      ? [`- ${clean}`, ""]
      : ["", `- ${clean}`, ""]),
  );
  return lines.join("\n").replace(/\n{3,}/g, "\n\n");
}
