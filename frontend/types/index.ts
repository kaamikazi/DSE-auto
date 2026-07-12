export interface Health { application: string; database: boolean; trading_mode: string; live_trading_enabled: boolean; audit_chain_valid: boolean; provider?: any }
export interface Holding { symbol: string; quantity: string; average_purchase_price: string; current_price: string | null; market_value: string | null; unrealized_pnl: string | null; allocation_percent: string | null }
export interface Portfolio { holdings: Holding[]; total_cost: string; total_market_value: string | null; total_unrealized_pnl: string | null; total_realized_pnl: string; dividend_income: string; cash: string }

