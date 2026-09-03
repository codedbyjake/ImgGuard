# ImgGuard

A MediaWiki extension that checks uploaded images for explicit content before they're saved.

## How it works

Uploads are scored for how likely they are to be safe. A flagged upload is rejected.

Formats scanned:

- Raster (PNG/APNG, GIF, JPEG, WebP, BMP, TIFF, AVIF, HEIC/HEIF, ICO)
- SVG
- PDF and DjVu
- Video, including Ogg/Theora
- Office documents

Multi-page, multi-frame and container formats are scanned throughout, not only their first page or frame. The worst score across everything scanned is the one that counts.

Two models score every view. Each view is resized once and shared between them, so the second costs one inference rather than another decode. The lower of the two safe scores is the one kept.

A file that cannot be screened is rejected. `$wgImgGuardFailClosed` controls this.

Repeated real rejections within `$wgImgGuardAutoBlockWindow` auto-block a user. Off by default.

`maintenance/RescanUploads.php` re-scans existing uploads and reports what would flag at the current threshold. It does not log or block. `--limit` caps how many files it checks, `--start` resumes from a given file name.

## Permissions

Granted to `sysop` by default.

- `imgguard-bypass`: skip screening.
- `imgguard-log`: view `Special:Log/imgguard`.
- `imgguard-autoblock-exempt`: never auto-blocked.

## Settings

- `$wgImgGuardEnforce` (default `false`): actually reject flagged uploads, instead of only logging.
- `$wgImgGuardFailClosed` (default `true`): reject an upload if scoring fails.
- `$wgImgGuardSfwThreshold` (default `0.5`): minimum "safe" score required to pass.
- `$wgImgGuardBorderlineMargin` (default `0.15`): a pass within this margin above the threshold is logged as `imgguard/borderline` instead of `imgguard/pass`.
- `$wgImgGuardTimeout` (default `60`): max seconds to allow for scoring.
- `$wgImgGuardMemoryLimit` (default `1048576`, 1 GB in KB): memory ceiling for the classifier, replacing `$wgMaxShellMemory` for this script.
- `$wgImgGuardMaxConcurrent` (default `4`): how many classifications may run at once across all web workers. `0` disables the limit.
- `$wgImgGuardCacheTtl` (default `2592000`, 30 days): how long a file's score is cached.
- `$wgImgGuardFailureCacheTtl` (default `60`): how long a classification failure is negative-cached.
- `$wgImgGuardEligibleMimeTypes`: extra MIME types to scan beyond `image/*` and `video/*`. Defaults to PDF, DjVu, Ogg and office documents.
- `$wgImgGuardScriptPath` / `$wgImgGuardModelPath`: paths to the classification script and ONNX model.
- `$wgImgGuardMatureModelPath` (default `/wiki/imgguard/mature.onnx`): second ONNX model. Empty disables it and falls back to a single model.
- `$wgImgGuardLogPasses` (default `false`): log every passing upload's score, not just flagged or failed ones.
- `$wgImgGuardAutoBlockEnabled` (default `false`): automatically block a user after repeated real rejections.
- `$wgImgGuardAutoBlockThreshold` (default `3`): number of real `imgguard/reject` log entries, within `$wgImgGuardAutoBlockWindow`, that triggers the block.
- `$wgImgGuardAutoBlockWindow` (default `"30 days"`): how far back to count rejections toward the threshold, as a string accepted by `strtotime()`.
- `$wgImgGuardAutoBlockDuration` (default `"3 days"`): duration of the automatic block, as a string accepted by `strtotime()`.

## Structure

```
ImgGuard/
├── extension.json
├── bin/
│   ├── classify.py
│   └── extract.py
├── includes/
│   ├── Classifier.php
│   ├── Hooks.php
│   └── ServiceWiring.php
├── maintenance/
│   └── RescanUploads.php
├── i18n/
│   ├── en.json
│   └── qqq.json
├── LICENSE
└── README.md
```

## License

GPL-2.0-or-later
