# Warp 级编程：shuffle、lanemask、投票与归约

## 1. 本讲目标

GPU 上最小的硬件执行单位不是单个线程，而是一个 **warp（线程束）**——32 个始终「锁步」执行的 lane。warp 内的 32 个 lane 共享一条指令流，硬件为它们提供了无需经过共享内存、无需屏障就能在**寄存器之间直接交换数据**的指令，统称 warp 级原语（warp-level primitives）。

学完本讲，你应当能够：

- 说清 **warp / lane / 参与掩码（participation mask）** 三者的关系，理解为什么 warp 内通信「天然同步」。
- 用 `warp::shuffle_*` 系列函数（`idx` / `bfly` / `up` / `down` 四种模式）在 lane 之间搬运寄存器值，并知道 64 位 shuffle 为什么必须拆成两次 32 位 shuffle。
- 区分两类容易混淆的「掩码」：参与掩码（`active_mask`、`*_sync` 的 `mask` 形参）与**位置掩码**（`lanemask_lt/le/eq/ge/gt` 五个特殊寄存器），并用 `ballot` / `any` / `all` 做 warp 级投票。
- 用 shuffle 的**蝶形（butterfly）模式**与**顺序（down）模式**实现 warp 内归约（sum / max），并理解 `redux.sync` 单指令归约的适用场景。
- 用 `ballot_sync` + `lanemask_lt` + `popcount` 实现 warp 级**排他前缀和**，进而完成流压缩（stream compaction）。

本讲是「专家：高级设备能力」单元的首讲，承接 [u2-l2](u2-l2-thread-indexing-and-safety.md) 的 `ThreadIndex` 见证类型与 [u2-l3](u2-l3-shared-memory-and-sync.md) 的共享内存 / `sync_threads`。你会清楚地看到：很多原本需要「共享内存 + 屏障」的块内协作算法，在 warp 粒度上可以只用寄存器完成，既快又无需同步。

## 2. 前置知识

### 2.1 warp、lane 与 SIMT 执行模型

CUDA 的 SIMT（Single Instruction, Multiple Threads）模型把一个线程块（block）切成若干个 **warp**，每个 warp 恰好 32 个线程。同一个 warp 里的 32 个线程被称为 32 个 **lane**，编号 `0..31`。在任意时刻，一条指令被发射给整个 warp，32 个 lane 同步执行它（不同 lane 只是在各自的数据上做事）。这意味着：

- warp 内的通信延迟极低（约 2 个时钟周期），因为数据始终留在寄存器里。
- warp 内的交换指令**隐式同步**：发起 shuffle 时，32 个 lane 必然都执行同一条指令，不存在「我先发你后到」的竞态。
- warp 是不可分的调度单位：一个 warp 里的所有 lane 要么都执行某条指令、要么（在分支处）部分 lane 被 mask 掉暂时休眠，但它们仍属于同一个硬件 warp。

### 2.2 参与掩码（participation mask）

warp 级的集体指令（`shuffle_sync`、`ballot_sync`、`redux_sync_*`、`elect_sync` …）几乎都接受一个 32 位的 `mask` 参数：**bit `k` 置 1 表示 lane `k` 参与这次集体操作**。`mask = u32::MAX`（全 1）即「整个 warp 都参与」。这是 Volta 之后（PTX 6.0+）为支持子 warp 协作而引入的硬性约定：所有 mask 中置位的、尚未退出的 lane，必须带着**相同的 mask 值**到达调用点，否则行为未定义。

在笔直的、warp 一致的代码里，mask 永远是 `u32::MAX`，所以 cuda-oxide 提供了一组「无 mask」的便捷函数（`shuffle`、`ballot`、`any`、`all` …），它们只是 `#[inline(always)]` 地把 `u32::MAX` 塞给对应的 `*_sync` 版本。本讲大多数示例都用便捷函数，但你需要知道底层都是带 mask 的 `*_sync`。

> ⚠️ 注意区分两类「掩码」，这是本讲最容易踩的坑：
> - **参与掩码**：`active_mask()` 和 `*_sync` 的 `mask` 形参。bit `k` = lane `k` **参与**这次集体操作。
> - **位置掩码**：`lanemask_lt/le/eq/ge/gt` 五个**只读特殊寄存器**。bit `k` = lane `k` 在编号上**位于我的前面/后面**。它们不需要参与掩码、不是集体指令，只是一次寄存器读。

### 2.3 与共享内存的对比（承接 u2-l3）

[u2-l3](u2-l3-shared-memory-and-sync.md) 讲过 `SharedArray<T,N>` 与 `sync_threads()`：写共享内存 → `sync_threads()` 屏障 → 读别人的值，作用域是整个 block（最多 1024 线程），延迟约 20 周期。warp 级原语作用域是 warp（32 线程），延迟约 2 周期，且**隐式同步、不需要屏障**。源码顶部的对比表把这件事说得很直白：

| 操作 | 共享内存 | Warp Shuffle |
|------|----------|--------------|
| 延迟 | ~20 周期 | ~2 周期 |
| 同步 | 需要 `sync_threads()` | warp 内隐式 |
| 作用域 | Block（最多 1024 线程） | Warp（32 线程） |

### 2.4 这些函数为什么写着 `unreachable!()`

