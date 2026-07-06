import os
import sys
import time
import datetime
import json
import argparse
import re
import yfinance as yf

# Add workspace directory to path to load config
current_dir = os.path.dirname(os.path.abspath(__file__))
if current_dir not in sys.path:
    sys.path.append(current_dir)
import alpaca_config as config
import requests

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import LimitOrderRequest, MarketOrderRequest, GetOptionContractsRequest, ReplaceOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce, OrderStatus, PositionIntent, ContractType, AssetStatus
from alpaca.data.historical import OptionHistoricalDataClient, StockHistoricalDataClient
from alpaca.data.requests import OptionLatestQuoteRequest, StockLatestQuoteRequest

# --- State File Path ---
STATE_FILE_PATH = os.path.join(current_dir, "bot_state.json")

# --- Discord Notification Helper ---
def send_discord_alert(message):
    url = getattr(config, 'DISCORD_WEBHOOK_URL', None)
    if not url:
        return
    try:
        payload = {"content": message}
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        print(f"Failed to send Discord alert: {e}")

# --- State Persistence Helpers ---
def load_state():
    if os.path.exists(STATE_FILE_PATH):
        try:
            with open(STATE_FILE_PATH, 'r') as f:
                state = json.load(f)
                print(f"Loaded state from {STATE_FILE_PATH}")
                return state
        except Exception as e:
            print(f"[Error] Failed to load state file: {e}")
            
    print("No valid state file found. Initializing new state.")
    return {
        "last_roll_month": None,
        "active_strike": None,
        "active_expiry": None,
        "initial_premium_paid": None,
        "option_symbol": None,
        "crash_reinvestment_triggered": False
    }

def save_state(state):
    try:
        # Create directory if it doesn't exist
        os.makedirs(os.path.dirname(STATE_FILE_PATH), exist_ok=True)
        with open(STATE_FILE_PATH, 'w') as f:
            json.dump(state, f, indent=2)
        print(f"Saved state to {STATE_FILE_PATH}")
    except Exception as e:
        print(f"[Error] Failed to save state file: {e}")

# --- Helper: Get Target Expiry (Third Friday of Next Calendar Month) ---
def get_third_friday(year, month):
    """Find the third Friday of a given year and month."""
    for day in range(15, 22):
        d = datetime.date(year, month, day)
        if d.weekday() == 4:  # Friday is index 4
            return d
    return None

def get_target_expiry(current_date):
    """Get the standard options expiry date for the next calendar month."""
    m_next = current_date.month + 1
    y_next = current_date.year
    if m_next > 12:
        m_next = 1
        y_next += 1
    return get_third_friday(y_next, m_next)

# --- Helper: Get Current VIX ---
def get_current_vix():
    print("Fetching VIX from yfinance...")
    try:
        vix_ticker = yf.Ticker('^VIX')
        hist = vix_ticker.history(period='1d')
        if not hist.empty:
            vix = float(hist['Close'].iloc[-1])
            print(f"Current VIX Close: {vix:.2f}")
            return vix
    except Exception as e:
        print(f"[Warning] Failed to fetch VIX: {e}. Falling back to 15.0")
    return 15.0

# --- Helper: Calculate Strike Percentage from VIX ---
def get_strike_pct(vix):
    return 1.0  # ATM (user-elected Jun 25)

# --- Helper: Option Chain Search (Workaround) ---
def find_active_put_contract(trading_client, underlying, target_expiry, target_strike):
    """Query Alpaca option contracts API to find the closest PUT contract for target expiry and strike."""
    print(f"Searching option contracts for {underlying} expiring near {target_expiry}...")
    
    # Try exact expiry first, then widen by ±2 days
    for offset in [0, 1, -1, 2, -2]:
        search_date = target_expiry + datetime.timedelta(days=offset)
        req = GetOptionContractsRequest(
            underlying_symbols=[underlying],
            status=AssetStatus.ACTIVE,
            expiration_date=search_date,
            type=ContractType.PUT
        )
        contracts = trading_client.get_option_contracts(req)
        if contracts and contracts.option_contracts:
            break
    
    if not contracts or not contracts.option_contracts:
        print(f"[Error] No active put contracts found for {underlying} near {target_expiry} (checked ±2 days)")
        return None, None, (0.0, 0.0)
    
    if offset != 0:
        print(f"  Exact expiry had no contracts; using {search_date} instead.")
    
    # Find closest strike
    closest = min(contracts.option_contracts, key=lambda c: abs(float(c.strike_price) - target_strike))
    strike = float(closest.strike_price)
    
    # We don't get quotes from contracts API, so return 0s — quotes fetched live during chasing
    print(f"Found contract: {closest.symbol} (Strike: ${strike:.2f}, Expiry: {search_date})")
    return closest.symbol, strike, (0.0, 0.0)

