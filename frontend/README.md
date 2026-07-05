# MediaMate Frontend

Production-ready frontend for MediaMate Downloader, designed to deploy on Vercel.

## Quick Start

### Local Development

```bash
# Install dependencies
npm install

# Build with local backend (http://localhost:8000)
REACT_APP_API_BASE="" npm run build

# Serve locally
npm run dev
```

Then open http://localhost:3000

### Build for Production

```bash
# Build with your Render backend URL
REACT_APP_API_BASE="https://your-backend.onrender.com" npm run build
```

## Environment Variables

- `REACT_APP_API_BASE`: Base URL of the backend API
  - Local: `""` (uses same origin)
  - Production: `https://your-backend.onrender.com`

This variable is injected during the build process via `build.js`.

## Deployment

### Vercel

1. Connect your GitHub repository to Vercel
2. Set root directory to `frontend`
3. Set build command to `npm run build`
4. Set output directory to `dist`
5. Add environment variable `REACT_APP_API_BASE` with your Render URL
6. Deploy

See [../DEPLOYMENT.md](../DEPLOYMENT.md) for detailed steps.

## Project Structure

```
frontend/
├── index.html         # Single HTML file with all CSS & JS
├── package.json       # NPM metadata and scripts
├── build.js          # Build script that injects env vars
├── vercel.json       # Vercel configuration
└── README.md         # This file
```

## Features

- Download videos from YouTube, TikTok, Instagram, Twitter, Facebook, Vimeo, LinkedIn
- Download audio/music
- Format conversion (mp4, mp3, etc.)
- Subtitle support
- Video enhancement and upscaling
- Batch downloads with progress tracking
- Dark mode support
- Responsive design

## Browser Support

Works on all modern browsers (Chrome, Firefox, Safari, Edge).
