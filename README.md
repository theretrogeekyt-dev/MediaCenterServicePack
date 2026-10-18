# Project eHome (OpenMCE Backend)

[![License: Unlicense](https://img.shields.io/badge/license-Unlicense-blue.svg)](LICENSE)
[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue.svg)](https://www.python.org/)
[![Docker](https://img.shields.io/badge/docker-ready-2496ED.svg)](Dockerfile)
[![WMC Tested](https://img.shields.io/badge/Windows%20Media%20Center-Vista%20%7C%207%20%7C%208.1%20%7C%2010%20%7C%2011-0078D7.svg)](#)

**Project eHome (OpenMCE Backend)** is an open-source, fully self-contained Dockerized backend designed to restore Electronic Program Guide (EPG) TV listings, movie library metadata, and music album artwork to Windows Media Center (WMC).

Rather than relying on fragile SSL/TLS proxying or deprecated Microsoft online cloud services, Project eHome runs on-premises as a headless appliance. It ingests modern upstream data feeds (XMLTV, Schedules Direct, TMDb, MusicBrainz) and compiles schema-compliant payloads natively ingested by legacy WMC binaries (`loadmxf.exe` and `ehshell.exe`).

---

## Key Features

1. **EPG Lineup Service**:
   - Ingests XMLTV feeds and Schedules Direct JSON APIs.
   - Compiles schema-compliant `.mxf` (Media Center XML Format) payloads adhering to Microsoft's `McStore.dll` schema (`Channel`, `Lineup`, `ScheduleEntry`, `Program`, `SeriesInfo`, `Keyword`, `GuideImage`).
   - Enables native Windows Media Center "Record Series" DVR scheduling.
   - Hosted at `http://<docker-host>:8080/api/guide.mxf`.

2. **Movie Metadata Engine**:
   - Scans mapped movie shares (`/media/movies`).
   - Automatically queries The Movie Database (TMDb) for synopsis, release date, director, cast, studio, and MPAA rating.
   - Writes legacy `dvdid.xml` (5.0 MDR-DVD schema) and downloads front cover artwork as `folder.jpg` and `poster.jpg`.

3. **Music Metadata Engine**:
   - Recursively scans audio shares (`/media/music`).
   - Queries MusicBrainz WS/2 and Cover Art Archive.
   - Standardizes tags strictly to **ID3v2.3** (UTF-16 encoding), resolving WMC/Windows Media Player indexing crashes.
   - Downscales and embeds **500x500** front cover artwork (`APIC` frame) and outputs sidecar `Folder.jpg` / `AlbumArtSmall.jpg`.

4. **Self-Contained Dashboard & Scheduler**:
   - Built-in APScheduler running configurable cron jobs for guide synchronization and library scans.
   - Responsive web dashboard providing status cards, channel statistics, and one-click manual sync triggers.
   - Native container health check endpoint (`GET /health`).

5. **Client Automation Helper**:
   - `client/sync-wmc.ps1`: Lightweight PowerShell script for WMC machines to download `guide.mxf`, safely manage background services (`ehsched`, `ehrecvr`), execute `loadmxf.exe -i guide.mxf`, and invoke `ehPrivJob.exe` to refresh thumbnail and metadata caches.

---

## Architecture Overview

```
                      +---------------------------------------+
                      | Upstream Feeds                        |
                      | - XMLTV / Schedules Direct            |
                      | - The Movie Database (TMDb)           |
                      | - MusicBrainz & Cover Art Archive     |
                      +-------------------+-------------------+
                                          |
                                          v
+-----------------------------------------------------------------------------+
| OpenMCE Backend (Docker Container)                                          |
|                                                                             |
|   +--------------------+   +--------------------+   +-------------------+   |
|   | EPG Lineup Service |   | Movie Engine       |   | Music Engine      |   |
|   | - XMLTV Parser     |   | - TMDb Client      |   | - MusicBrainz WS2 |   |
|   | - Schedules Direct |   | - MDR-DVD 5.0      |   | - ID3v2.3 Tagger  |   |
|   | - MXF Generator    |   | - dvdid.xml Writer |   | - 500x500 Art Res |   |
|   +---------+----------+   +---------+----------+   +---------+---------+   |
|             |                        |                        |             |
|             v                        v                        v             |
|      /config/guide.mxf        /media/movies/           /media/music/        |
|                               (dvdid.xml + folder.jpg) (Folder.jpg + APIC)  |
|                                                                             |
|   +---------------------------------------------------------------------+   |
|   | FastAPI Web Server & Admin Dashboard (Port 8080)                     |   |
|   | GET /health | GET /api/guide.mxf | GET /api/status | POST /api/...  |   |
|   +---------------------------------------------------------------------+   |
+-------------------------------------+---------------------------------------+
                                      |
                     HTTP Pull        | Local Shared Volumes
               (/api/guide.mxf)       | (SMB / NFS Shares)
                                      v
+-----------------------------------------------------------------------------+
| Windows Media Center Host (Windows 7 / 8.1 / 10 / 11 Community Edition)     |
|                                                                             |
|   1. Windows Task Scheduler executes:                                       |
|      client/sync-wmc.ps1 -ServerUrl http://<docker-host>:8080               |
|                                                                             |
|   2. Service Management:                                                    |
|      Stop-Service ehsched, ehrecvr                                          |
|                                                                             |
|   3. Database Ingestion:                                                    |
|      %windir%\ehome\loadmxf.exe -i guide.mxf                                |
|                                                                             |
|   4. Cache & Thumbnail Refresh:                                             |
|      %windir%\ehome\ehPrivJob.exe /DoReIndexing /UpdateDiscMetadata         |
|      Start-Service ehsched, ehrecvr                                         |
+-----------------------------------------------------------------------------+
```

---

## Directory Structure

```
.
├── Dockerfile                   # Multi-stage lightweight Python 3.12-slim build
├── docker-compose.yml           # Pre-configured compose stack with persistent mounts
├── docker-entrypoint.sh         # Dynamic non-root PUID/PGID permissions mapper
├── requirements.txt             # Core backend dependencies
├── pytest.ini                   # Async test runner configuration
├── client/
│   └── sync-wmc.ps1             # Standalone Windows client sync script
├── src/
│   ├── config.py                # Pydantic settings and path resolution
│   ├── api/
│   │   ├── main.py              # FastAPI server and health check endpoints
│   │   └── templates/
│   │       └── index.html       # Web administration dashboard
│   ├── epg/
│   │   ├── models.py            # Relational EPG data models
│   │   ├── xmltv_parser.py      # XMLTV feed parser with episode numbering
│   │   ├── schedules_direct.py  # Schedules Direct JSON API client
│   │   ├── mxf_generator.py     # Microsoft MXF schema serializer
│   │   └── pipeline.py          # Orchestration pipeline and fallback generator
│   ├── metadata/
│   │   ├── tmdb_client.py       # TMDb API client & title extraction
│   │   ├── dvdid_generator.py   # 5.0 MDR-DVD dvdid.xml sidecar generator
│   │   ├── movie_scanner.py     # Movie directory scan daemon
│   │   ├── musicbrainz_client.py# MusicBrainz & Cover Art Archive client
│   │   ├── music_tagger.py      # Mutagen ID3v2.3 tagger & Pillow 500x500 resizer
│   │   └── music_scanner.py     # Music directory scan daemon
│   └── scheduler/
│       └── tasks.py             # APScheduler cron background runner
└── tests/
    ├── conftest.py              # Pytest isolation fixtures
    ├── test_api.py              # FastAPI endpoint tests
    ├── test_epg_mxf.py          # MXF schema compliance tests
    ├── test_metadata.py         # dvdid.xml & ID3v2.3 tagger tests
    └── test_scanners.py         # End-to-end scanner pipeline tests
```

---

## Quickstart Guide

### 1. Configure and Launch the Backend

Clone the repository and inspect `docker-compose.yml`:

```bash
git clone https://github.com/OpenMCE/Project-eHome.git
cd Project-eHome
```

Edit `docker-compose.yml` to specify your media paths, timezone, and optional API keys:

```yaml
services:
  openmce:
    build: .
    container_name: openmce-backend
    restart: unless-stopped
    ports:
      - "8080:8080"
    environment:
      - TZ=America/New_York
      - PUID=1000
      - PGID=1000
      - XMLTV_URL=https://example.com/epg.xml
      - TMDB_API_KEY=your_tmdb_api_key_here
      - CRON_EPG_SYNC=0 3 * * *
      - CRON_MEDIA_SCAN=0 4 * * *
    volumes:
      - ./config:/config
      - /mnt/storage/movies:/media/movies
      - /mnt/storage/music:/media/music
```

Start the container:

```bash
docker compose up -d
```

Access the dashboard at `http://<docker-host>:8080/`.

---

## Configuration Reference

### Environment Variables

| Variable | Default | Description |
|---|---|---|
| `TZ` | `UTC` | Timezone for cron schedules and EPG alignment |
| `PUID` | `1000` | User ID for volume file ownership (unRAID/Synology/NAS) |
| `PGID` | `1000` | Group ID for volume file ownership |
| `PORT` | `8080` | Internal HTTP listening port |
| `XMLTV_URL` | *None* | HTTP/HTTPS URL to an upstream XMLTV feed |
| `XMLTV_FILE` | *None* | Local path to an XMLTV file (e.g. `/config/guide.xml`) |
| `SCHEDULES_DIRECT_USER` | *None* | Schedules Direct account username |
| `SCHEDULES_DIRECT_PASS` | *None* | Schedules Direct account password |
| `SCHEDULES_DIRECT_KEY` | *None* | Schedules Direct API token (optional) |
| `TMDB_API_KEY` | *None* | API Key for The Movie Database (v3) |
| `CRON_EPG_SYNC` | `0 3 * * *` | Crontab expression for daily EPG sync (03:00) |
| `CRON_MEDIA_SCAN` | `0 4 * * *` | Crontab expression for media scanning (04:00) |

---

## Windows Media Center Client Setup

On your Windows Media Center PC (running Windows 7, 8.1, 10, or 11 with the WMC community package):

1. Copy `client/sync-wmc.ps1` to a permanent location (e.g., `C:\Scripts\sync-wmc.ps1`).
2. Test the script in an elevated PowerShell terminal:
   ```powershell
   powershell.exe -ExecutionPolicy Bypass -File C:\Scripts\sync-wmc.ps1 -ServerUrl http://192.168.1.100:8080
   ```
3. Create a Scheduled Task in Windows Task Scheduler:
   - **Trigger**: Daily at 04:00 AM (or after your Docker EPG sync cron).
   - **Action**: Start a program
   - **Program**: `powershell.exe`
   - **Arguments**: `-ExecutionPolicy Bypass -WindowStyle Hidden -File C:\Scripts\sync-wmc.ps1 -ServerUrl http://192.168.1.100:8080 -Quiet`
   - **Run with highest privileges**: Enabled (required for `loadmxf.exe`).

---

## API Endpoints

- `GET /health`: JSON container health check reporting service uptime and component states.
- `GET /api/guide.mxf`: Serves compiled MXF XML payload (`loadmxf.exe` target).
- `GET /api/status`: Detailed system metrics, configured paths, channel counts, and job schedules.
- `POST /api/epg/sync`: Manually triggers immediate EPG download and MXF generation.
- `POST /api/media/scan`: Manually triggers background scan for movies and music shares.
- `GET /`: Management web dashboard.

---

## Running Unit Tests

To run the automated test suite locally:

```bash
pip install -r requirements.txt pytest pytest-asyncio
python -m pytest -v
```

All 15 tests validate:
- XMLTV date format parsing with timezone offsets.
- Microsoft MXF schema compliance (`McStore.dll`, `Providers`, `With`, `ScheduleEntries`, `Lineup`).
- `dvdid.xml` 5.0 MDR-DVD schema validation.
- ID3v2.3 tag compliance and 500x500 Pillow image resizing.
- End-to-end scanner directory processing.
- FastAPI endpoints and HTML dashboard rendering.

---

## License

This project is released into the public domain under the [Unlicense](LICENSE).

