export function StatusBanners() {
  return <div className="flex flex-wrap items-center gap-2 border-b border-amber-500/20 bg-amber-500/[.06] px-5 py-2 text-xs font-bold tracking-[.16em]">
    <span className="rounded-full bg-amber-400 px-3 py-1 text-ink">PAPER TRADING</span>
    <span className="rounded-full border border-red-400/40 px-3 py-1 text-red-300">LIVE TRADING DISABLED</span>
    <span className="ml-auto font-normal tracking-normal text-slate-500">Human approval + deterministic risk checks required</span>
  </div>;
}

