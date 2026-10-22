# PDF Capture

A small Windows app that pages or scrolls through a document open in Adobe Acrobat or Reader,
screenshots every page, and binds the screenshots into a single PDF.

## Get the app

Download `PDFCapture.exe` from the latest release, then double-click it. Because the file is not
digitally signed, Windows may show a SmartScreen warning the first time: choose
**More info > Run anyway**.

## Build it

The exe is built automatically by GitHub Actions (`.github/workflows/build.yml`). To build a new
copy, open the **Actions** tab, choose **Build PDFCapture.exe** and click **Run workflow**.
