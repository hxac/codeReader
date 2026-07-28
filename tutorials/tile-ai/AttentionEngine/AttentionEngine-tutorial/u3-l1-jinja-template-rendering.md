# Jinja2 模板渲染机制

## 1. 本讲目标

本讲打开 AttentionEngine 的第四层、也是编译链的最后一层——**模板层（template）**。前面几讲我们已经看到：用户写的 `score_mod` / `online_func` / `custom_inputs` / `mask_mod` 会被 `transform`（符号 IR）和 `codegen`（代码发射）翻译成一堆**字符串片段**，再由 `lower` 层把这些片段编排成若干「降级字段」（如 `score_mod_func_def`、`online_func_def`、`online_rowscales_initvalue`）。

但这些片段本身并不能运行——它们只是一段段「带洞的代码」。本讲要回答的核心问题是：

> 这些降级字段，最终是怎么被拼成一个**完整、可编译、可 `exec` 的 TileLang 程序**的？

学完本讲，你应当能够：

1. 说出 `core/template` 这一层的职责：用 Jinja2 把降级字符串「灌」进带占位符的模板骨架，产出最终设备代码源文件。
2. 理解 `TlAttnTemplate` / `TlLinearAttnTemplate` / `CuteAttnTemplate` 三个包装类的渲染流程与差异。
3. 读懂 `attn_tl.py` 这个「骨架程序」的 `@T.macro` / `@T.prim_func` 结构。
4. 能把模板里的每一个 `{{...}}` 占位符，精确对应到某个降级函数产出的某个字段——也就是「降级产物→模板」的接线表。

## 2. 前置知识

### 2.1 什么是模板渲染

「模板（template）」是一种**带占位符的文本**。占位符是一些被特殊符号标记的「洞」，渲染时用一个字典把这些洞逐个填上，得到最终文本。AttentionEngine 用的是 Python 生态最常见的模板库 **Jinja2**，它的占位符写作 `{{ 变量名 }}`。

一个最小例子：

```
模板文本：  def add(a, b): return a + {{op}} b
渲染参数：  op="c"
渲染结果：  def add(a, b): return a + c b
```

AttentionEngine 的模板不是上面这种玩具——`attn_tl.py` 是一个**几乎完整的 TileLang 程序**，只在需要注入用户逻辑的地方留 `{{...}}` 洞。

### 2.2 Jinja2 的两个关键点

1. **`{{ var }}` 在 `var` 为 `None` 时会渲染成字符串 `"None"`**，这在 Python 代码里会变成语法错误（例如 `def score_mod(None):`）。所以渲染前必须把 `None` 转成空串。
2. **`indent` 过滤器**：`{{ code | indent(8) }}` 会给多行字符串的**除第一行外**的每一行前面补 8 个空格。第一行的缩进由模板里占位符前的空格负责。这样多行降级代码才能和它所在的 `@T.prim_func` 块对齐。

### 2.3 前置讲义衔接

- **u2-l3 ~ u2-l8** 讲了降级三件套（`score_mod` / `online_func` / `custom_inputs`）和 `mask` 如何产出代码字符串；
- 本讲只关心这些字符串被「装」进模板的最后一公里；
- **u3-l2** 会讲 `lower_tl` 如何把这些字段编排后**调用**模板，本讲先把「模板本身」讲透，为 u3-l2 铺路。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| `attention_engine/core/template/attn_template.py` | `TlAttnTemplate` 包装类：读模板、编译、渲染，返回 TileLang 源码字符串 |
| `attention_engine/core/template/tl_template/attn/attn_tl.py` | **本讲主角**：带 `{{...}}` 占位符的 transformer 注意力骨架程序（前向+反向） |
| `attention_engine/core/template/blockattn_template.py` | `TlBlockAttnTemplate`：blocksparse 版骨架，结构与 `TlAttnTemplate` 几乎相同 |
| `attention_engine/core/template/linear_attn_template.py` | `TlLinearAttnTemplate`：线性注意力模板包装，读 `linear_tl.py` |
| `attention_engine/core/template/cute_template.py` | `CuteAttnTemplate`：CuTe 后端，整目录渲染 C++ 文件并落盘 |
| `attention_engine/core/lower/lower.py` | 渲染的**调用点**：`lower_tl` 末尾用降级字段实例化模板并 `()` 取结果 |
| `attention_engine/core/codegen/common.py` | codegen helper：`func_block` / `call_op` / `arg_def` 等，产出灌进模板的字符串 |

## 4. 核心概念与源码讲解

### 4.1 Jinja2 渲染流程：模板包装类如何工作

#### 4.1.1 概念说明

模板层是 `core` 四层（transform → codegen → lower → **template**）的最后一层。它的输入是：

- 一份**骨架文件**（如 `attn_tl.py`），里面除了 `{{...}}` 洞之外，其余都是真实、可直接编译的 TileLang 代码；
- 一个**字段字典**（由 `lower_tl` 收集而来），键名与洞一一对应。

