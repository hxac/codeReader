# 矩阵乘加速器：mma.sync / WGMMA / tcgen05

## 1. 本讲目标

矩阵乘（GEMM）是深度学习与科学计算里最耗算力的运算。现代 NVIDIA GPU 专门为它配备了**张量核（Tensor Core）**——一条独立于 CUDA Core 的乘加流水线，能在一条指令里完成一个小矩阵块（tile）的 \(D = A \times B + C\)。cuda-oxide 把三代张量核指令都封装成了纯 Rust 的 `unsafe` 设备函数。

学完本讲，你应当能够：

1. 说清**三代矩阵乘加速器**——warp 级 `mma.sync`、Hopper 的 `wgmma`、Blackwell 的 `tcgen05`——在「谁来发指令」「操作数放哪」「怎么等它算完」三个维度上的本质差异。
2. 读懂 cuda-oxide 设备 API 的「桩函数 + 编译器识别」约定：函数体是 `unreachable!()`，真正的 PTX 由 `mir-importer`/`mir-lower` 注入。
3. 手算一个 warp 分布式 **fragment（片段）** 在 32 个 lane 上的分布，区分 f16/bf16/tf32/f64/s8 五种 dtype 的 mma.sync。
4. 理解 `ldmatrix` 如何把共享内存里的矩阵块喂给 mma.sync 的寄存器片段。
5. 把 wgmma 的「fence → mma_async → commit_group → wait_group」异步协议，与 tcgen05 的「单线程下单 + mbarrier 等待 + TMEM」模型对应起来。

## 2. 前置知识

本讲是「专家：高级设备能力」单元的最后一讲，默认你已经读过：

- **u5-l1 Warp 级编程**：warp = 32 个 lane 锁步执行，`lane_id = threadIdx % 32`；本讲的 `mma.sync` 就是 warp 协作的。
- **u2-l3 共享内存与同步**：`SharedArray<T,N>`、`sync_threads()`、`threadfence`。`ldmatrix`/`wgmma`/`tcgen05` 都从共享内存取操作数。
- **u5-l3 异步屏障与异步拷贝**：mbarrier 的 arrive/wait 协议、`fence_proxy_async_shared_cta`。tcgen05 完成同步就靠 mbarrier。

几个本讲反复用到的小概念：

- **tile（块）**：把大矩阵切成固定大小的小块（如 \(16\times 8\)）交给一条张量核指令。
- **fragment（片段）**：一个 tile 被「摊」到 warp 的 32 个 lane 的寄存器里，每个 lane 持有其中若干元素；这部分寄存器视图就叫 fragment。**fragment 布局由硬件规定**，软件必须照此排布操作数，否则算的是另一个矩阵。
- **lane / group / thread**：本讲里对 `lane`（0..31）做分解 `group = lane / 4`、`thread = lane % 4`，是 PTX mma.sync 文档的通用记法。
- **TMEM（Tensor Memory）**：Blackwell 新增的、专给张量核用的片上存储，与寄存器/共享内存并列。

数学上，张量核一次算的是矩阵乘加：

\[
D_{M\times N} \;=\; A_{M\times K}\,B_{K\times N} \;+\; C_{M\times N}
\]

三代加速器的根本区别，是 \((M,N,K)\) 越来越大、操作数从「寄存器」搬到「共享内存」再搬到「TMEM」、指令从「同步」变「异步」。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [crates/cuda-device/src/wmma.rs](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-device/src/wmma.rs) | warp 级矩阵操作桩：`movmatrix`、`ldmatrix`、五种 dtype 的 `mma.sync` |
| [crates/cuda-device/src/wgmma.rs](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-device/src/wgmma.rs) | Hopper warpgroup MMA（m64n64k16）桩与 SMEM 描述符 |
| [crates/cuda-device/src/tcgen05.rs](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-device/src/tcgen05.rs) | Blackwell tcgen05 桩：TMEM 分配、单线程 MMA、指令/SMEM 描述符、stmatrix |
| [crates/dialect-nvvm/src/ops/wmma.rs](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/dialect-nvvm/src/ops/wmma.rs) | mma.sync 在 IR 层的 op 定义与最小校验 |
| [crates/mir-importer/src/translator/terminator/intrinsics/wmma.rs](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-importer/src/translator/terminator/intrinsics/wmma.rs) | 把 Rust 调用翻译成 dialect-nvvm op（数组拆成 SSA 寄存器） |
| [crates/mir-lower/src/convert/intrinsics/wmma.rs](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-lower/src/convert/intrinsics/wmma.rs) | 把 op 降级成一条 convergent inline-asm PTX 指令 |
| [crates/rustc-codegen-cuda/examples/gemm/src/main.rs](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/examples/gemm/src/main.rs) | 朴素 SGEMM（每线程一元素，**不**用张量核，作对照） |
| [crates/rustc-codegen-cuda/examples/standalone_device_fn/src/main.rs](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/examples/standalone_device_fn/src/main.rs) | 唯一演示 warp 级 mma.sync（f16/tf32/s8）的示例 |
| [crates/rustc-codegen-cuda/examples/tcgen05/src/main.rs](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/examples/tcgen05/src/main.rs) | tcgen05 完整流水线示例（alloc→cp→mma→commit→ld→dealloc） |

## 4. 核心概念与源码讲解

### 4.1 三代矩阵乘加速器总览

#### 4.1.1 概念说明

NVIDIA 的张量核经历了三代演进，cuda-oxide 用三个模块分别封装：

| 维度 | `mma.sync`（Ampere 及以后） | `wgmma`（Hopper sm_90a） | `tcgen05`（Blackwell sm_100a） |
|------|------------------------------|--------------------------|--------------------------------|
| 设备文件 | `wmma.rs` | `wgmma.rs` | `tcgen05.rs` |
| 谁发指令 | **整个 warp（32 线程）** 协作 | **整个 warpgroup（128 线程）** 协作 | **1 个线程** |
| 典型 tile | \(16\times 8\times 16\) 等小块 | \(64\times 64\times 16\) | \(128\times 256\times 16\) |
| A/B 操作数 | warp 各 lane 的**寄存器**片段 | **共享内存**（用 SMEM 描述符） | A 在 **TMEM**、B 在共享内存 |
| 累加器 D | 寄存器 | 寄存器 | **TMEM** |
| 同步模型 | 同步指令（发即等） | 异步：`commit_group`+`wait_group` | 异步：`commit` 到 mbarrier |
| 最低架构 | sm_80（部分 ldmatrix/movmatrix sm_75） | sm_90a | sm_100a |

