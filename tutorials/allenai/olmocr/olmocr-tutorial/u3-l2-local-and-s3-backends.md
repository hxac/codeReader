# LocalBackend 与 S3Backend：同一队列的两种落地

## 1. 本讲目标

上一讲（u3-l1）我们抽象地看了 `WorkItem`、`Backend` 接口和 `WorkQueue` 的调度语义。本讲往下钻一层，回答三个问题：

1. **同一个队列语义，如何落在两种完全不同的存储上？**——`LocalBackend` 用本地文件（或 NFS 网络文件系统）实现，`S3Backend` 用 S3 对象存储实现。我们会逐个方法对照它们的实现映射。
2. **worker lock 的超时判定为什么能防死锁？**——一个 worker 崩溃后锁会残留，为什么整个流水线不会永远卡在这个 work item 上？
3. **为什么这些锁操作全是 best-effort（尽力而为）？**——失败时只打一条 warning 就继续走，为什么这是正确的设计，而不是偷懒？

学完本讲，你应该能独立读懂 [olmocr/work_queue.py](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/work_queue.py) 的后半部分（两个后端类，约 230 行），并能在不启动 GPU 的情况下，亲手实验锁的生命周期与超时行为。

## 2. 前置知识

### 2.1 「文件存在性」就是一种状态存储

传统的锁（比如数据库的行锁）依赖一个中心服务来裁决。但 olmOCR 的队列只需要两种二进制状态：

- **这个 work item 做完了吗？**（done flag）
- **有 worker 正在做它吗？**（worker lock）

这两种状态都可以用一个更原始的问题表达：**「某个路径上有没有一个东西？」**

- 本地文件系统：这个路径下有没有这个文件？
- S3：这个 key 下有没有这个对象？

只要「存在性查询」和「创建/删除」这两种操作可用，就能搭出完整的队列状态机。这是本讲最重要的一种思维转换。

### 2.2 mtime 与 LastModified：文件/对象的「出生时间」

- 本地文件有 **mtime**（modification time，最后修改时间），Python 用 `os.path.getmtime(path)` 读取，返回一个 Unix 时间戳（浮点秒数）。
- S3 对象有 **LastModified** 属性，用 `head_object` 请求获取，boto3 会把它解析成一个带时区信息的 `datetime` 对象。

锁的超时判定就建立在这两者之上：**「一个锁是不是新鲜」=「它的创建时间距今是否还在阈值内」**。

### 2.3 时区感知（timezone-aware）的 datetime

Python 的 `datetime` 分两种：

- **naive**（无知觉）：不带时区信息，比如 `datetime.now()`。
- **aware**（有知觉）：带时区信息，比如 `datetime.now(timezone.utc)`。

两者**相减会直接抛 `TypeError`**。S3 的 `LastModified` 天生是 aware 的（UTC），所以本地实现必须手动把 mtime 转换成 aware 的 UTC 时间，两种后端才能用同一个公式比较。这个细节稍后在源码里会看到。

### 2.4 asyncio.to_thread 与超时包装

`Backend` 的所有方法都是 `async` 的，但底层操作（读写文件、发 HTTP 请求）是阻塞的。`asyncio.to_thread(func)` 把阻塞函数扔进线程池，避免卡死事件循环。`asyncio.wait_for(coro, timeout=t)` 再包一层超时。`LocalBackend` 把这两个组合成了一个带 120 秒超时的包装器，专门防御网络文件系统（NFS）挂起。

### 2.5 S3 对象存储的三条关键特性

如果你没用过 S3，记住这三条就够读懂本讲：

1. **没有真正的目录**：`s3://bucket/a/b/c.txt` 里的「目录」只是 key 名字里的前缀。所谓「建目录」就是把 key 写得长一点。
2. **单对象操作是原子的**：一个 `put_object` 要么完整成功、要么失败，不存在写了一半的对象；且成功后全球可见。
3. **delete 是幂等的**：删除一个不存在的 key 也返回成功（204），不报错。这和本地文件系统「删除不存在的文件抛 `FileNotFoundError`」不同。

### 2.6 谁在什么时候选哪个后端

在 [olmocr/pipeline.py:1329-1332](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L1329-L1332) 中，选择逻辑只有一行判断：workspace 路径以 `s3://` 开头就用 `S3Backend`，否则用 `LocalBackend`。也就是说，**同一条 `olmocr <workspace> ...` 命令，workspace 换成 S3 路径，队列就自动变成多机共享队列**——这是整个设计最优雅的地方。

```python
if args.workspace.startswith("s3://"):
    work_queue = WorkQueue(S3Backend(workspace_s3, args.workspace))
else:
    work_queue = WorkQueue(LocalBackend(args.workspace))
```

## 3. 本讲源码地图

| 文件 | 本讲涉及范围 | 作用 |
| --- | --- | --- |
| [olmocr/work_queue.py](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/work_queue.py) | L238-L389（LocalBackend）、L392-L473（S3Backend）、L26-L27（共享目录名常量） | 本讲主角：两个后端实现 |
| [olmocr/s3_utils.py](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/s3_utils.py) | L26-L33（parse_s3_path）、L36-L84（expand_s3_glob）、L163-L184（zstd CSV 读写） | S3Backend 复用的工具函数 |
| [olmocr/pipeline.py](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py) | L1329-L1332、L701 | 后端选择点与 get_work 调用点 |

