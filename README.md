# MediaMate Downloader

A full-stack application for downloading media from YouTube, TikTok, Instagram, and other platforms. Production-ready for deployment on Vercel (frontend) and Render (backend).

## 🚀 Features

- **Multi-platform Downloads**: YouTube, TikTok, Instagram, Twitter/X, Facebook, Vimeo, LinkedIn
- **Format Support**: MP4, MP3, WebM, MKV, and more
- **Quality Control**: Download in various resolutions (360p - 4K)
- **Subtitle Support**: Download and embed subtitles
- **Audio Extraction**: Convert videos to audio formats
- **Video Enhancement**: Upscale and denoise videos with FFmpeg
- **Batch Downloads**: Download multiple videos at once with progress tracking
- **Dark Mode**: Eye-friendly dark theme
- **Persistent Storage**: Downloads persist across server restarts
- **Job History**: Completed jobs available for 2 hours

## 📋 Project Structure

```
MediaMate_Downloader/
├── app.py                 # FastAPI backend
├── Dockerfile             # Docker configuration for Render
├── requirements.txt       # Python dependencies
├── render.yaml           # Render deployment config
├── DEPLOYMENT.md         # Detailed deployment guide
├── .env.example          # Environment variables template
└── frontend/
    ├── index.html        # Single-page frontend (HTML + CSS + JS)
    ├── package.json      # NPM metadata
    ├── build.js          # Build script for production
    ├── vercel.json       # Vercel deployment config
    └── README.md         # Frontend documentation
```

## 🛠️ Tech Stack

**Backend:**
- FastAPI (async Python web framework)
- yt-dlp (media downloading)
- FFmpeg (video processing)
- Pydantic (data validation)

**Frontend:**
- HTML5 + CSS3 + Vanilla JavaScript
- No build dependencies required for development
- Responsive design, dark mode support

## 🚀 Quick Deployment

### Prerequisites

- GitHub account
- Render account (for backend)
- Vercel account (for frontend)

### 1. Deploy Backend to Render

1. Push your code to GitHub
2. Go to [render.com](https://render.com)
3. Create new Web Service from GitHub repository
4. Use `render.yaml` for configuration
5. Add environment variable: `ALLOWED_ORIGINS=https://your-vercel-url.vercel.app,http://localhost:3000`
6. Deploy
7. Note your Render URL (e.g., `https://mediamate-backend.onrender.com`)

### 2. Deploy Frontend to Vercel

1. Go to [vercel.com](https://vercel.com)
2. Import your GitHub repository
3. Set root directory to `frontend`
4. Add environment variable: `REACT_APP_API_BASE=https://your-render-url.onrender.com`
5. Deploy

For detailed step-by-step instructions, see [DEPLOYMENT.md](./DEPLOYMENT.md)

## 💻 Local Development

### Backend

```bash
# Install dependencies
pip install -r requirements.txt

# Run server
python app.py
# Server runs on http://localhost:8000
```

### Frontend

```bash
cd frontend

# Build with local backend
REACT_APP_API_BASE="" npm run build

# Serve
npm run dev
# Frontend runs on http://localhost:3000
```

## 🔐 Configuration

### Environment Variables

**Backend (.env or Render environment):**
```
ENVIRONMENT=production
PORT=8000
ALLOWED_ORIGINS=https://your-frontend.vercel.app
BASE_DIR=/tmp/mediamate
```

**Frontend (Vercel environment):**
```
REACT_APP_API_BASE=https://your-backend.onrender.com
```

See `.env.example` for all available options.

## 📁 Storage

- **All environments**: `/tmp/mediamate` (ephemeral — cleared on restart)

Files are temporary and do not persist across server restarts.

## 🗄️ Job Persistence

Jobs are now stored as JSON files and persist across server restarts:
- Completed jobs kept for 2 hours
- Maximum 200 jobs in store
- Jobs directory: `{BASE_DIR}/jobs_db/`

## 🔒 CORS Security

CORS origins are configurable via environment variable:
```
ALLOWED_ORIGINS=https://frontend.com,http://localhost:3000
```

Wildcard domains are not allowed with credentials for security.

## 📊 API Endpoints

### Info
- `GET /api/info` - Server info and supported sources

### Download
- `POST /api/download` - Start video/audio download
- `GET /api/progress/{job_id}` - Real-time progress (Server-Sent Events)
- `GET /api/job/{job_id}` - Get job status
- `GET /download-file/{job_id}` - Download completed file

### Conversion
- `POST /api/convert` - Convert video format

### Utilities
- `POST /api/video-info` - Get video metadata
- `POST /api/update-ytdlp` - Update yt-dlp version

## 🐛 Troubleshooting

### CORS Errors
Add your Vercel URL to `ALLOWED_ORIGINS` on Render and restart the service.

### Downloads Not Persisting
This is expected behavior on Render free tier. Downloads are temporary and cleared on restart. For persistent storage, upgrade to a paid Render plan and use persistent disk volumes.

### Format Not Available
Update yt-dlp: `POST /api/update-ytdlp` or `pip install --upgrade yt-dlp`

### ffmpeg Not Found
Backend requires ffmpeg. On Render, it's installed via Dockerfile. For local dev:
```bash
# macOS
brew install ffmpeg

# Ubuntu/Debian
sudo apt-get install ffmpeg

# Windows
choco install ffmpeg
```

## 📝 License

MIT

## 🤝 Support

For issues, check the [DEPLOYMENT.md](./DEPLOYMENT.md) troubleshooting section or review Render/Vercel logs.

## 🔄 Updates

To update yt-dlp after deployment:
- Call `POST /api/update-ytdlp` endpoint, or
- Restart the Render service (forces requirement re-install)

---

**Happy downloading! 🎉**
