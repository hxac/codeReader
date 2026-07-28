# 讲义：测试与调试技巧

## 1. 本讲目标

AttentionEngine 是一个「编译器」：用户写的 Python 注意力描述，最终被翻译成一段可编译、可运行的 GPU kernel。编译器最难的不是写，而是「跑出来的 kernel 不对」时定位问题出在哪一层。

本讲学完后，你应该能够：

1. 说清 `attention_engine/tests/` 下三个脚本各自在验证什么、覆盖了哪一层。
2. 面对一处生成代码的 bug，判断它属于 **IR 层（transform）**、**降级层（lower + codegen）** 还是 **模板层（template）**，并选用对应的调试手段。
3. 理解 `attn_engine/cache/` 的 md5 缓存命中机制，知道如何**强制重新生成**代码，以及如何把生成的 TileLang 源码**导出**到磁盘人工阅读。
4. 会用 `SymbolScalar.__repr__`、`.code`、`.prev` 等结构打印符号 DAG，配合 `generate_tl_from_dag` 里的调试开关定位符号层问题。

> 本讲是**专家层**，默认你已经读过 u3-l3（引擎入口、分发与缓存），知道 `AttentionEngine(...)` 构造即编译、`tl_code` 经 importlib 动态加载这一条主线。

## 2. 前置知识

在开始前，请确认你理解下面几个概念（来自前置讲义）：

- **编译链四层**：`transform`（符号 IR，建计算图 DAG）→ `codegen`（把节点翻译成后端代码片段）→ `lower`（降级编排，把三件套拼起来）→ `template`（Jinja2 模板，渲染出完整 TileLang 源码）。详见 u1-l3。
- **符号对象**：`SymbolScalar`（带形状的符号值）、`SymbolicArray`（行规约数组），它们通过运算符重载自动建 DAG。详见 u2-l2。
- **三件套降级**：`lower_score_mod` / `lower_online_func` / `lower_custom_inputs`，产物是字符串片段，灌进模板占位符。详见 u2-l5 ~ u2-l7。
- **缓存机制**：生成源码以 md5 为键落盘于 `attn_engine/cache/`，命中则跳过写盘，但每次仍 `exec_module`。详见 u3-l3。

一个贯穿全讲的关键直觉：**AttentionEngine 的「测试」与「调试」都不是黑盒的**。因为它是编译器，每一层的产物都是可读的字符串——符号对象可以打印、降级片段可以打印、最终 TileLang 源码可以导出。调试的本质就是「逐层对照产物」。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲用来做什么 |
|------|------|----------------|
| `attention_engine/tests/test_sympy.py` | 验证 sympy 符号打印 | 说明测试是「依赖探测」型脚本 |
| `attention_engine/tests/test_blockmask.py` | 验证 `create_block_mask` 与两个 causal 判定 | 说明测试覆盖 mask 层（IR/降级） |
| `attention_engine/tests/test_torchtrace.py` | 用 `torch.fx` 追踪 mask 并翻译成代码 | 说明测试覆盖 fx 降级路径 |
| `attention_engine/core/codegen/tl_gen.py` | DAG → TileLang/CuTe/PyTorch 代码发射 | 调试打印开关、`lowered` 去重、inplace 复用 |
| `attention_engine/attn_engine/attn_engine.py` | 引擎入口、缓存与 importlib 加载 | 导出生成代码、强制重新生成 |
| `attention_engine/core/transform/core.py` | `SymbolScalar` 定义 | `.code`/`.prev`/`__repr__`/`clear_codegen` |
| `attention_engine/core/transform/graph.py` | `Node` 符号 IR 与手动反向 | 用 `__main__` 验证反向链 |
| `attention_engine/core/utils.py` | `IndentedCode` | 理解生成代码的缩进容器 |

> 重要：仓库相对路径以 `attention_engine/` 开头，但运行时的 **import 根**是 `attention_engine/` 本身（见 u1-l3），所以测试里写的是 `from core.transform.core import ...`、`from attn_engine import ...`，而非 `from attention_engine.core...`。下文代码引用忠实于源码中的写法。

## 4. 核心概念与源码讲解

### 4.1 测试覆盖范围：tests/ 三个脚本在验证什么

