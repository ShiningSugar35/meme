import { Bot, RefreshCw, RotateCcw, ShieldCheck } from "lucide-react";
import { useCallback, useState } from "react";
import { api } from "../api/client";
import type { EvaluationMetricPayload, ModelVersion } from "../api/types";
import { useApiData } from "../api/useApiData";
import { EmptyState } from "../components/EmptyState";
import { PageError, PageLoading } from "../components/PageState";
import { StatusBadge } from "../components/StatusBadge";

const pct = (value: unknown, digits = 1) => typeof value === "number" && Number.isFinite(value) ? `${(value * 100).toFixed(digits)}%` : "—";
const score = (value: unknown) => typeof value === "number" && Number.isFinite(value) ? value.toFixed(3) : "—";
const money = (value: unknown) => typeof value === "number" && Number.isFinite(value) ? new Intl.NumberFormat("zh-CN", { style: "currency", currency: "USD", currencyDisplay: "narrowSymbol", maximumFractionDigits: 0 }).format(value) : "—";
const algorithmNames: Record<string, string> = { logistic_regression: "Logistic Regression", decision_tree: "Decision Tree", hist_gradient_boosting: "Histogram Gradient Boosting", gradient_boosting: "Gradient Boosting", ada_boost: "AdaBoost", extra_trees: "Extra Trees", random_forest: "Random Forest", rbf_svm: "RBF SVM", xgboost: "XGBoost", lightgbm: "LightGBM", catboost: "CatBoost", flaml_automl: "FLAML AutoML" };
const algorithmName = (algorithm: string) => algorithmNames[algorithm] ?? algorithm;
const modelLabel = (model: ModelVersion) => {
  const parts = new Intl.DateTimeFormat("en-US", {
    timeZone: "Asia/Shanghai",
    year: "numeric",
    month: "2-digit",
    day: "2-digit"
  }).formatToParts(new Date(model.trained_at));
  const pick = (type: Intl.DateTimeFormatPartTypes) => parts.find((item) => item.type === type)?.value ?? "";
  return `${pick("year")}${pick("month")}${pick("day")}-${algorithmName(model.algorithm)}`;
};
const asRecord = (value: unknown) => value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : {};
const asArray = (value: unknown) => Array.isArray(value) ? value as Array<Record<string, unknown>> : [];

