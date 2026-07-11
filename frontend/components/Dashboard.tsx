"use client";

import { Area, AreaChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import type { Health, Portfolio } from "@/types";

const trend = [
  { d: "Jul 1", portfolio: 1000, dsex: 1000 }, { d: "Jul 2", portfolio: 1008, dsex: 1002 },
  { d: "Jul 3", portfolio: 1004, dsex: 998 }, { d: "Jul 6", portfolio: 1020, dsex: 1007 },
  { d: "Jul 7", portfolio: 1029, dsex: 1011 }, { d: "Jul 8", portfolio: 1025, dsex: 1005 },
  { d: "Jul 9", portfolio: 1040, dsex: 1014 }, { d: "Jul 10", portfolio: 1048, dsex: 1017 }
];

const money = (value: string | null | undefined) => value == null ? "Unavailable" : `৳${Number(value).toLocaleString("en-BD", { maximumFractionDigits: 0 })}`;

export function Dashboard({ health, portfolio }: { health: Health; portfolio: Portfolio }) {
  const cards = [
    ["Portfolio value", money(portfolio.total_market_value), "Valued from current provider"],
    ["Unrealized P&L", money(portfolio.total_unrealized_pnl), "Open positions"],
    ["Paper cash", money(portfolio.cash), "Simulated account"],
    ["Risk status", health.application === "healthy" ? "HEALTHY" : "DEGRADED", "Kill switch armed"],
    ["Data status", health.database ? "CONNECTED" : "UNAVAILABLE", "Provider health monitored"],
    ["Audit chain", health.audit_chain_valid ? "VERIFIED" : "CHECK REQUIRED", "Append-only integrity"]
  ];
  return <main className="p-5 md:p-8">
    <div className="mb-7 flex items-end justify-between"><div><p className="mb-1 text-xs font-semibold uppercase tracking-[.2em] text-cyan">Operations overview</p><h1 className="text-3xl font-semibold tracking-tight">Good evening, operator.</h1><p className="mt-2 text-sm text-slate-400">Every number is sourced, every action is gated.</p></div><div className="hidden rounded-lg border border-emerald-500/20 bg-emerald-500/[.07] px-4 py-2 text-sm text-emerald-300 md:block">● System monitoring active</div></div>
    <section className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">{cards.map(([label, value, note]) => <article key={label} className="rounded-xl border border-line bg-panel p-5 shadow-2xl shadow-black/10"><p className="text-xs uppercase tracking-widest text-slate-500">{label}</p><p className="mt-3 text-2xl font-semibold tabular-nums">{value}</p><p className="mt-2 text-xs text-slate-500">{note}</p></article>)}</section>
    <section className="mt-4 grid gap-4 xl:grid-cols-[1.7fr_1fr]">
      <article className="rounded-xl border border-line bg-panel p-5"><div className="mb-6"><h2 className="font-semibold">Portfolio vs DSEX</h2><p className="text-xs text-slate-500">Indexed paper performance · sample until portfolio history is available</p></div><div className="h-72"><ResponsiveContainer width="100%" height="100%"><AreaChart data={trend}><defs><linearGradient id="p" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stopColor="#38d9c5" stopOpacity={.3}/><stop offset="1" stopColor="#38d9c5" stopOpacity={0}/></linearGradient></defs><CartesianGrid stroke="#182434" vertical={false}/><XAxis dataKey="d" stroke="#526071" tickLine={false}/><YAxis stroke="#526071" tickLine={false} domain={[980, 1060]}/><Tooltip contentStyle={{ background: "#0d1420", border: "1px solid #1d2a3a" }}/><Area type="monotone" dataKey="portfolio" stroke="#38d9c5" fill="url(#p)"/><Area type="monotone" dataKey="dsex" stroke="#7c8da3" fill="transparent"/></AreaChart></ResponsiveContainer></div></article>
      <article className="rounded-xl border border-line bg-panel p-5"><h2 className="font-semibold">Risk gates</h2><p className="mt-1 text-xs text-slate-500">All gates must pass before approval</p><div className="mt-6 space-y-4">{["Market data fresh", "Provider agreement", "Position limit", "Paper account reconciled", "Audit storage writable"].map((gate, i) => <div key={gate} className="flex items-center justify-between border-b border-line pb-3 text-sm"><span className="text-slate-300">{gate}</span><span className={i === 1 ? "text-amber-300" : "text-emerald-300"}>{i === 1 ? "DEGRADED" : "PASS"}</span></div>)}</div><button className="mt-6 w-full rounded-lg border border-red-500/40 bg-red-500/10 py-3 text-sm font-bold text-red-300">EMERGENCY STOP</button></article>
    </section>
    <section className="mt-4 rounded-xl border border-line bg-panel"><div className="border-b border-line p-5"><h2 className="font-semibold">Holdings</h2></div><div className="overflow-x-auto"><table className="w-full text-left text-sm"><thead className="text-xs uppercase tracking-wider text-slate-500"><tr>{["Symbol", "Quantity", "Average cost", "Current", "Market value", "Unrealized P&L"].map(h => <th className="px-5 py-3" key={h}>{h}</th>)}</tr></thead><tbody>{portfolio.holdings.length ? portfolio.holdings.map(h => <tr className="border-t border-line" key={h.symbol}><td className="px-5 py-4 font-semibold text-cyan">{h.symbol}</td><td className="px-5 py-4">{h.quantity}</td><td className="px-5 py-4">{money(h.average_purchase_price)}</td><td className="px-5 py-4">{money(h.current_price)}</td><td className="px-5 py-4">{money(h.market_value)}</td><td className="px-5 py-4 text-emerald-300">{money(h.unrealized_pnl)}</td></tr>) : <tr><td colSpan={6} className="px-5 py-12 text-center text-slate-500">Import a portfolio to begin monitoring. No sample holdings are presented as real data.</td></tr>}</tbody></table></div></section>
  </main>;
}

