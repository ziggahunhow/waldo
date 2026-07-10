# LINE Bot: Dynamic Reference Photos (`/setref` / `/done`)

## Context

Reference photos are currently a single hardcoded filesystem path
(`_REF_DIR = Path.home() / "Documents" / "photos" / "target_person"` in `line_bot.py`),
marked with a `TODO` from the earlier rewrite that removed all reference-photo
collection from the bot. This replaces that hardcode with a LINE-native
`/setref` / `/done` command flow, while keeping the reference set global
(one set, shared by every group the bot is in) rather than per-group — the
bot is currently single-purpose (searching for one person), and per-group
sets were considered and explicitly deferred in favor of this simpler scope.

## Storage

- `REFS_DIR = Path(__file__).parent / "refs"` — the live reference set
  `_reference_photo_paths()` reads from at search time. Same
  project-relative-directory pattern as `ALBUMS_DIR`/`LOG_DIR`.
- `REFS_STAGING_DIR = Path(__file__).parent / "refs_staging"` — holds
  in-progress collections. `/setref` writes here, never touching `refs/`
  directly, so an interrupted, abandoned, or empty collection can never
  leave the live reference set broken or empty.
- Both `refs/` and `refs_staging/` are added to `.gitignore`, matching the
  existing treatment of `albums/`, `logs/`, `.cache/`, `results/`.

## Collector state

A single global (not per-group) in-memory variable:

```python
_ref_collector: Optional[dict] = None  # {"user_id": str, "count": int}
```

Global rather than per-group because the reference set itself is global —
two concurrent collections (even from different groups) would both be
writing into the same `refs_staging/` directory, so only one collection
can be active system-wide at a time.

## `/setref` (any user, in an approved group/room — DMs already rejected upstream)

1. If `_ref_collector is not None`: reply "已有人正在收集參考照片，請稍候。" (already
   collecting) and do nothing further.
2. Otherwise: clear `refs_staging/` (remove and recreate), set
   `_ref_collector = {"user_id": event.source.user_id via _push_target's
   underlying sender, "count": 0}`, and reply:
   `"📸 開始收集參考照片。請傳送 3-5 張人像照片（只有你傳送的照片會被使用），完成後輸入 /done。"`
   — explicitly telling the initiator that only their own photos count, so
   it's never a silent surprise when someone else's shared photo in the
   same window doesn't get picked up.

Note: the *sender* identity needed here is the individual LINE user
(`event.source.user_id`), not `_push_target()`'s group/room id — collector
attribution is per-person even though delivery/search state is
per-group/room. `GroupSource.user_id`/`RoomSource.user_id` can be `None`
per the SDK for some LINE clients; if it's `None` when `/setref` is
invoked, treat it the same as a normal user (reply with an error — "無法識別
你的使用者 ID，請改用其他裝置再試一次。") since attribution can't work without it.

## Image messages (`on_image` — reintroduced; removed entirely in the earlier rewrite)

- If `_ref_collector is None`: ignore silently (consistent with the
  existing "ignore anything that doesn't fit" policy).
- If `_ref_collector is not None` but the sender's `user_id` doesn't match
  `_ref_collector["user_id"]`: ignore silently — this is the enforcement of
  "only that user's photos count," not an error condition worth replying to.
- If it matches: fetch the image bytes (same `MessagingApiBlob.get_message_content`
  call the old `on_image` used), write to
  `refs_staging/ref_{count}.jpg`, increment `_ref_collector["count"]`, reply
  with a running count: `f"已收到第 {count} 張參考照片，傳送更多或輸入 /done 完成。"`
- DMs: also ignored (mirrors `on_text`'s DM rejection) — collection can only
  meaningfully happen in a group since `/setref` itself requires one.

## `/done`

1. `_ref_collector is None`: reply "目前沒有正在收集的參考照片。"
2. `_ref_collector` exists but sender ≠ `_ref_collector["user_id"]`: reply
   "只有發起 /setref 的人可以使用 /done。" — state is untouched, the original
   collector can still finish it.
3. Sender matches, `count == 0`: reply "尚未收到任何照片，保留原本的參考照片。",
   clear `_ref_collector`, leave `refs/` and `refs_staging/` untouched (abort).
4. Sender matches, `count >= 1`: atomically replace `refs/` with
   `refs_staging/`'s contents (remove old `refs/`, rename
   `refs_staging/` → `refs/`, recreate an empty `refs_staging/` for next
   time), clear `_ref_collector`, reply
   `f"✅ 已更新參考照片（共 {count} 張）。"`

## Search-time changes

- `_reference_photo_paths()` reads from `REFS_DIR` instead of the removed
  `_REF_DIR`.
- The "no reference photos" error in `_run_search` changes from mentioning
  a filesystem path to: `"❌ 尚未設定參考照片，請先使用 /setref 設定。"`
- No interaction with `_active_searches` needed in either direction: a
  `/setref` while a search is running doesn't affect it (the search already
  computed its `known` encodings from `refs/` before `/setref` could swap
  anything), and a search started while a collection is in progress simply
  reads whatever `refs/` currently holds (untouched until `/done` succeeds).

## Out of scope

- Per-group/room reference sets (explicitly deferred — see Context).
- Any cap on how many photos can be collected per `/setref` session (matches
  existing permissive behavior elsewhere — no caps on reference uploads).
- Updating `_HELP_TEXT` beyond adding the two new commands — no other
  command behavior changes.
