import { KeyRound, Plus, RefreshCw, Save, Settings2, Trash2 } from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";
import { api } from "../api/client";
import type { PlatformConfiguration } from "../api/types";
import { PageError, PageLoading } from "../components/PageState";
import { StatusBadge } from "../components/StatusBadge";

export function ConfigurationPage() {
  const [data, setData] = useState<PlatformConfiguration | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [pollSeconds, setPollSeconds] = useState(3);
  const [gmgnRps, setGmgnRps] = useState(10);
  const [regimePollSeconds, setRegimePollSeconds] = useState(60);
  const [adaptiveInterval, setAdaptiveInterval] = useState(15);
  const [adaptiveMinConfidence, setAdaptiveMinConfidence] = useState(0.55);
  const [adaptiveExplorationRate, setAdaptiveExplorationRate] = useState(0);
  const [gmgnBaseUrl, setGmgnBaseUrl] = useState("");
  const [jupiterQuoteUrl, setJupiterQuoteUrl] = useState("");
  const [newCredentials, setNewCredentials] = useState<Record<string, string>>({});

  const load = useCallback(async () => {
    setError(null);
    try {
      const result = await api.configuration();
      setData(result);
      setPollSeconds(result.runtime.position_monitor_poll_seconds);
      setGmgnRps(result.runtime.gmgn_global_rps);
      setRegimePollSeconds(result.runtime.regime_poll_seconds);
      setAdaptiveInterval(result.runtime.adaptive_action_interval_minutes);
      setAdaptiveMinConfidence(result.runtime.adaptive_min_confidence);
      setAdaptiveExplorationRate(result.runtime.adaptive_exploration_rate);
      setGmgnBaseUrl(result.providers.find((item) => item.key === "gmgn")?.base_url ?? "");
      setJupiterQuoteUrl(result.providers.find((item) => item.key === "jupiter")?.base_url ?? "");
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "配置读取失败");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { void load(); }, [load]);

  const saveRuntime = async () => {
    setBusy(true);
    setNotice(null);
    try {
      const result = await api.saveRuntimeConfiguration({
        position_monitor_poll_seconds: pollSeconds,
        gmgn_global_rps: gmgnRps,
        regime_poll_seconds: regimePollSeconds,
        adaptive_action_interval_minutes: adaptiveInterval,
        adaptive_min_confidence: adaptiveMinConfidence,
        adaptive_exploration_rate: adaptiveExplorationRate,
        gmgn_base_url: gmgnBaseUrl,
        jupiter_quote_url: jupiterQuoteUrl
      });
      setData(result);
      setNotice("运行参数已保存。持仓监控会在后续周期自动采用新频率和 API 预算。 ");
    } catch (cause) {
      setNotice(cause instanceof Error ? cause.message : "保存失败");
    } finally {
      setBusy(false);
    }
  };

  const addCredential = async (provider: string) => {
    const credential = (newCredentials[provider] ?? "").trim();
    if (!credential) return;
    setBusy(true);
    setNotice(null);
    try {
      const result = await api.addProviderCredential(provider, credential);
      setData(result);
      setNewCredentials((current) => ({ ...current, [provider]: "" }));
      setNotice("API Key 已加入资源池；页面不会再次显示明文。 ");
    } catch (cause) {
      setNotice(cause instanceof Error ? cause.message : "API Key 添加失败");
    } finally {
      setBusy(false);
    }
  };

  const deleteCredential = async (provider: string, slot: number) => {
    setBusy(true);
    setNotice(null);
    try {
      const result = await api.deleteProviderCredential(provider, slot);
      setData(result);
      setNotice("API Key 已移除并重新编号；资源池会自动重新分配。 ");
    } catch (cause) {
      setNotice(cause instanceof Error ? cause.message : "API Key 删除失败");
    } finally {
      setBusy(false);
    }
  };

  const derivedCards = useMemo(() => data ? [
    ["持仓目标周期", `${data.derived.position_monitor_target_seconds.toFixed(1)}s`, `默认已收紧到 3s`],
    ["GMGN 总预算", `${data.derived.gmgn_total_rps.toFixed(1)} req/s`, `${data.derived.gmgn_key_count} 个 Key 共享 IP 预算`],
    ["Jupiter 退出并发", `${data.derived.jupiter_exit_concurrency}`, `${data.derived.jupiter_key_count} 个 Key 自动限并发`],
    ["Solana RPC 主池", `${data.derived.alchemy_account_count} Alchemy`, `${data.derived.ankr_freemium_count} Ankr HTTPS 灾备`],
    ["Regime 采样周期", `${data.runtime.regime_poll_seconds}s`, `${data.runtime.adaptive_action_interval_minutes} 分钟更新买入松紧动作`],
    ["3s 可覆盖唯一 Token", `${data.derived.gmgn_unique_tokens_per_target_cycle}`, `当前持仓 ${data.derived.open_unique_tokens} 个唯一 Token`],
    ["预计最短实际周期", `${data.derived.estimated_min_cycle_seconds.toFixed(1)}s`, data.derived.capacity_state === "within_target" ? "当前 API 预算可满足目标" : "当前 API 预算受限，将自动降级"]
  ] : [], [data]);

  if (loading && !data) return <PageLoading />;
  if (error || !data) return <PageError message={error ?? "无配置数据"} retry={load} />;

  return (
    <div className="page-stack">
      <section className="page-heading">
        <div>
          <p className="eyebrow">PLATFORM CONFIGURATION</p>
          <h1>配置</h1>
          <p>集中管理平台外部 API 与持仓监控频率。密钥只写入本机 .env，页面永远只返回掩码。</p>
        </div>
        <button className="button button-secondary" disabled={busy} onClick={() => void load()}><RefreshCw size={17} />刷新</button>
      </section>

      {notice && <div className="inline-notice">{notice}</div>}

      <section className="configuration-summary-grid">
        {derivedCards.map(([label, value, note]) => (
          <article className="configuration-summary-card" key={label}>
            <span>{label}</span><strong>{value}</strong><small>{note}</small>
          </article>
        ))}
      </section>

      <section className="panel">
        <div className="panel-heading"><div><h2>运行参数</h2><p>3 秒是目标 start-to-start 周期；系统同时记录实际周期，API 预算不足时不会偷偷超限。</p></div><Settings2 size={20} /></div>
        <div className="configuration-form-grid">
          <label><span>持仓轮询频率（秒）</span><input type="number" min="1" max="60" step="0.5" value={pollSeconds} onChange={(event) => setPollSeconds(Number(event.target.value))} /></label>
          <label><span>GMGN 全局预算（req/s）</span><input type="number" min="0.1" max="50" step="0.1" value={gmgnRps} onChange={(event) => setGmgnRps(Number(event.target.value))} /></label>
          <label><span>市场状态采样（秒）</span><input type="number" min="15" max="3600" step="15" value={regimePollSeconds} onChange={(event) => setRegimePollSeconds(Number(event.target.value))} /></label>
          <label><span>自适应动作周期（分钟）</span><input type="number" min="5" max="60" step="5" value={adaptiveInterval} onChange={(event) => setAdaptiveInterval(Number(event.target.value))} /></label>
          <label><span>自适应最低置信度</span><input type="number" min="0" max="1" step="0.05" value={adaptiveMinConfidence} onChange={(event) => setAdaptiveMinConfidence(Number(event.target.value))} /></label>
          <label><span>安全探索率（0–5%）</span><input type="number" min="0" max="0.05" step="0.01" value={adaptiveExplorationRate} onChange={(event) => setAdaptiveExplorationRate(Number(event.target.value))} /></label>
          <label><span>GMGN Base URL</span><input value={gmgnBaseUrl} onChange={(event) => setGmgnBaseUrl(event.target.value)} placeholder="按当前部署填写" /></label>
          <label><span>Jupiter Quote URL</span><input value={jupiterQuoteUrl} onChange={(event) => setJupiterQuoteUrl(event.target.value)} placeholder="https://api.jup.ag/swap/v2/order" /></label>
        </div>
        <div className="configuration-help">GMGN 默认总预算为 10 req/s：取当前官方 CLI reference 的生产口径作为保守默认；部分最新 Skill 文档标注 rate=20/capacity=20。只有确认你的实际账号/接口合同支持时才提高；增加 Key 默认只增强轮换与 fallback，不自动把总吞吐乘以 Key 数。</div>
        <button className="button button-primary" disabled={busy} onClick={() => void saveRuntime()}><Save size={17} />保存运行参数</button>
      </section>

      <section className="configuration-provider-grid">
        {data.providers.map((provider) => (
          <article className="panel configuration-provider-card" key={provider.key}>
            <div className="panel-heading">
              <div><h2>{provider.label}</h2><p>{provider.description}</p></div>
              <StatusBadge tone={provider.credential_count ? "blue" : "orange"} label={`${provider.credential_count} KEY`} />
            </div>
            <div className="configuration-key-list">
              {provider.credentials.length ? provider.credentials.map((credential) => (
                <div className="configuration-key-row" key={`${provider.key}-${credential.slot}`}>
                  <span><KeyRound size={15} />Key {credential.slot}</span>
                  <code>{credential.masked}</code>
                  <button className="icon-button" disabled={busy} aria-label={`删除 ${provider.label} Key ${credential.slot}`} onClick={() => void deleteCredential(provider.key, credential.slot)}><Trash2 size={15} /></button>
                </div>
              )) : <div className="configuration-empty">尚未配置 Key；依赖该 Provider 的功能会显式降级或使用已有 fallback。</div>}
            </div>
            <div className="configuration-add-row">
              <input type="password" autoComplete="off" value={newCredentials[provider.key] ?? ""} onChange={(event) => setNewCredentials((current) => ({ ...current, [provider.key]: event.target.value }))} placeholder={provider.multiple ? "新增 API Key" : "设置 Token"} />
              <button className="button button-secondary" disabled={busy || !(newCredentials[provider.key] ?? "").trim()} onClick={() => void addCredential(provider.key)}><Plus size={16} />添加</button>
            </div>
          </article>
        ))}
      </section>
    </div>
  );
}