export function ModelsPage() {
  const loader = useCallback(() => api.models(), []);
  const { data, loading, error, refresh } = useApiData(loader);
  const [training, setTraining] = useState(false);
  const [savingFeatures, setSavingFeatures] = useState(false);
  const [rollingBack, setRollingBack] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [selectedFeatures, setSelectedFeatures] = useState<string[] | null>(null);

  if (loading && !data) return <PageLoading />;
  if (error || !data) return <PageError message={error ?? "无数据"} retry={refresh} />;

  const catalog = data.feature_catalog;
  const activeRun = data.training_runs.find((run) => ["queued", "running"].includes(run.status));
  const activeFeatures = selectedFeatures ?? catalog.selected_features;
  const selected = new Set(activeFeatures);
  const saveFeatureSelection = async (nextFeatures: string[]) => {
    if (!nextFeatures.length) return setNotice("至少保留一个候选特征");
    const previous = activeFeatures;
    setSelectedFeatures(nextFeatures);
    setSavingFeatures(true);
    try {
      const result = await api.saveModelFeatureSelection(nextFeatures);
      setSelectedFeatures(result.selected_features);
      setNotice(`候选特征已自动保存：${result.count} 个；后续自动训练将从这组候选中重新评估。`);
    } catch (cause) {
      setSelectedFeatures(previous);
      setNotice(cause instanceof Error ? cause.message : "候选特征保存失败");
    } finally {
      setSavingFeatures(false);
    }
  };
  const toggleFeature = (name: string) => {
    if (savingFeatures) return;
    const next = new Set(activeFeatures);
    if (next.has(name)) next.delete(name); else next.add(name);
    void saveFeatureSelection(catalog.available_features.filter((item) => next.has(item)));
  };
  const train = async () => {
    if (!activeFeatures.length) return setNotice("至少选择一个训练特征");
    setTraining(true); setNotice(null);
    try {
      const result = await api.trainModel(activeFeatures);
      setNotice(`Top 3 训练任务 ${result.run_id} 已${result.status}，候选特征池 ${activeFeatures.length} 个`);
      await refresh();
    } catch (cause) { setNotice(cause instanceof Error ? cause.message : "训练失败"); }
    finally { setTraining(false); }
  };
  const rollback = async (modelId: string) => {
    if (!window.confirm("确认将这个已退役的 Rank 1 模型恢复到 Top 3 首位？")) return;
    setRollingBack(modelId); setNotice(null);
    try { const result = await api.rollbackModel(modelId); setNotice(`已恢复 ${modelLabel(result.champion)}`); await refresh(); }
    catch (cause) { setNotice(cause instanceof Error ? cause.message : "模型回滚失败"); }
    finally { setRollingBack(null); }
  };

  const activeModels = [...data.active_models].sort((a, b) => Number(a.active_slot ?? 99) - Number(b.active_slot ?? 99));
  const rank1 = activeModels[0];
  const ruleBaseline = rank1?.metrics.rule_baseline_final as EvaluationMetricPayload | undefined;
  const latestCompleted = data.training_runs.find((run) => run.status === "completed" && asArray(asRecord(run.summary).top_models).length > 0);
  const candidates = latestCompleted ? asArray(asRecord(latestCompleted.summary).candidates) : [];

  return <div className="page-stack">
    <section className="page-heading"><div><p className="eyebrow">MODEL REGISTRY</p><h1>模型中心</h1><p>候选模型统一在时间外开发折上评估，按经济得分、泛化稳定性与奥卡姆特征原则选出 Top 3；最终时间留出只用于审计。</p></div><div className="heading-actions"><button className="button button-primary" disabled={training || savingFeatures || Boolean(activeRun) || !activeFeatures.length} onClick={() => void train()}><RefreshCw size={17} className={training || activeRun?.status === "running" ? "spinning" : ""} />{activeRun ? `训练任务${activeRun.status === "running" ? "执行中" : "排队中"}` : training ? "提交中…" : `重新评估 Top 3`}</button></div></section>
    {notice && <div className="inline-notice">{notice}</div>}

    <section className="panel table-panel"><div className="panel-heading"><div><h2>当前 Top 3</h2><p>综合分 S = 0.60 × 经济得分 E + 0.40 × 泛化得分 G</p></div><StatusBadge tone={activeModels.length === 3 ? "blue" : "orange"} label={`${activeModels.length}/3 就绪`} /></div>
      {activeModels.length ? <div className="table-scroll"><table><thead><tr><th>排名</th><th>模型</th><th>特征</th><th>决策线</th><th>Precision</th><th>Recall</th><th>理论收益</th><th>E</th><th>G</th><th>S</th></tr></thead><tbody>{activeModels.map((model) => <tr key={model.id}><td><StatusBadge tone={model.active_slot === 1 ? "gold" : "blue"} label={`Top ${model.active_slot ?? "—"}`} /></td><td><strong>{modelLabel(model)}</strong><span className="table-sub">{algorithmName(model.algorithm)}</span></td><td>{model.feature_names.length}</td><td className="mono">{typeof model.active_threshold === "number" ? model.active_threshold.toFixed(3) : model.thresholds.decision?.toFixed(3) ?? "—"}</td><td>{pct(model.metrics.precision)}</td><td>{pct(model.metrics.recall)}</td><td>{money(model.metrics.fixed_profit_usd)}</td><td>{score(model.metrics.economic_score)}</td><td>{score(model.metrics.generalization_score)}</td><td><strong>{score(model.metrics.composite_score ?? model.active_composite_score)}</strong></td></tr>)}</tbody></table></div> : <EmptyState title="Top 3 尚未生成" detail="启动一次新训练后，系统会从候选池中选出三个模型。" />}
      {ruleBaseline && <div className="info-strip"><ShieldCheck size={19} /><div><strong>不用模型 · 最终留出基线</strong><span>所有规则准入样本都交易：Precision {pct(ruleBaseline.precision)} · Recall {pct(ruleBaseline.recall)} · 理论收益 {money(ruleBaseline.fixed_profit_usd)}。该行只作同样本审计，不参与 Top 3 模型排名。</span></div></div>}
    </section>

    <section className="panel feature-panel"><div className="panel-heading"><div><h2>候选特征池</h2><p>{savingFeatures ? "正在自动保存…" : `已保存 ${activeFeatures.length} 个候选；后续自动训练每轮都会从这组候选中重新评估。`}</p></div><div className="heading-actions"><button className="button button-secondary" disabled={savingFeatures} onClick={() => void saveFeatureSelection(catalog.default_features)}><RotateCcw size={15} />恢复默认</button><button className="button button-secondary" disabled={savingFeatures} onClick={() => void saveFeatureSelection(catalog.available_features)}>全选可用</button></div></div><div className="feature-grid">{catalog.items.map((item) => <label className={`feature-option ${selected.has(item.name) ? "feature-selected" : ""}`} key={item.name}><input type="checkbox" disabled={savingFeatures} checked={selected.has(item.name)} onChange={() => toggleFeature(item.name)} /><span className="feature-name mono">{item.name}</span><span className="feature-meta">覆盖 {item.available_rows}/{item.total_mature_rows} · {pct(item.coverage)}{item.default_enabled ? " · 默认" : " · 可选"}{item.active_model_slots.length ? ` · Top ${item.active_model_slots.join("/")} 使用` : ""}</span></label>)}</div></section>

    <section className="panel table-panel"><div className="panel-heading"><div><h2>最近候选池</h2><p>完整候选包含传统树模型、Boosting、核方法及可选 AutoML；不可用依赖会显式标记 skipped。</p></div></div>{candidates.length ? <div className="table-scroll"><table><thead><tr><th>算法</th><th>状态</th><th>特征</th><th>E</th><th>G</th><th>S</th><th>最终 Precision</th><th>最终 Recall</th><th>说明</th></tr></thead><tbody>{candidates.map((candidate, index) => { const final = asRecord(candidate.final_metrics); const gen = asRecord(candidate.generalization); return <tr key={`${String(candidate.algorithm)}-${index}`}><td>{algorithmName(String(candidate.algorithm ?? "—"))}</td><td><StatusBadge tone={candidate.status === "ok" ? "blue" : candidate.status === "failed" ? "orange" : "neutral"} label={String(candidate.status ?? "—")} /></td><td>{Array.isArray(candidate.feature_names) ? candidate.feature_names.length : 0}</td><td>{score(candidate.economic_score)}</td><td>{score(gen.score)}</td><td>{score(candidate.composite_score)}</td><td>{pct(final.precision)}</td><td>{pct(final.recall)}</td><td className="muted-cell">{String(candidate.skip_reason ?? "—")}</td></tr>; })}</tbody></table></div> : <EmptyState title="暂无新候选评估" detail="完成一次 Top 3 训练后会显示完整候选池。" />}</section>

    <section className="panel table-panel"><div className="panel-heading"><div><h2>模型版本</h2><p>Top 3、历史 Rank 1、拒绝与回滚记录</p></div></div>{data.items.length ? <div className="table-scroll"><table><thead><tr><th>版本</th><th>算法</th><th>状态</th><th>特征数</th><th>综合分</th><th>训练时间</th><th>说明</th><th>操作</th></tr></thead><tbody>{data.items.map((model) => <tr key={model.id}><td className="mono">{modelLabel(model)}</td><td>{algorithmName(model.algorithm)}</td><td><StatusBadge tone={model.status === "champion" ? "gold" : model.status === "candidate" ? "blue" : "neutral"} label={model.status} /></td><td>{model.feature_names.length}</td><td>{score(model.metrics.composite_score)}</td><td>{new Date(model.trained_at).toLocaleString("zh-CN")}</td><td className="muted-cell">{model.rejection_reason ?? (model.early_stage ? "EARLY_STAGE" : "—")}</td><td>{model.status === "retired" ? <button className="button button-secondary" disabled={rollingBack === model.id} onClick={() => void rollback(model.id)}>{rollingBack === model.id ? "回滚中…" : "恢复 Rank 1"}</button> : "—"}</td></tr>)}</tbody></table></div> : <EmptyState title="没有模型版本" detail="训练后会形成可审计版本链。" />}</section>

    <section className="panel table-panel"><div className="panel-heading"><div><h2>训练与自更新记录</h2><p>手动训练、样本不足时每日训练、正常周训与启动补跑共用持久化任务；训练完成可先等待旧模型空仓再切换。</p></div></div>{data.training_runs.length ? <div className="table-scroll"><table><thead><tr><th>触发</th><th>状态</th><th>候选特征</th><th>重试</th><th>计划时间</th><th>Top 3 更新</th><th>结果</th></tr></thead><tbody>{data.training_runs.map((run) => { const top = asArray(asRecord(run.summary).top_models); const activation = asRecord(asRecord(run.summary).activation); const resultText = run.error_message ?? (top.length ? `${top.map((item) => algorithmName(String(item.algorithm ?? ""))).join(" / ")}${run.promoted ? " · 已切换" : activation.status === "waiting_for_flat" ? " · 已训练，等待旧模型空仓" : ""}` : run.status === "completed" ? "已完成" : "—"); return <tr key={run.id}><td>{run.trigger}</td><td><StatusBadge tone={run.status === "failed" ? "orange" : run.status === "completed" ? "blue" : "neutral"} label={run.status} /></td><td>{run.request.feature_names?.length ?? 0}</td><td>{run.retry_count}</td><td>{run.scheduled_for ? new Date(run.scheduled_for).toLocaleString("zh-CN") : "手动"}</td><td>{run.promoted ? "是" : "待切换"}</td><td className="muted-cell">{resultText}</td></tr>; })}</tbody></table></div> : <EmptyState title="暂无训练任务" detail="手动或定时训练后会显示在这里。" />}</section>

    <section className="info-strip"><Bot size={19} /><div><strong>自动换模节奏</strong><span>北京时间 16:00 起旧模型停止新买入，17:00 先训练候选 Top 3；若三个模型策略仍有持仓，候选模型持久等待，全部空仓后立即原子切换。模型健康为 insufficient_data 时每天执行；其余状态按每周约定日执行。系统若错过 17:00，会在下次启动补训。</span></div></section>

    <section className="info-strip"><Bot size={19} /><div><strong>评分与奥卡姆原则</strong><span>E 使用开发期时间外固定 $50 收益的归一化捕获率；G 综合 AP Skill、时间稳定性与衰减；S 固定为 0.60E + 0.40G。最终留出集只做审计，不参与 Top 3 排名。</span></div></section>
  </div>;
}
