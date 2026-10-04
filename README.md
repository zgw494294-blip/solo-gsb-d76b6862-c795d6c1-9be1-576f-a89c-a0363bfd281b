# 离线对白字幕分轨 HTTP API

用 Python + FastAPI 实现的离线服务：给定 2～3 条屏幕轨道和 1～12 条对白字幕，为每条字幕选择一条轨道，满足：

- **轨道互斥**：字幕占用左闭右开时间区间 `[start_ms, end_ms)`；时间重叠的字幕不能占同一轨；端点相接（前一条的 `end_ms` == 后一条的 `start_ms`）可以复用同一轨。
- **锁定**：字幕可指定 `locked_track`，强制占用该轨道（轨道号从 1 开始）。
- **优化目标**：同一说话人按时间相邻的两条字幕，换轨次数总和最小。
- **并列裁决**：换轨次数相同时，取“按输入顺序排列的轨道号序列”字典序最小的方案。
- 同一说话人的字幕时间不得重叠（属于输入合法性约束，非法返回 422）。
- 分轨结果可作为**场景（scene）**按 `scene_id` 保存为版本化修订：新场景以 `expected_revision: 0` 创建（成功后版本 1），后续修订须带当前版本；版本不符返回 409。数据落在本地 SQLite，容器重启后仍可读取。
- 已保存场景的指定时段可**另存为片段（clip）**：`POST /scenes/{id}/clip` 把 `[from_ms, to_ms)` 内相交字幕裁出并减去 `from_ms`，时间轴从 0 毫秒起，源分轨写入各条 `locked_track` 以保持原画面轨道，片段保存为 revision 1 的独立新场景；裁剪数据全部取自与 `expected_revision` 一致的**同一份完整源快照**——读取快照时先核对版本（不符立即 409，绝不拿错配版本裁剪），提交前在同一事务内再核对一次（读取匹配后源被修订同样 409），目标已占用返回 409、源不存在返回 404、窗口无相交字幕返回 422；失败不创建目标、不改源场景或播放会话。
- 2～4 个已保存场景可按给定顺序**拼接（concat）为连续播放场景**：`POST /scenes/concat` 接收非空 `target_scene_id` 与 2～4 段（每段给出非空 `scene_id`、正整数 `expected_revision` 与正整数 `duration_ms`，同源可重复，整数拒绝布尔值和字符串）；各源轨道数须相同，每段时长不得小于该源字幕的最大 `end_ms`，拼接后字幕总数 ≤ 12。字幕起止时间加上之前各段时长之和，按段序、源顺序排列，id 为“段号:段内序号”（均从 1 起），speaker 不变，源实际分轨写入 `locked_track`，`changes` 按拼接后同说话人时间相邻字幕计算（跨段相邻计入）。读取与提交时均核对全部源版本，**提交核对和目标创建在同一事务内一同生效**：源不存在 404、版本不符或目标已占用 409、其余非法输入 422；失败不留目标、不改源及播放会话。成功保存为版本 1 并返回完整场景，可直接用原读取、修订及播放接口操作。
- 已保存场景可创建**播放会话（session）**：单进程内存态会话复制场景完整快照，游标从 0 毫秒开始推进（只能不减）；播放中可接纳场景的后续修订——已开始（`start_ms` ≤ 游标）的字幕保持原分轨，其余字幕按原最少换轨与并列规则重算。会话不跨重启保留。接纳是完整操作：场景读取、已开始字幕校验、分轨重算与会话替换在会话锁内串行生效，迟到的接纳请求读到的是轮到它生效时的场景快照，不会把会话回写到旧版本。除原 `/advance` 外，另有 `/events` 事件推进：一次推进返回跨过的**全部**字幕进入/退出事件，短字幕即使在两次采样之间开始又结束也不会漏掉。播放时还可通过 `/hidden_speakers` **临时隐藏指定说话人的字幕（仅影响该会话、不持久化）**：按 `speaker` 精确匹配，提交无重复非空字符串数组（空数组清除，允许尚未出现者）；切换时以当前游标的左闭右开活动区间判定，原可见而现隐藏者发 `exit`、原隐藏而现可见者发 `enter`（时刻为当前游标，先 `exit` 后 `enter`，同类按会话字幕顺序，无变化则无事件）；设置后推进与事件推进的活动字幕、事件均排除隐藏者而游标照常推进，隐藏默认空、接纳后保留，完整快照与分轨不受影响。播放结束以 `POST /sessions/{id}/finish` **一次完整操作取回最终快照并释放会话**：成功响应沿用创建会话的全部字段，快照取自轮到结束时的会话（不读取尚未接纳的场景修订），未知会话返回 404；结束与同会话的推进、事件推进、隐藏切换、接纳串行生效，并发结束最多一次成功，结束后针对该 id 的一切请求（含已取得会话引用但尚未生效的请求）均返回 404；不改动源场景、其他会话或此前返回的快照，不保存结束记录、不跨重启恢复。
- 播放会话可开启一个**补取通道（channel）**，让显示端在**推进响应丢失后仍能补取未确认的字幕变更**：`POST /sessions/{id}/channel` 每会话开启一个内存通道（重复开启 409），返回当前可见 `active` 与序号 0；此后普通推进、事件推进与隐藏切换产生的全部 enter/exit 按事件字段与排序入队，`seq` 从 1 连续编号，短字幕不漏记，接纳不重写已排队内容。`POST /sessions/{id}/channel/fetch` 提交 `ack_seq`（严格非负整数，拒绝布尔值与字符串）补取：只能确认此前补取已返回的最大 `seq` 以内的前缀（越界 422，旧确认不回退），返回确认位置与剩余未确认事件（按 `seq` 递增，重复补取内容与序号不变）。状态变更与入队一同生效，开启、补取与既有会话操作完整串行，失败不改会话或通道；结束会话清除通道，未知或已结束会话、未开启通道均 404。通道仅内存态：不跨重启恢复、不做后台推送，旧接口响应不变。**通道共享容量**由环境变量 `CHANNEL_EVENT_LIMIT` 配置（正整数，默认 1024，非法配置拒绝启动，可在 Compose 中配置）：它是**单进程内所有会话未确认事件总量**的上限。普通推进、事件推进与隐藏切换须按**本次实际生成的事件数**原子申请共享容量，申请与状态变更、`seq` 分配、完整入队一同生效；容量不足立即返回 **503**——不推进游标、不改变隐藏集合、不消费 `seq`、不部分入队。**未开启通道的操作不占容量**；**无事件的操作在满额时仍可执行**。容量**仅在补取确认并删除前缀时**按删除数量释放（纯读取与旧确认不释放），或在**结束会话时释放该通道剩余队列**（重复/并发结束不重复释放）；释放后被 503 拒绝的原请求可原样重试。
- 已开启补取通道的会话还可启动**自动播放（autoplay）**：客户端不再逐次提交推进时间，进程内后台驱动**每 50 毫秒**按真实经过时间自动推进，目标为**本段起始游标 + 单调时钟经过的整毫秒**（`time.monotonic`，毫秒取整）。HTTP 接口按 `session_id` **启动 / 暂停 / 恢复 / 查询**，返回 `idle`（未启动）/ `running`（运行）/ `paused`（暂停）/ `blocked`（满额阻塞）状态与当前游标。启动仅限**已开补取通道且从未启动**的会话（未开通道或已启动返回 409）；暂停仅限 running，恢复仅限 paused 或 blocked，其余状态迁移均 409；未知或已结束会话 404。**暂停、阻塞不计时**，恢复以保留游标作为新一段起点重新计时，**不追赶**停顿时间；启动与恢复的计时段起点在**会话锁内、控制请求串行生效的同一时刻**读取——同会话接纳等操作持锁时，控制请求在锁外等待的间隔不计入计时，生效后不会把等锁时长额外推进到游标上、也不会提前入队字幕事件。后台推进沿用事件窗口、隐藏过滤、排序、`seq` 与共享容量的**全部既有规则**：一次推进的整批事件原子入队，共享容量不足时整批转为 `blocked`——游标、`seq` 与队列保持原样；确认释放容量后**必须显式恢复**才继续，且不追赶停顿。**运行中两种手动推进（`/advance`、`/events`）返回 409**，未启动/暂停/阻塞时仍可手动推进；隐藏切换、接纳、补取等其他既有会话操作在四种状态下仍可用并与后台推进完整串行。暂停返回后在途后台任务进锁即见非 running，不可能再推进；结束会话即停止对它的驱动（注册表移除后旧任务取不到它）、按原规则释放通道容量，之后该 id 一切请求 404。自动播放驱动状态**仅存单进程内存**，不跨重启，旧接口的请求与响应完全不变。
- 已开补取通道、自动播放未在运行的播放会话还可保存**播放存档（checkpoint）**以支持**跨重启续播**：`PUT /sessions/{id}/checkpoints/{checkpoint_id}`（或集合式 `POST /sessions/{id}/checkpoints`）按 `session_id` 与非空 `checkpoint_id` 把存档写入场景同一个 SQLite；仅**已开通道且自动播放处于非运行状态**（idle/paused/blocked）可保存，未开通道或运行中返回 **409**。存档在会话锁内序列化，含**完整快照、游标、隐藏集合、通道未确认事件及 `next_seq`、`acked_seq`、`max_returned_seq`** 序号边界，与同会话操作串行；成功**不结束会话**，失败不留存档（同名存档 **409**、未知会话 **404**、非法 id **422**）。`POST /checkpoints/{checkpoint_id}/restore` 按存档恢复为**全新 `session_id`** 的会话，**不读取当前场景**；事件、序号与确认边界保留，后续新事件接着 `next_seq` 连续编号；未启动者恢复后仍未启动，暂停或阻塞者恢复为**暂停**（显式恢复才续播，**重新计时、不追赶离线时间**）。队列按存档未确认事件数**计入共享容量**，容量不足返回 **503** 且不留会话或容量占用，并发恢复不可能超额。同一份存档可**重复恢复**为互不影响的独立会话，恢复及后续操作都不修改存档或其他会话；恢复响应含新会话 id、完整快照与播放状态。旧接口的请求与响应完全不变。

## 一条命令启动

```bash
docker compose up --build -d
```

服务监听 **8080** 端口，接口地址：`http://localhost:8080/assign`

停止：`docker compose down`

> - 交互式查看日志可去掉 `-d`。交互式文档：`http://localhost:8080/docs`（Swagger UI）。
> - Compose 自动把具名卷 `scenes-data` 挂载到容器内 `/data`：场景数据库（`SCENES_DB_PATH=/data/scenes.db`）在容器重启甚至 `docker compose down && up` 后仍然保留。仅 `docker compose down -v` 删除卷时才会清除场景。**播放存档（checkpoint）写在同一个 SQLite 中**，因此同样随具名卷跨重启保留，恢复为新会话即可续播。
> - Compose 还通过环境变量 `CHANNEL_EVENT_LIMIT` 配置补取通道的共享容量（默认 `1024`，正整数；非法配置容器会拒绝启动，详见下文“补取通道”）。恢复存档时存档保留的未确认事件队列也计入这一份共享容量。

## 请求

`POST /assign`，JSON Body：

| 字段 | 类型 | 约束 |
| --- | --- | --- |
| `track_count` | int | 屏幕轨道数，2 或 3 |
| `subtitles` | array | 1～12 条，顺序即输入顺序 |
| `subtitles[].id` | string \| int | 全局唯一，字符串不得为空 |
| `subtitles[].speaker` | string | 非空（不能全为空白） |
| `subtitles[].start_ms` | int | 非负 |
| `subtitles[].end_ms` | int | 非负，且严格大于 `start_ms` |
| `subtitles[].locked_track` | int \| null | 可选，锁定轨道号（≥1 且 ≤ `track_count`） |

## 响应

- 有解：HTTP 200，`feasible: true`，`changes` 为换轨总数，`assignments` 按**输入原顺序**给出每条字幕的轨道。
- 锁定冲突或轨道不足：HTTP 200，`feasible: false`，`changes: 0`，`assignments: []`（空方案）。
- 非法输入（字段缺失/越界、id 重复、同一说话人字幕时间重叠等）：HTTP **422**，带校验错误明细。

## 可执行的 HTTP 调用示例

### 示例 1：有解（含锁定轨道与端点相接）

```bash
curl -s http://localhost:8080/assign \
  -H 'Content-Type: application/json' \
  -d '{
    "track_count": 2,
    "subtitles": [
      {"id": "s1", "speaker": "Alice", "start_ms": 0,    "end_ms": 1000},
      {"id": "s2", "speaker": "Bob",   "start_ms": 800,  "end_ms": 3000, "locked_track": 2},
      {"id": "s3", "speaker": "Alice", "start_ms": 1000, "end_ms": 2500},
      {"id": "s4", "speaker": "Alice", "start_ms": 2500, "end_ms": 4000}
    ]
  }'
```

响应：

```json
{
  "feasible": true,
  "changes": 0,
  "assignments": [
    {"id": "s1", "track": 1},
    {"id": "s2", "track": 2},
    {"id": "s3", "track": 1},
    {"id": "s4", "track": 1}
  ]
}
```

说明：s1 与 s3 时间相接可同轨，s3 与 s4 同轨；Alice 三条字幕在同一轨，换轨 0 次。

### 示例 2：非零换轨（锁定迫使相邻字幕分轨）

```bash
curl -s http://localhost:8080/assign \
  -H 'Content-Type: application/json' \
  -d '{
    "track_count": 2,
    "subtitles": [
      {"id": "a", "speaker": "A", "start_ms": 0,   "end_ms": 100, "locked_track": 1},
      {"id": "b", "speaker": "A", "start_ms": 100, "end_ms": 200, "locked_track": 2},
      {"id": "c", "speaker": "A", "start_ms": 200, "end_ms": 300}
    ]
  }'
```

响应：

```json
{
  "feasible": true,
  "changes": 1,
  "assignments": [
    {"id": "a", "track": 1},
    {"id": "b", "track": 2},
    {"id": "c", "track": 2}
  ]
}
```

### 示例 3：无解 —— 两条重叠字幕锁定同一轨（空方案）

```bash
curl -s http://localhost:8080/assign \
  -H 'Content-Type: application/json' \
  -d '{
    "track_count": 2,
    "subtitles": [
      {"id": "s1", "speaker": "Alice", "start_ms": 0,    "end_ms": 2000, "locked_track": 1},
      {"id": "s2", "speaker": "Bob",   "start_ms": 1000, "end_ms": 3000, "locked_track": 1}
    ]
  }'
```

响应：

```json
{"feasible": false, "changes": 0, "assignments": []}
```

### 示例 4：无解 —— 3 条字幕同时重叠但只有 2 轨（轨道不足）

```bash
curl -s http://localhost:8080/assign \
  -H 'Content-Type: application/json' \
  -d '{
    "track_count": 2,
    "subtitles": [
      {"id": "a", "speaker": "A", "start_ms": 0,  "end_ms": 100},
      {"id": "b", "speaker": "B", "start_ms": 10, "end_ms": 110},
      {"id": "c", "speaker": "C", "start_ms": 20, "end_ms": 120}
    ]
  }'
```

响应：

```json
{"feasible": false, "changes": 0, "assignments": []}
```

### 示例 5：非法输入返回 422（同一说话人字幕时间重叠）

```bash
curl -s -i http://localhost:8080/assign \
  -H 'Content-Type: application/json' \
  -d '{
    "track_count": 2,
    "subtitles": [
      {"id": "a", "speaker": "A", "start_ms": 0,   "end_ms": 100},
      {"id": "b", "speaker": "A", "start_ms": 50,  "end_ms": 150}
    ]
  }'
```

返回 `HTTP/1.1 422 Unprocessable Entity`，body 为 FastAPI 标准校验错误结构。

## 场景（scene）：可保存的修订

`/assign` 只做一次性计算、不保存任何数据。如果需要把某次分轨结果保存下来、以后读取并在其基础上继续修订，使用场景接口，数据写入容器内本地 SQLite（Docker Compose 已挂载具名卷，**容器重启甚至重新创建后场景仍可读取**）。

- `PUT /scenes/{scene_id}`：创建或修订场景。请求体沿用 `/assign` 的 `track_count` 与 `subtitles`（同样的字段约束、校验、最少换轨与并列规则），另加一个字段：

| 字段 | 类型 | 约束 |
| --- | --- | --- |
| `expected_revision` | int | 非负。客户端所依据的当前版本：**新场景填 `0`**；修订已存在场景时必须填写其当前版本 |

- 新场景首次成功后 `revision = 1`；此后每次成功修订 `revision + 1`。
- 合法但无解：返回与 `/assign` **完全相同**的无解响应（`feasible: false`、空方案），**不保存、不改变任何已有版本**。
- 非法输入：**422**，不改变已有版本。
- 版本不符（含同一版本的并发竞争落败方）：**409**，响应体给出 `current_revision`，不改变已有版本。同一版本的并发修订最多只有一次成功，读取结果永远是某次成功提交的完整快照，不会混用两次提交的数据。
- `GET /scenes/{scene_id}`：读取最近一次成功修订保存的完整场景（字幕、分轨、changes、版本）；场景不存在返回 **404**。
- 数据库路径可用环境变量 `SCENES_DB_PATH` 覆盖（Compose 中为 `/data/scenes.db`）。

### 示例 6：创建场景（`expected_revision=0`，成功后版本为 1）

```bash
curl -s -X PUT http://localhost:8080/scenes/scene-001 \
  -H 'Content-Type: application/json' \
  -d '{
    "track_count": 2,
    "expected_revision": 0,
    "subtitles": [
      {"id": "s1", "speaker": "Alice", "start_ms": 0,    "end_ms": 1000},
      {"id": "s2", "speaker": "Bob",   "start_ms": 800,  "end_ms": 3000, "locked_track": 2},
      {"id": "s3", "speaker": "Alice", "start_ms": 1000, "end_ms": 2500}
    ]
  }'
```

