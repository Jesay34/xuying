# 序影 Xuying

> **中文**：一个面向 NAS 的自托管 Telegram 媒体归档与整理工具，支持原文件保留、硬链接整理、XMP 元数据、历史补全，以及 Immich 外部库与相册同步。  
> **English**: A self-hosted NAS service for Telegram media archiving and organization, with original-file preservation, hard-link libraries, XMP metadata, resumable history backfill, and Immich synchronization.

**当前版本 / Current version:** `1.0.0-alpha.60`<br>
**语言 / Language:** 中文 + English · [English-only README](README_EN.md)

---

## 项目简介 / Introduction

### 中文

序影适合希望把自己**有权访问和保存**的 Telegram 媒体长期归档到 NAS 的用户。它不会为了整理而移动或重复复制原始媒体：原始文件保留在 `raw/`，整理结果通过**硬链接**生成到 `library/`，再交给 Immich 浏览和管理。

项目重点不是“下载后重新编码”，而是尽可能保持原文件不变，同时把 Telegram 消息顺序、相册分组、文件名线索、相机序列与 EXIF/XMP 信息组织成更适合长期浏览的媒体库。

### English

Xuying is designed for users who want to archive Telegram media that they are **authorized to access and store** on a NAS. It does not move or duplicate source media for organization: originals remain under `raw/`, while the organized view is created under `library/` using **hard links**, ready for Immich to index and browse.

The project focuses on preserving original files while turning Telegram message order, albums, filename hints, camera sequence information, and EXIF/XMP metadata into a stable long-term media library.

---

## 主要功能 / Highlights

- **Telegram 实时监听与历史补全** / Live channel monitoring and resumable history backfill
- **Telegram 用户账号登录** / Telegram user-account login
- **Bot 转发下载独立归档** / Isolated archive for forwarded Bot media
- **相同 `Chat ID + Message ID` 并发互斥**，避免监听与补全重复落盘 / Mutual exclusion for identical `Chat ID + Message ID` to prevent duplicate writes
- **Telegram 相册分组与自定义标记分组** / Telegram album grouping and marker-based batch boundaries
- **原始媒体保留在 `raw/`** / Original media preserved under `raw/`
- **整理库使用硬链接**，不重复占用一份媒体空间 / Organized libraries use hard links instead of duplicate media copies
- **稳定排序**：结合消息顺序、文件名与相机序列等信息 / Stable ordering using message order, filename hints, and camera sequence metadata
- **XMP sidecar 生成** / XMP sidecar generation
- **Immich 外部库扫描与相册同步** / Immich external-library scanning and album synchronization
- **Live Photo 兼容处理** / Live Photo-aware synchronization
- **Web 管理界面**，支持暂停、恢复与可恢复的历史任务 / Web UI with pause, resume, and recoverable history jobs

---

## 使用方法 / Quick Start

### 1. 克隆项目 / Clone the repository

```bash
git clone https://github.com/Jesay34/xuying.git
cd xuying
cp .env.example .env
```

**中文：** 默认持久化数据保存在项目目录下的 `./data/`。NAS 用户可以编辑 `.env`，把 `XUYING_MEDIA_DIR` 和 `XUYING_CONFIG_DIR` 改成自己的绝对路径。  
**English:** Persistent data is stored under `./data/` by default. NAS users can edit `.env` and set `XUYING_MEDIA_DIR` and `XUYING_CONFIG_DIR` to their preferred absolute host paths.

> **重要 / Important:** `raw/` 与 `library/` 必须位于同一个文件系统或挂载点，硬链接才能正常工作。  
> `raw/` and `library/` must reside on the same filesystem or mount for hard links to work.

### 2. 启动容器 / Start the container

```bash
docker compose up -d --build
```

浏览器访问 / Open in a browser:

```text
http://<NAS-IP>:3434
```

