import { Bot, RefreshCw, RotateCcw, ShieldCheck } from "lucide-react";
import { useCallback, useState } from "react";
import { api } from "../api/client";
import { useApiData } from "../api/useApiData";
import { EmptyState } from "../components/EmptyState";
import { PageError, PageLoading } from "../components/PageState";
import { StatusBadge } from "../components/StatusBadge";

const pct = (value: unknown) => typeof value === "number" ? `${(value * 100).toFixed(1)}%` : "—";
const runBlockers = (summary: Record<string, unknown>) => {
  const promotion = summary.promotion;
  if (!promotion || typeof promotion !== "object") return [] as string[];
  const blockers = (promotion as Record<string, unknown>).blockers;
  return Array.isArray(blockers) ? blockers.map(String) : [];
};

export function ModelsPage() {
  const loader = useCallback(() => api.models(), []);
  const { data, loading, error, refresh } = useApiData(loader);
  const [training, setTraining] = useState(false);
  const [rollingBack, setRollingBack] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [selectedFeatures, setSelectedFeatures] = useState<string[] | null>(null);

  if (loading && !data) return <PageLoading />;
  if (error || !data) return <PageError message={error ?? "无数据"} retry={refresh} />;

  const catalog = data.feature_catalog;
  const activeRun = data.training_runs.find((run) => ["queued", "running"].includes(run.status));
  const activeFeatures = selectedFeatures ?? catalog.default_features;
  const selected = new Set(activeFeatures);
  const toggleFeature = (name: string) => {
    const next = new Set(activeFeatures);
    if (next.has(name)) next.delete(name); else next.add(name);
    setSelectedFeatures(catalog.available_features.filter((item) => next.has(item)));
  };
  const train = async () => {
    if (!activeFeatures.length) {
      setNotice("至少选择一个训练特征");
      return;
    }
    setTraining(true);
    setNotice(null);
    try {
      const result = await api.trainModel(activeFeatures);
      setNotice(`训练任务 ${result.run_id} 已${result.status}，使用 ${activeFeatures.length} 个特征`);
      await refresh();
    } catch (cause) {
      setNotice(cause instanceof Error ? cause.message : "训练失败");
    } finally {
      setTraining(false);
    }
  };
  const rollback = async (modelId: string) => {
    if (!window.confirm("确认将这个已退役模型恢复为 Champion？当前 Champion 会同时退役。")) return;
    setRollingBack(modelId);
    setNotice(null);
    try {
      const result = await api.rollbackModel(modelId);
      setNotice(`已回滚至 ${result.champion.version}`);
      await refresh();
    } catch (cause) {
      setNotice(cause instanceof Error ? cause.message : "模型回滚失败");
    } finally {
      setRollingBack(null);
    }
  };

  return (
    <div className="page-stack">
      <section className="page-heading">
        <div>
          <p className="eyebrow">MODEL REGISTRY</p>
          <h1>模型中心</h1>
          <p>一个 Champion、三档阈值；候选只在相同时间外窗口公平比较。训练特征可按数据成熟度自行选择。</p>
        </div>
        <div className="heading-actions">
          <button className="button button-primary" disabled={training || Boolean(activeRun) || !activeFeatures.length} onClick={() => void train()}>
            <RefreshCw size={17} className={training || activeRun?.status === "running" ? "spinning" : ""} />
            {activeRun ? `训练任务${activeRun.status === "running" ? "执行中" : "排队中"}` : training ? "提交中…" : `按所选 ${activeFeatures.length} 个特征训练`}
          </button>
        </div>
      </section>
      {notice && <div className="inline-notice">{notice}</div>}

      <section className="panel feature-panel">
        <div className="panel-heading">
          <div>
            <h2>训练特征</h2>
            <p>默认特征来自 README 冻结口径；可选特征会持续采集，覆盖率足够后可手动加入下一次训练。</p>
          </div>
          <div className="heading-actions">
            <button className="button button-secondary" onClick={() => setSelectedFeatures(catalog.default_features)}>
              <RotateCcw size={15} />恢复默认
            </button>
            <button className="button button-secondary" onClick={() => setSelectedFeatures(catalog.available_features)}>
              全选可用
            </button>
          </div>
        </div>
        <div className="feature-grid">
          {catalog.items.map((item) => (
            <label className={`feature-option ${selected.has(item.name) ? "feature-selected" : ""}`} key={item.name}>
              <input type="checkbox" checked={selected.has(item.name)} onChange={() => toggleFeature(item.name)} />
              <span className="feature-name mono">{item.name}</span>
              <span className="feature-meta">
                覆盖 {item.available_rows}/{item.total_mature_rows} · {pct(item.coverage)}
                {item.default_enabled ? " · 默认" : " · 可选"}
                {item.champion_enabled ? " · Champion正在使用" : ""}
              </span>
            </label>
          ))}
        </div>
      </section>

      {data.champion ? (
        <section className="panel champion-card">
          <div className="champion-icon"><ShieldCheck size={24} /></div>
          <div className="champion-main">
            <div className="champion-title">
              <StatusBadge tone="gold" label="CHAMPION" />
              <h2>{data.champion.algorithm}</h2>
              {data.champion.early_stage && <StatusBadge tone="orange" label="EARLY STAGE" />}
            </div>
            <p>{data.champion.version} · 训练于 {new Date(data.champion.trained_at).toLocaleString("zh-CN")}</p>
            <div className="champion-metrics">
              <div><span>Precision</span><strong>{pct(data.champion.metrics.precision)}</strong></div>
              <div><span>Recall</span><strong>{pct(data.champion.metrics.recall)}</strong></div>
              <div><span>真实效用资格</span><strong>{data.champion.metrics.promotion_eligible === false ? "否" : "是"}</strong></div>
              <div><span>特征数</span><strong>{data.champion.feature_names.length}</strong></div>
            </div>
          </div>
          <div className="threshold-stack">
            {(["aggressive", "balanced", "conservative"] as const).map((profile) => (
              <div key={profile}><span>{profile}</span><strong>{data.champion!.thresholds[profile]?.toFixed(3) ?? "—"}</strong></div>
            ))}
          </div>
        </section>
      ) : (
        <EmptyState title="尚无 Champion 模型" detail="选择训练特征后，系统会运行严格的时序验证并保存候选。" />
      )}

      <section className="panel table-panel">
        <div className="panel-heading"><div><h2>模型版本</h2><p>候选、晋级、拒绝与回滚记录</p></div></div>
        {data.items.length ? (
          <div className="table-scroll"><table><thead><tr><th>版本</th><th>算法</th><th>状态</th><th>Precision</th><th>Recall</th><th>特征数</th><th>训练时间</th><th>说明</th><th>操作</th></tr></thead><tbody>
            {data.items.map((model) => <tr key={model.id}><td className="mono">{model.version}</td><td>{model.algorithm}</td><td><StatusBadge tone={model.status === "champion" ? "gold" : "neutral"} label={model.status} /></td><td>{pct(model.metrics.precision)}</td><td>{pct(model.metrics.recall)}</td><td>{model.feature_names.length}</td><td>{new Date(model.trained_at).toLocaleString("zh-CN")}</td><td className="muted-cell">{model.rejection_reason ?? (model.early_stage ? "数据不足120天" : "—")}</td><td>{model.status === "retired" ? <button className="button button-secondary" disabled={rollingBack === model.id} onClick={() => void rollback(model.id)}>{rollingBack === model.id ? "回滚中…" : "恢复为 Champion"}</button> : "—"}</td></tr>)}
          </tbody></table></div>
        ) : <EmptyState title="没有模型版本" detail="训练记录会在此形成可审计的版本链。" />}
      </section>

      <section className="panel table-panel">
        <div className="panel-heading"><div><h2>训练与自更新记录</h2><p>手动、周训、启动补跑和 degraded 重训均使用持久化任务</p></div></div>
        {data.training_runs.length ? (
          <div className="table-scroll"><table><thead><tr><th>触发</th><th>状态</th><th>特征</th><th>重试</th><th>计划时间</th><th>晋级</th><th>结果 / Blocker</th></tr></thead><tbody>
            {data.training_runs.map((run) => {
              const blockers = runBlockers(run.summary);
              return <tr key={run.id}><td>{run.trigger}</td><td><StatusBadge tone={run.status === "failed" ? "orange" : run.status === "completed" ? "blue" : "neutral"} label={run.status} /></td><td>{run.request.feature_names?.length ?? 0}</td><td>{run.retry_count}</td><td>{run.scheduled_for ? new Date(run.scheduled_for).toLocaleString("zh-CN") : "手动"}</td><td>{run.promoted ? "是" : "否"}</td><td className="muted-cell">{run.error_message ?? (blockers.join("；") || (run.promoted ? "已切换 Champion" : "已完成"))}</td></tr>;
            })}
          </tbody></table></div>
        ) : <EmptyState title="暂无训练任务" detail="手动训练或周日自动训练后，任务状态和晋级原因会显示在这里。" />}
      </section>

      <section className="info-strip"><Bot size={19} /><div><strong>奥卡姆选择原则</strong><span>收益和 Precision 接近时，优先选择更少特征、更低复杂度且最差窗口更稳定的模型。</span></div></section>
    </div>
  );
}