和 [u2-l3](u2-l3-shared-memory-and-sync.md) 的 `sync_threads`、`SharedArray` 一样，本讲所有 warp 函数在 `cuda-device` 里的函数体都是 `unreachable!("... called outside CUDA kernel context")`。它们是**占位桩**：在普通 Rust 主机上调用会 panic；但当 `#[kernel]` 函数被 cuda-oxide 后端编译时，mir-importer 会把这些调用识别成方言操作（dialect op），最终 lowering 成真正的 PTX 指令或 NVVM intrinsic。这一点会在每个模块的「源码精读」里给出证据。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [crates/cuda-device/src/warp.rs](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-device/src/warp.rs) | warp 级原语的**设备端 API 全集**：`lane_id`、参与掩码、四模式 shuffle（u32/f32/u64/f64）、投票（ballot/any/all）、`redux.sync` 单指令归约、`elect.sync` 选举。本讲的「字典」。 |
| [crates/cuda-device/src/lib.rs](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-device/src/lib.rs) | 把 `warp` 模块 `pub mod warp;` 导出给 kernel 代码使用。 |
| [crates/rustc-codegen-cuda/examples/shuffle_64/src/main.rs](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/examples/shuffle_64/src/main.rs) | 64 位 shuffle 示例：`idx` 广播、`bfly` 蝶形求和、`down`/`up` 邻居交换、带 mask 的半 warp 独立 shuffle。 |
| [crates/rustc-codegen-cuda/examples/warp_reduce/src/main.rs](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/examples/warp_reduce/src/main.rs) | warp 归约示例：蝶形 `shuffle_xor` 求和、顺序 `shuffle_down` 求和、`shuffle` 广播、`lane_id` 自检。**本讲主实践的基础**。 |
| [crates/rustc-codegen-cuda/examples/lanemask_scan/src/main.rs](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/examples/lanemask_scan/src/main.rs) | 位置掩码与前缀和示例：五个 `lanemask_*` 寄存器读出、`ballot_sync` + `lanemask_lt` 实现流压缩排他前缀和。 |
| [crates/mir-importer/src/translator/terminator/intrinsics/warp.rs](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-importer/src/translator/terminator/intrinsics/warp.rs) | mir-importer 把 warp 函数调用翻译成 `dialect-nvvm` op 的实现，用来佐证「占位桩 → 真指令」的落地路径。 |

## 4. 核心概念与源码讲解

### 4.1 Warp Shuffle：lane 间的寄存器直传

#### 4.1.1 概念说明

**Shuffle** 是「我把我自己寄存器里的一个值，直接交给 warp 内另一个 lane 的寄存器」的硬件指令，全程不经过任何内存。PTX 里写作 `shfl.sync.*.b32`（只搬 32 位）。cuda-oxide 按四种**寻址模式**把它们封装成四组函数：

| 模式 | 含义 | 我读谁的值 | 典型用途 |
|------|------|-----------|----------|
| `idx`（索引） | 读「绝对编号 `src_lane`」的 lane | 固定的某个 lane | **广播**：把 lane 0 的值发给所有人 |
| `bfly`（蝶形 / XOR） | 读「我的编号 XOR `lane_mask`」的 lane | `lane_id ^ lane_mask` | **归约 / 扫描**的蝶形网络 |
| `up`（向上） | 读「我的编号 − `delta`」的 lane | `lane_id − delta` | 前缀和、把值往低 lane 累加 |
| `down`（向下） | 读「我的编号 + `delta`」的 lane | `lane_id + delta` | 顺序归约、把值往高 lane 累加 |

每种模式都有 u32 与 f32 两个版本；u64 / f64 则走一条「拆成两个 32 位」的特殊路径（见 4.1.3）。

shuffle 的两个关键性质：

1. **每条 lane 各自提供一个值，各自收到一个值**。它不是「lane A 单方面发给 lane B」，而是 32 个 lane 同时各交出 `var`、各收回一个结果。理解蝶形归约时这一点尤其重要。
2. **越界规则**：当 `idx`/`up`/`down` 算出的源 lane 超出 `0..31` 时，硬件不会报错，而是让调用 lane **保留自己的原值**。例如 `shuffle_down(val, 1)` 在 lane 31 上会得到 lane 31 自己的值（因为 lane 32 不存在）。

#### 4.1.2 核心流程

一次 `shuffle_sync(mask, var, src_lane)` 的执行可以想象成：

```text
每个 lane k（k 在 mask 中置位）:
    1. 把自己的 var 寄存器值「摆上」warp 内的交换总线。
    2. 计算「我要读谁」:
       - idx   模式: 源 = src_lane                  （所有 lane 读同一个 → 广播）
       - bfly  模式: 源 = lane_id XOR lane_mask      （对称交换 → 蝶形）
       - up    模式: 源 = lane_id - delta            （向低编号读）
       - down  模式: 源 = lane_id + delta            （向高编号读）
    3. 从源 lane 的 var 里取值，写回自己的结果寄存器。
       若源 lane 越界（< 0 或 > 31），结果 = 自己原本的 var。
```

蝶形归约（butterfly reduction）之所以高效，是因为它用 `log2(32) = 5` 步把 32 个值汇成一个：

\[ T_{\text{butterfly}} = \lceil \log_2 N \rceil = \lceil \log_2 32 \rceil = 5 \text{ 步} \]

每步把当前累加值与「编号 XOR {16,8,4,2,1}」的 lane 交换并相加，5 步之后每个 lane 都持有全 warp 的和。

#### 4.1.3 源码精读

`lane_id()` 是所有 warp 算法的起点，它读 PTX 的 `%laneid` 特殊寄存器，返回 `0..31`：

