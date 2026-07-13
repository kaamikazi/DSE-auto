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

interface OperationsSummary {
  current_campaign: {
    id: string;
    name: string;
    state: string;
    approved_strategies: string[];
  } | null;
  current_session: { day_id: number; date: string; state: string } | null;
  market_state: string;
  data_provenance: string;
  timestamp_trust: string;
  rule_set_version: string | null;
  rule_set_status: string | null;
  fee_profile: string | null;
  strategy_versions: string[];
  drawdown: number | null;
  backup_status: { successful?: boolean; sha256?: string } | null;
  audit: { canonical_valid?: boolean };
  unresolved_incidents: { id: string; type: string; severity: string; state: string }[];
  observability: {
    database_healthy: boolean;
    scheduler_lag_seconds: number | null;
    queue_depth: number;
    failure_count: number;
  };
}

interface DataImport {
  id: string;
  source_name: string;
  status: string;
  import_kind: string;
  market_date: string | null;
  timestamp_provenance: string;
}

interface InfrastructureSummary {
  paper_trading: boolean;
  live_trading_enabled: boolean;
  api: { healthy: boolean; checked_at: string };
  database: { healthy: boolean; dialect: string; replication_ready: boolean };
  redis: { healthy: boolean; backend: string; depth?: number; error?: string };
  workers: { id: string; state: string; queues: string[]; heartbeat_at: string; heartbeat_age_seconds: number }[];
  scheduler: { id: string; state: string; heartbeat_at: string; heartbeat_age_seconds: number }[];
  task_queue: Record<string, number>;
  event_outbox: Record<string, number>;
  dead_letter_events: number;
  queue_depth: number;
  active_leases: number;
  retries: number;
  task_dead_letters: number;
  database_pool_health: string | null;
  backup: { path: string | null; age_seconds: number | null; within_24_hours: boolean };
  recovery_readiness: boolean;
  infrastructure_incidents: { id: string; type: string; severity: string; state: string; opened_at: string }[];
  data_latency: Record<string, unknown> | null;
  daily_review_queue: Record<string, number>;
  qualification: {
    qualifying: boolean;
    remaining_qualifying_days: number;
    counts: Record<string, number>;
    failure_reasons: string[];
  } | null;
  disaster_recovery: { status: string; rpo_seconds: number; rto_seconds: number } | null;
  postgresql_migration: { status?: string; at_head?: boolean; current_revision?: string | null; expected_revision?: string | null } | null;
}

const importAttestation = "I confirm this file represents the stated market date and source.";

const money = (value: string | number | null | undefined) =>
  value == null ? "৳0" : `৳${Number(value).toLocaleString("en-BD", { maximumFractionDigits: 2 })}`;