演进的主线很清晰：**把操作数搬离寄存器**。寄存器是 lane 私有的，要靠软件（`ldmatrix`）费力地把数据排成 fragment；wgmma 直接吃共享内存描述符，硬件自己去取；tcgen05 更进一步，把累加器和 A 都放进专用 TMEM，连「凑 fragment」都省了，于是**一条指令可以由一个线程下单**。

#### 4.1.2 核心流程

无论哪一代，cuda-oxide 里的张量核函数都是**桩（stub）**：

```text
源码层：  unsafe fn mma_...(c, a, b) -> d { unreachable!(...) }   // 永远不会真执行
            │  函数体只是占位符，真正的语义在「函数名 + 参数类型」里
            ▼
mir-importer：识别函数名 → 拆 [T; N] 数组为 SSA 寄存器 → 生成 dialect-nvvm op
            ▼
mir-lower：  把 op 降级为一条 convergent inline-asm PTX 指令（mma.sync.aligned ...）
            ▼
最终 PTX：   mma.sync.aligned.m16n8k16.row.col.f32.f16.f16.f32 {...}, {...}, {...}, {...};
```

这就是 u1-l1/u5-l1 反复强调的「**编译器识别桩**」模式：函数体 `unreachable!()` 只是为了让 Rust 类型检查通过；运行时绝不能真调到它。所有真实语义由编译流水线在函数名上匹配后注入。

#### 4.1.3 源码精读

三代桩的「自我介绍」都写在模块文档里。wmma.rs 顶部点明了「寄存器操作 + warp 协作加载」这一层定位：

