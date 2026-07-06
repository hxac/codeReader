# 快速上手：初始化分布式环境并跑通 test_ep.py

## 1. 本讲目标

本讲是入门单元的最后一篇，目标是让读者在一台真实的 8 卡机器（或多节点集群）上把 DeepEP 跑起来，并看懂屏幕上滚动出的那一堆带宽/延迟数字。学完后你应当能够：

- 说清楚 `init_dist` / `init_seed` / `dist_print` 三个测试工具函数各自在做什么、它们如何配合 `torch.multiprocessing.spawn` 拉起一个多进程 NCCL 集群。
- 顺着 `test_ep.py` 的 `__main__` → `test_loop` → `test_dispatch_combine` 主线，读懂一篇 600 行的端到端测试。
- 自己用 `ElasticBuffer` 跑完一次 dispatch + combine，并正确解读输出里的 SO/SU 带宽、copy 带宽与延迟（us）。
- 知道何时 SO（scaleout/RDMA）带宽会是 0、为什么单机 8 卡只能看到 SU（scaleup/NVLink）数字。

本讲建立在 u1-l1（EP/MoE 概念）、u1-l2（目录分层与五层调用）、u1-l3（安装与构建）之上，默认你已经 `python setup.py install` 成功并能 `import deep_ep`。关于 `ElasticBuffer` 构造参数的细节、dispatch/combine 的张量语义，本讲只做「够用」的介绍，深入留到 u2。

## 2. 前置知识

在动手之前，先用最朴素的语言对齐几个概念。

- **rank / world_size**：分布式训练里，一个 rank 就是一个独立的 GPU 进程。8 卡单机就是 8 个 rank，`world_size = 8`。`local_rank` 是「本进程在所在节点内用第几张卡」（0~7），`rank`（全局）是「在整个集群里它是第几个进程」。
- **NCCL backend**：PyTorch `torch.distributed` 默认的 GPU 通信后端，封装了 NVLink/RDMA。DeepEP 的 NCCL Gin 后端会**复用**同一个 NCCL communicator（详见 u3-l4），所以测试里必须先把 `torch.distributed` 初始化好。
- **master 地址握手**：多进程要互连，需要一个共同的「接头地点」。DeepEP 测试用最简单的 TCP 法：`MASTER_ADDR:MASTER_PORT`，rank 0 当协调者，其余 rank 连过去交换信息。
- **SO / SU**：DeepEP 输出里反复出现的两个缩写。**SO = scaleout**，对应节点间的 RDMA 网卡流量；**SU = scaleup**，对应节点内的 NVLink 流量。这是逻辑域（scaleout/scaleup）划分，详见 u3-l1。**单机场景下没有 RDMA 流量，SO 带宽恒为 0**，这点会在 4.4 节反复强调。
- **逻辑带宽（logical bandwidth）**：README 性能表里明确写了「the results are logical bandwidth」，意思是统计的是「本 rank 视角下经过通信内核的总字节」，包含本 rank 内部搬运，**不等于**网线上的纯物理带宽。这和 u1-l1 的结论一致。

## 3. 本讲源码地图

本讲只聚焦两个最小模块：`test_ep.py` 与 `init_dist`。涉及的文件如下：

| 文件 | 作用 | 本讲用到哪部分 |
|--|--|--|
| `tests/elastic/test_ep.py` | V2（elastic）端到端正确性 + 性能测试主入口 | 全文主线：`__main__` → `test_loop` → `test_dispatch_combine`，以及命令行参数 |
| `deep_ep/utils/envs.py` | 分布式启动、随机种子、跨 rank 打印、NVLink/RDMA 带宽探测等工具 | `init_dist` / `init_seed` / `dist_print` |
| `deep_ep/utils/testing.py` | 基准测试工具 | `bench_kineto`（用 PyTorch profiler 测单 kernel 时长，4.4 节会用） |
| `README.md` | 性能参考表与命令行用法 | 拿来和你的实测数字对照 |

