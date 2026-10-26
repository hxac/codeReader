# 目录结构导览：src、csrc、tools、cubin、examples、tests

> 所属单元：u1 初识 pegainfer-kernels ｜ 讲义编号：u1-l2 ｜ 前置讲义：u1-l1（定位与职责边界）
> 阅读姿势：本讲是「地图讲」，建议边读边在终端敲文中命令。所有行号基于仓库 HEAD 09a1a38f，仓库演进后请以 grep 实测为准。

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 pegainfer-kernels 根目录下每个文件与目录的职责，不再在一百多个 CUDA 源文件面前迷路。
2. 背出 src/lib.rs 的模块清单——默认构建下的 7 个常驻模块，外加 1 个 feature 门控模块，并能各用一句话说清职责。
3. 把 csrc/ 下每个子目录映射到 Cargo.toml 里的 cargo feature，理解「一条模型线 = 一个 feature = 一组 CUDA 源」的组织规律，以及 moe 这种被多条线共享的例外。
4. 说出 tools/、cubin/、third_party/ 各自存放什么构建期资产，谁在什么时候消费它们。
5. 按 dflash2、gemma4、glm52、k3、qwen4、hd256、hd512 等前缀读懂 tests/ 的分组规律，并能自己统计复现。

## 2. 前置知识

本讲不需要你写过 CUDA，只需要下表中的概念。学过 u1-l1 的读者可以快速扫过前四行。

| 术语 | 通俗解释 |
| --- | --- |
| crate 与 lib.rs | crate 是 Rust 的编译单元，好比一个库项目；src/lib.rs 是库 crate 的门面，声明对外的模块清单。 |
| mod 与 pub use | mod 引入一个子模块（对应同名 .rs 文件或同名目录）；pub use 把子模块里的条目抬到当前路径，让调用方少写层级。 |
| cargo feature | 编译期开关：Cargo.toml 声明 feature 名，源码里用 #[cfg(feature = "…")] 决定代码是否参与编译。u1-l1 讲过本 crate 的门控矩阵。 |
| .cu 与 .cuh | CUDA 的源文件与头文件（类比 .c 与 .h），由 nvcc 编译；本 crate 的 CUDA 代码全部在 csrc/ 下。 |
| build.rs | cargo 约定的构建脚本，在编译 crate 本体之前执行；本 crate 的 build.rs 有 2931 行，负责 nvcc 编译、AOT 生成与 cubin 嵌入（u1-l3、u1-l4 精读）。 |
| AOT 与 cubin | AOT（Ahead-Of-Time）指构建期提前生成或编译 kernel；cubin 是 GPU 可直接加载执行的二进制机器码文件。 |
| vendored 源码 | 把上游第三方项目源码直接复制进本仓库一起维护，避免外部依赖漂移；third_party/ 与 csrc/marlin/vllm_marlin/ 都是这种形态。 |
| 集成测试 | Rust 约定 tests/ 目录下每个 .rs 编译成独立测试二进制（集成测试）；写在 src 内 #[cfg(test)] 里的才是单元测试。本 crate 的 GPU 测试几乎都在 tests/ 下。 |

本讲反复用三个只读命令逛目录：

```bash
ls <目录>                 # 列目录内容
find <目录> -maxdepth 2   # 限深列出目录树
grep -n "模式" <文件>      # 带行号查找
```
## 3. 本讲源码地图

本讲的关键文件与目录如下（行数为编写讲义时在 HEAD 09a1a38f 实测）：

| 文件 | 行数 | 作用 |
| --- | --- | --- |
| src/lib.rs | 12 | crate 门面：声明全部顶层模块，短到可以整篇背下 |
| src/ops.rs | 318 | 算子包装层入口：cfg 门控 + 私有 mod + pub use 再导出 |
| src/ffi.rs | 50 | FFI 声明聚合入口：按模型线拆子模块并扁平再导出 |
| KERNELS.md | 246 | 算子索引路由表（u1-l1 介绍过 Scope，u1-l5 详讲用法） |
| Cargo.toml | 69 | feature 表与 example/test 的 required-features |
| build.rs | 2931 | 构建管线大脑（u1-l3、u1-l4 详讲，本讲只负责定位它） |

涉及的目录：src/、csrc/、tools/、cubin/、third_party/、examples/、tests/。

## 4. 核心概念与源码讲解

### 4.1 顶层鸟瞰：crate 根目录全景

#### 4.1.1 概念说明

u1-l1 把 pegainfer-kernels 定位成整个引擎的「算子地基」。本讲先建立空间感：这块地基由哪些房间组成。在 crate 根目录执行 ls，你会看到：

```
Cargo.toml   KERNELS.md   build.rs
csrc/        cubin/       examples/
src/         tests/       third_party/
tools/
```

（此外还有本学习手册所在的 openinfer-pegainfer-kernels-tutorial/ 目录，它不属于 crate。）

一张「三文件 + 七目录」速记表：

| 条目 | 类型 | 职责 |
| --- | --- | --- |
| Cargo.toml | 文件 | crate 名（L4）、feature 表、依赖、example/test 的 required-features |
| build.rs | 文件 | 2931 行构建脚本：nvcc 编译、Triton/TileLang AOT、cubin 嵌入 |
| KERNELS.md | 文件 | 246 行算子索引：op_id → Rust wrapper → FFI symbol → 源文件 |
| src/ | 目录 | Rust 本体：张量运行时、类型层、FFI 声明、算子包装（4.2 节） |
| csrc/ | 目录 | CUDA/C++ 侧：按模型线分组的 kernel 源（4.3 节） |
| tools/ | 目录 | 构建期代码生成器（4.4 节） |
| cubin/ | 目录 | 预编译 GPU 二进制资产（4.4 节） |
| third_party/ | 目录 | vendored 上游项目：DeepEP、DeepGEMM、FlashMLA、flash-kda、flashinfer（4.4 节） |
| examples/ | 目录 | 示例可执行文件，全部带 required-features（4.5 节） |
| tests/ | 目录 | 集成测试，按模型线前缀分组（4.5 节） |

