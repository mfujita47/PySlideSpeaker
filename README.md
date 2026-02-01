# PySlideSpeaker

PySlideSpeaker is a CLI tool that automatically generates presentation videos (MP4) from PDF slides and a script defined in YAML. It leverages Microsoft Edge TTS for high-quality voice synthesis and MoviePy for video assembly, featuring an incremental build system to minimize processing time during edits.

https://github.com/user-attachments/assets/292f6921-1442-4b9b-aaf2-d952496dcd4a

## ✨ Features

- 🎬 **Automated Video Generation**: Converts PDF slides and text scripts into a complete video presentation.
- 🎙️ **High-Quality TTS**: Uses `edge-tts` (Microsoft Edge Text-to-Speech) for natural-sounding voiceovers without API keys.
- ⚡ **Smart Incremental Builds**: Each video clip is hash-managed based on slide content, voice settings, and pauses. When you edit your script, only the modified slides are regenerated—minimizing rework and dramatically speeding up iteration cycles.
- ⚙️ **Flexible Configuration**: Supports global settings for voice, speed, and pauses, with per-slide overrides.
- 🌍 **Cross-Platform**: Works on Windows, macOS, and Linux (Python environment required).

## 📋 Requirements

- Python 3.10+
- [FFmpeg](https://ffmpeg.org/) (usually handled automatically by `imageio-ffmpeg`)

See `requirements.txt` for Python dependencies:
- `PyMuPDF` (PDF processing)
- `edge-tts` (Text-to-Speech)
- `moviepy` (Video editing)
- `PyYAML` (Configuration)

## 🚀 Installation

1. Clone the repository:
   ```bash
   git clone https://github.com/mfujita47/PySlideSpeaker.git
   cd PySlideSpeaker
   ```

2. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```

## 💻 Usage

### ⚡ Quick Start

With a single `.pdf` and `.yaml` in your directory:

```bash
python PySlideSpeaker.py
```

Or specify files explicitly:

```bash
python PySlideSpeaker.py --pdf slides.pdf --script script.yaml --output presentation.mp4
```

### 🔄 Workflow Example

1. **Create slides** with Marp (or PowerPoint/Keynote):
   ```bash
   marp sample.md -o sample.pdf
   ```

2. **Generate YAML script** using `prompt for yaml generation.md` with an LLM (see [YAML Generation](#yaml-script-generation))

3. **Generate video**: `python PySlideSpeaker.py`

4. **Iterate**: Edit `sample.yaml` and re-run—only modified slides regenerate

### Command Line Options

- `--pdf`: Input PDF (default: auto-detect `*.pdf`)
- `--script`: Input YAML (default: auto-detect `*.yaml`)
- `--output`: Output MP4 (default: `<pdf_name>.mp4`)
- `--cache`: Cache directory (default: current directory)
- `--clean`: Force clean rebuild

## ⚙️ Configuration

### Example `script.yaml`

```yaml
global_settings:
  voice: "en-US-AriaNeural"  # Edge TTS voice ID
  rate: "+0%"                # Speech rate adjustment
  inline_pause: 0.5          # Pause duration for [pause] tag (seconds)
  slide_pause: 1.0           # Silence added at the end of each slide (seconds)
  video_fps: 24              # Output video framerate
  image_dpi: 200             # PDF rasterization quality

slides:
  - page: 1                  # PDF page number (1-based)
    text: "Welcome to PySlideSpeaker.[pause] This tool automates video creation."

  - page: 2
    text: "You can customize settings per slide."
    voice: "en-US-GuyNeural" # Override voice for this slide
    rate: "+10%"             # Override speed for this slide
```

### 🤖 YAML Script Generation

Use `prompt for yaml generation.md` as an LLM prompt template:

1. Copy `prompt for yaml generation.md` into your LLM (ChatGPT, Claude, etc.)
2. Provide your slide content (Markdown, OCR text, or notes)
3. Get a properly formatted YAML with natural narration

### Script Tags

- `[pause]`: Inserts a silence of `inline_pause` seconds within the speech.

## 👤 Author

- **mfujita47 (Mitsugu Fujita)**

## 📄 License

[MIT License](LICENSE)
