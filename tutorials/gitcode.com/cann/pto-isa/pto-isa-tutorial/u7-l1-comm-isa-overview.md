# 通信 ISA 总览：点对点、信号同步与集合通信

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 PTO 通信扩展指令集的**四大类共 11 条指令**（同步点对点、异步点对点、信号同步、集合通信），以及每一类解决什么问题。
2. 掌握 `comm_types.hpp` 中的核心类型：`ParallelGroup`、`Signal`/`Signal2D`、`NotifyOp`/`WaitCmp`/`ReduceOp`/`DmaEngine`/`CollEngine`、`AsyncEvent`/`AsyncSession`。
3. 理解通信指令与计算指令**同级的 tile 抽象设计**：为什么远端读写也要经过一个 staging Tile。
4. 理解「驱动多引擎」的含义——同一条上层 API 背后可以路由到 MTE 搬运流水线、Vector 计算单元、SDMA/URMA DMA 引擎或 CCU 集合通信硬件，且路由全部发生在编译期。
5. 能独立浏览 `include/pto/comm/` 目录并把指令头文件按类别归类，为下一讲精读 TGET/TPUT 打基础。

本讲是单元七（通信指令集与计算通信融合）的第一讲，只做**全景导览**，不深入任何一条指令的完整实现细节。

## 2. 前置知识

阅读本讲前，你需要具备以下概念（均在前面讲义中建立）：

- **NPU 与多卡**：一块昇腾 NPU 有多个 AI Core；一台服务器上有多块 NPU 卡（也称多个 rank）。单卡内的数据流（GM → UB → GM）你已经通过 TLOAD/TSTORE 掌握；本讲处理的是**跨 NPU** 的数据流——数据要从一个 rank 的 GM 搬到另一个 rank 的 GM。
- **GlobalTensor 视图**（u2-l1）：`__gm__` 指针 + shape/stride 元数据的零拷贝视图。通信指令的远端地址、本落地地址都用它描述。
- **Tile 与 staging（中转）缓冲**（u2-l2、u3-l1）：Tile 是片上（UB）固定形状的 2-D 缓冲。跨卡搬运时数据通常要"落"在一个 Tile 上中转，这个 Tile 就叫 staging tile。
- **事件同步**（u2-l3）：`(srcPipe, dstPipe, eventId)` 三元组的 set_flag/wait_flag 配对，以及 `RecordEvent` 返回值风格。通信 API 与计算指令一样接受 `WaitEvents...`、返回 `RecordEvent`。
- **多后端编译路由**（u2-l4）：`__CPU_SIM` / `__CCE_AICORE__` / `__COSTMODEL` 三个宏决定同一份 kernel 代码编译到哪个后端；`arch_macro.hpp` 把 `__NPU_ARCH__` 翻译成 `PTO_NPU_ARCH_A2A3/A5/...` 架构宏。
- **术语：rank**：参与通信的一方的编号，源自 MPI 的习惯叫法；**root**：集合通信中负责发起/汇总的那个 rank；**UB**：Unified Buffer，向量核心的片上缓冲；**GM**：Global Memory（HBM），卡间可见的地址空间。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [docs/isa/comm/README.md](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/isa/comm/README.md) | 通信 ISA 文档索引：四大类指令清单 + 核心类型速查（语义权威入口） |
| [include/pto/comm/README.md](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/comm/README.md) | 通信模块工程说明：目录布局、架构分层图、指令分类表 |
| [include/pto/comm/comm_types.hpp](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/comm/comm_types.hpp) | 跨后端共享类型定义：ParallelGroup、Signal/Signal2D、各枚举、AsyncEvent |
| [include/pto/comm/pto_comm_inst.hpp](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/comm/pto_comm_inst.hpp) | 通信指令统一公共 API：TPUT/TGET/TNOTIFY/TWAIT/TTEST/TGATHER/TSCATTER/TBROADCAST/TREDUCE/TPUT_ASYNC/TGET_ASYNC |
| [include/pto/comm/pto_comm_instr_impl.hpp](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/comm/pto_comm_instr_impl.hpp) | 编译期后端分发器：按架构宏互斥 include a2a3 / a5 / cpu 实现 |
| [include/pto/common/arch_macro.hpp](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/arch_macro.hpp) | 架构号 → 架构宏翻译，含 `PTO_COMM_NOT_SUPPORTED`（无通信能力架构）与 `PTO_URMA_SUPPORTED` |
| [include/pto/comm/a2a3/TGet.hpp](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/comm/a2a3/TGet.hpp) | TGET 在 A2/A3 上的真机实现（MTE 流水线路径的典型样本） |
| [include/pto/comm/a2a3/TNotify.hpp](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/comm/a2a3/TNotify.hpp) | TNOTIFY 真机实现（GM 原子写 + 缓存维护路径样本） |
| [include/pto/comm/a2a3/TWait.hpp](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/comm/a2a3/TWait.hpp) | TWAIT 真机实现（自旋等待 + 死锁检测样本） |
| [include/pto/comm/a2a3/TReduce.hpp](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/comm/a2a3/TReduce.hpp) | TREDUCE 真机实现（复用计算指令的 AIV 引擎路径样本） |
| [include/pto/comm/async_common/async_types.hpp](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/comm/async_common/async_types.hpp) | 异步 DMA 会话类型：SDMA/URMA 上下文与引擎无关的 AsyncSession |
| [include/pto/cpu/comm/TGet.hpp](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/cpu/comm/TGet.hpp) | TGET 的 CPU 仿真桩（与真机实现对照） |

目录层面的全景（摘自模块 README）：

