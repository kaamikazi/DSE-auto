# DSE Data Limitations

Public DSE-derived packages may lag, change HTML structure, omit trades/depth/corporate actions, or report timestamps with ambiguous timezone/session semantics. Historical constituents can create survivorship bias. Price limits, auctions, halts, record dates and adjusted series are not consistently available.

The adapters therefore fail explicitly, the validator flags zero volume and conflicts, and order approval blocks on stale/unsafe data. Operators must validate provider licensing, exchange terms and correctness before depending on it. CSV remains the disaster-recovery import path.

