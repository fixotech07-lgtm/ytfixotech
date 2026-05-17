# FixoTools — Media Downloader

Aplikacion Flask per shkarkim audio/video nga YouTube me:
- Kerkim te integruar (kerkon "Dua Lipa" dhe te dalin kenget)
- Buton "Bej Playlist" per cdo artist (mbledh kenget e tij automatikisht)
- Shkarkim direkt me 1 klikim (auto-save dialog)
- 12 shkarkime paralel njekohesisht (super fast)
- Deri 250 URL ne batch
- Playlist YouTube te plota
- Histori me thumbnail

## Instalim Lokal (recommended!)

```bash
# 1. Instalo Python 3.11+
# 2. Instalo dependencies
pip install -r requirements.txt

# 3. Instalo FFmpeg
# Windows:
winget install ffmpeg
# macOS:
brew install ffmpeg
# Linux:
sudo apt install ffmpeg

# 4. Nis app-in
python app.py

# 5. Hap browser ne:
# http://127.0.0.1:5000
```

## Deploy ne Railway

1. Push kete projekt ne GitHub
2. Hap https://railway.app
3. New Project -> Deploy from GitHub
4. Zgjidh repo-n
5. Railway do te perdore Dockerfile-in automatikisht
6. ffmpeg do te instalohet gjate build-it

### Verifikim ne Railway

Te Build Logs duhet te shohesh:
```
Step 3/9 : RUN ffmpeg -version
ffmpeg version 5.x ...      <- KJO konfirmon
```

## Paralajmerime per Railway

YouTube shpesh i bllokon IP-te e cloud providers (Railway, AWS, Google Cloud).
Nese sheh gabimin "Sign in to confirm you're not a bot", duhet ta ekzekutosh
app-in lokalisht ne kompjuterin tend.

## Si te perdorush

### Kerkim
1. Hap tab "Kerko & Shkarko"
2. Shkruaj "Dua Lipa" ose cdo emer kenge
3. Kliko "Shkarko" tek cdo rezultat per shkarkim direkt

### Bej Playlist nga Artisti
1. Kerko nje artist
2. Kliko butonin "🎵 Playlist" (i arte)
3. Hapet modal me deri 50 kenge te artistit
4. Zgjidh ato qe do
5. Kliko "Shkarko Playlist" -> ZIP automatik

### Playlist YouTube
1. Hap tab "Playlist / Batch"
2. Vendos URL-n e playlist-it
3. Sistemi e dallon automatikisht
4. Kliko "Shkarko Playlist"

### Batch (shume URL)
1. Hap tab "Playlist / Batch"
2. Vendos shume URL (nje per rresht), deri 250
3. Kliko "Shkarko N file"

## Konfigurime ne app.py

```python
MAX_BATCH      = 250    # maks URL ne batch
PARALLEL_JOBS  = 12     # sa shkarkime njekohesisht
SEARCH_RESULTS = 15     # sa rezultate per kerkim
ARTIST_LIMIT   = 50     # sa kenge per artist
```

## Vetem per perdorim vetjak. Respekto te drejtat e autorit.