它的输出是一份**完整的 TileLang 源码字符串**，随后被 `exec` 或 `importlib` 动态加载成可调用的 kernel 模块。

这一层的价值在于「**骨架复用，洞口可变**」：骨架里那些固定不变的部分（`T.Kernel` 网格启动、`T.gemm` 调用、前后反向的编译流程、`torch.autograd.Function` 包装）由项目维护者精心写好；而每个用户注意力各不相同的部分（如何改 score、如何做 online 递推）则由降级层动态生成、灌进洞里。这样既保住了手写 fused kernel 的性能，又让用户无需触碰 kernel 代码。

#### 4.1.2 核心流程

`TlAttnTemplate` 的渲染流程可以概括为四步：

```
1. 打开骨架文件 attn_tl.py，读成纯文本 TL_KERNEL
2. jinja2.Template(TL_KERNEL) 把文本编译成模板对象
3. 把字段字典里的 None 值替换成 ""（避免渲染出 "None"）
4. template.render(**kargs) 填洞，得到 self.tlcode
   __call__() 时直接返回这串源码
```

调用方（`lower_tl`）拿到这串源码后，会以 md5 为键缓存它、再 `importlib` 动态加载（详见 u3-l3）。

#### 4.1.3 源码精读

整个包装类只有 20 行。先看模板路径的定位——它就在与本文件同目录的 `tl_template/attn/attn_tl.py`：

