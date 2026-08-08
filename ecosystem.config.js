// pm2 process definition for Perennia's web server.
//
// Used by start.sh (`pm2 startOrReload ecosystem.config.js`) so the app
// runs as a named, supervised process ("web") that pm2 restarts on
// crash and can be told to auto-start on system boot (`pm2 startup` +
// `pm2 save`).
//
// This app currently runs as a single PM2 instance as an operational
// choice for this deployment model (one dedicated instance per paid
// tenant) — NOT because of a storage-correctness requirement. Before
// the MySQL storage port (see app/storage.py), local JSON file storage
// made multi-instance genuinely unsafe; that constraint no longer
// applies now that storage is MySQL, which handles concurrent writers
// on its own. Raise `instances` if this deployment model ever needs it.
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
