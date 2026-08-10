import { Search } from "lucide-react";
import { useCallback, useMemo, useState } from "react";
import { api } from "../api/client";
import { useApiData } from "../api/useApiData";
import { EmptyState } from "../components/EmptyState";
import { PageError, PageLoading } from "../components/PageState";
import { StatusBadge } from "../components/StatusBadge";

export function SignalsPage() {
  const loader = useCallback(() => api.signals(), []);
  const { data, loading, error, refresh } = useApiData(loader);
  const [query, setQuery] = useState("");
  const [profile, setProfile] = useState("all");
  const items = useMemo(() => (data?.items ?? []).filter((item) => (profile === "all" || item.profile === profile) && `${item.symbol ?? ""} ${item.address}`.toLowerCase().includes(query.toLowerCase())), [data, profile, query]);
  if (loading && !data) return <PageLoading />;
  if (error || !data) return <PageError message={error ?? "无数据"} retry={refresh} />;
  return <div className="page-stack"><section className="page-heading"><div><p className="eyebrow">SIGNAL LEDGER</p><h1>信号</h1><p>每个通过初筛的样本都会留痕，无论最终是否开仓。</p></div></section><section className="panel table-panel"><div className="table-toolbar"><label className="search-box"><Search size={17} /><input value={query} onChange={(e) => setQuery(e.target.value)} placeholder="搜索 symbol 或地址" /></label><select value={profile} onChange={(e) => setProfile(e.target.value)}><option value="all">全部档位</option><option value="aggressive">激进</option><option value="balanced">平衡</option><option value="conservative">保守</option><option value="shadow">影子</option></select></div>{items.length ? <div className="table-scroll"><table><thead><tr><th>时间</th><th>Token</th><th>Launchpad</th><th>档位</th><th>概率 / 阈值</th><th>决策</th><th>成熟标签</th><th>模型</th></tr></thead><tbody>{items.map((item) => <tr key={item.id}><td>{new Date(item.predicted_at).toLocaleString("zh-CN")}</td><td><strong>{item.symbol ?? item.name ?? "Unknown"}</strong><span className="table-sub mono">{item.address.slice(0, 6)}…{item.address.slice(-5)}</span></td><td>{item.launchpad ?? "—"}</td><td><StatusBadge label={item.profile} /></td><td className="mono">{(item.probability * 100).toFixed(1)}% / {(item.threshold * 100).toFixed(1)}%</td><td><StatusBadge tone={item.selected ? "blue" : "neutral"} label={item.selected ? "入选" : "仅记录"} /></td><td>{item.tag == null ? "待补齐" : `tag ${item.tag}`}</td><td className="mono muted-cell">{item.model_version}</td></tr>)}</tbody></table></div> : <EmptyState title="没有匹配信号" detail="采集器和模型开始运行后，评分记录会自动出现。" />}</section></div>;
}

