# Sparse fwd 接口与实现选择

## 1. 本讲目标

本讲聚焦 FlashMLA **token-level 稀疏 prefill** 的 C++ 接口层 `csrc/api/sparse_fwd.h`。它是 Python 入口 `flash_mla_sparse_fwd` 与底层 SM90/SM100 kernel 之间的「校验 + 编排 + 派发」中枢。

学完后你应该能够：

1. 说清楚 `sparse_attn_prefill_interface` 这个函数做了哪四件事（架构门禁、张量校验、参数装配、实现派发）。
2. 理解 `FwdFeatures` 枚举与 `required_features` 需求集合是如何从运行时张量属性构造出来的。
3. 看懂基于 `ImplBase` 的「需求集合 ⊆ 支持集合」派发机制在 prefill 路径上落地为四个实现类。
4. 复现 SM100 head128 下「small_topk vs 普通」的选择决策树，解释 `topk <= 1280` 阈值与 `!regular_impl.check_if_all_features_are_supported(...)` 兜底分支各自的作用。

本讲只讲**接口与派发**，不展开 kernel 内部的 online softmax / TMEM / crossover 细节（那是 u6-l2、u6-l3 的内容），也不重复 `ImplBase` 通用框架的设计动机（那是 u2-l4 的内容）。

## 2. 前置知识

在进入本讲前，你需要先建立以下认知（来自前置讲义）：

- **sparse attention 的数据契约（u6-l1）**：prefill 路径的 `indices` 形状是 `[s_q, h_kv, topk]`，直接作为 `kv` 张量第 0 维的下标，无效索引判 `-1` 或 `>= s_kv`；`topk_length` 是 `[s_q]` 的「最左若干个」截断掩码；输出三件套 `(out, max_logits, lse)`，其中 `lse` 是 base-e 的 log-sum-exp。
- **ImplBase 派发框架（u2-l4）**：把每个 kernel 实现抽象为「它支持的能力清单」，派发即做子集校验——需求集合 \(R\) 必须是实现支持集合 \(S\) 的子集（\(R \subseteq S\)）。`ImplBase::run` 先 `check_if_all_features_are_supported_and_abort` 再调 `run_`，强制「先校验后执行」。
- **DISPATCH 宏（u2-l3）**：用立即调用的 lambda（IIFE）把运行时值（如 `d_qk`）编译期化为模板常量，从而为每个合法取值实例化一份独立模板特化。
- **SM100 sparse prefill 的三条路径（u6-l3）**：普通 head64（单 CTA，Q 全进 TMEM）、普通 head128（2-CTA cluster）、以及为小 topk 优化的 `fwd_for_small_topk` 变体——后者最巧妙之处是用模板参数 `SparseAttnFwdMode` 把 prefill 与 decode 编进同一份 kernel 代码。

> 关键术语速查：`SparseAttnFwdParams`（prefill 的纯数据参数包）、`FwdFeatures`（prefill 的能力枚举）、`required_features`（本次调用需求的能力集合）、`FwdImplBase`（prefill 实现的基类）、small_topk（针对小 topk 优化的 head128 变体）。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| `csrc/api/sparse_fwd.h` | **本讲主角**。定义 `FwdFeatures` 枚举、四个 `Fwd*Impl` 实现类，以及接口函数 `sparse_attn_prefill_interface`（校验 + 装配 + 派发）。 |
| `csrc/api/common.h` | 提供 `ImplBase` 基类、`DECLARE_SUPPORTED_FEATURES` 宏、`Arch`、`int64_stride_to_int`、`DISPATCH_*` 宏等胶水机制。 |
| `csrc/params.h` | 定义 `SparseAttnFwdParams` 参数结构、`SparseAttnFwdMode` 枚举与 `SparseFwdArgT` 类型别名。 |
| `csrc/api/api.cpp` | pybind 绑定，把 `sparse_attn_prefill_interface` 注册为 Python 侧的 `flash_mla_cuda.sparse_prefill_fwd`。 |
| `flash_mla/flash_mla_interface.py` | Python 薄壳 `flash_mla_sparse_fwd`，仅转发参数。 |
| `csrc/api/sparse_decode.h` | 对照参考：decode 侧 `Decode_Sm100_Head128_Impl` 直接复用 small_topk kernel，证明 prefill/decode 同源复用。 |
| `csrc/sm100/prefill/sparse/fwd/head128/phase1.h` 与 `fwd_for_small_topk/head128/phase1.h` | 两条 head128 路径的 kernel 入口签名，体现「普通 kernel 只特化 `D_QK`」与「small_topk kernel 额外特化 `SparseAttnFwdMode`」的差异。 |

## 4. 核心概念与源码讲解

本讲按四个最小模块展开：**接口校验 → required_features 构造 → FwdImplBase 派发框架与四个实现 → small_topk 选择阈值与 fallback**。

### 4.1 接口校验：从 Python 到 C++ 的防御层

#### 4.1.1 概念说明

`flash_mla_sparse_fwd` 在 Python 端几乎不做校验，只把参数原样转发给 C++：

