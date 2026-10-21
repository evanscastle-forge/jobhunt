# Running jobhunt unattended

`python jobhunt.py schedule --write` generates the two launchd agents in this folder from the
current paths. They hold absolute paths to your machine, so they are gitignored; run the command on
each machine. Nothing is installed by it. To enable them:

```bash
cp deploy/local.jobhunt-serve.plist ~/Library/LaunchAgents/
cp deploy/local.jobhunt-scan.plist  ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/local.jobhunt-serve.plist
launchctl load ~/Library/LaunchAgents/local.jobhunt-scan.plist
```

* `jobhunt-serve` keeps the dashboard up at http://127.0.0.1:8765/ (localhost only; use remote
  desktop or `ssh -L 8765:127.0.0.1:8765 <mac>` from another machine). Log: `data/serve.log`.
* `jobhunt-scan` runs one scan a day at 02:00 by the Mac's clock (keep the Mac on Eastern time;
  `--hour/--minute` to change). The scan includes the LLM fit pass and may take up to 30 minutes.
  Log: `data/scan.log`. The dashboard picks up the results on the next refresh.

If the Mac is asleep at 02:00, launchd runs the scan when it wakes. Both agents carry a PATH that
includes `~/.local/bin`, where the `claude` CLI lives; without it the scan's rating step would get no model.

To disable: `launchctl unload ~/Library/LaunchAgents/local.jobhunt-*.plist`.

Both agents use the project's uv virtualenv (`.venv/bin/python`), so run `uv sync` first.
