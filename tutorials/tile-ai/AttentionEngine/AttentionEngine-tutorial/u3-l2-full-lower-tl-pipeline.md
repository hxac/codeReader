# 训练前向+反向的完整 lower_tl 链路

## 1. 本讲目标

前两讲（u3-l1 模板渲染、u2-l6 online_func 降级）我们分别打开了编译链的「模板层」和「降级三件套中的 online_func」。本讲要把它们重新缝合回一条完整的主流程——`lower_tl`。

学完本讲，你应当能够：

1. 说出 `lower_tl` 的**整体编排顺序**：配置选择 → kernel_options 构建 → 降级三件套 + mask → `lower_kernel` → 模板选择与渲染。
2. 看懂 `AttnFwdKernelOption` / `AttnBwdKernelOption` 这两个数据类如何作为「张量簿记本」收集所有输入/输出/片上缓冲，以及 `lower_kernel` 如何把这本簿记翻译成 TileLang 的内存分配与搬运代码。
3. 推导 `output_idx_list` / `bwd_output_idx_list` 这两个索引列表的公式，理解它们如何声明 kernel 的输入输出边界，并最终影响 `tl.compile` 的 `out_idx` 与 autograd 的张量拆包。

本讲是 u3 单元（模板、引擎、完整编译链）的核心，承上（降级产物）启下（u3-l3 引擎入口的分发与缓存）。

## 2. 前置知识

- **编译链四层回顾**：transform（符号 IR）→ codegen（节点翻译）→ lower（降级编排）→ template（Jinja2 渲染）。`lower_tl` 属于 lower 层，是「唯一同时认识另外三层」的指挥者（见 u1-l3）。
- **降级三件套**：`lower_custom_inputs`、`lower_score_mod`、`lower_online_func`。它们各自把用户描述（CustomIO / score_mod / OnlineFunc）符号化后翻译成一段 TileLang 字符串（见 u2-l5、u2-l6、u2-l7）。
- **online 算法的行级状态**：`online_rowscales`（循环内更新的过程状态，如行最大值 `m`、指数和 `r`）与 `final_rowscales`（循环后落盘供反向复用，如 `lse`）。本讲会反复用到「final_rowscales 有几个」这个数字。
- **TileLang 的 `out_idx` 语义**：一个 `@T.prim_func` 的形参里，哪些是 kernel 的「输出」（即编译后 `mod(...)` 的返回值），由 `out_idx` 列表按下标指定。
- **在线 softmax 反向的 doosum**：反向需要 `delta = rowsum(o * do)`，代码里称为 `doosum`（d-output-o-sum），由独立的预处理 kernel `flashattn_bwd_preprocess` 计算。它是否被引用，会动态改变 bwd 的输入输出下标。

> 如果你对上面某一项不熟，建议先翻对应讲义再回来；本讲会直接使用这些术语，不再重新解释。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [lower.py](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/lower/lower.py) | 本讲主角。定义 `lower_tl` 主流程、`KernelOptionsBase` 簿记类、`lower_kernel` 翻译器，以及各 `lower*Output` 数据类。 |
| [codegen/common.py](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/codegen/common.py) | codegen helper：`arg_def`（形参声明）、`alloc_*_op`（片上分配）、`load_op`/`store_op`/`copy_op`（搬运）、`fill_op`（初始化）。`lower_kernel` 直接调用它们。 |
| [template/attn_template.py](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/template/attn_template.py) | `TlAttnTemplate`：读骨架 `attn_tl.py` → Jinja2 编译 → `render(**kargs)` 灌入降级字段。 |
| [template/tl_template/attn/attn_tl.py](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/template/tl_template/attn/attn_tl.py) | TileLang 骨架程序，含 `main`（前向）、`flash_bwd`（反向）、`_attention`（autograd 包装）。`lower_tl` 的所有产物都灌进它的 `{{...}}` 占位符。 |
| [attn_script/mha.py](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attn_script/mha.py) | 标准参照样本。`AttentionEngine(...)` 构造时最终会调用 `lower_tl`。 |

---

## 4. 核心概念与源码讲解

### 4.1 lower_tl 主流程编排

#### 4.1.1 概念说明

`lower_tl` 是 lower 层的**总编排函数**。它接收用户描述（`score_mod` / `block_mask` / `online_func` / `custom_fwd_inputs`）与问题形状，输出一份**渲染好的 TileLang 源码字符串** + 一份 `block_mask`。

它的核心职责不是「亲自写代码」，而是**按正确顺序调度**：