[flash_mla/flash_mla_interface.py:208-210](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/flash_mla/flash_mla_interface.py#L208-L210) —— Python 端把 7 个参数直接喂给 `flash_mla_cuda.sparse_prefill_fwd`。

真正的校验、装配、派发全部发生在 C++ 接口函数 `sparse_attn_prefill_interface` 里。它由 pybind 绑定暴露：

[csrc/api/api.cpp:12](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/api.cpp#L12) —— `m.def("sparse_prefill_fwd", &sparse_attn_prefill_interface);` 把接口函数注册成 Python 绑定 `sparse_prefill_fwd`。

这种「Python 薄壳 + C++ 重活」的分工是 FlashMLA 一贯风格（见 u2-l1 的调用链全景）：Python 只负责易用的参数表达，C++ 负责所有与硬件、内存布局、模板特化相关的硬约束。

#### 4.1.2 核心流程

`sparse_attn_prefill_interface` 的执行可以拆成四个阶段：

```text
1. 架构门禁   : 只放行 SM90a / SM100f，否则 TORCH_CHECK 失败
2. 张量校验   : NDIM / device / dtype / shape / last-dim contiguous 五道防线
3. 输出分配   : 申请 out[s_q,h_q,d_v] / lse[s_q,h_q] / max_logits[s_q,h_q]
4. 参数装配   : 把指针、stride、scale、stream 填入 SparseAttnFwdParams
（之后进入 4.2/4.3 的 required_features 构造与实现派发）
```

其中第 2 步是关键防御层：所有 kernel 都假定张量满足特定形状与布局，一旦违反就会越界访问或读到垃圾数据，因此接口层必须把这些假设全部显式断言出来。

#### 4.1.3 源码精读

**架构门禁**：构造 `Arch` 对象（构造时调用 `at::cuda::getCurrentDeviceProperties()` 查询当前 GPU），缓存 `major/minor/num_sms`，然后用两个判定函数筛选：

[csrc/api/sparse_fwd.h:112-115](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/sparse_fwd.h#L112-L115) —— `is_sm90a || is_sm100f` 才放行，否则报错。`Arch::is_sm90a()` 判 `major==9 && minor==0`，`is_sm100f()` 判 `major==10`（见 [csrc/api/common.h:34-40](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/common.h#L34-L40)）。

**张量校验五道防线**（以 `q` 为例，其余张量同构）：

[csrc/api/sparse_fwd.h:117-156](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/sparse_fwd.h#L117-L156) —— 依次做：

1. `KU_CHECK_NDIM(q, 3)`：`q` 必须是 3 维 `[s_q, h_q, d_qk]`。
2. `KU_CHECK_DEVICE(q)`：与其他张量在同一 GPU 上。
3. `KU_CHECK_DTYPE(q, torch::kBFloat16)`：`q`/`kv` 必须 bf16，`indices` 必须 int32，`attn_sink`/`topk_length` 必须是指定类型。
4. `KU_CHECK_SHAPE(q, s_q, h_q, d_qk)`：形状与从 `size()` 读出的维度一致，交叉校验 `q`/`kv`/`indices` 的维度自洽（如 `indices` 的第 2 维 `topk`、`h_kv` 必须与 `kv` 对齐）。
5. `KU_CHECK_LAST_DIM_CONTIGUOUS(q)`：最后一维内存连续（kernel 用 TMA 搬运最后一维，必须连续）。

中间还夹了两个硬约束：

[csrc/api/sparse_fwd.h:131-132](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/sparse_fwd.h#L131-L132) —— `d_qk` 只能是 576 或 512（对应 MLA 的两种 head_dim，见 u1-l1），`d_v` 只能是 512。

**输出分配与参数装配**：

[csrc/api/sparse_fwd.h:162-189](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/sparse_fwd.h#L162-L189) —— 申请三个输出张量后，把所有指针、stride、scale 填进 `SparseAttnFwdParams`。注意两处细节：

- `sm_scale * LOG_2_E`：kernel 内部用 base-2 的 `exp2f` 计算（更快），所以把外部传入的 base-e scale 预乘 \(\ln 2\) 的倒数 \(1/\ln 2 = \log_2 e\)，存为 `sm_scale_div_log2`（此处命名为 `sm_scale * LOG_2_E`，其中 `LOG_2_E = 1.44269504f`，见 [csrc/api/common.h:12](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/common.h#L12)）。这与 u4-l2、u6-l2 讲过的「内部 base-2、输出转 base-e」约定一致。
- `int64_stride_to_int(...)`：PyTorch 的 stride 是 `int64_t`，而 prefill 路径的 `SparseAttnFwdParams` 用 `int` 存 stride（见 4.2），收窄时若越过 \(2^{31}-1\) 即报错（[csrc/api/common.h:44-49](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/common.h#L44-L49)），防止静默截断。

`SparseAttnFwdParams` 的字段含义见 [csrc/params.h:145-168](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/params.h#L145-L168)：它是 prefill 专用的纯数据结构，无 batch 维、无 split-KV 缓冲（prefill 不切 KV），只含 `s_q/s_kv/h_q/h_kv/d_qk/d_v/topk`、两个 scale、输入输出指针、stride、`num_sm` 与 `stream`。

#### 4.1.4 代码实践

**实践目标**：用源码阅读验证「Python 薄壳 + C++ 重活」的分工，并定位校验失败时的报错路径。

**操作步骤**：

1. 打开 [flash_mla/flash_mla_interface.py:176-211](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/flash_mla/flash_mla_interface.py#L176-L211)，确认 `flash_mla_sparse_fwd` 的函数体只有一行 `flash_mla_cuda.sparse_prefill_fwd(...)`，没有任何 `assert` 或形状检查。
2. 打开 [csrc/api/sparse_fwd.h:117-156](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/sparse_fwd.h#L117-L156)，数一下一共有多少个 `KU_CHECK_*` / `TORCH_CHECK` 调用。
3. 假设用户传入一个 `dtype=float32` 的 `q`，跟踪它会命中第几行、报什么错（应是 `KU_CHECK_DTYPE(q, torch::kBFloat16)` 这一行）。

**需要观察的现象**：Python 端对错误输入完全不设防；所有校验都依赖 C++ 侧的 `TORCH_CHECK` 抛出可读异常。

**预期结果**：`flash_mla_sparse_fwd` 函数体仅一行转发；C++ 侧 `sparse_attn_prefill_interface` 在校验阶段有约 20+ 个断言点。错误信息会以 `torch` 异常形式冒泡到 Python，附带 `KU_CHECK_*` 自带的描述串。

> 待本地验证：在装有 SM90/SM100 GPU 的环境里，故意传一个错的 dtype 跑 `flash_mla_sparse_fwd`，观察异常文本是否与你在源码里定位的行一致。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `q`/`kv` 只要求「最后一维连续」而不是「整个张量连续」？

**参考答案**：kernel 用 TMA 沿最后一维（`d_qk` 维）搬运数据，只要这一维内存连续即可正确装载；允许前两维有任意 stride，能给上层（如转置视图）更大的布局自由度，不必强制 `contiguous()` 拷贝。

**练习 2**：`sm_scale * LOG_2_E` 这一行的目的是什么？如果删掉会怎样？

**参考答案**：把 base-e 的 softmax scale 换算成 base-2，因为 kernel 内部用 `exp2f` 而非 `expf`（`exp2f` 在 Hopper/Blackwell 上更高效）。删掉的话，所有 attention 分数会被错误地按 base-e scale 放进 base-2 的 `exp2f`，相当于多乘了一个 \(\log_2 e\) 因子，输出数值会系统性偏大、与参考实现不符。

### 4.2 required_features 构造与 FwdFeatures 枚举

#### 4.2.1 概念说明

`sparse_attn_prefill_interface` 在装配完 `params` 后，并不直接调 kernel，而是先构造一个**需求能力集合** `required_features`。这个集合描述「本次调用需要哪些能力」，随后被用来在多个实现类之间做子集校验派发。

能力集合来自一个枚举：

[csrc/api/sparse_fwd.h:12-22](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/sparse_fwd.h#L12-L22) —— `FwdFeatures` 枚举定义了 7 个能力位。

| 枚举值 | 含义 | 何时被需求 |
|--------|------|-----------|
| `HEAD_64` | query 64 头 | `h_q == 64` |
| `HEAD_128` | query 128 头 | `h_q == 128` |
| `HEAD_DIM_576` | `d_qk == 576`（含 64 维 RoPE 的 MLA） | `d_qk == 576` |
| `HEAD_DIM_512` | `d_qk == 512`（纯 NoPE 段） | `d_qk == 512` |
| `ATTN_SINK` | 启用 attention sink 缩放 | `attn_sink.has_value()` |
| `SINK_LSE` | lse 含 sink 的变体 | **本接口从不需求**（见下方说明） |
| `TOPK_LENGTH` | 启用 `topk_length` 截断掩码 | `topk_length.has_value()` |

#### 4.2.2 核心流程

构造 `required_features` 的逻辑是一段「运行时张量属性 → 能力枚举」的翻译：

```text
h_q   == 64/128  -->  push HEAD_64 / HEAD_128   (其他值直接 TORCH_CHECK 失败)
d_qk  == 512/576 -->  push HEAD_DIM_512 / HEAD_DIM_576
attn_sink.has_value()      -->  push ATTN_SINK
topk_length.has_value()    -->  push TOPK_LENGTH
```

注意：`SINK_LSE` 虽然出现在枚举里，且四个实现类都声明支持它，但**当前 `sparse_attn_prefill_interface` 从不把它 push 进 `required_features`**。它是「所有实现都支持、但本接口从不主动需求」的保留能力位。因此对 prefill 派发而言，`SINK_LSE` 实际上不参与选择逻辑——但它仍出现在每个实现的 `DECLARE_SUPPORTED_FEATURES` 列表里（见 4.3.3），属于「无害的冗余声明」。

#### 4.2.3 源码精读

[csrc/api/sparse_fwd.h:191-211](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/sparse_fwd.h#L191-L211) —— 这段代码用一连串 `if` 把运行时值翻译成 `FwdFeatures`。每个 `if` 都带一个 `else { TORCH_CHECK(false, ...) }` 兜底，所以非法的 `h_q`/`d_qk` 会在这里被拦下，而不是拖到 kernel 里崩溃。

可选能力（`ATTN_SINK`、`TOPK_LENGTH`）只在对应张量「有值」时才 push——这呼应 u2-l2 讲过的「可空张量用 `nullptr` 表示禁用」约定：能力不存在等价于该字段为 `nullptr`，kernel 会据此编译期特化出无分支路径。

#### 4.2.4 代码实践

**实践目标**：把运行时输入映射到 `required_features` 集合，体会「需求集合」是如何被构造的。

**操作步骤**：对下面三组输入，写出它们各自产生的 `required_features`：

| 输入 | `required_features` |
|------|---------------------|
| `h_q=128, d_qk=576, attn_sink=None, topk_length=None` | 待填 |
| `h_q=64, d_qk=512, attn_sink=tensor, topk_length=tensor` | 待填 |
| `h_q=128, d_qk=512, attn_sink=tensor, topk_length=None` | 待填 |

**预期结果**：

1. `{HEAD_128, HEAD_DIM_576}`
2. `{HEAD_64, HEAD_DIM_512, ATTN_SINK, TOPK_LENGTH}`
3. `{HEAD_128, HEAD_DIM_512, ATTN_SINK}`

三组都没有 `SINK_LSE`，验证它不参与本接口的需求构造。

#### 4.2.5 小练习与答案

**练习**：如果用户传 `h_q=96`，会在哪一行、报什么错？

**参考答案**：在 [sparse_fwd.h:196-198](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/sparse_fwd.h#L196-L198) 命中 `TORCH_CHECK(false, "Unsupported h_q: ", h_q);`，抛出带 `Unsupported h_q: 96` 字样的异常。`96` 既不是 64 也不是 128，没有对应的能力枚举值，也没有对应的 kernel 特化，所以必须在接口层显式拒绝。

### 4.3 FwdImplBase 派发框架与四个实现类

#### 4.3.1 概念说明

有了 `required_features`，下一步是「选一个能服务它的实现」。prefill 路径把这套机制架在 u2-l4 讲过的通用 `ImplBase` 模板上：

[csrc/api/sparse_fwd.h:24-27](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/sparse_fwd.h#L24-L27) —— `FwdImplBase` 就是 `ImplBase<SparseAttnFwdParams, FwdFeatures>` 的别名。把「参数类型」和「能力枚举类型」作为模板参数传入，复用 `ImplBase` 的全部校验/诊断逻辑。

随后定义了**四个具体实现类**，每个对应一条 kernel 路径，并用 `DECLARE_SUPPORTED_FEATURES` 声明自己支持哪些能力。

#### 4.3.2 核心流程

派发的总思路是「显式路由表 + feature 子集校验安全网」：

```text
按 arch × h_q 选定一个（或两个候选）实现类
   --> ImplBase::run(params, required_features)
        --> check_if_all_features_are_supported_and_abort(required_features)  // 安全网
        --> run_(params, required_features)                                   // 真正启动 kernel
```

其中 `run` 是 public 入口，`run_` 与 `get_supported_features` 是 protected 纯虚函数——靠访问控制强制「先校验后执行」（u2-l4）。`DECLARE_SUPPORTED_FEATURES` 宏一行声明 `static constexpr` 支持数组并 override `get_supported_features`：

[csrc/api/common.h:144-149](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/common.h#L144-L149) —— 宏展开后是一个 `static constexpr FeatureT features[]` 与一个返回 `std::span` 的 override。

子集校验逻辑：

[csrc/api/common.h:176-190](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/common.h#L176-L190) —— `check_if_all_features_are_supported` 遍历 `required_features`，每个需求都要在支持集合里找到才算通过，返回 `bool`（只判定不报错，供 prefill 的「优先/兜底」选择用，见 4.4）。

#### 4.3.3 源码精读：四个实现类与各自的支持集合

| 实现类 | 路由条件 | 支持集合 \(S\) | `run_` 调用的 kernel |
|--------|---------|----------------|---------------------|
| `Fwd_Sm90_Impl` | `is_sm90a` | HEAD_64, HEAD_128, HEAD_DIM_512, HEAD_DIM_576, ATTN_SINK, SINK_LSE, TOPK_LENGTH | `sm90::fwd::run_fwd_phase1_kernel<HEAD_DIM_QK, HAVE_TOPK_LENGTH>` |
| `Fwd_Sm100_Head64_Impl` | `is_sm100f && h_q==64` | HEAD_64, HEAD_DIM_512, HEAD_DIM_576, ATTN_SINK, SINK_LSE, TOPK_LENGTH | `sm100::fwd::head64::run_fwd_phase1_kernel<HEAD_DIM_QK>` |
| `Fwd_Sm100_Head128_Impl`（普通） | `is_sm100f && h_q==128`（候选之一） | HEAD_128, HEAD_DIM_512, HEAD_DIM_576, ATTN_SINK, SINK_LSE, TOPK_LENGTH | `sm100::fwd::head128::run_fwd_phase1_kernel<HEAD_DIM_QK>` |
| `Fwd_Sm100_Head128_Small_TopK_Impl` | `is_sm100f && h_q==128`（候选之一） | HEAD_128, HEAD_DIM_512, ATTN_SINK, SINK_LSE, TOPK_LENGTH | `sm100::fwd_for_small_topk::head128::run_fwd_for_small_topk_phase1_kernel<Prefill, 512>` |

逐个精读：

[csrc/api/sparse_fwd.h:29-48](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/sparse_fwd.h#L29-L48) —— `Fwd_Sm90_Impl` 是 SM90 上唯一的实现，支持全部 7 个能力（两种头数、两种 head_dim 全覆盖）。它的 `run_` 套了两层 DISPATCH：`DISPATCH_HEAD_DIM` 把 `d_qk` 编译期化、`DISPATCH_BOOLEAN_FLAG` 把「有没有 topk_length」编译期化，最终 4 种组合各实例化一份 SM90 kernel（呼应 u6-l2）。

[csrc/api/sparse_fwd.h:50-66](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/sparse_fwd.h#L50-L66) —— `Fwd_Sm100_Head64_Impl` 只支持 HEAD_64，但两种 head_dim 都支持，所以 `run_` 只需要 `DISPATCH_HEAD_DIM`。

[csrc/api/sparse_fwd.h:68-84](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/sparse_fwd.h#L68-L84) —— `Fwd_Sm100_Head128_Impl`（普通 head128）支持 HEAD_128 与两种 head_dim。

[csrc/api/sparse_fwd.h:86-99](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/sparse_fwd.h#L86-L99) —— `Fwd_Sm100_Head128_Small_TopK_Impl` 是关键差异点：它的支持集合**少了 `HEAD_DIM_576`**，`run_` 直接写死 `run_fwd_for_small_topk_phase1_kernel<SparseAttnFwdMode::Prefill, 512>`，不套任何 DISPATCH——因为这个变体**只服务 `d_qk=512` 的小 topk 场景**（见 u6-l3：small_topk 仅支持 D_QK=512）。

#### 4.3.4 代码实践

**实践目标**：用一个真实的合法请求，跟踪它如何通过 `ImplBase::run` 的安全网。

**操作步骤**：

1. 假设 `is_sm100f && h_q==64 && d_qk==576 && attn_sink=tensor`，`required_features = {HEAD_64, HEAD_DIM_576, ATTN_SINK}`。
2. 路由选中 `Fwd_Sm100_Head64_Impl`，其支持集合为 `{HEAD_64, HEAD_DIM_512, HEAD_DIM_576, ATTN_SINK, SINK_LSE, TOPK_LENGTH}`。
3. 对照 [csrc/api/common.h:176-190](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/common.h#L176-L190) 的子集校验逻辑，逐个需求验证它都在支持集合里。
4. 校验通过后，[csrc/api/common.h:226-229](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/common.h#L226-L229) 的 `run` 调用 `run_`，进入 `DISPATCH_HEAD_DIM` 选 `D_QK=576` 特化。

**需要观察的现象**：安全网只做「只读判定」，不改写参数；它是一道独立于路由逻辑之外的防线——即使路由表写错选了不支持的实现，也会在这里以可读的诊断信息 abort，而不是 silent miscompute。

**预期结果**：上述请求通过校验，最终落到 `sm100::fwd::head64::run_fwd_phase1_kernel<576>`。

#### 4.3.5 小练习与答案

**练习**：为什么 SM90 只用一个 `Fwd_Sm90_Impl`，而 SM100 要拆成三个（head64 / head128 普通 / head128 small_topk）？

**参考答案**：SM90（Hopper）用 WGMMA，head64 与 head128 可以用同一套 seesaw 调度模板覆盖，所以一个实现类 + DISPATCH 就够了。SM100（Blackwell）改用 TMEM/UMMA，head64 与 head128 的 tile/cluster 配置差异大（head128 需要 2-CTA cluster 且 TMEM 容量紧张，需把 Q 拆成 tQ/sQ），必须拆成不同 kernel；而 small_topk 又是针对小 topk 的专门流水优化，三者无法合并。

### 4.4 small_topk 选择阈值与 fallback 分支

#### 4.4.1 概念说明

SM100 head128 是 prefill 路径里**唯一需要在两个实现之间做选择**的场景（其他场景都是一一对应）。这两个候选是：

- **普通** `Fwd_Sm100_Head128_Impl`：两种 head_dim 都支持，但没有针对小 topk 优化。
- **small_topk** `Fwd_Sm100_Head128_Small_TopK_Impl`：用更细的 `B_TOPK=64` 与更深流水专门服务小 topk，但**只支持 `d_qk=512`**（见 u6-l3）。

选择的依据是 `topk` 的大小：小 topk 时 small_topk 变体更快，应该优先用；大 topk 时它反而不优，且它根本不支持 `d_qk=576`，应退回普通实现。

#### 4.4.2 核心流程

选择逻辑可以写成一个布尔表达式。设：

- \(R\) = `required_features`（需求集合）
- \(S_{\text{small}}\) = small_topk 支持集合
- \(S_{\text{regular}}\) = 普通实现支持集合

则 [sparse_fwd.h:223-229](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/sparse_fwd.h#L223-L229) 的判定等价于：

\[
\text{use\_small\_topk} = \big(\text{topk} \le 1280 \;\land\; R \subseteq S_{\text{small}}\big) \;\lor\; \big(R \not\subseteq S_{\text{regular}}\big)
\]

- 第一项是**优先路径**：topk 小且 small_topk 能服务 → 用 small_topk（更快）。
- 第二项是**兜底路径**：若普通实现无法服务该需求，则哪怕 topk 不小也尝试 small_topk，避免直接失败。

由于 \(S_{\text{regular}} = S_{\text{small}} \cup \{\text{HEAD\_DIM\_576}\}\)（普通实现是 small_topk 的严格超集，见 4.3.3 表格），对 head128 的所有合法 \(R\) 而言 \(R \subseteq S_{\text{regular}}\) 恒成立，所以第二项在当前代码里**永远为假**——它是一道防御性的安全网，保证「万一未来普通实现丢失了某个能力，派发器会优先退回 small_topk 而不是让普通实现 abort」。

#### 4.4.3 源码精读

[csrc/api/sparse_fwd.h:213-240](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/sparse_fwd.h#L213-L240) —— 整个派发的顶层结构：

- `is_sm90a` → `Fwd_Sm90_Impl`（一一对应，无选择）。
- `is_sm100f && h_q==64` → `Fwd_Sm100_Head64_Impl`（一一对应）。
- `is_sm100f && h_q==128` → 进入 small_topk vs 普通的二选一。

重点看 head128 的二选一：

[csrc/api/sparse_fwd.h:220-234](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/sparse_fwd.h#L220-L234) —— 先实例化两个候选 `small_topk_impl` 与 `regular_impl`，再用上面那个布尔表达式决定 `use_small_topk_impl`，最后据此调 `.run()`。注意两个候选都只构造、未必都调用——未被选中的那个只浪费一次构造（很便宜），但不启动 kernel。

把这个逻辑展开成对 `(d_qk, topk)` 的决策表（固定 `h_q=128, is_sm100f`）：

| `d_qk` | `topk` | 第一项 `topk≤1280 ∧ R⊆S_small` | 第二项 `R⊄S_regular` | 结果 |
|--------|--------|-------------------------------|----------------------|------|
| 576 | 任意 | 假（small_topk 不支持 576） | 假（regular 支持 576） | **普通** |
| 512 | ≤1280 | 真 | 假 | **small_topk** |
| 512 | >1280 | 假（topk 太大） | 假 | **普通** |

这恰好对应三个直觉：576 必走普通；512 且 topk 小走 small_topk；512 且 topk 大走普通。

**阈值 1280 的来源**：small_topk 变体的流水缓冲与 `B_TOPK=64` 是为「每个 query 关注的 token 数较少」量身设计的（见 u6-l3 的 prefill 4 / decode 3 层流水）。当 topk 超过 1280，这种细粒度设计反而不如普通实现高效，故以 1280 为分界。这个魔数直接硬编码在接口里，没有做成可配置参数。

**为什么 small_topk kernel 同时能服务 decode**：本讲只看 prefill 入口（`SparseAttnFwdMode::Prefill`），但同一个 kernel 在 decode 侧被复用——对照 [csrc/api/sparse_decode.h:156-181](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/sparse_decode.h#L156-L181)：`Decode_Sm100_Head128_Impl::run_` 调的是同一个 `run_fwd_for_small_topk_phase1_kernel`，只是模板实参换成 `SparseAttnFwdMode::DecodeWithSplitKV`。这正是 u6-l3 讲过的「用 `SparseAttnFwdMode` 把 prefill 与 decode 编进同一份 kernel」在接口层的体现——`SparseFwdArgT<FWD_MODE>` 类型别名（[csrc/params.h:176-180](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/params.h#L176-L180)）让同一函数签名在 prefill 吃 `SparseAttnFwdParams`、在 decode 吃 `SparseAttnDecodeParams`。

#### 4.4.4 代码实践

**实践目标**：画出完整的实现选择决策树，并解释 `!regular_impl.check_if_all_features_are_supported(...)` 兜底分支的作用。这是本讲规格指定的实践任务。

**操作步骤**：

1. 阅读 [csrc/api/sparse_fwd.h:213-240](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/sparse_fwd.h#L213-L240)，把 `(arch, h_q, d_qk, topk, required_features)` 五元组映射到具体实现。
2. 画出如下决策树（请读者自行补全箭头终点）：

```text
                        [入口]
                          |
              +----- SM90a? -----+
             是                  否
              |                   |
        Fwd_Sm90_Impl      +-- SM100f? --+
                           是            否 → TORCH_CHECK 失败
                            |
                   +--- h_q == 64? ---+
                  是                  否
                   |                   |
          Fwd_Sm100_Head64_Impl   h_q==128?
                                     |
                       +--- d_qk == 576? ---+
                      是                    否
                       |                     |
                  Fwd_Sm100_Head128    topk <= 1280 ?
                  (普通，因 small          |
                  _topk 不支持 576)   +--- 是 ---+--- 否 ---+
                                      |         |
                                Fwd_Sm100_      Fwd_Sm100_
                                Head128_        Head128_Impl
                                Small_TopK      (普通)
                                _Impl
```

3. 用 `tests/test_flash_mla_sparse_prefill.py` 里的真实 topk 取值验证你的决策树。该测试用了这些 topk 值（见 [tests/test_flash_mla_sparse_prefill.py:69-85](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/tests/test_flash_mla_sparse_prefill.py#L69-L85) 与 [tests/test_flash_mla_sparse_prefill.py:113-144](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/tests/test_flash_mla_sparse_prefill.py#L113-L144)）：`128, 256, 384, 512, 2048, 4096, 8192`。

**需要观察的现象**：把每个 topk 套进决策树，确认 `h_q=128, d_qk=512` 时 topk ∈ {128,256,384,512} 落到 small_topk，topk ∈ {2048,4096,8192} 落到普通；而 `d_qk=576` 时一律落到普通。

**预期结果**：

| (h_q, d_qk, topk) | 实际命中的实现 |
|-------------------|----------------|
| (128, 512, 512) | `Fwd_Sm100_Head128_Small_TopK_Impl` |
| (128, 512, 2048) | `Fwd_Sm100_Head128_Impl` |
| (128, 576, 512) | `Fwd_Sm100_Head128_Impl` |
| (128, 576, 8192) | `Fwd_Sm100_Head128_Impl` |
| (64, *, *) | `Fwd_Sm100_Head64_Impl` |

**关于兜底分支的解释**：`!regular_impl.check_if_all_features_are_supported(required_features)` 这一项，在当前 head128 的合法输入下永远为假（因为普通实现的支持集合是 small_topk 的超集，对所有合法 \(R\) 都成立 \(R \subseteq S_{\text{regular}}\)）。它的作用是**防御性安全网**：一旦未来有人从普通实现里移除某个能力（或新增一个只被 small_topk 支持的能力），派发器会自动退回 small_topk 尝试服务，而不是让普通实现的 `run` 在 `check_if_all_features_are_supported_and_abort` 里抛异常崩溃。换言之，它把「选择逻辑写错」的后果从「硬崩溃」降级为「用一个可能次优但仍正确的实现」。这与 `ImplBase::run` 内部的那道 abort 安全网形成两层独立保护：路由层尽量选对，选错时框架层兜底。

> 待本地验证：在 SM100 GPU 上跑 `tests/test_flash_mla_sparse_prefill.py`，用 `nsys`/`kernelkit` 观察 `h_q=128, d_qk=512, topk=512` 与 `topk=2048` 两次调用启动的 kernel 名字是否分别落在 `sparse_attn_fwd_for_small_topk` 与普通 `sparse_attn_fwd`。

#### 4.4.5 小练习与答案

**练习 1**：假设未来 Small_TopK 实现新增了对 `HEAD_DIM_576` 的支持，决策表会怎么变？

**参考答案**：此时 `d_qk=576` 那一行的第一项 `R ⊆ S_small` 可能变真——若 `topk≤1280` 则 576 也会走 small_topk，普通实现只在 `topk>1280` 时被选中。决策表会更偏向 small_topk。

**练习 2**：为什么兜底分支用 `check_if_all_features_are_supported`（只判定）而不是 `..._and_abort`（判定失败就抛异常）？

**参考答案**：兜底分支的语义是「尝试退回 small_topk」，它需要的是一个布尔结果来驱动 `if`，而不是在失败时直接崩溃。如果用 `_and_abort`，那么「普通实现不支持」的情况会直接抛异常，违背了「优先用 small_topk 兜底」的设计意图。`_and_abort` 留给最终 `.run()` 调用——那是真正「无路可走」时才该崩溃的地方。

## 5. 综合实践

**任务**：模拟一次完整的 `sparse_attn_prefill_interface` 派发决策，把本讲四个模块串起来。

给定下面这个调用（假设运行在 SM100f / B200 上）：

```python
flash_mla.flash_mla_sparse_fwd(
    q,         # shape [s_q=213, h_q=128, d_qk=512], bf16
    kv,        # shape [s_kv=1840, h_kv=1, d_qk=512], bf16
    indices,   # shape [213, 1, 256], int32
    sm_scale=0.005,
    d_v=512,
    attn_sink=sink_tensor,    # [128], float32
    topk_length=None,
)
```

请按顺序完成：

1. **校验**：对照 4.1，列出这次调用会通过哪些 `KU_CHECK_*`（无需运行，只列检查项）。
2. **参数装配**：写出 `SparseAttnFwdParams` 里 `sm_scale_div_log2` 的值（即 `0.005 * LOG_2_E`）。
3. **required_features**：写出本次的 `required_features` 集合。
4. **派发决策**：套用 4.4 的决策树，判定最终命中的实现类，以及该实现 `run_` 里会走到哪个 kernel 模板实参。
5. **兜底分析**：解释为什么这次调用里 `!regular_impl.check_if_all_features_are_supported(...)` 这一项为假，从而不影响选择。

**参考答案**：

1. 会通过：`KU_CHECK_NDIM(q/kv/indices, 3)`、`KU_CHECK_NDIM(attn_sink, 1)`、device/dtype/shape/last-dim-contiguous 全套；`d_qk==512`、`d_v==512` 两个硬约束也通过。
2. `sm_scale_div_log2 = 0.005 * 1.44269504 ≈ 0.0072135`。
3. `required_features = {HEAD_128, HEAD_DIM_512, ATTN_SINK}`（无 TOPK_LENGTH，且本接口从不加 SINK_LSE）。
4. `is_sm100f && h_q==128 && d_qk==512 && topk=256 ≤ 1280` → 命中 `Fwd_Sm100_Head128_Small_TopK_Impl`，其 `run_` 调 `run_fwd_for_small_topk_phase1_kernel<SparseAttnFwdMode::Prefill, 512>`。
5. 因为 \(R=\{\text{HEAD\_128}, \text{HEAD\_DIM\_512}, \text{ATTN\_SINK}\}\) 是普通实现支持集合的子集（普通实现支持这三个），所以 `regular_impl.check_if_all_features_are_supported(R)` 为真，取反为假——兜底项不触发，选择完全由第一项 `topk≤1280 ∧ R⊆S_small` 决定。

## 6. 本讲小结

- `sparse_attn_prefill_interface` 是 sparse prefill 的「校验 + 装配 + 派发」中枢：架构门禁 → 张量五道校验 → 输出分配 → `SparseAttnFwdParams` 装配 → 构造 `required_features` → 按架构/头数路由实现 → `ImplBase::run` 安全网校验后启动 kernel。
- `FwdFeatures` 有 7 个能力位；接口从 `h_q`/`d_qk`/`attn_sink`/`topk_length` 构造需求集合 `R`；`SINK_LSE` 虽被所有实现声明支持，但本接口从不需求它。
- prefill 落地为四个实现类：SM90 一个全覆盖实现、SM100 head64、SM100 head128 普通、SM100 head128 small_topk；前三个一一对应路由，唯有 SM100 head128 需要在两个实现间二选一。
- small_topk 是为小 topk 优化的 head128 变体，**只支持 `d_qk=512`**；选择公式为 `use_small_topk = (topk≤1280 ∧ R⊆S_small) ∨ (R⊄S_regular)`，阈值 1280 硬编码在接口。
- 由于普通实现的支持集合是 small_topk 的严格超集，`R⊄S_regular` 这个兜底项在当前合法输入下永远为假，它是防御未来 feature 漂移的安全网；它用「只判定」的 `check_if_all_features_are_supported` 而非「判定即 abort」的版本，以便退回 small_topk 而非崩溃。
- 同一个 small_topk kernel 经 `SparseAttnFwdMode` 模板开关同时服务 prefill 与 SM100 head128 decode，接口层只是用不同模板实参调用。

## 7. 下一步学习建议

- **回到 kernel 内部**：本讲只讲了「选哪个 kernel」。若想理解被选中的 kernel 内部如何算 attention，回到 u6-l2（SM90 sparse prefill phase1）与 u6-l3（SM100 sparse prefill 与 small_topk 变体），重点看 online softmax 主循环与 `SparseAttnFwdMode` 模板开关如何让一份代码兼容 prefill/decode。
- **对照 decode 接口**：阅读 `csrc/api/sparse_decode.h` 的 `sparse_attn_decode_interface`，对比它与本讲的 prefill 接口在「校验项、参数结构（`SparseAttnDecodeParams` vs `SparseAttnFwdParams`）、路由表」上的异同，体会 decode 多出的 split-KV 编排与 `extra_kv` 处理。
- **进入专家层**：Unit 7 将转向基于 CUTLASS 的 dense MHA prefill/backward（SM100），那里会看到更重的 device/kernel/collective 分层；本讲建立的「接口校验 + DISPATCH + ImplBase 派发」心智模型在那里依然适用。
- **扩展练习**：参考 u9-l2 的扩展点讲义，尝试规划「为 sparse prefill 新增一个 `d_qk` 取值」需要改动 `FwdFeatures`、`required_features` 构造、各实现的 `DECLARE_SUPPORTED_FEATURES`、kernel 实例化与 `setup.py` 的哪些地方，把本讲的派发框架当成改动清单的入口。
