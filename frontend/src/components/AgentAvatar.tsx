import { SparklesIcon } from "./icons";

type AvatarSize = "sm" | "md" | "lg";

const SIZE_CLASSES: Record<AvatarSize, string> = {
  sm: "size-7",
  md: "size-9",
  lg: "size-16",
};

const ICON_CLASSES: Record<AvatarSize, string> = {
  sm: "size-3.5",
  md: "size-4",
  lg: "size-7",
};

type AgentAvatarProps = {
  size?: AvatarSize;
  className?: string;
};

export function AgentAvatar({ size = "md", className = "" }: AgentAvatarProps) {
  return (
    <div
      className={`${SIZE_CLASSES[size]} flex shrink-0 items-center justify-center rounded-full bg-accent bg-[linear-gradient(140deg,#5ac8fa_0%,#0068e0_55%,#4b48c9_100%)] text-white shadow-[0_0_20px_-6px_rgba(10,132,255,0.6)] ${className}`}
    >
      <SparklesIcon className={ICON_CLASSES[size]} strokeWidth={1.9} />
    </div>
  );
}