> ⚠️ 一个容易踩的小坑：README 第 95 行写「you may modify the `init_dist` function in `tests/utils/envs.py`」，但仓库里**并没有** `tests/utils/envs.py` 这个文件（`tests/utils/` 下只有 `test_gate.py`）。真正的 `init_dist` 在 [deep_ep/utils/envs.py](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/utils/envs.py#L73-L113)。要改集群配置就改这里。

永久链接 base 统一为 `https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/`。

---

## 4. 核心概念与源码讲解

### 4.1 多进程分布式环境初始化（init_dist / init_seed / dist_print）

#### 4.1.1 概念说明

DeepEP 的所有内核都是「跨 rank 协作」的：dispatch 时 8 个 rank 要往彼此的 buffer 里写数据，combine 时又要互相读。这就要求在跑任何 `buffer.dispatch()` 之前，必须先有一组**互相连通**的进程。`init_dist` 就是干这件事的——它把当前进程登记进 PyTorch 的 NCCL 世界，并返回一个 `ProcessGroup`，后面 `ElasticBuffer` 会拿这个 group 去建立对称内存窗口。

`init_seed` 解决的是「可复现性」：每个 rank 用 `全局种子 + rank` 当自己的本地种子，这样既能整体复现，又保证不同 rank 生成不同的随机 token（否则 dispatch 测试就退化成「8 个进程发完全一样的数据」）。

`dist_print` 解决的是「多进程打印混乱」：如果 8 个进程同时 `print`，输出会交错。它通过 `once_in_node` 参数控制「每节点只有 local_rank 0 打印」，并在末尾加一个 `dist.barrier()` 保证打印顺序。

#### 4.1.2 核心流程

`init_dist` 的执行流程：

1. 从环境变量读集群拓扑：`MASTER_ADDR`（默认 `127.0.0.1`，适合单机多进程）、`MASTER_PORT`（默认 `8361`）、`WORLD_SIZE`（节点数，默认 1）、`RANK`（节点序号，默认 0）。
2. 记下 `local_rank`（模块级全局变量，供 `dist_print` 用）。
3. 计算 `world_size = 节点数 × 节点内卡数`，`rank = 节点序号 × 节点内卡数 + local_rank`。
4. 调 `dist.init_process_group(backend='nccl', init_method='tcp://ip:port', ...)`，让各 rank 通过 TCP 握手建 NCCL communicator。
5. 设默认 dtype 为 bfloat16、默认 device 为 cuda、把当前进程绑到 `cuda:local_rank`。
6. 调 `init_seed(seed)` 给每个 rank 派一个独立种子。
7. 返回 `(rank, world_size, group)`——这个 `group` 就是后面喂给 `ElasticBuffer` 的通信组。

#### 4.1.3 源码精读

先看 `init_dist` 的签名与环境变量读取（这是最常需要按自己集群改的地方）：

[deep_ep/utils/envs.py:73-91](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/utils/envs.py#L73-L91) —— `init_dist` 函数签名与从环境变量读取 `MASTER_ADDR/PORT`、节点数、节点序号；默认值（`127.0.0.1:8361`、单节点）正是为单机 8 卡多进程准备的。

[deep_ep/utils/envs.py:97-110](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/utils/envs.py#L97-L110) —— 用 `inspect.signature` 探测 `init_process_group` 是否支持 `device_id` 形参（新版 PyTorch 才有），然后拼接参数、调用 `dist.init_process_group`，并把默认 dtype/device/cuda 设备绑到当前进程。

```python
params = {
    'backend': 'nccl',
    'init_method': f'tcp://{ip}:{port}',
    'world_size': num_nodes * num_local_ranks,
    'rank': node_rank * num_local_ranks + local_rank,
}
if 'device_id' in sig.parameters:
    params['device_id'] = torch.device(f'cuda:{local_rank}')
dist.init_process_group(**params)
```

注意 `rank` 的算法：`节点序号 × 节点内卡数 + local_rank`。单机时 `node_rank=0`，所以全局 rank 就等于 local_rank。

再看 `init_seed` 与 `dist_print`：

[deep_ep/utils/envs.py:24-35](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/utils/envs.py#L24-L35) —— `init_seed`：本地种子 = 全局种子 + rank，同时播种 `torch` 和 `random` 两个随机源，保证「整体可复现、各 rank 不同」。

[deep_ep/utils/envs.py:58-70](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/utils/envs.py#L58-L70) —— `dist_print`：`once_in_node=True` 时只让每个节点的 local_rank 0 打印，末尾 `dist.barrier()` 强制所有 rank 同步，避免输出交错。

#### 4.1.4 代码实践（源码阅读型）

1. **实践目标**：搞清楚 `init_dist` 是怎么从环境变量推算 `world_size` 和 `rank` 的，从而能在多节点场景正确配置。
2. **操作步骤**：
   - 打开 `deep_ep/utils/envs.py`，定位到 `init_dist` 的第 88~91 行那四个 `os.getenv`。
   - 想象一个双节点（每节点 8 卡）集群：节点 0 上你应该设哪些环境变量？节点 1 呢？
3. **需要观察的现象**：手算节点 1 上 local_rank=3 的进程，它的全局 `rank` 是多少。
4. **预期结果**：节点序号 `RANK=1`，`num_local_ranks=8`，所以 `rank = 1×8 + 3 = 11`，`world_size = 2×8 = 16`。如果你答出 11，就说明你读懂了这套编号规则。
5. 多节点时还需把 `MASTER_ADDR` 设成节点 0 的可达 IP（不能再用默认 `127.0.0.1`）。**待本地验证**：在没有真实集群时，你可以在单机上模拟双节点（两个进程组共用一块卡）但意义不大，建议等有集群再验证。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `init_seed` 里要把「全局种子 + rank」当本地种子，而不是让所有 rank 用同一个种子？

**答案**：如果所有 rank 用同一个种子，`torch.randn` 会在 8 个进程里生成完全相同的 token，dispatch 测试就退化成「8 个 rank 互发一模一样的数据」，无法覆盖真实的 MoE 路由分布。加上 rank 偏移保证各 rank 数据不同，同时种子本身可复现。

**练习 2**：`dist_print(..., once_in_node=True)` 末尾的 `dist.barrier()` 如果删掉，会有什么副作用？

**答案**：`barrier` 在这里有两个作用：一是保证「这一条打印在所有 rank 都完成之后，下一条才开始」，避免多行输出交错；二是隐式做一次 NCCL 同步，保证打印时前面异步的通信结果已就绪。删掉后输出顺序会乱（尤其是和性能测试的计时穿插时）。

---

### 4.2 test_ep.py 的整体结构：从命令行到 dispatch/combine

#### 4.2.1 概念说明

`test_ep.py` 是一个「大而全」的端到端测试：它既验证正确性（拿纯 NCCL/PyTorch 写的参考实现做**逐位**比对），又测性能（带宽与延迟）。要读懂它，关键是抓住一条主线：

```
__main__ (argparse)
   └─ torch.multiprocessing.spawn(test_loop, nprocs=num_processes)
        └─ test_loop(local_rank, ...)               ← 每张卡一个进程
             ├─ init_dist(...)                       ← 4.1 节的分布式初始化
             ├─ construct_elastic_buffer()           ← 构造 ElasticBuffer
             ├─ test_dispatch_combine(buffer, args)  ← 主测试逻辑
             └─ buffer.destroy() / dist.destroy_process_group()
```

`__main__` 里用 `torch.multiprocessing.spawn` 把 `test_loop` 复制成 `num_processes` 份（默认 8），每份绑到一张卡。这是单机多 GPU 跑分布式测试的标准套路。

#### 4.2.2 核心流程

`test_dispatch_combine`（真正干活的核心函数）大致分四步：

1. **配置打印**：从 `buffer` 读出逻辑域大小（`num_scaleout_ranks × num_scaleup_ranks`），算出 `num_sms` / `num_qps`（V2 解析式计算，详见 u3-l3），打印配置。
2. **造数据**：用 `get_unbalanced_scores` 造一组 MoE 路由分数，`torch.topk` 选出每个 token 的 top-k 专家，得到 `topk_idx` / `topk_weights`（u8-l4 会讲这个 gate 工具）。
3. **跑 dispatch + combine**：在 `enumerate_ep_modes()` 枚举的一大堆模式组合下（FP8/BF16、expand/非 expand、各种流配置…）反复 dispatch、combine。
4. **正确性 + 性能**：和参考实现逐位比对（`torch.equal`），并用 `bench_kineto` 测带宽，打印 `SO/SU 带宽 + 延迟`。

#### 4.2.3 源码精读

入口 `__main__` 的 `spawn`：

[tests/elastic/test_ep.py:564-609](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/tests/elastic/test_ep.py#L564-L609) —— argparse 定义所有命令行参数（默认 `--num-processes=8`、`--num-tokens=4096`、`--hidden=7168`、`--num-topk=6`、`--num-experts=256`），最后用 `torch.multiprocessing.spawn(test_loop, ..., nprocs=num_processes)` 把测试扇出到每张卡。

`test_loop` 是每个进程的「主循环」：

[tests/elastic/test_ep.py:521-545](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/tests/elastic/test_ep.py#L521-L545) —— `test_loop` 先 `init_dist` 拉起分布式环境，再用 `construct_elastic_buffer()` 造 buffer，最后调 `test_dispatch_combine(buffer, args)`。注意它带了 `@torch.inference_mode()` 装饰器，关掉 autograd 以省显存。

[tests/elastic/test_ep.py:561](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/tests/elastic/test_ep.py#L561) —— 测试结束 `dist.destroy_process_group()` 收尾，否则下次跑可能会因为端口/communicator 未释放报错。

`test_dispatch_combine` 的配置打印与数据构造：

[tests/elastic/test_ep.py:67-72](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/tests/elastic/test_ep.py#L67-L72) —— 打印配置：`Ranks: {scaleout} x {scaleup}`、`Experts: {topk}/{num_experts}`、`#SM`、`#QPs`。`once_in_node=True` 让每节点只打印一次，避免 8 份重复。

[tests/elastic/test_ep.py:75-77](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/tests/elastic/test_ep.py#L75-L77) —— 用 `get_unbalanced_scores` 造路由分数（可控的负载不均衡），`torch.topk` 选专家。`topk_idx` 转成 DeepEP 要求的 `deep_ep.topk_idx_t` 类型。

`enumerate_ep_modes` 是测试「覆盖面」的核心：

[tests/elastic/test_ep.py:22-31](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/tests/elastic/test_ep.py#L22-L31) —— 用生成器枚举各种模式组合：`do_handle_copy`、`expert_alignment(128/1)`、`use_fp8_dispatch`、`num_bias(0/1/2)`、`with_previous_event`、`async_with_compute_stream`、`allocate_on_comm_stream`。这就是为什么一次 `python test_ep.py` 会跑很久——它在大量排列组合下都做了正确性检查。

#### 4.2.4 代码实践（跟踪型）

1. **实践目标**：建立 `test_ep.py` 的全局心智图，能在屏幕输出和源码位置之间对上号。
2. **操作步骤**：
   - 先别跑，纯阅读：从 `__main__`（564 行）出发，沿 `spawn → test_loop → test_dispatch_combine` 走一遍。
   - 在 `test_dispatch_combine` 里找三处打印：第 67 行的 `Config:`、第 87 行的 `Testing with ...`、第 259 行的 `dispatch:` 性能行。
3. **需要观察的现象**：脑海里预演「屏幕上先出 Config，再出每个模式的 Testing with，最后出 dispatch/combine 性能行」，并知道每段对应源码的哪一行。
4. **预期结果**：你能不看源码说出 `Config` 里的 `#SM` 来自 `buffer.get_theoretical_num_sms(num_experts, num_topk)`（65 行），`#QPs` 来自 `buffer.get_theoretical_num_qps(num_sms)`（66 行）。
5. 这一节不需要 GPU，纯阅读即可完成。

#### 4.2.5 小练习与答案

**练习 1**：`test_loop` 上的 `@torch.inference_mode()` 装饰器有什么用？删掉会怎样？

**答案**：它在该进程整个生命周期关闭 autograd 记录，省掉反向图构建的开销和显存。删掉不会立刻报错（测试里没有反向），但会浪费显存，且可能与 `torch.empty` 的某些路径产生额外开销。DeepEP 自身的 dispatch/combine 是手写 CUDA kernel，并不依赖 autograd。

**练习 2**：默认 `--num-processes=8`，但你的机器只有 4 张卡。直接 `python test_ep.py` 会发生什么？怎么改？

**答案**：`spawn` 会尝试起 8 个进程，但 `torch.cuda.set_device(local_rank)` 在 local_rank≥4 时会因「无效 device ordinal」报错。改法：加参数 `--num-processes 4`。`init_dist` 会用 `num_local_ranks=4` 推算 `world_size=4`。

---

### 4.3 构造 ElasticBuffer 并运行一次 dispatch + combine

#### 4.3.1 概念说明

分布式环境就绪后，下一步是造一个 `ElasticBuffer`。它是 V2 唯一的 buffer 接口（u1-l1），负责：
- 在 NCCL Gin 后端上为每个 rank 分配**对称**的 GPU/CPU 内存窗口（u3-l4）；
- 自动解析出最优的 SM 数、QP 数（u3-l3）；
- 提供 `dispatch` / `combine` / `barrier` 等方法。

`test_ep.py` 的 `construct_elastic_buffer()` 是个非常好的「最小可用范例」。dispatch 与 combine 是一对互逆操作：
- **dispatch**：把本 rank 的 token 按 `topk_idx` 路由，发送到目标专家所在的 rank，返回接收到的 `recv_x`、路由元数据 `handle`（`EPHandle`）和一个 `event`。
- **combine**：用 dispatch 返回的 `handle` 里保存的路由信息，把专家算完的输出送回原 rank，并按 `topk_weights` 加权归约。

`handle`（`EPHandle`）是串起 dispatch 与 combine 的「信物」——它记下了每个收到的 token 来自哪个原 rank、原 token 全局索引等元数据。combine 完全依赖这些元数据来反向路由。本节只演示「怎么调」，handle 内部字段语义留到 u2-l3。

#### 4.3.2 核心流程

1. `init_dist` 拿到 `group`。
2. `ElasticBuffer(group, num_max_tokens_per_rank=..., hidden=..., ...)` 构造 buffer（这会触发对称内存分配与 JIT 编译，首次调用会慢）。
3. dispatch：`buffer.dispatch(x, topk_idx=..., topk_weights=..., num_experts=..., num_max_tokens_per_rank=..., ...)` → 得到 `(recv_x, recv_topk_idx, recv_topk_weights, handle, event)`。
4. （在真实 MoE 里：用 `handle.num_recv_tokens_per_expert_list` 做分组 GEMM，产出专家输出。）
5. combine：`buffer.combine(expert_output, handle=handle, topk_weights=..., ...)` → 得到 `(combined_x, combined_topk_weights, event)`。
6. 用完后 `buffer.destroy()`。

#### 4.3.3 源码精读

`construct_elastic_buffer` 的构造参数：

[tests/elastic/test_ep.py:523-534](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/tests/elastic/test_ep.py#L523-L534) —— 用 MoE 设置（`num_max_tokens_per_rank`、`hidden`）直接构造 `ElasticBuffer`，并传 `allow_hybrid_mode`、`allow_multiple_reduction`、`prefer_overlap_with_compute`、`deterministic`、超时时间等开关。`explicitly_destroy=True` 要求测试结束时显式释放，便于发现生命周期 bug。

dispatch 的调用：

[tests/elastic/test_ep.py:144-153](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/tests/elastic/test_ep.py#L144-L153) —— 把所有 dispatch 参数装进 `dispatch_args`（含 `num_sms`/`num_qps`/`expert_alignment`/`do_cpu_sync` 等），再交给 `launch()` 帮助函数实际调用，返回 `recv_x, recv_topk_idx, recv_topk_weights, handle, dispatch_event`。

combine 的调用：

[tests/elastic/test_ep.py:209-217](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/tests/elastic/test_ep.py#L209-L217) —— combine 复用 dispatch 返回的 `handle`，把本 rank 算好的专家输出（`input_for_combine`）按原路由推回，得到 `combined_x`。注意 combine 不需要再传 `topk_idx`，路由全靠 `handle`。

`launch` 这个薄包装的作用：

[tests/elastic/test_ep.py:34-41](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/tests/elastic/test_ep.py#L34-L41) —— `launch` 统一处理两件事：① 若 `with_previous_event`，先 `buffer.capture()` 抓一个事件作为 `previous_event`（建立前后依赖）；② 若 `async_with_compute_stream`，对返回的最后一个 `event` 调 `current_stream_wait()` 让计算流等通信完成。这俩开关的组合正是 u2-l4 要讲的「通信-计算重叠」。

#### 4.3.4 代码实践（运行型）

1. **实践目标**：在单机 8 卡上跑通一次 dispatch + combine，确认环境无误（这是后续所有讲义的前提）。
2. **操作步骤**：
   ```bash
   # 已 python setup.py install 之后
   python tests/elastic/test_ep.py --test-first-only --skip-perf-test
   ```
   - `--test-first-only`：只跑 `enumerate_ep_modes()` 的第一个组合（否则要跑很久）。
   - `--skip-perf-test`：跳过性能测试，只验证正确性，更快。
3. **需要观察的现象**：屏幕先打印 `Config:`、`Running all test cases:`，然后是若干 `Testing with ...` 行。若所有断言通过，程序正常退出且无 `AssertionError`。
4. **预期结果**：退出码 0，没有任何 `assert` 报错。首次运行时控制台可能出现 JIT 编译相关输出（首次编译会慢，之后命中缓存）。
5. 如果报 `NCCL ... version mismatch` 之类，说明 PyTorch 自带 NCCL 与 pip 装的 `nvidia-nccl-cu13` 不一致，可设 `EP_SUPPRESS_NCCL_CHECK=1` 临时绕过（但建议先对齐版本，见 u2-l1）。**待本地验证**：实际带宽取决于你的硬件，单机数字不会等于 README 的多节点数字。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `combine` 的参数里没有 `topk_idx`，但 dispatch 有？

**答案**：dispatch 需要知道「每个 token 要去哪个专家」才能路由，所以必须传 `topk_idx`。combine 是 dispatch 的逆过程，路由信息已经 baked 进 dispatch 返回的 `handle`（`EPHandle`）里，combine 直接复用即可，无需重复传。

**练习 2**：`construct_elastic_buffer` 里 `explicitly_destroy=True` 的意义是什么？

**答案**：它要求 buffer 必须被显式 `destroy()`，而不是依赖 Python GC。这样能在测试里尽早释放对称内存与 NCCL 资源，也便于发现「忘记销毁」导致的资源泄漏——若 `__del__` 被隐式触发，往往意味着生命周期管理有 bug。

---

### 4.4 解读输出：SO/SU 带宽、copy 带宽与延迟

#### 4.4.1 概念说明

跑通只是第一步，更重要的是看懂性能输出。一条典型的 dispatch 性能行长这样：

```
   * EP:   0/8 | dispatch: 0 GB/s (SO), 153 GB/s (SU), 42.310 us, 3984323 bytes | copy: 312 GB/s, 8.420 us
```

逐段拆解：

| 字段 | 含义 |
|--|--|
| `EP: 0/8` | 当前是 rank 0，共 8 个 rank |
| `dispatch:` | 这一行测的是 dispatch 内核 |
| `0 GB/s (SO)` | scaleout（RDMA）逻辑带宽。**单机时恒为 0**，因为没有跨节点流量 |
| `153 GB/s (SU)` | scaleup（NVLink）逻辑带宽，即节点内通信吞吐 |
| `42.310 us` | dispatch 主 kernel（`dispatch_impl`）的耗时 |
| `3984323 bytes` | SU 通道传输的总字节数 |
| `copy: 312 GB/s, 8.420 us` | `dispatch_copy_epilogue_impl` 这个 epilogue 内核（把 token 从 buffer 拷出到接收张量）的带宽与耗时 |

combine 行的格式几乎一样，只是把 `copy` 换成 `reduce`（对应 `combine_reduce_epilogue_impl`，做多 rank 加权归约）。

#### 4.4.2 核心流程

带宽是怎么算出来的？以 dispatch 为例：

1. 用 `count_bytes(recv_x, recv_topk_idx, recv_topk_weights)` 算出每个接收 token 的平均字节数 `num_bytes_per_dispatch_token`。
2. `num_scaleup_bytes = num_bytes_per_dispatch_token × num_scaleup_recv_tokens`（SU 字节数）。
3. `num_scaleout_bytes = num_bytes_per_dispatch_token × num_scaleout_send_tokens`（SO 字节数）。
4. 用 `bench_kineto` 测出 `dispatch_impl` 内核耗时 `t`。
5. 带宽 = 字节数 / 耗时，打印成 GB/s。

带宽（GB/s）与耗时（us）的换算：

\[
\text{bandwidth}(\text{GB/s}) = \frac{\text{bytes}}{t(\text{s}) \times 10^9}, \qquad t(\text{us}) = t(\text{s}) \times 10^6
\]

`bench_kineto` 的关键设计：它在每次测量前插一个 `torch.cuda._sleep(int(2e7))`（约 10ms 的大 kernel）+ 一个 `barrier`（默认 `dist.all_reduce`，这里用 `buffer.barrier`）。目的是**抹平各 rank CPU launch 不齐**带来的误差——通信内核对启动时机敏感，若各 rank launch 错峰，测出来的耗时会偏大。

#### 4.4.3 源码精读

dispatch 字节数的推导：

[tests/elastic/test_ep.py:253-255](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/tests/elastic/test_ep.py#L253-L255) —— `num_bytes_per_dispatch_token` 用 `safe_div(count_bytes(...), recv_topk_idx.size(0))` 算每 token 均摊字节；分别乘以 SU/SO 的 token 数得到两类字节数。`count_bytes` 会递归处理 FP8 的 `(data, scale)` 元组（u8-l4 详述）。

[tests/elastic/test_ep.py:241-245](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/tests/elastic/test_ep.py#L241-L245) —— 计算 SO 发送 token 数：遍历各 scaleout rank，若开启 `--ignore-local-traffic` 则跳过本节点。**单机时 `num_scaleout_ranks==1`，循环范围是 `range(0)`，所以 `num_scaleout_send_tokens=0`，SO 带宽必然是 0**。

dispatch 性能行打印：

[tests/elastic/test_ep.py:256-263](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/tests/elastic/test_ep.py#L256-L263) —— `bench_kineto` 测 `('dispatch_impl', 'dispatch_copy_epilogue_impl')` 两个 kernel，分别得到主 kernel 耗时 `t` 与 epilogue 耗时 `copy_t`；打印 `SO GB/s`、`SU GB/s`、`us`、`copy GB/s`。注意 `kernel_names` 是元组，`bench_kineto` 也返回元组 `(t, copy_t)`。

combine 性能行打印：

[tests/elastic/test_ep.py:339-346](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/tests/elastic/test_ep.py#L339-L346) —— combine 同样测 `('combine_impl', 'combine_reduce_epilogue_impl')`，但 epilogue 在这里叫 `reduce`，字节数包含 `bias`、归约读、归约写三部分。

`bench_kineto` 的 barrier 隔离设计：

[deep_ep/utils/testing.py:163-172](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/utils/testing.py#L163-L172) —— `barrier_comm_profiling` 模式下，每次 `fn()` 前先 `torch.cuda._sleep(int(2e7))` 占住约 10ms，再 `barrier()`（测试里传的是 `buffer.barrier`）对齐各 rank，消除 CPU launch 不均。这对「跨 rank 协作」的 EP kernel 计时至关重要。

> 关于 README 参考值对照：README 性能表用 **V3 配置**（8K tokens、hidden 7168、top-8、FP8 dispatch、BF16 combine）在 **SM90/CX7/EP 8×2** 上得到 `dispatch 90 GB/s (RDMA)`、`combine 81 GB/s (RDMA)`。注意这是**多节点 RDMA（SO）**数字。你在**单机 8 卡**上跑，SO 永远是 0，能对照的是 SM100 单节点那两行（`EP 8`，726/740 GB/s NVLink，64 SM）。所以单机实测要和 README 的「NVLink 行」比，而不是「RDMA 行」。

#### 4.4.4 代码实践（运行 + 解读，对应本讲核心实践任务）

1. **实践目标**：亲手改命令行参数，跑出 dispatch/combine 的 SO/SU 带宽，并与 README 参考值对照（这是本讲规格指定的实践）。
2. **操作步骤**（单机 8 卡）：
   ```bash
   # 默认配置（4096 tokens, hidden 7168, top-6, 256 experts），只跑首个模式 + 性能测试
   python tests/elastic/test_ep.py --test-first-only --skip-check

   # 改成更接近 README V3 的规模（更大 batch）
   python tests/elastic/test_ep.py --test-first-only --skip-check \
       --num-tokens 8192 --num-topk 8

   # 试试小 batch，观察带宽是否下降
   python tests/elastic/test_ep.py --test-first-only --skip-check --num-tokens 512
   ```
   - `--skip-check`：跳过正确性比对，专注性能（注意 `--dump-profile-traces` 与 `--skip-perf-test` 互斥，见 233 行）。
3. **需要观察的现象**：记录每组的 `dispatch` 与 `combine` 行里的 `(SU)` 带宽和 `us` 延迟。重点关注：
   - 单机下 `(SO)` 是否恒为 0；
   - batch 从 512 → 4096 → 8192 时，SU 带宽是上升还是饱和（NVLink 物理带宽有上限）；
   - copy/reduce 带宽随 batch 的变化。
4. **预期结果**：
   - 单机 `(SO)` 全部为 `0 GB/s`（因为 `num_scaleout_ranks==1`，源码 241~245 行）；
   - batch 越大，SU 带宽越接近 NVLink 物理上限（Hopper 8 卡 NVLink 单向逻辑带宽量级在数百 GB/s），小 batch 因 launch 开销占比大而带宽偏低；
   - 把单机 SU 实测值与 README「SM100 EP 8」的 NVLink 行（726/740 GB/s）做量级比较；若你的卡是 SM90 单节点，README 没给单节点 NVLink 数字，可参考 u1-l1 的性能表说明。
5. **多节点才有 RDMA 数字**：若想看到非零的 `(SO)` 带宽并与 README 的 `EP 8×2 / 8×4` 行（90/61 GB/s RDMA）对照，需要双节点或四节点集群，并按 4.1.5 的方法设 `MASTER_ADDR/RANK/WORLD_SIZE`。**待本地验证**：具体数字取决于 NIC 型号与网络拓扑。

#### 4.4.5 小练习与答案

**练习 1**：在单机 8 卡上，dispatch 行的 `(SO)` 永远是 `0 GB/s`，是 bug 吗？

**答案**：不是。SO（scaleout）对应跨节点 RDMA 流量，单机时 `num_scaleout_ranks==1`，计算 SO 发送 token 数的循环范围是 `range(num_scaleout_ranks if num_scaleout_ranks > 1 else 0)` 即 `range(0)`（241~245 行），所以 `num_scaleout_bytes=0`，带宽自然为 0。要看非零 SO 必须多节点。

**练习 2**：`bench_kineto` 为什么要在每次测量前插一个 `torch.cuda._sleep(int(2e7))` + `barrier`？

**答案**：EP 的 dispatch/combine 是跨 rank 协作 kernel，对「各 rank 是否同时 launch」非常敏感。如果各 rank 的 CPU launch 时机错峰，通信内核会在「等队友」上空耗，测出来的耗时偏大、带宽偏低。`_sleep` 先用一个长 kernel 把 GPU 占住，再用 `barrier` 把所有 rank 对齐到同一个起点，再 launch 被测函数，从而抹平 launch 不均的误差。

**练习 3**：输出里的 `copy:` 和 `reduce:` 分别对应哪两个 CUDA kernel？

**答案**：dispatch 的 `copy:` 对应 `dispatch_copy_epilogue_impl`（把 buffer 里的 token 拷出到 `recv_x` 等接收张量，见 256~258 行）；combine 的 `reduce:` 对应 `combine_reduce_epilogue_impl`（对多 rank 收到的 token 做加权归约并叠加 bias，见 339~341 行）。这两个 epilogue 内核的设计动机留到 u5-l3 / u6-l2。

---

## 5. 综合实践

把 4.1~4.4 串起来，做一个「自定义规模 + 解读报告」的小任务。

**任务**：在单机 8 卡上，对比三组配置的 dispatch/combine 性能，写一份简短的结论。

1. **环境检查**：确认 `python -c "import deep_ep"` 不报错；`nvidia-smi` 看到 8 张 Hopper 卡。
2. **跑三组配置**（都用 `--test-first-only --skip-check` 只看性能）：
   - 小 batch：`--num-tokens 512 --num-topk 4`
   - 默认 batch：`--num-tokens 4096 --num-topk 6`
   - 大 batch：`--num-tokens 8192 --num-topk 8`
3. **记录**：每组各取一条 `dispatch` 行和 `combine` 行，记下 `(SU)` 带宽和 `us` 延迟，填入下表（示例表头）：

   | 配置 | dispatch SU (GB/s) | dispatch us | combine SU (GB/s) | combine us |
   |--|--|--|--|--|
   | 512 tokens, top-4 | | | | |
   | 4096 tokens, top-6 | | | | |
   | 8192 tokens, top-8 | | | | |

4. **分析**：用 4.4 学到的方法解释——为什么大 batch 的 SU 带宽更高？（提示：固定 launch 开销被更多 token 摊薄，更接近带宽受限而非延迟受限。）
5. **对照 README**：把大 batch 的 SU 数字与 README「SM100 EP 8」的 NVLink 行做量级比较；说明为何你的数字不会等于 README 的「EP 8×2」RDMA 行（90 GB/s）。如果偏差较大，思考可能原因（卡型号不同、不是 SM100、NVLink 拓扑、JIT 首次编译未热身等）。

**交付物**：一张填好的表 + 3~5 句结论。这个练习直接呼应本讲「跑通 + 看懂」的核心目标，也是后续 u2（Python 接口）、u3（拓扑与布局）、u5/u6（内核深入）所有性能讨论的基线。

## 6. 本讲小结

- `init_dist`（[envs.py:73-113](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/utils/envs.py#L73-L113)）通过环境变量 `MASTER_ADDR/PORT/WORLD_SIZE/RANK` 建立 NCCL 集群，`init_seed` 给每个 rank 派独立种子，`dist_print` 用 `once_in_node` + `barrier` 控制多进程打印。README 提到的 `tests/utils/envs.py` 是笔误，真实位置是 `deep_ep/utils/envs.py`。
- `test_ep.py` 主线是 `__main__` → `torch.multiprocessing.spawn(test_loop, nprocs=8)` → 每个 rank 先 `init_dist`、再造 `ElasticBuffer`、再跑 `test_dispatch_combine`，最后 `destroy`。
- `construct_elastic_buffer()`（[test_ep.py:523-534](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/tests/elastic/test_ep.py#L523-L534)）是最小可用的 buffer 构造范例；dispatch 返回的 `handle`（`EPHandle`）承载路由元数据，combine 完全依赖它做反向路由。
- 性能输出里 `SO` = scaleout（RDMA，跨节点）、`SU` = scaleup（NVLink，节点内）。**单机时 SO 恒为 0**，只有多节点才能看到非零 RDMA 带宽。
- 带宽 = 字节数 / 内核耗时，由 `bench_kineto`（[testing.py:163-172](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/utils/testing.py#L163-L172)）用 `_sleep` + `barrier` 抹平 launch 不均后测得；`copy`/`reduce` 分别对应 dispatch/combine 的 epilogue 内核。
- 对照 README 性能表时要注意拓扑匹配：单机 8 卡对照 NVLink 行，多节点对照 RDMA 行；且 README 数字是「逻辑带宽」。

## 7. 下一步学习建议

本讲把 DeepEP 「跑起来了」，但很多细节是黑盒。接下来：

- **u2-l1（import deep_ep 背后）**：本讲你 `import deep_ep` 时其实自动跑了 `check_nccl_so` 和 `init_jit`，下一讲解释这两个隐藏步骤，以及为何 NCCL 版本不一致会出问题。
- **u2-l2（ElasticBuffer 构造）**：本讲只用了 `construct_elastic_buffer` 的参数列表，下一讲深入讲两种尺寸指定方式、`allow_hybrid_mode` 等开关的语义和拓扑属性。
- **u3-l1（拓扑域）**：本讲的 SO/SU 就是逻辑域概念，下一讲正式区分物理域（RDMA/NVLink）与逻辑域（scaleout/scaleup）。
- **想直接看内核**：可以跳到 u5-l1（dispatch 内核）或 u8-4（测试与基准体系，详细讲 `bench_kineto` 与 `refs.py` 参考实现），但建议先过一遍 u2 的 Python 接口层。

建议继续精读的源码：`tests/elastic/test_ep.py`（本讲只读了主线，`enumerate_ep_modes` 各分支的正确性断言值得细读）、`deep_ep/utils/envs.py`、`deep_ep/utils/testing.py`。
