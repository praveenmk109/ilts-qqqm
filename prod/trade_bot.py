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
from alpaca.trading.requests import LimitOrderRequest, MarketOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce, OrderStatus, PositionIntent
from alpaca.data.historical import OptionHistoricalDataClient, StockHistoricalDataClient
from alpaca.data.requests import OptionChainRequest, OptionLatestQuoteRequest, StockLatestQuoteRequest

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
def find_active_put_contract(data_client, underlying, target_expiry, target_strike):
    """Query options chain for QQQM, parse symbols, and find the closest PUT contract."""
    print(f"Searching option chain for {underlying} expiring near {target_expiry}...")
    req = OptionChainRequest(underlying_symbol=underlying)
    chain = data_client.get_option_chain(req)
    
    parsed_options = []
    for sym in chain.keys():
        m = re.match(r"^([A-Z]{1,6})(\d{6})([CP])(\d{8})$", sym)
        if not m:
            continue
        und, expiry_str, op_type, strike_str = m.groups()
        if und != underlying or op_type != "P":
            continue
        try:
            expiry = datetime.datetime.strptime(expiry_str, "%y%m%d").date()
            strike = float(strike_str) / 1000.0
            parsed_options.append({
                "symbol": sym,
                "expiry": expiry,
                "strike": strike
            })
        except Exception:
            continue
            
    # Filter for target expiry
    expiry_puts = [o for o in parsed_options if o["expiry"] == target_expiry]
    
    # Fallback to nearest expiry (within 2 days) if exact match not found
    if not expiry_puts:
        expiry_puts = [o for o in parsed_options if abs((o["expiry"] - target_expiry).days) <= 2]
        
    if not expiry_puts:
        print(f"[Error] No active put contracts found for {underlying} expiring near {target_expiry}")
        return None, None, (0.0, 0.0)
        
    # Find closest strike
    closest = min(expiry_puts, key=lambda x: abs(x["strike"] - target_strike))
    
    # Get quote details from chain snapshot
    snapshot = chain[closest["symbol"]]
    quote = snapshot.latest_quote
    bid = float(quote.bid_price) if (quote and quote.bid_price is not None) else 0.0
    ask = float(quote.ask_price) if (quote and quote.ask_price is not None) else 0.0
    
    print(f"Found contract: {closest['symbol']} (Strike: ${closest['strike']}, Bid: ${bid:.2f}, Ask: ${ask:.2f})")
    return closest["symbol"], closest["strike"], (bid, ask)

