# 自定义注意力变体（JIT customize）

## 1. 本讲目标

本讲讲解 FlashInfer 注意力的**最高级扩展点**——`customize_config` 与 `variants` 机制。读完本讲，你应该能够：

1. 说清「默认注意力其实就是一个 customize 变体」这件事，并指出 `DefaultAttention` 是怎么被注入到 kernel 模板里的。
2. 掌握 `gen_customize_*_module` 系列函数的入参含义，特别是 `additional_tensor_names/dtypes`、`additional_scalar_names/dtypes`、`variant_name`、`variant_decl` 这四组参数如何决定生成出的 C++ 代码。
3. 理解 `AttentionVariantBase` 提供的可重写钩子（logits 变换、掩码、m/d 更新、输出变换），并区分 fa2（Ampere）与 fa3（Hopper）两种变体写法。
4. 能够参照 `tests/utils/test_jit_example.py` 自己写一个最小变体（例如自定义 logit 软截断），渲染出 `.inc` 并确认额外参数被正确注入。

本讲是「进阶注意力变体」单元的收官篇，把前面 u3-l3（decode wrapper）、u2-l3（代码生成五步）串起来：你将看到 `customize_config.jinja` 正是 u2-l3 中「Jinja 渲染做类型特化」在注意力这条线上的最完整落地。

## 2. 前置知识

本讲假设你已经掌握以下概念（若不熟请先回看对应讲义）：

- **JIT 三层架构与五步代码生成**（u2-l1、u2-l3）：知道 `gen_*_module` 算 URI → 建生成目录 → 渲染 Jinja → 产生源文件 → `gen_jit_spec` 装配的标准流程。
- **decode/prefill wrapper 的 plan/run**（u3-l3、u3-l4）：知道 wrapper 内部持有一个 `_jit_module`，`run` 时把张量按固定顺序传给 C++ 的 `run` 符号。
- **C++ 模板显式实例化**：FlashInfer 的 attention kernel 都是模板，JIT 阶段会针对具体的 `head_dim`、`posenc`、`Variant`、`Params` 类型生成一份 `.cu` 显式实例化，编译成 `.so`。
- **在线 softmax（online softmax）**：注意力 kernel 里维护三元组 `(m, d, o)`（行最大值、行和、累加输出），逐 KV 块更新。理解这一点才能看懂为什么「变体」要暴露 `REGISTER_M_D_UPDATE` 与 `REGISTER_OUTPUT_TRANSFORM` 钩子。

一个关键直觉先建立起来：**FlashInfer 的 attention kernel 模板本身是「骨架」，而 logits 如何变换、哪些位置被 mask、输出如何归一化，这些「血肉」是由一个叫 `AttentionVariant` 的 C++ 结构体提供的。** 所谓「自定义注意力变体」，就是自己写一个继承自 `AttentionVariantBase` 的结构体，通过 `gen_customize_*_module` 注入到骨架里，让 JIT 为你专门编译一个 kernel。默认的 `DefaultAttention`（带 ALiBi / soft-cap / sliding-window / custom-mask）也走的是同一条路——这是本讲最重要的一句话。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 |
|------|------|
| [flashinfer/jit/attention/modules.py](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/jit/attention/modules.py) | 所有 `gen_*_module` 的家。`gen_customize_*_module` 是本讲主角；`gen_single_decode_module` 等默认函数会委托给它 |
| [flashinfer/jit/attention/utils.py](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/jit/attention/utils.py) | `generate_additional_params`：把名字/类型列表翻译成三段 C++ 代码（字段声明、函数参数、赋值语句） |
| [csrc/batch_decode_customize_config.jinja](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/csrc/batch_decode_customize_config.jinja) | decode 的「配置模板」，渲染出 `Params` 结构体与几个注入宏 |
| [csrc/batch_prefill_customize_config.jinja](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/csrc/batch_prefill_customize_config.jinja) | prefill 的配置模板，比 decode 多出 `RaggedParams`/`PagedParams` 两个结构体 |
| [include/flashinfer/attention/variant_helper.cuh](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/include/flashinfer/attention/variant_helper.cuh) | 定义 `REGISTER_*` 钩子宏与 `AttentionVariantBase` 基类 |
| [include/flashinfer/attention/variants.cuh](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/include/flashinfer/attention/variants.cuh) | 内置的 `DefaultAttention` 变体（fa2 路径） |
| [tests/utils/test_jit_example.py](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/tests/utils/test_jit_example.py) | 官方自定义变体范例集：FlashSigmoid、DumpLogits、自定义 mask 等 |
| [flashinfer/decode.py](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/decode.py) | wrapper 如何接收 `jit_args` 并在 `run` 里自动注入「知名缓冲」 |

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：**4.1 customize_config**（配置模板如何把额外参数焊进 `Params`）、**4.2 variants**（变体结构体可重写的钩子，以及 fa2/fa3 两种写法）、**4.3 自定义示例**（从 `gen_*` 到跑通一次 FlashSigmoid 的完整链路）。

---

### 4.1 customize_config：类型特化与变体注入

#### 4.1.1 概念说明

回顾 u2-l3：`gen_*_module` 的第三步是「渲染 Jinja 做类型特化」。对 activation 这种简单算子，特化的是 dtype；对 attention，特化的维度更多——dtype、head_dim、posenc、是否滑窗、是否 soft-cap，**外加用户自定义的额外参数与变体逻辑**。

`customize_config.jinja` 就是承载这套特化的模板。它最终渲染出一个 `.inc` 文件（例如 `batch_decode_config.inc`），这个 `.inc` 会被同目录下的 `.cu` 源文件 `#include`，从而把「这一组编译期参数」固化进编译单元。它干三件事：