[lane_id 占位桩，注释里写明 lowering 目标](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-device/src/warp.rs#L67-L71)

四种模式的**带 mask 底层函数**——以 `idx` 为例（其余三种同构）：

[`shuffle_sync(mask, var, src_lane)` —— PTX `shfl.sync.idx.b32`](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-device/src/warp.rs#L291-L295)

而**全 warp 便捷函数**只是把 `u32::MAX` 塞进去（`#[inline(always)]`，MIR 内联后 codegen 只看到 `*_sync` 形态）：

[`shuffle(var, src_lane)` = `shuffle_sync(u32::MAX, var, src_lane)`](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-device/src/warp.rs#L337-L340)

f32 系列与 u32 完全平行，只是值类型换成 `f32`：

[`shuffle_f32_sync` —— float 变体](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-device/src/warp.rs#L382-L386)

**64 位 shuffle 是本节的难点**。PTX 根本没有 `shfl.sync.*.b64` 指令，LLVM 也没有 64 位 shuffle intrinsic。源码顶部那段注释解释了对策——把 64 位值拆成高低两个 32 位半，分别 shuffle，再拼回去，且整个拆/拼放在**一个 convergent 内联 PTX 块**里，保证两次 32 位 shuffle 在调用点仍是一次融合的集体操作：

[64 位 shuffle 的拆分策略说明](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-device/src/warp.rs#L433-L446)

`f64` 则是零成本地 `to_bits` / `from_bits` 穿过 `u64`：

[`shuffle_f64_sync` 经 `u64` 比特搬运](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-device/src/warp.rs#L522-L525)

落地证据：在 mir-importer 里，`emit_warp_shuffle_i32` 把 `[mask, value, lane]` 三个操作数组装成一个 `dialect-nvvm` shuffle op（op 的具体种类由传入的 `shuffle_opid` 决定，对应 idx/bfly/up/down）：

[`emit_warp_shuffle_i32` 组装 shuffle 方言 op](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-importer/src/translator/terminator/intrinsics/warp.rs#L199-L288)

而 64 位版本 `emit_warp_shuffle_i64` 的文档注释再次点明：64 位 shuffle op **不携带 LLVM intrinsic**，lowering 时由 `convert_shuffle_i64` 拆成内联 PTX：

[`emit_warp_shuffle_i64` 注释说明 64 位拆分 lowering](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-importer/src/translator/terminator/intrinsics/warp.rs#L388-L398)

最后看一个真实 kernel。`shuffle_64` 示例里的 `shuffle_u64_broadcast` 让每个 lane 构造一个高低半不同的 64 位值 `(lane<<32) | (TAG+lane)`，然后读 lane `SRC_LANE=5` 的值——如果拆/拼搞错了，高低半会对不上：

[`shuffle_u64_broadcast` kernel](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/examples/shuffle_64/src/main.rs#L50-L58)

而 `shuffle_u64_neighbor` 演示了越界规则：lane 31 的 `down` 与 lane 0 的 `up` 都会保留自身原值：

[`shuffle_u64_neighbor` kernel —— 演示 down/up 与越界规则](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/examples/shuffle_64/src/main.rs#L81-L91)

#### 4.1.4 代码实践

1. **目标**：直观感受四种 shuffle 模式与 64 位拆分的正确性。
2. **操作步骤**：进入示例目录，编译并运行（无需改源码）：
   ```bash
   cd crates/rustc-codegen-cuda/examples/shuffle_64
   cargo oxide run shuffle_64
   ```
3. **需要观察的现象**：程序会依次打印 4 个测试：
   - Test 1：每个 lane 都收到 lane 5 的完整 64 位值 `(5<<32)|(TAG+5)`，验证高低半拼接无误。
   - Test 2：`shuffle_xor_f64` 蝶形求和，每个 lane 都得到 `1+2+...+32 = 528`。
   - Test 3：`down`/`up` 邻居交换，lane 31 的 `down`、lane 0 的 `up` 因越界保留自身原值。
   - Test 4：两个 16-lane 半 warp 各自独立 shuffle，互不串扰（验证参与掩码正确接线）。
4. **预期结果**：每项都打印 `✓`，结尾输出 `SUCCESS`。若无 GPU，可改用 `cargo oxide build shuffle_64` 仅验证能编译通过（运行结果待本地验证）。
5. **延伸**：把 Test 2 的种子 `v = (lane as f64) + 1.0` 改成 `(lane as f64) * 2.0`，先在纸上算出新结果（应为 `2*(0+1+...+31) = 992`），再运行核对。

#### 4.1.5 小练习与答案

**练习 1**：`shuffle_xor(val, 0)` 的结果是什么？为什么？
**答案**：等于 `val` 本身。因为源 lane = `lane_id XOR 0 = lane_id`，每个 lane 读的是自己。

**练习 2**：为什么 cuda-oxide 没有 `shfl.sync.*.b64` 对应的单条 64 位 shuffle 指令？
**答案**：PTX 与 LLVM 都不提供 64 位 shuffle；硬件 `shfl.sync` 只搬 32 位寄存器。所以 64 位值必须拆成高低两个 32 位半分别 shuffle 再拼接。

**练习 3**：`shuffle_down(val, 1)` 在 lane 31 上返回什么？
**答案**：返回 lane 31 自己的 `val`。因为源 lane `31+1=32` 越界，PTX 规定越界时调用 lane 保留自身原值。

---

### 4.2 Lanemask 与投票：warp 级谓词与位置编码

#### 4.2.1 概念说明

warp 里有两类「每位 lane 一个 bit」的信息常常被搞混，本节专门讲清楚。

**A. 投票（vote）—— 把每条 lane 的 bool 收成一个掩码**

`ballot_sync(mask, predicate)` 让每条参与 lane 各自评估一个 `predicate: bool`，然后把结果汇总成一个 32 位掩码：**bit `k` = 1 当且仅当 lane `k` 在 mask 内且其 predicate 为真**。这是「warp 级的集体布尔运算」。配套的两个布尔归约：

- `all_sync(mask, predicate)`：所有参与 lane 的 predicate 是否**全为真**。
- `any_sync(mask, predicate)`：是否**至少一个**参与 lane 的 predicate 为真。

便捷函数 `ballot` / `all` / `any` = `*_sync(u32::MAX, ...)`。`popc(predicate)` 是 `ballot(predicate).count_ones()` 的简写，直接给出「warp 内有多少 lane 的 predicate 为真」。

**B. 位置掩码（lanemask）—— 描述「我相对于其他 lane 的位置」**

这是五个**只读特殊寄存器**，每条 lane 各自读取、描述自己的编号在 warp 里的位置，**不需要参与掩码、不是集体指令**：

| 寄存器 | lane `i` 的值 | 含义 |
|--------|---------------|------|
| `lanemask_lt` | `(1 << i) - 1` | 编号**严格小于**我的 lane 集合 |
| `lanemask_le` | `(1 << (i+1)) - 1` | 编号**小于等于**我的 lane 集合（含自身） |
| `lanemask_eq` | `1 << i` | **只有我自己** |
| `lanemask_ge` | `!(lanemask_lt)` | 编号**大于等于**我的 lane 集合 |
| `lanemask_gt` | `!(lanemask_le)` | 编号**严格大于**我的 lane 集合 |

把投票和位置掩码组合起来，就能写出 warp 级的**排他前缀和**：`(ballot & lanemask_lt()).count_ones()` = 「在我之前、predicate 也为真的 lane 数量」。这正是流压缩（stream compaction）算输出槽位的核心公式（详见 4.4）。

#### 4.2.2 核心流程

一次 `ballot_sync(mask, predicate)` 的语义：

```text
每个参与 lane k:
    1. 计算自己的 predicate（一个 bool）。
    2. 硬件把 32 个 lane 的 predicate 拼成一个 32 位掩码 B:
       B 的 bit k = (k 在 mask 中) && (lane k 的 predicate 为真)。
    3. 把同一个 B 广播回每条参与 lane。
后续典型组合:
    rank = (B & lanemask_lt()).count_ones();   // 在我之前有多少 lane 投了真
```

`lanemask_lt()` 只是一次特殊寄存器读（`%lanemask_lt`），不参与上面的集体过程，因此可以放在任何地方、不受收敛约束。

#### 4.2.3 源码精读

五个位置掩码寄存器，文档注释里都点出了 PTX / LLVM lowering 目标与位运算含义。以最常用的 `lanemask_lt` 为例（对 lane `i` 返回 `(1<<i)-1`）：

[`lanemask_lt()` —— PTX `%lanemask_lt`](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-device/src/warp.rs#L101-L105)

其余四个（`le`/`eq`/`ge`/`gt`）结构相同：

[`lanemask_le / eq / ge / gt`](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-device/src/warp.rs#L113-L148)

顶部那段注释把「ballot + lanemask_lt」的典型用法写得很清楚——`rank` 就是「在我之前也投了真的 lane 数」，即我在压缩后输出里的槽位：

[ballot + lanemask_lt 组合成前缀和的惯用法注释](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-device/src/warp.rs#L73-L94)

三个投票原语的底层带 mask 版本：

[`ballot_sync` —— PTX `vote.sync.ballot`](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-device/src/warp.rs#L596-L600)

[`all_sync` / `any_sync`](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-device/src/warp.rs#L576-L589)

便捷封装：`ballot` / `all` / `any` 把 mask 钉成全 1；`popc` 直接给出 popcount：

[`ballot` / `popc` 便捷函数](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-device/src/warp.rs#L644-L655)

落地证据：mir-importer 的 `emit_warp_vote` 把 `[mask, predicate]` 组装成一个 vote op（`ballot` 返回 i32 掩码，`all`/`any` 返回 i1）：

[`emit_warp_vote` 组装投票方言 op](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-importer/src/translator/terminator/intrinsics/warp.rs#L798-L881)

示例 `lanemask_scan` 直接把每条 lane 的 `%lanemask_lt` 写到一个 buffer，让你能肉眼看到 `(1<<i)-1` 这个模式：

[`lanemask_lt_values` kernel —— 逐 lane 读出位置掩码](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/examples/lanemask_scan/src/main.rs#L39-L47)

而 `all_lanemasks` 把五个寄存器一并写出，宿主侧用数学不变量（`lt=(1<<i)-1`、`le=(2<<i)-1`、`eq=1<<i`、`ge=!lt`、`gt=!le`）逐 lane 校验：

[`all_lanemasks` kernel 与宿主侧不变量校验](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/examples/lanemask_scan/src/main.rs#L54-L68)

#### 4.2.4 代码实践

1. **目标**：亲眼看到五个位置掩码寄存器在不同 lane 上的取值，验证 `(1<<i)-1` 等公式。
2. **操作步骤**：
   ```bash
   cd crates/rustc-codegen-cuda/examples/lanemask_scan
   cargo oxide run lanemask_scan
   ```
3. **需要观察的现象**：Test 1 会打印 `lane 0..4` 与 `lane 30,31` 的 `lanemask_lt` 值；Test 3 会打印 lane 0/1/16/31 的全部五个掩码。
4. **预期结果**：例如 lane 0 的 `lt=0x00000000`、lane 1 的 `lt=0x00000001`、lane 16 的 `lt=0x0000ffff`、lane 31 的 `lt=0x7fffffff`；`eq` 始终是 `1<<lane`。程序打印 `✓ all five lanemask registers correct`。运行结果待本地验证（无 GPU 可用 `cargo oxide build`）。
5. **延伸**：在某个 kernel 里写 `let n_even = warp::popc(warp::lane_id() % 2 == 0);`，预测 `n_even` 的值（应为 16），再用 `ballot` 打印掩码核对。

#### 4.2.5 小练习与答案

**练习 1**：`lanemask_lt()` 和参与掩码 `active_mask()` 有什么本质区别？
**答案**：`lanemask_lt()` 是只读特殊寄存器，描述「编号比我小的 lane 集合」，与运行时哪些 lane 活跃无关，也不是集体指令；`active_mask()` 返回「当前动态执行区里与我一同收敛的 lane 集合」，会随分支发散/收敛而变化，常用来给 `*_sync` 提供参与掩码。

**练习 2**：用 `ballot` 和 `lanemask_lt` 写一个表达式，得到「warp 内 predicate 为真、且编号严格小于我的 lane 数」。
**答案**：`(warp::ballot(predicate) & warp::lanemask_lt()).count_ones()`。

**练习 3**：lane 31 的 `lanemask_le()` 等于多少？
**答案**：`(1<<32)-1` 在 32 位上 wrapping 减后等于 `0xFFFF_FFFF`（全 1），即「小于等于 31 的所有 lane」覆盖整个 warp。

---

### 4.3 Warp 归约：蝶形与顺序模式

#### 4.3.1 概念说明

**归约（reduction）** 是把 N 个值用一个满足结合律的算子（求和、求最大、求最小、按位与/或/异或）合并成一个值。warp 粒度下有两种经典做法，本节都讲：

**A. 蝶形归约（butterfly / `shuffle_xor`）** —— 5 步之后**每个 lane 都持有完整结果**。

每步 `val = val OP shuffle_xor_f32(val, stride)`，`stride` 依次取 `16,8,4,2,1`。因为 `bfly` 是对称交换（`a` 与 `a XOR stride` 互读），每步参与的两个 lane 都会得到相同的「两者合并值」，5 步后所有 lane 的 `val` 收敛到同一个全 warp 结果。这适合「每个 lane 都需要这个归约值」的场景（如 warp 级 softmax：每个 lane 都要除以 sum）。

**B. 顺序归约（`shuffle_down`）** —— 5 步之后**只有 lane 0 持有完整结果**。

每步 `val = val OP shuffle_down_f32(val, stride)`，`stride` 同样取 `16,8,4,2,1`。`down` 把高 lane 的值往低 lane 拉：lane 0 依次吸收 lane 16、8、4、2、1 的贡献，最终独占全和。适合「只需一个 lane（通常是 lane 0）写出结果」的场景，省一次广播。

**C. 单指令归约（`redux.sync`，sm_80+）** —— 一条指令完成整个 warp 的 add / min / max / and / or / xor，结果广播给所有参与 lane。它不依赖 shuffle 序列，对加法/极值这类常见归约是最快的路径，但要求 Ampere 及以上架构，且算子必须是硬件支持的那几种。

#### 4.3.2 核心流程

**蝶形求和**（每个 lane 终态相同）：

```text
val ← 自己的输入
for stride in [16, 8, 4, 2, 1]:
    val ← val + shuffle_xor_f32(val, stride)   // 与对位 lane 交换并相加
// 5 步后: val == 全 warp 之和（每个 lane 都一样）
```

**顺序求和**（仅 lane 0 持有结果）：

```text
val ← 自己的输入
for stride in [16, 8, 4, 2, 1]:
    val ← val + shuffle_down_f32(val, stride)  // 把高编号 lane 的值往低拉
// 5 步后: 只有 lane 0 == 全 warp 之和
```

两相比较：蝶形用对称交换、全员收敛；顺序用单向拉取、汇聚到 lane 0。把 `+` 换成 `max`（用 `if` 取较大值，或用 `f32::max`）就成了 max 归约——这正是本讲的综合实践任务。

复杂度：两种 shuffle 归约都是

\[ T = \lceil \log_2 N \rceil = 5 \text{ 次 shuffle} \]

而 `redux.sync` 是 1 条指令（受架构支持限制）。

#### 4.3.3 源码精读

`warp.rs` 顶部的文档示例就是一份蝶形求和模板，注释点明「5 步后 lane 0 持有和」：

[文档里的蝶形归约模板](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-device/src/warp.rs#L26-L44)

`shuffle_xor_f32` 的便捷封装：

[`shuffle_xor_f32` = `shuffle_xor_f32_sync(u32::MAX, ...)`](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-device/src/warp.rs#L415-L419)

`shuffle_down_f32` 的便捷封装：

[`shuffle_down_f32`](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-device/src/warp.rs#L421-L425)

`redux.sync` 单指令归约家族——以加法为例（注意它对 `u32`/`i32` 都成立，因为补码加法比特相同；收敛约束同其他 `*_sync`）：

[`redux_sync_add` —— sm_80+ 单指令加法归约](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-device/src/warp.rs#L764-L768)

以及无符号 / 有符号 max：

[`redux_sync_max_u32` / `redux_sync_max_i32`](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-device/src/warp.rs#L806-L820)

真实 kernel：`warp_reduce` 示例的 `warp_reduce_sum` 用蝶形求和，`if lane == 0` 才写结果，处理了输入不足整 warp 时补 0 的边界：

[`warp_reduce_sum` —— 蝶形 `shuffle_xor` 求和](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/examples/warp_reduce/src/main.rs#L29-L55)

`warp_reduce_sum_down` 对照地用 `shuffle_down`，注释明确「只有 lane 0 持有完整和」：

[`warp_reduce_sum_down` —— 顺序 `shuffle_down` 求和](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/examples/warp_reduce/src/main.rs#L59-L84)

宿主侧用 raw `LaunchConfig` 启动，按 [u2-l4](u2-l4-launching-kernels.md) 的安全边界，启动调用包在 `unsafe` 块里并附 `SAFETY:` 注释：

[raw LaunchConfig 与 unsafe 启动](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/examples/warp_reduce/src/main.rs#L147-L159)

#### 4.3.4 代码实践（本讲主实践：warp 内 max 归约）

1. **目标**：基于 `warp_reduce` 示例，用 `shuffle_xor` 实现一个 **warp 内 max 归约**，并解释为何不需要共享内存。
2. **操作步骤**：
   - 复制示例：`cp -r crates/rustc-codegen-cuda/examples/warp_reduce crates/rustc-codegen-cuda/examples/warp_max_reduce`（并改 `Cargo.tom` 里的包名与示例名）。
   - 在 `mod kernels` 里新增一个 kernel（示例代码，仿照 `warp_reduce_sum` 的结构）：
     ```rust
     // 示例代码：warp 内 max 归约（蝶形）
     #[kernel]
     pub fn warp_reduce_max(data: &[f32], mut out: DisjointSlice<f32>) {
         let gid = thread::index_1d();
         let lane = warp::lane_id();

         // 取自己的值（越界补 f32::NEG_INFINITY，不影响 max）
         let mut val = if gid.in_bounds(out.len() * 32) {
             data[gid.get()]
         } else {
             f32::NEG_INFINITY
         };

         // 蝶形 max 归约：把 + 换成 f32::max
         val = val.max(warp::shuffle_xor_f32(val, 16));
         val = val.max(warp::shuffle_xor_f32(val, 8));
         val = val.max(warp::shuffle_xor_f32(val, 4));
         val = val.max(warp::shuffle_xor_f32(val, 2));
         val = val.max(warp::shuffle_xor_f32(val, 1));

         // 蝶形模式：每个 lane 都持有全 warp 的 max，只需 lane 0 写出
         if lane == 0 {
             let warp_idx = gid.get() / 32;
             unsafe { *out.get_unchecked_mut(warp_idx) = val; }
         }
     }
     ```
   - 在 `main` 里准备一组已知输入（如每个 warp 是 `0..32`），用 `unsafe { module.warp_reduce_max(...) }` 启动，回收后与 CPU 参考的每 warp max（应为 31.0）对比。
3. **需要观察的现象**：每个 warp 的输出都等于该 warp 32 个输入里的最大值。
4. **预期结果**：所有 warp 输出 == 31.0（对 `0..32` 输入）。运行结果待本地验证。
5. **为何不需要共享内存**：蝶形归约里，每步参与交换的两个 lane 通过 `shuffle_xor` **直接读取对方的寄存器值**并就地取 `max`，全程数据都在寄存器里流转；既不写共享内存，也不需要 `sync_threads()` 屏障——`shuffle` 本身就是 warp 一致的集体指令，32 个 lane 锁步执行，天然同步。共享内存方案则需要「写 → `sync_threads` → 读」三段式，延迟更高。若你的 GPU 是 sm_80+，还可以把整个循环替换成单条 `warp::redux_sync_max_i32(...)`（整数情形）体验单指令归约。

#### 4.3.5 小练习与答案

**练习 1**：蝶形归约 5 步之后，lane 5 和 lane 10 的 `val` 是什么关系？为什么？
**答案**：两者相等，都等于全 warp 之和（或 max）。因为 `bfly` 是对称交换：每步 `a` 与 `a XOR stride` 互读互加，两个对位 lane 步入下一步时持有相同的合并值；5 步覆盖了全部 32 个 lane 的两两配对，故所有 lane 收敛到同一结果。

**练习 2**：`shuffle_down` 顺序归约 5 步之后，哪些 lane 持有正确的全 warp 和？
**答案**：只有 lane 0。`down` 把高编号 lane 的值单向拉向低编号，lane 0 依次吸收 lane 16/8/4/2/1 的贡献；其他 lane 只收集了部分 lane 的值，没有完整和。

**练习 3**：把求和改成求 max 时，越界（输入不足 32 的倍数）的 lane 应该补什么值？为什么不能补 0？
**答案**：应补 `f32::NEG_INFINITY`（无符号整数补 `0`、有符号补 `i32::MIN`）。若补 0，当真实数据全为负时 max 会被错误地取成 0（补的那条 lane 的值），污染结果。补「单位元的逆」（max 的单位元是 `-∞`）保证它绝不参与 max。

---

### 4.4 Warp 扫描：排他前缀和与流压缩

#### 4.4.1 概念说明

**扫描（scan / prefix sum）** 是归约的「带位置」版本：对每个位置 `i`，输出前 `i` 个值的某种累计。**排他扫描（exclusive scan）** 不含自身：`out[i] = x0 ⊕ x1 ⊕ ... ⊕ x[i-1]`，`out[0] = 单位元`。

warp 级排他扫描有一个极其优雅的「**一次 ballot + 一次 popcount**」实现，专门用于**流压缩（stream compaction）**——把一个数组里满足谓词的元素紧凑地拷贝到输出前面，丢掉不满足的。问题本质是：每条保留的 lane 需要知道「在我之前有多少条 lane 也被保留了」，那就是它在压缩输出里的槽位。

公式（4.2 已见过）：

\[ \text{rank}_i = \mathrm{popcount}\bigl(\text{ballot}(\text{keep}) \mathbin{\&} \text{lanemask\_lt}()\bigr) \]

这里 `ballot(keep)` 是全 warp 的保留掩码，`lanemask_lt()` 是「编号严格小于我」的掩码，二者按位与之后 popcount，正好是「编号比我小且 keep 为真的 lane 数」——也就是排他前缀和。

与蝶形扫描（Kogge-Stone / `shuffle_up` 多步）相比，这个 ballot 技巧**只对二元谓词（keep/drop）有效**，但只需一次集体操作 + 一次 popcount，是流压缩的标准武器。

#### 4.4.2 核心流程

```text
每条 lane k:
    keep_k = (gid 在界内) && (data[gid] != 0)         // 我的保留谓词
    B      = ballot_sync(FULL_MASK, keep_k)            // 全 warp 的保留掩码
    rank_k = popcount(B & lanemask_lt())               // 在我之前被保留的 lane 数 = 我的输出槽位
    若 keep_k: 把 data[gid] 写到 compact_out[rank_k]   // 流压缩
```

注意三点：

1. `ballot` 是**集体指令**：mask 中所有 lane 必须带相同 mask 到达，故本模式适合「整 warp 都在界内」或先用 `active_mask()` 收敛子集的情形。
2. `rank` 是**排他**的（不含自身），所以第一条保留 lane 的 `rank == 0`，正好写到输出首位。
3. 整个过程**不分配共享内存、不写循环**：一个 ballot、一个 popcount，warp 宽度内一步到位。

#### 4.4.3 源码精读

`lanemask_scan` 示例的 `warp_compact_rank` kernel 把上面的公式原样实现，注释解释了 `rank` 的语义：

[`warp_compact_rank` —— ballot + lanemask_lt 实现排他前缀和](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/examples/lanemask_scan/src/main.rs#L76-L92)

宿主侧用「每 3 个元素保留 1 个」的输入，并给出 CPU 参考的排他前缀和（每个 warp 重置累加器），逐元素比对：

[宿主侧 CPU 参考与校验](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/examples/lanemask_scan/src/main.rs#L147-L181)

文件顶部的文档注释把 `rank` 的含义和「一次 ballot、一次 popcount、无共享内存、无循环」的卖点写得最清楚：

[lanemask_scan 文档：流压缩 rank 的核心公式](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/examples/lanemask_scan/src/main.rs#L6-L19)

#### 4.4.4 代码实践

1. **目标**：理解排他前缀和如何用 ballot + lanemask_lt 一步算出，并能改谓词验证。
2. **操作步骤**：
   ```bash
   cd crates/rustc-codegen-cuda/examples/lanemask_scan
   cargo oxide run lanemask_scan
   ```
3. **需要观察的现象**：Test 2 打印 `ranks[0..8]`；输入是「每 3 个保留 1 个」（`i%3==0` 时为 1，否则 0），所以 lane 0 的 rank=0、lane 3 的 rank=1、lane 6 的 rank=2 ……
4. **预期结果**：GPU ranks 与 CPU 排他前缀和逐元素相等，程序打印 `✓ warp_compact_rank matches CPU exclusive prefix sum`。运行结果待本地验证。
5. **延伸**：把谓词改成 `data[gid] > 5`（先把 `data_host` 改成 `0..N`），手算每个 warp 的排他前缀和，再运行核对。再尝试：把 `ballot_sync(FULL_MASK, keep)` 换成便捷的 `warp::ballot(keep)`，确认行为一致（两者只差一个 mask 形参）。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `rank = popcount(B & lanemask_lt())` 是**排他**前缀和（不含自身）？怎样改成**包含自身**（inclusive）？
**答案**：`lanemask_lt()` 只置位「严格小于我」的 lane，故 popcount 不含 lane `k` 自己，是排他的。改成 inclusive 只需把 `lanemask_lt()` 换成 `lanemask_le()`（含自身），或写作 `rank + (keep_k as u32)`。

**练习 2**：流压缩里，第一条被保留的 lane 算出的 `rank` 是多少？为什么这正好是它该去的输出槽位？
**答案**：是 0。因为它之前没有任何被保留的 lane，popcount 为 0；作为第一条保留记录，它就该写到压缩输出的第 0 位。

**练习 3**：`ballot_sync` 为什么要求「mask 内所有 lane 带相同 mask 到达」？如果在一个发散分支里、只有部分 lane 调用 `ballot_sync(FULL_MASK, ...)` 会怎样？
**答案**：`ballot_sync` 是收敛的集体指令，硬件需要所有 mask 内的 lane 一起执行才能拼出正确的 32 位掩码。若只有部分 lane 到达而 mask 写成全 1，未到达的 lane 不会贡献其 predicate，行为未定义（可能死锁或得到垃圾掩码）。正确做法是在分支内用 `active_mask()` 拿到「实际到达的 lane 子集」作为 mask。

---

## 5. 综合实践：warp 级「求 max 并统计并列者」

把本讲四个模块串起来，设计一个 kernel：对一个 warp 的 32 个 `i32` 输入，求出全 warp 的最大值，并统计有多少个 lane 的值等于这个最大值。它需要用到：

- **4.3 归约**：先做一次 warp 内 max 归约，得到 `m`（用 `redux_sync_max_i32` 或蝶形 `shuffle_*`）。
- **4.2 投票**：再用 `ballot_sync` 对谓词 `val == m` 投票，`popcount` 得到「并列最大值的 lane 数」。
- **4.1 shuffle**：如果你用蝶形 max 归约，每条 lane 都已经持有 `m`，无需额外广播；若用 `redux`，结果也已广播。

示例代码（示例代码，仅作设计参考，未在本仓库中实现）：

```rust
#[kernel]
pub fn warp_max_and_ties(data: &[i32], mut out_max: DisjointSlice<i32>, mut out_ties: DisjointSlice<u32>) {
    let gid = thread::index_1d();
    let lane = warp::lane_id();

    let val = if gid.in_bounds(out_max.len() * 32) { data[gid.get()] } else { i32::MIN };

    // (A) 归约：全 warp max。sm_80+ 用 redux；否则用蝶形 shuffle_xor_i32 序列。
    let m = warp::redux_sync_max_i32(u32::MAX, val);

    // (B) 投票：有多少 lane 等于 m。
    let tie_mask = warp::ballot_sync(u32::MAX, val == m);
    let ties = tie_mask.count_ones();

    // (C) lane 0 写出（每条 lane 的 m、ties 都相同，写一个即可）。
    if lane == 0 {
        let warp_idx = gid.get() / 32;
        unsafe {
            *out_max.get_unchecked_mut(warp_idx) = m;
            *out_ties.get_unchecked_mut(warp_idx) = ties;
        }
    }
}
```

**实践要点**：

1. 把它放进一个新的示例 crate（仿照 `warp_reduce` 的 `Cargo.toml` 与 `main.rs` 结构），输入构造若干个 warp，每个 warp 故意放入重复的最大值。
2. 用 `cargo oxide pipeline warp_max_and_ties` 查看 lowering 产物：你能看到 `redux.sync.max.s32` 与 `vote.sync.ballot` 对应的 NVVM/PTX 指令。
3. 思考：为什么这个 kernel 全程不需要 `SharedArray` 与 `sync_threads()`？因为归约由 warp 级集体指令完成、投票也是 warp 级集体指令，二者作用域都是 warp，warp 内天然锁步同步——这正是本讲反复强调的「warp 粒度上，寄存器 + 集体指令即可替代共享内存 + 屏障」。运行结果待本地验证。

## 6. 本讲小结

- **warp 是最小执行单位**：32 个 lane 锁步执行，warp 内通信走寄存器（shuffle，~2 周期）而非共享内存（~20 周期），且隐式同步、不需要 `sync_threads()`。
- **shuffle 有四种寻址模式**：`idx`（广播）、`bfly`/XOR（蝶形，对称交换）、`up`、`down`（单向）；每种有 u32/f32 版本，64 位值必须拆成两个 32 位半分别 shuffle。
- **务必区分两类掩码**：参与掩码（`active_mask`、`*_sync` 的 `mask` 形参，描述「谁参与」）与位置掩码（`lanemask_lt/le/eq/ge/gt`，描述「我相对他人的位置」，只读、非集体）。
- **投票** `ballot/any/all` 把每条 lane 的 bool 收成 32 位掩码；`popc` 直接给 popcount。
- **warp 归约**：蝶形 `shuffle_xor` 让每个 lane 都拿到全 warp 结果（5 步）；`shuffle_down` 只让 lane 0 拿到（顺序）；sm_80+ 的 `redux.sync` 用一条指令搞定 add/min/max/and/or/xor。
- **warp 扫描**：`(ballot(keep) & lanemask_lt()).count_ones()` 一步算出排他前缀和，是流压缩的标准武器——一次集体操作 + 一次 popcount，无共享内存、无循环。
- **落地路径**：所有 warp 函数体在 `cuda-device` 里是 `unreachable!()` 占位桩，由 mir-importer 翻译成 `dialect-nvvm` op，再 lowering 成 PTX 指令或 NVVM intrinsic。

## 7. 下一步学习建议

- **横向扩展 warp 能力**：阅读 [crates/cuda-device/src/warp.rs](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-device/src/warp.rs) 的 `match_any_sync`/`match_all_sync`（sm_70+，用于去重、聚类头选举）与 `elect_sync`/`is_elected_sync`（sm_90+，warp 聚合写入的领导者选举），它们是本讲原语的高级组合。
- **走向块级协作**：本讲所有算法都局限在 warp 内；下一讲 [u5-l2](u5-l2-scoped-and-packed-atomics.md)（作用域原子与打包原子）会讨论跨 warp 的原子协作，届时你会再次用到本讲的 `ballot`/`active_mask` 来做 warp 聚合原子。
- **Hopper/Blackwell 异步机制**：[u5-l3](u5-l3-async-barriers-and-copy.md)（异步屏障与异步拷贝）与 [u5-l5](u5-l5-tensor-memory-accelerator.md)（TMA）会用到 `bar.warp.sync` 与更复杂的参与掩码管理；本讲对参与掩码的理解是那里的前置。
- **编译器侧深潜**：如果想看清 `shuffle`/`ballot`/`redux` 如何从函数调用变成 PTX，可顺着本讲引用的 [mir-importer/src/translator/terminator/intrinsics/warp.rs](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-importer/src/translator/terminator/intrinsics/warp.rs) 进入 [u6-l2](u6-l2-mir-importer-intrinsics-deep-dive.md) 的 intrinsic 翻译机深潜。
- **张量核**：warp 级编程的巅峰是矩阵乘加速器 `mma.sync`（[u5-l6](u5-l6-matrix-multiply-accelerators.md)），它要求你彻底理解 lane 与 fragment 的映射——本讲的 lane/warp 概念是那里的基础。
