#!/usr/bin/env python3
"""
Whale Game bot - runs every hour on GitHub Actions.

What it does each run:
  1. Safety checks (paused? wallet nonce as expected?).
  2. Splits any new ETH that arrived in the Whale Game wallet into four pots:
     burn / daily / weekly / monthly (25% each by default).
  3. Posts the live leaderboard to Telegram (hourly).
  4. Settles finished periods once their 25-hour hold has passed:
       - ranks wallets by NET ACCUMULATION (tokens bought minus tokens sold,
         credited to the wallet that signed each trade)
       - checks each candidate held at least their net buy for the whole hold
       - pays the first wallet that passes (or queues it for your approval)
  5. Saves everything to state.json (committed back to GitHub by the workflow).

Manual actions (GitHub -> Actions -> Whale Game bot -> Run workflow):
  approve       pay every queued weekly/monthly crown
  release_burn  send the burn pot to burn_executor_wallet for the buyback
  status        post the pot balances
  resume        clear an automatic safety pause

Secrets it needs (GitHub -> Settings -> Secrets -> Actions):
  BOT_PRIVATE_KEY     private key of the Whale Game wallet (a NEW wallet, only for this)
  TELEGRAM_BOT_TOKEN  from @BotFather
  TELEGRAM_CHAT_ID    your channel, e.g. @whalegamecrowns
"""

import json
import os
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "config.json")
STATE_PATH = os.path.join(HERE, "state.json")
WEI = 10 ** 18
ALWAYS_EXCLUDED = {
    "0x0000000000000000000000000000000000000000",
    "0x000000000000000000000000000000000000dead",
}
ERC20_ABI = [{"constant": True, "inputs": [{"name": "a", "type": "address"}], "name": "balanceOf",
              "outputs": [{"name": "", "type": "uint256"}], "type": "function"}]

CFG = {}
STATE = {}
LOG = []


# ------------------------------------------------------------------ helpers
def log(msg):
    line = f"[{datetime.now(timezone.utc):%Y-%m-%d %H:%M:%S}Z] {msg}"
    print(line, flush=True)
    LOG.append(line)


def load():
    global CFG, STATE
    with open(CONFIG_PATH) as f:
        CFG = json.load(f)
    with open(STATE_PATH) as f:
        STATE = json.load(f) or {}
    STATE.setdefault("pots_wei", {k: 0 for k in CFG["pot_split"]})
    STATE.setdefault("settled", {})          # period id -> result
    STATE.setdefault("pending", [])          # queued manual payouts
    STATE.setdefault("history", [])          # every payout ever made
    STATE.setdefault("signers", {})          # tx hash -> signer cache


def save():
    # keep the signer cache from growing forever
    if len(STATE.get("signers", {})) > 20000:
        STATE["signers"] = dict(list(STATE["signers"].items())[-10000:])
    with open(STATE_PATH, "w") as f:
        json.dump(STATE, f, indent=1, sort_keys=True)


def lower_set(xs):
    return {x.lower() for x in xs if x and not x.startswith("0xPASTE")}


def fmt_eth(wei):
    return f"{wei / WEI:.4f} ETH"


def fmt_tok(raw, decimals=18):
    v = raw / 10 ** decimals
    if abs(v) >= 1e6:
        return f"{v / 1e6:.2f}M"
    if abs(v) >= 1e3:
        return f"{v / 1e3:.1f}K"
    return f"{v:.0f}"


def short(a):
    return f"{a[:6]}…{a[-4:]}"


def parse_ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


# ------------------------------------------------------------------ telegram
def tg(text):
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    prefix = "🧪 DRY RUN\n" if CFG.get("dry_run") else ""
    if not token or not chat:
        log("Telegram not configured; message was:\n" + prefix + text)
        return
    try:
        r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                          json={"chat_id": chat, "text": prefix + text, "parse_mode": "HTML",
                                "disable_web_page_preview": True}, timeout=30)
        if not r.ok:
            log(f"Telegram error {r.status_code}: {r.text[:200]}")
    except Exception as e:  # never let Telegram break a payout run
        log(f"Telegram failed: {e}")


