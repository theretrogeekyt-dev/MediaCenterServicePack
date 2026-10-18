# MediaCenterServicePack

[![License: Unlicense](https://img.shields.io/badge/license-Unlicense-blue.svg)](LICENSE)
[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue.svg)](https://www.python.org/)
[![Docker](https://img.shields.io/badge/docker-ready-2496ED.svg)](Dockerfile)
[![WMC Tested](https://img.shields.io/badge/Windows%20Media%20Center-Vista%20%7C%207%20%7C%208.1%20%7C%2010%20%7C%2011-0078D7.svg)](#)

**MediaCenterServicePack** is an open-source, fully self-contained Dockerized backend designed to restore Electronic Program Guide (EPG) TV listings, movie library metadata, and music album artwork to Windows Media Center (WMC).

Rather than relying on fragile SSL/TLS proxying or deprecated Microsoft online cloud services, MediaCenterServicePack runs on-premises as a headless appliance. It ingests modern upstream data feeds (Schedules Direct, TMDb, MusicBrainz) and compiles schema-compliant payloads natively ingested by legacy WMC binaries (`loadmxf.exe` and `ehshell.exe`).

> [!IMPORTANT]
> **Headless Server Appliance**: MediaCenterServicePack is strictly a backend server, not a media player or media manager. It does **NOT** require or mount media folders. All movie metadata and music artwork are fetched and served directly on-demand via REST APIs, the web dashboard, and client automation scripts.

---

## Key Features

1. **EPG Lineup Service**:
   - Ingests TV listings directly using Schedules Direct account credentials (`SCHEDULES_DIRECT_USER` & `SCHEDULES_DIRECT_PASS`) or an API key/token (`EPG_API_KEY`).
   - Automatically handles SD-JSON lineup discovery and registration for your local postal code (`POSTAL_CODE`), supporting over-the-air (OTA) antenna and cable lineups.
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

4. **DNS Auto-Discovery & Zero-Config Setup**:
   - Built-in lightweight async DNS server (UDP/TCP port 53) that intercepts legacy Microsoft TV Guide and metadata endpoints (`data.tvdownload.microsoft.com`, `services.tvdownload.microsoft.com`, `dvd.windowsmedia.com`, etc.) and discovery hostnames (`mediacenter.local`, `wmc.local`, `openmce.local`), resolving them to the server IP.
   - Forwards all standard internet DNS queries to upstream resolvers (`1.1.1.1`, `8.8.8.8`).
   - WMC clients pointing their DNS to MediaCenterServicePack connect automatically without needing `-ServerUrl`, and legacy background tasks resolve locally.

5. **Self-Contained Dashboard & Scheduler**:
   - Built-in APScheduler running configurable cron jobs for guide synchronization.
   - Interactive web dashboard with real-time direct search and 1-click download tools for movies and music.
   - Native container health check endpoint (`GET /health`) and DNS diagnostics (`GET /api/dns/status`).

6. **Client Automation Helper**:
   - `client/sync-wmc.ps1`: Lightweight PowerShell script with automatic server discovery. Can be executed with zero parameters (`.\sync-wmc.ps1`) when DNS is configured, or with `-ServerUrl`. Downloads `guide.mxf`, safely manages background services (`ehsched`, `ehrecvr`), executes `loadmxf.exe -i guide.mxf`, and invokes `ehPrivJob.exe`. Also supports direct movie and album artwork package downloads.

---

## Architecture Overview

```
                      +---------------------------------------+
                      | Upstream Online Services              |
                      | - Schedules Direct (TV Guide)         |
                      | - The Movie Database (TMDb)           |
                      | - MusicBrainz & Cover Art Archive     |
                      | - Upstream DNS (1.1.1.1, 8.8.8.8)     |
                      +-------------------+-------------------+
                                          |
                                          v
+-----------------------------------------------------------------------------+
| MediaCenterServicePack Backend (Docker Container - Headless Appliance)      |
|                                                                             |
|   +--------------------+   +--------------------+   +-------------------+   |
|   | EPG Lineup Service |   | Direct Movie Engine|   | Direct Music Eng  |   |
|   | - Schedules Direct |   | - TMDb Search/Get  |   | - MusicBrainz WS2 |   |
|   | - SD-JSON API      |   | - MDR-DVD 5.0 XML  |   | - 500x500 Cover   |   |
|   | - MXF Generator    |   | - folder/poster.jpg|   | - AlbumArtSmall   |   |
|   +---------+----------+   +---------+----------+   +---------+---------+   |
|             |                        |                        |             |
|             v                        v                        v             |
|      /config/guide.mxf        Direct On-Demand ZIP     Direct On-Demand ZIP |
|                               (dvdid.xml + artwork)    (Folder.jpg + APIC)  |
|                                                                             |
|   +---------------------------------------------------------------------+   |
|   | Async DNS Auto-Discovery & Interception Engine (Port 53 UDP/TCP)     |   |
|   | Resolves data.tvdownload.microsoft.com, dvd.windowsmedia.com,       |   |
|   | mediacenter.local, wmc.local -> Local Server IP                     |   |
|   +---------------------------------------------------------------------+   |
|   | FastAPI Web Server & Admin Dashboard (Ports 80 & 8080)              |   |
|   | GET /health | GET /api/guide.mxf | GET /api/dns/status | GET /      |   |
|   +---------------------------------------------------------------------+   |
+-------------------------------------+---------------------------------------+
                                      |
         DNS Interception (Port 53) & HTTP Data Pull (Ports 80 / 8080)
           (guide.mxf / movie ZIP bundles / album art ZIP bundles)
                                      v
+-----------------------------------------------------------------------------+
| Windows Media Center Host (Windows 7 / 8.1 / 10 / 11 Community Edition)     |
|                                                                             |
|   Option A: Zero-Config DNS Auto-Discovery                                  |
|     - Point client DNS to MediaCenterServicePack IP                         |
|     - Run: sync-wmc.ps1 (auto-discovers server & imports guide.mxf)         |
|                                                                             |
|   Option B: Direct Script Execution                                         |
|     - Run: sync-wmc.ps1 -ServerUrl http://<docker-host>:8080                |
|                                                                             |
|   Database Ingestion & Cache Refresh:                                       |
|     - %windir%\ehome\loadmxf.exe -i guide.mxf                               |
|     - %windir%\ehome\ehPrivJob.exe /DoReIndexing /UpdateDiscMetadata        |
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
├── docker-entrypoint.sh         # Dynamic non-root PUID/PGID permissions mapper & net bind capabilities
├── requirements.txt             # Core backend dependencies (FastAPI, dnslib, lxml, mutagen, Pillow)
├── pytest.ini                   # Async test runner configuration
├── client/
│   └── sync-wmc.ps1             # Standalone Windows client sync script with auto-discovery
├── src/
│   ├── config.py                # Pydantic settings, dynamic LAN IP detection & path resolution
│   ├── api/
│   │   ├── main.py              # FastAPI server, WMC domain middleware & health check endpoints
│   │   └── templates/
│   │       └── index.html       # Web administration dashboard with DNS status & 1-click tools
│   ├── dns/
│   │   ├── __init__.py          # DNS module package
│   │   └── server.py            # Async UDP & TCP DNS server and upstream forwarder
│   ├── epg/
│   │   ├── models.py            # Relational EPG data models
│   │   ├── schedules_direct.py  # Schedules Direct JSON API client & token authenticator
│   │   ├── mxf_generator.py     # Microsoft MXF schema serializer
│   │   ├── xmltv_parser.py      # Secondary XMLTV feed parser
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
    ├── test_dns.py              # DNS server, stats, interception & middleware tests
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

Edit `docker-compose.yml` to specify your timezone, Schedules Direct credentials or EPG API key, and optional TMDb API key:

```yaml
services:
  mediacenter:
    build: .
    image: ghcr.io/theretrogeekyt-dev/mediacenterservicepack:latest
    container_name: mediacenter-servicepack
    restart: unless-stopped
    ports:
      - "8080:8080"
      # - "80:8080"        # Optional: uncomment only if host port 80 is free (NAS devices like Synology DSM use port 80)
      - "53:53/udp"
      - "53:53/tcp"
    environment:
      # System & Timezone configuration
      - TZ=America/New_York
      - PUID=1000
      - PGID=1000

      # DNS Server & Auto-Discovery Configuration
      - ENABLE_DNS_SERVER=true
      - DNS_PORT=53
      # Host IP for DNS resolution (auto-detected if omitted)
      # - DNS_HOST_IP=192.168.1.100
      - UPSTREAM_DNS=1.1.1.1,8.8.8.8

      # TV Guide EPG Service Configuration (Schedules Direct)
      # Provide your Login Name & Password directly:
      - SCHEDULES_DIRECT_USER=your_login_name
      - SCHEDULES_DIRECT_PASS=your_password
      # Optional local postal code for antenna broadcast lineup (e.g. 67147):
      - POSTAL_CODE=your_postal_code
      # Or provide session token / username:password via EPG_API_KEY:
      # - EPG_API_KEY=

      # Metadata API Keys (for direct TMDb queries)
      - TMDB_API_KEY=your_tmdb_api_key_here

      # Background cron EPG sync schedule (standard crontab format)
      - CRON_EPG_SYNC=0 3 * * *
    volumes:
      - ./config:/config
      # - ./src:/app/src       # Optional: only for local dev when git repo is cloned locally
```

Start the container:

```bash
docker compose up -d --build
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
| `ENABLE_DNS_SERVER` | `true` | Enable built-in async DNS server for zero-config WMC auto-discovery |
| `DNS_PORT` | `53` | DNS listening port (UDP & TCP) |
| `DNS_HOST_IP` | *Auto-detected* | Local LAN IP returned for local private clients |
| `DNS_PUBLIC_IP` | *Auto-detected* | Public WAN IP or DDNS hostname returned for remote clients |
| `DNS_AUTO_DETECT_PUBLIC_IP` | `true` | Automatically detect public WAN IP via external lookup |
| `DNS_AUTHORITATIVE_ONLY` | `false` | When `true`, reject all non-WMC domain queries |
| `DNS_ALLOW_EXTERNAL_RECURSION` | `false` | When `false`, refuse public WAN recursion to prevent open resolver DDoS abuse |
| `DNS_RATE_LIMIT_PER_SEC` | `50` | Maximum queries per second per source IP (Response Rate Limiting) |
| `UPSTREAM_DNS` | `1.1.1.1,8.8.8.8` | Comma-separated upstream DNS servers for forwarding internet queries |
| `SCHEDULES_DIRECT_USER` | *None* | Schedules Direct Login Name (from your account overview) |
| `SCHEDULES_DIRECT_PASS` | *None* | Schedules Direct account password |
| `POSTAL_CODE` | *None* | Local zip/postal code for over-the-air antenna lineup auto-discovery |
| `EPG_API_KEY` | *None* | Direct service API key, session token, or `username:password` string |
| `SCHEDULES_DIRECT_KEY` | *None* | Schedules Direct direct API token alias |
| `EPG_SERVICE_URL` | *None* | Custom EPG service endpoint (defaults to Schedules Direct 20141201) |
| `TMDB_API_KEY` | *None* | API Key for The Movie Database (v3) |
| `CRON_EPG_SYNC` | `0 3 * * *` | Crontab expression for daily EPG sync (03:00) |

---

## Public DNS & Remote WAN Setup (Connect WMC from Anywhere)

You can run MediaCenterServicePack's DNS server publicly so remote Windows Media Center clients outside your local network (e.g., family homes, laptops, remote setups) automatically discover your server and ingest TV guide and metadata.

### 1. Router Port Forwarding
On your home router or firewall, forward the following ports to your server/NAS local IP:
- **Port 53 (UDP & TCP)** &rarr; Server IP:53 *(DNS Resolution)*
- **Port 8080 (TCP)** &rarr; Server IP:8080 *(Web UI & Metadata API)*
- *(Optional)* **Port 80 (TCP)** &rarr; Server IP:8080 *(Allows native Windows Media Center background tasks to reach `data.tvdownload.microsoft.com` without port numbers)*

### 2. Built-in Security & DDoS Protection
Running an open DNS resolver on the public internet can expose servers to DNS amplification attacks. MediaCenterServicePack includes enterprise-grade protection built-in:
- **Response Rate Limiting (RRL)**: Drops query floods exceeding `DNS_RATE_LIMIT_PER_SEC` (default: 50 qps per IP).
- **Open Resolver Protection (`DNS_ALLOW_EXTERNAL_RECURSION=false`)**: External public clients can only query authoritative WMC domains. Random non-WMC recursion queries return `REFUSED`, preventing DDoS amplification abuse.
- **Split-Horizon DNS**: Local LAN clients querying your DNS server receive your local LAN IP (preventing NAT loopback failure), while remote internet clients receive your public WAN IP or DDNS hostname!

### 3. Remote Client Configuration
On remote Windows Media Center machines:
- Set their IPv4 DNS server to your **Public IP** or **DDNS address**.
- Or run:
  ```powershell
  .\sync-wmc.ps1 -InstallTask -SetDnsServer auto -ServerUrl http://<your-public-ip-or-ddns>:8080
  ```

---

## Windows Media Center Client Setup

On your Windows Media Center PC (running Windows 7, 8.1, 10, or 11 with the WMC community package):

### Option 1: 1-Click Setup (Zero Terminal Required - Recommended)

You never need to open a terminal, command prompt, or PowerShell script:

1. Open your browser to `http://<server-ip>:8080/` and navigate to the **WMC Client Setup** tab.
2. Click **Download 1-Click Setup (.bat)** (or download directly from `http://<server-ip>:8080/api/client/setup.bat`).
3. Right-click `setup-wmc.bat` and select **Run as administrator**.
4. **Done!** The installer automatically:
   - Installs background tasks that run daily at 04:00 AM and at logon.
   - Automatically synchronizes TV Guide (`loadmxf.exe`).
   - Automatically scans your Music folder and downloads 500x500 `Folder.jpg` and `AlbumArtSmall.jpg` covers.
   - Automatically scans your Videos folder and downloads `dvdid.xml` movie metadata.
   - Places a **"Sync Windows Media Center"** shortcut on your desktop for 1-click silent updates anytime with no terminal windows!

### Option 2: Zero-Config DNS Auto-Discovery

1. Set the Primary DNS server on your WMC client machine or network adapter to the IP of your MediaCenterServicePack server (e.g. `192.168.1.100`).
   ```powershell
   # In elevated PowerShell on the WMC client:
   Get-NetAdapter | Where-Object Status -eq 'Up' | Set-DnsClientServerAddress -ServerAddresses ("192.168.1.100")
   ```
2. Copy `client/sync-wmc.ps1` to your WMC machine (e.g., `C:\Scripts\sync-wmc.ps1`).
3. Run `sync-wmc.ps1 -All` to synchronize guide, music, and movies:
   ```powershell
   powershell.exe -ExecutionPolicy Bypass -File C:\Scripts\sync-wmc.ps1 -All
   ```

### Option 3: Direct HTTP URL Execution

If you prefer not to change DNS settings on the client machine, pass `-ServerUrl` explicitly:

```powershell
powershell.exe -ExecutionPolicy Bypass -File C:\Scripts\sync-wmc.ps1 -ServerUrl http://192.168.1.100:8080 -All
```

### Direct Movie & Music Downloads via Client Script

You can also use `sync-wmc.ps1` to download movie metadata and music artwork packages directly from your backend:

```powershell
# Directly download movie metadata (dvdid.xml + folder.jpg) into WMC DvdInfoCache:
powershell.exe -ExecutionPolicy Bypass -File C:\Scripts\sync-wmc.ps1 -DownloadMovie "Inception"

# Directly download album artwork (Folder.jpg 500x500 + AlbumArtSmall.jpg 75x75) into WMC art_cache:
powershell.exe -ExecutionPolicy Bypass -File C:\Scripts\sync-wmc.ps1 -DownloadAlbum "Dark Side of the Moon"

# Automatically scan a music folder and download missing 500x500 Folder.jpg for all albums:
powershell.exe -ExecutionPolicy Bypass -File C:\Scripts\sync-wmc.ps1 -MusicFolder "C:\Users\YourUser\Music"

# Re-index and refresh WMC's music library cache:
powershell.exe -ExecutionPolicy Bypass -File C:\Scripts\sync-wmc.ps1 -RefreshMusic

# Deep reset: Clear corrupt WMP/WMC media library database & rebuild from Folder.jpg:
powershell.exe -ExecutionPolicy Bypass -File C:\Scripts\sync-wmc.ps1 -ResetMusicDatabase
```

---

## API Endpoints

- `GET /health`: JSON container health check reporting service uptime, EPG status, and DNS service metrics.
- `GET /api/guide.mxf`: Serves compiled MXF XML payload (`loadmxf.exe` target).
- `GET /api/dns/status`: Returns DNS server status, listening port, target server IP, and query metrics.
- `GET /api/status`: Detailed system metrics, configured paths, channel counts, and job schedules.
- `GET /api/epg/channels`: Formatted active channel lineup and upcoming schedule entries for web browser viewing.
- `POST /api/epg/sync`: Manually triggers immediate EPG compilation, optionally accepting `?api_key=...` or `?source_url=...`.
- `POST /api/epg/configure`: Updates upstream credentials (Schedules Direct / TMDb) in `/config/settings.json` and runs an EPG sync.
- `GET /api/metadata/movies/search`: Directly search TMDb movies (`?query=...&year=...`).
- `GET /api/metadata/movies/{id}`: Directly retrieve full TMDb movie details.
- `GET /api/metadata/movies/{id}/dvdid`: Directly preview generated WMC `dvdid.xml` (5.0 MDR-DVD XML).
- `GET /api/metadata/movies/{id}/download`: Download complete WMC movie package (.zip) with `dvdid.xml` and `folder.jpg`.
- `GET /api/metadata/music/search`: Directly search MusicBrainz album releases (`?query=...&artist=...`).
- `GET /api/metadata/music/{mbid}`: Directly retrieve release details and track list.
- `GET /api/client/setup.bat`: Serves pre-configured 1-click Windows setup batch installer.
- `GET /api/client/sync-wmc.ps1`: Serves latest Windows Media Center PowerShell sync script.
- `GET /`: Modern Web Suite dashboard featuring tabbed navigation: TV Guide & Lineup Viewer, Movie Studio, Music Studio, Dynamic WMC Client Setup (with 1-click setup & DNS auto-discovery), and System Diagnostics.

---

## Running Unit Tests

To run the automated test suite locally:

```bash
pip install -r requirements.txt pytest pytest-asyncio
python -m pytest -v
```

All 33 tests validate:
- 1-Click Windows client setup script generator (`setup.bat`), silent task runner, and script delivery.
- DNS auto-discovery server, query interception (`A` and `AAAA` records), upstream forwarding, and native WMC HTTP interception middleware (TV Guide, Movies, and Music).
- Schedules Direct API token authentication, SHA-1 password hashing, and auto lineup mapping.
- Microsoft MXF schema compliance (`McStore.dll`, `Providers`, `With`, `ScheduleEntries`, `Lineup`).
- `dvdid.xml` 5.0 MDR-DVD schema validation and XML preview endpoint.
- ID3v2.3 tag compliance and 500x500 Pillow image resizing.
- Direct TMDb & MusicBrainz online metadata queries and WMC ZIP bundle generation.
- Dynamic LAN host detection, EPG configuration endpoints, and rich HTML dashboard rendering.


---

## Continuous Integration & Automated Docker Testing

The repository includes an automated GitHub Actions CI/CD pipeline defined in [`.github/workflows/docker-ci.yml`](file:///.github/workflows/docker-ci.yml):

1. **Unit & Integration Testing**:
   Executes the full 18-test pytest suite on Python 3.12.
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
