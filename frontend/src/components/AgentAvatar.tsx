import { useState } from "react";

import { SparklesIcon } from "./icons";

type AvatarSize = "sm" | "md" | "lg" | "xl";

const SIZE_CLASSES: Record<AvatarSize, string> = {
  sm: "size-7",
  md: "size-9",
  lg: "size-16",
  xl: "size-11",
};

const ICON_CLASSES: Record<AvatarSize, string> = {
  sm: "size-3.5",
  md: "size-4",
  lg: "size-7",
  xl: "size-4.5",
};

/**
 * Drop a square `avatar.png` (or `.svg`/`.jpg`/`.webp`) into
 * `src/assets/agent-avatar/` to replace the built-in avatar.
 * See the README in that folder.
 */
const AVATAR_MODULES = import.meta.glob(
  "../assets/agent-avatar/*.{png,svg,jpg,jpeg,webp}",
  { eager: true, query: "?url", import: "default" },
) as Record<string, string>;

const AVATAR_URL =
  Object.keys(AVATAR_MODULES)
    .sort((left, right) => left.localeCompare(right))
    .map((key) => AVATAR_MODULES[key])[0] ?? "";

type AgentAvatarProps = {
  size?: AvatarSize;
  className?: string;
};

export function AgentAvatar({ size = "md", className = "" }: AgentAvatarProps) {
  const [imageFailed, setImageFailed] = useState(false);

  return (
    <div
      className={`${SIZE_CLASSES[size]} flex shrink-0 items-center justify-center overflow-hidden rounded-full bg-accent bg-[linear-gradient(140deg,#5ac8fa_0%,#0068e0_55%,#4b48c9_100%)] text-white shadow-[0_0_20px_-6px_rgba(10,132,255,0.6)] ${className}`}
    >
      {AVATAR_URL && !imageFailed ? (
        <img
          src={AVATAR_URL}
          alt=""
          aria-hidden="true"
          draggable={false}
          className="size-full rounded-full object-cover"
          onError={() => setImageFailed(true)}
        />
      ) : (
        <SparklesIcon className={ICON_CLASSES[size]} strokeWidth={1.9} />
      )}
    </div>
  );
}
