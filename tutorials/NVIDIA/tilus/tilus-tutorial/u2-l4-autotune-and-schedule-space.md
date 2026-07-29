# 自动调优：@autotune 与调度空间

## 1. 本讲目标

本讲解决一个问题：**同一个内核写好后，怎么让 Tilus 自动帮你选出最快的分块/线程配置？**

学完后你应当能够：

- 用 `@tilus.autotune` 装饰器为一个 `tilus.Script` 声明调优空间；
- 说清 `span_space` 如何把声明出的空间做笛卡尔积展开成一份份「调度（schedule）」；
- 描述 `generate_schedules` 如何把每份调度绑定到 `__init__`，以及首次调用内核时如何并行编译、逐个 benchmark 并选出最优调度；
- 知道调优结果落在缓存目录的哪些文件里，以及为何换机器后要重新调优。

本讲是 [u2-l1](u2-l1-script-init-call-semantics.md) 的直接延续：u2-l1 讲清了 `Script` 实例化与 `__call__` 参数（const / tuning / kernel 三分），本讲聚焦于 `__init__` 那批「编译期超参」是如何被自动搜索的。

## 2. 前置知识

在进入源码前，先用三段白话建立直觉。

**什么是「调度（schedule）」？** 一个内核里有一批「不影响结果、只影响性能」的超参，例如分块大小 `block_m/block_n/block_k`、每块 warp 数 `num_warps`。给这些超参填入一组具体数值，就得到一份「调度」。同一份内核代码，不同调度编译出的 CUDA 是不同的（分块不同、网格不同、寄存器分配不同），性能也不同。

**为什么要自动调优？** 最优调度取决于硬件（Ampere/Hopper/Blackwell 表现不同）和输入规模（4096×4096 和 8192×8192 的最优分块可能不同），而这些在写内核时无法预知。手动试每一个组合既慢又容易漏。自动调优的核心思路很简单：**把所有候选调度都编译出来，用真实输入跑一遍，谁快选谁**。这一点官方文档 `autotuning.rst` 开篇就讲明了。

**调优参数和 JIT 是什么关系？** 这是最容易混的两件事，务必分清：

| 概念 | 来自哪里 | 改变它的后果 |
|------|----------|--------------|
| 调优参数（schedule） | `__init__` 的形参 | 在调优空间里枚举，每份都编译一份程序 |
| 常量参数（const） | `__call__` 里标 `int/float/bool/str` 的形参 | 值一变就触发**重新 JIT 编译** |
| 调优指纹（tuning_key） | `__call__` 里标 `int32/int64` 的整数形参 | 只影响**选哪份已编译的调度**，不重编译 |

`@autotune` 只管第一行——`__init__` 的调优空间。后两行是 u2-l1 的内容，本讲在「benchmark 选优」一节会用到 tuning_key，但不会重复展开。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
|------|------|
| [python/tilus/lang/script.py](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/script.py) | 定义 `Script` 基类与 `autotune` 装饰器。装饰器只负责「标记」调优空间。 |
| [python/tilus/lang/instantiated_script.py](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/instantiated_script.py) | 调优的核心实现：`span_space`（展开）、`generate_schedules`（绑定）、`JitInstance`（编译 + benchmark + dispatch 表）。 |
| [python/tilus/option.py](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/option.py) | `bench_warmup`/`bench_repeat`/`parallel_workers` 三个影响调优行为的全局开关。 |
| [examples/matmul/matmul_v2.py](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/examples/matmul/matmul_v2.py) | 官方 autotune 示例，本讲反复引用它的 `@autotune` 写法。 |
| [examples/matmul/matmul_v0.py](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/examples/matmul/matmul_v0.py) | 综合实践的改造对象（naive matmul）。 |
| [docs/source/programming-guides/autotuning.rst](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/docs/source/programming-guides/autotuning.rst) | 官方调优指南，讲清理念与硬件感知缓存。 |

## 4. 核心概念与源码讲解

