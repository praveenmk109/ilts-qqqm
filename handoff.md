# Session Handoff: QQQM Daily Trading Bot Deployment

This handoff document is written for the next AI agent or engineer taking over the deployment and maintenance of the QQQM Index Long Term Strategy (ILTS) trading bot. 

Follow these instructions to deploy and run the bot on an Ubuntu Cloud VM.

---

## 1. Project Context & Strategy Overview

The bot implements an **unleveraged QQQM ETF + Protective Put options strategy** on an Alpaca paper trading account. It is designed to capture market gains while capping downside risk.

### Core Strategy Logic:
1. **Holding**: 100 shares of QQQM ETF for every 1 protective put option contract held. 
2. **Monthly Roll**: On the first trading day of each month, the bot closes the old puts and purchases new puts expiring on the **third Friday of the next calendar month**.
3. **VIX-Dynamic Strikes**: Strikes are chosen dynamically based on VIX index close:
   * **VIX < 15**: 100% strike (ATM)
   * **15 <= VIX <= 25**: 95% strike (OTM)
   * **VIX > 25**: 90% strike (OTM)
4. **Daily Crash Reinvestment**: If the put options intrinsic value gains **$\ge 3.0$x (300%)** of the initial premium paid:
   * Sell all puts to lock in profit.
   * Buy **100 additional shares of QQQM** at cheap crashed prices.
   * Re-buy puts to cover the new total shares count expiring on the same date.
   * Set `crash_reinvestment_triggered = true` in state.

---

## 2. Directory Structure

The `prod` folder contains the following files:
* **`trade_bot.py`**: The core execution script.
* **`alpaca_config.py`**: Configurations, target symbols, and API key loading (reads from `.env`).
* **`requirements.txt`**: Python dependencies.
* **`.env`**: All secrets (API keys, Discord webhook URL) — **restricted to owner only**.
* **`.venv/`**: Python virtual environment (isolated dependencies).

*Note: Once run, `trade_bot.py` will automatically create `bot_state.json` in the same directory to track state between daily runs.*

---

## 3. Deployment Instructions on Ubuntu VM

Follow these exact commands to set up the environment and run the bot.

### Step 1: Install Python Dependencies
Update the VM, clone/transfer this directory, and install requirements:
```bash
sudo apt update
sudo apt install -y python3-pip
python3 -m venv prod/.venv
prod/.venv/bin/pip install -r prod/requirements.txt
```

> **Secrets**: API keys and the Discord webhook URL are stored in `prod/.env`. Ensure this file exists with the correct values before running (restricted to owner `chmod 600`).

### Step 2: Establish the Initial Position (Run on Monday)
Since today is Sunday, the US markets are closed. Running the bot live now will result in an immediate exit.
* **Action**: On **Monday morning (after 9:30 AM EST)**, execute the bot live to place the initial orders:
  ```bash
  cd /path/to/prod
  .venv/bin/python3 trade_bot.py
  ```
* **Expected Result**: 
  1. The bot will buy **100 shares of QQQM** (market order).
  2. The bot will place a limit order for **1 Put contract** of `QQQM260717P00290000` (or the closest strike on the July 17, 2026 contract) at the mid-price of bid/ask.
  3. The bot will wait for execution (chasing order if unfilled) and initialize `bot_state.json` with the purchase price and symbol.
  4. You will receive a status notification on Discord.

### Step 3: Schedule the Daily Run
Schedule the bot to run every weekday at **3:50 PM EST** (10 minutes before the market close) using `cron`.
1. Verify the timezone of your VM:
   ```bash
   date
   ```
2. Open your crontab editor:
   ```bash
   crontab -e
   ```
3. Add the cron job. Adjust the hour according to your VM's timezone (3:50 PM EST is **20:50 UTC** or **19:50 UTC** depending on Daylight Saving Time):
   ```cron
    # Example for a UTC timezone server (3:50 PM EST = 20:50 UTC)
    50 20 * * 1-5 cd /path/to/prod && .venv/bin/python3 trade_bot.py >> bot_run.log 2>&1
   ```

---

## 4. Discord Alerts and Monitoring

The bot uses the Discord webhook URL in `alpaca_config.py`.
* **Daily Report**: At the end of every run, a summary report is posted containing cash balance, spot price, VIX, current positions, and actions taken (roll details or crash triggers).
* **Failure Alerts**: If any error occurs (API failures, unfilled orders, or network drops), the bot catches the exception and immediately posts a red alert containing the traceback to Discord.

---

## 5. Technical Context & Troubleshooting (For the LLM)

### Option Contract Lookup Workaround:
Alpaca's Trading API endpoint `/v2/options/contracts` has a server-side bug returning 0 active contracts when querying `underlying_symbols=["QQQM"]`. 
* **Workaround implemented**: The bot queries the option chain using `OptionHistoricalDataClient.get_option_chain` and parses the symbols manually (e.g. parsing `QQQM260717P00290000` to extract the expiry, strike, and type). **Do not replace this logic with standard TradingClient queries, as they will fail.**

### Option Chasing Logic:
Options have wider bid-ask spreads. To get filled safely:
* The bot places a limit order at the mid-price of the bid/ask.
* It waits 20 seconds. If unfilled, it cancels, fetches updated quotes, and places a new mid-price order.
* On the 3rd (final) attempt, it crosses the spread (buying at the ask, selling at the bid) to guarantee execution before the market close.

### State Recovery:
If `bot_state.json` is deleted or corrupted, the bot will treat the next execution as a new month rollover and attempt to roll positions. If you need to manually restore the state, recreate `bot_state.json` with this format:
```json
{
  "last_roll_month": 6,
  "active_strike": 290.0,
  "active_expiry": "2026-07-17",
  "initial_premium_paid": 3.35,
  "option_symbol": "QQQM260717P00290000",
  "crash_reinvestment_triggered": false
}
```
Replace the values above with your actual positions on Alpaca.
