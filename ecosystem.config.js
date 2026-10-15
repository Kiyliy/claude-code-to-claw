// PM2 config. Secrets are read from the environment (e.g. a .env loaded by your shell
// or `pm2 start ecosystem.config.js --update-env`); never commit real values here.
module.exports = {
  apps: [
    {
      name: "claw-telegram",
      script: "bot.py",
      interpreter: "python3",
      cwd: __dirname,
      env: {
        TELEGRAM_BOT_TOKEN: process.env.TELEGRAM_BOT_TOKEN,
      },
      max_restarts: 10,
      restart_delay: 5000,
      autorestart: true,
    },
    {
      name: "claw-feishu",
      script: "bot_feishu.py",
      interpreter: "python3",
      cwd: __dirname,
      env: {
        FEISHU_APP_ID: process.env.FEISHU_APP_ID,
        FEISHU_APP_SECRET: process.env.FEISHU_APP_SECRET,
      },
      max_restarts: 10,
      restart_delay: 5000,
      autorestart: true,
    },
  ],
};
