from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import httpx

from app.config import Settings

logger = logging.getLogger(__name__)

# Immich imports every extension in its own image/video maps
# (server/src/utils/mime-types.ts). Xuying previously enumerated only 16 of
# them, so any other importable file was hardlinked, indexed by Immich, and
# then silently skipped here — the album was short with no missing count.
IMAGE_EXTENSIONS = frozenset({
    ".avif", ".bmp", ".gif", ".jpeg", ".jpg", ".png", ".webp",
    ".heic", ".heif", ".hif", ".insp", ".jp2", ".jpe", ".jxl",
    ".mpo", ".svg", ".tif", ".tiff",
})
RAW_EXTENSIONS = frozenset({
    ".3fr", ".ari", ".arw", ".cap", ".cin", ".cr2", ".cr3", ".crw",
    ".dcr", ".dng", ".erf", ".fff", ".iiq", ".k25", ".kdc", ".mrw",
    ".nef", ".nrw", ".orf", ".ori", ".pef", ".psd", ".raf", ".raw",
    ".rw2", ".rwl", ".sr2", ".srf", ".srw", ".x3f",
})
VIDEO_EXTENSIONS = frozenset({
    ".3gp", ".3gpp", ".avi", ".flv", ".insv", ".m2t", ".m2ts", ".m4v",
    ".mkv", ".mov", ".mp4", ".mpe", ".mpeg", ".mpg", ".mts", ".mxf",
    ".ts", ".vob", ".webm", ".wmv",
})
MEDIA_EXTENSIONS = IMAGE_EXTENSIONS | RAW_EXTENSIONS | VIDEO_EXTENSIONS
SIDECAR_EXTENSIONS = frozenset({".xmp"})


