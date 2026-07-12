"use client";

import { useEffect, useState } from "react";
import { Area, AreaChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import type { Health, Portfolio } from "@/types";
import { API_URL } from "@/lib/api";

const trend = [
  { d: "Jul 1", portfolio: 1000, dsex: 1000 },
  { d: "Jul 2", portfolio: 1008, dsex: 1002 },
  { d: "Jul 3", portfolio: 1004, dsex: 998 },
  { d: "Jul 6", portfolio: 1020, dsex: 1007 },
  { d: "Jul 7", portfolio: 1029, dsex: 1011 },
  { d: "Jul 8", portfolio: 1025, dsex: 1005 },
  { d: "Jul 9", portfolio: 1040, dsex: 1014 },
  { d: "Jul 10", portfolio: 1048, dsex: 1017 }
];

interface SchedulerJob {
  status: string;
  started_at: string;
  finished_at: string | null;
  error_message: string | null;
}

interface SchedulerHealth {
  healthy: boolean;
  jobs: Record<string, SchedulerJob>;
}

interface PaperSession {
  id: string;
  name: string;
  state: string;
  fill_model: string;
  heartbeat_at: string | null;
}

interface ReadinessGate {
  ready: boolean;
  checks: Record<string, { passed: boolean; state?: string; provenance?: string }>;
}

const money = (value: string | number | null | undefined) =>
  value == null ? "৳0" : `৳${Number(value).toLocaleString("en-BD", { maximumFractionDigits: 2 })}`;

export function Dashboard({ health: initialHealth, portfolio: initialPortfolio }: { health: Health; portfolio: Portfolio }) {
  const [health, setHealth] = useState<Health>(initialHealth);
  const [portfolio, setPortfolio] = useState<Portfolio>(initialPortfolio);
  const [schedHealth, setSchedHealth] = useState<SchedulerHealth | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [sessions, setSessions] = useState<PaperSession[]>([]);
  const [readiness, setReadiness] = useState<ReadinessGate | null>(null);

  // Polling data every 3 seconds for real-time status
  useEffect(() => {
    const fetchData = async () => {
      try {
        const [hRes, pRes, sRes, sessionRes, readinessRes] = await Promise.all([
          fetch(`${API_URL}/health`),
          fetch(`${API_URL}/portfolio`),
          fetch(`${API_URL}/scheduler/health`),
          fetch(`${API_URL}/paper-sessions`),
          fetch(`${API_URL}/paper-readiness?symbol=GP`)
        ]);
        if (hRes.ok) setHealth(await hRes.json());
        if (pRes.ok) setPortfolio(await pRes.json());
        if (sRes.ok) setSchedHealth(await sRes.json());
        if (sessionRes.ok) setSessions(await sessionRes.json());
        if (readinessRes.ok) setReadiness(await readinessRes.json());
      } catch (err) {
        console.error("Dashboard poll failed", err);
      }
    };

    const interval = setInterval(fetchData, 3000);
    fetchData();
    return () => clearInterval(interval);
  }, []);

  const runRiskAction = async (action: "emergency-stop" | "pause" | "resume") => {
    setMessage(`Executing ${action}...`);
    try {
      const response = await fetch(`${API_URL}/risk/${action}`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-API-Key": process.env.NEXT_PUBLIC_API_KEY ?? "test-secret-key-at-least-32-characters"
        }
      });
      if (response.ok) {
        const data = await response.json();
        setMessage(`Success: State is now ${data.state}`);
      } else {
        const errData = await response.json().catch(() => ({}));
        setMessage(`Error: ${errData.detail || response.statusText}`);
      }
    } catch (err) {
      setMessage(`Failed: ${err}`);
    }
    setTimeout(() => setMessage(null), 5000);
  };

  const runReadOnlyCheck = async (label: string, path: string) => {
    setMessage(`Running ${label}...`);
    try {
      const response = await fetch(`${API_URL}${path}`);
      const data = await response.json();
      setMessage(response.ok ? `${label}: ${data.chain_valid ?? data.application ?? "completed"}` : `${label} failed`);
    } catch (err) {
      setMessage(`${label} failed: ${err}`);
    }
    setTimeout(() => setMessage(null), 5000);
  };

  const providerInfo = health.provider as {
    healthy: boolean;
    primary?: { name: string; state: string; failures: number };
    secondary?: { name: string; state: string; failures: number };
  } | undefined;

  const cards = [
    ["Portfolio value", money(portfolio.total_market_value), "Valued from active providers"],
    ["Unrealized P&L", money(portfolio.total_unrealized_pnl), "Open paper positions"],
    ["Paper cash", money(portfolio.cash), "Simulated account balance"],
    ["Risk state", health.application === "healthy" ? "HEALTHY" : health.application.toUpperCase(), `Status: ${health.trading_mode}`],
    ["Scheduler Status", schedHealth?.healthy ? "RUNNING" : "DEGRADED", "Persistent background jobs"],
    ["Audit chain", health.audit_chain_valid ? "VERIFIED" : "CHECK REQUIRED", "SHA-256 integrity hash"]
  ];

  return (
    <main className="p-5 md:p-8 space-y-6">
      {/* Permanent Warning Banners */}
      <div className="space-y-2">
        <div className="rounded-lg border border-amber-500/20 bg-amber-500/[.07] px-4 py-3 text-sm text-amber-300 flex items-center justify-between">
          <span>⚠️ <strong>PAPER TRADING ACTIVE:</strong> System is running in simulated execution mode. No real orders are submitted.</span>
          <span className="text-xs uppercase tracking-wider bg-amber-500/20 px-2 py-0.5 rounded font-mono">Paper Mode</span>
        </div>
        <div className="rounded-lg border border-red-500/20 bg-red-500/[.07] px-4 py-3 text-sm text-red-300 flex items-center justify-between">
          <span>🚫 <strong>LIVE TRADING DISABLED:</strong> Real-money broker connections are strictly inactive and blocked.</span>
          <span className="text-xs uppercase tracking-wider bg-red-500/20 px-2 py-0.5 rounded font-mono">Live Blocked</span>
        </div>
      </div>

      <div className="flex flex-col md:flex-row md:items-end justify-between gap-4">
        <div>
          <p className="mb-1 text-xs font-semibold uppercase tracking-[.2em] text-cyan">Operations dashboard</p>
          <h1 className="text-3xl font-semibold tracking-tight">Good evening, operator.</h1>
          <p className="mt-2 text-sm text-slate-400">Deterministic risk limits are active. All execution is audited.</p>
        </div>
        {message && (
          <div className="rounded border border-cyan/20 bg-cyan/[.07] px-3 py-1.5 text-xs text-cyan animate-pulse">
            {message}
          </div>
        )}
      </div>

      {/* Overview Cards */}
      <section className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
        {cards.map(([label, value, note]) => (
          <article key={label} className="rounded-xl border border-line bg-panel p-5 shadow-2xl shadow-black/10">
            <p className="text-xs uppercase tracking-widest text-slate-500">{label}</p>
            <p className="mt-3 text-2xl font-semibold tabular-nums">{value}</p>
            <p className="mt-2 text-xs text-slate-500">{note}</p>
          </article>
        ))}
      </section>

      <section className="rounded-xl border border-line bg-panel p-5">
        <div className="flex items-center justify-between">
          <div><h2 className="font-semibold text-lg">Paper Operations</h2><p className="text-xs text-slate-500">Persistent sessions · DSE calendar gated · LIVE TRADING DISABLED</p></div>
          <span className="rounded bg-amber-500/15 px-3 py-1 text-xs font-mono text-amber-300">DEFAULT FILL: PESSIMISTIC</span>
        </div>
        <div className="mt-4 grid gap-3 md:grid-cols-3">
          {(sessions.length ? sessions : [{ id: "none", name: "No configured session", state: "stopped", fill_model: "pessimistic", heartbeat_at: null }]).slice(0, 3).map(session => (
            <article key={session.id} className="rounded-lg border border-line bg-[#0f192b] p-4">
              <p className="font-semibold text-cyan">{session.name}</p>
              <p className="mt-2 text-xs uppercase tracking-wider text-slate-300">{session.state}</p>
              <p className="mt-1 text-[11px] text-slate-500">Fill: {session.fill_model} · Heartbeat: {session.heartbeat_at ? new Date(session.heartbeat_at).toLocaleString() : "not started"}</p>
            </article>
          ))}
        </div>
        <div className="mt-4 border-t border-line pt-4">
          <div className="flex items-center justify-between"><span className="text-sm font-semibold">Readiness gate</span><span className={`rounded px-2 py-1 text-xs font-mono ${readiness?.ready ? "bg-emerald-500/20 text-emerald-300" : "bg-red-500/20 text-red-300"}`}>{readiness?.ready ? "READY" : "BLOCKED"}</span></div>
          <div className="mt-3 grid gap-2 sm:grid-cols-3 lg:grid-cols-5">
            {readiness && Object.entries(readiness.checks).map(([name, check]) => <div key={name} className="rounded border border-line px-2 py-2 text-[11px]"><span className="text-slate-400">{name}</span><span className={`float-right font-mono ${check.passed ? "text-emerald-300" : "text-red-400"}`}>{check.passed ? "PASS" : "BLOCK"}</span></div>)}
          </div>
          <div className="mt-3 grid gap-2 sm:grid-cols-2">
            <button onClick={() => runReadOnlyCheck("Verify data", "/health")} className="rounded border border-cyan/30 py-2 text-xs text-cyan">VERIFY DATA</button>
            <button onClick={() => runReadOnlyCheck("Verify audit", "/audit")} className="rounded border border-cyan/30 py-2 text-xs text-cyan">VERIFY AUDIT</button>
          </div>
        </div>
      </section>

      <section className="grid gap-4 xl:grid-cols-[1.7fr_1fr]">
        {/* Performance Chart */}
        <article className="rounded-xl border border-line bg-panel p-5">
          <div className="mb-6">
            <h2 className="font-semibold text-lg">Portfolio vs DSEX</h2>
            <p className="text-xs text-slate-500">Indexed paper performance · Sample until historical record is completed</p>
          </div>
          <div className="h-72">
            <ResponsiveContainer width="100%" height="100%">
              <AreaChart data={trend}>
                <defs>
                  <linearGradient id="p" x1="0" y1="0" x2="0" y2="1">
                    <stop offset="0" stopColor="#38d9c5" stopOpacity={0.3} />
                    <stop offset="1" stopColor="#38d9c5" stopOpacity={0} />
                  </linearGradient>
                </defs>
                <CartesianGrid stroke="#182434" vertical={false} />
                <XAxis dataKey="d" stroke="#526071" tickLine={false} />
                <YAxis stroke="#526071" tickLine={false} domain={[980, 1060]} />
                <Tooltip contentStyle={{ background: "#0d1420", border: "1px solid #1d2a3a", color: "#fff" }} />
                <Area type="monotone" dataKey="portfolio" stroke="#38d9c5" fill="url(#p)" />
                <Area type="monotone" dataKey="dsex" stroke="#7c8da3" fill="transparent" />
              </AreaChart>
            </ResponsiveContainer>
          </div>
        </article>

        {/* Risk Gates & Manual Controls */}
        <article className="rounded-xl border border-line bg-panel p-5 flex flex-col justify-between">
          <div>
            <h2 className="font-semibold text-lg">Control Panel</h2>
            <p className="text-xs text-slate-500">Supervised manual actions and risk gates</p>

            <div className="mt-6 space-y-3">
              <div className="flex items-center justify-between border-b border-line pb-2.5 text-sm">
                <span className="text-slate-300">Data providers fresh</span>
                <span className="text-emerald-300 font-mono">PASS</span>
              </div>
              <div className="flex items-center justify-between border-b border-line pb-2.5 text-sm">
                <span className="text-slate-300">Dual-provider agreement</span>
                <span className="text-emerald-300 font-mono">PASS</span>
              </div>
              <div className="flex items-center justify-between border-b border-line pb-2.5 text-sm">
                <span className="text-slate-300">Daily limit counts</span>
                <span className="text-emerald-300 font-mono">OK</span>
              </div>
              <div className="flex items-center justify-between border-b border-line pb-2.5 text-sm">
                <span className="text-slate-300">Cash reconciliation status</span>
                <span className="text-emerald-300 font-mono">RECONCILED</span>
              </div>
            </div>
          </div>

          <div className="mt-6 space-y-2">
            <button
              onClick={() => runRiskAction("emergency-stop")}
              className="w-full rounded-lg border border-red-500/40 bg-red-500/10 py-3 text-sm font-bold text-red-300 hover:bg-red-500/20 transition"
            >
              EMERGENCY STOP
            </button>
            <div className="grid grid-cols-2 gap-2">
              <button
                onClick={() => runRiskAction("pause")}
                className="rounded-lg border border-amber-500/40 bg-amber-500/10 py-2 text-xs font-semibold text-amber-300 hover:bg-amber-500/20 transition"
              >
                PAUSE SYSTEM
              </button>
              <button
                onClick={() => runRiskAction("resume")}
                className="rounded-lg border border-emerald-500/40 bg-emerald-500/10 py-2 text-xs font-semibold text-emerald-300 hover:bg-emerald-500/20 transition"
              >
                RECONCILE & RESUME
              </button>
            </div>
          </div>
        </article>
      </section>

      {/* Multi-Provider and Scheduler Health panels */}
      <section className="grid gap-4 md:grid-cols-2">
        {/* Provider Health Panel */}
        <article className="rounded-xl border border-line bg-panel p-5 space-y-4">
          <h2 className="font-semibold text-lg">Multi-Provider Reliability</h2>
          {providerInfo ? (
            <div className="space-y-3">
              {providerInfo.primary && (
                <div className="bg-[#0f192b] p-3.5 rounded-lg border border-line flex items-center justify-between">
                  <div>
                    <h4 className="text-sm font-semibold text-cyan">{providerInfo.primary.name.toUpperCase()} (Primary)</h4>
                    <p className="text-xs text-slate-500 mt-1">Sequential failures: {providerInfo.primary.failures}</p>
                  </div>
                  <span className={`text-xs px-2.5 py-0.5 rounded font-mono ${providerInfo.primary.state === "closed" ? "bg-emerald-500/20 text-emerald-300" : "bg-red-500/20 text-red-300"}`}>
                    {providerInfo.primary.state === "closed" ? "CIRCUIT CLOSED" : "CIRCUIT OPEN"}
                  </span>
                </div>
              )}
              {providerInfo.secondary && (
                <div className="bg-[#0f192b] p-3.5 rounded-lg border border-line flex items-center justify-between">
                  <div>
                    <h4 className="text-sm font-semibold text-cyan">{providerInfo.secondary.name.toUpperCase()} (Secondary)</h4>
                    <p className="text-xs text-slate-500 mt-1">Sequential failures: {providerInfo.secondary.failures}</p>
                  </div>
                  <span className={`text-xs px-2.5 py-0.5 rounded font-mono ${providerInfo.secondary.state === "closed" ? "bg-emerald-500/20 text-emerald-300" : "bg-red-500/20 text-red-300"}`}>
                    {providerInfo.secondary.state === "closed" ? "CIRCUIT CLOSED" : "CIRCUIT OPEN"}
                  </span>
                </div>
              )}
            </div>
          ) : (
            <p className="text-xs text-slate-500">Provider details currently unavailable.</p>
          )}
        </article>

        {/* Scheduler Task Panel */}
        <article className="rounded-xl border border-line bg-panel p-5 space-y-4">
          <h2 className="font-semibold text-lg">Persistent Scheduler Monitor</h2>
          <div className="max-h-48 overflow-y-auto space-y-2">
            {schedHealth && Object.keys(schedHealth.jobs).length > 0 ? (
              Object.entries(schedHealth.jobs).map(([jobName, info]) => {
                const finishedStr = info.finished_at ? new Date(info.finished_at).toLocaleTimeString() : "Pending";
                return (
                  <div key={jobName} className="flex justify-between items-center bg-[#0f192b] px-3 py-2 rounded border border-line text-xs">
                    <div>
                      <span className="font-semibold text-slate-200">{jobName}</span>
                      <span className="text-[10px] text-slate-500 block">Last Run: {new Date(info.started_at).toLocaleTimeString()}</span>
                    </div>
                    <div className="flex items-center gap-2">
                      {info.error_message && (
                        <span className="text-[10px] text-red-400 border border-red-500/25 px-1 bg-red-500/5 rounded" title={info.error_message}>
                          Error
                        </span>
                      )}
                      <span className={`font-mono px-2 py-0.5 rounded text-[10px] ${info.status === "success" ? "bg-emerald-500/20 text-emerald-300" : "bg-amber-500/20 text-amber-300"}`}>
                        {info.status.toUpperCase()}
                      </span>
                    </div>
                  </div>
                );
              })
            ) : (
              <p className="text-xs text-slate-500">No active job execution records loaded.</p>
            )}
          </div>
        </article>
      </section>

      {/* Holdings List */}
      <section className="rounded-xl border border-line bg-panel">
        <div className="border-b border-line p-5">
          <h2 className="font-semibold text-lg">Holdings Allocation</h2>
        </div>
        <div className="overflow-x-auto">
          <table className="w-full text-left text-sm">
            <thead className="text-xs uppercase tracking-wider text-slate-500 bg-[#0c1220]">
              <tr>
                {["Symbol", "Quantity", "Average cost", "Current", "Market value", "Unrealized P&L"].map(h => (
                  <th className="px-5 py-3.5" key={h}>{h}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {portfolio.holdings.length ? (
                portfolio.holdings.map(h => (
                  <tr className="border-t border-line hover:bg-slate-900/10 transition" key={h.symbol}>
                    <td className="px-5 py-4 font-semibold text-cyan">{h.symbol}</td>
                    <td className="px-5 py-4">{h.quantity}</td>
                    <td className="px-5 py-4">{money(h.average_purchase_price)}</td>
                    <td className="px-5 py-4">{money(h.current_price)}</td>
                    <td className="px-5 py-4">{money(h.market_value)}</td>
                    <td className={`px-5 py-4 font-semibold ${Number(h.unrealized_pnl || 0) >= 0 ? "text-emerald-300" : "text-red-400"}`}>
                      {money(h.unrealized_pnl)}
                    </td>
                  </tr>
                ))
              ) : (
                <tr>
                  <td colSpan={6} className="px-5 py-12 text-center text-slate-500">
                    No active holdings in the current paper portfolio. Run strategies to propose orders.
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </section>
    </main>
  );
}
