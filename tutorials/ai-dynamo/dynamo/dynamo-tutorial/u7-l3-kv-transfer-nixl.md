# u7-l3 KV 传输引擎：NIXL / NCCL / memcpy

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 `TransferStrategy` 七种策略各自的适用场景：什么源存储、什么目标存储、本机还是跨节点。
2. 解释 `block/transfer/` 这层抽象如何用「编译期类型矩阵 + 运行期分派」两段式设计，把 NIXL、NCCL、CUDA memcpy、纯 memcpy 四类后端的差异屏蔽在一个统一入口 `write_to` 后面。
3. 精读 NIXL 后端的完整执行序列：内存注册 → 描述符 → 描述符列表 → 传输请求 → 提交 → 轮询完成。
4. 定位一次 KV 传输在源码中的触发点：从 offload 请求 / 分布式 transfer 消息，一路追到 `write_blocks_to` 里的 `post_xfer_req`。
5. 画出一张 KV 块从 prefill GPU 显存到 decode GPU 显存的函数级时序图（本讲主实践）。

本讲承接 u7-l1 建立的认知：disaggregated_params 等控制面元数据经路由器传递，而 KV 字节本身走 NIXL 点对点直传、不经过 frontend。本讲回答的问题是：**「直传」这两个字在 Rust 源码里到底落在哪几个函数上。**

## 2. 前置知识

### 2.1 四层存储：Device / Pinned / System / Disk

Dynamo 的 KVBM（KV Block Manager）把一块 KV 缓存可能栖身的内存分为四层，对应四个存储类型：

| 存储类型 | 位置 | NIXL 内存类型 | 说明 |
|---|---|---|---|
| `DeviceStorage` | GPU 显存（VRAM） | `MemType::Vram` | 速度最快、容量最小 |
| `PinnedStorage` | 主机锁页内存 | `MemType::Dram` | 页不会被操作系统换出，GPU 可直接 DMA 访问 |
| `SystemStorage` | 普通主机内存 | `MemType::Dram` | 可换页，GPU 不能直接访问 |
| `DiskStorage` | 磁盘文件 | `MemType::File` | 容量最大、速度最慢 |

**「锁页」（pinned）是理解 H2D/D2H 性能的关键**：普通内存可能被 OS 换到磁盘，GPU 的 DMA 引擎无法直接读写它；锁页内存被钉在物理页上，DMA 引擎可以直接访问。这就是为什么 `PinnedStorage → DeviceStorage` 走异步 CUDA 拷贝（`CudaAsyncH2D`），而 `SystemStorage → DeviceStorage` 走阻塞拷贝（`CudaBlockingH2D`）——后者必须先经过一次隐式的锁页中转。

### 2.2 NIXL：跨进程内存注册与一键传输

NIXL 是 ai-dynamo 生态里的兄弟库（见 AGENTS.md 的 Ecosystem 表格），它把「RDMA / NVLink / GPUDirect Storage / UCX」等底层传输统一起来。它有三个核心概念，本讲会反复出现：

- **Agent（代理）**：每个进程创建一个有名字的 agent（如 `kvbm-worker-0`），agent 之间可以互相发现。
- **内存注册（register memory）**：把一段本地内存（显存或锁页内存）登记给 agent，agent 才能对它发起传输。注册后得到一个**描述符（descriptor）**：`(地址, 大小, 内存类型, 设备号)` 四元组。
- **传输请求（xfer request）**：把一组源描述符和一组目标描述符打包成两个描述符列表（`XferDescList`），提交给 agent，由 NIXL 选择最优物理通道（NVLink、InfiniBand 等）执行。

描述符是可以序列化的——这就让「远端的地址」能通过控制面消息发过来，本地的 agent 拿着远端描述符就能直接读/写远端内存。这正是 P/D 分离时 KV「点对点直传、不经过 frontend」的实现基础。

### 2.3 NCCL：集合通信

NCCL 是 NVIDIA 的集合通信库，`ncclBcast`（广播）是它的原语之一：一个组（communicator）内所有 rank **必须集体参与**同一次调用，root rank 的数据被复制到所有 rank。它解决的是「多 GPU 副本一致」问题，与 NIXL 解决的「两点之间搬数据」问题正交。

### 2.4 CUDA 流与事件

CUDA 拷贝是异步的：提交给某个 stream（流）后立即返回，真正完成需要事后同步。Dynamo 用 `CudaEvent` 记录「流推进到此处」的时刻，再由一个专职线程 `event.synchronize()` 阻塞等待，等待结束后通过 oneshot channel 通知异步层。**本讲所有传输 API 的返回值都是 `oneshot::Receiver<()>`**——统一以「异步完成通知」的形态出现，无论底层是 memcpy、CUDA 还是 NIXL。

## 3. 本讲源码地图