# ------------------------------------------------------------------ explorer api
def api(path):
    url = CFG["robinscan_api"] + path
    for attempt in range(6):
        try:
            r = requests.get(url, timeout=30, headers={"User-Agent": "whalegame-bot"})
            if r.status_code == 200:
                return r.json()
            if r.status_code == 404:
                return None
        except Exception:
            pass
        time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"Explorer API not reachable: {url}")


def signer(tx_hash):
    cache = STATE["signers"]
    if tx_hash not in cache:
        tx = api(f"/txs/{tx_hash}")
        cache[tx_hash] = (tx or {}).get("from", "").lower()
    return cache[tx_hash]


def token_transfers(start, end):
    """All transfers of the token between start and end (newest first from the API)."""
    out, page = [], 1
    while page <= 400:
        data = api(f"/tokens/{CFG['token']}/transfers?page={page}&pageSize=50") or {}
        items = data.get("items", [])
        if not items:
            break
        oldest = None
        for t in items:
            ts = parse_ts(t["timestamp"])
            oldest = ts
            if start <= ts < end:
                out.append(t)
        if oldest is not None and oldest < start:
            break
        page += 1
        time.sleep(0.15)
    return out


def wallet_token_transfers_since(wallet, start):
    token = CFG["token"].lower()
    out, page = [], 1
    while page <= 100:
        data = api(f"/addresses/{wallet}/transfers?page={page}&pageSize=50") or {}
        items = data.get("items", [])
        if not items:
            break
        oldest = None
        for t in items:
            ts = parse_ts(t["timestamp"])
            oldest = ts
            if ts >= start and t["token"].lower() == token:
                out.append(t)
        if oldest is not None and oldest < start:
            break
        page += 1
        time.sleep(0.15)
    return out  # newest first


def token_info():
    return api(f"/tokens/{CFG['token']}") or {}


# ------------------------------------------------------------------ chain (web3)
_w3 = None


def w3():
    global _w3
    if _w3 is None:
        from web3 import Web3
        _w3 = Web3(Web3.HTTPProvider(CFG["rpc_url"], request_kwargs={"timeout": 60}))
    return _w3


def bot_account():
    key = os.environ.get("BOT_PRIVATE_KEY")
    if not key:
        return None
    return w3().eth.account.from_key(key)


def token_balance(wallet):
    from web3 import Web3
    c = w3().eth.contract(address=Web3.to_checksum_address(CFG["token"]), abi=ERC20_ABI)
    return c.functions.balanceOf(Web3.to_checksum_address(wallet)).call()


def is_contract(addr):
    from web3 import Web3
    return len(w3().eth.get_code(Web3.to_checksum_address(addr))) > 0


def send_eth(to, amount_wei, reason):
    """Send ETH from the bot wallet. Returns tx hash (or 'DRY-RUN')."""
    from web3 import Web3
    if amount_wei <= 0:
        raise ValueError("Nothing to send")
    cap = int(CFG["max_payout_eth"] * WEI)
    if amount_wei > cap:
        raise ValueError(f"{fmt_eth(amount_wei)} is above the safety cap of {fmt_eth(cap)}")
    if CFG.get("dry_run"):
        log(f"DRY RUN: would send {fmt_eth(amount_wei)} to {to} ({reason})")
        return "DRY-RUN"
    acct = bot_account()
    if acct is None:
        raise RuntimeError("BOT_PRIVATE_KEY secret is missing")
    e = w3().eth
    nonce = e.get_transaction_count(acct.address)
    tx = {"from": acct.address, "to": Web3.to_checksum_address(to), "value": int(amount_wei),
          "nonce": nonce, "chainId": int(CFG["chain_id"])}
    tx["gas"] = int(e.estimate_gas(tx) * 1.3)
    base = e.gas_price
    tx["maxFeePerGas"] = int(base * 2)
    tx["maxPriorityFeePerGas"] = 0
    signed = acct.sign_transaction(tx)
    raw = getattr(signed, "raw_transaction", None) or getattr(signed, "rawTransaction")
    h = e.send_raw_transaction(raw).hex()
    if not h.startswith("0x"):
        h = "0x" + h
    STATE["expected_nonce"] = nonce + 1
    save()  # record immediately
    rcpt = e.wait_for_transaction_receipt(h, timeout=180)
    if rcpt.status != 1:
        raise RuntimeError(f"Transaction failed on-chain: {h}")
    gas_cost = rcpt.gasUsed * rcpt.effectiveGasPrice
    STATE["pots_wei"]["daily"] = max(0, STATE["pots_wei"]["daily"] - gas_cost)  # gas comes out of the daily pot
    log(f"Sent {fmt_eth(amount_wei)} to {to}: {h}")
    return h


