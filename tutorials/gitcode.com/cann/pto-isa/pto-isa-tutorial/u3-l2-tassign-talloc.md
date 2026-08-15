# TASSIGN 与缓冲管理：Tile 显式绑定、TAlloc/TFree

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清 Manual 模式下「Tile 变量」与「片上缓冲地址」的关系，理解为什么必须用 TASSIGN 显式绑定。
2. 掌握 TASSIGN 的两种形式：运行期地址（`TASSIGN(obj, addr)`）与编译期地址（`TASSIGN<Addr>(obj)`，带 SA-0351～SA-0354 静态检查）。
3. 理解 TAlloc/TFree 的真实语义：它们管理的是 `TPipe` 生产者-消费者 FIFO 的槽位生命周期（与 TPUSH/TPOP 配套），而不是通用的片上缓冲 malloc/free。
4. 区分 Auto 模式（编译器自动分配缓冲、自动同步，`TASSIGN` 变空操作）与 Manual 模式（开发者手工摆地址、手工同步）的边界。

> 一个先纠偏的说明：本讲大纲里「TAlloc/TFree」容易被理解为「片上缓冲的分配/释放」。读完源码后会发现，PTO 里片上缓冲的"分配"其实就是 **TASSIGN 手工摆放地址**；而 `TALLOC`/`TFREE` 是 **跨核流水线（TPipe）FIFO 槽位** 的申请与归还指令，服务于 Cube 核与 Vector 核之间的数据交接。本讲按源码真实语义讲解。

## 2. 前置知识

- **Tile 变量 vs 片上存储**（回顾 u2-l2）：`Tile<TileType::Vec, half, R, C>` 只是编译期类型 + 一个数据指针，它本身不"拥有"存储；`TileType` 决定它应该落在哪块物理存储上（Vec→UB、Mat→L1、Left→L0A、Right→L0B、Acc→L0C）。
- **存储层级**（回顾 u1-l4/u3-l1）：GM（全局内存）→ UB/L1 → L0A/L0B/L0C，容量逐级变小。A2A3 上 UB 为 192KB、L1 为 512KB、L0A/L0B 各 64KB、L0C 为 128KB。
- **生产者-消费者**：一条流水线（或一个核）产出数据，另一条消费它。经典解法是环形 FIFO：生产者往槽里写、挂牌；消费者等牌、读槽、归还槽。`TPUSH/TPOP/TALLOC/TFREE` 就是这个协议的 PTO 指令化。
- **编译期检查**：C++ 模板非类型参数（如 `TASSIGN<0x1000>(t)` 中的 `0x1000`）在编译期可见，所以容量/越界/对齐检查可以用 `static_assert` 在编译期完成，错误成本为零。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [docs/isa/TASSIGN.md](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/isa/TASSIGN.md) | TASSIGN 的 ISA 文档：两种形式、各存储容量表、静态检查表 |
| [include/pto/common/pto_instr.hpp](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/pto_instr.hpp) | 用户 API 层：TASSIGN 两个重载、TPUSH/TPOP/TALLOC/TFREE 薄壳 |
| [include/pto/npu/a2a3/TAssign.hpp](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/npu/a2a3/TAssign.hpp) | A2A3 真机实现：Tile 按整型地址重解释绑定，GlobalTensor 按指针绑定 |
| [include/pto/cpu/TAssign.hpp](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/cpu/TAssign.hpp) | CPU 仿真实现：地址经 NPUMemoryModel 翻译后再绑定 |
| [include/pto/common/tassign_check.hpp](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/tassign_check.hpp) | 编译期地址的静态检查（SA-0351～SA-0354）与各 TileType 的容量/对齐特征 |
| [docs/isa/TALLOC.md](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/isa/TALLOC.md) | TALLOC 的 ISA 文档：GlobalData 流程、TPipe 约束 |
| [include/pto/npu/a2a3/TAlloc.hpp](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/npu/a2a3/TAlloc.hpp) | A2A3 实现：等空间 → 算槽地址 → 绑定 GlobalTensor |
| [include/pto/npu/a2a3/TFree.hpp](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/npu/a2a3/TFree.hpp) | A2A3 实现：GlobalData 槽位归还（TileData 版为 no-op） |
| [include/pto/npu/a2a3/TPush.hpp](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/npu/a2a3/TPush.hpp) | TPipe 模板定义：方向、槽大小/槽数、SyncPeriod |
| [docs/isa/TFREE.md](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/isa/TFREE.md) | TFREE 的 ISA 文档：TileData 流与 GlobalData 流的差异 |
| [demos/baseline/add/csrc/kernel/add_custom.cpp](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/demos/baseline/add/csrc/kernel/add_custom.cpp) | Manual 模式实战样板：手工规划 UB 地址 + ping-pong |
| [tests/cpu/st/testcase/tpushpop/main.cpp](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/cpu/st/testcase/tpushpop/main.cpp) | CPU 可运行测试：TASSIGN + TPUSH/TPOP/TFREE 全流程 |
| [docs/auto_mode/Auto_Mode_Overview.md](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/auto_mode/Auto_Mode_Overview.md) | Auto 模式总览：编译器自动分配与自动同步 |