#### 4.1.1 概念说明

打开 `attention_engine/tests/` 目录，你会发现只有三个文件，而且它们**都不是 pytest 风格的 `def test_xxx():` 断言测试**，而是「`if __name__ == "__main__":` 一把跑、把结果 print 出来人工看」的探索脚本。这是理解本项目测试策略的第一个关键：

> AttentionEngine 的「测试」更接近**学习的探针（probe）**和**编译期的单元验证**，而不是回归测试套件。它们专门挑那些**最隐蔽、最容易出错、又能在不碰 GPU 的前提下验证**的环节。

为什么这样做？因为整个编译链里，最贵的一步是「生成 TileLang → 编译成 CUDA → 跑 GPU」。能在这之前（CPU 上、纯符号层面）就把 bug 挡住，调试成本最低。这三个脚本正是分别卡在编译链的「依赖」「mask」「fx 降级」三个**早期且关键**的关口。

#### 4.1.2 核心流程

三个脚本的定位：

```
test_sympy.py        —— 验证 sympy 可用与符号打印        （依赖探测，对应 SymbolScalar.shape）
test_blockmask.py    —— 验证 block_mask 生成与 causal 判定（mask 层，对应 u2-l8 的 create_mask 链路）
test_torchtrace.py   —— 验证 torch.fx 追踪与 tl 翻译     （fx 降级，对应 u2-l8 的 torch.fx 链路）
```

它们共同的特点：

1. **不依赖 GPU、不生成 kernel**：全部在 CPU、纯符号/纯张量层面跑。
2. **以 print 为输出**：让你**肉眼**判断结果是否符合预期，而不是 `assert`。
3. **覆盖编译链的前段（transform / mask）**，这正是数值错误最容易被掩盖、最需要单独验证的地方。

#### 4.1.3 源码精读

**test_sympy.py** 全文只有几行，验证 sympy 能正常导入并打印符号：

