# Transformer monkey-patching 集成

## 1. 本讲目标

学完本讲后，你应该能够：

- 理解「monkey-patch（猴子补丁）」即「在运行时替换模块属性」这一机制的原理。
- 说清楚 TileGym 用「非侵入式」方式接入 HuggingFace `transformers` 模型的完整套路。
- 看懂 `monkey_patch.py` 里 `apply_tilegym_kernel_to_*` 系列函数各自替换了哪些点（RoPE / RMSNorm / MLP / Attention / MoE）。
- 理解 `use_cutile` 分支的作用：它如何切换全局后端、并额外打上只有 cuTile 后端才有的融合补丁。
- 理解 `MODEL_TYPE_TO_APPLY_TILEGYM_FN` 表 + `_apply_tilegym_kernel` 如何按模型类型做分发。
- 能解释「为什么 monkey-patch 必须在模型初始化之前调用」。

## 2. 前置知识

在进入本讲前，建议你已经具备以下认知（这些是前面讲义建立的，本讲直接承接）：

- **统一算子接口与分发（u2-l1、u2-l2）**：`tilegym.ops` 里每个算子只是带 `@dispatch` 装饰的 stub，真正实现由分发器在运行时按当前后端查全局注册表 `_REGISTRY` 路由。
- **后端选择（u2-l3）**：`set_backend(name)` 设置进程级当前后端（默认 `cutile`），`_CURRENT_BACKENDS` 是一个全局单值。
- **FMHA 注意力内核（u6-l1）**：`tilegym.ops.fmha` 是分块计算 + 在线 softmax 的 Flash 注意力。

本讲还需要几个 Python 层面的基础概念，先用大白话解释：

| 术语 | 通俗解释 |
|------|----------|
| **HuggingFace `transformers`** | 一个提供了 Llama、Qwen、Gemma 等上百种大模型实现的开源库。每种模型的实现都放在 `transformers.models.<模型名>.modeling_<模型名>` 模块里。 |
| **模块属性** | Python 模块本身是一个对象，模块里定义的类（如 `LlamaRMSNorm`）、函数（如 `apply_rotary_pos_emb`）都是它的「属性」，可以读写。 |
| **monkey-patch（猴子补丁）** | 在运行时把某个模块/类的属性「偷偷换掉」，让别处的代码在不知情的情况下用上新实现。本讲的全部技巧都建立在这上面。 |
| **模型实例化** | 调用 `AutoModel.from_pretrained(...)`（内部 `__init__`）时，`transformers` 会真正去「引用」建模模块里的类来构造每一层。 |

> 一个关键直觉：`transformers` 的 DecoderLayer 在 `__init__` 里写的是 `self.input_layernorm = LlamaRMSNorm(...)`。如果在它 `__init__` 之前，我们把 `LlamaRMSNorm` 这个名字指向了 TileGym 的版本，那么这一层造出来的归一化层就「天然是 TileGym 的」。这就是 monkey-patch 的全部魔法。

## 3. 本讲源码地图

本讲主要围绕一个文件展开，另有两个文件用于佐证「调用时机」：

| 文件 | 作用 |
|------|------|
| `src/tilegym/transformers/monkey_patch.py` | **本讲主角**。定义所有 `apply_tilegym_kernel_to_*` 函数与分发表 `_apply_tilegym_kernel`。 |
| `src/tilegym/ops/ops.py` | 提供 `get_apply_rope_func / get_rms_norm_module / get_swiglu_module / get_fused_swiglu_module` 等工厂算子，是 monkey-patch 取「替换物」的来源。 |
| `src/tilegym/ops/attn_interface.py` | 提供 `get_fmha_interface / get_attention_sink_interface / get_fmha_gemma3_interface` 等注意力工厂，用于注册到 `transformers` 的注意力表。 |
| `src/tilegym/transformers/deepseek2/modeling_deepseek.py` | DeepSeek V2 的 TileGym 替换实现（`tilegym_deepseek_v2_forward`、`DeepseekV2MoETileGym`），被 monkey-patch 引用。 |
| `modeling/transformers/src/tilegym_hf_bench/_cli.py` | 真实推理 CLI，展示「先 patch、后加载模型」的正确调用顺序，是本讲「为何必须在初始化前调用」的佐证。 |

## 4. 核心概念与源码讲解

### 4.1 模块属性替换（monkey-patch 机制）

#### 4.1.1 概念说明

TileGym 的内核（RoPE、RMSNorm、SwiGLU、FMHA、MoE 等）都很高效，但 HuggingFace `transformers` 已经有一整套成熟的、可加载权重的模型实现。重新实现一遍模型的加载与组装代价巨大，也不现实。

于是 TileGym 选择了一条**非侵入式（non-intrusive）**路线——不改 `transformers` 的源码，而是在运行时把它内部的某些**类、函数、方法**替换成 TileGym 版本。`skills/tilegym-monkey-patch-kernels-to-transformers/SKILL.md` 把这条路线说得最清楚：「We will replace certain modules/classes/methods in transformers library … such that at model instantiation, that model's core components will be replaced by TileGym implementations」。