1. 定义一个 `Params` 结构体，里面除了 kernel 标准字段（指针、stride、plan 产物），还把用户声明的额外参数（tensor 指针 + scalar）作为成员塞进去。
2. 定义两个宏 `ADDITIONAL_FUNC_PARAMS` 与 `ADDITIONAL_PARAMS_SETTER`，分别用于扩展 C++ `run` 函数的形参列表、以及把形参拷进 `Params`。
3. 定义一个 `DISPATCH_context` 宏，在原本「按 dtype/mask 派发」的位置插入 `using AttentionVariant = {{ variant_name }}`——这一句就是把变体类型焊进 kernel 模板的关键。

#### 4.1.2 核心流程

一条额外的 tensor/scalar 参数，从 Python 列表走到 C++ `Params` 字段，要经过三处加工：

```text
Python 侧:
  additional_tensor_names = ["maybe_alibi_slopes"]
  additional_tensor_dtypes = ["float"]
        │
        ▼  generate_additional_params()   (utils.py)
三段 C++ 字符串:
  additional_params_decl   →  "float* maybe_alibi_slopes;\n"          （塞进 Params 结构体）
  additional_func_params   →  ", Optional<ffi::Tensor> maybe_alibi_slopes"  （扩展 run 形参）
  additional_params_setter →  "params.maybe_alibi_slopes = maybe_alibi_slopes ? ... : nullptr;"
        │
        ▼  渲染 customize_config.jinja
生成的 .inc:
  struct Params { ... float* maybe_alibi_slopes; ... };
  #define ADDITIONAL_FUNC_PARAMS , Optional<ffi::Tensor> maybe_alibi_slopes
  #define ADDITIONAL_PARAMS_SETTER params.maybe_alibi_slopes = ... ;
  using AttentionVariant = <variant_name>;
```

注意 `maybe_` 前缀有特殊语义：以 `maybe_` 开头的 tensor 会被处理成 `Optional<ffi::Tensor>`（可空），否则是必填的 `ffi::Tensor`。这对应 kernel 内部「某些缓冲可能不存在」的场景（例如 ALiBi 斜率、自定义 mask），后面 4.3 会看到 FlashInfer 还为几个 `maybe_` 名字做了自动注入。

#### 4.1.3 源码精读

先看 `generate_additional_params` 如何把列表翻译成三段字符串。这是整个机制的字幕翻译器：