响应（HTTP 200）：

```json
{
  "scene_id": "scene-001",
  "revision": 1,
  "track_count": 2,
  "subtitles": [
    {"id": "s1", "speaker": "Alice", "start_ms": 0,    "end_ms": 1000, "locked_track": null},
    {"id": "s2", "speaker": "Bob",   "start_ms": 800,  "end_ms": 3000, "locked_track": 2},
    {"id": "s3", "speaker": "Alice", "start_ms": 1000, "end_ms": 2500, "locked_track": null}
  ],
  "changes": 0,
  "assignments": [
    {"id": "s1", "track": 1},
    {"id": "s2", "track": 2},
    {"id": "s3", "track": 1}
  ],
  "feasible": true
}
```

### 示例 7：在当前版本上修订（`expected_revision` 填当前版本，成功后 +1）

```bash
curl -s -X PUT http://localhost:8080/scenes/scene-001 \
  -H 'Content-Type: application/json' \
  -d '{
    "track_count": 2,
    "expected_revision": 1,
    "subtitles": [
      {"id": "s1", "speaker": "Alice", "start_ms": 0,    "end_ms": 1200},
      {"id": "s2", "speaker": "Bob",   "start_ms": 800,  "end_ms": 3000},
      {"id": "s3", "speaker": "Alice", "start_ms": 1200, "end_ms": 2500}
    ]
  }'
```

响应中的 `"revision": 2`。再次修订时需把 `expected_revision` 改为 `2`。

### 示例 8：读取场景

```bash
curl -s http://localhost:8080/scenes/scene-001
```

返回最近一次成功修订的完整快照（结构同示例 7 的成功响应）。场景不存在时返回 `HTTP/1.1 404 Not Found`。

### 示例 9：版本冲突返回 409（不改动已有版本）

假设场景当前版本为 2，却仍按旧版本 1 提交：

```bash
curl -s -i -X PUT http://localhost:8080/scenes/scene-001 \
  -H 'Content-Type: application/json' \
  -d '{
    "track_count": 2,
    "expected_revision": 1,
    "subtitles": [
      {"id": "s1", "speaker": "Alice", "start_ms": 0, "end_ms": 1000}
    ]
  }'
```

```
HTTP/1.1 409 Conflict
```

```json
{
  "detail": {
    "message": "场景版本冲突：expected_revision 与当前版本不一致",
    "scene_id": "scene-001",
    "current_revision": 2
  }
}
```

此时读取场景，`revision` 仍为 `2`，字幕与分轨仍是修订前的内容。客户端应先 `GET /scenes/scene-001` 取得当前版本，再带上新的 `expected_revision` 重试。对一个已存在的场景提交 `expected_revision: 0`（按新场景创建）同样返回 409；同一版本的并发提交也只有一次成功，其余均返回此响应。

> 合法但无解的场景修订不产生 409/200 快照，而是与示例 3/4 的 `/assign` 一样返回 `{"feasible": false, "changes": 0, "assignments": []}`，且不落库、不推进版本。非法输入（缺少 `expected_revision`、`track_count` 越界、说话人字幕重叠等）返回 422，同样不改动已有版本。

## 场景片段（clip）：把指定时段另存为从 0 毫秒起的独立片段

已保存场景可以把其中的一个时段**另存为全新场景**：`POST /scenes/{scene_id}/clip`。片段的时间轴从 **0 毫秒**重新开始，并**保持源场景的画面轨道**——裁剪与创建是原子的，源场景与任何播放会话都不受影响。

请求体字段：

| 字段 | 类型 | 约束 |
| --- | --- | --- |
| `target_scene_id` | string | 片段的新场景 id，**非空且尚未被占用**（不能与已有场景同名，包括与源同名） |
| `expected_revision` | int | 非负。所依据的**源场景**当前版本 |
| `from_ms` | int | 裁剪起点（毫秒），非负整数；**布尔值与字符串一律 422 拒绝**（不做隐式转换） |
| `to_ms` | int | 裁剪终点（毫秒），非负整数且严格满足 `from_ms < to_ms`；同样拒绝布尔值与字符串 |

裁剪规则：

- 保留与窗口 **`[from_ms, to_ms)`（左闭右开）相交**的字幕，按**源场景的字幕顺序**保留 `id`、`speaker`；时间裁到窗口内（`max(start, from)`、`min(end, to)`）再整体减去 `from_ms`，片段从 0 毫秒起。端点相接不算相交：`end_ms == from_ms` 或 `start_ms == to_ms` 的字幕被丢弃。
- 源场景为每条字幕求出的轨道写入片段字幕的 **`locked_track`**，目标沿用源的 **`track_count`** 与既有最少换轨、并列规则重新求解——源分轨本身就是互不冲突的合法解，因此片段的 `assignments` 与源画面轨道**逐一相同**；`changes` 按裁剪后的片段重算。
- 片段保存为 **revision 1** 的独立场景，返回完整场景快照。之后可直接用既有 `GET/PUT /scenes/...` 与 `/sessions` 读取、修订、播放，**旧接口一律不变**。

状态码：

- **200**：裁剪成功，返回片段完整场景。
- **404**：源场景不存在。
- **409**：`expected_revision` 与源当前版本不一致（源被并发修订）；或 `target_scene_id` 已被占用。裁剪所用的 `track_count`、字幕与源分轨**全部取自与 `expected_revision` 一致的同一份完整快照**，版本核对分两道：**读取源快照时**先核对 `record.revision == expected_revision`（版本不符立即 409，过期与超前同等待遇，绝不拿版本 1 的数据按版本 2 的请求裁剪）；**提交前**在同一个 `BEGIN IMMEDIATE` 事务内再核对一次，覆盖“读取匹配后、提交前源被修订”的窗口。失败既不会留下目标场景，也不会改动源场景或任何播放会话。
- **422**：`from_ms >= to_ms`、时间为负数/布尔值/字符串/浮点数、`target_scene_id` 为空等非法请求；或窗口内**没有任何相交字幕**。

### 示例 10：裁剪时段并另存（时间归零、保持原轨道）

先创建源场景 `scene-clip-src`（s1 `[0,1000)`、s2 `[800,3000)` 锁轨 2、s3 `[1000,2500)`，分轨 1/2/1）：

```bash
curl -s -X PUT http://localhost:8080/scenes/scene-clip-src \
  -H 'Content-Type: application/json' \
  -d '{
    "track_count": 2,
    "expected_revision": 0,
    "subtitles": [
      {"id": "s1", "speaker": "Alice", "start_ms": 0,    "end_ms": 1000},
      {"id": "s2", "speaker": "Bob",   "start_ms": 800,  "end_ms": 3000, "locked_track": 2},
      {"id": "s3", "speaker": "Alice", "start_ms": 1000, "end_ms": 2500}
    ]
  }'
```

把窗口 `[900, 3000)` 另存为 `scene-clip-1`（依据 revision 1）：

```bash
curl -s -X POST http://localhost:8080/scenes/scene-clip-src/clip \
  -H 'Content-Type: application/json' \
  -d '{
    "target_scene_id": "scene-clip-1",
    "expected_revision": 1,
    "from_ms": 900,
    "to_ms": 3000
  }'
```

```json
{
  "scene_id": "scene-clip-1",
  "revision": 1,
  "track_count": 2,
  "subtitles": [
    {"id": "s1", "speaker": "Alice", "start_ms": 0,   "end_ms": 100,  "locked_track": 1},
    {"id": "s2", "speaker": "Bob",   "start_ms": 0,   "end_ms": 2100, "locked_track": 2},
    {"id": "s3", "speaker": "Alice", "start_ms": 100, "end_ms": 1600, "locked_track": 1}
  ],
  "changes": 0,
  "assignments": [
    {"id": "s1", "track": 1},
    {"id": "s2", "track": 2},
    {"id": "s3", "track": 1}
  ],
  "feasible": true
}
```

要点：三条字幕都与 `[900,3000)` 相交，时间裁到窗口后减去 900（s1 只剩尾部 `[0,100)`，s2 变 `[0,2100)`，s3 变 `[100,1600)`）；每条的 `locked_track` 都是源分轨（1/2/1），重算后的 `assignments` 与源画面轨道完全一致。源场景 `scene-clip-src` 仍停留在 revision 1、内容不变。

也可以直接读取验证：

```bash
curl -s http://localhost:8080/scenes/scene-clip-1
```

### 示例 11：裁剪冲突与非法窗口（404 / 409 / 422）

**源不存在 → 404**（不会创建目标）：

```bash
curl -s -i -X POST http://localhost:8080/scenes/no-such-scene/clip \
  -H 'Content-Type: application/json' \
  -d '{"target_scene_id": "scene-x", "expected_revision": 0, "from_ms": 0, "to_ms": 1000}'
# HTTP/1.1 404 Not Found
```

**源版本过期 → 409**（对已存在的源提交 `expected_revision: 0` 同样是 409）：

```bash
curl -s -i -X POST http://localhost:8080/scenes/scene-clip-src/clip \
  -H 'Content-Type: application/json' \
  -d '{"target_scene_id": "scene-stale", "expected_revision": 0, "from_ms": 0, "to_ms": 1000}'
```

```
HTTP/1.1 409 Conflict
```

```json
{"detail": {"message": "场景版本冲突：expected_revision 与当前版本不一致",
            "scene_id": "scene-clip-src", "current_revision": 1}}
```

**目标已被占用 → 409**（重复使用示例 10 的目标 id；已有目标不会被覆盖，源也不变）：

```bash
curl -s -i -X POST http://localhost:8080/scenes/scene-clip-src/clip \
  -H 'Content-Type: application/json' \
  -d '{"target_scene_id": "scene-clip-1", "expected_revision": 1, "from_ms": 0, "to_ms": 1000}'
# HTTP/1.1 409 Conflict
# {"detail":{"message":"target_scene_id 已被占用，裁剪目标必须使用新的 scene_id",
#            "scene_id":"scene-clip-1","current_revision":1}}
```

**窗口内没有相交字幕 → 422**（三条字幕都在 5000 之前结束；不留目标、不改源）：

```bash
curl -s -i -X POST http://localhost:8080/scenes/scene-clip-src/clip \
  -H 'Content-Type: application/json' \
  -d '{"target_scene_id": "scene-empty", "expected_revision": 1, "from_ms": 5000, "to_ms": 6000}'
# HTTP/1.1 422 Unprocessable Entity
# {"detail":{"message":"裁剪窗口 [from_ms, to_ms) 内没有与任何字幕相交的时段",
#            "scene_id":"scene-clip-src","from_ms":5000,"to_ms":6000}}
```

其余非法请求同样返回 422（Pydantic 校验，均不落库）：

```bash
# from_ms 不小于 to_ms
curl -s -o /dev/null -w '%{http_code}\n' -X POST http://localhost:8080/scenes/scene-clip-src/clip \
  -H 'Content-Type: application/json' \
  -d '{"target_scene_id": "scene-bad", "expected_revision": 1, "from_ms": 3000, "to_ms": 900}'
# 422

# 时间字段拒绝布尔值（true 在 JSON 里不是整数 1）和字符串
curl -s -o /dev/null -w '%{http_code}\n' -X POST http://localhost:8080/scenes/scene-clip-src/clip \
  -H 'Content-Type: application/json' \
  -d '{"target_scene_id": "scene-bad", "expected_revision": 1, "from_ms": true, "to_ms": 1000}'
# 422
curl -s -o /dev/null -w '%{http_code}\n' -X POST http://localhost:8080/scenes/scene-clip-src/clip \
  -H 'Content-Type: application/json' \
  -d '{"target_scene_id": "scene-bad", "expected_revision": 1, "from_ms": 0, "to_ms": "1000"}'
# 422
```

### 示例 12：裁剪版本冲突——读取即核对，按当前版本重试

裁剪所用的 `track_count`、字幕与源分轨全部取自与 `expected_revision` 一致的**同一份完整源快照**：读取源快照时先核对版本（不符立即 409，绝不拿错配版本裁剪），提交前在同一事务内再核对一次。下面是可直接复制执行的完整脚本。

先创建源场景（revision 1）：

```bash
curl -s -X PUT http://localhost:8080/scenes/scene-clip-conf \
  -H 'Content-Type: application/json' \
  -d '{
    "track_count": 2,
    "expected_revision": 0,
    "subtitles": [
      {"id": "s1", "speaker": "Alice", "start_ms": 0,    "end_ms": 1000},
      {"id": "s2", "speaker": "Bob",   "start_ms": 800,  "end_ms": 3000, "locked_track": 2},
      {"id": "s3", "speaker": "Alice", "start_ms": 1000, "end_ms": 2500}
    ]
  }' >/dev/null
```

**读取时版本不符 → 409**（请求依据的是尚不存在的 revision 2，源当前为 revision 1；过期版本同理）：

```bash
curl -s -i -X POST http://localhost:8080/scenes/scene-clip-conf/clip \
  -H 'Content-Type: application/json' \
  -d '{"target_scene_id": "scene-clip-conf-out", "expected_revision": 2, "from_ms": 0, "to_ms": 3000}'
```

```
HTTP/1.1 409 Conflict
```

```json
{"detail": {"message": "场景版本冲突：expected_revision 与当前版本不一致",
            "scene_id": "scene-clip-conf", "current_revision": 1}}
```

失败不创建目标（源、播放会话同样不变）：

```bash
curl -s -o /dev/null -w '%{http_code}\n' http://localhost:8080/scenes/scene-clip-conf-out
# 404
```

客户端应先 `GET /scenes/scene-clip-conf` 取得当前版本再重试。把源修订到 revision 2（s3 延长到 4000，新增 s4），然后带上 `expected_revision: 2` 重试裁剪：

```bash
# 源：revision 1 -> 2
curl -s -X PUT http://localhost:8080/scenes/scene-clip-conf \
  -H 'Content-Type: application/json' \
  -d '{
    "track_count": 2,
    "expected_revision": 1,
    "subtitles": [
      {"id": "s1", "speaker": "Alice", "start_ms": 0,    "end_ms": 1000},
      {"id": "s2", "speaker": "Bob",   "start_ms": 800,  "end_ms": 3000, "locked_track": 2},
      {"id": "s3", "speaker": "Alice", "start_ms": 1000, "end_ms": 4000},
      {"id": "s4", "speaker": "Carol", "start_ms": 3200, "end_ms": 4000}
    ]
  }' >/dev/null

# 依据 revision 2 裁剪窗口 [0, 3200)：s4 start==to 不相交
curl -s -X POST http://localhost:8080/scenes/scene-clip-conf/clip \
  -H 'Content-Type: application/json' \
  -d '{"target_scene_id": "scene-clip-conf-out", "expected_revision": 2, "from_ms": 0, "to_ms": 3200}'
```

```json
{
  "scene_id": "scene-clip-conf-out",
  "revision": 1,
  "track_count": 2,
  "subtitles": [
    {"id": "s1", "speaker": "Alice", "start_ms": 0,    "end_ms": 1000, "locked_track": 1},
    {"id": "s2", "speaker": "Bob",   "start_ms": 800,  "end_ms": 3000, "locked_track": 2},
    {"id": "s3", "speaker": "Alice", "start_ms": 1000, "end_ms": 3200, "locked_track": 1}
  ],
  "changes": 0,
  "assignments": [
    {"id": "s1", "track": 1},
    {"id": "s2", "track": 2},
    {"id": "s3", "track": 1}
  ],
  "feasible": true
}
```

片段内容严格来自 revision 2 的快照（含延长后的 s3、且不含 revision 2 才出现的窗口外 s4），目标仍保存为 revision 1。

**提交前源被修订 → 同样 409**：第二道核对覆盖“读取匹配后、提交前”的窗口。并发地同时发起裁剪与源修订时，二者被数据库写锁串行——裁剪先生效则得到与某一完整版本一致的片段（恒为 revision 1），修订先生效则裁剪返回 409 且 `current_revision` 为新版本，绝不会把旧快照的字幕/分轨存成新版本片段：

```bash
# 先把源重建为 revision 1，再并发：裁剪(期望 1) 与 修订(1 -> 2)
curl -s -X PUT http://localhost:8080/scenes/scene-clip-race \
  -H 'Content-Type: application/json' \
  -d '{"track_count": 2, "expected_revision": 0,
       "subtitles": [
         {"id": "s1", "speaker": "Alice", "start_ms": 0, "end_ms": 1000},
         {"id": "s2", "speaker": "Bob", "start_ms": 800, "end_ms": 3000, "locked_track": 2}
       ]}' >/dev/null

curl -s -w '\nclip:%{http_code}\n' -X POST http://localhost:8080/scenes/scene-clip-race/clip \
  -H 'Content-Type: application/json' \
  -d '{"target_scene_id": "scene-clip-race-out", "expected_revision": 1, "from_ms": 0, "to_ms": 3000}' &
curl -s -o /dev/null -X PUT http://localhost:8080/scenes/scene-clip-race \
  -H 'Content-Type: application/json' \
  -d '{"track_count": 2, "expected_revision": 1,
       "subtitles": [{"id": "only", "speaker": "Solo", "start_ms": 0, "end_ms": 50}]}' &
wait
# clip:200 -> 片段含 s1、s2（revision 1 的完整快照）；clip:409 -> current_revision 为 2，无目标残留
```

### 示例 13：播放裁剪出来的片段（沿用既有会话接口）

