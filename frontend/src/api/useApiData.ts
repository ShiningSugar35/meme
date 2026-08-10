import { useCallback, useEffect, useState } from "react";

export function useApiData<T>(loader: () => Promise<T>) {
  const [data, setData] = useState<T | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setData(await loader());
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "未知错误");
    } finally {
      setLoading(false);
    }
  }, [loader]);

  useEffect(() => { void refresh(); }, [refresh]);
  return { data, loading, error, refresh };
}

