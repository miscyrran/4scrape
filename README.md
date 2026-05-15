# 4scrape

A self-hosted 4chan thread archiver with a web GUI. Monitors threads on a schedule, saves post text, images, and video files, and provides a local archive viewer.

## Requirements

- Docker and Docker Compose

## Quick start

```bash
docker compose up -d
```

Then open http://localhost:5000.

Archived content is written to `./data/` on your machine. This folder is live while the container runs and persists across restarts.

## Usage

1. Paste or drag a 4chan thread URL into the input box and press Enter (or click Add Thread).
2. The thread is scraped immediately, then re-checked on the configured schedule.
3. Click a thread title to view the local archive. Use the "4chan" button next to it to open the live page.

Supported URL formats:
- `https://boards.4chan.org/{board}/thread/{id}`
- `https://boards.4channel.org/{board}/thread/{id}`

## Configuration

Settings are available in the collapsible panel at the bottom of the GUI. Key options:

| Setting | Default | Description |
|---|---|---|
| Interval | 30 min | How often all monitored threads are re-scraped |
| Max images | 200 | Skip image downloads on threads above this count (0 = unlimited) |
| Request delay | 1 sec | Pause between API calls (4chan rate limit guidance: >= 1s) |
| Output directory | `4chan_archive` | Where archives are written inside the container |
| Download images | on | Save full-size images and video files |
| Save raw JSON | on | Keep the raw API response alongside the plain-text archive |

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
- Threads marked 404 are kept in the list but not re-scraped.
- Video files (.webm, .mp4) are downloaded and play inline in the archive viewer.
