# Recast

Offline macOS file converter (images, video, audio, documents, PDF). A SwiftUI app starts a bundled Python engine as a subprocess and reads its progress back as JSON lines. Nothing is sent over the network. Output defaults to `~/Downloads`; originals are never touched.

## Layout

- `app/Recast/` SwiftUI app (macOS 14+, arm64 only). `AppModel.swift` holds all state and logic, `ContentView.swift` the UI, `EngineClient.swift` runs the engine process, `FormatMatrix.swift` the format rules, `DiagnosticsLog.swift` the rolling log.
- `app/RecastTests/` XCTest unit tests. `FakeEngine.swift` stands in for the Python engine.
- `engine/recast_engine/` Python 3.13 package (`cli.py` entry point, `jobs.py` batch runner, `formats.py` conversion matrix, `converters/`).
- `engine/tests/` pytest suite.
- `app/project.yml` XcodeGen spec. **`app/Recast.xcodeproj` is generated and gitignored.**
- Root `*.command` scripts are the author's setup/build/publish helpers (macOS, interactive). Don't run them from here.

## Commands

```bash
# Swift tests (SKIP_ENGINE_EMBED skips bundling Python, which tests don't need)
cd app && xcodegen generate
SKIP_ENGINE_EMBED=1 xcodebuild test -project Recast.xcodeproj -scheme Recast \
  -destination 'platform=macOS,arch=arm64' CODE_SIGNING_ALLOWED=NO

# Engine tests (run from engine/; use build/python/bin/python3 if system Python lacks pytest)
cd engine && python -m pytest tests -m "not slow"     # ~45 s; drop -m for the full ~3 min run
```

Re-run `xcodegen generate` after editing `app/project.yml` or adding/removing Swift files.

## Rules that are easy to break

- **The format matrix has one source of truth: `engine/recast_engine/formats.py`.** The app reads a generated copy, `app/Recast/Resources/formats.json`, plus a test fixture `app/RecastTests/Fixtures/matrix_cases.json`. After changing `formats.py`, regenerate both:
  `cd engine && RECAST_UPDATE_FIXTURES=1 python -m pytest tests/test_swift_parity.py`
  CI fails if they're stale. Never hand-edit those two JSON files.
- **"Convert to" is computed in Swift** (`FormatMatrix.analyze`), not by launching Python. Keep it a direct translation of `analyze_targets` in `cli.py`; the parity tests compare them.
- **Input paths go to the engine via `--files-from` (a NUL-separated temp file), never as argv**, so large drops can't hit the argument-length limit.
- **`AppModel` talks to the engine only through the `EngineRunning` protocol** so tests can inject `FakeEngine`. Don't call `EngineClient.shared` directly from it.
- Engine stderr is captured (last 8 KB) and shown via "Copy Details"; the rolling log is `~/Library/Logs/Recast/recast.log`. Logs and reports can contain file names, so never send them anywhere automatically.
- The app is not sandboxed. If that ever changes, the output-folder setting needs security-scoped bookmarks (see the comment in `AppModel`).
- `pillow-heif` is GPL: it's a dev-only dependency and is stripped from the shipped app (HEIC goes through macOS `sips`). Don't add it to `engine/requirements.txt`.
- Bundled Python and its packages are listed in `THIRD_PARTY.md`; update it when dependencies change.

## Workflow

- Run `/code-review` before every commit, fix what it finds, and commit locally. Push only when the user asks.
- Commit messages: short imperative subject, then a body explaining what changed and why.
- CI (`.github/workflows/ci.yml`) runs the engine tests and the Swift tests on `macos-latest`. Dependabot opens weekly PRs for pip and GitHub Actions.
