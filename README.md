# 4scrape

A self-hosted 4chan thread archiver with a web GUI. Monitors threads on a schedule, saves post text, images, and video files, and provides a local archive viewer.

This is slopcoded mostly to experiment; don't be surprised if any issues are ignored.

## Requirements

- Docker and Docker Compose

## Quick start

```bash
docker compose up -d
```

Then open http://localhost:5000.

Archived content is written to `./data/` on your machine. This folder is live while the container runs and persists across restarts.

To pull the latest image after an update:

```bash
docker compose pull && docker compose up -d
```

## Usage

1. Paste or drag a 4chan thread URL into the input box and press Enter (or click Add Thread).
2. The thread is scraped immediately, then re-checked on the configured schedule.
3. Click a thread title to view the local archive. Use the "4chan" button next to it to open the live page.

Supported URL formats:
- `https://boards.4chan.org/{board}/thread/{id}`
- `https://boards.4channel.org/{board}/thread/{id}`

## Archive viewer

- `>>quotelinks` are rendered as links that jump to the quoted post within the page.
- Images are shown as thumbnails and expand to full size on click.
- Video files (.webm, .mp4) play inline.

## Thread list

### Archived Threads

Threads can be moved to a collapsible **Archived Threads** section at any time using the ⊟ button on each row. Archived threads are no longer scraped. Use the ↩ button to move a thread back to the monitored list.

Threads can also be archived automatically — see the Auto-archive settings below.

### Thread icons

| Icon | Meaning |
|------|---------|
| ⤵ | Auto-followed — added because a post in a previous thread linked to it |
| ≈ | Named discovery — added because its title matched a configured name pattern |

### Thread following

When a thread is nearing its bump limit, users typically post a "new thread" link pointing to the successor thread. 4scrape can detect these posts and automatically add the linked thread to the monitored list.

Threads added this way are tagged with a ⤵ icon in the thread list.

### Named thread discovery

If a successor thread is not linked directly, 4scrape can also find it by scanning board catalogs for threads whose title fuzzy-matches a configured name pattern. Patterns are managed under **Named Thread Discovery** in the Settings panel.

Threads added this way are tagged with a ≈ icon in the thread list.

## Configuration

Settings are available in the collapsible panel at the bottom of the GUI.

### General

| Setting | Default | Description |
|---|---|---|
| Interval | 30 min | How often all monitored threads are re-scraped |
| Max images | 200 | Skip image downloads on threads above this count (0 = unlimited) |
| Request delay | 1 sec | Pause between API calls (4chan rate limit guidance: >= 1s) |
| Output directory | `4chan_archive` | Where archives are written inside the container |
| Download images | on | Save full-size images and video files |
| Save raw JSON | on | Keep the raw API response alongside the plain-text archive |

### Thread Following

| Setting | Default | Description |
|---|---|---|
| Enable thread following | on | Scan new posts for successor thread links |
| Only near bump limit | on | Only scan threads with 300 or more posts |
| Allow cross-board links | off | Follow links that point to a different board |
| Tag auto-added threads | on | Show the ⤵ or ≈ icon on automatically added threads |
| Keywords | `new thread`, `new bread`, `bake`, `baked` | A post must contain one of these words alongside a cross-thread link to trigger following |

### Auto-archive

| Setting | Default | Description |
|---|---|---|
| Auto-archive when thread 404s | on | Move dead threads to the Archived section automatically |
| Auto-archive when 4chan archives the thread | on | Move threads to the Archived section when 4chan marks them read-only |

Config is stored in `./data/config.json` and persists across restarts.

## Changing the port

Edit the left-hand port number in `docker-compose.yml`:

```yaml
ports:
  - "8080:5000"   # GUI now at http://localhost:8080
```

## File layout

```
./data/
  config.json
  monitored_threads.json
  archive/
    {board}/
      {thread_id}_{slug}/
        posts.txt
        thread.json
        images/
```

## Running without Docker

```bash
pip install flask requests schedule
python web_gui.py
```

The scraper can also be run standalone (no GUI):

```bash
python 4chan_scraper.py --config config.json
python 4chan_scraper.py --run-once   # single pass, then exit
```

## Notes

- 4chan's API asks for no more than one request per second and one catalog request per ten seconds. The scraper enforces these automatically.
- Threads marked 404 are kept in the list (in the Archived section if auto-archive is on) but not re-scraped.
- The Docker image is built and pushed to `ghcr.io/miscyrran/4scrape:latest` automatically on every push to main.
