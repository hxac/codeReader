# u1-l3 目录结构与代码地图

## 1. 本讲目标

本讲是 AttentionEngine 的「找代码」训练。读完本讲，你应该能够：

- 一眼看清仓库顶层有哪些目录、每个目录负责什么。
- 理解 `attention_engine/core/` 下的 **transform / codegen / lower / template** 四层结构，知道哪一层做什么。
- 拿到一个功能需求，能沿着「用户 API → 降级（lower）→ 模板（template）→ 引擎（attn_engine）」这条调用链，快速定位到它对应的源文件。
- 看懂引擎是如何根据输入形状把任务**分发**到不同降级函数的，以及生成出来的代码去了哪里。

本讲不深入任何一层的实现细节，只建立「地图」。具体每一层怎么工作，是后续讲义（u2、u3）的内容。

## 2. 前置知识

学习本讲前，你需要已经掌握（来自 u1-l1、u1-l2）：

- **AttentionEngine 是一个编译器**：用户用 Python 函数描述注意力（`score_mod`、`mask_mod`、`online_func`、`custom_fwd_inputs`），框架把它翻译成 GPU 设备代码（TileLang 或 CuTe）。
- **`qkv_meta` 与 `meta_tensor`**：三个 `meta_tensor` 组成的元组，只携带形状信息，是编译阶段的唯一形状来源。
- **两种后端**：`tl`（TileLang，默认，生成 Python kernel）与 `cute`（CuTe C++，面向 Hopper）。
- **`mod = AttentionEngine(...)` 构造即编译**，得到的 `mod` 可以像普通 PyTorch 算子一样 `mod(q,k,v)` 前向、`.backward()` 反向。

本讲会反复用到两个术语，先统一说明：

- **符号 IR（Intermediate Representation）**：把用户的 Python 表达式记录成一个「计算图」（节点 + 连边），而不是真的去算数值。这样后续才能从这个图「打印」出 device 代码。
- **降级（lowering）**：编译器术语，指「把高层的、抽象的描述，转换成更底层、更接近硬件的代码」这一动作。AttentionEngine 里几乎每一个 `lower_*.py` 文件都在做这件事。

## 3. 本讲源码地图

本讲只读「地图相关」的入口文件，目的是建立全局认识，不展开实现：

| 文件 | 作用 |
| --- | --- |
| [README.md](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/README.md) | 项目说明、安装方式、`PYTHONPATH` 配置、示例清单、roadmap。建立顶层认识的权威来源。 |
| [attention_engine/attn_engine/__init__.py](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/attn_engine/__init__.py) | 引擎层包入口，导出对外 API。 |
| [attention_engine/core/__init__.py](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/__init__.py) | 编译核心包入口，导出符号 IR 与工具。 |
| [attention_engine/attn_engine/attn_engine.py](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/attn_engine/attn_engine.py) | 引擎主类 `AttentionEngine`：分发、编译、缓存、调用全在这里。 |
| [attention_engine/core/lower/lower.py](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/lower/lower.py) | 降级层编排者：把 transform/codegen 的产物拼起来交给 template。 |
| [attention_engine/core/template/attn_template.py](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/template/attn_template.py) | 模板层入口：用 Jinja2 把降级字段渲染进 TileLang 模板。 |
| [attention_engine/core/utils.py](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/utils.py) | 公共工具：`meta_tensor` 形状占位、`IndentedCode` 缩进代码拼接器。 |

## 4. 核心概念与源码讲解

### 4.1 顶层目录划分

#### 4.1.1 概念说明

AttentionEngine 的仓库分为三大块：

1. **`attention_engine/`**：框架本体，也就是「编译器」的全部代码。
2. **`attn_script/`**：用户脚本，每个文件都是一种自定义注意力的完整可运行例子（softmax / sigmoid / relu / linear / gqa / mla decode ……）。这是你学习「怎么用」的最佳入口。
3. **`3rd_parties/`**：第三方依赖子模块，包括 `tilelang`（TileLang 编译器）和 `cutlass` / `cutlass_39`（CuTe 后端依赖）。这些不属于 AttentionEngine 的源码，但运行时需要。

一个关键细节：**`attention_engine/` 本身不是 Python 包**，而是被加进 `PYTHONPATH` 的「源码根目录」。所以 README 里的导入写的是 `from attn_engine import ...`、`from core import ...`——`attn_engine` 和 `core` 才是顶层包名。