本讲的核心是 `lib/llm/src/block_manager/block/transfer/` 这棵子树（注意：整个 `block_manager` 模块由 `block-manager` feature 门控，见 [lib/llm/src/lib.rs:49-50](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/lib.rs#L49-L50)，在 Linux 上是默认 feature）：

| 文件 | 角色 |
|---|---|
| `block/transfer.rs` | **抽象层**：`TransferStrategy` 枚举、`WriteToStrategy`/`ReadFromStrategy` trait、运行期分派函数 `handle_local_transfer`、统一入口 `WriteTo` |
| `block/transfer/strategy.rs` | **类型矩阵**：为每一对（源存储， 目标存储）静态绑定一种策略，纯编译期查表 |
| `block/transfer/nixl.rs` | **NIXL 后端**：跨节点 GPU↔GPU / GPU↔磁盘直传（本讲主角） |
| `block/transfer/nccl.rs` | **NCCL 后端**：副本模式下的多 GPU 广播 |
| `block/transfer/memcpy.rs` | **memcpy 后端**：同进程主机内存之间的普通拷贝 |
| `block/transfer/cuda.rs` | **CUDA 后端**：H2D/D2H/D2D 拷贝与非连续布局的自定义 kernel（本讲摘要式带过） |
| `block/transfer/context.rs` | `TransferContext`：被所有后端共享的运行资源（NIXL agent、CUDA stream、事件工作线程） |
| `block/locality.rs` | `LocalityProvider`：把 `write_to` 调用路由到本地或逻辑资源处理器 |
| `storage/nixl.rs` | 存储类型与 NIXL 的桥：内存注册、`NixlStorage` 远端描述符 |
| `offload/pending.rs`、`offload.rs` | **触发点一**：KVBM offload 管理器如何调用传输 |
| `distributed/transfer.rs` | **触发点二**：分布式 worker 收到 leader 的消息后如何执行传输；sharded/replicated 两种模式 |

建议按「抽象层 → 三个后端 → 触发点」的顺序阅读。

## 4. 核心概念与源码讲解

### 4.1 传输抽象：TransferStrategy 与两段式分派

#### 4.1.1 概念说明

三个后端（NIXL、NCCL、memcpy/CUDA）的 API 形态完全不同：NIXL 要先注册内存再提交请求，NCCL 要求所有 rank 集体参与，memcpy 只是裸的指针拷贝。抽象层要解决的问题是：**调用方（offload 管理器、分布式 worker）只说「把这 N 个源块写进那 N 个目标块」，后端选择必须自动完成，而且不能在运行期付出动态分派的代价。**

Dynamo 的答案分两段：

- **编译期**：用 Rust 的类型系统做查表。源块和目标块的存储类型在编译期就确定了，所以「哪种存储到哪种存储该用什么策略」可以表达为一组 trait impl，编译器直接内联成常量。
- **运行期**：一个普通的 `match` 把策略枚举分发到对应后端函数。

#### 4.1.2 核心流程

```
调用方: sources.write_to(&mut targets, ctx)
        │  (Vec<RB> 上的 WriteTo trait 方法)
        ▼
LocalityProvider::handle_transfer(...)          ── block/locality.rs
        │  Local ⇒ handle_local_transfer
        │  Logical<R> ⇒ 校验资源一致后转交 R.handle_transfer
        ▼
handle_local_transfer(...)                      ── block/transfer.rs
        │  RB::write_to_strategy()  ← 编译期由 strategy.rs 的类型矩阵决定
        ▼  运行期 match
   ┌────────────┬────────────────────┬──────────────────┐
   │ Memcpy     │ CudaAsync/Blocking │ Nixl(Read/Write) │
   ▼            ▼                    ▼
memcpy::     cuda::copy_block /   nixl::write_blocks_to
copy_block   custom kernel        （4.2 详述）
   │            │                    │
   └────────────┴────────→ oneshot::Receiver<()> ←──────┘
                            （统一完成通知）
```

#### 4.1.3 源码精读

先看策略枚举的定义——整个传输子系统的「词汇表」：

[lib/llm/src/block_manager/block/transfer.rs:73-106](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/block_manager/block/transfer.rs#L73-L106)

这段代码定义了三件事：`NixlTransfer`（NIXL 的读/写两个方向，`as_xfer_op` 把它翻译成 nixl-sys 的 `XferOp::Read/Write`）；`CudaTransferMode`（GPU↔GPU 拷贝用自定义 kernel 还是默认异步 memcpy）；以及 `TransferStrategy` 七种策略——`Memcpy`、`CudaAsyncH2D/D2H/D2D`（异步：Pinned↔Device、Device↔Device）、`CudaBlockingH2D/D2H`（阻塞：System↔Device）、`Nixl(NixlTransfer)`，外加一个表示「不可行」的 `Invalid`。

两个策略 trait 是编译期查表的接口：

[lib/llm/src/block_manager/block/transfer.rs:108-123](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/block_manager/block/transfer.rs#L108-L123)

`WriteToStrategy<Target>` 回答「从本地源写到目标用什么策略」，`ReadFromStrategy<Source>` 回答「从（可能远端的）源读进本地用什么策略」。默认都是 `Invalid`——没有实现对应组合就编译期报错，这是把错误左移到编译期的典型手法。

真正的「查表」在 strategy.rs，形式是一长串 trait impl。看几个代表性条目：

[lib/llm/src/block_manager/block/transfer/strategy.rs:44-63](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/block_manager/block/transfer/strategy.rs#L44-L63)

这三条规则分别是：主机普通内存 → 主机普通内存，用纯 `Memcpy`（CPU 指针直接拷，无 GPU 参与）；普通内存 → 锁页内存，也是 `Memcpy`（都在主机侧，锁页只是对 DMA 有意义）；普通内存 → 显存，用 `CudaBlockingH2D`（因为源不是锁页的，走异步收益为负）。对照 2.1 节的表格，规则背后的物理原因一目了然。

再看跨节点方向的规则：

[lib/llm/src/block_manager/block/transfer/strategy.rs:114-126](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/block_manager/block/transfer/strategy.rs#L114-L126)

`DeviceStorage → DeviceStorage` 是 `CudaAsyncD2D`（同一进程内或经由统一寻址的 GPU 间拷贝）；而**任何** `Local` 存储 → `NixlStorage`（远端内存描述）一律 `Nixl(NixlTransfer::Write)`。这个 blanket impl 是关键：只要目标侧是「远端注册内存的描述符」，就必然走 NIXL，其他后端根本没有能力写远端。

读方向同理，[lib/llm/src/block_manager/block/transfer/strategy.rs:158-163](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/block_manager/block/transfer/strategy.rs#L158-L163) 规定从 `NixlStorage` 读入本地就是 `Nixl(NixlTransfer::Read)`。**P/D 分离时 decode 从 prefill 拉 KV，用的正是 Read 方向**：prefill 把自己显存的描述符发过来，decode 侧把它包成 `NixlStorage` 源块，策略自动落到 NIXL Read。

运行期分派的核心是 `handle_local_transfer`：

[lib/llm/src/block_manager/block/transfer.rs:176-277](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/block_manager/block/transfer.rs#L176-L277)

函数开头做两个防御：源目标都为空时直接返回一个已完成的 oneshot；长度不等报 `CountMismatch`。随后 `match RB::write_to_strategy()` 分四路。注意每一路的完成语义都被刻意拉平成同一种形状——`oneshot::Receiver<()>`：

- `Memcpy` 臂（[L203-L213](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/block_manager/block/transfer.rs#L203-L213)）：逐块同步拷完，`tx.send(())` 立即完成。源码里的 TODO 注释坦承这是唯一全阻塞的策略，将来应挪进线程池。
- CUDA 臂（[L214-L262](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/block_manager/block/transfer.rs#L214-L262)）：H2D/D2H 时先看首块是否两侧完全连续——连续走默认 `cuda::copy_block` 循环，非连续走 `copy_blocks_with_customized_kernel`（自定义 kernel 把逐层指针拷贝摊平成一次 kernel 启动）；最后 `ctx.cuda_event(tx)` 把「事件同步完成」接到 oneshot 上。
- NIXL 臂（[L263-L271](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/block_manager/block/transfer.rs#L263-L271)）：调用 `nixl::write_blocks_to` 拿到一个 Future，再 `spawn` 到 `ctx.async_rt_handle()` 上，Future 完成时 `tx.send(())`。**传输的异步性由 NIXL 的提交-轮询模型天然提供**（4.2 详解）。

最外层的入口是 `WriteTo` trait：

[lib/llm/src/block_manager/block/transfer.rs:279-302](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/block_manager/block/transfer.rs#L279-L302)

`Vec<RB>` 实现了 `WriteTo<WB>`，方法体只有一行 `L::handle_transfer(...)`——把工作转交给块数据自带的 Locality。`Local` 的实现直接调 `handle_local_transfer`（[lib/llm/src/block_manager/block/locality.rs:50-67](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/block_manager/block/locality.rs#L50-L67)）；`Logical<R>` 则先校验源和目标引用的是同一份逻辑资源（`Arc::ptr_eq`），再把传输交给资源自己处理（[lib/llm/src/block_manager/block/locality.rs:113-162](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/block_manager/block/locality.rs#L113-L162)）。这样「块在哪、归谁管」的语义与「用什么后端搬」彻底解耦。

#### 4.1.4 代码实践

**实践：跑通类型矩阵单元测试，亲手加一条新规则**

1. **实践目标**：验证策略矩阵的行为可被机器断言，并体会「编译期查表」——写错组合编译器直接拒绝。
2. **操作步骤**：
   - 运行 strategy.rs 自带的单元测试（纯类型断言，不需要 GPU）：
     ```bash
     cargo test -p dynamo-llm transfer::strategy
     ```
     （注意：`block_manager` 模块由默认 feature `block-manager` 门控；按 AGENTS.md 提示，macOS 上需谨慎使用默认 feature。Linux CI 环境可直接跑。）
   - 打开 [lib/llm/src/block_manager/block/transfer/strategy.rs:165-245](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/block_manager/block/transfer/strategy.rs#L165-L245)，阅读 `write_to_strategy` 测试：它把 System/Pinned/Device 到各目标的断言全部列了一遍。
   - **不要修改源码**，只在纸上做思想实验：假如把 `<SystemStorage as WriteToStrategy<DeviceStorage>>::write_to_strategy()` 的实现从 `CudaBlockingH2D` 改成 `Memcpy`，会发生什么？（提示：`Memcpy` 路径会拿到 GPU 指针当主机指针解引用。）
3. **需要观察的现象**：测试输出中 `transfer::strategy::tests::write_to_strategy` 与 `read_from_strategy` 两个用例通过。
4. **预期结果**：所有断言与 strategy.rs 的 impl 一一对应；矩阵中不存在 `NixlStorage` 作为**本地源**的行（源码注释里被注释掉的断言说明：这些组合根本不编译）。具体测试运行结果**待本地验证**（本讲义写作环境未编译）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `SystemStorage → DeviceStorage` 是 `CudaBlockingH2D` 而 `PinnedStorage → DeviceStorage` 是 `CudaAsyncH2D`？

**答案**：普通系统内存可被操作系统换页，DMA 引擎不能直接访问，驱动必须先把它拷进一块临时的锁页缓冲，这次中转让异步失去意义，所以走阻塞路径；锁页内存天然可 DMA，提交给流后立即返回，用事件事后同步即可（见 2.1 节与 strategy.rs L58-L91 的两条规则）。

**练习 2**：`handle_local_transfer` 的四个 match 臂返回的都是 `oneshot::Receiver<()>`，这样设计的好处是什么？

**答案**：调用方（如 offload 管理器）不必关心后端是同步的 memcpy、流式的 CUDA 还是提交-轮询的 NIXL，统一 `notify.await` 即可等待完成；上层因此能把「发起传输」和「等待传输」放进不同的并发结构（例如 `FuturesUnordered`，见 4.5 的 pending.rs）。

**练习 3**：如果把 `WriteToStrategy` 的默认实现从 `TransferStrategy::Invalid` 改成一个可用的策略，最直接的危害是什么？

**答案**：非法的存储组合将从编译期错误退化成运行期才暴露的问题。`Invalid` 默认值 + 显式 impl 的组合，等价于白名单：漏写实现 → 编译失败；写错组合 → `handle_local_transfer` 的兜底臂返回 `IncompatibleTypes` 错误（transfer.rs L272-L276）。

### 4.2 NIXL 后端：跨节点 GPU 直传

#### 4.2.1 概念说明

nixl.rs 是 P/D 分离性能承诺的兑现处：官方文档 [disaggregated-serving.md](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/docs/fern/pages/developer-guide/knowledge-base/concepts/system-architecture/disaggregated-serving.md) 明确说「decode worker 用传输元数据协调 prefill worker，NIXL 以最优可用通道（NVLink、InfiniBand/UCX 等）完成 GPU 到 GPU 的直传，且传输不阻塞 GPU 前向」。

NIXL 的编程模型是「**注册 + 提交 + 轮询**」三段式：

1. **注册**：显存/锁页内存块在分配时登记给本进程的 agent（`nixl_register`），登记后才能拿到描述符。
2. **提交**：把一批源描述符和目标描述符分别填进两个 `XferDescList`，打包成一个传输请求，`post_xfer_req` 一次性提交——NIXL 内部会把这些不连续的段聚合成高效的多段传输。
3. **轮询**：`post` 返回一个布尔值 `still_pending`——false 表示已经完成（比如全在本地），true 则需要轮询 `get_xfer_status` 直到 `Success`。

#### 4.2.2 核心流程

一次 `NixlStorage` 参与的传输（以 decode 从 prefill 拉 KV 为例，Read 方向）：

```
prefill 侧（更早发生）                        decode 侧（本讲的调用点）
─────────────────────                        ──────────────────────
DeviceStorage 分配
  └ nixl_register(agent)          控制面消息  收到远端描述符
  └ as_nixl_descriptor()  ───────────────────▶ 包装为 NixlStorage 源块
              (addr,size,Vram,devid)              │
                                                   ▼
                                   write_to(dst=本地 DeviceStorage)
                                                   │ strategy = Nixl(Read)
                                                   ▼
                                   append_xfer_request（逐块填两份描述符表）
                                                   │
                                                   ▼
                                   create_xfer_req(Read, src_dl, dst_dl, agent)
                                                   │
                                                   ▼
                                   post_xfer_req ──▶ still_pending?
                                                   │ yes
                                                   ▼
                                   spawn 轮询任务：get_xfer_status
                                   InProgress ⇒ sleep 5ms 重试
                                   Success   ⇒ break ⇒ tx.send(())
```

关键点：`post_xfer_req` 之后**控制流立即返回**，GPU 前向可以继续跑；轮询是一个独立的轻量异步任务，每 5ms 查一次状态。这就是「非阻塞」的准确含义。

#### 4.2.3 源码精读

先看逐块填表的辅助函数：

[lib/llm/src/block_manager/block/transfer/nixl.rs:10-73](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/block_manager/block/transfer/nixl.rs#L10-L73)

`append_xfer_request` 对每一对（源块， 目标块）做两件事：取两侧的 `block_data()`；然后按布局分两条路。**快速路径**（[L25-L43](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/block_manager/block/transfer/nixl.rs#L25-L43)）：两侧都完全连续时，整块就是一个内存段，各生成一个描述符 `add_desc` 进源/目标描述符表即可——一次传输请求里就一段，开销最小。**慢速路径**（[L44-L72](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/block_manager/block/transfer/nixl.rs#L44-L72)）：KV 块按「层 × 外维」组织，非连续时逐层取 `layer_view`，每层各加一条描述符。`add_desc` 是 unsafe 的——它把裸指针、大小、设备号塞进描述符表，安全性由「块存活期间描述符有效」这一生命周期约定保障。

> 顺带一个诚实的观察：`write_blocks_to` 上方的文档注释写着 "using CUDA memcpy"（[L75](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/block_manager/block/transfer/nixl.rs#L75)），这是历史遗留的笔误——函数体用的完全是 NIXL。读源码时不要被注释带偏，以函数体为准。

主函数：

[lib/llm/src/block_manager/block/transfer/nixl.rs:76-152](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/block_manager/block/transfer/nixl.rs#L76-L152)

逐段拆解：

- [L88-L97](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/block_manager/block/transfer/nixl.rs#L88-L97)：空集快速返回已完成 Future；源目标数量必须相等。然后从 `TransferContext` 取 NIXL agent——context 没有 agent 直接 `expect` panic，说明 NIXL 策略被选中时 agent 必然已建好。
- [L99-L113](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/block_manager/block/transfer/nixl.rs#L99-L113)：用首块推断两侧的 NIXL 内存类型（映射规则：System/Pinned→Dram，Device→Vram，Disk→File，见 [storage/nixl.rs:140-150](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/block_manager/storage/nixl.rs#L140-L150)），据此 `XferDescList::new` 建两张表。
- [L115-L127](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/block_manager/block/transfer/nixl.rs#L115-L127)：zip 遍历逐块填表；然后 `create_xfer_req` 把两张表、方向（`transfer_type.as_xfer_op()`）与对端 agent 名打包成一个请求，`post_xfer_req` 提交。**一次请求承载整批块**——这是批量摊薄提交开销的设计。
- [L129-L151](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/block_manager/block/transfer/nixl.rs#L129-L151)：`still_pending == false` 直接返回已完成 Future；否则返回一个 async 块，循环 `get_xfer_status`：`Success` 跳出、`InProgress` 睡 5ms、出错记日志后跳出。

描述符从哪来？看存储侧的桥接：

[lib/llm/src/block_manager/storage/nixl.rs:160-214](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/block_manager/storage/nixl.rs#L160-L214)

`nixl_register` 把存储交给 agent 登记，注册句柄存进存储自身的 `"nixl"` 槽位（Drop 时自动 `deregister`）；`as_nixl_descriptor` 只有在已注册时才返回 `(addr, size, mem_type, device_id)` 四元组，否则 None。而承载「远端内存」的类型就是 `NixlStorage`：

[lib/llm/src/block_manager/storage/nixl.rs:226-234](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/block_manager/storage/nixl.rs#L226-L234)

它不拥有任何内存，只是一个可序列化的远端地址描述；源码注释点明它是为「序列化后跨节点传输」而生——控制面消息里传的 KV 句柄，本质就是这四元组（外面包一层 `NixlRemoteDescriptor`，[storage/nixl.rs:85-99](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/block_manager/storage/nixl.rs#L85-L99)，附带 agent 名与可选的完成通知字串）。

最后是被所有后端共享的 `TransferContext`：

[lib/llm/src/block_manager/block/transfer/context.rs:173-187](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/block_manager/block/transfer/context.rs#L173-L187)

它聚合了：NIXL agent（`Arc<Option<...>>`，None 表示本进程不用 NIXL）、一条共享 CUDA stream、一个 tokio runtime Handle（给 NIXL 轮询任务用）、CUDA 内存池与遗留锁页缓冲池，以及一个 CUDA 事件工作线程通道。NIXL agent 的创建时机在分布式 worker 初始化时——[distributed/worker.rs:94-106](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/block_manager/distributed/worker.rs#L94-L106) 的 `build_agent` 以 `kvbm-worker-{id}` 命名 agent，并在需要磁盘时挂 GDS（GPUDirect Storage）后端、总是挂 POSIX 后端——这是 NIXL 拿到 File 类型内存传输能力的来源。

#### 4.2.4 代码实践

**实践：画 prefill→decode KV 块传输时序图（本讲主实践）**

1. **实践目标**：把 4.2.2 的流程落实到具体函数名，产出一张能对着源码逐步核对的时序图。
2. **操作步骤**：
   - 无 GPU 环境（静态分析路线）：
     a. 通读 [nixl.rs:76-152](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/block_manager/block/transfer/nixl.rs#L76-L152)，为每一行关键调用在图上立一个节点。
     b. 画出两个泳道（prefill 进程 / decode 进程），按下面 12 步标注函数名：
        1. prefill：`PinnedAllocator`/Device 分配 → `nixl_register`（storage/nixl.rs:172-178）
        2. prefill：`as_nixl_descriptor` 生成四元组（storage/nixl.rs:207-215）
        3. prefill → decode：控制面消息携带 `NixlRemoteDescriptor`（含 agent 名）
        4. decode：目标侧本地 `DeviceStorage` 已注册
        5. decode：`write_to`（transfer.rs:295-302）
        6. decode：`handle_local_transfer` → strategy 矩阵判为 `Nixl(Read)`（strategy.rs:158-163）
        7. decode：`nixl::write_blocks_to` 取 agent（nixl.rs:93-97）
        8. decode：`XferDescList::new` × 2（nixl.rs:112-113）
        9. decode：`append_xfer_request` 逐块 `add_desc`（nixl.rs:10-73）
        10. decode：`create_xfer_req(Read, ...)` + `post_xfer_req`（nixl.rs:119-127）
        11. decode：`still_pending == true` → spawn 轮询 `get_xfer_status`，5ms 间隔（nixl.rs:129-148）
        12. decode：`Success` → `tx.send(())` → 上层 `notify.await` 返回（transfer.rs:266-270）
     c. 用 memcpy 路径做对照：同一步骤号下，`Memcpy` 策略只有第 5、6 步 + `memcpy::copy_block`（一步同步完成）+ `tx.send(())`，体会「策略不同、完成语义相同」。
   - 有 GPU 环境（可选加分）：跑通任一 disagg 示例（u7-l1 的 disagg.sh），把 `RUST_LOG=debug` 打开，在日志里找 `Performing sharded transfer` / `Executing N batches concurrently` 字样对应 4.5 的触发点。
3. **需要观察的现象**：时序图上每一步都能在源码里指出行号；memcpy 对照路径明显短一截。
4. **预期结果**：一张两泳道、12 步、含函数名的时序图 + 一段不超过 100 字的对照说明（说明 memcpy 路径缺了哪些步骤、为什么 NIXL 需要轮询而 memcpy 不需要）。GPU 日志部分**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：`post_xfer_req` 返回的 `still_pending` 为 false 意味着什么？代码怎么利用它？

**答案**：意味着提交即完成（例如数据全在本地内存，无需异步通道）。代码走 else 臂返回 `std::future::ready(())`（nixl.rs:149-151），省掉 spawn 一个轮询任务的开销。

**练习 2**：为什么 `append_xfer_request` 要区分「完全连续」和「逐层」两条路径，而不是无脑逐层填表？

**答案**：描述符越少，NIXL 一次请求内的段数越少、聚合效率越高、元数据开销越小。连续布局是 KVBM 分配器尽力保证的常态，快速路径让常态走最少描述符；逐层路径只为非连续布局兜底。

**练习 3**：`NixlStorage` 为什么标记为 `Remote` 而不是 `NixlRegisterableStorage`？

**答案**：它描述的是**别的 agent** 注册的内存，本地 agent 只能对其发起传输，不能也不应该再次注册它；源码注释（storage/nixl.rs:134-137）明确区分了这两个 trait——`NixlRegisterableStorage` 要求存储拥有并管理注册句柄，`NixlStorage` 只是一个地址四元组。

### 4.3 NCCL 后端：副本模式广播

#### 4.3.1 概念说明

NCCL 后端不是为「prefill→decode 搬运」服务的，而是为**多 GPU 副本（replicated）部署**服务的：当一组 rank 各自持有一份相同的 KV（例如张量并行下每 rank 的分片需要保持一致，或多副本缓存），任何一次「从 Host/Disk 装载到 Device」都必须让所有 rank 的显存同步更新。NCCL 的 `ncclBcast` 是做这件事的标准原语。

它有两种使用粒度：`bcast_block`（整块）与 `bcast_layer`（按层区间），外加一个 RAII 的 `NcclGroup` 用来把多次广播合并成一次组提交——NCCL 的 group 语义能把多个集合操作融合，显著减少同步次数。

#### 4.3.2 核心流程

副本模式的一次「Host→Device 装载」（由 [distributed/transfer.rs:396-471](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/block_manager/distributed/transfer.rs#L396-L471) 的 `execute_transfer_spmd_replicated` 编排）：

```
leader 发出 BlockTransferRequest（所有 rank 都收到）
        │
        ├─ Device→Device？ ⇒ 退回 sharded 本地路径，各 rank 自行拷贝
        ├─ 非 rank0 且本次无广播（to_pool != Device）？ ⇒ no-op 直接返回
        ├─ rank0：begin_transfer 走 4.1 的本地策略（如 NIXL/CUDA）把数据搬进自己显存
        └─ to_pool == Device（需要广播）：
                所有 rank 集体执行 broadcast_device_blocks：
                NcclGroup::new (ncclGroupStart)
                循环 bcast_block(block, root=0, comm, stream)
                group.end()   (ncclGroupEnd，提交并暴露错误)
                ctx.cuda_event(tx) + rx.await   ← 流上同步等待真正完成
```

**注意不对称性**：装载（Host→Device）只有 rank0 做实际拷贝、随后全员广播；而 Device→Device 一律各 rank 本地处理。这来自 [L426-L433](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/block_manager/distributed/transfer.rs#L426-L433) 的两个前置分支。

#### 4.3.3 源码精读

RAII 守卫解决的是 NCCL 的一个坑：`ncclGroupStart` 之后必须有一个匹配的 `ncclGroupEnd`，漏了会让整个 communicator 卡死。

[lib/llm/src/block_manager/block/transfer/nccl.rs:52-107](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/block_manager/block/transfer/nccl.rs#L52-L107)

`NcclGroup` 用 `Cell<bool>` 记录是否已成功 end：正常路径调用方显式 `group.end()?` 以便观察提交错误；若忘了调，`Drop` 里补调 `ncclGroupEnd` 并在失败时 **panic**——源码注释明说这是刻意的不静默吞错。文档注释（L36-45）给出了标准用法示例。

广播单块的函数：

[lib/llm/src/block_manager/block/transfer/nccl.rs:129-155](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/block_manager/block/transfer/nccl.rs#L129-L155)

与 NIXL 的 `append_xfer_request` 同构的二分：连续布局一次 `ncclBcast`（数据类型用 `ncclChar` 即逐字节，语义上等于搬原始字节）；非连续降级到 `bcast_layer`。注意 root 固定由调用方传入（分布式侧总是传 0）。

[lib/llm/src/block_manager/block/transfer/nccl.rs:179-215](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/block_manager/block/transfer/nccl.rs#L179-L215)

`bcast_layer` 支持可选层区间，逐层逐外维 `ncclBcast`。与 memcpy.rs 的 `copy_layers` 逐字对得上——**三个后端共享同一套「块 → 视图 → 指针+大小」的布局抽象**，这是屏蔽后端差异的真正粘合剂。

编排方在分布式侧。先看模式定义：

[lib/llm/src/block_manager/distributed/transfer.rs:34-41](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/block_manager/distributed/transfer.rs#L34-L41)

`TransferMode::Sharded`（默认，各 rank 独立管理自己的分片）与 `TransferMode::Replicated`（全员复制，需 NCCL）。模式不是显式配置的，而是由 `NcclConfig.is_enabled()` 推导（[L261-L265](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/block_manager/distributed/transfer.rs#L261-L265)）。

广播的完整执行：

[lib/llm/src/block_manager/distributed/transfer.rs:474-527](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/block_manager/distributed/transfer.rs#L474-L527)

`broadcast_device_blocks` 是 NCCL 后端与分布式编排的接缝：从 `self.context.stream()` 取 CUDA 流、从 `nccl_config` 取 communicator，`NcclGroup::new` 开组，对每个目标块索引调 `bcast_block(block, 0, ...)`，`group.end()` 提交，最后 `cuda_event(tx)` + `rx.await` 等待流上的广播真正完成。**结束处的日志（L519-L524）打印 rank/world_size/块数**——这是副本模式下排查广播问题最直接的观测点。

#### 4.3.4 代码实践

**实践：填写副本模式的 rank 参与表**

1. **实践目标**：吃透 `execute_transfer_spmd_replicated` 中每个 rank 在每种池组合下的行为差异。
2. **操作步骤**：
   - 精读 [distributed/transfer.rs:396-471](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/block_manager/distributed/transfer.rs#L396-L471)。
   - 填写下面表格（6 行 × 3 列，共 18 格）：

     | from→to | rank0 | 非 rank0 |
     |---|---|---|
     | Device→Device | ？ | ？ |
     | Host→Device | ？ | ？ |
     | Disk→Device | ？ | ？ |
     | Device→Host | ？ | ？ |
     | Host→Disk | ？ | ？ |
     | Disk→Host | ？ | ？ |

   - 每格填「本地拷贝 / 广播 / no-op / 报错」四选一，并注明依据的分支行号。
3. **需要观察的现象**：自己填的表与同事/同学互查时，最容易在 `Device→Device`（L426-428 提前返回 sharded）和 `非 rank0 + Host→Disk`（L431-433 no-op）两处产生分歧。
4. **预期结果**：18 格全部能落到源码分支；特别标注出「非 rank0 也必须参与」的行——广播是集合操作，缺席会挂起全组。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `bcast_block` 的数据类型固定用 `ncclChar`？

**答案**：KV 块传输关心的是字节保真，不关心数值语义；`ncclChar` 表示逐字节广播，避免了对 KV 元素的 dtype 假设，与 memcpy 语义一致。

**练习 2**：`broadcast_device_blocks` 末尾为什么还要 `cuda_event` + `rx.await`？`group.end()` 成功不就完了吗？

**答案**：NCCL 调用只是把操作**入队**到流上，`ncclGroupEnd` 成功代表提交成功而非数据就位；必须等流推进过这些操作（事件同步）才算传输完成，随后才能让上层把目标块标记为有效。

**练习 3**：副本模式下为什么批量切分（`ConnectorTransferBatcher`）被禁用，改为 `execute_transfer_direct`？

**答案**：见 [distributed/transfer.rs:196-200](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/block_manager/distributed/transfer.rs#L196-L200)：广播是集体操作，若各 rank 各自把请求切成不同批次并发执行，不同 rank 的集合调用次序会错位导致死锁；串行直发保证全员按同一顺序参与。

### 4.4 memcpy 后端与本地分派细节

#### 4.4.1 概念说明

memcpy.rs 只有 70 行，是三个后端里最简单的，但它有两个值得精读的点：其一，它是**唯一同步完成**的策略，展示了「完成通知」如何被人为拉平；其二，它对内存布局的处理与 NIXL/NCCL 完全同构（连续整块 vs 逐层循环），证明布局抽象是所有后端的公共前端。

CUDA 后端（cuda.rs）本讲只做摘要：它按策略选择 `cudaMemcpyAsync` 的方向变体，对非连续布局提供 `copy_blocks_with_customized_kernel`（把逐层拷贝摊平为一次 kernel 启动，用锁页缓冲传指针数组），入口 `copy_block` 见 [cuda.rs:299-345](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/block_manager/block/transfer/cuda.rs#L299-L345)，其 debug 断言会在调试构建里核对传入策略与存储类型对是否匹配（L313-318）——又一个「编译期矩阵 + 运行期自检」的双保险。

#### 4.4.2 核心流程

```
copy_block(src, dst)
   ├─ 两侧都 is_fully_contiguous()？
   │     ├─ 是 ⇒ block_view() 取整块视图
   │     │        memcpy(ptr, ptr, size)   ← copy_nonoverlapping
   │     └─ 否 ⇒ copy_layers(0..num_layers)
   │                for layer × outer: layer_view() → memcpy
   └─ Ok(())   （返回即完成，无异步阶段）
```

#### 4.4.3 源码精读

[lib/llm/src/block_manager/block/transfer/memcpy.rs:7-30](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/block_manager/block/transfer/memcpy.rs#L7-L30)

`copy_block` 的骨架与 `append_xfer_request`、`bcast_block` 三胞胎：同一个 `is_fully_contiguous()` 判断、同一对 `block_view()/layer_view()` 视图 API，只是末端动作换成裸指针拷贝。

[lib/llm/src/block_manager/block/transfer/memcpy.rs:61-70](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/block_manager/block/transfer/memcpy.rs#L61-L70)

最底层的 `memcpy` 帮手用 `debug_assert` 校验源目标区间**不重叠**，然后 `std::ptr::copy_nonoverlapping`。为什么重叠是 bug？因为 `copy_nonoverlapping` 的语义前提就是无重叠，重叠时的结果是未定义的；而 KVBM 的分配器保证源块与目标块来自不同池，正常情况下不会重叠——这个断言是分配器不变量的运行期哨兵。

再看 `handle_local_transfer` 的 Memcpy 臂如何把它包成异步形态：

[lib/llm/src/block_manager/block/transfer.rs:203-213](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/block_manager/block/transfer.rs#L203-L213)

逐块同步拷完、`tx.send(())` 立刻兑现。TODO 注释承认这是全阻塞的——对大块主机内存拷贝会占住调用线程，改进方向是线程池。这个「诚实的技术债标注」本身值得学习：抽象拉平了语义，但物理代价（阻塞）还在，注释把它显式暴露给读者。

#### 4.4.4 代码实践

**实践：用 memcpy 路径对照 NIXL 路径，量化「异步」的差异**

1. **实践目标**：理解两种完成模型（立即完成 vs 轮询完成）对上层并发结构的影响。
2. **操作步骤**：
   - 静态分析：并排打开 [nixl.rs:127-151](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/block_manager/block/transfer/nixl.rs#L127-L151) 与 [memcpy.rs:18-29](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/block_manager/block/transfer/memcpy.rs#L18-L29)，列一张对照表：返回时机、是否占用调用线程、失败如何暴露、完成通知何时兑现。
   - 思考题自答：`handle_local_transfer` 的 Memcpy 臂在函数返回前就 `tx.send(())` 了，那么上层 `notify.await` 永远不会挂起——这对 4.5 将看到的 `FuturesUnordered` 并发结构意味着什么？（答案在练习里。）
3. **需要观察的现象**：对照表上 NIXL 行的「返回时机」是提交后立即返回、Memcpy 行是全部拷完才返回。
4. **预期结果**：一张 4 行对照表 + 一段 50 字结论：两种模型都能塞进 `FuturesUnordered`，但 Memcpy 的「假异步」会把耗时折叠进 enqueue 阶段，表现为入队慢、等待快。

#### 4.4.5 小练习与答案

**练习 1**：`copy_layers` 的签名接受 `Range<usize>` 而不是层总数，这个设计为谁服务？

**答案**：为「部分层传输」的调用方服务——某些引擎按层渐进产出 KV（u9 会见到 connector 调度器的 `LayerComplete` 语义），只需要搬已算好的层区间；NIXL 的逐层路径和 NCCL 的 `bcast_layer` 同样保留了这个自由度。

**练习 2**：如果源和目标块真的重叠了（例如同池内移动），memcpy.rs 会发生什么？NIXL 路径呢？

**答案**：memcpy 路径在 debug 构建触发 `debug_assert` panic，release 构建则是未定义行为；NIXL 路径没有这个检查（append_xfer_request 只比 num_layers），依赖上层保证源目标来自不同池——不同的后端对同一不变量的防御深度不同，读码时应留意。

**练习 3**：cuda.rs 的 `copy_block` 里那段 `#[cfg(debug_assertions)]` 断言（L313-318）防的是什么错误？

**答案**：防「调用方手动传错策略」。它用 `TypeId` 在运行期重算期望策略并与传入值比对，等价于把 strategy.rs 编译期矩阵在调试构建里再做一次抽查——若有人绕过类型系统直接调 `cuda::copy_block` 并传错方向，调试构建立刻暴露。

### 4.5 传输触发点：谁在调用 write_to

#### 4.5.1 概念说明

前四节讲的是「怎么搬」，这一节回答「谁在搬、什么时候搬」。`write_to` 在生产代码里有两条主要触发链：

- **KVBM offload/onboard 链**：块池把「块注册事件」交给 offload 管理器，管理器按优先级排队，worker 线程取出后经 `LocalTransferManager::enqueue_transfer` 发起传输。这是 GPU↔CPU↔SSD 分层缓存（u9 的主题）的动力源。
- **分布式 transfer 链**：leader 决策出一次搬运后，经 ZMQ 活动消息把 `BlockTransferRequest` 发给 worker 进程，worker 的 `BlockTransferHandler::handle` 解析请求并执行。这是 u10/分布式 KVBM 的路径。

#### 4.5.2 核心流程

```
路径一（offload）：
块注册事件 → OffloadManager 优先级队列（低 priority 值先出队）
          → offload_worker/onboard_worker 线程
          → TransferManager::enqueue_transfer(PendingTransfer)
          → sources.write_to(...)                    ← 进入 4.1 的抽象层
          → 完成后 TransferCompletionManager.handle_complete 更新块状态

路径二（分布式）：
leader → ZMQ 活动消息（JSON: BlockTransferRequest{from_pool,to_pool,blocks}）
      → BlockTransferHandler::handle
      → ConnectorTransferBatcher（超过 max_transfer_batch_size 则分批并发）
      → execute_transfer_direct
           ├─ Sharded   → execute_transfer_spmd_sharded   → begin_transfer → write_to
           └─ Replicated → rank0 本地搬 + 全员 NCCL 广播（4.3）
```

#### 4.5.3 源码精读

路径一的发射点：

[lib/llm/src/block_manager/offload/pending.rs:286-305](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/block_manager/offload/pending.rs#L286-L305)

`enqueue_transfer` 只有三步：`write_to` 拿到完成通知 channel；把「等待通知 + 归还 PendingTransfer」包成一个 Future；塞进容量为 1 的 mpsc。真正并发控制在 `LocalTransferManager::new`（[pending.rs:214-266](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/block_manager/offload/pending.rs#L214-L266)）：一个 `FuturesUnordered` 装 in-flight 传输，超过 `max_concurrent_transfers` 就先 `next().await` 等最早的完成。**4.4 练习 2 的答案在此兑现**——Memcpy 策略的「假异步」会让 FuturesUnordered 几乎不排队，因为 Future 入队时就已经完成了。

offload 管理器自己的文档把语义讲得很清楚：

[lib/llm/src/block_manager/offload.rs:1-30](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/block_manager/offload.rs#L1-L30)

「offload = 把块推向离设备更远的层，带宽有限所以必须按优先级排队；onboard = 把块拉回离设备更近的层，全部手动触发」。两个并发上限可以用环境变量调：

[lib/llm/src/block_manager/offload.rs:76-88](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/block_manager/offload.rs#L76-L88)

`DYN_KVBM_MAX_CONCURRENT_TRANSFERS`（默认 4）与 `DYN_KVBM_MAX_TRANSFER_BATCH_SIZE`（默认 16）。这两个旋钮直接决定 offload 吞吐与显存/带宽占用之间的折中。

路径二的执行体：

[lib/llm/src/block_manager/distributed/transfer.rs:304-348](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/block_manager/distributed/transfer.rs#L304-L348)

`begin_transfer` 是泛型的「按索引取块 → write_to」：请求里的 `blocks` 是 `(from_idx, to_idx)` 对的列表，从预分配的池列表里克隆出源/目标块序列，然后调用我们 4.1 节精读的 `write_to`。注意这里源目标都是 `LocalBlockData`——**同一进程内**的 Device/Host/Disk 池之间搬运；跨节点的部分由 NIXL 描述符在更上层交换。

sharded 模式的池组合分派：

[lib/llm/src/block_manager/distributed/transfer.rs:379-392](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/block_manager/distributed/transfer.rs#L379-L392)

六个合法组合（Device↔Host、Device↔Disk、Host↔Disk）各对应一次 `begin_transfer` 调用，其余报「Invalid transfer type」。每个组合由 Rust 类型系统自动选择策略：Device→Host 落 `CudaAsyncD2H`、Host→Host 落 `Memcpy`、涉及 Disk 落 `Nixl(...)`（Disk 的 File 内存类型必须经 NIXL 的 POSIX/GDS 后端访问）——对照 4.1 的矩阵即可验证。

最后是消息入口：

[lib/llm/src/block_manager/distributed/transfer.rs:530-581](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/block_manager/distributed/transfer.rs#L530-L581)

`BlockTransferHandler` 实现 ZMQ 活动消息的 `Handler`：反序列化 `BlockTransferRequest`；若带 connector 请求则先经调度器客户端 `schedule_transfer`（与引擎的逐层产出进度对齐，详见 [connector/protocol.rs](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/block_manager/connector/protocol.rs#L1-L60) 的模块文档，那里详细定义了 load/store 与调度器的握手协议）；执行 `execute_transfer`；无论成败都 `message.ack()`。**「先 ack 后报错」是分布式传输的容错取舍**：让 leader 尽早知道消息已送达，错误走独立的取消路径。

#### 4.5.4 代码实践

**实践：用环境变量旋钮做一次思想实验 + 日志验证**

1. **实践目标**：把两个 KVBM 传输旋钮与源码行为对应起来。
2. **操作步骤**：
   - 静态分析：读 [offload.rs:74-88](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/block_manager/offload.rs#L74-L88) 与 [distributed/transfer.rs:184-234](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/block_manager/distributed/transfer.rs#L184-L234)，回答：把 `DYN_KVBM_MAX_TRANSFER_BATCH_SIZE` 从 16 调到 160，分布式路径上会发生什么？（提示：`ConnectorTransferBatcher` 按 `chunks(max_batch_size)` 切批并发。）
   - 动手验证（无 GPU 可做静态推演，标注「待本地验证」）：若能跑 mocker/示例集群，分别以默认值与 `DYN_KVBM_MAX_CONCURRENT_TRANSFERS=1` 启动，观察 offload 吞吐与 `Executing N batches concurrently` 日志中 N 的变化。
3. **需要观察的现象**：批大小调大后单批块数增多、并发批数减少；并发传输数调小后 offload 队列积压、块到达远端层的延迟增大。
4. **预期结果**：一张「旋钮 → 源码位置 → 行为变化」的三列表格；动态部分**待本地验证**。

#### 4.5.5 小练习与答案

**练习 1**：`enqueue_transfer` 用的 mpsc 容量为 1，为什么这么小？

**答案**：见 [pending.rs:299-301](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/block_manager/offload/pending.rs#L299-L301) 注释：容量 1 形成天然背压——队列 worker 若因 FuturesUnordered 满而在 `next().await` 上等待，发送端就会阻塞，不会无限堆积 PendingTransfer（每份都持有块引用，堆积会占住池容量）。

**练习 2**：`BlockTransferHandler::handle` 里为什么无论成败都先 `message.ack()`？

**答案**：ack 表示「消息已收到并处理过」，错误已通过 `handle.mark_complete(Err(...))` 上报给调度器；若失败时不 ack，leader 侧会按消息层超时重发，而传输本身可能已部分完成，重发反而引入重复传输的复杂性。

**练习 3**：分布式路径的六个池组合里，哪几个会走到 NIXL 后端？

**答案**：凡是涉及 `DiskStorage` 的组合（Device↔Disk、Host↔Disk）。因为 Disk 的 NIXL 内存类型是 `File`（storage/nixl.rs:140-150），只有 NIXL 的 POSIX/GDS 后端能访问文件型内存；strategy.rs 中 DiskStorage 与各目标的 impl 全部落 `TransferStrategy::Nixl(...)`（strategy.rs:9-126）。

## 5. 综合实践

**任务：交付一张「KV 块 prefill GPU → decode GPU」函数级时序图 + 三后端对照卡**

综合本讲全部模块，产出一份可复查的文档（建议 Markdown + Mermaid 或 ASCII 图）：

1. **时序图（主体）**：按 4.2.4 的 12 步要求，画两个泳道（prefill 进程 / decode 进程），每步标注：函数名、文件与行号、同步还是异步。控制面（描述符如何跨进程）与数据面（KV 字节走 NIXL）用不同颜色/线型区分。
2. **三后端对照卡**：为 NIXL / NCCL / memcpy 各写半页卡片，包含：适用场景（跨节点直传 / 多 GPU 副本广播 / 同进程主机拷贝）、入口函数、连续与非连续布局的分支、完成通知的兑现方式、一个独有陷阱（NIXL 轮询间隔 5ms；NCCL 组必须集体参与且 drop 前要 end；memcpy 的重叠断言只在 debug 生效）。
3. **触发点标注**：在时序图左上角注明本次传输由哪条链触发（offload 链或分布式 transfer 链），并链到对应源码行。
4. **自查**：找一位同样学完本讲的同学（或自己隔一天）遮住源码，只看图复述每一步，任何一步说不出函数名即为不合格，回对应小节重读。

无 GPU 环境整个任务均可完成（全部为静态分析）；若后续拿到 GPU 环境，补充第 5 步：跑 disagg 示例并用 `RUST_LOG=debug` 抓 `Performing sharded transfer` 与 NCCL 广播日志，与图中步骤一一勾稽（待本地验证）。

## 6. 本讲小结

- `block/transfer/` 用**两段式分派**屏蔽后端差异：strategy.rs 的类型矩阵在编译期把（源存储, 目标存储）绑定为 `TransferStrategy`，`handle_local_transfer` 在运行期把策略 match 到具体后端，所有后端统一返回 `oneshot::Receiver<()>`。
- **NIXL 后端**（`write_blocks_to`）走「注册 → 描述符表 → create_xfer_req → post_xfer_req → 5ms 轮询」的提交-轮询模型，是 P/D 分离时 KV 点对点直传（不经 frontend）的落地；`NixlStorage` 只是可序列化的远端地址四元组。
- **NCCL 后端**（`bcast_block` + `NcclGroup`）服务多 GPU 副本模式：rank0 先本地装载、全员集体广播、CUDA 事件收尾；集合语义要求批量切分在副本模式下禁用。
- **memcpy/CUDA 后端**负责进程内搬运：memcpy 完全同步但被包成「立即兑现的完成通知」；CUDA 按 Pinned/System 决定异步或阻塞，非连续布局走自定义 kernel。三个后端共享「块 → 视图 → 指针+大小」的布局抽象。
- 两条触发链：KVBM offload 优先级队列（`enqueue_transfer`，受 `DYN_KVBM_MAX_*` 环境变量调节）与分布式 ZMQ 活动消息（`BlockTransferHandler::handle`，sharded/replicated 两模式）。
- 读源码的一个教训：nixl.rs 的文档注释写着 "CUDA memcpy" 但实现是 NIXL——**以函数体为准，不迷信注释**。

## 7. 下一步学习建议

本讲把「KV 块如何搬运」讲完了，接下来应该问「谁决定搬哪些块、搬到哪层」：

1. **u9 KVBM 系列**（推荐下一步）：`u9-l1 KvBlockManager 总览` 讲 controller/handler 的控制面调用——本讲 4.5 的 offload 链正是它的执行机构；`u9-l3` 的 TinyLFU 决定优先级队列里 priority 的来源；`u9-l4` 的物理层布局是本讲「层 × 外维」视图的底层。
2. **新世代实现**：本讲的 `block/transfer/` 是 v1 路径；`lib/kvbm-physical/src/transfer/executor/nixl.rs` 与 `lib/llm/src/block_manager/v2/physical/transfer/` 是同一思想的重写版（NIXL 状态从轮询改为通知对象，见 `notifications/nixl_status.rs`）。读完本讲再对照 v2，能清晰看到演进取舍。
3. **引擎侧协议**：`connector/protocol.rs` 的模块文档定义了 leader/TransferEngine/Scheduler 三方的 load/store 握手（层完成度门控传输时机），是理解「逐层 KV 渐进上抛」的钥匙，衔接 u8 各后端接入。
4. **动手方向**：把 4.2.4 的时序图扩展成「含逐层传输」的版本（对照 `SchedulerRequirement::LayerComplete`），你就把本讲与 u9 的调度语义串起来了。
