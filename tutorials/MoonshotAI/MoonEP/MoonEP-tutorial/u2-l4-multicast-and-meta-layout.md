# u2-l4 组播视图与 meta_buf 共享布局

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清 NVSwitch 组播（multicast / multimem）是什么：一次写、硬件复制到所有 rank 的同一偏移，以及 MoonEP 在哪里用到它。
2. 掌握组播视图的「创建 → 导入 → 添加设备 → 绑定映射」四步流程，能指出每一步由哪些 rank 参与、两个 `dist.barrier` 分别夹在哪两步之间。
3. 独立推导 `_create_context` 中 meta_buf 的全部七段（WEIGHTS / TPE / PLAN / TOPK0 / ORDER+ORDER0 / BARRIER / SRC_INFO）偏移公式，理解 16 字节对齐与 padding 的来源。
4. 理解 meta_buf 的物理 chunk 为什么必须同时满足 VMM 粒度与组播粒度的双重对齐，以及两条 int32 溢出断言分别保护什么。
5. 完成一个不依赖 GPU 的布局计算实践，并在有 GPU 的环境下与真实 `Buffer` 的 ctx 对照验证。

## 2. 前置知识

本讲建立在 u2-l1 ~ u2-l3 三讲之上，先用两段话把需要的背景补齐。

**对称内存（承接 u2-l2）**。MoonEP 用 CUDA VMM 的「分配-映射两步模型」为每类通信缓冲构建对称内存：每个 rank 用 `cuMemCreate` 分配一块物理显存（chunk），再把全组 R 块 chunk 按 rank 顺序映射进每个进程的虚拟地址空间，得到一个布局完全一致的大张量。于是「第 r 段」物理上就驻留在 rank r 的 GPU 上，内核可以直接经 NVLink 读写远端段。符号系统（S/K/E/R/B/NvS、`epn = E/R`）沿用 u2-l1。

**句柄分发（承接 u2-l3）**。跨进程共享任何 VMM 对象都要先分发它的 shareable 句柄：同节点用 unix datagram socket 的 `SCM_RIGHTS` 传 fd，跨节点用 64 字节 fabric handle。`_exchange_ipc_fds` 支持「子集发送者」——只有列表里的 rank 发送，其余 rank 只收。本讲的组播对象句柄恰好复用这套机制。

还需要一个新概念：**NVSwitch 组播（NVSwitch SHARP / multimem）**。普通 NVLink 写是点对点的：写远端地址，只影响一个 GPU 上的一个物理位置。组播则把 R 块物理内存「捆绑」进一个组播对象，再为它映射一个特殊的虚拟地址（下称 mc 地址）。向 mc 地址的偏移 `off` 写一个值，NVSwitch 硬件会把这个写操作复制到**所有** rank 的 chunk 的同一偏移 `off` 上——一次存储指令，R 份数据。它在 CUDA 驱动层对应 `cuMulticastCreate` / `cuMulticastBindMem` 等接口，在 PTX 层对应 `multimem.st` 指令。「粒度（granularity）」指这类操作对地址和大小的对齐要求，类比 u2-l2 讲过的 VMM 粒度。

最后回顾一个软件工程事实：MoonEP 的规划（planning）是「rank 0 算、全员用」的模式——rank 0 汇总全组路由统计并算出通信计划，所有 rank 都要拿到同一份结果。这正是组播的用武之地。

## 3. 本讲源码地图

| 文件 | 本讲关注点 |
| --- | --- |
| `moonep/api.py` | `_create_context`：meta_buf 七段偏移计算、双重对齐、int32 溢出断言、`meta_buf` + `meta_mc` 的分配与构造期清零 |
| `moonep/buffer.py` | `create_nvl_dist_multicast_tensor` / `_create_nvl_multicast_view`：组播视图的四步创建流程 |
| `csrc/nvl_shared_buffer.cuh` | 组播驱动 API 的 C++ 封装：`nvl_multicast_create/import/add_device/bind_map` 与粒度查询 |
| `moonep/planning.py` | `meta_mc` 的唯一消费者：`multimem_st_v4` PTX 助手与规划结果的组播发布循环 |

阅读建议：先读 4.1 建立「谁在什么时机用组播」的全景，再读 4.2 的四步流程，最后读 4.3/4.4 的布局与对齐——这个顺序正好是数据流自上而下落到内存字节的顺序。

## 4. 核心概念与源码讲解

### 4.1 multimem 组播：一次写、全 rank 收

#### 4.1.1 概念说明

MoonEP 的在线规划器由 rank 0 主导：它把各 rank 的 `tokens_per_expert` 汇聚到自己分段里，算出均衡所需的迁移矩阵，最终生成一批**所有 rank 都需要**的数据——最典型的是三张 `[R, E]` 形状的矩阵（alloc 分配、tpe 累计、专家段偏移），每个 rank 都要按「行 = rank」切走自己那一行。

如果没有组播，rank 0 只有两种办法发布这份数据：

- 逐 rank 远端写：对每个目的 rank 循环一次，写 R−1 遍，NVLink 流量与 rank 数成正比；
- 集合通信：把数据搬回宿主或走 NCCL，破坏「一切通信都在设备端对称内存上完成」的设计。

组播提供了第三条路：rank 0 把数据写进 mc 地址一次，NVSwitch 在硬件层把写操作扇出到所有 rank 的 chunk。流量不随 R 增长，且完全在设备端完成。注意代价上的细节：组播写本身仍是 R 份物理写入（硬件复制），省的是**发起侧的指令数与 NVLink 上的重复遍历**，发布延迟近似常数而非随 R 线性增长。

关键约束：mc 地址是「单 chunk 视图」——它只覆盖一块 chunk 的大小，`mc[off]` 命中每个 rank chunk 的 `off`。所以组播发布的偏移量必须落在每个 rank 分段内**相同**的位置，这就要求所有分段布局同构（4.3 节的对称布局正是为此）。

#### 4.1.2 核心流程

规划内核中组播发布的时序（简化）：

```text
rank 0（所有其他 rank 也在跑同一内核，只是分工不同）
  1. Phase A：各 rank 把自己的 tpe 份额远端写进 rank0 分段的 TPE 区
  2. cross_rank_barrier：等 rank0 收齐全组的 tpe
  3. rank0 在自己的 PLAN 暂存区算出 alloc / tpe_cumsum / expert_offsets
  4. 广播循环：读自己暂存区的 3*E*R 个 int32，
     以 4 个 int32（128 位）为一组 multimem.st 到 mc 地址的 PLAN_OFF 偏移
     —— NVSwitch 硬件把每个写复制到所有 rank 分段的同一偏移
  5. 之后各 rank 从「自己分段的 PLAN 副本」按 rank 行切片读取
```

