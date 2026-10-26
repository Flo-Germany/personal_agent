import { useEffect } from "react";

import { isDesktop, recordFrameStats } from "../lib/desktop";

/**
 * Samples requestAnimationFrame deltas while the agent streams and writes the
 * result to the desktop shell (`desktop-state.json`). The Phase 23 QA script
 * asserts both live streaming frames and the synthetic layout stress run.
 */
export function useFrameStats(active: boolean, mode: "live" | "stress") {
  useEffect(() => {
    if (!active || !isDesktop()) {
      return;
    }

    let raf = 0;
    let last = performance.now();
    let windowStart = last;
    let windowFrames = 0;
    let totalFrames = 0;
    let maxFrame = 0;
    let longFrames = 0;

    const flush = () => {
      const seconds = (last - windowStart) / 1000;
      const fps = seconds > 0.2 ? windowFrames / seconds : 0;
      if (totalFrames >= 5) {
        void recordFrameStats({
          fps,
          maxFrameMs: maxFrame,
          frames: totalFrames,
          longFrames,
          mode,
        });
      }
      windowStart = last;
      windowFrames = 0;
    };

    const loop = (now: number) => {
      const delta = now - last;
      last = now;
      windowFrames += 1;
      totalFrames += 1;
      if (delta > maxFrame) {
        maxFrame = delta;
      }
      if (delta > 50) {
        longFrames += 1;
      }
      if (now - windowStart >= 1200) {
        flush();
      }
      raf = requestAnimationFrame(loop);
    };

    raf = requestAnimationFrame(loop);
    return () => {
      cancelAnimationFrame(raf);
      flush();
    };
  }, [active, mode]);
}
