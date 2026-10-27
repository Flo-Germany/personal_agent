import type { Transition, Variants } from "framer-motion";

/**
 * Shared motion language: entrances ease out, exits ease in, interface
 * elements use a lightly overshooting spring. Nothing runs linear.
 */
export const EASE_OUT = [0.16, 1, 0.3, 1] as const;
export const EASE_IN = [0.7, 0, 0.84, 0] as const;

export const SPRING: Transition = {
  type: "spring",
  stiffness: 420,
  damping: 28,
  mass: 0.72,
};

export const ENTRANCE: Variants = {
  hidden: { opacity: 0, y: -4 },
  visible: {
    opacity: 1,
    y: 0,
    transition: { duration: 0.18, ease: EASE_OUT },
  },
  exit: { opacity: 0, y: -2, transition: { duration: 0.13, ease: EASE_IN } },
};

export const CONTENT_ENTRANCE: Variants = {
  hidden: { opacity: 0, y: 3 },
  visible: {
    opacity: 1,
    y: 0,
    transition: { duration: 0.2, ease: EASE_OUT },
  },
  exit: { opacity: 0, y: 2, transition: { duration: 0.14, ease: EASE_IN } },
};

export const SPRING_POP: Variants = {
  hidden: { opacity: 0, scale: 0.92 },
  visible: { opacity: 1, scale: 1, transition: SPRING },
  exit: { opacity: 0, scale: 0.95, transition: { duration: 0.12, ease: EASE_IN } },
};
