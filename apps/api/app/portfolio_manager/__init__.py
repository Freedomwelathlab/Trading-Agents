"""The trade-path Portfolio Manager (spec Sec18, D029) - the "Portfolio
Manager" box in docs/ARCHITECTURE.md's Target diagram, sitting between the
deterministic Risk Engine and the OMS's broker call.

Deliberately NOT the same thing as apps/api/app/portfolio/, which is the
read-only reporting module D022/D027 built (it answers "what does this
broker hold and what's its P&L" for a human or a UI, and never touches the
trade path). This package is a decision component: it receives a
risk-approved TradeProposal plus deterministic portfolio state and returns
an APPROVE / MODIFY / REJECT PortfolioDecision that the OMS acts on.

Like apps/api/app/risk/engine.py, everything here is a pure function: no
LLM, no network call, no database. See apps/api/app/portfolio_manager/
manager.py for the decision logic and docs/DECISIONS.md D029 for which of
spec Sec18's listed considerations are implemented and which are explicitly
deferred for want of data this repo does not yet store.
"""