#### 4.1.2 核心流程

把环境变量配好后，目录与导入的关系是这样的：

```
仓库根目录/
├── attention_engine/        ← PYTHONPATH 根（不是包）
│   ├── attn_engine/         ← from attn_engine import AttentionEngine
│   ├── core/                ← from core import ...
│   ├── autotuner/           ← from autotuner.decider import decider
│   ├── benchmark/           ← from benchmark.bench_utils import ...
│   ├── tests/               ← pytest 测试
│   └── (无 __init__.py)     ← 所以 attention_engine 本身不可被 import
├── attn_script/             ← 用户示例脚本（不是包，直接 python 运行）
├── 3rd_parties/             ← tilelang / cutlass 子模块
└── docs/                    ← API.md 等
```

环境变量配置（来自 README 安装步骤）：

```bash
export PYTHONPATH="$(pwd)/attention_engine:$(pwd)/3rd_parties/tilelang:$PYTHONPATH"
export LD_PRELOAD=/usr/lib/x86_64-linux-gnu/libcuda.so
```

这意味着：**`attention_engine/` 内部的子目录（`attn_engine`、`core`、`autotuner`、`benchmark`、`tests`）都直接成为可导入的顶层包**，不需要写 `attention_engine.core.xxx` 这种长前缀。这是整个项目导入风格的根基。

#### 4.1.3 源码精读

