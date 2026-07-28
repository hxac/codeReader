# CuTe 后端代码生成

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清 **tl 后端** 与 **cute 后端** 在「降级（lowering）」和「目标语言」这两层上的差异，以及它们各自适合的场景。
- 跟踪一个 `online_func`/`score_mod` 从 Python 符号描述，一路降级成 CuTe C++ 片段（`to_cute_op`），再被 Jinja2 灌进 `flash_fwd`/`flash_bwd` 的 `.h`/`.cu`/`.cpp` 骨架，最终 JIT 编译成 CUDA 扩展的全过程。
- 理解 `final_rowscales`（如 softmax 的 `lse`）如何在一个 `lse` 变量上「长出」十几个互相关联的 C++ 代码片段，分别注入到参数结构体、宿主机分配、epilogue 写回等不同位置。
- 解释 `cute_template_output/` 目录里那些产物文件是怎么来的，以及引擎为何用 `importlib` 加载其中的 `flash_attn_interface.py`。

本讲是 **advanced** 层，承接 u3-l3（引擎入口：分发、编译与缓存）。在 u3-l3 里我们已知引擎按 `backend` 分流：`tl` 走 `_compile_tl`，`cute` 经 `lower_cute` 直接渲染。本讲专门拆开 `cute` 这条支路。

## 2. 前置知识

在进入本讲前，请确认你已理解以下概念（前面讲义已建立）：

- **编译链四层**：transform（符号 IR）→ codegen（节点翻译成代码片段）→ lower（降级编排）→ template（Jinja2 模板渲染）。见 u1-l3。
- **`generate_tl_from_dag` 的三套发射器**：`to_tl_op` / `to_cute_op` / `to_pytorch_op`，由 `to_tl` / `to_cute` 两个布尔参数三选一，而 DAG 的后序遍历、去重、inplace 复用逻辑三后端共用。见 u2-l3、u2-l4。
- **符号降级三件套**：`score_mod`（逐元素分数变换）、`online_func`（行级在线算法，含 `online_fwd` / `online_fwd_epilogue` 两段）、`custom_inputs`（额外输入张量）。见 u2-l5 ~ u2-l7。
- **`final_rowscales` 与 `online_rowscales`**：前者是循环后落盘供反向复用的行级状态（如 `lse`），后者是循环内递推的过程状态（如行最大值 `m`、指数和 `r`）。见 u2-l6。

本讲用到的两个本讲内概念先通俗解释一下：

- **CuTe**：NVIDIA CUTLASS 提供的「C++ Templates for CUDA」库，用 `cute::Tensor` 这类 C++ 模板抽象来表达 GPU 上的张量与分块（tile）布局，是 Hopper（H100）架构上手写高性能 fused kernel 的主流方式。FlashAttention-3 就是基于 CuTe 写的。AttentionEngine 的 cute 后端生成的 C++ 代码，骨架就脱胎自 FlashAttention 的 Hopper 模板。
- **JIT 编译（Just-In-Time）**：与 tl 后端「生成 TileLang 源码 → importlib 加载」不同，cute 后端生成的是 C++/CUDA 源码，必须先用 nvcc 编译成 `.so` 才能调用。这件事由 PyTorch 的 `torch.utils.cpp_extension.load` 在运行期完成——读源码、调 nvcc、加载动态库、返回 Python 可调用对象，全部自动。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [attention_engine/core/lower/lower_cute.py](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/lower/lower_cute.py) | cute 后端的降级编排。把 `online_func`/`score_mod` 跑成符号 DAG，用 `to_cute_op` 发射成 C++ 片段，填进 `LowerCuteOutput` 数据类的几十个字段，最后交给模板渲染。 |
| [attention_engine/core/template/cute_template.py](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/template/cute_template.py) | `CuteAttnTemplate` 类。遍历整个 `cute_template/` 目录，用 Jinja2 把每个 `.h`/`.cu`/`.cpp`/`.py` 文件都渲染一遍，写入 `cute_template_output/`。 |
| [attention_engine/core/template/cute_template/](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/template/cute_template/online_func.h) | CuTe C++ 骨架模板目录。里面是一份合法的 FlashAttention-3 风格骨架，散布着 `{{...}}` 占位符，等待降级片段注入。 |
| [attention_engine/core/codegen/tl_gen.py](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/codegen/tl_gen.py) | 三套发射器所在。本讲关注其中的 `to_cute_op`（L135-L233）以及 `generate_tl_from_dag` 的三后端分派（L353-L358）。 |
| [attention_engine/attn_engine/attn_engine.py](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/attn_engine/attn_engine.py) | 引擎入口。`backend == "cute"` 分支（L138-L215）选择模板目录、调用 `lower_cute`、用 `importlib` 加载生成的 `flash_attn_interface.py`。 |
| [attn_script/mha_cute.py](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attn_script/mha_cute.py) | cute 后端的标准 softmax 示例脚本。和 `mha.py` 用同一套 `score_mod`/`mask_mod`/`OnlineSoftmax`，只把 `backend` 改成 `"cute"`。 |