# ------------------------------------------------------------------ periods
def close_time(day):
    hh, mm = map(int, CFG["day_close_utc"].split(":"))
    return datetime(day.year, day.month, day.day, hh, mm, tzinfo=timezone.utc)


def launch_close():
    return close_time(datetime.strptime(CFG["launch_date_utc"], "%Y-%m-%d"))


def recent_periods(now):
    """(kind, id, start, end) for periods whose window has closed, newest first, last ~40 days."""
    out = []
    first = launch_close()
    last_close = close_time(now) if now >= close_time(now) else close_time(now) - timedelta(days=1)
    for i in range(0, 40):
        end = last_close - timedelta(days=i)
        start = end - timedelta(days=1)
        if end <= first:
            break
        out.append(("daily", end.strftime("D%Y-%m-%d"), max(start, first - timedelta(days=1)), end))
        if end.weekday() == 6:  # Sunday close = 8pm AEST Sunday
            out.append(("weekly", end.strftime("W%Y-%m-%d"), max(end - timedelta(days=7), first - timedelta(days=1)), end))
        if end.day == 1:  # 1st of the month close
            prev = (end - timedelta(days=1)).replace(day=1)
            out.append(("monthly", prev.strftime("M%Y-%m"), max(close_time(prev), first - timedelta(days=1)), end))
    return out


# ------------------------------------------------------------------ scoring
def net_accumulation(start, end):
    """Return {wallet: {"net": raw tokens, "bought": raw, "sold": raw, "trades": n}}."""
    markets = lower_set(CFG["markets"])
    per_tx = defaultdict(int)
    for t in token_transfers(start, end):
        v = int(t["value"])
        frm, to = t["from"].lower(), t["to"].lower()
        if frm in markets and to not in markets:
            per_tx[t["txHash"]] += v      # tokens came OUT of the market -> buy
        elif to in markets and frm not in markets:
            per_tx[t["txHash"]] -= v      # tokens went INTO the market -> sell
    board = defaultdict(lambda: {"net": 0, "bought": 0, "sold": 0, "trades": 0})
    for h, delta in per_tx.items():
        if delta == 0:
            continue
        w = signer(h)
        if not w:
            continue
        b = board[w]
        b["net"] += delta
        b["trades"] += 1
        if delta > 0:
            b["bought"] += delta
        else:
            b["sold"] += -delta
    excluded = lower_set(CFG["excluded_wallets"]) | markets | ALWAYS_EXCLUDED
    try:
        acct = bot_account()
        if acct:
            excluded.add(acct.address.lower())
    except Exception:
        pass
    return {w: v for w, v in board.items() if w not in excluded}


