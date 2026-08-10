import { CheckCircle2, RefreshCw, ShieldCheck, XCircle } from "lucide-react";
import { useCallback, useState } from "react";
import { api } from "../api/client";
import { useApiData } from "../api/useApiData";
import { EmptyState } from "../components/EmptyState";
import { PageError, PageLoading } from "../components/PageState";
import { StatusBadge } from "../components/StatusBadge";

const pretty = (value: unknown) => {
  if (value == null) return "—";
  if (typeof value === "string") return value;
  return JSON.stringify(value);
};

export function AgentPage() {
  const loader = useCallback(async () => {
    const [context, proposals] = await Promise.all([api.agentContext(), api.agentProposals()]);
    return { context, proposals };
  }, []);
  const { data, loading, error, refresh } = useApiData(loader);
  const [busy, setBusy] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  if (loading && !data) return <PageLoading />;
  if (error || !data) return <PageError message={error ?? "无数据"} retry={refresh} />;

  const decide = async (proposalId: string, action: "approve" | "reject") => {
    const verb = action === "approve" ? "批准并执行这个非实盘提案" : "驳回这个提案";
    if (!window.confirm(`确认${verb}？`)) return;
    setBusy(proposalId);
    setNotice(null);
    try {
      const result = action === "approve"
        ? await api.approveAgentProposal(proposalId)
        : await api.rejectAgentProposal(proposalId);
      setNotice(`提案 ${proposalId.slice(0, 8)}… 已更新为 ${result.status}`);
      await refresh();
    } catch (cause) {
      setNotice(cause instanceof Error ? cause.message : "提案处理失败");
    } finally {
      setBusy(null);
    }
  };

  return (
    <div className="page-stack">
      <section className="page-heading">
        <div>
          <p className="eyebrow">AGENT GOVERNANCE</p>
          <h1>Agent 提案审批</h1>
          <p>Agent 只能读取上下文并提交白名单提案；任何动作必须由人工批准，实盘买卖、清仓、钱包转账和秘密访问永久不在白名单内。</p>
        </div>
        <div className="heading-actions">
          <button className="button button-secondary" onClick={() => void refresh()}><RefreshCw size={17} />刷新</button>
        </div>
      </section>
      {notice && <div className="inline-notice">{notice}</div>}

      <section className="runtime-grid">
        <article className="panel">
          <div className="panel-heading"><div><h2>可审批动作</h2><p>批准后仅执行本地、非实盘、可审计动作</p></div><ShieldCheck size={20} /></div>
          <dl className="health-list">
            {data.context.allowed_proposal_types.map((item) => <div key={item}><dt className="mono">{item}</dt><dd><StatusBadge tone="blue" label="ALLOW" /></dd></div>)}
          </dl>
        </article>
        <article className="panel">
          <div className="panel-heading"><div><h2>永久禁止</h2><p>Agent 即使提交也会在创建阶段被后端拒绝</p></div></div>
          <dl className="health-list">
            {data.context.explicitly_forbidden.map((item) => <div key={item}><dt className="mono">{item}</dt><dd><StatusBadge tone="orange" label="BLOCKED" /></dd></div>)}
          </dl>
        </article>
      </section>

      <section className="panel table-panel">
        <div className="panel-heading"><div><h2>提案队列</h2><p>所有决策和执行结果持久化到 SQLite，并写入审计日志</p></div><StatusBadge label={`${data.proposals.items.length} 条`} /></div>
        {data.proposals.items.length ? (
          <div className="table-scroll"><table><thead><tr><th>时间</th><th>类型</th><th>参数</th><th>状态</th><th>执行结果</th><th>操作</th></tr></thead><tbody>
            {data.proposals.items.map((item) => (
              <tr key={item.id}>
                <td>{new Date(item.created_at).toLocaleString("zh-CN")}</td>
                <td className="mono">{item.proposal_type}</td>
                <td className="mono muted-cell">{pretty(item.payload)}</td>
                <td><StatusBadge tone={item.status === "failed" || item.status === "rejected" ? "orange" : item.status === "executed" ? "blue" : "neutral"} label={item.status} /></td>
                <td className="mono muted-cell">{item.error_message ?? pretty(item.result)}</td>
                <td>{item.status === "pending_approval" ? <div className="heading-actions"><button className="button button-primary" disabled={busy === item.id} onClick={() => void decide(item.id, "approve")}><CheckCircle2 size={15} />批准</button><button className="button button-danger-outline" disabled={busy === item.id} onClick={() => void decide(item.id, "reject")}><XCircle size={15} />驳回</button></div> : "—"}</td>
              </tr>
            ))}
          </tbody></table></div>
        ) : <EmptyState title="当前没有 Agent 提案" detail="Agent 通过 API 提交的安全提案会出现在这里等待人工审批。" />}
      </section>
    </div>
  );
}
