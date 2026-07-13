# Fee Profiles

Named versioned profiles support effective date, broker/account labels, side-specific brokerage, minimum brokerage, exchange and regulatory fees, taxes, settlement and account charges, and flat charges.

Unknown inputs use conservative modeling defaults: 0.50% brokerage per side, BDT 10 minimum, exchange/regulatory allowances, sell-side tax allowance, and settlement charge. These are not verified broker quotations.

Calculations return a component breakdown and total plus 0.75x, 1.0x, and 1.25x sensitivities. Campaigns lock the profile ID so costs cannot drift silently.