def board_for(start, end):
    """Net accumulation for a window. Whole days are cached, and weeks/months are the sum of their days."""
    boards = STATE.setdefault("boards", {})
    span = end - start
    if span <= timedelta(days=1):
        key = end.strftime("%Y-%m-%dT%H")
        if key not in boards or span < timedelta(days=1):
            b = {w: v["net"] for w, v in net_accumulation(start, end).items()}
            if span == timedelta(days=1):
                boards[key] = {w: str(n) for w, n in b.items()}
            return b
        return {w: int(n) for w, n in boards[key].items()}
    total = defaultdict(int)
    cur = end
    while cur > start:
        day_start = max(start, cur - timedelta(days=1))
        for w, n in board_for(day_start, cur).items():
            total[w] += n
        cur = day_start
    # drop cached days older than 45 days
    cutoff = (end - timedelta(days=45)).strftime("%Y-%m-%dT%H")
    for k in [k for k in boards if k < cutoff]:
        del boards[k]
    return dict(total)


def min_tokens_to_qualify(info):
    price = info.get("priceUsd")
    dec = int(info.get("decimals") or 18)
    if price:
        return int(CFG["min_net_buy_usd"] / float(price) * 10 ** dec)
    return int(CFG.get("min_net_buy_tokens_if_no_price", 0) * 10 ** dec)


def held_through(wallet, required, since):
    """True if the wallet's balance never dropped below `required` from `since` until now."""
    bal = token_balance(wallet)
    lowest = bal
    for t in wallet_token_transfers_since(wallet, since):   # newest first: walk back in time
        v = int(t["value"])
        if t["to"].lower() == wallet:
            bal -= v
        if t["from"].lower() == wallet:
            bal += v
        lowest = min(lowest, bal)
    return lowest >= required, lowest


def pick_winner(kind, start, end, info):
    board = {w: {"net": n} for w, n in board_for(start, end).items()}
    ranked = sorted(board.items(), key=lambda x: -x[1]["net"])
    need = min_tokens_to_qualify(info)
    checked = []
    for w, v in ranked[: CFG["top_candidates"]]:
        if v["net"] <= 0 or v["net"] < need:
            break
        if is_contract(w):
            checked.append({"wallet": w, "result": "contract, skipped"})
            continue
        ok, lowest = held_through(w, v["net"], end)
        checked.append({"wallet": w, "net": str(v["net"]), "lowest_balance": str(lowest), "held": ok})
        if ok:
            return w, v, ranked, checked
    return None, None, ranked, checked


# ------------------------------------------------------------------ pots
def collect_fees_if_due(force=False):
    """Call collectFees() on the Mosh bundle contract from the team wallet (once a day by default)."""
    from web3 import Web3
    bundle = CFG.get("bundle_contract", "")
    if not CFG.get("auto_collect_fees", True) or not bundle or bundle.startswith("0xPASTE"):
        return
    last = STATE.get("last_collect_utc")
    if not force and last and datetime.now(timezone.utc) - parse_ts(last) < timedelta(hours=CFG.get("collect_every_hours", 24)):
        return
    acct = bot_account()
    if acct is None:
        return
    e = w3().eth
    data = "0x" + Web3.keccak(text="collectFees()").hex().replace("0x", "")[:8]
    tx = {"from": acct.address, "to": Web3.to_checksum_address(bundle), "value": 0, "data": data,
          "chainId": int(CFG["chain_id"])}
    try:
        gas = e.estimate_gas(tx)
    except Exception as ex:
        log(f"collectFees() would fail right now ({ex}); will retry next run")
        return
    if CFG.get("dry_run"):
        log(f"DRY RUN: collectFees() looks OK (gas estimate {gas}); not sent")
        STATE["last_collect_utc"] = datetime.now(timezone.utc).isoformat()
        return
    tx.update({"nonce": e.get_transaction_count(acct.address), "gas": int(gas * 1.3),
               "maxFeePerGas": int(e.gas_price * 2), "maxPriorityFeePerGas": 0})
    signed = acct.sign_transaction(tx)
    raw = getattr(signed, "raw_transaction", None) or getattr(signed, "rawTransaction")
    h = e.send_raw_transaction(raw).hex()
    h = h if h.startswith("0x") else "0x" + h
    STATE["expected_nonce"] = tx["nonce"] + 1
    save()
    rcpt = e.wait_for_transaction_receipt(h, timeout=180)
    if rcpt.status != 1:
        STATE["paused_reason"] = f"collectFees() transaction failed: {h}"
        tg("⚠️ Fee collection failed and the bot paused itself. Tx: " + CFG["explorer_tx_url"] + h)
        return
    STATE["last_collect_utc"] = datetime.now(timezone.utc).isoformat()
    log(f"Collected fees: {h}")


