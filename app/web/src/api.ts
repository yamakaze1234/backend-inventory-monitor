import { useCallback, useEffect, useRef, useState } from 'react';

// fragment 不会发给 HTTP 服务；仅同一次打开的页面持有本机会话凭据。
const token = location.hash.slice(1);
export async function api<T>(path: string, body?: unknown, signal?: AbortSignal): Promise<T> {
  const response = await fetch(path, { method: body === undefined ? 'GET' : 'POST', signal,
    headers: { 'X-Console-Token': token, ...(body === undefined ? {} : { 'Content-Type': 'application/json' }) },
    body: body === undefined ? undefined : JSON.stringify(body) });
  const value = await response.json();
  if (!response.ok) throw new Error(value.error || '请求未完成');
  return value;
}

/** 可见时轮询、不重叠请求；相同快照不触发 React 重绘，卸载时取消请求。 */
export function useSnapshot<T>(path: string, interval = 5000) {
  const [data, setData] = useState<T>();
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(true);
  const [revision, setRevision] = useState(0);
  const previous = useRef('');
  const refresh = useCallback(() => setRevision(v => v + 1), []);
  useEffect(() => {
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    let stopped = false;
    previous.current = '';
    setLoading(true);
    const read = async () => {
      if (!document.hidden) {
        try {
          const value = await api<T>(path, undefined, controller.signal);
          if (stopped) return;
          const serialized = JSON.stringify(value);
          if (serialized !== previous.current) { previous.current = serialized; setData(value); }
          setError('');
        } catch (e) {
          if (!stopped) setError(e instanceof Error ? e.message : '读取失败');
        } finally { if (!stopped) setLoading(false); }
      }
      if (!stopped) timer = setTimeout(read, interval);
    };
    void read();
    return () => { stopped = true; controller.abort(); clearTimeout(timer); };
  }, [path, interval, revision]);
  return { data, error, loading, refresh };
}
