# SM90 与 SM100 的架构取舍

## 1. 本讲目标

读到这里，你已经把 FlashMLA 的四类 kernel（dense/sparse × decode/prefill）逐个走了一遍。但还有一个贯穿全局、却容易被忽略的问题：**为什么这些 kernel 的「支持矩阵」是这样的不对称形状？**

- Dense Decoding 只跑在 SM90（Hopper），没有 SM100 版本；
- Dense Prefill 只跑在 SM100（Blackwell），没有 SM90 版本；
- Sparse 的两条路径（decode / prefill）在两代架构上都有，但内部走的实现类完全不同；
- SM100 上 head128 的 V3.2 形状没有原生 kernel，要靠「把 head64 kernel 跑两遍」来凑。

本讲是 Unit 9 的第一篇，目标不是再讲一遍某个 kernel 的算法，而是**退一步，站在架构层面把这四类 kernel 的「路径选择」统起来看**。读完本讲，你应当能够：

1. 默写出 README 的支持矩阵，并解释每一格背后的架构原因；
2. 在源码里快速定位「某架构 × 某 kernel 家族」对应的是哪个实现类 / 命名空间；
3. 理解 SM100 上 head128 为什么需要「head64x2 折中」，以及它和 sparse prefill 的 small_topk kernel 是怎么复用的；
4. 用「寄存器 / smem / cluster / num_sm_parts」这几把尺子，理解两代架构在调度粒度上的差异，以及由此带来的设计取舍。

本讲是把前八单元串起来的「俯瞰图」，建议在读完 u3-l4 / u5-l4 / u6-l4 / u7-l4 之后阅读。

## 2. 前置知识

本讲默认你已经掌握以下概念（在前面单元都已建立），这里只做一句话提醒：

- **SM90（Hopper）** 的核心算力原语：WGMMA（warp group 异步矩阵乘）、TMA（张量内存加速器，异步批量搬运）、CTA cluster + DSM（分布式共享内存，让两个 CTA 互相访问 smem）。FlashMLA 在 SM90 上的 kernel 大量用到这些（见 u3-l2 / u3-l3 / u5-l3）。
- **SM100（Blackwell）** 的核心算力原语：TMEM（专用的「张量内存」，存放 MMA 累加器和大块 Q/P）、UMMA（统一矩阵乘，取代 WGMMA）、UTCCP（统一张量拷贝）、2-SM cluster MMA（两个 SM 协作完成一次 MMA）、CLC（协作启动控制，支撑 persistent kernel）。这些是 u6-l3 / u7-l1 / u7-l2 反复出现的术语。
- **ImplBase 派发框架**（u2-l4）：sparse 路径把每个 kernel 实现抽象成「支持的能力清单（feature 子集）」，派发 = 校验「需求集合 R ⊆ 支持集合 S」。
- **DISPATCH 宏**（u2-l3）：用「立即调用的 lambda」把运行时值（head_dim / num_heads / model_type）编译期化，为每个取值生成一份模板特化。
- **MLA 的两种模式**（u1-l1）：MQA 模式（`head_dim_k=576`、`head_dim_v=512`，用于 decode 与 sparse prefill）与 MHA 模式（`head_dim_k=192/128`、`head_dim_v=128`，用于 dense prefill）。
- **split-KV 三段式**（u4-l1/u4-l2）：所有 decode 路径都是 `sched_meta → 主 kernel → combine`，prefill 路径无 combine。

一个直觉性的对比，先记住这张「两代架构速查表」，后面的源码细节都会回到它：

| 维度 | SM90（Hopper, H800） | SM100（Blackwell, B200） |
| :--- | :--- | :--- |
| MMA 原语 | WGMMA（wgmma async） | UMMA + 2-SM cluster MMA |
| 累加器存放 | 寄存器 | TMEM（专用张量内存） |
| 异步搬运 | TMA | UTCCP / TMA |
| 跨 CTA 共享 | DSM（CTA cluster size=2） | 2-SM cluster、multicast TMA |
| FP8 反量化 | 无 fp8→bf16 直转指令（见 u5-l2） | 改进的 Tensor Core |
| FlashMLA 角色 | dense decode / sparse decode / sparse prefill | dense prefill / sparse decode / sparse prefill |

## 3. 本讲源码地图

本讲的「源码」主要是**接口层的派发逻辑**，而不是 kernel 内部算法（那些前面单元讲过了）。涉及的关键文件：

| 文件 | 作用 |
| :--- | :--- |
| [README.md](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/README.md) | 权威的「支持矩阵」表格，本讲一切结论的源头 |
| [csrc/api/common.h](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/common.h) | `Arch` 结构（架构检测）与 `ImplBase` 派发框架 |
| [csrc/api/dense_decode.h](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/dense_decode.h) | dense decode 接口：显式 SM90 门禁 |
| [csrc/api/sparse_decode.h](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/sparse_decode.h) | sparse decode 接口：四个实现类 + 派发表 |
| [csrc/api/sparse_fwd.h](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/sparse_fwd.h) | sparse prefill 接口：四个实现类 + small_topk 选择 |
| [csrc/api/dense_fwd.h](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/dense_fwd.h) | dense prefill 接口：仅一行 include，直通 CUTLASS |
| [csrc/sm100/prefill/dense/interface.h](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/prefill/dense/interface.h) | dense prefill 的 `FMHACutlassSM100Fwd/BwdRun` 声明（名字里就带 SM100） |