class ImmichClient:
    """Small client around Immich's stable external-library and album APIs."""

    def __init__(self, settings: Settings):
        self.settings = settings
        # First failure reason from the most recent unpair attempt, so the UI
        # can explain a refusal instead of only saying it did not work.
        self._last_unpair_error: str = ""

    @property
    def configured(self) -> bool:
        return bool(self.settings.immich.enabled and self.settings.immich_api_key)

    @property
    def api_base_url(self) -> str:
        base = self.settings.immich.base_url.rstrip("/")
        return base if base.endswith("/api") else f"{base}/api"

    def _client(self, timeout: float = 20) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self.api_base_url,
            headers={"x-api-key": self.settings.immich_api_key or ""},
            timeout=timeout,
        )

    def _require_configured(self) -> None:
        if not self.configured:
            raise RuntimeError("请先保存 Immich 地址和 API Key")

    async def settings_state(self) -> dict:
        config = self.settings.immich
        return {
            "enabled": config.enabled,
            "configured": self.configured,
            "base_url": config.base_url,
            "api_key_saved": bool(self.settings.immich_api_key),
            "library_id": config.library_id,
            "external_library_path": config.external_library_path,
            "auto_scan": config.auto_scan,
            "auto_album": config.auto_album,
            "album_prefix": config.album_prefix,
            "forwarded_library_id": config.forwarded_library_id,
            "forwarded_external_library_path": (
                config.forwarded_external_library_path
            ),
            "forwarded_auto_scan": config.forwarded_auto_scan,
            "forwarded_auto_album": config.forwarded_auto_album,
            "forwarded_auto_archive": config.forwarded_auto_archive,
            "forwarded_album_prefix": config.forwarded_album_prefix,
        }

    async def server_info(self) -> dict:
        if not self.configured:
            return {"status": "disabled"}
        async with self._client() as client:
            response = await client.get("/server/about")
            response.raise_for_status()
            return {"status": "connected", **response.json()}

    async def libraries(self) -> list[dict]:
        self._require_configured()
        async with self._client() as client:
            response = await client.get("/libraries")
            response.raise_for_status()
            data = response.json()
            libraries = (
                data
                if isinstance(data, list)
                else data.get("libraries", [])
            )

            async def include_statistics(library: dict) -> dict:
                item = dict(library)
                library_id = str(item.get("id") or "").strip()
                if not library_id:
                    item["assetCount"] = int(item.get("assetCount") or 0)
                    return item

                try:
                    statistics_response = await client.get(
                        f"/libraries/{library_id}/statistics"
                    )
                    statistics_response.raise_for_status()
                    statistics = statistics_response.json()
                    item["statistics"] = statistics
                    item["assetCount"] = int(
                        statistics.get("total")
                        or statistics.get("photos", 0)
                        + statistics.get("videos", 0)
                    )
                    return item
                except httpx.HTTPError:
                    # Some restricted API keys cannot read statistics. The
                    # single-library response still exposes assetCount.
                    logger.debug(
                        "Immich library statistics unavailable for %s",
                        library_id,
                        exc_info=True,
                    )

                try:
                    detail_response = await client.get(
                        f"/libraries/{library_id}"
                    )
                    detail_response.raise_for_status()
                    detail = detail_response.json()
                    item["assetCount"] = int(
                        detail.get("assetCount")
                        or item.get("assetCount")
                        or 0
                    )
                except httpx.HTTPError:
                    item["assetCount"] = int(item.get("assetCount") or 0)
                return item

            return list(
                await asyncio.gather(
                    *(include_statistics(library) for library in libraries)
                )
            )

    async def scan_library(self) -> None:
        self._require_configured()
        library_id = self.settings.immich.library_id.strip()
        if not library_id:
            raise RuntimeError("请先选择 Immich 外部库")
        async with self._client(timeout=60) as client:
            response = await client.post(f"/libraries/{library_id}/scan")
            response.raise_for_status()

    async def scan_forwarded_library(self) -> None:
        self._require_configured()
        library_id = self.settings.immich.forwarded_library_id.strip()
        if not library_id:
            raise RuntimeError("请先选择机器人下载专用 Immich 外部库")
        async with self._client(timeout=60) as client:
            response = await client.post(f"/libraries/{library_id}/scan")
            response.raise_for_status()

    async def _albums(self) -> list[dict]:
        async with self._client() as client:
            response = await client.get("/albums")
            response.raise_for_status()
            data = response.json()
            return data if isinstance(data, list) else data.get("albums", [])

    @staticmethod
    def _normalize_asset_path(value: str) -> str:
        """Normalize an Immich path without weakening path identity."""
        normalized = str(value or "").replace("\\", "/").rstrip("/")
        return "/" + normalized.lstrip("/") if normalized else ""

    @staticmethod
    def _rejected_bulk_ids(response: httpx.Response) -> list[str]:
        """Return ids Immich refused in a BulkIdResponseDto list.

        Immich answers 200 with a per-asset success flag, so an unchecked
        status code hides individual refusals. `duplicate` means the asset is
        already a member, which is the desired end state, not a failure.
        """
        try:
            payload = response.json()
        except ValueError:
            return []
        if not isinstance(payload, list):
            return []
        rejected: list[str] = []
        for entry in payload:
            if not isinstance(entry, dict) or entry.get("success"):
                continue
            if str(entry.get("error") or "").lower() == "duplicate":
                continue
            asset_id = str(entry.get("id") or "").strip()
            rejected.append(
                f"{asset_id}:{entry.get('error') or 'unknown'}"
                if asset_id
                else str(entry.get("error") or "unknown")
            )
        return rejected

    async def _asset_id_for_path(
        self,
        client: httpx.AsyncClient,
        original_path: str,
        *,
        library_id: str | None = None,
    ) -> str | None:
        """Resolve one exact path; never use an unrelated search result."""
        response = await client.post(
            "/search/metadata",
            json={
                "libraryId": library_id or self.settings.immich.library_id or None,
                "originalPath": original_path,
                "page": 1,
                "size": 20,
            },
        )
        response.raise_for_status()
        assets = response.json().get("assets", {})
        items = assets.get("items", []) if isinstance(assets, dict) else []
        expected = self._normalize_asset_path(original_path)
        for item in items:
            if self._normalize_asset_path(item.get("originalPath", "")) == expected:
                return item.get("id")
        return None

    async def _library_asset_index(
        self,
        client: httpx.AsyncClient,
        library_id: str,
    ) -> tuple[dict[str, str], set[str], list[tuple[str, str]]]:
        """Index one external library by exact path.

        Also returns the Live Photo pairs Immich has made: the motion half ids
        (every asset referenced as another asset's `livePhotoVideoId`) and the
        photo/motion id pairs themselves. Pairing hides the video half and
        calls `removeAssetsFromAll` on it, which both shortens the album and
        leaves the merged item unplayable, so the pairs are undone rather than
        merely tolerated.
        """
        index: dict[str, str] = {}
        motion_ids: set[str] = set()
        live_pairs: list[tuple[str, str]] = []
        page = 1
        page_size = 1000
        # Hard stop: a server that ignored `page` and always answered with a
        # full page would otherwise spin forever.
        max_pages = 2000
        while page <= max_pages:
            response = await client.post(
                "/search/metadata",
                json={"libraryId": library_id, "page": page, "size": page_size},
            )
            response.raise_for_status()
            assets = response.json().get("assets", {})
            items = assets.get("items", []) if isinstance(assets, dict) else []
            for item in items:
                asset_id = str(item.get("id") or "").strip()
                original_path = self._normalize_asset_path(
                    item.get("originalPath", "")
                )
                if asset_id and original_path:
                    index[original_path] = asset_id
                motion_id = str(item.get("livePhotoVideoId") or "").strip()
                if motion_id:
                    motion_ids.add(motion_id)
                    if asset_id:
                        live_pairs.append((asset_id, motion_id))
            next_page = assets.get("nextPage") if isinstance(assets, dict) else None
            if next_page not in (None, ""):
                try:
                    next_value = int(next_page)
                except (TypeError, ValueError):
                    next_value = page + 1
                if next_value <= page:
                    break
                page = next_value
                continue
            # Immich reports `total` as the size of the current page, never the
            # overall match count, so it must not gate pagination. A full page
            # without nextPage is still followed by one more probe request.
            if len(items) >= page_size:
                page += 1
                continue
            break
        return index, motion_ids, live_pairs

    async def _album_assets(
        self,
        client: httpx.AsyncClient,
        album_id: str,
    ) -> list[dict]:
        """Read every member of one Immich v3 album.

        Immich v3's GET /albums/{id} returns album metadata only. Album
        membership must be queried through POST /search/metadata with
        albumIds, otherwise an existing album is incorrectly seen as empty.

        Omitting `visibility` is deliberate. Immich substitutes `not-locked`,
        which becomes `visibility != 'locked'`, so hidden Live Photo videos
        are included. Passing `visibility: 'timeline'` here would hide the
        video half of every Live Photo and make it look permanently missing.
        """
        items_by_id: dict[str, dict] = {}
        page = 1
        page_size = 1000
        max_pages = 2000
        while page <= max_pages:
            response = await client.post(
                "/search/metadata",
                json={
                    "albumIds": [album_id],
                    "page": page,
                    "size": page_size,
                },
            )
            response.raise_for_status()
            assets = response.json().get("assets", {})
            items = assets.get("items", []) if isinstance(assets, dict) else []
            for item in items:
                if not isinstance(item, dict):
                    continue
                asset_id = str(item.get("id") or "").strip()
                if asset_id:
                    items_by_id[asset_id] = item
            next_page = assets.get("nextPage") if isinstance(assets, dict) else None
            if next_page not in (None, ""):
                try:
                    next_value = int(next_page)
                except (TypeError, ValueError):
                    next_value = page + 1
                if next_value <= page:
                    break
                page = next_value
                continue
            # `total` is this page's length in Immich's search response, so it
            # cannot be used to decide whether another page exists.
            if len(items) >= page_size:
                page += 1
                continue
            break
        return list(items_by_id.values())

    async def _album_hidden_asset_ids(
        self,
        client: httpx.AsyncClient,
        album_id: str,
    ) -> set[str]:
        """Return album members Immich marks `hidden`, best effort.

        Immich uses `hidden` for the video half of a Live Photo or Motion
        Photo. Those members exist and are correct, but the web timeline
        filters them out, so the album looks shorter than the hardlink
        directory. Reporting the split is the only way to tell that apart
        from real data loss.

        Only `visibility: 'hidden'` is attempted. The search DTO is Zod
        based and strips unknown keys instead of rejecting them, so probing
        older field names such as `isVisible` would silently return every
        member and mislabel all of them as hidden. An empty set on failure
        simply means the split is not reported.
        """
        hidden: set[str] = set()
        page = 1
        page_size = 1000
        while page <= 2000:
            try:
                response = await client.post(
                    "/search/metadata",
                    json={
                        "albumIds": [album_id],
                        "page": page,
                        "size": page_size,
                        "visibility": "hidden",
                    },
                )
                response.raise_for_status()
            except httpx.HTTPError:
                return set()
            assets = response.json().get("assets", {})
            items = assets.get("items", []) if isinstance(assets, dict) else []
            for item in items:
                if isinstance(item, dict):
                    asset_id = str(item.get("id") or "").strip()
                    if asset_id:
                        hidden.add(asset_id)
            next_page = assets.get("nextPage") if isinstance(assets, dict) else None
            if next_page not in (None, ""):
                try:
                    next_value = int(next_page)
                except (TypeError, ValueError):
                    next_value = page + 1
                if next_value > page:
                    page = next_value
                    continue
            if len(items) >= page_size:
                page += 1
                continue
            break
        return hidden

    def _asset_from_index(
        self,
        index: dict[str, str],
        expected_path: str,
        relative_path: str,
    ) -> tuple[str | None, bool, bool]:
        """Return (asset id, used unique-relative match, ambiguous)."""
        exact = index.get(self._normalize_asset_path(expected_path))
        if exact:
            return exact, False, False
        suffix = "/" + str(relative_path).replace("\\", "/").lstrip("/")
        candidates = {
            asset_id
            for path_value, asset_id in index.items()
            if path_value.endswith(suffix)
        }
        if len(candidates) == 1:
            return next(iter(candidates)), True, False
        return None, False, len(candidates) > 1

    def _immich_path(self, local_path: Path) -> str:
        relative = local_path.relative_to(self.settings.storage.library_path)
        root = self.settings.immich.external_library_path.rstrip("/")
        return f"{root}/{relative.as_posix()}"

    def _forwarded_immich_path(self, local_path: Path) -> str:
        relative = local_path.relative_to(
            self.settings.storage.forwarded_library_path
        )
        root = self.settings.immich.forwarded_external_library_path.rstrip("/")
        return f"{root}/{relative.as_posix()}"

    async def _unpair_live_photos(
        self,
        client: httpx.AsyncClient,
        live_pairs: list[tuple[str, str]],
    ) -> set[str]:
        """Split Immich's Live Photo pairs back into two ordinary assets.

        Merging is actively harmful for this library. `linkLivePhotos` hides
        the video half, drops it from every album, and leaves the merged item
        unplayable on both web and mobile: the hidden half is skipped by
        `handleGenerateThumbnails` outright, and skipped by the transcode queue
        too because that visibility filter sits inside `$if(!force, ...)`. The
        photo half then points at a video with neither poster nor transcode.
        A standalone MOV plays fine, so unpairing turns a broken merged item
        back into a working one.

        `PUT /assets/{id}` takes `livePhotoVideoId: null` to detach, and
        `visibility: 'timeline'` to undo the hide. Order matters: detach first,
        so nothing re-hides the video between the two calls. Returns the motion
        ids successfully freed, so the caller can treat the rest as still
        merged instead of assuming success.
        """
        freed: set[str] = set()
        for photo_id, motion_id in live_pairs:
            try:
                await self._update_asset(
                    client, photo_id, {"livePhotoVideoId": None}
                )
                await self._update_asset(
                    client, motion_id, {"visibility": "timeline"}
                )
            except httpx.HTTPError as error:
                # Leave it merged rather than half-detached; the next pass
                # retries because the pair is still reported by the index.
                reason = self._http_error_reason(error)
                logger.warning(
                    "拆分 Live Photo 失败（照片 %s / 视频 %s）：%s",
                    photo_id,
                    motion_id,
                    reason,
                )
                if not self._last_unpair_error:
                    # Surfaced in the UI: a permission-scoped API key is the
                    # usual cause, and a log line nobody reads hides that.
                    self._last_unpair_error = reason
                continue
            freed.add(motion_id)
        if freed:
            # Now that they are visible again, thumbnails will actually
            # generate instead of returning Skipped.
            await self._request_asset_job(
                client, sorted(freed), "regenerate-thumbnail"
            )
        return freed

    async def _reveal_motion_halves(
        self,
        client: httpx.AsyncClient,
        motion_ids: list[str],
    ) -> set[str]:
        """Un-hide album members Immich merged into a Live Photo.

        The photo's id is not needed. Immich hides the video half on pairing,
        and hidden is exactly what breaks it: `handleGenerateThumbnails`
        returns Skipped, and the transcode queue's visibility filter sits
        inside `$if(!force, ...)`, so the half ends up with neither poster nor
        transcode and the merged item will not play on web or mobile. Setting
        visibility back to `timeline` restores both jobs and makes the video a
        normal, playable timeline item. The photo may keep pointing at it,
        which is harmless once the video itself works.

        Detected from the per-album hidden probe rather than the library index,
        because Immich's search rows do not carry `livePhotoVideoId`.
        """
        if not motion_ids:
            return set()
        revealed: set[str] = set()
        for motion_id in motion_ids:
            try:
                await self._update_asset(
                    client, motion_id, {"visibility": "timeline"}
                )
            except httpx.HTTPError as error:
                reason = self._http_error_reason(error)
                logger.warning("取消隐藏 Live Photo 视频 %s 失败：%s", motion_id, reason)
                if not self._last_unpair_error:
                    self._last_unpair_error = reason
                continue
            revealed.add(motion_id)
        if revealed:
            ordered = sorted(revealed)
            # Now visible, so neither job is skipped any more.
            await self._request_asset_job(client, ordered, "regenerate-thumbnail")
            await self._request_asset_job(client, ordered, "transcode-video")
            logger.info("已取消隐藏 %d 个 Live Photo 视频部分并补发缩略图/转码", len(revealed))
        return revealed

    @staticmethod
    def _http_error_reason(error: httpx.HTTPError) -> str:
        """Turn an httpx error into something worth showing a user."""
        response = getattr(error, "response", None)
        if response is None:
            return str(error) or error.__class__.__name__
        detail = ""
        try:
            payload = response.json()
        except ValueError:
            detail = (response.text or "").strip()[:200]
        else:
            if isinstance(payload, dict):
                detail = str(
                    payload.get("message") or payload.get("error") or ""
                ).strip()[:200]
        return f"HTTP {response.status_code}{' ' + detail if detail else ''}"

    async def _update_asset(
        self,
        client: httpx.AsyncClient,
        asset_id: str,
        payload: dict,
    ) -> None:
        """Update one asset, preferring v3's PATCH over the deprecated PUT.

        v3.1.0 exposes both `@Patch(':id')` (the v3 successor) and
        `@Put(':id')` (deprecated). PATCH is tried first; only a routing
        rejection falls back, so a real error such as 403 still propagates
        instead of being retried against the older verb.
        """
        response = await client.patch(f"/assets/{asset_id}", json=payload)
        if response.status_code in (404, 405, 501):
            response = await client.put(f"/assets/{asset_id}", json=payload)
        response.raise_for_status()

    async def _request_asset_job(
        self,
        client: httpx.AsyncClient,
        asset_ids: list[str],
        job_name: str,
    ) -> int:
        """Queue a per-asset job, chunked, tolerating a refused batch."""
        queued = 0
        for start in range(0, len(asset_ids), 100):
            chunk = asset_ids[start : start + 100]
            try:
                response = await client.post(
                    "/assets/jobs",
                    json={"assetIds": chunk, "name": job_name},
                )
                response.raise_for_status()
            except httpx.HTTPError as error:
                logger.warning("Immich %s 请求失败：%s", job_name, error)
                continue
            queued += len(chunk)
        return queued

    async def request_video_transcode(self, asset_ids: list[str]) -> int:
        """Queue a transcode for Live Photo motion halves. Returns the count.

        Immich pairs a Live Photo by setting the video half to `hidden`, and
        its own transcode queue excludes hidden assets: the visibility filter
        in `streamForVideoConversion` sits inside `$if(!force, ...)`. A motion
        half paired before its transcode ran is therefore skipped forever, and
        `playbackVideo` falls back to `encodedVideoPath || originalPath` with
        no error branch, so the browser receives the raw iPhone HEVC file and
        spins. Per-asset `POST /assets/jobs` is the way around it: that path
        reaches `getForVideoConversion(id)`, which filters on id and type only.

        Thumbnails deliberately are not requested. `handleGenerateThumbnails`
        returns Skipped for hidden assets inside the handler, so queueing it
        would be wasted work rather than a second fix.
        """
        self._require_configured()
        wanted = [str(asset_id).strip() for asset_id in asset_ids]
        wanted = [asset_id for asset_id in wanted if asset_id]
        if not wanted:
            return 0
        async with self._client(timeout=60) as client:
            return await self._request_asset_job(client, wanted, "transcode-video")

    async def sync_subject_albums(
        self,
        *,
        refresh_metadata: bool = False,
        metadata_refreshed_asset_ids: set[str] | None = None,
        unmerged_motion_ids: set[str] | None = None,
    ) -> dict:
        self._require_configured()
        library_id = self.settings.immich.library_id.strip()
        if not library_id:
            raise RuntimeError("请先选择 Immich 外部库并完成扫描")

        subject_dirs = sorted(
            path
            for path in self.settings.storage.library_path.rglob("subjects/*")
            if path.is_dir()
        )
        existing = {
            album.get("albumName"): album.get("id")
            for album in await self._albums()
            if album.get("albumName") and album.get("id")
        }
        created = 0
        updated = 0
        missing_assets = 0
        ambiguous_assets = 0
        relative_path_matches = 0
        merged_legacy_albums = 0
        removed_album_assets = 0
        repaired_album_assets = 0
        album_member_missing_assets = 0
        album_member_assets = 0
        album_timeline_assets = 0
        collapsed_live_photo_assets = 0
        album_verification_errors = 0
        unsupported_assets = 0
        unsupported_examples: list[str] = []
        deleted_album_ids: set[str] = set()
        metadata_asset_ids: set[str] = set()
        motion_asset_ids: set[str] = set()
        album_reports: list[dict] = []
        async with self._client(timeout=120) as client:
            # One authoritative index prevents the old per-file search fallback
            # from ever attaching an unrelated asset to a person's album.
            asset_index, motion_ids, live_pairs = await self._library_asset_index(
                client, library_id
            )
            # Undo Immich's Live Photo merging before album membership is
            # computed, so the freed videos are treated as ordinary assets and
            # land back in their person album in the same pass.
            self._last_unpair_error = ""
            unpaired_live_photos = await self._unpair_live_photos(client, live_pairs)
            motion_ids -= unpaired_live_photos
            # Photo halves whose partner was just detached must also skip
            # metadata refresh: `linkLivePhotos` runs on EITHER half and would
            # re-discover the matching filename pair immediately.
            unpaired_photo_halves = {
                photo_id
                for photo_id, motion_id in live_pairs
                if motion_id in unpaired_live_photos
            }
            # Collected per album below, because the library index cannot
            # identify motion halves on its own.
            hidden_motion_ids: set[str] = set()
            for subject_dir in subject_dirs:
                relative = subject_dir.relative_to(self.settings.storage.library_path)
                channel_name = relative.parts[0] if relative.parts else "频道"
                marker_id = (
                    subject_dir.name.split("_", 1)[1]
                    if "_" in subject_dir.name
                    else subject_dir.name
                )
                if marker_id.startswith("S") and marker_id[1:].isdigit():
                    marker_id = marker_id[1:]
                album_name = (
                    f"{self.settings.immich.album_prefix} · "
                    f"{channel_name} · 人物 M{marker_id}"
                )
                legacy_album_name = (
                    f"{self.settings.immich.album_prefix} · "
                    f"{channel_name} · 人物 MS{marker_id}"
                )

                asset_ids: list[str] = []
                seen_asset_ids: set[str] = set()
                subject_missing_assets = 0
                subject_unsupported_assets = 0
                subject_hardlinked = 0
                subject_motion_halves = 0
                subject_suffix = "/" + relative.as_posix().strip("/") + "/"
                for media in sorted(subject_dir.iterdir()):
                    if not media.is_file():
                        continue
                    suffix = media.suffix.lower()
                    if suffix in SIDECAR_EXTENSIONS:
                        continue
                    if suffix not in MEDIA_EXTENSIONS:
                        # Immich cannot import this extension at all. Report it
                        # instead of dropping it silently, otherwise the album
                        # stays short while the status claims completion.
                        unsupported_assets += 1
                        subject_unsupported_assets += 1
                        if len(unsupported_examples) < 20:
                            unsupported_examples.append(media.name)
                        continue
                    subject_hardlinked += 1
                    media_relative = media.relative_to(
                        self.settings.storage.library_path
                    ).as_posix()
                    asset_id, used_relative, ambiguous = self._asset_from_index(
                        asset_index,
                        self._immich_path(media),
                        media_relative,
                    )
                    if asset_id and asset_id in motion_ids:
                        # Immich already merged this video into its photo and
                        # dropped it from every album. Asking for it back only
                        # churns the member count until the next metadata pass
                        # removes it again, so treat it as satisfied.
                        subject_motion_halves += 1
                        collapsed_live_photo_assets += 1
                        motion_asset_ids.add(asset_id)
                        continue
                    if asset_id:
                        if asset_id not in seen_asset_ids:
                            asset_ids.append(asset_id)
                            seen_asset_ids.add(asset_id)
                        if used_relative:
                            relative_path_matches += 1
                    else:
                        missing_assets += 1
                        subject_missing_assets += 1
                        if ambiguous:
                            ambiguous_assets += 1
                # Do not create empty albums while Immich is still importing.
                if not asset_ids:
                    # Still report the directory: a row saying "0 members,
                    # N pending" is what tells the user Immich has not indexed
                    # this person yet, instead of the row simply disappearing.
                    album_reports.append(
                        {
                            "album": album_name,
                            "channel": channel_name,
                            "directory": relative.as_posix(),
                            "hardlinked": subject_hardlinked,
                            "members": 0,
                            "timeline": 0,
                            "collapsed": subject_motion_halves,
                            "missing": subject_missing_assets,
                            "unsupported": subject_unsupported_assets,
                        }
                    )
                    continue

                album_id = existing.get(album_name)
                if not album_id:
                    response = await client.post(
                        "/albums",
                        json={
                            "albumName": album_name,
                            "description": f"由序影自动同步：{relative.as_posix()}",
                        },
                    )
                    response.raise_for_status()
                    album_id = response.json()["id"]
                    existing[album_name] = album_id
                    created += 1

                # The hardlink subject directory is the source of truth.
                # Use the per-album route: it is unambiguous (the bulk
                # /albums/assets path can be shadowed by the /albums/:id route)
                # and it returns a per-asset result instead of one boolean.
                for offset in range(0, len(asset_ids), 500):
                    batch = asset_ids[offset : offset + 500]
                    response = await client.put(
                        f"/albums/{album_id}/assets",
                        json={"ids": batch},
                    )
                    response.raise_for_status()
                    rejected = self._rejected_bulk_ids(response)
                    if rejected:
                        # A 200 can still refuse individual assets. Surface it;
                        # the verification pass below decides the final count.
                        logger.warning(
                            "Immich refused %d/%d assets for album %s: %s",
                            len(rejected),
                            len(batch),
                            album_name,
                            rejected[:5],
                        )

                try:
                    desired_ids = set(asset_ids)

                    def member_sets(items: list[dict]) -> tuple[set[str], set[str]]:
                        visible_ids: set[str] = set()
                        live_partner_ids: set[str] = set()
                        for member in items:
                            if not isinstance(member, dict):
                                continue
                            member_id = str(member.get("id") or "").strip()
                            live_id = str(
                                member.get("livePhotoVideoId") or ""
                            ).strip()
                            if member_id:
                                visible_ids.add(member_id)
                            if live_id:
                                live_partner_ids.add(live_id)
                        return visible_ids, live_partner_ids

                    album_assets = await self._album_assets(client, album_id)
                    visible_ids, live_partner_ids = member_sets(album_assets)
                    hidden_ids = await self._album_hidden_asset_ids(
                        client, album_id
                    )
                    # Motion halves were already excluded from desired_ids, so
                    # anything still hidden here was paired between the library
                    # index and this read. Count it satisfied, not missing.
                    member_ids = visible_ids | live_partner_ids | hidden_ids
                    late_paired_ids = desired_ids & (hidden_ids | live_partner_ids)
                    timeline_ids = (desired_ids & visible_ids) - late_paired_ids
                    missing_member_ids = desired_ids - member_ids

                    # Retry only members that are absent from the authoritative
                    # album search result. This repairs the intermittent case in
                    # which a large bulk add accepts the request but omits videos.
                    if missing_member_ids:
                        ordered_missing = [
                            asset_id
                            for asset_id in asset_ids
                            if asset_id in missing_member_ids
                        ]
                        before_retry = len(missing_member_ids)
                        for offset in range(0, len(ordered_missing), 200):
                            batch = ordered_missing[offset : offset + 200]
                            retry = await client.put(
                                f"/albums/{album_id}/assets",
                                json={"ids": batch},
                            )
                            retry.raise_for_status()
                            rejected = self._rejected_bulk_ids(retry)
                            if rejected:
                                logger.warning(
                                    "Immich refused %d retried assets for "
                                    "album %s: %s",
                                    len(rejected),
                                    album_name,
                                    rejected[:5],
                                )
                        album_assets = await self._album_assets(client, album_id)
                        visible_ids, live_partner_ids = member_sets(album_assets)
                        hidden_ids = await self._album_hidden_asset_ids(
                            client, album_id
                        )
                        member_ids = visible_ids | live_partner_ids | hidden_ids
                        late_paired_ids = (
                            desired_ids & (hidden_ids | live_partner_ids)
                        )
                        timeline_ids = (
                            desired_ids & visible_ids
                        ) - late_paired_ids
                        missing_member_ids = desired_ids - member_ids
                        repaired_album_assets += (
                            before_retry - len(missing_member_ids)
                        )

                    if missing_member_ids:
                        album_member_missing_assets += len(missing_member_ids)
                        missing_assets += len(missing_member_ids)

                    # The library index cannot see these: Immich's search rows do
                    # not carry livePhotoVideoId, so the per-album hidden probe
                    # is the only reliable way to find merged motion halves.
                    hidden_motion_ids |= desired_ids & hidden_ids
                    collapsed = subject_motion_halves + len(late_paired_ids)
                    collapsed_live_photo_assets += len(late_paired_ids)
                    album_member_assets += len(desired_ids & member_ids)
                    album_timeline_assets += len(timeline_ids)
                    album_reports.append(
                        {
                            "album": album_name,
                            "channel": channel_name,
                            "directory": relative.as_posix(),
                            "hardlinked": subject_hardlinked,
                            "members": len(desired_ids & member_ids),
                            "timeline": len(timeline_ids),
                            "collapsed": collapsed,
                            "missing": len(missing_member_ids),
                            "unsupported": subject_unsupported_assets,
                        }
                    )
                    if collapsed or missing_member_ids:
                        # The timeline count can legitimately be lower than the
                        # hardlink count. Record the split so a short album can
                        # be told apart from real data loss without guessing.
                        logger.info(
                            "Album %s: %d hardlinked, %d members, %d on "
                            "timeline, %d merged into Live Photos, %d missing",
                            album_name,
                            subject_hardlinked,
                            len(desired_ids & member_ids),
                            len(timeline_ids),
                            collapsed,
                            len(missing_member_ids),
                        )

                    # Remove stale members only after desired membership has been
                    # verified. During indexing, remove only assets whose path
                    # clearly belongs to a different subject directory.
                    stale_asset_ids: list[str] = []
                    for item in album_assets:
                        current_id = str(item.get("id") or "").strip()
                        if not current_id:
                            continue
                        live_id = str(item.get("livePhotoVideoId") or "").strip()
                        if current_id in desired_ids or live_id in desired_ids:
                            continue
                        if (
                            subject_missing_assets == 0
                            and subject_unsupported_assets == 0
                        ):
                            stale_asset_ids.append(current_id)
                            continue
                        current_path = self._normalize_asset_path(
                            item.get("originalPath", "")
                        )
                        if current_path and subject_suffix not in current_path:
                            stale_asset_ids.append(current_id)
                    for offset in range(0, len(stale_asset_ids), 500):
                        batch = stale_asset_ids[offset : offset + 500]
                        response = await client.request(
                            "DELETE",
                            f"/albums/{album_id}/assets",
                            json={"ids": batch},
                        )
                        response.raise_for_status()
                        removed_album_assets += len(batch)
                except httpx.HTTPStatusError as exc:
                    # A verification API error is one failed album, not hundreds
                    # of missing media. Keep the pipeline retryable without
                    # presenting a false missing-media count to the user.
                    album_verification_errors += 1
                    missing_assets += 1
                    logger.warning(
                        "Unable to verify album %s members: %s",
                        album_id,
                        exc,
                    )

                # Immich orders albums by asset metadata, not filename. Xuying
                # writes descending synthetic XMP times and refreshes metadata;
                # the first desired asset is therefore also the intended cover.
                response = await client.patch(
                    f"/albums/{album_id}",
                    json={
                        "order": "desc",
                        "albumThumbnailAssetId": asset_ids[0],
                    },
                )
                response.raise_for_status()
                if refresh_metadata:
                    metadata_asset_ids.update(asset_ids)
                updated += 1

                legacy_album_id = existing.get(legacy_album_name)
                if (
                    legacy_album_id
                    and legacy_album_id != album_id
                    and legacy_album_id not in deleted_album_ids
                ):
                    response = await client.delete(f"/albums/{legacy_album_id}")
                    response.raise_for_status()
                    deleted_album_ids.add(legacy_album_id)
                    existing.pop(legacy_album_name, None)
                    merged_legacy_albums += 1

            revealed_motion = await self._reveal_motion_halves(
                client, sorted(hidden_motion_ids)
            )

            if refresh_metadata:
                already_refreshed = metadata_refreshed_asset_ids or set()
                # Never refresh a motion half that has been un-merged. Metadata
                # extraction re-runs `linkLivePhotos`, which would pair and hide
                # it again, so refreshing these is what makes the merge come
                # back on the next pass.
                protected = (
                    (unmerged_motion_ids or set())
                    | revealed_motion
                    | unpaired_live_photos
                    | unpaired_photo_halves
                )
                ordered_ids = sorted(
                    metadata_asset_ids - already_refreshed - protected
                )
                for offset in range(0, len(ordered_ids), 1000):
                    response = await client.post(
                        "/assets/jobs",
                        json={
                            "assetIds": ordered_ids[offset : offset + 1000],
                            "name": "refresh-metadata",
                        },
                    )
                    response.raise_for_status()
                if metadata_refreshed_asset_ids is not None:
                    metadata_refreshed_asset_ids.update(ordered_ids)
        return {
            "message": "Immich 人物相册同步完成",
            "created_albums": created,
            "updated_albums": updated,
            "missing_assets": missing_assets,
            "ambiguous_assets": ambiguous_assets,
            "relative_path_matches": relative_path_matches,
            "indexed_assets": len(asset_index),
            "merged_legacy_albums": merged_legacy_albums,
            "removed_album_assets": removed_album_assets,
            "repaired_album_assets": repaired_album_assets,
            "album_member_missing_assets": album_member_missing_assets,
            "album_member_assets": album_member_assets,
            "album_timeline_assets": album_timeline_assets,
            "album_reports": sorted(
                album_reports,
                key=lambda entry: (
                    -(entry["missing"] + entry["unsupported"]),
                    entry["album"],
                ),
            ),
            "collapsed_live_photo_assets": collapsed_live_photo_assets,
            "album_verification_errors": album_verification_errors,
            "unsupported_assets": unsupported_assets,
            "unsupported_examples": unsupported_examples,
            "metadata_refresh_assets": len(metadata_asset_ids),
            "motion_asset_ids": sorted(motion_asset_ids),
            "unpaired_live_photos": len(unpaired_live_photos) + len(revealed_motion),
            "unpair_error": self._last_unpair_error,
            "revealed_motion_ids": sorted(
                revealed_motion | unpaired_live_photos | unpaired_photo_halves
            ),
            "archived_assets": 0,
        }

    async def sync_forwarded_albums(self) -> dict:
        self._require_configured()
        library_id = self.settings.immich.forwarded_library_id.strip()
        if not library_id:
            raise RuntimeError("请先选择机器人下载专用 Immich 外部库并完成扫描")

        root = self.settings.storage.forwarded_library_path
        task_dirs = sorted(
            path
            for path in root.glob("*/*")
            if path.is_dir() and path.name.startswith("F")
        )
        existing = {
            album.get("albumName"): album.get("id")
            for album in await self._albums()
            if album.get("albumName") and album.get("id")
        }
        created = 0
        updated = 0
        missing_assets = 0
        archived_assets = 0
        unsupported_assets = 0
        async with self._client(timeout=60) as client:
            for task_dir in task_dirs:
                intro_path = task_dir / "简介.txt"
                description = (
                    intro_path.read_text(encoding="utf-8").strip()
                    if intro_path.is_file()
                    else "由序影机器人自动下载并归档"
                )
                task_name = task_dir.name.split("_", 1)[0]
                album_name = (
                    f"{self.settings.immich.forwarded_album_prefix} · "
                    f"{task_name}"
                )
                album_id = existing.get(album_name)
                if not album_id:
                    response = await client.post(
                        "/albums",
                        json={
                            "albumName": album_name,
                            "description": description[:2000],
                        },
                    )
                    response.raise_for_status()
                    album_id = response.json()["id"]
                    existing[album_name] = album_id
                    created += 1

                asset_ids: list[str] = []
                for media in sorted(task_dir.iterdir()):
                    if not media.is_file():
                        continue
                    suffix = media.suffix.lower()
                    if (
                        suffix in SIDECAR_EXTENSIONS
                        or media.name == "简介.txt"
                        or media.name == "metadata.json"
                    ):
                        continue
                    if suffix not in MEDIA_EXTENSIONS:
                        unsupported_assets += 1
                        continue
                    asset_id = await self._asset_id_for_path(
                        client,
                        self._forwarded_immich_path(media),
                        library_id=library_id,
                    )
                    if asset_id:
                        asset_ids.append(asset_id)
                    else:
                        missing_assets += 1
                if asset_ids:
                    for offset in range(0, len(asset_ids), 500):
                        batch = asset_ids[offset : offset + 500]
                        response = await client.put(
                            f"/albums/{album_id}/assets",
                            json={"ids": batch},
                        )
                        response.raise_for_status()
                        rejected = self._rejected_bulk_ids(response)
                        if rejected:
                            logger.warning(
                                "Immich refused %d/%d assets for album %s: %s",
                                len(rejected),
                                len(batch),
                                album_name,
                                rejected[:5],
                            )
                    updated += 1
                    if self.settings.immich.forwarded_auto_archive:
                        try:
                            response = await client.put(
                                "/assets",
                                json={
                                    "ids": asset_ids,
                                    "visibility": "archive",
                                },
                            )
                            response.raise_for_status()
                            archived_assets += len(asset_ids)
                        except Exception:
                            # Album separation must still succeed if a future
                            # Immich version changes the archive endpoint.
                            logger.warning(
                                "机器人资源自动归档失败，已保留独立相册",
                                exc_info=True,
                            )
        return {
            "message": "Immich 机器人独立相册同步完成",
            "created_albums": created,
            "updated_albums": updated,
            "missing_assets": missing_assets,
            "unsupported_assets": unsupported_assets,
            "archived_assets": archived_assets,
        }
