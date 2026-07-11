import Link from "next/link";

const names: Record<string, string> = {
  portfolio: "Portfolio", market: "Market", watchlist: "Watchlist", signals: "Signals", orders: "Orders",
  strategies: "Strategies", backtests: "Backtests", "risk-center": "Risk Center", "data-health": "Data Health",
  news: "News", settings: "Settings", "audit-log": "Audit Log"
};

export default async function Section({ params }: { params: Promise<{ section: string }> }) {
  const { section } = await params; const name = names[section] ?? "Not found";
  return <main className="p-5 md:p-8"><p className="text-xs font-semibold uppercase tracking-[.2em] text-cyan">DSE AutoTrader</p><h1 className="mt-2 text-3xl font-semibold">{name}</h1><section className="mt-8 rounded-xl border border-line bg-panel p-8"><h2 className="font-semibold">Milestone 1 control surface</h2><p className="mt-3 max-w-2xl text-sm leading-6 text-slate-400">This page is connected to the same fail-closed paper environment. Operational actions are available through the documented API; the overview exposes current portfolio, system, data and risk status.</p><Link href="/" className="mt-6 inline-block rounded-lg bg-cyan px-4 py-2 text-sm font-bold text-ink">Return to overview</Link></section></main>;
}

