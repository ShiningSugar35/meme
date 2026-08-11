import { Activity, Bot, ChartNoAxesCombined, LayoutDashboard, Menu, Radio, ShieldCheck, WalletCards, X } from "lucide-react";
import { useState } from "react";
import type { MouseEvent, ReactNode } from "react";

const navigation = [
  { to: "/", label: "总览", icon: LayoutDashboard },
  { to: "/portfolio", label: "持仓", icon: WalletCards },
  { to: "/signals", label: "样本采集", icon: Radio },
  { to: "/runtime", label: "运行监控", icon: Activity },
  { to: "/models", label: "模型中心", icon: Bot },
  { to: "/agent", label: "Agent审批", icon: ShieldCheck }
];

interface AppShellProps {
  children: ReactNode;
  currentPath: string;
  onNavigate: (path: string) => void;
}

export function AppShell({ children, currentPath, onNavigate }: AppShellProps) {
  const [mobileOpen, setMobileOpen] = useState(false);
  const navigate = (event: MouseEvent<HTMLAnchorElement>, to: string) => {
    if (event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    event.preventDefault();
    onNavigate(to);
    setMobileOpen(false);
  };
  return (
    <div className="app-shell">
      <aside className={`sidebar ${mobileOpen ? "sidebar-open" : ""}`}>
        <div className="brand"><span className="brand-mark"><ChartNoAxesCombined size={20} /></span><div><strong>Meme Quant</strong><small>Solana Research Desk</small></div></div>
        <button className="icon-button mobile-close" onClick={() => setMobileOpen(false)} aria-label="关闭导航"><X size={20} /></button>
        <nav>
          {navigation.map(({ to, label, icon: Icon }) => (
            <a
              key={to}
              href={to}
              className={currentPath === to ? "active" : undefined}
              aria-current={currentPath === to ? "page" : undefined}
              onClick={(event) => navigate(event, to)}
            >
              <Icon size={18} strokeWidth={1.8} /><span>{label}</span>
            </a>
          ))}
        </nav>
        <div className="sidebar-note"><span className="pulse-dot" />所有实盘动作均需二次点击确认</div>
      </aside>
      {mobileOpen && <button className="mobile-overlay" aria-label="关闭导航" onClick={() => setMobileOpen(false)} />}
      <main className="main-panel">
        <header className="topbar"><button className="icon-button menu-button" onClick={() => setMobileOpen(true)} aria-label="打开导航"><Menu size={21} /></button><div><span className="topbar-kicker">OPERATIONS CONSOLE</span><strong>量化交易控制台</strong></div>{currentPath === "/portfolio" ? <div id="portfolio-mode-switch" className="topbar-page-action" /> : <span className="topbar-clock">Asia / Shanghai</span>}</header>
        <div className="page-container">{children}</div>
      </main>
    </div>
  );
}
