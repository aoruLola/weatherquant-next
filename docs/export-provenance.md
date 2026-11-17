# 决策导出 Provenance 设计文档（WQN-26）

## 背景

Shadow 引擎把决策历史以 append-only 的 `part-XXXXXXXX.jsonl` 分片发布到 `decisions-export/parts/`，
paper consumer 与 performance backfill 通过 `read_export` / `ExportReader` 读取。
当前只有分片连续性校验（索引不允许 gap），以及下游对 row 内容做的 digest；
缺少一份由发布方签名、不可伪造的"这段分片是 Shadow 引擎写的，且内容没有被篡改"的证明。

现有消费者只能检测到"分片丢了 / 重写了"，无法分辨"这是今天的 Shadow 引擎写的"还是
"有人在这台机器上手工改了一行 / 塞进一个旧分片 / 半写了一半被 kill 留了残片"。
本设计在不改动任何现有 part 内容和 consumer 行为的前提下补上这一层。

## 威胁模型

需要证明什么，以及我们**故意不**证明什么。

| 威胁 | 当前能否检出 | 本设计能否检出 | 检出方式 |
|---|---|---|---|
| 分片被手工 edit 一行 | 部分（依赖下游 digest） | 可以 | `DIGEST_MISMATCH`：part 文件 sha256 与 manifest 不一致 |
| 整个分片被替换为另一文件（内容不同） | 部分 | 可以 | 同上 |
| 引擎在 write part 之后、写 state.json 之前 crash，留下孤立 part | 部分（下游 gap 检测） | 可以 | part 有 manifest 但下一个 part 没有 → `MISSING_MANIFEST` / 哈希链断裂 |
| 引擎在 write part 之后、写 manifest 之前 crash，留下半写分片 | 否 | 可以 | 哈希链起点：第一份 manifest 出现之后，后续 part 缺 manifest 即被判定 |
| 另一个进程/脚本塞进一个旧分片（replay） | 否 | 可以 | 旧分片对应的 manifest 哈希链已过 → `CHAIN_BROKEN`；或签名不匹配 → `BAD_SIGNATURE` |
| 只替换 manifest 而不碰 part | 否 | 可以 | 对 part 内容 sha256 哈希链已断 |
| 截断最后一行 / 手动追加一行 | 否 | 可以 | `DIGEST_MISMATCH` + `ROWCOUNT_MISMATCH` |
| 密钥被拿到，伪造一个带正确签名的分片 | — | **不能** | HMAC 只证明签名者拥有密钥，不证明密钥持有者身份 |
| 同一密钥在多台机器上同时用，每台各写一片 | — | **不能** | HMAC 不提供身份；publisher_run_id 只是帮助日志诊断 |
| 生产 DB/WAL/archive 被改 | — | **不涉及** | provenance 只管 export 侧的数据面，不越界到数据库 |

## 签名方案

每分片发布完之后额外写一份 sidecar manifest：

`part-XXXXXXXX.manifest.json`，原子写入（temp → fsync → rename）。

### Manifest 字段（schema_version=1）

```
schema_version:           1            # 整型
part_file:                "part-000000005.jsonl"
index:                    5
part_sha256:              "<64 hex>"  # part 文件完整字节的 sha256
row_count:                1234
first_export_seq:         10001       # 若 part 中全部为 legacy v1 行则 null
last_export_seq:          11234       # 同上
first_decision_time_utc:  "2026-..."
last_decision_time_utc:   "2026-..."
prev_manifest_sha256:     "<64 hex or null>"  # 上一份 manifest 的完整文件内容 sha256；首片 null
publisher_run_id:         "host=<x>:pid=<y>:start=<z>"
created_at_utc:           "2026-11-17T15:18:16.488645+00:00"
hmac_sha256:              "<64 hex or null>"  # 可选，见下
```

### HMAC 签名

- 仅当环境变量 `WQ_EXPORT_SIGNING_KEY` 非空时启用。密钥**永不写入 manifest、永不写入日志**。
- 签名输入：将 manifest 按字段顺序（见上表，不含 `hmac_sha256`）序列化为紧凑 JSON（`separators=(",", ":")`），
  UTF-8 编码。这样攻击者即使交换两个字段顺序也拿不到合法签名。
- 签名算法：`hmac.new(key_bytes, canonical_bytes, hashlib.sha256).hexdigest()`。
- 验签常量时间比较：复用 `hmac.compare_digest`（已在 `src/weatherquant/mcp/security.py` 用同一模式）。

### 哈希链

每份 manifest 里的 `prev_manifest_sha256` 是**上一份 manifest 完整文件内容**的 sha256。
攻击者无法在不修改已有分片 manifest 的情况下"中间插一片"，因为那会让该分片的后续 manifest 的
`prev_manifest_sha256` 全部对不上 → verifier 报 `CHAIN_BROKEN`。

## 发布原子性

```
① _stage_part()            → 临时文件 *.tmp（写完整字节 + fsync；尚未发布，消费者不读 *.tmp）
② build_manifest(临时文件字节, prev_manifest)
③ _write_part_manifest()   → part-XXXXXXXXX.manifest.json (temp + fsync + rename)
④ rename 临时文件 → part-XXXXXXXXX.jsonl（发布）
⑤ 只有 ①–④ 都成功，run_batch() 才推进 state.json
```

manifest 先于 part 发布，所以**任何已发布的 part 在发布那一刻就已有 manifest**；
导出器从不为磁盘上已有的 part 事后补签。崩溃时外部可能看到的状态：