片段就是普通场景，用原来的 `/sessions` 接口播放即可。对示例 10 的 `scene-clip-1`（s1 `[0,100)`、s2 `[0,2100)`、s3 `[100,1600)`，轨道 1/2/1）创建会话并事件推进到 2100：

```bash
CID=$(curl -s -X POST http://localhost:8080/sessions \
  -H 'Content-Type: application/json' \
  -d '{"scene_id": "scene-clip-1"}' \
  | python3 -c 'import sys,json; print(json.load(sys.stdin)["session_id"])')

# 0 毫秒已活动的 s1、s2 由创建响应快照确定；直接事件推进到 2100
curl -s -X POST http://localhost:8080/sessions/$CID/events \
  -H 'Content-Type: application/json' -d '{"time_ms": 2100}'
```

```json
{
  "session_id": "…",
  "scene_id": "scene-clip-1",
  "source_revision": 1,
  "cursor_ms": 2100,
  "events": [
    {"id": "s1", "speaker": "Alice", "track": 1, "time_ms": 100,  "type": "exit"},
    {"id": "s3", "speaker": "Alice", "track": 1, "time_ms": 100,  "type": "enter"},
    {"id": "s3", "speaker": "Alice", "track": 1, "time_ms": 1600, "type": "exit"},
    {"id": "s2", "speaker": "Bob",   "track": 2, "time_ms": 2100, "type": "exit"}
  ],
  "active": []
}
```

片段时间轴从 0 开始：s1 在 100 毫秒退出与 s3 在 100 毫秒进入同刻，按既有规则**先退出后进入**；s1、s2 在 0 毫秒已活动，其进入事件由创建会话的响应快照确定，事件流不补发。片段还可以按 `expected_revision: 1` 用 `PUT /scenes/scene-clip-1` 继续修订（成功后 revision 2），与源场景互不影响。

## 场景拼接（concat）：按给定顺序拼成连续播放场景

2～4 个已保存场景可以按给定顺序**拼接为一个连续播放的新场景**：`POST /scenes/concat`。每一段在连续时间轴上占用 `duration_ms` 毫秒，段内字幕整体平移到“之前各段时长之和”之后；拼接结果保存为 **revision 1** 的全新场景，全部源场景与任何播放会话都不受影响。

请求体字段：

| 字段 | 类型 | 约束 |
| --- | --- | --- |
| `target_scene_id` | string | 拼接结果的新场景 id，**非空且尚未被占用**（不能与已有场景同名，包括与某个源同名） |
| `segments` | array | **2～4 段**，数组顺序即播放顺序 |
| `segments[].scene_id` | string | 本段引用的已保存场景 id，**非空**；同一个源可以重复出现 |
| `segments[].expected_revision` | int | **正整数**。所依据的该源当前版本；**布尔值与字符串一律 422 拒绝**（不做隐式转换） |
| `segments[].duration_ms` | int | **正整数**。本段在连续播放时间轴上占用的毫秒数，**不得小于该源字幕的最大 `end_ms`**；同样拒绝布尔值与字符串 |

拼接规则：

- 字幕起止时间**加上之前各段 `duration_ms` 之和**（第 1 段偏移 0，第 2 段偏移第 1 段时长，以此类推），按**段序、段内源顺序**排列。
- 新 id 恒为字符串 **“段号:段内序号”**（均从 1 起，如 `1:1`、`1:2`、`2:1`）；`speaker` 沿用源字幕不变。
- 每个源场景为每条字幕求出的**实际轨道写入拼接字幕的 `locked_track`**，目标沿用各源共同的 **`track_count`** 与既有最少换轨、并列规则重新求解——各源分轨平移后仍互不冲突且与锁定一致，因此拼接结果的 `assignments` 与各源画面轨道**逐一相同**。
- **`changes` 按拼接后的时间轴重新计算**：同一说话人按时间相邻的两条字幕（含跨段相邻）换轨次数总和。各段时长允许大于内容时长（段尾留静音）。
- 跨源合法性：**各源 `track_count` 必须相同**；**每段 `duration_ms` 不得小于该源字幕的最大 `end_ms`**；**拼接后字幕总数 ≤ 12**（每源本身 1～12 条）。任一不满足返回 422。

状态码：

- **200**：拼接成功，返回 revision 1 的完整场景快照。
- **404**：按段序读取时任一源场景不存在（不创建目标）。
- **409**：任一源的 `expected_revision` 与其当前版本不一致（源被并发修订；同源重复时每一次出现都单独核对）；或 `target_scene_id` 已被占用。拼接所用的字幕与源分轨**全部取自与各段 `expected_revision` 一致的同一份完整快照**，版本核对分两道：**读取各源快照时**按段序先核对（不存在立即 404、不符立即 409）；**提交时**在同一个 `BEGIN IMMEDIATE` 事务内再核对**全部源版本**并占用目标——**提交核对与目标创建一同生效**，覆盖“读取匹配后、提交前任一源被修订”的窗口。失败不留目标、不改源场景或播放会话。
- **422**：段数不在 2～4、缺字段/空 id、版本号或时长非正整数（含布尔值/字符串/浮点）、各源轨道数不一致、某段时长小于该源最大 `end_ms`、字幕总数超过 12 等。

### 示例 14：拼接两个场景（时间平移、保轨、跨段 changes）

先创建两个 revision 1 的源场景：

```bash
# 源 1：Alice[0,1000)、Bob[800,3000) 锁轨 2；分轨 1/2，max_end=3000
curl -s -X PUT http://localhost:8080/scenes/scene-cat-a \
  -H 'Content-Type: application/json' \
  -d '{
    "track_count": 2,
    "expected_revision": 0,
    "subtitles": [
      {"id": "a1", "speaker": "Alice", "start_ms": 0,    "end_ms": 1000},
      {"id": "a2", "speaker": "Bob",   "start_ms": 800,  "end_ms": 3000, "locked_track": 2}
    ]
  }' >/dev/null

# 源 2：Alice[0,1500) 与 Bob[0,1500) 重叠；分轨 Alice=1、Bob=2，max_end=1500
curl -s -X PUT http://localhost:8080/scenes/scene-cat-b \
  -H 'Content-Type: application/json' \
  -d '{
    "track_count": 2,
    "expected_revision": 0,
    "subtitles": [
      {"id": "b1", "speaker": "Alice", "start_ms": 0, "end_ms": 1500},
      {"id": "b2", "speaker": "Bob",   "start_ms": 0, "end_ms": 1500}
    ]
  }' >/dev/null
```

按段序拼接：第 1 段占用 3000 毫秒（恰好等于源 1 的最大 `end_ms`），第 2 段占用 2000 毫秒（大于内容 1500，段尾留 500 毫秒静音）：

```bash
curl -s -X POST http://localhost:8080/scenes/concat \
  -H 'Content-Type: application/json' \
  -d '{
    "target_scene_id": "scene-cat-out",
    "segments": [
      {"scene_id": "scene-cat-a", "expected_revision": 1, "duration_ms": 3000},
      {"scene_id": "scene-cat-b", "expected_revision": 1, "duration_ms": 2000}
    ]
  }'
```

```json
{
  "scene_id": "scene-cat-out",
  "revision": 1,
  "track_count": 2,
  "subtitles": [
    {"id": "1:1", "speaker": "Alice", "start_ms": 0,    "end_ms": 1000, "locked_track": 1},
    {"id": "1:2", "speaker": "Bob",   "start_ms": 800,  "end_ms": 3000, "locked_track": 2},
    {"id": "2:1", "speaker": "Alice", "start_ms": 3000, "end_ms": 4500, "locked_track": 1},
    {"id": "2:2", "speaker": "Bob",   "start_ms": 3000, "end_ms": 4500, "locked_track": 2}
  ],
  "changes": 0,
  "assignments": [
    {"id": "1:1", "track": 1},
    {"id": "1:2", "track": 2},
    {"id": "2:1", "track": 1},
    {"id": "2:2", "track": 2}
  ],
  "feasible": true
}
```

要点：第 2 段字幕整体加上前一段时长 3000（`b1` 从 `[0,1500)` 变为 `[3000,4500)`）；id 变为“段号:段内序号”；各条 `locked_track` 都是源实际分轨，重算后轨道不变；Alice 的 `a1` 与 `b1`、Bob 的 `a2` 与 `b2` 分别跨段时间相邻且同轨，`changes` 为 0。源场景 `scene-cat-a`、`scene-cat-b` 均保持 revision 1 不变。拼接结果是普通场景，可直接读取、修订（`PUT` 带 `expected_revision: 1`）和播放：

```bash
curl -s -X POST http://localhost:8080/sessions \
  -H 'Content-Type: application/json' \
  -d '{"scene_id": "scene-cat-out"}'
```

### 示例 15：同源重复拼接

同一个源可以在 `segments` 中重复出现，每次出现都单独核对版本；段序播放同一内容两遍：

```bash
curl -s -X POST http://localhost:8080/scenes/concat \
  -H 'Content-Type: application/json' \
  -d '{
    "target_scene_id": "scene-cat-repeat",
    "segments": [
      {"scene_id": "scene-cat-a", "expected_revision": 1, "duration_ms": 3000},
      {"scene_id": "scene-cat-a", "expected_revision": 1, "duration_ms": 3000}
    ]
  }'
```

第 2 段字幕偏移 3000 毫秒，id 为 `2:1`、`2:2`。若重复出现的某一次版本填错（如源已到 revision 2 仍填 1），409 响应的 `detail.segment` 指出是第几段冲突。

### 示例 16：拼接冲突与非法输入（404 / 409 / 422）

**源不存在 → 404**（按段序读取时发现，不留目标）：

```bash
curl -s -i -X POST http://localhost:8080/scenes/concat \
  -H 'Content-Type: application/json' \
  -d '{"target_scene_id": "scene-x",
       "segments": [
         {"scene_id": "no-such-scene", "expected_revision": 1, "duration_ms": 100},
         {"scene_id": "scene-cat-a",   "expected_revision": 1, "duration_ms": 3000}
       ]}'
# HTTP/1.1 404 Not Found
```

**源版本不符 → 409**（下面第 2 段依据不存在的 revision 2；源被并发修订同理。`detail.segment` 指明冲突段号，`current_revision` 为该源当前版本）：

```bash
curl -s -i -X POST http://localhost:8080/scenes/concat \
  -H 'Content-Type: application/json' \
  -d '{"target_scene_id": "scene-stale",
       "segments": [
         {"scene_id": "scene-cat-a", "expected_revision": 1, "duration_ms": 3000},
         {"scene_id": "scene-cat-b", "expected_revision": 2, "duration_ms": 2000}
       ]}'
```

```
HTTP/1.1 409 Conflict
```

```json
{"detail": {"message": "场景版本冲突：expected_revision 与当前版本不一致",
            "scene_id": "scene-cat-b", "segment": 2, "current_revision": 1}}
```

**目标已被占用 → 409**（已有目标不会被覆盖，源也不变；目标与某个源同名同样 409）：

```bash
curl -s -i -X POST http://localhost:8080/scenes/concat \
  -H 'Content-Type: application/json' \
  -d '{"target_scene_id": "scene-cat-a",
       "segments": [
         {"scene_id": "scene-cat-a", "expected_revision": 1, "duration_ms": 3000},
         {"scene_id": "scene-cat-b", "expected_revision": 1, "duration_ms": 2000}
       ]}'
# HTTP/1.1 409 Conflict
# {"detail":{"message":"target_scene_id 已被占用，拼接目标必须使用新的 scene_id",
#            "scene_id":"scene-cat-a","current_revision":1}}
```

其余非法请求一律 422（Pydantic 校验与跨源业务校验，均不落库）：

```bash
# 段数必须 2～4（下面只有 1 段）
curl -s -o /dev/null -w '%{http_code}\n' -X POST http://localhost:8080/scenes/concat \
  -H 'Content-Type: application/json' \
  -d '{"target_scene_id": "scene-bad",
       "segments": [{"scene_id": "scene-cat-a", "expected_revision": 1, "duration_ms": 3000}]}'
# 422

# expected_revision / duration_ms 拒绝布尔值与字符串（正整数严格校验）
curl -s -o /dev/null -w '%{http_code}\n' -X POST http://localhost:8080/scenes/concat \
  -H 'Content-Type: application/json' \
  -d '{"target_scene_id": "scene-bad",
       "segments": [
         {"scene_id": "scene-cat-a", "expected_revision": true, "duration_ms": 3000},
         {"scene_id": "scene-cat-b", "expected_revision": 1,    "duration_ms": 2000}
       ]}'
# 422

# duration_ms 小于该源字幕的最大 end_ms（源 scene-cat-a 最大为 3000）
curl -s -i -X POST http://localhost:8080/scenes/concat \
  -H 'Content-Type: application/json' \
  -d '{"target_scene_id": "scene-bad",
       "segments": [
         {"scene_id": "scene-cat-a", "expected_revision": 1, "duration_ms": 2999},
         {"scene_id": "scene-cat-b", "expected_revision": 1, "duration_ms": 2000}
       ]}'
# HTTP/1.1 422 Unprocessable Entity
# {"detail":{"message":"duration_ms 不得小于该源场景字幕的最大 end_ms",
#            "segment":1,"duration_ms":2999,"max_end_ms":3000}}

# 各源轨道数不一致 / 拼接后字幕总数超过 12 同样返回 422
```

**提交前源被修订 → 同样 409**：第二道核对在单个 `BEGIN IMMEDIATE` 事务内与目标创建一同生效。并发地同时发起拼接与源修订时，二者被数据库写锁串行——拼接先生效则得到与各源某一完整版本一致的新场景（恒为 revision 1），修订先生效则拼接返回 409 且 `current_revision` 为新版本，绝不会把旧快照存进目标；任何失败都不创建目标、不改动源场景或播放会话。

## 播放会话（session）：播放中接纳后续修订

`/assign` 与 `/scenes` 都是无状态的计算/存储；播放会话则让“正在播放”的场景跟上后续修订：先按 `scene_id` 创建会话（复制场景当时的完整快照，游标从 0 毫秒开始），播放过程中推进游标，场景被修订后让会话接纳最新快照，播放结束时一次取回最终快照并释放会话。会话是**单进程内存态**，不写入 SQLite、不跨重启保留；同一会话上的推进、事件推进、接纳与结束按完整操作串行生效（并发请求不会交错出中间态）。接纳的完整操作包含**场景读取本身**：请求在会话锁内才读取场景快照，因此并发下每个接纳请求应用的都是轮到它生效时的快照，会话的 `source_revision` 不会被迟到的请求回退。结束的完整操作在会话锁内置墓碑并移除注册表条目：快照取自轮到结束时的会话（不读取尚未接纳的场景修订），并发结束最多一次成功，已取得会话引用但轮到它时会话已结束的请求一律 404。

- `POST /sessions`：创建会话。请求体 `{"scene_id": "..."}`；场景不存在返回 404。响应为会话完整状态：`session_id`、`scene_id`、`source_revision`（快照来源的场景版本）、`cursor_ms`（初始 0）、`track_count`、`subtitles`、`assignments`、`changes`。
- `POST /sessions/{session_id}/advance`：推进游标。请求体 `{"time_ms": N}`，`N` 为非负整数且**只能不减**（等于当前游标允许）；负数、非整数或倒退返回 422 且会话不变。响应含 `source_revision`、`cursor_ms` 与 `active`：游标时刻处于 `[start_ms, end_ms)`（左闭右开）区间的字幕及其轨道。
- `POST /sessions/{session_id}/events`：**事件推进**。请求体与推进规则（非负整数、只能不减、422/404、共用同一游标）完全相同；额外返回本次推进跨过的**全部**字幕进入/退出事件，避免短字幕在两次采样之间漏掉：
  - 事件窗口为**（旧游标, time_ms]**（`旧游标 < 事件时刻 ≤ time_ms`）：字幕在 `start_ms` 进入（`type: "enter"`）、在 `end_ms` 退出（`type: "exit"`，区间左闭右开）。一条字幕开始和结束都落在窗口内时，进入与退出成对返回。
  - **0 毫秒已活动的字幕（`start_ms` 为 0）由创建会话响应的快照确定**：初始游标即 0，其进入时刻不在任何推进窗口内，事件流不会补发它的进入——客户端应以创建响应快照（字幕与分轨）自行确定初始活动集合。
  - 每个事件含字幕 `id`、`speaker`、`track`、毫秒时刻 `time_ms` 与类型 `type`（`enter`/`exit`）。事件按 `time_ms` **升序**；同一时刻**先退出后进入**；同类型按**会话字幕顺序**（接纳后即新快照顺序）。
  - 响应还含推进后的 `active`（同 `/advance` 的 `[start_ms, end_ms)` 活动字幕）、`source_revision` 与推进后的 `cursor_ms`。**等时推进**（`time_ms` 等于当前游标）`events` 为空。
  - 使用轮到该请求生效时的会话快照生成事件（接纳后的游标之后推进，事件与轨道来自接纳后的新快照），不读取未被接纳的场景修订；事件生成、游标更新与活动字幕计算在会话锁内与原推进、接纳**完整串行**。
  - 事件只随本次推进生成、即生即返：**不保存事件历史、不提供重放**；重复推进同一时刻得到空事件。
  - 未知会话（含服务重启后）返回 404；非法时间或游标倒退返回 422；任何失败都不改变会话与场景。
