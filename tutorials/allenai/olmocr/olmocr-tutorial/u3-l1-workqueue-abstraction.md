# WorkQueue 抽象：Backend 接口与队列语义

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 `WorkItem`、`Backend`、`WorkQueue` 三个角色各自承担的职责，以及 Backend 接口八个方法的分工。
2. 描述 `populate_queue` 的完整流程：路径级去重 → 排序 → 按 `items_per_group` 切组 → sha1 组哈希 → CSV 编码落盘，并解释每一步为什么存在。
3. 理解 `initialize_queue` 的洗牌逻辑与 `get_work` 的三级跳过策略（本地缓存 → done 标记 → worker 锁），以及 `mark_done` 中「先立旗、后放锁」的顺序设计。
4. 亲手在一个临时 workspace 上跑通「入队 → 初始化 → 领任务 → 完成」的完整生命周期，并核对磁盘上的每一个文件。

## 2. 前置知识

本讲建立在第 1、2 单元已有认知之上，先帮你把几个关键概念补齐。

**工作队列（work queue）与任务队列。** 在 u2-l1 中我们看到，`main()` 只做编排，真正干活的是 N 个 `worker` 协程。worker 从哪里拿活？答案就是本讲的 `WorkQueue`。它是一个典型的**生产者-消费者**结构：`populate_queue` 往队列里放活，`worker` 通过 `get_work` 领活，干完后 `mark_done` 销账。

**断点续跑（resumable）。** 百万级 PDF 的批处理不可能一次跑完：机器会崩、任务会被抢占、你会主动中断。olmOCR 的做法是把队列的**持久状态**全部落在 workspace 的文件里（u1-l3 介绍过五大产物），内存里的 `asyncio.Queue` 只是每次启动时从磁盘重建出来的临时视图。本讲会精确回答「哪些东西落盘、哪些东西在内存」。

**乐观并发与「标记」语义。** 多台机器（或多个进程）可能同时指向同一个 workspace。olmOCR 不用中心化的锁服务，而是用两个约定俗成的「标记」协调：

- **done flag**：`done_flags/done_{hash}.flag`，表示某个工作项已完成，任何人都不必再做。
- **worker lock**：`worker_locks/worker_{hash}.lock`，表示某个工作项正被某个 worker 处理，别人先别碰。

这本质是一种**乐观并发控制**：先领活、后检查冲突；冲突了（锁被占）就跳过去做下一个。代价是极端情况下可能重复计算，u2-l8 讲过结果落盘是「整文件覆盖」的，所以重复计算是幂等的、可接受的。

**CSV 编码与 zstd 压缩。** 队列索引 `work_index_list.csv.zstd` 是一个 zstd 压缩的 CSV 文件，每行形如 `组哈希,路径1,路径2,...`。为什么用 CSV？因为 S3 路径、`tarball::成员名` 这类字符串完全可能包含逗号，直接用 `','.join()` 切分会切错，必须用 CSV 规则转义（加双引号）。为什么压缩？百万行的索引能省不少空间与传输量。

**sha1 与内容寻址。** sha1 把任意字节串映射成 40 个十六进制字符。olmOCR 用它给「一组路径」算出一个确定性的身份 ID——同样的路径集合无论在哪台机器上算，哈希都相同。done flag、worker lock、结果文件名（u2-l8 的 `output_{hash}.jsonl`）全部以这个哈希为键，这就是**内容寻址**（content-addressing）：ID 即内容摘要，天然去重、天然跨机一致。

## 3. 本讲源码地图

| 文件 | 角色 | 本讲关注点 |
| --- | --- | --- |
| [olmocr/work_queue.py](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/work_queue.py) | 本讲主战场 | `WorkItem`、`Backend`、`WorkQueue` 三个类与 `LocalBackend` 的锁实现 |
| [olmocr/pipeline.py](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py) | 队列的消费方 | `main()` 在哪里创建、填充、初始化队列；`worker()` 怎么消费 |
| [olmocr/s3_utils.py](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/s3_utils.py) | S3 工具函数 | `S3Backend` 依赖的 `download_zstd_csv` / `upload_zstd_csv` 等（u3-l2、u4-l3 详讲） |

阅读建议：本讲以 `work_queue.py`（共 474 行）为主线，pipeline.py 只看四处调用点。

## 4. 核心概念与源码讲解

### 4.1 WorkItem、Backend 与 WorkQueue：一张接口地图

#### 4.1.1 概念说明

`work_queue.py` 用三个类把「队列」拆成了职责清晰的三层：

- **`WorkItem`（工作项）**：最小任务单元，只有两个字段——`hash`（这组路径的身份 ID）和 `work_paths`（这组里所有待处理文档的路径）。注意队列的粒度是「一组 PDF」，不是「单个 PDF」：u2-l2 讲过 `items_per_group` 按平均页数估算，一组大约对应 `--pages_per_group`（默认 500）页，这样每个工作项的耗时大致均匀。
- **`Backend`(存储后端)**：纯接口（抽象基类），规定「队列的持久状态如何读写」。它不知道任何调度逻辑，只提供八个原语：索引的读/写、完成集合的查询、单个完成标记的查询/创建、锁的查询/创建/删除。`LocalBackend`（本地文件）与 `S3Backend`（S3 对象）是它的两个实现——同一套队列语义，两种物理落地，下一讲对比。
- **`WorkQueue`（队列本体）**：持有内存中的 `asyncio.Queue`，实现调度语义——入队去重分组、初始化剔除已完成、领任务时的跳过与加锁、完成时的销账。它通过 `Backend` 读写持久状态。