1. 决定性能配置（block 尺寸、stages、是否 tune）；
2. 准备一个空的「张量簿记本」（kernel_options）；
3. 依次调用降级三件套，让它们把产物字符串和需注册的张量都登记进去；
4. 算出输入/输出下标列表；
5. 让 `lower_kernel` 把簿记本翻译成内存分配与搬运代码；
6. 处理 mask；
7. 把所有字段合并 `**kwargs` 交给模板渲染。

理解了「顺序」与「每步往簿记本里塞了什么」，就理解了 `lower_tl`。

#### 4.1.2 核心流程

下面用伪代码描述主流程（对应 [lower.py:617-783](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/lower/lower.py#L617-L783)）：

```
lower_tl(score_mod, block_mask, online_func, custom_fwd_inputs,
         Batch, head, seqlen, dimqk, dimv, tl_dtype, mask_value,
         tuned_config=None, infer_mask=False, ...,
         tune=False, tune_file="", tune_bwd=False, tune_file_bwd=""):

    # 0. 把形状字符串包成 T.symbolic(...)
    lower_output = lowerOutput(BATCH, HEADS, SEQ_LEN, DIM, DIMV)

    # ===== 前向配置 =====
    # 1a. tune_output（FWD）：block_M/N、stages、shared_fuse
    #     特例：dimv>256 → 64/64/stages=1/shared_fuse=True
    # 1b. kernel_options = AttnFwdKernelOption(tile_M=block_M, tile_N=block_N, ...)

    # ===== 反向配置 =====
    # 2a. tune_output_bwd：按 max(dimqk,dimv) 选 block_M_bwd/block_N_bwd
    # 2b. bwd_kernel_options = AttnBwdKernelOption(...)

    # ===== 降级三件套（向 kernel_options 登记张量）=====
    # 3a. lower_custom_inputs(...)        → customInputOutput
    # 3b. lower_score_mod(...)            → lowerScoreModOutput
    # 3c. lower_online_func(...)          → lowerOnlineFuncOutput

    # 4.  output_idx_list / bwd_output_idx_list   （见 4.3）
    # 5.  lower_kernel(kernel_options, kernel_code_template)  （见 4.2）
    # 6.  mask_mod 经 torch.fx 追踪 → mask_mod_code

    # ===== 模板选择与渲染 =====
    # 7. if infer_mask:
    #        据因果性选 TlAttnTemplate(dense) 或 TlBlockAttnTemplate(blocksparse)
    #    else:
    #        extern_block_mask ? blocksparse : dense
    #    合并所有降级对象的 __dict__ → template(**kwargs)() → tl_code
    return tl_code, block_mask
```

有两条返回分支值得注意：`infer_mask=True` 时会真正调用 `create_block_mask` 跑出块掩码，并据 `is_causal_mask` / `is_less_causal_mask` 在 dense 与 blocksparse 模板间二选一（mask 机制细节见 u2-l8）；`infer_mask=False` 时一律走 dense（或由 `extern_block_mask` 强制 blocksparse）。

#### 4.1.3 源码精读

**函数签名与形状符号化**。`Batch/head/seqlen` 若是字符串（动态形状），会被包成 `T.symbolic('...')` 写进 `lowerOutput`，这些值最终灌进模板顶部的 `get_problem_keys()` 与 `kernel(...)` 调用。

- 形状符号化：[lower.py:627-634](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/lower/lower.py#L627-L634)
- `is_inf_mask` 由 `mask_value` 是否为 `"-inf"` 决定，控制前向循环里被遮蔽位置填 `0` 还是 `-T.infinity`：[lower.py:637](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/lower/lower.py#L637)

**前向配置**。当没有外部传入 `tuned_config` 时，用默认 `TunnerOutput`（`block_M=128, block_N=128, stages=2, thread_num=256, shared_fuse=False`）。唯一一处硬编码特例：`dimv > 256` 时强行降到 `64/64/stages=1/shared_fuse=True`（大 head 维占用寄存器多，需缩小分块）。此外，`shared_fuse=True` 时会把 online_func 作用的 scores 变量改名为 `scores_1`，对应模板里「scores → scores_shared → scores_1」的拷贝路径。

- 默认 tune_output 与 dimv>256 特例：[lower.py:642-654](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/lower/lower.py#L642-L654)
- `TunnerOutput` 字段定义：[lower.py:155-163](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/lower/lower.py#L155-L163)

> 说明：上面这些 `block_M/N/stages` 是「不 tune 时的默认值」。`mha.py` 里 `tune=True`，真正取值由 autotuner 搜索后写回 `tune_file`（见 u5-l3）；只有 `tune=False` 时模板才直接用 `{{block_M}}` 等占位（见 attn_tl.py 末尾的 else 分支）。本讲讨论「默认值如何随 dim 变化」时，指的是这套 `TunnerOutput` / `TunnerOutputBwd` 的初值。

**前向 kernel_options**。注意 `tile_M/tile_N` 这里被赋成 sympy 符号 `block_M/block_N`，不是具体数字——它们是**符号 IR 里的形状名**，供降级时推导下标，真实数值在 autotuner/默认配置里才确定。

- 构造前向 kernel_options：[lower.py:657-659](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/lower/lower.py#L657-L659)

**反向配置**。反向的 block 尺寸按 `max(dimqk, dimv)` 分档：

| `max(dimqk, dimv)` | `block_M_bwd` | `block_N_bwd` | `thread_num_bwd` |
| --- | --- | --- | --- |
| `<= 64` | 128 | 128 | 256 |
| `<= 128` | 128 | 64 | 256 |
| `> 128`（保持默认） | 128 | 64 | 256 |

- 反向分档与 bwd_kernel_options：[lower.py:665-676](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/lower/lower.py#L665-L676)
- `TunnerOutputBwd` 字段定义：[lower.py:173-179](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/lower/lower.py#L173-L179)

> **关键差异：前向与反向的 block 语义是「转置」的**。前向 `main` 里 `block_M` 切 Q、`block_N` 切 KV；反向 `flash_bwd` 里 `block_M` 切 KV（K/V 按 `block_M` 加载）、`block_N` 切 Q（Q 按 `block_N` 加载），见骨架 [attn_tl.py:291-345](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/template/tl_template/attn/attn_tl.py#L291-L345)。这也解释了为何反向要单独一套分档：dQ 的累加用 `atomic_add`，访存模式与前向不同，最优分块自然不同。

**降级三件套调度**。三件事按 `custom_inputs → score_mod → online_func` 顺序执行，前两者的产物是「字符串片段」，后者（online_func）还会**副作用式地向 kernel_options 注册输出张量**（final_rowscales）。

- 三件套调用：[lower.py:681-688](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/lower/lower.py#L681-L688)

**mask 降级**。若传入了 `block_mask`（其实是 `mask_mod` 函数），用 `torch.fx.symbolic_trace` 追踪它，再由 `tl_codegen_from_torchfx` 逐节点翻译成 `operator.*` 调用片段 `mask_mod_code`，并记录下标节点名（`q_idx/kv_idx/...`）。

- mask 追踪：[lower.py:709-719](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/lower/lower.py#L709-L719)

**模板选择与渲染**。`infer_mask` 分支会调用 `create_block_mask` 真正跑出块掩码，并据 `is_causal_mask`（精确下三角）选 dense `TlAttnTemplate`、否则选 blocksparse `TlBlockAttnTemplate`。注意 blocksparse 会把两个 idx_list 各整体 `+1`——因为模板里多插了一个 `block_mask` 输入张量，所有后续下标都顺移一位。最后把六个降级对象的 `__dict__` 用 `**` 展开合并，交给模板。

- infer_mask 分支与模板渲染：[lower.py:722-756](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/lower/lower.py#L722-L756)
- 非 infer_mask 分支：[lower.py:758-783](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/lower/lower.py#L758-L783)

#### 4.1.4 代码实践

**实践目标**：跟随 `mha.py`（`D=DV=128`，`infer_mask=True`）走一遍 `lower_tl`，推断它走了哪个配置分支、选了哪个模板。

**操作步骤**（源码阅读型，无需 GPU）：

1. 打开 [mha.py:86-117](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attn_script/mha.py#L86-L117)，确认 `B,H,S,D,DV = 1,128,2048,128,128`，`infer_mask=True`（静态形状分支）。
2. 代入 `lower_tl`：
   - `dimv=128`，**不满足** `dimv>256`，故前向走默认 `block_M=128, block_N=128, stages=2, shared_fuse=False`（但因 `tune=True`，实际值由 autotuner 决定）。
   - `max(dimqk,dimv)=128 ≤ 128`，故反向走 `block_M_bwd=128, block_N_bwd=64, thread_num_bwd=256`。
3. 追踪 `infer_mask=True` 分支：`causal_mask` 是精确下三角，`is_causal_mask` 返回 True → 选 **dense 模板 `TlAttnTemplate`**，并丢弃 `block_mask`（`block_mask = None`）。

**需要观察的现象 / 预期结果**：

- 前向默认分块 128×128、反向 128×64，体现「随 dim 分档」。
- 因 `causal_mask` 精确因果，最终走 dense 模板且不传块掩码，`output_idx_list` **不** +1。

> 待本地验证：若有 GPU，可把 `mha.py` 的 `tune=True` 改成 `False`，在 `lower_tl` 末尾 `return` 前临时 `print(tl_code[:2000])`，确认默认配置下生成的 `kernel(...)` 调用里 `block_M/block_N` 正是 128/128。

#### 4.1.5 小练习与答案

**练习 1**：若把 `mha.py` 改成 `DV=512`（即 `dimv=512`），前向默认配置会变成什么？`scores_online` 变量名会变吗？

> **答案**：`dimv=512>256` 触发特例，前向默认变为 `block_M=64, block_N=64, stages=1, shared_fuse=True`；同时 `lower_output.scores_online` 被改名为 `"scores_1"`（[lower.py:648-654](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/lower/lower.py#L648-L654)），对应模板里 shared_fuse 的 scores 拷贝路径。

**练习 2**：为什么反向的 block 尺寸要按 `max(dimqk, dimv)` 分档，而不是像前向那样只看 `dimv`？

> **答案**：反向 kernel 同时要算 dQ（依赖 `dimqk` 的 K@Q）和 dK/dV（依赖 `dimv` 的 V），两条 gemm 都吃寄存器；所以用两者的较大值 `max(dimqk, dimv)` 来决定能否放大分块。前向的 scores 只与 `dimqk` 有关、acc_o 与 `dimv` 有关，特例只针对 `dimv` 较大时的 acc_o 路径，故只看 `dimv`。

---

### 4.2 kernel_options 与 lower_kernel：把张量簿记翻译成内存分配与搬运

#### 4.2.1 概念说明

降级三件套各自只关心「生成自己那一段逻辑代码」，但一个 kernel 还需要：声明哪些张量是**输入形参**、哪些是**输出**、片上要**分配多少 shared/fragment 缓冲、循环前后要做哪些 global↔片上的搬运**。这些是「跨三件套的公共基础设施」。

`lower_tl` 的解法是引入一个**张量簿记本** `kernel_options`（`KernelOptionsBase` 的子类）：三件套在执行时，通过 `add_output_tensor` / `add_input_tensor` / `add_intermediate_tensor` 把自己需要的张量**登记**进去；最后由 `lower_kernel` 统一读取这本簿记，翻译成五段代码。

这个设计的妙处是**关注点分离**：online_func 不必知道 score_mod 注册了什么，两者都只跟簿记本对话。

#### 4.2.2 核心流程

`KernelOptionsBase` 维护五个集合（[lower.py:82-88](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/lower/lower.py#L82-L88)）：

| 字段 | 含义 |
| --- | --- |
| `global_tensors_input` | kernel 的**输入**形参（global 内存），如 Q/K/V、custom input 的 `g_*` |
| `global_tensors_output` | kernel 的**输出**形参（global 内存），如 final_rowscales 的 `g_lse` |
| `shared_tensors` | 片上 shared memory 缓冲 |
| `fragment_tensors` | 片上 fragment（寄存器）缓冲 |
| `copy_maps` | 搬运计划：`(src, dst, 全局下标, 维映射)` 的列表 |

登记函数做了两件事：把张量塞进 shared/fragment 集合，**同时**（若给了 `global_idx`）生成一条 `CopyMap` 记下「这个片上皮片对应 global 张量的哪一切片」。这正是输入/输出搬运的源头。

随后 `lower_kernel(kernel_options, kernel_code_template)` 把簿记翻译成五段字符串，写入一个 `lowerKernelBaseOutput`：

```
lower_kernel(kernel_options, template):
    # ① input_args        : 遍历 global_tensors_input  → arg_def(...)
    # ② output_args       : 遍历 global_tensors_output → arg_def(...)
    # ③ alloc             : 遍历 shared_tensors → alloc_shared_op；
    #                       遍历 fragment_tensors → alloc_fragment_op
    # ④ output_args_copy_epilogue : copy_maps 中 dst 属于 output 的 → store_op（片上→global）
    # ⑤ input_args_copy_prologue  : copy_maps 中 src 属于 input 的 → load_op（global→片上）
```

这五段最终对应模板里的占位符：`{{custom_fwd_inputs}}`（①）、`{{final_rowscales_output}}`（②）、`{{custom_fwd_inputs_init}}`（③）、`{{final_rowscales_save}}`（④）、`{{custom_fwd_inputs_load_prolog}}`（⑤）。

#### 4.2.3 源码精读

**簿记本数据类**。`AttnFwdKernelOption` / `AttnBwdKernelOption` 继承 `KernelOptionsBase`，只多了 `tile_M/tile_N/dim/dimv` 这些符号形状，供降级时推导下标。

- `KernelOptionsBase` 与三个登记函数：[lower.py:82-143](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/lower/lower.py#L82-L143)
- 重点看 `add_output_tensor` 如何同时建片上皮片 + global 输出 + CopyMap：[lower.py:90-111](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/lower/lower.py#L90-L111)

**降级三件套如何登记**。以 online_func 为例：它在收尾把每个 `final_rowscales`（如 `lse`）通过 `add_output_tensor` 登记成输出张量，并附带 global 切片下标 `[bz, by, bx*block_M]` 与维映射 `[2,]`——这等于告诉 `lower_kernel`：「循环结束后，把片上的 lse 搬到 `g_lse[bz, bx, bx*block_M:(bx+1)*block_M]`」。

- online_func 注册输出张量：[lower.py:385-392](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/lower/lower.py#L385-L392)

**`lower_kernel` 翻译器**。逐段遍历簿记，调用 codegen helper 生成代码：

- 形参声明 `arg_def` 产出 `name: T.Buffer(shape, 'dtype'),`：[common.py:11-12](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/codegen/common.py#L11-L12)
- 片上分配 `alloc_shared_op` / `alloc_fragment_op` 产出 `name = T.alloc_shared([...), 'dtype')`：[common.py:15-22](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/codegen/common.py#L15-L22)
- `lower_kernel` 主体（五段翻译）：[lower.py:275-315](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/lower/lower.py#L275-L315)

重点看搬运段。`store_op`（输出搬运）按 `CopyMap` 的 `idx_dim_map` 与 `idx_list` 推导出 global 端的切片表达式 `start:end`，生成 `T.copy(片上, global[切片])`：

- epilogue（片上→global）：[lower.py:297-305](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/lower/lower.py#L297-L305)
- prologue（global→片上）：[lower.py:308-315](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/lower/lower.py#L308-L315)
- `store_op` / `load_op` 的切片推导：[common.py:32-85](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/codegen/common.py#L32-L85)

**模板侧的对应**。在 `attn_tl.py` 的前向 `main` 里，`{{custom_fwd_inputs_init | indent(16)}}` 就是 `lower_kernel` 产出的 `alloc`（片上分配），`{{final_rowscales_save | indent(16)}}` 就是 epilogue 搬运：

- 模板里 alloc 占位（`custom_fwd_inputs_init`）：[attn_tl.py:98](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/template/tl_template/attn/attn_tl.py#L98)
- 模板里输出搬运占位（`final_rowscales_save`）：[attn_tl.py:172](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/template/tl_template/attn/attn_tl.py#L172)

#### 4.2.4 代码实践

**实践目标**：确认 `mha.py` 的 online_func（`OnlineSoftmax`）在 `lower_tl` 里向 `kernel_options` 登记了哪些张量，并追踪它们如何变成模板里的 `alloc` 与 `final_rowscales_save`。

**操作步骤**：

1. 读 [mha.py:27-72](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attn_script/mha.py#L27-L72)：`OnlineSoftmax` 的 `final_rowscales` 只有一个键 `"lse"`，故 `online_func.final_rowscales` 长度为 1。
2. 跟到 [lower.py:385-392](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/lower/lower.py#L385-L392)：`lse` 经 `add_output_tensor` 登记进 `global_tensors_output["g_lse"]`，并附 `CopyMap(idx=[bz,by,bx*block_M], dim_map=[2,])`。
3. 跟到 `lower_kernel` 的 epilogue 段 [lower.py:297-305](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/lower/lower.py#L297-L305)：该 CopyMap 的 `dst.name=="g_lse"` 命中输出分支，`store_op` 据下标生成形如 `T.copy(lse, g_lse[bz, by, bx * block_M : (bx + 1) * block_M])`。
4. 在骨架 [attn_tl.py:172](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/template/tl_template/attn/attn_tl.py#L172) 确认 `{{final_rowscales_save}}` 正是承接这段代码的洞口。

**预期结果**：你能画出一条链 `OnlineSoftmax.final_rowscales["lse"] → add_output_tensor → CopyMap → lower_kernel epilogue → store_op → {{final_rowscales_save}}`。

> 待本地验证：`mha.py` 的 `custom_fwd_inputs` 为空（`CustomIO({})`），故 `lower_custom_inputs` 不登记任何输入张量，`{{custom_fwd_inputs}}` 与 `{{custom_fwd_inputs_load_prolog}}` 渲染为空串。可在 `sigmoidattn.py`（带 `softmax_bias`）里观察非空情形。

#### 4.2.5 小练习与答案

**练习 1**：`add_output_tensor` 和 `add_intermediate_tensor` 的本质区别是什么？

> **答案**：前者除了把片上皮片登记进 shared/fragment，**还会**在 `global_tensors_output` 建一个 `g_*` 输出形参，并生成一条 `CopyMap`（循环后搬运到 global）；后者只登记片上缓冲、不产生 global 形参与搬运，是纯中间量（如 online_func 的临时 `scale_tmp`）。见 [lower.py:90-111](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/lower/lower.py#L90-L111) 与 [lower.py:134-143](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/lower/lower.py#L134-L143)。

**练习 2**：`copy_maps` 里同一条记录，为何既可能被 epilogue 段处理、又可能被 prologue 段处理？判定依据是什么？

> **答案**：一条 `CopyMap(src, dst, ...)` 描述了 global 张量与片上皮片的一对映射。若 `dst.name` 落在 `global_tensors_output`（方向：片上→global），由 epilogue 段用 `store_op` 处理；若 `src.name` 落在 `global_tensors_input`（方向：global→片上），由 prologue 段用 `load_op` 处理。判定见 [lower.py:299 与 310](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/lower/lower.py#L299)。

---

### 4.3 output_idx_list：声明 kernel 的输入与输出边界

#### 4.3.1 概念说明

一个 TileLang kernel 的形参列表里，输入和输出是**混在一起**的（都是 `T.Buffer`）。TileLang 编译器需要你明确指出「哪些下标是输出」，才会把它们作为 `mod(...)` 的返回值。这个「输出下标列表」就是 `out_idx`。

`lower_tl` 必须在渲染前算出两个列表：

- `output_idx_list`：前向 `main` 的输出下标；
- `bwd_output_idx_list`：反向 `flash_bwd` 的输出下标（dQ/dK/dV）。

它们的难点在于：下标会随**用户描述**动态变化——custom input 有几个、final_rowscales 有几个、反向是否用了 doosum，都会平移所有下标。`lower_tl` 用两个 `range(...)` 公式把这套平移关系编码清楚。

#### 4.3.2 核心流程

**前向 `main` 的形参顺序**（见骨架 [attn_tl.py:77-85](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/template/tl_template/attn/attn_tl.py#L77-L85)）：

```
0:Q  1:K  2:V  |  3..3+n : custom_fwd_inputs  |  3+n :Output  |  3+n+1.. : final_rowscales
                \___________ 输入 ___________/    \_______ 输出 _______/
```

其中 `n = len(custom_fwd_inputs.input_tensors)`，`m = len(online_func.final_rowscales)`。输出 = `Output` + `final_rowscales`，故：

\[
\texttt{output\_idx\_list} = [\,3+n,\; 3+n+1,\; \dots,\; 3+n+m\,]
\]

即 `range(3+n, 3+n+1+m)`。

**反向 `flash_bwd` 的形参顺序**（见骨架 [attn_tl.py:272-289](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/template/tl_template/attn/attn_tl.py#L272-L289)）：

```
0:Q 1:K 2:V 3:dO | 4..4+n : custom_fwd_inputs | final_rowscales(m) | custom_bwd_inputs(doosum?1:0) | dQ dK dV
                  \_____________________ 输入 _____________________/                                  \_输出_/
```

输出 = `dQ, dK, dV`（3 个），起点 = `4 + n + m + doosum`，故：

\[
\texttt{bwd\_output\_idx\_list} = [\,s,\; s+1,\; s+2\,],\quad s = 4+n+m+\texttt{isused\_doosum}
\]

即 `range(s, s+3)`。其中 `isused_doosum` 是 `lower_online_func` 探测出来的布尔值（见 4.3.3）。

> **blocksparse 修正**：若选了 `TlBlockAttnTemplate`，模板会在输入最前面多插一个 `block_mask` 张量，于是所有下标整体 `+1`——这就是 [lower.py:734-735](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/lower/lower.py#L734-L735) 里 `[i+1 for i in output_idx_list]` 的由来。

#### 4.3.3 源码精读

**前向列表公式**。`+1` 对应 `Output`，`len(online_func.final_rowscales)` 对应 final_rowscales 数量。

- 前向 output_idx_list：[lower.py:689-693](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/lower/lower.py#L689-L693)

**反向列表公式**。起点 `4`（Q/K/V/dO 四个固定输入），再叠 custom 数、final_rowscales 数、doosum 标志，最后取连续 3 个（dQ/dK/dV）。注意代码里 `int(lower_online_func_output.isused_doosum)`——doosum 是否出现，**取决于用户 backward 方法是否真的引用了 doosum_rowscales**。

- 反向 bwd_output_idx_list：[lower.py:696-703](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/lower/lower.py#L696-L703)

**doosum 的探测**。`lower_online_func` 在反向分支里把 `doosum_shared` 作为符号诱饵喂给 `online_func.backward`，再调用 `generate_tl_from_dag` 生成 `custom_bwd_body`；若 `doosum_shared` 出现在生成代码的 `input_vars_bwd` 里，就判定 `isused_doosum = True`，并据此生成 `g_doosum` 形参与加载代码。

- doosum 探测与条件生成：[lower.py:442-453](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/lower/lower.py#L442-L453)

**模板与 autograd 侧的消费**。这两个列表灌进模板的 `{{output_idx_list}}` / `{{bwd_output_idx_list}}`，分别用于前向与反向的 `@jit(out_idx=...)` 和 `tl.compile(..., out_idx=...)`；`_attention` autograd 函数也读取 `{{output_idx_list}}` 来决定 `mod(...)` 返回值如何拆成 `o, *final_scale`。

- 模板里前向 out_idx：[attn_tl.py:182 与 527](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/template/tl_template/attn/attn_tl.py#L527)
- 模板里反向 out_idx：[attn_tl.py:560](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/template/tl_template/attn/attn_tl.py#L560)
- autograd 拆包：[attn_tl.py:572-578](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/template/tl_template/attn/attn_tl.py#L572-L578)

#### 4.3.4 代码实践

**实践目标**：手算 `mha.py` 的 `output_idx_list` 与 `bwd_output_idx_list`，并与模板消费方式对齐。

**操作步骤**：

1. 确认 `mha.py` 的 `custom_fwd_inputs = CustomIO({})` → `n = 0`；`OnlineSoftmax.final_rowscales = {"lse": ...}` → `m = 1`。
2. 读 [mha.py:81-84](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attn_script/mha.py#L81-L84) 的 `backward`：`dppsum = doosum_rowscales` 且 `dscores = (dp - dppsum)*scores`，**引用了** doosum → `isused_doosum = True`。
3. 代入公式：
   - 前向：`range(3+0, 3+0+1+1) = range(3,5) = [3, 4]` → `Output=3, lse=4`。
   - 反向：`s = 4+0+1+1 = 6` → `range(6,9) = [6, 7, 8]` → `dQ=6, dK=7, dV=8`。
4. 对照骨架 [attn_tl.py:572-578](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/template/tl_template/attn/attn_tl.py#L572-L578)：`output_idx_list=[3,4]` 长度 2，故 `o, *final_scale = mod(q,k,v)` → `o` 与 `final_scale=(lse,)`。

**预期结果**：手算的 `[3,4]` 与 `[6,7,8]` 与 `lower_tl` 的 `range` 输出一致；并能解释「为何反向起点是 6 而不是 5」——因为 doosum 占了第 5 号位。

> 待本地验证：若把 `OnlineSoftmax.backward` 改成不依赖 `doosum_rowscales`（例如 `dscores = dp * scores`），则 `isused_doosum` 应变为 False，反向起点变为 5，列表变为 `[5,6,7]`。注意这会改变数值正确性，仅供理解机制，不要用于真实训练。

#### 4.3.5 小练习与答案

**练习 1**：若用户的 `CustomIO({"softmax_bias": (1,)})` 被启用（即 `n=1`），`mha.py` 的前向 `output_idx_list` 会变成什么？

> **答案**：`n=1, m=1`，前向 `range(3+1, 3+1+1+1) = range(4,6) = [4,5]`。因为 `softmax_bias` 作为输入插在第 3 号位，`Output` 与 `lse` 各后移一位。

**练习 2**：为什么 `output_idx_list` 在 blocksparse 模板下要整体 `+1`，而 `bwd_output_idx_list` 也要跟着 `+1`？

> **答案**：blocksparse 模板在两个 kernel 的形参最前面都插入了 `block_mask` 输入张量，导致 `Output` / `dQ/dK/dV` 在形参表里的绝对下标全部后移一位，故两个列表都要 `[i+1 for i in ...]`。见 [lower.py:734-735](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/lower/lower.py#L734-L735) 与 [lower.py:761-762](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/lower/lower.py#L761-L762)。

---

## 5. 综合实践

**任务**：以 `mha.py` 为基准，写一份「`lower_tl` 执行轨迹表」，把从入口到模板渲染的每一步产物对应到最终 TileLang 源码的具体段落，验证整条编译链的缝合关系。

**要求**：

1. 在一张表里列出 `lower_tl` 的 7 个阶段（配置 → kernel_options → 三件套 → idx_list → lower_kernel → mask → 模板渲染）。
2. 对 `mha.py`（`D=DV=128, n=0, m=1, doosum=True, infer_mask=True, dense 模板`）这一具体情形，每一行写出：
   - **调用的函数 / 代码行**（带永久链接）；
   - **生成的关键代码片段**（init value、`online_func` 定义、gemm 循环、epilogue、`{{output_idx_list}}` 的值）。
3. 重点回答：
   - 前向默认 `block_M/block_N = 128/128`、反向 `128/64` 是哪两行决定的？
   - `lse` 这个输出是如何从 `OnlineSoftmax` 一路流到 `{{final_rowscales_save}}` 的（结合 4.2 的登记链）？
   - `output_idx_list=[3,4]` 与 `bwd_output_idx_list=[6,7,8]` 是哪两行 `range` 算出的？

**提示**：可对照 [attn_tl.py:107-172](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/template/tl_template/attn/attn_tl.py#L107-L172) 的前向循环体（`T.fill(acc_o,0)` / `T.fill(o_scale,1.0)` / `online_rowscales_initvalue` / `call_online_func` / `online_func_epilogue` / `final_rowscales_save`），逐一标注每段来自哪个降级字段。

> 这是源码阅读型实践，无需 GPU。完成它即等于把 u3-l1（模板）、u2-l5/l6/l7（三件套）、本讲（编排）串成了一张完整的「用户描述 → TileLang 源码」地图。

## 6. 本讲小结

- `lower_tl` 是 lower 层的总编排函数，按 **配置 → kernel_options → 降级三件套 → idx_list → lower_kernel → mask → 模板渲染** 七步，把用户描述缝合渲染成一份完整 TileLang 源码。
- 前向 block 默认 `128/128`（`dimv>256` 时特例降为 `64/64` 且 `shared_fuse=True`）；反向按 `max(dimqk,dimv)` 分档（`≤64`→`128/128`，`≤128` 或更大→`128/64`）。**前向 block_M 切 Q、反向 block_M 切 KV，语义转置**。
- `KernelOptionsBase` 是「张量簿记本」：三件套通过 `add_output/input/intermediate_tensor` 登记张量并建 `CopyMap`，`lower_kernel` 再统一翻译成 input_args / output_args / alloc / epilogue搬运 / prologue搬运 五段代码。
- `output_idx_list = range(3+n, 3+n+1+m)`（前向 Output+final_rowscales），`bwd_output_idx_list = range(4+n+m+doosum, …+3)`（反向 dQ/dK/dV）；blocksparse 模板下两者整体 `+1`。
- `isused_doosum` 由「用户的 backward 方法是否引用 doosum_rowscales」动态探测，会平移反向所有下标并条件生成 `g_doosum` 形参与加载代码。
- 末尾把六个降级对象的 `__dict__` 用 `**` 合并交给 `TlAttnTemplate` / `TlBlockAttnTemplate` 渲染，同名占位符取同值（如 `final_rowscales_output` 前后向共用）。

## 7. 下一步学习建议

- **进入 u3-l3（引擎入口）**：本讲的 `lower_tl` 只在「训练 MHA」形状下被调用。下一篇将打开 `attn_engine.py` 的 `_select_lower_template`，看引擎如何按 `qkv_meta` 形状（kv_shared、q_seqlen 对 kv_len、head 对 head_kv）分发到 `lower_tl` / `lower_gqa` / `lower_decode*` / `lower_decode_mla`，以及 md5 缓存 + importlib 动态加载如何把本讲渲染出的源码变成可调用的 `mod`。
- **横向对比**：读完 u3-l3 后，建议快速浏览 [lower_gqa.py](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/lower/lower_gqa.py)，对比它与本讲 `lower_tl` 在形状映射与 idx_list 上的异同（u4-l2 会详讲）。
- **回看 u3-l1**：若对 `{{...}}` 占位符与降级字段的对应仍有模糊，可用本讲 4.2.3 的「登记链」为索引，重读模板渲染机制。