- `POST /sessions/{session_id}/hidden_speakers`：**临时隐藏说话人**（仅影响该会话，不持久化）。请求体 `{"hidden_speakers": ["Alice", …]}`：
  - 值为**无重复的非空字符串数组**，按 `speaker` **精确匹配**（区分大小写、不去空白）；**空数组 `[]` 清除全部隐藏**；允许包含会话字幕中**尚未出现**（甚至永不出现）的说话人。非数组/缺失/`null`、元素不是字符串（数字、布尔、浮点等不做隐式转换）、空字符串、重复元素一律 **422**。
  - 切换**不移动游标**：以当前游标的左闭右开活动区间 `[start_ms, end_ms)` 判断显示变化——**原可见而现隐藏**的活动字幕发 `exit`，**原隐藏而现可见**者发 `enter`。事件沿用 `/events` 的字段（`id`、`speaker`、`track`、`time_ms`、`type`），`time_ms` 取**当前游标**；先全部 `exit` 后全部 `enter`，同类型按**会话字幕顺序**；新旧集合下显示状态相同（含非活动字幕）则无事件，集合无变化时 `events` 为空。
  - 响应返回 `hidden_speakers`（去重后按提交顺序回显的当前集合）、`cursor_ms`、`source_revision`、切换后可见的 `active` 与显示变更 `events`。
  - 设置成功后，`/advance` 的 `active` 与 `/events` 的 `events`/`active` **均排除隐藏者**（隐藏期间整条开始又结束的短字幕同样不会出现在事件流里），但**游标照常推进**；隐藏期间已整条播过的字幕在恢复显示时不补发 `enter`（只看当前游标是否活动）。
  - 隐藏**默认空**；`/accept` 接纳场景修订后隐藏集合**原样保留**；完整快照（创建/接纳/`finish` 的 `subtitles`、`assignments`、`changes`）、已开始字幕的冻结规则与分轨求解**完全不受影响**。设置只存在会话内存中，不写入 SQLite、不跨重启保留。
  - 切换与同会话的推进、事件推进、接纳、结束在会话锁内**串行生效**；未知或已结束会话返回 **404**，非法集合返回 **422**，失败不改变任何状态。
- `POST /sessions/{session_id}/accept`：接纳场景当前快照。规则：
  - 轨道数不得变；
  - 会话中 `start_ms` ≤ 游标的字幕（已开始，含已播完），其 `id`、`speaker`、起止时间、`locked_track` 必须不变，并**固定原分轨**；
  - 其余字幕可改、可删、可新增，但都必须在游标之后开始（`start_ms` > 游标）；
  - 按新快照顺序与最少换轨、并列规则重算全部分轨。
  - 场景读取、上述校验、分轨重算与会话替换是同一个完整操作，与同一会话的推进、其他接纳串行生效：并发接纳时，每个请求接纳的都是轮到它生效时的场景快照。
  - 冲突或无解返回 **409** 且会话不变；成功则整体替换会话快照与 `source_revision`（**场景本身不变**），返回会话最新完整状态。
- `POST /sessions/{session_id}/finish`：**播放结束，取回最终快照并释放会话**。无请求体；以一次完整操作在会话锁内标记结束、移除注册表条目并返回最终快照：
  - 成功 **200** 响应**沿用创建会话的全部字段**（`session_id`、`scene_id`、`source_revision`、`cursor_ms`、`track_count`、`changes`、`subtitles`、`assignments`）；快照取自**轮到结束生效时**的会话状态（含当时游标与已接纳快照），**不读取尚未接纳的场景修订**；
  - 未知会话返回 **404**；并发结束**最多一次成功**，落败方返回 404；结束后针对该 id 的推进、事件推进、接纳、重复结束**一律 404**——包括已经取得会话引用、但在会话锁上等待、轮到它时会话已结束的请求；
  - 结束与同会话的推进、事件推进、接纳**串行生效**（等待中的操作若先持锁则正常生效并进入最终快照，随后结束再生效）；
  - 结束**不改动源场景、其他会话或此前返回的快照**；结束不保存记录、不跨重启恢复（重启后该 id 同样 404）。场景本身仍可读取，也可基于它创建新会话。
- 会话不存在或已结束（含服务重启后）均返回 404。

### 示例 17：创建会话并推进游标

沿用示例 6 创建的场景 `scene-001`（revision 1）：

```bash
curl -s -X POST http://localhost:8080/sessions \
  -H 'Content-Type: application/json' \
  -d '{"scene_id": "scene-001"}'
```

```json
{
  "session_id": "9f2c…（略）",
  "scene_id": "scene-001",
  "source_revision": 1,
  "cursor_ms": 0,
  "track_count": 2,
  "changes": 0,
  "subtitles": [ … ],
  "assignments": [
    {"id": "s1", "track": 1},
    {"id": "s2", "track": 2},
    {"id": "s3", "track": 1}
  ]
}
```

推进到 900 毫秒（`$SID` 为上一步返回的 `session_id`）：

```bash
curl -s -X POST http://localhost:8080/sessions/$SID/advance \
  -H 'Content-Type: application/json' \
  -d '{"time_ms": 900}'
```

```json
{
  "session_id": "9f2c…",
  "scene_id": "scene-001",
  "source_revision": 1,
  "cursor_ms": 900,
  "active": [
    {"id": "s1", "speaker": "Alice", "start_ms": 0, "end_ms": 1000, "locked_track": null, "track": 1},
    {"id": "s2", "speaker": "Bob", "start_ms": 800, "end_ms": 3000, "locked_track": 2, "track": 2}
  ]
}
```

`active` 为游标时刻 `[start_ms, end_ms)` 内的字幕及轨道：900 时 s1（0≤900<1000）与 s2（800≤900<3000）在屏，s3（1000 才开始）不在。再次提交更小的 `time_ms` 返回 422。

### 示例 18：事件推进一次跨过整条短字幕（进入与退出都不漏）

新建一个含短字幕的场景与会话：短字幕 `b` 只在 `[500, 600)` 出现，若播放客户端在 400 与 1000 两次采样，用 `/advance` 就完全看不到它；用 `/events` 则在跨过它的这次推进里成对拿到进入与退出。

```bash
# 场景：a[0,1000)，短字幕 b[500,600)
curl -s -X PUT http://localhost:8080/scenes/scene-events \
  -H 'Content-Type: application/json' \
  -d '{
    "track_count": 2,
    "expected_revision": 0,
    "subtitles": [
      {"id": "a", "speaker": "A", "start_ms": 0,   "end_ms": 1000},
      {"id": "b", "speaker": "B", "start_ms": 500, "end_ms": 600}
    ]
  }'

SID=$(curl -s -X POST http://localhost:8080/sessions \
  -H 'Content-Type: application/json' \
  -d '{"scene_id": "scene-events"}' | python3 -c 'import sys,json; print(json.load(sys.stdin)["session_id"])')

# 第一次推进到 400：窗口 (0,400] 内无任何进入/退出（a 在 0 毫秒已活动，
# 其进入由上面创建响应的快照确定，事件流不补发）
curl -s -X POST http://localhost:8080/sessions/$SID/events \
  -H 'Content-Type: application/json' -d '{"time_ms": 400}'
```

```json
{
  "session_id": "…",
  "scene_id": "scene-events",
  "source_revision": 1,
  "cursor_ms": 400,
  "events": [],
  "active": [
    {"id": "a", "speaker": "A", "start_ms": 0, "end_ms": 1000, "locked_track": null, "track": 1}
  ]
}
```

```bash
# 第二次直接推进到 1000：窗口 (400,1000] 完整跨过短字幕 b
curl -s -X POST http://localhost:8080/sessions/$SID/events \
  -H 'Content-Type: application/json' -d '{"time_ms": 1000}'
```

```json
{
  "session_id": "…",
  "scene_id": "scene-events",
  "source_revision": 1,
  "cursor_ms": 1000,
  "events": [
    {"id": "b", "speaker": "B", "track": 2, "time_ms": 500,  "type": "enter"},
    {"id": "b", "speaker": "B", "track": 2, "time_ms": 600,  "type": "exit"},
    {"id": "a", "speaker": "A", "track": 1, "time_ms": 1000, "type": "exit"}
  ],
  "active": []
}
```

要点：

- 短字幕 `b` 的进入（500）与退出（600）都落在 (400,1000] 内，成对返回，不会因为两次采样间隔而漏掉；
- 事件按 `time_ms` 升序；同刻先 `exit` 后 `enter`（例如端点相接时前一条的退出排在后一条的进入之前）；同为进入或同为退出时按会话字幕顺序；
- 0 毫秒已活动的 `a` 不在事件流里给进入事件——初始活动集合由创建会话的响应快照（`subtitles` + `assignments`，游标 0）确定；
- 再对同一时刻推进一次为**等时推进**，`events` 为空：事件不保存历史、不提供重放。

```bash
curl -s -X POST http://localhost:8080/sessions/$SID/events \
  -H 'Content-Type: application/json' -d '{"time_ms": 1000}'
# {"session_id":"…","source_revision":1,"cursor_ms":1000,"events":[],"active":[]}
```

### 示例 19：两个推进端点交接（共用游标，原 /advance 响应不变）

`/advance` 与 `/events` 操作同一个会话游标，可以混用交接：客户端平时只需活动快照时用 `/advance`（响应仍是原有的 `source_revision`/`cursor_ms`/`active`，不含 `events`），需要补全跨过的进入/退出时用 `/events`。沿用示例 6 的 `scene-001`（s1 `[0,1000)`、s2 `[800,3000)` 锁轨 2、s3 `[1000,2500)`）：

```bash
SID=$(curl -s -X POST http://localhost:8080/sessions \
  -H 'Content-Type: application/json' \
  -d '{"scene_id": "scene-001"}' | python3 -c 'import sys,json; print(json.load(sys.stdin)["session_id"])')

# 原 /advance 先推进到 900（响应保持原样，没有 events 字段）
curl -s -X POST http://localhost:8080/sessions/$SID/advance \
  -H 'Content-Type: application/json' -d '{"time_ms": 900}'
# {"session_id":"…","source_revision":1,"cursor_ms":900,
#  "active":[{"id":"s1",…,"track":1},{"id":"s2",…,"track":2}]}

# 再由 /events 推进到 1000：窗口为 (900,1000]，
# 不会补发 800 时 s2 的进入（那已被旧游标 900 跨过）
curl -s -X POST http://localhost:8080/sessions/$SID/events \
  -H 'Content-Type: application/json' -d '{"time_ms": 1000}'
```

```json
{
  "session_id": "…",
  "scene_id": "scene-001",
  "source_revision": 1,
  "cursor_ms": 1000,
  "events": [
    {"id": "s1", "speaker": "Alice", "track": 1, "time_ms": 1000, "type": "exit"},
    {"id": "s3", "speaker": "Alice", "track": 1, "time_ms": 1000, "type": "enter"}
  ],
  "active": [
    {"id": "s2", "speaker": "Bob", "start_ms": 800, "end_ms": 3000, "locked_track": 2, "track": 2},
    {"id": "s3", "speaker": "Alice", "start_ms": 1000, "end_ms": 2500, "locked_track": null, "track": 1}
  ]
}
```

1000 时刻 s1 退出（右开）与 s3 进入（左闭）同刻，**先退出后进入**。此后再用任一端点把时间往回（999）推进都返回 422，会话不变：

```bash
curl -s -i -X POST http://localhost:8080/sessions/$SID/events \
  -H 'Content-Type: application/json' -d '{"time_ms": 999}'
# HTTP/1.1 422 Unprocessable Entity
# {"detail":{"message":"time_ms 只能不减，不得小于当前游标","cursor_ms":1000,"time_ms":999}}
```

未知会话返回 404（与 `/advance` 一致）：

```bash
curl -s -i -X POST http://localhost:8080/sessions/no-such-session/events \
  -H 'Content-Type: application/json' -d '{"time_ms": 0}'
# HTTP/1.1 404 Not Found
```

### 示例 20：接纳修订后再事件推进（事件与轨道来自接纳后的新快照）

下面是一段可直接复制执行的完整脚本：创建 revision 1 → 建会话并推进到 900（s1、s2 已开始）→ 场景修订为 revision 2（s3 延长到 `[1000,4000)`，新增 s4 `[3200,4000)`）→ 会话接纳 → 用 `/events` 一次推进到 4000。

```bash
# 1) revision 1：s1[0,1000)、s2[800,3000) 锁轨 2、s3[1000,2500)
curl -s -X PUT http://localhost:8080/scenes/scene-evt-accept \
  -H 'Content-Type: application/json' \
  -d '{
    "track_count": 2,
    "expected_revision": 0,
    "subtitles": [
      {"id": "s1", "speaker": "Alice", "start_ms": 0,    "end_ms": 1000},
      {"id": "s2", "speaker": "Bob",   "start_ms": 800,  "end_ms": 3000, "locked_track": 2},
      {"id": "s3", "speaker": "Alice", "start_ms": 1000, "end_ms": 2500}
    ]
  }' >/dev/null

SID=$(curl -s -X POST http://localhost:8080/sessions \
  -H 'Content-Type: application/json' \
  -d '{"scene_id": "scene-evt-accept"}' \
  | python3 -c 'import sys,json; print(json.load(sys.stdin)["session_id"])')

# 2) 游标先推进到 900（s1、s2 已开始，接纳时固定原分轨 1、2）
curl -s -X POST http://localhost:8080/sessions/$SID/advance \
  -H 'Content-Type: application/json' -d '{"time_ms": 900}' >/dev/null

# 3) 场景修订为 revision 2：s3 延长到 4000，新增 s4[3200,4000)
curl -s -X PUT http://localhost:8080/scenes/scene-evt-accept \
  -H 'Content-Type: application/json' \
  -d '{
    "track_count": 2,
    "expected_revision": 1,
    "subtitles": [
      {"id": "s1", "speaker": "Alice", "start_ms": 0,    "end_ms": 1000},
      {"id": "s2", "speaker": "Bob",   "start_ms": 800,  "end_ms": 3000, "locked_track": 2},
      {"id": "s3", "speaker": "Alice", "start_ms": 1000, "end_ms": 4000},
      {"id": "s4", "speaker": "Carol", "start_ms": 3200, "end_ms": 4000}
    ]
  }' >/dev/null

# 4) 会话接纳最新快照（source_revision -> 2），随后事件推进到 4000
curl -s -X POST http://localhost:8080/sessions/$SID/accept >/dev/null
curl -s -X POST http://localhost:8080/sessions/$SID/events \
  -H 'Content-Type: application/json' -d '{"time_ms": 4000}'
```

```json
{
  "session_id": "…",
  "scene_id": "scene-evt-accept",
  "source_revision": 2,
  "cursor_ms": 4000,
  "events": [
    {"id": "s1", "speaker": "Alice", "track": 1, "time_ms": 1000, "type": "exit"},
    {"id": "s3", "speaker": "Alice", "track": 1, "time_ms": 1000, "type": "enter"},
    {"id": "s2", "speaker": "Bob",   "track": 2, "time_ms": 3000, "type": "exit"},
    {"id": "s4", "speaker": "Carol", "track": 2, "time_ms": 3200, "type": "enter"},
    {"id": "s3", "speaker": "Alice", "track": 1, "time_ms": 4000, "type": "exit"},
    {"id": "s4", "speaker": "Carol", "track": 2, "time_ms": 4000, "type": "exit"}
  ],
  "active": []
}
```

事件与轨道全部来自**接纳后**的会话快照（s3 的 `end_ms` 已是 4000、s4 是修订新增的字幕、`source_revision` 为 2）；接纳失败（409）或尚未接纳的场景修订都不会影响事件——事件生成只读取轮到该请求生效时的会话快照，并与推进、接纳在会话锁内完整串行。4000 时刻 s3、s4 同为退出，按新快照的会话字幕顺序（s3 在 s4 前）排列。

### 示例 21：播放结束——finish 取回最终快照并保存响应，随后释放会话

下面脚本可直接复制执行：创建场景 → 创建会话（创建响应也保存一份）→ 推进播放 → 场景被修订但**不接纳** → 结束并把**最终完整快照保存到文件** → 验证结束后一切请求 404、源场景与其他会话不受影响。

```bash
# 1) 创建场景（revision 1：s1[0,1000)、s2[800,3000) 锁轨 2、s3[1000,2500)）
curl -s -X PUT http://localhost:8080/scenes/scene-finish \
  -H 'Content-Type: application/json' \
  -d '{
    "track_count": 2,
    "expected_revision": 0,
    "subtitles": [
      {"id": "s1", "speaker": "Alice", "start_ms": 0,    "end_ms": 1000},
      {"id": "s2", "speaker": "Bob",   "start_ms": 800,  "end_ms": 3000, "locked_track": 2},
      {"id": "s3", "speaker": "Alice", "start_ms": 1000, "end_ms": 2500}
    ]
  }' >/dev/null

# 2) 创建会话，并把创建响应快照保存为 /tmp/session-create.json
SID=$(curl -s -X POST http://localhost:8080/sessions \
  -H 'Content-Type: application/json' \
  -d '{"scene_id": "scene-finish"}' \
  | tee /tmp/session-create.json \
  | python3 -c 'import sys,json; print(json.load(sys.stdin)["session_id"])')

# 播放：推进到 900（也可混用 /events、/accept）
curl -s -X POST http://localhost:8080/sessions/$SID/advance \
  -H 'Content-Type: application/json' -d '{"time_ms": 900}' >/dev/null

# 3) 场景在此期间被修订为 revision 2（s3 延长到 4000），但会话不接纳
curl -s -X PUT http://localhost:8080/scenes/scene-finish \
  -H 'Content-Type: application/json' \
  -d '{
    "track_count": 2, "expected_revision": 1,
    "subtitles": [
      {"id": "s1", "speaker": "Alice", "start_ms": 0,    "end_ms": 1000},
      {"id": "s2", "speaker": "Bob",   "start_ms": 800,  "end_ms": 3000, "locked_track": 2},
      {"id": "s3", "speaker": "Alice", "start_ms": 1000, "end_ms": 4000}
    ]
  }' >/dev/null

# 4) 播放结束：一次完整操作取回最终快照并保存到 /tmp/session-final.json
curl -s -X POST http://localhost:8080/sessions/$SID/finish \
  | tee /tmp/session-final.json | python3 -m json.tool
```

