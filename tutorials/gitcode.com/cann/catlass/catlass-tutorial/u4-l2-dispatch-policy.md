# DispatchPolicy 调度策略

## 1. 本讲目标

在上一讲（u4-l1）里，我们已经知道 `BlockMmad` 本身没有通用实现——它只是一个「主模板 + 偏特化」的空壳，靠 `DispatchPolicy` 这个标签去选中具体的主循环实现。本讲就来回答一个关键问题：

**`DispatchPolicy` 到底是什么？它如何驱动 Block 层选实现？又有哪些可选项？**

学完本讲你应该能够：

1. 说清 `DispatchPolicy` 作为「编译期策略标签」的角色，以及它驱动 `BlockMmad` 偏特化的两种匹配手法。
2. 识别四种核心 `DispatchPolicy`（`MmadAtlasA2Pingpong` / `MmadAtlasA2Preload` / `MmadAtlasA2PreloadAsync` / `MmadAtlasA2PreloadAsyncWithCallback`）的功能差异与适用样例。
3. 解释 `STAGES`、`ENABLE_UNIT_FLAG`、`ENABLE_SHUFFLE_K`、`PRELOAD_STAGES` 等参数的含义、约束，以及违反约束时编译器的报错来源。

---

## 2. 前置知识

本讲建立在 u4-l1 的结论之上，不再重复 Block 层主循环的内部细节，只补充三个前置概念：

- **策略标签（Policy Tag）**：C++ 模板元编程中，用一个「只装编译期常量、不装运行期数据」的空结构体当作类型参数传入，从而在编译期「勾选」一种行为。`DispatchPolicy` 就是这种标签。它本身不参与计算，只是让编译器在实例化时挑出正确的偏特化版本。
- **偏特化（Partial Specialization）**：当主模板对某个类型参数「无法实现」时，可以为特定的类型参数组合提供一份专用实现。本讲会看到两种写法：直接对策略结构体偏特化、以及用 `std::enable_if_t` 配合一个 checker trait 偏特化。
- **乒乓缓冲 / 预加载（Pingpong / Preload）**：u4-l1 已讲过多缓冲让搬运与计算并行；本讲的四种 `DispatchPolicy` 正是在「开几片缓冲、是否提前搬运、是否跨块/跨组预加载」上做文章，把工程优化手段暴露成可勾选的开关。

如果对「BlockMmad 主模板 static_assert 报错」「HardEvent 同步」「unitFlag 细粒度并行」这几个词还有印象模糊，建议先回顾 u4-l1。

---

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [include/catlass/gemm/dispatch_policy.hpp](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/gemm/dispatch_policy.hpp) | **本讲主战场**：所有 `DispatchPolicy` 结构体的定义，每个结构体只装 `static constexpr` 常量。 |
| [include/catlass/gemm/block/block_mmad.hpp](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/gemm/block/block_mmad.hpp) | `BlockMmad` 主模板（`static_assert` 空壳）与默认 `TileCopy`/`TileMmad` 的选取入口，展示 `DispatchPolicy::ArchTag` 如何向下渗透。 |
| [include/catlass/gemm/block/block_mmad_pingpong.hpp](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/gemm/block/block_mmad_pingpong.hpp) | 用 **checker + enable_if** 手法匹配多种 Pingpong 策略的偏特化实现。 |
| [include/catlass/gemm/block/block_mmad_preload.hpp](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/gemm/block/block_mmad_preload.hpp) | 用 **直接偏特化** 手法匹配 `MmadAtlasA2Preload`，并含 ShuffleK 的 `firstTileIdx` 实现。 |
| [include/catlass/gemm/block/block_mmad_preload_async.hpp](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/gemm/block/block_mmad_preload_async.hpp) | `MmadAtlasA2PreloadAsync` 的偏特化实现，展示 N-buffer 各级 STAGES 的提取。 |
| [include/catlass/gemm/block/block_mmad_preload_async_with_callback.hpp](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/gemm/block/block_mmad_preload_async_with_callback.hpp) | `MmadAtlasA2PreloadAsyncWithCallback` 的偏特化，新增 AIC/AIV 同步 callback。 |
| [docs/zh/2_Design/01_kernel_design/03_dispatch_policies.md](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/docs/zh/2_Design/01_kernel_design/03_dispatch_policies.md) | 官方对四种 `DispatchPolicy` 的功能/参数/适用样例说明。 |
| [examples/00_basic_matmul/basic_matmul.cpp](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/examples/00_basic_matmul/basic_matmul.cpp) | 用 `MmadAtlasA2Pingpong<true>` 的最简样例。 |
| [examples/06_optimized_matmul/optimized_matmul.cpp](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/examples/06_optimized_matmul/optimized_matmul.cpp) | 用 `MmadAtlasA2Preload<true, true>` 的优化样例。 |

