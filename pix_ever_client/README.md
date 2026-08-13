# PixEver Client

The mobile companion app for the PixEver backup system. Built with Flutter, this app allows you to manually select photos and videos from your device and sync them securely to your local PixEver Server over Wi-Fi.

## Features

- **Manual Sync Control:** Bypasses aggressive OS background task killers by putting you in control of when and what to sync.
- **Native Gallery Picker:** Uses the OS-level media picker to easily select multiple photos and videos.
- **Local State Management:** Tracks `PENDING` and `BACKED_UP` statuses via an internal SQLite database so you never lose your sync progress.
- **Memory-Efficient Hashing:** Processes large files (like 4K videos) using chunked SHA-256 streaming, preventing RAM crashes on mobile devices.

## Setup & Installation

### Prerequisites

- Flutter SDK installed and configured (`flutter doctor` passes).
- A physical device connected via USB (with Developer Mode enabled) or an emulator.

### Step 1: Prepare the Environment

1. Navigate into the client directory:
   ```bash
   cd pix_ever_client
   ```
2. Fetch the required Flutter packages:
   ```bash
   flutter clean
   flutter pub get
   ```

### Step 2: Run or Build the App

**Option A: Run in Debug Mode (For Testing)**

Launch the app directly on your connected device:

```bash
flutter run
```

**Option B: Build the Android APK (For Permanent Installation)**

Compile a standalone Android APK to install permanently on your phone:

```bash
flutter build apk --release
```

Once the build finishes, locate the APK at:

```
build/app/outputs/flutter-apk/app-release.apk
```

Transfer this file to your phone and install it (ensure **"Install from Unknown Sources"** is enabled).

## Permissions

This application requires access to the device's media library to function.

- **Android:** Configured in `android/app/src/main/AndroidManifest.xml`.
- **iOS:** Configured in `ios/Runner/Info.plist`.

The app will prompt the user for these permissions upon attempting to select media for the first time.
