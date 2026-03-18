module.exports = {
  apps: [
    {
      name: "claw-telegram",
      script: "bot.py",
      interpreter: "python3",
      cwd: "/root/kimi_dev/claude-code-to-claw",
      env: {
        TELEGRAM_BOT_TOKEN: "8762099138:AAFw11rUOBNY8zZdn2QWHAJ-szrfn2INxYI",
      },
      max_restarts: 10,
      restart_delay: 5000,
      autorestart: true,
    },
    {
      name: "claw-feishu",
      script: "bot_feishu.py",
      interpreter: "python3",
      cwd: "/root/kimi_dev/claude-code-to-claw",
      env: {
        FEISHU_APP_ID: "cli_a9338154fc38dbc6",
        FEISHU_APP_SECRET: "7eVe50nbx3IWznerfyKRBbrgPpWl1QV3",
      },
      max_restarts: 10,
      restart_delay: 5000,
      autorestart: true,
    },
  ],
};