[wmma.rs:6-27](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-device/src/wmma.rs#L6-L27) 说明本模块提供「寄存器内矩阵操作（`movmatrix`、`mma.sync`）+ warp 协作的共享内存加载（`ldmatrix`）」，并强调 `ldmatrix` 是弱内存操作（`.sync` 只汇聚 warp、不排序内存），依赖访问需自带 barrier/fence；而 `movmatrix`/MMA 是纯寄存器操作、无内存副作用。

tcgen05.rs 顶部用一张表把与 wgmma 的区别说得最直白：

[tcgen05.rs:14-20](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-device/src/tcgen05.rs#L14-L20) 四行对照：MMA 下单从「128 线程」缩到「1 线程」、A/D 存储从「寄存器/SMEM」换成「TMEM」、分配从「隐式」变「动态 `tcgen05.alloc`」、等待机制从 `wgmma.wait_group` 换成 `mbarrier.try_wait`。

#### 4.1.4 代码实践

**目标**：用最低成本建立三代加速器「谁来发指令」的体感。

**步骤**：

1. 打开 `crates/cuda-device/src/{wmma,wgmma,tcgen05}.rs` 三个文件的模块注释。
2. 在每个文件里数一下「多少线程参与一次 MMA」：wmma 是 32，wgmma 是 128，tcgen05 是 1。
3. 对照 [README.md:243-261](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/README.md#L243-L261) 的示例表，注意 `tcgen05` 标注的是 `sm_100a`，而朴素 `gemm` 没有张量核标注。

**预期结果**：你会清楚看到，同样的「矩阵乘」语义，三代指令在「线程粒度」上差了两个数量级（32 → 128 → 1），这正是「操作数离寄存器越远，下单线程数越少」的体现。

#### 4.1.5 小练习与答案

**练习 1**：为什么 tcgen05 能用 1 个线程下单一次 MMA，而 mma.sync 需要 32 个？

**答案**：mma.sync 的操作数（A/B）散落在 warp 各 lane 的私有寄存器里，必须 32 个 lane 一起把各自的 fragment 喂给张量核；tcgen05 的操作数（A 和累加器 D）放在专用的 TMEM 里、B 用 SMEM 描述符描述，硬件能自己取，下单线程只需提供地址/描述符，不再需要别人凑 fragment。

**练习 2**：三代里哪一代的累加器 D 不在寄存器里？

**答案**：tcgen05。它的 D 在 TMEM，所以算完后要额外用 `tcgen05.ld` 把结果从 TMEM 取回寄存器才能写回全局内存（见 4.5）。

---

### 4.2 warp 级 mma.sync 与 ldmatrix 片段布局

#### 4.2.1 概念说明

`mma.sync` 是最老的一代张量核指令，命名里的 `mMnNkK` 直接标注了它一次算的 tile 形状：\(M\times N\) 的输出、\(K\) 维度的内积。例如 `m16n8k16` 表示 \(D_{16\times 8} = A_{16\times 16} B_{16\times 8} + C_{16\times 8}\)。

它的两个特点决定了用法：

1. **操作数是寄存器 fragment**：A、B、C/D 都不是内存地址，而是 warp 32 个 lane 各自寄存器里的一组值。硬件规定了「哪个 lane 持有 tile 的哪个元素」，软件必须照此排布。
2. **`.sync` = warp 同步**：32 个 lane 必须同时执行同一条 mma.sync（同一个掩码、同一组限定符），否则是 UB。

要把共享内存里的数据排成 fragment，就用 `ldmatrix`：它让 warp 协作地一次加载多个 \(8\times 8\) b16 矩阵块，**直接按 fragment 布局**写进各 lane 的寄存器，省去手算「lane X 该读哪个地址」。

#### 4.2.2 核心流程

一次「共享内存 → mma.sync」的标准流水线：

```text
1. 各 lane 把全局内存数据搬到 SharedArray（普通 load）
2. sync_threads()                          // 确保共享内存写对全 warp 可见
3. ldmatrix_x4_trans(smem_ptr) -> [u32;4]  // warp 协作加载，输出已是 fragment 布局
4. mma_m16n8k16_f32_f16(c_frag, a_frag, b_frag) -> d_frag   // 寄存器内乘加
5. （可选）重复 3-4，沿 K 维累加
```

`ldmatrix` 的地址-lane 规则（来自 wmma.rs 模块注释）：每 4 个 lane 提供一个 16 字节对齐的行地址——`x1` 用 lane 0..7，`x2` 用 0..15，`x4` 用 0..31。`.trans` 变体按列主序加载。

#### 4.2.3 源码精读

`ldmatrix_x4` 是最常用的加载形式，4 个 lane 组提供 4 个 \(8\times 8\) 块的行地址：

[wmma.rs:133-150](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-device/src/wmma.rs#L133-L150) 加载 4 个打包的 \(8\times 8\) 矩阵，返回 `[u32; 4]`（每个 u32 打包 2 个 b16）；要求**所有 32 个 lane 都提供有效、16 字节对齐的共享内存行地址**，且需要 sm_75+。

注意它返回 `[u32; 4]`，正好匹配 `mma_m16n8k16_f32_f16` 的 `a: [u32; 4]` 形参——这就是 fragment 的「形状契约」：加载和计算的数组长度必须对齐。

[wmma.rs:77-81](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-device/src/wmma.rs#L77-L81) 是桩函数本体：`#[inline(never)]`（禁止内联，保证函数名在 IR 里可被识别）+ `unsafe` + `unreachable!()`，是 cuda-oxide 全部设备 intrinsic 的统一写法。

#### 4.2.4 代码实践

**目标**：亲眼看到 mma.sync 在 PTX 里的样子，确认桩确实被替换成了真指令。

**步骤**：

1. 进入示例目录：`crates/rustc-codegen-cuda/examples/standalone_device_fn`。这是全仓库唯一调用 warp 级 mma.sync 的示例，[main.rs:24](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/examples/standalone_device_fn/src/main.rs#L24) 引入了 `mma_m16n8k8_f32_tf32, mma_m16n8k16_f32_f16, mma_m16n8k32_s32_s8`。
2. 运行 `cargo oxide build standalone_device_fn`（无需 GPU，只编译）。
3. 用 `cargo oxide pipeline standalone_device_fn` 找到生成的 `.ptx` 文件并打开。
4. 在 PTX 里搜索 `mma.sync`，确认存在形如 `mma.sync.aligned.m16n8k16.row.col.f32.f16.f16.f32` 的指令，而**源码里并没有手写汇编**。

**需要观察的现象**：源码里只是普通 Rust 函数调用 `mma_m16n8k16_f32_f16(c, a, b)`，但 PTX 里出现了真实硬件指令；函数体里的 `unreachable!()` 消失了。

**预期结果**：证明桩已被 mir-importer + mir-lower 替换。若本地无工具链，此步骤标注「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：`ldmatrix_x4` 为什么要求「所有 32 个 lane 都提供有效地址」，而不是只用提供地址的那几个 lane？

**答案**：因为 `ldmatrix.sync` 是 warp 协作指令，硬件会读取所有 32 个 lane 提供的地址寄存器（即便部分 lane 的地址不被使用）。在 sm_75 上若高位 lane 地址非法会触发非法访问，所以常见做法是把低位 lane 的地址复制到高位 lane。

**练习 2**：`ldmatrix_x4` 返回 `[u32; 4]`，`mma_m16n8k16_f32_f16` 的 `a` 形参也是 `[u32; 4]`。这个长度一致是巧合吗？

**答案**：不是巧合，是 fragment 形状契约。`ldmatrix_x4` 加载 4 个 \(8\times 8\) 块正好拼出一个 \(16\times 16\) 的 A tile（按 mma 的 A fragment 布局），所以输出长度（4 个 u32）必须等于 mma 的 A 输入长度（4 个 u32）。长度或类型不匹配会在 mir-importer 阶段被 `extract_array_registers` 拒绝。

---

### 4.3 五种 dtype 的 mma.sync：f16 / bf16 / tf32 / f64 / s8

#### 4.3.1 概念说明

cuda-oxide 的 wmma.rs 提供了五种 dtype 的 mma.sync，它们 tile 形状和「每 lane 持有几个元素」各不相同：

| 设备函数 | tile (M·N·K) | A/B 元素类型 | 累加器 D | 每 lane 持有 A/B/C | 最低 sm |
|----------|--------------|--------------|----------|---------------------|---------|
| `mma_m16n8k16_f32_f16`  | 16·8·16 | f16  | f32 | 8 / 4 / 4 | sm_80 |
| `mma_m16n8k16_f32_bf16` | 16·8·16 | bf16 | f32 | 8 / 4 / 4 | sm_80 |
| `mma_m16n8k8_f32_tf32`  | 16·8·8  | tf32 | f32 | 4 / 2 / 4 | sm_80 |
| `mma_m8n8k4_f64`        | 8·8·4   | f64  | f64 | 1 / 1 / 2 | sm_80 |
| `mma_m16n8k32_s32_s8`   | 16·8·32 | s8   | i32 | 16 / 8 / 4 | sm_80 |

注意三个要点：

- **打包（packing）**：f16/bf16/s8 是「小类型」，多个被打包进一个 u32 寄存器（f16/bf16 每 u32 装 2 个，s8 每 u32 装 4 个）。函数签名因此用 `a: [u32; 4]` 而非 `[f16; 8]`——调用方负责把两个 f16 塞进一个 u32 的低/高 16 位。
- **tf32 的特殊性**：tf32 不是 Rust 原生类型，它用 `u32` 承载，但必须是 `cvt.rna.tf32.f32` 产生的合法 tf32 位模式，**不能**直接拿 `f32::to_bits()` 冒充（文档明确警告）。
- **s8 的 K=32**：因为 s8 只占 1 字节，同样 4 个 u32 寄存器能装下 K=32（\(4\times 4\times 8\text{bit}\)），所以 INT8 的 K 维翻倍到 32，吞吐更高。本轮（#329）新增的就是它。

#### 4.3.2 核心流程

所有五种 mma.sync 共享同一个调用骨架，差异只在数组长度与元素类型：

```rust
// f16/bf16 版：A=[u32;4](每 u32 装 2 个 f16), B=[u32;2], C/D=[f32;4]
let d: [f32; 4] = unsafe { mma_m16n8k16_f32_f16(c, a, b) };
```

每条指令完成：

\[
D_{16\times 8} \;=\; A_{16\times 16}\,B_{16\times 8} \;+\; C_{16\times 8}
\]

D 的 fragment 分布（对 f16/bf16/s8 都一样，因为输出都是 \(16\times 8\)）：对 `group = lane/4`、`thread = lane%4`，lane 持有 4 个 D 元素

\[
\text{D}_{\,g + \mathbb{1}[j\ge 2]\cdot 8,\; 2t + (j\bmod 2)},\quad j=0..3
\]

即每个 lane 持有 \((\text{group},\,2\text{thread})\)、\((\text{group},\,2\text{thread}{+}1)\)、\((\text{group}{+}8,\,2\text{thread})\)、\((\text{group}{+}8,\,2\text{thread}{+}1)\) 四个元素。

#### 4.3.3 源码精读

**f16 的完整映射**——这是本讲实践要手算的那张表，由函数 doc-comment 直接给出：

[wmma.rs:232-249](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-device/src/wmma.rs#L232-L249) 给出 A（j=0..7）、B（j=0..3）、C/D（j=0..3）的 `(row, col)` 公式，并说明这是 `.row.col` 布局（A 行主序、B 列主序）；同时强调每个 u32 是「原始 b32 载体，装 2 个 f16，元素 j 在 `a[j/2]`、低 16 位在前高 16 位在后」。

函数签名本身极简，复杂度全在调用方的排布上：

[wmma.rs:271-276](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-device/src/wmma.rs#L271-L276) `mma_m16n8k16_f32_f16(c: [f32;4], a: [u32;4], b: [u32;2]) -> [f32;4]`，`#[must_use]` 提醒别丢弃结果 fragment，`#[inline(never)]` 保证函数名进入 IR。

**INT8 的映射差异**——本轮 #329 新增，关键是 A 维度变成 16×32（K=32）：

[wmma.rs:398-416](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-device/src/wmma.rs#L398-L416) 说明每个 a/b 寄存器「从低字节到高字节打包 4 个补码 s8」，元素 i 装在寄存器 `[i/4]` 的 `(i%4)*8 .. (i%4+1)*8` 位；A 元素 i=0..15，B 元素 i=0..7，且因 K=32，列号公式多了 `i>=8 ? 16 : 0` 的偏移。

[wmma.rs:440-445](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-device/src/wmma.rs#L440-L445) `mma_m16n8k32_s32_s8(c: [i32;4], a: [u32;4], b: [u32;2]) -> [i32;4]`，注意累加器是 `i32` 不是 `f32`，且文档指出这是非 `.satfinite` 形式——有符号累加器溢出是回绕而非钳位。

**mir-lower 把它降级成 PTX**——五种 dtype 共享同一段「拼模板」逻辑，差异只在约束符和指令名：

[mir-lower/wmma.rs:113-156](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-lower/src/convert/intrinsics/wmma.rs#L113-L156) 把 `mma_m16n8k16_f32_f16` 降级为**一条** convergent inline-asm：模板把 14 个操作数（C×4、A×4、B×2 经重排）填进 `mma.sync.aligned.m16n8k16.row.col.f32.f16.f16.f32 {d},{a},{b},{c}`，约束 `=f,=f,=f,=f,f,f,f,f,r,r,r,r,r,r` 表示 4 个 f32 输出、4 个 f32 累加器输入、6 个整型寄存器（A/B 打包值）。结果是 LLVM struct 再 `extractvalue` 拆回 4 个 SSA 值。

[mir-lower/wmma.rs:213-256](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-lower/src/convert/intrinsics/wmma.rs#L213-L256) 是 s8 版本，结构完全相同，但约束全用 `r`（整型）、模板换成 `mma.sync.aligned.m16n8k32.row.col.s32.s8.s8.s32`。

**importer 拆数组**——把 Rust 的 `[u32;4]` 拆成 4 个独立 SSA 寄存器喂给 op：

[mir-importer/wmma.rs:105-157](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-importer/src/translator/terminator/intrinsics/wmma.rs#L105-L157) `extract_array_registers` 用常量索引的 `MirExtractFieldOp` 把数组逐元素拆成标量 SSA 值（这样降到 LLVM 时是 `extractvalue`，不引入栈槽），并校验数组长度与元素类型必须匹配（否则报「MMA fragment must be an array of N scalar registers」）。

#### 4.3.4 代码实践

**目标**：手算一个 \(16\times 8\) 的 D fragment 在 32 个 lane 上的分布，再对比 s8 的差异。这是本讲的指定实践。

**步骤 1（手算 D 分布）**：对 `mma_m16n8k16_f32_f16`，用 [wmma.rs:243-246](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-device/src/wmma.rs#L243-L246) 的 C/D 公式 `row = group + (j>=2 ? 8 : 0)`、`col = thread*2 + (j&1)`，填下面这张表（`group = lane/4`，`thread = lane%4`）：

| lane | group | thread | D[j=0] | D[j=1] | D[j=2] | D[j=3] |
|------|-------|--------|--------|--------|--------|--------|
| 0    | 0     | 0      | (0,0)  | (0,1)  | (8,0)  | (8,1)  |
| 1    | 0     | 1      | (0,2)  | (0,3)  | (8,2)  | (8,3)  |
| 4    | 1     | 0      | (1,0)  | (1,1)  | (9,0)  | (9,1)  |
| 31   | 7     | 3      | ?      | ?      | ?      | ?      |

**步骤 2（验证覆盖性）**：32 lane × 4 元素 = 128 = \(16\times 8\)，且每个 \((row,col)\) 恰好出现一次，证明 fragment 无重叠无遗漏。

**步骤 3（手算 lane 0 的 A 元素）**：用 [wmma.rs:235-238](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-device/src/wmma.rs#L235-L238) 的 A 公式，lane 0（group=0,thread=0）持有 A[0,0]、A[0,1]、A[8,0]、A[8,1]、A[0,8]、A[0,9]、A[8,8]、A[8,9] 共 8 个 f16，打包成 4 个 u32。

**步骤 4（对比 s8）**：读 [wmma.rs:404-416](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-device/src/wmma.rs#L404-L416)，注意 s8 的 **D 分布公式与 f16 完全一致**（仍是 16×8、每 lane 4 元素），差异全在 A（16 元素，K=32）和 B（8 元素）。

**需要观察的现象**：D fragment 分布对 f16/bf16/tf32/s8 **完全相同**（都是 16×8 输出），变的是 A/B 的形状和打包方式。

**预期结果**：上表 lane 31 一行应填 `(7,6) (7,7) (15,6) (15,7)`。

#### 4.3.5 小练习与答案

**练习 1**：`mma_m16n8k8_f32_tf32` 的 A 形参是 `[u32; 4]`，能否直接把 4 个 `f32::to_bits()` 塞进去？

**答案**：不能。tf32 寄存器必须是 `cvt.rna.tf32.f32` 产生的合法 tf32 位模式（1 符号 + 8 指数 + 10 尾数）。直接用 `f32::to_bits()` 得到的是 f32 的 32 位，不是 tf32，[wmma.rs:309-312](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-device/src/wmma.rs#L309-L312) 明确指出「invalid TF32 bit patterns do not become valid merely because they are carried in u32」。

**练习 2**：为什么 s8 版的 K 能做到 32，而 f16 只有 16？

**答案**：因为 s8 只占 8 位，同样 4 个 u32 寄存器能装 \(4\times 4=16\) 个 s8（对应 K=32 时每 lane 持有的 A 元素数），而 f16 占 16 位，4 个 u32 只能装 8 个 f16（K=16）。硬件每条指令的寄存器预算相同，元素越小 K 越大。

**练习 3**：五种 mma.sync 里，哪一个的累加器类型与其他四个不同？

**答案**：`mma_m8n8k4_f64`——它的累加器是 f64（`[f64; 2]`），其余四个的累加器要么 f32 要么 i32。

---

### 4.4 wgmma：Hopper 的异步 warp-group MMA

#### 4.4.1 概念说明

Hopper（sm_90a）把协作单位从 warp（32 线程）提升到 **warpgroup（128 线程 = 4 个 warp）**，并引入了 `wgmma`（warpgroup MMA）。两个根本变化：

1. **操作数来自共享内存，不再是寄存器 fragment**：A、B 用 64 位 **SMEM 描述符（descriptor）** 描述（编码基地址、leading dimension、stride、swizzle 模式），硬件自己去共享内存取。软件再也不用手算 lane→element 映射。
2. **异步**：`wgmma.mma_async` 只是「下单」，不等算完；要用 `commit_group` + `wait_group` 显式同步。这让你可以「下单多个 MMA → 等一次」，重叠计算与数据搬运。

代价是 tile 变大（典型 \(64\times 64\times 16\)），累加器也大：每个线程持 32 个 f32（`[[f32; 8]; 4]`），128 线程 × 32 = 4096 = \(64\times 64\)。

#### 4.4.2 核心流程

wgmma 的标准异步协议（四步）：

```text
1. make_smem_desc(a_smem_ptr) / make_smem_desc(b_smem_ptr)   // 烘焙描述符
2. wgmma_fence()                                              // 保证 SMEM 写对张量核可见
3. wgmma_mma_m64n64k16_f32_bf16(&mut acc, desc_a, desc_b)     // 异步下单（可重复，沿 K 累加）
4. wgmma_commit_group();                                      // 把这批 MMA 打包成一组
5. wgmma_wait_group::<0>();                                   // 等所有组完成（N=0=全等）
```

`wait_group::<N>()` 的 `N` 表示「允许至多剩 N 组未完成」，`N=0` 即等全部完成。这是 Hopper 软件 GEMM 流水线（Producer-Consumer）的核心机制。

#### 4.4.3 源码精读

模块文档用 ASCII 图说明了 m64n64k16 的形状与累加器布局：

[wgmma.rs:14-34](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-device/src/wmma.rs#L14-L34) 画出 A(64×16) × B(16×64) = D(64×64)；累加器「每线程持 32 个 f32，`[[f32;8];4]`，128 线程 × 32 = 4096 = 64×64」。

异步三件套是桩函数（注意 `wgmma_fence`/`commit_group` 不是 `unsafe`，因为它们不直接接触用户数据）：

[wgmma.rs:81-85](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-device/src/wgmma.rs#L81-L85) `wgmma_fence()` 降级为 `wgmma.fence.sync.aligned`，确保共享内存写对张量核硬件可见——下单前必须调。

[wgmma.rs:98-102](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-device/src/wgmma.rs#L98-L102) `wgmma_commit_group()` 降级为 `wgmma.commit_group.sync.aligned`，把已下单的 MMA 打包成可等待的一组。

[wgmma.rs:132-137](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-device/src/wgmma.rs#L132-L137) `wgmma_wait_group::<N>()` 用 const generic 把 `N` 编进类型，降级为 `wgmma.wait_group.sync.aligned N`。

真正的 MMA 接收累加器引用 + 两个描述符：

[wgmma.rs:276-281](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-device/src/wgmma.rs#L276-L281) `wgmma_mma_m64n64k16_f32_bf16(acc: &mut [[f32;8];4], desc_a: u64, desc_b: u64)`——注意它**不返回值**，结果直接写回 `acc` 引用（异步指令无法用返回值）。文档注释的 PTX 片段显示它展开成 32 个累加器寄存器 + 2 个描述符 + 5 个立即数。

描述符构造器：

[wgmma.rs:177-192](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-device/src/wgmma.rs#L177-L192) `make_smem_desc(ptr)` 把共享内存指针编码成 64 位描述符：低位装基地址（右移 4）、中间装 leading dimension 与 stride、bit 62 是 128 字节 swizzle 开关，注释里给出了完整内联 PTX 草图。

累加器工具：

[wgmma.rs:319-329](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-device/src/wgmma.rs#L319-L329) `Acc64x64 = [[f32;8];4]` 与 `const zero_accumulator()` 提供 m64n64 的标准累加器类型与零初值。

#### 4.4.4 代码实践

**目标**：在不跑 GPU 的前提下，把 wgmma 的异步协议画成时序图，理解「下单-等待」解耦。

**步骤**：

1. 读 [wgmma.rs:38-59](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-device/src/wgmma.rs#L38-L59) 的用法示例（`fence → mma → commit → wait::<0>`）。
2. 读 [wgmma.rs:121-131](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-device/src/wgmma.rs#L121-L131) 的「两组 MMA」示例：先 `mma + commit`，再 `mma + commit`，最后只调一次 `wait_group::<0>()`。
3. 用纸笔画出时序：两组 MMA 都在 `wait` 之前就下单了，说明它们在硬件里可以并行/流水线执行。

**需要观察的现象**：`commit_group` 把多条 MMA 切成组，`wait_group::<N>` 一次等掉多组——这就是「下单与等待解耦」，是 Hopper GEMM 高吞吐的关键。

**预期结果**：你能解释为什么把 `wait_group::<0>` 提前到两次 `mma` 之间会损失性能（强行把异步变同步）。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `wgmma_mma_m64n64k16_f32_bf16` 用 `&mut acc` 而不是返回 `[f32; 32]`？

**答案**：因为它是异步指令——「下单」时结果还没算出来，没法用返回值。结果在 `wait_group` 完成后才真正写回 `acc` 引用的寄存器。这是异步 MMA 与同步 mma.sync 在签名上的本质区别。

**练习 2**：wgmma 的 A/B 操作数为什么不再是 `[u32; N]` 寄存器数组，而是 `u64` 描述符？

**答案**：因为 wgmma 的操作数在共享内存而非寄存器。`u64` 描述符告诉硬件「去共享内存的哪个地址、按什么 stride/swizzle 取」，硬件自己搬数据，软件不再需要把数据预排成 lane 级 fragment。

---

### 4.5 tcgen05：Blackwell 的单线程 MMA 与 Tensor Memory

#### 4.5.1 概念说明

Blackwell（sm_100a）的 tcgen05 把张量核推到极致：**一条 MMA 只需要 1 个线程下单**。这之所以可能，是因为操作数进一步集中到专用存储：

- **A 和累加器 D 放在 TMEM（Tensor Memory）**——一种每 SM 独有、按列分配的新存储，运行时动态分配（不像寄存器/共享内存那样静态划分）。
- **B 仍在共享内存**，用 64 位 SMEM 描述符描述。
- 操作的「形状、dtype、是否转置」等编码进一个 32 位**指令描述符（idesc）**。

由此带来全新的编程模型：先用一个 warp 跑 `tcgen05.alloc` 申请 TMEM 列、把 A 从共享内存 `cp` 进 TMEM、然后**单个线程**发 `tcgen05.mma` 并 `commit` 到一个 mbarrier、全体线程 `mbarrier_try_wait` 等完成、最后用 `tcgen05.ld` 把 D 从 TMEM 取回寄存器写回全局内存、收尾 `dealloc`。**TMEM 必须显式释放，否则触发 `CUDA_ERROR_TENSOR_MEMORY_LEAK`。**

#### 4.5.2 核心流程

tcgen05 完整 GEMM 单步流水线（参考示例 tcgen05_mma_minimal）：

```text
1. mbarrier_init(&MBAR, 1)                        // 建一个到达数=1 的 mbarrier
2. tcgen05_alloc(&TMEM_SLOT, 64)      [warp 0]    // 申请 64 列 TMEM，地址写回共享内存
   sync_threads()
3. tcgen05_cp_smem_to_tmem(tmem, a_desc) [tid 0]  // A: SMEM → TMEM
   sync_threads()
4. tcgen05_mma_ws_f16(d_tmem, a_tmem, a_desc, b_desc, idesc, enable_d=false)  [tid 0]
   tcgen05_fence_before_thread_sync()
   tcgen05_commit(&MBAR)                          // 把 MMA 完成信号绑到 mbarrier
5. mbarrier_try_wait(&MBAR, 0)         [全体]      // 等张量核算完
6. tcgen05_ld_16x256b_pure(d_tmem) -> regs [warp 0]  // D: TMEM → 寄存器（异步）
   tcgen05_load_wait()                            // 等 load 完成
7. sync_threads(); tcgen05_dealloc(tmem, 64)  [warp 0]  // 释放 TMEM
```

注意线程粒度随步骤切换：alloc/dealloc 是 warp 级（32 线程），MMA/commit 是单线程，wait 是全体。tcgen05.rs 顶部有一张「线程需求图」精确标注了每步需要的线程数。

#### 4.5.3 源码精读

线程粒度图与对比表：

[tcgen05.rs:32-41](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-device/src/tcgen05.rs#L32-L41) 用条形图标出：`mma_ws`/`commit`/`fence` 各只需 1 线程，`alloc`/`dealloc` 需 1 warp（32 线程）。

**TMEM 的 typestate 封装**——用类型状态在编译期防错：

[tcgen05.rs:195-199](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-device/src/tcgen05.rs#L195-L199) `TmemGuard<State, N_COLS>` 用 `TmemUninit`/`TmemReady`/`TmemDeallocated` 三个状态标记编码生命周期。

[tcgen05.rs:271-287](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-device/src/tcgen05.rs#L271-L287) `alloc_by(alloc_warp)` 实现「全体线程调、仅指定 warp 真申请」模式：内部 `if warp_id == alloc_warp { tcgen05_alloc(...) }` 后跟一个 `sync_threads()`，让所有线程都拿到 `TmemReady` 句柄。

[tcgen05.rs:341-344](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-device/src/tcgen05.rs#L341-L344) `dealloc()` 消费 `TmemReady` 返回 `TmemDeallocated`——后者没有任何方法，编译期就无法再用已释放的 TMEM。

底层的 alloc/dealloc 桩：

[tcgen05.rs:459-463](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-device/src/tcgen05.rs#L459-L463) `tcgen05_alloc(dst_smem, n_cols)` 降级为 `tcgen05.alloc.cta_group::1.sync.aligned.shared::cta.b32`；关键是**硬件把申请到的 TMEM 地址写回你给的共享内存指针**，之后所有线程从共享内存读这个地址才能用 TMEM。

[tcgen05.rs:504-508](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-device/src/tcgen05.rs#L504-L508) `tcgen05_dealloc(tmem_addr, n_cols)`，文档「CRITICAL」警告：所有 TMEM 必须在 kernel 退出前释放，否则报 tensor memory leak。

**指令描述符 idesc**——把形状/dtype 编码进 32 位：

[tcgen05.rs:709-730](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-device/src/tcgen05.rs#L709-L730) 给出 idesc 的位布局：bits 4-5 是 D 类型、7-9 是 A 类型、10-12 是 B 类型、13-14 是 A/B 取反、15-16 是 A/B 转置、17-22 是 `N>>3`、24-28 是 `M>>4`。

[tcgen05.rs:968-1031](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-device/src/tcgen05.rs#L968-L1031) `build()` 用位运算把所有字段拼成一个 `u32`，是纯 const fn，可在编译期完成。

**单线程 MMA 桩**：

[tcgen05.rs:1449-1460](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-device/src/tcgen05.rs#L1449-L1460) `tcgen05_mma_ws_f16(d_tmem, a_tmem, a_desc, b_desc, idesc, enable_d)` 降级为 `tcgen05.mma.ws.cta_group::1.kind::f16`，`enable_d=true` 表示 `D += A×B`、`false` 表示 `D = A×B`。

**TMEM 取回与等待**——异步 load，必须等：

[tcgen05.rs:1657-1661](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-device/src/tcgen05.rs#L1657-L1661) `tcgen05_ld_16x256b_x8_pure(tmem)` 把 TMEM 的 32 个 f32 装进 `CuSimd<f32,32>` 返回（warp 协作）。

[tcgen05.rs:1829-1832](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-device/src/tcgen05.rs#L1829-L1832) `tcgen05_load_wait()` 降级为 `tcgen05.wait::ld.sync.aligned`——`tcgen05.ld` 是异步的，不等就读到旧数据。

**完整示例**——`tcgen05_mma_minimal` 把上述九步串起来：

[tcgen05/main.rs:184-260](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/examples/tcgen05/src/main.rs#L184-L260) 是一个完整 kernel：init mbarrier → warp0 alloc → cp A 进 TMEM → tid0 构造 idesc 与 b_desc 发 `tcgen05_mma_ws_f16` → fence+commit → 全体 `mbarrier_try_wait` → warp0 `ld` + `load_wait` 读 D → warp0 dealloc。它演示了线程粒度的精确切换。

**cta_group::2（CTA pair）变体**——两个 CTA 协作更大的 tile：

[tcgen05.rs:2134-2144](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-device/src/tcgen05.rs#L2134-L2144) 注释说明 cta_group::2 把两个 CTA 放到相邻 SM（一个 TPC）协作更大的 tile（如 \(256\times 128\)），且**一个 kernel 内所有 tcgen05 指令必须用同一个 cta_group 值**，混用是 UB。

[tcgen05.rs:2288-2292](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-device/src/tcgen05.rs#L2288-L2292) `tcgen05_commit_multicast_cg2(mbar, cta_mask)` 在 cooperative MMA 完成时，用 `cta_mask` 一条指令通知集群里多个 CTA 的 mbarrier——这是 cta_group::2 完成同步的标准武器。

#### 4.5.4 代码实践

**目标**：阅读 `tcgen05_mma_minimal`，验证你理解的「线程粒度切换」与示例代码一致。

**步骤**：

1. 打开 [tcgen05/main.rs:165-266](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/examples/tcgen05/src/main.rs#L165-L266)。
2. 给每一步标注它用的线程粒度：`if warp_id == 0`（warp 级）、`if tid == 0`（单线程）、无 guard（全体）。
3. 特别注意第 207-230 行：构造 `b_desc`、`idesc` 与发 MMA、fence、commit **全部在 `if tid == 0` 内**——这印证了「单线程下单」。
4. 注意第 233 行 `mbarrier_try_wait` **没有任何 guard**——全体线程一起等。
5. 阅读 [tcgen05/main.rs:569-597](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/examples/tcgen05/src/main.rs#L569-L597) 的宿主侧：`shared_mem_bytes: 8192 + 256` 说明共享内存预算；输出 `[u32; 3]` 装回 tmem 地址与一个 D 采样值。
6. 在 Blackwell 卡上 `cargo oxide run tcgen05`；非 Blackwell 卡会走 [main.rs:449-484](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/examples/tcgen05/src/main.rs#L449-L484) 的 `verify_ptx_only` 退化路径，用 `ptxas -arch=sm_120a` 校验 PTX。

**需要观察的现象**：MMA 下单那几行被包在 `if tid == 0`；等待那行没有 guard；TMEM 地址通过共享内存中转（`*(&raw const TMEM_ADDR ...)`）。

**预期结果**：你能口头复述「为什么 alloc 用 warp、MMA 用单线程、wait 用全体」。无 Blackwell 硬件时，PTX 校验部分标注「待本地验证」。

#### 4.5.5 小练习与答案

**练习 1**：`tcgen05_alloc` 为什么把结果「写回共享内存」而不是用返回值？

**答案**：因为 alloc 是 warp 级指令（32 线程一起执行），但硬件只产生一个 TMEM 地址，需要让 warp 内/块内所有线程都看到。把地址写到一个共享内存槽，再让所有线程 `sync_threads` 后从该槽读取，是最简单的广播方式。详见 [tcgen05.rs:404-417](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-device/src/tcgen05.rs#L404-L417) 的图示。

**练习 2**：`tcgen05_ld_16x256b_x8_pure` 之后为什么必须调 `tcgen05_load_wait()`？

**答案**：`tcgen05.ld` 是异步的——它启动加载但不等完成。若立刻读返回的寄存器，可能读到旧值。`tcgen05_load_wait`（`tcgen05.wait::ld.sync.aligned`）是必须的完成屏障，[tcgen05.rs:1800-1818](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-device/src/tcgen05.rs#L1800-L1818) 明确警告「Without this wait, you may read stale data」。

**练习 3**：`TmemGuard` 的三个状态类型 `TmemUninit`/`TmemReady`/`TmemDeallocated` 各能调用哪些方法？

**答案**：`TmemUninit` 只能调 `alloc`/`alloc_by`；`TmemReady` 能调 `address`/`raw_address`/`n_cols`/`dealloc`/`dealloc_by`；`TmemDeallocated` 没有任何方法（[tcgen05.rs:392](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-device/src/tcgen05.rs#L392) 注释「has NO methods - can't use after dealloc」）。这样「释放后再用」与「未分配就用」都在编译期被拒。

---

## 5. 综合实践

把本讲三代加速器串起来，完成下面这张「选型决策 + fragment 手算」综合任务：

**任务**：假设你要为不同架构写一个 fp16 GEMM 的张量核主循环，针对三类硬件各写一段**伪代码**（不必真能编译），并解释每段的关键差异。

1. **Ampere（sm_80，用 `mma.sync`）**：
   - 用 `ldmatrix_x4_trans` 把共享内存的 A、B 加载成 fragment；
   - 循环里调 `mma_m16n8k16_f32_f16(acc, a_frag, b_frag)`，沿 K 维累加；
   - 写出 lane 0 在 D fragment（\(16\times 8\)）中持有的 4 个元素的坐标（用 4.3 的公式）。

2. **Hopper（sm_90a，用 `wgmma`）**：
   - 用 `make_smem_desc` 烘焙 A、B 描述符；
   - `wgmma_fence()` → 循环 `wgmma_mma_m64n64k16_f32_f16(&mut acc, desc_a, desc_b)` → `wgmma_commit_group()` → `wgmma_wait_group::<0>()`；
   - 解释：为什么这里不需要 `ldmatrix`？为什么 `acc` 用引用而 `mma.sync` 用返回值？

3. **Blackwell（sm_100a，用 `tcgen05`）**：
   - `tcgen05_alloc` 申请 TMEM → `tcgen05_cp_smem_to_tmem` 把 A 搬进 TMEM → 单线程 `tcgen05_mma_ws_f16` + `tcgen05_commit` → 全体 `mbarrier_try_wait` → `tcgen05_ld_16x256b_x8_pure` + `tcgen05_load_wait` 取回 D → `tcgen05_dealloc`；
   - 解释：为什么 MMA 可以只让 `tid == 0` 发，而 alloc/dealloc 必须 warp 级？

**验收标准**：

- lane 0 的 D 元素坐标为 \((0,0)、(0,1)、(8,0)、(8,1)\)（来自 4.3.4 步骤 1）。
- 能说清「wgmma 操作数在共享内存、异步故用引用」「tcgen05 操作数在 TMEM、单线程即可下单」。
- 能指出三代里只有 Ampere 版需要 `ldmatrix`，只有 Blackwell 版需要显式释放（TMEM）。

完成后，建议阅读 `crates/rustc-codegen-cuda/examples/gemm_sol/src/main.rs`（Blackwell tcgen05 的工业级 GEMM SoL，含 tiled/swizzled/pipelined/warp-spec/persistent 多个变体，均以 `tcgen05_mma_f16` 为核心，见其第 448/716/980 行等调用点），把本讲的「单步流水线」扩展为「真实 GEMM 主循环」。

## 6. 本讲小结

- cuda-oxide 把**三代**矩阵乘加速器封装成纯 Rust 桩函数：warp 级 `mma.sync`（`wmma.rs`，sm_80）、Hopper `wgmma`（`wgmma.rs`，sm_90a）、Blackwell `tcgen05`（`tcgen05.rs`，sm_100a）。
- 所有桩都是 `#[inline(never)] unsafe fn ... { unreachable!() }`，真实 PTX 由 `mir-importer`（拆数组为 SSA 寄存器、生成 dialect-nvvm op）+ `mir-lower`（降级为 convergent inline-asm `mma.sync.aligned ...`）注入。
- `mma.sync` 的操作数是 **warp 分布式寄存器 fragment**，布局由硬件规定；f16/bf16 是 \(16\times 8\times 16\)、tf32 是 \(16\times 8\times 8\)、f64 是 \(8\times 8\times 4\)、本轮新增的 s8 是 \(16\times 8\times 32\)（D fragment 对前四者都是 \(16\times 8\)，每 lane 4 元素）。
- `ldmatrix` 是把共享内存数据排成 fragment 的专用 warp 协作加载（x1/x2/x4 + `.trans`），是 mma.sync 的「喂料」搭档。
- wgmma 把操作数搬到**共享内存描述符**、协作单位升到 128 线程、变**异步**（`fence → mma_async → commit_group → wait_group`），故累加器用 `&mut` 而非返回值。
- tcgen05 进一步把 A/D 搬到**动态分配的 TMEM**，使 MMA 可由**单线程**下单，完成靠 mbarrier；TMEM 必须显式释放，生命周期用 `TmemGuard` 的 typestate 在编译期保护。

## 7. 下一步学习建议

- **横向读完三个设备模块的文档注释**：[wmma.rs](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-device/src/wmma.rs)、[wgmma.rs](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-device/src/wgmma.rs)、[tcgen05.rs](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-device/src/tcgen05.rs)——本讲只覆盖了主线路径，三个文件里还有 `movmatrix`、collector buffer、stmatrix 全家族、cta_group::2 等待你深挖。
- **追一遍编译侧的完整 lowering**：从 [dialect-nvvm/ops/wmma.rs](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/dialect-nvvm/src/ops/wmma.rs) → [mir-importer wmma.rs](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-importer/src/translator/terminator/intrinsics/wmma.rs) → [mir-lower wmma.rs](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-lower/src/convert/intrinsics/wmma.rs)，这是 u6-l2/u6-l3「新增一个 intrinsic全栈模板」的实战预演。
- **读工业级 GEMM**：`examples/gemm_sol/src/main.rs`（Blackwell tcgen05 多变体）与 `examples/gemm_sol_final`（size-specialized CLC + cg2 + vector stores），看真实 GEMM 如何把本讲的「单步」展开成分块、双缓冲、warp-specialized 的主循环。
- **补齐前置**：若对 mbarrier/cp.async/cluster 还不熟，先读 u5-l3（异步屏障与拷贝）与 u5-l4（集群与 DSMEM），tcgen05 的完成同步与 cta_group::2 都建立在其上。
