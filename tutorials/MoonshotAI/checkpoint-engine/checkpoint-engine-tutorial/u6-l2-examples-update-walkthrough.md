# u6-l2 examples/update.py 完整编排解析

## 1. 本讲目标

前面各讲我们分别精读了 ParameterServer 的生命周期（u3 系列）、worker 状态机（u4-l1/u4-l2）与 HTTP API 层（u4-l5）。本讲把这些零件装回一台完整的机器：**通读仓库自带的端到端驱动脚本 `examples/update.py`**——README 中的所有性能基准都是用它测出来的（见 [README.md:56](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/README.md#L56)）。

学完本讲，你应该能够：

1. 说清楚 **rank 与 `inference_parallel_size` 如何共同决定哪个进程触发推理请求**（"实例组首"的概念）。
2. 掌握 **按文件切分（`split_checkpoint_files`）与按张量切分（`split_tensors`）** 两种负载均衡策略的原理与选择条件。
3. 会用 `--update-method`、`--custom-dist`、`--uds` 等命令行参数，并理解它们分别影响了源码中的哪个分支。
4. 能把 `update_weights` 的七步编排与 `ParameterServer` 的生命周期方法一一对上，理解 `update_method="all"` 时两次 update 之间 `time.sleep(2)` 的真实原因。

## 2. 前置知识

本讲是「编排层」讲义，不再深入任何单个模块的内部，但默认你已从前置讲义带走以下认知（忘了可以回看）：

- **torchrun 与 RANK/WORLD_SIZE**（u1-l2）：`torchrun --nproc-per-node 8 examples/update.py ...` 会拉起 8 个进程，每个进程的 `RANK` 环境变量分别是 0~7，`WORLD_SIZE` 为 8。
- **ParameterServer 生命周期**（u3 系列）：`register_checkpoint`（注册锁页）→ `gather_metas`（全局元数据收集）→ `update`（广播或 P2P 传输）→ `unregister_checkpoint`。`update` 必须在 `gather_metas` 之后调用。
- **`update` 的 ranks 参数语义**（u3-l4/u5-l6）：`ranks=None`（或空列表）走 Broadcast 三阶段流水线；`ranks` 指定具体 rank 列表则走 P2P（Mooncake RDMA 单边读）。
- **`auto_pg` 进程组自动建毁**（u1-l2/u3-l4）：`ParameterServer(auto_pg=True)` 时，每次 `update` 结束都会 `destroy_process_group`，下次集合操作前再按需重建。
- **req_func 与 `/collective_rpc`**（u4-l2/u4-l5）：PS 不直接依赖 vLLM，而是把一个 `req_func(socket_paths)` 回调传给 `ps.update`；`req_func` 负责通过 HTTP（或 UDS）调用 vLLM 的 `/collective_rpc` 端点，触发 worker 侧的 `update_weights_from_ipc`。
- **`dist` 抽象层**（u5-l2）：示例脚本 `import checkpoint_engine.distributed as dist`，所有集合通信都走模块级函数，`dist.use_backend()` 可把全局后端单例换成 vLLM NCCL/HCCL 后端。

两个本讲新引入的术语：

- **实例（instance）与实例并行度 P（`inference_parallel_size`）**：一个 vLLM 推理实例由 P 个张量并行（TP）进程组成。示例脚本假设：一个 torchrun 会话里的 rank 按 P 为一段连续分组，每一段对应一个推理实例。
- **实例组首（group leader）**：每段的第一个 rank，即满足 `rank % P == 0` 的进程。它承担"对外说话"的职责：健康检查、向推理引擎发更新请求。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [examples/update.py](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/examples/update.py) | 本讲主角：端到端驱动脚本，含参数解析、两种切分、req_inference、update_weights 编排与 join 模式入口 |
| [README.md](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/README.md) | 运行命令、基准测试说明、join 用法、环境变量 |
| [checkpoint_engine/api.py](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/api.py) | `request_inference_to_update` 的实现：`req_func` 最终发出去的那个 HTTP/UDS POST |
| [checkpoint_engine/ps.py](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py) | `ParameterServer.update` 的 `auto_pg` 建毁组逻辑（理解 sleep(2) 的关键） |
| [checkpoint_engine/distributed/base.py](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/distributed/base.py) | `dist.use_backend`：`--custom-dist` 参数的最终去向 |
| [checkpoint_engine/\_\_init\_\_.py](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/__init__.py) | 门面导出：示例脚本 `from checkpoint_engine import request_inference_to_update` 的来源 |

`examples/update.py` 自顶向下的函数布局（也是本讲四个最小模块的地图）：

```
examples/update.py
├── check_vllm_ready(L33)      ── 组首阻塞轮询 vLLM /health
├── split_checkpoint_files(L51) ── 模块②按文件均分
├── split_tensors(L60)          ── 模块③按张量均分
├── req_inference(L77)          ── 模块④组首计算 + req_func 闭包
├── update_weights(L96)         ── 模块①(核心)七步生命周期编排
├── join(L131)                  ── join 复用模式入口(u6-l3 详讲)
└── __main__(L162)              ── 参数解析与三种模式分派
```

## 4. 核心概念与源码讲解

### 4.1 入口分派：`__main__` 的参数与三种运行模式

#### 4.1.1 概念说明

`examples/update.py` 的 `__main__` 块是这个脚本的"总开关面板"：它解析命令行参数、决定本次运行走哪种模式、并在构造 `ParameterServer` **之前**完成 dist 后端切换。理解这一段，后面的所有命令行参数就都有了着落。

脚本有三种运行模式：

| 模式 | 触发条件 | 走向 |
| --- | --- | --- |
| 常规更新 | 未传 `--load-metas-file` / `--metas-url` | `split_*` 切分 → `update_weights`（本讲主线） |
| join 复用 | 传了二者之一 | `join`（u6-l3 详讲，本讲只看分岔点） |
| 退出前驻留 | `--sleep-time N` | 脚本末尾睡 N 秒，让 PS 存活着供新实例 join |

#### 4.1.2 核心流程

```
解析 argparse 参数(11 个)
    │
    ├─ rank/world_size ← 环境变量 RANK / WORLD_SIZE(torchrun 注入)
    ├─ req_func = req_inference(endpoint, P, uds)     ← 模块④,先造好回调
    ├─ dist.use_backend(args.custom_dist)             ← 必须在首次建组前!
    ├─ ps = ParameterServer(auto_pg=True)
    │
    ├─ 传了 --load-metas-file 或 --metas-url ?
    │       ├─ 是 → join(...)            # join 模式
    │       └─ 否 → 选择切分策略
    │               ├─ 目录里有 model.safetensors.index.json
    │               │   且路径不以 /dev/shm/ 开头
    │               │       → named_tensors = split_tensors(...)   # 按张量
    │               └─ 否则 → checkpoint_files = split_checkpoint_files(...)  # 按文件
    │           → update_weights(...)    # 常规模式
    └─ time.sleep(args.sleep_time)
```

#### 4.1.3 源码精读

参数定义集中在 [examples/update.py:162-186](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/examples/update.py#L162-L186)：注意 `--load-metas-file` 与 `--metas-url` 被放进 `add_mutually_exclusive_group()`（互斥组，同时传两个直接被 argparse 拒绝），它们的 help 文本都写着 "triggers join mode"。

关键参数对照表：

| 参数 | 默认值 | 作用的源码位置 |
| --- | --- | --- |
| `--checkpoint-path` | None | 切分策略判断（L205-207）与两个 split 函数 |
| `--update-method` | broadcast | `update_weights` 内的两次 `if` 分岔（L119/L123） |
| `--inference-parallel-size` | 8 | 贯穿 `check_vllm_ready`、`req_inference`、P2P 的 `ranks` |
| `--endpoint` | http://localhost:19730 | vLLM API 地址，拼 `/health` 与 `/collective_rpc` |
| `--uds` | None | 传给 `httpx.HTTPTransport(uds=...)`，改走 Unix domain socket |
| `--custom-dist` | None | `dist.use_backend`（L191） |
| `--save-metas-file` / `--load-metas-file` / `--metas-url` | None | metas 导出 / join 模式入口（u6-l3） |
| `--sleep-time` | 0 | 脚本末尾驻留（L225） |

初始化四连击在 [examples/update.py:187-192](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/examples/update.py#L187-L192)：先从环境变量读 rank/world_size，随后**先造 `req_func`、再切后端、最后构造 PS**。这个顺序不是随意的：

- [examples/update.py:191](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/examples/update.py#L191) 调用 `dist.use_backend(args.custom_dist)`——`use_backend` 会替换全局单例 `_BACKEND_INSTANCE`（见 [checkpoint_engine/distributed/base.py:221-242](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/distributed/base.py#L221-L242)），只认 `vllm_nccl` / `vllm_hccl` 两个名字，传非法值直接 `ValueError`；传 `None`（默认）则是幂等 no-op，继续用 `TorchBackend`。按 u5-l2 的结论，自定义后端**必须在首次建组之前**切换——所以这一行排在 `ParameterServer(auto_pg=True)` 之前。
- [examples/update.py:192](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/examples/update.py#L192) 显式传 `auto_pg=True`（其实这也是默认值），让进程组随每次 `update` 自动建毁——这是同一个进程里能**连续跑两次 update**（`update_method="all"`）的前提。

join 分岔与切分策略选择在 [examples/update.py:193-212](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/examples/update.py#L193-L212)。其中 L205-207 的判断条件是模块③的"选择条件"：

- 目录里存在 `model.safetensors.index.json` **且** 路径不以 `/dev/shm/` 开头 → `split_tensors`（按张量切，产出 `named_tensors`）；
- 否则（没有 index 文件，**或** checkpoint 在 `/dev/shm` 下）→ `split_checkpoint_files`（按文件切，产出 `checkpoint_files`）。

为什么 `/dev/shm` 要特判？回忆 u2-l3/u2-l4：`register_checkpoint` 的 `files` 输入在"CUDA 后端 + `/dev/shm` 下的 safetensors"时可以走 **inplace pin**（mmap 后 `cudaHostRegister` 原地锁页、零拷贝、不搬数据），而 `named_tensors` 输入**恒走 normal pin**（先分配锁页池再逐张量拷入）。所以在 `/dev/shm` 场景下，即使有 index 文件也故意选择按文件注册，把机会留给 inplace pin——两种切分策略的选择本质上是在为下游的 pin 策略让路。

另一个细节：两个分支产出的是"二选一"的输入——走张量切分时 `checkpoint_files = []`（L209），走文件切分时 `named_tensors = {}`（L212），最终由 `update_weights` 原样传给 `register_checkpoint(files=..., named_tensors=...)`（u3-l2 讲过这两种输入的分派）。

#### 4.1.4 代码实践

1. **实践目标**：熟悉参数面板，并观察"脱离 torchrun 直接运行"会发生什么。
2. **操作步骤**：
   ```bash
   cd checkpoint-engine   # 仓库根目录
   python examples/update.py --help
   ```
   然后不带任何参数、也不设环境变量运行：
   ```bash
   python examples/update.py
   ```
3. **需要观察的现象**：第一条命令应打印完整 usage，确认 `--load-metas-file` 与 `--metas-url` 互斥、`--update-method` 的 help 文本；第二条命令在参数解析之后、走到读取 `RANK` 环境变量处（[examples/update.py:187](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/examples/update.py#L187) 的 `int(os.getenv("RANK"))`）时因取到 `None` 抛出 `TypeError`。
4. **预期结果**：`--help` 正常输出（import torch/httpx/safetensors/checkpoint_engine 均可在纯 CPU 环境完成，argparse 在读 RANK 之前就退出了）；裸跑则失败于 `int(None)` 类型的 `TypeError`——这正说明该脚本必须由 torchrun（或手工导出 RANK/WORLD_SIZE/MASTER_ADDR）驱动。具体报错文本待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `dist.use_backend(args.custom_dist)` 必须写在 `ParameterServer(auto_pg=True)` 之前？写在后面会怎样？

**答案**：`use_backend` 替换的是 `distributed` 模块的全局后端单例 `_BACKEND_INSTANCE`，之后所有 `dist.*` 模块级函数（建组、barrier、broadcast……）都晚绑定到新单例上。PS 的 `auto_pg` 模式会在每次集合操作前自动 `init_process_group`；如果先建组再换后端，已有的进程组/通信器仍是旧后端建立的，会出现"一半通信走 TorchBackend、一半走 vLLM 后端"的割裂状态。所以在构造 PS（以及任何建组）之前切换。

**练习 2**：`--custom-dist` 传 `"vllm_xpu"` 会发生什么？

**答案**：`use_backend` 的映射表只有 `vllm_nccl` 和 `vllm_hccl` 两个键，其他值抛 `ValueError`，且错误信息明确说明 XPU 不支持自定义后端、应保持默认（xccl 的 TorchBackend），见 [checkpoint_engine/distributed/base.py:231-237](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/distributed/base.py#L231-L237)。

**练习 3**：`--sleep-time 300`（README join 用法里出现）的作用是什么？

**答案**：见 [examples/update.py:225](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/examples/update.py#L225)：脚本完成更新后原地睡 N 秒。它不是性能参数，而是让"存量实例"的 PS 进程存活足够久——其锁页内存与 p2p store 注册因此保持有效，新实例才能通过 `--load-metas-file` join 进来拉取权重（u6-l3 展开）。

### 4.2 模块②：`split_checkpoint_files`——按文件均分

#### 4.2.1 概念说明

Broadcast 更新的第一步是每个 rank 把自己分到的权重从磁盘加载并锁页。如果 8 个 rank 都去读全部文件，磁盘带宽和锁页内存都会爆炸。所以需要一个**静态负载均衡**：把 checkpoint 目录里的文件清单均分给各个 rank。

`split_checkpoint_files` 是粒度最粗的策略：**以文件为单位，按 rank 连续均分**。它是 u2-l2 讲过的 `_load_checkpoint`（文件加载链路）与 u2-l4 讲过的 inplace pin（文件粒度锁页）的天然搭档——每个文件保持完整，下游才有"整文件 mmap + cudaHostRegister"的机会。

#### 4.2.2 核心流程

```
列出 checkpoint_path 下所有 .safetensors 文件(按 os.listdir 顺序)
    │
    ├─ files_per_rank = ⌈N / W⌉     # N=文件数, W=world_size
    └─ 返回 files[rank * fpr : (rank+1) * fpr]   # 连续切片,末尾 rank 可能少分
```

整除向上取整用整数运算 \(\lceil N/W \rceil = (N + W - 1) \,//\, W\) 实现，避免浮点误差。

#### 4.2.3 源码精读

完整实现只有 7 行，见 [examples/update.py:51-57](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/examples/update.py#L51-L57)：

- [examples/update.py:52-55](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/examples/update.py#L52-L55)：用 `filter` + `endswith(".safetensors")` 列出目录下全部 safetensors 文件并拼成绝对路径。注意**没有排序**——顺序完全取决于 `os.listdir` 的返回（通常与目录项创建顺序有关，不同机器可能不同），好在均分只要求"互不重叠、总体覆盖"，对顺序不敏感。
- [examples/update.py:56](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/examples/update.py#L56)：`files_per_rank = (len(checkpoint_files) + world_size - 1) // world_size`，即向上取整。
- [examples/update.py:57](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/examples/update.py#L57)：Python 切片天然越界安全——当 \(N\) 不是 \(W\) 的倍数时，最后一个 rank 分到不足 `files_per_rank` 个文件，再往后的 rank 得到空列表。空列表是合法输入：u3-l2 讲过 `register_checkpoint` 支持"空注册"（返回空 metas、不能当 owner），这正是高秩 rank 分不到文件时的兜底。

该策略的均衡质量取决于文件大小是否均匀。safetensors 分片通常按大致相等的字节数切（如每片 5GB），所以按文件数均分在实践中已经够用；这也是 README 基准（如 8.00GiB 桶）能稳定复现的前提之一。

#### 4.2.4 代码实践

1. **实践目标**：验证均分公式的切片行为，特别是"分不满"与"分不到"两种边界。
2. **操作步骤**（示例代码，可在纯 CPU 环境运行，前提是已 `pip install -e .` 且装好 torch 等依赖）：
   ```python
   # practice_split_files.py(示例代码)
   import os, tempfile
   import examples.update as U

   d = tempfile.mkdtemp()
   for i in range(5):
       open(os.path.join(d, f"shard-{i}.safetensors"), "w").close()
   open(os.path.join(d, "README.txt"), "w").close()  # 应被过滤掉

   for rank in range(3):
       print(rank, [os.path.basename(f) for f in U.split_checkpoint_files(d, rank, 3)])
   ```
3. **需要观察的现象**：N=5、W=3 → `files_per_rank = ⌈5/3⌉ = 2`；`.txt` 文件不出现在任何人的清单里。
4. **预期结果**（按源码推演，待本地验证）：rank0 与 rank1 各分 2 个文件，rank2 只分到最后 1 个；`README.txt` 被过滤。可再改成 7 个文件、W=8，观察末尾 rank 拿到空列表。

#### 4.2.5 小练习与答案

**练习 1**：如果目录里有 100 个文件、world_size=8，rank 6 分到第几个到第几个文件？

**答案**：`files_per_rank = ⌈100/8⌉ = 13`；rank6 分到 `[6×13, 7×13) = [78, 91)`，共 13 个；rank7 分到 `[91, 100)` 只有 9 个——不均衡度最多差一个 `files_per_rank`。

**练习 2**：为什么这个函数不做 `sorted()`？加不加有什么区别？

**答案**：均分的正确性只依赖"各 rank 的切片区间互不重叠且覆盖全表"，与顺序无关；`os.listdir` 的任意顺序都满足这一点。加 `sorted()` 只能让分配结果跨机器可复现（便于调试对比），不影响正确性——但注意各 rank 上的 `os.listdir` 顺序通常一致（同一文件系统），即使不一致也不破坏"总覆盖"性质。

### 4.3 模块③：`split_tensors`——按张量均分与两种策略的选择条件

#### 4.3.1 概念说明

按文件均分的隐患是**文件粒度太粗**：如果分片文件大小不均（比如某些文件塞满了大 MLP 权重、另一些只有零散的 embedding），按文件数均分就会让有的 rank 锁页 20GB、有的只有 3GB。`split_tensors` 换成**以张量为单位均分**：借助 HF 格式的 `model.safetensors.index.json`（一张"张量名 → 所在文件"的全局映射表），把**张量总数**均分给各 rank，从而把粒度细化一个数量级。

代价是：它必须把张量真的读进内存（返回 `dict[str, torch.Tensor]`），于是只能走 `named_tensors` 输入 → normal pin（先分配锁页池再拷贝），失去了 inplace pin 的零拷贝机会。这就是 4.1 节选择条件的深层逻辑：**均衡性（张量粒度）与锁页效率（文件粒度的原地锁页）二选一**，`/dev/shm` 与否是裁决条件。

#### 4.3.2 核心流程

```
读 model.safetensors.index.json 的 weight_map: {张量名: 文件名}
    │
    ├─ weights_per_rank = ⌈len(weight_map) / W⌉
    ├─ 取 items() 第 [rank×wpr, (rank+1)×wpr) 段 → 按所在文件聚合 fn_tensors
    └─ 对每个涉及文件 safe_open 一次,逐名 get_tensor → named_tensors dict
```

两段式设计（先按文件聚合、再逐文件打开）保证每个 safetensors 文件**至多被打开一次**——如果直接"逐张量找文件打开"，一个文件的几十个张量会触发几十次 `safe_open`。

#### 4.3.3 源精读

完整实现见 [examples/update.py:60-74](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/examples/update.py#L60-L74)：

- [examples/update.py:61-63](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/examples/update.py#L61-L63)：打开 index 文件，取 `weight_map` 字段。类型标注 `dict[str, str]` 说明它就是"张量名 → 文件名"的平面映射（HF 标准格式）。注意 `json.load` 保持键的插入顺序（Python 3.7+ 字典有序），所以下面的切片顺序是确定性的、等于 index.json 的书写顺序。
- [examples/update.py:64](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/examples/update.py#L64)：与 4.2 完全相同的向上取整公式，只是均分单位从文件数换成了张量数。
- [examples/update.py:65-68](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/examples/update.py#L65-L68)：`defaultdict(list)` 把本 rank 分到的张量按所在文件聚合。这一步是"张量区间 → 文件分组"的重排，是实现"每文件只开一次"的关键。
- [examples/update.py:69-74](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/examples/update.py#L69-L74)：逐文件 `safe_open(framework="pt")` 后按名字 `get_tensor`，把张量实际读入内存。这里用的正是 u2-l2 讲过的 safetensors 零拷贝读取 API（在读取侧不产生多余拷贝，但跨进程读文件本身仍是一次磁盘 IO）。

与 `split_checkpoint_files` 对照：

| 维度 | 按文件切分 | 按张量切分 |
| --- | --- | --- |
| 均衡单位 | 文件数 | 张量数 |
| 返回类型 | `list[str]`（路径） | `dict[str, torch.Tensor]`（已加载） |
| 下游 pin 策略 | normal 或 **inplace**（`/dev/shm` 时） | 恒 normal（u2-l3 的分派规则） |
| 触发条件 | 无 index 文件，或路径在 `/dev/shm` 下 | 有 index 文件且不在 `/dev/shm` 下 |
| 内存形态 | 文件路径，加载延迟到 register 阶段 | 本进程已持有张量 |

#### 4.3.4 代码实践

1. **实践目标**：亲手构造一个微型 checkpoint 目录，验证张量级均分与"按文件聚合"行为。
2. **操作步骤**（示例代码，需要 torch 与 safetensors，纯 CPU 即可）：
   ```python
   # practice_split_tensors.py(示例代码)
   import json, os, tempfile
   import torch
   from safetensors.torch import save_file
   import examples.update as U

   d = tempfile.mkdtemp()
   names = [f"model.layers.{i}.weight" for i in range(10)]       # 10 个张量
   file_of = {n: f"shard-{i % 3}.safetensors" for i, n in enumerate(names)}
   by_file = {}
   for n, f in file_of.items():
       by_file.setdefault(f, {})[n] = torch.zeros(2)
   for f, ts in by_file.items():
       save_file(ts, os.path.join(d, f), metadata={"format": "pt"})
   with open(os.path.join(d, "model.safetensors.index.json"), "w") as fp:
       json.dump({"weight_map": file_of}, fp)

   for rank in range(4):
       got = U.split_tensors(d, rank, 4)
       print(rank, sorted(got))
   ```
3. **需要观察的现象**：`weights_per_rank = ⌈10/4⌉ = 3`；每个 rank 拿到的张量恰好是名字连续的一段；三个分片文件各自被打开过多少次（可在 L71 处临时加一行 print 观察，属于建议修改，改完请还原）。
4. **预期结果**（按源码推演，待本地验证）：rank0 → layers 0-2，rank1 → layers 3-5，rank2 → layers 6-8，rank3 → 只有 layer 9；每个 rank 涉及 2 个分片文件，但每个文件在单次 `split_tensors` 调用内只 `safe_open` 一次。

#### 4.3.5 小练习与答案

**练习 1**：index.json 存在、路径为 `/dev/shm/ckpt/`，脚本会走哪条切分？为什么这样设计？

**答案**：走 `split_checkpoint_files`（文件切分）。因为 `/dev/shm` 是 tmpfs（内存盘），文件已在内存中，`register_checkpoint` 的 files 输入可以走 inplace pin：mmap 后 `cudaHostRegister` 原地锁页、不搬数据、还顺带删掉源文件（u2-l4）。`named_tensors` 恒走 normal pin（先分配再拷贝），在 `/dev/shm` 场景反而多一次拷贝，所以判断条件用 `not args.checkpoint_path.startswith("/dev/shm/")` 把这种情况排除掉。

**练习 2**：`split_tensors` 返回的 `named_tensors` 交给 `register_checkpoint` 后，参数在锁页池里的排布顺序由谁决定？

**答案**：不再由本函数决定。`named_tensors` 进入 `_normal_pin_memory` 后会按**参数名字排序**（u2-l3 讲过的确定性布局），与本函数的分片顺序、聚合顺序无关——这是刻意设计：布局只依赖参数集合本身，与切分方式解耦。

**练习 3**：如果删掉 L65-68 的"按文件聚合"，直接对每个张量 `safe_open` 它所在的文件，最坏会发生什么？

**答案**：IO 次数从"涉及文件数"膨胀到"分到的张量数"。HF 大模型一个分片常含上百个张量，逐张量开关文件会放大 syscalls 与元数据解析开销；聚合后每文件一次 `safe_open`，开销与文件数成正比。

### 4.4 模块④：`req_inference`——组首计算与推理请求闭包

#### 4.4.1 概念说明

`ps.update(checkpoint_name, req_func)` 需要一个回调：当 PS 侧把 ZMQ socket 全部 bind 好、IPC 句柄导出完毕后，它会调用 `req_func(socket_paths)`，其中 `socket_paths` 是**全集群所有 rank** 的 `(设备UUID, ZMQ地址)` 清单（u3-l6 讲过它由各 rank 的 `_bind_zmq_socket` 拼装）。`req_func` 的职责是：**让推理引擎开始干活**——通知 vLLM 通过 `/collective_rpc` 调用每个 worker 的 `update_weights_from_ipc`。

这里有一个必须解决的映射问题：一个 torchrun 会话覆盖多个推理实例（每个实例 P 个 TP 进程）时，`socket_paths` 里有 W 条地址，而**每个 vLLM 实例只能使用、也只需要自己那台机器上的 P 条地址**——因为 ZMQ 地址是 Linux 抽象 Unix domain socket，**仅主机内有效**（u3-l6 的关键结论）。于是：

1. 谁去通知实例 X？→ 该实例对应 rank 段的**组首**（`rank % P == 0`）。
2. 通知时带哪些地址？→ `socket_paths[src : src+P]`，恰为本实例的 P 条。

`req_inference` 就是用一个闭包把"我是谁（rank）、段首是谁（src）"固化进去，产出一个可序列化传递的 `req_func`。

#### 4.4.2 核心流程

```
构造期(每个进程各执行一次):
    rank ← getenv("RANK")
    src  = ⌊rank / P⌋ × P          # 本 rank 所属实例的段首 rank

运行期(PS 在 update 中调用 req_func(socket_paths)):
    rank == src ?
        ├─ 是 → 取 socket_paths[src : src+P] (P 条 (uuid, zmq_addr))
        │        转 dict → request_inference_to_update(f"{endpoint}/collective_rpc", dict, uds)
        └─ 否 → 直接返回(静默,不发声)
```

数学上，`src = ⌊rank / P⌋ × P` 就是把 rank 向下对齐到 P 的倍数，等价于 `rank - rank % P`；"rank 是组首" ⟺ `rank == src` ⟺ `rank % P == 0`。

#### 4.4.3 源码精读

完整实现见 [examples/update.py:77-93](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/examples/update.py#L77-L93)：

- [examples/update.py:82](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/examples/update.py#L82)：闭包构造期从环境变量**重新**读一次 `RANK` 作为局部变量。注意这与 `__main__` 里 L187 的模块级 `rank` 是两个变量——`req_inference` 不依赖全局状态，作为函数更自包含（import 它的测试代码只需设环境变量即可）。
- [examples/update.py:83](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/examples/update.py#L83)：`src = rank // inference_parallel_size * inference_parallel_size`，即上文的对齐公式。
- [examples/update.py:85-91](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/examples/update.py#L85-L91)：闭包本体。只有 `rank == src` 的进程真正发请求；`dict(socket_paths[src : src + inference_parallel_size])` 把 `[(uuid, addr), ...]` 列表转成 `{uuid: addr}` 字典后发出。
- 请求最终落到 [checkpoint_engine/api.py:15-43](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/api.py#L15-L43) 的 `request_inference_to_update`：向 `{endpoint}/collective_rpc` POST `{"method": "update_weights_from_ipc", "args": [socket_paths], "timeout": ...}`；当 `uds` 不为 None 时用 `httpx.HTTPTransport(uds=uds)` 改走 Unix domain socket（u4-l5 讲过的双通道）。vLLM 收到 RPC 后会把它广播给本实例的 P 个 worker，每个 worker 按自己的设备 UUID 查字典拿到 ZMQ 地址并 connect——这就是 u4-l2 的调用链。

关于多实例部署的说明：README 的用法（如 [README.md:135](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/README.md#L135)、SGLang 一节"run the same commands on both nodes"）是**每个节点各跑一个 torchrun 会话、`--endpoint` 指向本节点的推理服务**，此时会话内 `world_size == P`，只有 rank 0 是组首。`src : src + P` 的切片写法天然兼容"一个会话覆盖多个实例"的推广——每个组首切出自己那 P 条，各自向自己会话的 endpoint 发请求。

还应注意一个隐含假设：`socket_paths` 的**顺序**与 rank 对齐。u3-l6 讲过 `socket_paths` 由 `(uid, addr) for uid in self._global_device_uuids` 生成，而 `_global_device_uuids` 在 `gather_metas` 里按 rank 顺序收集——所以"下标切一段"才是安全的。这也是 `update` 必须在 `gather_metas` 之后调用的原因之一。

#### 4.4.4 代码实践

1. **实践目标**：在不启动任何 HTTP 服务的情况下，验证"组首才发请求 + 段切片"两个行为。
2. **操作步骤**（示例代码，纯 CPU 可跑；关键是替换 `examples.update` 命名空间里的函数名，替换 `checkpoint_engine` 包里的同名函数**无效**，因为示例脚本用的是自己模块里的绑定）：
   ```python
   # practice_req_inference.py(示例代码)
   import os
   import examples.update as U

   captured = {}
   U.request_inference_to_update = (       # 打桩:拦截而不是真的发 HTTP
       lambda url, paths, uds=None: captured.update({"url": url, "paths": paths})
   )
   paths = [(f"uuid{i}", f"ipc://@sock{i}") for i in range(24)]  # 模拟 24 rank 的清单

   for r in ("0", "8", "10", "16"):
       os.environ["RANK"] = r
       captured.clear()
       U.req_inference("http://h:1", 8)(paths)
       print(f"RANK={r:>2} ->", captured.get("paths"))
   ```
3. **需要观察的现象**：RANK=10（非组首）时 `captured` 保持为空；RANK=0/8/16 时各自拿到互不重叠的 8 条。
4. **预期结果**（按源码推演，待本地验证）：RANK=0 → uuid0~uuid7；RANK=8 → uuid8~uuid15；RANK=10 → `None`（未发请求）；RANK=16 → uuid16~uuid23。三次请求的 `url` 都是 `http://h:1/collective_rpc`。

#### 4.4.5 小练习与答案

**练习 1**：为什么非组首 rank 的 `req_func` 什么都不做也不会"漏通知"？

**答案**：`req_func` 是 PS 在 `update` 内部调用的回调，每个 rank 各自持有自己的闭包；对同一个实例而言，它的 P 个 rank 中恰有一个组首会发请求，而请求里携带了该实例**全部 P 个** socket 地址（`src : src + P` 切片），vLLM 的 `/collective_rpc` 会把调用广播给所有 P 个 worker。所以"每实例一个发声者、发声者带全组地址"即可覆盖全员，其余 rank 保持沉默。

**练习 2**：如果把 L89 改成 `dict(socket_paths)`（全量发送）会出什么问题？

**答案**：两个问题。其一，vLLM 实例收到的是全集群的地址字典，而抽象 Unix domain socket 只在本机有效，跨机地址 connect 必然失败；其二，多实例场景下每个实例都拿到别家实例的地址，设备 UUID 查表会错乱（u4-l2 讲过 UUID 是跨进程配对的钥匙）。切片保证"每个实例只看到自己机器上的 P 条"。

**练习 3**：`req_func` 由哪个线程、在什么时机执行？

**答案**：由 PS 的 `update` 数据面流程在 ZMQ socket bind 完成、IPC 句柄导出之后调用（u3-l4 的主循环启动前），而不是 HTTP 线程——这与 u4-l5 中 API 服务层"闭包由 PS 数据面线程执行"的设计一致。示例脚本里 `req_inference(...)` 在 `__main__` 早期就构造好了闭包，但真正执行要等到 `ps.update` 内部。

### 4.5 模块①：`update_weights`——七步生命周期编排

#### 4.5.1 概念说明

`update_weights` 是整个脚本的中枢：它把前面所有模块的产出（切分结果、`req_func`、参数）按正确顺序喂给 `ParameterServer` 的生命周期方法，并用 `update_method` 参数控制最后一步走 Broadcast、P2P 还是两者都走。它是"README 一条命令 → 一次权重更新"之间的全部胶水。

#### 4.5.2 核心流程

七步编排（括号内为对应的 PS 方法）：

```
① ps.init_process_group()          显式建组(auto_pg 下后续不再重建)
② dist.barrier()                   对齐:所有 rank 完成各自的准备
③ ps.register_checkpoint(files/named_tensors)   磁盘→锁页内存(u3-l2)
④ check_vllm_ready(endpoint, P, uds)  组首轮询 vLLM /health,其余 rank 直接过
⑤ dist.barrier()                   对齐:等 vLLM 就绪
⑥ ps.gather_metas(name)            全局元数据收集(u3-l3)
   └ (可选) rank0 把 ps.get_metas() 导出为 JSON 文件(save_metas_file)
⑦ 按 update_method 分岔:
   broadcast | all → ps.update(name, req_func)                  # ranks=None → 广播
   p2p      | all → sleep(2) → ps.update(name, req_func, ranks=range(P))  # P2P
```

两次 barrier 的位置是编排的精髓：第一次保证"人人锁页完毕"（register 涉及磁盘 IO 与多线程拷贝，快慢差异大），第二次保证"vLLM 已就绪再进入需要 worker 配合的 gather/update 阶段"（`check_vllm_ready` 只在组首阻塞，其他 rank 靠 barrier 等它）。

#### 4.5.3 源码精读

函数签名与默认值见 [examples/update.py:96-107](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/examples/update.py#L96-L107)：注意 `update_method` 的类型是 `Literal["broadcast", "p2p", "all"]`，`uds` 与 `save_metas_file` 均可空。

①② 建组与第一道屏障：[examples/update.py:108-109](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/examples/update.py#L108-L109)。虽然 `auto_pg=True` 时各生命周期方法会自建进程组（u3-l3 讲过 `gather_metas` 内部就有 `if self._auto_pg and not dist.is_initialized()` 的兜底），但这里**显式**先建一次，让整段编排共享同一个组，避免每步各自建毁的开销。

③ 注册：[examples/update.py:110](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/examples/update.py#L110)。`files` 与 `named_tensors` 二选一（另一个为空），正是 4.1 节切分策略的产出。注册内部完成磁盘加载 + 锁页 + p2p store 报备（u3-l2）。

④ 就绪检查：[examples/update.py:111](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/examples/update.py#L111) 调用 `check_vllm_ready`（实现于 [examples/update.py:33-48](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/examples/update.py#L33-L48)）：

- [examples/update.py:34](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/examples/update.py#L34)：`if rank != rank // inference_parallel_size * inference_parallel_size: return`——**非组首直接返回**。注意这里引用的是 `__main__` 里定义的**模块级全局变量** `rank`（L187），与 `req_inference` 内部重新读环境变量的做法不同：若从别的模块 import `check_vllm_ready` 而没有先给 `examples.update.rank` 赋值，会抛 `NameError`。这是示例代码的一个"脚本味"细节，阅读时要留意。
- [examples/update.py:38-39](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/examples/update.py#L38-L39)：`uds` 非空时构造 `httpx.HTTPTransport(uds=uds)`，健康检查也走 UDS。
- [examples/update.py:40-48](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/examples/update.py#L40-L48)：`GET {endpoint}/health`（超时 10 秒），仅在 `ConnectError` / `HTTPStatusError` 时重试，每轮间隔 5 秒、**无限重试**——这就是 README 说"No need to wait for vLLM to get ready"的实现：checkpoint 注册与 vLLM 启动（含 dummy 加载）并行推进，谁慢等谁。

⑥ 元数据收集与可选导出：[examples/update.py:113-117](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/examples/update.py#L113-L117)。`timer` 上下文（[examples/update.py:25-30](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/examples/update.py#L25-L30)）只是打点耗时。当 `save_metas_file` 非空且 `RANK==0` 时，用模块顶部的 `_METAS_ADAPTER = TypeAdapter(dict[int, MemoryBufferMetaList])`（[examples/update.py:22](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/examples/update.py#L22)）把 `ps.get_metas()` 序列化为 JSON 文件——这正是 u2-l1 讲过的 metas JSON 出口，为 u6-l3 的 join 模式埋下伏笔。

⑦ 更新方法分岔：[examples/update.py:119-128](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/examples/update.py#L119-L128)：

- Broadcast 分支（L119-121）：`ps.update(checkpoint_name, req_func)`，不传 `ranks` → `ranks=None` → 全量广播（README 基准里最快的默认路径）。
- P2P 分支（L123-128）：先 `time.sleep(2)`，再 `ps.update(checkpoint_name, req_func, ranks=list(range(inference_parallel_size)))`——目标显式指定为**第一个实例**的 P 个 rank。README 基准注明 P2P 是在 256 卡集群里只更新其中 16 卡（`ParameterServer.update(ranks=range(0, 16))`，见 [README.md:61](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/README.md#L61)）测的，对应"新实例加入、不打扰存量实例"的弹性场景。

**`sleep(2)` 的真实原因**：`auto_pg=True` 时 `ps.update` 的 `finally` 会销毁进程组（[checkpoint_engine/ps.py:610-614](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L610-L614)）。`update_method="all"` 时第二次 update 进入后会检测到 `not dist.is_initialized()` 并**重建**进程组（[checkpoint_engine/ps.py:596-597](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L596-L597)）。若某个 rank 还没拆完旧组、另一个 rank 已开始建新组，共享的 TCPStore 上会出现读写竞态（u3-l4 讲过 PrefixStore 自增前缀只能防键冲突，防不了时序交错）。注释 `# sleep 2s to wait destroy process group`（[examples/update.py:125](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/examples/update.py#L125)）说的就是这件事——用一段静默时间换各 rank 拆组完成后再齐步建组。

**一个值得辨析的细节**：L124 的判断写的是 `if update_method:`，而能走到这里 `update_method` 必是 `"p2p"` 或 `"all"`（非空字符串恒真），所以这个 sleep 实际上**无条件执行**。推测原意是 `if update_method == "all"`（只有前面跑过 broadcast、确实存在"等拆组"的需求时才睡）；对 `"p2p"` 单独运行的情况，此时组是 L108 建的且未销毁，sleep 并非必需，只是无害地多等 2 秒。阅读示例代码时要能区分"逻辑必需"与"写法使然"。

#### 4.5.4 代码实践（源码阅读型）

1. **实践目标**：把 `sleep(2)` 的因果链在源码里走通，能指出涉及的三个函数。
2. **操作步骤**：
   - 读 [examples/update.py:123-128](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/examples/update.py#L123-L128)，确认第二次 `ps.update` 传了 `ranks=list(range(8))`。
   - 读 [checkpoint_engine/ps.py:596-597](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L596-L597)：`if self._auto_pg and not dist.is_initialized(): self.init_process_group(timeout=timeout)`——重建发生在这里。
   - 读 [checkpoint_engine/ps.py:610-614](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L610-L614)：`finally` 中 `dist.destroy_process_group()`——销毁发生在这里。
   - 再看 [examples/update.py:108](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/examples/update.py#L108)：`update_weights` 开头显式建的那次组，正是第一次 update 结束时被销毁的对象。
3. **需要观察的现象**：三个位置构成"建组 → update 内销毁 → 下次 update 内重建"的闭环；sleep 夹在销毁与重建之间。
4. **预期结果**：能独立复述——`auto_pg` 让每次 update 自带组的生灭，两次 update 之间必须留出全组拆完的时间窗；`update_weights` 开头的显式建组则让 register/gather 等中间步骤复用同一个组。

#### 4.5.5 小练习与答案

**练习 1**：`update_method="all"` 时，两次 `ps.update` 分别走什么传输路径？为什么 P2P 那次的目标是 `range(P)` 而不是全部 rank？

**答案**：第一次 `ranks=None` 走 Broadcast（H2D + 广播 + reload 三阶段流水线，u1-l4）；第二次 `ranks=range(P)` 走 P2P（Mooncake RDMA 单边读，u5-l5/u5-l6）。`range(P)` 只覆盖第一个实例的 P 个 rank，模拟"给一个新加入的实例补权重"的弹性场景——这正是 P2P 的设计用途（不打扰存量实例），README 基准也按此口径测量。

**练习 2**：把 L111 的 `check_vllm_ready` 与 L112 的 `dist.barrier()` 交换顺序，会有什么风险？

**答案**：非组首 rank 会先到达 barrier 并阻塞等待；组首则陷入 `/health` 的无限重试循环。如果 vLLM 迟迟不就绪，全体 rank 都卡在 barrier 上——行为上看起来类似，但真正的区别在于：先 check 后 barrier 的现有顺序下，组首在等待期间**不断重试并打日志**（每 5 秒一条 warning），定位问题更直观；而且交换后若有 rank 在 register 阶段就失败，错误会先以 barrier 超时（默认 timeout 较长）的形式暴露，而不是明确的注册异常。编排顺序的原则：把"可能长时间阻塞的旁路检查"放在 barrier 之前，让快路径尽快对齐。

**练习 3**：`save_metas_file` 导出为什么只在 rank 0 做？其他 rank 的 metas 难道不需要吗？

**答案**：不需要。`ps.get_metas()` 返回的是 `gather_metas` 用 `all_gather_object` 收齐的**全局**表（以 owner_rank 为键，u3-l3），每个 rank 手里都有完整副本；rank 0 导出一份即代表全集群。`_METAS_ADAPTER` 的类型 `dict[int, MemoryBufferMetaList]` 正说明这份文件是跨 rank 的全局元数据。

## 5. 综合实践

**任务：在纯 CPU 环境下"干跑" `update_weights` 的完整编排，验证七步顺序与 `update_method` 分岔。**

思路沿用 u6-l1 讲过的测试替身范式：`update_weights` 依赖的 `ParameterServer`、`dist.barrier`、`check_vllm_ready`、`time.sleep` 全部替换成"记录桩"，然后用 `update_method="all"` 驱动它，观察编排层到底按什么顺序调用了什么。

```python
# dry_run_update_weights.py(示例代码)
"""纯 CPU 干跑 examples/update.py 的 update_weights 编排。"""
from unittest.mock import MagicMock

import examples.update as U
import checkpoint_engine.distributed as dist

calls = []

# 1. 替身 PS:记录生命周期方法的调用
ps = MagicMock()
ps.init_process_group.side_effect = lambda *a, **k: calls.append("① init_process_group")
ps.register_checkpoint.side_effect = (
    lambda name, *, files=None, named_tensors=None, **k:
    calls.append(f"③ register_checkpoint(files={files}, named_tensors={list(named_tensors)})")
)
ps.gather_metas.side_effect = lambda *a, **k: calls.append("⑥ gather_metas")
ps.get_metas.return_value = {}
def fake_update(name, req_func, *, ranks=None, **k):
    calls.append(f"⑦ update(ranks={ranks})")
    req_func([("uuid", "ipc://@x")] * 8)      # 模拟 PS 在数据面调用回调
ps.update.side_effect = fake_update

# 2. 替换编排层的其余依赖
dist.barrier = lambda *a, **k: calls.append("barrier")
U.check_vllm_ready = lambda *a, **k: calls.append("④ check_vllm_ready")
U.time.sleep = lambda s: calls.append(f"sleep({s})")   # 不真的睡
captured = []
req_func = lambda paths: captured.append(len(paths))

# 3. 以 update_method="all" 驱动
U.update_weights(
    ps, "my-ckpt", ["f0.safetensors"], {"w0": None},
    req_func, inference_parallel_size=8,
    endpoint="http://localhost:19730", update_method="all",
)
print("\n".join(calls))
print("req_func 被调用次数:", len(captured))
```

**操作步骤**：

1. 在仓库根目录保存上述脚本（属于你自己的实践文件，不要提交）。
2. `python dry_run_update_weights.py`（需要环境已安装 torch、httpx、safetensors、loguru、pydantic 并 `pip install -e .`）。
3. 把 `update_method="all"` 依次改成 `"broadcast"`、`"p2p"` 再跑两次，对比三次输出。

**需要观察的现象**：

- `update_method="all"`：出现**两次** `update(ranks=...)`，第一次 `ranks=None`、第二次 `ranks=[0,...,7]`，且两次之间夹着 `sleep(2)`。
- `update_method="broadcast"`：只有一次 `update(ranks=None)`，没有 sleep 与第二次 update。
- `update_method="p2p"`：没有 broadcast 那次，但 sleep(2) 依然出现（印证 4.5 节"L124 恒真"的辨析）。
- `req_func` 的调用次数等于 `ps.update` 的真实执行次数——它由（被替换前的）`ps.update` 数据面调用，编排层只负责传递。

**预期结果**（按源码推演，`"all"` 模式下应类似如下顺序，具体文本待本地验证）：

```
① init_process_group
barrier
③ register_checkpoint(files=['f0.safetensors'], named_tensors=['w0'])
④ check_vllm_ready
barrier
⑥ gather_metas
⑦ update(ranks=None)
sleep(2)
⑦ update(ranks=[0, 1, 2, 3, 4, 5, 6, 7])
req_func 被调用次数: 2
```

**思考题**（配合观察）：如果把 `ps.update.side_effect` 里的 `req_func(...)` 那行删掉，输出会有什么变化？这说明了 `req_func` 的调用者是谁？（答案：`captured` 变为 0——编排层从不主动调用回调，调用发生在 `ParameterServer.update` 内部数据面就绪之后。）

## 6. 本讲小结

- `examples/update.py` 的 `__main__` 按"参数 → req_func 闭包 → dist 后端切换 → PS 构造 → 模式分派 → 切分策略选择"的顺序启动；`dist.use_backend(args.custom_dist)` 必须发生在任何建组之前，`--custom-dist` 只认 `vllm_nccl` / `vllm_hccl`。
- 两种切分策略：有 index 文件且不在 `/dev/shm` 时按**张量**均分（`split_tensors`，均衡粒度细，但恒走 normal pin）；否则按**文件**均分（`split_checkpoint_files`，粒度粗，但为 `/dev/shm` 下的 inplace pin 留门）。二者都用 \(\lceil N/W \rceil\) 连续切片，空分配合法。
- `req_inference` 用 `src = ⌊rank/P⌋×P` 计算"实例组首"：只有组首向 `{endpoint}/collective_rpc` 发请求，且只携带 `socket_paths[src:src+P]` 这 P 条本机地址（抽象 UDS 仅主机内有效）；非组首静默返回。
- `update_weights` 的七步编排：建组 → barrier → 注册 → 组首健康检查（无限重试，与 vLLM 启动并行）→ barrier → gather_metas（可选 rank0 导出 metas）→ 按 `update_method` 分岔。
- `update_method="all"` 两次 update 之间的 `sleep(2)` 是在等 `auto_pg` 的拆组完成——`ps.update` 的 `finally` 销毁进程组、下次进入时检测 `not is_initialized()` 再重建；而 L124 的 `if update_method:` 恒真，sleep 实际无条件执行。
- 本脚本就是 README 全部基准的测量载体；join 模式（`--load-metas-file` / `--metas-url`）的分岔口在 `__main__` L193，细节留给下一讲。

## 7. 下一步学习建议

- **下一讲 u6-l3《metas 导出与新实例 join：权重复用机制》**：本讲两处伏笔——`_METAS_ADAPTER` 的 JSON 导出（L115-117）与 `join` 函数（[examples/update.py:131-159](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/examples/update.py#L131-L159)）——将在那里展开：`get_metas`/`load_metas` 如何配合 p2p store 的稳定注册实现新实例零拷贝拉权重。
- **回顾 u3-l4/u5-l6**：本讲的两次 `ps.update` 分别对应广播流水线与 P2P 桶分配两条主链路；如果你对 `ranks` 参数进入 `_update_per_bucket` 后的分流细节模糊，值得回头精读。
- **动手方向**：参照第 5 节的干跑范式，把 `join` 函数也做成纯 CPU 干跑（替掉 httpx.get 与 PS 方法），验证"load_metas 必须在 gather_metas 之后调用"的顺序约束；或给 `split_checkpoint_files` 加上按文件大小加权均衡的改进（先 `os.stat` 再贪心装箱），对比原实现在大小组片混杂目录下的均衡度。