# --- Helper: Submit Option Orders with Chasing ---
def submit_option_order_with_chasing(trading_client, data_client, symbol, qty, side, max_attempts=3, dry_run=False):
    qty = int(qty)
    print(f"  [Option Order] Side: {side.value.upper()}, Qty: {qty}, Symbol: {symbol} (Dry-run: {dry_run})")
    if dry_run:
        return 1.0 # return dummy avg price in dry-run
        
    attempt = 1
    order = None
    
    while attempt <= max_attempts:
        # Fetch latest quotes
        req_quote = OptionLatestQuoteRequest(symbol_or_symbols=symbol)
        try:
            res_quote = data_client.get_option_latest_quote(req_quote)
            quote = res_quote.get(symbol)
        except Exception as e:
            print(f"  [Attempt {attempt}] Failed to fetch quote: {e}. Retrying in 5 seconds...")
            time.sleep(5)
            attempt += 1
            continue
            
        if not quote or quote.bid_price == 0 or quote.ask_price == 0:
            print(f"  [Attempt {attempt}] No valid bid/ask quotes available. Retrying in 5 seconds...")
            time.sleep(5)
            attempt += 1
            continue
            
        # Determine limit price
        if attempt < max_attempts:
            # Mid-price limit order
            limit_price = round((quote.bid_price + quote.ask_price) / 2.0, 2)
            print(f"  [Attempt {attempt}] Placing limit order at mid-price: ${limit_price:.2f} (Bid: ${quote.bid_price:.2f}, Ask: ${quote.ask_price:.2f})")
        else:
            # Final attempt: cross the spread + small buffer to guarantee fill
            buffer = 0.05
            limit_price = (quote.ask_price + buffer) if side == OrderSide.BUY else (quote.bid_price - buffer)
            limit_price = round(limit_price, 2)
            print(f"  [Attempt {attempt}] Final attempt: crossing the spread + ${buffer:.2f} buffer. Limit price: ${limit_price:.2f} (Bid: ${quote.bid_price:.2f}, Ask: ${quote.ask_price:.2f})")
            
        # Create order request
        order_req = LimitOrderRequest(
            symbol=symbol,
            qty=qty,
            side=side,
            limit_price=limit_price,
            time_in_force=TimeInForce.DAY,
            position_intent=PositionIntent.BUY_TO_OPEN if side == OrderSide.BUY else PositionIntent.SELL_TO_CLOSE
        )
        
        # Cancel previous order if active
        if order:
            try:
                print(f"  Cancelling previous unfilled order {order.id}...")
                trading_client.cancel_order_by_id(order.id)
                time.sleep(2)
            except Exception as e:
                print(f"  Failed to cancel order: {e}")
                
        # Submit new order
        try:
            order = trading_client.submit_order(order_req)
            print(f"  Order submitted. ID: {order.id}, Status: {order.status}")
        except Exception as e:
            print(f"  [Error] Failed to submit order: {e}")
            attempt += 1
            continue
            
        # Wait and check if filled
        wait_time = 20
        print(f"  Waiting {wait_time} seconds for fill...")
        time.sleep(wait_time)
        
        # Retrieve updated order status
        try:
            updated_order = trading_client.get_order_by_id(order.id)
            if updated_order.status == OrderStatus.FILLED:
                avg_price = float(updated_order.filled_avg_price)
                print(f"  Order FILLED successfully at average fill price: ${avg_price:.2f}")
                return avg_price
            else:
                print(f"  Order status: {updated_order.status} (unfilled)")
        except Exception as e:
            print(f"  Failed to get order status: {e}")
            
        attempt += 1
        
    print(f"  [Error] Failed to fill option order after {max_attempts} attempts.")
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
        max_wait = 30
        waited = 0
        while waited < max_wait:
            time.sleep(3)
            waited += 3
            filled_order = trading_client.get_order_by_id(order.id)
            if filled_order.status == OrderStatus.FILLED:
                filled_qty = int(filled_order.filled_qty)
                print(f"  Shares order FILLED: {filled_qty} shares @ avg ${filled_order.filled_avg_price}")
                return True
            elif filled_order.status in (OrderStatus.CANCELED, OrderStatus.REJECTED):
                print(f"  Shares order {filled_order.status}. Failed.")
                return False
            else:
                print(f"  Shares order status: {filled_order.status} ({waited}s elapsed)...")
        print(f"  [Error] Shares order still {filled_order.status} after {max_wait}s.")
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
        option_market_value = 0.0
        option_cost_basis = 0.0
        
        for pos in positions:
            if pos.symbol == config.TARGET_SYMBOL:
                shares_held = int(float(pos.qty))
                shares_cost_basis = float(pos.cost_basis)
                shares_market_value = float(pos.market_value)
            elif pos.symbol == state.get("option_symbol"):
                puts_held = int(float(pos.qty))
                active_puts_pos = pos
                option_cost_basis = float(pos.cost_basis)
                option_market_value = float(pos.market_value)
                
        print(f"Current Positions: QQQM Shares = {shares_held} | Puts ({state.get('option_symbol')}) = {puts_held}")
        
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
                option_data_client, config.TARGET_SYMBOL, target_expiry, target_strike
            )
            
            if not new_symbol:
                raise Exception("Failed to find a matching options contract for monthly roll.")
                
            bid, ask = new_quote
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
            
            save_state(state)
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
                            old_put_revenue = old_qty * 100.0 * opt_price
                    else:
                        print("No puts held in positions.")
                        
                    # 2. Buy 100 additional QQQM shares
                    print("Buying 100 additional shares of QQQM...")
                    success_shares = submit_shares_order(trading_client, config.TARGET_SYMBOL, 100, OrderSide.BUY, dry_run=dry_run)
                    if not success_shares and not dry_run:
                        raise Exception("Failed to buy 100 additional shares of QQQM during crash reinvestment.")
                    
                    # 3. Find and buy new puts to cover the new total shares count
                    new_contracts = puts_held + 1
                    if new_contracts < 1:
                        new_contracts = 1
                        
                    # Calculate new strike based on today's spot and VIX
                    strike_pct = get_strike_pct(vix)
                    target_strike = spot * strike_pct
                    target_expiry = datetime.datetime.strptime(active_expiry_str, "%Y-%m-%d").date()
                    
                    new_symbol, new_strike, new_quote = find_active_put_contract(
                        option_data_client, config.TARGET_SYMBOL, target_expiry, target_strike
                    )
                    
                    if not new_symbol:
                        raise Exception("Failed to find a matching options contract for crash reinvestment puts.")
                        
                    bid, ask = new_quote
                    new_put_mid_price = round((bid + ask) / 2.0, 2) if (bid > 0 and ask > 0) else 1.0
                    
                    print(f"Buying new puts to cover total shares ({new_contracts * 100} shares): {new_contracts} contracts of {new_symbol}...")
                    avg_buy_price = submit_option_order_with_chasing(
                        trading_client, option_data_client, new_symbol, new_contracts, OrderSide.BUY, dry_run=dry_run
                    )
                    
                    if avg_buy_price is None and not dry_run:
                        raise Exception(f"Failed to buy new puts for crash reinvestment: {new_contracts} contracts of {new_symbol}")
                        
                    # 4. Update State
                    state["active_strike"] = new_strike
                    state["initial_premium_paid"] = avg_buy_price if avg_buy_price else new_put_mid_price
                    state["option_symbol"] = new_symbol
                    state["crash_reinvestment_triggered"] = True
                    
                    save_state(state)
                    print("Crash reinvestment completed successfully!")
                else:
                    status_summary = "Hedge Value Normal"
                    print("Hedge ratio is normal. No crash reinvestment needed today.")
                    report_details.append(f"🛡️ Put option multiplier: **{multiplier:.2f}x** (VIX: {vix:.2f}). No crash trigger today.")
                    
        # --- SEND SUCCESS DISCORD REPORT ---
        emoji = "🧪" if dry_run else "🚀"
        
        # Compute portfolio metrics
        portfolio_value = cash + shares_market_value + option_market_value
        total_cost_basis = shares_cost_basis + option_cost_basis
        total_invested = cash + total_cost_basis  # total money put in
        
        shares_pl = shares_market_value - shares_cost_basis
        option_pl = option_market_value - option_cost_basis
        net_pl = shares_pl + option_pl
        
        shares_pl_pct = (shares_pl / shares_cost_basis * 100) if shares_cost_basis else 0
        opt_price_mid = (option_market_value / (puts_held * 100)) if puts_held > 0 else 0.0
        
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
        report.append("**🛡️ PROTECTIVE PUT**")
        report.append(f"  Contract: `{state.get('option_symbol', 'N/A')}`")
        report.append(f"  Strike: **${state.get('active_strike', 0):.1f}** | Expiry: **{state.get('active_expiry', 'N/A')}**")
        report.append(f"  Current Price: **${opt_price_mid:.2f}**")
        report.append(f"  Market Value: ${option_market_value:,.2f}")
        report.append(f"  Premium Paid: ${state.get('initial_premium_paid', 0):.2f}")
        opt_pl_str = f"**${option_pl:+,.2f}**"
        if option_cost_basis:
            opt_pl_str += f" ({option_pl/option_cost_basis*100:+.2f}%)"
        report.append(f"  Option P&L: {opt_pl_str}")
        
        # Intrinsic value & hedge check
        intrinsic = max(0.0, state.get('active_strike', 0) - spot)
        report.append(f"  Intrinsic Value: ${intrinsic:.2f}")
        if state.get('initial_premium_paid', 0) > 0 and intrinsic > 0:
            mult = intrinsic / state['initial_premium_paid']
            report.append(f"  Crash Multiplier: **{mult:.2f}x** / 3.0x threshold")
        report.append("")
        
        # --- Net P&L ---
        port_pl_pct = (net_pl / total_invested * 100) if total_invested else 0
        report.append("**💵 NET PERFORMANCE**")
        report.append(f"  Total Invested: ${total_invested:,.2f}")
        report.append(f"  **Net P&L: ${net_pl:+,.2f}** ({port_pl_pct:+.2f}%)")
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
