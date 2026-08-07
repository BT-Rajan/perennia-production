// pm2 process definition for Perennia's web server.
//
// Used by start.sh (`pm2 startOrReload ecosystem.config.js`) so the app
// runs as a named, supervised process ("web") that pm2 restarts on
// crash and can be told to auto-start on system boot (`pm2 startup` +
// `pm2 save`).
//
// This app is single-instance by design (local JSON storage, no shared
// DB — see README and the startup instance lock in app/main.py), so
// `instances` is deliberately 1, not cluster mode. Do not raise it
// without first moving config/appointments/leads/knowledge-base/stats
// storage to something that's actually safe to share across processes.
module.exports = {
  apps: [
    {
      name: "web",
      script: "scripts/run_server.py",
      interpreter: "venv/bin/python",
      cwd: __dirname,
      instances: 1,
      exec_mode: "fork",
      autorestart: true,
      max_restarts: 10,
      min_uptime: "10s",
      watch: false,
      env: {
        NO_BROWSER: "1",
      },
    },
  ],
};