这套替换在 Python 里靠三件事完成：

1. **拿到目标模块对象**：例如 `from transformers.models.llama import modeling_llama`，`modeling_llama` 就是 Llama 建模模块对象。
2. **给模块属性重新赋值**：`modeling_llama.LlamaRMSNorm = <TileGym 的类>`，这样模块里 `LlamaRMSNorm` 这个名字就指向了新类。
3. **替换发生后，任何「新引用」该名字的地方都会拿到新实现**——关键就是「模型实例化」这一刻，`DecoderLayer.__init__` 会引用这些名字来造层。

本讲先讲清这套**机制**，4.2 讲它替换了**哪些点**，4.3 讲 `use_cutile` 分支，4.4 讲**按模型类型分发**。

#### 4.1.2 核心流程

monkey-patch 有三种「替换粒度」，全仓库就这三种组合使用：

```
┌─────────────────────────────────────────────────────────────────┐
│ 粒度 A：替换模块里的「类」                                       │
│   modeling_llama.LlamaRMSNorm = get_rms_norm_module()          │
│   → 生效时机：模型实例化时 __init__ 引用该类                     │
├─────────────────────────────────────────────────────────────────┤
│ 粒度 B：替换类上的「方法」                                       │
│   modeling_deepseek.DeepseekV2Attention.forward = 新函数        │
│   → 生效时机：实例调用 .forward(...) 时（按对象查到的是新方法）  │
├─────────────────────────────────────────────────────────────────┤
│ 粒度 C：往全局字典里「注册」                                     │
│   ALL_ATTENTION_FUNCTIONS["sdpa"] = get_fmha_interface()       │
│   → 生效时机：transformers 按 attn_implementation 查字典取函数  │
└─────────────────────────────────────────────────────────────────┘
```

伪代码概括一次完整 patch 的形态（以 Llama 为骨架）：

```python
# 示例代码（说明形态，非逐字抄录）
from transformers.models.llama import modeling_llama
from transformers.modeling_utils import ALL_ATTENTION_FUNCTIONS

modeling_llama.apply_rotary_pos_emb = get_apply_rope_func(model="llama")  # 粒度 A：替换函数
modeling_llama.LlamaRMSNorm = get_rms_norm_module()                      # 粒度 A：替换类
modeling_llama.LlamaMLP = get_swiglu_module()                            # 粒度 A：替换类
ALL_ATTENTION_FUNCTIONS["sdpa"] = get_fmha_interface()                   # 粒度 C：注册
```

注意三件事：

- **不修改 `transformers` 源码文件**，只在当前进程的内存里改属性，进程结束即还原——这就是「非侵入」。
- **取替换物的 `get_*` 工厂本身也走分发**：例如 `get_rms_norm_module` 是带 `@dispatch` 的算子（见 4.1.3），它返回的「替换类」会随当前后端而变。
- **顺序敏感**：必须先打补丁，再实例化模型（4.4 与综合实践会专门讲原因）。

#### 4.1.3 源码精读

先看 `monkey_patch.py` 顶部的导入，理解「替换物从哪来」：