#### 4.1.2 核心流程

目录之间的关系要分两条时间线看。

构建期（cargo build 触发 build.rs）：

```
Cargo.toml 的 feature 表 ──决定──► build.rs 的编译分支
                                       │
        ┌──────────────────────────────┼────────────────────────┐
        ▼                              ▼                        ▼
  nvcc 编译 csrc/**/*.cu        tools/ 生成器产出 AOT      cubin/*.cubin 嵌入符号
        │                              │                        │
        └──────────► 产出静态库并链接进 crate ◄──────────────────┘
                     （third_party/ 提供头文件与依赖库）
```

编译期（cargo 编译 Rust 本体）：

```
src/lib.rs
 ├── tensor.rs / typed_ops.rs / gpu_buffers.rs / forward_pass.rs / paged_kv.rs
 ├── ops.rs ──► ops/*.rs      算子包装（按 feature 门控）
 └── ffi.rs ──► ffi/*.rs      extern 声明（按 feature 门控）
                     │ unsafe 调用
                     ▼
         build.rs 链接进来的 CUDA 静态库符号
```

使用期：examples/ 与 tests/ 以外部使用者身份调用 crate，cargo 按 required-features 自动拉起所需 feature。

#### 4.1.3 源码精读

[Cargo.toml:L32-L50](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/Cargo.toml#L32-L50) 是整个目录结构的「总开关面板」，feature 定义部分如下（源文件中的注释行未抄录，请点链接看原文）：

```toml
[features]
default = []
tvm-ffi-triton-cubin = ["dep:tvm-ffi", "qwen35"]
deepseek-v2-lite = []
gemma4 = []
qwen35 = []
qwen4 = []
glm52 = ["moe"]
k3 = []
kimi-k2 = ["moe"]
moe = []
```

三个关键读法：

1. default = []——默认一个模型 feature 都不开。这是 u1-l1「默认构建零 Python」在目录层的体现：src/ops.rs 的模型专属模块被 cfg 关掉，csrc/ 对应目录的编译也被 build.rs 跳过。
2. glm52 与 kimi-k2 都写成 ["moe"]——moe 是共享底座，同时对应 csrc/marlin/ 与 csrc/deepep/ 两组目录。feature 与目录不是一对一，而是网状映射。
3. 表里没有 qwen3——Qwen3 主线复用本 crate 的常驻部分（csrc/shared/ 与 ops.rs 未门控模块），不需要专属 feature；正因如此，Qwen3 原生 DFlash2 检查点带来的 dflash2 家族也是常驻编译的。

[KERNELS.md:L3-L5](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/KERNELS.md#L3-L5) 用三行讲清边界与用法：Scope 行声明本 crate 拥有 CUDA/Triton 构建产物、FFI 声明、kernel ABI 张量助手、paged-KV 布局元数据与 Rust 算子包装层，而 KvPool、PagePool、SamplingParams 等运行时策略对象不在其中；随后一行说明用法——把本文件当入口，从 op_id 跳到 Rust wrapper、FFI symbol 与源文件。表格正文按模型线分节（L7 Shared、L17 Qwen3-4B、L39 Kimi-K2、L74 GLM5.2、L87 起为 Kimi-K3 各段），u1-l5 再展开。

另外留意 [Cargo.toml:L52-L66](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/Cargo.toml#L52-L66)：三个 [[example]] 与一个 [[test]] 在这里声明 required-features，这是 examples/、tests/ 与 feature 表之间的合同，4.5 节会用到。

#### 4.1.4 代码实践

实践目标：亲手核对根目录清单与 feature 表。

操作步骤：

```bash
cd pegainfer-kernels
ls
grep -n "= \[" Cargo.toml
```

需要观察的现象：

- ls 的输出应与 4.1.1 一致（外加学习手册目录）。
- grep 的输出应包含 L32-L50 的 feature 行和 L52-L66 的四条 required-features 行。

预期结果：逐行对上讲义表格。仓库演进后行号可能平移，以 grep 实测为准。

#### 4.1.5 小练习与答案

练习 1：为什么 Cargo.toml 的 default 是空数组，仓库默认构建却仍能跑 Qwen3-4B？

答案：pegainfer-kernels 默认不启用任何模型专属 feature；Qwen3 主线只用本 crate 的常驻部分（csrc/shared 与未门控的 Rust 模块）。「默认带 qwen3」是上层模型 crate 与 workspace 根层面的事。

练习 2：build.rs 的文件名和位置能改吗？

答案：不能。build.rs 必须位于包根目录且名字固定，这是 cargo 的硬约定，cargo 会在编译 crate 本体之前先编译并执行它；能自由改动的只是这 2931 行的内容。
### 4.2 src/：Rust 侧的模块树

#### 4.2.1 概念说明

src/ 是 crate 的 Rust 本体。它的顶层小得惊人——lib.rs 只有 12 行，却挂出了整个 Rust 侧世界的入口。src 下共 9 个顶层 .rs 文件与 2 个子目录（ops/ 与 ffi/）：

```
src/
├── lib.rs             # 门面：12 行
├── tensor.rs          # 设备上下文与张量
├── typed_ops.rs       # 常量泛型类型化算子
├── gpu_buffers.rs     # gpu_buffers! 宏
├── forward_pass.rs    # typed_pipeline! 前向 DSL
├── paged_kv.rs        # KV 池布局元数据
├── triton_cubin.rs    # TVM FFI cubin 桥（feature 门控）
├── ops.rs ──► ops/    # 算子包装层：23 个模块
└── ffi.rs ──► ffi/    # extern 声明层：12 个模块
```

ops/ 与 ffi/ 内部再按「共享 + 模型线」拆文件，并与 csrc/ 的目录遥相呼应——这是本 crate 最重要的空间韵律：Rust 包装层、FFI 声明层、CUDA 实现层用的是同一套模型线命名。

#### 4.2.2 核心流程

一个算子从 Rust 调用到达 GPU 的路径：

```
模型 crate 调 pegainfer_kernels::ops::某算子
        │（ops.rs 的 pub use 把子模块函数抬到 ops:: 路径）
        ▼
ops/<线>.rs 包装函数：取 device_ptr、选 active stream
        │ unsafe extern 调用
        ▼
ffi.rs 聚合的 ffi/<线>.rs 里的 extern "C" 声明
        ▼
build.rs 链接进来的 csrc/<线>/*.cu 符号
```

本讲只需记住这条链的「地址簿」：ops 管 Rust 包装、ffi 管声明、csrc 管实现，三层按模型线同名对齐。u2 单元精读 tensor 与 typed_ops，u3 单元精读 ops 与 csrc 的配对。

#### 4.2.3 源码精读

lib.rs 全文 12 行（[src/lib.rs:L1-L12](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/lib.rs#L1-L12)）：

```rust
#![allow(incomplete_features)]
#![feature(generic_const_exprs)]

pub mod ffi;
pub mod forward_pass;
pub mod gpu_buffers;
pub mod ops;
pub mod paged_kv;
pub mod tensor;
#[cfg(feature = "tvm-ffi-triton-cubin")]
pub mod triton_cubin;
pub mod typed_ops;
```

L1-L2 打开 generic_const_exprs 不稳定特性，它是 typed_ops.rs 用 const generics 在编译期锁定矩阵维度的前提。L4-L12 是全部模块声明：默认构建（default = []）下共 7 个常驻模块，第 8 个 triton_cubin 仅在 tvm-ffi-triton-cubin feature 下编译。

一句话职责表：

| 模块 | 职责（详讲位置） |
| --- | --- |
| tensor.rs | DeviceContext/CudaStream 与 GpuTensor/DeviceVec/HiddenStates，一切 GPU 内存的持有者（u2-l1） |
| typed_ops.rs | 把矩阵维度写进类型的 GEMM 等类型化算子（u2-l2） |
| gpu_buffers.rs | gpu_buffers! 宏：声明随 batch 伸缩的预分配缓冲（u2-l2） |
| forward_pass.rs | typed_pipeline! 前向声明 DSL（u2-l4） |
| paged_kv.rs | KvStorage/KvFormat，KV 池页内布局的唯一真源（u3-l3） |
| ops.rs | 算子包装层入口：门控并再导出 ops/ 全部子模块 |
| ffi.rs | extern 声明聚合入口，含共享的 Half 类型 |
| triton_cubin.rs | TVM FFI 启动 Triton cubin 的桥（u5-l3） |

[src/ops.rs:L3-L49](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ops.rs#L3-L49) 是算子包装层的「户口本」：318 行里的前 49 行集中了全部 23 个模块声明，按门控分三类（行号为 09a1a38f 实测）。

常驻共享模块（无 cfg）：

| 声明行 | 模块 | 职责 |
| --- | --- | --- |
| L3 | attention | 共享 paged prefill 与 batch decode 注意力包装（u3-l4） |
| L22 | elementwise | gelu、softcap 等逐元素算子（u3-l5） |
| L23 | embedding | token embedding（u3-l5） |
| L40 | linear | 线性层与 GEMM 家族包装（u3-l2） |
| L41 | lora | LoRA 融合 kernel 包装，常驻编译，对应 csrc/lora_fused.cu |
| L44 | norm | RMSNorm 家族包装（u3-l6） |
| L49 | sampling | 采样与 argmax 包装（u3-l5） |

常驻 Qwen3 家族（同样无 cfg——Qwen3 是默认主线，不需要 feature）：

| 声明行 | 模块 | 职责 |
| --- | --- | --- |
| L8 | dflash2 | Qwen3 原生 DFlash2 检查点的算子面（u5-l5） |
| L9 | dflash2_conv | DFlash2 分组卷积包装（u5-l5） |

feature 门控模块（每个 mod 的上一行是 #[cfg(...)]）：

| 声明行 | 模块 | 门控（cfg 所在行） |
| --- | --- | --- |
| L5 | deepep | moe（L4） |
| L7 | deepseek_v2_lite | deepseek-v2-lite（L6） |
| L11 | dsv2_routed_moe | deepseek-v2-lite（L10；其 L13-L21 的 pub use 也逐条门控） |
| L25 / L27 / L29 / L31 | gemma4 / gemma4_attention / gemma4_tilelang / gemma4_w4a16 | gemma4（L24 / L26 / L28 / L30） |
| L33 | glm52 | glm52（L32） |
| L35 / L37 | k3 / k3_tilelang | k3（L34 / L36） |
| L39 | kimi_k2 | kimi-k2（L38） |
| L43 | marlin_face | any(gemma4, kimi-k2)（L42）——Marlin 量化 GEMM 的 Rust 面，被两条线复用 |
| L46 | qwen35 | qwen35（L45） |
| L48 | qwen4 | qwen4（L47） |

[src/ffi.rs:L1-L5](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ffi.rs#L1-L5) 的开头把组织原则写成了注释：

```rust
// FFI surface for CUDA/cuBLAS/FlashInfer kernels, split by owning model.
// Public paths are unchanged: `pegainfer_kernels::ffi::<symbol>` resolves via the re-exports below.

// Half type (16-bit float) - same layout as CUDA half. Shared ABI type used by all submodules.
pub type Half = u16;
```

「按 owning model 拆文件、公共路径保持扁平」。[src/ffi.rs:L7-L28](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ffi.rs#L7-L28) 声明 12 个私有子模块（deepep、deepseek_v2_lite、gemma4、gemma4_tilelang、glm52、k3、k3_tilelang、kimi、lora、qwen35、qwen4、shared；其中 lora 与 shared 常驻，其余按模型线 feature 门控，与 ops.rs 的门控一一对应），[src/ffi.rs:L30-L50](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ffi.rs#L30-L50) 再用 pub use …::* 把符号全部拍平到 ffi:: 根路径——调用方永远写 pegainfer_kernels::ffi::<symbol>，不感知文件拆分。

ops/ 与 ffi/ 下还有把大模型线继续拆细的子目录（Rust 2018 的「同名 .rs + 同名目录」与目录模块两种形态）：

| 子目录 | 组织形态 | 文件数 | 内容 |
| --- | --- | --- | --- |
| src/ops/glm52/ | glm52.rs + glm52/ | 17 | indexer、router、topk、fp8_gemm、flashmla_sparse、moe_gemv、mla_assembly 等 |
| src/ops/k3/ | k3/mod.rs | 8（含 mod.rs） | flash_kda、mega_moe、moe_chain、mla_paged、router_topk、deepgemm 等 |
| src/ops/kimi_k2/ | kimi_k2.rs + kimi_k2/ | 6 | experts、mla、router、shared_gate_up、o_proj 等 |
| src/ffi/glm52/ | glm52.rs + glm52/ | 目录模块 | glm52 线 extern 声明的进一步拆分 |

#### 4.2.4 代码实践

实践目标：自己产出一份「模块 ↔ 门控」对照表，并抓住两个反直觉点。

操作步骤：

```bash
cd pegainfer-kernels
grep -n "^mod\|^pub mod" src/ops.rs
grep -n "cfg" src/ops.rs
grep -n "^mod\|^pub use\|^pub type" src/ffi.rs
```

需要观察的现象：

- ops.rs 的 mod 行与 cfg 行交错出现，把每个 cfg 与紧随其后的 mod 配对。
- 每个门控里的 feature 名都应能在 Cargo.toml 的 feature 表（L32-L50）里找到。

预期结果（09a1a38f 实测）：

- 门控覆盖 moe、deepseek-v2-lite（含 dsv2_routed_moe 及其再导出）、gemma4×4、glm52、k3×2、kimi-k2、qwen35、qwen4，以及 any(gemma4, kimi-k2) 的 marlin_face。
- 反直觉点 1：lora（L41）与 dflash2 家族（L8-L9）完全没有 cfg——LoRA 与 Qwen3 原生 DFlash2 属于常驻编译面。
- 反直觉点 2：门控并不都挂在单一 feature 上，marlin_face 用的是 any(gemma4, kimi-k2)（L42）。

#### 4.2.5 小练习与答案

练习 1：ops.rs 里的 mod 都是私有的，外部为什么能用 pegainfer_kernels::ops::xxx？

答案：模块私有 + pub use 再导出是刻意设计——ops:: 对外只暴露精选路径，内部文件结构可以自由重组而不破坏公共 API。ffi.rs 第 2 行注释把同一原则说得更直白。

练习 2：cargo build --features k3 时，ops 侧会多编译哪些模块？

答案：k3 与 k3_tilelang（L34-L37）。注意 k3 不拉起 moe：deepep 不会编译，marlin_face 的 any 门控不含 k3 也不会编译。换成 --features glm52 则经 glm52 = ["moe"] 连带点亮 moe 底座（deepep 会编译），marlin_face 仍要等 gemma4 或 kimi-k2 之一开启。

练习 3：ffi.rs 为什么在根部定义 Half = u16，而不是各子模块各写各的？

答案：Half 是所有子模块共享的 ABI 类型，定义一次再被各 extern 声明引用可避免漂移；注释明确它只承诺「与 CUDA half 同为 16 bit」的位级布局。
### 4.3 csrc/：按模型线分组的 CUDA 源

#### 4.3.1 概念说明

csrc/ 是 CUDA/C++ 世界，体量远大于 Rust 侧：不计 csrc/marlin/vllm_marlin/ 与 csrc/glm52/trtllm_gen/ 这两个 vendored/生成子树，共有 104 个 .cu/.cuh/.c 源文件（09a1a38f 实测），分布在 11 个子目录加 2 个根级文件里。组织规则：

- csrc/common.cuh：全局公共头，kernel 侧公共设施（u3-l6、u4-l1 精读）。
- csrc/shared/：所有模型共用的「标准件」kernel——norm、elementwise、attention、sampling、FFI 错误防护等 20 个文件。
- csrc/<模型线>/：一条模型线一个目录，文件名普遍带模型线前缀（kimi_mla.cu、glm52_topk.cu、k3_flash_kda.cu）。
- 两个跨线目录：csrc/marlin/（量化 GEMM 基座，被 gemma4 与 kimi-k2 复用）与 csrc/deepep/（EP 通信 shim，moe 底座）。
- csrc/lora_fused.cu：根级常驻的 LoRA 融合 kernel。

#### 4.3.2 核心流程

给一个新 kernel 文件「找家」的心智清单：

```
1. 所有模型都能用？     → csrc/shared/xxx.cu（常驻编译）
2. 只属于某条模型线？   → csrc/<line>/<line>_xxx.cu，并在 build.rs 登记
3. 是量化 MoE 基座？    → csrc/marlin/（复用，勿重复造）
4. 是 EP 通信？         → csrc/deepep/（moe 底座）
5. Rust 侧配对：        → src/ffi/<line>.rs 声明 + src/ops/<line>.rs 包装
6. 登记：              → KERNELS.md 加表行（Editing Rule，见 u1-l5 与 u4-l2）
```

命名后缀的「暗号」：hd256 / hd512 表示 head_dim 特化的注意力 kernel；sm100、sm89 表示目标 GPU 架构（Blackwell、Ada）；wide224 / wide896 表示 tile 宽度变体；ep4 / ep16 / ep32 / ep64 表示 EP 宽度。

#### 4.3.3 源码精读

目录 ↔ feature ↔ 代表文件对照表（文件数为 09a1a38f 实测；每行链接一个代表文件，其余为同目录文件名）：

| csrc 条目 | feature | 文件数 | 代表文件 |
| --- | --- | --- | --- |
| common.cuh（根级） | 常驻 | 1 | [csrc/common.cuh](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/csrc/common.cuh) kernel 公共设施头 |
| lora_fused.cu（根级） | 常驻 | 1 | [csrc/lora_fused.cu](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/csrc/lora_fused.cu) LoRA 融合 kernel |
| shared/ | 常驻 | 20 | [norm.cu](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/csrc/shared/norm.cu)、elementwise.cu、prefill_attention.cu、paged_attention.cu、flashinfer_sampling.cu、ffi_guard.cuh、ffi_last_error.cu、lt_algo_store.cu、dflash2_selector.cu 等 |
| qwen35/ | qwen35 | 5 | [gated_delta_rule.cu](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/csrc/qwen35/gated_delta_rule.cu)、prefill_attention_hd256.cu、paged_attention_hd256.cu、paged_attention_split_hd256.cu、conv1d.cu |
| qwen4/ | qwen4 | 2 | [qwen4_qsa_indexer_score.cu](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/csrc/qwen4/qwen4_qsa_indexer_score.cu)、qwen4_grouped_rmsnorm.cu |
| gemma4/ | gemma4 | 9 | [paged_attention_window_hd256.cu](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/csrc/gemma4/paged_attention_window_hd256.cu)、paged_attention_hd512.cu、prefill_attention_hd256_plain.cu、w4a16_layout.cu、marlin_nvfp4.cu、local_kv_fp8.cu 等 |
| marlin/ | moe | 4 + vendored | [repack_4bit.cu](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/csrc/marlin/repack_4bit.cu)、moe_align.cu、ffi.cuh、moe_wna16_launch.cuh + vllm_marlin/ |
| deepep/ | moe | 20 | [deepep_shim.cu](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/csrc/deepep/deepep_shim.cu)、deepep.h、deepep_shim_impl.cuh + glm52 的 ep4/ep16/ep32/ep64 config 与 shim 实例 |
| deepseek_v2_lite/ | deepseek-v2-lite | 2 | [dsv2_lite_moe.cu](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/csrc/deepseek_v2_lite/dsv2_lite_moe.cu)、dsv2_lite_attention.cu |
| kimi_k2/ | kimi-k2 | 9 | [kimi_mla.cu](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/csrc/kimi_k2/kimi_mla.cu)、kimi_marlin_int4.cu、kimi_router.cu、kimi_top1.cu、kimi_shared_gate_up.cu 等 |
| glm52/ | glm52（→ moe） | 18 + trtllm_gen/ | [glm52_flashmla_sparse.cu](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/csrc/glm52/glm52_flashmla_sparse.cu)、glm52_indexer.cu、glm52_topk.cu、glm52_fp8_gemm.cu、glm52_router.cu 等 |
| k3/ | k3 | 13 | [k3_mega_moe_sm100.cu](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/csrc/k3/k3_mega_moe_sm100.cu)、k3_flash_kda.cu、k3_moe_chain.cu、k3_mla_paged_attn.cu、k3_router_topk.cu 等 |

三个值得停下来想的观察：

1. feature 与目录是多对多。moe 同时对应 csrc/marlin/ 与 csrc/deepep/ 两组目录；开 glm52 或 kimi-k2 都会经由 ["moe"] 点亮它们（[Cargo.toml:L45](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/Cargo.toml#L45) 与 [Cargo.toml:L49](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/Cargo.toml#L49)）。所以「按 feature 找目录」时要多想一层依赖展开。
2. shared/ 里混居着四类东西：FFI 错误契约（ffi_guard.cuh、ffi_last_error.cu）、FlashInfer 委托（flashinfer_norm.cu、flashinfer_sampling.cu、flashinfer_top1.cu）、自研 kernel（norm.cu、elementwise.cu、argmax.cu、fused_proj.cu、logprob_topk.cu）与辅助头（paged_launch.cuh、qk_prep.cuh、flashinfer_radix_scratch.cuh）。u3、u4 单元会逐族精读。
3. deepep/ 里 4 组 deepep_config_glm52_ep{4,16,32,64} 与配套 shim 文件是「同一 kernel 面 × 不同 EP 宽度」的预生成展开——记住这个模式，u8-l1 会解释为什么要按 EP 宽度预生成。

#### 4.3.4 代码实践

实践目标：统计每个 csrc 子目录的文件数，验证上表。

操作步骤：

```bash
cd pegainfer-kernels
ls csrc
ls csrc/shared | wc -l
ls csrc/qwen35 | wc -l
ls csrc/qwen4 | wc -l
ls csrc/gemma4 | wc -l
ls csrc/marlin | wc -l
ls csrc/deepep | wc -l
ls csrc/deepseek_v2_lite | wc -l
ls csrc/kimi_k2 | wc -l
ls csrc/glm52 | wc -l
ls csrc/k3 | wc -l
```

需要观察的现象：每个目录的计数（marlin 与 glm52 会把 vllm_marlin/、trtllm_gen/ 子目录各计为 1 项）。

预期结果（09a1a38f 实测）：shared=20、qwen35=5、qwen4=2、gemma4=9、marlin=5（4 个文件 + vllm_marlin）、deepep=20、deepseek_v2_lite=2、kimi_k2=9、glm52=19（18 个文件 + trtllm_gen）、k3=13；加根级 2 个文件，共 104 个源文件与 2 个子树。

#### 4.3.5 小练习与答案

练习 1：接入新模型线 foo 时，csrc 侧要动哪些位置？

答案：新建 csrc/foo/（或判断复用 marlin/deepep 底座）；Cargo.toml 声明 feature（MoE 线记得写成 ["moe"]）；build.rs 登记编译；配对 src/ffi/foo.rs 与 src/ops/foo.rs；KERNELS.md 加表段。完整清单见 u9-l3。

练习 2：为什么 marlin_face 的 Rust 门控是 any(gemma4, kimi-k2) 而不是 moe？

答案：Marlin 量化 GEMM 的 Rust 包装只被 gemma4（NVFP4 路径）与 kimi-k2（WnA16/INT4 experts）使用；开 glm52 虽会拉起 moe（CUDA 侧 EP 底座），但用不到 marlin_face 的 Rust 面。门控粒度跟着「谁用谁编」走。

练习 3：只看文件名 paged_attention_window_hd256.cu，能推断出什么？

答案：paged KV 注意力；window 表示滑窗 family；hd256 表示 head_dim=256 的特化；它位于 csrc/gemma4/ 所以属于 gemma4 线——qwen35/ 下也有 hd256 命名的文件，目录前缀才是归属的最终判据。
### 4.4 tools/、cubin/ 与 third_party/：构建期资产

#### 4.4.1 概念说明

这三个目录都不参与 cargo 对 Rust 本体的常规编译，而是被 build.rs 在构建期消费：

- tools/ 放「生成器」：用 Python DSL 表达 kernel，构建期编译或导出成 CUDA 源与二进制。
- cubin/ 放「预编译好的 GPU 二进制」：构建期直接嵌入为可链接符号。
- third_party/ 放「vendored 上游」：DeepEP、DeepGEMM、FlashMLA、flash-kda、flashinfer 五个项目，为 nvcc 提供头文件与库。

三者合起来支撑 u1-l1 讲过的构建哲学：默认构建零 Python（默认 feature 不触发任何生成器），而 qwen35、glm52 等线的重资产 kernel 通过 AOT 生成或预编译 cubin 进入构建。

#### 4.4.2 核心流程

```
tools/triton/gen_triton_aot.py                     ──(qwen35)──► Triton AOT 产物
tools/triton/gated_delta_rule_chunkwise_kernels.py ──► kernel 的 Python 源
tools/tilelang/ 生成器（见其 README）              ──(TileLang 线)──► 生成的 CUDA 源
tools/cutedsl/export_glm52_fp8_dsl.py             ──(glm52)──► FP8 DSL kernel
cubin/glm52/*.cubin（7 个）                        ──(build.rs 嵌入)──► 可链接符号
third_party/*                                      ──► 头文件搜索路径与链接库
```

注意 tools/tilelang/ 目前只看到 README.md 一个文件——TileLang 生成器的具体落位以该 README 与 build.rs 为准（u1-l4 展开，此处待确认）。

#### 4.4.3 源码精读

tools/ 四个子目录：

| 子目录 | 文件 | 消费方（详讲位置） |
| --- | --- | --- |
| tools/triton/ | gen_triton_aot.py、gated_delta_rule_chunkwise_kernels.py、README.md | qwen35 的 Triton AOT 管线（u5-l2） |
| tools/tilelang/ | README.md | TileLang 生成器说明（u1-l4、u6-l2、u8-l4） |
| tools/cutedsl/ | export_glm52_fp8_dsl.py | glm52 的 FP8 DSL 导出（u8-l3） |
| tools/kimi_k2/ | torch_reference.py、vllm_marlin_wna16_reference.py、hf_logits_reference.py、vllm_logits_reference.py、compare_logits_fixture.py、compare_vllm_topk_fixture.py、kimi_k25_parity_prompts.json 等 | 离线生成对拍参考数据（u7-l2、u9-l2） |

[tools/triton/README.md:L1-L3](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/tools/triton/README.md#L1-L3) 自述：pegainfer uses Triton AOT for the Qwen3.5 GDR chunkwise prefill kernels，且整条管线挂在 pegainfer-kernels 的 qwen35 feature 之后。[tools/tilelang/README.md](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/tools/tilelang/README.md) 第一句则说明该目录持有 pegainfer-kernels 使用的 TileLang 系 CUDA 源生成器。

cubin/ 目前只有 glm52 一个子目录：7 个 .cubin 加 README（09a1a38f 实测）。[cubin/glm52/README.md:L1-L3](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/cubin/glm52/README.md#L1-L3) 自述：这 7 个 SM100 家族 cubin 构成 GLM5.2（TP4 拓扑）的最小 selector 闭包。文件名本身就是一张形状参数表（下例为排版折行，实际是一整行文件名）：

```
fmhaSm100fKernel_QkvE4m3OBfloat16HQk576HV512HVPerCta256
_PagedKvDenseStaticTokenSparseP1MultiCtasKvVarSeqQ16Kv128
_StaticSwapsAbForGen.cubin
```

可读出：Sm100（Blackwell 架构）、Q 输入 E4m3、输出 B 为 float16、Qk576（Q/K 头维 576，MLA 隐维）、HV512（V 头维 512）、HVPerCta256、PagedKv、TokenSparse P1、Q16 与 Kv128 的 tile 形状、ForGen（TRT-LLM 生成版）。7 个文件只在少数字段上互不相同。

third_party/ 五个项目与 CLAUDE.md 架构图的三方支撑对应：flashinfer（采样、注意力、norm 库）、DeepEP（EP 通信）、DeepGEMM（FP8 分组 GEMM）、FlashMLA（MLA kernel）、flash-kda（KDA 注意力）。

#### 4.4.4 代码实践

实践目标：解码 cubin 文件名，产出 7 行差异表。

操作步骤：

```bash
cd pegainfer-kernels
ls cubin/glm52
head -40 cubin/glm52/README.md
```

需要观察的现象：7 个文件名只在 HVPerCta（128/256）、Q tile（Q8/Q16）、是否带 HVPerCta 字段、MultiCtasKv 与 Persistent 调度形态等少数字段上互不相同。

预期结果：整理出 7 行 × 关键差异列的小表；再对照 [KERNELS.md:L74](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/KERNELS.md#L74) 起的 GLM5.2 段，核对这些 cubin 支撑的 op 条目（若索引未逐条登记，以 README 文字为准）。

#### 4.4.5 小练习与答案

练习 1：为什么这些 cubin 选择 checked-in 而不是构建期生成？

答案：它们来自 TRT-LLM 的生成器（ForGen），生成环境依赖重、复现风险大；check-in 换来可复现与零 Python 构建。代价是换 SM 架构时必须重新生成并替换（u1-l4 讲 build.rs 里的 cubin 嵌入表）。

练习 2：tools/kimi_k2/ 的 Python 脚本会在 cargo build 时执行吗？

答案：不会。它们是离线参考数据生成器（生成 logits 与 marlin 对拍 fixture），不接入 build.rs；构建期会执行的只有 qwen35 门控下的 Triton AOT 与 TileLang 相关生成器。

练习 3：third_party/flashinfer 与 csrc/shared/flashinfer_sampling.cu 是什么关系？

答案：前者是 vendored 的 FlashInfer 库本体；后者是本 crate 侧调用 FlashInfer 采样接口的 CUDA 入口——「自研 kernel 与委托库混居」的一个例子（u3-l5 展开）。
### 4.5 examples/ 与 tests/：可执行入口与测试门禁

#### 4.5.1 概念说明

- examples/ 下每个 .rs 是一个可用 cargo run --example 运行的二进制；本 crate 只有 3 个，全部通过 required-features 与 feature 绑定。
- tests/ 下每个 .rs 编译成独立的集成测试二进制；文件名前缀告诉你它属于哪条模型线或哪个 kernel 面，后缀告诉你它是什么类型的门禁（smoke、parity、trap、gate 等）。

#### 4.5.2 核心流程

examples 与 feature 的绑定写在 [Cargo.toml:L52-L66](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/Cargo.toml#L52-L66)：

| example | required-features | 声明行 | 用途（详讲位置） |
| --- | --- | --- | --- |
| triton_cubin_tvm_ffi.rs | tvm-ffi-triton-cubin | L52-L54 | TVM FFI 启动 Triton cubin（u5-l3） |
| hd512_prefill_bench.rs | gemma4 | L56-L58 | hd512 全局注意力 prefill 基准（u6-l2） |
| hd512_decode_bench.rs | gemma4 | L60-L62 | hd512 decode 基准（u6-l2、u9-l2） |

运行形态（需要 GPU 与相应构建环境，待本地验证）：

```bash
cargo run --release -p pegainfer-kernels --example hd512_decode_bench --features gemma4
```

tests/ 的前缀分组（37 个 .rs，另有 common/mod.rs 与 qwen4_qsa_trap/mod.rs 两个共享模块；09a1a38f 实测）：

| 前缀 | 数量 | 覆盖内容 |
| --- | --- | --- |
| gemma4_* | 6 | 滑窗/全局注意力的 parity 系列 + w4a16_gemm |
| glm52_* | 6 | indexer、router、sparse、vocab、weights 的 smoke + fp8_dsl_gate |
| hd256_* | 4 | hd256 kernel 面：fp8 池、decode csr trap、window prefill reject、qk rope smoke |
| k3_* | 4 | flash_kda 的 bench 与 md_equiv、mega parity gate、moe_chain gate |
| dflash2_* | 3 | conv、selector、attention |
| qwen4_* | 3 | qsa indexer 的 score parity 与 rows、visible 两个 trap |
| hd512_* | 2 | qk_rope 的 smoke 与 trap_page |
| kimi_* | 1 | router smoke |
| 无模型前缀 | 8 | batch_invariance_gemm、ffi_exception_catch、fused_projection_bands、gelu_scale_smoke、grouped_rmsnorm_equivalence、lt_algo_store、prefill_plan_refill、softcap_smoke |

按后缀看类型学（u9-l1 详讲）：smoke（冒烟）10 个、parity（对拍）6 个、trap/reject（负向非法输入）5 个、gate（门禁）3 个、bench 1 个、equivalence 2 个、其余 10 个。

两个细节：

1. tests/grouped_rmsnorm_equivalence.rs 是唯一在 Cargo.toml 显式声明 [[test]] required-features 的（[Cargo.toml:L64-L66](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/Cargo.toml#L64-L66)）——不开 qwen4 feature 时 cargo 直接跳过编译它。
2. tests/common/mod.rs 是公共测试设施，tests/qwen4_qsa_trap/mod.rs 是 trap 用例的共享模块——cargo 只把 tests/ 顶层的 .rs 文件自动当作测试目标，子目录不会被自动发现，只作为模块被引用。

#### 4.5.3 源码精读

[Cargo.toml:L52-L66](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/Cargo.toml#L52-L66) 的四个 target 声明（节选，注释以链接原文为准）：

```toml
[[example]]
name = "triton_cubin_tvm_ffi"
required-features = ["tvm-ffi-triton-cubin"]

[[example]]
name = "hd512_prefill_bench"
required-features = ["gemma4"]

[[example]]
name = "hd512_decode_bench"
required-features = ["gemma4"]

[[test]]
name = "grouped_rmsnorm_equivalence"
required-features = ["qwen4"]
```

tests 文件名的「前缀 + 后缀」读法举两例：

- gemma4_local_decode_parity.rs = gemma4 线 + local（滑窗 family）+ decode 阶段 + parity（与参考实现对拍）。
- qwen4_qsa_indexer_visible_trap.rs = qwen4 线 + QSA indexer + 可见性约束 + trap（喂非法输入、断言被拒绝）。

#### 4.5.4 代码实践

实践目标：复现前缀统计表。

操作步骤：

```bash
cd pegainfer-kernels
ls tests
ls tests/*.rs | sed "s:tests/::; s:_.*::" | sort | uniq -c | sort -rn
```

需要观察的现象：各前缀计数与总数。

预期结果：gemma4 6、glm52 6、hd256 4、k3 4、dflash2 3、qwen4 3、hd512 2、kimi 1，其余 8 个无模型前缀；合计 37。

说明：hd256_ / hd512_ 前缀按「kernel 面」（head_dim 特化）命名而不是按模型线——目前主要覆盖 gemma4 的滑窗与全局面；qwen35 线的测试不使用这一前缀。读表时不要把前缀硬套成模型线。

#### 4.5.5 小练习与答案

练习 1：为什么 hd512 两个 bench 挂 gemma4，而 triton_cubin_tvm_ffi 挂 tvm-ffi-triton-cubin？

答案：required-features 是「编译此 target 前必须开启的 feature」。hd512 基准只测 gemma4 的全局注意力路径；TVM FFI 示例依赖 tvm-ffi 这个可选依赖与 qwen35 的 AOT 产物，feature 定义（[Cargo.toml:L33](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/Cargo.toml#L33)）把两者显式绑在一起。

练习 2：不带任何 feature 执行 cargo test --release -p pegainfer-kernels 会编译哪些测试？

答案：除 grouped_rmsnorm_equivalence（被 required-features 挡住）外的 36 个集成测试二进制都会编译；但它们大多需要 GPU 才能通过，无 GPU 环境下预期失败——具体行为待本地验证。单元测试用 --lib 过滤。

练习 3：想给 kimi-k2 线的新 kernel 加一个冒烟测试，文件名怎么起？

答案：仿照 kimi_router_smoke.rs 的「模型线前缀 + 算子 + smoke 后缀」命名，如 kimi_算子名_smoke.rs，放进 tests/ 即自动成为集成测试目标；除非要声明 required-features，否则无需改 Cargo.toml。
## 5. 综合实践

把整讲内容串成一张可长期维护的「crate 地图」，产出两件东西。

任务 A：目录结构图。以根目录清单为骨架，为每个 csrc/<线>/ 子目录标注三件事：对应 cargo feature、文件数、两个代表 kernel 文件名。骨架已填两行示例，其余自己补全后保存为个人笔记：

```
pegainfer-kernels/
├── Cargo.toml            # feature 总开关（L32-L50）
├── build.rs              # 2931 行构建管线（u1-l3 详讲）
├── KERNELS.md            # 算子索引路由表（u1-l5 详讲）
├── src/                  # lib.rs 12 行 + 7 个常驻模块 + ops/ + ffi/
├── csrc/
│   ├── common.cuh        # [常驻] 公共设施头
│   ├── shared/           # [常驻] 20 个：norm.cu、paged_attention.cu
│   ├── qwen35/           # [qwen35] 5 个：gated_delta_rule.cu、conv1d.cu
│   ├── qwen4/            # [qwen4] 2 个：……
│   ├── gemma4/           # [gemma4] 9 个：……
│   ├── marlin/           # [moe] repack_4bit.cu、moe_align.cu + vllm_marlin/
│   ├── deepep/           # [moe] 20 个：……
│   ├── deepseek_v2_lite/ # [deepseek-v2-lite] 2 个：……
│   ├── kimi_k2/          # [kimi-k2→moe] 9 个：……
│   ├── glm52/            # [glm52→moe] 18 个 + trtllm_gen/
│   └── k3/               # [k3] 13 个：……
├── tools/                # triton/、tilelang/、cutedsl/、kimi_k2/
├── cubin/                # glm52/：7 个 TRT-LLM cubin
├── third_party/          # DeepEP、DeepGEMM、FlashMLA、flash-kda、flashinfer
├── examples/             # 3 个，全部带 required-features
└── tests/                # 37 个集成测试 + common/
```

任务 B：tests 前缀统计。执行 4.5.4 的命令，把输出整理成 Markdown 表，并回答两个问题：哪个（哪些）前缀的测试最多？trap/reject 类负向测试覆盖哪几个面？

验收标准：

1. 结构图每个目录的 feature 标注与 Cargo.toml 一致（注意 glm52 与 kimi-k2 的 moe 传递，以及 marlin_face 的 any 门控）。
2. 文件数与实测一致（用 4.3.4 的命令复核）。
3. 统计总数等于 37。

参考答案（09a1a38f 实测）：最多的是 gemma4 与 glm52 并列 6 个；trap/reject 覆盖 hd256、hd512、qwen4 三个面共 5 个文件。

## 6. 本讲小结

- 根目录 = 三个「说明书」文件（Cargo.toml 的 feature 表、build.rs 构建管线、KERNELS.md 算子索引）+ Rust 侧 src + CUDA 侧 csrc + 构建期资产 tools/cubin/third_party + 验证面 examples/tests。
- src/lib.rs 只有 12 行：默认 7 个常驻模块（tensor、typed_ops、gpu_buffers、forward_pass、paged_kv、ops、ffi），外加 tvm-ffi-triton-cubin 门控的 triton_cubin。
- src/ops.rs 的模式是「私有 mod + cfg 门控 + pub use 再导出」；lora 与 dflash2 家族常驻、marlin_face 挂 any(gemma4, kimi-k2) 是三个容易看漏的点。
- csrc/ 按模型线分目录、文件名带模型线前缀；feature 与目录是多对多（moe 对应 marlin 与 deepep 两组目录，glm52 与 kimi-k2 都传递拉起 moe）。
- tools/ 生成器、cubin/ 预编译二进制、third_party/ vendored 上游，共同支撑「默认零 Python、按需 AOT」的构建哲学。
- tests/ 按前缀分组（gemma4 6、glm52 6、hd256 4、k3 4、dflash2 3、qwen4 3、hd512 2、kimi 1、共享 8），按后缀分类型；examples 与个别测试通过 required-features 与 feature 绑定。

## 7. 下一步学习建议

- 按学习路线，下一讲是 u1-l3《构建管线 I：nvcc 编译、SM 检测与 FlashInfer 头》：打开本讲只定位过位置的 build.rs，看 SM 探测、NvccTask 并行编译与 FlashInfer 头文件搜索路径。预习动作：用 grep -n "^fn" build.rs 数一数构建脚本的函数，建立轮廓。
- 想先拿到「导航工具」的读者可以并行读 u1-l5《KERNELS.md：算子索引与编辑规则》，学会从 op_id 一条链跳到 Rust wrapper、FFI symbol 与源文件——之后每个单元的源码阅读都会用到它。
- 延伸阅读：csrc/common.cuh 与 csrc/shared/ffi_guard.cuh（为 u4-l1 的 CUDA 侧工程约定打底）；仓库根 CLAUDE.md 的 Build & Run 与环境变量清单（PEGAINFER_CUDA_SM、PEGAINFER_TRITON_PYTHON 等）。
