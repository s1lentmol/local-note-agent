import { useEffect, useRef } from "react";

// A lifetime token is replaced on every effect setup, including StrictMode replay.
// Ignore late mutation replies locally; the server may still commit the write.
export function useOperationScope() {
  const lifetime = useRef({ active: true });
  useEffect(() => {
    const current = { active: true };
    lifetime.current = current;
    return () => {
      current.active = false;
    };
  }, []);
  return () => {
    const current = lifetime.current;
    const path = window.location.hash;
    // Also cover a changed hash before React has cleaned up the old screen.
    return () => current.active && window.location.hash === path;
  };
}