记住一个观察总览：**dense 路径用「直接调用 + 硬件门禁」，sparse 路径用「ImplBase 派发器 + 实现类清单」**。这条线索贯穿本讲四个模块。

## 4. 核心概念与源码讲解

### 4.1 支持矩阵回顾

#### 4.1.1 概念说明

「支持矩阵」回答一个最朴素的问题：**给定一块 GPU 和一个想跑的 kernel，能不能跑？** FlashMLA 把答案压缩成 README 里一张 4 行 4 列的表。这张表的不对称是本讲的全部出发点——它不是随手写的限制，而是**架构能力差异**与**工程优先级**共同决定的。

注意矩阵的两个「非对称」：

1. **Dense Decoding 只在 SM90，Dense Prefill 只在 SM100**——两条 dense 路径各自只覆盖一代架构，且互补。
2. **两条 sparse 路径都跨两代架构**——但「跨」不等于「同」，两代上走的是完全不同的实现。

还有一条隐含信息：支持矩阵里 **Sparse Decoding 强制 FP8 KV cache**、**Sparse Prefill 不强制 FP8**、**Dense Decoding 用 BF16**、**Dense Prefill 用 MHA 模式**。这些是 kernel 家族的固有属性，不是架构属性。

#### 4.1.2 核心流程

把支持矩阵读成一张「架构 × 家族」决策表：

```
                Dense Decoding   Sparse Decoding   Dense Prefill   Sparse Prefill
SM90 (H800)         ✅                ✅               ❌               ✅
SM100 (B200)        ❌                ✅               ✅               ✅
MLA Mode            MQA               MQA              MHA              MQA
KVCache Format      BF16              FP8              (任意)           (bf16)
```

判定流程很简单：

1. 构造 `Arch arch = Arch();`，读出 `major/minor`；
2. 用 `arch.is_sm90a()` / `arch.is_sm100f()` 做架构判定；
3. 对应接口函数内部，要么硬门禁（dense），要么派发到该架构对应的实现类（sparse）。

#### 4.1.3 源码精读

支持矩阵的权威来源是 README：