```json
{
  "session_id": "…",
  "scene_id": "scene-finish",
  "source_revision": 1,
  "cursor_ms": 900,
  "track_count": 2,
  "changes": 0,
  "subtitles": [
    {"id": "s1", "speaker": "Alice", "start_ms": 0,    "end_ms": 1000, "locked_track": null},
    {"id": "s2", "speaker": "Bob",   "start_ms": 800,  "end_ms": 3000, "locked_track": 2},
    {"id": "s3", "speaker": "Alice", "start_ms": 1000, "end_ms": 2500, "locked_track": null}
  ],
  "assignments": [
    {"id": "s1", "track": 1},
    {"id": "s2", "track": 2},
    {"id": "s3", "track": 1}
  ]
}
```

结束响应与创建会话的字段完全相同；`cursor_ms` 为轮到结束时的游标（900），`subtitles`/`assignments` 是会话当时的快照。场景已被修订为 revision 2，但会话没有 `/accept`，因此结束快照仍是旧版本——**不读取尚未接纳的场景修订**（s3 的 `end_ms` 仍为 2500，`source_revision` 仍为 1）：

```bash
python3 -c 'import json; d=json.load(open("/tmp/session-final.json")); print(d["source_revision"], d["cursor_ms"], [s["end_ms"] for s in d["subtitles"] if s["id"]=="s3"])'
# 1 900 [2500] —— 保存下来的结束快照停留在会话实际接纳的 revision 1
```

结束后该 id 的一切有效请求都返回 404，包括重复结束；未知会话同样 404：

```bash
curl -s -o /dev/null -w '%{http_code}\n' -X POST http://localhost:8080/sessions/$SID/advance \
  -H 'Content-Type: application/json' -d '{"time_ms": 1000}'   # 404
curl -s -o /dev/null -w '%{http_code}\n' -X POST http://localhost:8080/sessions/$SID/events \
  -H 'Content-Type: application/json' -d '{"time_ms": 1000}'   # 404
curl -s -o /dev/null -w '%{http_code}\n' -X POST http://localhost:8080/sessions/$SID/accept   # 404
curl -s -o /dev/null -w '%{http_code}\n' -X POST http://localhost:8080/sessions/$SID/finish   # 404（重复结束）
curl -s -o /dev/null -w '%{http_code}\n' -X POST http://localhost:8080/sessions/no-such/finish  # 404（未知会话）
```

**并发结束最多一次成功**：另建一个会话，同时发起 8 次结束，恰好 1 个 200、7 个 404。

```bash
SID2=$(curl -s -X POST http://localhost:8080/sessions \
  -H 'Content-Type: application/json' -d '{"scene_id": "scene-finish"}' \
  | python3 -c 'import sys,json; print(json.load(sys.stdin)["session_id"])')
rm -f /tmp/finish-codes
for i in $(seq 1 8); do
  (curl -s -o /dev/null -w '%{http_code}\n' \
    -X POST http://localhost:8080/sessions/$SID2/finish >> /tmp/finish-codes) &
done
wait
sort /tmp/finish-codes | uniq -c
#       1 200
#       7 404
```

源场景不受影响、仍然可读（revision 2 的修订照常存在），还可以基于它创建全新会话（游标从 0 毫秒开始、复制场景当前快照）；同场景的其他会话也照常播放：

```bash
curl -s http://localhost:8080/scenes/scene-finish \
  | python3 -c 'import sys,json; print(json.load(sys.stdin)["revision"])'
# 2 —— 结束会话不会回滚或删除场景
curl -s -X POST http://localhost:8080/sessions \
  -H 'Content-Type: application/json' -d '{"scene_id": "scene-finish"}' \
  | python3 -c 'import sys,json; d=json.load(sys.stdin); print(d["cursor_ms"], d["source_revision"])'
# 0 2 —— 全新会话从 0 毫秒、场景当前快照开始
```

结束与同会话的推进、事件推进、接纳**串行生效**：若某操作先拿到会话锁，则它先正常生效（其游标/接纳进入最终快照），随后结束再生效；已取得会话引用但轮到它时会话已结束的请求一律 404。结束不保存任何记录、不跨重启恢复（会话本就是内存态，重启后该 id 同样 404）；此前返回的快照（`/tmp/session-create.json`、`/tmp/session-final.json`）是独立拷贝，不受任何后续操作影响。

### 示例 22：播放中接纳场景修订

播放期间场景被修订（例如按示例 7 的方式把 `scene-001` 推进到 revision 2：s3 延长到 4000，并新增 s4 `[3200, 4000)`）。会话接纳最新快照：

```bash
curl -s -X POST http://localhost:8080/sessions/$SID/accept
```

```json
{
  "session_id": "9f2c…",
  "scene_id": "scene-001",
  "source_revision": 2,
  "cursor_ms": 900,
  "track_count": 2,
  "changes": 0,
  "subtitles": [ … s1, s2, s3(1000–4000), s4(3200–4000) ],
  "assignments": [
    {"id": "s1", "track": 1},
    {"id": "s2", "track": 2},
    {"id": "s3", "track": 1},
    {"id": "s4", "track": 2}
  ]
}
```

游标 900 时 s1、s2 已开始，保持原分轨 1、2 不变；s3、s4 在游标之后开始，按新快照重算分轨。`source_revision` 变为 2，场景本身的版本与内容不受接纳影响。

### 示例 23：接纳冲突返回 409（会话不变）

若场景的修订动了已开始字幕（上例中游标 900 时修改了 s1 的起止时间、删除 s2、把轨道数改为 3，或新增的字幕在 900 之前开始等），接纳失败：

```bash
curl -s -i -X POST http://localhost:8080/sessions/$SID/accept
```

```
HTTP/1.1 409 Conflict
```

```json
{
  "detail": {
    "message": "字幕 's1' 已开始，其 id/speaker/起止时间/locked_track 不得修改",
    "scene_id": "scene-001"
  }
}
```

固定已开始字幕的原分轨后新快照无解时同样返回 409。失败的接纳不改变会话：游标、快照、`source_revision` 保持原值，可继续推进或在场景改回合法内容后重试接纳。

### 接纳的并发修复说明

此前 `accept` 在进入会话锁**之前**读取场景快照，存在回写缺陷：请求 R1 读到场景版本 1 后被挂起；期间场景把某条未开始字幕的 `end_ms` 从 2000 改为 4000 并保存为版本 2，另一请求 R2 先完成接纳（会话到达版本 2）；R1 恢复后仍应用手中过期的版本 1 快照，把会话的 `source_revision` 与字幕**回退**到版本 1。

修复后，接纳把“读取场景快照 → 校验已开始字幕 → 重算分轨 → 替换会话状态”作为**完整操作**在会话锁内串行生效：场景读取发生在轮到该请求生效之时，因此迟到的请求接纳的一定是当时的场景快照——既不会把旧数据回写到会话上，也不会跳过旧数据却报告成功。会话的 `source_revision` 对同一会话只增不回退。请求、响应与错误码（200/404/409/422）均与之前兼容。

### 示例 24：并发接纳——迟到请求不会回写旧快照

```bash
# 1) 创建场景（版本 1：未开始的 s3 end_ms=2000）
curl -s -X PUT http://localhost:8080/scenes/scene-race \
  -H 'Content-Type: application/json' \
  -d '{
    "track_count": 2,
    "expected_revision": 0,
    "subtitles": [
      {"id": "s1", "speaker": "Alice", "start_ms": 0,    "end_ms": 1000},
      {"id": "s2", "speaker": "Bob",   "start_ms": 800,  "end_ms": 3000, "locked_track": 2},
      {"id": "s3", "speaker": "Alice", "start_ms": 1000, "end_ms": 2000}
    ]
  }'

# 2) 创建会话并推进游标到 900（s1、s2 已开始，s3 未开始）
SID=$(curl -s -X POST http://localhost:8080/sessions \
  -H 'Content-Type: application/json' \
  -d '{"scene_id": "scene-race"}' | python3 -c 'import sys,json; print(json.load(sys.stdin)["session_id"])')
curl -s -X POST http://localhost:8080/sessions/$SID/advance \
  -H 'Content-Type: application/json' -d '{"time_ms": 900}'

# 3) 场景修订为版本 2：s3 的 end_ms 从 2000 改为 4000
curl -s -X PUT http://localhost:8080/scenes/scene-race \
  -H 'Content-Type: application/json' \
  -d '{
    "track_count": 2,
    "expected_revision": 1,
    "subtitles": [
      {"id": "s1", "speaker": "Alice", "start_ms": 0,    "end_ms": 1000},
      {"id": "s2", "speaker": "Bob",   "start_ms": 800,  "end_ms": 3000, "locked_track": 2},
      {"id": "s3", "speaker": "Alice", "start_ms": 1000, "end_ms": 4000}
    ]
  }'

# 4) 并发发起两次接纳：串行生效，响应一致，均为版本 2 快照
curl -s -X POST http://localhost:8080/sessions/$SID/accept &
curl -s -X POST http://localhost:8080/sessions/$SID/accept &
wait
```

两个并发响应都是 `source_revision: 2`、`cursor_ms: 900`、s3 的 `end_ms: 4000`，分轨为 s1→1、s2→2（已开始，固定原分轨）、s3→1。无论请求到达顺序如何交错，会话都不会再回退到版本 1 的内容；可用推进验证（s3 在 2500 时仍在屏，说明其 `end_ms` 是 4000 而非 2000）：

```bash
curl -s -X POST http://localhost:8080/sessions/$SID/advance \
  -H 'Content-Type: application/json' -d '{"time_ms": 2500}'
# {"session_id":"…","scene_id":"scene-race","source_revision":2,"cursor_ms":2500,
#  "active":[{"id":"s2",…,"track":2},{"id":"s3",…,"end_ms":4000,…,"track":1}]}
```

### 示例 25：临时隐藏说话人——切换、推进、接纳

隐藏设置只作用于**当前会话**：不改动场景快照、分轨与其他会话，也不持久化。下面脚本可直接复制执行：创建场景 → 建会话并推进到 900 → 隐藏 Alice（s1 立即 `exit`）→ 带隐藏事件推进（Alice 的进入/退出全部消失）→ 清除隐藏 → 非法集合 422。

```bash
# 场景（revision 1）：s1 Alice[0,1000)，s2 Bob[800,3000) 锁轨 2，s3 Alice[1000,2500)
curl -s -X PUT http://localhost:8080/scenes/scene-hide \
  -H 'Content-Type: application/json' \
  -d '{
    "track_count": 2,
    "expected_revision": 0,
    "subtitles": [
      {"id": "s1", "speaker": "Alice", "start_ms": 0,    "end_ms": 1000},
      {"id": "s2", "speaker": "Bob",   "start_ms": 800,  "end_ms": 3000, "locked_track": 2},
      {"id": "s3", "speaker": "Alice", "start_ms": 1000, "end_ms": 2500}
    ]
  }' >/dev/null

SID=$(curl -s -X POST http://localhost:8080/sessions \
  -H 'Content-Type: application/json' \
  -d '{"scene_id": "scene-hide"}' \
  | python3 -c 'import sys,json; print(json.load(sys.stdin)["session_id"])')

# 先推进到 900：s1、s2 在屏
curl -s -X POST http://localhost:8080/sessions/$SID/advance \
  -H 'Content-Type: application/json' -d '{"time_ms": 900}' >/dev/null
```

**切换隐藏**：游标不动，当前活动区间里原可见而现隐藏的 s1 发 `exit`（时刻取当前游标 900），响应的 `active` 已排除 Alice：

```bash
curl -s -X POST http://localhost:8080/sessions/$SID/hidden_speakers \
  -H 'Content-Type: application/json' \
  -d '{"hidden_speakers": ["Alice"]}'
```

```json
{
  "session_id": "…",
  "scene_id": "scene-hide",
  "source_revision": 1,
  "cursor_ms": 900,
  "hidden_speakers": ["Alice"],
  "events": [
    {"id": "s1", "speaker": "Alice", "track": 1, "time_ms": 900, "type": "exit"}
  ],
  "active": [
    {"id": "s2", "speaker": "Bob", "start_ms": 800, "end_ms": 3000, "locked_track": 2, "track": 2}
  ]
}
```

**带隐藏推进（/events）**：窗口 (900,3000] 内 s1 的 1000 退出、s3 的 1000 进入与 2500 退出全部属于 Alice 而被排除，只剩 s2 的 3000 退出；游标照常到达 3000：

```bash
curl -s -X POST http://localhost:8080/sessions/$SID/events \
  -H 'Content-Type: application/json' -d '{"time_ms": 3000}'
```

```json
{
  "session_id": "…",
  "scene_id": "scene-hide",
  "source_revision": 1,
  "cursor_ms": 3000,
  "events": [
    {"id": "s2", "speaker": "Bob", "track": 2, "time_ms": 3000, "type": "exit"}
  ],
  "active": []
}
```

> 对照：未隐藏时同一推进还会含 `(1000, exit, s1)`、`(1000, enter, s3)`、`(2500, exit, s3)`。隐藏期间整条开始又结束的 s3 不会出现在事件流，也不会在恢复显示时补发。

**空数组清除**：以当前游标 3000 的活动区间判定——Alice 此刻没有活动字幕，因此无 `enter` 事件：

```bash
curl -s -X POST http://localhost:8080/sessions/$SID/hidden_speakers \
  -H 'Content-Type: application/json' -d '{"hidden_speakers": []}'
# {"session_id":"…","source_revision":1,"cursor_ms":3000,"hidden_speakers":[],
#  "events":[],"active":[]}
```

同集合重复提交、或加入尚未出现的说话人（如 `"Carol"`）都返回 200 且 `events` 为空；隐藏在 `/accept` 接纳修订后仍然保留，但**创建/接纳/finish 的完整快照与分轨不含任何隐藏过滤**——`subtitles` 与 `assignments` 始终是全量的。

**非法集合 422（会话状态不变）、未知或已结束会话 404**：

```bash
# 重复说话人 / 空字符串 / 元素非字符串（数字、布尔不隐式转换）/ 非数组 / 缺失
curl -s -o /dev/null -w '%{http_code}\n' -X POST http://localhost:8080/sessions/$SID/hidden_speakers \
  -H 'Content-Type: application/json' -d '{"hidden_speakers": ["Alice", "Alice"]}'   # 422
curl -s -o /dev/null -w '%{http_code}\n' -X POST http://localhost:8080/sessions/$SID/hidden_speakers \
  -H 'Content-Type: application/json' -d '{"hidden_speakers": [""]}'                # 422
curl -s -o /dev/null -w '%{http_code}\n' -X POST http://localhost:8080/sessions/$SID/hidden_speakers \
  -H 'Content-Type: application/json' -d '{"hidden_speakers": [1]}'                 # 422
curl -s -o /dev/null -w '%{http_code}\n' -X POST http://localhost:8080/sessions/no-such/hidden_speakers \
  -H 'Content-Type: application/json' -d '{"hidden_speakers": ["Alice"]}'           # 404
```

## 补取通道（channel）：推进响应丢失后补取未确认的字幕变更

`/events` 与 `/hidden_speakers` 的事件只随当次响应返回，不保存历史；若显示端遇到**推进响应丢失**（网络超时、连接中断等），错过的字幕变更无从得知。补取通道为此提供一条会话内的内存事件队列：开启后，会话产生的全部 enter/exit 都按序入队，显示端随时补取未确认的事件并按序号确认。

- `POST /sessions/{session_id}/channel`：**开启通道**（无请求体）。每个会话最多一个内存通道，重复开启返回 **409**（已有通道不变）。成功响应返回当前可见 `active`（与 `/advance` 相同、排除隐藏说话人）与序号 `seq: 0`——开启之前的事件不补记，客户端以本响应的 `active` 作为初始可见集合。未知或已结束会话返回 404。
- 开启后，**普通推进（`/advance`）、事件推进（`/events`）与隐藏切换（`/hidden_speakers`）产生的全部 enter/exit** 都追加到通道队列：事件字段与排序沿用事件推进的规则（`id`/`speaker`/`track`/`time_ms`/`type`，时刻升序、同刻先退出后进入、同类按会话字幕顺序；隐藏切换的事件时刻为当前游标），每条事件分配**从 1 起连续递增的 `seq`**。普通推进的响应不变（仍不含 `events`），但它跨过的进入/退出——包括两次采样之间开始又结束的短字幕——同样完整入队；隐藏说话人的字幕不产生事件，也不入队。**接纳（`/accept`）不重写已排队内容**：队列中的事件保持入队时的内容与序号，接纳后的新事件继续递增编号。状态变更（游标推进、隐藏集合替换）与入队在同一会话锁内一同生效；开启、补取与既有会话操作完整串行；任何失败都不改会话或通道。
- **共享容量（`CHANNEL_EVENT_LIMIT`）**：所有会话的补取通道共用一个“未确认事件总量”上限。该环境变量必须是**正整数**（十进制，允许首尾空白），**默认 `1024`；未设置时取默认值，配置为 `0`、负数、小数或其他非数字值时服务拒绝启动**（uvicorn 导入配置即失败），Docker Compose 中可直接配置。
  - 上述三类生产操作按**本次实际生成的事件数**在会话锁内**原子申请**共享容量，申请与游标推进/隐藏集合替换/`seq` 分配/完整入队**一同生效**。容量不足（哪怕只够容纳批次的一部分）立即返回 **HTTP 503**，响应体给出 `limit`、本次需要的 `requested` 与当前 `available`；此时**不推进游标、不改变隐藏集合、不分配/消费 `seq`、不部分入队**，会话与通道与请求前完全一致。
  - **未开启通道的会话不占容量**（推进、事件推进、隐藏切换都不会 503）；**无事件的操作不占名额**——等时推进、不产生显示变更的隐藏切换在满额时仍返回 200。**开启通道与接纳也不占容量**（接纳既不生产事件也不释放容量）。
  - 容量**只在“确认并删除前缀”时**按本次实际删除的事件数释放：纯读取（`fetch` 只返回事件、`ack_seq` 未前进）与旧确认（不回退、不再次删除）**不释放**；或在**结束会话时一次性释放该通道剩余的未确认队列**——重复或并发结束最多成功一次，容量不会重复释放。被 503 拒绝的请求在确认前缀或结束其他会话腾出容量后，用**完全相同的原请求重试**即可成功（游标与隐藏集合当时都未改变）。
