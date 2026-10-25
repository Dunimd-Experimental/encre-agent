---
name: revolut
description: Revolut neobank operations - transfers, cards, currency exchange
user_invocable: true
hidden: true
context: inline
---

## Revolut
# Revolut Banking Automation

Fetch current account balances, investment portfolio holdings, and transactions for all wallet currencies and depots in JSON format. Uses Playwright to automate Revolut web banking.

**Entry point:** `{baseDir}/scripts/revolut.py`

## Setup

See [SETUP.md](SETUP.md) for prerequisites and setup instructions.

## Commands

```shell
python3 {baseDir}/scripts/revolut.py --user oliver login
python3 {baseDir}/scripts/revolut.py --user oliver accounts
python3 {baseDir}/scripts/revolut.py --user oliver transactions --from YYYY-MM-DD --until YYYY-MM-DD
python3 {baseDir}/scripts/revolut.py --user sylvia portfolio
python3 {baseDir}/scripts/revolut.py --user oliver invest-transactions --from YYYY-MM-DD --until YYYY-MM-DD
```

## Updating Banker (Full Refresh)

To fully synchronize Revolut data into the `banker` archive:

```shell
# 1. Login (needs approval via Revolut App)
python3 {baseDir}/scripts/revolut.py --user oliver login

# 2. Export accounts + portfolios
python3 {baseDir}/scripts/revolut.py --user oliver --out /tmp/rev_accounts.json accounts
python3 {baseDir}/scripts/revolut.py --user oliver --out /tmp/rev_portfolio.json portfolio

# 3. Export transactions (separated by wallet and invest depot)
# Find Wallet IDs from the accounts JSON (e.g. EUR wallet, USD wallet)
python3 {baseDir}/scripts/revolut.py --user oliver --out /tmp/rev_tx_eur.json transactions --account <EUR-WALLET-ID> --from 2025-01-01 --until 2026-12-31
python3 {baseDir}/scripts/revolut.py --user oliver --out /tmp/rev_tx_usd.json transactions --account <USD-WALLET-ID> --from 2025-01-01 --until 2026-12-31
python3 {baseDir}/scripts/revolut.py --user oliver --out /tmp/rev_invest_tx.json invest-transactions --from 2025-01-01 --until 2026-12-31

# 4. Import into banker
# Accounts and Wallet transactions
python3 ~/Developer/Skills/banker/scripts/banker.py import --bank oliver@revolut /tmp/rev_accounts.json /tmp/rev_tx_eur.json /tmp/rev_tx_usd.json

# Invest portfolio & transactions (must specify --account revolut_gia)
python3 ~/Developer/Skills/banker/scripts/banker.py import --bank oliver@revolut --account revolut_gia /tmp/rev_portfolio.json
python3 ~/Developer/Skills/banker/scripts/banker.py import --bank oliver@revolut --account revolut_gia --kind transactions /tmp/rev_invest_tx.json

# 5. Logout
python3 {baseDir}/scripts/revolut.py --user oliver logout
```

## Recommended Flow

```
login → accounts → transactions → portfolio → logout
```

Always call `logout` after completing all operations to delete the stored browser session.

## Notes
- Per-user state stored in `{workspace}/revolut/` (deleted by `logout`).
- Output paths (`--out`) are sandboxed to workspace or `/tmp`.
- No `.env` file loading — credentials in config.json only.