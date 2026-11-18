# Recast 1.0.0

The first release of Recast, my free, offline file converter for the Mac.

## What's in it

- Drop files on the window or the Dock icon, or right click a file in Finder and pick Convert with Recast.
- Converts images, video, audio, documents, presentations, spreadsheets and PDF pages.
- Batches convert in parallel. Everything goes to Downloads and your originals are never changed.
- A simple quality setting (Smaller, Balanced, Best), plus a resolution option for video.
- Combine a bunch of images into one PDF.
- Light mode, Dark mode, or match your Mac.
- Everything runs on your Mac. Nothing gets uploaded.

## What you need

- An Apple Silicon Mac on macOS 14 or newer
- Homebrew with `brew install ffmpeg` and `brew install --cask libreoffice`. Recast can install these for you from inside the app.

## Opening it the first time

I haven't paid for Apple's notarization, so macOS will block the first launch. Go to System Settings, then Privacy & Security, and click **Open Anyway**.

If macOS says Recast is damaged instead, run `xattr -dr com.apple.quarantine /Applications/Recast.app` in Terminal once and open it again.

## Finder shortcut

Download `Convert-with-Recast-Quick-Action.zip` too, unzip it and double click the workflow to add **Convert with Recast** to your right click menu.