README 给出的安装与环境变量，确认了上述导入模型（[README.md:48-52](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/README.md#L48-L52)）——`PYTHONPATH` 同时挂载 `attention_engine/` 和 `3rd_parties/tilelang`。

引擎层和核心层的两个 `__init__.py` 揭示了对外暴露的「门面」：

引擎层入口，导出三类用户直接接触的 API（[attention_engine/attn_engine/__init__.py:1-2](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/attn_engine/__init__.py#L1-L2)）：

```python
from .attn_engine import AttentionEngine, OnlineFunc
from .linear_attn_engine import LinearAttentionEngine
```

这说明：`attn_engine` 包只对外暴露 `AttentionEngine`（transformer 注意力引擎）、`LinearAttentionEngine`（线性注意力引擎）、`OnlineFunc`（用户继承的在线算法基类）这三个名字。其余的 `lower_*`、template 等都被藏在内部，用户看不到。

核心层入口，导出符号 IR 与工具（[attention_engine/core/__init__.py:1-2](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/__init__.py#L1-L2)）：

```python
from .transform.core import CustomIO, SymbolicArray, SymbolScalar, SymbolicTensor, Var
from .utils import meta_tensor
```

这说明：`core` 包对用户只暴露「符号表示类」（`SymbolScalar` 等）和「形状占位」`meta_tensor`。`core` 内部的 `lower/`、`codegen/`、`template/` 三个子目录的细节，用户脚本里基本不直接 import。

#### 4.1.4 代码实践

**实践目标**：亲手验证「`attention_engine/` 是 PYTHONPATH 根、其子目录是顶层包」这件事。

**操作步骤**（在配好 PYTHONPATH 的环境里）：

1. 在仓库根目录启动 Python。
2. 依次尝试以下导入，观察哪些成功：
   ```python
   from attn_engine import AttentionEngine      # 预期：成功
   from core import SymbolScalar, meta_tensor   # 预期：成功
   import attention_engine                      # 预期：失败（ModuleNotFoundError）
   from autotuner.decider import decider        # 预期：成功
   ```

**需要观察的现象**：前两条、第四条成功；第三条 `import attention_engine` 失败。

**预期结果**：这证明 `attention_engine/` 只是路径根，本身不是包。如果第三条成功了，说明你的环境里恰好有同名的其他东西，需要检查 `PYTHONPATH`。

> 待本地验证：在没有 GPU 的纯 CPU 机器上，`from attn_engine import AttentionEngine` 会触发 `import torch` 等依赖；只要 torch、tilelang 已装好，导入本身不需要 GPU。运行 `mod(q,k,v)` 才真正需要 GPU。

#### 4.1.5 小练习与答案

**练习 1**：README 里写的是 `from attn_engine import AttentionEngine`，而不是 `from attention_engine.attn_engine import AttentionEngine`。为什么？

**参考答案**：因为 `attention_engine/` 目录被加进了 `PYTHONPATH`，它内部的第一层子目录（如 `attn_engine`）才成为顶层可导入包；`attention_engine` 本身没有 `__init__.py`，所以不能作为包名。

**练习 2**：用户脚本（如 `mha.py`）通常放在 `attn_script/` 下，它没有被加进 `PYTHONPATH`，却能 `from attn_engine import ...`，这是怎么做到的？

**参考答案**：导入解析只看 `PYTHONPATH`（以及当前解释器的搜索路径），与脚本本身所在目录无关。只要 `attention_engine/` 在 `PYTHONPATH` 里，无论脚本在哪里运行都能 `from attn_engine import ...`。

---

### 4.2 core 四层架构

#### 4.2.1 概念说明

`attention_engine/core/` 是整个编译器的「心脏」，分为四层，各司其职：

| 层 | 目录 | 一句话职责 | 类比 |
| --- | --- | --- | --- |
| **符号 IR** | `transform/` | 把用户的 Python 函数记录成计算图（符号节点 DAG） | 「做笔记」，记录要算什么 |
| **代码发射** | `codegen/` | 把符号节点翻译成具体后端代码片段（`T.reduce_max`、`exp2f`…） | 「翻译」，笔记 → 代码 |
| **降级编排** | `lower/` | 把多个片段按注意力骨架拼起来，决定哪个模板、哪些输出 | 「装配」，零件 → 整机 |
| **模板渲染** | `template/` | 用 Jinja2 把降级产物填进 kernel 骨架文件，产出完整源码 | 「浇铸」，整机 → 成品 |

这四层里，**只有 `lower/` 是「指挥者」**：它调用 `transform/`、`codegen/` 生成片段，再把片段交给 `template/` 渲染。`transform/` 和 `codegen/` 是被调用的「工具」，`template/` 是产出端。

#### 4.2.2 核心流程

数据在 core 四层中的流动方向（自上而下）：

```
用户 Python 函数 (score_mod / online_func / custom_fwd_inputs)
        │
        ▼
[transform 层]  跑成符号 DAG：SymbolScalar 节点 + 连边
        │                        (graph.py 定义节点，core.py 定义 SymbolScalar)
        ▼
[codegen 层]    把每个节点发射成后端代码片段
        │       (tl_gen.py 的 generate_tl_from_dag / to_tl_op；common.py 的 helper)
        ▼
[lower 层]      编排：lower_custom_inputs → lower_score_mod →
        │       lower_online_func → lower_kernel → mask 处理
        │       并选择 dense(TlAttnTemplate) 还是 blocksparse(TlBlockAttnTemplate)
        ▼
[template 层]   Jinja2 把上述字段 render 进 attn_tl.py → 一整段 TileLang 源码字符串
        │
        ▼
    返回 tl_code（交给引擎层编译/缓存）
```

注意：`transform` 和 `codegen` 之间是「内容」与「翻译」的关系——`transform` 产生**抽象节点**（如一个 `Exp` 节点），`codegen` 把它**翻译**成不同后端的写法（`T.exp` / `exp2f` / `torch.exp`）。同一张图可以发射出三种目标代码，这正是后续 u2-l4 要展开的内容。

#### 4.2.3 源码精读

`lower.py` 文件开头的 import，一图展示了四层之间的依赖（[attention_engine/core/lower/lower.py:6-10](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/lower/lower.py#L6-L10)）：

```python
from ..transform.core import SymbolScalar, SymbolicArray, CustomIO, is_causal_mask, is_less_causal_mask, create_block_mask   # ① IR 层
from ..transform.graph import Var, Const                                                                                    # ① IR 层
from ..codegen.tl_gen import generate_tl_from_dag                                                                           # ② 发射层
from ..template.attn_template import TlAttnTemplate                                                                         # ④ 模板层
from ..template.blockattn_template import TlBlockAttnTemplate                                                               # ④ 模板层
```

可以看到：`lower` 同时 import 了 `transform`（①）、`codegen`（②）、`template`（④）三个兄弟层——它是唯一同时认识三者的「编排者」。`codegen/common.py` 也是通过 `from ..codegen.common import *` 引入，提供 `arg_def`/`alloc_op`/`call_op` 等拼接 helper。

`lower_tl` 主函数里的核心编排顺序（[attention_engine/core/lower/lower.py:681-706](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/lower/lower.py#L681-L706)）：

```python
# 3. kernel template specific lower  (fwd & bwd)
lower_custom_inputs_output = lower_custom_inputs(
    custom_fwd_inputs, lower_output, kernel_options)          # 自定义输入降级

lower_score_mod_output = lower_score_mod(
    score_mod, custom_fwd_inputs, lower_output, kernel_options, bwd_kernel_options)   # score_mod 降级

lower_online_func_output = lower_online_func(
    online_func, lower_output, kernel_options, bwd_kernel_options)                    # online 算法降级

# ... 计算 output_idx_list / bwd_output_idx_list（哪些张量作为 kernel 输出）

# 4. general kernel lower
lower_kernel(kernel_options, kernel_code_template)            # 通用 kernel（内存分配/拷贝）降级

# 5. mask mod （用 torch.fx 把 mask_mod 翻译成 kernel 内代码）
```

这四步 `lower_*` 调用，正好对应「降级编排」层的核心工作：每一步都调用 `transform`+`codegen` 生成一段代码片段，最后一起交给模板。

选择哪种模板的逻辑（[attention_engine/core/lower/lower.py:722-738](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/lower/lower.py#L722-L738)）：

```python
# infer mask: choose blocksparse attn or dense attn
if infer_mask:
    ...
    if (block_mask is not None and not is_causal_mask(...)) or extern_block_mask:
        tlattn_template = TlBlockAttnTemplate       # 稀疏掩码 → 用 blocksparse 模板
    else:
        block_mask = None
        tlattn_template = TlAttnTemplate            # 因果/无掩码 → 用 dense 模板
```

模板渲染的产出（[attention_engine/core/lower/lower.py:740-756](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/lower/lower.py#L740-L756)）：把 `lower_custom_inputs_output`、`lower_online_func_output`、`lower_score_mod_output`、`lower_output`、`tune_output` 等一堆「降级字段」用 `**.__dict__` 全部展开，传给 `tlattn_template(...)`，得到一段完整的 TileLang 源码 `tl_code`。

模板层本身极其简短——它的全部职责就是「读模板文件 → Jinja2 渲染」（[attention_engine/core/template/attn_template.py:11-25](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/template/attn_template.py#L11-L25)）：

```python
class TlAttnTemplate:
    def __init__(self, template_dir=TEMPLATE_DIR, **kargs):
        with open(template_dir, 'r') as f:
            TL_KERNEL = f.read()
        template = jinja2.Template(TL_KERNEL)
        kargs = {k: (v if v is not None else "") for k, v in kargs.items()}   # None → 空串
        self.tlcode = template.render(**kargs)
    def __call__(self):
        return self.tlcode
```

其中 `TEMPLATE_DIR` 指向真正的 kernel 骨架文件（[attention_engine/core/template/attn_template.py:5-8](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/template/attn_template.py#L5-L8)）：`tl_template/attn/attn_tl.py`。这个 `.py` 文件里布满 `{{...}}` 占位符，渲染时被降级字段填上。

#### 4.2.4 代码实践

**实践目标**：学会判断「一个函数属于 core 的哪一层」。

**操作步骤**：

1. 打开 [attention_engine/core/lower/lower.py](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/lower/lower.py)，定位 `lower_score_mod`（约 475 行）。
2. 阅读它的内部，回答：它调用了哪些来自 `codegen` 的函数？调用了哪些来自 `transform` 的类？
3. 同样地看 `lower_online_func`（约 318 行）、`lower_custom_inputs`（约 560 行），把它们的「输入来源层」和「输出用途层」填进下表。

**需要观察的现象**：你会发现这三个 `lower_*` 函数的模式高度一致——都是「接收用户描述 + 调用 `transform`/`codegen` 生成片段 → 产出若干字段（被 template 消费）」。

**预期结果**（参考填法）：

| 降级函数 | 主要调用 transform 的 | 主要调用 codegen 的 | 产出被 template 的哪个占位消费 |
| --- | --- | --- | --- |
| `lower_custom_inputs` | `CustomIO` 解析 | `arg_def` / `alloc_*` / `load_op` | `{{custom_fwd_inputs}}`、`{{custom_fwd_inputs_load_prolog}}` |
| `lower_score_mod` | `SymbolScalar` | `generate_tl_from_dag` | `{{score_mod_func_def}}`、`{{score_mod_func_call}}` |
| `lower_online_func` | `SymbolScalar` / `SymbolicArray` | `generate_tl_from_dag` | `{{online_func_def}}`、`{{online_func_epilogue}}` |

> 待确认：第 3 列的具体字段名需要你在 `attn_tl.py` 模板里核对；上表给出的是常见对应关系，以你实际在模板里看到的 `{{...}}` 为准。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `core` 内部不把四层合并成一个大文件，而要拆成 `transform/codegen/lower/template`？

**参考答案**：关注点分离。`transform` 只管「符号表示」，`codegen` 只管「翻译到不同后端」（同一张图可发射 tl/cute/pytorch 三种代码），`lower` 只管「按注意力骨架编排」，`template` 只管「渲染骨架」。拆分后，新增一个后端只需改 `codegen`；新增一种注意力结构（如 decode）只需新增一个 `lower_*.py`；新增一个算子只需在 `transform` 加节点。

**练习 2**：`lower.py` 里 `from ..codegen.common import *` 用了星号导入。这会带来什么阅读上的麻烦？

**参考答案**：星号导入把 `common.py` 里所有名字都注入当前命名空间，导致阅读 `lower.py` 时，看到 `arg_def(...)` 这种调用无法立刻知道它来自哪里，需要去 `common.py` 翻找。这是阅读本项目代码时要习惯的一个点。

---

### 4.3 调用链定位

#### 4.3.1 概念说明

掌握了顶层目录和 core 四层之后，本节把它们串成**一条完整的调用链**，这是本讲最实用的部分——以后看到任何行为，你都能沿着这条链找到源头。

完整编译链（构造阶段，即 `mod = AttentionEngine(...)` 时发生的事）：

```
用户脚本 mha.py
    │  构造 AttentionEngine(qkv_meta, custom_fwd_inputs, score_mod, mask_mod, online_func, ...)
    ▼
[attn_engine.py] AttentionEngine.__init__  ──按 backend 分流
    │      backend="tl"  → _compile_tl
    │      backend="cute"→ lower_cute（C++ 路径）
    ▼
[attn_engine.py] _compile_tl → _select_lower_template  ──按形状分发
    │      ① kv_shared            → lower_decode_mla
    │      ② q≠kv & head>head_kv  → lower_decode_gqa   （decode GQA）
    │      ③ q≠kv & head==head_kv → lower_decode       （decode MHA）
    │      ④ q==kv & head==head_kv → lower             （训练 MHA）← mha.py 走这里
    │      ⑤ q==kv & head>head_kv  → lower_gqa         （训练 GQA）
    ▼
[core/lower/lower.py] lower_tl  ──core 四层编排（见 4.2）
    │      调 transform + codegen 生成片段 → template 渲染
    ▼
返回 tl_code（一整段 TileLang 源码字符串）
    │
    ▼
[attn_engine.py] md5(tl_code) → cache/{hash}.py → importlib 动态加载
    │
    ▼
self.attention = <加载出来的可调用 kernel>
```

运行阶段（`out = mod(q, k, v)` / `out.backward()`）只是调用已编译好的 `self.attention`，不再触发编译。

#### 4.3.2 核心流程

分发逻辑的判定依据是 `qkv_meta` 里解出的四个形状量：

- `q_seqlen = qkv_meta[0].shape[2]`（query 序列长）
- `kv_len = qkv_meta[2].shape[2]`（key/value 序列长）
- `head = qkv_meta[0].shape[1]`（query 头数）
- `head_kv = qkv_meta[2].shape[1]`（key/value 头数）

用伪代码概括分发规则：

```
if kv_shared:                       → lower_decode_mla     # MLA 解码（kv 共享）
elif q_seqlen != kv_len and head > head_kv:  → lower_decode_gqa   # GQA 解码
elif q_seqlen != kv_len and head == head_kv: → lower_decode       # MHA 解码
elif q_seqlen == kv_len and head == head_kv: → lower              # MHA 训练
elif q_seqlen == kv_len and head > head_kv:  → lower_gqa          # GQA 训练
```

记忆口诀：**先看是否 `kv_shared`（MLA 特殊路径），再看 `q_seqlen` 和 `kv_len` 是否相等（相等=训练/prefill，不等=解码），最后看 `head` 和 `head_kv` 是否相等（相等=MHA，不等=GQA）**。

编译完成后，生成的代码去向（缓存与加载机制）：

1. 对 `tl_code` 字符串算 md5，得到 `code_hash`。
2. 把源码写到 `attention_engine/attn_engine/cache/{code_hash}.py`（若已存在则跳过写入，即缓存命中）。
3. 用 `importlib` 从这个文件动态加载模块，取出其中的 `attention` 函数挂到 `self.attention`。

#### 4.3.3 源码精读

引擎主类构造签名，`backend` 默认是 `"tl"`（[attention_engine/attn_engine/attn_engine.py:108-115](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/attn_engine/attn_engine.py#L108-L115)）：

```python
class AttentionEngine:
    def __init__(self, qkv_meta, custom_fwd_inputs, score_mod, mask_mod,
                 online_func, mask_value="-inf", device=H100(), backend="tl",
                 tune=False, tune_file="", ...):
        if backend == "tl":
            self._compile_tl(qkv_meta, custom_fwd_inputs, score_mod, ...)
        elif backend == "cute":
            from core.lower.lower_cute import lower_cute
            ...
```

`_select_lower_template` 的开头，先把四个关键形状解出来（[attention_engine/attn_engine/attn_engine.py:218-232](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/attn_engine/attn_engine.py#L218-L232)）：

```python
def _select_lower_template(self, qkv_meta, ...):
    ...
    q_seqlen = qkv_meta[0].shape[2]
    kv_len = qkv_meta[2].shape[2]
    head = qkv_meta[0].shape[1]
    head_kv = qkv_meta[2].shape[1]
```

随后五条分发分支按顺序匹配（[attention_engine/attn_engine/attn_engine.py:234-332](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/attn_engine/attn_engine.py#L234-L332)）。每条分支都 `from core.lower.lower_xxx import lower_tl` 按需导入对应的降级函数。例如训练 MHA 走的分支（[attention_engine/attn_engine/attn_engine.py:292-313](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/attn_engine/attn_engine.py#L292-L313)）：

```python
# train/prefill mha forward & backward
if q_seqlen == kv_len and head == head_kv:
    from core.lower.lower import lower_tl
    tl_code, block_mask = lower_tl(score_mod, mask_mod, online_func,
                                   custom_fwd_inputs, B, head, seqlen, dimqk, dimv, ...)
    return tl_code, block_mask
```

分发得到 `tl_code` 后，`_compile_tl` 做缓存与动态加载（[attention_engine/attn_engine/attn_engine.py:369-382](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/attn_engine/attn_engine.py#L369-L382)）：

```python
code_hash = hashlib.md5(tl_code.encode()).hexdigest()
cache_dir = os.path.join(os.path.dirname(__file__), "cache")
file_path = os.path.join(cache_dir, f"{code_hash}.py")
os.makedirs(cache_dir, exist_ok=True)
if not os.path.exists(file_path):
    with open(file_path, "w") as f:
        f.write(tl_code)
        f.flush()
spec = importlib.util.spec_from_file_location("tl_attn", file_path)
tl_attn = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tl_attn)
self.attention = tl_attn.attention
```

这段非常关键：**它把「生成代码」变成「可调用对象」**。md5 保证：只要生成的源码一字不差（同样的注意力描述 + 同样的形状），就直接复用已编译的模块，不必重新编译。

运行时调用入口（[attention_engine/attn_engine/attn_engine.py:388-395](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/attn_engine/attn_engine.py#L388-L395)）：

```python
def __call__(self, *args, **kargs):
    if kargs.get("block_mask") is not None:
        self.block_mask = kargs["block_mask"]
    if self.block_mask is not None:
        o = self.attention(*args, self.block_mask)
    else:
        o = self.attention(*args, **kargs)
    return o
```

注意：如果用了 blocksparse（需要 `block_mask`），调用时会多传一个 `block_mask` 张量；否则直接透传。这就是为什么 `mha.py` 里 `out = mod(q, k, v)` 看起来和普通算子没区别——`block_mask` 的有无由引擎内部处理。

> 旁注：引擎里还有一段被注释掉的调试代码（[attention_engine/attn_engine/attn_engine.py:366-368](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/attn_engine/attn_engine.py#L366-L368)），把 `tl_code` 写到 `generated_tl.py`。调试生成代码时把这几行取消注释即可，这也是 u5-l6 会用到的技巧。

#### 4.3.4 代码实践

**实践目标**：给定若干组输入形状，推断引擎会走哪个 `lower_*` 文件，并定位到具体的分支代码。

**操作步骤**：

1. 对照 [attn_engine.py 的分发分支](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/attn_engine/attn_engine.py#L218-L332)，为下面五组 `qkv_meta` 形状（记为 `qkv_meta = (q_meta, k_meta, v_meta)`，形状顺序是 `meta_tensor(B, H, S, D)`）填表：

| 场景 | q 形状 | v 形状 | q_seqlen | kv_len | head | head_kv | 命中分支 | lower 文件 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 训练 MHA | (1,128,32768,128) | (1,128,32768,128) | | | | | | |
| 训练 GQA | (1,8,4096,128) | (1,2,4096,128) | | | | | | |
| MHA 解码 | (1,128,1,128) | (1,128,32768,128) | | | | | | |
| GQA 解码 | (1,8,1,128) | (1,2,32768,128) | | | | | | |
| MLA 解码（kv_shared）| (1,128,1,576) | (1,1,32768,512) | — | — | — | — | | |

2. 找到 `attn_script/` 里对应的示例脚本（如 `gqa.py`、`gqa_inference.py`、`mla_decode.py`），核对它们构造 `AttentionEngine` 时传的形状是否与你的推断一致。

**需要观察的现象**：第 4 行（GQA 解码）会触发 `assert q_seqlen == 1`，说明 decode 分支要求 query 序列长恰好为 1。

**预期结果**（参考答案）：

| 场景 | q_seqlen | kv_len | head | head_kv | 命中分支 | lower 文件 |
| --- | --- | --- | --- | --- | --- | --- |
| 训练 MHA | 32768 | 32768 | 128 | 128 | ④ | `lower/lower.py` |
| 训练 GQA | 4096 | 4096 | 8 | 2 | ⑤ | `lower/lower_gqa.py` |
| MHA 解码 | 1 | 32768 | 128 | 128 | ③ | `lower/lower_decode.py` |
| GQA 解码 | 1 | 32768 | 8 | 2 | ② | `lower/lower_decode_gqa.py` |
| MLA 解码 | — | — | — | — | ①（kv_shared） | `lower/lower_decode_mla.py` |

> 待本地验证：MLA 解码需要显式传 `kv_shared=True`，否则即便形状满足 ② 也会被当成普通 GQA 解码处理。

#### 4.3.5 小练习与答案

**练习 1**：为什么 AttentionEngine 用 md5 哈希来做缓存键，而不是用 `(score_mod 名, shape)` 这种组合？

**参考答案**：因为最终决定编译产物的是「生成的源码字符串」本身。同样的注意力描述和形状，必然生成同样的源码；用源码的 md5 作键，可以精确命中缓存，避免重复编译。而用名字或形状组合做键，容易在描述有细微差别（例如 `score_mod` 内部改了一个常数）时误判为「相同」，导致用了过期的编译结果。

**练习 2**：生成出来的 `.py` 缓存文件在哪个目录？如何强制重新编译？

**参考答案**：在 `attention_engine/attn_engine/cache/` 目录下，文件名为 `{md5}.py`。强制重新编译的方法是删除该缓存文件（或整个 `cache/` 目录），下次构造 `AttentionEngine` 时会因 `os.path.exists(file_path)` 为假而重新写入并编译。

---

## 5. 综合实践

**综合任务**：画一张 AttentionEngine 的「模块依赖与调用链图」，把本讲三节内容串起来。

要求你的图里至少包含以下信息：

1. **顶层目录**：`attn_script/`（用户）、`attention_engine/`（框架，PYTHONPATH 根）、`3rd_parties/`（依赖）。
2. **`attention_engine/` 的五个子模块**（`attn_engine` / `core` / `autotuner` / `benchmark` / `tests`），每个用一句话标注职责。
3. **`core` 的四层**（`transform` / `codegen` / `lower` / `template`），用箭头标出数据流向，并注明「`lower` 是编排者，调用另外三个」。
4. **完整调用链**：用一条主线串起 `mha.py → AttentionEngine.__init__ → _compile_tl → _select_lower_template → lower_tl（core 四层）→ md5 缓存 → importlib → self.attention → mod(q,k,v)`，并在每个节点旁标注**落在哪个文件**。

建议画法（文字版示意，你可以画得更细）：

```
attn_script/mha.py  (用户描述注意力)
        │ 构造 AttentionEngine(...)
        ▼
attn_engine/attn_engine.py::AttentionEngine.__init__
        │ backend=="tl"
        ▼
attn_engine.py::_compile_tl  →  _select_lower_template  (按 q_seqlen/kv_len/head/head_kv 分发)
        │
        ▼
core/lower/lower.py::lower_tl  ──编排──
        ├──▶ core/transform/core.py + graph.py        (符号 IR：SymbolScalar/Var/节点)
        ├──▶ core/codegen/tl_gen.py + common.py       (发射：generate_tl_from_dag/to_tl_op)
        └──▶ core/template/attn_template.py           (渲染：Jinja2 填 attn_tl.py)
        │ 得到 tl_code 字符串
        ▼
attn_engine.py: md5(tl_code) → attn_engine/cache/{hash}.py → importlib 加载
        │
        ▼
self.attention  ←  mod(q, k, v) 运行时调用
```

**自检清单**（做完后对照）：

- [ ] 我能说出 `attention_engine/` 为什么不是包、却能让 `from attn_engine import ...` 生效。
- [ ] 我能说出 `core` 四层每一层的一句话职责，并知道 `lower` 是编排者。
- [ ] 我能在不查文档的情况下，根据一组 `qkv_meta` 形状推断出引擎会走哪个 `lower_*.py`。
- [ ] 我知道生成代码缓存在 `attn_engine/cache/`，用 md5 命名。

## 6. 本讲小结

- 仓库分三块：`attention_engine/`（框架，PYTHONPATH 根）、`attn_script/`（用户示例）、`3rd_parties/`（依赖子模块）。`attention_engine/` 本身不是包，其子目录才是顶层可导入包。
- `core/` 是编译器心脏，分四层：`transform`（符号 IR）→ `codegen`（代码发射）→ `lower`（降级编排）→ `template`（模板渲染），其中 `lower` 是唯一同时认识另外三层的编排者。
- `lower_tl` 的编排顺序是：`lower_custom_inputs` → `lower_score_mod` → `lower_online_func` → `lower_kernel` → mask 处理 → 选择 `TlAttnTemplate`/`TlBlockAttnTemplate` 渲染。
- 引擎 `AttentionEngine.__init__` 按 `backend`（tl/cute）分流；`tl` 路径再按形状（`kv_shared`/`q_seqlen` vs `kv_len`/`head` vs `head_kv`）分发到五个 `lower_*` 文件之一。
- 生成代码用 md5 哈希做键，缓存在 `attn_engine/cache/{hash}.py`，再用 `importlib` 动态加载成可调用的 `self.attention`，实现「同描述+同形状 → 复用编译结果」。
- 运行时 `mod(q,k,v)` 只是调用已编译的 `self.attention`，不再触发编译；`block_mask` 的有无由引擎在 `__call__` 内部处理。

## 7. 下一步学习建议

本讲只建立了「地图」，没有进入任何一层的实现。建议按以下顺序深入：

1. **先读懂用户 API 的四个组件**：进入 [u1-l4 用户 API 全景](u1-l4-user-api-overview.md)，对照 `mha.py`、`sigmoidattn.py` 弄清 `score_mod`/`mask_mod`/`online_func`/`CustomIO` 的签名与组合方式——这是后续理解任何一层的前提。
2. **再进 `transform` 层**：本讲提到 `SymbolScalar`、`Var`、计算图节点，具体怎么定义、怎么自动反向，见 u2 单元（符号 IR 与代码生成）。
3. **想看完整降级链路**：u3 单元会带你走一遍 `lower_tl` 从头到尾，并把模板占位符和降级字段一一对应。
4. **想立刻看到生成产物**：可以先把 `attn_engine.py` 里注释掉的 `generated_tl.py` 导出（[第 366-368 行](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/attn_engine/attn_engine.py#L366-L368)）取消注释，跑一次 `mha.py`，人眼阅读那段自动生成的 TileLang 代码——这是理解「编译链最终产物」最直观的方式。