- `POST /sessions/{session_id}/channel/fetch`：**补取并确认**。请求体 `{"ack_seq": N}`：
  - `ack_seq` 为**非负整数**，布尔值与字符串一律 **422**（严格整数，不做隐式转换）；
  - 只能确认**此前补取已返回的最大 `seq` 以内**的前缀：`ack_seq` 超过该上限返回 **422** 且通道不变；小于等于已确认位置的**旧确认不回退**（确认位置只增）；
  - 成功响应返回**确认位置 `acked_seq` 与剩余未确认事件**（`seq > acked_seq`，按 `seq` 递增）；已确认前缀随即丢弃，未确认事件**重复补取时内容与序号不变**——推进响应丢失后，用上次已确认的 `ack_seq` 重试即可拿到完全相同的剩余事件。
- 结束会话（`/finish`）**清除通道**；未知或已结束会话、未开启通道的补取均返回 **404**。通道只存在于会话内存中：**不跨重启恢复、不做后台推送**（拉取式）；旧接口（创建/推进/事件推进/隐藏切换/接纳/结束）的响应完全不变。

### 示例 26：开启通道与丢响应后补取

创建场景 `scene-chan`（s1 `[0,1000)`、s2 `[800,3000)` 锁轨 2、s3 `[1000,2500)`，分轨 1/2/1）并创建会话，然后开启补取通道：

```bash
curl -s -X PUT http://localhost:8080/scenes/scene-chan \
  -H 'Content-Type: application/json' \
  -d '{
    "track_count": 2,
    "expected_revision": 0,
    "subtitles": [
      {"id": "s1", "speaker": "Alice", "start_ms": 0,    "end_ms": 1000},
      {"id": "s2", "speaker": "Bob",   "start_ms": 800,  "end_ms": 3000, "locked_track": 2},
      {"id": "s3", "speaker": "Alice", "start_ms": 1000, "end_ms": 2500}
    ]
  }' >/dev/null

SID=$(curl -s -X POST http://localhost:8080/sessions \
  -H 'Content-Type: application/json' \
  -d '{"scene_id": "scene-chan"}' | python3 -c 'import sys,json; print(json.load(sys.stdin)["session_id"])')

# 开启通道：游标 0 处仅 s1 可见，序号 0（首个事件将为 seq 1）
curl -s -X POST http://localhost:8080/sessions/$SID/channel
```

```json
{
  "session_id": "…",
  "scene_id": "scene-chan",
  "source_revision": 1,
  "cursor_ms": 0,
  "seq": 0,
  "active": [
    {"id": "s1", "speaker": "Alice", "start_ms": 0, "end_ms": 1000, "locked_track": null, "track": 1}
  ]
}
```

正常播放：先用 `/advance` 推进到 900（响应仍是原样、不含事件，但 s2 的 800 进入已入队为 seq 1），再用 `/events` 推进到 1000——**假设这次响应丢失**（s1 退出、s3 进入已入队为 seq 2、3）：

```bash
curl -s -X POST http://localhost:8080/sessions/$SID/advance \
  -H 'Content-Type: application/json' -d '{"time_ms": 900}' >/dev/null
curl -s -X POST http://localhost:8080/sessions/$SID/events \
  -H 'Content-Type: application/json' -d '{"time_ms": 1000}'   # 响应丢失！
```

丢响应后补取：以上次确认位置 `ack_seq: 0` 重取，拿到全部未确认事件（含普通推进记录的那条）：

```bash
curl -s -X POST http://localhost:8080/sessions/$SID/channel/fetch \
  -H 'Content-Type: application/json' -d '{"ack_seq": 0}'
```

```json
{
  "session_id": "…",
  "scene_id": "scene-chan",
  "acked_seq": 0,
  "events": [
    {"seq": 1, "id": "s2", "speaker": "Bob",   "track": 2, "time_ms": 800,  "type": "enter"},
    {"seq": 2, "id": "s1", "speaker": "Alice", "track": 1, "time_ms": 1000, "type": "exit"},
    {"seq": 3, "id": "s3", "speaker": "Alice", "track": 1, "time_ms": 1000, "type": "enter"}
  ]
}
```

若这次补取响应也丢失，用同样的 `ack_seq: 0` 重试，内容与序号完全相同；确认已收到前 3 条后，剩余事件为空：

```bash
curl -s -X POST http://localhost:8080/sessions/$SID/channel/fetch \
  -H 'Content-Type: application/json' -d '{"ack_seq": 3}'
# {"session_id":"…","scene_id":"scene-chan","acked_seq":3,"events":[]}
```

### 示例 27：确认边界与隐藏切换的记录

接上例（游标 1000、已确认到 seq 3）。隐藏 Alice：游标处 s3 活动，可见→隐藏发 `exit`（时刻取当前游标），该事件入队为 seq 4；隐藏期间事件推进到 3000，Alice 的事件被排除，只记录 s2 的 3000 退出（seq 5）：

```bash
curl -s -X POST http://localhost:8080/sessions/$SID/hidden_speakers \
  -H 'Content-Type: application/json' -d '{"hidden_speakers": ["Alice"]}' >/dev/null
curl -s -X POST http://localhost:8080/sessions/$SID/events \
  -H 'Content-Type: application/json' -d '{"time_ms": 3000}' >/dev/null

# 补取：ack 3 确认旧位置，返回 seq 4、5（按 seq 递增）
curl -s -X POST http://localhost:8080/sessions/$SID/channel/fetch \
  -H 'Content-Type: application/json' -d '{"ack_seq": 3}'
```

```json
{
  "session_id": "…",
  "scene_id": "scene-chan",
  "acked_seq": 3,
  "events": [
    {"seq": 4, "id": "s3", "speaker": "Alice", "track": 1, "time_ms": 1000, "type": "exit"},
    {"seq": 5, "id": "s2", "speaker": "Bob",   "track": 2, "time_ms": 3000, "type": "exit"}
  ]
}
```

**确认边界**：只能确认此前补取已返回的最大 `seq`（上例已返回到 5）；越界 422 且通道不变，旧确认不回退：

```bash
# 越界：ack 6 超过已返回的最大 seq 5
curl -s -i -X POST http://localhost:8080/sessions/$SID/channel/fetch \
  -H 'Content-Type: application/json' -d '{"ack_seq": 6}'
# HTTP/1.1 422 Unprocessable Entity
# {"detail":{"message":"ack_seq 越界：只能确认此前补取已返回的最大 seq 以内的前缀",
#            "ack_seq":6,"max_returned_seq":5}}

# 旧确认不回退：ack 2 小于已确认位置 3，确认位置保持 3，仍返回 seq 4、5
curl -s -X POST http://localhost:8080/sessions/$SID/channel/fetch \
  -H 'Content-Type: application/json' -d '{"ack_seq": 2}'
# {"session_id":"…","scene_id":"scene-chan","acked_seq":3,"events":[seq 4, seq 5]}

# 确认到 5：队列清空
curl -s -X POST http://localhost:8080/sessions/$SID/channel/fetch \
  -H 'Content-Type: application/json' -d '{"ack_seq": 5}'
# {"session_id":"…","scene_id":"scene-chan","acked_seq":5,"events":[]}

# ack_seq 拒绝布尔值与字符串（严格整数，不做隐式转换）
curl -s -o /dev/null -w '%{http_code}\n' -X POST http://localhost:8080/sessions/$SID/channel/fetch \
  -H 'Content-Type: application/json' -d '{"ack_seq": true}'    # 422
curl -s -o /dev/null -w '%{http_code}\n' -X POST http://localhost:8080/sessions/$SID/channel/fetch \
  -H 'Content-Type: application/json' -d '{"ack_seq": "5"}'     # 422
```

**生命周期**：每会话只能开启一个通道（重复开启 409）；结束会话清除通道，此后开启与补取均 404；未开启通道的会话补取同样 404：

```bash
curl -s -o /dev/null -w '%{http_code}\n' -X POST http://localhost:8080/sessions/$SID/channel
# 409（重复开启）
curl -s -o /dev/null -w '%{http_code}\n' -X POST http://localhost:8080/sessions/$SID/finish
# 200（结束并清除通道）
curl -s -o /dev/null -w '%{http_code}\n' -X POST http://localhost:8080/sessions/$SID/channel/fetch \
  -H 'Content-Type: application/json' -d '{"ack_seq": 0}'        # 404（会话已结束）
curl -s -o /dev/null -w '%{http_code}\n' -X POST http://localhost:8080/sessions/$SID/channel
# 404（会话已结束）
```

### 示例 28：共享容量配置、满额 503、确认释放与重试

容量是**全进程所有会话未确认事件的总量**，用环境变量 `CHANNEL_EVENT_LIMIT` 配置（Compose 中改 `environment` 即可；非法值拒绝启动）：

```yaml
# docker-compose.yml 节选
services:
  subtitle-tracks:
    environment:
      SCENES_DB_PATH: /data/scenes.db
      CHANNEL_EVENT_LIMIT: "1024"   # 正整数，缺省 1024；0、负数、小数、非数字（含空值）都会拒绝启动
```

```bash
# 非法配置拒绝启动（容器启动失败并打印 CHANNEL_EVENT_LIMIT 错误）：
CHANNEL_EVENT_LIMIT=0 uvicorn app.main:app --port 8080
# ValueError: CHANNEL_EVENT_LIMIT 必须为正整数（≥1），实际配置为 '0'
```

下面把上限临时调成 **3** 来演示满额（本地非 Docker：`CHANNEL_EVENT_LIMIT=3 uvicorn app.main:app --port 8080`）。仍使用 `scene-chan`：一次事件推进到 1000 恰好产生 3 条事件，占满全部共享容量：

```bash
SID=$(curl -s -X POST http://localhost:8080/sessions \
  -H 'Content-Type: application/json' \
  -d '{"scene_id": "scene-chan"}' | python3 -c 'import sys,json; print(json.load(sys.stdin)["session_id"])')
curl -s -X POST http://localhost:8080/sessions/$SID/channel >/dev/null

# 3 条事件入队，占满容量
curl -s -o /dev/null -w '%{http_code}\n' -X POST http://localhost:8080/sessions/$SID/events \
  -H 'Content-Type: application/json' -d '{"time_ms": 1000}'
# 200
```

**满额后立即 503**，且不推进游标、不改隐藏集合、不消费 seq、不部分入队（下一批 2 条事件即使只缺 1 个名额也整体失败）：

```bash
curl -s -i -X POST http://localhost:8080/sessions/$SID/events \
  -H 'Content-Type: application/json' -d '{"time_ms": 3000}'
# HTTP/1.1 503 Service Unavailable
# {"detail":{"message":"补取通道未确认事件总量已达上限：本次事件无法完整入队，状态未改变；确认前缀或结束会话释放容量后可重试原请求",
#            "limit":3,"requested":2,"available":0}}

# 游标仍是 1000（未推进）；队列仍是 seq 1~3（未部分入队、未消费新 seq）
curl -s -X POST http://localhost:8080/sessions/$SID/channel/fetch \
  -H 'Content-Type: application/json' -d '{"ack_seq": 0}'
# {"session_id":"…","scene_id":"scene-chan","acked_seq":0,
#  "events":[seq 1, seq 2, seq 3]}
```

**无事件的操作在满额时仍可执行**（等时推进、无显示变更的隐藏切换返回 200）：

```bash
curl -s -o /dev/null -w '%{http_code}\n' -X POST http://localhost:8080/sessions/$SID/events \
  -H 'Content-Type: application/json' -d '{"time_ms": 1000}'   # 等时：0 个事件
# 200
curl -s -o /dev/null -w '%{http_code}\n' -X POST http://localhost:8080/sessions/$SID/hidden_speakers \
  -H 'Content-Type: application/json' -d '{"hidden_speakers": []}'   # 集合无变化：0 个事件
# 200
```

**确认前缀释放容量，原请求重试成功**：先补取（建立“已返回”上界），再确认并删除前 2 条，即释放 2 个名额；纯读取与旧确认不释放：

```bash
# 纯读取（ack 0）不释放；旧确认（已确认到 2 再发 ack 0）也不释放、不回退
curl -s -X POST http://localhost:8080/sessions/$SID/channel/fetch \
  -H 'Content-Type: application/json' -d '{"ack_seq": 0}' >/dev/null
# 确认并删除前 2 条：剩余队列 1 条，占用降到 1，剩余容量 2
curl -s -X POST http://localhost:8080/sessions/$SID/channel/fetch \
  -H 'Content-Type: application/json' -d '{"ack_seq": 2}'
# {"session_id":"…","scene_id":"scene-chan","acked_seq":2,"events":[seq 3]}

# 此前 503 的同一请求原样重试：完整成功，新事件接着 seq 3 编号为 4、5
curl -s -X POST http://localhost:8080/sessions/$SID/events \
  -H 'Content-Type: application/json' -d '{"time_ms": 3000}'
# HTTP 200
curl -s -X POST http://localhost:8080/sessions/$SID/channel/fetch \
  -H 'Content-Type: application/json' -d '{"ack_seq": 2}'
# {"…","acked_seq":2,"events":[seq 3, seq 4(2500 exit s3), seq 5(3000 exit s2)]}
```

**结束会话释放剩余队列**（重复/并发结束不会重复释放）；其他会话占用了容量时，结束它们也能为本会话腾出名额：

```bash
# 当前仍有 seq 3/4/5 未确认（占用 3）；结束会话一次性归还，容量归零
curl -s -o /dev/null -w '%{http_code}\n' -X POST http://localhost:8080/sessions/$SID/finish
# 200
curl -s -o /dev/null -w '%{http_code}\n' -X POST http://localhost:8080/sessions/$SID/finish
# 404（重复结束；不会再释放一次容量）
```

健康检查：

```bash
curl -s http://localhost:8080/health
# {"status":"ok"}
```

## 自动播放（autoplay）：按真实经过时间推进

正常播放需要显示端反复提交 `/advance` 或 `/events` 推进游标。开启补取通道的会话还可以改为**自动播放**：启动后由服务进程内的后台驱动**每 50 毫秒**尝试推进一次，游标目标为**本段起始游标加上单调时钟经过的整毫秒**，客户端只需轮询/补取，不再逐次提交时间。自动播放沿用播放会话与补取通道的全部既有语义——事件窗口（旧游标, 目标]、隐藏过滤、时刻升序/同刻先 exit 后 enter/同类按会话顺序、短字幕不漏、`seq` 连续编号、共享容量原子申请——客户端通过通道即可拿到自动推进产生的全部 enter/exit。

控制接口（均无请求体）：

| 接口 | 方法 | 允许的当前状态 | 成功后状态 |
| --- | --- | --- | --- |
| `/sessions/{id}/autoplay/start` | POST | `idle` 且**已开启补取通道** | `running` |
| `/sessions/{id}/autoplay/pause` | POST | `running` | `paused` |
| `/sessions/{id}/autoplay/resume` | POST | `paused`、`blocked` | `running` |
| `/sessions/{id}/autoplay` | GET | 任意 | 不变 |

- 响应统一为 `{"session_id", "status", "cursor_ms"}`；状态不在允许集合时返回 **409**（`detail.reason` 给出原因：`channel_missing`/`already_started`/`not_started`/`pause_only_running`/`resume_paused_or_blocked`），未知或已结束会话返回 **404**。
- **启动以当前游标为本段起始游标**（启动前可先手动推进到任意时刻）；后台目标 = 起始游标 + `time.monotonic()` 经过的整毫秒。
- **暂停、阻塞不计时**：暂停保留后台最后一次成功推进的游标（最多一拍粒度），恢复以保留游标作为**新一段起点**重新计时，**不追赶**暂停或阻塞期间流逝的时间；恢复后各拍窗口仍从保留游标算起，停顿区间内的事件会在恢复后的正常推进中入队，不会丢失。
- **等锁不计时**：启动与恢复的单调时钟起点在**会话锁内、控制请求串行生效的同一时刻**读取。同会话的接纳等操作持锁时，控制请求在锁外排队等待的间隔不属于任何计时段——生效后游标只按真实播放时间推进，不会把等锁时长算进去，该区间内的字幕事件也不会被提前入队（详见下文“启动/恢复的等锁计时修复说明”）。
- **满额阻塞**：某一拍要整批入队的事件数超过共享容量剩余时，这一拍**整批失败**并转为 `blocked`：游标不推进、`seq` 不分配、事件不部分入队（与手动推进的 503 同一条原子规则）。阻塞后后台不再推进该会话，直到客户端用 `/channel/fetch` 确认前缀（或结束其他会话）腾出容量，再**显式 `resume`**；若恢复后容量仍不足，下一拍再次回到 `blocked`。
- **运行中（running）手动 `/advance`、`/events` 返回 409**（`reason: manual_advance_while_running`）；`idle`/`paused`/`blocked` 下两种手动推进仍可正常使用（阻塞满额时依旧按 503 失败）。隐藏切换、接纳、通道开启/补取、结束等其他操作在四种状态下都可用，并在会话锁内与后台推进完整串行。
- **结束**：`/finish` 立即停止对该会话的驱动（注册表移除后在途后台任务取不到它）、按原规则一次性释放通道剩余容量，之后该 id 的一切请求（含四个自动播放接口）均 404。并发结束仍最多一次成功。
- 自动播放的全部驱动状态（running/paused/blocked、本段起点与单调时钟读数）**只存在于本进程内存**：不持久化、不跨重启；重启后会话本身即按未知处理（404）。创建、推进、事件推进、隐藏、通道、接纳、结束等旧接口的路径、请求与响应字段保持不变。

