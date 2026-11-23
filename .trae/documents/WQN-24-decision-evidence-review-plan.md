# WQN-24 完善 Codex 草稿 PR #44：审查、补测试与验证计划

## Context

分支 `trae/wqn-24-decision-evidence` 以 Codex 草稿 PR #44 的两个提交（16e9c11、e902d85）为起点，目标是「固化 LIVE 决策当时的完整盘口和费率」：决策提交时写入来源事件、费率与两侧深度；Paper 只按决策当时证据定价；跨进程重放不丢、不重、不改原证据；迁移对已有数据安全。

已完成的审查（只读）确认大部分实现正确：

- `book_id = sha256(token_id | canonical bids | canonical asks | venue_ts)`（`data/writer.py:158`），哈希覆盖两侧全部档位与交易所时间，不同盘口不会误合并。
- `BookEvidence.from_signal`（`paper/books.py:101`）重新解析并复算哈希，缺失、来源事件缺失、采集时间晚于决策、哈希不符均返回 `None`，Paper 记 `NOT_EXECUTABLE`，不会回退扫描归档。
- 费率/费率指数按决策行固化（`shadow/decision.py:511`、`shadow/store.py:391`）；导出的费率对 `evidence_version IS NOT NULL` 取 `d.fee_rate`，旧格式仍取 catalog（`shadow/export.py:759`）。
- 重放通过 `(cycle_id, market_id, direction)` 回查原 `decision_id`（`shadow/store.py:335`），冲突更新不触碰 `decision_id/export_seq/book_* /fee_*`；拒绝原因按最新决策整体替换。
- 迁移 0007 仅做可空列/新表；0008 重建 `rejection_reasons`（保留 `(decision_id, reason)` 主键、去掉 ordinal 唯一约束）并移除会被 UPSERT 更新的索引，`initialize_shadow` 在迁移后先 `CHECKPOINT`。

**审查发现的缺陷（需修复）**：`ShadowStore.save_snapshots`（`shadow/store.py:486-495`）按 `book_id` 从 `token_books` 复制深度。`token_books` 以 `token_id` 为主键、只保留最新一条；若同一批次内同一 token 先后写入 rest/ws 两帧（或任何后到的帧），决策引用的旧 `book_id` 已不在 `token_books`，深度行不会写入。结果：LIVE 决策导出行 `evidence_version=1` 但深度为 NULL，Paper 只能记 `NOT_EXECUTABLE`，丢失本应固化的证据。

## 实施步骤

### 1. 修复深度写入来源（src/weatherquant/shadow/store.py）

- 从 `weatherquant.data.writer` 引入 `canonical_levels_json`。
- 将 `save_snapshots` 末尾「从 token_books SELECT」改为直接序列化 `Decision.live_book` 的两侧档位（best-first），以 `d.book_id` 为键写入 `decision_book_depth`，批量 `INSERT ... ON CONFLICT (book_id) DO NOTHING`，批内按 `book_id` 去重。
- 仍要求 `live_book.source_event_id` 存在（旧库升级后的历史 book 没有来源事件，不应伪造证据）。
- 同步更新该段注释：深度来自决策实际观察到的对象，而不是提交时刻 `token_books` 的「最新一行」；序列化使用 collector 的规范形式，保证重新哈希等于 `book_id`。

### 2. 补验收测试（公开入口、临时目录/临时 DuckDB）

a. `tests/test_shadow_storage.py`：`save_snapshots` 的深度来自决策引用盘口——先 `upsert_books` 写入一个更新的同 token 盘口，再保存引用旧盘口的 LIVE 决策，断言 `decision_book_depth` 只有决策的 `book_id` 且档位是旧盘口（修复前该行缺失、测试失败）。

b. `tests/test_paper_book_evidence.py`：捕获证据被篡改（`book_id` 不变、`book_bids_json` 改价）时，Paper 记 `NOT_EXECUTABLE` 且无成交行——补齐「哈希不匹配 → 拒绝」这一条（缺失/超时已有测试）。

c. `tests/test_shadow_runner.py`：真实端到端——跑一个 LIVE cycle，用 `DecisionExporter` 导出，向归档追加一帧「决策前、价格不同」的更新盘口，再把导出行交给 `PaperStore` + `FillEngine` 成交：
   - 断言 `ArchiveBookSource` 在决策时刻解析到的是新追加帧（证明测试有区分度）；
   - 断言成交均价等于决策当时导出的盘口（0.47），`book_source_event_id` 等于决策那行归档的 sha256、不等于追加行；
   - 修复前（Paper 总是回查归档）该用例会因哈希不符被拒，测试失败。

### 3. 文档

- 更新 `docs/adr/0001-paper-trading-architecture.md` D9，补一句：深度直接来自决策观察对象并按内容哈希去重，不依赖 `token_books` 的当前行；`export_seq` 在重放时可能跳号，导出游标按单调递增序列工作，跳号无影响。

### 4. 反向验证「没有修复就会失败」

- 临时把 `paper/fills.py` 的 `evidence_version` 分支改回总是 `self.books.resolve(...)`，运行上述 (c) 与 (b)，确认失败；恢复后通过。
- 临时把 `save_snapshots` 的深度来源改回 `token_books`，运行 (a)，确认失败；恢复后通过。
- 在提交说明中记录这两次反向验证的命令与结果。

### 5. 验证与提交

- `uv run pytest -q` 全量通过（基线约 1521 passed, 6 skipped；若 `tests/test_paper_cli.py` 时间问题重现，按任务要求改用相对当前时间并注明）。
- `uv run ruff check .` 无问题；仅对改动过的 Python 文件执行 `ruff format`（避免整文件历史漂移）。
- 本地 `git commit`，提交信息包含 `WQN-24`，不 push；不混入 #42/#43/#49/#63–#67 的改动，不改依赖版本、不碰生产数据与 Jira。

## 关键文件

- `src/weatherquant/shadow/store.py`（深度写入修复）
- `tests/test_shadow_storage.py`、`tests/test_paper_book_evidence.py`、`tests/test_shadow_runner.py`（新增验收测试）
- `docs/adr/0001-paper-trading-architecture.md`（D9 补充说明）

## 验证清单

```bash
uv run pytest -q tests/test_shadow_storage.py tests/test_paper_book_evidence.py tests/test_shadow_runner.py
uv run pytest -q          # 全量
uv run ruff check .
uv run ruff format <改动的 Python 文件>
```