### 4.1 @autotune 装饰器：声明调优空间

#### 4.1.1 概念说明

`@tilus.autotune(arg_names, arg_values)` 是一个**装饰器工厂**：调用它得到一个真正的装饰器，再把装饰器套到 `Script` 子类上。它的唯一职责是把「我想调哪些 `__init__` 形参、每个形参有哪些候选值」这件事**记录**到类身上。

一个关键设计：**它什么都不展开、什么都不编译**。它只把信息塞进类属性 `_autotune_space`。真正的展开是后面懒加载做的。这种「先标记、后展开」的分离，让你可以堆叠多个 `@autotune`，也让展开逻辑可以独立测试。

`arg_names` 支持用逗号写多个名字（如 `"block_m, block_n"`），这时 `arg_values` 要给一组**元组**，每个元组按顺序对应这几个名字——这样把「天然要一起出现」的组合绑死，避免产生无意义的配置（比如 `block_m=128, block_n=128` 合理，但你不一定想试遍所有交叉组合）。

#### 4.1.2 核心流程

`@autotune` 的执行可以画成：

```
@tilus.autotune("num_warps", [4, 8])          # 装饰器3（最外层）
@tilus.autotune("block_m, block_n", [(128,128),(128,64)])  # 装饰器2
@tilus.auttune("block_k", [16, 32])           # 装饰器1（最内层，先执行）
class MatmulV2(tilus.Script): ...
```

1. Python 装饰器**自下而上**执行：先装饰器1，再装饰器2，再装饰器3。
2. 每个装饰器都执行同一个 `decorator(script_cls)`：
   - 若类上还没有 `_autotune_space`，就新建一个空 dict；
   - 做**两道校验**：不允许重复指定同一个形参名；多名字时候选值必须能正确解包；
   - 把 `arg_names`（原始字符串）作为 key、`arg_values` 作为 value 存进 dict。
3. 最终类上得到一个 `_autotune_space = {"block_k": [...], "block_m, block_n": [...], "num_warps": [...]}`。

注意：dict 的 key 是**原始的逗号字符串**（如 `"block_m, block_n"`），不是拆开后的列表。拆开是 `span_space` 的事。装饰器阶段刻意保持原样。

#### 4.1.3 源码精读

