# Evidence Intake

Accepted file types are PDF, CSV, XLSX, PNG/JPEG, UTF-8 text, and Markdown, up to 20 MiB. Intake sanitizes filenames, validates extension/MIME/signature consistency, rejects executable or active content, hashes bytes with SHA-256, detects duplicates, and retains the raw file under a content-addressed path.

Preview metadata includes original/sanitized name, media type, byte size, source description, attestation, hash, and review status. Extracted text or AI-assisted claims remain explicitly `human_verified: false` until a reviewer decides them. Files are never externally uploaded and never alter configuration automatically.