发布的数据量恰好是 \(3 \times E \times R\) 个 int32：PLAN 暂存区的前三个 `[R, E]` 子矩阵（ALLOC、TPE、EOFF）。之所以能一次广播，是因为这三个矩阵是「全员同构」数据；而 cu_seqlens、zero_fill、experts_to_copy 等只属于某个 rank 的切片，则不走组播，由各 rank 用 G2S 批量拷贝从 rank0 暂存区取走（Phase D，详见 u3-l6）。

#### 4.1.3 源码精读

先看 PTX 助手。[moonep/planning.py:148-158](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/planning.py#L148-L158) 用 `llvm.inline_asm` 封装了 `multimem.st.relaxed.sys.global.v4.f32`：一次写 4 个 fp32（这里实际承载 int32 位型），`sys` 作用域表示可见性覆盖整个系统（跨 GPU），`relaxed` 表示这条写不附带额外的内存顺序约束——顺序由随后的显式屏障保证（本讲不展开，u3-l6 讲屏障）。与它对照的普通远端写助手 `st_global_v4_s32`（[moonep/planning.py:135-145](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/planning.py#L135-L145)）只能写一个确定地址。

再看发布循环。[moonep/planning.py:954-960](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/planning.py#L954-L960) 中，`nb = 3 * E * R` 是广播元素数，`nvec = nb // 4` 是 128 位向量数；循环体从 rank0 自己分段的 `meta[PB + i*4 + k]` 读出暂存值，然后计算 `mc.iterator + (PLAN_OFF + i*4)` 的地址并调用 `multimem_st_v4`——这一步就是「写一次、全 rank 收」。

PLAN 暂存区的内部结构由子偏移常量定义。[moonep/planning.py:546-553](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/planning.py#L546-L553) 给出：`ALLOC_SUB = 0`、`TPE_SUB = E*R`、`EOFF_SUB = 2*E*R`、`CU_SUB = 3*E*R`——前三个子区（共 `3*E*R` 元素）正是组播覆盖的范围；之后是 `ZFR_SUB`、`ETC_SUB`、`STATS_SUB` 等 per-rank 切片暂存。rank0 在 Phase B/C 中把这些子区建成 `[R, ...]` 形状的张量视图并逐 `dest_rank` 填充，见 [moonep/planning.py:799-822](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/planning.py#L799-L822)，整段逻辑被 [moonep/planning.py:610](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/planning.py#L610) 的 `if rank == 0:` 守卫——只有 rank0 写暂存区。

`meta_mc` 的传递链：[moonep/api.py:362-364](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L362-L364) 在构造期与 `meta_buf` 一起创建；存入 `ctx['meta_mc']`（[moonep/api.py:410](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L410)）；启动规划内核时作为指针参数传入（[moonep/planning.py:1182](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/planning.py#L1182)）。全仓库搜索可以确认：planning 是 `meta_mc` 的唯一消费者。

#### 4.1.4 代码实践

**实践目标**：不运行任何代码，纯靠阅读，把 `meta_mc` 从 Python 字典追踪到 PTX 指令，画出这条数据通路。

**操作步骤**：

1. 打开 [moonep/api.py:362-364](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L362-L364)，确认 `meta_mc` 是 `create_nvl_dist_multicast_tensor` 的第二个返回值。
2. 在 `moonep/` 下搜索 `meta_mc`，记录每个命中点及其角色（创建 / 存储 / 传参 / 释放）。
3. 打开 [moonep/planning.py:148-158](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/planning.py#L148-L158)，抄下 PTX 指令的完整拼写，逐个词标注含义（`multimem` / `.st` / `.relaxed` / `.sys` / `.v4` / `.f32`）。
4. 打开 [moonep/planning.py:954-960](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/planning.py#L954-L960)，写出循环变量 `i` 的取值范围（`0 <= i < 3*E*R/4`）与每个 `i` 覆盖的字节区间。

**需要观察的现象**：通路应该收敛为一条链——`ctx['meta_mc']` → launch 参数 → 内核形参 `mc` → `mc.iterator + PLAN_OFF + i*4` → `multimem.st`。

**预期结果**：你会在纸上得到类似下面的表（以 `mc` 为内核形参名）：

| 环节 | 位置 | 作用 |
| --- | --- | --- |
| 创建 | api.py `_create_context` | 组播视图分配 |
| 存储 | `ctx['meta_mc']` | 与 meta_buf 同生命周期 |
| 传参 | planning.py `p16(ctx['meta_mc'])` | 16 字节对齐指针 |
| 消费 | `mc.iterator + (PLAN_OFF + i*4)` | 广播地址 |
| 指令 | `multimem.st.relaxed.sys.global.v4.f32` | 硬件扇出写 |

若任何一环找不到对应源码行，回到 4.1.3 的链接逐个核对（本实践为源码阅读型，无需 GPU，结论「待本地验证」仅指你没有实际运行内核）。

#### 4.1.5 小练习与答案

**练习 1**：如果把组播换成普通远端写，rank0 发布 `3*E*R` 个元素需要多少次写？组播后呢？

**答案**：普通远端写需要对 R−1 个目的 rank 各写一遍，共 `(R-1) * 3*E*R` 个元素写（组播对象自己也覆盖 rank0 的 chunk，但 rank0 本地写不走 NVLink）；组播后只需 `3*E*R` 个元素写，每个写由 NVSwitch 硬件复制到全组，发起侧指令数与 R 无关。

**练习 2**：`multimem_st_v4` 每次写 4 个 int32，为什么 `broadcast_elems` 必须能被 4 整除？这条约束在哪里检查？

**答案**：`v4` 是 128 位向量写，一次消费 4 个 32 位元素，广播循环以 `nvec = nb // 4` 计数，若 `3*E*R` 不是 4 的倍数就会漏掉尾部元素。约束在 [moonep/api.py:311-314](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L311-L314) 显式断言 `broadcast_elems % 4 == 0`。

**练习 3**：为什么 mc 视图是「单 chunk 大小」而不是像 `meta_buf` 那样覆盖 R 个 chunk？

**答案**：组播地址的语义是「同一偏移扇出到所有成员的物理内存」，逻辑上只有一份布局；给它 R 倍大小会造成语义歧义（第 r 段的偏移该扇出到哪里）。[moonep/buffer.py:296-297](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/buffer.py#L296-L297) 明确说明：写入 `mc[off]` 命中每个 rank chunk 的 `[off]`。

### 4.2 组播视图创建：create → import → add_device → bind_map

#### 4.2.1 概念说明

组播对象是驱动层的容器，它本身不拥有内存，而是把「每个 rank 各自 `cuMemCreate` 出来的那块 chunk 物理内存」绑定到一起，再提供一个可映射的 mc 地址。要在 R 个进程上完成这件事，必须有人创建对象、其他人导入、每个进程把自己的设备加进组、最后各自绑定自己的物理内存并映射 mc 地址——这就是四步。

为什么创建流程这么讲究顺序？C++ 侧的设计注释写得很直白：**注册是一次性的、持久持有的**，且各步之间必须用 `dist.barrier` 分隔，否则 `cuMulticastBindMem` 会以 `CUDA_ERROR_ILLEGAL_STATE` 失败或直接挂起。这是典型的「分布式资源协商」问题：组播对象的成员资格是全组性质，任何一步的「部分完成」状态对其余 rank 都不可见，只能用屏障切割出全局一致的阶段。

另一个概念是 `owned_handle`：u2-l2 提过 `nvl_dist_alloc` 会返回三个东西，其中 owned handle 是本进程所分配物理内存的持有凭证。普通张量映射完成后就释放它，但组播绑定 `cuMulticastBindMem` 恰恰需要这个凭证，所以 `create_nvl_dist_multicast_tensor` 会把它留到 mc 视图也建完才释放。

#### 4.2.2 核心流程

```text
所有 rank：nvl_dist_alloc 分配自己的 chunk（得到 keepalive/shareable/owned_handle）
所有 rank：_map_nvl_dist_tensor —— 普通 R 段对称映射（u2-l2 内容）得到 meta_buf
─────────────────────────────────────────────
rank 0：  nvl_multicast_create(size_bytes, R)
          └─ 创建组播对象（size 向上对齐到组播粒度）并导出 shareable 句柄
rank 0 → 其他 rank：分发句柄
          ├─ fabric 模式：broadcast_shareable（64 字节 handle 走 broadcast）
          └─ fd 模式：_exchange_ipc_fds(sender_ranks=[0])（SCM_RIGHTS）
其他 rank：nvl_multicast_import(handle) 得到本进程的 mc_handle
─────────────────────────────────────────────  dist.barrier
所有 rank：nvl_multicast_add_device(mc_handle)   把自己的 GPU 加入组播组
─────────────────────────────────────────────  dist.barrier
所有 rank：nvl_multicast_bind_map(mc_handle, owned_handle, size_bytes, R)
          └─ cuMulticastBindMem：把自己的 chunk 物理内存绑到组播对象偏移 0
          └─ cuMemAddressReserve + cuMemMap + cuMemSetAccess：映射出 mc VA
─────────────────────────────────────────────  dist.barrier
```

#### 4.2.3 源码精读

Python 入口是 [moonep/buffer.py:244-274](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/buffer.py#L244-L274) 的 `create_nvl_dist_multicast_tensor`：先 `nvl_dist_alloc` + `_map_nvl_dist_tensor` 建好普通对称映射 `full_tensor`，再调 `_create_nvl_multicast_view` 把组播视图「叠加」上去，最后在 `finally` 里释放 owned handle——docstring 明确说这是为了让 `cuMulticastBindMem` 能用上它才延迟到此刻释放。

四步流程的本体在 [moonep/buffer.py:277-335](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/buffer.py#L277-L335) 的 `_create_nvl_multicast_view`：

- [moonep/buffer.py:299-309](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/buffer.py#L299-L309)：先断言设备支持组播；`chunk_elems = meta_buf.numel() // world_size` 算出单 chunk 元素数（对 meta_buf 就是 `meta_chunk_padded`），乘 4 得 `size_bytes`。只有 rank 0 执行 `nvl_multicast_create`。
- [moonep/buffer.py:311-326](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/buffer.py#L311-L326)：句柄分发——fabric 模式走 `_broadcast_shareable`（u2-l3），fd 模式走 `_exchange_ipc_fds` 且 `sender_ranks=[0]`（u2-l3 的子集发送者模式）；非 root 用 `nvl_multicast_import` 换取本进程的 `mc_handle`。
- [moonep/buffer.py:328-335](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/buffer.py#L328-L335)：**所有 rank** 调 `nvl_multicast_add_device`，然后 `dist.barrier`；再**所有 rank** 调 `nvl_multicast_bind_map`，再一个 `dist.barrier`。两个屏障的位置与 C++ 注释里的顺序图一一对应。

C++ 侧逐个看（文件为 [csrc/nvl_shared_buffer.cuh](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/csrc/nvl_shared_buffer.cuh)）：

- [csrc/nvl_shared_buffer.cuh:358-380](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/csrc/nvl_shared_buffer.cuh#L358-L380)：设计总注释——组播映射叠加在已分配的 NVLink chunk 上、不占额外显存、生命周期与 meta_buf 相同，并给出必须由屏障分隔的注册顺序。
- [csrc/nvl_shared_buffer.cuh:432-452](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/csrc/nvl_shared_buffer.cuh#L432-L452) `nvl_multicast_create`：把 `size_bytes` 向上对齐到组播粒度后填进 `CUmulticastObjectProp`，`cuMulticastCreate` 建对象并导出 shareable 句柄。
- [csrc/nvl_shared_buffer.cuh:456-463](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/csrc/nvl_shared_buffer.cuh#L456-L463) `nvl_multicast_import`：非 root 从句柄导入，本质是 u2-l2 的 `nvl_import_shareable`。
- [csrc/nvl_shared_buffer.cuh:465-474](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/csrc/nvl_shared_buffer.cuh#L465-L474) `nvl_multicast_add_device`：`cuMulticastAddDevice`，注释强调必须在 bind 之前、且所有 rank 都加完（屏障）才能 bind。
- [csrc/nvl_shared_buffer.cuh:482-526](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/csrc/nvl_shared_buffer.cuh#L482-L526) `nvl_multicast_bind_map`：先检查 `size_bytes % gran == 0`（错误信息直接说 "pad meta_buf chunk accordingly"——4.4 节的双重对齐就源自这里）；然后 `cuMulticastBindMem(mc, 0, mem, 0, size, 0)` 把本 rank 的 owned 物理内存绑到组播对象偏移 0；再 Reserve/Map/SetAccess 映射出 mc VA；返回一个 keepalive 张量，其 deleter 在析构时 unmap、释放 VA、release 组播句柄。注释还记录了一个平台细节：用 BindMem 而非 BindAddr，因为后者对 IPC 导入的 VA 会返回 invalid argument。

这些函数经 [csrc/bindings.cu:29-49](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/csrc/bindings.cu#L29-L49) 的 pybind11 定义导出为 `moonep._C` 的成员，Python 侧的导入见 [moonep/buffer.py:10-23](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/buffer.py#L10-L23)。

#### 4.2.4 代码实践

**实践目标**：用静态阅读画出 root / 非 root 在 fd 与 fabric 两种模式下各自调用的 `_C` 函数序列，验证两种模式的分岔点与汇合点。

**操作步骤**：

1. 通读 [moonep/buffer.py:277-335](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/buffer.py#L277-L335)，为每行涉及 `nvl_multicast_*` / `dist.barrier` / `_exchange_ipc_fds` / `_broadcast_shareable` 的代码打上标签：`root-only`、`non-root-only` 或 `all`。
2. 写一个纯 Python 脚本（不 import moonep），用两个列表分别模拟 root 和 non-root 的事件流，输出文本时序图。示例骨架（示例代码，非项目原有）：

   ```python
   events = [
       ("root",      "nvl_multicast_create(size_bytes, R)"),
       ("root->all", "handle 分发（fd: SCM_RIGHTS / fabric: broadcast）"),
       ("non-root",  "nvl_multicast_import(handle)"),
       ("all",       "nvl_multicast_add_device(mc_handle)"),
       ("all",       "dist.barrier  # add 完成 <-> bind 开始"),
       ("all",       "nvl_multicast_bind_map(mc_handle, owned_handle, ...)"),
       ("all",       "dist.barrier  # bind 完成"),
   ]
   for who, what in events:
       print(f"{who:<10} {what}")
   ```

3. 对照 [csrc/nvl_shared_buffer.cuh:373-380](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/csrc/nvl_shared_buffer.cuh#L373-L380) 的官方顺序注释，逐行核对。

**需要观察的现象**：两种句柄模式的差异只出现在「分发 + import」这一小段；`add_device` 之后两条路径完全汇合。

**预期结果**：`add_device` 前恰好一个屏障、`bind_map` 后恰好一个屏障，与 C++ 注释一致。脚本本身不依赖 GPU（时序来自源码，运行结果「待本地验证」仅在你想在真实集群上复现四步时适用）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `add_device` 与 `bind_map` 之间必须有 `dist.barrier`？去掉会发生什么？

**答案**：bind 要求组播对象已经包含全组设备。若某个 rank 还没 `add_device` 而另一个 rank 已开始 bind，驱动的成员状态不完整，`cuMulticastBindMem` 会失败或挂起——[csrc/nvl_shared_buffer.cuh:373-374](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/csrc/nvl_shared_buffer.cuh#L373-L374) 与 [csrc/nvl_shared_buffer.cuh:465-466](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/csrc/nvl_shared_buffer.cuh#L465-L466) 都明确写了这一点。

**练习 2**：`bind_map` 为什么用 `cuMulticastBindMem`（owned handle）而不是 `cuMulticastBindAddr`？

**答案**：[csrc/nvl_shared_buffer.cuh:479-481](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/csrc/nvl_shared_buffer.cuh#L479-L481) 注释记录：BindAddr 对 IPC 导入的虚拟地址在本平台返回 invalid argument，所以走物理内存句柄绑定。这也是 `nvl_dist_alloc` 的 owned handle 必须活到此刻的原因。

**练习 3**：`mc_view` 和 `meta_buf` 的 `data_ptr()` 相同吗？各自的释放顺序是什么？

**答案**：不同。两者是两套虚拟地址（普通 R 段映射 vs 单段组播映射），指向同一批物理内存。`Buffer.destroy()` 按先视图后 owner 的顺序释放：`meta_mc` 排在 `meta_buf` 之前（[moonep/api.py:558-576](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L558-L576)），张量 deleter 各自负责 unmap 与句柄释放。

### 4.3 meta_buf 的七段布局与偏移计算

#### 4.3.1 概念说明

`meta_buf` 是 MoonEP 的「元数据总线」：dispatch/combine 传输的 hidden 数据放在独立的 `hidden_buf`，而所有**控制平面**数据——路由权重、规划中间量、通信计划、排序 scratch、跨 rank 屏障、槽位溯源——全部挤在这一根 int32 大缓冲里。为什么合并成一根？因为每个区段单独分配就要单独走一遍 VMM 分配-映射-组播的四步协商（R 个进程 × 多次屏障），合并后只需一次；而且对齐浪费也只发生一次（尾部 padding）。

每个 rank 分到的那一份叫 chunk，长度为 `meta_chunk_padded` 个 int32（4.4 节讲 padded 的来历）。chunk 内从低到高依次是七个区段，布局对每个 rank 完全同构——这是对称内存（同构映射）与组播（同偏移扇出）的共同前提：

```text
一个 rank 的 chunk（int32 元素为单位）：
 ┌──────────────┬────────────┬──────────────┬───────┬───────┬────────┬─────────┬─────────┬──────────┐
 │ WEIGHTS      │ (pad to 4) │ TPE          │ PLAN  │ TOPK0 │ ORDER  │ ORDER0  │ BARRIER │ SRC_INFO │
 │ NvS 个 fp32  │            │ R*E          │ 见下  │ N4     │ N4      │ N4      │ 3       │ NvS      │
 └──────────────┴────────────┴──────────────┴───────┴───────┴────────┴─────────┴─────────┴──────────┘
 0            NvS      TPE_OFF            PLAN_OFF                            BARRIER_OFF SRC_INFO_OFF
```

各区段职责（依据 `_create_context` 的 docstring）：

| 区段 | 长度 | 谁写 | 谁读 | 内容 |
| --- | --- | --- | --- | --- |
| WEIGHTS | NvS | dispatch/combine | 用户、combine | 路由权重，fp32 位型别名成 int32 存放 |
| TPE | R*E | 各 rank 写自己份额到 rank0 段 | rank0（规划） | tokens_per_expert 汇聚区；**对称保留**——每个 rank 分段里都有同样区域，但通常只有 rank0 的副本被当汇聚目标 |
| PLAN | planning_out_elems | rank0 暂存 + 组播广播 | 全员 | 规划暂存/发布区，内部再切 ALLOC/TPE_CUM/EOFF/CU/ZFR/ETC/STATS 子区 |
| TOPK0 | N4 | rank0 远端拷入 | rank1 | rank0 的 topk 卸载 scratch（rank1 代跑一次 c1 排序） |
| ORDER / ORDER0 | 各 N4 | c1 排序 | c2/passB | token 全局序 scratch（ORDER0 是 rank1 为 rank0 产的副本） |
| BARRIER | 3 | 跨 rank 屏障原语 | 同左 | 2 个相位信号 + 1 个相位/符号计数器，自复位 |
| SRC_INFO | NvS | passB 发布 | dispatch 去重构建 | 槽位溯源：非负值编码 `src_rank * NvS + offv`，`-1` 是空槽哨兵 |

TOPK0/ORDER/ORDER0 属于规划内部 scratch，本讲只需知道它们占多宽；算法细节留给第 3 单元。

#### 4.3.2 核心流程

偏移是一条首尾相接的链，每个新区段从前一段末尾向上对齐到 4 个 int32（= 16 字节，向量化的前提）开始：

\[
\begin{aligned}
N &= S \times K \\
\textit{epn} &= E / R \\
\textit{NvS} &= N + (\textit{token\_padding} - 1) \times 2 \times \textit{epn} \\
\textit{WEIGHTS\_OFF} &= 0 \\
\textit{TPE\_OFF} &= \operatorname{align}_4(\textit{NvS}) \\
\textit{PLAN\_OFF} &= \operatorname{align}_4(\textit{TPE\_OFF} + R \times E) \\
\textit{broadcast} &= 3 \times E \times R \\
\textit{plan\_out} &= \textit{broadcast} + R(E{+}B) + 2R(E{+}B) + BR + 2R \\
N_4 &= \operatorname{align}_4(N) \\
\textit{TOPK0\_OFF} &= \operatorname{align}_4(\textit{PLAN\_OFF} + \textit{plan\_out}) \\
\textit{ORDER\_OFF} &= \textit{TOPK0\_OFF} + N_4 \\
\textit{ORDER0\_OFF} &= \textit{ORDER\_OFF} + N_4 \\
\textit{BARRIER\_OFF} &= \textit{ORDER0\_OFF} + N_4 \\
\textit{SRC\_INFO\_OFF} &= \textit{BARRIER\_OFF} + 3 \\
\textit{meta\_chunk\_logical} &= \textit{SRC\_INFO\_OFF} + \textit{NvS}
\end{aligned}
\]

其中 \(\operatorname{align}_4(x) = \lceil x/4 \rceil \times 4\)。`plan_out` 的五项分别对应 PLAN 区内的：组播广播区（3ER）、per-rank cu_seqlens 暂存（R(E+B)）、zero_fill 起止交织暂存（2R(E+B)）、experts_to_copy 暂存（BR）、remote_stats 暂存（2R）——与 [moonep/planning.py:546-552](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/planning.py#L546-L552) 的子偏移一一对应。

规划内核访问第 r 个 rank 分段的统一寻址模式是 `rank * ms + OFF`（`ms` = `meta_chunk_padded`），例如 [moonep/planning.py:961](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/planning.py#L961) 的 ORDER 视图、[moonep/planning.py:986-1001](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/planning.py#L986-L1001) 的 `plo = rank * ms + PLAN_OFF` 系列视图。

#### 4.3.3 源码精读

布局的权威定义在 [moonep/api.py:222-252](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L222-L252) 的 docstring 与 [moonep/api.py:305-333](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L305-L333) 的代码，逐行对应上表的公式：

- [moonep/api.py:307-310](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L307-L310)：注释点明「Meta offsets are int32 elements. Vectorized regions are 16B-aligned」，随后 `WEIGHTS_OFF/TPE_OFF/PLAN_OFF` 三个偏移相继算出。
- [moonep/api.py:311-321](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L311-L321)：断言 `broadcast_elems % 4 == 0` 并累加出 `planning_out_elems`。
- [moonep/api.py:323-333](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L323-L333)：注释解释「C1 scratch segments are padded so bulk copies can stay vectorized」——`N4 = align_up(N, 4)`，三个 N4 宽的 scratch 区首尾相接，BARRIER 固定 3 槽，最后 `SRC_INFO_OFF` 与 `meta_chunk_logical` 收口。

构造期还有两件与布局相关的初始化，见 [moonep/api.py:366-370](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L366-L370)：分配完成后，每个 rank 把自己视角下所有 R 个分段里的 BARRIER 区清零（对称映射下这些写互相重叠、都是写 0，幂等无害），再一个 `dist.barrier` 保证清零在任何内核启动前全局完成。SRC_INFO 则**不在**构造期清零——每次 planning 都会先整体重写为 `-1`（[moonep/planning.py:977-978](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/planning.py#L977-L978)），因为它每步的语义就是「全新发布」。

两个用户可见的视图也锚定在布局上：`hidden_buf_local` 与 `weights_buf_local` 在 [moonep/api.py:432-434](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L432-L434) 按 `rank * chunk + OFF` 切出，其中权重区随后以 `view(torch.float32)` 暴露为 fp32（[moonep/api.py:526-532](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L526-L532)）——这就是 WEIGHTS 段「fp32 别名为 int32」的落地：int32 缓冲里的位型按 fp32 解释，数值一点没变。

#### 4.3.4 代码实践

**实践目标**：手算一个小配置的全部 meta 偏移，体会「对齐到 4」在哪些位置真的补了空隙。

**操作步骤**：

1. 取配置 `S=128, H=1024, K=8, E=32, R=4, token_padding=128, B=8`（B 取默认 epn=8）。
2. 按上面公式逐步求 `N、epn、NvS、TPE_OFF、PLAN_OFF、broadcast_elems、planning_out_elems、N4、TOPK0_OFF、ORDER_OFF、ORDER0_OFF、BARRIER_OFF、SRC_INFO_OFF、meta_chunk_logical`。
3. 把结果画成一张按比例示意不了的「偏移表」（列出每段的 [起点, 终点) 即可）。

**需要观察的现象**：哪些 `align_up(..., 4)` 实际改变了数值（补了 1~3 个空元素），哪些恰好天然对齐。

**预期结果**：`N=1024`，`epn=8`，`NvS = 1024 + 127×16 = 3056`（天然是 4 的倍数，TPE_OFF 无补齐）；`TPE_OFF=3056`，`PLAN_OFF=3056+128=3184`；`broadcast_elems=3×32×4=384`；`planning_out_elems = 384 + 160 + 320 + 32 + 8 = 904`；`TOPK0_OFF=4088`，`N4=1024`，`ORDER_OFF=5112`，`ORDER0_OFF=6136`，`BARRIER_OFF=7160`，`SRC_INFO_OFF=7163`，`meta_chunk_logical=7163+3056=10219`（即 40876 字节，本配置未触发任何 16 字节补齐，`meta_chunk_logical % 4 = 3`——物理 chunk 的对齐在下一步才做）。可用第 5 节脚本核对手算（手算结果与脚本一致即验证通过）。

#### 4.3.5 小练习与答案

**练习 1**：TPE 区在每个 rank 的分段里都保留了一份，为什么？

**答案**：对称映射 `nvl_dist_map` 要求所有 rank 的 chunk 用同一个 shape 映射，布局必须同构，所以即使某区段主要由 rank0 使用，也得在每个分段里保留同样宽度——docstring 称之为 "symmetric reserve"（[moonep/api.py:242-243](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L242-L243)）。

**练习 2**：BARRIER 区为什么恰好 3 个 int32？

**答案**：[moonep/api.py:329-331](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L329-L331) 注释：2 个相位信号 + 1 个相位/符号计数器，与 rank 数无关的自复位双缓冲设计，使屏障不需要每次使用前清零。

**练习 3**：SRC_INFO 的长度为什么是 NvS 而不是 N？

**答案**：它镜像 dst 的 rank-stride 槽位编码，而 dispatch 的接收槽位按 padded 段布局铺在 `[0, NvS)` 上（含 padding 槽）；[moonep/planning.py:973-976](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/planning.py#L973-L976) 注释同时说明编码值 `src_rank * NvS + offv` 中 `offv ∈ [0, N)`、`NvS ≥ N`。

### 4.4 双重对齐与 int32 溢出防护

#### 4.4.1 概念说明

meta_buf 的物理 chunk 被两套机制同时引用：普通 VMM 映射（u2-l2）与组播绑定（4.2）。两套机制各有自己的粒度要求——VMM 映射要求 chunk 字节数是 VMM 粒度的倍数，组播绑定要求 bind 的地址与大小是组播粒度的倍数。于是 chunk 的对齐目标是两者的**最大值**：

\[
\textit{chunk\_align} = \max(\,g_{\text{vmm}},\; g_{\text{mc}}(R)\,), \qquad
\textit{meta\_chunk\_padded} = \frac{\operatorname{align}_{\textit{chunk\_align}}\!\left(\textit{meta\_chunk\_logical} \times 4\right)}{4}
\]

组播粒度还依赖组内设备数 R（`cuMulticastGetGranularity` 的属性里有 numDevices），所以它是对 R 查询的。两个粒度的真实值是设备属性，必须运行时查询；且像 u2-l2 的做法一样，C++ 侧的 `nvl_multicast_granularity` 取 fd 与 fabric 两类句柄粒度的最大值，保证无论进程组最终选哪种句柄类型，padding 都成立。

注意 `hidden_buf` **不**受双重对齐约束：它只建普通映射、不进组播对象，所以只需 `pad_dim0_for_alignment` 对齐 VMM 粒度（[moonep/api.py:305](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L305)）。双重对齐是 meta_buf 独有的，因为它才承担组播发布。

第二道防线是 **int32 溢出防护**。CuTe DSL 内核里很多地址表达式是「运行时的 Int32 rank 值 × 编译期 stride」：例如访问 rank r 分段的 `r * meta_chunk_padded + OFF + i`。Int32 的最大值是 \(2^{31}-1\)，如果 R 或 chunk 很大，最后一个可寻址元素 `(R-1) * meta_chunk_padded + meta_chunk_logical - 1` 可能悄悄溢出变负——这类 bug 在 GPU 上极难定位，所以在构造期（宿主端）就直接断言拦截。

#### 4.4.2 核心流程

```text
meta_chunk_logical（4.3 的收口值）
  × 4 字节
  → 对齐到 max(VMM 粒度, 组播粒度(R))
  → meta_chunk_padded（int32 元素数）
  → 检查 1：max_meta_index   = (R-1)*meta_chunk_padded + meta_chunk_logical - 1 ≤ INT32_MAX
  → 检查 2：max_hidden_index = (R-1)*NvS_padded        + NvS - 1               ≤ INT32_MAX
  → 分配：hidden_buf（普通映射）；meta_buf + meta_mc（普通映射 + 组播视图）
  → 构造期清零 BARRIER 区 + dist.barrier
```

#### 4.4.3 源码精读

- [moonep/api.py:335-340](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L335-L340)：双重对齐的落点——注释写明「Chunk size must satisfy both VMM mapping and multicast binding」，`chunk_align_bytes = max(gran, mc_gran)`，`meta_chunk_padded = _align_up(meta_chunk_bytes, chunk_align_bytes) // 4`。`_align_up` 是简单的向上取整（[moonep/api.py:77-79](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L77-L79)）。
- [moonep/buffer.py:89-110](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/buffer.py#L89-L110)：`pad_to_granularity` / `pad_dim0_for_alignment` 只按 VMM 粒度补齐——供 hidden_buf 与其他普通缓冲使用。
- [csrc/nvl_shared_buffer.cuh:394-425](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/csrc/nvl_shared_buffer.cuh#L394-L425)：组播粒度查询——`nvl_multicast_granularity` 取 fd 与 fabric（若支持）两类粒度的最大值并暴露 `get_multicast_granularity(num_devices)`；注释说明 bind 的 addr 与 size 都要对齐到它。
- [csrc/nvl_shared_buffer.cuh:493-496](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/csrc/nvl_shared_buffer.cuh#L493-L496)：`bind_map` 里对 bind 大小的运行时检查，错误信息「pad meta_buf chunk accordingly」把失败原因直接指回 Python 侧的 padding 逻辑——如果 4.4 的对齐算错，这里就是第一现场。
- [moonep/api.py:342-356](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L342-L356)：两条 int32 防护断言。注释解释动机——「Some CuTe DSL address expressions multiply a runtime Int32 rank/drank by these constexpr strides, so guard the largest reachable nonnegative index」；报错信息里带上全部相关尺寸，方便直接定位是哪个维度撑爆了。
- [moonep/api.py:361-364](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L361-L364)：分配调用本身——`hidden_buf` 走 `create_nvl_dist_tensor`（普通），`meta_buf, meta_mc` 走 `create_nvl_dist_multicast_tensor`（普通 + 组播），shape 用 `[meta_chunk_padded]`。

#### 4.4.4 代码实践

**实践目标**：验证「只差一点没对齐」时 padding 的行为，并观察 int32 断言在什么规模才会触发。

**操作步骤**：

1. 心算/纸笔推演：设 `g_vmm = g_mc = 2 MiB`，三个逻辑字节数 `meta_chunk_logical*4` 分别为 `5 MiB`、`2 MiB`、`2 MiB + 1`，求各自的 `meta_chunk_padded` 字节数。
2. 对 `max_meta_index` 做量级估算：固定 `meta_chunk_padded` 恰好为 \(2^{29}\)（2 GiB / 4），问 R 最大是多少时 `(R-1) * 2^{29} + \text{chunk\_logical}` 仍不超过 \(2^{31}-1\)？
3. （可选，需本地验证）在有 GPU 的机器上运行 `python -c "from moonep.buffer import get_vmm_granularity, get_multicast_granularity; print(get_vmm_granularity(), get_multicast_granularity(8))"` 查询真实粒度。

**需要观察的现象**：padding 的浪费上界是「对齐目标减 1 个元素」；int32 断言在大 chunk × 多 rank 时才逼近。

**预期结果**：三个字节数分别 padding 到 `6 MiB`、`2 MiB`（恰好对齐不补）、`4 MiB`（2 MiB+1 也要补到 4 MiB——「差 1 字节补 2 MiB」是对齐的典型放大效应）；估算题中 `(R-1) * 2^{29} < 2^{31}` 要求 `R ≤ 4`（R=5 时 4×2^29 = 2^31 已越界）——可见**单 chunk 2 GiB、5 个 rank 就会触发断言**，这不是杞人忧天的防护。真实粒度值步骤 3 的输出「待本地验证」。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `hidden_buf` 不需要对齐到组播粒度？

**答案**：hidden_buf 只被普通 VMM 映射引用（[moonep/api.py:361](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L361) 用的是 `create_nvl_dist_tensor`），从不进组播对象；组播粒度约束只作用于被 `cuMulticastBindMem` 绑定的内存，即 meta_buf。

**练习 2**：`get_multicast_granularity` 为什么要传 R？

**答案**：组播粒度是组播对象的属性，而 `CUmulticastObjectProp` 包含 numDevices——[csrc/nvl_shared_buffer.cuh:394-405](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/csrc/nvl_shared_buffer.cuh#L394-L405) 里查询时把 `num_devices` 填进 prop，设备数不同推荐粒度可能不同。

**练习 3**：如果把 `max_meta_index` 断言去掉，故障会以什么形式出现？

**答案**：不会在构造期报错，而是内核运行时地址计算 `(R-1)*meta_chunk_padded + off` 在 Int32 里回绕成负数/错位地址，表现为读错数据、写越界或莫名其妙的挂起——正因难以排查，才在宿主端提前断言（[moonep/api.py:344-350](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L344-L350)）。

## 5. 综合实践

**任务**：把 4.3/4.4 的公式整理成一个可复用的布局计算器 `meta_layout.py`，对任意 `(S,H,K,E,R,token_padding,B)` 配置输出完整布局表、物理 chunk 大小与两条 int32 断言的判定结果；有 GPU 环境时再与真实 `Buffer` 对照。

**完整脚本（示例代码，非项目原有；纯 CPU 可运行）**：

```python
# meta_layout.py — 复现 _create_context 的 meta_buf 布局与对齐计算
import argparse

INT32_MAX = 2**31 - 1

def align_up(x, a):
    return ((x + a - 1) // a) * a

def pad_dim0(rows, inner_bytes, gran):
    """等价于 moonep.buffer.pad_dim0_for_alignment（gran 作为参数注入）。"""
    nbytes = rows * inner_bytes
    padded_bytes = align_up(nbytes, gran)
    padded_dim0 = padded_bytes // inner_bytes
    while padded_dim0 * inner_bytes % gran != 0:
        padded_dim0 += 1
    return padded_dim0

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--S", type=int, default=4096)
    ap.add_argument("--H", type=int, default=7168)
    ap.add_argument("--K", type=int, default=8)
    ap.add_argument("--E", type=int, default=256)
    ap.add_argument("--R", type=int, default=8)
    ap.add_argument("--token-padding", type=int, default=128)
    ap.add_argument("--B", type=int, default=None)
    # 真实粒度是设备属性，须运行时查询：
    #   from moonep.buffer import get_vmm_granularity, get_multicast_granularity
    # 此处作为参数注入，默认按常见 2 MiB 假设（待本地验证）。
    ap.add_argument("--vmm-gran", type=int, default=2 * 1024 * 1024)
    ap.add_argument("--mc-gran", type=int, default=2 * 1024 * 1024)
    a = ap.parse_args()

    assert a.E % a.R == 0, "E must be divisible by R"
    epn = a.E // a.R
    B = a.B if a.B is not None else epn          # api.py: B 默认 epn
    N = a.S * a.K
    assert 0 < N < INT32_MAX                     # api.py 的 N 断言

    NvS = N + (a.token_padding - 1) * 2 * epn    # 逻辑槽位
    NvS_padded = pad_dim0(NvS, a.H * 2, a.vmm_gran)   # bf16 每行 H*2 字节，仅 VMM 对齐

    # ---- meta 布局（全部以 int32 元素计）----
    WEIGHTS_OFF = 0
    TPE_OFF = align_up(NvS, 4)
    PLAN_OFF = align_up(TPE_OFF + a.R * a.E, 4)
    broadcast_elems = 3 * a.E * a.R
    assert broadcast_elems % 4 == 0
    planning_out_elems = (broadcast_elems + a.R * (a.E + B)
                          + 2 * a.R * (a.E + B) + B * a.R + 2 * a.R)
    N4 = align_up(N, 4)
    TOPK0_OFF = align_up(PLAN_OFF + planning_out_elems, 4)
    ORDER_OFF = TOPK0_OFF + N4
    ORDER0_OFF = ORDER_OFF + N4
    BARRIER_OFF = ORDER0_OFF + N4
    BARRIER_SLOTS = 3
    SRC_INFO_OFF = BARRIER_OFF + BARRIER_SLOTS
    meta_chunk_logical = SRC_INFO_OFF + NvS

    # ---- 双重对齐 ----
    chunk_align = max(a.vmm_gran, a.mc_gran)
    meta_chunk_padded = align_up(meta_chunk_logical * 4, chunk_align) // 4

    # ---- int32 溢出断言 ----
    max_meta_index = (a.R - 1) * meta_chunk_padded + meta_chunk_logical - 1
    max_hidden_index = (a.R - 1) * NvS_padded + NvS - 1

    regions = [
        ("WEIGHTS", WEIGHTS_OFF, NvS, "路由权重（fp32 别名 int32）"),
        ("TPE", TPE_OFF, a.R * a.E, "tpe 汇聚（对称保留）"),
        ("PLAN", PLAN_OFF, planning_out_elems, f"规划暂存/发布（组播前 {broadcast_elems}）"),
        ("TOPK0", TOPK0_OFF, N4, "rank0 topk 卸载"),
        ("ORDER", ORDER_OFF, N4, "c1 排序 scratch"),
        ("ORDER0", ORDER0_OFF, N4, "rank1 为 rank0 产的副本"),
        ("BARRIER", BARRIER_OFF, BARRIER_SLOTS, "跨 rank 自复位屏障"),
        ("SRC_INFO", SRC_INFO_OFF, NvS, "槽位溯源（-1 哨兵）"),
    ]
    print(f"N={N} epn={epn} B={B} NvS={NvS} NvS_padded={NvS_padded}")
    print(f"{'region':<9}{'off(elems)':>11}{'len':>9}{'off(bytes)':>11}  purpose")
    for name, off, length, why in regions:
        print(f"{name:<9}{off:>11}{length:>9}{off*4:>11}  {why}")
    print(f"meta_chunk_logical = {meta_chunk_logical} elems ({meta_chunk_logical*4} bytes)")
    print(f"meta_chunk_padded  = {meta_chunk_padded} elems ({meta_chunk_padded*4} bytes, "
          f"align={chunk_align})")
    print(f"max_meta_index     = {max_meta_index}  ok={max_meta_index <= INT32_MAX}")
    print(f"max_hidden_index   = {max_hidden_index}  ok={max_hidden_index <= INT32_MAX}")

if __name__ == "__main__":
    main()
```

**操作步骤**：

1. 保存脚本后运行 `python meta_layout.py`（默认大模型配置），再运行 `python meta_layout.py --S 128 --H 1024 --E 32 --R 4` 复核 4.3.4 的手算结果。
2. 尝试逼近断言边界：`python meta_layout.py --S 33554432 --H 7168 --K 8 --E 256 --R 8`（S×K 巨大），观察哪条断言先失败（预期是 `0 < N < int32_max` 或 `max_meta_index`，具体取决于粒度假设——待本地验证）。
3. 把 `--mc-gran` 改成 `4194304`（4 MiB），观察 `meta_chunk_padded` 的跳变，体会「组播粒度主导对齐」的情形。
4. （可选，需 8 卡 NVLink 机器 + 已构建 `moonep._C`）写对照脚本：`torchrun --nproc_per_node=8` 下构造 `Buffer(S=4096, H=7168, K=8, E=256, num_ep_ranks=8, token_padding=128)`，读取 `buf._ctx` 中的 `NvS / NvS_padded / *_OFF / meta_chunk_padded`（注意 `meta_chunk_logical` 不在 ctx 里，可由 `SRC_INFO_OFF + NvS` 推出），与脚本输出（用 `get_vmm_granularity()` / `get_multicast_granularity(R)` 的真实粒度重跑）逐项对比，最后 `buf.destroy()`。

**需要观察的现象**：默认配置下逻辑尾部 padding 占 chunk 的比例（浪费率）；粒度假设变化时 `meta_chunk_padded` 的台阶式跳变；int32 判定列的 `ok=True/False`。

**预期结果**（按 2 MiB 双粒度假设计算，物理值待本地验证）：默认配置 `N=32768、epn=32、NvS=40896`；`TPE_OFF=40896、PLAN_OFF=42944`（`broadcast_elems=6144、planning_out_elems=13328`）；`TOPK0_OFF=56272、ORDER_OFF=89040、ORDER0_OFF=121808、BARRIER_OFF=154576、SRC_INFO_OFF=154579、meta_chunk_logical=195475`（781900 字节）；`meta_chunk_padded=524288`（正好一个 2 MiB）；`max_meta_index=3865490`、`max_hidden_index=327615`，两条断言均通过。第 4 步的逐项一致即为最终验证标准。

## 6. 本讲小结

- **组播的价值**：规划结果是「rank0 算、全员要」的数据，`multimem.st` 让 rank0 只写一次，NVSwitch 硬件把写扇出到所有 rank chunk 的同一偏移，发布成本不随 R 增长；MoonEP 里 `meta_mc` 的唯一消费者是 planning 内核的 `3*E*R` 元素广播循环。
- **四步创建流程**：`nvl_multicast_create`（root）→ 句柄分发 + `nvl_multicast_import`（fabric 广播 / fd 的 SCM_RIGHTS 子集发送）→ `nvl_multicast_add_device`（全员，barrier）→ `nvl_multicast_bind_map`（全员，barrier）；bind 用 owned handle 走 `cuMulticastBindMem`，mc 视图不占额外显存、与 meta_buf 同生命周期。
- **七段布局**：meta_buf 每个rank的 chunk 内依次是 WEIGHTS(NvS) / TPE(R·E) / PLAN / TOPK0(N4) / ORDER(N4) / ORDER0(N4) / BARRIER(3) / SRC_INFO(NvS)，区间头尾用 `align_up(..., 4)` 对齐到 16 字节以允许向量化访问，布局对所有 rank 同构。
- **PLAN 区内部**：前 `3*E*R` 元素（ALLOC/TPE/EOFF 三个 `[R,E]` 矩阵）经组播发布，其余 per-rank 切片（cu_seqlens、zero_fill、experts_to_copy、remote_stats）由各 rank 从 rank0 暂存区 G2S 拷走。
- **双重对齐**：meta_buf 的 chunk 字节数必须同时是 VMM 粒度与组播粒度（对 R 查询、取两种句柄类型的最大值）的倍数，故对齐目标取 `max(两者)`；hidden_buf 只做普通映射，仅需 VMM 对齐。
- **int32 防护**：`(R-1)*meta_chunk_padded + meta_chunk_logical - 1` 与 `(R-1)*NvS_padded + NvS - 1` 都必须在宿主端断言不超 `INT32_MAX`，否则内核内 Int32 地址表达式回绕，故障极难定位。

## 7. 下一步学习建议

本讲补完了内存基础设施的最后一块：你现在知道 meta_buf 的每个字节属于谁、组播如何叠加其上。接下来进入第 3 单元「在线规划器」：

1. **u3-l1（MoonEPCommPlan）**：先看规划输出的数据结构——PLAN 区发布的暂存内容最终如何变成 Python 侧的 plan 张量，正好接上本讲的 `planning_out_elems` 五项分解。
2. **u3-l2（群组级均衡）**：精读 Phase A/B，理解本讲反复出现的 `tpe` 汇聚与 `3*E*R` 广播数据是怎么算出来的。
3. **u3-l6（计划发布与同步）**：本讲刻意略过的细节——Phase D 的 G2S 批量拷贝、`cross_rank_barrier` 自复位协议与 BARRIER 区 3 槽的实现，都在那一讲展开。
4. 想先动手的话，把第 5 节脚本的 `--R` 扫一遍（1/2/4/8/16），观察 `meta_chunk_padded` 与 `max_meta_index` 随 R 的变化曲线，为后续读内核的 rank-stride 寻址建立量级直觉。
