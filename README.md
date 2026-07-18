# hakchi-sync

Pulls SNES cartridge save files (`cartridge.sram`) off a hakchi2-ce SNES Mini
over SSH and uploads them to [RomM](https://docs.romm.app/) so they're backed
up and playable from RomM's browser emulator.

Save states are intentionally out of scope: hakchi's `snes9x2010` core and
RomM/EmulatorJS's `snes9x`/`bsnes` cores use incompatible internal state
formats, so a state produced on the device can't be replayed there.

## Setup

1. `pip install -r requirements.txt`
2. `cp .env.example .env` and set `ROMM_API_TOKEN` - generate it under RomM's
   Settings > API tokens (a `rmm_...` client token, not your login password).
   `.env` is gitignored; never put the token in config.yaml.
3. `cp config.example.yaml config.yaml` and fill in:
   - `romm.base_url`
   - `hakchi.host` - however you already reach the device (`ssh root@hakchi`
     working means this will too).
   - `games` - one entry per game: the `CLV-*` folder name from
     `ssh root@hakchi ls /var/lib/clover/profiles/0`, and the `rom_id` from
     that game's RomM URL (`.../rom/<id>`).
4. Confirm each `rom_id` actually points at the game you think it does:

   ```
   python -m hakchi_sync --verify-only
   ```

   This only calls RomM's API (no SSH needed) and prints the RomM name/platform
   next to each configured mapping - check the printed name before trusting it.

5. Dry run against the real device (reads saves over SSH, does not upload):

   ```
   python -m hakchi_sync --dry-run
   ```

6. Run for real:

   ```
   python -m hakchi_sync
   ```

Use `--game CLV-U-NRHVN` on any command to limit it to one game while testing.

## Retention

Uploads go into a dedicated RomM save slot (`slot` in config, default
`auto-sync`) with autocleanup on, keeping the last `autocleanup_limit` saves
in that slot. RomM also skips uploading when the save is byte-identical to
what's already in the slot, so an unplayed game doesn't churn out duplicates.

## Docker / scheduling

The container runs one sync pass and exits - point cron, a file-watcher, or
whatever scheduler you're already using at `docker run`:

```
docker build -t hakchi-sync .
docker run --rm \
  --env-file .env \
  -v $PWD/config.yaml:/config/config.yaml:ro \
  -v ~/.ssh/id_rsa:/root/.ssh/id_rsa:ro \
  hakchi-sync
```

Example cron entry (host crontab, runs every night at 3am):

```
0 3 * * * docker run --rm --env-file /path/to/.env -v /path/to/config.yaml:/config/config.yaml:ro -v /path/to/id_rsa:/root/.ssh/id_rsa:ro hakchi-sync
```

Exit code is `0` on success, `1` if any game failed to upload, `2` on a
config error, `3` if the hakchi device couldn't be reached.