## 4. 核心概念与源码讲解

### 4.1 lower_cute 降级

#### 4.1.1 概念说明

`lower_cute` 是 cute 后端的总编排函数，对应 tl 后端的 `lower_tl`。它要解决的问题是：用户用 Python 写的 `online_func` 和 `score_mod`，怎么变成能塞进 CuTe C++ 骨架的字符串片段？

核心思路和 `lower_tl` 一致——**不解析用户源码，而是「跑一遍」**：构造符号诱饵（`SymbolScalar`/`SymbolicArray`）喂进用户的 Python 函数，靠运算符重载自动挂出符号 DAG，再用 `generate_tl_from_dag` 把 DAG 翻译成代码。唯一的区别是：cute 后端把 `to_tl=False, to_cute=True`，于是分派到 `to_cute_op` 而不是 `to_tl_op`，产物是 C++ 片段而非 TileLang 片段。

和 tl 后端的另一个关键差异是：cute 后端**不做形状分发**。u3-l3 讲过 tl 后端会按 `q_seqlen`/`head`/`head_kv` 关系分发到五个 `lower_*` 文件；而 cute 后端只有「`kv_shared` 与否」两种模板（见 4.3），降级逻辑统一走 `lower_cute` 一条路。这也是 README 说 cute 后端目前主要面向 Hopper 上前向训练/解码的原因之一。

#### 4.1.2 核心流程

`lower_cute` 的执行流程可以用下面这段伪代码概括：

```
lower_cute(score_mod, mask_mod, online_func, custom_fwd_inputs, dimqk, dimv, dtype, ...):
    output = LowerCuteOutput()              # 一个装着几十个字段的数据类，初始全为空串
    output.dimqk, output.dimv, output.cutlass_dtype = ...

    if score_mod:
        lower_score_mod(...)                # 1) 逐元素变换 → C++ 片段，填 score_mod_code 等
    if online_func:
        lower_online_func(...)              # 2) 在线算法 → 多段 C++ 片段，填 online_fwd_body 等
                                            #    并为每个 final_rowscales 生成一整套参数/分配/写回片段

    return CuteAttnTemplate(**output.__dict__)()   # 3) 交给模板渲染（副作用：写一堆文件）
```

其中第 2 步 `lower_online_func` 是最复杂的，它把 `online_func` 的两段（`online_fwd` 与 `online_fwd_epilogue`）各跑一遍符号化、各发射一次，再把结果拆散填到 `LowerCuteOutput` 的多个字段里。关键点：

1. **两段分别符号化**：`online_fwd` 用形状 `[block_M, block_N]` 的 `scores` 当诱饵；`online_fwd_epilogue` 用形状 `[block_M, dimv]` 的 `acc_o_rowcol`（累积输出）当诱饵。
2. **每段都用 `to_cute=True` 发射**，得到 C++ 代码串。
3. **`final_rowscales`（如 `lse`）不只生成一段代码**，而是生成「参数结构体字段、宿主机分配、指针透传、epilogue 写回」等一组配套片段，分别注入到不同模板文件。

#### 4.1.3 源码精读

**总编排入口** `lower_cute`：

