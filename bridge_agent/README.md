# Trading OS bridge agent (Windows)

IBKR and moomoo expose their APIs only through a gateway process running on
**your own PC** (IBKR's Client Portal Gateway, moomoo's OpenD). The Trading OS
API runs on Railway and cannot reach your PC's `localhost`. This agent closes
that gap without opening any port on your PC:

```
Railway API  <----- HTTPS (outbound only) -----  bridge agent  --->  IBKR gateway (localhost:5000)
 bridge_jobs                                     on your PC     --->  moomoo OpenD (127.0.0.1:11111)
```

The agent long-polls the platform for jobs (read the balance, read positions,
preview an order...), runs each one against the local gateway, and posts the
answer back. It sends a heartbeat every 15 s saying which gateways it can
reach and whether they are logged in. Your PC listens on nothing new.

**What stays on your PC:** your IBKR login (you type it into the gateway's
own web page), the moomoo trade password, the gateway addresses. **What is on
the platform:** the bridge token, and public fields only: which account, SIMULATE
or REAL, whether orders are armed. No broker password is ever sent to or stored
on the platform.

**Orders are preview-only by default.** Unless the platform's broker
credential field `live_orders` is set to `true`, every order is checked (IBKR's
what-if, moomoo's trading-info pre-check) and **nothing is placed**. Leave it
off until you have run probes and previews and mean to trade.

---

## 1. Prerequisites

* Windows 10/11 with **Python 3.11+** (`python --version`). From python.org,
  tick "Add python.exe to PATH".
* For IBKR: **Java 8+** (the gateway is a Java program). `java -version`.
* For moomoo: **moomoo OpenD**, from moomoo's API download page.

## 2. IBKR: run the Client Portal Gateway

1. Download the **Client Portal Gateway** (the "Web API" gateway zip, not TWS) from
   IBKR's API download page and unzip it, e.g. to `C:\ibkr\clientportal.gw`.
2. Start it from a terminal in that folder:
   ```
   bin\run.bat root\conf.yaml
   ```
   Leave that window open. It listens on `https://localhost:5000`.
3. Open **https://localhost:5000** in a browser on the same PC. The certificate
   warning is expected (IBKR's own self-signed certificate); proceed. Log in with
   your IBKR username and password and complete 2FA. The page then says
   "Client login succeeds".
4. **This login expires daily** (and whenever IBKR resets sessions, typically
   around midnight US Eastern). When it does, the platform reports
   `GATEWAY_NOT_AUTHENTICATED` and nothing is queued; log in again in the
   browser. The agent keeps an active session alive with `/tickle` but cannot
   log in for you, by design.
5. Only one IBKR session per username may be "brokerage" at a time: if TWS or
   the mobile app is logged in with the same user, the gateway reports
   `competing`. Use a paper login (`DU...` account) to rehearse.

The agent only trusts the gateway's self-signed certificate when the URL is
`localhost` / `127.0.0.1`. Any other host is TLS-verified.

## 3. moomoo: run OpenD (optional)

1. Install and start **moomoo OpenD**, log in with your moomoo ID in its window.
   Default API address: `127.0.0.1:11111`.
2. Install the Python SDK into the agent's venv: run the launcher with
   `-Moomoo` (below), which runs `pip install futu-api`.
3. SIMULATE (paper) needs no unlock. REAL orders need the trade password in
   `MOOMOO_TRADE_PASSWORD` in `bridge_agent\.env` on this PC; it is used only to
   unlock right before a real order and is never sent anywhere.

The moomoo handler is written against futu-api's documented interface and
tested with a stand-in module. It has **not** yet been run against a real
OpenD, so start with a probe, then a preview, in SIMULATE.

## 4. Configure the platform (Railway variables)

| Variable | Value |
|---|---|
| `BRIDGE_AGENT_TOKEN` | A long random secret, at least 32 characters. Generate: `python -c "import secrets; print(secrets.token_urlsafe(48))"` |
| `IBKR_ACCOUNT_ID` | Your IBKR account, e.g. `DU1234567` (paper) or `U1234567` |
| `IBKR_ACCOUNT_CURRENCY` | Optional, default `USD` |
| `MOOMOO_ACCOUNT_ID` / `MOOMOO_TRADE_ENV` | moomoo only: numeric acc_id, and `SIMULATE` or `REAL` (no default) |

Leave `IBKR_LIVE_ORDERS` / `MOOMOO_LIVE_ORDERS` **unset**.

## 5. Configure the agent

1. Copy the `bridge_agent` folder to the PC (or use a checkout of the repo).
2. Copy `bridge_agent\.env.example` to `bridge_agent\.env` and fill in
   `PLATFORM_URL` (the **API** URL), `BRIDGE_AGENT_TOKEN` (the same value as on
   Railway), `BRIDGE_PROVIDERS`, and `IBKR_ACCOUNT_ID` (the agent refuses jobs for
   any other account). `.env` is gitignored; never commit it.
3. Check everything in one go:
   ```
   powershell -ExecutionPolicy Bypass -File .\bridge_agent\run_agent.ps1 -Check
   ```
   The first run creates `bridge_agent\.venv` and installs `httpx`. Exit code 0
   means the platform accepted the token and every gateway is reachable and
   logged in; otherwise the log line names what is wrong.

## 6. Run it

```
powershell -ExecutionPolicy Bypass -File .\bridge_agent\run_agent.ps1
```

It logs to the window and to `bridge_agent\bridge_agent.log` (rotated at 2 MB).
Ctrl+C stops it. If it crashes, the launcher restarts it after 30 s; a
configuration error (exit code 2) is not retried.

Then, in Trading OS: **Admin -> Broker bridge -> Supported brokers -> Test** on
Interactive Brokers. A green result shows your IBKR cash and the conids you hold,
read through the agent. `GET /bridge/status` (admin) shows the agent's last
heartbeat and each gateway's state.

## 7. Start automatically at logon (Task Scheduler)

The gateway login needs you at a browser anyway, so run the agent when you log
in rather than as a system service:

```
$script = "C:\path\to\trading-os\bridge_agent\run_agent.ps1"
$action = New-ScheduledTaskAction -Execute "powershell.exe" `
  -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Minimized -File `"$script`""
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
  -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
Register-ScheduledTask -TaskName "TradingOS Bridge Agent" -Action $action -Trigger $trigger `
  -Settings $settings -Description "Trading OS bridge agent (outbound only)"
```

Remove it with `Unregister-ScheduledTask -TaskName "TradingOS Bridge Agent"`.
Start the IBKR gateway the same way (a second task running `bin\run.bat
root\conf.yaml` in its folder) if you want it up at logon too; you still log in
to it in the browser.

## What the platform will and will not do through this agent

* **Reads:** cash (per currency, from the IBKR ledger), positions (keyed by
  IBKR **conid**), order status.
* **Orders:** addressed by conid at IBKR (no ticker-to-contract mapping, on
  purpose) and by `US.AAPL`-style codes at moomoo. Preview-only unless
  `live_orders=true`. The agent **never auto-confirms** an IBKR warning prompt: if
  IBKR asks "are you sure?", the order is reported as not placed.
* **Expiry:** each job carries how long the platform will wait (25 s by default).
  The agent refuses to start an order with less than 8 s left, so an order can
  never be placed after the platform has given up on it. If the agent claimed an
  order and the answer comes back late, the platform reports
  `BRIDGE_OUTCOME_UNKNOWN`. **Check the venue's own order list before retrying.**
* **Security:** the token is compared as a SHA-256 digest; it grants only the
  three `/bridge/agent/*` routes, never a user session. Rotate it by changing it
  on Railway and in `.env` together.
