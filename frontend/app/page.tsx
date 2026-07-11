import { Dashboard } from "@/components/Dashboard";
import { getJson } from "@/lib/api";
import type { Health, Portfolio } from "@/types";

const fallbackHealth: Health = { application: "offline", database: false, trading_mode: "paper", live_trading_enabled: false, audit_chain_valid: false };
const fallbackPortfolio: Portfolio = { holdings: [], total_cost: "0", total_market_value: null, total_unrealized_pnl: null, total_realized_pnl: "0", dividend_income: "0", cash: "0" };

export default async function Home() {
  const [health, portfolio] = await Promise.all([getJson<Health>("/health", fallbackHealth), getJson<Portfolio>("/portfolio", fallbackPortfolio)]);
  return <Dashboard health={health} portfolio={portfolio}/>;
}

