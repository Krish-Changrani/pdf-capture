# PDF Capture

A small Windows app that scrolls or pages through a document open in Adobe Acrobat or Reader,
screenshots every page, and binds the screenshots into a single PDF.

## Get the app

1. Download `PDFCapture.zip` from the latest release.
2. Right-click it and choose **Extract All**.
3. Open the extracted **PDFCapture** folder and double-click **PDFCapture.exe**.
   Keep the `_internal` folder next to it; the app needs it.

Because the app is not digitally signed, Windows may show a SmartScreen warning the first time:
choose **More info > Run anyway**.

## Build it

Built automatically by GitHub Actions (`.github/workflows/build.yml`) as a folder build, so it
starts quickly and does not unpack itself to a temp folder on every run. To build a new version,
open the **Actions** tab, choose **Build PDF Capture** and click **Run workflow**.