### 示例 29：启动、观察自动推进、暂停、恢复

沿用示例 26 的 `scene-chan`（s1 `[0,1000)`、s2 `[800,3000)` 锁轨 2、s3 `[1000,2500)`）。自动播放必须先开通道：

```bash
SID=$(curl -s -X POST http://localhost:8080/sessions \
  -H 'Content-Type: application/json' \
  -d '{"scene_id": "scene-chan"}' | python3 -c 'import sys,json; print(json.load(sys.stdin)["session_id"])')

# 未开通道直接启动 -> 409
curl -s -o /dev/null -w '%{http_code}\n' -X POST http://localhost:8080/sessions/$SID/autoplay/start
# 409

# 开启补取通道（初始 active 为 s1，seq 从 0 起）
curl -s -X POST http://localhost:8080/sessions/$SID/channel

# 启动自动播放：状态 running、游标 0
curl -s -X POST http://localhost:8080/sessions/$SID/autoplay/start
# {"session_id":"…","status":"running","cursor_ms":0}

# 约 0.3 秒后查询：游标随真实时间增长（后台每 50ms 一拍）
sleep 0.3
curl -s http://localhost:8080/sessions/$SID/autoplay
# {"session_id":"…","status":"running","cursor_ms":301}

# 运行中手动推进被拒绝：409
curl -s -o /dev/null -w '%{http_code}\n' -X POST http://localhost:8080/sessions/$SID/advance \
  -H 'Content-Type: application/json' -d '{"time_ms": 9999}'
# 409

# 期间自动推进产生的事件已按既有规则入队（800ms 时 s2 enter 在前）
curl -s -X POST http://localhost:8080/sessions/$SID/channel/fetch \
  -H 'Content-Type: application/json' -d '{"ack_seq": 0}'
# {"…","acked_seq":0,"events":[{"seq":1,"id":"s2",…,"time_ms":800,"type":"enter"}, …]}

# 暂停：保留游标，暂停期间不计时
curl -s -X POST http://localhost:8080/sessions/$SID/autoplay/pause
# {"session_id":"…","status":"paused","cursor_ms":301}
sleep 1
curl -s http://localhost:8080/sessions/$SID/autoplay   # 1 秒后游标仍是 301
# {"…","status":"paused","cursor_ms":301}

# 运行中才能暂停：暂停中再暂停 -> 409
curl -s -i -X POST http://localhost:8080/sessions/$SID/autoplay/pause
# HTTP/1.1 409 Conflict
# {"detail":{"message":"暂停仅限运行中的自动播放会话",
#            "reason":"pause_only_running","status":"paused"}}

# 恢复：从保留游标 301 重新计时，不补这 1 秒
curl -s -X POST http://localhost:8080/sessions/$SID/autoplay/resume
# {"…","status":"running","cursor_ms":301}
sleep 0.1
curl -s http://localhost:8080/sessions/$SID/autoplay   # 约 400，而不是 1300
# {"…","status":"running","cursor_ms":402}

# 结束：停止驱动、释放通道容量，之后自动播放接口同样 404
curl -s -o /dev/null -X POST http://localhost:8080/sessions/$SID/finish
curl -s -o /dev/null -w '%{http_code}\n' http://localhost:8080/sessions/$SID/autoplay
# 404
```

### 示例 30：满额转阻塞，确认释放后显式恢复（不追赶）

把共享容量调小（`CHANNEL_EVENT_LIMIT=4`，本地可 `CHANNEL_EVENT_LIMIT=4 uvicorn app.main:app --port 8080`；Compose 中改 `environment`）。用一段短字幕密集的场景，使某一拍的整批事件超过剩余容量：

```bash
curl -s -X PUT http://localhost:8080/scenes/scene-auto-full \
  -H 'Content-Type: application/json' \
  -d '{
    "track_count": 2,
    "expected_revision": 0,
    "subtitles": [
      {"id": "d0", "speaker": "D0", "start_ms": 100, "end_ms": 102},
      {"id": "d1", "speaker": "D1", "start_ms": 101, "end_ms": 103},
      {"id": "d2", "speaker": "D2", "start_ms": 102, "end_ms": 104},
      {"id": "d3", "speaker": "D3", "start_ms": 103, "end_ms": 105}
    ]
  }' >/dev/null

SID=$(curl -s -X POST http://localhost:8080/sessions \
  -H 'Content-Type: application/json' -d '{"scene_id": "scene-auto-full"}' \
  | python3 -c 'import sys,json; print(json.load(sys.stdin)["session_id"])')
curl -s -X POST http://localhost:8080/sessions/$SID/channel >/dev/null
curl -s -X POST http://localhost:8080/sessions/$SID/autoplay/start >/dev/null

# 等待后台进入 blocked（轮询查询接口）
for i in $(seq 1 50); do
  ST=$(curl -s http://localhost:8080/sessions/$SID/autoplay)
  echo "$ST" | grep -q '"blocked"' && break
  sleep 0.02
done
echo "$ST"
# {"session_id":"…","status":"blocked","cursor_ms":100}

# 阻塞不计时：游标停在保留值
sleep 0.3
curl -s http://localhost:8080/sessions/$SID/autoplay
# {"…","status":"blocked","cursor_ms":100}

# 阻塞时暂停/启动均 409；恢复只接受 paused 或 blocked（200 前先看队列）
curl -s -o /dev/null -w '%{http_code}\n' -X POST http://localhost:8080/sessions/$SID/autoplay/pause
# 409

# 补取已入队的前缀（seq 连续、无空洞），确认后释放共享容量
curl -s -X POST http://localhost:8080/sessions/$SID/channel/fetch \
  -H 'Content-Type: application/json' -d '{"ack_seq": 0}'
# {"…","acked_seq":0,"events":[ … 已入队的若干条，seq 从 1 起连续 … ]}

# 假设上一步返回了 N 条，确认这 N 条（N 换成实际数量）；确认后仍 blocked，
# 不会因容量空出来就自动恢复，也不会追赶这 0.3 秒
N=4
curl -s -X POST http://localhost:8080/sessions/$SID/channel/fetch \
  -H 'Content-Type: application/json' -d "{\"ack_seq\": $N}" >/dev/null
sleep 0.2
curl -s http://localhost:8080/sessions/$SID/autoplay
# {"…","status":"blocked","cursor_ms":100}

# 必须显式恢复：从保留游标 100 重新计时；容量已够，后续各拍整批入队
curl -s -X POST http://localhost:8080/sessions/$SID/autoplay/resume
# {"…","status":"running","cursor_ms":100}
sleep 0.1
curl -s http://localhost:8080/sessions/$SID/autoplay
# {"…","status":"running","cursor_ms":198}

# 恢复后的事件接着确认位置之后连续编号（无空洞、不重号）
curl -s -X POST http://localhost:8080/sessions/$SID/channel/fetch \
  -H 'Content-Type: application/json' -d "{\"ack_seq\": $N}"
# {"…","acked_seq":4,"events":[{"seq":5, …}, {"seq":6, …}, …]}
```

要点：满额的那一拍**整批失败**（不推进游标、不消费 `seq`、不部分入队，等价于手动推进的 503 语义但由后台转为 `blocked` 状态）；确认前缀只是腾出容量，会话**保持 blocked**，显式 `resume` 后从保留游标继续，停顿期间的墙钟时间**不会**被追回来。

### 启动/恢复的等锁计时修复说明

此前 `POST /sessions/{id}/autoplay/start` 与 `POST /sessions/{id}/autoplay/resume` 在**进入会话锁之前**读取单调时钟：当同一会话的接纳等操作正持锁时，控制请求**先取时、再排队等锁**，锁外等待的时长被算进本段计时——生效后的第一拍会把等锁时间额外推进到游标上，落在该区间内的字幕事件被**提前入队**（并消耗 `seq`）。

修复后，启动与恢复在**会话锁内、状态迁移串行生效的同一时刻**读取单调时钟、从当时的保留游标开新计时段：未启动、暂停、阻塞状态下在锁外等待的间隔一律不计入。受控时钟回归（`tests/test_autoplay.py`，可控时钟 + 线程交错）：游标 0、等锁 250 毫秒，生效后 10 毫秒推进一拍，游标为 **10**（而非 260），100 毫秒字幕**不入队、不消耗 seq**；暂停恢复与阻塞恢复路径同样覆盖。请求、响应与 404/409 语义不变：运行中手动推进仍 409，容量不足仍整批阻塞（游标、`seq`、队列不变），确认后仍须显式恢复。

下面脚本可直接复制执行：并发发起“接纳 + 启动”，二者在会话锁内串行生效，启动的计时段从它**实际生效**（而非开始等锁）的那一刻起算；随后暂停，再并发“接纳 + 恢复”，恢复同样只从生效时刻重新计时。

```bash
# 场景 + 会话 + 补取通道（自动播放前提）
curl -s -X PUT http://localhost:8080/scenes/scene-lockwait \
  -H 'Content-Type: application/json' \
  -d '{
    "track_count": 2,
    "expected_revision": 0,
    "subtitles": [
      {"id": "s1", "speaker": "Alice", "start_ms": 0,    "end_ms": 1000},
      {"id": "s2", "speaker": "Bob",   "start_ms": 800,  "end_ms": 3000, "locked_track": 2},
      {"id": "s3", "speaker": "Alice", "start_ms": 1000, "end_ms": 2500}
    ]
  }' >/dev/null
SID=$(curl -s -X POST http://localhost:8080/sessions \
  -H 'Content-Type: application/json' \
  -d '{"scene_id": "scene-lockwait"}' | python3 -c 'import sys,json; print(json.load(sys.stdin)["session_id"])')
curl -s -X POST http://localhost:8080/sessions/$SID/channel >/dev/null

# 场景修订为 revision 2（s3 延长），让接下来的接纳有实际工作
curl -s -X PUT http://localhost:8080/scenes/scene-lockwait \
  -H 'Content-Type: application/json' \
  -d '{
    "track_count": 2, "expected_revision": 1,
    "subtitles": [
      {"id": "s1", "speaker": "Alice", "start_ms": 0,    "end_ms": 1000},
      {"id": "s2", "speaker": "Bob",   "start_ms": 800,  "end_ms": 3000, "locked_track": 2},
      {"id": "s3", "speaker": "Alice", "start_ms": 1000, "end_ms": 4000}
    ]
  }' >/dev/null

# 并发“接纳 + 启动”：串行生效，计时段从启动实际生效的时刻起算，
# 不含在锁外等待接纳完成的时间
curl -s -X POST http://localhost:8080/sessions/$SID/accept &
curl -s -X POST http://localhost:8080/sessions/$SID/autoplay/start &
wait
# {"session_id":"…","status":"running","cursor_ms":0}

sleep 0.3
curl -s http://localhost:8080/sessions/$SID/autoplay
# {"…","status":"running","cursor_ms":300 左右} —— 只计启动生效后真实经过
# 的时间；等锁间隔不会被额外推进，区间内的字幕事件也不会提前入队

# 暂停后再并发“接纳 + 恢复”：恢复从保留游标、生效时刻重新计时
curl -s -X POST http://localhost:8080/sessions/$SID/autoplay/pause >/dev/null
curl -s -X PUT http://localhost:8080/scenes/scene-lockwait \
  -H 'Content-Type: application/json' \
  -d '{
    "track_count": 2, "expected_revision": 2,
    "subtitles": [
      {"id": "s1", "speaker": "Alice", "start_ms": 0,    "end_ms": 1000},
      {"id": "s2", "speaker": "Bob",   "start_ms": 800,  "end_ms": 3000, "locked_track": 2},
      {"id": "s3", "speaker": "Alice", "start_ms": 1000, "end_ms": 5000}
    ]
  }' >/dev/null
curl -s -X POST http://localhost:8080/sessions/$SID/accept &
curl -s -X POST http://localhost:8080/sessions/$SID/autoplay/resume &
wait
sleep 0.2
curl -s http://localhost:8080/sessions/$SID/autoplay
# 游标 ≈ 暂停时保留值 + 恢复生效后真实经过的 200ms——等锁与停顿时间都不计入

curl -s -o /dev/null -X POST http://localhost:8080/sessions/$SID/finish
```

### 与既有操作的关系

- `idle`（未启动）时一切旧行为不变：手动 `/advance`、`/events`、隐藏、接纳、通道、结束都照旧；自动播放是**叠加**在通道之上的可选驱动，不开通道即与旧版本完全一致。
- `running` 时仅禁止两种手动推进（409）；隐藏切换照常（与后台推进串行，显示变更事件照常入队），`/accept` 照常（接纳后后台按新快照继续推进，已开始字幕冻结规则不变），`/channel/fetch` 照常拉取自动推进产生的事件。
- `paused`/`blocked` 时可手动推进（满额仍 503）、可切换隐藏、可接纳、可补取；这些操作改变的游标就是随后 `resume` 的本段起始游标。

## 播放存档（checkpoint）：跨重启续播，保留未确认显示事件

播放会话与自动播放驱动本身只是单进程内存态，**进程重启后原会话即按未知处理（404）**。播放存档为此提供一条 SQLite 持久化路径：把某个会话某一时刻的**完整播放状态**按 `checkpoint_id` 落库，重启（或在另一时刻）按存档恢复为一个**全新会话**继续播放——未确认的显示事件、序号与确认边界一并保留，显示端不需要重新初始化。存档与场景写入**同一个 SQLite**（`SCENES_DB_PATH`，Compose 的 `scenes-data` 具名卷），因此沿用原来的单命令启动与数据卷，无需额外配置。

**保存**（两种等价写法，均无请求体/或集合式带 `checkpoint_id` 字段）：

| 接口 | 方法 | 说明 |
| --- | --- | --- |
| `/sessions/{session_id}/checkpoints/{checkpoint_id}` | PUT / POST | 按路径中的 `session_id` 与非空 `checkpoint_id` 保存 |
| `/sessions/{session_id}/checkpoints` | POST | 请求体 `{"checkpoint_id": "..."}`，语义相同 |

- 仅**已开启补取通道且自动播放不处于运行状态**的会话可以保存：自动播放状态为 `idle`（从未启动）、`paused`（已暂停）、`blocked`（满额阻塞）都允许；**未开通道**（`detail.reason: "channel_missing"`）或**运行中**（`detail.reason: "autoplay_running"`，请先暂停）返回 **409**。
- 存档与同会话的推进、事件推进、隐藏切换、接纳、补取等操作在**会话锁内完整串行**，因此落库的完整快照（字幕/分轨/`changes`/`source_revision`）、游标、隐藏集合、通道未确认事件与 `next_seq`/`acked_seq`/`max_returned_seq` 是同一份一致状态。
- 保存**成功不结束会话**：原会话、游标、隐藏集合与通道队列完全不变，可以继续播放或用别的 `checkpoint_id` 再次保存。保存**失败不留存档**，也不改变会话或通道容量。
- 状态码：成功 **200**；同名 `checkpoint_id`（全局）已存在 **409**（先保存的那份保留）；未知或已结束会话 **404**；`checkpoint_id` 为空或纯空白 **422**。

**恢复**：

| 接口 | 方法 | 说明 |
| --- | --- | --- |
| `/checkpoints/{checkpoint_id}/restore` | POST | 按存档恢复为新会话（无请求体） |
| `/sessions/checkpoints/{checkpoint_id}/restore` | POST | 会话命名空间别名，语义相同 |

- 恢复**不读取当前场景**：快照只取自存档，存档后场景被修订甚至不再可用都不影响恢复。
- 恢复出的会话获得**全新 `session_id`**，与存档源会话及其他恢复会话**相互独立**；同一份存档可**重复恢复**任意次，恢复与之后的任何操作都**不修改存档或其他会话**。
- 通道状态完整保留：未确认事件原样带回，`acked_seq`（确认边界）、`next_seq`（下一个事件序号）、`max_returned_seq`（补取越界上界）都不变；之后新产生的事件接着存档的 `next_seq` 连续编号，补取的“越界 422、旧确认不回退、重复补取内容不变”规则也完全沿用。
- 自动播放状态映射：存档时**未启动（idle）恢复后仍为 idle**；存档时**暂停（paused）或阻塞（blocked）恢复为暂停（paused）**——需要客户端显式 `autoplay/resume` 才续播，恢复以存档保留的游标作为**新一段起点重新计时，不追赶任何离线时间**（停顿区间内的事件会在恢复后的正常推进中入队，不会丢失）。
- 存档的未确认事件队列**计入 `CHANNEL_EVENT_LIMIT` 共享容量**：恢复时原子申请队列所需名额，不足返回 **503**（`detail` 给出 `limit`/`requested`/`available`），且**不创建会话、不留任何容量占用**；多个并发恢复由容量原子记账串行化，合计不可能超额，客户端确认前缀/结束会话腾出容量后用同一存档重试即可。空队列恢复不占容量。
- 状态码：成功 **200**（响应含新会话 id、存档 id、源会话 id、完整快照、游标、隐藏集合、播放状态与通道边界/事件）；未知存档 **404**；非法 `checkpoint_id` **422**；容量不足 **503**。
- 创建、推进、事件推进、隐藏、通道开启/补取、自动播放、接纳、结束等**旧接口的请求与响应完全不变**。