这个分层的价值：调度逻辑写一遍，换存储后端不用改一行队列代码；反之，给 Backend 加新实现（比如 GCS）也不动调度。

#### 4.1.2 核心流程

`WorkQueue` 的完整生命周期：

```text
┌─────────────────────────────────────────────────────────────┐
│  populate_queue(paths, items_per_group)                      │
│    去重 → 读旧索引 → 求新路径 → 排序 → 切组 → 算组哈希        │
│    → 合并旧组+新组 → Backend.save_index_lines() 落盘          │
├─────────────────────────────────────────────────────────────┤
│  initialize_queue()                                          │
│    读索引 → 查 done flags → 剔除已完成 → random.shuffle      │
│    → 灌入内存 asyncio.Queue → 返回队列长度                    │
├─────────────────────────────────────────────────────────────┤
│  循环: get_work() ──► worker 处理 ──► mark_done(item)        │
│    三级跳过: 本地缓存 / done flag / worker lock               │
│    领到: create_worker_lock 后返回 WorkItem                   │
│    完成: create_done_flag + delete_worker_lock                │
└─────────────────────────────────────────────────────────────┘
```

对应 pipeline.py 中的真实调用点：`main()` 创建队列（本地或 S3）、`--pdfs` 存在时填充、推理前初始化，然后交给 worker 循环消费。

#### 4.1.3 源码精读

先看最小的数据单元 `WorkItem`：

