export function PageLoading() {
  return <div className="page-state"><span className="loader" />正在读取系统状态…</div>;
}

export function PageError({ message, retry }: { message: string; retry: () => void }) {
  return <div className="page-state error-state"><strong>页面暂时不可用</strong><span>{message}</span><button className="button button-secondary" onClick={retry}>重试</button></div>;
}

