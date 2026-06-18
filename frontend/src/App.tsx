import { useState } from 'react';
import './App.css';
import ControlCenter from './pages/ControlCenter';
import Portfolio from './pages/Portfolio';
import Operations from './pages/Operations';

const tabs = [
  { key: 'control', label: 'Control Center' },
  { key: 'portfolio', label: '交易看板' },
  { key: 'ops', label: 'Ops & Emergency' },
];

function App() {
  const [activeTab, setActiveTab] = useState('control');

  return (
    <div className="app-shell">
      <header className="app-header">
        <div>
          <h1>Solana Meme Trading Bot</h1>
          <p>模拟盘用于策略对照，实盘仅执行唯一实盘策略，二者在同一运行态下统一记录。</p>
        </div>
        <nav className="tab-nav">
          {tabs.map((tab) => (
            <button
              key={tab.key}
              className={activeTab === tab.key ? 'active' : ''}
              onClick={() => setActiveTab(tab.key)}
            >
              {tab.label}
            </button>
          ))}
        </nav>
      </header>

      <main>
        <div hidden={activeTab !== 'control'}><ControlCenter /></div>
        <div hidden={activeTab !== 'portfolio'}><Portfolio active={activeTab === 'portfolio'} /></div>
        <div hidden={activeTab !== 'ops'}><Operations active={activeTab === 'ops'} /></div>
      </main>
    </div>
  );
}

export default App;
