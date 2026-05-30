# PrivForge Hub

**PrivForge Hub** is the companion PC server for the [PrivForge](https://play.google.com/store/apps/details?id=com.privforge.app) Android app.

It allows you to wirelessly transfer, stream, and process videos/audio/images between your Android device and Windows PC.

---

## Features

- 📡 Wireless file transfer between Android and PC
- 🎬 Stream videos stored on your PC directly to the app
- 🖼 Transfer images and audio files
- 🔒 Password-protected vault — files are hidden from Windows Explorer
- ⚙ **Tool Manager** — connect any CLI tool (AI upscalers, video processors, etc.) and run them from your phone
- 📊 Job progress tracking for long-running tools
- 🔑 QR code connection for instant pairing

---

## Requirements

- Windows 10 / 11 (64-bit)
- Both PC and Android device must be on the **same Wi-Fi network**

---

## Installation

### Option A: Download EXE (Recommended)

1. Go to the [**Releases**](../../releases/latest) page
2. Download `PrivForge_Hub.exe`
3. Run it — no installation required

> ⚠ Windows SmartScreen may appear on first launch.  
> Click **"More info" → "Run anyway"** to proceed.

### Option B: Run from source

```bash
git clone https://github.com/YOUR_USERNAME/privforge-hub.git
cd privforge-hub
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
python main.py
```

---

## How to Use

### 1. Launch PrivForge Hub
Run `PrivForge_Hub.exe`

### 2. Set a password
Enter a password and click **INITIALIZE** (first time only).  
After that, enter your password and click **UNLOCK**.

### 3. Start the server
Click **START SERVER**  
→ Status changes to `● BROADCASTING — 0.0.0.0:8000`

### 4. Connect from your Android device
- Open **PrivForge** app → **PC Link** tab
- Tap **Show QR** in the Hub, then scan from the app
- Or enter the URL manually: `http://YOUR_PC_IP:8000`

---

## Tool Manager

You can connect any CLI tool (EXE) to process videos directly from your phone.

1. Click **⚙ Tool Manager**
2. Fill in:
   - **Tool Name**: display name shown in the app
   - **EXE Path**: full path to the CLI executable
   - **Command Template**: e.g. `--input {input} --output {output} --scale 4`
3. Click **Register Tool**

`{input}` and `{output}` are automatically replaced with file paths.  
Other `{key}` placeholders become user-adjustable parameters in the app.

**Example tools you can connect:**
- Real-ESRGAN, Topaz Video AI, FFmpeg, custom Python scripts, etc.

---

## File Storage Location

Vault files are stored at:
```
C:\Users\YOUR_NAME\AppData\Local\PrivForgeData\.privforge_vault\
```

| Subfolder | Contents |
|---|---|
| (root) | Videos |
| `audio\` | Audio files |
| `images\` | Images |

---

## API Endpoints

| Method | Endpoint | Description |
|---|---|---|
| GET | `/videos` | List video files |
| GET | `/stream/{name}` | Stream video (range supported) |
| POST | `/upload` | Upload file (auto-routes by extension) |
| GET | `/audio` | List audio files |
| GET | `/audio/{name}` | Stream audio |
| GET | `/images` | List image files |
| GET | `/images/{name}` | Serve image |
| GET | `/tools` | List registered tools |
| POST | `/tools/{id}/process/{video}` | Start tool job |
| GET | `/tools/job/{job_id}` | Get job status |

---

## Android App

Download **PrivForge** on Google Play:  
👉 [PrivForge on Google Play](https://play.google.com/store/apps/details?id=com.privforge.app)

---

## License

MIT License — see [LICENSE](LICENSE) for details.
