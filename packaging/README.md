# Running Winnow as a systemd user service

Winnow is a long-lived local process: `winnow serve` runs the web app and a
background loop that ticks every five minutes and runs ingest when the last
successful ingest is older than six hours.

## Install

Install the CLI so `winnow` is on `~/.local/bin` (for example `uv tool install
.` or `pip install --user .`), initialize the database, and connect a Google
account in the data directory the unit uses:

```sh
mkdir -p ~/.local/share/winnow
cd ~/.local/share/winnow
winnow init
winnow connect
```

Then install and enable the unit:

```sh
make install-service
```

This copies `packaging/winnow.service` to `~/.config/systemd/user/`, enables it
against `default.target` so it starts at login, and starts it now. The unit
restarts on failure.

## Logs

```sh
journalctl --user -u winnow -f
```

## Uninstall

```sh
make uninstall-service
```