**中文：** 首次启动不会自动连接 Telegram。进入 Web 设置页，填写你自己的 Telegram `API ID`、`API Hash` 和手机号，完成账号登录后，再从账号可访问的频道中添加监听频道。  
**English:** Xuying does not connect to Telegram automatically on first launch. Open the Web settings page, enter your own Telegram `API ID`, `API Hash`, and phone number, complete account login, then add channels that your account is authorized to access.

### 3. 中国大陆网络镜像（可选） / Mainland China mirrors (optional)

默认使用官方 `python:3.12-slim` 与 PyPI。若访问较慢，可在 `.env` 中启用镜像：  
Official `python:3.12-slim` and PyPI are used by default. If access is slow, optional mirrors can be configured in `.env`:

```dotenv
PYTHON_IMAGE=m.daocloud.io/docker.io/library/python:3.12-slim
PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
```

### 4. 查看运行状态 / Check status

```bash
docker compose ps
docker compose logs -f
```

停止 / Stop:

```bash
docker compose down
```

更新代码后重新构建 / Rebuild after updating the code:

```bash
docker compose up -d --build
```

---

## 数据目录 / Storage Layout

```text
/media/xydown/
├── raw/                 # 频道原始下载 / original channel downloads
├── library/             # 整理后的硬链接与 XMP / organized hard links + XMP
├── rebuild/             # 历史补全任务状态 / history-job state
└── forwarded/
    ├── raw/             # Bot 转发原始下载 / forwarded originals
    └── library/         # Bot 转发整理库 / forwarded organized library

/config/
├── config.yaml          # 运行配置 / runtime configuration
├── secrets.env          # 私密凭据 / private credentials
├── xuying.db            # SQLite database
└── sessions/            # Telegram sessions
```

**中文：** `config.yaml`、`secrets.env`、数据库、Telegram Session 和媒体文件都不应该上传到 GitHub。  
**English:** `config.yaml`, `secrets.env`, databases, Telegram sessions, and media files must never be published to GitHub.

这些内容已加入 `.gitignore` / `.dockerignore`，但在提交 Issue、截图或日志前仍应主动检查并打码。  
They are excluded by `.gitignore` / `.dockerignore`, but you should still redact private data before posting Issues, screenshots, or logs.

---

## Telegram 分组规则 / Telegram Grouping

### 中文

序影支持两种主要分组方式：

1. **Telegram 相册模式**：按照 Telegram media group 保持同一批资源。
2. **标记模式**：可以把特定文案作为新批次起点，例如文案严格等于 `1` 的媒体作为新人物/批次开始，直到下一条标记出现。

不同 Telegram 消息不会做内容级的“全局去重”。即使两个 Message ID 内容相同，也会分别保留；只有完全相同的 `Chat ID + Message ID` 会互斥复用，用于避免实时监听和历史补全同时写入同一条消息。

### English

Xuying supports two primary grouping strategies:

1. **Telegram album mode**: preserve Telegram media groups as batches.
2. **Marker mode**: treat a specific caption as the start of a new batch, for example a caption exactly equal to `1`, until the next marker appears.

Xuying does not perform content-level global deduplication across different Telegram messages. Two different Message IDs are preserved even when their files are identical. Only the exact same `Chat ID + Message ID` is mutually excluded to prevent live monitoring and history backfill from writing the same message concurrently.

---

## Immich 接入 / Immich Integration

**中文：** 建议只把整理后的 `library/` 挂载给 Immich，不要同时把 `raw/` 与整理库都加入外部库，否则会看到看似重复的媒体。  
**English:** Mount only the organized `library/` paths into Immich. Avoid indexing both `raw/` and the organized hard-link library unless duplicate-looking assets are intentional.

示例 / Example:

```yaml
- /path/to/xuying-media/library:/external/xuying-main:ro
- /path/to/xuying-media/forwarded/library:/external/xuying-forwarded:ro
```

然后在 Immich 中分别创建外部库，并在序影的 Immich 设置页填写对应的容器内路径：  
Create matching external libraries in Immich, then configure the corresponding container-side paths in Xuying:

```text
频道整理库 / Channel library: /external/xuying-main
Bot 整理库 / Forwarded library: /external/xuying-forwarded
```