[attention_engine/core/lower/lower_cute.py:244-268](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/lower/lower_cute.py#L244-L268) 创建 `LowerCuteOutput`、按 `score_mod` → `online_func` 顺序降级，最后把整个 `__dict__` 作为关键字参数传给 `CuteAttnTemplate` 并立刻 `()` 调用它（触发渲染写盘）。

注意一个细节：`lower_cute` 的返回值其实是 `CuteAttnTemplate(...)()`，即「最后一个被渲染文件的内容」。但引擎在 cute 分支里**并不使用这个返回值**——它只关心渲染写盘的副作用，然后另行用 `importlib` 去 `cute_template_output/` 加载 `flash_attn_interface.py`（见 4.3）。这点和 tl 后端（`tl_code` 字符串被 md5 缓存并 `exec`）截然不同。

**`LowerCuteOutput` 数据类**：

[attention_engine/core/lower/lower_cute.py:9-56](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/lower/lower_cute.py#L9-L56) 这是一个 `@dataclass`，字段全部默认空串。它本质上是一张「字段 → 模板占位符」的接线表：每个字段名都对应骨架里某个 `{{字段名}}` 占位符。你可以把它理解成一个巨大的「待填充信封」，降级过程就是把信一封封写好塞进去。字段大致分四组：

| 组别 | 代表字段 | 注入到哪个模板 |
|------|----------|----------------|
| online 算法体 | `online_fwd_body`、`online_fwd_body_vardefine`、`finalize_epilogue_body` | `online_func.h` |
| 行状态初始化 | `online_rowscales_init`、`online_rowscales_vardefine`、`o_scale_var` | `online_func.h` |
| final_rowscales 全套 | `final_rowscales_struct`、`final_rowscales_store_code_write`、`global_ptr_*`、`online_rowscale_tensor_def` | `flash.h`、`flash_api.cpp`、`epilogue_fwd_sm90_tma.hpp` |
| score_mod | `score_mod_code`、`mainloop_arguments_define` | `mainloop_fwd_sm90_tma_gmma_ws.hpp` |

**`online_fwd` 段的符号化与发射**：

[attention_engine/core/lower/lower_cute.py:76-84](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/lower/lower_cute.py#L76-L84) 调用 `online_func.online_fwd(scores, online_rowscales, b, h, q_idx)` 跑出 `scores_new, new_online_rowscales, o_scalevar`，然后用：

```python
tl_code_online, input_vars_online = generate_tl_from_dag(
    list(new_online_rowscales.values()) + [scores_new, o_scalevar],
    to_tl=False, to_cute=True)
```

注意 `to_tl=False, to_cute=True`——这就是把发射器切到 `to_cute_op` 的开关。得到的 `tl_code_online`（虽然变量名叫 tl，实际是 C++ 代码）随后被填进 `lower_cute_output.online_fwd_body`：

[attention_engine/core/lower/lower_cute.py:111](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/lower/lower_cute.py#L111) `lower_cute_output.online_fwd_body += str(tl_code_online)`。

**行状态初始化值的改写**：cute 后端把符号里的 `-inf` 改写成 C++ 的 `-INFINITY`：

[attention_engine/core/lower/lower_cute.py:101-108](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/lower/lower_cute.py#L101-L108) 遍历 `online_rowscales`，若初值是 `"-inf"` 就输出 `cute::fill(m, -INFINITY);`，否则原样输出（如 `r` 的 `0.0`）。这段产物填进 `online_rowscales_init`，最终注入 `online_func.h` 的构造函数。

**final_rowscales 的写回片段**（这是「LSE 处理」的典型代表）：

[attention_engine/core/lower/lower_cute.py:147-155](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/lower/lower_cute.py#L147-L155) 为每个 final_rowscale（如 `lse`）生成一段 epilogue 写回代码：构造 global tensor `gLSE`、按行索引把片上的 `lse(mi)` 写回 gmem，并做越界保护（`row < actual_seq_len - m_block * kBlockM`）。这段产物填进 `final_rowscales_store_code_write`，注入 `epilogue_fwd_sm90_tma.hpp`。

可以看到：**一个 `lse` 变量，从 L141 开始一直「长」到 L194**，衍生出 `final_rowscales_struct`、`final_rowscales_store_code_define`、`final_rowscales_store_code_assert`、`final_rowscales_store_code_write`、`global_ptr_args`、`global_ptr_params_init`、`online_rowscale_tensor_def`、`global_ptr_args_init`、`final_rowscale_return`……十几个字段，分别负责「在参数结构体里声明指针」「在宿主机 `torch::empty` 分配」「把指针透传给 kernel」「在 epilogue 里写回」「在 Python 接口里返回」等不同环节。这是 cute 后端 final_rowscales 处理的全貌。

**score_mod 段**：

[attention_engine/core/lower/lower_cute.py:206-241](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/lower/lower_cute.py#L206-L241) 同样跑一遍 `score_mod` 符号化、`to_cute=True` 发射，填进 `score_mod_code`。此外它还处理 `custom_fwd_inputs`：形状为 `(1,)` 的标量输入（如 `softmax_bias`）会被声明成 `float const` 并经 `mainloop_params` 透传到 kernel。

#### 4.1.4 代码实践（源码阅读型）

> 实践目标：在不跑 GPU 的前提下，凭源码追踪「`online_fwd` 方法 → `online_fwd_body` 字段 → 模板占位符」的接线。

操作步骤：

1. 打开 [attn_script/mha_cute.py:48-64](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attn_script/mha_cute.py#L48-L64)，读懂 `OnlineSoftmax.online_fwd`：它输入 `scores`（`[block_M, block_N]`）和 `online_rowscales`（`m`、`r`），输出新的 `m_new`、`r`、`scores`（已 exp2）、`o_scale`。
2. 打开 [lower_cute.py:76-84](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/lower/lower_cute.py#L76-L84)，确认这段就是把 `online_fwd` 的输出列表喂给 `generate_tl_from_dag(..., to_cute=True)`。
3. 在 [online_func.h:159](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/template/cute_template/online_func.h#L159) 找到 `{{online_fwd_body}}` 占位符——这就是上面那段 C++ 代码最终注入的位置。

需要观察的现象：`online_fwd` 里的 `scores.get_reduce("max")` 和 `scores.get_reduce("sum")` 是行规约（沿 `block_N`），它们在 `to_cute_op` 里会变成 `flash::reduce_max` / `flash::reduce_sum`（见 4.2.3）。

预期结果：你能在脑中画出 `m.max(scores.get_reduce("max")) → reduce_max → exp2f → reduce_sum` 这条链，并且知道每一步落在 `online_func.h` 的 `online_fwd` 模板方法里。运行结果：待本地验证（本实践为源码阅读型，无需运行）。

#### 4.1.5 小练习与答案

**练习 1**：`lower_cute.py` 里 `lower_online_func` 跑了两次符号化（`online_fwd` 和 `online_fwd_epilogue`），它们用的诱饵张量形状分别是什么？为什么不同？

参考答案：`online_fwd` 用 `[block_M, block_N]` 的 `scores`（打分矩阵分块），因为这一段处理的是「q×k 的分数块」；`online_fwd_epilogue` 用 `[block_M, dimv]` 的 `acc_o_rowcol`（累积输出），因为收尾段处理的是「q×v 的输出块」。两段操作的张量语义不同，所以诱饵形状必须不同，否则 `to_cute_op` 推导出的循环范围会错。

**练习 2**：`LowerCuteOutput` 里有个字段 `global_ptr_params_def_bwd` 默认值是 `"void *__restrict__ softmax_lse_ptr;"`，并被注释标记为「tmp solution」。结合 [lower_cute.py:258-260](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/lower/lower_cute.py#L258-L260)，说明它在什么情况下会被清空。

参考答案：当 `global_ptr_params_def` 里已经包含 `"softmax_lse_ptr"`（即前向已经为 `lse` 生成了规范的指针声明）时，就把这个「临时占位」的 `global_ptr_params_def_bwd` 清成空串，避免重复声明。这说明 cute 后端的反向（bwd）路径仍在完善中，目前用一个写死的占位符过渡。

### 4.2 CuTe C++ 模板渲染

#### 4.2.1 概念说明

降级层产出的几十个字段（`online_fwd_body`、`final_rowscales_store_code_write` 等）都是字符串片段。要把它们组装成一份完整、可编译的 CuTe C++ 工程，就需要模板层。

AttentionEngine 的 cute 模板层和 tl 模板层有一个本质区别：

- **tl 模板**（u3-l1）：一份骨架（`attn_tl.py`），渲染出一个 Python 文件。
- **cute 模板**：**一整个目录**的骨架（`cute_template/` 里有 `.h`/`.cu`/`.cpp`/`.py` 共二十多个文件），渲染出**一整个目录**的产物（`cute_template_output/`）。

这是因为 CuTe kernel 工程本身就是多文件的：kernel 逻辑、host API、Python 接口、launch 模板各司其职，没法塞进一个文件。所以 `CuteAttnTemplate` 用 `os.walk` 遍历整个模板目录，把**每个文件都用同一份 kwargs 渲染一遍**，写到输出目录。

#### 4.2.2 核心流程

```
CuteAttnTemplate(template_dir, output_dir, **kwargs):
    for root, dirs, files in os.walk(template_dir):
        for file in files:
            text = read(template_dir / ... / file)
            rendered = jinja2.Template(text).render(**{k:v for k,v in kwargs if v is not None})
            write(output_dir / ... / file, rendered)   # 内容相同则跳过
    self.tlcode = 最后一个文件的渲染结果   # __call__ 返回它（引擎实际不使用）
```

两个细节决定了渲染的正确性：

1. **`None` 过滤**：`kwargs = {k: v for k, v in kwargs.items() if v is not None}`。骨架里有些占位符在某些配置下不需要填充（例如没有 `score_mod` 时 `score_mod_code` 相关字段为空串而非 `None`），过滤掉 `None` 可避免 Jinja2 渲染出字面量 `"None"`。
2. **空串即「什么都不注入」**：大多数字段默认空串，渲染后占位符原地消失，骨架保持合法。这要求骨架在设计时就保证「占位符为空时仍合法」。

#### 4.2.3 源码精读

**渲染器主体**：

[attention_engine/core/template/cute_template.py:19-52](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/template/cute_template.py#L19-L52) `CuteAttnTemplate.__init__` 用 `os.walk(template_dir)` 遍历，对每个文件 `render_code` 后写入 `output_dir` 对应子目录，并做了「内容相同则跳过写盘」的优化（L42-L50）。

**Jinja2 渲染**：

[attention_engine/core/template/cute_template.py:54-59](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/template/cute_template.py#L54-L59) `jinja2.Template(temp_code).render(**kwargs)`。注意这里**没有**用 tl 模板那种 `indent` 过滤器——因为 cute 的降级片段已经在 `to_cute_op` 里用 `#pragma unroll` + `{ }` 块自己管好了缩进。

**输出目录的自动创建**：

[attention_engine/core/template/cute_template.py:6-12](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/template/cute_template.py#L6-L12) `OUTPUT_DIR` 默认是 `cute_template_output/`，模块加载时即 `makedirs`。

**`to_cute_op` 发射器**（产物长什么样）：

这是理解「降级片段」的关键。[attention_engine/core/codegen/tl_gen.py:135-233](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/codegen/tl_gen.py#L135-L233) 是 `to_cute_op`。对比 `to_tl_op`（L7-L132），三后端的差异一目了然：

- **循环结构**：tl 用 `for i0,i1 in T.Parallel(block_M,block_N):`（Python 风格）；cute 用嵌套的 `#pragma unroll` + `for (int i0=0; i0 < size<0>(tensor); ++i0) {`（C++ 风格，循环上界用 CuTe 的 `size<i>(...)` 推导）。
- **下标语法**：tl 用方括号 `scores[i0,i1]`；cute 用圆括号 `scores(i0,i1)`（CuTe Tensor 的调用运算符）。
- **算子映射**：
  - `Exp` → tl: `T.exp2(x*1.442695)`；cute: `exp2f(x*1.442695)`（CUDA 内建快指令，[L193](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/codegen/tl_gen.py#L193)）。
  - `Log` → tl: `T.log2(x)*0.69314718`；cute: `__logf(x)`（CUDA 内建，[L209](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/codegen/tl_gen.py#L209)）。
  - `Tanh` → cute: `cutlass::fast_tanh(x)`（[L213](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/codegen/tl_gen.py#L213)）。
  - `ReduceMax` → cute: `flash::template reduce_max</*zero_init=*/true>(src, dst);`（[L143](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/codegen/tl_gen.py#L143)），调用 `online_func.h` 里的 warp 规约函数。
  - `ReduceSum` → cute: `flash::reduce_sum<...>(...)`（[L139](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/codegen/tl_gen.py#L139)）。

两者都沿用 `exp → exp2(x·log₂e)` 的换底优化（`1.442695 ≈ log₂e`），因为 GPU 的 `exp2f` 比普通 `expf` 快得多。这点 tl 和 cute 一致，只是分别落在 `T.exp2` 与 `exp2f`。

**占位符在骨架里的样子**：以 `online_func.h` 为例，这是一份合法的 CuTe C++ 头文件，定义了 `flash::OnlineFunc` 结构体：

[attention_engine/core/template/cute_template/online_func.h:132-193](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/template/cute_template/online_func.h#L132-L193) 这个结构体就是把用户的 `OnlineFunc` 翻译成 C++ 的产物。它的 `online_fwd` 方法（L149-L166）里依次有 `{{online_fwd_body_vardefine}}`（变量声明）、`{{online_fwd_body}}`（降级来的算子循环）、`{{copy_o_scale_var}}`、`{{copy_online_rowscales}}`（把片上状态拷回结构体成员）。`finalize_epilogue` 方法（L169-L179）则注入 `{{finalize_epilogue_body}}` 等收尾片段。可以看到：**用户写的 Python `online_fwd`，最终变成了这个 C++ 结构体里的一段方法体**。

#### 4.2.4 代码实践（源码阅读型）

> 实践目标：手工「脑补」一次渲染，验证占位符与字段的对应。

操作步骤：

1. 在 [cute_template 目录](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/template/cute_template/online_func.h) 用搜索找到所有 `{{...}}` 占位符。
2. 对每个占位符，回到 [lower_cute.py](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/lower/lower_cute.py) 找到给它赋值的字段。例如 `{{online_rowscales_init}}` ← L108，`{{o_scale_var}}` ← L116。
3. 重点对比 [online_func.h:187](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/template/cute_template/online_func.h#L187) 的 `{{online_rowscales_0_size}}` 与 [lower_cute.py:135-137](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/lower/lower_cute.py#L135-L137)：这个字段被填成 `size(m)`（第一个行状态张量的大小），用来决定 `rescale_o` 方法里循环多少行。

预期结果：你能列出一张「占位符 → 字段 → 赋值行号」的对照表，证明降级层的每个字段都有明确的注入点。运行结果：待本地验证（源码阅读型）。

#### 4.2.5 小练习与答案

**练习 1**：`CuteAttnTemplate` 渲染时为什么要把 kwargs 里值为 `None` 的键过滤掉？如果不过滤会怎样？

参考答案：Jinja2 遇到未提供的变量会渲染成空串，但若变量值是 Python 的 `None` 对象且被显式传入，可能渲染出字面量 `None`（取决于配置）。cute 后端的字段大多默认空串而非 `None`，但个别字段在某些路径下可能保持 `None`；过滤掉它们让 Jinja2 把这些占位符当作「未定义」渲染成空，保证产物是合法 C++。

**练习 2**：同一个符号 `Exp` 节点，`to_tl_op` 和 `to_cute_op` 各生成什么？为什么都带 `1.442695`？

参考答案：`to_tl_op` 生成 `T.exp2(x*1.442695)`；`to_cute_op` 生成 `exp2f(x*1.442695)`。`1.442695 ≈ log₂e`，因为 `exp(x) = exp2(x·log₂e)`，而 GPU 的 `exp2/exp2f` 指令比 `exp/expf` 快得多，所以两后端都换底到 exp2 以借快速指令。差异只在调用形式：TileLang 的 `T.exp2` vs CUDA 内建 `exp2f`。

### 4.3 importlib 加载 interface

#### 4.3.1 概念说明

渲染完 `cute_template_output/` 后，怎么把这些 C++ 文件变成可调用的 Python 函数？这是引擎入口（attn_engine.py）的 cute 分支要解决的事。

关键链条有三步：

1. **选模板目录**：按 `kv_shared` 选 `cute_template/` 或 `cute_template_kvshared/`，并确定输出目录和要加载的接口文件名。
2. **调 `lower_cute` 触发渲染**：把降级 + 渲染的副作用落到输出目录（生成一整套 `.h`/`.cu`/`.cpp`/`.py`）。
3. **`importlib` 加载接口文件**：用 `importlib.util.spec_from_file_location` 从输出目录加载 `flash_attn_interface.py`，取出 `flash_attn_func`（普通注意力）或 `flash_mla_with_kvcache`（MLA），用 `functools.partial` 绑定成 `self.attention`。

第三步里，被加载的 `flash_attn_interface.py` 自身又会调用 `torch.utils.cpp_extension.load`，把同目录的 C++ 源码 JIT 编译成名为 `flashattn_hopper_cuda<dimqk>_<dimv>_<dtype>` 的 CUDA 扩展。也就是说：**importlib 加载的是 Python 接口，而真正的 kernel 是这个接口在首次调用时 JIT 编译出来的**。

#### 4.3.2 核心流程

```
AttentionEngine.__init__(..., backend="cute"):
    1. 选 template_dir / OUTPUT_DIR / file_path：
         not kv_shared → cute_template/ , cute_template_output/ , flash_attn_interface.py
         kv_shared     → cute_template_kvshared/ , cute_template_output_{dimqk}_{dimv}/ , flash_mla_interface.py
    2. lower_cute(score_mod, mask_mod, online_func, custom_fwd_inputs, dimqk, dimv, dtype,
                  template_dir=..., output_dir=...)      # 副作用：渲染写盘
    3. spec = importlib.util.spec_from_file_location("cute_attn", file_path)
       cute_attn = importlib.util.module_from_spec(spec)
       spec.loader.exec_module(cute_attn)                # 执行接口文件（首次会 JIT 编译 C++）
    4. self.attention = partial(cute_attn.flash_attn_func, causal=(mask_mod is not None))
```

注意与 tl 后端的对比：tl 后端用 **md5 哈希** 做缓存键（`cache/<hash>.py`），cute 后端则**没有 md5 哈希**——它靠 `CuteAttnTemplate` 内部「内容相同则跳过写盘」（4.2.3 提到 L42-L50）和 PyTorch `cpp_extension.load` 自身的编译缓存来避免重复工作。

#### 4.3.3 源码精读

**backend 分流与模板选择**：

[attention_engine/attn_engine/attn_engine.py:138-162](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/attn_engine/attn_engine.py#L138-L162) `elif backend == "cute":` 分支。`not kv_shared` 时用默认的 `cute_template/` 目录、`cute_template_output/` 输出、加载 `flash_attn_interface.py`；`kv_shared` 时换用 `cute_template_kvshared/` 目录、按 `dimqk_dimv` 命名的输出目录、加载 `flash_mla_interface.py`。注释 `# must be same with cute_template.py` 提醒：这里的输出目录路径必须和 `cute_template.py` 模块顶层的 `OUTPUT_DIR` 默认值一致。

**触发降级渲染**：

[attention_engine/attn_engine/attn_engine.py:163-175](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/attn_engine/attn_engine.py#L163-L175) 先用 `cutlass_dtype_map` 把 torch dtype（`float16`/`bfloat16`）映射成 CUTLASS 类型（`cutlass::half_t`/`cutlass::bfloat16_t`），再调 `lower_cute(...)`。`dimqk` 取 `qkv_meta[0].shape[3]`，`dimv` 取 `qkv_meta[2].shape[3]`。`lower_cute` 的返回值在这里被丢弃——引擎只取它的写盘副作用。

**importlib 动态加载**：

[attention_engine/attn_engine/attn_engine.py:176-184](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/attn_engine/attn_engine.py#L176-L184) 标准的 `importlib` 三件套：`spec_from_file_location` → `module_from_spec` → `exec_module`。`exec_module` 执行 `flash_attn_interface.py`，该文件顶部会 `torch.utils.cpp_extension.load(...)` 把 C++ 编译成扩展。取出 `cute_attn.flash_attn_func`，用 `partial(..., causal=True if mask_mod is not None else False)` 绑定成 `self.attention`。

注意：这里 **`mask_mod` 并没有被符号降级**，而是简单地转成一个布尔 `causal` 标志传给 `flash_attn_func`（[L182-L184](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/attn_engine/attn_engine.py#L182-L184)）。也就是说 cute 后端目前只支持「是否因果」这种粗粒度掩码，不像 tl 后端有完整的 `create_block_mask`/`torch.fx` 降级（u2-l8）。这是 cute 后端的一个能力边界。

**接口文件里的 JIT 编译**：

[attention_engine/core/template/cute_template/flash_attn_interface.py:136-147](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/template/cute_template/flash_attn_interface.py#L136-L147) 这段在被 `exec_module` 执行时运行。`torch.utils.cpp_extension.load` 把 `sources`（同目录的 `.cu`/`.cpp`）编译成一个名为 `flashattn_hopper_cuda{{dimqk}}_{{dimv}}_{{cutlass_dtype}}` 的扩展模块。扩展名里带 `dimqk`/`dimv`/`dtype`，是为了让不同形状/精度各自有独立的编译缓存。

**接口文件里的前向包装**：

[attention_engine/core/template/cute_template/flash_attn_interface.py:155-172](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/template/cute_template/flash_attn_interface.py#L155-L172) `_flash_attn_forward` 是被 `flash_attn_func` 调用的底层函数，它把 q/k/v 和 custom 张量透传给编译出来的 `flashattn_hopper_cuda.fwd`。注意 `{{custom_tensors}}` 占位符——它由 `lower_cute` 的 `custom_tensors` 字段填充（[lower_cute.py:240](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/lower/lower_cute.py#L240)），把用户声明的标量输入名串成参数列表。

**示例脚本**：

[attn_script/mha_cute.py:102-111](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attn_script/mha_cute.py#L102-L111) 构造引擎时传 `backend="cute"`。注意它的 `score_mod`/`mask_mod`/`OnlineSoftmax` 和 `mha.py`（tl 后端）几乎完全一样——这正体现了「前端描述与后端解耦」：**换后端不需要改用户描述，只改一个 `backend` 参数**。

#### 4.3.4 代码实践（运行型，需 H100 GPU）

> 实践目标：实际运行 cute 后端，观察 `cute_template_output/` 的产物，并对照降级字段定位注入点。

前置条件：H100（Hopper）GPU、CUDA、CUTLASS，并按 u1-l2 配好 PYTHONPATH。若无 GPU，改做下面的源码阅读型变体。

操作步骤：

1. 运行 `python attn_script/mha_cute.py`（首次会 JIT 编译 C++，耗时较长，属正常现象）。
2. 查看产物目录 `attention_engine/core/template/cute_template_output/`，应看到与 `cute_template/` 同构的一组文件（`online_func.h`、`flash_fwd.cu`、`flash_api.cpp`、`flash_attn_interface.py` 等）。
3. 打开生成出的 `online_func.h`，定位渲染后的 `online_fwd` 方法体——它就是 [online_func.h:159](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/template/cute_template/online_func.h#L159) 处 `{{online_fwd_body}}` 被替换后的结果，里面应能看到 `exp2f(...)`、`flash::reduce_max`、`flash::reduce_sum` 等 `to_cute_op` 产物。
4. 打开生成出的 `epilogue_fwd_sm90_tma.hpp`，定位 [L241](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/template/cute_template/epilogue_fwd_sm90_tma.hpp#L241) 处 `{{final_rowscales_store_code_write}}` 被替换后的 lse 写回循环。

需要观察的现象：渲染后的 `online_func.h` 里，原本骨架中被注释掉的「默认 softmax」代码已被用户的 `OnlineSoftmax` 逻辑替换；`lse` 的写回片段出现在 epilogue 里。

预期结果：每个 `{{...}}` 占位符都被替换成了一段具体的 C++ 代码，且代码内容与 `mha_cute.py` 的 `OnlineSoftmax` 语义一致。运行结果：待本地验证（依赖 H100 环境）。

**源码阅读型变体（无 GPU）**：跳过步骤 1-2，直接在原始骨架文件上完成步骤 3-4 的「占位符 → 字段」对照（即 4.2.4 的实践），同样能掌握注入点定位。

#### 4.3.5 小练习与答案

**练习 1**：cute 后端为什么不像 tl 后端那样用 md5 哈希做缓存键？

参考答案：cute 后端的「缓存」分两层：`CuteAttnTemplate` 渲染时已做「内容相同则跳过写盘」（避免重写文件），而真正昂贵的 C++ 编译由 PyTorch `cpp_extension.load` 内部按扩展名（含 `dimqk_dimv_dtype`）自动缓存。因此没有必要再在引擎层维护一层 md5 索引。tl 后端用 md5 是因为它直接 `exec` 生成的 Python 源码，需要显式的文件级缓存键。

**练习 2**：在 cute 后端，用户的 `mask_mod` 是如何被处理的？和 tl 后端有何不同？

参考答案：cute 后端**没有**对 `mask_mod` 做符号降级（不调 `create_block_mask`/`torch.fx`），而是简单地把 `mask_mod is not None` 转成布尔 `causal`，经 `partial(flash_attn_func, causal=...)` 传入。也就是只支持「整块因果」这一种掩码语义。tl 后端则完整支持任意 `mask_mod` 的块级稀疏与逐元素降级（u2-l8）。这是 cute 后端当前的能力边界之一。

## 5. 综合实践

> 综合任务：给 cute 后端「加一个标量 bias 输入」，验证你对「降级 → 渲染 → importlib 加载」整条链的理解。

背景：`mha_cute.py` 当前 `custom_fwd_inputs = CustomIO({})` 是空的，`score_mod` 直接 `return score`。现在假设要加一个形状为 `(1,)` 的可学习 `softmax_bias`，让 `score_mod` 做加法。

要求你完成：

1. **改用户层**（不改框架源码，只在脚本里改）：
   - 把 `custom_fwd_inputs` 改成 `CustomIO({"softmax_bias": (1,)})`。
   - 把 `score_mod` 改成 `return score + custom_fwd_inputs.input_tensors["softmax_bias"]`。
2. **预测降级产物**：根据 [lower_cute.py:228-241](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/lower/lower_cute.py#L228-L241)，预测 `softmax_bias` 会在哪些字段里出现（提示：`mainloop_arguments_define`、`global_ptr_params_def`、`score_mod_code`、`custom_tensors` 等），分别注入到哪些模板文件。
3. **验证**：若有 H100，运行修改后的脚本，打开生成的 `mainloop_fwd_sm90_tma_gmma_ws.hpp` 查看 [L737](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/template/cute_template/mainloop_fwd_sm90_tma_gmma_ws.hpp#L737) 处 `{{score_mod_code}}` 渲染后的内容，确认有一段 `scores(...) = scores(...) + softmax_bias;` 的循环。若无 GPU，则写出你预测的渲染结果片段（标注「待本地验证」）。

这个任务把三个最小模块串了起来：改用户描述（验证前端与后端解耦）→ 预测降级字段（验证 lower_cute）→ 定位注入点（验证渲染与 importlib）。

## 6. 本讲小结

- **cute 后端 = `lower_cute` + `CuteAttnTemplate` + importlib 加载**。降级用 `to_cute_op` 发射 C++ 片段，渲染整目录骨架，最后 importlib 加载接口文件触发 JIT 编译。
- **tl 与 cute 的差异在两个层面**：降级层（`to_tl_op` vs `to_cute_op`，循环结构/下标/算子映射都不同，但共用换底优化 `exp→exp2(x·log₂e)`）；目标层（TileLang 单文件 Python kernel vs CuTe 多文件 C++ 工程）。
- **`LowerCuteOutput` 是一张接线表**，几十个字段一一对应骨架里的 `{{...}}` 占位符；一个 `final_rowscales`（如 `lse`）会衍生出参数声明、宿主机分配、指针透传、epilogue 写回等十几个配套片段。
- **`CuteAttnTemplate` 渲染整目录**：用 `os.walk` 把 `cute_template/` 下每个文件都用同一份 kwargs 渲染一遍，写入 `cute_template_output/`，内容相同则跳过。
- **引擎加载靠 importlib**：`spec_from_file_location` 加载 `flash_attn_interface.py`，该文件内部用 `torch.utils.cpp_extension.load` JIT 编译 C++ 成 CUDA 扩展；引擎取 `flash_attn_func` 用 `partial` 绑定成 `self.attention`。
- **cute 后端的能力边界**：不做形状分发（只按 `kv_shared` 选模板）、`mask_mod` 只转成布尔 `causal`、反向（bwd）仍在完善（见 `global_ptr_params_def_bwd` 的 tmp solution）。

## 7. 下一步学习建议

- **u5-l2 MLA 解码的 CuTe kv_shared 后端**：本讲提到的 `kv_shared` 分支（`cute_template_kvshared/`、`flash_mla_interface.py`、`get_mla_metadata`/`flash_mla_with_kvcache`）在那里专门展开，讲清 paged-kv 的 `block_table`/`cache_seqlens` 组织与 `tile_scheduler_metadata`/`num_split` 的计算。
- **回顾 u3-l3**：把本讲的 cute 分支和 tl 分支（`_compile_tl`、md5 缓存、形状分发）对照重读，体会「同一前端、两种后端」的解耦设计。
- **阅读真实骨架**：直接通读 `cute_template/flash_fwd_kernel.h` 和 `mainloop_fwd_sm90_tma_gmma_ws.hpp`，理解 CuTe 上 warp-specialized mainloop 的结构，这有助于你日后为 cute 后端扩展新的算子或掩码语义。
- **若想做扩展开发**：尝试在 `to_cute_op` 里增加一个新算子（如 `Sigmoid`）的 C++ 发射，并参照 `lower_cute` 的字段表把它接进骨架——这是检验你是否真正掌握 cute 代码生成的最佳练习。