# --- Helper: Submit Option Orders with Chasing ---
def submit_option_order_with_chasing(trading_client, data_client, symbol, qty, side, max_attempts=3, dry_run=False):
    qty = int(qty)
    print(f"  [Option Order] Side: {side.value.upper()}, Qty: {qty}, Symbol: {symbol} (Dry-run: {dry_run})")
    if dry_run:
        return 1.0

    from alpaca.trading.requests import ReplaceOrderRequest

    # Cancel any existing stale orders for this symbol first
    existing = trading_client.get_orders()
    for o in existing:
        if o.symbol == symbol:
            try:
                trading_client.cancel_order_by_id(o.id)
                print(f"  Cancelled stale order {o.id}")
            except Exception:
                pass

    now = datetime.datetime.now()
    market_close = now.replace(hour=15, minute=0, second=0, microsecond=0)
    if now >= market_close:
        print("  Market already closed. Cannot trade.")
        return None

    total_seconds = (market_close - now).total_seconds()
    segment_seconds = total_seconds / max_attempts
    print(f"  Market closes in {total_seconds:.0f}s. {max_attempts} segments of {segment_seconds:.0f}s each.")

    spread_fractions = {1: 0.30, 2: 0.50, 3: 1.00}
    check_interval = 10
    order = None

    for attempt in range(1, max_attempts + 1):
        # Fetch latest quotes
        req_quote = OptionLatestQuoteRequest(symbol_or_symbols=symbol)
        try:
            res_quote = data_client.get_option_latest_quote(req_quote)
            quote = res_quote.get(symbol)
        except Exception as e:
            print(f"  [Segment {attempt}] Failed to fetch quote: {e}")
            if attempt == 1:
                time.sleep(3)
                continue
            else:
                break

        if not quote or quote.bid_price is None or quote.ask_price is None or quote.bid_price == 0 or quote.ask_price == 0:
            print(f"  [Segment {attempt}] No valid bid/ask quotes.")
            if attempt == 1:
                time.sleep(3)
                continue
            else:
                break

        spread = quote.ask_price - quote.bid_price
        frac = spread_fractions[attempt]
        if side == OrderSide.BUY:
            limit_price = round(quote.bid_price + frac * spread, 2)
        else:
            limit_price = round(quote.ask_price - frac * spread, 2)
        print(f"  [Segment {attempt}/{max_attempts}] bid=${quote.bid_price:.2f} ask=${quote.ask_price:.2f} spread=${spread:.2f} → price at {frac:.0%} = ${limit_price:.2f}")

        if order:
            # Use replace_order_by_id to update price (no gap)
            try:
                replace_req = ReplaceOrderRequest(limit_price=limit_price)
                order = trading_client.replace_order_by_id(order.id, replace_req)
                print(f"  Replaced order {order.id} to ${limit_price:.2f}")
            except Exception as e:
                print(f"  Replace failed: {e}. Submitting new order.")
                try:
                    trading_client.cancel_order_by_id(order.id)
                except Exception:
                    pass
                order = None
        else:
            # First submission: also check for any existing orders for this symbol and cancel them
            try:
                for o in trading_client.get_orders():
                    if o.symbol == symbol and o.id != getattr(order, 'id', None):
                        trading_client.cancel_order_by_id(o.id)
            except Exception:
                pass

        if not order:
            order_req = LimitOrderRequest(
                symbol=symbol, qty=qty, side=side,
                limit_price=limit_price, time_in_force=TimeInForce.DAY,
                position_intent=PositionIntent.BUY_TO_OPEN if side == OrderSide.BUY else PositionIntent.SELL_TO_CLOSE
            )
            try:
                order = trading_client.submit_order(order_req)
                print(f"  Order submitted. ID: {order.id}, Status: {order.status}")
            except Exception as e:
                print(f"  [Error] Failed to submit order: {e}")
                order = None
                continue

        # Wait until end of segment, checking periodically
        segment_end = now + datetime.timedelta(seconds=attempt * segment_seconds)
        while True:
            remaining = (segment_end - datetime.datetime.now()).total_seconds()
            if remaining <= 0:
                break
            time.sleep(min(check_interval, remaining))
            try:
                updated_order = trading_client.get_order_by_id(order.id)
                if updated_order.status == OrderStatus.FILLED:
                    avg_price = float(updated_order.filled_avg_price)
                    print(f"  Order FILLED at ${avg_price:.2f}")
                    return avg_price
            except Exception as e:
                print(f"  Failed to check order status: {e}")

        print(f"  Segment {attempt} ended — order still unfilled.")

    # Final check + cleanup
    if order:
        try:
            final_check = trading_client.get_order_by_id(order.id)
            if final_check.status == OrderStatus.FILLED:
                avg_price = float(final_check.filled_avg_price)
                print(f"  Order FILLED at ${avg_price:.2f}")
                return avg_price
        except Exception:
            pass
        try:
            trading_client.cancel_order_by_id(order.id)
            print(f"  Cancelled leftover order {order.id}")
        except Exception:
            pass

    print(f"  [Error] Failed to fill option order after {max_attempts} segments.")
    return None