[flashinfer/jit/attention/utils.py:35-50](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/jit/attention/utils.py#L35-L50) 把每个额外 tensor 拼成 `<dtype>* <name>;`、每个额外 scalar 拼成 `<dtype> <name>;`，串成 `additional_params_decl`——这就是要塞进 `Params` 的成员声明。

[flashinfer/jit/attention/utils.py:51-66](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/jit/attention/utils.py#L51-L66) 生成 `additional_func_params`。关键在 53-57 行的三元判断：名字以 `maybe_` 开头 → `Optional<ffi::Tensor>`，否则 `ffi::Tensor`。scalar 直接拼 `,<dtype> <name>`。

[flashinfer/jit/attention/utils.py:84-97](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/jit/attention/utils.py#L84-L97) 生成 `additional_params_setter`（非 SM90 路径）：`maybe_` 名字做空指针保护（`? static_cast<...>(...data_ptr()) : nullptr`），普通 tensor 直接取 `data_ptr()`，scalar 直接赋值。注意 SM90（fa3）路径走 67-83 行，赋值目标是 `params.additional_params.<name>` 而非 `params.<name>`——这是 fa2/fa3 变体写法不同的根因（见 4.2.4）。

再看渲染出的 `.inc` 长什么样。以 decode 为例：

[csrc/batch_decode_customize_config.jinja:8-14](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/csrc/batch_decode_customize_config.jinja#L8-L14) 把 `{{ additional_func_params }}` 与 `{{ additional_params_setter }}` 物化成两个宏 `ADDITIONAL_FUNC_PARAMS`、`ADDITIONAL_PARAMS_SETTER`，并把 `using AttentionVariant = {{ variant_name }}` 塞进 `DISPATCH_context` 宏——这一句让下游 kernel 模板「看到」用户指定的变体类型。

[csrc/batch_decode_customize_config.jinja:28-60](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/csrc/batch_decode_customize_config.jinja#L28-L60) 定义 `Params` 结构体。第 39 行的 `{{ additional_params_decl }}` 就是用户额外参数的落点；其余字段（`q`、`paged_kv`、`o`、`lse`、plan 产物 `request_indices` 等）是 decode kernel 的标准接口。`get_kv_len` 这类内联方法让变体在构造时能反查序列长度。

[csrc/batch_decode_customize_config.jinja:62](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/csrc/batch_decode_customize_config.jinja#L62) 末尾的 `{{ variant_decl }}` 是变体结构体本身的来源——它通常是一句 `#include<flashinfer/attention/variants.cuh>`（用内置变体），也可以是用户直接贴进来的一大段 `struct ... {}` C++ 代码（自定义变体，见 4.3）。

prefill 的模板同构，但因为 prefill 同时支持 ragged 与 paged 两种 KV 存储，所以定义了两个 Params：

[csrc/batch_prefill_customize_config.jinja:34-85](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/csrc/batch_prefill_customize_config.jinja#L34-L85) 定义 `RaggedParams`，第 49 行同样植入 `{{ additional_params_decl }}`。

[csrc/batch_prefill_customize_config.jinja:87-131](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/csrc/batch_prefill_customize_config.jinja#L87-L131) 定义 `PagedParams`，第 100 行再次植入同一份 `additional_params_decl`——注意同一组额外参数被同时焊进两个结构体，保证 ragged/paged 两条 run 路径都能拿到。

#### 4.1.4 代码实践

**实践目标**：亲眼看到「额外参数列表」如何变成 `.inc` 里的 `Params` 字段与宏，不依赖 GPU、不需要编译。

**操作步骤**（源码阅读 + 离线渲染型实践）：

1. 在仓库根目录打开 Python，仅调用「生成」步骤（`gen_customize_*_module` 内部会 `write_if_different` 把 `.inc` 落盘，**这一步不触发 nvcc**）：

   ```python
   # 示例代码：仅演示生成，不运行 kernel
   import torch
   from flashinfer.jit.attention import gen_customize_single_decode_module

   spec = gen_customize_single_decode_module(
       "demo_softcap_inspect",        # uri
       torch.float16, torch.float16, torch.float16,  # dtype_q/kv/o
       128, 128,                      # head_dim_qk/vo
       [], [],                        # 无额外 tensor
       ["soft_cap"], ["double"],      # 一个额外 scalar：soft_cap
       "DefaultAttention<false, false, false, false>",  # variant_name
       "#include<flashinfer/attention/variants.cuh>",   # variant_decl
   )
   ```

2. 找到生成目录（`FLASHINFER_GEN_SRC_DIR / uri`，可用 `flashinfer show-config` 查看工作区路径），打开其中的 `single_decode_config.inc`。

**需要观察的现象**：生成的 `.inc` 中应出现形如：

```cpp
double soft_cap;                 // ← additional_params_decl 产物
...
#define ADDITIONAL_FUNC_PARAMS , double soft_cap
#define ADDITIONAL_PARAMS_SETTER params.soft_cap = soft_cap;
```

**预期结果**：`soft_cap` 同时出现在 `Params` 结构体、`ADDITIONAL_FUNC_PARAMS`、`ADDITIONAL_PARAMS_SETTER` 三处——这正对应 4.1.2 流程图里的三个落点。若你把 `soft_cap` 改名为 `maybe_soft_cap`（注意它是 scalar 不是 tensor，这里仅做命名实验），重新生成会发现 scalar 不受 `maybe_` 影响（`maybe_` 只对 tensor 生效）。

> 说明：本实践只验证「生成」环节，不调用 `.build_and_load()`，因此无需 GPU 与 nvcc。具体路径与版本子目录以本机 `flashinfer show-config` 为准（参见 u2-l2）。

#### 4.1.5 小练习与答案

**练习 1**：如果把一个 tensor 命名为 `mask_buf`（无 `maybe_` 前缀），生成的 `ADDITIONAL_FUNC_PARAMS` 与 setter 会和命名为 `maybe_mask_buf` 时有何不同？

**参考答案**：无前缀时形参为 `ffi::Tensor mask_buf`（必填），setter 为 `params.mask_buf = static_cast<...>(mask_buf.data_ptr());`；带 `maybe_` 前缀时形参变为 `Optional<ffi::Tensor> maybe_mask_buf`（可空），setter 增加空指针保护 `maybe_mask_buf ? static_cast<...>(maybe_mask_buf.value().data_ptr()) : nullptr;`。

**练习 2**：为什么 prefill 的配置模板要把 `additional_params_decl` 同时植入 `RaggedParams` 和 `PagedParams` 两个结构体？

**参考答案**：因为 prefill 在 `run` 阶段会分叉成 `ragged_run` / `paged_run` 两条路径（见 u3-l4），它们各自实例化不同的 `Params` 结构体。变体逻辑（例如读 `params.sm_scale`）必须对两条路径都成立，所以同一组额外参数必须同时出现在两个结构体里。

---

### 4.2 variants：可重写的注意力钩子

#### 4.2.1 概念说明

`customize_config` 解决了「额外参数怎么传」，但还没解决「注意力数学怎么改」。这部分由 **variant 结构体** 负责。

所有变体都继承自 `AttentionVariantBase`。基类提供了一套「默认行为」（恒等 logits 变换、全 True 掩码、标准 `1/d` 归一化输出），并通过一组 `REGISTER_*` 宏暴露可重写的钩子。子类只需要用同名宏「覆盖」自己关心的那个钩子，其余保持基类默认。这套设计本质上是 **CRTP 风格的静态多态**：钩子是模板特化，编译期就决议好，零运行期开销——这对 bandwidth-bound 的 decode 尤其重要。

可重写的钩子有六个：

| 宏 | 触发时机 | 典型用途 |
|----|---------|---------|
| `REGISTER_QUERY_TRANSFORM` | 读入 Q 之后 | Q 的预处理（如缩放） |
| `REGISTER_KEY_TRANSFORM` | 读入 K 之后 | K 的预处理 |
| `REGISTER_LOGITS_TRANSFORM` | 算出 \(QK^\top\) 之后、softmax 之前 | soft-cap、ALiBi 偏置、FlashSigmoid |
| `REGISTER_LOGITS_MASK` | softmax 之前 | 自定义布尔掩码（因果/滑窗/稀疏） |
| `REGISTER_M_D_UPDATE` | 每个 KV tile 累加前后 | attention sink（往 m 里塞一个 logit 锚点） |
| `REGISTER_OUTPUT_TRANSFORM` | 累加完成、写回之前 | 反归一化、dump logits |

#### 4.2.2 核心流程

一个变体的生命周期（fa2/Ampere 路径）：

```text
每个 (batch, qo_head) 的处理开始
   │
   ▼  Variant(params, batch_idx, smem_ptr)   ← 构造闭包，从 params 读额外参数
   │
   ▼  对每个 KV tile:
   │     QK^T → logits
   │     LogitsTransform(params, logits, batch_idx, qo_idx, kv_idx, qo_head_idx, kv_head_idx)
   │     LogitsMask(params, batch_idx, qo_idx, kv_idx, ...)   ← 返回 bool
   │     update_m_d(params, kv_tile_idx, qo_head_idx, m, d, scale)  ← 可改在线 softmax 状态
   │     exp2 / 累加 o
   │
   ▼  OutputTransform(params, output, batch_idx, qo_idx, qo_head_idx, m, d, scale)
   │
   ▼  写回 o
```

其中在线 softmax 的归一化因子 \(d\) 是每个头逐 tile 累加的 exp2 之和：

\[
d = \sum_{kv} \mathrm{exp2}\!\left(\mathrm{logits}_{kv} \cdot s_{\log 2} - m\right), \qquad
o = \frac{1}{d}\sum_{kv} \mathrm{exp2}(\cdot)\, v_{kv}
\]

`REGISTER_OUTPUT_TRANSFORM` 默认就是乘 `1/d`（即 `math::ptx_rcp(d)`）。FlashInfer 全程用 base-2 的 `exp2/log2`（`math::log2e` 把自然底缩放折算进去），比 `exp/log` 快得多——所以你会看到变体里频繁出现 `sm_scale_log2 = params.sm_scale * math::log2e`。

#### 4.2.3 源码精读

先看钩子宏与基类的定义：

[include/flashinfer/attention/variant_helper.cuh:41-48](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/include/flashinfer/attention/variant_helper.cuh#L41-L48) 定义 `REGISTER_LOGITS_TRANSFORM`：它展开成一个模板成员函数 `LogitsTransform(params, logits, batch_idx, qo_idx, kv_idx, qo_head_idx, kv_head_idx)`，返回变换后的 logits。

[include/flashinfer/attention/variant_helper.cuh:50-56](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/include/flashinfer/attention/variant_helper.cuh#L50-L56) 定义 `REGISTER_LOGITS_MASK`，返回 `bool`，决定某个 `(qo_idx, kv_idx)` 是否参与计算。

[include/flashinfer/attention/variant_helper.cuh:58-64](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/include/flashinfer/attention/variant_helper.cuh#L58-L64) 定义 `REGISTER_M_D_UPDATE`，允许变体在 tile 边界改写在线 softmax 的 `m`（行最大值）与 `d`（行和）——这是 attention sink 实现的关键（往 `m` 里注入一个「锚 logit」）。

[include/flashinfer/attention/variant_helper.cuh:66-73](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/include/flashinfer/attention/variant_helper.cuh#L66-L73) 定义 `REGISTER_OUTPUT_TRANSFORM`，做最后的反归一化。

[include/flashinfer/attention/variant_helper.cuh:75-100](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/include/flashinfer/attention/variant_helper.cuh#L75-L100) `AttentionVariantBase`：注意它已经用各 `REGISTER_*` 宏提供了默认实现——`LogitsTransform` 原样返回、`LogitsMask` 恒为 `true`、`OutputTransform` 默认乘 `1/d`。所以子类「不重写的钩子」自动落到这套默认行为上。这就是为什么 4.3 里的 `FlashSigmoid` 只重写了 `REGISTER_LOGITS_TRANSFORM` 和 `REGISTER_OUTPUT_TRANSFORM` 两个钩子就能工作。

再看内置的 `DefaultAttention`，它是「默认注意力 = 一个变体」的最佳证据：

[include/flashinfer/attention/variants.cuh:31-93](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/include/flashinfer/attention/variants.cuh#L31-L93) 是一个模板 `DefaultAttention<use_custom_mask, use_sliding_window, use_logits_soft_cap, use_alibi>`。它的构造函数从 `params` 读 `sm_scale`、`logits_soft_cap`、`maybe_custom_mask`、`maybe_alibi_slopes` 等字段（这些字段正是默认 wrapper 通过 `additional_*` 注入的）；`REGISTER_LOGITS_TRANSFORM`（67-76 行）按 `if constexpr` 分支处理 ALiBi 偏置与 tanh soft-cap；`REGISTER_LOGITS_MASK`（78-92 行）处理自定义位打包掩码与滑窗。**这四布尔模板参数的取值，正是默认 `gen_*_module` 在拼接 `variant_name` 字符串时根据 `posenc`/`use_sliding_window`/`use_logits_soft_cap` 决定的**——下一节会看到这一点。

#### 4.2.4 fa2 与 fa3 变体写法的区别

FlashInfer 的注意力分两条 backend：fa2（Ampere 及以上，SM80+）与 fa3（Hopper，SM90a）。两条路径的 kernel 骨架不同（fa3 用 `wgmma`/TMA），因此变体写法也不同——这是初学者最容易踩的坑：

- **fa2 变体**：构造函数签名是 `(const Params& params, uint32_t batch_idx, uint8_t* smem_ptr)`，额外参数直接挂在 `params` 上（`params.sm_scale`），因为 4.1 里 `additional_params_setter` 走的是非 SM90 分支，赋值目标是 `params.<name>`。
- **fa3 变体**：构造函数签名是 `(const MainloopParams& params, const BlockCoord& block_coord)`，额外参数挂在 `params.additional_params.<name>` 上（对应 SM90 分支 setter），且需要提供一个 `GetAttentionUpdater()` 工厂返回在线 softmax 更新器。

两种写法的对照范例就在 `test_jit_example.py` 里（见 4.3.3）。**结论：同一个数学变体（如 FlashSigmoid），fa2 与 fa3 要各写一份 struct。**

#### 4.2.5 代码实践

**实践目标**：通过阅读源码，在 `DefaultAttention` 里定位 soft-cap 的数学实现，并理解它为何等价于 `tanh(logits/cap)*cap`。

**操作步骤**（源码阅读型实践）：

1. 打开 [include/flashinfer/attention/variants.cuh:46-56](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/include/flashinfer/attention/variants.cuh#L46-L56)，读构造函数里 `use_logits_soft_cap` 分支：`soft_cap_pre_tanh_scale = params.sm_scale * math::ptx_rcp(params.logits_soft_cap)`，并把 `sm_scale_log2` 设为 `log2e * logits_soft_cap`。
2. 再看 [include/flashinfer/attention/variants.cuh:72-74](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/include/flashinfer/attention/variants.cuh#L72-L74) 的 `REGISTER_LOGITS_TRANSFORM`：`logits = tanh(logits * soft_cap_pre_tanh_scale)`。

**需要观察的现象**：soft-cap 把 logits 写成 `tanh((logits * sm_scale)/cap)`，而后续 `sm_scale_log2` 又乘了 `cap`，二者相乘恰好恢复成标准 soft-cap 公式 \(\mathrm{cap}\cdot\tanh(\mathrm{logits}/\mathrm{cap})\)。

**预期结果**：你能用一句话解释「为什么 `sm_scale` 在 soft-cap 分支里被吸收进 `soft_cap_pre_tanh_scale`，而 `sm_scale_log2` 改用 `cap`」。提示：softmax 的 exp2 缩放与 tanh 前的线性缩放是两次独立的乘法，被拆进了两个常量。

#### 4.2.6 小练习与答案

**练习 1**：`AttentionVariantBase` 已经提供了 `OutputTransform` 的默认实现（乘 `1/d`）。如果一个变体只关心 logits 变换、不想动输出，它需要重写 `REGISTER_OUTPUT_TRANSFORM` 吗？

**参考答案**：不需要。因为基类已经用 `REGISTER_OUTPUT_TRANSFORM` 提供了默认行为，子类不写同名宏就自动继承默认。`FlashSigmoid`（fa2）重写它只是为了把输出原样返回（因为 sigmoid 不是标准 softmax，归一化方式不同）。

**练习 2**：为什么所有变体里看到的缩放都是 `math::log2e` 和 `exp2`，而不是自然底 `e` 和 `exp`？

**参考答案**：GPU 上 `exp2`/`log2` 指令比 `exp`/`log` 快且精度好。FlashInfer 把 softmax 的温度缩放预先折算成 base-2（乘 `math::log2e` 把自然底缩放转成 base-2），全程用 `exp2` 累加，最后再用 `log2` 还原 LSE。这是 flash-attention 系列内核的通用优化。

---

### 4.3 自定义示例：从 gen_* 到跑通一次变体

#### 4.3.1 概念说明

有了 4.1 的参数注入与 4.2 的钩子机制，自定义一个变体就剩「组装」：写一段 `variant_decl`（C++ struct 定义），配上对应的 `additional_*` 列表与 `variant_name`，调用 `gen_customize_*_module(...)` 拿到 `JitSpec`，再 `.build_and_load()` 编译加载成 `jit_module`。

调用这个自定义 kernel 有两种方式：

1. **单次直调（low-level）**：拿到 `jit_module` 后，调用 `single_decode_with_kv_cache_with_jit_module(jit_module, q, k, v, *extra_args)` 或 `single_prefill_with_kv_cache_with_jit_module(...)`。`*extra_args` 会按位置透传给 C++ `run` 符号（紧跟在 `window_left` 之后）。
2. **wrapper 集成**：把生成参数打包成 `jit_args` 元组传给 `BatchDecodeWithPagedKVCacheWrapper(...)` / `BatchPrefillWith*RaggedKVCacheWrapper(...)` 的构造函数，wrapper 内部自动 `gen_customize_*_module(*jit_args).build_and_load()`，并在 `run` 时自动注入若干「知名缓冲」。

`tests/utils/test_jit_example.py` 是官方范例集，本节以其中的 **FlashSigmoid**（用 sigmoid 代替 softmax 的注意力）为主线，因为它同时演示了：额外 scalar 参数（`logits_scale`、`sigmoid_bias`）、fa2 与 fa3 两份变体写法、单次直调与 wrapper 集成两种调用方式。

#### 4.3.2 核心流程

```text
用户侧:
  variant_decl = r""" struct FlashSigmoid : AttentionVariantBase { ... } """   ← 自己写的 C++
  gen_customize_single_prefill_module(
        backend, uri, dtype_q/kv/o, head_dim_qk/vo,
        additional_tensor_names, additional_tensor_dtypes,
        additional_scalar_names, additional_scalar_dtypes,
        "FlashSigmoid", variant_decl,
  ).build_and_load()   → jit_module
        │
        ▼  渲染 customize_config.jinja + kernel_inst.jinja
生成目录内:
  single_prefill_config.inc    ← Params 含额外字段、ADDITIONAL_FUNC_PARAMS 含额外形参
  single_prefill_kernel_mask_{0..3}.cu   ← 显式实例化 SinglePrefill...<..., FlashSigmoid, Params>
  single_prefill.cu / single_prefill_jit_binding.cu   ← 原地拷贝自 CSRC_DIR
        │
        ▼  ninja + nvcc
jit_module.run(q, k, v, tmp, o, lse, mask_mode, layout, window_left, logits_scale, sigmoid_bias)
        │
        ▼  TVM-FFI 透传 *args（logits_scale, sigmoid_bias 即额外 scalar）
```

关键在「显式实例化」这一步：`kernel_inst.jinja` 把变体类型 `FlashSigmoid` 与 `Params` 一起喂给模板，编译器才会为这个特定组合生成机器码。来看实例化模板：

[csrc/single_decode_kernel_inst.jinja:8-11](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/csrc/single_decode_kernel_inst.jinja#L8-L11) 显式实例化 `SingleDecodeWithKVCacheDispatched<head_dim_qk, pos_encoding_mode, variant_name, Params>`——`{{ variant_name }}`（例如 `FlashSigmoid`）就是被焊进模板实参的位置。

[csrc/batch_decode_kernel_inst.jinja:8-11](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a7e40848d69e60609edb6f3b4ec/csrc/batch_decode_kernel_inst.jinja#L8-L11) 同理实例化 `BatchDecodeWithPagedKVCacheDispatched<...>`。

而 C++ 侧 `run` 符号的形参列表，正是用 `ADDITIONAL_FUNC_PARAMS` 宏扩展出来的：

[csrc/single_decode_jit_binding.cu:22-27](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/csrc/single_decode_jit_binding.cu#L22-L27) `single_decode_with_kv_cache(... int64_t window_left ADDITIONAL_FUNC_PARAMS)` 的形参末尾挂着宏——展开后就是 `, double logits_scale, double sigmoid_bias`（以 FlashSigmoid 为例）。这就是为什么 Python 侧 `*args` 必须按 `additional_scalar_names` 的顺序传入。

#### 4.3.3 源码精读：FlashSigmoid 全链路

先看用户写的变体（fa2 路径）：

[tests/utils/test_jit_example.py:82-109](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/tests/utils/test_jit_example.py#L82-L109) 是 `flash_sigmoid_sm80_decl`。要点：
- 继承 `AttentionVariantBase`，并设 `static constexpr bool use_softmax = false`（告诉骨架「我不用标准 softmax 归一化」）。
- 构造函数从 `params` 读 `sigmoid_bias` 与 `logits_scale`（这两个就是额外 scalar），并折算成 base-2：`sigmoid_bias_log2 = params.sigmoid_bias * math::log2e`。
- `REGISTER_LOGITS_TRANSFORM`（101-103 行）把 logits 替换成 `1/(1+exp2(-(logits*scale+bias)))`——即 sigmoid 概率，直接当作注意力权重，**绕过了 softmax**。
- `REGISTER_OUTPUT_TRANSFORM`（105-107 行）原样返回 output，因为 sigmoid 权重已经是概率，不需要再除 `d`。

对比 fa3（Hopper）路径：

[tests/utils/test_jit_example.py:111-131](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/tests/utils/test_jit_example.py#L111-L131) 是 `flash_sigmoid_sm90_decl`。注意三处不同：
- 构造函数签名是 `(const MainloopParams& params, const BlockCoord& block_coord)`（fa3 风格）。
- 额外参数挂在 `params.additional_params.logits_scale`（fa3 风格，对应 4.1 提到的 SM90 setter 分支）。
- 多了一个 `GetAttentionUpdater()` 工厂方法（123-125 行），返回 fa3 骨架需要的在线更新器。

再看调用链。单次直调用 `gen_customize_single_prefill_module` + `single_prefill_with_kv_cache_with_jit_module`：

[tests/utils/test_jit_example.py:137-166](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/tests/utils/test_jit_example.py#L137-L166) `test_flash_sigmoid`：调用 `gen_customize_single_prefill_module("fa2", uri, ..., [], [], ["logits_scale","sigmoid_bias"], ["double","double"], "FlashSigmoid", variant_decl).build_and_load()` 拿到 `jit_module`，再用 `functools.partial(single_prefill_with_kv_cache_with_jit_module, jit_module)` 包成 `f`，最后 `o = f(q, k, v, logits_scale, sigmoid_bias, mask_mode=...)`——末尾两个 scalar 就是 `*args` 透传。参考实现用 PyTorch 的 `sigmoid` + `einsum` 验证，`torch.testing.assert_close` 通过。

wrapper 集成则把同样的生成参数打包成 `jit_args` 元组：

[tests/utils/test_jit_example.py:235-260](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/tests/utils/test_jit_example.py#L235-L260) `test_batch_decode_flash_sigmoid` 把 `jit_args` 传给 `BatchDecodeWithPagedKVCacheWrapper(..., jit_args=jit_args, backend="fa2")`。注意 `jit_args` 的位置顺序与 `gen_customize_batch_decode_module` 形参严格对应：`(uri, dtype_q, dtype_kv, dtype_o, idtype, head_dim_qk, head_dim_vo, additional_tensor_names, additional_tensor_dtypes, additional_scalar_names, additional_scalar_dtypes, variant_name, variant_decl)`。

[flashinfer/decode.py:792-809](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/decode.py#L792-L809) 是 wrapper 接收 `jit_args` 的核心：若 `use_tensor_cores` 则走 prefill 模块（`gen_customize_batch_prefill_module`），否则走 decode 模块（`gen_customize_batch_decode_module`），都立即 `.build_and_load()`；并把 `jit_args[7]`（即 `additional_tensor_names`）缓存为 `self._jit_additional_tensor_names`，供 `run` 时自动注入缓冲用。

[tests/utils/test_jit_example.py:313](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/tests/utils/test_jit_example.py#L313) 调用 `wrapper.run(q, (k_cache, v_cache), logits_scale, sigmoid_bias)`——额外 scalar 直接追加在位置参数末尾。

最后看一个对工程化很关键的细节——「知名缓冲自动注入」（Issue #1044）。当额外 tensor 名字命中 `maybe_custom_mask` / `maybe_mask_indptr` / `maybe_alibi_slopes` 这类内置约定时，wrapper 会在 `run` 里**自动填入内部缓冲**，用户不必手动传：

[flashinfer/decode.py:1988-1999](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/decode.py#L1988-L1999) `run` 里若启用了 `jit_module`，则调用 `prepare_jit_additional_args`，对每个 `_jit_additional_tensor_names` 中的名字：命中 `known_bufs`（如 `maybe_alibi_slopes`）就用内部缓冲，否则从用户 `*args` 里顺序消费。

[flashinfer/utils.py:1304-1317](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/utils.py#L1304-L1317) `prepare_jit_additional_args` 的契约：知名名字 → 内部缓冲（值可以是惰性 callable）；其余名字 → 顺序消费用户参数；用户参数有剩余则追加到末尾。

[tests/utils/test_jit_example.py:619-680](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/tests/utils/test_jit_example.py#L619-L680) `test_batch_prefill_jit_wellknown_mask_buffers` 演示了这点：变体声明额外 tensor 名为 `["maybe_custom_mask", "maybe_mask_indptr"]`，但 `wrapper.run(q, kv_cache)` **完全不传这两个缓冲**——它们由 plan 阶段提供的 `custom_mask=` 自动打包并注入（参见 677 行 plan 时传 `custom_mask=custom_mask`，680 行 run 不传）。

#### 4.3.4 代码实践

**实践目标**：参照 `test_flash_sigmoid`，自己写一个最小的「logit 软截断（soft-cap）」变体，渲染 customize_config 并确认生成的 `.inc` 包含该参数；进一步可在 GPU 上验证数值。

**操作步骤**：

1. **写变体（fa2）**。新建一个 Python 脚本，定义如下 `variant_decl`（示例代码，仿照 `variants.cuh` 中的 soft-cap 逻辑，但作为独立变体注入）：

   ```cpp
   // 示例代码：自定义 soft-cap 变体
   struct MySoftCap : AttentionVariantBase {
     static constexpr bool use_softmax = true;
     float sm_scale_log2;
     float pre_tanh_scale;

     template <typename Params>
     __device__ __host__ MySoftCap(const Params& params, uint32_t batch_idx, uint8_t* smem_ptr) {
       sm_scale_log2 = params.sm_scale * math::log2e;
       pre_tanh_scale = params.sm_scale * math::ptx_rcp(params.soft_cap);
     }

     REGISTER_LOGITS_TRANSFORM(params, logits, batch_idx, qo_idx, kv_idx, qo_head_idx, kv_head_idx, {
       return float(math::tanh(logits * pre_tanh_scale));
     });
   };
   ```
   这里 `soft_cap` 与 `sm_scale` 都作为额外 scalar 注入。

2. **生成并检查 `.inc`**。调用（与 4.1.4 类似，仅生成不编译）：

   ```python
   # 示例代码
   spec = gen_customize_single_decode_module(
       "my_softcap", torch.float16, torch.float16, torch.float16, 128, 128,
       [], [],                          # 无额外 tensor
       ["soft_cap", "sm_scale"], ["double", "double"],
       "MySoftCap", variant_decl,
   )
   ```
   打开生成目录下的 `single_decode_config.inc`，确认 `Params` 内含 `double soft_cap;` 与 `double sm_scale;`，且 `ADDITIONAL_FUNC_PARAMS` 形如 `, double soft_cap, double sm_scale`。

3. **（可选，需 GPU）编译并运行**。把上面的 `gen_customize_single_decode_module(...).build_and_load()` 拿到 `jit_module`，再用 `single_decode_with_kv_cache_with_jit_module(jit_module, q, k, v, soft_cap, sm_scale)` 调用，与一段 PyTorch 参考（`tanh(QK^T*sm_scale/soft_cap)` 后接标准 softmax）对比。

**需要观察的现象**：
- 步骤 2：`.inc` 的三处落点（字段、`ADDITIONAL_FUNC_PARAMS`、`ADDITIONAL_PARAMS_SETTER`）都出现 `soft_cap`。
- 步骤 3（若运行）：自定义变体的输出与参考实现数值接近；增大 `soft_cap` 时输出趋近普通 softmax，减小 `soft_cap` 时大 logit 被压平。

**预期结果 / 待本地验证**：步骤 2 在纯 CPU 环境即可完成（生成不触发 nvcc）；步骤 3 需要 CUDA GPU 与已安装的 flashinfer，数值容差建议 `rtol=1e-3, atol=1e-3`（参考 `test_single_decode_mask` 的断言），具体精度**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：在 `test_batch_decode_flash_sigmoid` 中，`jit_args` 元组的第 7 个元素（索引 7）是什么？为什么 wrapper 要单独缓存它？

**参考答案**：是 `additional_tensor_names`（额外 tensor 名字列表）。wrapper 把它存为 `self._jit_additional_tensor_names`（decode.py:806），是为了在 `run` 时用 `prepare_jit_additional_args` 判断哪些名字是「知名缓冲」（如 `maybe_alibi_slopes`）需要自动注入、哪些要从用户 `*args` 顺序消费。

**练习 2**：如果你把 `variant_name` 写错（例如写成不存在的 `FooBar`），错误会在哪一步暴露？

**参考答案**：在 `.build_and_load()` 的 **ninja 编译阶段**暴露（链接/编译错误：未定义的类型 `FooBar` 或模板实例化失败）。因为 `variant_name` 只是先被写进 `.inc`/`.cu` 文本，直到 nvcc 编译时才会去解析这个类型名——这与 u2-l1「登记≠编译」一致：生成成功不代表编译成功。

**练习 3**：为什么 `test_batch_prefill_sm90_flash_sigmoid` 要用一份独立的 `flash_sigmoid_sm90_decl`，而不能复用 `flash_sigmoid_sm80_decl`？

**参考答案**：因为 fa3（Hopper）骨架的变体契约不同——构造函数签名是 `(MainloopParams, BlockCoord)`、额外参数挂在 `params.additional_params.*`、且需要 `GetAttentionUpdater()` 工厂。fa2 的 struct 不满足这些契约，强行复用会编译失败或运行错误。这印证了 4.2.4 的结论：同一变体在 fa2/fa3 要各写一份。

## 5. 综合实践

把本讲三个模块串起来，完成一个完整的「自定义注意力变体」小任务：

**任务**：实现一个 **「带 logit 偏置注入 + 软截断」** 的 decode 变体，并通过 wrapper 集成方式调用它。

要求：

1. 写一个 `variant_decl`（fa2），struct 名 `SoftCapWithBias`，继承 `AttentionVariantBase`：
   - 额外 scalar：`soft_cap`（double）、`sm_scale`（double）、`logit_bias`（double，逐请求共享的常数偏置）。
   - 在 `REGISTER_LOGITS_TRANSFORM` 里先做 `logits = logits + logit_bias`，再做 `tanh` 软截断（参照 4.3.4 的 `pre_tanh_scale` 思路）。
2. 打包 `jit_args` 元组（注意 batch_decode 的位置顺序：`uri, dtype_q, dtype_kv, dtype_o, idtype, head_dim_qk, head_dim_vo, additional_tensor_names, additional_tensor_dtypes, additional_scalar_names, additional_scalar_dtypes, variant_name, variant_decl`），传给 `BatchDecodeWithPagedKVCacheWrapper(..., jit_args=jit_args, backend="fa2")`。
3. `plan` 后调用 `wrapper.run(q, (k_cache, v_cache), soft_cap, sm_scale, logit_bias)`，确认三个额外 scalar 按顺序透传。
4. 用 PyTorch 写参考实现：`logits = QK^T*sm_scale + logit_bias; logits = tanh(logits/soft_cap)*soft_cap; o = softmax(logits) @ V`，与 kernel 输出对比。

**验收标准**：
- 生成的 `batch_decode_config.inc` 中 `Params` 同时含 `soft_cap/sm_scale/logit_bias` 三个字段。
- `run` 调用成功且数值与参考接近（容差**待本地验证**，建议从 `rtol=1e-3, atol=1e-3` 起调）。
- 能口述「额外 scalar 是如何从 Python `run` 的位置参数，经 `ADDITIONAL_FUNC_PARAMS` 宏，到达 C++ `Params` 字段，最后被变体构造函数读取」的完整链路。

**提示**：若遇到编译错误，先用 4.3.5 练习 2 的思路定位——是 `variant_name` 写错、`variant_decl` 语法有误，还是额外参数顺序不匹配。优先参考 [tests/utils/test_jit_example.py:231-337](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/tests/utils/test_jit_example.py#L231-L337) 的 `test_batch_decode_flash_sigmoid` 作为骨架。

## 6. 本讲小结

- **默认注意力就是一个变体**：`gen_single_decode_module` 等默认函数最终都委托给 `gen_customize_*_module`，用 `variant_name="DefaultAttention<...>"`、`variant_decl="#include<flashinfer/attention/variants.cuh>"`——自定义与内置走的是同一条路。
- **`customize_config.jinja` 三件事**：把额外参数焊进 `Params`（`additional_params_decl`）、扩展 `run` 形参（`ADDITIONAL_FUNC_PARAMS`）、把变体类型焊进 kernel 模板（`using AttentionVariant = {{ variant_name }}`）。
- **`generate_additional_params` 是字幕翻译器**：把名字/类型列表翻译成字段声明、函数形参、赋值语句三段 C++；`maybe_` 前缀让 tensor 变成可空的 `Optional<ffi::Tensor>`。
- **变体 = 一组可重写钩子**：`REGISTER_LOGITS_TRANSFORM/LOGITS_MASK/M_D_UPDATE/OUTPUT_TRANSFORM/...`，基类 `AttentionVariantBase` 提供默认实现，子类只覆盖关心的钩子，编译期静态多态、零运行期开销。
- **fa2 与 fa3 变体契约不同**：fa2 读 `params.X`、构造签名 `(params, batch_idx, smem_ptr)`；fa3 读 `params.additional_params.X`、构造签名 `(MainloopParams, BlockCoord)`、需提供 `GetAttentionUpdater()`——同一数学变体要各写一份。
- **两种调用方式**：单次直调用 `*_with_jit_module(jit_module, q,k,v, *extra_args)`；wrapper 集成用 `jit_args` 元组，且对 `maybe_custom_mask/maybe_alibi_slopes` 等「知名名字」会在 `run` 里经 `prepare_jit_additional_args` 自动注入内部缓冲。

## 7. 下一步学习建议

- **u9-l1（添加新 CUDA 算子全流程）**：本讲的变体是「在已有 kernel 骨架上注入逻辑」，而 u9-l1 讲的是「从零新增一个独立算子」的十步流程，两者互补。
- **u9-l2（TVM-FFI 绑定）**：本讲提到 `*args` 经 TVM-FFI 透传给 C++ `run` 符号，u9-l2 会讲清 `TVM_FFI_DLL_EXPORT_TYPED_FUNC` 与 `Optional<ffi::Tensor>` 的底层机制。
- **u9-l3（Jinja 模板与分发宏）**：若你想深入 `DISPATCH_context` 这类宏如何展开组合参数空间，u9-l3 系统讲解了 `DISPATCH_DTYPE/DISPATCH_MASK_MODE` 等分发宏。
- **继续阅读源码**：精读 [include/flashinfer/attention/variants.cuh](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/include/flashinfer/attention/variants.cuh) 的 `DefaultAttention` 与 [flashinfer/jit/attention/variants.py](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/jit/attention/variants.py) 的 `AttentionSink` 变体（它额外用了 `REGISTER_M_D_UPDATE` 注入 sink 锚点），是把本讲钩子机制吃透的最佳练习。