序影可以通知 Immich 扫描外部库，并根据整理目录校正相册成员。  
Xuying can trigger Immich external-library scans and reconcile album membership from the organized directory structure.

---

## 安全与隐私 / Security & Privacy

- **不要把 3434 端口直接暴露到公网。** / **Do not expose port 3434 directly to the Internet.**
- Web 管理界面当前没有面向公网设计的认证层，请放在可信局域网，或由自己的 VPN / 带认证反向代理保护。 / The Web UI currently has no Internet-facing authentication layer; keep it on a trusted LAN or protect it with your own VPN/authenticated reverse proxy.
- Telegram Session 应视为登录凭据的一部分。 / Treat Telegram session files as account credentials.
- 不要在 GitHub Issue、日志或截图中公开手机号、API Key、API Hash、频道 ID、本地路径或 Session。 / Never publish phone numbers, API keys, API hashes, channel IDs, local paths, or session data in GitHub Issues, logs, or screenshots.
- 项目不包含分析统计或遥测代码。 / Xuying does not include analytics or telemetry code.

更多说明 / More details: [SECURITY.md](SECURITY.md) · [docs/PRIVACY.md](docs/PRIVACY.md)

---

## 项目结构 / Project Structure

```text
app/
├── api.py                 # FastAPI pages and API
├── config.py              # configuration and local secret persistence
├── database.py            # SQLite / SQLAlchemy
├── models.py              # data models
├── services/
│   ├── telegram.py        # Telegram login, monitoring, backfill and download
│   ├── organizer.py       # hard-link organization
│   ├── rebuild.py         # history backfill and repair
│   ├── immich.py          # Immich API
│   └── immich_sync.py     # scan and album-sync orchestration
├── templates/             # Web pages
└── static/                # Web assets and icons
```

架构说明 / Architecture: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)

---

## 开发与发布检查 / Development & Release Checks

```bash
python -m compileall -q app
python scripts/check_public_release.py
```

**中文：** GitHub Actions 会执行基础代码检查、公开发布隐私检查与 Docker 构建验证。  
**English:** GitHub Actions performs basic code checks, public-release privacy checks, and Docker build validation.

---

## 项目状态 / Project Status

**中文：** 序影目前仍为 Alpha 软件。现阶段优先保证原始媒体安全、目录行为透明、排序可恢复和长期稳定性，而不是快速增加功能。  
**English:** Xuying is currently alpha software. Preservation of original media, transparent storage behavior, recoverable ordering, and long-term reliability take priority over rapid feature expansion.

路线图 / Roadmap: [ROADMAP.md](ROADMAP.md)

---

## 贡献 / Contributing

欢迎提交 Issue 和 Pull Request。请先阅读 [CONTRIBUTING.md](CONTRIBUTING.md)。  
Issues and pull requests are welcome. Please read [CONTRIBUTING.md](CONTRIBUTING.md) first.

提交问题时，请务必删除私人凭据、频道 ID、本地路径、手机号和账号数据。  
Always redact credentials, channel IDs, local paths, phone numbers, and account data before submitting reports.

---

## 致谢与项目沿革 / Upstream Lineage

**中文：** 序影的 Telegram 下载实现沿着 `hermes-telegram-downloader` / `telegram_media_downloader` 的开源谱系继续演进。相关 MIT 版权声明已保留。  
**English:** Xuying's Telegram download implementation evolved from the open-source `hermes-telegram-downloader` / `telegram_media_downloader` lineage. Required MIT notices are preserved.

详见 / See: [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)

---

## 合法使用 / Responsible Use

**中文：** 序影仅提供媒体归档与整理能力。请只保存你拥有权利、获得授权或平台规则允许下载的内容，并遵守所在地法律、Telegram 条款以及相关版权和隐私要求。  
**English:** Xuying only provides media archiving and organization capabilities. Only archive content you are authorized to access and store, and follow applicable law, Telegram's terms, and relevant copyright/privacy obligations.

---

## License

[MIT License](LICENSE)
