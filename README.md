# Groksito Discord Bot & Pantsu Connector

![Python](https://img.shields.io/badge/Python-3.11%2B-blue.svg)
![Discord](https://img.shields.io/badge/Discord-Bot-7289da.svg)
![Spotify](https://img.shields.io/badge/Spotify-Integration-1DB954.svg)
![License](https://img.shields.io/badge/License-MIT-green.svg)

**Groksito** (internal project name: **Pantsu**) is a professional Discord bot and custom MCP-style connector that exposes a powerful set of tools for Grok and the xAI ecosystem to natively interact with Discord servers and Spotify.

This project extends Grok's capabilities with real-time Discord control (guilds, messages, rich embeds including gaming/SovietNoWaifu styles, image handling, auth flows) and full Spotify playback & library management.

It was built iteratively using Grok Build for scaffolding, tool design, and automation, with heavy focus on clean architecture, security, and ease of use for other developers.

## ✨ Features

- **16+ Discord Tools**
  - Guild, channel & member management
  - Rich message & embed sending (highly customizable, gaming aesthetics supported)
  - Reactions, editing, deletion, bulk operations
  - User/server info retrieval
  - Image & attachment handling
  - Permission & role management
- **Spotify Integration Tools**
  - Full playback control (play/pause/skip/queue/seek)
  - Track, artist, album & playlist search
  - Device management & volume control
  - Currently playing info & recommendations
- **MCP / Function-Calling Ready**
  - Designed for seamless integration with Grok, local Ollama agents, or any LLM supporting tool use
  - JSON Schema validated parameters
- **Secure by Design**
  - All credentials via environment variables only
  - Proper Discord intents & least-privilege permissions
  - Rate-limit aware with backoff
- **Developer Friendly**
  - Clean modular structure
  - Easy to add new tools
  - Self-hostable with Docker support planned

## 🚀 Installation

### Prerequisites
- Python 3.11+
- Discord Bot token (create at https://discord.com/developers/applications)
- Spotify Client ID + Client Secret (from https://developer.spotify.com/dashboard)
- (Optional) xAI API key if using direct Grok calls

### Quick Start

```bash
# 1. Clone
 git clone https://github.com/lupintic/groksito-discord-bot.git
 cd groksito-discord-bot

# 2. Virtual environment
python -m venv .venv
source .venv/bin/activate   # Linux / macOS
# .venv\Scripts\activate     # Windows

# 3. Install dependencies
pip install -r requirements.txt

# 4. Configure environment
cp .env.example .env
# Edit .env with your real tokens (NEVER commit .env!)

# 5. Run the bot
python -m src.bot   # or the main entrypoint of the project
```

## 📖 Usage

Once running, Groksito exposes its tools via an MCP-compatible interface or HTTP endpoint that Grok (or your custom agents) can call.

Example prompt you can give to Grok:
> "Usa las tools de Pantsu para crear un embed épico de gaming estilo SovietNoWaifu en el canal #general del servidor y pon una canción de Led Zeppelin en Spotify."

See `examples/` folder (if present) or the tool schemas in `tools/` for full list of available functions and their parameters.

## 🏗️ Architecture

See the detailed [ARCHITECTURE.md](./ARCHITECTURE.md) for component breakdown, data flow, tech stack and extension guide.

## 🛠️ Development

- Tools live in `tools/discord_tools.py` and `tools/spotify_tools.py`
- Embed builders in `embeds/`
- Core bot logic and tool registry in `core/` or `src/`
- Configuration via Pydantic + dotenv

To add a new tool:
1. Implement the function in the appropriate tools file
2. Define its name, description and JSON Schema parameters
3. Register it in the central ToolRegistry
4. (Optional) Add visual embed support

## 📄 License

This project is licensed under the **MIT License** — see the [LICENSE](./LICENSE) file for details.

You are free to use, modify, fork and distribute it.

## 🤝 Contributing

Contributions, bug reports and feature ideas are very welcome!

1. Fork the repository
2. Create your feature branch (`git checkout -b feature/amazing-new-tool`)
3. Commit your changes
4. Push to the branch and open a Pull Request

Please keep code style consistent and add tests when possible.

## 🙏 Credits & Acknowledgments

- Built with heavy assistance from Grok Build and iterative prompting
- Inspired by real-world automation needs (Discord backoffice, content creation, gaming embeds)
- Thanks to the xAI team for the amazing Grok models

---

**Status**: Active development. Ready for self-hosting and experimentation by other developers.
Made with ❤️ by [@lupintic](https://github.com/lupintic) in Santiago, Chile.