### 示例 31：保存存档——游标、隐藏集合、未确认事件与序号边界

沿用 `scene-chan`（s1 `[0,1000)`、s2 `[800,3000)` 锁轨 2、s3 `[1000,2500)`）。建会话、开通道，播放到 1000（s2 enter/s1 exit/s3 enter 已入队为 seq 1～3），再隐藏 Alice（s3 的 exit 入队为 seq 4）：

```bash
curl -s -X PUT http://localhost:8080/scenes/scene-chan \
  -H 'Content-Type: application/json' \
  -d '{
    "track_count": 2,
    "expected_revision": 0,
    "subtitles": [
      {"id": "s1", "speaker": "Alice", "start_ms": 0,    "end_ms": 1000},
      {"id": "s2", "speaker": "Bob",   "start_ms": 800,  "end_ms": 3000, "locked_track": 2},
      {"id": "s3", "speaker": "Alice", "start_ms": 1000, "end_ms": 2500}
    ]
  }' >/dev/null

SID=$(curl -s -X POST http://localhost:8080/sessions \
  -H 'Content-Type: application/json' \
  -d '{"scene_id": "scene-chan"}' | python3 -c 'import sys,json; print(json.load(sys.stdin)["session_id"])')
curl -s -X POST http://localhost:8080/sessions/$SID/channel >/dev/null
curl -s -X POST http://localhost:8080/sessions/$SID/advance \
  -H 'Content-Type: application/json' -d '{"time_ms": 900}' >/dev/null
curl -s -X POST http://localhost:8080/sessions/$SID/events \
  -H 'Content-Type: application/json' -d '{"time_ms": 1000}' >/dev/null
curl -s -X POST http://localhost:8080/sessions/$SID/hidden_speakers \
  -H 'Content-Type: application/json' -d '{"hidden_speakers": ["Alice"]}' >/dev/null
```

把此刻保存为存档 `cp-2026-10-04-a`（PUT 与 POST 均可）：

```bash
curl -s -X PUT http://localhost:8080/sessions/$SID/checkpoints/cp-2026-10-04-a
```

```json
{
  "checkpoint_id": "cp-2026-10-04-a",
  "session_id": "…",
  "scene_id": "scene-chan",
  "source_revision": 1,
  "cursor_ms": 1000,
  "status": "idle",
  "autoplay_status": "idle",
  "next_seq": 5,
  "acked_seq": 0,
  "max_returned_seq": 0,
  "queued_events": 4
}
```

保存成功后会话仍在：游标、隐藏集合（Alice）与 4 条未确认队列都不变，可以继续播放；用同一 id 再存返回 409（已有存档保留，本次失败不留任何东西）：

```bash
curl -s -o /dev/null -w '%{http_code}\n' -X PUT \
  http://localhost:8080/sessions/$SID/checkpoints/cp-2026-10-04-a   # 409（同名）
curl -s -o /dev/null -w '%{http_code}\n' -X PUT \
  http://localhost:8080/sessions/$SID/checkpoints/%20%20            # 422（空白 id）
curl -s -o /dev/null -w '%{http_code}\n' \
  http://localhost:8080/sessions/no-such/checkpoints/cp-x           # 404（未知会话）
```

未开通道或自动播放运行中保存返回 409（`detail.reason` 区分原因）；运行中的会话先暂停即可保存（暂停态存档，恢复后是暂停态）：

```bash
SID2=$(curl -s -X POST http://localhost:8080/sessions \
  -H 'Content-Type: application/json' -d '{"scene_id": "scene-chan"}' \
  | python3 -c 'import sys,json; print(json.load(sys.stdin)["session_id"])')
curl -s -i -X PUT http://localhost:8080/sessions/$SID2/checkpoints/cp-nochan
# HTTP/1.1 409 Conflict
# {"detail":{"reason":"channel_missing", ...}}
```

### 示例 32：重启后恢复——新会话、新 id，状态原样续播

存档与场景在同一个具名卷里的 SQLite 中，因此 `docker compose restart`、甚至 `docker compose down && docker compose up`（不带 `-v`）后存档仍在。重启后旧会话 id 已不存在，但存档可以恢复为**全新会话**：

```bash
# 重启后：旧会话按未知处理
curl -s -o /dev/null -w '%{http_code}\n' -X POST http://localhost:8080/sessions/$SID/events \
  -H 'Content-Type: application/json' -d '{"time_ms": 2000}'
# 404

# 按存档恢复为新会话
curl -s -X POST http://localhost:8080/checkpoints/cp-2026-10-04-a/restore
```

```json
{
  "session_id": "8b51…（全新 id）",
  "checkpoint_id": "cp-2026-10-04-a",
  "source_session_id": "…（存档来源会话，仅信息用途）",
  "scene_id": "scene-chan",
  "source_revision": 1,
  "cursor_ms": 1000,
  "status": "idle",
  "autoplay_status": "idle",
  "track_count": 2,
  "changes": 0,
  "hidden_speakers": ["Alice"],
  "subtitles": [ … 存档时的完整快照，不读取场景当前内容 … ],
  "assignments": [
    {"id": "s1", "track": 1},
    {"id": "s2", "track": 2},
    {"id": "s3", "track": 1}
  ],
  "channel": {
    "acked_seq": 0,
    "next_seq": 5,
    "max_returned_seq": 0,
    "queued_count": 4,
    "events": [
      {"seq": 1, "id": "s2", "speaker": "Bob",   "track": 2, "time_ms": 800,  "type": "enter"},
      {"seq": 2, "id": "s1", "speaker": "Alice", "track": 1, "time_ms": 1000, "type": "exit"},
      {"seq": 3, "id": "s3", "speaker": "Alice", "track": 1, "time_ms": 1000, "type": "enter"},
      {"seq": 4, "id": "s3", "speaker": "Alice", "track": 1, "time_ms": 1000, "type": "exit"}
    ]
  }
}
```

要点：

- `session_id` 是全新的；游标停在存档时的 **1000**，隐藏集合仍是 **Alice**，快照是存档那一份（存档后场景再被修订也不读取）。
- 通道里 4 条未确认事件原样保留，`next_seq` 为 **5**：后续新事件从 seq 5 编号，确认边界与越界规则沿用。
- 存档时未启动自动播放，恢复后仍是 **idle**，可直接手动推进或先 `autoplay/start`。

### 示例 33：补取未确认事件并继续播放（序号连续）

显示端用恢复响应里的 `session_id` 继续操作。先补回 4 条未确认事件，确认前 4 条，再事件推进到 3000——新事件接着 seq 5 编号：

```bash
NSID=$(curl -s -X POST http://localhost:8080/checkpoints/cp-2026-10-04-a/restore \
  | python3 -c 'import sys,json; print(json.load(sys.stdin)["session_id"])')

# 补取存档保留的未确认事件（重复补取内容与序号不变）
curl -s -X POST http://localhost:8080/sessions/$NSID/channel/fetch \
  -H 'Content-Type: application/json' -d '{"ack_seq": 0}'
# {"…","acked_seq":0,"events":[seq 1, seq 2, seq 3, seq 4]}

# 确认这 4 条并继续播放到 3000（Alice 仍隐藏：只剩 s2 的 3000 退出入队）
curl -s -X POST http://localhost:8080/sessions/$NSID/channel/fetch \
  -H 'Content-Type: application/json' -d '{"ack_seq": 4}' >/dev/null
curl -s -X POST http://localhost:8080/sessions/$NSID/events \
  -H 'Content-Type: application/json' -d '{"time_ms": 3000}' >/dev/null
curl -s -X POST http://localhost:8080/sessions/$NSID/channel/fetch \
  -H 'Content-Type: application/json' -d '{"ack_seq": 4}'
```

```json
{
  "session_id": "…",
  "scene_id": "scene-chan",
  "acked_seq": 4,
  "events": [
    {"seq": 5, "id": "s2", "speaker": "Bob", "track": 2, "time_ms": 3000, "type": "exit"}
  ]
}
```

确认位置、越界校验（`ack_seq` 不得超过此前补取返回过的最大 seq）与旧确认不回退都和存档前一致：

```bash
curl -s -o /dev/null -w '%{http_code}\n' -X POST http://localhost:8080/sessions/$NSID/channel/fetch \
  -H 'Content-Type: application/json' -d '{"ack_seq": 99}'   # 422（越界，上界为 5）
```

### 示例 34：暂停/阻塞存档恢复为暂停，显式恢复续播、不追赶离线时间

自动播放运行中**不能**存档（409）；在暂停或满额阻塞时存档，恢复出来的会话是**暂停态**，存档与重启之间过去多久都不会被追回来。

```bash
# 新建一个会话（自动播放前必须开通道）
SID=$(curl -s -X POST http://localhost:8080/sessions \
  -H 'Content-Type: application/json' -d '{"scene_id": "scene-chan"}' \
  | python3 -c 'import sys,json; print(json.load(sys.stdin)["session_id"])')
curl -s -X POST http://localhost:8080/sessions/$SID/channel >/dev/null
curl -s -X POST http://localhost:8080/sessions/$SID/autoplay/start
sleep 0.3

# 运行中存档是 409（autoplay_running）
curl -s -i -X PUT http://localhost:8080/sessions/$SID/checkpoints/cp-while-running
# HTTP/1.1 409 Conflict
# {"detail":{"reason":"autoplay_running","status":"running", …}}

# 暂停（保留游标，暂停期间不计时）后再存档 -> 200
curl -s -X POST http://localhost:8080/sessions/$SID/autoplay/pause
# {"session_id":"…","status":"paused","cursor_ms":301}
curl -s -X PUT http://localhost:8080/sessions/$SID/checkpoints/cp-paused
# {"checkpoint_id":"cp-paused", …,"status":"paused","cursor_ms":301, …}

# …… 服务重启（docker compose restart），无论停多久 ……

# 恢复：状态为 paused，游标仍是 301，后台不驱动
curl -s -X POST http://localhost:8080/checkpoints/cp-paused/restore
# {"session_id":"…（全新）", …,"cursor_ms":301,"status":"paused", …}

# 必须显式恢复：从 301 重新计时，不补停机期间的时间
NSID=…
curl -s -X POST http://localhost:8080/sessions/$NSID/autoplay/resume
# {"session_id":"…","status":"running","cursor_ms":301}
sleep 0.1
curl -s http://localhost:8080/sessions/$NSID/autoplay
# {"…","status":"running","cursor_ms":402}   # 约 400，而不是 301 + 离线时长
```

阻塞（`blocked`，共享容量满额）时存档的恢复语义相同：恢复为 `paused`、保留游标与队列；先用 `/channel/fetch` 确认前缀腾出容量，再 `autoplay/resume` 即可。恢复本身也需要队列名额——容量不足时返回 **503** 且不留会话或容量占用：

```bash
# CHANNEL_EVENT_LIMIT 较小时，若全进程容量已被其他会话占满：
curl -s -i -X POST http://localhost:8080/checkpoints/cp-paused/restore
# HTTP/1.1 503 Service Unavailable
# {"detail":{"message":"补取通道未确认事件总量已达上限：本次事件无法完整入队，状态未改变；确认前缀或结束会话释放容量后可重试原请求",
#            "limit":1024,"requested":4,"available":0}}

# 同一份存档可重复恢复为相互独立的会话（彼此不影响，存档也不被修改）
curl -s -X POST http://localhost:8080/checkpoints/cp-paused/restore > /tmp/r1.json
curl -s -X POST http://localhost:8080/checkpoints/cp-paused/restore > /tmp/r2.json
python3 -c 'import json;a=json.load(open("/tmp/r1.json"));b=json.load(open("/tmp/r2.json"));print(a["session_id"]!=b["session_id"], a["cursor_ms"]==b["cursor_ms"])'
# True True
```

## 本地开发与测试（不使用 Docker）

需 Python 3.11+：

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
uvicorn app.main:app --reload --port 8080
pytest
```

本地（非 Docker）运行时，场景数据库默认写入工作目录下的 `scenes.db`；可用环境变量改到别处，例如 `SCENES_DB_PATH=/tmp/scenes.db uvicorn app.main:app --port 8080`。补取通道共享容量同理可用 `CHANNEL_EVENT_LIMIT` 覆盖（正整数，默认 1024；非法值拒绝启动），例如 `CHANNEL_EVENT_LIMIT=256 uvicorn app.main:app --port 8080`。

核心求解器（`app/solver.py`）无第三方依赖，也可直接运行其测试：

```bash
python tests/test_solver.py
```

## 算法说明

输入规模很小（≤12 条、≤3 轨），采用带剪枝的深度优先枚举：

1. 按输入顺序逐条分配轨道，每个位置按轨道号 1→k 升序尝试；
2. 候选轨道必须未被任何时间重叠的已分配字幕占用，锁定字幕只尝试锁定轨；
3. 代价在“同一说话人按时间排序后相邻”的字幕对两端都落位时累计（换轨计 1）；
4. 剪枝：当前代价不低于已找到的最优解时剪枝。由于按轨道号升序枚举，第一个到达的最优解即为字典序最小的轨道序列，无需额外比较。

实现已用穷举参考解对 4000 组随机实例交叉验证。

## 目录结构

```
app/
  main.py      # FastAPI 路由与请求/响应模型（Pydantic 校验），含片段另存 /scenes/{id}/clip、场景拼接 /scenes/concat、会话结束 /sessions/{id}/finish、补取通道 /sessions/{id}/channel(/fetch)、自动播放 /sessions/{id}/autoplay/(start|pause|resume) 与播放存档 /sessions/{id}/checkpoints(/{cp_id})、/checkpoints/{cp_id}/restore
  solver.py    # 纯 Python 求解器与业务校验（无第三方依赖）
  storage.py   # 场景持久化：本地 SQLite（乐观锁版本控制；片段/拼接核对全部源版本与创建目标为同一事务；playback_checkpoints 表保存跨重启播放存档）
  playback.py  # 播放会话：单进程内存态，推进/事件推进/临时隐藏说话人/补取通道/接纳/结束串行化（接纳含场景读取的完整操作；结束一次取回最终快照并移除注册表；通道记录全部 enter/exit 供丢响应后补取确认；CHANNEL_EVENT_LIMIT 共享容量随生产原子申请、确认前缀/结束释放）、自动播放（每 50ms 按单调时钟整批推进，满额转 blocked、确认后显式恢复，暂停/阻塞不计时、不追赶；状态机 idle/running/paused/blocked）、播放存档（锁内序列化完整状态；恢复为全新会话、队列计入共享容量、idle 保 idle/paused/blocked 转 paused 重新计时不追赶）与保轨重算
tests/
  conftest.py            # pytest 夹具（场景接口使用临时数据库、会话隔离）
  test_solver.py         # 求解器单元测试
  test_api.py            # /assign HTTP 层测试
  test_scenes.py         # 场景创建/修订/读取/版本冲突/并发测试
  test_clips.py          # 片段另存：裁剪/保轨/边界、404/409/422、原子性、源与会话不变、独立性、快照错配回归
  test_concat.py         # 场景拼接：时间平移/保轨/id/changes、同源重复、严格整数、轨道数/时长/字幕总数、404/409/422、原子性、提交前源修订回归
  test_playback.py       # 播放会话创建/推进/接纳/冲突/并发测试
  test_hidden_speakers.py # 临时隐藏说话人：切换事件顺序、推进/事件排除、接纳保留、快照不变、404/422、串行化
  test_finish.py         # 会话结束：最终快照字段、404、不读未接纳修订、并发结束唯一成功、等待中的推进/事件推进/接纳/重复结束可控交错
  test_channel.py        # 补取通道：开启/重复开启、三类操作入队与短字幕、ack 边界与不回退、重复补取不变、结束清除、404/409/422、串行化
  test_channel_limit.py  # 通道共享容量：默认/配置/非法拒绝启动、生产原子申请与满额 503（游标/隐藏集合/seq/队列不变）、无事件与未开启通道不占、确认前缀释放/读取与旧确认不释放、结束一次性释放、跨会话总额、并发不越限/不死锁/不泄漏、重试
  test_autoplay.py       # 自动播放：状态机 409/404、真实时间推进与本段起点、暂停不计时/恢复不追赶、满额整批转 blocked（游标/seq/队列不变）/确认后显式恢复、运行中手动推进 409、其他操作串行、结束停止驱动并释放容量（含注入时钟的确定性管理器测试）、启动/暂停恢复/阻塞恢复的等锁不计时回归（可控时钟 + 线程交错）与无竞争路径
  test_checkpoint.py     # 播放存档：保存前置条件（已开通道/非 running）、同名 409、404/422、存档内容（快照/游标/隐藏/通道事件与序号边界）、恢复为新会话且不读场景、序号与确认边界保留、idle 保 idle/paused/blocked 转 paused、resume 重新计时不追赶、重复恢复独立、恢复 503 不留会话/占用、并发不超额、模拟重启后恢复、旧接口不变
Dockerfile
docker-compose.yml   # 挂载 scenes-data 具名卷到 /data（场景与播放存档同在 SCENES_DB_PATH 的 SQLite 中）；CHANNEL_EVENT_LIMIT 配置通道共享容量（默认 1024）
requirements.txt / requirements-dev.txt
```