def allocate_new_funds():
    """Split any new ETH in the wallet into the pots."""
    acct = bot_account()
    if acct is None:
        log("No BOT_PRIVATE_KEY yet; skipping pot accounting")
        return
    bal = w3().eth.get_balance(acct.address)
    reserve = int(CFG["gas_reserve_eth"] * WEI)
    pots = STATE["pots_wei"]
    if "known_balance_wei" not in STATE:
        STATE["known_balance_wei"] = bal   # first run: whatever is there is the gas float
        log(f"First run: {fmt_eth(bal)} treated as gas float, not prize money")
        return
    new = bal - STATE["known_balance_wei"]
    if new > 0:
        for k, share in CFG["pot_split"].items():
            pots[k] = pots.get(k, 0) + int(new * share)
        log(f"New funds {fmt_eth(new)} split into pots")
        tg(f"💰 {fmt_eth(new)} of fees arrived and was split into the pots.")
    elif new < 0:
        # unexplained drop (manual withdrawal or gas) - shrink pots proportionally
        total = sum(pots.values())
        if total > 0:
            f = max(0.0, (total + new) / total)
            for k in pots:
                pots[k] = int(pots[k] * f)
        log(f"Balance fell by {fmt_eth(-new)} outside the bot; pots reduced to match")
    # never promise more than the wallet holds
    total = sum(pots.values())
    if total > max(0, bal - reserve):
        f = max(0, bal - reserve) / total if total else 0
        for k in pots:
            pots[k] = int(pots[k] * f)
    STATE["known_balance_wei"] = bal


def refresh_known_balance():
    acct = bot_account()
    if acct is not None and not CFG.get("dry_run"):
        STATE["known_balance_wei"] = w3().eth.get_balance(acct.address)