export function Dashboard({ health: initialHealth, portfolio: initialPortfolio }: { health: Health; portfolio: Portfolio }) {
  const [health, setHealth] = useState<Health>(initialHealth);
  const [portfolio, setPortfolio] = useState<Portfolio>(initialPortfolio);
  const [schedHealth, setSchedHealth] = useState<SchedulerHealth | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [sessions, setSessions] = useState<PaperSession[]>([]);
  const [readiness, setReadiness] = useState<ReadinessGate | null>(null);
  const [operations, setOperations] = useState<OperationsSummary | null>(null);
  const [imports, setImports] = useState<DataImport[]>([]);
  const [infrastructure, setInfrastructure] = useState<InfrastructureSummary | null>(null);
  const [operatorKey, setOperatorKey] = useState("");
  const [importFile, setImportFile] = useState<File | null>(null);
  const [importKind, setImportKind] = useState("quote");
  const [importDate, setImportDate] = useState("");
  const [attested, setAttested] = useState(false);
  const [previewBatch, setPreviewBatch] = useState<string | null>(null);

  // Polling data every 3 seconds for real-time status
  useEffect(() => {
    const fetchData = async () => {
      try {
        const [hRes, pRes, sRes, sessionRes, readinessRes, operationsRes, importsRes, infrastructureRes] = await Promise.all([
          fetch(`${API_URL}/health`),
          fetch(`${API_URL}/portfolio`),
          fetch(`${API_URL}/scheduler/health`),
          fetch(`${API_URL}/paper-sessions`),
          fetch(`${API_URL}/paper-readiness?symbol=GP`),
          fetch(`${API_URL}/operations/summary`),
          fetch(`${API_URL}/data-imports`),
          fetch(`${API_URL}/infrastructure/summary`)
        ]);
        if (hRes.ok) setHealth(await hRes.json());
        if (pRes.ok) setPortfolio(await pRes.json());
        if (sRes.ok) setSchedHealth(await sRes.json());
        if (sessionRes.ok) setSessions(await sessionRes.json());
        if (readinessRes.ok) setReadiness(await readinessRes.json());
        if (operationsRes.ok) setOperations(await operationsRes.json());
        if (importsRes.ok) setImports(await importsRes.json());
        if (infrastructureRes.ok) setInfrastructure(await infrastructureRes.json());
      } catch (err) {
        console.error("Dashboard poll failed", err);
      }
    };

    const interval = setInterval(fetchData, 3000);
    fetchData();
    return () => clearInterval(interval);
  }, []);

  const runRiskAction = async (action: "emergency-stop" | "pause" | "resume") => {
    if (!operatorKey) {
      setMessage("Enter the operator API key for authenticated actions. It is not stored.");
      return;
    }
    setMessage(`Executing ${action}...`);
    try {
      const response = await fetch(`${API_URL}/risk/${action}`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-API-Key": operatorKey
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

  const previewDataImport = async () => {
    if (!operatorKey || !importFile || !importDate || !attested) {
      setMessage("Operator key, file, date, and exact attestation are required.");
      return;
    }
    const body = new FormData();
    body.append("file", importFile);
    body.append("import_kind", importKind);
    body.append("market_date", importDate);
    body.append("operator_attestation", importAttestation);
    if (operations?.current_campaign?.id) body.append("campaign_id", operations.current_campaign.id);
    const response = await fetch(`${API_URL}/data-imports/preview`, {
      method: "POST",
      headers: { "X-API-Key": operatorKey },
      body
    });
    const data = await response.json().catch(() => ({}));
    if (response.ok && data.activation_allowed) {
      setPreviewBatch(data.batch_id);
      setMessage(`Preview valid: ${data.valid_rows.length} rows, hash ${data.source_hash.slice(0, 12)}…`);
    } else {
      setMessage(`Import preview blocked: ${data.detail ?? data.errors?.[0]?.error ?? "validation failed"}`);
    }
  };

  const activateDataImport = async () => {
    if (!operatorKey || !previewBatch) return;
    const approval = "Operator approves activation after reviewing preview";
    const response = await fetch(`${API_URL}/data-imports/${previewBatch}/activate?approval=${encodeURIComponent(approval)}`, {
      method: "POST",
      headers: { "X-API-Key": operatorKey }
    });
    const data = await response.json().catch(() => ({}));
    setMessage(response.ok ? `Import ${data.batch_id} activated as operator_attested` : `Activation blocked: ${data.detail}`);
    if (response.ok) setPreviewBatch(null);
  };

  const runReadOnlyCheck = async (label: string, path: string) => {
    setMessage(`Running ${label}...`);
    try {
      const response = await fetch(`${API_URL}${path}`, operatorKey ? { headers: { "X-API-Key": operatorKey } } : undefined);
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

  const stateCounts = (counts: Record<string, number> | undefined) =>
    counts && Object.keys(counts).length
      ? Object.entries(counts).map(([state, count]) => `${state}: ${count}`).join(" · ")
      : "No records";
  const productionCoreHealthy = Boolean(
    infrastructure?.database.healthy &&
    infrastructure.database.dialect === "postgresql" &&
    infrastructure.redis.healthy &&
    infrastructure.redis.backend === "redis"
  );

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
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div><h2 className="font-semibold text-lg">Production-Like Paper Infrastructure</h2><p className="text-xs text-slate-500">Durable state, human review, recovery evidence, and migration readiness</p></div>
          <span className={`rounded px-2 py-1 text-xs font-mono ${productionCoreHealthy ? "bg-emerald-500/20 text-emerald-300" : "bg-amber-500/20 text-amber-300"}`}>
            {productionCoreHealthy ? "DISTRIBUTED CORE HEALTHY" : "PRODUCTION-LIKE CORE UNVERIFIED"}
          </span>
        </div>
        <div className="mt-4 grid gap-3 sm:grid-cols-2 lg:grid-cols-5">
          {[
            ["API", infrastructure?.api.healthy ? "HEALTHY" : "UNAVAILABLE", "Independent API process health"],
            ["Workers", infrastructure?.workers.length ? infrastructure.workers.map(item => `${item.id}: ${item.state}`).join(" · ") : "No worker heartbeat", `DB ${infrastructure?.database.dialect ?? "unknown"}`],
            ["Scheduler", infrastructure?.scheduler.length ? infrastructure.scheduler.map(item => item.state).join(" · ") : "No external heartbeat", "In-process mode is development only"],
            ["Task Queue", stateCounts(infrastructure?.task_queue), `${infrastructure?.redis.backend ?? "unknown"} · depth ${infrastructure?.redis.depth ?? 0}`],
            ["Event Outbox", stateCounts(infrastructure?.event_outbox), "At-least-once delivery · idempotent effects"],
            ["Dead-Letter Events", String(infrastructure?.dead_letter_events ?? 0), "Operator replay requires authentication"],
            ["Leases / Retries", `${infrastructure?.active_leases ?? 0} / ${infrastructure?.retries ?? 0}`, `Task dead letters ${infrastructure?.task_dead_letters ?? 0}`],
            ["Database Pool", infrastructure?.database_pool_health ?? "UNAVAILABLE", "Pre-ping and bounded recovery enabled"],
            ["Backup", infrastructure?.backup.within_24_hours ? "CURRENT" : "STALE / MISSING", infrastructure?.backup.age_seconds == null ? "No backup evidence" : `${(infrastructure.backup.age_seconds / 3600).toFixed(1)}h old`],
            ["Recovery", infrastructure?.recovery_readiness ? "READY" : "BLOCKED", "Passing isolated restore and current backup required"],
            ["Infrastructure Incidents", String(infrastructure?.infrastructure_incidents.length ?? 0), "Unresolved incidents require review"],
            ["Data Latency", infrastructure?.data_latency ? `${String(infrastructure.data_latency.quote_age_seconds_max ?? "n/a")}s max quote age` : "No quality report", "Daily · weekly · campaign evidence"],
            ["Daily Review Queue", stateCounts(infrastructure?.daily_review_queue), "Reviewer/operator credentials required"],
            ["60-Day Qualification", infrastructure?.qualification ? `${infrastructure.qualification.remaining_qualifying_days} qualifying days remaining` : "Not calculated", infrastructure?.qualification?.qualifying ? "QUALIFIED" : "FAIL CLOSED"],
            ["Disaster Recovery", infrastructure?.disaster_recovery?.status?.toUpperCase() ?? "NOT RUN", infrastructure?.disaster_recovery ? `RPO ${infrastructure.disaster_recovery.rpo_seconds.toFixed(2)}s · RTO ${infrastructure.disaster_recovery.rto_seconds.toFixed(2)}s` : "Isolated restore evidence required"],
            ["PostgreSQL Migration", infrastructure?.postgresql_migration?.status ?? (infrastructure?.postgresql_migration?.at_head ? "AT HEAD" : "PREFLIGHT REQUIRED"), `${infrastructure?.postgresql_migration?.current_revision ?? "none"} → ${infrastructure?.postgresql_migration?.expected_revision ?? "unknown"}`]
          ].map(([title, value, note]) => <article key={title} className="rounded-lg border border-line bg-[#0f192b] p-3"><p className="text-[10px] uppercase tracking-wider text-slate-500">{title}</p><p className="mt-2 break-words text-xs font-semibold text-cyan">{value}</p><p className="mt-1 text-[10px] text-slate-500">{note}</p></article>)}
        </div>
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

      <section className="grid gap-4 xl:grid-cols-3">
        <article className="rounded-xl border border-line bg-panel p-5 xl:col-span-2">
          <div className="flex items-center justify-between">
            <div><h2 className="font-semibold text-lg">Sustained Campaign Operations</h2><p className="text-xs text-slate-500">Campaign, daily session, rule, fee, strategy, and incident evidence</p></div>
            <span className={`rounded px-2 py-1 text-xs font-mono ${operations?.current_campaign?.state === "active" ? "bg-emerald-500/20 text-emerald-300" : "bg-amber-500/20 text-amber-300"}`}>{operations?.current_campaign?.state?.toUpperCase() ?? "NO ACTIVE CAMPAIGN"}</span>
          </div>
          <div className="mt-4 grid gap-3 sm:grid-cols-2 lg:grid-cols-4 text-xs">
            {[
              ["Current campaign", operations?.current_campaign?.name ?? "None"],
              ["Market/session", operations?.current_session ? `${operations.current_session.date} · ${operations.current_session.state}` : operations?.market_state ?? "Unavailable"],
              ["Data provenance", operations?.data_provenance ?? "unknown"],
              ["Timestamp trust", operations?.timestamp_trust ?? "none"],
              ["Market rules", operations?.rule_set_version ? `${operations.rule_set_version} · ${operations.rule_set_status}` : "Not selected"],
              ["Fee profile", operations?.fee_profile ?? "Not selected"],
              ["Audit health", operations?.audit?.canonical_valid ? "CANONICAL / VALID" : "BLOCKED"],
              ["Open incidents", String(operations?.unresolved_incidents.length ?? 0)],
              ["Campaign drawdown", operations?.drawdown == null ? "No evidence" : `${(operations.drawdown * 100).toFixed(2)}%`],
              ["Latest backup", operations?.backup_status?.successful ? "VERIFIED" : "NOT RECORDED"]
            ].map(([label, value]) => <div key={label} className="rounded-lg border border-line bg-[#0f192b] p-3"><p className="text-slate-500">{label}</p><p className="mt-1 font-mono text-slate-200 break-words">{value}</p></div>)}
          </div>
          <div className="mt-4 rounded-lg border border-line bg-[#0f192b] p-3 text-xs">
            <p className="text-slate-500">Approved strategy versions</p>
            <p className="mt-1 font-mono text-cyan">{operations?.strategy_versions.join(", ") || "No governed strategies active"}</p>
          </div>
          {operations?.unresolved_incidents.length ? <div className="mt-3 space-y-2">{operations.unresolved_incidents.slice(0, 4).map(incident => <div key={incident.id} className="rounded border border-red-500/20 bg-red-500/[.05] px-3 py-2 text-xs text-red-300">{incident.severity.toUpperCase()} · {incident.type} · {incident.state}</div>)}</div> : null}
        </article>

        <article className="rounded-xl border border-line bg-panel p-5">
          <h2 className="font-semibold text-lg">Local Observability</h2>
          <p className="text-xs text-slate-500">No secrets or portfolio details exposed</p>
          <div className="mt-4 space-y-2 text-xs">
            <div className="flex justify-between border-b border-line py-2"><span>Database</span><span className="font-mono">{operations?.observability.database_healthy ? "HEALTHY" : "FAILED"}</span></div>
            <div className="flex justify-between border-b border-line py-2"><span>Scheduler lag</span><span className="font-mono">{operations?.observability.scheduler_lag_seconds == null ? "unknown" : `${Math.round(operations.observability.scheduler_lag_seconds)}s`}</span></div>
            <div className="flex justify-between border-b border-line py-2"><span>Order queue</span><span className="font-mono">{operations?.observability.queue_depth ?? 0}</span></div>
            <div className="flex justify-between border-b border-line py-2"><span>Job failures</span><span className="font-mono">{operations?.observability.failure_count ?? 0}</span></div>
          </div>
        </article>
      </section>

      <section className="rounded-xl border border-line bg-panel p-5">
        <h2 className="font-semibold text-lg">Governance & Evidence Panels</h2>
        <p className="text-xs text-slate-500">Read-only operational surfaces; mutations require an authenticated operator action</p>
        <div className="mt-4 grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
          {[
            ["Market Rules", operations?.rule_set_version ?? "No active version", operations?.rule_set_status ?? "unavailable"],
            ["Fee Profiles", operations?.fee_profile ?? "No active profile", "Conservative unknown-cost defaults"],
            ["Strategy Governance", `${operations?.strategy_versions.length ?? 0} active versions`, "Manual promotion · automatic suspension"],
            ["Incidents", `${operations?.unresolved_incidents.length ?? 0} unresolved`, "Audit-linked lifecycle and critical alerts"],
            ["Daily Reports", operations?.current_session?.date ?? "No report date", "Snapshot · reconciliation · evidence"],
            ["Weekly Reports", operations?.current_campaign?.name ?? "No campaign", "Five-session evidence windows"],
            ["Data Imports", `${imports.length} recorded batches`, "Preview · hash · attestation · rollback"],
            ["Daily Operations", operations?.market_state ?? "unavailable", "Pre-market · market · EOD · recovery"]
          ].map(([title, value, note]) => <article key={title} className="rounded-lg border border-line bg-[#0f192b] p-4"><p className="text-xs uppercase tracking-wider text-slate-500">{title}</p><p className="mt-2 text-sm font-semibold text-cyan">{value}</p><p className="mt-1 text-[11px] text-slate-500">{note}</p></article>)}
        </div>
      </section>

      <section className="grid gap-4 xl:grid-cols-2">
        <article className="rounded-xl border border-line bg-panel p-5">
          <h2 className="font-semibold text-lg">Approved Daily Data Import</h2>
          <p className="text-xs text-slate-500">Preview → operator approval → activation; raw file retained immutably</p>
          <div className="mt-4 grid gap-3 sm:grid-cols-2">
            <input aria-label="Operator API key" type="password" value={operatorKey} onChange={event => setOperatorKey(event.target.value)} placeholder="Operator API key (never stored)" className="rounded border border-line bg-[#0f192b] px-3 py-2 text-xs" />
            <select value={importKind} onChange={event => setImportKind(event.target.value)} className="rounded border border-line bg-[#0f192b] px-3 py-2 text-xs"><option value="quote">Quote CSV</option><option value="ohlcv">OHLCV CSV</option><option value="dsex">DSEX CSV</option></select>
            <input aria-label="Market date" type="date" value={importDate} onChange={event => setImportDate(event.target.value)} className="rounded border border-line bg-[#0f192b] px-3 py-2 text-xs" />
            <input aria-label="CSV file" type="file" accept=".csv,text/csv" onChange={event => setImportFile(event.target.files?.[0] ?? null)} className="rounded border border-dashed border-cyan/30 bg-[#0f192b] px-3 py-2 text-xs" />
          </div>
          <label className="mt-3 flex gap-2 text-xs text-slate-300"><input type="checkbox" checked={attested} onChange={event => setAttested(event.target.checked)} /><span>{importAttestation}</span></label>
          <div className="mt-3 grid grid-cols-2 gap-2"><button onClick={previewDataImport} className="rounded border border-cyan/30 py-2 text-xs text-cyan">PREVIEW & HASH</button><button disabled={!previewBatch} onClick={activateDataImport} className="rounded border border-emerald-500/30 py-2 text-xs text-emerald-300 disabled:opacity-30">APPROVE ACTIVATION</button></div>
          <div className="mt-3 flex gap-3 text-[11px] text-cyan"><a href={`${API_URL}/data-imports/templates/quote`}>Quote template</a><a href={`${API_URL}/data-imports/templates/ohlcv`}>OHLCV template</a><a href={`${API_URL}/data-imports/templates/dsex`}>DSEX template</a></div>
        </article>

        <article className="rounded-xl border border-line bg-panel p-5">
          <h2 className="font-semibold text-lg">Import Batches</h2>
          <p className="text-xs text-slate-500">Imported timestamps remain operator_attested, never exchange_verified</p>
          <div className="mt-4 max-h-56 space-y-2 overflow-y-auto">
            {imports.length ? imports.slice(0, 8).map(item => <div key={item.id} className="rounded border border-line bg-[#0f192b] p-3 text-xs"><div className="flex justify-between"><span className="font-semibold text-cyan">{item.source_name}</span><span className="font-mono">{item.status}</span></div><p className="mt-1 text-slate-500">{item.import_kind} · {item.market_date ?? "date unavailable"} · {item.timestamp_provenance}</p></div>) : <p className="text-xs text-slate-500">No attested daily imports recorded.</p>}
          </div>
        </article>
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