[src/tilegym/transformers/monkey_patch.py:L9-L19](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/transformers/monkey_patch.py#L9-L19) — 导入 `set_backend` 与一串 `get_*` 工厂，以及 DeepSeek 的 `DeepseekV2MoETileGym` / `tilegym_deepseek_v2_forward`。

这些 `get_*` 工厂都来自 `tilegym.ops`，它们本身就是分发算子。以 RMSNorm 为例：

[src/tilegym/ops/ops.py:L162-L169](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/ops.py#L162-L169) — `get_rms_norm_module` 是带 `@dispatch("get_rms_norm_module")` 的 stub，函数体只 `raise NotImplementedError`。它返回什么由当前后端的实现决定（u4-l3 已讲过 `get_rms_norm_module` 是模块工厂算子，按 model 名返回不同类）。

这正是 monkey-patch 与前几讲分发机制对接的**桥梁**：patch 时 `get_rms_norm_module()` 一次调用，经由 `@dispatch` 查 `_REGISTRY`，拿到当前后端（默认 cuTile）的 RMSNorm 类，赋值给 `modeling_llama.LlamaRMSNorm`。换句话说——**被换进去的「类」本身就是经过分发的 TileGym 实现**。

再看三种粒度的真实代码。粒度 A（替换类/函数）和粒度 C（注册字典）同时出现在 Llama：

[src/tilegym/transformers/monkey_patch.py:L52-L61](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/transformers/monkey_patch.py#L52-L61) — L52-53 替换 `apply_rotary_pos_emb`（粒度 A：函数），L54-55 替换 `LlamaRMSNorm`（粒度 A：类），L56-57 替换 `LlamaMLP`（粒度 A：类），L58-61 注册 `ALL_ATTENTION_FUNCTIONS["sdpa"]`（粒度 C：字典）。

粒度 B（替换类上的方法）出现在 DeepSeek：

[src/tilegym/transformers/monkey_patch.py:L106-L108](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/transformers/monkey_patch.py#L106-L108) — 把 `DeepseekV2Attention.forward` 整个换成 `tilegym_deepseek_v2_forward`（MLA 注意力的 TileGym 实现）。

> 三种粒度「生效时机」不同：粒度 A 在实例化时生效（因为 `__init__` 才引用类名）；粒度 B 在运行时调用 `.forward()` 时生效；粒度 C 在 `transformers` 按 `config.attn_implementation`（如 `"sdpa"`、`"eager"`）查字典时生效。这正是「为什么必须在初始化前打补丁」的根因——晚于实例化，粒度 A 就来不及了。

#### 4.1.4 代码实践

这是一道**源码阅读型实践**（无需下载大模型）。

1. **实践目标**：亲手确认「替换发生在内存、不改 `transformers` 源码文件」。
2. **操作步骤**：
   - 在已安装 `tilegym` 与 `transformers` 的环境里，写一段约 15 行的脚本（见下方）。
   - 在 `apply_tilegym_kernel_to_llama()` 调用前后，分别打印 `modeling_llama.LlamaRMSNorm` 这个对象，看它是否变了。
3. **需要观察的现象**：调用前，`LlamaRMSNorm` 是 `transformers` 原生类；调用后，它变成了 TileGym 的归一化类。整个过程中你没有修改任何 `transformers` 安装目录里的 `.py` 文件。
4. **预期结果**：两行打印的对象不同（类名/模块路径改变）。

```python
# 示例代码：验证 monkey-patch 只改内存属性、不改源文件
from transformers.models.llama import modeling_llama
from tilegym.transformers import apply_tilegym_kernel_to_llama

print("patch 前:", modeling_llama.LlamaRMSNorm)
apply_tilegym_kernel_to_llama(rms_norm=True, rope=False, swiglu=False, attn=False)
print("patch 后:", modeling_llama.LlamaRMSNorm)  # 期望：变成 TileGym 的归一化类
```

5. **如果无法确定运行结果**：在不方便装 GPU 环境时，可改为纯阅读——对照 [src/tilegym/transformers/monkey_patch.py:L54-L55](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/transformers/monkey_patch.py#L54-L55) 理解「`modeling_llama.LlamaRMSNorm = get_rms_norm_module()` 是一次普通赋值」，即只改了进程内属性，标注「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 TileGym 选择 monkey-patch 而不是「fork 一份 transformers 改源码」？

**参考答案**：fork 改源码会让 TileGym 与 `transformers` 的版本升级强绑定，维护成本极高；而 monkey-patch 在运行时只改进程内属性、不动安装的源文件，进程结束即还原，既非侵入又能跟随 `transformers` 升级（只需保证替换点的名字还在）。

**练习 2**：三种替换粒度（类 / 方法 / 字典）的「生效时机」分别是什么？

**参考答案**：类替换在**模型实例化**（`__init__` 引用类名）时生效；方法替换在**运行时调用该方法**时生效；字典注册在 `transformers` **按 `attn_implementation` 查表**时生效。

### 4.2 各模型的替换点

#### 4.2.1 概念说明

一个 Transformer 解码层通常包含这几大计算组件：**位置编码（RoPE）**、**归一化（RMSNorm / LayerNorm）**、**MLP（SwiGLU / GEGLU）**、**注意力（Attention）**，稀疏模型还有 **MoE（混合专家）**。TileGym 针对每个组件都准备了内核，并通过 `apply_tilegym_kernel_to_*` 把它们替换进对应的 `transformers` 建模模块。

不同模型的「替换点」基本对应同一组概念，但名字和细节各异：

- RoPE：各模型里位置编码函数名不同（`apply_rotary_pos_emb` vs DeepSeek 的 `apply_rotary_emb`）。
- RMSNorm：类名不同（`LlamaRMSNorm` / `Qwen2RMSNorm` / `MistralRMSNorm` / `OlmoeRMSNorm` …），且 Gemma3 / Qwen3.5 用「Gemma 风格」归一化（权重视为零、按 `(1+w)·norm(x)` 应用），需要传 `model="gemma3"`。
- MLP：标准 SwiGLU 用 `get_swiglu_module()`，**融合** SwiGLU（把多个线性算子融成一个内核）用 `get_fused_swiglu_module()`，Gemma3 用 GEGLU（GELU 激活）。
- Attention：多数模型注册 `ALL_ATTENTION_FUNCTIONS["sdpa"]`；GPT-OSS 因为用「注意力汇聚槽（attention sink）」而用 `get_attention_sink_interface()`；Gemma3 因为有 soft cap + 滑窗而用 `get_fmha_gemma3_interface()` 并同时注册 `"eager"` 和 `"sdpa"`。
- MoE：只有 DeepSeek V2 / OLMoE 这类稀疏模型才有，把整个 `DeepseekV2Moe` / `OlmoeSparseMoeBlock` 类替换成 TileGym 版。

#### 4.2.2 核心流程

以最典型的 `apply_tilegym_kernel_to_llama` 为骨架，一个函数内部就是「按开关逐项替换」：

```
apply_tilegym_kernel_to_llama(rope, rms_norm, swiglu, attn, use_cutile):
  1. (可选) use_cutile → set_backend("cutile")     # 4.3 详讲
  2. if rope:     modeling_llama.apply_rotary_pos_emb = get_apply_rope_func("llama")
  3. if rms_norm: modeling_llama.LlamaRMSNorm       = get_rms_norm_module()
  4. if swiglu:   modeling_llama.LlamaMLP           = get_swiglu_module()
  5. if attn:     ALL_ATTENTION_FUNCTIONS["sdpa"]   = get_fmha_interface()
```

每个 `if` 对应一个布尔开关，默认全开；调用方可关掉某一项只替换部分组件。`get_*` 工厂返回的「替换物」是 TileGym 的类/函数，背后连着 u2 的分发与 u3-u7 的真实内核。

各模型函数结构一致，差异只在「替换的名字」「用的工厂」和「是否多了 MoE / 特殊注意力」。

#### 4.2.3 源码精读

**Llama（4 个替换点）**——这是本讲实践任务要求你列出的样本：

[src/tilegym/transformers/monkey_patch.py:L24-L61](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/transformers/monkey_patch.py#L24-L61) — 完整的 Llama 替换函数。四个点分别是：

| 开关 | 替换点（行号） | 替换粒度 |
|------|----------------|----------|
| `rope` | `apply_rotary_pos_emb`（L53） | 模块函数 |
| `rms_norm` | `LlamaRMSNorm`（L55） | 模块类 |
| `swiglu` | `LlamaMLP`（L57） | 模块类 |
| `attn` | `ALL_ATTENTION_FUNCTIONS["sdpa"]`（L61） | 字典注册 |

注意 L47 把 `from transformers.models.llama import modeling_llama` 写在**函数体内**（不是文件顶部）——这是刻意延迟导入，避免在不打算 patch Llama 时也无谓地触发 `transformers` 的 Llama 子模块加载。

**DeepSeek V2（5 个点，含 MoE 与方法替换）**：

[src/tilegym/transformers/monkey_patch.py:L100-L115](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/transformers/monkey_patch.py#L100-L115) — 这一段信息量很大：

- `swiglu` 用的是 **`get_fused_swiglu_module()`（融合版）** 而非普通 `get_swiglu_module()`，注释说明这是为 shared experts（每个 token 都跑的共享专家）消除全部 PyTorch 线性算子。
- `attn` 是**方法替换**（粒度 B）：`DeepseekV2Attention.forward = tilegym_deepseek_v2_forward`，对应 MLA（多潜注意力）。
- `moe` 把 `DeepseekV2Moe` 类整体换成 `DeepseekV2MoETileGym`；并做了一段**版本兼容**：`transformers` 5.x 把类名从 `DeepseekV2MoE` 改成了 `DeepseekV2Moe`（小写 `e`），所以用 `hasattr` 两个名字都试着打补丁。

**Gemma3（注意力要注册两个键 + soft cap）**：

[src/tilegym/transformers/monkey_patch.py:L331-L347](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/transformers/monkey_patch.py#L331-L347) — Gemma3 的注意力同时注册到 `"eager"` 和 `"sdpa"` 两个键，并在模块存在 `eager_attention_forward` 时也替换它。原因是 Gemma3 注意力带 soft cap + 滑窗，必须用专门的 `get_fmha_gemma3_interface()`（见 [src/tilegym/ops/attn_interface.py:L456-L560](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/attn_interface.py#L456-L560)，它内部会处理 soft cap 与 `window_size`，并在 `seq_len_q==1` 时切换到 decode 内核）。

**GPT-OSS（用 attention sink 内核）**：

[src/tilegym/transformers/monkey_patch.py:L284-L290](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/transformers/monkey_patch.py#L284-L290) — GPT-OSS 用注意力汇聚槽，因此把 `eager_attention_forward` 换成 `get_attention_sink_interface()`（即 u6-l4 讲的 attention sink 内核）。注意它的 `swiglu` 默认是 **`False`**，因为 GPT-OSS 用带 clamping 与 MXFP4 量化的自定义专家实现。

#### 4.2.4 代码实践

这正是本讲实践任务的第一问。请直接在源码里数出 Llama 的 4 个替换点。

1. **实践目标**：独立列出 `apply_tilegym_kernel_to_llama` 的 4 个替换点及其粒度。
2. **操作步骤**：打开 [src/tilegym/transformers/monkey_patch.py:L52-L61](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/transformers/monkey_patch.py#L52-L61)，逐行对照。
3. **需要观察的现象**：每个 `if <开关>:` 块里都有一行赋值/注册。
4. **预期结果（4 个点）**：

   | 组件 | 替换目标 | 取值工厂 |
   |------|----------|----------|
   | RoPE | `modeling_llama.apply_rotary_pos_emb` | `get_apply_rope_func(model="llama")` |
   | RMSNorm | `modeling_llama.LlamaRMSNorm` | `get_rms_norm_module()` |
   | SwiGLU MLP | `modeling_llama.LlamaMLP` | `get_swiglu_module()` |
   | Attention | `ALL_ATTENTION_FUNCTIONS["sdpa"]` | `get_fmha_interface()` |

5. **延伸（可选）**：对比 DeepSeek V2，指出它比 Llama 多出的两件事——「方法替换 forward」与「MoE 类替换 + 版本兼容」，加深对 4.1 中三种粒度的理解。

#### 4.2.5 小练习与答案

**练习 1**：为什么 DeepSeek V2 的 MLP 用 `get_fused_swiglu_module()`（融合版）而不是普通 `get_swiglu_module()`？

**参考答案**：在 DeepSeek V2 里 `DeepseekV2MLP` 被用作 shared experts，每个 token 都会跑一遍，是性能热点；融合版把 `gate_proj + up_proj + 激活 + down_proj` 多个 PyTorch 线性算子与中间张量物化全部消除，对热点收益最大（普通版仍保留分离的 Linear）。

**练习 2**：GPT-OSS 的 `apply_tilegym_kernel_to_gpt_oss` 为什么把 `swiglu` 默认设为 `False`？

**参考答案**：因为 GPT-OSS 用了带 clamping 与 MXFP4 量化的自定义专家实现，与 TileGym 标准的 SwiGLU 接口不兼容，默认关闭以免破坏正确性。

**练习 3**：`apply_tilegym_kernel_to_llama` 里 `from transformers.models.llama import modeling_llama` 为什么写在函数体内而非文件顶部？

**参考答案**：延迟导入——只在真正要 patch Llama 时才加载 `transformers` 的 Llama 子模块，避免 patch 其他模型时也被无谓地加载（节省启动时间与内存）。

### 4.3 use_cutile 分支：后端切换与额外融合

#### 4.3.1 概念说明

每个 `apply_tilegym_kernel_to_*` 函数都有一个 `use_cutile: bool = False` 参数。它的作用有两层：

1. **第一层（所有模型共有）**：若为真，就调用 `set_backend("cutile")` 把全局当前后端切成 cuTile。因为 4.2 里那些 `get_*` 工厂都走分发，切了后端之后，后续工厂调用返回的就是 cuTile 后端的实现。
2. **第二层（仅 Qwen3.5 / OLMoE / OLMo-3 等少数模型有）**：在 `if use_cutile:` 里**额外**打一批「融合补丁」。这批补丁（融合的 `residual_add + RMSNorm`、融合的双 Q/K 归一化、融合注意力 forward 等）只有 cuTile 后端才有对应内核，所以被放在 `use_cutile` 开关之后，非 cuTile 后端时不会触发。

理解 `use_cutile` 的关键是：**它既是「后端选择」，也是「是否启用更激进的融合」**。默认 `False` 时，TileGym 走当前后端、只替换基础组件；`True` 时切到 cuTile 并替换更多算子。

#### 4.3.2 核心流程

```
apply_tilegym_kernel_to_<模型>(..., use_cutile=False):
  if use_cutile:
      set_backend("cutile")          # 第一层：切后端（每个模型都有）

  # —— 基础替换（rope / rms_norm / swiglu / attn / moe）——
  ... 4.2 讲的那些 ...

  if use_cutile:                      # 第二层：仅部分模型的额外融合
      modeling_<m>.Xxx.forward      = _xxx_forward_tilegym      # 融合注意力 forward
      modeling_<m>.XxxDecoderLayer.forward = _decoder_layer_forward_tilegym  # 融合 residual+norm
      modeling_<m>.XxxRMSNormGated  = <TileGym 融合类>           # 融合门控归一化
      ...
```

`set_backend("cutile")` 的语义：它把进程级单值 `_CURRENT_BACKENDS` 改为 `cutile`（见 u2-l3）。注意 cuTile 是默认后端，所以 `use_cutile=True` 在多数情况下其实是「显式确认用 cuTile」；它真正区别于默认行为的是第二层的额外融合。

#### 4.3.3 源码精读

**第一层——切后端**，每个 `apply_*` 函数开头都有：

[src/tilegym/transformers/monkey_patch.py:L49-L50](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/transformers/monkey_patch.py#L49-L50) — Llama 的 `if use_cutile: set_backend("cutile")`。

`set_backend` 的实现见 [src/tilegym/backend/selector.py:L232-L248](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/backend/selector.py#L232-L248)：它先校验后端在可用列表里（tilecpp 还会额外做 nvcc 校验以「快速失败」），再把 `_CURRENT_BACKENDS` 赋为新值。所以 `use_cutile=True` 等价于「在打补丁前先把进程切到 cuTile」。

**第二层——额外融合补丁**，以 OLMo-3 为例（最完整，含 MLP/Attention/DecoderLayer 三处融合）：

[src/tilegym/transformers/monkey_patch.py:L541-L552](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/transformers/monkey_patch.py#L541-L552) — 在 `if use_cutile:` 里：

- 把 `Olmo3MLP` 换成 `FusedOlmo3MLP`（融合 MLP）；
- 替换 `Olmo3Attention.forward` 为「融合双 Q/K RMSNorm」的 forward；
- 替换 `Olmo3DecoderLayer.forward` 为「融合 residual_add + RMSNorm」的 forward。

这三处都是**方法替换**（粒度 B），且只有 cuTile 后端提供了对应内核实现（这些 `_*_forward_tilegym` 与融合类来自 `src/tilegym/transformers/olmo3/modeling_olmo3.py`）。因此它们被放在 `use_cutile` 之后——非 cuTile 后端既不会切后端、也不会触发这些替换。

Qwen3.5 的 `use_cutile` 分支更复杂，还包括「融合 causal conv1d」「融合 RMSNormGated」等与 gated delta rule 线性注意力相关的融合，见 [src/tilegym/transformers/monkey_patch.py:L216-L237](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/transformers/monkey_patch.py#L216-L237)。

#### 4.3.4 代码实践

1. **实践目标**：理解 `use_cutile` 的两层作用，并能在源码里区分「基础替换」与「use_cutile 专属替换」。
2. **操作步骤**：
   - 打开 OLMo-3 的两个替换区：基础区 [src/tilegym/transformers/monkey_patch.py:L530-L539](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/transformers/monkey_patch.py#L530-L539) 与 `use_cutile` 区 [src/tilegym/transformers/monkey_patch.py:L541-L552](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/transformers/monkey_patch.py#L541-L552)。
   - 用两个不同颜色标注：哪些替换在任何后端都发生，哪些只在 `use_cutile=True` 时发生。
3. **需要观察的现象**：基础区替换的是「组件类」（`Olmo3RMSNorm`、`Olmo3MLP`、`ALL_ATTENTION_FUNCTIONS`），`use_cutile` 区替换的是「`.forward` 方法」与「融合子类」。
4. **预期结果**：你会得出结论——`use_cutile` 不仅切后端，还额外把 DecoderLayer 的 `forward` 也换成融合实现，因为「融合 residual + norm」必须改 `forward` 才能拿到残差张量。
5. **运行验证（可选，需 GPU）**：用 `modeling/transformers` 子项目，分别以默认与 `--use_cutile` 跑同一个模型，观察 kernel 覆盖率（u8-l3 会讲）变化，标注「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：为什么「融合 residual_add + RMSNorm」必须以「替换 `DecoderLayer.forward`」的形式实现，而不能像 RMSNorm 那样只换类？

**参考答案**：因为残差相加 `hidden + residual` 发生在 `DecoderLayer.forward` 内部、跨越了子模块边界；只换 `RMSNorm` 类拿不到「残差张量」，必须接管整个 `forward` 才能把相加与归一化融进一个内核。

**练习 2**：`set_backend("cutile")` 修改的是进程级状态还是调用级状态？如果在打补丁后又 `set_backend("triton")`，会发生什么？

**参考答案**：进程级（改的是 `_CURRENT_BACKENDS`）。若补丁已打好（类/方法已固化），后续 `set_backend` 主要影响新发生的工厂调用与运行时分发；但像 `ALL_ATTENTION_FUNCTIONS["sdpa"]` 在 patch 时就已取定了一个 wrapper（其内部 `backend=None` 会走当前后端），所以运行时再切后端仍可能影响该 wrapper 实际调用的内核——这正是 wrapper 内部用 `backend=None` 透传当前后端的设计原因（见 [src/tilegym/ops/attn_interface.py:L73-L122](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/attn_interface.py#L73-L122)）。

### 4.4 模型类型分发

#### 4.4.1 概念说明

前面三个模块讲的都是「针对某一个模型的 `apply_*` 函数」。真实使用时，调用方往往只知道一个模型 ID（如 `meta-llama/Meta-Llama-3.1-8B`）或 `config.json` 里的 `model_type`（如 `llama`、`deepseek_v2`、`qwen2`），不想为每个模型记住该调哪个函数。

为此 `monkey_patch.py` 提供了「分发层」：

1. **一张映射表 `MODEL_TYPE_TO_APPLY_TILEGYM_FN`**：把 `model_type` 字符串映射到对应的 `apply_*` 函数。
2. **一个通用入口 `_apply_tilegym_kernel(model_type, **kwargs)`**：查表 → 用 `inspect.signature` 过滤出该函数实际支持的参数 → 调用它。

这样新增一个模型只需：写一个 `apply_tilegym_kernel_to_<新模型>`，再往表里加一行。

#### 4.4.2 核心流程

```
_apply_tilegym_kernel(model_type, **kwargs):
  1. model_type 为空 → 记日志、直接返回
  2. model_type 不在表里 → 记日志、直接返回
  3. apply_fn = MODEL_TYPE_TO_APPLY_TILEGYM_FN[model_type]
  4. sig = inspect.signature(apply_fn)
  5. applicable = {k:v for k,v in kwargs if k in sig.parameters}   # 过滤不支持的关键字
  6. apply_fn(**applicable)
```

第 4-5 步用 `inspect.signature` 做参数过滤，是个很实用的工程细节：因为不同模型的 `apply_*` 函数支持的开关不同（有的有 `moe`、有的有 `gated_delta_rule`、有的叫 `mlp` 而非 `swiglu`），统一入口把「调用方误传的参数」静默丢掉，而不是抛 `TypeError`。

#### 4.4.3 源码精读

**映射表**：

[src/tilegym/transformers/monkey_patch.py:L555-L566](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/transformers/monkey_patch.py#L555-L566) — 10 个 `model_type` 到函数的映射（llama / deepseek_v2 / gpt_oss / mistral / qwen2 / qwen3_5 / gemma3 / phi3 / olmo3 / olmoe）。注意键用的是 `transformers` 里 `config.model_type` 的写法（如 `deepseek_v2`、`qwen3_5`），与模型 `config.json` 一致。

**通用入口（含参数过滤与「必须先于初始化」的告警）**：

[src/tilegym/transformers/monkey_patch.py:L569-L599](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/transformers/monkey_patch.py#L569-L599) — 重点看：

- L575 的 docstring 明确写了 **「Note: This must be called before model initialization.」**——这是「为什么必须在初始化前调用」的源头注释。
- L591-594 用 `inspect.signature(apply_fn)` 取该函数形参，再把 `kwargs` 里**不属于**这些形参的键过滤掉（`applicable_kwargs`）。
- L599 调用 `apply_fn(**applicable_kwargs)`。

**真实调用方**（佐证调用顺序）：`modeling/transformers` 推理 CLI 并没有直接用 `_apply_tilegym_kernel`，而是用一个等价的 `apply_tilegym_patch(model_id, ...)` 按 model_id 字符串匹配模型后调对应 `apply_*`。关键是**它在加载模型之前**调用：

[modeling/transformers/src/tilegym_hf_bench/_cli.py:L131-L139](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/modeling/transformers/src/tilegym_hf_bench/_cli.py#L131-L139) — L131 `apply_tilegym_patch(...)`（打补丁）**严格早于** L139 `load_model_with_cache(...)`（实例化加载模型）。这就是「先 patch、后实例化」在真实代码里的落地。

#### 4.4.4 代码实践

本实践回答实践任务的第二问：「为什么 monkey-patch 必须在模型初始化前调用」。

1. **实践目标**：结合代码与一个最小思维实验，证明「晚于实例化打补丁会失效」。
2. **操作步骤**：
   - 阅读 [modeling/transformers/src/tilegym_hf_bench/_cli.py:L131-L139](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/modeling/transformers/src/tilegym_hf_bench/_cli.py#L131-L139)，确认 patch 在 load 之前。
   - 做一个**思维实验（无需运行）**：若把顺序反过来——先 `load_model` 再 `apply_tilegym_patch`——会出现什么？
3. **需要观察的现象**（推理）：
   - 「替换类」（粒度 A，如 `LlamaRMSNorm`）在实例化时已被 `DecoderLayer.__init__` 引用过，层对象里的归一化子模块**已经是原生类**的实例。之后即使改了模块属性，已创建的对象不会变。
   - 「替换方法」（粒度 B，如 `*.forward`）在**后**打补丁反而可能仍生效（因为方法按对象查表是运行时），但这会造成「类已用旧类构造、方法却用新实现」的混乱状态。
   - 「字典注册」（粒度 C）若晚于实例化，注意力模块可能已经把原生 attention 函数取走并缓存。
4. **预期结论**：因此 TileGym 强制要求 patch 早于实例化——保证「模型构造时就天然用 TileGym 组件」，状态一致、无混乱。这正是 `_apply_tilegym_kernel` docstring 写「must be called before model initialization」的原因。
5. **若想本地验证**：在装好 GPU 与模型权重的环境里，故意把 `_cli.py` 的两行顺序对调，比较两次推理的 kernel 覆盖率（u8-l3），标注「待本地验证」。

#### 4.4.5 小练习与答案

**练习 1**：`_apply_tilegym_kernel` 为什么要用 `inspect.signature` 过滤 `kwargs`？不过滤会怎样？

**参考答案**：不同模型的 `apply_*` 函数支持的参数不同（`moe` / `gated_delta_rule` / `mlp` 等）。不过滤，调用方统一传一堆参数时，遇到不支持该参数的函数会抛 `TypeError`；过滤后，不支持的关键字被静默丢弃，统一入口就能对所有模型用同一份调用代码。

**练习 2**：如果要让 TileGym 支持一个全新模型（比如 `newmodel`），在 `monkey_patch.py` 层面最少要做哪两件事？

**参考答案**：① 写一个 `apply_tilegym_kernel_to_newmodel(...)`，在其中替换该模型建模模块里的 RoPE/RMSNorm/MLP/Attention 等点；② 在 `MODEL_TYPE_TO_APPLY_TILEGYM_FN` 表里加一行 `"newmodel": apply_tilegym_kernel_to_newmodel`。

## 5. 综合实践

把本讲四块知识（机制 / 替换点 / use_cutile / 分发）串成一个端到端小任务。

**任务**：模拟「给 Llama-3.1 打全量 TileGym 补丁并推理」的完整时序，画出时序图并解释每一步依据。

**步骤**：

1. **确定调用顺序**。阅读 [modeling/transformers/src/tilegym_hf_bench/tilegym_patch.py:L17-L20](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/modeling/transformers/src/tilegym_hf_bench/tilegym_patch.py#L17-L20) 与 [_cli.py:L131-L139](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/modeling/transformers/src/tilegym_hf_bench/_cli.py#L131-L139)，确认时序为：`set_backend`（若 use_cutile）→ `apply_tilegym_kernel_to_llama` → `load_model_with_cache`。
2. **列出 Llama 的 4 个替换点**（见 4.2.4 的预期结果表）。
3. **画时序图**（文字版即可）：

   ```
   ① (可选) set_backend("cutile")            # 4.3：切后端
   ② apply_tilegym_kernel_to_llama(...)       # 4.2：4 个替换点
        ├ modeling_llama.apply_rotary_pos_emb = RoPE
        ├ modeling_llama.LlamaRMSNorm        = RMSNorm 类
        ├ modeling_llama.LlamaMLP            = SwiGLU 类
        └ ALL_ATTENTION_FUNCTIONS["sdpa"]    = FMHA wrapper
   ③ load_model_with_cache(...)               # 实例化：此刻 __init__ 引用的是 TileGym 组件
   ④ model.generate(...)                      # 运行：实际跑 TileGym 内核
   ```

4. **解释 ① 必须在 ③ 之前**：复述 4.4.4 的结论——粒度 A 的类替换只在实例化时生效，晚于实例化则层对象已用原生类构造。
5. **延伸**：若把 `use_cutile=False`（默认）改成 `True`，对照 [monkey_patch.py:L49-L50](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/transformers/monkey_patch.py#L49-L50) 说明会多出 `set_backend("cutile")` 这一步；并指出 Llama 的 `use_cutile` 只有第一层（切后端）、没有第二层额外融合（那是 Qwen3.5/OLMoE/OLMo-3 才有）。

**交付物**：一张时序图 + 一段「为什么 ①② 必须在 ③ 之前」的文字说明（不少于 100 字）。

> 若本地有 GPU 与模型权重，可进一步用 `modeling/transformers` 子项目真实跑一遍：`uv run tilegym-hf-bench --model_id meta-llama/Meta-Llama-3.1-8B --use_tilegym --use_cutile --use_attn --show_outputs`（见 [modeling/transformers/README.md:L33-L43](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/modeling/transformers/README.md#L33-L43)）。无 GPU 环境则标注「待本地验证」，以上源码阅读与画图部分不受影响。

## 6. 本讲小结

- TileGym 用**非侵入式 monkey-patch**接入 `transformers`：在运行时把建模模块里的类/函数/方法换成 TileGym 实现，不改 `transformers` 源文件，进程结束即还原。
- 替换有**三种粒度**：替换模块里的类/函数（实例化时生效）、替换类上的方法（运行时调用时生效）、往 `ALL_ATTENTION_FUNCTIONS` 字典注册（按 `attn_implementation` 查表时生效）。
- 每个 `apply_tilegym_kernel_to_*` 函数按开关替换一组点：**RoPE / RMSNorm / MLP(SwiGLU/GEGLU) / Attention**，稀疏模型还多 **MoE**；不同模型的替换名字与所用工厂各异（DeepSeek 用融合 SwiGLU、Gemma3 用 GEGLU + soft cap、GPT-OSS 用 attention sink）。
- `use_cutile` 有两层作用：调 `set_backend("cutile")` 切后端；并在 Qwen3.5/OLMoE/OLMo-3 等模型上额外打「融合 residual+norm / 融合注意力 forward」等只有 cuTile 才有的补丁。
- **模型类型分发**由 `MODEL_TYPE_TO_APPLY_TILEGYM_FN` 表 + `_apply_tilegym_kernel`（用 `inspect.signature` 过滤参数）完成，新增模型只需加一个函数 + 表里加一行。
- **必须先 patch 再实例化**：因为「替换类」只在实例化时生效，真实 CLI（`_cli.py`）严格保证 patch 早于 `load_model`。

## 7. 下一步学习建议

- **u8-l2 融合模块与算子接口**：深入 `get_fused_swiglu_module()`、`PartiallyFusedSwiGLUMLP` 等被本讲引用的工厂与融合类，理解「融合 MLP 如何把 5 个 kernel 压成 3 个」。
- **u8-l3 HF 推理基准与内核覆盖率**：把本讲的 `modeling/transformers` CLI 讲透——`--use_tilegym / --use_cutile / --use_attn` 的含义、profiling 与 kernel coverage 报告如何衡量 monkey-patch 的效果。
- **重读 u2-l2 / u6-l1**：回到分发器与 FMHA，体会「被换进去的类/函数如何最终走到真实 GPU 内核」的完整链路。
- **动手尝试（进阶）**：仿照 4.4.5 练习 2，为一个本仓库尚未支持的小模型写一个 `apply_tilegym_kernel_to_*` 骨架（哪怕只替换 RMSNorm 一项），跑通「patch → 实例化 → 推理」闭环。
