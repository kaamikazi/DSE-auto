import Link from "next/link";

const links = ["Overview", "Portfolio", "Market", "Watchlist", "Signals", "Orders", "Strategies", "Backtests", "Risk Center", "Data Health", "News", "Settings", "Audit Log"];

export function Sidebar() {
  return <aside className="hidden w-64 shrink-0 border-r border-line bg-[#090f18] px-5 py-7 lg:block">
    <Link href="/" className="mb-9 flex items-center gap-3">
      <span className="grid h-10 w-10 place-items-center rounded-xl bg-cyan font-black text-ink">DA</span>
      <span><strong className="block tracking-tight">DSE AutoTrader</strong><small className="text-slate-500">Control surface</small></span>
    </Link>
    <nav className="space-y-1">{links.map((link, index) => {
      const slug = index === 0 ? "" : link.toLowerCase().replaceAll(" ", "-");
      return <Link key={link} href={`/${slug}`} className="flex items-center gap-3 rounded-lg px-3 py-2.5 text-sm text-slate-400 transition hover:bg-white/5 hover:text-white">
        <span className="w-5 text-center text-slate-600">{String(index + 1).padStart(2, "0")}</span>{link}
      </Link>;
    })}</nav>
  </aside>;
}

