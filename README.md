# AXL Airdrop Bot — Setup

1. Create a bot with @BotFather and copy the token.
2. Create two channels (News + Payments) and add your bot as ADMIN in both.
3. Install and run:

    pip install -r requirements.txt
    export BOT_TOKEN="123456:ABC..."
    export ADMIN_ID="your_numeric_telegram_id"   # get it from @userinfobot
    export CHANNEL_1="@your_news_channel"
    export CHANNEL_2="@your_payments_channel"
    export TOKEN_PRICE_USD="0.05"                # optional, for the ~$ display
    export MIN_WITHDRAW="1000"                   # optional
    python bot.py

Admin: /stats, /broadcast <message>. Withdrawals arrive with Approve/Reject buttons.
