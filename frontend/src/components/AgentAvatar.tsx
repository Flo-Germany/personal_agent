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
      className={`${SIZE_CLASSES[size]} flex shrink-0 items-center justify-center rounded-full bg-[linear-gradient(140deg,#c4b5fd_0%,#a78bfa_35%,#34d399_100%)] text-[#0c0c0c] shadow-[0_0_20px_-6px_rgba(167,139,250,0.55)] ${className}`}
    >
      <SparklesIcon className={ICON_CLASSES[size]} strokeWidth={1.9} />
    </div>
  );
}
