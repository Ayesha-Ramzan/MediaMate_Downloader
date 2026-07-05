# Environment Variables Documentation

## Backend (Render)

### Required Variables

- **ENVIRONMENT**: Set to `production` for Render deployment
- **PORT**: Set by Render automatically (default: 8000)
- **ALLOWED_ORIGINS**: Comma-separated list of allowed origins for CORS
  - Example: `https://mediamate-downloader.vercel.app,http://localhost:3000`

### Optional Variables

- **BASE_DIR**: Base directory for storage (default: `/mnt/data/mediamate` on Render, `/tmp/Downloader` locally)
- **HOST**: Server bind address (default: `0.0.0.0`)

## Frontend (Vercel)

### Required Variables

- **REACT_APP_API_BASE**: Full URL to the backend API
  - Example: `https://mediamate-backend.onrender.com`
  - This is injected during the build process

### How to Set Env Vars

**On Render:**
1. Go to your service's "Environment" tab
2. Add environment variables under "Environment"
3. For sensitive data (cookies.txt), use "Secret Files"

**On Vercel:**
1. Go to your project settings → "Environment Variables"
2. Add `REACT_APP_API_BASE` with your Render backend URL
3. Make sure it's available to Production deployments

## Deployment Steps

### 1. Backend (Render)

1. Push code to GitHub
2. Go to [render.com](https://render.com)
3. Click "New +" → "Web Service"
4. Connect your GitHub repository
5. Configure settings:
   - **Name**: mediamate-backend
   - **Runtime**: Python 3
   - **Build Command**: `pip install -r requirements.txt`
   - **Start Command**: `python app.py`
6. Add environment variables (see above)
7. Add persistent disk:
   - **Name**: mediamate-storage
   - **Mount Path**: `/mnt/data`
   - **Size**: 50 GB (adjustable)
8. Deploy

### 2. Frontend (Vercel)

1. Make sure you're in the `frontend` directory
2. Push code to GitHub
3. Go to [vercel.com](https://vercel.com)
4. Click "Add New..." → "Project"
5. Import your GitHub repository
6. Configure:
   - **Root Directory**: `frontend`
   - **Build Command**: `npm run build`
   - **Output Directory**: `dist`
7. Add environment variable:
   - **REACT_APP_API_BASE**: Your Render backend URL
   - Make sure it's set for Production
8. Deploy

### 3. Get Your URLs

After deployment:
- **Render Backend URL**: Will be like `https://mediamate-backend.onrender.com`
- **Vercel Frontend URL**: Will be like `https://mediamate-downloader.vercel.app`

### 4. Connect Frontend to Backend

Add your Vercel URL to the backend's ALLOWED_ORIGINS:

```
https://mediamate-downloader.vercel.app,http://localhost:3000
```

## Local Development

### Backend

```bash
# Terminal 1: Run the backend
cd /path/to/MediaMate_Downloader
python app.py
# Server runs on http://localhost:8000
```

### Frontend

```bash
# Terminal 2: Run the frontend
cd /path/to/MediaMate_Downloader/frontend
npm install  # First time only
npm run build
python -m http.server 3000
# Open http://localhost:3000
```

The frontend will automatically detect localhost and use empty API_BASE (same origin).

## Troubleshooting

### CORS Errors

If you see CORS errors in the browser console:
1. Check that your Vercel URL is in the backend's `ALLOWED_ORIGINS`
2. Make sure you're using the exact URL (https vs http matters)
3. Restart the Render service after updating env vars

### Downloads Not Persisting

1. Check that the Render persistent disk is mounted at `/mnt/data`
2. Verify the disk size isn't full
3. Check Render logs for any storage errors

### API Unreachable

1. Verify the Render service is running (check status on render.com)
2. Check that `REACT_APP_API_BASE` is set correctly on Vercel
3. Try accessing `https://your-backend.onrender.com/api/info` directly
