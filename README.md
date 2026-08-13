# PixEver: DIY Personal Photo Backup System

PixEver is a completely private, self-hosted, and ultra-lightweight photo backup system. Designed as a secure, zero-telemetry alternative to cloud services, it relies on a manual-sync mobile client and a highly portable, on-demand local server over your Wi-Fi network.

## Repository Structure

This monorepo contains two distinct modules that work together:

### 1. [PixEver Server](./pix-ever-server/README.md)

An ultra-lightweight, zero-telemetry Python/FastAPI backend. It runs on your computer or portable HDD, receives files over your local network, reads their EXIF data, and safely organizes them into human-readable folders (`YYYY/MM/Month_DD/`). It uses SQLite and SHA-256 hashing to guarantee zero duplicate files are saved.

### 2. [PixEver Client](./pix_ever_client/README.md)

A mobile companion app built with Flutter. It bypasses aggressive OS background task killers by putting you in control of exactly when and what to sync. It uses native OS gallery pickers and memory-efficient chunked hashing to securely stream large photos and 4K videos directly to your server without crashing your phone.

---

## Quick Start Guide

Follow these steps to get your server running, build the mobile app (APK), and start syncing your photos.

### Step 1: Start the Server

Navigate into the server directory and follow the [Server README](./pix-ever-server/README.md) to install dependencies and run the server.

```bash
cd pix-ever-server
python server.py
```

### Step 2: Find Your Computer's Local IP Address

You need this IP to connect the mobile app to the server.

- **Windows:** Open Command Prompt and run `ipconfig`. Look for **IPv4 Address** (e.g., `192.168.1.15`).
- **Mac/Linux:** Open Terminal and run `ifconfig` or `ip a`.

### Step 3: Build the Mobile App (APK)

Navigate into the client directory and follow the [Client README](./pix_ever_client/README.md) to build a release APK.

```bash
cd pix_ever_client
flutter build apk --release
```

Transfer the generated `.apk` file to your Android phone (via USB, email, or a cloud drive) and tap it to install. Make sure **"Install from Unknown Sources"** is enabled on your phone.

### Step 4: Sync Your Media

Once the server is running and the app is installed:

1. Ensure both your phone and computer are on the same Wi-Fi network.
2. Open the **PixEver App** on your phone.
3. Enter your computer's IP address (from Step 2) into the server address field.
4. Tap **"1. Select Media to Backup"** and choose the photos/videos you want to save.
5. Tap **"2. Sync Now"**.