| 外部看到 | 判定 |
|---|---|
| part 在，manifest 在 | 正常 OK（或 LEGACY_UNATTESTED 首段） |
| manifest 在，part 不在（③ 之后、④ 之前崩溃） | `UNKNOWN_PART`；state 未推进，下一次 tick 对同一 index 重写 manifest 与 part，恢复为 OK |
| 目录里存在 *.tmp（① 之后被 kill） | `STRAY_TMP_FILE`（与改动前的分片临时文件行为相同，需人工清理） |
| part 在，manifest 不在（第一份 manifest 之后） | `MISSING_MANIFEST`：不是本导出器发布的（手工拷贝/注入/老代码回滚），**不会被自动补签** |

**关键不变量**：一旦某分片有 manifest，它之后所有分片都必须有 manifest，并且哈希链连续；
否则视为"半写 / 未被发布者认证"。

## 兼容策略

### Legacy 分片（没有 manifest）

- verifier 报告 `LEGACY_UNATTESTED`，**非错误**：这些是旧版本已发布的分片，兼容保留。
- 判断方式：找到**第一份** manifest（按 index 升序），它之前的所有分片都视作 legacy。
- consumer / backfill **完全不受影响**：它们用 `glob("part-*.jsonl")`，manifest 后缀不同，不会被误收。

### 新分片必须带 manifest

- 一旦某个 part index 有 manifest，它之后所有连续 part 若没有 manifest → `MISSING_MANIFEST`。
- 若 legacy 之后**插入**一个新 part 但不带 manifest（老代码回滚，或手工拷贝）→ 同样报 `MISSING_MANIFEST`。

### 不做自动补写（安全原因）

导出器**不会**为缺 manifest 的已存在分片补写 manifest。早期草案在 `run_batch()` 里补签
「最高已签名分片之后」的尾部分片，但补签的对象只是磁盘上的文件：任何能写导出目录、却没有密钥的人
都可以放入一个新的尾部分片，或替换最新分片并删掉它的 manifest，然后等下一次 tick 让导出器用真密钥
签上——这正是 HMAC 要防的伪造。改为「先写 manifest、再发布 part」之后，正常流程与崩溃恢复都不需要补签：
缺 manifest 的分片一律保持 `MISSING_MANIFEST`，需要人工核查。

### Consumer 侧

默认**不做 provenance 校验**：manifest 是写侧/运维用的，consumer 的职责是读 JSONL 行并写信号事件。
`ExportReader.list_parts()` 仍然只扫 `part-*.jsonl`，manifest 不会被当作分片。
如果未来需要让 consumer "看见" provenance，可在 `consumer.py` 加一个可选 `verify_provenance=True` 开关，
默认 off。

## Verifier 模块（`src/weatherquant/shadow/provenance.py`）

纯只读：不读 shadow DB，不依赖 engine lease，不打开 DuckDB。

### Per-part 状态枚举

```
OK
LEGACY_UNATTESTED   # 处于第一份 manifest 之前的分片
MISSING_MANIFEST    # 分片存在但没有 manifest，且从第一份 manifest 起就该有
DIGEST_MISMATCH     # manifest.part_sha256 ≠ part 文件实际 sha256
ROWCOUNT_MISMATCH   # manifest.row_count ≠ 实际行数
SEQ_RANGE_MISMATCH  # manifest.first/last_export_seq ≠ 实际首尾行的 export_seq
CHAIN_BROKEN        # manifest.prev_manifest_sha256 ≠ 上一份 manifest 文件内容 sha256
BAD_SIGNATURE       # WQ_EXPORT_SIGNING_KEY 已配置，manifest 有 hmac_sha256 但 HMAC 校验失败
UNSIGNED            # WQ_EXPORT_SIGNING_KEY 已配置，但 manifest.hmac_sha256 为 null
STRAY_TMP_FILE      # parts/ 目录下残留 .tmp 文件
MALFORMED_MANIFEST  # manifest 无法解析、字段缺失或 index/part_file 与文件名不一致
UNKNOWN_PART        # 有 manifest 但没有对应的 part 文件
```

### 总体判定

- **PASS**：所有 part ∈ {OK, LEGACY_UNATTESTED}，且无 STRAY_TMP_FILE。
- **FAIL**：只要有一个 part 不在 OK/LEGACY_UNATTESTED。

### CLI

```
weatherquant-shadow export-provenance [--json] [--parts-dir <path>] [--signing-key <key>]
```

- `--parts-dir` 默认 `<shadow_report_path.parent>/decisions-export/parts`，无需 shadow DB。
- 签名密钥默认取环境变量 `WQ_EXPORT_SIGNING_KEY`；`--signing-key` 可显式覆盖。
- 退出码：PASS → 0；FAIL → 1；参数错误 → 2。
- 输出不含密钥。

## 密钥管理

- 密钥用环境变量注入（容器编排层负责），写侧启动时只读一次。
- manifest 里只存 HMAC 输出，never 存 key。
- verifier **可选**从同一环境变量读取 key 以验签；若未提供 key，签名相关判定跳过（UNSIGNED 不报错，BAD_SIGNATURE 也跳过）。
- 密钥轮换：新密钥对接下来发布的分片生效；已发布分片仍用旧密钥签名，轮换后需要同时持有两把密钥才能验全链。
  第一版先不做轮换历史管理，等出现轮换需求再加。

## 不改的东西

- **不读 / 不写 shadow DB / WAL / archive**。
- **不改动 part 文件内容 schema**（任何一行的 JSON 字段集不变）。
- **不改动 `state.json` 结构**。
- **不改动 consumer / backfill 默认行为**。
- **不改动生产运行时安全边界**（engine lease / 互斥不变）。