# ------------------------------------------------------------------ settle
def settle(kind, pid, start, end, info, now):
    if pid in STATE["settled"]:
        return
    if now < end + timedelta(hours=CFG["hold_hours"]):
        return  # hold period still running
    log(f"Settling {pid} ({start:%Y-%m-%d %H:%M} -> {end:%Y-%m-%d %H:%M} UTC)")
    winner, v, ranked, checked = pick_winner(kind, start, end, info)
    label = {"daily": "Whale of the Day", "weekly": "Whale of the Week", "monthly": "Whale of the Month"}[kind]
    pot = STATE["pots_wei"].get(kind, 0)
    dec = int(info.get("decimals") or 18)
    result = {"kind": kind, "start": start.isoformat(), "end": end.isoformat(),
              "checked": checked, "top": [[w, str(x["net"])] for w, x in ranked[:10]]}
    if winner is None:
        result["winner"] = None
        STATE["settled"][pid] = result
        tg(f"🐋 <b>{label}</b> ({pid[1:]})\nNo wallet qualified (minimum ${CFG['min_net_buy_usd']} net buy, held for "
           f"{CFG['hold_hours']}h). The {fmt_eth(pot)} pot rolls over.")
        return
    amount = min(pot, int(CFG["max_payout_eth"] * WEI))
    result.update({"winner": winner, "net": str(v["net"]), "amount_wei": str(amount)})
    top3 = "\n".join(f"{i}. {short(w)}  {fmt_tok(x['net'], dec)}" for i, (w, x) in enumerate(ranked[:3], 1))
    auto = CFG.get(f"auto_pay_{kind}", False)
    if amount <= 0:
        result["status"] = "no funds"
        STATE["settled"][pid] = result
        tg(f"🐋 <b>{label}</b> ({pid[1:]})\nWinner: <code>{winner}</code>\nNet accumulation: {fmt_tok(v['net'], dec)}\n"
           f"The pot is empty, so nothing to pay yet.\n\nTop 3:\n{top3}")
        return
    if not auto:
        result["status"] = "awaiting approval"
        STATE["pending"].append({"pid": pid, "kind": kind, "wallet": winner, "amount_wei": str(amount)})
        STATE["settled"][pid] = result
        tg(f"🐋 <b>{label}</b> ({pid[1:]})\nWinner: <code>{winner}</code>\nNet accumulation: {fmt_tok(v['net'], dec)}, "
           f"held for {CFG['hold_hours']}h ✅\nPrize: {fmt_eth(amount)} (payment pending)\n\nTop 3:\n{top3}")
        return
    try:
        h = send_eth(winner, amount, f"{label} {pid}")
    except Exception as e:
        result["status"] = f"payment failed: {e}"
        STATE["settled"][pid] = result
        STATE["paused_reason"] = f"Payment for {pid} failed: {e}"
        tg(f"⚠️ Payment for {label} ({pid[1:]}) failed and the bot has paused itself. Reason: {e}")
        return
    if h != "DRY-RUN":
        STATE["pots_wei"][kind] -= amount
        refresh_known_balance()
    result.update({"status": "paid", "tx": h})
    STATE["settled"][pid] = result
    STATE["history"].append({"pid": pid, "wallet": winner, "amount_wei": str(amount), "tx": h})
    link = h if h == "DRY-RUN" else f'<a href="{CFG["explorer_tx_url"]}{h}">{h[:10]}…</a>'
    tg(f"👑 <b>{label}</b> ({pid[1:]})\nWinner: <code>{winner}</code>\nNet accumulation: {fmt_tok(v['net'], dec)}, "
       f"held for {CFG['hold_hours']}h ✅\nPrize: {fmt_eth(amount)}\nTx: {link}\n\nTop 3:\n{top3}")


def approve_pending():
    still = []
    for p in STATE["pending"]:
        try:
            h = send_eth(p["wallet"], int(p["amount_wei"]), f"approved {p['pid']}")
            if h != "DRY-RUN":
                STATE["pots_wei"][p["kind"]] -= int(p["amount_wei"])
                refresh_known_balance()
                STATE["history"].append({**p, "tx": h})
                STATE["settled"][p["pid"]].update({"status": "paid", "tx": h})
                tg(f"👑 Paid {p['kind']} crown {p['pid'][1:]}: {fmt_eth(int(p['amount_wei']))} to <code>{p['wallet']}</code>\n"
                   f'Tx: <a href="{CFG["explorer_tx_url"]}{h}">{h[:10]}…</a>')
            else:
                still.append(p)
        except Exception as e:
            tg(f"⚠️ Approved payout {p['pid']} failed: {e}")
            still.append(p)
    STATE["pending"] = still


def release_burn():
    to = CFG.get("burn_executor_wallet", "")
    amt = STATE["pots_wei"].get("burn", 0)
    if not to or to.startswith("0xPASTE") or amt <= 0:
        tg("Burn pot not released: set burn_executor_wallet in config.json and make sure the pot has funds.")
        return
    h = send_eth(to, min(amt, int(CFG["max_payout_eth"] * WEI)), "burn pot to buyback wallet")
    if h != "DRY-RUN":
        STATE["pots_wei"]["burn"] -= min(amt, int(CFG["max_payout_eth"] * WEI))
        refresh_known_balance()
        tg(f"🔥 Burn pot {fmt_eth(amt)} sent to the buyback wallet for the next buy-and-burn.\n"
           f'Tx: <a href="{CFG["explorer_tx_url"]}{h}">{h[:10]}…</a>')


def post_status():
    p = STATE["pots_wei"]
    lines = "\n".join(f"• {k.title()}: {fmt_eth(v)}" for k, v in p.items())
    pend = len(STATE["pending"])
    tg(f"📊 <b>Whale Game pots</b>\n{lines}\nPending approvals: {pend}")