## 4. 核心概念与源码讲解

### 4.1 TASSIGN 绑定：把 Tile 钉到片上地址

#### 4.1.1 概念说明

在 Manual 模式下，声明一个 `Tile` 变量只是声明了「一块固定形状的片上数据的名字」，它的数据指针还不知道指向哪里。片上存储（UB/L1/L0A/L0B/L0C）没有操作系统帮你 malloc，**谁来摆、摆在哪里，完全由开发者决定**——这就是 manual placement（手工摆放）。TASSIGN 就是完成「绑定」的那条指令：

- 对 **Tile**：把一个整型数值解释为该片上存储的偏移地址，写入 Tile 的数据指针。
- 对 **GlobalTensor**：把一个 `__gm__` 指针设置为视图的起始地址（Add 示例循环里反复用它推进视图）。

为什么必须显式绑定？因为性能。上一讲的 ping-pong 双缓冲能工作，前提是同一组 Tile 在 UB 里的地址是开发者亲手排布、互不重叠、且轮转可复用的。编译器（Manual 模式下）不替你做这件事。

#### 4.1.2 核心流程

```
声明 Tile（编译期形状/类型）
        │
        ▼
TASSIGN(tile, addr)   ← 运行期地址：不做编译期边界检查
   或
TASSIGN<Addr>(tile)   ← 编译期地址：先过 SA-0351~0354 静态检查再转发到运行期形式
        │
        ▼
tile 的数据指针 ← addr（此后 TLOAD/TSTORE/计算指令都在这块存储上操作）
```

两种形式的分工：

| 形式 | 签名 | 检查时机 | 适用对象 |
|------|------|----------|----------|
| Form 1 运行期 | `TASSIGN(T& obj, AddrType addr)` | 无编译期边界检查（地址值编译期不可见） | Tile（整型地址）与 GlobalTensor（指针） |
| Form 2 编译期 | `TASSIGN<Addr>(T& obj)` | `static_assert` 四连检查 | 仅 Tile / ConvTile |

#### 4.1.3 源码精读

API 层的两个重载在 `pto_instr.hpp` 中：

