import { ChevronLeft, ChevronRight, Download, Search } from "lucide-react";
import { useCallback, useMemo, useState } from "react";
import { api } from "../api/client";
import { useApiData } from "../api/useApiData";
import { EmptyState } from "../components/EmptyState";
import { PageError, PageLoading } from "../components/PageState";
import { StatusBadge } from "../components/StatusBadge";

const strategyLabels: Record<string, string> = {
  model_1: "模型 1",
  model_2: "模型 2",
  model_3: "模型 3",
  rules_only: "不用模型"
};

const sampleTime = (value: string | null | undefined) => {
  if (!value) return "—";
  return new Intl.DateTimeFormat("zh-CN", {
    timeZone: "Asia/Shanghai",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hourCycle: "h23"
  }).format(new Date(value));
};

export function SignalsPage() {
  const [page, setPage] = useState(1);
  const pageSize = 100;
  const loader = useCallback(() => api.samples(page, pageSize), [page]);
  const { data, loading, error, refresh } = useApiData(loader);
  const [query, setQuery] = useState("");
  const [labelStatus, setLabelStatus] = useState<"all" | "pending" | "mature">("all");
  const [exporting, setExporting] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const items = useMemo(
    () => (data?.items ?? []).filter(
      (item) => (labelStatus === "all" || item.label_status === labelStatus)
        && `${item.symbol ?? ""} ${item.name ?? ""} ${item.address}`.toLowerCase().includes(query.toLowerCase())
    ),
    [data, labelStatus, query]
  );

  const exportSamples = async () => {
    setExporting(true);
    setNotice(null);
    try {
      const blob = await api.exportSamples();
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = "meme数据.csv";
      document.body.appendChild(link);
      link.click();
      link.remove();
      URL.revokeObjectURL(url);
      setNotice("已导出全部成熟样本");
    } catch (cause) {
      setNotice(cause instanceof Error ? cause.message : "样本集导出失败");
    } finally {
      setExporting(false);
    }
  };

  if (loading && !data) return <PageLoading />;
  if (error || !data) return <PageError message={error ?? "无数据"} retry={refresh} />;

  return (
    <div className="page-stack">
      <section className="page-heading">
        <div><p className="eyebrow">SAMPLE LEDGER</p><h1>样本采集</h1><p>所有通过硬编码规则筛选并写入当前特征代际的数据都会在这里出现，与模型是否评分或买入无关。</p></div>
        <div className="heading-actions">
          <StatusBadge label={`${data.total} 条入样`} tone="blue" />
          <button className="button button-secondary" disabled={exporting} onClick={() => void exportSamples()}>
            <Download size={17} />{exporting ? "导出中…" : "成熟样本导出"}
          </button>
        </div>
      </section>
      {notice && <div className="inline-notice">{notice}</div>}
      <section className="panel table-panel">
        <div className="table-toolbar">
          <label className="search-box"><Search size={17} /><input value={query} onChange={(e) => setQuery(e.target.value)} placeholder="搜索名称、symbol 或地址" /></label>
          <select value={labelStatus} onChange={(e) => setLabelStatus(e.target.value as "all" | "pending" | "mature")}>
            <option value="all">全部标签状态</option>
            <option value="pending">待成熟</option>
            <option value="mature">已成熟</option>
          </select>
          <StatusBadge label={data.feature_schema_version} />
        </div>
        {items.length ? (
          <>
            <div className="table-scroll"><table>
              <thead><tr><th>入样时间</th><th>Token</th><th>Launchpad</th><th>Age</th><th>标签状态</th><th>标签</th><th>模型评分</th><th>模型入选</th><th>模拟买入</th></tr></thead>
              <tbody>{items.map((item) => {
                const bought = item.bought_strategies.map((key) => strategyLabels[key] ?? key);
                return <tr key={item.id}>
                  <td>{sampleTime(item.collected_at)}</td>
                  <td><strong>{item.symbol ?? item.name ?? "Unknown"}</strong><span className="table-sub mono">{item.address.slice(0, 6)}…{item.address.slice(-5)}</span></td>
                  <td>{item.launchpad ?? "—"}</td>
                  <td className="mono">{item.age_seconds == null ? "—" : `${item.age_seconds}s`}</td>
                  <td><StatusBadge tone={item.label_status === "mature" ? "blue" : "gold"} label={item.label_status === "mature" ? "已成熟" : "待成熟"} /></td>
                  <td>{item.tag == null ? "—" : `tag ${item.tag}`}</td>
                  <td>{item.prediction_count ? `${item.prediction_count}/3` : "未评分"}</td>
                  <td><StatusBadge tone={item.selected_count ? "blue" : "neutral"} label={item.selected_count ? `${item.selected_count} 个模型入选` : "未入选"} /></td>
                  <td>{bought.length ? bought.join("、") : "未买入"}</td>
                </tr>;
              })}</tbody>
            </table></div>
            <div className="pagination-bar">
              <span>共 {data.total} 条</span>
              <button className="icon-button pagination-button" disabled={data.page <= 1} onClick={() => setPage(Math.max(1, data.page - 1))} aria-label="上一页"><ChevronLeft size={16} /></button>
              <span>第 {data.page} / {data.total_pages} 页</span>
              <button className="icon-button pagination-button" disabled={data.page >= data.total_pages} onClick={() => setPage(Math.min(data.total_pages, data.page + 1))} aria-label="下一页"><ChevronRight size={16} /></button>
            </div>
          </>
        ) : <EmptyState title="没有匹配样本" detail="只有未通过硬编码规则的池子不会进入这里；通过后会先写入 samples，再进入模型评分和模拟交易链路。" />}
      </section>
    </div>
  );
}