# --- Helper: Submit Shares Orders ---
def submit_shares_order(trading_client, symbol, qty, side, dry_run=False):
    if qty == 0:
        return True
    qty = abs(int(qty))
    print(f"  [Shares Order] Side: {side.value.upper()}, Qty: {qty}, Symbol: {symbol} (Dry-run: {dry_run})")
    if dry_run:
        return True
        
    order_req = MarketOrderRequest(
        symbol=symbol,
        qty=qty,
        side=side,
        time_in_force=TimeInForce.DAY
    )
    try:
        order = trading_client.submit_order(order_req)
        print(f"  Shares order submitted. ID: {order.id}, Status: {order.status}")
        max_wait = 60
        waited = 0
        total_filled = 0
        while waited < max_wait:
            time.sleep(3)
            waited += 3
            filled_order = trading_client.get_order_by_id(order.id)
            if filled_order.status == OrderStatus.FILLED:
                filled_qty = int(filled_order.filled_qty)
                filled_avg = float(filled_order.filled_avg_price)
                print(f"  Shares order FILLED: {filled_qty} shares @ avg ${filled_avg:.2f}")
                return True
            elif filled_order.status in (OrderStatus.CANCELED, OrderStatus.REJECTED):
                if int(filled_order.filled_qty) > 0:
                    filled_qty = int(filled_order.filled_qty)
                    filled_avg = float(filled_order.filled_avg_price)
                    print(f"  Shares order partially filled then {filled_order.status}: {filled_qty} shares @ ${filled_avg:.2f}")
                    return True
                print(f"  Shares order {filled_order.status}. Failed.")
                return False
            else:
                filled_so_far = int(filled_order.filled_qty) if hasattr(filled_order, 'filled_qty') and filled_order.filled_qty else 0
                if filled_so_far > total_filled:
                    total_filled = filled_so_far
                    print(f"  Shares order: {filled_so_far}/{qty} filled ({waited}s elapsed)...")
                else:
                    print(f"  Shares order status: {filled_order.status} ({waited}s elapsed)...")
        print(f"  [Error] Shares order still partial after {max_wait}s (filled: {total_filled}/{qty}).")
        # If partially filled, count it as success
        if total_filled > 0:
            return True
        return False
    except Exception as e:
        print(f"  [Error] Failed to submit shares order: {e}")
        return False