```
include/pto/comm/
├── pto_comm_inst.hpp        # 公共 API（唯一推荐上层包含入口）
├── pto_comm_instr_impl.hpp  # 后端分发器
├── comm_types.hpp           # 共享类型
├── a2a3/                    # A2/A3 (910B/910C) 真机实现 + async/（SDMA 异步）
├── a5/                      # A5 (950) 真机实现 + async/（SDMA+URMA 异步）
└── async_common/            # 两代架构共享的异步公共实现
```

## 4. 核心概念与源码讲解

### 4.1 通信分类：四大类 11 条指令

#### 4.1.1 概念说明

PTO 的计算指令集解决"单核/单卡内怎么算"，通信扩展指令集解决"多卡之间怎么搬数据、怎么对齐进度"。整个通信 ISA 只有 11 条指令，按语义分成四类：

| 类别 | 指令 | 一句话语义 |
|---|---|---|
| 点对点（同步） | `TPUT`、`TGET` | 远端写 / 远端读，数据经 UB 上的 staging tile 中转，走 MTE 搬运流水线 |
| 点对点（异步） | `TPUT_ASYNC`、`TGET_ASYNC` | GM→GM 直接 DMA（SDMA/URMA 引擎），不占 MTE 流水线，返回 AsyncEvent 句柄 |
| 信号同步 | `TNOTIFY`、`TWAIT`、`TTEST` | 跨卡"挂牌/等牌"：原子改写一个 int32 信号量，阻塞或非阻塞地等条件成立 |
| 集合通信 | `TGATHER`、`TSCATTER`、`TBROADCAST`、`TREDUCE` | 多 rank 收集 / 分发 / 广播 / 归约，用 ParallelGroup 描述参与方 |

类比 MPI：TPUT/TGET ≈ `MPI_Send/Recv`（这里是单边 put/get），TNOTIFY/TWAIT ≈ 原子计数 + 轮询，TGATHER/TSCATTER/TBROADCAST/TREDUCE ≈ `MPI_Gather/Scatter/Bcast/Reduce`。区别在于：PTO 通信是**指令级**的，可以直接写进 kernel 主循环，和 TMatmul 之类的计算指令交错编排——这正是 u7-l5 计算-通信融合算子的基础。

#### 4.1.2 核心流程

一次典型的双卡数据交换（卡 0 写 → 卡 1 读）：

```text
卡 0（生产者）                          卡 1（消费者）
─────────────                          ─────────────
comm::TPUT(dstOnCard1, srcOnCard0,     （TPUT 落地完成后，卡 1 的 GM 中数据就绪）
           stagingTile)
comm::TNOTIFY(signalOnCard1, 1,
             NotifyOp::Set)            comm::TWAIT(signalOnCard1, 1, WaitCmp::EQ)
                                       comm::TGET(dstLocal, srcOnCard1, stagingTile)
```

要点：

1. **数据与信号分离**——先搬数据，再用 TNOTIFY 告诉对方"到了"，对方 TWAIT 确认后才 TGET。这对应分布式系统里"数据消息 + 完成通知"的经典两段式。
2. 信号本身也是一个 GM 地址（`int32_t`），所以 TNOTIFY 写的其实是一个**远端内存变量**。
3. 所有通信指令与计算指令共用同一套事件机制：API 尾部可挂 `WaitEvents...`，搬运类指令返回 `RecordEvent`，可被后续指令等待。

#### 4.1.3 源码精读

指令分类的权威清单在 ISA 文档索引中，四大类标题即分类：