def post_leaderboard(now, info):
    last = STATE.get("last_leaderboard_hour")
    hour_key = now.strftime("%Y-%m-%d %H")
    if last == hour_key:
        return
    start = close_time(now) if now >= close_time(now) else close_time(now) - timedelta(days=1)
    board = {w: {"net": n} for w, n in board_for(start, now).items()}
    ranked = sorted(board.items(), key=lambda x: -x[1]["net"])[:5]
    dec = int(info.get("decimals") or 18)
    left = start + timedelta(days=1) - now
    hrs, mins = int(left.total_seconds() // 3600), int(left.total_seconds() % 3600 // 60)
    if ranked:
        rows = "\n".join(f"{i}. {short(w)}  {fmt_tok(v['net'], dec)}" for i, (w, v) in enumerate(ranked, 1))
    else:
        rows = "No buys yet today. The crown is up for grabs."
    tg(f"🐋 <b>Whale race - live</b>\n{rows}\n\nDaily pot: {fmt_eth(STATE['pots_wei'].get('daily', 0))}\n"
       f"Day closes in {hrs}h {mins}m (8pm AEST). Winner must hold {CFG['hold_hours']}h.")
    STATE["last_leaderboard_hour"] = hour_key


# ------------------------------------------------------------------ main
def safety_ok():
    if CFG.get("paused"):
        log("Paused in config.json")
        return False
    if STATE.get("paused_reason"):
        log(f"Auto-paused: {STATE['paused_reason']} (run the 'resume' action after checking)")
        return False
    acct = bot_account()
    if acct is not None and not CFG.get("dry_run") and "expected_nonce" in STATE:
        n = w3().eth.get_transaction_count(acct.address)
        if n != STATE["expected_nonce"]:
            STATE["paused_reason"] = (f"Wallet nonce is {n} but the bot expected {STATE['expected_nonce']}. "
                                      "A transaction was sent outside the bot or a run crashed mid-payment.")
            tg("⚠️ Whale Game bot paused itself: " + STATE["paused_reason"] + " Check the wallet, then run 'resume'.")
            return False
    return True


def main():
    action = (sys.argv[1] if len(sys.argv) > 1 else os.environ.get("BOT_ACTION") or "run").strip().lower()
    load()
    if any(str(CFG.get(k, "")).startswith("0xPASTE") for k in ("token",)):
        log("config.json still has placeholder addresses. Nothing to do until launch.")
        save()
        return
    try:
        if action == "resume":
            STATE.pop("paused_reason", None)
            acct = bot_account()
            if acct is not None:
                STATE["expected_nonce"] = w3().eth.get_transaction_count(acct.address)
                STATE["known_balance_wei"] = w3().eth.get_balance(acct.address)
            tg("✅ Whale Game bot resumed.")
            return
        if not safety_ok():
            return
        if action in ("run", "collect"):
            collect_fees_if_due(force=(action == "collect"))
            if STATE.get("paused_reason"):
                return
        allocate_new_funds()
        if action == "collect":
            post_status()
            return
        if action == "approve":
            approve_pending()
            return
        if action == "release_burn":
            release_burn()
            return
        if action == "status":
            post_status()
            return
        now = datetime.now(timezone.utc)
        info = token_info()
        # oldest first so pots are paid in order
        order = {"daily": 0, "weekly": 1, "monthly": 2}
        for kind, pid, start, end in sorted(recent_periods(now), key=lambda x: (x[3], order[x[0]])):
            settle(kind, pid, start, end, info, now)
            save()
            if STATE.get("paused_reason"):
                break
        if CFG.get("post_hourly_leaderboard", True):
            post_leaderboard(now, info)
    finally:
        STATE["last_run_utc"] = datetime.now(timezone.utc).isoformat()
        save()


if __name__ == "__main__":
    main()