[attention_engine/core/template/attn_template.py:5-8](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/template/attn_template.py#L5-L8) 用 `__file__` 推算出骨架文件的绝对路径，保证不论从哪里调用都能找到 `attn_tl.py`。

核心渲染逻辑在 `__init__` 里：

[attention_engine/core/template/attn_template.py:15-22](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/template/attn_template.py#L15-L22) 这四行就是全部魔法：读文件 → 编译模板 → 把 `None` 转空串 → `render` 填洞。注意第 21 行那个字典推导式——它是正确性的关键，缺了它，未提供的字段会被 Jinja2 渲染成 `"None"`，破坏生成的 Python 代码。

`__call__` 只是返回渲染结果：

[attention_engine/core/template/attn_template.py:24-25](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/template/attn_template.py#L24-L25) 因此在 `lower_tl` 里你会看到 `TlAttnTemplate(...)`（构造即渲染）紧跟一个 `()`（取出字符串）的写法。

`__main__` 段提供了一个观察骨架的好入口：

[attention_engine/core/template/attn_template.py:28-32](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/template/attn_template.py#L28-L32) 不传任何字段直接渲染，相当于「把所有洞填成空串」，打印出来的就是带空洞的骨架——本讲实践会用到它。

**三种模板包装的差异**（横向对比）：

- `TlAttnTemplate`（attn_template.py）与 `TlBlockAttnTemplate`（blockattn_template.py）几乎逐行相同，只是指向不同的骨架文件（`attn_tl.py` vs `blockattn_tl.py`）。
- `TlLinearAttnTemplate`（linear_attn_template.py）也相同，但它把 `open(...).read()` 放在**模块顶层**（文件被 import 时就读模板），而 attn/block 版放在 `__init__` 里。功能等价。
- `CuteAttnTemplate`（cute_template.py）与前两者**结构不同**：CuTe 后端的产物是多个 C++/头文件，所以它不是渲染单个文件，而是 `os.walk` 遍历整个 `cute_template/` 目录，对**每个文件**分别 `Template.render`，再写进 `cute_template_output/` 落盘。它的 None 处理是「直接丢弃该键」（`{k: v for k, v in kwargs.items() if v is not None}`），而非转空串。这部分细节留到 u5-l1（CuTe 后端）展开，本讲只点出差异。

#### 4.1.4 代码实践

**实践目标**：亲眼看到「带空洞的骨架」长什么样，体会「骨架+洞」的设计。

**操作步骤**：

1. 在项目根目录确认 `PYTHONPATH` 已挂上 `attention_engine/`（参考 u1-l2）。
2. 直接运行包装类的主程序：
   ```bash
   python -m attention_engine.core.template.attn_template
   ```
   等价于执行 `attn_template.py` 末尾的 `__main__`。

**需要观察的现象**：

- 终端会打印一大段 TileLang 代码，其中 `def score_mod(` 后面是空的、`def online_func(` 后面也是空的、`T.fill({{o_scale_varname}}, 1.0)` 会变成 `T.fill(, 1.0)`——这就是「洞」没填的样子。
- 随后 `exec(tl_code)` 这一行**大概率会报语法错误**（因为空洞让代码不完整）。这是**预期**的：说明骨架必须配齐字段才能编译，空渲染只用来「看结构」。

**预期结果**：你能从打印结果里找到 `@T.macro`、`@T.prim_func`、`with T.Kernel(...)`、`T.gemm(...)` 这些真实存在的骨架代码，且原本是 `{{...}}` 的位置现在变成空或残缺——这验证了「骨架是固定的，洞是可变的」。

**待本地验证**：`exec` 是否报错取决于 TileLang 版本对不完整代码的容忍度；若报错属正常，关注打印出的骨架文本即可。

#### 4.1.5 小练习与答案

**练习 1**：为什么渲染前必须做 `kargs = {k: (v if v is not None else "") ...}` 这一步？如果不做会怎样？

**参考答案**：因为 Jinja2 会把 `None` 渲染成字符串 `"None"`。例如反向场景里若不使用 doosum，`custom_bwd_inputs` 字段为 `None`，灌进 `{{custom_bwd_inputs}}` 后会变成 `None: T.Buffer(...)` 这样的非法 Python 形参声明，导致生成的源码语法错误、无法 `exec`。转成空串则让它「消失」于形参列表中。

**练习 2**：`TlAttnTemplate` 的 `__init__` 做渲染，`__call__` 做返回。为什么不直接在 `__call__` 里渲染？

**参考答案**：分离「构造（耗时、一次性）」与「取值」。渲染只应在拿到全部字段时做一次；`__call__` 仅用于取出已渲染好的 `self.tlcode`，可被多次读取而不重复渲染。

---

### 4.2 attn_tl.py 模板的结构：@T.macro 与 @T.prim_func 骨架

#### 4.2.1 概念说明

`attn_tl.py` 本身就是一份**合法的 TileLang 程序**（只差 `{{...}}` 未填）。它定义了完整的 softmax 注意力前向+反向+PyTorch 包装。理解它的结构，就理解了「降级字段最终落在程序的哪些位置」。

文件里有两种 TileLang 装饰器，要分清：

- **`@T.macro`**：定义一段「可被调用的代码模板」。调用它（如 `score_mod(...)`）会把宏体**内联**到调用处。AttentionEngine 把 `score_mod`、`online_func` 做成 macro，这样同一份「函数定义 + 函数体」既能在前向被调用，逻辑也由降级层统一注入。
- **`@T.prim_func`**：定义一个**完整的 kernel 原语函数**，是真正会被 `tl.compile` 编译成 GPU 代码的入口（如 `main`、`flash_bwd`、`flash_bwd_prep`）。

文件顶层结构（按出现顺序）如下，每个区块都是一段「骨架」：

| 区块 | 大致行号 | 作用 |
|------|----------|------|
| 导入与设备检测 | 1–21 | import torch/tilelang、按 GPU capability 选设备模型 |
| 辅助函数 | 24–48 | `fast_tanh`、`make_dq_layout`、`get_configs`（autotune 配置空间） |
| `kernel(...)` 工厂 | 51–192 | 返回前向 prim_func `main`，内含 score_mod/online_func 两个 macro |
| `flashattn_bwd_preprocess` | 196–223 | 反向预处理：算 Delta |
| `flashattn_bwd(...)` 工厂 | 239–433 | 返回反向 prim_func `flash_bwd`，内含 score_mod/score_mod_backward macro |
| `flashattn_bwd_postprocess` | 436–455 | 反向后处理：dQ 类型转换 |
| 调优与编译 | 460–561 | `TUNE` 开关、`tune(...)` 缓存逻辑、`tl.compile(...)` |
| PyTorch 接口 | 567–603 | `_attention(torch.autograd.Function)`，把上面编译好的 mod 包装成可 `.backward()` 的算子 |

文件里的注释 `# TL_KERNEL = """`、`# TL_MAIN = """` 等是**历史遗留的区块标记**（现在整文件一起渲染，注释只起导航作用）。

#### 4.2.2 核心流程

前向 kernel 的执行骨架（去掉洞后）是这样的：

```
def kernel(batch, heads, seq_len, dim, dimv, tune=False):
    def kernel_func(block_M, block_N, num_stages, thread_num, shared_fuse):

        @T.macro
        def score_mod(...):  <— {{score_mod_func_def}}  用户分数变换

        @T.macro
        def online_func(...): <— {{online_func_def}}    用户 online 递推

        @T.prim_func
        def main(Q, K, V, <custom_fwd_inputs>, Output, <final_rowscales>):
            启动 T.Kernel 网格
            分配 shared/fragment
            <初始化 custom 输入 / online_rowscales>
            for k in T.Pipelined(loop_range):     # 遍历 KV 块
                T.copy(K -> K_shared)
                T.gemm(Q_shared, K_shared -> scores)
                score_mod(...)                     # {{call_score_mod}}
                online_func(...)                   # {{call_online_func}}
                acc_o *= o_scale; 更新 rowscales   # {{online_rowscales_update}}
                T.gemm(scores, V_shared -> acc_o)
            online_func 收尾                        # {{online_func_epilogue}}
            存 Output 与 final_rowscales            # {{final_rowscales_save}}
        return main
```

反向 `flash_bwd` 结构对称：先 `score_mod(...)` 重算前向 scores，再 `score_mod_backward(...)` 算 `dscores`，沿 `T.Pipelined` 累加 `dK/dV/dQ`。

> 小提示：模板里变量名拼写为 `is_casual`（应为 `is_causal`）。这是项目里一处历史拼写，但**模板与降级层（`lowerOutput.is_casual`）保持一致**，所以能正常工作。读源码时别被它误导。

#### 4.2.3 源码精读

**前向 macro 定义**——两个用户逻辑的注入点：

[attention_engine/core/template/tl_template/attn/attn_tl.py:68-76](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/template/tl_template/attn/attn_tl.py#L68-L76) 这里先写 `@T.macro`，下一行放 `{{score_mod_func_def | indent(8)}}`。`func_block` 产出的字符串以 `def score_mod(...):` 开头，正好接在 `@T.macro` 下方。`indent(8)` 负责把函数体多行对齐到第 8 列。`online_func_def` 同理（L73）。

**前向 prim_func 签名**——张量形参也由降级层决定：

[attention_engine/core/template/tl_template/attn/attn_tl.py:76-85](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/template/tl_template/attn/attn_tl.py#L76-L85) `main` 的形参里，`Q/K/V/Output` 是固定的，但 `{{custom_fwd_inputs}}`（L81）和 `{{final_rowscales_output}}`（L84）是动态的——前者随用户 `CustomIO` 数量变化，后者随 `online_func.final_rowscales` 数量变化（如 online softmax 的 lse）。

**前向主循环前的初始化段**——多个洞集中处：

[attention_engine/core/template/tl_template/attn/attn_tl.py:107-111](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/template/tl_template/attn/attn_tl.py#L107-L111) 这里 `T.fill(acc_o, 0)` 把累加器清零、`T.fill({{o_scale_varname}}, 1.0)` 初始化重缩放因子、`{{online_rowscales_initvalue}}` 初始化行级状态（如行最大值 m 初值 `-1e38`）。注意 `acc_o` 在模板里被**硬编码**引用（因为它对所有注意力都是同一个累加器名），而 `o_scale_varname` 是变量名占位符——因为不同 online_func 用不同的重缩放变量名。

**反向 macro 定义**——score_mod 的前向+反向两段：

[attention_engine/core/template/tl_template/attn/attn_tl.py:255-269](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/template/tl_template/attn/attn_tl.py#L255-L269) 反向里 `score_mod`（L255-261）用 `{{score_mod_fwd_inputs}}` + `{{score_mod_fwd_body}}` 重算前向 scores；`score_mod_backward`（L263-269）用 `{{score_mod_backward}}` 做符号 autodiff 得到的反向体。这两段在 u2-l5 已讲过来源。

**PyTorch 接口**——最终的可调用形态：

[attention_engine/core/template/tl_template/attn/attn_tl.py:567-603](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/template/tl_template/attn/attn_tl.py#L567-L603) 渲染完成后，文件末尾会定义 `_attention(torch.autograd.Function)`，`forward` 调全局 `mod`、`backward` 调 `mod_prep/mod_bwd/mod_post`。`{{isused_doosum}}`（L593/L595）控制是否启用 doosum 预处理路径。最后 `attention = _attention.apply` 就是引擎挂出来的可调用对象。

#### 4.2.4 代码实践

**实践目标**：理解 `@T.macro` 与 `@T.prim_func` 的分工。

**操作步骤**：

1. 打开 `attn_tl.py`，分别在前向（L69/L76）和反向（L255/L271）数一下有几个 `@T.macro` 和几个 `@T.prim_func`。
2. 在前向 `main`（L141-142、L148）和反向 `flash_bwd`（L351、L399）里，找到这些 macro 被**调用**的位置。

**需要观察的现象**：

- 每个被降级层填进 macro 体的逻辑（score_mod / online_func），都在 kernel 主体里恰好有一次对应调用（`score_mod(...)`、`online_func(...)`）。
- macro 是「定义一次、调用 N 次」，调用处靠 `{{call_score_mod}}` / `{{call_online_func}}` 注入调用语句。

**预期结果**：你能列出一个对应关系——`score_mod_func_def`（定义）↔ `call_score_mod`（调用），`online_func_def`（定义）↔ `call_online_func`（调用）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `score_mod` 用 `@T.macro` 而不是写成普通 Python 函数？

**参考答案**：TileLang 的 `@T.macro` 会在编译期把宏体**内联**到调用点，等价于把 score_mod 的逻辑直接写进 kernel 循环里，不产生真实的函数调用开销，也方便编译器做寄存器/流水线优化。普通 Python 函数无法被 `tl.compile` 当作 kernel 内联代码处理。

**练习 2**：模板里 `acc_o` 这个名字是写死的，而 `o_scale_varname` 用占位符。为什么差别对待？

**参考答案**：`acc_o`（输出累加器）对所有注意力变体都存在、名字统一，所以骨架直接硬编码引用它（如 `T.fill(acc_o, 0)`、`T.gemm(..., acc_o, ...)`）。而「重缩放变量」的名字由 `lower_online_func` 根据 online_func 具体实现派生（不同算法可能用不同变量名），所以模板只能留 `{{o_scale_varname}}` 占位。

---

### 4.3 占位符与降级字段的对应关系

#### 4.3.1 概念说明

这是本讲的核心：**模板里每个 `{{...}}` 到底对应谁**。理解这张「接线表」，就把整条编译链从用户 API 一直连到了最终代码的每一行。

关键认知：渲染时传给模板的字段字典，是由 `lower_tl` 末尾用 `**` 把多个对象的 `__dict__` 展开**合并**而成的。所以一个占位符属于「哪个字段」，取决于它由哪个降级对象产出。注意：Jinja2 占位符名就是字段字典的键名；**键名相同就互相覆盖**，所以 `final_rowscales_output` 同时出现在 fwd 和 bwd 两处（L84、L282）会拿到同一个值——这是有意为之。

#### 4.3.2 核心流程

`lower_tl` 在末尾这样把字段喂给模板（这是「渲染调用点」）：

[attention_engine/core/lower/lower.py:740-756](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/lower/lower.py#L740-L756) `tlattn_template(**kwargs)()` ——构造时渲染、紧接 `()` 取出源码字符串。`kwargs` 由六组来源合并：

```
显式命名：     custom_fwd_inputs, custom_fwd_inputs_init,
              final_rowscales_output, final_rowscales_save,
              custom_fwd_inputs_load_prolog, output_idx_list, bwd_output_idx_list
展开对象：     **lower_custom_inputs_output.__dict__   (customInputOutput)
              **lower_online_func_output.__dict__     (lowerOnlineFuncOutput)
              **lower_score_mod_output.__dict__       (lowerScoreModOutput)
              **lower_output.__dict__                 (lowerOutput)
              **tune_output.__dict__                  (TunnerOutput)
              **tune_output_bwd.__dict__              (TunnerOutputBwd)
```

理解了这个合并关系，下面的对应表就一目了然。

#### 4.3.3 源码精读（占位符 ↔ 降级字段对应表）

下面这张表是本讲最重要的产物。**「来源对象」列**回答了「这个字符串由谁产出」，**「产生它的降级函数/步骤」列**回答了「在哪段代码里被生成」。

**A. 来自 `lowerOutput`（`lower_output.__dict__`，定义在 lower.py L182-217）**

| 占位符（行） | 来源字段 | 产生位置 | 含义 |
|--------------|----------|----------|------|
| `{{swizzle_shared}}`（L103） | `swizzle_shared` | `lower_custom_inputs`（L597）累加 | custom 输入 shared 张量的 swizzle 布局声明 |
| `{{tl_dtype}}`（L57） | `tl_dtype` | `lower_tl`（L635） | 计算精度，如 `"float16"` |
| `{{is_casual}}`（L61） | `is_casual` | `lower_tl`（L729/L765） | 是否因果截断 |
| `{{is_inf_mask}}`（L124） | `is_inf_mask` | `lower_tl`（L637） | 掩码值是否 `-inf` |
| `{{BATCH}}/{{HEADS}}/{{SEQ_LEN}}/{{DIM}}/{{DIMV}}`（L467-471,523-524） | 同名字段 | `lower_tl`（L630-634） | 问题形状 |
| `{{q_idx}}/{{kv_idx}}/{{batch_idx}}/{{head_idx}}`（L126-129） | 同名字段 | `lower_tl`（L713-716）从 torch.fx 节点名取 | mask 循环里的下标变量名 |
| `{{mask_mod_code}}`（L130） | `mask_mod_code` | `lower_tl`（L718）经 `tl_codegen_from_torchfx` | mask 逐元素代码 |
| `{{is_mask_mod_code}}`（L124） | `is_mask_mod_code` | `lower_tl`（L719） | 是否启用元素级 mask 补丁 |
| `{{mask_output}}`（L132） | `mask_output` | `lower_tl`（L717） | mask 输出布尔变量名 |
| `{{o_scale_varname}}`（L108,156） | 注意：来自 `lowerOnlineFuncOutput` | `lower_online_func`（L363） | 重缩放变量名 |

**B. 来自 `lowerScoreModOutput`（`lower_score_mod_output.__dict__`，lower.py L247-258）** —— 由 `lower_score_mod` 产出

| 占位符（行） | 来源字段 | 含义 |
|--------------|----------|------|
| `{{score_mod_func_def}}`（L70） | `score_mod_func_def` | 前向 score_mod macro 定义（`func_block` 拼出，common.py L110-118） |
| `{{call_score_mod}}`（L142） | `call_score_mod` | 前向循环内调用 score_mod 的语句（`call_op`，common.py L29-30） |
| `{{score_mod_fwd_inputs}}`（L258） | `score_mod_fwd_inputs` | 反向 score_mod macro 的形参声明 |
| `{{score_mod_fwd_body}}`（L260） | `score_mod_fwd_body` | 反向重算前向 scores 的函数体 |
| `{{score_mod_bwd_inputs}}`（L266） | `score_mod_bwd_inputs` | score_mod_backward macro 的形参声明 |
| `{{score_mod_backward}}`（L268） | `score_mod_backward` | 符号 autodiff 得到的反向体 |
| `{{score_mod_inputs_bwd_list}}`（L351） | `score_mod_inputs_bwd_list` | 反向调用 score_mod 的实参列表 |
| `{{score_mod_bwd_inputs_list}}`（L399） | `score_mod_bwd_inputs_list` | 反向调用 score_mod_backward 的实参列表 |
| `{{score_mod_output_var}}`（L367） | `score_mod_output_var` | 反向里 scores 输出变量名 |
| `{{score_mod_bwd_inputs_declare}}`（L313） | `score_mod_bwd_inputs_declare` | 反向 fragment 张量声明 |
| `{{score_mod_bwd_inputs_declare_shared}}`（L315） | `score_mod_bwd_inputs_declare_shared` | 反向 shared 张量声明 |

**C. 来自 `lowerOnlineFuncOutput`（`lower_online_func_output.__dict__`，lower.py L220-243）** —— 由 `lower_online_func` 产出

| 占位符（行） | 来源字段 | 产生步骤 | 含义 |
|--------------|----------|----------|------|
| `{{online_rowscales_initvalue}}`（L110） | `online_rowscales_initvalue` | 步骤2（L343-346） | 行级状态初值（如 m=`-1e38`、r=`0.0`） |
| `{{online_func_def}}`（L73） | `online_func_def` | 步骤3（L356-358） | online_fwd macro 定义 |
| `{{call_online_func}}`（L148,152） | `call_online_func` | 步骤3（L359） | 循环内调用 online_func |
| `{{online_rowscales_update}}`（L159） | `online_rowscales_update` | 步骤4（L366-372） | 把新 rowscales 拷回旧变量 |
| `{{online_func_epilogue}}`（L167） | `online_func_epilogue` | 步骤5（L381） | 收尾（除以 r、算 lse） |
| `{{online_func_fwd}}`（L357） | `online_func_fwd` | 步骤7反向（L432-433） | 反向里重算 scores 的 online forward |
| `{{custom_bwd_body}}`（L396） | `custom_bwd_body` | 步骤7反向（L439-440） | online 反向体（算 dscores） |
| `{{custom_bwd_inputs}}`（L285） | `custom_bwd_inputs` | 步骤7反向（L445） | doosum 形参声明（按需） |
| `{{custom_bwd_inputs_init}}`（L310） | `custom_bwd_inputs_init` | 步骤7反向（L448） | doosum shared 分配 |
| `{{custom_bwd_inputs_load}}`（L379） | `custom_bwd_inputs_load` | 步骤7反向（L452） | doosum 加载 |
| `{{final_rowscales_load}}`（L354） | `final_rowscales_load` | 步骤7反向（L450-451） | 反向加载 final_rowscales |
| `{{final_rowscales_shared_init}}`（L307） | `final_rowscales_shared_init` | 步骤7反向（L446-447） | 反向 final_rowscales shared 声明 |
| `{{isused_doosum}}`（L593,595） | `isused_doosum` | 步骤7反向（L442-443） | 是否启用 doosum 路径 |
| `{{final_rowscales_length}}`（L587） | `final_rowscales_length` | 步骤7反向（L453） | final_rowscales 个数 |

**D. 来自 `customInputOutput`（`lower_custom_inputs_output.__dict__`，lower.py L261-264）** —— 由 `lower_custom_inputs` 产出

| 占位符（行） | 来源字段 | 含义 |
|--------------|----------|------|
| `{{custom_fwd_inputs_load_shared}}`（L121） | `custom_fwd_inputs_load_shared` | global→shared 加载（含 block_N 的 custom 输入） |
| `{{custom_fwd_inputs_load_s2r}}`（L140） | `custom_fwd_inputs_load_s2r` | shared→fragment 拷贝 |
| `{{custom_fwd_inputs_load_shared_bwd}}`（L346） | `custom_fwd_inputs_load_shared_bwd` | 反向加载（当前常为空） |

**E. 来自 `lowerKernelBaseOutput`（kernel_code_template，由 `lower_kernel` 填充后**显式命名**传入）**

> ⚠️ **命名陷阱**：`{{custom_fwd_inputs}}`（L81）这个名字会让人以为它来自用户的 `CustomIO` 对象，**其实不是**。它接的是 `kernel_code_template.input_args`——即 `lower_kernel` 生成的「输入张量形参声明字符串」。之所以叫这个名字，是因为这些形参正是用户的 custom 输入张量。

| 占位符（行） | 显式传入的字段（lower.py L741-745） | 由谁填充 | 含义 |
|--------------|----------------------|----------|------|
| `{{custom_fwd_inputs}}`（L81,279） | `custom_fwd_inputs=kernel_code_template.input_args` | `lower_kernel`（L280） | kernel 形参里的输入张量声明 |
| `{{custom_fwd_inputs_init}}`（L98） | `custom_fwd_inputs_init=kernel_code_template.alloc` | `lower_kernel`（L294） | shared/fragment 分配语句 |
| `{{final_rowscales_output}}`（L84,282） | `final_rowscales_output=kernel_code_template.output_args` | `lower_kernel`（L286） | final_rowscales 输出张量声明 |
| `{{final_rowscales_save}}`（L172） | `final_rowscales_save=kernel_code_template.output_args_copy_epilogue` | `lower_kernel`（L305） | reg/shared→global 回存语句 |
| `{{custom_fwd_inputs_load_prolog}}`（L106） | `custom_fwd_inputs_load_prolog=kernel_code_template.input_args_copy_prologue` | `lower_kernel`（L315） | global→reg/shared 预加载 |

**F. 来自调优配置 `TunnerOutput` / `TunnerOutputBwd`**

| 占位符（行） | 来源字段 | 含义 |
|--------------|----------|------|
| `{{TUNE}}`（L460） / `{{TUNE_BWD}}`（L462） | `TUNE` / `TUNE_BWD` | 是否启用前向/反向自动调优 |
| `{{TUNE_FILE}}`（L461） / `{{TUNE_FILE_BWD}}`（L463） | `TUNE_FILE` / `TUNE_FILE_BWD` | 调优结果缓存文件路径 |
| `{{block_M}}/{{block_N}}/{{stages}}/{{thread_num}}/{{shared_fuse}}`（L516-520） | 同名字段 | 前向分块/流水线/线程配置 |
| `{{block_M_bwd}}/{{block_N_bwd}}/{{thread_num_bwd}}`（L552-554） | 同名字段 | 反向分块/线程配置 |

**G. 显式计算的输出索引列表**（`output_idx_list` / `bwd_output_idx_list`）

`{{output_idx_list}}`（L182,527,572）与 `{{bwd_output_idx_list}}`（L423,560）不是降级对象字段，而是 `lower_tl`（L689-703）根据 custom 输入数、final_rowscales 数、doosum 标志**当场计算**出的整数列表，告诉 TileLang 编译器 kernel 的哪些输出张量要返回。

#### 4.3.4 代码实践（本讲主实践）

**实践目标**：亲手完成「占位符 → 降级字段」的接线，验证本节的对应表。

**操作步骤**：

1. 打开 `attention_engine/core/template/tl_template/attn/attn_tl.py`，用搜索功能定位下面四个占位符，记录它们的行号与所在上下文（在前向还是反向、在 macro 还是 prim_func 里）：
   - `{{online_func_def}}`
   - `{{score_mod_func_def}}`
   - `{{online_rowscales_initvalue}}`
   - `{{custom_fwd_inputs}}`
2. 对每个占位符，按下表写出「来源对象 → 产生它的降级函数 → 那段代码在哪一行赋值」，对照本节对应表逐项核对。

**需要观察的现象 / 预期结果**（参考答案）：

| 占位符 | 模板位置 | 来源对象 | 产生它的降级函数（文件:行） |
|--------|----------|----------|------------------------------|
| `{{online_func_def}}` | L73（前向 macro） | `lowerOnlineFuncOutput.online_func_def` | `lower_online_func`（lower.py L356-358） |
| `{{score_mod_func_def}}` | L70（前向 macro） | `lowerScoreModOutput.score_mod_func_def` | `lower_score_mod`（lower.py L494） |
| `{{online_rowscales_initvalue}}` | L110（前向初始化） | `lowerOnlineFuncOutput.online_rowscales_initvalue` | `lower_online_func`（lower.py L343-346） |
| `{{custom_fwd_inputs}}` | L81（前向形参）、L279（反向形参） | `kernel_code_template.input_args`（显式传入） | `lower_kernel`（lower.py L280） |

3. **进阶**：在 `lower_tl` 的渲染调用点（lower.py L740-756）确认：`online_func_def` 是通过 `**lower_online_func_output.__dict__` 进入字段字典的，而 `custom_fwd_inputs` 是通过**显式命名参数** `custom_fwd_inputs=kernel_code_template.input_args` 进入的——两条路径不同。

**预期结果**：你完成了一张「占位符 → 降级函数」的对照表，并能解释为什么 `{{custom_fwd_inputs}}` 是命名陷阱（它来自 `lower_kernel`，而非用户 CustomIO 对象本身）。

#### 4.3.5 小练习与答案

**练习 1**：`{{final_rowscales_output}}` 在模板里出现两次（L84 前向、L282 反向），但只传一个值。这合理吗？

**参考答案**：合理。前向和反向都需要声明 final_rowscales 张量（前向输出、反向作为输入复用），且它们引用的是**同一组**张量声明字符串（`kernel_code_template.output_args`）。一个字段灌两个洞，正好保证前后向形参声明一致。Jinja2 里同名占位符拿到的是同一个字典值。

**练习 2**：如果你新增了一种 `online_func`，它多产出一个 `final_rowscales` 张量，模板需要改吗？

**参考答案**：**不需要改模板**。这正是模板+降级分离的好处——`final_rowscales` 的数量是动态的：`lower_online_func` 会把它注册成 kernel 输出（L385-392），`lower_kernel` 据此生成形参声明与回存语句，最终通过 `{{final_rowscales_output}}` / `{{final_rowscales_save}}` 自动注入。模板里的洞会容纳任意长度的字符串。

**练习 3**：`{{isused_doosum}}` 出现在 `if {{isused_doosum}}:`（L593）。它的值 `"True"` / `"False"` 是怎么来的？

**参考答案**：来自 `lowerOnlineFuncOutput.isused_doosum`，在 `lower_online_func` 的反向分支（lower.py L442-443）判定：若反向 `dscores` 的符号 DAG 真的引用了 `doosum_shared`，则置为 `True`，否则 `False`。它控制反向是否走 doosum 预处理路径（`mod_prep` 算 Delta）。

## 5. 综合实践

**任务**：以 `attn_script/mha.py` 的标准 softmax 注意力为对象，画出一条从「用户 OnlineFunc 方法」到「最终 kernel 代码某一行」的完整数据流，并在中间标注本讲的「模板渲染」环节。

**操作步骤**：

1. 打开 `attn_script/mha.py`，找到用户定义的 `OnlineSoftmax` 类，记下它的 `online_fwd` 方法里那句「更新行最大值并算 o_scale」的逻辑（类似 `m_new = m.max(...)`）。
2. 这段逻辑经 `lower_online_func` 步骤 3（lower.py L349-358）符号化 + 发射，产出 `online_func_def` 字符串。
3. 该字符串通过 `**lower_online_func_output.__dict__`（lower.py L747）进入字段字典。
4. 在渲染调用点（lower.py L740）实例化 `TlAttnTemplate`，Jinja2 把它灌进 `attn_tl.py` 的 L73 `{{online_func_def | indent(8)}}`。
5. 运行 u4.1.4 的 `python -m attention_engine.core.template.attn_template` 观察空骨架，再对照本节对应表，在脑中把 `{{online_func_def}}` 替换成 mha 的真实 online 逻辑。
6. （可选）运行 `mha.py` 后，到 `attention_engine/attn_engine/cache/` 目录下找到以 md5 命名的缓存 `.py` 文件，用编辑器打开，搜索 `def online_func`，亲眼确认 L73 的洞已被填上真实代码。

**预期结果**：你能写出一条链：

```
用户 OnlineSoftmax.online_fwd  (mha.py)
  → 符号化+发射 lower_online_func.online_func_def  (lower.py:356)
  → 字段字典 **lower_online_func_output.__dict__   (lower.py:747)
  → TlAttnTemplate 渲染 attn_tl.py#L73             (lower.py:740)
  → 缓存为 md5.py，importlib 加载                   (u3-l3)
  → kernel 内联进 @T.macro online_func 调用处        (attn_tl.py:148)
```

**待本地验证**：步骤 6 的 cache 目录路径与文件名（md5）取决于引擎实现，若找不到可改用步骤 5 的空骨架观察法。

## 6. 本讲小结

- **模板层是编译链最后一层**：用 Jinja2 把降级层产出的字符串片段「灌」进带 `{{...}}` 洞的骨架程序，产出完整可编译的 TileLang 源码。
- **渲染流程四步**：读骨架 → `jinja2.Template` 编译 → `None` 转空串 → `template.render(**kargs)`；`TlAttnTemplate` / `TlBlockAttnTemplate` / `TlLinearAttnTemplate` 同构，`CuteAttnTemplate` 改为整目录渲染落盘。
- **`attn_tl.py` 是一份骨架程序**：用 `@T.macro` 定义可内联的 score_mod/online_func 逻辑、用 `@T.prim_func` 定义 kernel 入口（main/flash_bwd），骨架固定、洞口可变。
- **占位符与降级字段一一对应**：字段字典由 `lower_tl` 末尾把六个降级对象的 `__dict__` 合并而成；同名占位符取同值（如 `final_rowscales_output` 前后向共用）。
- **命名陷阱**：`{{custom_fwd_inputs}}` 接的是 `lower_kernel` 产出的形参声明字符串，而非用户 CustomIO 对象本身。
- **`indent` 过滤器与 None 处理**是渲染正确性的两个关键细节：前者对齐多行代码，后者避免渲染出非法的 `"None"`。

## 7. 下一步学习建议

- **u3-l2（完整 lower_tl 链路）**：本讲只看了「模板被调用」的最后一行，下一讲会顺着 `lower_tl` 从头走到尾，看降级三件套+mask+kernel 是如何按顺序编排、最后交到模板手里的。
- **u3-l3（引擎入口：分发、编译、缓存）**：本讲产出的源码字符串如何被 md5 缓存、`importlib` 动态加载成 `self.attention`，下一讲会完整揭示。
- **u5-l1（CuTe 后端代码生成）**：本讲只点了 `CuteAttnTemplate` 的差异，CuTe 后端的整目录渲染与 C++ 模板细节留到第五单元深入。
- **延伸阅读**：可对照 `blockattn_tl.py`、`linear_tl.py` 两个骨架，体会「同一套渲染机制，不同骨架」如何支撑 blocksparse 与线性注意力两种变体。