- [docs/isa/comm/README.md:L8-L26](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/isa/comm/README.md#L8-L26) —— 文档侧的四大类分组：同步点对点（TPUT/TGET）、异步点对点（TPUT_ASYNC/TGET_ASYNC）、信号同步（TNOTIFY/TWAIT/TTEST）、集合通信（TGATHER/TSCATTER/TREDUCE/TBROADCAST）。
- [include/pto/comm/README.md:L64-L71](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/comm/README.md#L64-L71) —— 工程侧的指令分类表，与文档一致，并补充了关键实现信息：同步点对点支持单缓冲与 ping-pong 双缓冲；集合通信支持分块 2-D 滑窗与 ping-pong。
- [docs/isa/comm/README.md:L52-L57](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/isa/comm/README.md#L52-L57) —— 信号三件套的最小用法示例：`TNOTIFY(signal, 1, NotifyOp::Set)` 挂牌、`TWAIT(signal, 1, WaitCmp::EQ)` 阻塞等、`TTEST(signal, 1, WaitCmp::GE)` 非阻塞测。

公共 API 层的"薄壳"形态（与计算指令的三段式完全同构）：

- [include/pto/comm/pto_comm_inst.hpp:L36-L43](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/comm/pto_comm_inst.hpp#L36-L43) —— `TPUT` 公共 API：先 `WaitAllEvents(events...)` 等前置事件，再转发 `TPUT_IMPL<..., atomicType>`，返回 `RecordEvent`。原子类型（`AtomicNone`/`AtomicAdd`）是编译期模板参数。
- [include/pto/comm/pto_comm_inst.hpp:L82-L89](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/comm/pto_comm_inst.hpp#L82-L89) —— `TGET` 公共 API：数据通路为"远端 GM → staging tile (UB) → 本地 GM"，同样是事件等待 + `_IMPL` 转发的薄壳。
- [include/pto/comm/pto_comm_inst.hpp:L108-L142](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/comm/pto_comm_inst.hpp#L108-L142) —— 信号三件套 `TNOTIFY`/`TWAIT`/`TTEST`：注意三者返回 `void`/`bool` 而非 `RecordEvent`——TWAIT 本身就是阻塞点，TTEST 返回条件是否成立，它们不挂流水线。

一个值得注意的命名细节：文档里指令名全大写（`TBROADCAST`），而头文件名是大小写混合（`TBroadCast.hpp`），内核代码里调用写成 `pto::comm::TBROADCAST(...)`。

#### 4.1.4 代码实践

**实践目标**：浏览 `include/pto/comm/a2a3` 目录，把 9 个通信指令头文件按「点对点（同步）/信号同步/集合通信」分类填表（本讲综合实践会扩展到异步类）。

**操作步骤**：

1. 在仓库根目录执行 `ls include/pto/comm/a2a3/`，应看到 9 个 `.hpp` 文件（外加一个 `async/` 子目录）。
2. 逐个打开每个文件，**只看文件顶部注释里的 `XXX_IMPL` 说明行**（例如 TGet.hpp 顶部 `// TGET_IMPL: Remote read operation implementation`），不看实现体。
3. 制作如下空表并填写：

| 头文件 | 指令 | 类别 | 底层引擎（从注释推断） |
|---|---|---|---|
| TPut.hpp | TPUT | ？ | ？ |
| TGet.hpp | TGET | ？ | ？ |
| TNotify.hpp | TNOTIFY | ？ | ？ |
| TWait.hpp | TWAIT | ？ | ？ |
| TTest.hpp | TTEST | ？ | ？ |
| TGather.hpp | TGATHER | ？ | ？ |
| TScatter.hpp | TSCATTER | ？ | ？ |
| TBroadCast.hpp | TBROADCAST | ？ | ？ |
| TReduce.hpp | TREDUCE | ？ | ？ |

4. 用 `ls include/pto/comm/a2a3/async/` 补充第 10、11 条指令（TPutAsync.hpp / TGetAsync.hpp），归入"点对点（异步）"。

**需要观察的现象**：9 个文件的头部注释都遵循同一格式（`XXX_IMPL — 一句话语义`），且注释里的数据通路描述（如 "local GM → UB → remote GM"）能直接告诉你它走哪条引擎通路。

**预期结果**：分类应与 [include/pto/comm/README.md:L64-L71](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/comm/README.md#L64-L71) 的表格逐行一致；`async/` 下两条归入点对点异步类。本实践为源码阅读型实践，无需编译运行。

#### 4.1.5 小练习与答案

**练习 1**：TPUT 和 TPUT_ASYNC 都是"把本地数据写到远端 GM"，为什么前者需要 staging tile 参数而后者不需要？

**参考答案**：TPUT 走 MTE 搬运流水线，数据通路是"本地 GM → UB 上的 staging tile → 远端 GM"（[pto_comm_inst.hpp:L27-L29](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/comm/pto_comm_inst.hpp#L27-L29) 注释），tile 是必经中转站；TPUT_ASYNC 走 SDMA/URMA DMA 引擎，通路是"GM → DMA engine → GM"（[docs/isa/comm/README.md:L12-L14](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/isa/comm/README.md#L12-L14)），不经过片上 tile，完成同步改用返回的 `AsyncEvent` 句柄（`.Wait()`/`.Test()`）。

**练习 2**：TWAIT 和 TTEST 的区别是什么？各自适合什么场景？

**参考答案**：TWAIT 是阻塞等待——自旋轮询直到**全部**信号满足比较条件才返回（[pto_comm_inst.hpp:L123-L128](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/comm/pto_comm_inst.hpp#L123-L128)），适合"数据没到就不能往下算"的硬依赖；TTEST 是非阻塞测试——立即返回 bool 表示当前条件是否已满足（[pto_comm_inst.hpp:L137-L142](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/comm/pto_comm_inst.hpp#L137-L142)），适合"通信与计算重叠"场景：先 TTEST，条件不满足就先做别的计算，稍后再测。

### 4.2 通信类型定义：comm_types.hpp

#### 4.2.1 概念说明

所有通信指令共享的类型集中在 `comm_types.hpp`，分三组：

1. **地址描述组**：`ParallelGroup`（集合通信的参与方列表）、`Signal`/`GlobalSignal`/`Signal2D`（信号量的 GlobalTensor 化）。
2. **行为枚举组**：`NotifyOp`（挂牌方式）、`WaitCmp`（等待比较算子）、`ReduceOp`（归约算子）、`DmaEngine`（DMA 引擎选择）、`CollEngine`（集合通信后端引擎）、`AtomicType`（原子写类型，定义在 common/constants.hpp）。
3. **异步句柄组**：`AsyncEvent`（完成句柄）与 `AsyncSession`（引擎无关会话，定义在 async_common/async_types.hpp）。

设计哲学与 GlobalTensor 一脉相承：**类型只是元数据视图，不搬数据**。例如 `ParallelGroup` 就是一个裸指针 + 两个 int，设备侧不做任何动态分配（NPU kernel 里 `std::vector` 这类容器不可用）。

#### 4.2.2 核心流程

集合通信的寻址模型：

```text
ParallelGroup<GlobalData> group
 ├── group[0]  →  rank 0 的 GlobalTensor 视图（其 data() 指向该卡 GM 地址）
 ├── group[1]  →  rank 1 的视图
 ├── ...
 ├── nranks    →  参与方数量
 └── rootIdx   →  root 在组内的下标（所有 rank 必须传同一个值）
```

root（且只有 root）调用 `TREDUCE(group, dst, accTile, recvTile, op)` 时，实现层会用 `group[r]` 逐个取到各 rank 源数据的远端地址，把它们搬来归约。信号量的类型模型则更简单：

- `Signal` = 一个全静态的 1 元素 int32 GlobalTensor（标量旗子）。
- `Signal2D<Rows, Cols>` = Rows×Cols 的 int32 信号矩阵（一把旗子），DIM_3 步长留 DYNAMIC 以支持"大网格里的子区域视图"。

#### 4.2.3 源码精读

**ParallelGroup——集合通信的参与方描述**：

- [include/pto/comm/comm_types.hpp:L35-L72](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/comm/comm_types.hpp#L35-L72) —— 定义 `ParallelGroup<GlobalData>`：`tensors` 指向外部 `GlobalData` 对象数组（注意是对象数组不是指针数组），`nranks`/`rootIdx` 是两个 int；`operator[]` 带越界断言。文件头注释（L26-L33）明确说明它是轻量视图、设备侧零动态分配，每个元素代表"该组内 rank 的 GlobalTensor 视图"。
- [include/pto/comm/comm_types.hpp:L53-L56](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/comm/comm_types.hpp#L53-L56) —— 推荐的工厂用法 `ParallelGroup::Create(tensorArray, size, rootIdx)`；注释强调 rootIdx 是 root 在**组内**的下标而非调用者自己的 rank。

**信号量的三种形态**：

- [include/pto/comm/comm_types.hpp:L201-L205](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/comm/comm_types.hpp#L201-L205) —— `Signal` 是 `GlobalTensor<int32_t, Shape<1,1,1,1,1>, Stride<1,1,1,1,1>, Layout::ND>` 的别名：单元素、全静态；`GlobalSignal` 则是任意 GlobalTensor 的别名模板，供信号指令的形参使用。
- [include/pto/comm/comm_types.hpp:L221-L234](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/comm/comm_types.hpp#L221-L234) —— `Signal2D<Rows, Cols>` 继承自 int32 GlobalTensor，提供两个构造函数：稠密构造（行步长自动取 Cols）与跨步构造（自定义 DIM_3 步长，用于从更大的信号网格中切子区域）。

**行为枚举组**（每个枚举都是 uint8_t，开销为零）：

- [include/pto/comm/comm_types.hpp:L89-L105](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/comm/comm_types.hpp#L89-L105) —— `NotifyOp`（AtomicAdd：`signal += value`；Set：`signal = value`）与 `WaitCmp`（EQ/NE/GT/GE/LT/LE 六种比较）。
- [include/pto/comm/comm_types.hpp:L111-L115](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/comm/comm_types.hpp#L111-L115) —— `ReduceOp`（Sum/Max/Min），对应 TREDUCE 的三种归约。
- [include/pto/comm/comm_types.hpp:L121-L124](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/comm/comm_types.hpp#L121-L124) —— `DmaEngine`：SDMA 与 URMA（注释标明 URMA 仅 HCCP V2 Jetty / NPU_ARCH 3510 支持）。

**异步句柄**：

- [include/pto/comm/comm_types.hpp:L179-L189](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/comm/comm_types.hpp#L179-L189) —— `AsyncEvent`：`handle + engine` 两个成员，`valid()` 判句柄非零，`Wait(session)` 阻塞等完成、`Test(session)` 非阻塞查询。它替代了同步指令的 RecordEvent 角色。
- [include/pto/comm/async_common/async_types.hpp:L128-L148](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/comm/async_common/async_types.hpp#L128-L148) —— `AsyncSession`：引擎无关的会话对象，一次构建（`BuildAsyncSession<engine>()`）、处处传入；内部携带 GM 上下文、UB 临时缓冲、syncId、通道组、块大小、URMA 的 destRankId/qpIdx 等字段，SDMA 运行时状态（队列头尾、完成水位）也寄存在会话里跨多次 Post/Wait 传递。

#### 4.2.4 代码实践

**实践目标**：用信号类型拼出一个"两卡握手"的最小类型准备清单（只写类型声明，不写指令调用）。

**操作步骤**：

1. 阅读上列三处信号类型定义。
2. 手写（纸上或本地草稿文件，**不要改仓库源码**）一段示例代码（示例代码，非项目原有）：

```cpp
// 示例代码：仅演示类型用法
__gm__ int32_t* flagBuf;          // 假设已指向对端卡的信号区
comm::Signal       doneFlag(flagBuf);            // 单旗子
comm::Signal2D<4, 8> flagGrid(flagBuf + 16);     // 4x8 信号矩阵（稠密）
comm::Signal2D<4, 8> subGrid(flagBuf + 16, 128); // 同形状，但取自 128 列大网格的子区
```

3. 对照 `Signal2D` 的两个构造函数，解释第三个对象与第二个的区别。

**需要观察的现象**：`Signal`/`Signal2D` 的构造都是 O(1) 的元数据装配（指针 + shape/stride），不产生任何内存拷贝；`subGrid` 因为传了自定义步长 128，同一形状下可以映射到大网格中不相邻的行。

**预期结果**：能说出"flagGrid 的行步长是 8（自动），subGrid 的行步长是 128（显式）"；两者 shape 相同但底层地址布局不同。完整可编译验证需在 kernel 工程中（待本地验证，本练习以类型语义理解为主）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `ParallelGroup` 用"对象数组 + 裸指针"而不是 `std::vector<GlobalTensor>`？

**参考答案**：kernel 运行在 AICORE 上，设备侧不支持 `std::vector` 这类需要动态内存分配的容器；`comm_types.hpp` L26-L29 的注释明确写了"no dynamic memory allocation on device side"。数组由外部（通常是 GM 或常量区）持有，`ParallelGroup` 只是包了一个指针和两个 int 的视图。

**练习 2**：信号量为什么强制 `int32_t`？

**参考答案**：这是实现层的硬约束——`TNOTIFY_IMPL`/`TWAIT_IMPL`/`TTEST_IMPL` 都有 `static_assert(std::is_same_v<..., int32_t>)` 编译期检查（如 [include/pto/comm/a2a3/TNotify.hpp:L40](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/comm/a2a3/TNotify.hpp#L40)、[TWait.hpp:L59](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/comm/a2a3/TWait.hpp#L59)）。底层原子写走 `st_atomic<int32_t>`（硬件原子指令按 S32 语义配置，见 TNotify.hpp:L46），信号比较也按 32 位整数实现，统一类型保证跨卡读写语义一致。

### 4.3 驱动多引擎设计：一条 API，多条硬件通路

#### 4.3.1 概念说明

「驱动多引擎」有两层含义：

**第一层：不同类别的通信指令驱动不同的硬件引擎。** 11 条指令按实现路径可归入四条引擎通路：

| 通路 | 引擎 | 指令 | 特点 |
|---|---|---|---|
| A. MTE staging 通路 | MTE2/MTE3 搬运流水线（经 UB tile） | TPUT、TGET | 复用 TLOAD/TSTORE 的 burst DMA；与计算指令同用事件机制；大块数据自动分块 + ping-pong |
| B. Vector 通路（AIV 引擎） | Vector 计算单元 | TGATHER/TSCATTER/TBROADCAST/TREDUCE 的默认路径 | 集合通信退化为"多次 TGET + 片上计算"，归约直接复用 TADD/TMAX/TMIN |
| C. DMA 引擎通路 | SDMA / URMA 独立 DMA 引擎 | TPUT_ASYNC、TGET_ASYNC | GM→GM 直达，不占 MTE/Vector；URMA 仅 A5（NPU_ARCH 3510）且需 CANN ≥ 9.1.0 |
| D. GM 直写通路 | 硬件原子指令 + 缓存维护 | TNOTIFY（写）、TWAIT/TTEST（读） | 单字（4 字节）粒度，代价极低，用于同步而非数据 |

此外 A5 上集合通信还有第五条通路——**CCU 硬件引擎**（`CollEngine::CCU`）：AIV 核只负责"敲门"（触发 CKE 门控），真正的集合通信由 CCU 硬件完成。这是编译期模板参数选择的另一条引擎分支。

**第二层：同一 API 的实现按「架构 × 后端」编译期分发。** 这与计算指令的三段式（公共 API → `*_IMPL` 分发 → 架构实现）完全同构，由 `pto_comm_instr_impl.hpp` 完成。

#### 4.3.2 核心流程

编译期分发链（以 `pto::comm::TGET` 为例）：

```text
kernel 代码: pto::comm::TGET(dst, src, tile)
   │
   ▼ pto/comm/pto_comm_inst.hpp:L82-L89     公共 API 薄壳（WaitAllEvents + 转发）
   │
   ▼ pto/comm/pto_comm_instr_impl.hpp:L16   编译期互斥 include：
   │     ├─ __CCE_AICORE__ + PTO_NPU_ARCH_A2A3 → a2a3/TGet.hpp   （真机 A2/A3）
   │     ├─ __CCE_AICORE__ + PTO_NPU_ARCH_A5   → a5/TGet.hpp     （真机 A5）
   │     └─ __CPU_SIM                          → cpu/comm/TGet.hpp（CPU 仿真桩）
   ▼ TGET_IMPL(dst, src, tile)               三种后端签名一致
```

运行期引擎选择（集合通信与异步传输还多一层编译期引擎参数）：

```text
TGATHER<CollEngine::AIV>(group, dst, tile)   → 走 TLOAD/TSTORE 组合（默认）
TGATHER<CollEngine::CCU>(group, dst, tile, ccuCtx) → AIV 触发 CKE 门控，CCU 硬件执行
TPUT_ASYNC<DmaEngine::SDMA>(dst, src, session)     → SDMA 引擎
TPUT_ASYNC<DmaEngine::URMA>(dst, src, session, peer) → URMA 引擎（仅 A5）
```

两条流水线间同步量与硬件通路的对应关系：通路 A 参与事件调度（返回 RecordEvent），通路 C 不参与（返回 AsyncEvent，用会话查询），通路 D 完全绕开事件（TWAIT 自己自旋）。

#### 4.3.3 源码精读

**通路 A：MTE staging（TGET 的搬运骨架）**：

- [include/pto/comm/a2a3/TGet.hpp:L46-L56](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/comm/a2a3/TGet.hpp#L46-L56) —— `TgetTransferOnce`：一次单块远读的完整序列是 `TLOAD(tile, 远端视图) → set/wait(MTE2→MTE3) → TSTORE(本地视图, tile) → set/wait(MTE3→MTE2)`。**通信指令的实现内部直接调用了计算指令集的 TLOAD/TSTORE**——远端 GM 在寻址上与本地 GM 同构，跨卡读本质上是"对远端地址做一次 TLOAD"。这正是"通信与计算同级 tile 抽象"的落地。
- [include/pto/comm/a2a3/TGet.hpp:L248-L284](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/comm/a2a3/TGet.hpp#L248-L284) —— `TGET_IMPL` 入口：三组 `static_assert`（src/dst 元素类型一致、tile 类型匹配、layout 匹配）+ 空数据早退 + "装得下就一次搬 / 装不下走分块派发"的分流。分块路径（L282）沿用与 TPUT 相同的 2-D 滑窗策略。
- [include/pto/comm/a2a3/TGet.hpp:L160-L186](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/comm/a2a3/TGet.hpp#L160-L186) —— `TgetChunkedSingle`：把源/目的的 5 维 stride 摘成数组，按 tile 有效区在 DIM_3×DIM_4 平面上滑窗切块；满足条件时还启用"tile 内对半 ping-pong"（把一个 staging tile 劈成上下两半交替使用，L60-L89 的 `TgetIntraPingPongOneChunk`），让上一块的 TSTORE 与下一块的 TLOAD 重叠。

**通路 B：Vector 引擎（TREDUCE 复用计算指令）**：

- [include/pto/comm/a2a3/TReduce.hpp:L27-L42](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/comm/a2a3/TReduce.hpp#L27-L42) —— `ReduceTiles`：TREDUCE 的归约核就是把 PTO 计算指令 `TADD`/`TMAX`/`TMIN` 按 `ReduceOp` 三选一调用。集合通信在 AIV 引擎下完全由既有指令组合实现，没有专属 intrinsic。
- [include/pto/comm/a2a3/TReduce.hpp:L46-L58](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/comm/a2a3/TReduce.hpp#L46-L58) —— `TreduceSimple`：root 侧先 TLOAD 自己的数据进 acc tile，随后循环 TLOAD 各远端 rank 的数据进 recv tile，经 `MTE2→V` 事件唤醒 Vector 归约——一条集合指令内部就是标准的"搬运 + 计算"事件流水。

**通路 C：DMA 引擎（SDMA/URMA）**：

- [include/pto/comm/pto_comm_inst.hpp:L343-L349](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/comm/pto_comm_inst.hpp#L343-L349) —— `TPUT_ASYNC` 公共 API：模板参数选引擎（默认 SDMA），会话对象传入，返回 `AsyncEvent`。注释明确工作流："Build once with `BuildAsyncSession<engine>()`, then pass to all calls"。
- [include/pto/comm/pto_comm_inst.hpp:L351-L367](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/comm/pto_comm_inst.hpp#L351-L367) —— 带 `peer` 参数的变体被 `#if defined(PTO_NPU_ARCH_A5) || defined(__CPU_SIM)` 门控：URMA 需要显式对端编号来选 SQ/CQ/MR，SDMA 从 GlobalTensor 虚地址自行寻址（peer 被忽略）。这是"引擎差异封装在重载 + 宏门控里"的典型做法。

**通路 D + CCU 通路（枚举与上下文）**：

- [include/pto/comm/comm_types.hpp:L132-L135](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/comm/comm_types.hpp#L132-L135) —— `CollEngine`：AIV（默认 tile 路径）与 CCU（AIV 触发 CKE 门控、CCU 硬件执行集合通信）两个值。
- [include/pto/comm/comm_types.hpp:L153-L171](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/comm/comm_types.hpp#L153-L171) —— `CcuInputSource`（CCU 输入 HBM 由 host 预填还是 AIV 触发核顺路 TSTORE 进去，后者支撑 AIV 前级计算与 CCU 归约的无缝融合）与 `CcuTriggerContext`（host 下发的 CKE 槽位虚地址、触发掩码、自身下标等不透明上下文）。
- [include/pto/comm/pto_comm_inst.hpp:L153-L167](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/comm/pto_comm_inst.hpp#L153-L167) —— `TGATHER` 的 `if constexpr (engine == ...)` 编译期引擎分派：AIV 分支走 `TGATHER_IMPL`，CCU 分支要求第一个可变参数必须是 `CcuTriggerContext`（`static_assert` 把关）并走 `TGATHER_CCU_IMPL`。四个集合指令都是这个双分支结构。
- [include/pto/comm/a2a3/TNotify.hpp:L37-L61](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/comm/a2a3/TNotify.hpp#L37-L61) —— `TNOTIFY_IMPL`：AtomicAdd 分支配置 `set_st_atomic_cfg(ATOMIC_S32, ATOMIC_SUM)` 后用 `st_atomic` 硬件原子指令写远端；Set 分支直接解引用 volatile GM 指针赋值。两个分支都在写前后执行 `dcci`（缓存行维护，防止本核缓存把新值覆盖或读到旧值）并以 `dsb(DSB_DDR)` 确保落盘，最后 `pipe_barrier(PIPE_ALL)`。
- [include/pto/comm/a2a3/TWait.hpp:L50-L102](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/comm/a2a3/TWait.hpp#L50-L102) —— `TWAIT_IMPL`：按 5 维 shape/stride 完整遍历信号区，每个信号先 `dcci` 再读（绕开缓存取最新值），任一不满足条件就重来；自旋每 64 轮插一次 `pipe_barrier`，超过 1 亿次断言"疑似死锁"。这是把 u6-l1 学过的"SYNCALL 不保证可见性、需 dcci/dsb"应用到跨卡场景的实例。

**编译期分发与架构门控**：

- [include/pto/comm/pto_comm_instr_impl.hpp:L16-L54](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/comm/pto_comm_instr_impl.hpp#L16-L54) —— 真机侧分发：`__CCE_AICORE__` 下按 `PTO_NPU_ARCH_A2A3` / `PTO_NPU_ARCH_A5` 互斥 include 两套实现（注意两类架构是并列的 `#ifdef` 块，靠 arch_macro 保证只有一个生效）。
- [include/pto/comm/pto_comm_instr_impl.hpp:L56-L71](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/comm/pto_comm_instr_impl.hpp#L56-L71) —— CPU 仿真侧分发：include `pto/cpu/comm/` 下的桩实现；注意 CPU 路径没有异步目录——`TGET_ASYNC_IMPL` 直接定义在 `cpu/comm/TGet.hpp` 里。
- [include/pto/common/arch_macro.hpp:L19-L38](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/arch_macro.hpp#L19-L38) —— 架构号翻译表中，Kirin 系（3113/3003/5101）与 A6（9201）都定义了 `PTO_COMM_NOT_SUPPORTED`（这些架构无通信能力）；3510 额外定义 `PTO_URMA_SUPPORTED`。
- [include/pto/common/pto_instr.hpp:L19-L21](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/pto_instr.hpp#L19-L21) —— 计算指令主头文件在"`__COSTMODEL` 或 `PTO_COMM_NOT_SUPPORTED`"时干脆不 include 通信头：CostModel 后端与无通信架构上通信 API 整体不存在（用错直接编译失败，而非运行期报错）。
- [include/pto/pto-inst.hpp:L30](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/pto-inst.hpp#L30) —— 统一入口包含 `pto/common/pto_instr.hpp`，因此正常 NPU/CPU 工程里只 include `pto/pto-inst.hpp` 即可使用 `pto::comm::*`（真实算子里的调用见 `kernels/manual/a2a3/tget_bandwidth/tget_bandwidth_kernel.cpp`，写法即 `pto::comm::TGET(...)`、`pto::comm::TGET_ASYNC(...)`）。

**CPU 仿真桩对照**：

- [include/pto/cpu/comm/TGet.hpp:L57-L67](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/cpu/comm/TGet.hpp#L57-L67) —— CPU 桩的 `TGET_IMPL`：不看 tile、不搞分块，直接按 shape/stride 五重循环逐元素拷贝（staging tile 形参被忽略）。单缓冲/双缓冲两个重载都落到同一拷贝。
- [include/pto/cpu/comm/TGet.hpp:L69-L74](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/cpu/comm/TGet.hpp#L69-L74) —— CPU 桩的 `TGET_ASYNC_IMPL`：同步拷完后返回 `AsyncEvent(0, engine)`（句柄为 0，`valid()` 为 false）。结论与 u2-l3/u3 一致：**CPU 仿真只验证功能正确性，引擎差异、分块策略、ping-pong 重叠、缓存维护在桩里全部不存在**，性能与同步行为必须上真机验证。

#### 4.3.4 代码实践

**实践目标**：用 grep 量化「同一指令名 × 多后端实现」的组织方式，验证通信层与计算层使用相同的隔离纪律。

**操作步骤**：

1. 在仓库根目录执行：

```bash
grep -rl "TGET_IMPL" include/pto --include='*.hpp' | sort
```

2. 对输出按目录归类（预期命中：comm 公共层、a2a3、a5、cpu/comm，可能还有 async 相关文件）。
3. 再执行：

```bash
grep -c "__CPU_SIM\|__CCE_AICORE__" include/pto/comm/pto_comm_instr_impl.hpp
grep -n "PTO_COMM_NOT_SUPPORTED" include/pto/common/arch_macro.hpp
```

4. 打开 `include/pto/comm/a5/TGet.hpp` 与 `include/pto/comm/a2a3/TGet.hpp` 的头部（各看前 60 行即可），对比两者 include 与注释差异。

**需要观察的现象**：

- 第 1 步应显示 `TGET_IMPL` 这个符号在公共层（声明于使用处）、a2a3、a5、cpu/comm 中各有一份，且互相从不同时被编译——由 `pto_comm_instr_impl.hpp` 的互斥 include 保证。
- 第 4 步应发现 a5 目录的同步指令头多为薄包装（按模块 README L36 的说明："a5/ T*.hpp — Sync instructions (include a2a3/ counterparts)"），即 A5 复用 A2/A3 的同步实现，只在异步引擎（SDMA+URMA）上分叉。

**预期结果**：能画出 TGET 的「1 个公共 API → 3 类 IMPL（a2a3 / a5 / cpu）」分发树；能说出 Kirin 与 A6 架构上 `pto::comm` 命名空间整体不可用（`PTO_COMM_NOT_SUPPORTED`）。若第 1 步输出与预期不符（例如多出意料之外的文件），把实际清单记录下来并核对 include 链（待本地验证）。

#### 4.3.5 小练习与答案

**练习 1**：`TREDUCE<CollEngine::AIV>` 在 A2/A3 上没有专属硬件指令，它是怎么完成归约的？

**参考答案**：退化为"搬运 + 计算指令组合"：root 用 TLOAD 依次把各 rank（含自己）的数据搬进 tile，归约核 `ReduceTiles` 直接调用 PTO 计算指令 TADD/TMAX/TMIN（[TReduce.hpp:L27-L42](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/comm/a2a3/TReduce.hpp#L27-L42)），由 Vector 引擎执行，事件按 MTE2→V 配对（[TReduce.hpp:L46-L58](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/comm/a2a3/TReduce.hpp#L46-L58)）。这就是"通信与计算同级 tile 抽象"的直接收益：集合通信不需要独立的编程模型。

**练习 2**：TNOTIFY 里那串 `dcci` + `dsb(DSB_DDR)` 是干什么的？去掉行不行？

**参考答案**：它们做缓存与落盘维护。跨卡信号写在 GM 上，本核缓存里可能残留旧值（写回时会把旧值盖到新值上）或读到陈旧数据，`dcci` 在写前写后各失效/清理一次缓存行，`dsb(DSB_DDR)` 屏障确保写真正到达 DDR、对端可见（[TNotify.hpp:L44-L57](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/comm/a2a3/TNotify.hpp#L44-L57)）。去掉后功能在 CPU 仿真上仍"看起来对"（桩里没有缓存），但真机上会出现信号丢失/读到旧值的偶发错误——这是 u6-l1"SYNCALL 只保证到达、不保证可见"结论在跨卡场景的再现。TWAIT 读侧每个信号也先 `dcci` 再读（[TWait.hpp:L81-L87](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/comm/a2a3/TWait.hpp#L81-L87)）。

**练习 3**：为什么 `TPUT_ASYNC` 返回 `AsyncEvent` 而不是像 `TPUT` 一样返回 `RecordEvent`？

**参考答案**：两者挂在不同的同步体系上。TPUT 走 MTE 流水线，天然处于指令间事件调度的世界里，`RecordEvent` 可被同 kernel 内的后续指令等待；TPUT_ASYNC 交给独立的 SDMA/URMA DMA 引擎执行，脱离了 AICORE 流水线事件体系，完成情况只能通过引擎自身的完成队列查询，所以返回携带引擎句柄的 `AsyncEvent`，用 `.Wait(session)/.Test(session)` 同步（[comm_types.hpp:L179-L189](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/comm/comm_types.hpp#L179-L189)）。

## 5. 综合实践

**任务：制作一份「通信 ISA 全景分类卡」，并为每条指令标注引擎通路与后端矩阵。**

具体要求：

1. 列出全部 11 条通信指令，按四大类分组。
2. 为每条指令填四列：**数据通路**（如 GM→UB→GM / GM→DMA→GM）、**引擎**（MTE / Vector(AIV) / SDMA / URMA / CCU / 硬件原子）、**参与方**（单边 or root 发起）、**CPU 仿真桩行为**（真实现 / 简化拷贝 / 空桩）。信息来源限定为本讲读过的文件：`docs/isa/comm/README.md`、`include/pto/comm/README.md`、`pto_comm_inst.hpp`、`comm_types.hpp`、`a2a3/` 各头文件顶部注释、`cpu/comm/` 桩。
3. 追一条完整分发链并手写成三行式：以 `TGET` 为例，「公共 API（pto_comm_inst.hpp L82）→ 分发器（pto_comm_instr_impl.hpp L21）→ 实现（a2a3/TGet.hpp L249 / cpu/comm/TGet.hpp L58）」。再任选一条集合指令和一条异步指令重复一次。
4. 最后回答一个开放问题：如果你的 kernel 要在计算 (TMatmul) 的同时后台搬一块数据到邻卡，你会从 11 条指令里选哪条？为什么？（提示：从"哪条通路不占用 MTE/Vector 流水线"出发思考。）

参考答案要点（第 4 问）：选 `TPUT_ASYNC`（或 `TGET_ASYNC`）——它们走独立 SDMA/URMA DMA 引擎，不与 TMatmul 争用 MTE 搬运流水线和 Cube/Vector 计算单元，启动后立即返回 AsyncEvent，主循环继续计算，末尾 `event.Wait(session)` 收尾即可；这正是 u7-l4/u7-l5 计算-通信重叠的机制基础。

## 6. 本讲小结

- PTO 通信 ISA 共 **11 条指令、四大类**：同步点对点（TPUT/TGET）、异步点对点（TPUT_ASYNC/TGET_ASYNC）、信号同步（TNOTIFY/TWAIT/TTEST）、集合通信（TGATHER/TSCATTER/TBROADCAST/TREDUCE）。
- 通信指令与计算指令**同级且同构**：公共 API 都是「WaitAllEvents → `*_IMPL` → 返回事件」的薄壳，远端读写内部直接复用 TLOAD/TSTORE，集合归约直接复用 TADD/TMAX/TMIN。
- 核心类型集中在 `comm_types.hpp`：`ParallelGroup` 是"对象数组 + 两个 int"的零分配视图；`Signal`/`Signal2D` 把 int32 信号量 GlobalTensor 化；`NotifyOp/WaitCmp/ReduceOp/DmaEngine/CollEngine` 是零开销 uint8 枚举；`AsyncEvent`/`AsyncSession` 支撑异步 DMA 完成 synchronization。
- 「多引擎」是真实现分层的钥匙：MTE staging 通路（同步点对点）、Vector/AIV 通路（默认集合通信）、SDMA/URMA DMA 通路（异步点对点）、GM 原子直写通路（信号），外加 A5 的 CCU 硬件集合通信分支——引擎选择全部在编译期完成（模板参数 + `if constexpr` + 宏门控）。
- 后端矩阵按「架构 × 后端」互斥编译：a2a3 / a5 / cpu/comm 三套 `*_IMPL` 由 `pto_comm_instr_impl.hpp` 分发；Kirin 系与 A6 定义 `PTO_COMM_NOT_SUPPORTED`、CostModel 后端不含通信，这些环境上通信 API 整体不存在。
- CPU 仿真桩只保证功能正确（退化为逐元素拷贝、AsyncEvent 句柄恒为 0），缓存维护（dcci/dsb）、分块与 ping-pong 重叠、引擎差异都必须上真机验证。

## 7. 下一步学习建议

下一讲 **u7-l2「点对点通信与信号同步：TGet/TPut、TNotify/TWait/TTest」** 将深入本讲的全景骨架，逐条精读五条点对点/信号指令的完整实现——包括 TPUT 的原子写变体、TGET 的 2-D 滑窗分块与 tile 内对半 ping-pong 的完整代码。建议先自行通读 `include/pto/comm/a2a3/TPut.hpp` 与 `TGet.hpp` 的分块相关函数（`TgetChunkedDispatch`、`TgetPingPongProcessChunk`），带着"分块策略如何与 u6-l2 的 double buffer 模式对应"这个问题进入下一讲。后续 u7-l4 会展开 SDMA/URMA 引擎细节与带宽实测，u7-l5 则把本讲的指令组合成 gemm_ar 计算-通信融合算子。
