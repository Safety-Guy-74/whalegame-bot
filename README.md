# Whale Game bot: setup guide

The bot runs by itself every hour on GitHub (free). It posts the live leaderboard to Telegram, splits new fees into four pots, and pays the Whale of the Day automatically after the 25-hour hold. Weekly and monthly crowns wait for you to tap **Approve**.

It starts in **dry-run mode**: it posts what it *would* pay but sends nothing. Leave it like that for the first 3 days and check its decisions.

Allow about 30 minutes. Parts 1 to 3 can be done before launch. Part 4 needs the contract addresses.

---

## Part 1: The Whale Game wallet (10 min)

This wallet receives the 25% team fee and pays the crowns. Use it for nothing else.

1. In MetaMask or Rabby, choose **Add account → Create new account** and name it **Whale Game**.
2. Add the Robinhood Chain network:
   - Network name: `Robinhood Chain`
   - RPC URL: `https://rpc.mainnet.chain.robinhood.com`
   - Chain ID: `4663`
   - Currency: `ETH`
   - Explorer: `https://robinscan.io`
3. Send about **0.01 ETH** to it. That's the gas float. The bot never counts it as prize money.
4. Copy the wallet address. It's the **team wallet** when you create the Mosh bundle (team fee **25%**).
5. Export its private key (Account details → Show private key). You'll paste it into GitHub in Part 3.
   - Only ever paste it into GitHub Secrets. Never into chat, email or a file.

## Part 2: Telegram channel and bot (5 min)

1. In Telegram, choose **New Channel**, name it **Whale Game Crowns**, make it Public, and give it a link such as `whalegamecrowns`.
2. Open **@BotFather**, send `/newbot`, and follow the prompts. It gives you a **token** like `123456:ABC...`. Keep it.
3. Open your channel, then **Administrators → Add admin**, search your bot's name, and allow **Post messages**.
4. Your chat ID is the channel link with an @ in front, e.g. `@whalegamecrowns`.

## Part 3: GitHub (15 min, easiest on the work laptop)

1. Sign up at **github.com** (free).
2. Click **+ → New repository**.
   - Name: `whalegame-bot`
   - **Public**, so anyone can check the bot's code and decisions. Secrets stay hidden either way.
   - Tick **Add a README**, then click **Create**.
3. Upload the files: click **Add file → Upload files**, drag in `bot.py`, `config.json`, `state.json`, `requirements.txt` and `README.md` from the `whalegame_bot` folder on your Desktop, then click **Commit changes**.
4. Add the schedule file. GitHub needs it in a special folder:
   - Click **Add file → Create new file**.
   - Type the name exactly: `.github/workflows/whalegame.yml`. The slashes create the folders.
   - Open `workflow-whalegame.yml` from the Desktop folder in Notepad, copy everything, and paste it into the big box.
   - Click **Commit changes**.
5. Add the three secrets: **Settings → Secrets and variables → Actions → New repository secret**.

   | Name | Value |
   |---|---|
   | `BOT_PRIVATE_KEY` | the Whale Game wallet private key |
   | `TELEGRAM_BOT_TOKEN` | the BotFather token |
   | `TELEGRAM_CHAT_ID` | `@whalegamecrowns` (your channel) |

6. Let the bot save its progress: **Settings → Actions → General → Workflow permissions → Read and write permissions → Save**.
7. Test it: **Actions** tab → **Whale Game bot** → **Run workflow** → action `status` → **Run**. Until Part 4 is done, the log says it's waiting for launch addresses. That's expected.

## Part 4: After launch (Claude fills this in with you)

Open `config.json` in GitHub, click the pencil to edit, and replace every `0xPASTE...` value:

- `token`: the $WHALEGAME contract address.
- `markets`: the Uniswap V4 Pool Manager (already filled in) plus the Pons curve or locker address for your token.
- `excluded_wallets`: your personal wallet(s), every agent vault, and the wallets that run the agents.
- `launch_date_utc`: the launch date. The first crown covers the first full day after it.
- `bundle_contract`: your bundle's contract address from its Mosh page. The bot calls `collectFees()` on it once a day as the team wallet, so fees flow into the pots automatically.
- `burn_executor_wallet`: the wallet you use to do the buy-and-burn, until that's automated.

Commit the change. The bot picks it up on its next hourly run.

## Part 5: Everyday use (from your phone)

Use the GitHub mobile app, or github.com in your phone browser.

- **Hourly:** the leaderboard posts to Telegram automatically.
- **Daily crown:** paid automatically at about 9pm AEST (10pm AEDT) the next evening, and posted with the transaction link.
- **Weekly and monthly crowns:** the bot posts the winner. To pay, go to **Actions → Whale Game bot → Run workflow**, choose **approve**, then **Run**.
- **Buy-and-burn:** choose **release_burn**. The burn pot goes to your buyback wallet; you buy $WHALEGAME and send it to `0x000000000000000000000000000000000000dEaD`.
- **Collect fees now:** choose **collect**. It normally happens automatically once a day.
- **Check the pots:** choose **status**.
- **Pause everything:** edit `config.json`, set `"paused": true`, and commit.
- **The bot paused itself:** it posts the reason to Telegram. Check the wallet, then run **resume**.

## Going live

After 3 days of dry-run posts that look right, edit `config.json`, set `"dry_run": false`, and commit.

## Safety built in

- It only ever uses the Whale Game wallet. Your main wallets are never involved.
- It never sends more than `max_payout_eth` (0.5 ETH) in one payment.
- It pauses itself if the wallet sends anything it didn't expect, or if a payment fails.
- It never counts more prize money than the wallet actually holds.
- Every decision is saved in `state.json` in the repository, and posted to Telegram.

## Rules the bot applies

- **Score:** tokens bought minus tokens sold during the period, credited to the wallet that signed each trade.
- **Minimum:** US$100 worth of net buy.
- **Hold:** for 25 hours after the period closes, the winner's balance must never drop below their net buy. If it does, the next wallet on the board is checked.
- **Periods:**

  | Period | Closes | Paid |
  |---|---|---|
  | Daily | 10:00 UTC (8pm AEST) | 25 hours later |
  | Weekly | Sunday 10:00 UTC | Monday, 25 hours later |
  | Monthly | 1st of the month, 10:00 UTC | The 2nd |

- **No winner:** if nobody qualifies, the pot rolls over.
