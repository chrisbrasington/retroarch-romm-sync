# hakchi-sync

Pulls save data for hakchi2-ce games off a SNES Mini over SSH and uploads it
to [RomM](https://docs.romm.app/), so it's backed up and playable from RomM's
browser emulator. Works across every console hakchi2-ce runs (SNES, NES,
Game Boy, etc.) - the save-directory convention is the same regardless of
which core actually emulates the game.

Each configured game syncs two things independently:
- **The battery save** (`cartridge.sram`) - the actual in-game save. This is
  the reliable part; it's core-agnostic and plays back in RomM/EmulatorJS
  fine (confirmed against a real device).
- **The latest suspend-point state** - uploaded too, but likely won't load
  in EmulatorJS for cores where hakchi's version doesn't match RomM's
  (confirmed: hakchi's SNES core is `snes9x2010`, RomM/EmulatorJS only offer
  `snes9x`/`bsnes` - different internal state formats). It's uploaded anyway
  because some games (e.g. original Game Boy carts with no battery save at
  all) have nothing else to back up, and RomM's Game Boy core might happen
  to match. A game with neither a save nor a state is skipped, not an error.

## Setup

1. `pip install -r requirements.txt`
2. `cp .env.example .env` and set `ROMM_API_TOKEN` - generate it under RomM's
   Settings > API tokens (a `rmm_...` client token, not your login password).
   `.env` is gitignored; never put the token in config.yaml.
3. `cp config.example.yaml config.yaml` and fill in `romm.base_url` and
   `hakchi.host` (however you already reach the device - if `ssh root@hakchi`
   works, this will too). Leave `games:` empty; the next step fills it in.
4. Add game mappings interactively:

   ```
   python -m hakchi_sync --setup
   ```

   Walks every hakchi-installed game that has an actual save file on the
   device (across every console, not just SNES), one at a time. For each, it
   searches RomM by the hakchi game's own title and shows candidate matches
   to pick by number:

   ```
   Zelda II - The Adventure of Link  (CLV-H-RPCQP)
     RomM matches for 'Zelda II - The Adventure of Link':
       1) Zelda II: The Adventure of Link (Nintendo Entertainment System) [rom 12]
     pick a number, paste a rom URL/ID, type a new search term, blank to skip, 'q' to stop: 1
     added CLV-H-RPCQP -> rom 12 (Zelda II: The Adventure of Link)
   ```

   If nothing matches (or the wrong things match), type a different search
   term to search again, or paste a rom URL (`.../rom/93`) / bare rom ID
   (`93`) directly - that gets looked up and shown for confirmation before
   it's saved. Blank skips a game, `q` stops the whole wizard. Add
   `--all-roms` to also be offered games with no save file yet (e.g. to
   pre-map something you haven't played yet).

   Games already in `config.yaml` are skipped, so it's safe to re-run
   `--setup` later as you add more games to the device.

5. Confirm each `rom_id` actually points at the game you think it does
   (`--setup` already does this per-game, but this re-checks everything):

   ```
   python -m hakchi_sync --verify-only
   ```

6. Dry run against the real device (reads saves over SSH, does not upload):

   ```
   python -m hakchi_sync --dry-run
   ```

7. Run for real:

   ```
   python -m hakchi_sync
   ```

Use `--game CLV-U-NRHVN` on any command to limit it to one game while testing.

## Pushing a save/state back to the device

The sync direction above is one-way (device -> RomM). To go the other way -
push whatever's in RomM back down to the SNES Mini, e.g. to restore an older
save or move progress between devices - use the interactive push tool
instead:

```
python -m hakchi_sync.push
```

It lists your mapped games, and for whichever one you pick, lets you push
its save, its state, or both. Each one shows you exactly what it's about to
overwrite (RomM's filename and timestamp) and requires a `y` confirmation
before touching the device - **this permanently overwrites whatever's
currently on the SNES Mini for that game**, so make sure you're pushing the
right thing. Picks the most recently updated save/state in RomM if there's
more than one. Loops back to the game list after each push so you can do
several in one session; blank/`q` at the game picker exits.

## Retention

Battery saves go into a dedicated RomM save slot (`slot` in config, default
`auto-sync`) with autocleanup on, keeping the last `autocleanup_limit` saves
in that slot. RomM also skips uploading when the save is byte-identical to
what's already in the slot, so an unplayed game doesn't churn out duplicates.

States don't have a slot/autocleanup concept in RomM - each game's state
upload reuses the same filename, so it just replaces the previous one
in place rather than accumulating.

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