[README.md:L59-L66](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/README.md#L59-L66) — 这就是「架构 × 家族」表本身，四行分别对应四类 kernel，`GPU Architecture` 列写明了 SM90 / SM100 / 两者皆有。

架构检测的实现在 `Arch` 结构里：

[common.h:L21-L41](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/common.h#L21-L41) — `Arch()` 构造时调用 `at::cuda::getCurrentDeviceProperties()`，缓存 `major/minor/num_sms`，并提供 `is_sm90a()`（major==9 且 minor==0）与 `is_sm100f()`（major==10）两个判定。注意 `is_sm100f()` 只判 major，没有细化到 minor，这是因为目前 Blackwell 消费级与数据中心卡在 minor 上不同，但 FlashMLA 只关心「是不是 Blackwell 这一代」。

这个 `Arch` 对象是**所有接口函数的第一步**——dense decode、sparse decode、sparse prefill 全都先 `Arch arch = Arch();`。`num_sms` 字段后面还会用来计算 `num_sm_parts`（见 4.4）。

#### 4.1.4 代码实践

> **实践目标**：把 README 的支持矩阵从「文档」变成「源码里可查证的事实」。

操作步骤：

1. 打开 [README.md](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/README.md) 的支持矩阵（第 59-66 行）。
2. 在四个接口文件里分别搜索架构判定：`grep -n "is_sm90a\|is_sm100f\|Arch arch" csrc/api/*.h`。
3. 记录每个接口对架构的约束方式（硬门禁 / 派发分支 / 编译期）。

需要观察的现象：

- `dense_decode.h` 里有一个**显式**的 `if (!arch.is_sm90a())` 报错；
- `sparse_decode.h` 与 `sparse_fwd.h` 里是 `if (is_sm100f) {...} else if (is_sm90a) {...}` 的**分支**；
- `dense_fwd.h` 里**找不到**任何 `is_sm90a/is_sm100f` 判定——这是 4.2 要解释的关键差异。

预期结果：你会得到一张「接口 → 架构约束方式」的对照表，dense decode = 硬门禁，sparse 两条 = 运行时分支，dense prefill = 无运行时判定（靠编译期）。

#### 4.1.5 小练习与答案

**练习 1**：支持矩阵里 Dense Prefill 的 MLA Mode 写的是 MHA（`head_dim_k=192/128`），而其余三个都是 MQA（`head_dim_k=576/512`）。这说明 dense prefill 服务的是哪类工作负载？

> **参考答案**：dense prefill 是「标准稠密 MHA」（head_dim 128/128 或 MLA 的 192/128），主要作为通用注意力算子（类似 flash_attn），而非 DeepSeek 生产里的 MLA prefill 路径——生产环境的 MLA prefill 走的是 sparse prefill。这也部分解释了为什么它由 NVIDIA 贡献（见 README News 2025.08.01）且只做了 SM100。

**练习 2**：如果未来要在支持矩阵里新增一行「Sparse Decoding（BF16 KV cache）」，需要改哪些地方？

> **参考答案**：至少要改 `sparse_decode.h` 里的 KV dtype 校验（当前强制 `fp8_e4m3fn/int8/uint8`）、`SparseAttnDecodeParams` 的 KV 指针类型、`DecodeFeatures` 枚举（新增一个 BF16 cache 的 feature 位）、以及每个 `DecodeImpl` 类的 `DECLARE_SUPPORTED_FEATURES`。这正是下一讲 u9-l2「扩展点」要展开的内容。

### 4.2 两架构路径差异

#### 4.2.1 概念说明

「支持」是一回事，「怎么实现」是另一回事。同一个 kernel 家族在两代架构上，**派发风格**和**实现粒度**都不一样。本模块对比四类 kernel 在两代架构上的「路径」。

最重要的对比是 dense 的两条路径，因为它们体现了**两种完全不同的架构约束落地方式**：

- **Dense decode（SM90 only）**：接口函数里写**显式的运行时门禁**——`if (!arch.is_sm90a()) TORCH_CHECK(false, ...)`。它在第一行就把非 SM90 的请求挡在门外。
- **Dense prefill（SM100 only）**：接口函数里**没有任何运行时门禁**——它甚至没有一个独立的接口函数，pybind 直接把 `FMHACutlassSM100FwdRun` 暴露出去。SM100 的约束是靠**编译期**落地的：kernel 用了 `SM100_MMA_*`、`SM100_TMEM_*`、`UMMA` 等 Blackwell 专有指令，并以 `KERUTILS_ENABLE_SM100A` 宏保护。

为什么 dense 两条路径互补地各占一代？因为它们由**不同的来源、为不同的目的**写成：dense decode 是 DeepSeek 自研、针对 Hopper 的 seesaw 调度（u3-l3）；dense prefill 是 NVIDIA 贡献、基于 CUTLASS 的 Blackwell 实现（u7-l1）。两边都没有动力把对方那一代也补全——dense prefill 在 DeepSeek 生产里不是关键路径（MLA prefill 走 sparse），所以没有 SM90 移植。

#### 4.2.2 核心流程

四类 kernel 的「架构 → 实现类 / 命名空间」派发流程：

```
dense decode:
  Arch.is_sm90a()?  ──否──▶ TORCH_CHECK 报错（硬门禁）
       │是
       └──▶ sm90::run_flash_splitkv_mla_kernel<bf16/half>   （直接调用，无派发器）

dense prefill:
  （无运行时架构判定）
  pybind dense_prefill_fwd ──▶ FMHACutlassSM100FwdRun ──▶ CUTLASS SM100 kernel
  （约束靠编译期：SM100_MMA_* / SM100_TMEM_* / UMMA，KERUTILS_ENABLE_SM100A 保护）

sparse decode:
  is_sm100f? ─是─▶ 按 (h_q, d_qk) 选 Head64 / Head64x2 / Head128 三个实现类之一
       │否
       └──is_sm90a?─▶ Decode_Sm90_Impl（一个通用实现类，支持全部 head/dim 组合）

sparse prefill:
  is_sm90a? ─是─▶ Fwd_Sm90_Impl
       │否
       └──is_sm100f?─▶ 按 h_q 选 Head64 / Head128；Head128 再二选一（small_topk vs 普通）
```

注意一个对称性破缺：**sparse 在 SM90 上是「一个通用实现类吃所有形状」，在 SM100 上是「多个专用实现类按形状分」**。原因在 4.4 讲。

#### 4.2.3 源码精读

**(a) Dense decode 的显式 SM90 门禁**：

[dense_decode.h:L26-L29](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/dense_decode.h#L26-L29) — 这是 dense decode 唯一的架构约束：非 SM90a 直接 `TORCH_CHECK(false, "Dense decode MLA is only supported on SM90a architecture")`。之后只调用 `sm90::run_flash_splitkv_mla_kernel`（[dense_decode.h:L175-L185](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/dense_decode.h#L175-L185)），没有任何 SM100 分支。这是支持矩阵「Dense Decoding = SM90」的源码体现。

**(b) Dense prefill 没有独立接口函数、没有运行时门禁**：

[dense_fwd.h:L1-L6](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/dense_fwd.h#L1-L6) — 整个 `dense_fwd.h` 只有一行 `#include "sm100/prefill/dense/interface.h"`，没有写任何接口函数。

[interface.h:L5-L8](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/prefill/dense/interface.h#L5-L8) — 函数名 `FMHACutlassSM100FwdRun` 本身就把 SM100 写死了。

[fmha_cutlass_fwd_sm100.cu:L31-L83](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/prefill/dense/fmha_cutlass_fwd_sm100.cu#L31-L83) — 函数体里**找不到** `is_sm90a/is_sm100f` 判定。它按 `head_dim_qk / head_dim_vo`（192/128 是 MLA，128/128 是普通 MHA）和 mask/varlen 派发（[L66-L73](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/prefill/dense/fmha_cutlass_fwd_sm100.cu#L66-L73)），但架构约束不在运行时。真正的约束藏在 collective 层：例如 `PipelineTmaUmmaAsync`、`SM100_TMEM_LOAD/STORE`、`UMMA::ScaleOut`（见 `collective/sm100_fmha_mla_fwd_mainloop_tma_warpspecialized.hpp`），以及 kernel 层的 `#if defined(KERUTILS_ENABLE_SM100A)` 编译保护。**也就是说，dense prefill 的「SM100 only」是编译期的、隐式的**——在 SM90 上调用它不会得到友好的 `TORCH_CHECK` 报错，而是会在加载 / 启动 SM100 专有指令时失败。这是一个值得注意的工程取舍：用类型安全换运行时诊断。

**(c) Sparse decode 的「SM90 通用 vs SM100 多专用」**：

[sparse_decode.h:L44-L76](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/sparse_decode.h#L44-L76) — SM90 的 `Decode_Sm90_Impl` 用一个实现类支持 `HEAD_64 / HEAD_128` × `HEAD_DIM_512 / HEAD_DIM_576` 全部组合（feature 清单里四个都列了），内部用 `DISPATCH_MODEL_TYPE` + `DISPATCH_NUM_HEADS` 把运行时值编译期化，最终调 `sm90::decode::sparse_fp8::run_flash_splitkv_mla_fp8_sparse_kernel<MODEL_TYPE, NUM_HEADS>`。

[sparse_decode.h:L362-L381](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/sparse_decode.h#L362-L381) — SM100 的派发则是「按形状选实现类」：

```cpp
if (arch.is_sm100f()) {
    if (h_q == 64)        impl = new Decode_Sm100_Head64_Impl();
    else if (h_q == 128) {
        if (d_qk == 576)  impl = new Decode_Sm100_Head64x2_Impl();   // V3.2 折中
        else if (d_qk == 512) impl = new Decode_Sm100_Head128_Impl();// MODEL1
    }
} else if (arch.is_sm90a()) {
    impl = new Decode_Sm90_Impl();
}
```

**(d) Sparse prefill 的两代路径**：

[sparse_fwd.h:L213-L240](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/sparse_fwd.h#L213-L240) — SM90 一个 `Fwd_Sm90_Impl`，SM100 上 h_q==64 走 `Fwd_Sm100_Head64_Impl`，h_q==128 在 `Fwd_Sm100_Head128_Small_TopK_Impl` 与 `Fwd_Sm100_Head128_Impl` 之间二选一（见 4.3）。

#### 4.2.4 代码实践

> **实践目标**：亲手验证「dense decode 硬门禁 vs dense prefill 无门禁」的差异。

操作步骤：

1. 在 `csrc/api/dense_decode.h` 里找到 [L26-L29](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/dense_decode.h#L26-L29)，确认报错文案。
2. 用 `grep -rn "is_sm90a\|is_sm100f" csrc/sm100/prefill/dense/` 检查 dense prefill 是否有任何运行时架构判定。
3. 用 `grep -n "KERUTILS_ENABLE_SM100A" csrc/sm100/prefill/dense/kernel/*.hpp` 找到编译期保护点。

需要观察的现象：

- dense decode：有运行时 `TORCH_CHECK`，文案明确；
- dense prefill：运行时判定为 0 个命中，但 `KERUTILS_ENABLE_SM100A` 在多个 kernel 头里出现（例如 `sm100_fmha_fwd_kernel_tma_warpspecialized.hpp:255`），说明架构约束落在编译期。

预期结果：你能用一句话向别人解释——「dense decode 用运行时报错保护 SM90 边界，dense prefill 用编译期宏保护 SM100 边界，前者对用户更友好，后者对实现更省事」。

> 待本地验证：步骤 2/3 的 grep 命中行数与具体文件名，以你本仓库实际输出为准。

#### 4.2.5 小练习与答案

**练习 1**：为什么 dense prefill 不像 dense decode 那样加一个 `if (!is_sm100f()) TORCH_CHECK(...)`？

> **参考答案**：两个原因。其一，dense prefill 直接复用 NVIDIA 贡献的 CUTLASS 入口 `FMHACutlassSM100FwdRun`，没有 FlashMLA 自己的「接口函数」中间层（对比 dense decode 有 `dense_attn_decode_interface`），所以没有自然的位置塞运行时校验。其二，这些 kernel 本质上由 SM100 专有指令（TMEM/UMMA）定义，编译期 `KERUTILS_ENABLE_SM100A` 已能保证不在错误架构上编出可用代码。代价是诊断不友好——这是用「实现简洁」换「运行时诊断」的取舍。

**练习 2**：sparse 路径在 SM90 上用一个通用实现类，SM100 上用多个专用实现类。哪种风格更接近 dense decode 的做法？

> **参考答案**：SM90 的「一个通用实现类」更接近 dense decode——都是「一个 kernel 入口 + DISPATCH 宏把形状编译期化」。SM100 的「多个专用实现类」则是「每个形状一个独立 kernel」，粒度更细，但也意味着新增形状要新增实现类（见 u9-l2）。

### 4.3 head128 折中

#### 4.3.1 概念说明

「折中（compromise）」是本讲最值得品味的工程模式：**当硬件能力不足以原生支持某种形状时，用「跑两次更小的 kernel」来凑出正确结果**。

具体到 SM100 的 sparse decoding：V3.2 模型形状是 `h_q=128, d_qk=576`。但 SM100 上**没有原生支持 `HEAD_DIM_575` 的 head128 decode kernel**——现有的 head128 kernel（`Decode_Sm100_Head128_Impl`）只支持 `HEAD_DIM_512`（MODEL1），且其实是复用了 sparse prefill 的 small_topk kernel。

于是作者写了一个 `Decode_Sm100_Head64x2_Impl`：**把 128 个 query 头切成两段 64 头，平移 q / out / lse / accum / attn_sink 的指针，把 head64 kernel 跑两遍**。源码注释写得很直白："An implementation that calls the head64 kernel twice to process head128. Necessary for running V3.2 shape (i.e. h = 128, d_qk = 576) on SM100f"。

这是一个**纯粹的正确性兜底**，不追求性能最优——README 也坦言 SM100 sparse decode「not really optimized yet」。它的存在让 V3.2 形状在 B200 上能跑通，等未来写出原生 head128+576 kernel 再替换。

注意区分 SM100 上 head128 的**两条**不同路径，很容易混淆：

| 形状 | 实现类 | 实际跑的 kernel |
| :--- | :--- | :--- |
| `h_q=128, d_qk=576`（V3.2） | `Decode_Sm100_Head64x2_Impl` | head64 decode kernel × 2 |
| `h_q=128, d_qk=512`（MODEL1） | `Decode_Sm100_Head128_Impl` | small_topk prefill kernel（DecodeWithSplitKV 模式） |

第二条尤其巧妙：它在 sparse **decode** 路径里复用了 sparse **prefill** 的 small_topk kernel，靠模板参数 `SparseAttnFwdMode::DecodeWithSplitKV` 切换行为（这是 u6-l3 讲过的「一份代码两种模式」）。

#### 4.3.2 核心流程

`Decode_Sm100_Head64x2_Impl::run_` 的流程：

```
对 start_head_idx ∈ {0, 64}:
  1. 复制一份 params（cur_params = params）
  2. 把所有「按头索引」的指针平移 start_head_idx 个头：
     - q           += start_head_idx * stride_q_h_q
     - out         += start_head_idx * stride_o_h_q
     - lse         += start_head_idx          （lse 每头一个标量）
     - lse_accum   += start_head_idx
     - o_accum     += start_head_idx * stride_o_accum_h_q
     - attn_sink   += start_head_idx          （若有）
  3. cur_params.h_q = 64
  4. 调用 sm100::decode::head64::run_..._kernel<MODEL_TYPE>(cur_params)
两段结果拼起来，等价于一次 128 头的 decode。
```

这个平移的数学等价性来自 **MLA / MQA 的头独立性**：每个 query 头的 attention 只依赖自己头的 q 和共享的 KV，头之间互不影响。所以「128 头一次算」和「64 头算两次」在数学上完全等价，只是用了两倍的 kernel 启动和（可能）两倍的 KV 读取——后者是性能损失，但因为有 split-KV / combine 机制兜底，正确性无虞。

#### 4.3.3 源码精读

[sparse_decode.h:L110-L153](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/sparse_decode.h#L110-L153) — `Decode_Sm100_Head64x2_Impl` 全文。注意三点：

1. 第 113-123 行的 `DECLARE_SUPPORTED_FEATURES` 只声明了 `HEAD_128`（**不**含 `HEAD_64`），且支持 `HEAD_DIM_512 / HEAD_DIM_576` 两种——这是它被 [L367-L368](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/sparse_decode.h#L367-L368) 选中处理 V3.2 head128 的依据。
2. 第 138-150 行的循环 `for (int start_head_idx = 0; start_head_idx < 128; start_head_idx += 64)`，循环体内做指针平移后把 `cur_params.h_q = 64`，再调 head64 kernel。
3. 每个 split 内部仍走完整的 split-KV → combine 流程（在外层 `sparse_attn_decode_interface` 里统一调度），所以 head64x2 不需要自己处理 combine。

对照真正的 head128 实现：

[sparse_decode.h:L156-L181](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/sparse_decode.h#L156-L181) — `Decode_Sm100_Head128_Impl`，feature 清单**只支持 `HEAD_DIM_512`（不含 576）**，run_ 调的是 `sm100::fwd_for_small_topk::head128::run_fwd_for_small_topk_phase1_kernel<SparseAttnFwdMode::DecodeWithSplitKV, 512>`——注意命名空间是 `fwd_for_small_topk`（prefill 的），模板实参是 `DecodeWithSplitKV`（decode 模式）。这就是「sparse decode 的 head128 借用了 sparse prefill 的 small_topk kernel」。

再看 sparse prefill 端 head128 的 small_topk 选择逻辑：

[sparse_fwd.h:L220-L234](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/sparse_fwd.h#L220-L234) — 在 `Fwd_Sm100_Head128_Small_TopK_Impl` 与 `Fwd_Sm100_Head128_Impl` 之间二选一，规则是 `topk <= 1280 且 small_topk 支持所需 feature` 时优先 small_topk。注意 `small_topk` 只支持 `HEAD_DIM_512`（[sparse_fwd.h:L86-L99](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/sparse_fwd.h#L86-L99) 的 feature 清单没有 `HEAD_DIM_576`）。

#### 4.3.4 代码实践

> **实践目标**：亲手追踪一次「V3.2 形状在 SM100 上的 head64x2 折中」调用。

操作步骤：

1. 假设输入 `h_q=128, d_qk=576, d_v=512`，运行在 SM100（B200）。
2. 跟着 [sparse_decode.h:L362-L381](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/sparse_decode.h#L362-L381) 的派发，确认会落到 `Decode_Sm100_Head64x2_Impl`（`is_sm100f` 且 `h_q==128` 且 `d_qk==576`）。
3. 进入 [L136-L151](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/sparse_decode.h#L136-L151) 的 `run_`，列出两轮循环里 `start_head_idx = 0` 和 `64` 时，`cur_params.q / cur_params.out / cur_params.lse / cur_params.lse_accum / cur_params.o_accum` 各自的指针偏移量（用对应的 stride 乘 0 或 64）。
4. 解释为什么 `cur_params.attn_sink` 的平移是 `+= start_head_idx`（一个头一个 float），而 `cur_params.q` 的平移是 `+= start_head_idx * stride_q_h_q`。

需要观察的现象：

- 第一轮（`start_head_idx=0`）所有偏移为 0，处理头 0..63；
- 第二轮（`start_head_idx=64`）处理头 64..127；
- 两轮写到的 `out` / `lse` 内存区间不重叠，正好拼成 128 头的完整输出。

预期结果：你能画出一张「两轮循环的指针平移表」，并解释平移单位差异来自各张量「每头元素数」不同（lse 每头 1 个 float，q 每头 `d_qk` 个元素，out 每头 `d_v` 个元素）。

#### 4.3.5 小练习与答案

**练习 1**：head64x2 把 128 头跑两遍，会不会让 KV 被读两遍，从而性能减半？

> **参考答案**：会多读一遍 KV（两轮各读一次共享的 FP8 KV cache），这是性能损失的主要来源，也是 README 说 SM100 sparse decode「not really optimized yet」的原因之一。但正确性不受影响，因为 combine kernel 按 `num_splits` 前缀和归并，每轮各自的 split-KV 结果会被正确合并。这正是「折中」的代价：用性能换「能跑通」。

**练习 2**：为什么 `Decode_Sm100_Head128_Impl` 的 feature 清单里没有 `HEAD_DIM_576`？

> **参考答案**：因为它复用的 small_topk kernel 只实例化了 `d_qk=512`（见 [sparse_fwd.h:L97](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/sparse_fwd.h#L97) 的 `<SparseAttnFwdMode::Prefill, 512>`，以及 decode 复用时的 `<..., 512>`）。所以 V3.2 的 `d_qk=576` head128 请求无法被它满足，只能落到 `Decode_Sm100_Head64x2_Impl`。这也是 u6-l4 提过的「small_topk 仅支持 d_qk=512」在 decode 侧的回响。

### 4.4 能力差异与取舍

#### 4.4.1 概念说明

前三个模块讲「是什么」，本模块讲「为什么」。两代架构的能力差异最终落在三个维度，每个维度都驱动了一个具体的设计取舍：

1. **累加器存放：寄存器 vs TMEM**。SM90 的 WGMMA 把累加器放在寄存器，单个 warpgroup 的寄存器放不下两份 64×512 的输出 O，所以 dense decode 用 seesaw 把 O 竖切成 O_L/O_R 分给两个 warpgroup（u3-l3）。SM100 有专用 TMEM 存放累加器，容量充裕，于是 dense prefill 的 collective 把 S/P/O 全放进 TMEM（u7-l2），不用 seesaw。**取舍**：SM90 用算法技巧（seesaw）绕开寄存器不足，SM100 用硬件资源（TMEM）直接解决。

2. **cluster / SM 协作：DSM vs 2-SM cluster MMA**。SM90 的 CTA cluster（size=2）+ DSM 用于 sparse FP8 decode 的 crossover——两个 CTA 各反量化半份 KV 再互换（u5-l3）。SM100 的 2-SM cluster MMA 则是两个 SM 直接协作完成一次 MMA，sparse prefill 的 head128 kernel 据此把 `num_sm_parts` 再除以 2。**取舍**：同样是「两个执行单元协作」，SM90 用它来分摊反量化（CUDA Core 的活），SM100 用它来拼大块 MMA（Tensor Core 的活）。

3. **FP8 反量化能力**。SM90 无 fp8→bf16 直转指令，反量化要 fp8→half→fp32→bf16→×scale 四步（u5-l2），成为 dequantization-bound，催生了 crossover。SM100 改进了 Tensor Core，dequant 不再是同等瓶颈，所以 SM100 sparse decode 不需要 crossover 那套 DSM 互换。**取舍**：硬件短板（SM90 FP8）逼出算法创新（crossover），硬件补齐（SM100）后又让该创新变得不必要。

还有一个调度粒度上的差异，体现在 `num_sm_parts`（split-KV 的并行度）计算上：

- SM90 dense decode：`num_sms / num_heads_k / ceil_div(q_seq_per_hk, 64)`——除以 KV 头数，因为不同 KV 头组用不同的 SM 集合。
- SM90 sparse decode：`num_sms / s_q / (h_q/64)`——除以 query 头组数。
- SM100 sparse head64：`num_sms / s_q`——**不除以头数**，因为 head64 kernel 内部把 64 个头当一个整体处理。
- SM100 sparse head128（small_topk）：`num_sms / s_q / 2`——再除以 2，对应 2-SM cluster。

这个差异不是随意的，它反映了「一个 SM partition 能装下多少头的工作」在不同架构 / kernel 上的不同。

#### 4.4.2 核心流程

把四个维度的取舍浓缩成一张「能力差异 → 设计取舍」对照表：

```
维度            SM90 (Hopper)              SM100 (Blackwell)           取舍
─────────────────────────────────────────────────────────────────────────────
累加器          寄存器（紧张）              TMEM（充裕）                 SM90→seesaw 切 O；SM100→O 进 TMEM
跨单元协作      DSM / cluster size=2        2-SM cluster MMA            SM90→crossover 分摊反量化；SM100→拼大 MMA
FP8→bf16        无直转（4 步，瓶颈）        改进（非瓶颈）              SM90→crossover；SM100→不需要
num_sm_parts    除以头组数                  不除 / 再除 2               反映「单 partition 能装的头的多少」
```

#### 4.4.3 源码精读

`num_sm_parts` 的差异在 `get_meta` 里看得很清楚：

[dense_decode.h:L78](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/dense_decode.h#L78) — SM90 dense decode：`std::max(arch.num_sms / num_heads_k / cutlass::ceil_div(seqlen_q_ori*num_heads_q/num_heads_k, 64), 1)`，除以 `num_heads_k`（KV 头数）。

[sparse_decode.h:L59-L66](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/sparse_decode.h#L59-L66) — SM90 sparse decode：`num_sms / s_q / (h_q/64)`，除以 `h_q/64`（query 头组数），`fixed_overhead_num_blocks = 5`。

[sparse_decode.h:L92-L99](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/sparse_decode.h#L92-L99) — SM100 sparse head64：`num_sms / s_q`，**不除头数**，`fixed_overhead_num_blocks = 5`。

[sparse_decode.h:L168-L175](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/sparse_decode.h#L168-L175) — SM100 sparse head128（small_topk）：`num_sms / s_q / 2`，再除以 2（2-SM cluster），`fixed_overhead_num_blocks = 3`（比 head64 的 5 小，因为 cluster 内协作开销不同）。

派发框架本身的对称美在 `ImplBase::run`：

[common.h:L226-L229](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/common.h#L226-L229) — `run` 先 `check_if_all_features_are_supported_and_abort`，再 `run_`。无论实现类是 SM90 的通用版还是 SM100 的某个专用版，都走同一条「先校验 feature 再执行」的路径。这层抽象让「SM90 一个实现类、SM100 多个实现类」的差异对外不可见——接口函数只管 `impl->run(params, features)`。

#### 4.4.4 代码实践

> **实践目标**：用 `num_sm_parts` 公式量化「同一块 GPU 上，不同架构路径能切出多少 split-KV 并行度」。

操作步骤：

1. 假设一块 H800（SM90，`num_sms=132`），跑 dense decode：`h_kv=1, s_q=1, h_q=128, d=576`。
   - `num_q_heads_per_hk = 128/1 = 128`，`q_seq_per_hk = 1*128 = 128`；
   - 代入 [dense_decode.h:L78](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/dense_decode.h#L78)：`132 / 1 / ceil(128/64) = 132 / 1 / 2 = 66` 个 SM partition。
2. 假设一块 B200（SM100，`num_sms=148`），跑 sparse decode head64：`h_q=64, s_q=1`。
   - 代入 [sparse_decode.h:L95](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/sparse_decode.h#L95)：`148 / 1 = 148` 个 partition。
3. 同一块 B200 跑 sparse decode head128（small_topk，`h_q=128`）：
   - 代入 [sparse_decode.h:L171](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/sparse_decode.h#L171)：`148 / 1 / 2 = 74` 个 partition。

需要观察的现象：

- SM100 head64 不除头数，所以 partition 数 ≈ num_sms；
- SM100 head128 因 2-SM cluster 再除 2，partition 数减半；
- SM90 dense decode 除以 KV 头数相关的项，partition 数受 head 结构约束。

预期结果：你会直观看到「2-SM cluster 让 head128 的并行度减半」这一架构事实如何反映到 `num_sm_parts` 上。

> 待本地验证：上述 `num_sms` 取值（H800=132、B200=148）需以 `torch.cuda.get_device_properties().multiProcessorCount` 实测为准；不同 SXM/PCIe 版本可能不同。

#### 4.4.5 小练习与答案

**练习 1**：seesaw（SM90 dense decode）和 TMEM（SM100 dense prefill）解决的是同一个问题吗？

> **参考答案**：是——都是「放不下大块输出累加器」。SM90 的 WGMMA 累加器在寄存器，64×512 的 O 放不下两份，于是 seesaw 把 O 竖切成 O_L/O_R 给两个 warpgroup 轮转。SM100 把累加器放进专用 TMEM，容量充裕，于是不用 seesaw，直接在 TMEM 里维护 S/P/O。一个用算法绕，一个用硬件资源直接解。

**练习 2**：为什么 SM100 sparse decode 不需要 SM90 那套 crossover + DSM 互换？

> **参考答案**：crossover 的目的是把 FP8 反量化工作量减半，因为 SM90 上反量化（四步转换链）是 dequantization-bound（u5-l2/u5-l3）。SM100 的 Tensor Core 对 FP8 / 低精度支持更好，反量化不再是同等瓶颈，所以没有动力去引入 cluster + DSM 的复杂同步。**硬件补齐短板后，当初为绕开短板而发明的算法就被省掉了**——这是架构演进的一个常见模式。

## 5. 综合实践

把本讲四个模块串起来，做一张完整的「**架构 × kernel 家族 → 实现类 / 命名空间**」总表。这是本讲最重要的产出，建议亲手填、对照源码核验。

请按下表填空（实现类名 / 命名空间 / 入口函数），全部答案可在本讲引用的源码里找到：

| Kernel 家族 | 架构 | 入口 / 接口函数 | 实现类（sparse）或命名空间（dense） | 架构约束方式 |
| :--- | :--- | :--- | :--- | :--- |
| Dense Decoding | SM90 | `dense_attn_decode_interface` | `sm90::run_flash_splitkv_mla_kernel` | 运行时硬门禁 `is_sm90a()` |
| Dense Decoding | SM100 | — | （不存在） | — |
| Sparse Decoding | SM90 | `sparse_attn_decode_interface` | `Decode_Sm90_Impl` | 运行时分支 |
| Sparse Decoding | SM100, h_q=64 | 同上 | `Decode_Sm100_Head64_Impl` | 运行时分支 |
| Sparse Decoding | SM100, h_q=128, d_qk=576 | 同上 | `Decode_Sm100_Head64x2_Impl`（head64 跑两遍） | 运行时分支 |
| Sparse Decoding | SM100, h_q=128, d_qk=512 | 同上 | `Decode_Sm100_Head128_Impl`（复用 small_topk） | 运行时分支 |
| Dense Prefill | SM90 | — | （不存在） | — |
| Dense Prefill | SM100 | `FMHACutlassSM100FwdRun`（pybind 直通） | `sm100::prefill::dense`（CUTLASS SM100） | 编译期 `KERUTILS_ENABLE_SM100A` |
| Sparse Prefill | SM90 | `sparse_attn_prefill_interface` | `Fwd_Sm90_Impl` | 运行时分支 |
| Sparse Prefill | SM100, h_q=64 | 同上 | `Fwd_Sm100_Head64_Impl` | 运行时分支 |
| Sparse Prefill | SM100, h_q=128 | 同上 | `Fwd_Sm100_Head128_Impl` 或 `_Small_TopK_Impl`（topk≤1280 优先 small_topk） | 运行时分支 + topk 阈值 |

填完后，回答本讲开篇的核心问题——**用 2-3 句解释为何 dense prefill 没有落在 SM90**：

> 参考答案要点：(1) dense prefill 由 NVIDIA 贡献，是基于 CUTLASS 的 Blackwell 实现，用的是 TMEM / UMMA / 2-SM cluster MMA 等 SM100 专有指令（编译期由 `KERUTILS_ENABLE_SM100A` 保护），没有 SM90 移植；(2) DeepSeek 生产环境的 MLA prefill 走的是 sparse prefill（token-level 稀疏），dense prefill 主要作为通用 MHA 算子（类 flash_attn），不是关键路径，所以没有动力补 SM90 版本；(3) 若强要在 SM90 跑 dense prefill，需要另写一套基于 WGMMA 的实现（类似 FlashAttention-3 的 Hopper 路径），工作量不小而收益有限。

进阶（可选）：再补一张「能力差异 → 取舍」表（累加器 / cluster / FP8 / num_sm_parts 四行），检验你对 4.4 的掌握。

## 6. 本讲小结

- 支持矩阵的不对称（dense decode 仅 SM90、dense prefill 仅 SM100、sparse 两代都有）是**架构能力差异**与**工程优先级**共同决定的，不是随意限制。
- 架构约束有**两种落地方式**：dense decode 用运行时 `TORCH_CHECK` 硬门禁（对用户友好），dense prefill 用编译期 `KERUTILS_ENABLE_SM100A` + SM100 专有指令（对实现省事，但诊断不友好）。
- sparse 路径在 SM90 上是「**一个通用实现类**吃所有形状」，在 SM100 上是「**多个专用实现类**按形状分」，前者靠 DISPATCH 宏编译期化，后者靠 ImplBase 派发器选择。
- SM100 上 V3.2 的 head128（`d_qk=576`）没有原生 kernel，靠 `Decode_Sm100_Head64x2_Impl` 把 head64 kernel **跑两遍**凑出正确结果——典型的「能力不足时的折中」，用性能换能跑通。
- SM100 head128 的 MODEL1 形状（`d_qk=512`）则**复用 sparse prefill 的 small_topk kernel**，靠 `SparseAttnFwdMode::DecodeWithSplitKV` 模板开关切到 decode 行为，一份代码两种模式。
- 两代架构的核心取舍：SM90 用 seesaw 绕开寄存器不足、用 crossover+DSM 绕开 FP8 反量化瓶颈；SM100 用 TMEM 充裕的累加器容量和改进的 Tensor Core 直接解决，省掉了这些算法技巧——**硬件补齐短板后，软件绕路的技巧就被省掉**。

## 7. 下一步学习建议

- **u9-l2 扩展点：新增 head_dim / feature 的流程**：本讲你看到了支持矩阵和派发表的形状，下一讲教你怎么给它「加一格」——新增一个 head_dim 或 feature 要改 DISPATCH 宏、params、Impl 的 feature 清单、实例化文件、setup.py sources，是一份端到端的改动清单。
- **u9-l3 端到端实战：复现一次 sparse decode**：把本讲的认识落到一次真实的端到端复现里——构造数据 → FP8 量化 → 生成 indices/sched_meta → 调 kernel → 校验 → 测速。
- 若想进一步理解架构能力差异的硬件根源，可回到 **u3-l2/u3-l3（SM90 GMMA 与 seesaw）**、**u5-l2/u5-l3（SM90 FP8 反量化与 crossover）**、**u7-l2（SM100 TMEM/UMMA mainloop）** 对照阅读，你会更清楚地看到「同一类问题，两代架构给出的不同答案」。
