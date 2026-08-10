import { lazy, Suspense, useCallback, useEffect, useState } from "react";
import type { ComponentType } from "react";
import { AppShell } from "./components/AppShell";
import { PageLoading } from "./components/PageState";

const DashboardPage = lazy(async () => ({ default: (await import("./pages/DashboardPage")).DashboardPage }));
const ModelsPage = lazy(async () => ({ default: (await import("./pages/ModelsPage")).ModelsPage }));
const SignalsPage = lazy(async () => ({ default: (await import("./pages/SignalsPage")).SignalsPage }));
const PortfolioPage = lazy(async () => ({ default: (await import("./pages/PortfolioPage")).PortfolioPage }));
const RuntimePage = lazy(async () => ({ default: (await import("./pages/RuntimePage")).RuntimePage }));
const AgentPage = lazy(async () => ({ default: (await import("./pages/AgentPage")).AgentPage }));

const pages: Record<string, ComponentType> = {
  "/": DashboardPage,
  "/models": ModelsPage,
  "/signals": SignalsPage,
  "/portfolio": PortfolioPage,
  "/runtime": RuntimePage,
  "/agent": AgentPage
};

function browserPath() {
  return window.location.pathname.replace(/\/+$/, "") || "/";
}

export default function App() {
  const [path, setPath] = useState(browserPath);
  const currentPath = pages[path] ? path : "/";
  const Page = pages[currentPath];

  useEffect(() => {
    const onPopState = () => setPath(browserPath());
    window.addEventListener("popstate", onPopState);
    return () => window.removeEventListener("popstate", onPopState);
  }, []);

  useEffect(() => {
    if (path === currentPath) return;
    window.history.replaceState(null, "", currentPath);
    setPath(currentPath);
  }, [currentPath, path]);

  const navigate = useCallback((nextPath: string) => {
    if (nextPath === browserPath()) return;
    window.history.pushState(null, "", nextPath);
    setPath(nextPath);
    window.scrollTo({ top: 0, behavior: "smooth" });
  }, []);

  return (
    <AppShell currentPath={currentPath} onNavigate={navigate}>
      <Suspense fallback={<PageLoading />}><Page /></Suspense>
    </AppShell>
  );
}