两种后端共享两个目录名常量，本地是真实目录、S3 是 key 前缀，这正是「同一语义、两种落地」的第一个例子：[olmocr/work_queue.py:26-27](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/work_queue.py#L26-L27) 定义了 `worker_locks` 与 `done_flags` 两个名字，本地的 `workspace/worker_locks/worker_xxx.lock` 对应 S3 的 `s3://.../workspace/worker_locks/worker_xxx.lock`，路径形态完全一致，只是「存的东西」从 inode 变成了对象。

## 4. 核心概念与源码讲解

本讲的三个最小模块：**LocalBackend**、**S3Backend**、**锁超时与 best-effort 策略**。

回忆上一讲的 `Backend` 抽象基类，它规定了八个异步原语：[olmocr/work_queue.py:38-79](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/work_queue.py#L38-L79)。本讲的任务就是把八个原语的签名逐一填上两种实现。

### 4.1 模块一：LocalBackend——本地文件系统后端

#### 4.1.1 概念说明

`LocalBackend` 把 workspace 当成一个**用文件存在性做数据库的键值存储**：

| 逻辑状态 | 物理载体 |
| --- | --- |
| 队列索引 | `<workspace>/work_index_list.csv.zstd`（一个 zstd 压缩的 CSV 文件） |
| 「组哈希 h 已完成」 | `<workspace>/done_flags/done_h.flag`（一个**空文件**，内容不重要，存在即完成） |
| 「组哈希 h 正在被某 worker 处理」 | `<workspace>/worker_locks/worker_h.lock`（同样是空文件，**创建时间**才重要） |

注意一个精妙的细节：flag 和 lock 都是空文件，但语义完全不同——

- **done flag 是纯存在性语义**：文件在就是完成了，永远不删除，mtime 无意义。
- **worker lock 是存在性 + 时间语义**：文件在**且足够新**才算被持有；时间就是它的生命线。

`LocalBackend` 主要面向两种场景：单机批处理（workspace 是本地磁盘），以及 Beaker 集群上挂载 NFS/网络文件系统的多机协同（这也是它需要超时包装的原因——NFS 会挂起）。

#### 4.1.2 核心流程

`LocalBackend` 的生命周期：

```text
__init__
  ├── 把 workspace 转绝对路径
  ├── 立即创建 workspace/、done_flags/、worker_locks/ 三个目录（幂等）
  └── 预拼好三个路径：索引文件、done flag 目录、lock 目录

每次操作（以 create_worker_lock 为例）
  ├── 定义一个阻塞的内部函数 _create（open(lock_path, "wb") 建空文件）
  └── await _run_with_timeout(_create)
        ├── asyncio.to_thread(_create)     # 扔进线程池，不阻塞事件循环
        └── asyncio.wait_for(..., 120s)    # 超时则抛 TimeoutError
```

#### 4.1.3 源码精读

**构造函数：先搭好目录骨架。** [olmocr/work_queue.py:238-252](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/work_queue.py#L238-L252) 中 `__init__` 用 `os.makedirs(..., exist_ok=True)` 一次性建齐三个目录——这就是为什么你在第 1 单元第一次运行 `olmocr` 后，localworkspace 里立刻多出两个空目录。类属性 `DEFAULT_FS_TIMEOUT = 120` 是所有文件操作的统一超时上限。

**超时包装器：防 NFS 挂起的核心机关。** [olmocr/work_queue.py:254-263](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/work_queue.py#L254-L263)：

```python
result = await asyncio.wait_for(asyncio.to_thread(func, *args), timeout=LocalBackend.DEFAULT_FS_TIMEOUT)
```

这一行做两层防护：`to_thread` 保证阻塞的系统调用不冻结 asyncio 事件循环（否则整个进程的所有协程都会停摆）；`wait_for` 保证即使底层文件系统（典型如 NFS 服务器失联）永久挂起，120 秒后也会以 `TimeoutError` 失败，把控制权交还。注意异常处理里把 `asyncio.TimeoutError` 重新抛成内建的 `TimeoutError` 并附带操作名，方便日志定位是哪个操作挂了。

**值得注意的不对称：索引操作没有超时包装。** 对比 [olmocr/work_queue.py:291-303](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/work_queue.py#L291-L303) 可以看到，`load_index_lines`、`save_index_lines`、`get_completed_hashes` 只用了裸 `asyncio.to_thread`，而锁与标记相关操作（后面会读）全走 `_run_with_timeout`。从代码结构可以推断其取舍：索引读写只在启动阶段执行一次，挂起的代价可控；而锁判定在 `get_work` 的热路径上**每个 work item 都要调用**，NFS 抖动会在高频路径上被急剧放大，所以只有它们值得加超时。

**索引的本地读写：文件不存在等于空队列。** [olmocr/work_queue.py:265-289](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/work_queue.py#L265-L289) 中 `_download_zstd_csv_local` 的第一行判断 `if not os.path.exists(local_path): return []`——全新 workspace 没有索引文件，语义就是「队列还是空的」，而不是报错。写入侧 `_upload_zstd_csv_local` 则是整文件覆盖：先压缩全部行再一次性 `open(path, "wb")` 写出，不追加、不部分写，这是「分组一经写入即冻结」语义的物理保障（上一讲讲过）。

**get_completed_hashes：一次目录列举。** [olmocr/work_queue.py:297-303](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/work_queue.py#L297-L303) 用 `os.listdir` 列出 done_flags 目录，再用字符串切片剥掉 `done_` 前缀和 `.flag` 后缀，还原出组哈希集合。这里没有数据库、没有正则，就是文件名即数据。

**mtime 读取：时区转换的关键一步。** [olmocr/work_queue.py:313-321](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/work_queue.py#L313-L321)：

```python
return datetime.datetime.fromtimestamp(os.path.getmtime(path), datetime.timezone.utc)
```

`os.path.getmtime` 返回的是无时区的 Unix 时间戳，`fromtimestamp(ts, timezone.utc)` 的第二个参数把它显式锚定到 UTC，产出 aware datetime。文件不存在时返回 `None`——「没有 mtime」统一表达「文件/对象不存在」。这个函数取名 `_get_object_mtime`（object 而非 file）是有意为之：S3Backend 有一模一样签名的同名方法（返回 S3 的 `LastModified`），两者 interchangeable，后面的 `is_completed` / `is_worker_lock_taken` 只依赖这个抽象。

#### 4.1.4 代码实践

**实践 A：观察 LocalBackend 自动搭建的骨架（约 3 分钟）。**

1. **实践目标**：验证 `__init__` 的目录创建行为，为后续实验准备好环境。
2. **操作步骤**：

   先补两个小依赖（`olmocr.work_queue` 会间接导入 `olmocr.s3_utils`，而后者的顶层 `from google.cloud import storage` 与 `from tqdm import tqdm` 这两个包**没有**在 pyproject.toml 的任何依赖组里声明，属于依赖声明的遗漏，轻量安装下可能缺失；它们都是纯 Python 小包，装上不影响轻量性）：

   ```bash
   pip install tqdm google-cloud-storage
   ```

   然后运行（示例代码）：

   ```python
   # practice_a_local_skeleton.py
   import asyncio
   from olmocr.work_queue import LocalBackend

   async def main():
       backend = LocalBackend("./localbackend_demo")
       print("workspace 骨架已就绪:", backend.workspace_path)
       print("索引路径:", backend._index_path)

   asyncio.run(main())
   ```

3. **需要观察的现象**：脚本运行前后各执行一次 `ls ./localbackend_demo`，第一次应看到 `done_flags/` 与 `worker_locks/` 两个空目录已经出现，且 `work_index_list.csv.zstd` 并**不**存在（索引只在 `populate_queue` 有新路径时才写）。
4. **预期结果**：目录结构被幂等创建；重复运行脚本不报错、不破坏已有内容。
5. 若你的环境中 `from olmocr.work_queue import ...` 报 `ModuleNotFoundError`，按上面先补装 `tqdm` 与 `google-cloud-storage`；仍失败则检查是否在源码仓库根目录运行（可用 `PYTHONPATH=.` 兜底）。

#### 4.1.5 小练习与答案

**练习 1**：`done_h.flag` 和 `worker_h.lock` 都是空文件，为什么一个永远不删、另一个完成时必须删？

**答案**：done flag 表达的是**终态事实**（「这组工作已完成」），事实不会过期，留着供下次启动时 `initialize_queue` 剔除、`get_work` 跳过；worker lock 表达的是**进行时状态**（「有人正在做」），工作完成后状态就不该继续成立，若不删除，其他 worker 会在超时窗口内一直跳过这个 item，造成无谓的等待（虽然超时后仍会被抢占，但白白浪费了至多 30 分钟的并行度）。`mark_done` 的顺序是先写 done flag 再删锁（[olmocr/work_queue.py:221-230](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/work_queue.py#L221-L230)），保证任何中间时刻被观察到，最坏情况是「已完成 + 还锁着」（会被超时机制自愈），绝不会是「没完成却标记完成」。

**练习 2**：如果把 `_run_with_timeout` 的 120 秒改成 1 秒，哪类真实场景会最先受影响？

**答案**：NFS/网络文件系统上的大索引文件读写。`save_index_lines` 写 `work_index_list.csv.zstd`（百万级 PDF 的索引可达几十 MB 压缩数据）在慢网络盘上可能超过 1 秒。但注意：索引读写其实没走 `_run_with_timeout`（见 4.1.3 的不对称分析），所以最先受影响的是**锁与标记操作**——NFS 元数据操作（stat/创建空文件）偶尔超过 1 秒时，`is_worker_lock_taken` 会大量走进超时分支返回 False，导致锁形同虚设、重复计算激增。

### 4.2 模块二：S3Backend——对象存储后端

#### 4.2.1 概念说明

`S3Backend` 回答的问题是：**如果没有共享文件系统，只有 S3 桶，能不能跑同样的队列？**答案是能，因为 S3 恰好提供了等价的三种能力：

| 需要的能力 | 本地实现 | S3 实现 |
| --- | --- | --- |
| 探测存在性与时间 | `os.path.getmtime` | `head_object`（404 = 不存在，成功则返回 `LastModified`） |
| 创建标记/锁 | `open(path, "wb")` 建空文件 | `put_object(Body=b"")` 写空对象 |
| 删除锁 | `os.remove` | `delete_object`（幂等，不存在也不报错） |

S3 的优势是**全球可见 + 高可用**：N 个 Beaker 副本各自创建 boto3 client 指向同一个桶，锁一写大家立刻可见，天然构成分布式队列；代价是每次状态查询都是一次网络往返（毫秒级延迟 vs 本地的微秒级），所以 `WorkQueue` 才在 `get_work` 里设计了本地完成缓存与「连续 3 次命中才全量刷新」的节流（上一讲内容）。

#### 4.2.2 核心流程

S3Backend 的调用拓扑（它比 LocalBackend 更薄，因为重活委托给了 `s3_utils`）：

```text
S3Backend
  ├── 索引读写 ──委托──> s3_utils.download_zstd_csv / upload_zstd_csv
  │                        └── 底层是 get_object / put_object
  ├── get_completed_hashes ──委托──> s3_utils.expand_s3_glob(".../done_flags/*.flag")
  │                        └── 底层是 list_objects_v2 分页 + fnmatch
  └── 锁与标记 ──直接调 boto3──> head_object / put_object / delete_object
```

#### 4.2.3 源码精读

**构造函数：只拼路径，不碰远端。** [olmocr/work_queue.py:392-399](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/work_queue.py#L392-L399) 的 `__init__` 接收一个已建好的 `s3_client`（由 pipeline.py 里 boto3 Session 创建，见 [olmocr/pipeline.py:1318-1319](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L1318-L1319)，支持 `--workspace_profile` 指定凭证 profile），然后 `rstrip("/")` 去掉路径尾部斜杠，拼出索引 key 与 done flag 的 glob 模式。与 LocalBackend 不同，它**没有任何 makedirs**——S3 没有目录，key 直接写就是了。这也意味着 S3Backend 构造时**不会发起任何网络请求**，纯字符串准备。

**索引读写：完全委托给 s3_utils。** [olmocr/work_queue.py:401-405](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/work_queue.py#L401-L405) 一行一个：

```python
async def load_index_lines(self) -> List[str]:
    return await asyncio.to_thread(download_zstd_csv, self.s3_client, self._index_path)
```

追进 [olmocr/s3_utils.py:163-175](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/s3_utils.py#L163-L175)：`download_zstd_csv` 下载字节 → zstd 解压 → 按行拆分；捕获 `NoSuchKey` 异常并返回空列表，注释写得很直白——「No existing ... found in s3, starting fresh」，与本地版「文件不存在返回 []」语义对齐。上传侧 [olmocr/s3_utils.py:178-184](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/s3_utils.py#L178-L184) 则是行拼接 → 压缩 → `put_s3_bytes`（[olmocr/s3_utils.py:151-154](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/s3_utils.py#L151-L154)，底层 `put_object`）整对象覆盖。S3 的 `put_object` 是原子替换，旧读新旧读新都不会看到半个文件，天然满足「索引整文件覆盖」的要求。

**get_completed_hashes：一次 glob 列举。** [olmocr/work_queue.py:407-416](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/work_queue.py#L407-L416) 把 `done_flags/*.flag` 交给 `expand_s3_glob`。看 [olmocr/s3_utils.py:36-63](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/s3_utils.py#L36-L63) 的实现：先定位第一个通配符，把它之前的固定串当 `Prefix` 调 `list_objects_v2` 分页列举（S3 的 Prefix 是前缀过滤，不做模式匹配），再在客户端用 `glob.fnmatch` 精筛。这对应本地版的 `os.listdir` + 前后缀过滤。当 done flag 多达几十万个时，这是 S3 后端启动阶段最贵的一次调用（要翻页拉完整个前缀），也是 `get_work` 想尽办法缓存它的原因。

**mtime 读取：head_object 与 404 的哲学。** [olmocr/work_queue.py:426-439](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/work_queue.py#L426-L439)：

```python
response = self.s3_client.head_object(Bucket=bucket, Key=key)
return response["LastModified"]
...
if e.response["Error"]["Code"] == "404":
    return None
```

`head_object` 只取元数据不取 body（等价于本地 `stat` 而非 `read`），不存在的对象抛 `ClientError` 且 Code 为 `"404"`，代码把它翻译成 `None`——与本地版 `_get_object_mtime` 的返回约定严丝合缝。注意 `parse_s3_path`（[olmocr/s3_utils.py:26-33](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/s3_utils.py#L26-L33)）把 `s3://bucket/key` 拆成 `(bucket, key)`，还兼容 `gs://` 与 `weka://` 前缀。其他非 404 的错误（网络故障、权限）原样上抛。

**锁三件套：put / delete 的极简。** [olmocr/work_queue.py:452-473](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/work_queue.py#L452-L473)：

- `create_worker_lock` / `create_done_flag`：`put_object(Bucket, Key, Body=b"")`——写一个零字节对象。S3 的 put 对已存在的 key 是**覆盖**（同时刷新 `LastModified`），这正好实现了 `WorkQueue.get_work` 里那句注释「Create lock (overwrites if stale)」（[olmocr/work_queue.py:210](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/work_queue.py#L210)）：抢占 stale lock 时不需要先删再建，一次 put 覆盖即完成。
- `delete_worker_lock`：`delete_object`。S3 删除不存在的对象也成功，天然幂等，不像本地版要先 `os.path.exists` 再删（[olmocr/work_queue.py:357-359](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/work_queue.py#L357-L359)）。

**留一个观察点：S3Backend 没有异常包装。** 对比两个类会发现，LocalBackend 的每个锁方法都裹着 try/except（下一模块细讲），而 S3Backend 的方法除 404 处理外**不捕获任何异常**，网络错误会直接上抛给 `WorkQueue.get_work`。`get_work` 只对 `create_worker_lock` 有兜底 try/except（[olmocr/work_queue.py:210-216](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/work_queue.py#L210-L216)），`is_completed` / `is_worker_lock_taken` 若抛异常会沿 `get_work` → `worker()` 一路上传导致该 worker 退出。这是一处真实的行为差异：本地 NFS 的偶发挂起被 120 秒超时 + best-effort 吸收，而 S3 的偶发网络错误更多依赖 boto3 内建的重试机制（构造 client 时未显式配置时 botocore 默认重试数次）。

#### 4.2.4 代码实践

**实践 B：用替身客户端离线验证 S3Backend 的锁生命周期（约 10 分钟）。**

1. **实践目标**：不看 AWS 账单、不要凭证，验证 `S3Backend` 在 `head_object`/`put_object`/`delete_object` 三个调用上的行为，尤其是 404 → `None` 的翻译。
2. **操作步骤**（示例代码）：

   ```python
   # practice_b_s3_stub.py
   import asyncio, datetime
   from types import SimpleNamespace
   from botocore.exceptions import ClientError
   from olmocr.work_queue import S3Backend

   class FakeS3Client:
       """最小替身：只实现 S3Backend 用到的三个调用，对象存在内存 dict 里。"""
       def __init__(self):
           self._objects = {}  # key -> LastModified
           self.exceptions = SimpleNamespace(ClientError=ClientError)

       def head_object(self, Bucket, Key):
           if Key not in self._objects:
               raise ClientError({"Error": {"Code": "404", "Message": "Not Found"}}, "HeadObject")
           return {"LastModified": self._objects[Key]}

       def put_object(self, Bucket, Key, Body=b""):
           self._objects[Key] = datetime.datetime.now(datetime.timezone.utc)
           return {}

       def delete_object(self, Bucket, Key):
           self._objects.pop(Key, None)  # 幂等：不存在也不报错
           return {}

   async def main():
       backend = S3Backend(FakeS3Client(), "s3://fake-bucket/ws-demo")
       h = "b" * 40  # 模拟一个 40 位组哈希

       print("锁路径:", backend._get_worker_lock_path(h))
       print("1) 初始 is_worker_lock_taken:", await backend.is_worker_lock_taken(h))

       await backend.create_worker_lock(h)
       print("2) 加锁后 is_worker_lock_taken:", await backend.is_worker_lock_taken(h))

       await backend.delete_worker_lock(h)
       print("3) 删锁后 is_worker_lock_taken:", await backend.is_worker_lock_taken(h))

       await backend.create_done_flag(h)
       print("4) is_completed:", await backend.is_completed(h))

   asyncio.run(main())
   ```

3. **需要观察的现象**：四行输出依次为 `False`、`True`、`False`、`True`；同时注意 `backend._get_worker_lock_path(h)` 打印出的 key 形如 `s3://fake-bucket/ws-demo/worker_locks/worker_bbb...b.lock`——目录名常量被用作 key 前缀。
4. **预期结果**：替身客户端抛出的 `ClientError(Code="404")` 被 `_get_object_mtime` 翻译成 `None`，从而第 1、3 步判定「未持锁」。这证明了 S3Backend 只依赖 boto3 的这三个方法签名，其余全是路径拼装与时间比较。
5. 本实践不依赖网络与真实 S3；若想进一步贴近真实，可以把 FakeS3Client 换成 `moto` 库的 mock server，或换成真实 boto3 client 指向自己的测试桶（需要凭证，属于可选）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `S3Backend.__init__` 不需要（也不能）做 LocalBackend 那样的 makedirs？

**答案**：S3 没有目录概念，`worker_locks/`、`done_flags/` 只是 key 的字符串前缀，写第一个 `put_object(".../worker_locks/worker_x.lock")` 时「目录」就自动存在了；反过来，一个前缀下没有对象时，`list_objects_v2` 自然返回空，也不需要预先占位。LocalBackend 的 makedirs 是因为 POSIX 的 `open()` 不能写进不存在的目录。

**练习 2**：`expand_s3_glob` 为什么要「先取第一个通配符之前的固定前缀做 Prefix 查询，再在客户端 fnmatch 精筛」，而不是把整段模式交给服务端？

**答案**：S3 的 `list_objects_v2` 只有 `Prefix`（字符串前缀）参数，不支持 `*` 这类模式匹配；`*`、`[`、`]` 在 key 里是合法字符而非通配符。所以只能服务端做最粗的前缀收敛（减少传输量），客户端用 `glob.fnmatch` 做完整模式匹配。这个两段式是所有 S3 glob 工具的通用做法。

### 4.3 模块三：锁超时与 best-effort 策略

#### 4.3.1 概念说明

这个模块回答本讲开头的两个「为什么」。

**为什么锁需要超时（stale lock 问题）？**

设想 Beaker 上有 8 个 worker 共享一个队列。worker A 对组哈希 `h` 调用了 `create_worker_lock(h)`，随后所在机器被抢占式回收（preemptible 节点很常见）——进程没了，锁文件还在，而且**永远不会有人来删它**。如果锁的有效性只看存在性，`h` 这个 work item 就会被所有后续 worker 永远跳过：它没 done，又永远 locked，成为「孤儿」。当这类孤儿积累起来，队列会莫名「漏算」。

解法是给锁加上时间维度：**锁的效力 = 存在性 × 新鲜度**。默认阈值 1800 秒（30 分钟），定义在 `Backend.is_worker_lock_taken` 的签名默认值上（[olmocr/work_queue.py:61-64](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/work_queue.py#L61-L64)），pipeline 调用 `get_work()` 时不传参（[olmocr/pipeline.py:701](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L701)），所以生产中就是 30 分钟。写成公式：

\[ \text{锁被有效持有} \iff t_{\text{now}} - t_{\text{lock}} \le T_{\text{timeout}}, \quad T_{\text{timeout}} = 1800\,\text{s} \]

注意隐含假设：**锁持有者不会续租（no heartbeat）**。锁的 mtime 只在创建（或被抢占覆盖）时写一次，之后 30 分钟是「信任额度」。这意味着一个真跑超过 30 分钟的 work item 会被别的 worker 抢走重算——默认 `pages_per_group=500`（本地 GPU 形态）时一组页数不小，这个额度是否宽裕取决于吞吐，属于工程上的经验折中：宁可偶发重复计算，也不要孤儿 item。

**为什么锁操作是 best-effort（宁重复、勿卡死）？**

先看「重复计算」到底有多可怕。回顾上一讲的输出契约：worker 把结果写 `output_{hash}.jsonl` 时先写临时文件再整体覆盖，done flag 只在全部落盘后写。两个 worker 同时算同一组，结果字节级一致（同输入同模型，temperature 再高也各自完整），后写者覆盖先写者，最终状态不变——**重复计算是幂等且安全的**，代价只是一点 GPU 时间。

再看反面：如果 `create_worker_lock` 失败就让 worker 崩溃，一次 NFS 抖动就能放倒一个 worker；如果 `is_worker_lock_taken` 失败就保守地返回 True（「不确定就当它被锁着」），一个查询错误就会让 item 被跳过一整轮。所以每个失败分支都被刻意导向「假设没有障碍、放行去做」：

- 查询失败（`is_completed` / `is_worker_lock_taken` 出错）→ 返回 `False` → 当作「没完成 / 没锁」→ 可能重复计算；
- 写失败（`create_worker_lock` / `create_done_flag` / `delete_worker_lock` 出错）→ 打 warning 继续 → 可能与其他 worker 撞车 → 还是重复计算。

所有失败路径殊途同归于「重复计算」，没有一条通向「永久等待」或「进程崩溃」。这就是 best-effort 的完整含义：**用可接受的浪费换取不可接受的卡死的消除**。

#### 4.3.2 核心流程

把三个模块的错误处理决策汇总成一张表：

| 操作 | 出错时 LocalBackend 的行为 | 对系统的后果 |
| --- | --- | --- |
| `is_completed` 超时/异常 | 返回 `False`（[L367-375](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/work_queue.py#L367-L375)） | 该 item 被重算一次 |
| `is_worker_lock_taken` 超时/异常 | 返回 `False`（[L323-337](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/work_queue.py#L323-L337)） | 可能与其他 worker 撞车重算 |
| `create_worker_lock` 超时/异常 | warning 后继续（[L339-351](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/work_queue.py#L339-L351)） | 同上，且本 worker 无锁保护 |
| `delete_worker_lock` 超时/异常 | warning 后继续（[L353-365](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/work_queue.py#L353-L365)） | 锁残留，30 分钟后自然过期 |
| `create_done_flag` 超时/异常 | warning 后继续（[L377-389](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/work_queue.py#L377-L389)） | 结果已落盘，仅下次重算 |

超时判定的完整时序（两个后端逻辑相同）：

```text
get_work 领到 item（组哈希 h）
  ├── is_worker_lock_taken(h)
  │     ├── mtime 查不到（无文件 / 404）        → False，无锁
  │     └── (now_utc - mtime).total_seconds()
  │            ├── <= 1800                        → True，别的 worker 在做，跳过
  │            └── >  1800（stale，持有者可能死了）→ False，可抢占
  └── create_worker_lock(h)                       → 覆盖写，mtime 重置为 now
```

#### 4.3.3 源码精读

**两个后端共享同一条超时判式。** 本地版 [olmocr/work_queue.py:323-337](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/work_queue.py#L323-L337) 与 S3 版 [olmocr/work_queue.py:441-450](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/work_queue.py#L441-L450) 的判定核心一字不差：

```python
now = datetime.datetime.now(datetime.timezone.utc)
return (now - lock_mtime).total_seconds() <= worker_lock_timeout_secs
```

两处能共用，前提正是 4.1.3 里 `_get_object_mtime` 做的时区归一：本地 mtime 被 `fromtimestamp(ts, timezone.utc)` 锚定为 aware UTC，S3 的 `LastModified` 本身就是 aware UTC，于是与 `now(timezone.utc)` 相减不会抛 `TypeError`。这是一个跨后端对齐数据表示的典型小案例。

**本地版的 best-effort 包装范式。** 以 `is_worker_lock_taken` 为例，[olmocr/work_queue.py:326-331](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/work_queue.py#L326-L331)：

```python
except (TimeoutError, Exception) as e:
    # On timeout or error, return False to let this worker attempt the work
    logger.warning(f"is_worker_lock_taken({work_hash}) failed: {e}, returning False")
    return False
```

注释把意图说得很明白：失败时返回 False，**放行让本 worker 尝试这项工作**。顺带一提，`(TimeoutError, Exception)` 这个元组在语义上就等于 `Exception`（前者是后者的子类），是防御式写法的冗余，读代码时按「捕获一切」理解即可。同样的三段式（try → 捕获 → warning → 放行）也出现在 `create_worker_lock`（[L347-351](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/work_queue.py#L347-L351)，注释直言 "duplicate work is okay"）、`delete_worker_lock`（[L361-365](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/work_queue.py#L361-L365)）、`is_completed`（[L372-375](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/work_queue.py#L372-L375)）和 `create_done_flag`（[L385-389](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/work_queue.py#L385-L389)，注释 "work was done, just couldn't mark it"——活干完了，只是没能立碑）。

**调用方的兜底与 S3 版的裸奔。** `WorkQueue.get_work` 自己也对 `create_worker_lock` 加了一层 try/except（[olmocr/work_queue.py:210-216](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/work_queue.py#L210-L216)），失败时 `task_done()` 后 `continue` 去取下一个 item——这层兜底对两种后端都生效，也解释了为什么 S3Backend 内部不包 try/except 也不会让 worker 当场崩溃（`create_worker_lock` 路径有保护；`is_*` 查询路径的异常则会上抛，见 4.2.3 的观察点）。

**stale lock 的自愈闭环。** 把 [olmocr/work_queue.py:205-219](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/work_queue.py#L205-L219) 连起来读：超时判定为 False → 不跳过 → 直接 `create_worker_lock` **覆盖**旧锁（本地 `open("wb")` 与 S3 `put_object` 的覆盖语义在此合流，新 mtime/LastModified 即新的 30 分钟计时）→ 返回 item 给新 worker。旧持有者若其实还活着并最终完成，`mark_done` 写 done flag 时两者写的是同一个 key，结果一致。整个机制没有一行「检测对方死活」的代码，完全靠时间过期 + 幂等覆盖达成自愈——这是分布式系统里「租约（lease）」模式的极简实现，只是租约的续租被省略了。

#### 4.3.4 代码实践

**实践 C：亲手制造锁、flag 与 stale lock（约 10 分钟）。**

1. **实践目标**：用 `os.utime` 把一个锁的 mtime 人为回拨，直观验证 1800 秒超时判定与查询方法的返回值。
2. **操作步骤**（示例代码，续用实践 A 的 `./localbackend_demo`）：

   ```python
   # practice_c_timeout.py
   import asyncio, os, time
   from olmocr.work_queue import LocalBackend

   async def main():
       backend = LocalBackend("./localbackend_demo")
       h = "a" * 40  # 模拟 40 位组哈希
       lock_path = backend._get_worker_lock_path(h)
       flag_path = backend._get_done_flag_path(h)

       # --- 第 1 幕：什么都没有 ---
       print("1) is_completed:", await backend.is_completed(h),
             "| is_worker_lock_taken:", await backend.is_worker_lock_taken(h))

       # --- 第 2 幕：手动创建 done flag 与新鲜 lock（模拟 worker A 抢到任务）---
       with open(flag_path, "wb"): pass
       with open(lock_path, "wb"): pass
       print("2) is_completed:", await backend.is_completed(h),
             "| is_worker_lock_taken:", await backend.is_worker_lock_taken(h))

       # --- 第 3 幕：把 lock 的 mtime 回拨 1 小时，制造 stale lock ---
       old = time.time() - 3600
       os.utime(lock_path, (old, old))   # (atime, mtime)
       print("3) stale(1h) is_worker_lock_taken:", await backend.is_worker_lock_taken(h),
             "| 放宽到 7200s:", await backend.is_worker_lock_taken(h, worker_lock_timeout_secs=7200))

       # --- 第 4 幕：get_completed_hashes 汇总 ---
       print("4) completed hashes:", await backend.get_completed_hashes())

       # --- 收尾：清理演示文件 ---
       os.remove(lock_path); os.remove(flag_path)

   asyncio.run(main())
   ```

3. **需要观察的现象**：
   - 第 1 幕两个方法都返回 `False`（flag/lock 不存在 → mtime 为 `None`）；
   - 第 2 幕都返回 `True`（存在且新鲜）；
   - 第 3 幕默认参数下返回 `False`（1 小时 > 30 分钟，stale 被判可抢占），但把超时放宽到 7200 秒又变回 `True`——证明阈值是判定的一部分，而非文件属性；
   - 第 4 幕集合里应含 `aaaa...a`（40 个 a）这个哈希。
4. **预期结果**：输出形如 `False False / True True / False True / {'aaa...'}`。若第 3 幕仍为 `True`，检查是否改错了文件（注意传给 `os.utime` 的元组是 `(访问时间, 修改时间)`）。
5. 本实践纯本地、无网络依赖；若想观察 best-effort 分支的日志，可在运行前设置 `logging.basicConfig(level=logging.WARNING)` 并在 `is_worker_lock_taken` 查询期间用 `chmod 000` 锁文件制造权限错误（Linux 下生效），观察 warning 日志与 `False` 返回值。

#### 4.3.5 小练习与答案

**练习 1**：如果把 `worker_lock_timeout_secs` 调到 60 秒，会在默认配置（`pages_per_group=500`）下引发什么问题？调到 7 天呢？

**答案**：调到 60 秒——一组 500 页通常跑不完 60 秒，还在干活时锁就过期了，其他 worker 会不断抢占、覆盖锁，同一组被多个 worker 反复重算，吞吐反而崩塌（「锁颠簸」）。调到 7 天——真正崩溃的 worker 留下的孤儿锁要等 7 天才能被抢占，期间这批 item 相当于丢失（除非整批重跑）。1800 秒是「一组典型处理时长」与「孤儿恢复延迟」之间的折中，改动它应该配合评估单组实际耗时。

**练习 2**：两个 worker 同时通过 `is_worker_lock_taken` 检查（都看到 False）并同时 `create_worker_lock`，会发生什么？这算 bug 吗？

**答案**：会发生「检查—创建」竞态（check-then-act race）：两个 worker 都认为自己持锁、同时处理同一组。这在严格互斥的意义上确实是失效的，但设计者判定它**不是必须修复的 bug**——因为重复处理是幂等的（结果覆盖写、done flag 后写），代价只是浪费一份算力；而要消灭这个竞态需要原子化的 test-and-set（本地可用 `O_CREAT|O_EXCL`，S3 则需要条件写入 `If-None-Match`，兼容性受限），复杂度与收益不成比例。这是 best-effort 哲学的又一处体现。

## 5. 综合实践

**任务：制作双后端方法对照表 + 模拟双 worker 抢锁。**

这个任务把本讲三个模块串起来，产出一份可留存的笔记。

**第一部分：方法对照表。** 对照源码填写下表（空格处即你的作业，括号内为提示，建议先遮住提示自行填写再核对）：

| Backend 原语 | LocalBackend 载体与调用 | S3Backend 载体与调用 |
| --- | --- | --- |
| `load_index_lines` | `work_index_list.csv.zstd`；`open`+zstd 解压，不存在→`[]` | 同名对象；`download_zstd_csv`→`get_object`，`NoSuchKey`→`[]` |
| `save_index_lines` | 整文件覆盖写（zstd 压缩后 `open(path,"wb")`） | `upload_zstd_csv`→`put_object` 原子替换 |
| `get_completed_hashes` | `os.listdir(done_flags/)`＋剥前后缀 | `expand_s3_glob(*.flag)`→`list_objects_v2` 分页＋`fnmatch` |
| `is_completed` | done flag 的 mtime 非 `None` | `head_object` 有 `LastModified` |
| `is_worker_lock_taken` | lock mtime 与 now 差 ≤ 超时值 | `LastModified` 与 now 差 ≤ 超时值 |
| `create_worker_lock` | `open(lock,"wb")` 空文件 | `put_object(Body=b"")` 覆盖写 |
| `delete_worker_lock` | 存在则 `os.remove` | `delete_object`（幂等） |
| `create_done_flag` | `open(flag,"wb")` 空文件 | `put_object(Body=b"")` |

再补三行「横切差异」：内部异常处理（本地 best-effort 包装 / S3 仅 404 处理）、超时保护（本地 `_run_with_timeout` 120s / S3 无显式包装，依赖 boto3 重试）、构造副作用（本地建三目录 / S3 零网络请求）。

**第二部分：模拟两个 worker 抢同一个 work item。**（示例代码）

```python
# practice_final_two_workers.py
import asyncio, os, time
from olmocr.work_queue import LocalBackend

async def main():
    backend = LocalBackend("./localbackend_demo")
    h = "c" * 40

    # --- worker A：走 get_work 的底层三步 ---
    print("A 检查完成:", await backend.is_completed(h))            # False
    print("A 检查锁:", await backend.is_worker_lock_taken(h))      # False
    await backend.create_worker_lock(h)                             # A 抢到

    # --- worker B：同样三步 ---
    print("B 检查锁:", await backend.is_worker_lock_taken(h))      # True → B 应跳过

    # --- 假装 A 崩溃：锁残留 1 小时 ---
    lock_path = backend._get_worker_lock_path(h)
    old = time.time() - 3600
    os.utime(lock_path, (old, old))
    print("C 检查锁(过期):", await backend.is_worker_lock_taken(h)) # False
    await backend.create_worker_lock(h)                             # C 抢占并覆盖

    # --- C 完成工作：先立 flag 再放锁（mark_done 的顺序）---
    await backend.create_done_flag(h)
    await backend.delete_worker_lock(h)
    print("事后 is_completed:", await backend.is_completed(h))      # True

    # 清理
    os.remove(backend._get_done_flag_path(h))

asyncio.run(main())
```

**预期结果与观察点**：B 的检查返回 `True`（被锁挡住）；C 的检查返回 `False`（stale 放行）；最终 `is_completed` 为 `True` 而锁文件已消失——完整重演了「抢锁 → 崩溃 → 超时抢占 → 完成收尾」的一生。把每步输出与 [olmocr/work_queue.py:205-230](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/work_queue.py#L205-L230) 的 `get_work`/`mark_done` 逐行对应，你会发现自己手写的正是它们的展开形式。

## 6. 本讲小结

- **同一语义、两种落地**：`LocalBackend` 与 `S3Backend` 实现同一组八原语，本地用「文件存在性 + mtime」，S3 用「对象存在性 + LastModified」，共享目录名常量使两种 workspace 的路径形态完全一致，pipeline 仅凭 `s3://` 前缀一行判断切换。
- **mtime 归一是桥梁**：本地把 Unix 时间戳显式锚定为 aware UTC（`fromtimestamp(ts, timezone.utc)`），与 S3 天生 aware 的 `LastModified` 对齐，两种后端得以共用同一条超时判式 `(now - mtime).total_seconds() <= worker_lock_timeout_secs`。
- **锁 = 存在性 × 新鲜度**：done flag 是永久事实，worker lock 是带 30 分钟额度的租约且不续租；worker 崩溃留下的 stale lock 会在超时后被其他 worker 以覆盖写方式抢占，全程无需检测对方死活。
- **best-effort 的本质是错误收敛**：所有锁/标记操作的失败分支（查询失败返回 False、写入失败仅告警）都收敛到「可能重复计算」，而重复计算因「结果覆盖写 + done flag 后写」是幂等安全的；没有一条失败路径通向死锁或崩溃。
- **两后端的健壮性策略不同**：LocalBackend 靠 `_run_with_timeout`（120 秒）防御 NFS 挂起且逐方法包裹异常；S3Backend 不做内部包装（仅 404→None），依赖 boto3 自身重试与 `get_work` 调用方的兜底。
- **已知的真实边界**：S3Backend 查询类方法（`is_completed`/`is_worker_lock_taken`）的异常会直接上抛，极端情况下可能导致 worker 退出；「检查—创建」之间存在竞态窗口。两者都是被接受的取舍而非疏忽。

## 7. 下一步学习建议

- **下一讲（u3-l3）**将转向 `pipeline.py` 的 vLLM 服务器生命周期管理：`vllm_server_task/host/ready` 三件套如何拼装 `vllm serve` 命令、从日志解析队列深度，与本讲的队列消费端（`get_work`）汇合成完整的 worker 视角。
- 想巩固本讲，建议通读 [olmocr/work_queue.py](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/work_queue.py) 全文一遍（约 470 行），此刻应当已无障碍；再横向对照 `s3_utils.py` 中 `get_s3_bytes_with_backoff`（[olmocr/s3_utils.py:125-148](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/s3_utils.py#L125-L148)）的指数退避，体会「重试」在不同模块中的两种姿态（数据下载重试 vs 锁操作的放弃与自愈）。
- 延伸阅读方向：分布式租约（lease）与心跳续租、S3 条件写入（`If-None-Match`）为何能但不能完全消除 check-then-act 竞态——想想如果要求你改造 `S3Backend` 消除练习 2 的竞态，你会动哪三个方法。