- [olmocr/work_queue.py:L30-L35](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/work_queue.py#L30-L35) — 用 `@dataclass` 定义的工作项：`hash` 是组身份，`work_paths` 是这一组内所有文档路径。没有进度、没有状态字段——完成与否不记在队列里，而是记在后端的 done flag 上。

再看 `Backend` 抽象基类的八个方法（按「索引、完成、锁」三类记忆）：

- [olmocr/work_queue.py:L38-L49](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/work_queue.py#L38-L49) — `Backend` 继承 `abc.ABC`，`load_index_lines` / `save_index_lines` 负责队列索引（那个 zstd CSV）的读与写，是队列唯一的「全量清单」。
- [olmocr/work_queue.py:L51-L59](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/work_queue.py#L51-L59) — `get_completed_hashes` 一次性取回全部已完成哈希（初始化时用）；`is_completed` 查单个哈希是否已完成（领任务时逐项用）。
- [olmocr/work_queue.py:L61-L74](https://github.com/allenai/olmocr/blob/f7cfe4c22098b6ec950d1c0a464eecf8d/olmocr/work_queue.py#L61-L74) — 锁三件套：`is_worker_lock_taken`（锁存在且未过期？）、`create_worker_lock`、`delete_worker_lock`。注意 `is_worker_lock_taken` 带默认 1800 秒超时参数——锁的「新鲜度」由 mtime 判定，后面 4.3 详说。
- [olmocr/work_queue.py:L76-L79](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/work_queue.py#L76-L79) — `create_done_flag`：写一个 `done_{hash}.flag` 空文件即算完成标记。

八个方法职责一览：

| 方法 | 类型 | 语义 | LocalBackend 的落地 |
| --- | --- | --- | --- |
| `load_index_lines` | 读 | 读队列索引（CSV 行列表） | 解压 `work_index_list.csv.zstd` |
| `save_index_lines` | 写 | 覆盖写队列索引 | 压缩写回同一文件 |
| `get_completed_hashes` | 读 | 全量已完成哈希集合 | 列目录 `done_flags/done_*.flag` |
| `is_completed(hash)` | 读 | 单项是否已完成 | 探测 `done_{hash}.flag` 是否存在 |
| `is_worker_lock_taken(hash)` | 读 | 锁是否被占且未过期 | 取 `worker_{hash}.lock` 的 mtime 比较 |
| `create_worker_lock(hash)` | 写 | 占锁 | 建空文件 `worker_{hash}.lock` |
| `delete_worker_lock(hash)` | 写 | 放锁 | 删该文件 |
| `create_done_flag(hash)` | 写 | 标记完成 | 建空文件 `done_{hash}.flag` |

最后是 `WorkQueue` 本体的初始化：

- [olmocr/work_queue.py:L87-L90](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/work_queue.py#L87-L90) — `WorkQueue.__init__` 只持有一个 backend、一个内存队列 `self._queue` 和一个**已完成哈希的本地缓存** `self._completed_hash_cache`（初始为空集）。记住这三个字段，后面所有方法都在操作它们。

还要注意两个模块级常量：

- [olmocr/work_queue.py:L26-L27](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/work_queue.py#L26-L27) — `worker_locks` 与 `done_flags` 两个目录名是本地/S3 后端共用的约定，就是 u1-l3 里你在 workspace 下看到的那两个目录。

#### 4.1.4 代码实践

**实践目标**：用 Python 内省（introspection）把 `Backend` 接口「照」出来，建立接口地图的肌肉记忆。

**操作步骤**：

1. 在已安装 olmocr 的环境中启动 `python`（轻量安装即可，本实践不碰 GPU）。
2. 依次执行：

```python
import inspect
from olmocr.work_queue import Backend, WorkItem, WorkQueue

# 1. 列出 Backend 的全部抽象方法及签名
for name, fn in sorted(inspect.getmembers(Backend, inspect.isfunction)):
    if not name.startswith("__"):
        print(f"{name}{inspect.signature(fn)}")

# 2. 观察 WorkItem 的字段
import dataclasses
print([f.name for f in dataclasses.fields(WorkItem)])

# 3. 确认 Backend 不能被实例化（抽象类）
try:
    Backend()
except TypeError as e:
    print("TypeError:", e)
```

**需要观察的现象**：第一步应打印出八个方法名及参数签名（与你上面那张表逐行核对）；第二步打印 `['hash', 'work_paths']`；第三步抛出 `TypeError`，说明抽象基类只能被继承、不能直接实例化。

**预期结果**：`Backend` 的八个抽象方法与 4.1.3 的表格一一对应。（依据源码结构推得，待本地验证。）

#### 4.1.5 小练习与答案

**练习 1**：为什么 `WorkItem` 只有 `hash` 和 `work_paths` 两个字段，连「处理进度」都没有？

**答案**：队列的最小单元是「一组文档」，完成以**整组**为单位原子判定——整组落盘后才写 done flag（u2-l1 讲过 worker 的 TaskGroup 同生共死）。中间进度没有跨进程可见的价值，所以不进队列结构；一旦需要知道「做完没有」，查的是后端的 done flag 而非内存对象。

**练习 2**：`WorkQueue._queue` 是 `asyncio.Queue`，进程崩溃后它里面的工作项会丢吗？丢了要紧吗？

**答案**：会丢（纯内存）。但不要紧：持久状态是索引文件 + done flags + 结果文件，重启后 `initialize_queue` 从索引重建内存队列并剔除已完成项，一切如初。这正是「内存视图 + 磁盘事实」的设计。

**练习 3**：如果想把队列状态存到 GCS 而不是 S3，要改哪些类？

**答案**：只需新写一个 `GCSBackend(Backend)` 实现八个抽象方法；`WorkQueue` 一行都不用改——这就是抽象基类带来的开闭原则收益。

### 4.2 populate_queue：去重、分组与 sha1 组哈希

#### 4.2.1 概念说明

`populate_queue(work_paths, items_per_group)` 解决的问题是：**把一批新文档路径幂等地并入队列索引**。三个关键词：

- **幂等**：同一批路径重复传入不会产生重复分组。pipeline 里 `main()` 每次带 `--pdfs` 启动都会调用它，重跑是常态。
- **分组**：新路径按 `items_per_group` 个一组切分。u2-l2 讲过这个数由「目标页数 ÷ 平均页数」估出；tarball 则固定每组 1 个（见 pipeline.py 的两次调用）。
- **组哈希**：每组算一个 sha1 作为身份。这个哈希是后续一切协调的钥匙——done flag、worker lock、结果文件名都用它。

为什么哈希前要先 `sorted`？因为 Python 的 set 迭代顺序受字符串哈希随机化影响（PYTHONHASHSEED），同一组路径在不同进程里的顺序可能不同。先排序再哈希，保证**任何机器、任何时刻**对同一路径集合算出同一个 ID。公式化地说，对路径集合 \( G \)：

\[ \mathrm{hash}(G) = \mathrm{sha1}\big(\,\texttt{utf8}(p_1) \,\Vert\, \texttt{utf8}(p_2) \,\Vert\, \cdots \,\Vert\, \texttt{utf8}(p_k)\,\big), \quad p_1 < p_2 < \cdots < p_k = \mathrm{sorted}(G) \]

其中 \( \Vert \) 表示字节串直接拼接（无分隔符）。

#### 4.2.2 核心流程

`populate_queue` 的执行步骤（伪代码）：

```text
输入: work_paths（新一批路径）, items_per_group
1. all_paths  = set(work_paths)                # 入参级去重
2. lines      = backend.load_index_lines()      # 读现有索引
3. existing   = 解析 lines → {组哈希: [路径...]}
4. existing_paths = existing 中所有路径摊平成集合
5. new_paths  = sorted(all_paths - existing_paths)   # 跨批次去重 + 排序
6. 若 new_paths 为空 → 直接返回（什么都不写）
7. 把 new_paths 按 items_per_group 逐段切片
   每段算 _compute_workgroup_hash(段) → 得到 (组哈希, 段)
8. combined = existing ∪ 新组                  # 同哈希时新组覆盖旧组
9. backend.save_index_lines(编码后的 combined)  # 整文件覆盖写
```

两个值得咀嚼的细节：

- **去重是路径级的，不是组级的**。第 5 步在「路径」粒度上做差集。后果：如果第二次运行换了 `items_per_group`，**已有分组不会被重新切分**——旧路径全部命中 `existing_paths` 被剔除，只有新增路径按新的组大小分组。索引一旦写入，分组就冻结了。
- **第 5 步的 `sorted` 有双重作用**：既让分组结果确定（同批路径在任何机器上切出相同的组），也让组内顺序确定，进一步稳定组哈希。

CSV 编解码是索引的「物理格式」层：每行 `[组哈希, 路径1, 路径2, ...]` 用 `csv.writer` 的 `QUOTE_MINIMAL` 规则转义——只有含逗号/引号的字段才会被双引号包裹，其余裸写。

#### 4.2.3 源码精读

主函数：

- [olmocr/work_queue.py:L127-L140](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/work_queue.py#L127-L140) — `populate_queue` 开头：`set(work_paths)` 入参去重；读旧索引并解析成 `existing_groups`；把所有已有路径摊平成 `existing_path_set`；`new_paths = sorted(all_paths - existing_path_set)` 求差集并排序；若没有新路径直接 `return`，**不触发任何写操作**。
- [olmocr/work_queue.py:L142-L151](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/work_queue.py#L142-L151) — 切组循环：`range(0, len(new_paths), items_per_group)` 步进，`new_paths[i : i + items_per_group]` 取段（最后一段可能不足额），对每段调 `_compute_workgroup_hash`；随后 `{**existing_groups, **dict(new_groups)}` 合并（同哈希的新组覆盖旧组——同路径集合算出的哈希本来就相同，覆盖无害）；最后 `_make_index_lines` 编码并 `save_index_lines` 落盘。**注意是整文件覆盖写**，不是增量追加，所以重跑 populate 也是幂等的。

组哈希：

- [olmocr/work_queue.py:L105-L111](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/work_queue.py#L105-L111) — `_compute_workgroup_hash`：对 `sorted(work_paths)` 逐个 `sha1.update(path.encode("utf-8"))`，返回十六进制摘要。三行核心代码承载了「跨机确定性」的全部要求：排序消除顺序、utf-8 消除编码歧义、sha1 保证碰撞概率可忽略。

索引的编解码与序列化：

- [olmocr/work_queue.py:L92-L98](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/work_queue.py#L92-L98) — `_encode_csv_row`：用 `csv.writer`（`QUOTE_MINIMAL`）把一行字符串列表写成 CSV 行，`strip()` 掉 `writerow` 自动加的换行符。含逗号的路径（比如 `tarball::weird,name.pdf`）会被自动加双引号。
- [olmocr/work_queue.py:L100-L103](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/work_queue.py#L100-L103) — `_decode_csv_row`：`next(csv.reader([line]))` 把单行还原成字段列表，自动处理引号转义。与 encode 严格互逆。
- [olmocr/work_queue.py:L113-L121](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/work_queue.py#L113-L121) — `_parse_index_lines`：逐行解码，`parts[0]` 是组哈希作键，`parts[1:]` 是路径列表；空行跳过。
- [olmocr/work_queue.py:L123-L125](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/work_queue.py#L123-L125) — `_make_index_lines`：逆操作，`[组哈希] + 路径列表` 编码成一行。

pipeline 侧的真实调用（衔接 u2-l1/u2-l2）：

- [olmocr/pipeline.py:L1407-L1412](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L1407-L1412) — `main()` 调了**两次** `populate_queue`：普通 PDF 用动态估算的 `items_per_group`；tarball 固定每组 1 个（一个 tarball 就是一个工作项）。
- [olmocr/pipeline.py:L1328-L1332](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L1328-L1332) — 队列创建处的二选一：workspace 以 `s3://` 开头用 `S3Backend`，否则 `LocalBackend`。这就是「同一语义、两种落地」的分岔点。

#### 4.2.4 代码实践

**实践目标**：亲手建一个临时 workspace，加入 5 个假 PDF 路径（`items_per_group=2`），解开 zstd 看索引原文，并用 `_compute_workgroup_hash` 核对每行的组哈希。

**操作步骤**：

1. 把下面的脚本存为 `practice_populate.py`（放在仓库外的任意目录均可，不依赖 GPU）：

```python
# 示例代码：本讲实践脚本
import asyncio, csv, os, tempfile, zstandard
from olmocr.work_queue import LocalBackend, WorkQueue

async def main():
    ws = tempfile.mkdtemp(prefix="wq_practice_")
    print("workspace:", ws)

    wq = WorkQueue(LocalBackend(ws))
    paths = [f"/fake/docs/paper_{i}.pdf" for i in range(5)]
    await wq.populate_queue(paths, items_per_group=2)   # 5 个路径，每组 2 个

    idx = os.path.join(ws, "work_index_list.csv.zstd")
    print("index exists:", os.path.exists(idx))

    raw = zstandard.ZstdDecompressor().decompress(open(idx, "rb").read()).decode("utf-8")
    print("--- 解压后的索引原文 ---")
    print(raw)

    print("--- 逐行核对组哈希 ---")
    for line in raw.splitlines():
        cells = next(csv.reader([line]))          # 用 CSV 规则解码，而非 split(",")
        recomputed = WorkQueue._compute_workgroup_hash(cells[1:])
        print("hash 匹配:", cells[0] == recomputed, "| 组大小:", len(cells) - 1)

    print("workspace 子目录:", sorted(os.listdir(ws)))

asyncio.run(main())
```

2. 运行 `python practice_populate.py`。
3. 观察 workspace 目录下出现了哪些东西（`LocalBackend.__init__` 建了哪几个目录，见 [olmocr/work_queue.py:L244-L252](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/work_queue.py#L244-L252)）。
4. 再跑一遍同一个脚本（或对同一 workspace 重复 `populate_queue(paths, 2)`），看索引是否变长。

**需要观察的现象**：

- 5 个路径按 2 个一组切成 **3 组**：`[paper_0, paper_1]`、`[paper_2, paper_3]`、`[paper_4]`（最后一段只有 1 个），索引应有 3 行。
- 每行第一列是 40 个十六进制字符，`hash 匹配` 全部为 `True`。
- workspace 下有 `work_index_list.csv.zstd`、`done_flags/`、`worker_locks/` 三个条目，后两者为空目录。
- 重复 populate 后索引仍是 3 行——路径级去重生效。

**预期结果**：索引原文形如（哈希值以实际输出为准，待本地验证）：

```text
a1b2...,/fake/docs/paper_0.pdf,/fake/docs/paper_1.pdf
c3d4...,/fake/docs/paper_2.pdf,/fake/docs/paper_3.pdf
e5f6...,/fake/docs/paper_4.pdf
```

#### 4.2.5 小练习与答案

**练习 1**：把上一节的脚本里 `paths` 的顺序打乱（比如倒序传入），组哈希会变吗？分组会变吗？

**答案**：都不会。入参先经过 `set` 去重，`new_paths` 再经过 `sorted`，切组与哈希都作用在排序后的序列上——这正是为了消除调用方顺序的影响。

**练习 2**：若某路径是 `s3://bucket/dir/a,b.pdf`（含逗号），索引行会坏掉吗？

**答案**：不会。`_encode_csv_row` 的 `QUOTE_MINIMAL` 会把该字段写成 `"s3://bucket/dir/a,b.pdf"`（带双引号），`_decode_csv_row` 读回时自动剥引号还原。这也是为什么索引解析绝不能用简单的 `line.split(",")`。

**练习 3**：第一次用 `items_per_group=2` 入队 5 个路径，第二次用 `items_per_group=3` 再传同样 5 个路径，索引最终有几行？

**答案**：仍是 3 行。第二次传入的 5 个路径全部命中 `existing_path_set`，`new_paths` 为空，函数在 [L139-L140](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/work_queue.py#L139-L140) 直接返回，索引原样不动——已有分组一经写入即冻结。

### 4.3 initialize_queue 与 get_work：洗牌、三级跳过与锁

#### 4.3.1 概念说明

消费侧要回答三个问题：

**一、启动时队列从哪来？** `initialize_queue` 把索引重建为内存队列：剔除已有 done flag 的组，剩余的 `random.shuffle` 打乱后灌进 `asyncio.Queue`。为什么要洗牌？两个理由：(1) **负载均衡**——索引按路径排序切组，相邻路径往往同源同长短，不洗牌的话耗时大户可能聚堆，多个 worker 快慢悬殊；(2) **多机错峰**——若两台机器都从头按序消费，会在同样的项目上反复撞锁；随机起点让它们自然散开。

**二、领任务时要检查什么？** 内存队列是启动时刻的快照，别的 worker/机器此后可能已经完成了其中某些项。所以 `get_work` 每弹出一项要做**三级跳过检查**：

1. 本地 `_completed_hash_cache` 命中 → 跳过（纯内存，零后端 IO）；
2. 后端 `is_completed(hash)` → 跳过（一次后端查询）；
3. 后端 `is_worker_lock_taken(hash)` 锁被占且未过期 → 跳过（别人正在做）。

三级全过 → `create_worker_lock` 抢锁，把这项返回给 worker。

**三、锁为什么会「过期」？** worker 崩溃时不会有人来放锁，锁文件会永久残留。olmOCR 的解法是**锁自带新鲜度**：锁是否有效看它的 mtime 距今是否超过 `worker_lock_timeout_secs`（默认 1800 秒）：

\[ \text{锁有效} \iff now - \mathrm{mtime}(\text{lock}) \leq 1800\,\text{s} \]

超过 30 分钟的锁视为 stale（主人大概率已死），`create_worker_lock` 直接覆盖它（见 [L210](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/work_queue.py#L210) 的注释 "Create lock (overwrites if stale)"）。这要求工作项的处理时长明显小于 30 分钟——分组大小（约 500 页）正是按这个量级校准的。

**best-effort 语义**：锁与旗的写操作失败时只 `logger.warning` 然后继续，不抛异常（LocalBackend 中尤为明确，见 [L339-L351](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/work_queue.py#L339-L351) 的注释 "duplicate work is okay"）。因为最坏结果只是重复计算，而结果落盘是覆盖写、幂等（u2-l8）；反之，为了严格锁语义而让流水线卡死才是不可接受的。

#### 4.3.2 核心流程

`initialize_queue`：

```text
1. lines = backend.load_index_lines()
2. work_queue = 解析 lines → {组哈希: [路径...]}
3. done_hashes = backend.get_completed_hashes()      # 全量已完成集合
4. remaining = work_queue 的键 - done_hashes
5. remaining_items = [WorkItem(hash=h, work_paths=...) for h in remaining]
6. random.shuffle(remaining_items)                    # 洗牌
7. 重建空 self._queue，逐项 put 进去
8. 返回 self.size
```

`get_work` 的主循环：

```text
while True:
    item = self._queue.get_nowait()          # 队列空 → 返回 None（worker 退出）
    ① item.hash ∈ 本地已完成缓存 → task_done, continue
    ② backend.is_completed(item.hash)  为真 → task_done, continue
       连续 ② 命中满 3 次 → 全量刷新本地缓存（此后走①）
    ③ backend.is_worker_lock_taken(item.hash) 为真 → task_done, continue
    ④ backend.create_worker_lock(item.hash)  # 失败仅告警并 continue
    重置连续命中计数，返回 item
```

`mark_done`：

```text
1. backend.create_done_flag(item.hash)     # 先立旗
2. backend.delete_worker_lock(item.hash)   # 后放锁
3. self._queue.task_done()                 # 销账
```

顺序很关键：**先立旗再放锁**。若反过来（先放锁后立旗），存在一个窗口——锁已删、旗未写——另一个 worker 恰好在此窗口检查会同时通过 ②③，把同一项重复领走。旗先落地，放锁瞬间该 item 已在完成集合里，任何并发检查都会被 ② 拦下。

#### 4.3.3 源码精读

- [olmocr/work_queue.py:L153-L172](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/work_queue.py#L153-L172) — `initialize_queue`：读索引、取全量 done 集合、`remaining_hashes = set(work_queue) - done_hashes` 剔除已完成、`random.shuffle` 洗牌、重建 `self._queue` 并逐项 `put`，最后记日志并返回 `self.size`。pipeline 在推理就绪前调用它，拿到 0 就直接收工（调用点见 [olmocr/pipeline.py:L1439-L1444](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L1439-L1444)）。
- [olmocr/work_queue.py:L174-L185](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/work_queue.py#L174-L185) — `get_work` 开头：定义 `REFRESH_COMPLETED_HASH_CACHE_MAX_ATTEMPTS = 3`；主循环里 `get_nowait()` 弹项，`QueueEmpty` 时返回 `None`——这正是 pipeline.py 里 worker 退出的信号（[olmocr/pipeline.py:L698-L707](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L698-L707)：`work_item is None` 则 break）。第 ① 级跳过：哈希在本地缓存直接跳过并 `task_done()`（对 `asyncio.Queue` 的未决计数销账，跳过的项也必须销）。
- [olmocr/work_queue.py:L192-L203](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/work_queue.py#L192-L203) — 第 ② 级跳过与**自适应缓存刷新**：逐项查后端 `is_completed`；每连续命中一次计数 +1，满 3 次就调用 `get_completed_hashes()` 全量拉一次 done 集合替换本地缓存并清零计数。妙处在于按需升级：队列尾部只剩零星未完成项时，前几次是廉价的后端单查，确认「已完成项很多」后一次全量同步，之后的跳过全部命中本地缓存——用一次 O(全量) 换掉后续 O(逐项) 的后端往返。
- [olmocr/work_queue.py:L205-L219](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/work_queue.py#L205-L219) — 第 ③ 级跳过与抢锁：`is_worker_lock_taken` 为真则跳过；否则 `create_worker_lock`（覆盖 stale 锁），创建失败只 `warning` 并 `continue`（放不下锁就放弃这项，绝不带着无锁状态返回）；成功后把 `refresh_completed_hash_attempt` 清零（成功领到新项说明又进入「未完成密集区」，刷新逻辑重新开始计数），返回 `work_item`。
- [olmocr/work_queue.py:L221-L230](https://github.com/allenai/olmocr/blob/f7cfe4c22098b6ec950d1c0a464eecf8d/olmocr/work_queue.py#L221-L230) — `mark_done`：先 `create_done_flag` 再 `delete_worker_lock`，最后 `task_done`。「先立旗、后放锁」消除重复领取窗口（如上分析）。
- [olmocr/work_queue.py:L323-L337](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/work_queue.py#L323-L337) — LocalBackend 的 `is_worker_lock_taken`：取锁文件 mtime（UTC），`(now - lock_mtime).total_seconds() <= worker_lock_timeout_secs` 判定有效；取 mtime 出错时**返回 False**（当作没锁，宁可重复做也不卡队列）。
- [olmocr/work_queue.py:L339-L351](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/work_queue.py#L339-L351) — LocalBackend 的 `create_worker_lock`：建空文件；失败仅告警并继续，注释明言 "duplicate work is okay"。对照 [olmocr/work_queue.py:L367-L375](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/work_queue.py#L367-L375) 的 `is_completed`：探测 done flag 的 mtime 是否存在，出错同样返回 False。两个「读」原语都朝「允许重做」的方向失败——这是贯穿整个模块的取向。
- 顺带一提 [olmocr/work_queue.py:L254-L263](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/work_queue.py#L254-L263)：LocalBackend 把阻塞文件 IO 丢进 `asyncio.to_thread` 并包 120 秒超时（`_run_with_timeout`），保证慢磁盘不会卡死事件循环——细节留到 u3-l2 展开。

worker 消费端的完整闭环在 pipeline.py：

- [olmocr/pipeline.py:L698-L707](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L698-L707) — `worker()` 的 while 循环：`get_work()` 返回 None 则退出；拿到 `work_item` 后对 `work_item.work_paths` 里的每个路径起任务（tarball 走 `process_tarball`、其余走 `process_pdf`），整组完成后才走到 [L802](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L802) 的 `mark_done(work_item)`——「整组原子完成」由此闭环。

#### 4.3.4 代码实践

**实践目标**：在 4.2 的 workspace 上继续——初始化队列、领一个任务、观察锁文件出现、`mark_done` 后观察旗立锁消，最后**重新初始化**验证断点续跑。

**操作步骤**：

1. 把下面脚本存为 `practice_consume.py`，把 `WS` 改成 4.2 实践打印出的 workspace 路径（或把两段脚本合并成一次运行）：

```python
# 示例代码：本讲实践脚本（续）
import asyncio, os
from olmocr.work_queue import LocalBackend, WorkQueue

WS = "/tmp/wq_practice_xxxxx"   # ← 换成 4.2 实践的 workspace 路径

async def main():
    wq = WorkQueue(LocalBackend(WS))          # 同一 workspace，可视为"重启后的新进程"

    n = await wq.initialize_queue()
    print("初始化后的队列长度:", n)             # 预期 3

    item = await wq.get_work()
    print("领到工作项:", item.hash[:12], "包含", len(item.work_paths), "个路径")

    locks = os.listdir(os.path.join(WS, "worker_locks"))
    print("领到后 worker_locks:", locks)       # 预期出现 1 个 worker_{hash}.lock

    await wq.mark_done(item)

    print("完成后 done_flags:", os.listdir(os.path.join(WS, "done_flags")))  # 1 个 done_{hash}.flag
    print("完成后 worker_locks:", os.listdir(os.path.join(WS, "worker_locks")))  # 空

    # 断点续跑：再来一次"重启"
    wq2 = WorkQueue(LocalBackend(WS))
    n2 = await wq2.initialize_queue()
    print("再次初始化后的队列长度:", n2)          # 预期 2（已完成 1 项被剔除）

asyncio.run(main())
```

2. 运行脚本，对照输出逐行核对。
3. 进阶一步：手动在 `worker_locks/` 下仿造一个别人的新鲜锁——复制现有锁文件改名成队列里另一组的 `worker_{hash}.lock`（哈希可从索引第一列拿到，新文件 mtime 即「现在」，锁视为有效）——再 `initialize_queue` 并反复 `get_work()` 直到返回 `None`，数一数实际领到几项、被跳过几项。

**需要观察的现象**：

- `initialize_queue` 返回 3（5 路径 ÷ 每组 2 = 3 组，尚无完成项）。
- `get_work` 返回一个 `WorkItem` 的同时，`worker_locks/` 出现对应的 `worker_{hash}.lock` 空文件——**领任务与上锁是同一次调用里原子完成的**。
- `mark_done` 后 `done_flags/` 出现 `done_{hash}.flag`，锁文件消失。
- 重新 `initialize_queue` 返回 2：done flag 把已完成项从队列里永久剔除了，这就是断点续跑的全部机制。
- 进阶级：手动造锁的那个组不会被 `get_work` 返回——被第 ③ 级跳过拦下。

**预期结果**：以上数值（3 → 领 1 → 剩 2）依据源码逻辑推得，待本地验证。注意 `get_work` 领到的是洗牌后队列中的**任意**一组，哈希值每次运行都可能不同，但「组数」不变。

#### 4.3.5 小练习与答案

**练习 1**：三级跳过检查中，哪一级不产生任何后端 IO？为什么要设计这一级？

**答案**：第 ① 级（本地 `_completed_hash_cache` 命中）纯内存。因为队列尾部往往聚集大量已被其他机器完成的项，若每一项都去后端查 `is_completed`（S3 下是一次网络请求），开销巨大；本地缓存把绝大多数跳过变成零成本，再靠「连续 3 次命中后全量刷新」保证缓存不至于过期太久。

**练习 2**：如果把 `mark_done` 改成「先 `delete_worker_lock` 再 `create_done_flag`」，会出什么问题？

**答案**：在锁已删、旗未立的窗口内，另一台机器的 `get_work` 会同时通过 ②（无 done flag）和 ③（无锁）两级检查，把同一工作项重复领走、重复计算。原顺序（先旗后锁）保证放锁的那一刻完成标记已可见，把窗口堵死。

**练习 3**：一个 worker 领了任务后进程被 kill -9，这个工作项会永远丢失吗？

**答案**：不会。锁文件残留但其 mtime 停留在领任务时刻；30 分钟（1800 秒）后任何 worker 的 `is_worker_lock_taken` 都会判定它 stale，`get_work` 跳过检查放行、`create_worker_lock` 覆盖旧锁，该组被重新处理。结果文件是覆盖写（u2-l8），重做幂等。

**练习 4**：`get_work` 里每跳过一个 item 都要调 `self._queue.task_done()`，这一行删掉会怎样？

**答案**：`asyncio.Queue` 内部维护未决计数，`get_nowait()` 使计数 +1、`task_done()` 使计数 −1。跳过的项若不销账，`await queue.join()`（如果有人调用）会永远等不到归零而挂起；本仓库当前主流程未用 `join`，但销账是使用 Queue 的正确姿势，删掉属于埋雷。

## 5. 综合实践

把本讲三个模块串成一个「**模拟两台机器协同处理**」的小实验。全部在本地完成，不需要 GPU 和真实 PDF（队列并不校验路径指向的文件存在与否——populate 阶段只做字符串登记，下载发生在 worker 阶段，见 u2-l2）。

**任务**：同一 workspace，模拟「机器 A 做了一部分后崩溃 → 机器 B 接手做完」的全过程。

```python
# 示例代码：综合实践脚本
import asyncio, os, tempfile
from olmocr.work_queue import LocalBackend, WorkQueue

async def machine_a(ws):
    """机器 A：入队 10 个路径，初始化，领 2 项做完 1 项，然后"崩溃"（不放最后一个锁）。"""
    wq = WorkQueue(LocalBackend(ws))
    await wq.populate_queue([f"/fake/docs/doc_{i}.pdf" for i in range(10)], items_per_group=2)
    n = await wq.initialize_queue()
    print(f"[A] 初始化队列: {n} 项")            # 预期 5

    item1 = await wq.get_work()
    await wq.mark_done(item1)                    # 第 1 项：正常完成

    item2 = await wq.get_work()                  # 第 2 项：领了但没做完 —— 模拟 kill -9
    print(f"[A] 崩溃前领走: {item2.hash[:12]}（锁未释放）")

async def machine_b(ws):
    """机器 B：接手。注意 item2 的锁尚新鲜，会被跳过。"""
    wq = WorkQueue(LocalBackend(ws))
    n = await wq.initialize_queue()
    print(f"[B] 接手时队列: {n} 项")             # 预期 4（A 完成的 1 项被剔除）

    done, skipped_locked = 0, 0
    while True:
        before = wq.size
        item = await wq.get_work()
        if item is None:
            break
        if wq.size == before - 1 and item.hash:  # 正常领到
            pass
        await wq.mark_done(item)
        done += 1
    # 试着统计：B 实际完成了几项？item2 是否被 B 碰过？（提示：观察 worker_locks 残留）

async def main():
    ws = tempfile.mkdtemp(prefix="wq_two_machines_")
    await machine_a(ws)
    await machine_b(ws)
    print("最终 done_flags 数量:", len(os.listdir(os.path.join(ws, "done_flags"))))

asyncio.run(main())
```

**要回答的问题**：

1. 机器 B 接手时队列为什么是 4 项而不是 3 项？（A 完成的 1 项被 done flag 剔除；A 崩溃时锁未放的 item2 **仍在队列里**，只是被锁挡住。）
2. 机器 B 的 `get_work` 对 item2 做了什么？（第 ③ 级跳过：锁的 mtime 距今不足 1800 秒，判定有主，跳过。）
3. 运行结束后 `worker_locks/` 里还剩几个文件？它属于谁？（剩 1 个，即 A 崩溃时留下的 item2 的锁；30 分钟后它才会被判定 stale 而可被覆盖。）
4. 把 `machine_a` 里领 item2 与崩溃之间的等待拉长到超过 `worker_lock_timeout_secs`（不必真等 30 分钟——可以临时给 `get_work` 传一个很小的 `worker_lock_timeout_secs=0` 试试），B 还会跳过 item2 吗？（不会，锁立即 stale，B 会覆盖锁并接手。）

**预期结果**：最终 `done_flags` 数量为 4（A 做 1 项 + B 做 3 项，item2 被锁保护无人重做）；改为 0 超时后应为 5。以上依据源码逻辑推得，待本地验证。

## 6. 本讲小结

- **三层抽象**：`WorkItem`（hash + 路径组的最小任务单元）、`Backend`（索引读写/完成标记/锁共八个原语的存储接口）、`WorkQueue`（去重分组、跳过、加锁的调度语义）——换存储后端不动调度，换调度不碰存储。
- **populate_queue 是幂等的路径级合并**：set 去重 → 读旧索引求差集 → `sorted`（确定性切组）→ 按 `items_per_group` 切段 → 每段 `sorted` 后 sha1 得组哈希 → CSV（QUOTE_MINIMAL 转义逗号）+zstd 整文件覆盖写；已有分组一经写入即冻结。
- **组哈希是协调的钥匙**：排序 + utf-8 + sha1 保证跨机确定性，done flag、worker lock、结果文件名全部内容寻址到它。
- **initialize_queue = 重建内存视图**：从索引剔除 done 集合后 `random.shuffle` 灌入 `asyncio.Queue`——洗牌为了负载均衡与多机错峰；内存队列崩了就丢，磁盘事实（索引 + flags）才是持久状态。
- **get_work 的三级跳过 + mark_done 的先旗后锁**：本地缓存（零 IO）→ 逐项查 done（连续 3 次命中则全量刷新缓存）→ 查锁新鲜度（mtime 距今 ≤ 1800 秒算有主）；完成时先写 done flag 再删锁，堵死重复领取窗口。
- **best-effort 取向**：锁与旗操作失败一律告警继续、读操作失败一律返回「未完成/未上锁」——宁可重复计算（结果覆盖写幂等），绝不卡死流水线。

## 7. 下一步学习建议

本讲只点了 `LocalBackend` 的锁实现细节，并把 `S3Backend` 当黑盒。下一讲 **u3-l2《LocalBackend 与 S3Backend：同一队列的两种落地》**会逐方法对比两个后端：本地文件操作如何经 `asyncio.to_thread` + 120 秒超时进入事件循环，S3 如何用 `head_object`/`put_object`/`delete_object` 实现同一套语义，以及 `parse_s3_path`、`expand_s3_glob`、`download_zstd_csv` 等 s3_utils 函数（u4-l3 还会再深入）。建议先自己动手：把 [work_queue.py:L392-L473](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/work_queue.py#L392-L473) 的 `S3Backend` 与 LocalBackend 并排读一遍，列一张「同一方法、两种落地」的对照表，再去听下一讲核对。
