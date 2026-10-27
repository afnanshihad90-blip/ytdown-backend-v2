# YTDown Backend (Render)

This is the backend API for YTDown video downloader.
It uses **yt-dlp** to extract real download links (360p → 4K + MP3).

## Deploy on Render (Free)

1. Go to [https://render.com](https://render.com) and sign up (GitHub recommended)
2. Click **New +** → **Web Service**
3. Connect your GitHub repo (or upload these files)
4. Settings:
   - **Name**: ytdown-backend (or anything)
   - **Runtime**: Python 3
   - **Build Command**: `pip install -r requirements.txt`
   - **Start Command**: `gunicorn app:app --bind 0.0.0.0:$PORT --workers 1 --timeout 120`
5. Click **Create Web Service**
6. Wait 2-5 minutes for deploy
7. Copy your service URL (example: `https://ytdown-backend.onrender.com`)

## After Deploy

You will get a URL like:
```
https://your-app-name.onrender.com
```

Then update the frontend (`assets/js/app.js`) with this URL.

## API Endpoints

- `GET /` → Status check
- `POST /api/info` → Get video info + download links
  ```json
  {
    "url": "https://www.youtube.com/watch?v=xxxxx"
  }
  ```

## Notes

- Free plan sleeps after 15 minutes of inactivity
- First request after sleep takes 30-60 seconds
- Timeout is set to 120 seconds for slow extractions