[attention_engine/tests/test_sympy.py:4-10](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/tests/test_sympy.py#L4-L10) —— 用 `symbols('x y')` 建符号并打印 `str(x)`、`x+y`。

它看似「没用」，其实是在探测一个真实依赖：`SymbolScalar.shape` 属性里调了 `sympy.simplify` 来规整动态形状字符串：

[attention_engine/core/transform/core.py:73-77](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/transform/core.py#L73-L77) —— `shapes = [sympy.simplify(sh_idx) for sh_idx in self.shape_idx]`。

如果 sympy 未安装或版本不兼容，符号形状比较会在后续比较时悄悄出错，而这个脚本能在最早期暴露它。

**test_blockmask.py** 是三个里信息量最大的。它定义了三个形似因果的 mask，逐一构造块掩码并打印两个 causal 判定：

[attention_engine/tests/test_blockmask.py:3-10](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/tests/test_blockmask.py#L3-L10) —— `causal_mask`（标准下三角）、`causal_mask_1`（`q_idx+1 >= kv_idx`，整体上移一格）、`causal_mask_2`（`q_idx-128 >= kv_idx`，按块大小偏移的稀疏形态）。

[attention_engine/tests/test_blockmask.py:16-22](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/tests/test_blockmask.py#L16-L22) —— `test_mask` 用 `create_block_mask` 生成块级掩码，再分别打印 `is_causal_mask`（是否精确等于下三角）和 `is_less_causal_mask`（是否严格上三角全空）。

这两个判定直接决定了引擎选 dense 模板还是 blocksparse 模板（u2-l8、u3-l2）。如果判定错了，生成的 kernel 会用错误的循环范围——而这种错误不会报语法错，只会算出错误数值，极难发现。这个脚本就是用来肉眼核对判定结果的。

**test_torchtrace.py** 验证 mask 的 `torch.fx` 降级路径。它定义了一个滑动窗口 mask，追踪成 fx 图，再逐节点翻译成代码：

[attention_engine/tests/test_torchtrace.py:5-12](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/tests/test_torchtrace.py#L5-L12) —— `sliding_window_mask` 用 `torch.logical_and` 组合两个不等式；`supported_ops` 只登记了 `logical_and`。

[attention_engine/tests/test_torchtrace.py:13-26](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/tests/test_torchtrace.py#L13-L26) —— `tl_codegen` 按 fx 节点的 `op` 分类翻译：`call_function` 里区分 `operator.*` 函数与 `supported_ops` 里的函数，其它一律 `raise NotImplementedError`。

这段是 `tl_codegen_from_torchfx`（u2-l8 提到的真实降级函数）的**独立原型**。它的价值在于：如果你写了一个 mask 用了「不支持的操作」（比如 `torch.where`），错误会在这里第一时间以 `NotImplementedError` 暴露，而不是等生成 kernel 后才数值错误。

#### 4.1.4 代码实践

**实践目标**：亲手跑一遍 mask 测试，理解「肉眼判定」如何替代断言。

**操作步骤**：

1. 确认 `PYTHONPATH` 已挂载 `attention_engine/`（见 u1-l2）。
2. 运行（CPU 即可，不需 GPU）：

   ```bash
   cd attention_engine
   python tests/test_blockmask.py
   ```

3. 观察三个 mask 各自打印的 `is_causal_mask` 与 `is_less_causal_mask` 值。

**需要观察的现象**：

- `causal_mask`（`q_idx >= kv_idx`）：`is_causal_mask` 应为 `True`（精确下三角），`is_less_causal_mask` 也为 `True`。
- `causal_mask_1`（`q_idx+1 >= kv_idx`）：整体上移后不再精确等于下三角，`is_causal_mask` 会变 `False`，但 `is_less_causal_mask` 仍可能为 `True`。
- `causal_mask_2`（`q_idx-128 >= kv_idx`）：一个稀疏带状 mask，两个判定都会偏离标准因果。

**预期结果**：你会看到三组 `(shape, block_mask 张量, is_causal, is_less_causal)`。关键是理解——**同样的判定逻辑，对三个「长得都像因果」的 mask 给出不同结论，而这直接决定模板选择**。

> 如果本地没有 GPU 或环境未配齐，这一步仍是「待本地验证」——因为它纯 CPU、纯符号，是本讲里最容易跑通的部分。

#### 4.1.5 小练习与答案

**练习 1**：`test_torchtrace.py` 里的 `tl_codegen` 对未知操作会 `raise NotImplementedError`。如果把 `sliding_window_mask` 改成用 `torch.where(...)`，会发生什么？属于哪一层的错误？

**参考答案**：会抛 `NotImplementedError: Operator <where> is not supported`。它属于 **mask 降级层（torch.fx → codegen）** 的错误，发生在 kernel 生成之前、纯 CPU 阶段，因此是最容易定位的一类。

**练习 2**：为什么 `test_sympy.py` 这样一个「只 print 符号」的脚本值得留在仓库里？

**参考答案**：因为 `SymbolScalar.shape` 真实地依赖 `sympy.simplify`（core.py:76）。sympy 缺失或版本不兼容会导致动态形状字符串的比较出错，而这种错误在后续 lower 阶段不会报错、只会算错。这个脚本是这类隐患的最早哨兵。

### 4.2 分层错误定位：IR / 降级 / 模板

#### 4.2.1 概念说明

当生成的 kernel 行为不对时，第一件事不是去看数值，而是**判断 bug 落在编译链的哪一层**。因为不同层的错误，调试手段完全不同：

- **IR 层（transform）**：符号 DAG 本身建错了——比如某算子反向未实现、`shape_idx` 不对、梯度链断了。这类错误往往表现为 `NotImplementedError` 或生成的表达式明显不对。
- **降级层（lower + codegen）**：DAG 是对的，但翻译出的 TileLang 代码错了——下标对不齐、循环范围错、inplace 复用误覆盖、输出索引漏了一个张量。表现为生成的 `.py` 语法错或运行时下标越界。
- **模板层（template）**：降级片段都对，但灌进模板后错位——占位符接错字段、Jinja2 渲染出非法缩进、fwd/bwd 的 `block_M`/`block_N` 搭配错。表现为 kernel 能编译但数值错（NaN、精度不达标）。

掌握「先分层、再下手」能把调试时间缩短一个数量级。

#### 4.2.2 核心流程

分层定位的决策流程（伪代码）：

```
报错或数值异常
   │
   ├─ 是 NotImplementedError(算子) 吗？
   │     是 → IR 层（transform），看 SymbolScalar._backward 或 to_tl_op 支持表
   │
   ├─ 是生成 .py 的语法错 / 下标越界吗？
   │     是 → 降级层（lower + codegen），导出 .py 人工阅读 to_tl_op 产物
   │
   ├─ kernel 能编译，但 print_debug 数值不达标？
   │     是 → 模板层/算法层，检查 online 状态、fwd/bwd block 尺寸、占位符接线
   │
   └─ 改了源码却「没生效」？
         是 → 缓存层（见 4.3），删 cache 强制重新生成
```

下面这张表把常见现象与对应层级、首选手段对应起来：

| 错误现象 | 多半属于 | 首选定位手段 |
|---------|---------|------------|
| `backward for XXX is not implemented` | IR 层 | 查 `SymbolScalar._backward` 支持表 |
| 生成代码里出现 `print("Error: ... shape_idx ...")` | 降级层（发射） | 看 `to_tl_op` 的广播对齐分支 |
| 生成 `.py` 缺少某段（如没有 epilogue） | 降级层 | 检查 `clear_codegen` 是否被漏调 |
| kernel 跑出 NaN / 与参考实现误差大 | 模板层/算法层 | `print_debug` 对齐、检查 rowscales 初值 |
| 改了 `lower.py` 但行为不变 | 缓存层 | 删 `attn_engine/cache/` 重新生成 |

#### 4.2.3 源码精读

**IR 层的「能力边界」信号**。`SymbolScalar._backward` 对算子的反向是逐个手写的，遇到不支持的算子会明确报错：

[attention_engine/core/transform/core.py:193-195](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/transform/core.py#L193-L195) —— `raise NotImplementedError(f"backward for {self.code.type} is not implemented")`。

这就是为什么 u5-l5 强调「sigmoid 要用 tanh 等价写」——一旦你在 `score_mod` 里用了反向未实现的算子，错误会从这里抛出，属于**最易定位的 IR 层错误**。

**降级层的「广播对齐失败」信号**。`to_tl_op` 在推导逐元素算子的下标时，如果两个操作数的 `shape_idx` 维度对不上，会打印一条错误信息（但不抛异常，而是回退到一个兜底下标）：

[attention_engine/core/codegen/tl_gen.py:52-57](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/codegen/tl_gen.py#L52-L57) —— 当某个维度无法在 `args[0].shape_idx` 里找到匹配时，`print(f"Error: ...")` 并回退到 `f"i{i}"`。

如果你在控制台看到大量 `Error: <varname> <shape_idx> ...`，说明降级时两个张量的形状对不齐，属于降级层（发射）问题。

**降级层的「漏调 clear_codegen」陷阱**。`generate_tl_from_dag` 用 `lowered` 标志做去重，节点一旦发射过就会被跳过：

[attention_engine/core/codegen/tl_gen.py:314-315](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/codegen/tl_gen.py#L314-L315) —— `if x.lowered: return tl_code`。

这意味着：**同一批节点要被第二次 `generate_tl_from_dag` 调用时，必须先 `clear_codegen` 复位**，否则第二次会得到空代码。`lower.py` 正是这样做的——前向生成完后，反向前先复位：

[attention_engine/core/lower/lower.py:432-439](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/lower/lower.py#L432-L439) —— 先 `generate_tl_from_dag([scores_2])` 得到 `online_func_fwd`，再 `qkT.clear_codegen()` 复位，最后 `generate_tl_from_dag([dscores])` 得到反向体。

如果你发现某段降级产物是空字符串，第一个怀疑对象就是 `clear_codegen` 没调或调错了对象。

#### 4.2.4 代码实践

**实践目标**：用三个「错误剧本」练习分层判断（源码阅读型实践，无需运行）。

**操作步骤**：对下面三种现象，分别判断属于 IR / 降级 / 模板 / 缓存哪一层，并说出首选手段。

1. 你把 `score_mod` 里的 `score * scale` 改成了 `score ** scale`（幂运算），构造引擎时报错。
2. 生成的 `.py` 能跑，但 `print_debug` 显示输出全是 NaN，而 `score_mod` 只是简单的缩放。
3. 你修了 `lower.py` 里一个明显 bug，重新运行 `mha.py`，结果行为完全没变。

**需要观察的现象 / 预期结果**：

1. **幂运算（`**`）**：`SymbolScalar` 没有重载 `__pow__`，更没有反向 → 报错或构建出错，属 **IR 层**，手段是查 `_backward` 支持表（u2-l5、u5-l5）。
2. **缩放却全 NaN**：`score_mod` 只是乘法，IR/降级都没问题 → 属 **模板层/算法层**，手段是 `print_debug` 对齐参考实现、检查 `online_rowscales` 的初值（如 `m` 是否初值 `-inf`，见 mha.py 的 `OnlineSoftmax`）。
3. **改了不生效**：`tl_code` 字符串变了，md5 应该变 → 但若你改的是不影响最终字符串的部分，或 TileLang 的二进制缓存命中 → 属 **缓存层**，手段是删 `attn_engine/cache/`（见 4.3）。

> 如果无法本地运行，把上面三条作为「判断题」自测即可——重点练的是「先分层」的反射。

#### 4.2.5 小练习与答案

**练习 1**：`to_tl_op` 在广播对齐失败时只 `print` 不 `raise`（tl_gen.py:55）。这种「软错误」对调试是好事还是坏事？为什么？

**参考答案**：是**坏事**偏多。它让程序继续生成「看似合法但下标错误」的代码，bug 会被推迟到 GPU 运行时才以数值错误暴露，定位成本变高。调试时应在控制台主动搜 `Error:` 关键字，把它当强信号。

**练习 2**：一个 kernel 数值错误，你怀疑是 online softmax 的 `m`（行最大值）初值不对。这属于哪一层？该查哪个文件？

**参考答案**：属**模板层/算法层**（算法语义错误，而非 IR/降级结构错误）。应查用户层 `OnlineSoftmax.__init__` 里 `online_rowscales["m"]` 的初值（mha.py 中为 `Var("-inf")`），以及它如何灌进模板的 `online_rowscales_initvalue` 占位符。

### 4.3 缓存与导出调试：code_hash 命中、强制重新生成、SymbolScalar 打印

#### 4.3.1 概念说明

这一节解决两个最常见的调试场景：

1. **「我改了代码，为什么生成的 kernel 没变？」** —— 这是缓存命中的副作用。
2. **「生成的 TileLang 代码到底长什么样？我怎么读到它？」** —— 这是导出调试。

AttentionEngine 的缓存逻辑很简洁：**用生成源码本身的 md5 当缓存键**。同一段描述、同一组形状，生成的 `tl_code` 字符串完全相同 → md5 相同 → 命中磁盘上已有的 `.py` 文件。这套机制保证「同输入同输出」，但也带来一个调试陷阱：**当你以为改了什么、其实没改变最终字符串时，磁盘上的旧文件会被原样加载**。

至于「导出」，引擎里其实**预留了**一行被注释掉的导出代码，专门为调试而生——只是默认不开。

#### 4.3.2 核心流程

`_compile_tl` 里缓存与加载的完整流程：

```
tl_code = _select_lower_template(...)          # 降级 + 渲染，得到完整 TileLang 源码字符串
self.tl_code = tl_code                          # 保存到 self，方便调试时访问
# (注释掉的) with open("generated_tl.py","w") as f: f.write(tl_code)
code_hash = md5(tl_code)                        # 用源码本身算 md5 当 key
cache_dir  = .../attn_engine/cache/
file_path  = cache_dir/<code_hash>.py
makedirs(cache_dir)
if not exists(file_path):                       # 缓存未命中才写盘
    write(tl_code) → file_path
spec  = importlib.spec_from_file_location(...)  # 无论命中与否都加载
module = module_from_spec(spec)
exec_module(module)                             # 执行 .py，定义 attention 符号
self.attention = module.attention               # 取出 kernel 挂到引擎
```

两个要点：

- **命中也执行**：`exec_module` 每次都跑，所以内存里的 `module` 是新建的；但**文件内容**是旧的（命中时没覆盖）。也就是说，缓存的是「文件内容」，不是「内存对象」。
- **md5 由源码决定**：只有改动了**会影响 `tl_code` 字符串**的东西（降级逻辑、模板、形状），才会产生新 hash；改注释、改变量名（不影响输出字符串）则不会。

#### 4.3.3 源码精读

`_compile_tl` 的缓存与加载段落（这是本讲最关键的一段源码）：

[attention_engine/attn_engine/attn_engine.py:365-382](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/attn_engine/attn_engine.py#L365-L382) —— `self.tl_code = tl_code` 保留生成源码；紧跟着两行被注释的 `# with open("generated_tl.py","w") as f: f.write(tl_code)` 就是**为调试预留的导出开关**；随后 `code_hash = hashlib.md5(tl_code.encode()).hexdigest()` 算键、`cache_dir`/`file_path` 定位、`os.makedirs(..., exist_ok=True)` 建目录、`if not os.path.exists(file_path)` 控制只在未命中时写盘，最后 importlib 三件套（`spec_from_file_location` → `module_from_spec` → `exec_module`）加载并取出 `tl_attn.attention`。

**强制重新生成**有两种官方做法：

1. **删 cache 目录**：直接删除 `attention_engine/attn_engine/cache/`（或其中某个 `<hash>.py`），下次构造引擎时 `not os.path.exists` 为真，必然重写。
2. **取消注释导出开关**：把第 367-368 行的 `with open("generated_tl.py","w") as f: f.write(tl_code)` 放开，每次都会把当前 `tl_code` 写到固定路径 `generated_tl.py`，便于人工阅读（注意：这是改源码仅用于调试，本讲强调「读」，不建议长期保留改动）。

> 还有一个**不改源码**的更干净做法：构造引擎后，直接访问 `mod.tl_code`（即 `self.tl_code`，第 365 行赋值），把它写出来或 print。这是源码已经暴露的调试入口，无需改任何一行。

**导出后如何阅读**：导出的 `.py` 是一份完整的 TileLang 程序（合法、可 `exec`）。你要能在里面认出降级三件套灌进去的段落，并与降级函数对应——这正是本讲综合实践的任务。

**SymbolScalar 的调试打印**。符号对象的 `__repr__` 已经把关键字段都暴露了：

[attention_engine/core/transform/core.py:42-67](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/transform/core.py#L42-L67) —— `__init__` 里依次设置了 `varname`、`code`（底层 `Node`）、`prev`（用户层输入链）、`shape_idx`、`count`/`use_list`/`allow_reuse`/`lowered`/`visit_count`、`grad`；`__repr__` 打印 `varname, code, prev, shape_idx, require_grad, dtype`。

调试时务必区分两套结构（u2-l2 已建立，这里复习其调试意义）：

- `x.code.inputs`：底层 `Node` IR 的子节点（graph.py 里的图）。
- `x.prev`：用户层 `SymbolScalar` 输入链——**`generate_tl_from_dag` 实际遍历的是这个**（见 tl_gen.py:326 `for i, input_item in enumerate(x.prev)`）。

如果你发现生成的代码少了一段，先用 `__repr__` 打印根节点的 `.prev`，确认用户层 DAG 是否完整，再看 `.code.inputs`。

**`generate_tl_from_dag` 内置的调试开关**。函数体里留了大量被注释的 `print`，专门用来观察拓扑遍历、复用判定：

[attention_engine/core/codegen/tl_gen.py:328-341](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/codegen/tl_gen.py#L328-L341) —— 注释掉的调试 print，分别能打印每个节点的 `varname`、`prev` 名字、`count`、`visit_count`、`use_list`。把它们放开即可观察 inplace 复用判定的输入。

[attention_engine/core/codegen/tl_gen.py:342-346](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/codegen/tl_gen.py#L342-L346) —— inplace 复用的核心判定：形状相同、且（`count==1` 或 `visit_count==count`）、且 `allow_reuse` 为真时，把输出 `varname` 改写成输入 `varname`，省一次缓冲区分配。

如果你怀疑某次复用「误覆盖」了跨块状态（典型症状：反向用到的前向量被覆盖），就放开这些 print，重点看跨块状态变量（如 `m`/`r`/`o_scale`）是否被错误地 `allow_reuse`——它们在 lower.py 里被 `set_allow_reuse(False)` 显式保护（u2-l6 已述）。

**CuTe 后端自带的活动调试注释**。有趣的是，`to_cute_op`（不像 `to_tl_op`）在**每个**操作末尾都主动写了一条调试注释：

[attention_engine/core/codegen/tl_gen.py:229-232](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/codegen/tl_gen.py#L229-L232) —— `code.add_line(f"// used: {args[0].count}, use_list: {[usea.varname for usea in args[0].use_list]}")`。

也就是说，如果你导出的是 cute 后端产物，每行生成的 C++ 代码后面都自带 `count` 与 `use_list`，可以直接在产物里读出复用信息——这本身就是一种内建的调试支持。

#### 4.3.4 代码实践

**实践目标**：用「不改源码」的方式，把 mha.py 生成的 TileLang 源码导出到磁盘并打印某段符号 DAG。这是本讲对应规格要求的核心实践。

**操作步骤**：

1. 确认环境已配齐（u1-l2），能在本地跑 `attn_script/mha.py`（若无 GPU 则此步「待本地验证」，但下面的代码片段本身是正确的示例代码）。

2. 在 mha.py 构造引擎之后，加一段**示例代码**（仅用于调试，不修改框架源码）：

   ```python
   # 示例代码：不改框架，直接读 mod.tl_code 导出生成源码
   with open("exported_tl.py", "w") as f:
       f.write(mod.tl_code)          # self.tl_code 在 attn_engine.py:365 被赋值
   print("生成代码总行数：", len(mod.tl_code.splitlines()))
   ```

3. （或，纯读 cache 目录）构造一次引擎后，到 `attention_engine/attn_engine/cache/` 找到唯一的 `<md5>.py`，直接打开阅读。

4. 想观察符号 DAG 时，可在 `OnlineSoftmax.online_fwd` 里临时 `print` 中间 `SymbolScalar`：

   ```python
   # 示例代码：打印 online softmax 的行最大值递推
   m_new = m.max(scores.get_reduce("max"))
   print(repr(m_new))                 # 触发 SymbolScalar.__repr__
   ```

**需要观察的现象**：

- `exported_tl.py` 是一份合法的 TileLang 程序，含 `@T.prim_func` 的前向 `main` 与反向 `flash_bwd`，以及 `@T.macro` 定义的 `score_mod`/`online_func` 内联块。
- `repr(m_new)` 会输出类似 `SymbolScalar(m_1, Max(...), [...], ['block_M'], True, float)`，能看到 `code`（底层 Max 节点）、`prev`（用户层输入链）、`shape_idx`。

**预期结果**：你能拿到一份完整的生成源码，并能在其中找到下一段（综合实践）要标注的各个段落。

> 若无 GPU 或编译链未就绪，本实践标注为「待本地验证」。退而求其次，可只做符号打印部分（纯 CPU、纯符号，依赖 sympy 与 torch.fx 即可）。

#### 4.3.5 小练习与答案

**练习 1**：你改了 `lower.py` 里一个真正的 bug，重新跑 mha.py 却发现行为没变。最可能的原因是什么？怎么最快验证？

**参考答案**：最可能是 `attn_engine/cache/` 里命中了旧文件——但你「改的 bug」恰好没改变最终 `tl_code` 字符串（或 TileLang 的二进制缓存命中）。最快验证：删 `cache/` 目录后重跑；或 print `mod.tl_code` 的 md5，对比改动前后是否变化。

**练习 2**：`generate_tl_from_dag` 第一次调用正常，第二次返回空代码。为什么？怎么修？

**参考答案**：因为 `lowered` 标志在第一次发射后被置为 `True`（tl_gen.py:359），第二次遍历到这些节点会直接 `return`（tl_gen.py:314）。修法：第二次调用前对相关节点 `clear_codegen()`（core.py:209-211，复位 `count`/`use_list`/`visit_count`/`lowered`），正如 lower.py:435 在反向前做的那样。

## 5. 综合实践

**任务**：导出一次 mha.py 生成的完整 TileLang 代码，人工阅读并在其中标注：`online_func` 定义段、`score_mod` 定义段、epilogue（收尾归一化）段，分别说明它们来自哪个降级函数的哪个字段。

**操作步骤**：

1. 用 4.3.4 的「不改源码」方法导出 `exported_tl.py`（或读 `cache/<md5>.py`）。
2. 在导出文件中定位以下三段，并在每段开头用注释标注其来源：

   | 你要找的段落（在生成代码里） | 来自哪个降级函数 | 对应模板占位符 |
   |----------------------------|-----------------|---------------|
   | `online_func` 的 `@T.macro`（含 `m_new = ...max(...)`、`o_scale` 递推） | `lower_online_func` 的 `online_func_def` | `{{online_func_def}}` |
   | `score_mod` 的 `@T.macro`（含 `score * softmax_scale`） | `lower_score_mod` 的 `score_mod_func_def` | `{{score_mod_func_def}}` |
   | 循环结束后的收尾（`lse` 落盘、`o / r` 归一化） | `lower_online_func` 的 `online_func_epilogue` | `{{online_func_epilogue}}` 或 `{{online_func_fwd}}` |

3. 对比 mha.py 里 `OnlineSoftmax` 的四个静态方法（`online_fwd` / `online_fwd_epilogue` / `forward` / `backward`），确认每段生成代码对应哪一个方法。

**预期结果**：你应当能建立起一条**端到端**的对应链——

```
用户 OnlineSoftmax.online_fwd (mha.py)
   → 符号化成 SymbolScalar DAG
   → generate_tl_from_dag 发射成 TileLang 代码
   → 灌进模板 {{online_func_def}}
   → 出现在 exported_tl.py 的 @T.macro online_func 块里
```

走通这条链，你就掌握了「从用户描述到生成代码」的完整调试视野——这正是本讲的核心目标。> 提示：模板占位符与降级字段的精确对应关系已在 u3-l1 的「接线表」中给出，本实践侧重**在真实产物里认出它们**。

## 6. 本讲小结

- `tests/` 下三个脚本是**编译期的探索探针**，不是 pytest 回归测试：`test_sympy` 探测 sympy 依赖、`test_blockmask` 验证 causal 判定、`test_torchtrace` 验证 fx 降级——都在 CPU/符号层挡住早期错误。
- 调试的第一步是**分层**：`NotImplementedError` → IR 层；生成 `.py` 语法/下标错 → 降级层；能编译但数值错 → 模板/算法层；改了不生效 → 缓存层。
- 缓存键是**生成源码的 md5**，命中时跳过写盘但仍 `exec_module`；强制重新生成可删 `attn_engine/cache/`，导出代码可直接读 `mod.tl_code`（无需改源码）。
- `SymbolScalar.__repr__` 暴露 `code`/`prev`/`shape_idx` 等全部字段；务必区分 `.prev`（用户层、`generate_tl_from_dag` 实际遍历的）与 `.code.inputs`（底层 Node IR）。
- `generate_tl_from_dag` 内置大量注释掉的调试 print；二次调用前必须 `clear_codegen` 复位 `lowered`，否则得到空代码。
- CuTe 后端的 `to_cute_op` 每条产物自带 `// used: ... use_list: ...` 注释，是内建的复用信息调试支持。

## 7. 下一步学习建议

- 想把「分层定位」落到一处真实 bug 的完整修复流程，建议接着做 **u5-l5 综合实战**：从零实现一种自定义注意力，亲手经历 IR/降级/模板三层联调与对齐。
- 想深入理解缓存命中后「TileLang 二进制层」的缓存行为，可阅读 TileLang 的 JIT 编译与 `~/.tilelang` 缓存机制（项目外文档），补全「源码缓存 → 二进制缓存」的完整图景。
- 想扩展测试覆盖，可参照 `test_blockmask.py` 的写法，为 `lower_score_mod` 或 `lower_online_func` 的**符号化产物**补一个 CPU 级探针脚本（断言 `generate_tl_from_dag` 输出包含特定算子），把目前「肉眼判定」升级为可回归的断言。
- 若计划做框架级二次开发（如新算子反向、新后端），先读 **u5-l7 架构取舍与 roadmap**，了解能力边界与改动落点，再回到本讲用导出法验证你的改动确实进了生成代码。
