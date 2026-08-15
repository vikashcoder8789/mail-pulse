# Mail Pulse

Send up to **200 emails** per campaign and track **sent**, **opened**, **clicked**, and **unsubscribed**.

Anyone with the site link can create an account. Each person only sees their own campaigns and must use **their own SMTP** login.

Only email people who opted in. Every message includes an unsubscribe link.

## Go live for free on Render

Hugging Face Docker Spaces are paid. Render can host this Flask app on the free web plan.

### 1. Put the project on GitHub

1. Open [github.com/new](https://github.com/new).
2. Repository name: `mail-pulse`.
3. Keep it **Public**.
4. Do **not** add a README (this folder already has one).
5. Create the repo, then in PowerShell:

```powershell
cd C:\Users\vikas\mail-pulse
git init
git add .
git commit -m "Mail Pulse ready for Render"
git branch -M main
git remote add origin https://github.com/YOUR-USERNAME/mail-pulse.git
git push -u origin main
```

Replace `YOUR-USERNAME` with your GitHub username. Skip files you do not want public (`.env` is already ignored).

### 2. Create the Render web service

1. Open [dashboard.render.com](https://dashboard.render.com) and sign in with GitHub.
2. **New +** → **Web Service**.
3. Connect the `mail-pulse` repo.
4. Use these settings:

| Field | Choose |
| --- | --- |
| Language | Python 3 |
| Branch | `main` |
| Build command | `pip install -r requirements.txt` |
| Start command | `gunicorn --bind 0.0.0.0:$PORT --workers 1 --threads 8 --timeout 120 app:app` |
| Instance type | **Free** |

5. **Environment** → add:

| Key | Value |
| --- | --- |
| `SECRET_KEY` | any long random string |
| `PYTHON_VERSION` | `3.11.9` |

6. Click **Deploy**. Wait until it says **Live**.

Your shareable URL looks like:

`https://mail-pulse.onrender.com`

People open that link → **Create an account** → **Settings** (their SMTP) → send.

## Local run

```powershell
cd C:\Users\vikas\mail-pulse
.\.venv\Scripts\activate
pip install -r requirements.txt
python app.py
```

Open [http://127.0.0.1:5000](http://127.0.0.1:5000) and register.

## Notes

- Free Render apps **sleep** after idle time. The first visit can take ~30–60 seconds. Opens and clicks will not count while it is asleep.
- SQLite data can reset when the service is rebuilt. That is normal on the free disk.
- Consumer Gmail accounts often throttle bulk mail. A real SMTP provider is safer for 200 messages.
- Open counts are a lower bound because many inboxes block images.
