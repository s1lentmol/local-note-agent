import { useEffect, useRef, useState } from "react";

// Each visible resource owns one request and one timer. Cleanup also rejects stale replies.
export function useResource<T>(
  key: string,
  load: (signal: AbortSignal) => Promise<T>,
  shouldPoll: (value: T) => boolean,
) {
  const [state, setState] = useState<{
    key: string;
    data?: T;
    error?: Error;
    loading: boolean;
  }>({ key, loading: true });
  const [revision, setRevision] = useState(0);
  const loadRef = useRef(load);
  loadRef.current = load;
  const pollRef = useRef(shouldPoll);
  pollRef.current = shouldPoll;
  useEffect(() => {
    let disposed = false;
    let controller: AbortController | undefined;
    let timer: ReturnType<typeof setTimeout> | undefined;
    async function run() {
      if (document.hidden || disposed) return;
      controller?.abort();
      const current = new AbortController();
      controller = current;
      setState((previous) => ({
        key,
        data: previous.key === key ? previous.data : undefined,
        loading: true,
      }));
      try {
        const data = await loadRef.current(current.signal);
        if (disposed || current.signal.aborted) return;
        setState({ key, data, loading: false });
        if (pollRef.current(data)) timer = setTimeout(run, 2000);
      } catch (error) {
        if (!disposed && !current.signal.aborted)
          setState((previous) => ({
            ...previous,
            key,
            loading: false,
            error:
              error instanceof Error
                ? error
                : new Error("Не удалось загрузить данные."),
          }));
        // A failed poll stops until an explicit retry or returning to the visible tab.
      }
    }
    function visibility() {
      clearTimeout(timer);
      controller?.abort();
      if (!document.hidden) void run();
    }
    document.addEventListener("visibilitychange", visibility);
    void run();
    return () => {
      disposed = true;
      clearTimeout(timer);
      controller?.abort();
      document.removeEventListener("visibilitychange", visibility);
    };
  }, [key, revision]);
  return {
    ...(state.key === key
      ? state
      : { key, data: undefined, error: undefined, loading: true }),
    refresh: () => setRevision((value) => value + 1),
  };
}