`autotune` 的定义在 [python/tilus/lang/script.py:L106-L152](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/script.py#L106-L152)。它返回内部的 `decorator`，下面是关键部分：

```python
def decorator(script_cls: T) -> T:
    if not hasattr(script_cls, "_autotune_space"):
        setattr(script_cls, "_autotune_space", {})
    space = getattr(script_cls, "_autotune_space")
    names = [name.strip() for name in arg_names.split(",")]
    ...
    space[arg_names] = arg_values          # 用原始字符串作 key
    setattr(script_cls, "_autotune_space", space)
    return script_cls
```

- [script.py:L124-L126](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/script.py#L124-L126)：`hasattr` 检查保证多个装饰器**累积**到同一个 dict，而不是互相覆盖。这是「堆叠多个 @autotune」能生效的根因。
- [script.py:L131-L133](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/script.py#L131-L133)：重复名校言校验。若同一个形参名在两次 `@autotune` 里都出现，直接抛 `RuntimeError`，避免歧义。
- [script.py:L137-L144](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/script.py#L137-L144)：解包校验。当 `arg_names` 含逗号（多名字）时，要求每个候选值都是一个长度匹配的序列，否则抛 `TypeError`。
- [script.py:L146](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/script.py#L146)：`space[arg_names] = arg_values`——注意 key 是**原始字符串**。

`autotune` 通过 [python/tilus/__init__.py:L102](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/__init__.py#L102) 导出为 `tilus.autotune`，所以用户直接写 `@tilus.autotune(...)` 即可。

官方文档 [autotuning.rst:L43-L65](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/docs/source/programming-guides/autotuning.rst#L43-L65) 给了一个 9 组合的例子，明确说明「最终调优空间是所有装饰器调用中数值的笛卡尔积」「同一个超参不能重复标注」，与源码两道校验一一对应。

#### 4.1.4 代码实践

**实践目标**：亲手验证「装饰器只标记、不展开」，并观察 `_autotune_space` 的累积。

**操作步骤**（纯 Python，无需 GPU）：

1. 在能 `import tilus` 的环境里新建一个脚本，定义一个带多个 `@autotune` 的空 Script：
   ```python
   import tilus

   @tilus.autotune("num_warps", [4, 8])
   @tilus.autotune("block_m, block_n", [(128, 128), (128, 64), (64, 128)])
   @tilus.autotune("block_k", [16, 32])
   class Probe(tilus.Script):
       def __init__(self, num_warps, block_m, block_n, block_k):
           super().__init__()
       def __call__(self): ...

   print(Probe._autotune_space)
   ```
2. 再写一个**故意重复**的版本，观察报错：
   ```python
   @tilus.autotune("block_k", [16])
   @tilus.autotune("block_k", [32])   # 重复
   class BadProbe(tilus.Script): ...
   ```

**需要观察的现象**：
- 第 1 步打印出一个 dict，key 是原始字符串（包括 `"block_m, block_n"`），共 3 项。
- 第 2 步抛 `RuntimeError: Duplicated specification for parameters: ...`。

**预期结果**：第 1 步输出形如 `{'block_k': [16, 32], 'block_m, block_n': [(128, 128), (128, 64), (64, 128)], 'num_warps': [4, 8]}`。此时**没有任何编译发生**——证明装饰器只标记。若你的环境打印顺序不同属正常（dict 顺序按装饰器自下而上）。

#### 4.1.5 小练习与答案

**练习 1**：下面两种写法等价吗？为什么。
```python
# A
@tilus.autotune("block_m, block_n", [(64,64), (128,128)])
# B
@tilus.autotune("block_m", [64, 128])
@tilus.autotune("block_n", [64, 128])
```
**答案**：不等价。A 只产生 2 个绑定组合 `(64,64)`、`(128,128)`；B 是笛卡尔积，产生 4 个 `(64,64),(64,128),(128,64),(128,128)`。当你只想保留「方阵分块」时用 A，想全空间搜索时用 B。

**练习 2**：为什么 `@autotune` 要把校验（重复名、解包）放在装饰器执行时，而不是等展开时？
**答案**：fail-fast。装饰器在 import 时就执行，能在写内核的第一时间暴露笔误（比如把 `(128, 64)` 写成了 `128, 64` 这种长度不匹配），而不是等到运行内核、已经编译了几份程序后才在 `span_space` 里报错。

---

### 4.2 span_space：笛卡尔展开搜索空间

#### 4.2.1 概念说明

`span_space` 是一个**纯函数**：输入 `_autotune_space` 那个 dict，输出一个列表，每个元素是一份「扁平化」的调度字典 `{形参名: 具体值}`。它把多个装饰器声明出的子空间做**笛卡尔积**，并把多名字的逗号 key 拆开。

它是纯函数意味着：给定相同输入，输出完全确定，不依赖任何全局状态，非常适合单独写单元测试。

#### 4.2.2 核心流程

设第 `i` 个子空间有 `n_i` 个候选，则总调度数为：

\[
|\text{schedules}| = \prod_{i=1}^{k} n_i
\]

例如 `num_warps`(2) × `block_m,block_n`(3) × `block_k`(2) = 12 份调度。

展开过程：

1. 遍历 `_autotune_space`，把每个 key 按 `,` 拆成子名字列表（单名字则保持字符串）。
2. 用 `itertools.product(*values)` 对所有候选做笛卡尔积。
3. 对每个组合，若该 key 是多名字，就把对应元组按顺序拆给各子名字；若是单名字，直接赋值。
4. 汇总成一个扁平 dict，追加到结果列表。

#### 4.2.3 源码精读

`span_space` 定义在 [python/tilus/lang/instantiated_script.py:L48-L99](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/instantiated_script.py#L48-L99)。关键三段：

```python
for key, value in space.items():
    if "," in key:
        subkeys = key.split(", ")      # 多名字拆开
    else:
        subkeys = key
    keys.append(subkeys); values.append(value)
```
[instantiated_script.py:L77-L84](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/instantiated_script.py#L77-L84)：决定每个 key 是「子名字列表」还是「单名字字符串」。

```python
for combination in product(*values):   # 笛卡尔积
    spanned_dict = {}
    for subkeys, comb in zip(keys, combination):
        if isinstance(subkeys, list):  # 多名字：按位解包
            for subkey, val in zip(subkeys, comb):
                spanned_dict[subkey] = val
        else:
            spanned_dict[subkeys] = comb
    spanned_space.append(spanned_dict)
```
[instantiated_script.py:L87-L97](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/instantiated_script.py#L87-L97)：`product(*values)` 是笛卡尔积的核心；随后按子名字解包成扁平 dict。

注意一个细节：`key.split(", ")` 用的是 `", "`（逗号+空格）。所以装饰器里写 `"block_m, block_n"`（带空格）能正确拆成 `["block_m", "block_n"]`。源码注释的示例 `span_space({'m': [1,2,3], 'n, k': [[1,2],[3,4]]})`（[L54-L62](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/instantiated_script.py#L54-L62)）展示了混合单/多名字的情形，产出 3×2=6 份调度。

#### 4.2.4 代码实践

**实践目标**：脱离整个 Tilus，单独调用 `span_space` 验证展开数量与形状。

**操作步骤**（纯 Python）：
```python
from tilus.lang.instantiated_script import span_space

space = {
    "num_warps": [4, 8],
    "block_m, block_n": [(128, 128), (128, 64), (64, 128)],
    "block_k": [16, 32],
}
scheds = span_space(space)
print("总数:", len(scheds))
for s in scheds:
    print(s)
```

**需要观察的现象**：总数应为 2×3×2=12；每条都是扁平 dict，如 `{'num_warps': 4, 'block_m': 128, 'block_n': 64, 'block_k': 16}`；`block_m` 与 `block_n` 总是成对出现（不会出现 `(128, 128)` 之外的错配）。

**预期结果**：12 条调度，第一条 `{'num_warps': 4, 'block_m': 128, 'block_n': 128, 'block_k': 16}`。若想确认笛卡尔顺序，可对照 `itertools.product` 的输出顺序。

#### 4.2.5 小练习与答案

**练习 1**：若把 `"block_m, block_n"` 的候选误写成 `[(128, 128, 64)]`（三元组），`span_space` 会怎样？
**答案**：`span_space` 本身不会报错（它只做 `zip(subkeys, comb)`，多出来的 64 被忽略），但在下一步 `generate_schedules` 里 `signature.bind` 会因多余/不匹配参数而抛错。这也是为什么装饰器阶段 [script.py:L137-L144](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/script.py#L137-L144) 要提前做长度校验。

**练习 2**：一份有 5 个子空间、各 2 个候选的调优空间，会展开出多少份调度？若每份编译需 10 秒，串行要多久？
**答案**：\(2^5 = 32\) 份。串行 320 秒。这就是为什么 Tilus 用 `parallel_imap` 并行编译（见 4.3）。

---

### 4.3 调度生成与 benchmark 选优

#### 4.3.1 概念说明

`span_space` 给出的是「形参名→值」的裸字典，还不能直接用——它要和用户实例化时传的参数合并、绑定到 `__init__` 的签名上，才算一份合法调度。`generate_schedules` 负责这件事。

随后，每份调度会被**并行转译**成 Tilus IR 的 `Program`，再**并行编译**成 `.so`。注意：**编译失败的调度不会让整个内核失败**，它只是被丢弃并记录到缓存的 `failed/` 目录——有些分块组合在特定架构上本就非法（比如寄存器溢出），自动调优容得下它们。

编译完成后，真正「选最优」发生在**首次调用内核**时（`_pick_best_program`）：用真实输入把每份合法程序各跑若干轮，取延迟最小者，写入 dispatch 表并落盘。

#### 4.3.2 核心流程

完整时间线（从写好内核到拿到最优调度）：

```
import 时     : @autotune 把空间累积到 _autotune_space（标记）
实例化时      : InstantiatedScript.__init__ → generate_schedules → span_space
              : 得到 self.schedules（一份份扁平调度 dict）
首次调用(kernel(...)) 且 jit_key 未命中:
              : 建 JitInstance → _transpile_programs（并行转译每份调度为 Program）
              :                → _build_programs（并行编译为 .so，失败者记录丢弃）
              : _pick_best_program: 对每个新 tuning_key，逐个 benchmark，选 min
              : 写 dispatch_table.json / dispatch_table.txt / latency/<key>/report.txt
后续同规模调用: 命中 dispatch 表，零开销直接启动已选程序
```

benchmark 选优的核心是「对每份合法程序测延迟，取最小」：

\[
\text{choice}(\text{tuning\_key}) = \arg\min_{i \in \text{valid}} \text{latency}(i, \text{tuning\_key})
\]

#### 4.3.3 源码精读

**① 调度生成 `generate_schedules`** —— [instantiated_script.py:L102-L159](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/instantiated_script.py#L102-L159)

```python
if script_cls.debug_schedule:
    spanned_space = [script_cls.debug_schedule]   # 调试：只编译单点
else:
    spanned_space = span_space(space)

for spanned_dict in spanned_space:
    conflict_names = set(spanned_dict) & set(script_kwargs)
    if conflict_names: raise ValueError(...)      # 不许覆盖用户显式传的值
    init_kwargs = dict(script_kwargs) | spanned_dict
    bound_args = signature.bind(*init_args, **init_kwargs)
    bound_args.apply_defaults()
    schedule = dict(bound_args.arguments); schedule.pop("self")
    schedules.append(schedule)
```

- [L132-L135](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/instantiated_script.py#L132-L135)：`debug_schedule` 优先级最高，直接把整个空间替换成单点。这是 u2-l1 提到的调试手段——只编译一个配置，用于 CI 冒烟或定位问题。
- [L138-L144](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/instantiated_script.py#L138-L144)：冲突检查。若调优空间想调的形参，用户实例化时已经显式给了（如 `MatmulV2(block_m=64)`），就报错——避免「用户指定」与「自动搜索」打架。
- [L145-L148](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/instantiated_script.py#L145-L148)：`signature.bind` + `apply_defaults` 把调度值与用户参数合并绑定到 `__init__` 签名，填上默认值。
- [L156-L157](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/instantiated_script.py#L156-L157)：把绑定结果（去掉 `self`）存为一份 schedule。

**② 实例化时展开一次** —— [instantiated_script.py:L808-L813](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/instantiated_script.py#L808-L813)

```python
self.space = getattr(script_cls, "_autotune_space", {})                 # L811
self.schedules = generate_schedules(self.space, script_cls, ...)        # L813
```
一个 `InstantiatedScript` 只展开一次，`self.schedules` 是整份调度列表，之后所有调用共享它。

**③ 并行转译 + 并行编译** —— `JitInstance` 在创建时立即转译（[L402](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/instantiated_script.py#L402) 调 `_transpile_programs`），用 `parallel_imap` 并行把每份调度实例化成 `Program`（[L492-L501](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/instantiated_script.py#L492-L501)）；构建时再 `_build_programs` 并行编译（[L611-L620](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/instantiated_script.py#L611-L620)）。失败的调度被收集到 `failed_scheduling` / `failed_building`（[L505-L515](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/instantiated_script.py#L505-L515)、[L626-L640](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/instantiated_script.py#L626-L640)），不会中断流程。并行度由 `tilus.parallel_workers` 控制（默认 `os.cpu_count()`，见 [option.py:L60-L65](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/option.py#L60-L65)）。

**④ benchmark 选优 `_pick_best_program`** —— [instantiated_script.py:L700-L771](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/instantiated_script.py#L700-L771)

```python
_, tuning_key = extract_keys(args, ...)                 # L704 取当前输入的 tuning_key
if tuning_key not in self.dispatch_table:               # L707 未命中才调
    ...
    if len(self.compiled_programs) == 1:
        latency.append(0.0)                             # L716-L718 只有一份就跳过 benchmark
    else:
        for i, compiled_program in tqdm(...):
            lat = benchmark_func(                       # L734-L738 真测延迟
                lambda: compiled_func(*kernel_args),
                warmup=tilus.option.get_option("bench_warmup"),
                repeat=tilus.option.get_option("bench_repeat"),
            )
            latency.append(lat)
    best_latency = min(latency)                         # L748
    best_program_idx = latency.index(best_latency)      # L749
    self.dispatch_table[tuning_key] = best_program_idx  # L750
    self.dump_dispatch_table()                          # L751 落盘
```

- [L716-L718](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/instantiated_script.py#L716-L718)：只有一份合法程序时直接跳过 benchmark（省时间）。
- [L734-L738](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/instantiated_script.py#L734-L738)：`benchmark_func` 的 warmup/repeat 取自全局选项，默认 5/50（[option.py:L80-L91](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/option.py#L80-L91)）。想加快调优可调小 `bench_repeat`，想更稳可调大。
- [L748-L750](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/instantiated_script.py#L748-L750)：取最小延迟者写入 dispatch 表。`tuning_key` 来自 `__call__` 的整数形参（如 `m_size`），所以同一组已编译程序对不同输入规模可能选不同调度——这正是「按规模 dispatch」。
- [L753-L769](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/instantiated_script.py#L753-L769)：把每份调度的延迟排序写入 `latency/<tuning_key>/report.txt`，并把最优程序软链接到 `latency/<tuning_key>/<best_idx>`。**这是你「查看选中调度」的第一手文件。**

**⑤ 硬件感知的 dispatch 缓存** —— 加载 dispatch 表时会校验环境指纹（[L773-L785](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/instantiated_script.py#L773-L785)）：保存时写入 GPU 名、算力、CUDA 版本、target、tilus 版本（`collect_tuning_metadata`，[L193-L208](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/instantiated_script.py#L193-L208)）；加载时任一字段不匹配就丢弃旧表、重新调优。这保证「B200 上调好的表被 B300 共享缓存误用时不会静默用错调度」（见 [autotuning.rst:L72-L89](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/docs/source/programming-guides/autotuning.rst#L72-L89)）。dispatch 表的深度机制留到 u8-l2 讲。

**⑥ 触发时机** —— `InstantiatedScript.__call__` 的慢路径在 jit_key 未命中时创建 `JitInstance` 并调 `_pick_best_program`（[L842-L852](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/instantiated_script.py#L842-L852)）。也就是说：**调优发生在你第一次 `kernel(...)` 时**，而不是 `MyScript()` 实例化时——实例化只做了静态分析（u2-l1）。

#### 4.3.4 代码实践

**实践目标**：运行官方 autotune 示例 `matmul_v2.py`，定位缓存目录里的「选中调度」文件。

**操作步骤**：

1. 先指定一个干净的缓存目录，便于观察：
   ```bash
   export TILUS_CACHE_DIR=/tmp/tilus_autotune_cache
   rm -rf /tmp/tilus_autotune_cache
   ```
2. 运行示例：
   ```bash
   cd examples/matmul
   python matmul_v2.py
   ```
3. 等待首次运行的 `Scheduling` → `Building` → `Tuning` 三条进度条结束后，查看产物：
   ```bash
   ls /tmp/tilus_autotune_cache/scripts/matmul_v2/
   # 进入对应 jit_key 的目录（形如 4096-4096-1-<8位hash>）
   cat .../schedule.txt          # 所有合法调度
   cat .../dispatch_table.txt    # 每个 tuning_key 选中的 index
   cat .../latency/*/report.txt  # 各调度的延迟排序
   ```

**需要观察的现象**：
- 首次运行能看到 `Scheduling`（12 份）、`Building`（合法的那几份）、`Tuning`（逐份 benchmark）三条 tqdm 进度条。
- `report.txt` 是按延迟升序的表格，**第一行**就是被选中的最优调度。
- 再次运行同一规模时，不再出现 `Tuning` 条——直接命中 dispatch 表。

**预期结果**：`report.txt` 第一行的 `index` 与 `dispatch_table.txt` 里 `choice` 列一致；该 index 对应的 schedule（如 `num_warps=8, block_m=128, block_n=128, block_k=32`）就是 Tilus 为你的 GPU + 4096×4096 选出的最优配置。**待本地验证**：具体最优值随你的 GPU 型号而变。

#### 4.3.5 小练习与答案

**练习 1**：如果 12 份调度里有 3 份编译失败，autotune 会怎样？
**答案**：不会报错。失败的 3 份被记入 `failed/building/` 目录（[L668-L678](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/instantiated_script.py#L668-L678)），benchmark 只在剩余 9 份合法程序里选最优。只有当**全部**失败、`compiled_programs` 为空时才抛 `RuntimeError`（[L683-L698](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/instantiated_script.py#L683-L698)）。

**练习 2**：为什么 `_pick_best_program` 用 `tuning_key`（而不是 `jit_key`）作为 dispatch 表的 key？
**答案**：`jit_key` 决定**编译**（不同编译产物），`tuning_key` 决定**选型**（在同一组已编译产物里挑）。一个 jit_key 对应一组共享的已编译程序；这组程序对不同输入规模（不同 tuning_key）可能各有所长，所以 dispatch 表按 tuning_key 存最优 index。

**练习 3**：把 `tilus.option.bench_repeat` 从默认 50 调到 5，会对调优结果有什么影响？
**答案**：调优变快，但每份程序的延迟测量噪声变大，可能选出「偶然跑得快」的次优调度。benchmark 选项是为「快」与「准」提供的旋钮（[option.py:L80-L91](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/option.py#L80-L91)）。

---

## 5. 综合实践

把本讲三块知识串起来：**给 naive matmul（`matmul_v0.py`）加上 `@autotune`，让 Tilus 自动选最优分块**。

**背景**：`matmul_v0.py` 里 `block_m/block_n/block_k` 是**写死**在 `__init__` 体内的（[matmul_v0.py:L48-L55](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/examples/matmul/matmul_v0.py#L48-L55)），所以 `@autotune` 无从下手。要让它们可调，必须把它们**提升为 `__init__` 的形参**——这正是 4.1 强调的「autotune 调的是 `__init__` 形参」。

**步骤**：

1. 复制一份 `matmul_v0.py` 为 `matmul_v0_autotune.py`。
2. 把 `__init__` 改成形参，并加上装饰器（参照 [matmul_v2.py:L55-L70](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/examples/matmul/matmul_v2.py#L55-L70) 的写法）：
   ```python
   @tilus.autotune("block_m, block_n", [(64, 64), (128, 64), (64, 128)])
   @tilus.autotune("block_k", [16, 32])
   class MatmulV0Auto(tilus.Script):
       def __init__(self, block_m, block_n, block_k):
           super().__init__()
           self.block_m = block_m
           self.block_n = block_n
           self.block_k = block_k
       def __call__(self, m_size: int32, n_size: int, k_size: int,
                    a_ptr: ~float16, b_ptr: ~float16, c_ptr: ~float16):
           ...  # 原有 __call__ 体不变
   ```
   注意 `attrs.warps` 原本是 1，这里先保持；想顺带调 warps 可参考 v2 把 `num_warps` 也加进空间。
3. 实例化时**不再传** block_m 等（它们由 autotune 提供）：
   ```python
   matmul = MatmulV0Auto()     # 只实例化，不编译
   matmul(m, n, k, a, b, c_actual)   # 首次调用触发编译 + 调优
   ```
4. 用 `TILUS_CACHE_DIR` 指定干净目录，运行后查看：
   - `scripts/matmul_v0_auto/<jit_key>/schedule.txt`：确认展开了 3×2=6 份调度；
   - `latency/*/report.txt` 第一行：Tilus 为你选中的最优 block_m/block_n/block_k；
   - `dispatch_table.txt`：确认该 index 被记录。
5. 对比改造前后（写死 `block_m=64,block_n=64,block_k=16` vs autotune 选优）在 4096×4096 上的 TFLOPS。

**需要观察的现象**：autotune 选出的分块通常优于 v0 的写死值；不同 GPU 选出的最优分块可能不同；第二次运行同规模不再出现 `Tuning` 进度条（命中 dispatch）。

**预期结果**：autotune 版的 TFLOPS 应**不低于** v0 写死版。若反而更差，多半是 `bench_repeat` 太小导致测量噪声——调大重试。**待本地验证**：确切的最优配置与加速比取决于你的 GPU。

> 进阶对照：官方 [matmul_v2.py](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/examples/matmul/matmul_v2.py) 在 autotune 的基础上还引入了共享内存分块（`shared_tensor`/`store_shared`/`load_shared`），性能远高于本实践的 naive 版。共享内存与流水线优化是 u7 系列的主题。

## 6. 本讲小结

- `@tilus.autotune(arg_names, arg_values)` 是装饰器工厂，**只标记不展开**：把调优子空间累积到类属性 `_autotune_space`，并做「重复名」「解包长度」两道校验。
- `span_space` 是纯函数，用 `itertools.product` 对所有子空间做**笛卡尔积**，并把多名字的逗号 key 拆成扁平 dict；总调度数是各子空间候选数的乘积。
- `generate_schedules` 把每份展开结果绑定到 `__init__` 签名（`debug_schedule` 可覆盖为单点，冲突用户参数会报错），在实例化时展开一次。
- 每份调度被**并行转译 + 并行编译**，失败的调度被丢弃而非中断；选优发生在**首次调用**内核时，对每个新 `tuning_key` 逐份 benchmark 取最小延迟。
- 调优结果落盘为 `schedule.txt` / `dispatch_table.txt` / `latency/<key>/report.txt`；dispatch 表带硬件环境指纹，换机器会自动重新调优。
- `bench_warmup`/`bench_repeat`/`parallel_workers` 三个选项分别控制测量预热、重复次数与编译并行度。

## 7. 下一步学习建议

本讲聚焦 `__init__` 超参的搜索。建议接下来：

- **向纵深**：dispatch 表的环境指纹、`tuning_key` 的分桶（`extract_keys` 里的 bucket 公式）是 [u8-l2 自动调优调度与硬件感知 dispatch 缓存](u8-l2-autotune-dispatch-cache.md) 的主题，去那里看「为何换机器要重调」「为何相近规模共享一份调度」。
- **向编译流程**：调优只是入口，每份调度如何变成 `.so` 走的是 `drivers.build_program` 流水线——进入 [u3-l1 编译流水线总览](u3-l1-compilation-pipeline-overview.md)。
- **向性能实践**：autotune 配合共享内存/张量核才真正发力，看 [u7-l1 Ampere matmul 进阶](u7-l1-ampere-matmul-deep-dive.md) 里 v2→v5 如何在 autotune 之上叠加优化。
- **源码练习**：通读 [instantiated_script.py](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/instantiated_script.py) 的 `JitInstance` 类，画出「实例化 → 转译 → 编译 → benchmark → dispatch」的状态流转图。