[include/pto/common/pto_instr.hpp:27-44](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/pto_instr.hpp#L27-L44)：Form 1 直接经 `MAP_INSTR_IMPL` 转发到后端 `TASSIGN_IMPL`；Form 2（模板非类型参数 `Addr`）先用 `detail::tassign_static_check<T, Addr>` 触发编译期检查，然后**转发回 Form 1**——两种形式最终走同一条运行期路径。

A2A3 真机实现只有十几行：

[include/pto/npu/a2a3/TAssign.hpp:17-35](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/npu/a2a3/TAssign.hpp#L17-L35)：这段代码用 `if constexpr` 分两支——Tile 分支要求 `AddrType` 必须是整型，把 `addr` 重解释后调用 `obj.assignData(...)` 写入 Tile 的数据指针；GlobalTensor 分支要求 `AddrType` 必须是**元素类型匹配的指针**，调用 `obj.SetAddr(addr)`。注意第 21/25 行的 `#ifndef __PTO_AUTO__`：**Auto 模式下对 Tile 的 TASSIGN 是空操作**（4.3 节展开）。

编译期检查的完整逻辑在 `tassign_check.hpp`：

[include/pto/common/tassign_check.hpp:140-179](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/tassign_check.hpp#L140-L179)：`tassign_static_check` 结构体在实例化时依次触发四条 `static_assert`——SA-0351（该架构上这块存储存在）、SA-0352（tile 字节数 ≤ 容量）、SA-0353（`Addr + tile_bytes ≤ 容量`，越界）、SA-0354（`Addr % alignment == 0`，A2A3 上各类存储均要求 32 字节对齐）。tile 字节数按 `Rows * Cols * sizeof(DType)` 计算（ConvTile 用 `bufferSize`），见 [tassign_check.hpp:41-58](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/tassign_check.hpp#L41-L58)；每个 TileType 对应哪块存储、多大容量，由 `BufferTraits` 特化给出，见 [tassign_check.hpp:64-128](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/tassign_check.hpp#L64-L128)。

还有一个关键分支：

[include/pto/common/tassign_check.hpp:17-29](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/tassign_check.hpp#L17-L29)：CPU 仿真与 CostModel 后端**不建模片上缓冲容量**，`tassign_static_check` 被定义为空结构体——所以 SA-0352/0353 这类「UB 放不下」的错误只能在 NPU 编译时暴露，CPU 仿真下不会拦截。

CPU 仿真版的 `TASSIGN_IMPL`：

[include/pto/cpu/TAssign.hpp:22-37](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/cpu/TAssign.hpp#L22-L37)：与真机版的差异只在 Tile 分支——地址先经 `NPUMemoryModel::Instance().ResolveAssignedAddress<T>(addr)` 翻译成宿主机内存里的真实指针，再绑定。也就是说 CPU 仿真用一张「片上偏移 → 宿主指针」的映射表模拟了存储层级；两个 Tile 绑了重叠的偏移，仿真下就会真的共享内存（本讲实践正是利用这一点观察 bug）。

真实 kernel 中的用法（上一讲 Add 示例的手工排布）：

[demos/baseline/add/csrc/kernel/add_custom.cpp:22-29](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/demos/baseline/add/csrc/kernel/add_custom.cpp#L22-L29)：六个常量 `X_PING/X_PONG/Y_PING/Y_PONG/Z_PING/Z_PONG` 把 192KB UB 手工切成输入/输出、乒乓四象限——这就是 manual placement 的真实样子：**地址规划写在源码里，人肉保证不重叠、不越界、对齐**。随后 [add_custom.cpp:66-71](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/demos/baseline/add/csrc/kernel/add_custom.cpp#L66-L71) 用 Form 1 把六个 Tile 绑上去；循环体内 [add_custom.cpp:86-88](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/demos/baseline/add/csrc/kernel/add_custom.cpp#L86-L88) 则用 GlobalTensor 分支的 TASSIGN 推进 GM 视图地址。

#### 4.1.4 代码实践

**实践目标**：体会 Form 2（编译期地址）的静态检查威力，以及它在 CPU 仿真下「不设防」的差异。

1. 打开 `docs/isa/TASSIGN.md`，阅读其中的 `example_oob_addr` 示例（`TASSIGN<0x20020>(t)` 触发 SA-0353）。
2. 把 `demos/baseline/add/csrc/kernel/add_custom.cpp` 第 66 行的 `TASSIGN(xTiles[0], X_PING);` 改成编译期形式 `TASSIGN<X_PING>(xTiles[0]);`（其余 5 行同理），提交前用 `git diff` 核对。
3. 若本地有 NPU 工具链，按该 demo 的 README 用 bisheng/CCE 编译一次，确认能过编译（`0x0 + tile 字节数 ≤ 192KB`）。
4. 再故意写一行 `TASSIGN<0x30000>(xTiles[0]);`（0x30000 = 192KB，恰为 UB 容量上界），重新编译，观察 SA-0353 报错信息；随后删掉这行。

**需要观察的现象**：NPU 编译时第 4 步必须在编译期报 `[SA-0353]` 错误（编译不产出目标文件）。

**预期结果**：合法地址静默通过、非法地址编译期被拦截——这就是 Form 2 存在的意义。若你只在 CPU 仿真下编译（`tests/run_cpu.py` 路径），由于 `tassign_check.hpp` 对 `__CPU_SIM` 跳过检查，第 4 步**不会**报错——这本身就是本实践要验证的结论。无 NPU 环境时，第 3、4 步标注「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：`TASSIGN(tile, 0x1000)` 和 `TASSIGN<0x1000>(tile)` 功能上等价吗？差别在哪？

> 答案：运行期行为等价（Form 2 最终转发到 Form 1）。差别在编译期：Form 2 会触发 SA-0351～SA-0354 四条 `static_assert`（容量存在、tile 装得下、不越界、对齐），Form 1 完全不检查。能用编译期常量地址时应优先 Form 2。

**练习 2**：`Tile<TileType::Vec, float, 128, 128>` 在 A2A3 上最多能同时放几份到 UB？为什么 `TASSIGN<0x20020>` 会报错而 `TASSIGN<0x20000>` 不会（假设只放这一份）？

> 答案：该 tile 占 128×128×4 = 64KB，UB 容量 192KB，理论上可放 3 份（地址 0x0/0x10000/0x20000）。`0x20000 + 0x10000 = 0x30000 = 192KB`，恰好未超界且 32 字节对齐，所以合法；`0x20020` 越界（0x30020 > 0x30000），触发 SA-0353（对齐本身没问题，报的是越界）。

**练习 3**：为什么 GlobalTensor 版 TASSIGN 要求指针元素类型必须与 `GlobalTensor::DType` 一致？

> 答案：GlobalTensor 是零拷贝视图，搬运指令（TLOAD/TSTORE）按 `DType` 计算元素个数与步长；如果指针类型与 `DType` 不一致，同一份内存会被两种元素宽度解释，寻址立即错乱。源码用 `static_assert(std::is_same_v<...>)` 在编译期拦截（`TAssign.hpp` 第 30-32 行）。

### 4.2 缓冲分配/释放：TAlloc/TFree 与 TPipe 槽位生命周期

#### 4.2.1 概念说明

先纠正一个直觉：PTO **没有**「给 Tile 申请一块 UB」的通用 alloc 指令——那件事由 TASSIGN 完成。`TALLOC`/`TFREE` 解决的是另一个问题：**Cube 核（生产者）与 Vector 核（消费者）之间的跨核数据交接**。

在 A2A3/A5 上，一个 AI Core 常被切成一个 Cube 子块 + 两个 Vector 子块协同工作（C2V 方向），或反过来（V2C 方向）。Cube 算完的 tile 要交给 Vector 做后处理，最自然的结构是一块约定好的 GM/片上环形 FIFO：生产者写槽、挂牌；消费者等牌、读槽、还槽。`TPush/TPop` 指令族把这套协议指令化：

- `TALLOC(pipe, gmTensor)`：从 `TPipe` 申请一个生产者 FIFO 槽，把它暴露成一个 `GlobalTensor` 视图（地址已指向槽）。
- 生产者用普通指令（如 `TSTORE`）往这个视图写数据。
- `TPUSH(pipe, gmTensor)`：记录「数据就绪」同步，把槽提交给消费者。
- `TPOP(pipe, gmTensor)`：消费者等数据就绪，把 `gmTensor` 指到当前槽地址（不搬数据到本地 tile）。
- 消费者用普通指令（如 `TLOAD`）从视图读。
- `TFREE(pipe, gmTensor)`：归还槽位，通知生产者空间空闲。

所以「TAlloc/TFree 的生命周期管理」准确说是 **FIFO 槽位从申请到归还的生命周期**：`TALLOC → 写 → TPUSH` 与 `TPOP → 读 → TFREE` 两条腿配对，槽位数（`SlotNum`）决定了生产者能领先消费者多少步。

#### 4.2.2 核心流程

GlobalData 流一个完整来回：

```
生产者（如 Cube 核）                     消费者（如 Vector 核）
────────────────────                    ────────────────────
TALLOC(pipe, slot)
  ├─ 必要时等空闲空间
  ├─ slot ← FIFO 基址 + (tileIndex % SlotNum) * SlotSize
  └─ tileIndex++
TSTORE(slot, tile)      ← 真正写数据
TPUSH(pipe, slot)       ← 数据就绪挂牌
                                        TPOP(pipe, slot)
                                          ├─ 等数据就绪
                                          ├─ slot ← 当前槽地址
                                          └─ tileIndex++
                                        TLOAD(tile2, slot)   ← 真正读数据
                                        TFREE(pipe, slot)    ← 归还槽位
```

几个关键数字关系（TPipe 模板参数）：

- 槽大小 `SlotSize` 必须容纳一个逻辑 FIFO 条目；
- 环形槽总数 `SlotNum >= 1`，生产者领先量不能超过它；
- 同步周期 \[ SyncPeriod = \begin{cases} SlotNum, & SlotNum \le 2 \\ SlotNum/2, & SlotNum > 2 \end{cases} \]——「等空间/还空间」的同步是**稀疏**的，不是每拍都同步，以摊薄同步开销。

#### 4.2.3 源码精读

`TPipe` 的定义骨架：

[include/pto/npu/a2a3/TPush.hpp:24-46](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/npu/a2a3/TPush.hpp#L24-L46)：`TPipe<FlagID, DirType, SlotSize, SlotNum, LocalSlotNum, ...>` 用 `Direction::DIR_C2V/DIR_V2C/DIR_BOTH` 声明数据流向，`SyncPeriod` 由 `SlotNum` 推导，`RingFIFO<SlotSize, SlotNum, LocalSlotNum>` 是底层环形缓冲。[TPush.hpp:56-73](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/npu/a2a3/TPush.hpp#L56-L73) 的 `shouldWaitFree/shouldNotifyFree` 实现了「每 SyncPeriod 拍才真正等/还一次」的稀疏同步策略。

`TALLOC_IMPL` 三步走：

[include/pto/npu/a2a3/TAlloc.hpp:38-70](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/npu/a2a3/TAlloc.hpp#L38-L70)：注释把步骤写得很清楚——(1) `pipe.prod.getAllocateStatus() && Pipe::shouldWaitFree(...)` 同时成立才等空闲空间；(2) 计算 `entryBase = FIFO 基址 + (tileIndex % SLOT_NUM) * SLOT_SIZE`，V2C 方向还会按子块编号加 `getSubAIVOffset` 的切分偏移（[TAlloc.hpp:21-35](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/npu/a2a3/TAlloc.hpp#L21-L35) 支持上下切/左右切两种子块映射）；(3) `tileIndex++` 并**复用 TASSIGN** 把 `gmTensor` 绑到槽地址（第 69 行调用的正是 `TASSIGN_IMPL`）——注意 TALLOC 自己不写任何数据、不通知消费者，写数据靠随后的 TSTORE，挂牌靠 TPUSH。

`TFREE_IMPL` 的两个版本：

[include/pto/npu/a2a3/TFree.hpp:20-41](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/npu/a2a3/TFree.hpp#L20-L41)：TileData 版（第 21-24 行）在 A2A3 上是**空操作**——因为 A2A3 的 `TPOP` 内部已经完成了还空间通知，接口保留只为与 GlobalData 流对称（A5 上才有实际动作）；GlobalData 版（第 27-41 行）在 `cons.getFreeStatus() && shouldNotifyFree(...)` 成立时调用 `pipe.cons.free()` 归还槽位。

CPU 下可运行的完整协议示例（本讲实践的基础）：

[tests/cpu/st/testcase/tpushpop/main.cpp:100-126](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/cpu/st/testcase/tpushpop/main.cpp#L100-L126)：`testPushPopSingleThread` 用的是 TileData 流——先 `TASSIGN(src, 0)`、`TASSIGN(dst, rows*cols*sizeof(T))` 手工摆放两个 tile（**错开一个 tile 的字节距离**，这正是 4.1 的 manual placement），再 `TPUSH(src, pipe)` → `TPOP(dst, pipe)` → `TFREE(pipe)`，最后与期望值比对。多核版本 [main.cpp:128-173](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/cpu/st/testcase/tpushpop/main.cpp#L128-L173) 用两个 `std::thread` 分别扮演生产者与消费者，真实展示 FIFO 的阻塞语义（生产者领先不超过 `FiFoDepth`）。

`TALLOC` 的 GlobalData 流完整例子见 ISA 文档：

[docs/isa/TALLOC.md:68-96](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/isa/TALLOC.md#L68-L96)：`example_talloc` 展示了 `TPipe` 构造 → `TASSIGN(tile, 0x0)` 摆本地 tile → `TALLOC` 拿槽视图 → `TSTORE` 写入 → `TPUSH` 提交 的标准序列。

#### 4.2.4 代码实践

**实践目标**：在 CPU 仿真下亲手跑通 TPUSH/TPOP/TFREE，并通过「故意绑重叠地址」验证 TASSIGN 手工排布的正确性约束。

1. 在仓库根目录运行：
   ```bash
   python3 tests/run_cpu.py -t tpushpop
   ```
   （`-t` 指定单个 ST 用例；首次运行会自动 cmake 增量构建。）
2. 观察全部 `TPushPopTest` 用例 PASSED，包括 `multicore_float_64_128_Vec`（双线程生产者-消费者）。
3. 打开 `tests/cpu/st/testcase/tpushpop/main.cpp`，把第 113 行
   ```cpp
   TASSIGN(dst, rows * cols * sizeof(T));
   ```
   改为
   ```cpp
   TASSIGN(dst, 0);
   ```
   使 `dst` 与 `src` 绑到同一仿真地址，再次运行 `-t tpushpop`。
4. 观察 `T*/#rows/#cols/*` 系列用例的失败情况，然后恢复第 113 行，确认重新 PASSED。

**需要观察的现象**：第 3 步后单线程用例中 `TPUSH` 写入的数据与 `TPOP` 读出的数据混在同一块内存，`ResultCmp` 比对应报失败（或结果异常）；恢复后全部通过。

**预期结果**：CPU 仿真不会替你检查片上地址重叠——两个 Tile 绑到同一偏移就真的共享宿主内存（`ResolveAssignedAddress` 翻译后是同一指针）。这说明 Manual 模式下「地址排布不重叠」是开发者的责任，静态检查只覆盖编译期常量地址的越界/对齐，不覆盖重叠。若无本地编译环境，本实践标注「待本地验证」，可改为源码阅读：跟踪 `testPushPopMultiCore` 中生产者 12 次迭代与 `FiFoDepth=4` 的领先关系。

#### 4.2.5 小练习与答案

**练习 1**：`TALLOC` 之后可以直接 `TPUSH` 而不写数据吗？会发生什么？

> 答案：语法上可以（编译器不管），但语义错误：`TALLOC` 只申请槽并把视图指到槽地址，`TPUSH` 只挂牌说「数据就绪」——两者都不搬数据。不写就 PUSH，消费者会读到旧值/垃圾值。写数据必须由中间的 `TSTORE` 等普通指令完成（`TAlloc.hpp` 的实现里没有任何写内存动作）。

**练习 2**：A2A3 上 `TFREE(pipe)`（TileData 版）是空操作，为什么 API 还要保留？

> 答案：两个原因——(1) A2A3 的 `TPOP` 内部已顺带完成还空间通知，但 A5 的实现需要显式 TFREE，保留接口让同一份 kernel 源码跨架构通用；(2) 与 GlobalData 流的 `TFREE(pipe, gmTensor)` 保持 API 对称，降低心智负担（见 `TFree.hpp` 第 21-24 行与 `docs/isa/TFREE.md` 说明）。

**练习 3**：生产者循环 `TPUSH` 了 5 次，`TPipe` 的 `SlotNum = 4`，消费者一次都没 `TPOP`，会发生什么？

> 答案：第 5 次 `TPUSH` 前，生产者侧的等待空间逻辑会阻塞（`shouldWaitFree` 命中、等待消费者的还空间通知）。环形 FIFO 的容量就是 `SlotNum` 个槽，生产者最多领先消费者 `SlotNum` 步——这正是多核测试里 `FiFoDepth` 参数控制的领先量。

### 4.3 Auto 与 Manual 差异

#### 4.3.1 概念说明

Manual 模式的代价已经很明显：Add 示例里六个地址常量、二十多条 set/wait 事件，全靠人肉保证正确。Auto 模式把其中两类样板交给编译器：

1. **Tile 内存分配**：实例化 `Tile` 变量即可，编译器根据 Tile 的活跃区间（live range）自动分配片上地址，`TASSIGN` 不再需要；
2. **自动同步**：编译器在硬件流水线之间自动插入同步，`TSYNC`/`Event` 不再需要。

官方文档明确：Auto 模式下这些写法**不是报错，而是变成空操作**——同一份 kernel 源码可以在两种模式下切换编译。

#### 4.3.2 核心流程

```
                 ┌─ Manual：开发者 TASSIGN 摆地址 + 事件同步
同一份 kernel ───┤
（源码不变）     └─ Auto：--cce-pto-enable --cce-pto-auto-enable
                        编译器做 Tile 活跃区间分析
                        → 自动分配缓冲（TASSIGN 对 Tile 变 no-op）
                        → 自动插入流水线同步（TSYNC/Event 变 no-op）
```

#### 4.3.3 源码精读

Auto 模式的行为定义：

[docs/auto_mode/Auto_Mode_Overview.md:45-47](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/auto_mode/Auto_Mode_Overview.md#L45-L47)：官方文档说明默认（Manual）模式需要 `TASSIGN` 手工指定缓冲地址，Auto 模式下只需实例化 `Tile` 变量，编译器在底层自动分配；第 9 行（[Auto_Mode_Overview.md:7-13](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/auto_mode/Auto_Mode_Overview.md#L7-L13)）进一步说明 Auto 模式下 `TASSIGN` 与 `TSYNC`/`Event`「将不做任何事」。

源码层面的开关：

[include/pto/npu/a2a3/TAssign.hpp:20-26](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/npu/a2a3/TAssign.hpp#L20-L26)：`#ifndef __PTO_AUTO__` 包住整段绑定逻辑——Auto 模式（定义了 `__PTO_AUTO__`）下对 Tile 的 TASSIGN 直接 `return`，是货真价实的空操作。注意 GlobalTensor 分支**不在**此豁免之列：GM 视图地址（循环里推进偏移那类）仍需开发者自己 TASSIGN。

启动 Auto 模式的方式：

[docs/auto_mode/Auto_Mode_Overview.md:49-70](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/auto_mode/Auto_Mode_Overview.md#L49-L70)：编译时给 bisheng/CCE 加 `--cce-pto-enable --cce-pto-auto-enable` 两个开关，并按 SoC 选择 `--cce-aicore-arch`。

#### 4.3.4 代码实践

**实践目标**：从工程上对比同一算子的 Auto/Manual 两份源码差异。

1. 阅读 `demos/baseline/add/csrc/kernel/add_custom.cpp`（Manual 版，本讲已精读）。
2. 打开 `demos/auto_mode/baseline/add/` 目录下的 kernel 源码，与 Manual 版对照。
3. 列出三处 Manual 版有而 Auto 版没有（或被简化）的代码类别，预期包括：UB 地址常量与 TASSIGN 摆放、set_flag/wait_flag 事件序列、（可能的）ping-pong 管理逻辑。
4. 用一句话总结：Auto 版的循环体应该只剩「视图推进 → TLOAD → TADD → TSTORE」这类纯数据流指令。

**需要观察的现象**：Auto 版 kernel 明显更短，没有任何十六进制地址常量。

**预期结果**：印证 4.3.1 的两条自动化（缓冲分配 + 同步插入）。对照结论可记录在笔记里，供单元九（u9-l1 Auto Mode 专讲）展开。本实践为源码阅读型，无需运行硬件；两份文件均在仓库内，可直接 `git diff --no-index` 对比。

#### 4.3.5 小练习与答案

**练习 1**：Auto 模式下写 `TASSIGN(xGlobal, x + offset)`（GlobalTensor 版）还是有效的吗？

> 答案：有效。`__PTO_AUTO__` 豁免只覆盖 Tile 分支（`TAssign.hpp` 的 `if constexpr (is_tile_data_v<T> || is_conv_tile_v<T>)` 内部）；GlobalTensor 的 GM 视图地址始终由开发者维护，Auto 模式不会替你推进 GM 偏移。

**练习 2**：既然 Auto 模式更省事，为什么仓库里 `kernels/manual/` 下还有大量手工优化的高性能算子？

> 答案：性能。Auto 模式目标是「有竞争力的性能」，但手工版本可以做编译器难以发现的优化：精确的 ping-pong 排布、跨流水线的细粒度重叠、L0 缓冲的复用顺序等（Flash Attention、gemm_performance 等都在 manual 目录）。Manual 是性能上限，Auto 是开发效率上限。

**练习 3**：把 Manual 版 Add 的 `X_PONG` 改成与 `X_PING` 相同（都是 0x0），在 CPU 仿真和 NPU 真机上分别会发生什么？

> 答案：CPU 仿真——两个 Tile 绑到同一仿真地址，乒乓两路数据互相踩，结果错误但不会崩溃；NPU 真机——同样数据覆盖，且由于 Form 1 无编译期检查，也不会被拦截（只有 `TASSIGN<Addr>` 编译期常量形式才检查越界/对齐，且重叠本身在任何形式下都不检查）。这再次说明地址排布正确性是 Manual 模式开发者的第一责任。

## 5. 综合实践

**任务：把上一讲的「加载-计算-写回」改造成带 FIFO 的生产者-消费者双段流水（CPU 仿真验证）。**

背景：上一讲（u3-l1）你实现了 `TLOAD → ×2 → TSTORE` 的单线程搬运链。本综合实践把它拆成两段，中间用 `TPush/TPop` 协议衔接，模拟「一个核负责计算、另一个核负责写出」的真实分工：

1. **准备**：复制 `tests/cpu/st/testcase/tpushpop/` 为新用例目录（参考 u1-l3 讲过的 ST 用例四件套结构，需含 `CMakeLists.txt` 与 `main.cpp`；`gen_data.py` 可省略，直接在代码内构造期望值，与 tpushpop 一致）。
2. **生产者段**：`TLOAD` 从一个 `GlobalTensor` 读入 tile，`TMULS`（或逐元素 ×2）得到中间结果，`TPUSH` 推入 `TPipe`。
3. **消费者段**：`TPOP` 弹出到另一个 tile，`TSTORE` 写回输出 `GlobalTensor`，随后 `TFREE`。
4. **显式缓冲管理**：所有 Tile 用 TASSIGN 手工摆放地址，画一张 UB 偏移排布图（谁在 0x0、谁在 0x8000……），确保互不重叠。
5. **运行**：`python3 tests/run_cpu.py -t <你的用例名>`，比对输出与 `numpy` 期望（输入 ×2）。
6. **思考题**（写进用例注释）：如果把 `TPipe` 的 `SlotNum` 从 2 改成 1，单线程版本和多线程版本的行为有何差异？

**验收标准**：结果比对通过；能口头回答「这段代码里哪些地址是 TASSIGN 管的、哪个生命周期是 TALLOC/TFREE 管的、哪些样板在 Auto 模式下会消失」。

## 6. 本讲小结

- Manual 模式下 Tile 不自带存储，`TASSIGN` 把整型片上偏移绑给 Tile（或把指针绑给 GlobalTensor 视图），地址规划完全由开发者负责。
- `TASSIGN<Addr>(tile)` 编译期地址形式带 SA-0351～SA-0354 静态检查（容量存在、装得下、不越界、32 字节对齐）；CPU 仿真/CostModel 后端跳过这些检查。
- `TALLOC/TFREE` 不是通用缓冲 malloc/free，而是 `TPipe` 生产者-消费者 FIFO 的槽位申请与归还，与 `TPUSH/TPOP` 组成跨核数据交接协议；同步按 `SyncPeriod` 稀疏进行。
- A2A3 上 TileData 流的 `TFREE` 是空操作（`TPOP` 已内部完成通知），接口保留为跨架构与跨流程的 API 对称。
- Auto 模式（`__PTO_AUTO__`）下对 Tile 的 TASSIGN 与事件同步全部变空操作，编译器基于 Tile 活跃区间自动分配缓冲、自动插入同步；GM 视图地址仍需手工维护。
- CPU 仿真不检查片上地址重叠，绑错地址会以「数据互相踩」的形式静默出错——排布正确性是 Manual 模式的第一纪律。

## 7. 下一步学习建议

下一讲（u3-l3）将学习片上搬移类指令 `TMov/TTrans/TReshape`——当数据已经在 UB/L1 里，如何在 tile 之间复制、转置与重排形状。建议提前浏览 `docs/isa/TMOV.md`、`docs/isa/TTRANS.md`，并思考：既然有了 TASSIGN 手工摆地址，「转置」为什么不能靠改绑定地址实现（提示：回忆 u2-l2 的分形布局与存储组织）。后续单元四还会把本讲的 TPipe 协议用在真实算子里（如 `kernels/manual/common/flash_atten/fa_performance_kernel.cpp` 中的 TALLOC 用法），届时可回看本讲的槽位生命周期模型。
