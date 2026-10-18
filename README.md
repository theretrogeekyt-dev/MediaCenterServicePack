# MediaCenterServicePack

[![License: Unlicense](https://img.shields.io/badge/license-Unlicense-blue.svg)](LICENSE)
[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue.svg)](https://www.python.org/)
[![Docker](https://img.shields.io/badge/docker-ready-2496ED.svg)](Dockerfile)
[![WMC Tested](https://img.shields.io/badge/Windows%20Media%20Center-Vista%20%7C%207%20%7C%208.1%20%7C%2010%20%7C%2011-0078D7.svg)](#)

**MediaCenterServicePack** is an open-source, fully self-contained Dockerized backend designed to restore Electronic Program Guide (EPG) TV listings, movie library metadata, and music album artwork to Windows Media Center (WMC).

Rather than relying on fragile SSL/TLS proxying or deprecated Microsoft online cloud services, MediaCenterServicePack runs on-premises as a headless appliance. It ingests modern upstream data feeds (XMLTV, Schedules Direct, TMDb, MusicBrainz) and compiles schema-compliant payloads natively ingested by legacy WMC binaries (`loadmxf.exe` and `ehshell.exe`).

> [!IMPORTANT]
> **Headless Server Appliance**: MediaCenterServicePack is strictly a backend server, not a media player or media manager. It does **NOT** require or mount media folders. All movie metadata and music artwork are fetched and served directly on-demand via REST APIs, the web dashboard, and client automation scripts.

---

## Key Features

1. **EPG Lineup Service**:
   - Ingests TV listings directly using a service API key (`EPG_API_KEY` or `SCHEDULES_DIRECT_KEY`), querying Schedules Direct / EPG services directly without requiring client files or username/passwords.
   - Also supports standard upstream XMLTV feeds and local XMLTV files.
   - Compiles schema-compliant `.mxf` (Media Center XML Format) payloads adhering to Microsoft's `McStore.dll` schema (`Channel`, `Lineup`, `ScheduleEntry`, `Program`, `SeriesInfo`, `Keyword`, `GuideImage`).
   - Enables native Windows Media Center "Record Series" DVR scheduling.
   - Hosted at `http://<docker-host>:8080/api/guide.mxf`.

2. **Direct Movie Metadata Engine (TMDb)**:
   - Queries The Movie Database (TMDb v3) directly without requiring local media folders.
   - Compiles legacy `dvdid.xml` (5.0 MDR-DVD schema) and downloads front cover artwork (`folder.jpg`, `poster.jpg`).
   - Generates downloadable ZIP packages for direct injection into WMC's `DvdInfoCache` or local directories.

3. **Direct Music Metadata Engine (MusicBrainz & CAA)**:
   - Queries MusicBrainz WS/2 and Cover Art Archive directly without requiring local media folders.
   - Standardizes tags strictly to **ID3v2.3** (UTF-16 encoding), resolving WMC/Windows Media Player indexing crashes.
   - Downloads and resizes front cover art to strict **500x500** (`Folder.jpg`) and **75x75** (`AlbumArtSmall.jpg`) WMC standards.
   - Generates downloadable ZIP packages for direct injection into WMC's `art_cache` or local directories.

4. **Self-Contained Dashboard & Scheduler**:
   - Built-in APScheduler running configurable cron jobs for guide synchronization.
   - Interactive web dashboard with real-time direct search and 1-click download tools for movies and music.
   - Native container health check endpoint (`GET /health`).

5. **Client Automation Helper**:
   - `client/sync-wmc.ps1`: Lightweight PowerShell script for WMC machines to download `guide.mxf`, safely manage background services (`ehsched`, `ehrecvr`), execute `loadmxf.exe -i guide.mxf`, and invoke `ehPrivJob.exe` to refresh thumbnail and metadata caches. Also supports direct movie and album artwork package downloads.

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
| MediaCenterServicePack Backend (Docker Container - Headless Appliance)      |
|                                                                             |
|   +--------------------+   +--------------------+   +-------------------+   |
|   | EPG Lineup Service |   | Direct Movie Engine|   | Direct Music Eng  |   |
|   | - XMLTV Parser     |   | - TMDb Search/Get  |   | - MusicBrainz WS2 |   |
|   | - Schedules Direct |   | - MDR-DVD 5.0 XML  |   | - 500x500 Cover   |   |
|   | - MXF Generator    |   | - folder/poster.jpg|   | - AlbumArtSmall   |   |
|   +---------+----------+   +---------+----------+   +---------+---------+   |
|             |                        |                        |             |
|             v                        v                        v             |
|      /config/guide.mxf        Direct On-Demand ZIP     Direct On-Demand ZIP |
|                               (dvdid.xml + artwork)    (Folder.jpg + APIC)  |
|                                                                             |
|   +---------------------------------------------------------------------+   |
|   | FastAPI Web Server & Admin Dashboard (Port 8080)                     |   |
|   | GET /health | GET /api/guide.mxf | GET /api/status | GET /api/...   |   |
|   +---------------------------------------------------------------------+   |
+-------------------------------------+---------------------------------------+
                                      |
                     HTTP Pull / On-Demand Downloads
          (guide.mxf / movie ZIP bundles / album art ZIP bundles)
                                      v
+-----------------------------------------------------------------------------+
| Windows Media Center Host (Windows 7 / 8.1 / 10 / 11 Community Edition)     |
|                                                                             |
|   1. Windows Task Scheduler executes:                                       |
|      client/sync-wmc.ps1 -ServerUrl http://<docker-host>:8080               |
|                                                                             |
|   2. Database Ingestion:                                                    |
|      %windir%\ehome\loadmxf.exe -i guide.mxf                                |
|                                                                             |
|   3. Cache & Thumbnail Refresh:                                             |
|      %windir%\ehome\ehPrivJob.exe /DoReIndexing /UpdateDiscMetadata         |
|      Start-Service ehsched, ehrecvr                                         |
+-----------------------------------------------------------------------------+
```

---

## Directory Structure

```
├── .github/
│   └── workflows/
│       └── docker-ci.yml        # Automated CI/CD pipeline (build, test, publish)
├── .dockerignore                # Build context exclusion rules
├── Dockerfile                   # Multi-stage lightweight Python 3.12-slim build
├── docker-compose.yml           # Pre-configured compose stack with persistent config mount
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
│   │   ├── movie_engine.py      # Direct TMDb movie metadata & ZIP bundle engine
│   │   ├── dvdid_generator.py   # 5.0 MDR-DVD dvdid.xml sidecar generator
│   │   ├── musicbrainz_client.py# MusicBrainz & Cover Art Archive client
│   │   ├── music_engine.py      # Direct MusicBrainz/CAA artwork & ZIP engine
│   │   └── music_tagger.py      # Mutagen ID3v2.3 tagger & Pillow 500x500 resizer
│   └── scheduler/
│       └── tasks.py             # APScheduler cron background runner
└── tests/
    ├── conftest.py              # Pytest isolation fixtures
    ├── test_api.py              # FastAPI endpoint tests
    ├── test_direct_metadata.py  # Direct movie and music engine & download tests
    ├── test_epg_mxf.py          # MXF schema compliance & pipeline tests
    └── test_metadata.py         # dvdid.xml & ID3v2.3 tagger tests
```

---

## Quickstart Guide

### 1. Configure and Launch the Backend

Clone the repository and inspect `docker-compose.yml`:

```bash
git clone https://github.com/theretrogeekyt-dev/MediaCenterServicePack.git
cd MediaCenterServicePack
```

Edit `docker-compose.yml` to specify your timezone, upstream guide source, and optional API keys:

```yaml
services:
  mediacenter:
    build: .
    image: ghcr.io/theretrogeekyt-dev/mediacenterservicepack:latest
    container_name: mediacenter-servicepack
    restart: unless-stopped
    ports:
      - "8080:8080"
    environment:
      - TZ=America/New_York
      - PUID=1000
      - PGID=1000
      - EPG_API_KEY=your_epg_service_api_key_here
      - TMDB_API_KEY=your_tmdb_api_key_here
      - CRON_EPG_SYNC=0 3 * * *
    volumes:
      - ./config:/config
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
| `CONFIG_DIR` | `/config` | Path to persistent storage for app settings, cache, and `guide.mxf` |
| `EPG_API_KEY` | *None* | Direct Service API key / token (Schedules Direct or EPG service) |
| `SCHEDULES_DIRECT_KEY` | *None* | Schedules Direct direct API token alias |
| `EPG_SERVICE_URL` | *None* | Custom EPG service endpoint (defaults to Schedules Direct 20141201) |
| `XMLTV_URL` | *None* | HTTP/HTTPS URL to an upstream XMLTV feed |
| `XMLTV_FILE` | *None* | Local path to an XMLTV file (e.g. `/config/guide.xml`) |
| `SCHEDULES_DIRECT_USER` | *None* | Schedules Direct account username (if no API key used) |
| `SCHEDULES_DIRECT_PASS` | *None* | Schedules Direct account password (if no API key used) |
| `TMDB_API_KEY` | *None* | API Key for The Movie Database (v3) |
| `CRON_EPG_SYNC` | `0 3 * * *` | Crontab expression for daily EPG sync (03:00) |

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

### Direct Movie & Music Downloads via Client Script

You can also use `sync-wmc.ps1` to download movie metadata and music artwork packages directly from your backend:

```powershell
# Directly download movie metadata (dvdid.xml + folder.jpg) into WMC DvdInfoCache:
powershell.exe -ExecutionPolicy Bypass -File C:\Scripts\sync-wmc.ps1 -ServerUrl http://192.168.1.100:8080 -DownloadMovie "Inception"

# Directly download album artwork (Folder.jpg 500x500 + AlbumArtSmall.jpg 75x75) into WMC art_cache:
powershell.exe -ExecutionPolicy Bypass -File C:\Scripts\sync-wmc.ps1 -ServerUrl http://192.168.1.100:8080 -DownloadAlbum "Dark Side of the Moon"
```

---

## API Endpoints

- `GET /health`: JSON container health check reporting service uptime and EPG status.
- `GET /api/guide.mxf`: Serves compiled MXF XML payload (`loadmxf.exe` target).
- `GET /api/status`: Detailed system metrics, configured paths, channel counts, and job schedules.
- `POST /api/epg/sync`: Manually triggers immediate EPG compilation, optionally accepting `?api_key=...` or `?source_url=...`.
- `GET /api/metadata/movies/search`: Directly search TMDb movies (`?query=...&year=...`).
- `GET /api/metadata/movies/{id}`: Directly retrieve full TMDb movie details.
- `GET /api/metadata/movies/{id}/download`: Download complete WMC movie package (.zip) with `dvdid.xml` and `folder.jpg`.
- `GET /api/metadata/music/search`: Directly search MusicBrainz album releases (`?query=...&artist=...`).
- `GET /api/metadata/music/{mbid}`: Directly retrieve release details and track list.
- `GET /api/metadata/music/{mbid}/download`: Download complete WMC music package (.zip) with 500x500 `Folder.jpg` and `AlbumArtSmall.jpg`.
- `GET /`: Management web dashboard with direct interactive metadata search & download tool.

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
- Direct TMDb & MusicBrainz online metadata queries and WMC ZIP bundle generation.
- FastAPI endpoints and HTML dashboard rendering.

---

## Continuous Integration & Automated Docker Testing

The repository includes an automated GitHub Actions CI/CD pipeline defined in [`.github/workflows/docker-ci.yml`](file:///.github/workflows/docker-ci.yml):

1. **Unit & Integration Testing**:
   Executes the full 15-test pytest suite on Python 3.12.
2. **Container Build & Live Smoke Testing**:
   - Builds the Docker image with Docker Buildx (`docker/setup-buildx-action@v3`) and GitHub Actions layer caching.
   - Spins up the container and polls `/health` until healthy.
   - Validates that `/api/guide.mxf` produces valid XML containing `McStore.dll` and `<MXF>`.
   - Validates `/api/status`.
3. **Artifact Export for Local Testing**:
   - Archives the tested Docker image (`mediacenter-servicepack-docker-image.tar.gz`) and uploads it as a workflow artifact.
   - You can download the artifact from any GitHub Actions run summary and load it immediately:
     ```bash
     gzip -d mediacenter-servicepack-docker-image.tar.gz
     docker load < mediacenter-servicepack-docker-image.tar
     docker run -d -p 8080:8080 mediacenter-servicepack:test
     ```
4. **Automated Publishing**:
   - Automatically publishes versioned and `latest` tags to the GitHub Container Registry (`ghcr.io/theretrogeekyt-dev/mediacenterservicepack`).

---

## License

This project is released into the public domain under the [Unlicense](LICENSE).