---

## 4. 核心概念与源码讲解

### 4.1 标签特化机制：DispatchPolicy 如何选中 BlockMmad 实现

#### 4.1.1 概念说明

`DispatchPolicy` 是一个**只携带编译期常量、不携带运行期数据**的策略结构体。它的职责有两件：

1. **当「类型开关」**：作为 `BlockMmad` 的第一个模板参数，决定编译器实例化哪一份主循环实现。
2. **当「配置包」**：把 `STAGES`、`ENABLE_UNIT_FLAG` 等开关打包成一个类型，主循环实现通过 `DispatchPolicy::STAGES` 这样的方式读取配置。

所有策略结构体都有一个共同的根基 `MmadBase`，它只暴露两件事：`ArchTag`（绑定的硬件架构）和 `ASYNC`（是否走异步搬运流水）：

[include/catlass/gemm/dispatch_policy.hpp:L21-L28](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/gemm/dispatch_policy.hpp#L21-L28) —— `MmadBase` 模板，派生出同步别名 `MmadAtlasA2`（`ASYNC=false`）与异步别名 `MmadAtlasA2Async`（`ASYNC=true`）。

```cpp
template <class ArchTag_, bool ASYNC_ = false>
struct MmadBase {
    using ArchTag = ArchTag_;
    static constexpr uint32_t ASYNC = ASYNC_;
};
using MmadAtlasA2      = MmadBase<Arch::AtlasA2, false>;
using MmadAtlasA2Async = MmadBase<Arch::AtlasA2, true>;
```

注意 `ArchTag` 不只是「标记」，它还会**向下渗透**到 Tile 层组件：`BlockMmad` 主模板默认用 `DispatchPolicy::ArchTag` 去挑选底层的 `TileCopy` 与 `TileMmad`（即按架构自动选搬运/计算组件，详见 u5-l3）：

[include/catlass/gemm/block/block_mmad.hpp:L20-L28](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/gemm/block/block_mmad.hpp#L20-L28) —— 主模板是 `static_assert(DEPENDENT_FALSE<...>)` 空壳，匹配不到任何偏特化就会编译报错。

```cpp
template <class DispatchPolicy, ..., class Enable = void>
struct BlockMmad {
    static_assert(DEPENDENT_FALSE<DispatchPolicy>,
                  "BlockMmad is not implemented for this DispatchPolicy");
};
```

#### 4.1.2 核心流程

标签特化的完整路由链路是：

```text
用户写 using DispatchPolicy = MmadAtlasA2Preload<true, true>;
        │
        ▼
传给 BlockMmad<DispatchPolicy, ...> 的第 1 个模板参数
        │
        ▼
编译器在所有 BlockMmad 偏特化里「按类型匹配」
        │
        ├── 命中直接偏特化  ──► 用那份实现
        ├── 命中 enable_if 偏特化（checker.value == true）──► 用那份实现
        └── 都不命中        ──► 落回主模板 static_assert，编译报错
```

本仓库里，匹配偏特化用了**两种手法**，理解它们的区别是本模块的核心：

| 手法 | 写法 | 何时用 | 代表文件 |
| --- | --- | --- | --- |
| **直接偏特化** | `template<...> struct BlockMmad<MmadAtlasA2Preload<...>, ...>` | 一份实现只服务**一种**策略结构体形状 | `block_mmad_preload.hpp`、`block_mmad_preload_async.hpp` |
| **checker + enable_if** | 偏特化的 `Enable` 槽填 `std::enable_if_t<Checker<Policy>::value>` | 一份实现要服务**多种**策略形状 | `block_mmad_pingpong.hpp`（同时服务 `MmadAtlasA2Pingpong` 与跨架构的 `MmadPingpong`） |

第二种手法之所以存在，是因为「乒乓主循环」这套实现对 AtlasA2 专用策略 `MmadAtlasA2Pingpong<>` 和跨架构策略 `MmadPingpong<ArchTag,...>` 都适用，用一个 checker trait 把这两种形状都判为 `true`，就能让一份代码复用。

#### 4.1.3 源码精读

**手法一：checker + enable_if（Pingpong）**

先看 checker trait。它是一个主模板恒为 `false`、靠偏特化把「认可的策略形状」判为 `true` 的判定器：

[include/catlass/gemm/block/block_mmad_pingpong.hpp:L26-L43](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/gemm/block/block_mmad_pingpong.hpp#L26-L43) —— `MmadPingpongDispatchChecker` 对 `MmadAtlasA2Pingpong<...>` 与 `MmadPingpong<...>` 两种形状都特化为 `true`。

```cpp
template <class DispatchPolicy>
struct MmadPingpongDispatchChecker { static constexpr bool value = false; };

template <bool ENABLE_UNIT_FLAG>
struct MmadPingpongDispatchChecker<MmadAtlasA2Pingpong<ENABLE_UNIT_FLAG>> {
    static constexpr bool value = true;
};
// 还有一个对 MmadPingpong<ArchTag,...> 的偏特化，同样 value = true
```

然后偏特化把第一个参数留成通用 `DispatchPolicy_`，把判定结果塞进主模板第 10 个参数 `Enable`：

[include/catlass/gemm/block/block_mmad_pingpong.hpp:L68-L73](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/gemm/block/block_mmad_pingpong.hpp#L68-L73) —— `Enable` 槽填 `std::enable_if_t<MmadPingpongDispatchChecker<DispatchPolicy_>::value>`。

```cpp
template <class DispatchPolicy_, ..., class TileMmad_>
struct BlockMmad<
    DispatchPolicy_, ..., TileMmad_,
    std::enable_if_t<MmadPingpongDispatchChecker<DispatchPolicy_>::value>> {
    // 真正的乒乓主循环实现
};
```

**手法二：直接偏特化（Preload）**

`MmadAtlasA2Preload` 只有一种形状，直接把它「写死」在偏特化的参数里即可，不需要 checker：

[include/catlass/gemm/block/block_mmad_preload.hpp:L25-L30](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/gemm/block/block_mmad_preload.hpp#L25-L30) —— 偏特化直接对 `MmadAtlasA2Preload<ENABLE_UNIT_FLAG_, ENABLE_SHUFFLE_K_>` 命中。

```cpp
template <bool ENABLE_UNIT_FLAG_, bool ENABLE_SHUFFLE_K_, ...>
struct BlockMmad<
    MmadAtlasA2Preload<ENABLE_UNIT_FLAG_, ENABLE_SHUFFLE_K_>,
    L1TileShape_, L0TileShape_, ...> { /* 预加载主循环 */ };
```

实现内部再通过 `DispatchPolicy::XXX` 读回策略包里的常量，例如：

[include/catlass/gemm/block/block_mmad_preload.hpp:L60-L62](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/gemm/block/block_mmad_preload.hpp#L60-L62) —— 从策略包读取 `ENABLE_UNIT_FLAG`、`ENABLE_SHUFFLE_K`、`STAGES`。

#### 4.1.4 代码实践

**实践目标**：亲手验证「标签匹配不上就报错」的机制，建立对两种匹配手法的直觉。

**操作步骤**：

1. 打开 `examples/00_basic_matmul/basic_matmul.cpp` 第 87 行，确认它用的是 `using DispatchPolicy = Gemm::MmadAtlasA2Pingpong<true>;`。
2. 想象（不必真编译）把它改成一个**根本不存在**的名字，例如 `Gemm::MmadAtlasA2NonExist<>`。
3. 推断：没有任何偏特化会匹配它，最终会落回 `block_mmad.hpp` 主模板，触发 `static_assert(DEPENDENT_FALSE<DispatchPolicy>, "BlockMmad is not implemented for this DispatchPolicy")`。

**需要观察的现象**：编译器报错信息里会明确出现 `BlockMmad is not implemented for this DispatchPolicy` 字样——这正是「策略标签没匹配到实现」的唯一信号。

**预期结果**：能复述「报错来自主模板 static_assert，说明 DispatchPolicy 是类型匹配的开关」。若想真正触发，可在本地 `bash scripts/build.sh` 编译该样例（待本地验证，本环境无 NPU/编译器）。

#### 4.1.5 小练习与答案

**练习 1**：`DispatchPolicy` 结构体里没有任何成员变量、全是 `static constexpr`，这样做有什么好处？

> **答案**：它是「零运行期开销」的编译期配置包——既当类型开关（驱动偏特化匹配），又当配置容器（供实现读取 `STAGES` 等常量），且不占任何运行期内存或 cycles。

**练习 2**：为什么 Pingpong 用 checker+enable_if，而 Preload 用直接偏特化？

> **答案**：Pingpong 的一份实现要同时服务 `MmadAtlasA2Pingpong<>`（A2 专用）和 `MmadPingpong<ArchTag,...>`（跨架构）两种形状，需要 checker 把多种形状都判为 `true` 才能复用；Preload 只对应一种形状，直接偏特化更简洁。

---

### 4.2 四种 DispatchPolicy：从 Pingpong 到 PreloadAsyncWithCallback

#### 4.2.1 概念说明

`dispatch_policy.hpp` 里定义了**几十种**策略结构体（覆盖 FlashAttention、MLA、MX 量化、SVD、稀疏、动态选择等），但本讲聚焦文档 [03_dispatch_policies.md](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/docs/zh/2_Design/01_kernel_design/03_dispatch_policies.md) 明确介绍的**四种通用 GEMM 策略**。它们按「优化能力」递进：

1. **`MmadAtlasA2Pingpong`**：最基础的乒乓缓冲，Block 内部搬运与计算并行。
2. **`MmadAtlasA2Preload`**：在乒乓之上，支持**块间预加载**（提前搬运下一个 C 块的数据）与 **ShuffleK**。
3. **`MmadAtlasA2PreloadAsync`**：改用 **N-buffer**（L1/L0A/L0B/L0C 各自独立的缓冲片数）与 **`PRELOAD_STAGES` 提前发射**，还支持**组间预加载**，走异步流水（`ASYNC=true`）。
4. **`MmadAtlasA2PreloadAsyncWithCallback`**：在 Async 之上，允许用户把 **AIC↔AIV 同步命令以 callback 形式**传入，由 Block 层决定调用时机。

#### 4.2.2 核心流程

四种策略的继承关系本身就编码了「能力递进」：

```text
MmadBase<ArchTag, ASYNC>
  │
  ├─ MmadAtlasA2 (ASYNC=false) ──────────────────────── 同步族
  │     ├─ MmadAtlasA2Pingpong<UNIT_FLAG>              ← STAGES=2 乒乓
  │     └─ MmadAtlasA2Preload<UNIT_FLAG, SHUFFLE_K>    ← + 块间预加载 + ShuffleK
  │
  └─ MmadAtlasA2Async (ASYNC=true) ──────────────────── 异步族
        └─ MmadAtlasA2PreloadAsync<PRELOAD,L1,L0A,L0B,L0C,UNIT_FLAG,SHUFFLE_K>
              └─ MmadAtlasA2PreloadAsyncWithCallback<...>   ← + AIC/AIV 同步 callback
```

注意 `PreloadAsyncWithCallback` 是**直接继承** `PreloadAsync` 的（只多了一层语义，参数完全相同），这从源码就能印证。能力与参数对照如下：

| 策略 | 基类 ASYNC | 缓冲模型 | 块间预加载 | 组间预加载 | ShuffleK | Callback | 典型样例 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `MmadAtlasA2Pingpong` | false | STAGES 乒乓 | ✗ | ✗ | ✗ | ✗ | 00_basic_matmul |
| `MmadAtlasA2Preload` | false | STAGES 乒乓 | ✓ | ✗ | 可选 | ✗ | 06_optimized_matmul |
| `MmadAtlasA2PreloadAsync` | true | N-buffer | ✓ | ✓ | 可选 | ✗ | 02/05_grouped_matmul_slice_* |
| `MmadAtlasA2PreloadAsyncWithCallback` | true | N-buffer | ✓ | ✓ | 可选 | ✓ | 12_quant_matmul |

> 表中「✓/✗」依据官方文档 03_dispatch_policies.md 的功能描述归纳，其中「可选」表示由对应 bool 模板参数控制。

#### 4.2.3 源码精读

四种策略结构体的定义都在 `dispatch_policy.hpp`：

[include/catlass/gemm/dispatch_policy.hpp:L31-L35](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/gemm/dispatch_policy.hpp#L31-L35) —— `MmadAtlasA2Pingpong`，只有 `STAGES=2` 和一个 `ENABLE_UNIT_FLAG` 开关。

[include/catlass/gemm/dispatch_policy.hpp:L59-L64](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/gemm/dispatch_policy.hpp#L59-L64) —— `MmadAtlasA2Preload`，在 Pingpong 基础上多了 `ENABLE_SHUFFLE_K`。

```cpp
template <bool ENABLE_UNIT_FLAG_ = false, bool ENABLE_SHUFFLE_K_ = false>
struct MmadAtlasA2Preload : public MmadAtlasA2 {
    static constexpr uint32_t STAGES = 2;
    static constexpr bool ENABLE_UNIT_FLAG = ENABLE_UNIT_FLAG_;
    static constexpr bool ENABLE_SHUFFLE_K = ENABLE_SHUFFLE_K_;
};
```

[include/catlass/gemm/dispatch_policy.hpp:L94-L105](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/gemm/dispatch_policy.hpp#L94-L105) —— `MmadAtlasA2PreloadAsync` 继承自**异步**基类 `MmadAtlasA2Async`，参数从 2 个膨胀到 7 个（各级 STAGES + 两个开关）。

[include/catlass/gemm/dispatch_policy.hpp:L107-L112](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/gemm/dispatch_policy.hpp#L107-L112) —— `MmadAtlasA2PreloadAsyncWithCallback` **直接继承** `MmadAtlasA2PreloadAsync`，参数原样透传，只新增「支持 callback」的语义。

```cpp
template <uint32_t PRELOAD_STAGES_, uint32_t L1_STAGES_, uint32_t L0A_STAGES_,
          uint32_t L0B_STAGES_, uint32_t L0C_STAGES_, bool ENABLE_UNIT_FLAG_, bool ENABLE_SHUFFLE_K_>
struct MmadAtlasA2PreloadAsyncWithCallback
    : public MmadAtlasA2PreloadAsync<
          PRELOAD_STAGES_, L1_STAGES_, L0A_STAGES_, L0B_STAGES_, L0C_STAGES_,
          ENABLE_UNIT_FLAG_, ENABLE_SHUFFLE_K_> {};
```

实现侧的偏特化，Async 系用直接偏特化（与 4.1 的 Preload 同手法）：

[include/catlass/gemm/block/block_mmad_preload_async.hpp:L26-L33](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/gemm/block/block_mmad_preload_async.hpp#L26-L33) —— `MmadAtlasA2PreloadAsync` 的偏特化。

[include/catlass/gemm/block/block_mmad_preload_async_with_callback.hpp:L26-L33](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/gemm/block/block_mmad_preload_async_with_callback.hpp#L26-L33) —— `MmadAtlasA2PreloadAsyncWithCallback` 的偏特化，多了两个 callback 形参：

[include/catlass/gemm/block/block_mmad_preload_async_with_callback.hpp:L138](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/gemm/block/block_mmad_preload_async_with_callback.hpp#L138) —— 主循环接收 `callbackBeforeFixpipe` 与 `callbackAfterFixpipe` 两个 callback，并在搬出 L0C→GM 前后调用它们（见 L366、L378 的 `params.callbackBeforeFixpipe()` / `params.callbackAfterFixpipe()`）。

#### 4.2.4 代码实践

**实践目标**：完成规格指定的任务——对照文档，为 `00_basic_matmul` 与 `06_optimized_matmul` 写出所用的 `DispatchPolicy` 并指出差异。

**操作步骤**：

1. 阅读文档 [03_dispatch_policies.md](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/docs/zh/2_Design/01_kernel_design/03_dispatch_policies.md) 中「MmadAtlasA2Pingpong」「MmadAtlasA2Preload」两节。
2. 查源码确认：
   - [examples/00_basic_matmul/basic_matmul.cpp:L87](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/examples/00_basic_matmul/basic_matmul.cpp#L87)：`using DispatchPolicy = Gemm::MmadAtlasA2Pingpong<true>;`
   - [examples/06_optimized_matmul/optimized_matmul.cpp:L74-L86](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/examples/06_optimized_matmul/optimized_matmul.cpp#L74-L86)：`constexpr bool ENABLE_UNIT_FLAG = true; constexpr bool ENABLE_SHUFFLE_K = true; ... using DispatchPolicy = Gemm::MmadAtlasA2Preload<ENABLE_UNIT_FLAG, ENABLE_SHUFFLE_K>;`
3. 写出差异表。

**需要观察的现象**：两者都把 `ENABLE_UNIT_FLAG` 设为 `true`；区别在于 06 多了 `ENABLE_SHUFFLE_K=true`，并且整体从 `Pingpong` 升级到 `Preload`。

**预期结果**（差异表）：

| 维度 | 00_basic_matmul | 06_optimized_matmul |
| --- | --- | --- |
| 策略结构体 | `MmadAtlasA2Pingpong<true>` | `MmadAtlasA2Preload<true, true>` |
| `ENABLE_UNIT_FLAG` | true | true |
| `ENABLE_SHUFFLE_K` | （无此参数） | true |
| 块间预加载 | 否 | 是 |
| 对应偏特化文件 | `block_mmad_pingpong.hpp` | `block_mmad_preload.hpp` |

#### 4.2.5 小练习与答案

**练习 1**：`MmadAtlasA2PreloadAsync` 相对 `MmadAtlasA2Preload` 多了哪些「能力维度」？

> **答案**：从同步（`ASYNC=false`）变异步（`ASYNC=true`）；缓冲模型从单一 `STAGES` 乒乓变为 L1/L0A/L0B/L0C 各自独立的 N-buffer；新增 `PRELOAD_STAGES` 提前发射 GM→L1 指令；并支持组间预加载。

**练习 2**：从源码看，`MmadAtlasA2PreloadAsyncWithCallback` 与 `MmadAtlasA2PreloadAsync` 的模板参数完全相同，为什么要分成两个策略？

> **答案**：它们参数相同、且前者直接继承后者，但语义不同——只有 `WithCallback` 这一份会被偏特化成「接收并在主循环中调用 callback」的实现。策略名本身就是给 Block 层「我要 callback 能力」的开关，靠不同的类型名路由到不同的偏特化。

---

### 4.3 关键策略参数：STAGES / ENABLE_UNIT_FLAG / ENABLE_SHUFFLE_K / PRELOAD_STAGES

#### 4.3.1 概念说明

这些参数是策略包里的「旋钮」。理解它们要抓住三点：**含义、约束、违反约束时的报错来源**。

- **`STAGES`**：乒乓/多缓冲的「片数」。`STAGES=2` 即双缓冲（一片搬运、一片计算），是绝大多数策略的默认值。片数越多越能隐藏搬运延迟，但越吃存储（L1/L0 容量是固定的，见 u1-l2）。
- **`ENABLE_UNIT_FLAG`**：是否启用 `unitFlag` 优化——让 **Mmad 计算与 L0C→GM 搬出**做细粒度并行（不必等整块 L0C 写完才开始搬）。u4-l1 已讲过其机制。
- **`ENABLE_SHUFFLE_K`**：是否启用 ShuffleK——在多核切 K 时，让不同核**错开**起始搬运的 K-tile 序号，缓解多核同址读取冲突。
- **`PRELOAD_STAGES` / `L1_STAGES` / `L0A_STAGES` / `L0B_STAGES` / `L0C_STAGES`**：仅 Async 系使用。`PRELOAD_STAGES` 表示「提前发射几次 GM→L1 搬运后，才开始 L1→L0 搬运与 Mmad 计算」；后四个分别表示各级存储开的缓冲片数。

#### 4.3.2 核心流程

各级缓冲片数受**存储容量**硬约束。以 L1 为例，`L1_STAGES` 片缓冲要能装下「A 的一个 L1Tile + B 的一个 L1Tile」乘以片数。设 L1Tile 形状为 \((M_1, N_1, K_1)\)，A/B 元素字节数为 \(w_A, w_B\)，则一片 L1 占用为：

\[
\text{L1PerStage} = M_1 \cdot K_1 \cdot w_A + K_1 \cdot N_1 \cdot w_B
\]

需满足 \(\text{L1PerStage} \cdot \text{L1\_STAGES} \le \text{L1\_SIZE}\)（AtlasA2 上 `L1_SIZE=512KB`，见 u1-l2）。这正是文档对各 STAGES 参数标注「需满足 … ≤ L1 大小」的由来。

部分参数组合还**互斥**，编译期用 `static_assert` 卡死。最典型的两条：

1. `ENABLE_UNIT_FLAG` 与 `L0C_STAGES > 1` 不能同时成立——多片 L0C 缓冲与 unitFlag 的细粒度搬出语义冲突。
2. 输入为 int8 时 `ENABLE_UNIT_FLAG` 必须为 `false`（见 `dispatch_policy.hpp` 第 30、297 行注释）。

#### 4.3.3 源码精读

**参数互斥的 static_assert**

[include/catlass/gemm/dispatch_policy.hpp:L43-L51](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/gemm/dispatch_policy.hpp#L43-L51) —— `MmadAtlasA2SingleCoreSplitk` 在结构体内部直接断言「`L0C_STAGES>1` 时不能开 unitFlag」，违反时编译报错信息明确。

```cpp
static_assert(!(ENABLE_UNIT_FLAG && (L0C_STAGES > 1)),
              "When L0C_STAGES > 1, can not enable unitflag");
```

**ShuffleK 的实现：错开起始 tile**

[include/catlass/gemm/block/block_mmad_preload.hpp:L149-L159](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/gemm/block/block_mmad_preload.hpp#L149-L159) —— 计算当前块的 K-tile 总数 `kTileCount`，并定出遍历的起止 tile。

```cpp
uint32_t kTileCount = CeilDiv<L1TileShape::K>(actualShape.k());
...
if constexpr (ENABLE_SHUFFLE_K) {
    startTileIdx = AscendC::GetBlockIdx();   // 用核号错开起点
}
uint32_t firstTileIdx = startTileIdx % kTileCount;
```

当 `ENABLE_SHUFFLE_K` 为真时，`startTileIdx` 取本核的 `GetBlockIdx()`，于是不同核的第一个 K-tile 序号 `firstTileIdx = BlockIdx % kTileCount` 各不相同——多核就不会在同一时刻去 GM 读同一段 K 数据，从而缓解读取冲突（u8-l3 会更系统地讨论带宽优化）。

**Async 系的 STAGES 提取**

[include/catlass/gemm/block/block_mmad_preload_async.hpp:L64-L67](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/gemm/block/block_mmad_preload_async.hpp#L64-L67) —— 实现侧把策略包里的各级 STAGES 逐个读出，供后续分配多缓冲与控制提前发射。

#### 4.3.4 代码实践

**实践目标**：通过阅读约束，判断一组参数是否合法，并预测编译结果。

**操作步骤**：

1. 假设有人写了 `using DispatchPolicy = Gemm::MmadAtlasA2SingleCoreSplitk<2, 2, 2, true>;`（模板参数顺序见 [dispatch_policy.hpp:L43-L44](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/gemm/dispatch_policy.hpp#L43-L44)：`L1A_STAGES_, L1B_STAGES_, L0C_STAGES_, ENABLE_UNIT_FLAG_`）。
2. 读出：`L0C_STAGES=2`、`ENABLE_UNIT_FLAG=true`。
3. 对照 [L50](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/gemm/dispatch_policy.hpp#L50) 的 `static_assert` 判断。

**需要观察的现象**：该组合命中「`L0C_STAGES > 1` 且 `ENABLE_UNIT_FLAG` 为真」。

**预期结果**：编译失败，报错 `When L0C_STAGES > 1, can not enable unitflag`。本环境无编译器，结论为「待本地验证」，但依据 `static_assert` 可确定性推断。

#### 4.3.5 小练习与答案

**练习 1**：`STAGES` 越大越好吗？限制是什么？

> **答案**：不是。`STAGES` 越大，搬运与计算越能并行（隐藏延迟），但多缓冲占用的 L1/L0 容量线性增长，而各级存储容量是固定的（如 AtlasA2 的 L1=512KB）。片数受 `L1PerStage · STAGES ≤ L1_SIZE` 约束，超过会被偏特化里的 `static_assert` 拦下。

**练习 2**：用一句话解释 ShuffleK 为什么能缓解多核读取冲突。

> **答案**：ShuffleK 让每个核的起始 K-tile 序号 `firstTileIdx = BlockIdx % kTileCount`，于是同一时刻各核读取的是 K 维上**不同位置**的数据，避免多核同时读 GM 同一段地址造成的带宽竞争。

**练习 3**：`PRELOAD_STAGES` 的取值有什么约束？

> **答案**：依据文档，`PRELOAD_STAGES` 表示「提前发射几次 GM→L1 后再启动 L1→L0 与 Mmad」，取值要求**小于 `L1_STAGES`**（否则提前搬进来的片数超过 L1 缓冲容量，无处可放）。

---

## 5. 综合实践

**任务**：把本讲三个模块（标签路由、四种策略、关键参数）串起来，做一次「换策略」的源码阅读推演。

**步骤**：

1. **读策略**：确认 `00_basic_matmul` 用 `MmadAtlasA2Pingpong<true>`（[basic_matmul.cpp:L87](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/examples/00_basic_matmul/basic_matmul.cpp#L87)），`06_optimized_matmul` 用 `MmadAtlasA2Preload<true, true>`（[optimized_matmul.cpp:L86](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/examples/06_optimized_matmul/optimized_matmul.cpp#L86)）。
2. **判路由**：分别在 `include/catlass/gemm/block/` 下找到它们命中的偏特化文件——前者是 `block_mmad_pingpong.hpp`（checker+enable_if 手法），后者是 `block_mmad_preload.hpp`（直接偏特化手法）。
3. **读参数**：在两份偏特化实现里找到 `static constexpr ... = DispatchPolicy::...` 的读取处，列出各自用到了哪些策略常量。
4. **预测换策略的后果**：如果把 00 的 `DispatchPolicy` 从 `MmadAtlasA2Pingpong<true>` 改成 `MmadAtlasA2Preload<true, true>`，主循环会从「乒乓」换成「预加载 + ShuffleK」，多出 `ENABLE_SHUFFLE_K` 分支（`startTileIdx = GetBlockIdx()`）。
5. **（可选，待本地验证）**：在本地 `bash scripts/build.sh` 重新编译改后的 00 样例，确认仍能 `Compare success`，说明同一份 GEMM 换策略不影响数值正确性，只影响性能/流水行为。

**预期产出**：一张「策略 → 偏特化文件 → 读取的参数 → 换策略后新增的优化分支」对照表。

---

## 6. 本讲小结

- `DispatchPolicy` 是**零开销的编译期策略标签**：既当类型开关（驱动 `BlockMmad` 偏特化匹配），又当配置包（装 `STAGES` 等常量）；所有策略共享根基 `MmadBase<ArchTag, ASYNC>`。
- 匹配偏特化有**两种手法**：直接偏特化（一种策略一种形状，如 `Preload`/`PreloadAsync`）与 checker + `enable_if`（一份实现服务多种策略形状，如 `Pingpong` 同时服务 `MmadAtlasA2Pingpong` 与 `MmadPingpong`）；匹配不上则落回主模板 `static_assert` 报错。
- **四种通用 GEMM 策略**按能力递进：`Pingpong`（基础乒乓）→ `Preload`（+ 块间预加载 + ShuffleK）→ `PreloadAsync`（异步 + N-buffer + 组间预加载）→ `PreloadAsyncWithCallback`（+ AIC/AIV 同步 callback），继承关系直接编码了这一递进。
- **关键参数**：`STAGES`（缓冲片数，受存储容量约束）、`ENABLE_UNIT_FLAG`（Mmad 与 L0C→GM 细粒度并行）、`ENABLE_SHUFFLE_K`（按 `BlockIdx % kTileCount` 错开读取起点）、`PRELOAD_STAGES`（提前发射次数，须小于 `L1_STAGES`）。
- 参数间的**互斥约束**用 `static_assert` 在编译期卡死，例如 `L0C_STAGES > 1` 时不能开 `ENABLE_UNIT_FLAG`、int8 输入时 `ENABLE_UNIT_FLAG` 必须为 `false`。
- `DispatchPolicy::ArchTag` 会**向下渗透**到 Tile 层，自动挑选对应架构的 `TileCopy`/`TileMmad`——这是连接本讲与 u5（Tile 层）的桥梁。

---

## 7. 下一步学习建议

- **深入多缓冲与预加载的实现**：本讲只讲了策略「旋钮」，下一讲 **u4-l3（多缓冲 Pingpong 与 Preload 流水）** 会进入 `block_mmad_preload.hpp` / `block_mmad_preload_async.hpp` 的主循环内部，画 MTE2/MTE1/计算的时间线，解释 Preload 如何消除搬运空泡。
- **理解 ShuffleK 的带宽背景**：本讲只点到 ShuffleK 的错位读取，完整的带宽优化（含 Padding 三种重排）在 **u8-l3（Padding 与 ShuffleK 读写带宽优化）** 展开。
- **向 Tile 层下钻**：想看清 `ArchTag` 如何驱动搬运/计算组件特化，进入 **u5-l3（架构特化与 Tile 组件路由）**。
- **扩展阅读**：`dispatch_policy.hpp` 里还有大量专用策略（`MmadMx`、`MmadFlashAttentionQK/PV`、`MmadFullLoadA`、`MmadPingpongMutex` 等），分别对应 u9（复杂算子）、u8-l4（全载/Small）、u10-l2（Ascend950 Mutex 同步），可按需查阅。