# --- Core Bot Execution Function ---
def execute_bot(dry_run=False, force_roll=False, force_crash=False):
    now_str = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    print("\n" + "="*60)
    print(f"ILTS DAILY TRADING BOT RUN (Time: {now_str})")
    print(f"Mode: {'DRY-RUN (No orders placed)' if dry_run else 'LIVE PAPER'}")
    print("="*60)
    
    # Initialize report fields for Discord
    status_summary = "Normal Monitoring"
    report_details = []
    spot = 0.0
    vix = 15.0
    cash = 0.0
    shares_held = 0
    puts_held = 0
    old_put_qty = 0
    shares_delta = 0
    target_contracts = 0
    new_symbol = ""
    old_qty = 0
    opt_price = 0.0
    new_contracts = 0
    is_roll_date = False
    is_crash_trigger = False
    
    try:
        # Initialize clients
        trading_client = TradingClient(config.ALPACA_API_KEY, config.ALPACA_SECRET_KEY, paper=True)
        trading_data_client = StockHistoricalDataClient(config.ALPACA_API_KEY, config.ALPACA_SECRET_KEY)
        option_data_client = OptionHistoricalDataClient(config.ALPACA_API_KEY, config.ALPACA_SECRET_KEY)
        
        # 1. Market Open Check
        clock = trading_client.get_clock()
        if not clock.is_open and not dry_run:
            print("Market is closed. Cannot trade. Exiting.")
            send_discord_alert(f"💤 **[ILTS Bot]** Run skipped at {now_str} (Market is closed).")
            return
            
        # Clean up any stale orders from previous failed runs (only our symbols)
        try:
            stale_orders = trading_client.get_orders()
            qqqm_symbols = [config.TARGET_SYMBOL]
            for o in stale_orders:
                if any(s in o.symbol for s in qqqm_symbols):
                    print(f"  Cleaning up stale order: {o.id} {o.symbol} {o.side} limit=${o.limit_price}")
                    trading_client.cancel_order_by_id(o.id)
        except Exception as e:
            print(f"  [Warning] Failed to clean up stale orders: {e}")
            
        # Load current bot state
        state = load_state()
        
        # Get current QQQM spot price
        print("Fetching QQQM latest price...")
        req_quote = StockLatestQuoteRequest(symbol_or_symbols=config.TARGET_SYMBOL)
        quotes = trading_data_client.get_stock_latest_quote(req_quote)
        quote = quotes.get(config.TARGET_SYMBOL)
        if not quote or quote.bid_price == 0 or quote.ask_price == 0:
            raise Exception(f"Failed to get latest price for {config.TARGET_SYMBOL} from Alpaca API.")
        spot = round((quote.bid_price + quote.ask_price) / 2.0, 2)
        print(f"QQQM Spot Price: ${spot:.2f} (Bid: ${quote.bid_price:.2f}, Ask: ${quote.ask_price:.2f})")
        
        # Get current positions
        positions = trading_client.get_all_positions()
        active_puts_pos = None
        shares_cost_basis = 0.0
        shares_market_value = 0.0
        tracked_option_cost_basis = 0.0
        tracked_option_market_value = 0.0
        all_option_positions = []  # ALL option positions (tracked or not)
        extra_option_positions = []  # untracked options for warning
        tracked_option_symbol = state.get("option_symbol")
        
        for pos in positions:
            if pos.symbol == config.TARGET_SYMBOL:
                shares_held = int(float(pos.qty))
                shares_cost_basis = float(pos.cost_basis)
                shares_market_value = float(pos.market_value)
            elif "QQQM" in pos.symbol and "P" in pos.symbol:
                all_option_positions.append(pos)
                if tracked_option_symbol and pos.symbol == tracked_option_symbol:
                    puts_held = int(float(pos.qty))
                    active_puts_pos = pos
                    tracked_option_cost_basis = float(pos.cost_basis)
                    tracked_option_market_value = float(pos.market_value)
                else:
                    extra_option_positions.append(pos)
        
        # Total portfolio metrics include ALL positions (shares + all options)
        option_market_value = sum(float(p.market_value) for p in all_option_positions)
        option_cost_basis = sum(float(p.cost_basis) for p in all_option_positions)
        print(f"Current Positions: QQQM Shares = {shares_held} | All puts = {[p.symbol for p in all_option_positions]} | Tracked ({state.get('option_symbol')}) = {puts_held}")
        
        if extra_option_positions:
            for ep in extra_option_positions:
                pl = float(ep.unrealized_pl)
                print(f"[WARNING] Untracked option position: {ep.symbol} qty={ep.qty} cost=${float(ep.cost_basis):.2f} P&L=${pl:+.2f}")
                report_details.append(f"⚠️ Untracked: `{ep.symbol}` qty={ep.qty} P&L=${pl:+.2f}")
        
        # Get account cash
        account = trading_client.get_account()
        cash = float(account.cash)
        print(f"Account Cash Balance: ${cash:,.2f}")
        
        # Fetch current VIX
        vix = get_current_vix()
        
        # Check if today is a Roll Date (First trading day of the calendar month)
        today = datetime.date.today()
        current_month = today.month
        
        is_roll_date = (current_month != state.get("last_roll_month")) or force_roll
        
        # --- MONTHLY ROLL DATE ---
        if is_roll_date:
            status_summary = "Monthly Rollover Executed"
            print("\n=== TRIGGERING MONTHLY ROLL ===")
            if force_roll:
                print("  (Forced roll triggered via flag)")
                
            # 1. Close existing put options
            old_put_symbol = state.get("option_symbol")
            old_put_qty = puts_held
            old_put_revenue = 0.0
            
            if old_put_symbol and old_put_qty > 0:
                print(f"Closing existing puts: {old_put_qty} contracts of {old_put_symbol}...")
                req_old_quote = OptionLatestQuoteRequest(symbol_or_symbols=old_put_symbol)
                try:
                    res_old_quote = option_data_client.get_option_latest_quote(req_old_quote)
                    old_quote = res_old_quote.get(old_put_symbol)
                    old_mid = round((old_quote.bid_price + old_quote.ask_price) / 2.0, 2) if old_quote else 0.0
                except Exception:
                    old_mid = 0.0
                    
                avg_sell_price = submit_option_order_with_chasing(
                    trading_client, option_data_client, old_put_symbol, old_put_qty, OrderSide.SELL, dry_run=dry_run
                )
                if avg_sell_price:
                    old_put_revenue = old_put_qty * 100.0 * avg_sell_price
                    print(f"Closed puts. Estimated revenue added: ${old_put_revenue:.2f}")
                else:
                    print("[Warning] Failed to close old puts. Proceeding with cash calculations using mid-price...")
                    old_put_revenue = old_put_qty * 100.0 * old_mid
            else:
                print("No old puts to close.")
                
            # 2. Determine target options contract specifications
            strike_pct = get_strike_pct(vix)
            target_strike = spot * strike_pct
            target_expiry = get_target_expiry(today)
            print(f"Target Specifications: Strike Pct = {strike_pct:.2f} ({'ATM' if strike_pct==1.0 else 'OTM'}), Expiry = {target_expiry}")
            
            # 3. Find closest PUT contract on Alpaca
            new_symbol, new_strike, new_quote = find_active_put_contract(
                trading_client, config.TARGET_SYMBOL, target_expiry, target_strike
            )
            
            if not new_symbol:
                raise Exception("Failed to find a matching options contract for monthly roll.")
                
            # Fetch live quote for sizing
            try:
                q_req = OptionLatestQuoteRequest(symbol_or_symbols=new_symbol)
                q_res = option_data_client.get_option_latest_quote(q_req)
                opt_q = q_res.get(new_symbol)
                bid = float(opt_q.bid_price) if (opt_q and opt_q.bid_price is not None) else 0.0
                ask = float(opt_q.ask_price) if (opt_q and opt_q.ask_price is not None) else 0.0
            except Exception:
                bid = ask = 0.0
            new_put_mid_price = round((bid + ask) / 2.0, 2) if (bid > 0 and ask > 0) else 1.0
            
            # 4. Cash calculations for share sizing
            estimated_cash_pool = cash + old_put_revenue
            block_cost = 100.0 * (spot + new_put_mid_price)
            
            # Determine number of contracts/shares blocks we want to hold (compounding)
            target_contracts = int(estimated_cash_pool / block_cost)
            target_contracts = max(1, target_contracts) # Always hold at least 1 contract
            target_shares = target_contracts * 100
            
            print(f"Sizing Calculation:")
            print(f"  Estimated Cash Pool:  ${estimated_cash_pool:,.2f}")
            print(f"  Block Cost:           ${block_cost:,.2f}")
            print(f"  Target Contracts:     {target_contracts} (representing {target_shares} shares)")
            
            # Adjust shares held
            shares_delta = target_shares - shares_held
            shares_order_side = OrderSide.BUY if shares_delta > 0 else OrderSide.SELL
            
            if shares_delta != 0:
                print(f"Adjusting QQQM shares by {shares_delta} ({shares_order_side.value})...")
                success = submit_shares_order(trading_client, config.TARGET_SYMBOL, abs(shares_delta), shares_order_side, dry_run=dry_run)
                if not success and not dry_run:
                    raise Exception(f"Failed to execute shares trade: {shares_delta} {shares_order_side.value}")
            else:
                print("Shares count already matches target. No shares adjustment needed.")
                
            # 5. Buy new puts
            print(f"Buying new puts: {target_contracts} contracts of {new_symbol}...")
            avg_buy_price = submit_option_order_with_chasing(
                trading_client, option_data_client, new_symbol, target_contracts, OrderSide.BUY, dry_run=dry_run
            )
            
            if avg_buy_price is None and not dry_run:
                raise Exception(f"Failed to buy new protective puts: {target_contracts} contracts of {new_symbol}")
                
            # 6. Update State
            state["last_roll_month"] = current_month
            state["active_strike"] = new_strike
            state["active_expiry"] = str(target_expiry)
            state["initial_premium_paid"] = avg_buy_price if avg_buy_price else new_put_mid_price
            state["option_symbol"] = new_symbol
            state["crash_reinvestment_triggered"] = False
            
            save_state(state) if not dry_run else print("  [Dry-run] State not saved.")
            print("Monthly roll completed successfully!")
            
        # --- DAILY MONITORING (CRASH CHECK) ---
        else:
            print("\n=== DAILY CRASH REINVESTMENT CHECK ===")
            active_symbol = state.get("option_symbol")
            initial_premium = state.get("initial_premium_paid")
            active_strike = state.get("active_strike")
            active_expiry_str = state.get("active_expiry")
            crash_triggered = state.get("crash_reinvestment_triggered", False)
            
            if not active_symbol or not initial_premium:
                print("No active option contract registered in state. Cannot run crash check. Run with --force-roll to initialize positioning.")
                status_summary = "Missing State / Uninitialized"
                report_details.append("⚠️ No active option in state. Run with `--force-roll` to initialize positions.")
            elif crash_triggered and not force_crash:
                print("Crash reinvestment already triggered for this month. Monitoring complete.")
                status_summary = "Monitoring Complete (Crash Already Triggered)"
                report_details.append("🛡️ Crash reinvestment has already triggered earlier this month. Standing by until next monthly roll.")
            else:
                # Fetch current price of active option contract
                req_opt_quote = OptionLatestQuoteRequest(symbol_or_symbols=active_symbol)
                try:
                    res_opt_quote = option_data_client.get_option_latest_quote(req_opt_quote)
                    opt_quote = res_opt_quote.get(active_symbol)
                except Exception as e:
                    raise Exception(f"Failed to fetch active option quote for {active_symbol}: {e}")
                    
                if not opt_quote:
                    raise Exception(f"No latest quote returned for active option {active_symbol}")
                    
                opt_price = round((opt_quote.bid_price + opt_quote.ask_price) / 2.0, 2)
                opt_intrinsic = max(0.0, active_strike - spot)
                
                # Log details
                print(f"Active Option Symbol:  {active_symbol}")
                print(f"Initial Premium Paid:  ${initial_premium:.2f}")
                print(f"Active Strike:         ${active_strike:.2f}")
                print(f"Current Option Price:  ${opt_price:.2f} (Mid-price)")
                print(f"Current Put Intrinsic: ${opt_intrinsic:.2f}")
                
                multiplier = opt_intrinsic / initial_premium
                print(f"Premium Gain Multiplier: {multiplier:.2f}x (Crash Threshold: {config.CRASH_THRESHOLD:.1f}x)")
                
                # Check trigger
                is_crash_trigger = (multiplier >= config.CRASH_THRESHOLD) or force_crash
                
                if is_crash_trigger:
                    status_summary = "CRASH REINVESTMENT TRIGGERED!"
                    print("\n=== TRIGGERING CRASH REINVESTMENT ===")
                    if force_crash:
                        print("  (Forced crash triggered via flag)")
                    
                    # Mark as triggered FIRST so partial failures don't cause share accumulation
                    state["crash_reinvestment_triggered"] = True
                    save_state(state) if not dry_run else print("  [Dry-run] State not saved.")
                    print("  Crash flagged in state — prevents re-entry on subsequent days.")
                        
                    # 1. Close existing put options (sell)
                    old_qty = puts_held
                    old_put_revenue = 0.0
                    
                    if old_qty > 0:
                        print(f"Selling active puts to realize profit: {old_qty} contracts of {active_symbol}...")
                        avg_sell_price = submit_option_order_with_chasing(
                            trading_client, option_data_client, active_symbol, old_qty, OrderSide.SELL, dry_run=dry_run
                        )
                        if avg_sell_price:
                            old_put_revenue = old_qty * 100.0 * avg_sell_price
                        else:
                            print("[Warning] Failed to sell puts. Continuing with mid-price estimate.")
                            old_put_revenue = old_qty * 100.0 * opt_price
                    else:
                        print("No puts held in positions.")
                        
                    # 2. Buy 100 additional QQQM shares
                    print("Buying 100 additional shares of QQQM...")
                    success_shares = submit_shares_order(trading_client, config.TARGET_SYMBOL, 100, OrderSide.BUY, dry_run=dry_run)
                    if not success_shares and not dry_run:
                        print("[Warning] Share purchase may have partially failed.")
                    
                    # 3. Find and buy new puts to cover the new total shares count
                    new_contracts = puts_held + 1
                    if new_contracts < 1:
                        new_contracts = 1
                        
                    # Calculate new strike based on today's spot and VIX
                    strike_pct = get_strike_pct(vix)
                    target_strike = spot * strike_pct
                    target_expiry = datetime.datetime.strptime(active_expiry_str, "%Y-%m-%d").date() if active_expiry_str else get_target_expiry(today)
                    
                    new_symbol, new_strike, new_quote = find_active_put_contract(
                        trading_client, config.TARGET_SYMBOL, target_expiry, target_strike
                    )
                    
                    if not new_symbol:
                        print("[Warning] Failed to find option contract for crash reinvestment. State already saved — will retry order.")
                    else:
                        try:
                            q_req = OptionLatestQuoteRequest(symbol_or_symbols=new_symbol)
                            q_res = option_data_client.get_option_latest_quote(q_req)
                            opt_q = q_res.get(new_symbol)
                            bid = float(opt_q.bid_price) if (opt_q and opt_q.bid_price is not None) else 0.0
                            ask = float(opt_q.ask_price) if (opt_q and opt_q.ask_price is not None) else 0.0
                        except Exception:
                            bid = ask = 0.0
                        new_put_mid_price = round((bid + ask) / 2.0, 2) if (bid > 0 and ask > 0) else 1.0
                        
                        print(f"Buying new puts to cover total shares ({new_contracts * 100} shares): {new_contracts} contracts of {new_symbol}...")
                        avg_buy_price = submit_option_order_with_chasing(
                            trading_client, option_data_client, new_symbol, new_contracts, OrderSide.BUY, dry_run=dry_run
                        )
                        
                        if avg_buy_price is not None or dry_run:
                            # 4. Update State with new option details
                            state["active_strike"] = new_strike
                            state["initial_premium_paid"] = avg_buy_price if avg_buy_price else new_put_mid_price
                            state["option_symbol"] = new_symbol
                            
                    save_state(state) if not dry_run else print("  [Dry-run] State not saved.")
                    print("Crash reinvestment completed! (Any order failures will retry next run with fresh quotes.)")
                else:
                    status_summary = "Hedge Value Normal"
                    print("Hedge ratio is normal. No crash reinvestment needed today.")
                    report_details.append(f"🛡️ Put option multiplier: **{multiplier:.2f}x** (VIX: {vix:.2f}). No crash trigger today.")
                    
        # --- SEND SUCCESS DISCORD REPORT ---
        emoji = "🧪" if dry_run else "🚀"
        
        # Compute portfolio metrics
        portfolio_value = cash + shares_market_value + option_market_value
        
        shares_pl = shares_market_value - shares_cost_basis
        option_pl = option_market_value - option_cost_basis
        net_pl = shares_pl + option_pl
        
        shares_pl_pct = (shares_pl / shares_cost_basis * 100) if shares_cost_basis else 0
        tracked_opt_price_mid = (tracked_option_market_value / (puts_held * 100)) if puts_held > 0 else (
            state.get('initial_premium_paid', 0) if state.get('option_symbol') else 0.0
        )
        
        report = []
        report.append(f"{emoji} **ILTS Daily Trading Report ({'DRY-RUN' if dry_run else 'LIVE PAPER'})**")
        report.append(f"📅 **Date**: {now_str}")
        report.append(f"🔍 **Status**: **{status_summary}**")
        report.append("")
        
        # --- Portfolio Summary ---
        report.append("**📊 PORTFOLIO SUMMARY**")
        report.append(f"💰 **Portfolio Value**: **${portfolio_value:,.2f}**")
        report.append(f"💵 **Cash**: ${cash:,.2f}")
        report.append("")
        
        # --- Shares Section ---
        report.append("**📈 QQQM SHARES**")
        report.append(f"  Quantity: {shares_held} shares")
        report.append(f"  Current Price: **${spot:.2f}**")
        report.append(f"  Market Value: ${shares_market_value:,.2f}")
        report.append(f"  Cost Basis: ${shares_cost_basis:,.2f}")
        report.append(f"  P&L: **${shares_pl:+,.2f}** ({shares_pl_pct:+.2f}%)")
        report.append("")
        
        # --- Options Section ---
        tracked_opt_pl = tracked_option_market_value - tracked_option_cost_basis if tracked_option_cost_basis else 0
        report.append("**🛡️ PROTECTIVE PUT**")
        report.append(f"  Contract: `{state.get('option_symbol', 'N/A')}`")
        report.append(f"  Strike: **${state.get('active_strike', 0):.1f}** | Expiry: **{state.get('active_expiry', 'N/A')}**")
        report.append(f"  Current Price: **${tracked_opt_price_mid:.2f}**")
        report.append(f"  Market Value: ${tracked_option_market_value:,.2f}")
        report.append(f"  Premium Paid: ${state.get('initial_premium_paid', 0):.2f}")
        opt_pl_str = f"**${tracked_opt_pl:+,.2f}**"
        if tracked_option_cost_basis:
            opt_pl_str += f" ({tracked_opt_pl/tracked_option_cost_basis*100:+.2f}%)"
        report.append(f"  Option P&L: {opt_pl_str}")
        
        # Intrinsic value & hedge check
        intrinsic = max(0.0, state.get('active_strike', 0) - spot)
        report.append(f"  Intrinsic Value: ${intrinsic:.2f}")
        if state.get('initial_premium_paid', 0) > 0 and intrinsic > 0:
            mult = intrinsic / state['initial_premium_paid']
            report.append(f"  Crash Multiplier: **{mult:.2f}x** / 3.0x threshold")
        report.append("")
        
        # --- Net P&L ---
        report.append("**💵 NET PERFORMANCE**")
        report.append(f"  **Net P&L: ${net_pl:+,.2f}**")
        report.append(f"  **Portfolio Value: ${portfolio_value:,.2f}**")
        report.append("")
        
        # --- Market Context ---
        report.append("**🌍 MARKET CONTEXT**")
        report.append(f"  QQQM: **${spot:.2f}** | VIX: **{vix:.2f}**")
        if shares_cost_basis:
            breakeven = (shares_cost_basis + option_cost_basis) / shares_held if shares_held else 0
            report.append(f"  Portfolio Breakeven: ${breakeven:.2f}/share")
        
        # Details on actions
        if is_roll_date:
            report.append("")
            report.append("🔄 **Roll Action Details**:")
            report.append(f"  • Old puts closed: {old_put_qty} contracts")
            report.append(f"  • Shares adjustment: {shares_delta:+} shares")
            report.append(f"  • New puts bought: {target_contracts} contracts of `{new_symbol}` @ ${state['initial_premium_paid']:.2f}")
            report.append(f"  • New Strike: ${state['active_strike']:.2f} | New Expiry: {state['active_expiry']}")
        elif is_crash_trigger:
            report.append("")
            report.append("💥 **Crash Trigger Details**:")
            report.append(f"  • Active puts profit realized: {old_qty} contracts @ ${opt_price:.2f}")
            report.append(f"  • Additional shares bought: +100 shares")
            report.append(f"  • New puts bought: {new_contracts} contracts of `{new_symbol}` @ ${state['initial_premium_paid']:.2f}")
            report.append(f"  • New Strike: ${state['active_strike']:.2f}")
        else:
            for detail in report_details:
                report.append(detail)
                
        # Send to Discord
        send_discord_alert("\n".join(report))
        
    except Exception as e:
        # --- SEND ERROR ALERT ---
        error_msg = f"❌ **[ILTS Bot Error]** Daily bot run failed at {now_str}.\nError Details: `{str(e)}`"
        print(f"\n[CRITICAL ERROR] {e}")
        send_discord_alert(error_msg)
        raise e

# --- CLI Parser Entry ---
if __name__ == '__main__':
    parser = argparse.ArgumentParser(description="Alpaca Daily Trading Bot for Unleveraged QQQM Protective Puts")
    parser.add_argument('--dry-run', action='store_true', help="Print calculated orders without placing them")
    parser.add_argument('--force-roll', action='store_true', help="Force monthly roll execution immediately")
    parser.add_argument('--force-crash', action='store_true', help="Force crash reinvestment execution immediately")
    
    args = parser.parse_args()
    
    execute_bot(
        dry_run=args.dry_run,
        force_roll=args.force_roll,
        force_crash=args.force_crash
    )
