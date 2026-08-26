# 动手写第一个自定义算子

## 1. 本讲目标

上一讲（u1-l4）我们逐行精读了 hello world，理解了 `@pl.jit`、`pl.at` 作用域和三段式编程模型。本讲要把「读懂」升级为「会写」。

读完本讲，你应该能够：

1. **独立编写**一个包含 load / compute / store 三段式的完整算子，包括处理「张量比一个 Tile 大」的分块循环情况。
2. **用基础算子组合出复杂函数**——理解为什么 DSL 只提供 `exp`、`recip` 这样的原语，而 sigmoid、SiLU、GELU 都要自己拼。
3. **说清 `pl.Out` 的真实身份**——它不是一个类型构造器，而是一个运行时透传的方向标记。
4. **掌握用 torch 张量对照验证算子正确性的标准套路**，并且能区分「实现错误」和「近似误差」这两类完全不同的偏差。

本讲所有结论都基于仓库中真实存在的代码，不虚构任何接口。

---

## 2. 前置知识

**2.1 三段式（load → compute → store）**

在 Tile 级写算子，几乎永远遵循同一个骨架：

```text
① pl.load   把全局内存（GM）里的一小块数据搬进片上 Tile
② 片上计算  用 tile.* 算子对 Tile 做计算，结果仍是 Tile
③ pl.store  把结果 Tile 写回某个输出张量
```

这三段分别对应「搬运」「计算」「搬运」。片上计算极快，但片上空间有限，所以你必须自己决定每次搬多少、搬到哪。这就是 Tile 级编程和 Tensor 级编程的本质区别：**搬运是显式的**。

**2.2 offsets 与 shapes 的坐标约定**

`pl.load` 的两个关键参数容易混淆，务必记牢：

| 参数 | 含义 | 坐标系 |
| --- | --- | --- |
| `offsets` | 这次搬运的**起点** | 源张量的坐标系 |
| `shapes` | 这次搬运的**窗口大小** | 固定为 Tile 尺寸 |

口诀（承接 u1-l4）：**shapes 恒为 Tile 尺寸，offsets 在源张量坐标系中移动**。分块循环时，只有 offsets 在变。

**2.3 什么是「激活函数的近似」**

GELU 的精确定义用到误差函数 erf：

\[ \mathrm{GELU}_{exact}(x) = 0.5\,x\left(1 + \mathrm{erf}\left(\frac{x}{\sqrt{2}}\right)\right) \]

硬件上算 erf 很贵。工程上常用一个只用 `exp` 和 `tanh` 的近似：

\[ \mathrm{GELU}_{tanh}(x) = 0.5\,x\left(1 + \tanh\left(\sqrt{\tfrac{2}{\pi}}\,\left(x + 0.044715\,x^{3}\right)\right)\right) \]

其中 \(\sqrt{2/\pi} \approx 0.7978845608\)。这个近似与精确版有约 \(3\times10^{-4}\) 量级的固有偏差——这一点在本讲综合实践中会变成一个关键的验证陷阱。

**2.4 tanh 在 PyPTO 里不存在**

先给一个**重要的事实**：本仓库的 DSL 中**没有 `pl.tanh`**。在 `python/pypto/language/` 下全文检索 `tanh`，不会命中任何算子定义；片上可用的超越函数原语只有 `exp`、`recip`、`sqrt`、`rsqrt`、`log`、`sin` 等（见 [python/pypto/language/op/tile_ops.py:1101](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/language/op/tile_ops.py#L1101) 一带的函数族）。

所以「写一个 tanh 近似的 GELU」这个任务，实际含义是：**用恒等式把 tanh 拆成 exp 和 recip 的组合**。这正是本讲综合实践的核心训练点。

---

## 3. 本讲源码地图

| 文件 | 作用 | 本讲用法 |
| --- | --- | --- |
| `examples/beginner/02_elementwise.py` | elementwise 加/乘 + 分块循环 | 模块 4.1：三段式模板与分块 |
| `examples/beginner/04_activation.py` | SiLU / GELU / SwiGLU / GeGLU | 模块 4.2：算子组合 |
| `examples/beginner/06_concat.py` | Tile 列拼接 | 模块 4.3：形状运算 |
| `examples/beginner/05_matmul.py` | cube 单元矩阵乘 | 模块 4.4：多级内存（延伸） |
| `examples/beginner/01_hello_world.py` | 最简示例 | 承接 u1-l4，对照用 |
| `python/pypto/language/op/tile_ops.py` | Tile 级算子的 Python 封装 | 各模块的底层依据 |
| `python/pypto/language/op/unified_ops.py` | 按 Tensor/Tile 类型分发的统一入口 | 模块 4.2：分发机制 |
| `python/pypto/language/typing/direction.py` | `Out` / `InOut` 方向标记 | 模块 4.3：`pl.Out` 真相 |

---

## 4. 核心概念与源码讲解

### 4.1 三段式算子模板与分块循环

#### 4.1.1 概念说明

一个 Tile 是固定大小的片上窗口。当你的张量恰好等于一个 Tile 大小时，算子写起来最简单；当张量比 Tile 大时，你必须**分块（chunking）**：用循环把张量切成若干 Tile 大小的块，逐块执行三段式。

这不是可选优化，而是**必须做的事**——一个 512×128 的张量物理上塞不进一个 128×128 的 Tile。

#### 4.1.2 核心流程

单块算子（张量 == Tile 尺寸）：

```text
进入 pl.at 作用域
  ├─ tile_a = pl.load(a, [0,0], [128,128])   # 左上角起，取 128x128
  ├─ tile_b = pl.load(b, [0,0], [128,128])
  ├─ tile_c = pl.add(tile_a, tile_b)          # 片上计算
  └─ pl.store(tile_c, [0,0], c)               # 写回左上角
返回 c
```

分块算子（张量 > Tile 尺寸）：

```text
设张量 ROWS×COLS，Tile 高 TILE_ROWS
for i in 0 .. (ROWS // TILE_ROWS - 1):
    row_off = i * TILE_ROWS
    load(a, [row_off, 0], [TILE_ROWS, COLS])   # shapes 不变！
    load(b, [row_off, 0], [TILE_ROWS, COLS])
    store(add(...), [row_off, 0], c)
```

注意循环次数是整除结果。如果 `ROWS % TILE_ROWS != 0`，会有尾巴块需要 `valid_shape` 或 `clamp` 处理——那是 u2 系列的话题，本讲所有例子都取整除尺寸。

#### 4.1.3 源码精读

先看单块模板，和 hello world 一模一样，只是把 `add` 换成了 `mul`：

> [examples/beginner/02_elementwise.py:39-46](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/examples/beginner/02_elementwise.py#L39-L46) —— 标准 `@pl.jit` 算子：`pl.Out[pl.Tensor]` 声明输出参数，`pl.at(CORE_GROUP)` 划定片上作用域，三段式完成 `c = a + b`。

```python
@pl.jit
def tile_add_128(a: pl.Tensor, b: pl.Tensor, c: pl.Out[pl.Tensor]):
    with pl.at(level=pl.Level.CORE_GROUP):
        tile_a = pl.load(a, [0, 0], [128, 128])
        tile_b = pl.load(b, [0, 0], [128, 128])
        tile_c = pl.add(tile_a, tile_b)
        pl.store(tile_c, [0, 0], c)
    return c
```

同一个文件里还有 64×64 版本：

> [examples/beginner/02_elementwise.py:59-67](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/examples/beginner/02_elementwise.py#L59-L67) —— 同样的骨架，只改了 `pl.load` / `pl.store` 的 shapes 为 `[64, 64]`。这证明 **Tile 尺寸是调用方选的参数，不是全局常量**。

重点是分块版本：

> [examples/beginner/02_elementwise.py:81-95](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/examples/beginner/02_elementwise.py#L81-L95) —— 分块循环：`pl.range(ROWS // TILE_ROWS)` 循环 4 次，每次 offsets 的行坐标移动 `i * TILE_ROWS`，shapes 恒为 `[TILE_ROWS, COLS]`。

```python
ROWS = 512
COLS = 128
TILE_ROWS = 128

@pl.jit
def chunked_add(a: pl.Tensor, b: pl.Tensor, c: pl.Out[pl.Tensor]):
    with pl.at(level=pl.Level.CORE_GROUP):
        for i in pl.range(ROWS // TILE_ROWS):
            tile_a = pl.load(a, [i * TILE_ROWS, 0], [TILE_ROWS, COLS])
            tile_b = pl.load(b, [i * TILE_ROWS, 0], [TILE_ROWS, COLS])
            pl.store(pl.add(tile_a, tile_b), [i * TILE_ROWS, 0], c)
    return c
```

三个值得盯住的细节：

- `pl.range(...)` 而不是 Python 内建 `range`。这不是风格偏好而是硬性要求：解析器在校验循环迭代器时只接受 `ast.Attribute` 形式（即 `xxx.range(...)`），内建 `range(...)` 是 `ast.Name`，会直接抛 `ParserSyntaxError`，见 [python/pypto/language/parser/ast_parser.py:2602-2635](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/language/parser/ast_parser.py#L2602-L2635) 的 `_VALID_ITERATORS` 集合与 `_validate_for_loop_iterator`。同一族还有 `pl.parallel` / `pl.unroll` / `pl.pipeline` / `pl.while_` / `pl.spmd` / `pl.split_aiv` 六个兄弟迭代器。这是 u2-l5 的主题，这里先用。
- 循环变量 `i` 参与 `i * TILE_ROWS` 运算后直接作为 offsets 传入，说明 offsets 接受标量表达式，不要求是字面量整数。
- `pl.store` 紧跟在 `pl.add` 后面，没有中间变量。三段式允许内联写法。

再看宿主侧怎么验证：

> [examples/beginner/02_elementwise.py:121-127](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/examples/beginner/02_elementwise.py#L121-L127) —— 用 `torch.randn` 造随机输入、`torch.zeros` 造输出缓冲，调用算子后 `torch.allclose` 对照 PyTorch 自己算的结果。

```python
torch.manual_seed(0)
a_big = torch.randn((ROWS, COLS), dtype=torch.float32)
b_big = torch.randn((ROWS, COLS), dtype=torch.float32)
c_big = torch.zeros((ROWS, COLS), dtype=torch.float32)
chunked_add(a_big, b_big, c_big, config=cfg)
assert torch.allclose(c_big, a_big + b_big, rtol=1e-5, atol=1e-5)
```

这就是 PyPTO 验证算子正确性的标准姿势：**PyPTO 算子当被测对象，torch 当参考实现**。

最后确认底层接口。`pl.load` 的签名与文档：

> [python/pypto/language/op/tile_ops.py:374-424](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/language/op/tile_ops.py#L374-L424) —— `load(tensor, offsets, shapes, valid_shape=None, target_memory=None, clamp=False)`。docstring 明确写了两个坐标约定："offsets: Offsets in each dimension. Always in the source tensor's coordinate system" 和 "shapes: Shape of the region to load … Always in the source tensor's coordinate system"。

> [python/pypto/language/op/tile_ops.py:427-470](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/language/op/tile_ops.py#L427-L470) —— `store(tile, offsets, output_tensor, shapes=None, *, atomic=...)`。注意 `atomic=pl.AtomicType.Add` 可做原子累加，用于 split-K 场景——这是 u7-l3 性能优化会用到的东西。

#### 4.1.4 代码实践

**实践目标**：确认「shapes 不变、offsets 移动」这条口诀，并观察 Tile 尺寸这个自由参数的影响。

**操作步骤**：

1. 进入仓库根目录，先跑通原版：`python examples/beginner/02_elementwise.py`，预期打印 `OK`。
2. 复制一份为 `/tmp/my_chunked.py`（不要改仓库源码）。
3. 把模块级常量改成 `ROWS = 1024; COLS = 128; TILE_ROWS = 64`，循环次数变为 16。
4. 把宿主侧的张量尺寸同步改成 `(ROWS, COLS)`。
5. 重新运行。

**需要观察的现象**：程序仍然打印 `OK`——你只改了三个数字，算子就适配了新的张量高度。

**预期结果**：分块循环的次数由 `ROWS // TILE_ROWS` 自动推导，无需改动算子主体。1024×128 的张量用 64 行的 Tile，循环 16 次。

若本机未装好环境，标记为「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：`chunked_add` 中如果把 `pl.load(a, [i * TILE_ROWS, 0], ...)` 的第二个分量 `0` 改成 `i * TILE_ROWS`，会发生什么？

**答案**：第二个分量是列偏移。源张量 `a` 只有 `COLS = 128` 列，当 `i >= 1` 时列偏移 `i * 128 >= 128`，load 请求的窗口会完全越出源张量右边界。依据 [python/pypto/language/op/tile_ops.py:402-405](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/language/op/tile_ops.py#L402-L405) 的文档，默认行为是「provably 越界则被拒绝」，即报错，而不是静默读到垃圾数据。若确实需要越界读，必须显式传 `clamp=True`。

**练习 2**：为什么不把 512×128 的张量直接 `pl.load(a, [0,0], [512,128])`？

**答案**：因为 shapes 参数指定的是 Tile 尺寸，而 Tile 是片上固定大小的数据块，512×128 的 FP32 是 256 KB，远超单个 Tile 的物理容量。`pl.load` 不是「任意大小的内存拷贝」，而是「申请一个 Tile 并从 GM 填充它」。所以必须分块。

---

### 4.2 用基础算子组合复杂函数

#### 4.2.1 概念说明

这是本讲最重要的一块。看 `04_activation.py` 你会发现一个现象：**文件里的 SiLU、GELU、SwiGLU、GeGLU 没有一个是内置算子**，全部是用 `mul`、`add`、`exp`、`recip` 四个原语手工拼出来的。

为什么这么设计？因为 PTO 是一套贴近硬件的虚拟指令集。每加一个内置算子，就意味着 C++ 注册表、类型推断、Python 绑定、代码生成、测试五层都要同步维护（这个代价会在 u7-l7 详述）。而 `exp` 和 `recip` 是硬件真有的指令，用它们组合出 sigmoid，编译器还能顺手做表达式级的融合与调度。

**给读者的直接启示**：写自定义算子时，先问「我能不能用现有原语拼出来」，而不是「有没有现成的算子」。

#### 4.2.2 核心流程

组合的基本材料是 sigmoid 恒等式：

\[ \sigma(z) = \frac{1}{1 + e^{-z}} \]

用 `mul` / `exp` / `add` / `recip` 四条指令即可拼出：

```text
输入 tile_x
  ├─ neg    = mul(tile_x, -1.0)     # -x
  ├─ e      = exp(neg)              # e^{-x}
  ├─ denom  = add(e, 1.0)           # 1 + e^{-x}
  ├─ sig    = recip(denom)          # σ(x)
  └─ result = mul(tile_x, sig)      # x·σ(x)  —— 这就是 SiLU
```

每个中间结果都是一个 Tile，链式传递。算子组合就是 Tile 数据流图的搭建。

#### 4.2.3 源码精读

先看 SiLU 的完整实现：

> [examples/beginner/04_activation.py:47-58](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/examples/beginner/04_activation.py#L47-L58) —— SiLU = x·σ(x)，用四个原语拼出 sigmoid，再乘回 x。注释里直接写出了数学恒等式。

```python
@pl.jit
def silu(x: pl.Tensor, output: pl.Out[pl.Tensor]):
    with pl.at(level=pl.Level.CORE_GROUP):
        # SiLU(x) = x * sigmoid(x) = x / (1 + exp(-x))
        tile_x = pl.load(x, [0, 0], [32, 128])
        x_neg = pl.mul(tile_x, -1.0)
        exp_neg = pl.exp(x_neg)
        denom = pl.add(exp_neg, 1.0)
        sigmoid = pl.recip(denom)
        result = pl.mul(tile_x, sigmoid)
        pl.store(result, [0, 0], output)
    return output
```

五行原语、逐行对应数学恒等式 \(\sigma(x)=\frac{1}{1+e^{-x}}\)：取负 → 指数 → 加一 → 求倒数 → 乘回 \(x\)。

接着看 GELU 的 **sigmoid 快速近似**——注意这不是本讲综合实践要的 tanh 近似，而是更粗的一档：

> [examples/beginner/04_activation.py:61-73](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/examples/beginner/04_activation.py#L61-L73) —— GELU(x) ≈ x·σ(1.702x)。对比 SiLU，只在最前面多了一步 `pl.mul(tile_x, 1.702)` 缩放。

```python
@pl.jit
def gelu(x: pl.Tensor, output: pl.Out[pl.Tensor]):
    with pl.at(level=pl.Level.CORE_GROUP):
        # GELU(x) = x * sigmoid(1.702 * x)  (fast approximation)
        tile_x = pl.load(x, [0, 0], [32, 128])
        x_scaled = pl.mul(tile_x, 1.702)
        x_neg = pl.mul(x_scaled, -1.0)
        exp_neg = pl.exp(x_neg)
        denom = pl.add(exp_neg, 1.0)
        sigmoid = pl.recip(denom)
        result = pl.mul(tile_x, sigmoid)
        pl.store(result, [0, 0], output)
    return output
```

这里出现了一个非常重要的手法：**`x_scaled` 只喂给 sigmoid，而最后 `pl.mul(tile_x, sigmoid)` 用的是原始 `tile_x`**。也就是说 `tile_x` 这个 Tile 的生命周期跨越了整条计算链，一直活到收尾。这个「哪个 Tile 活多久」的问题，就是后面 u5-l7 内存规划 Pass 要自动解决的事。

再看双输入版本 SwiGLU：

> [examples/beginner/04_activation.py:76-89](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/examples/beginner/04_activation.py#L76-L89) —— SwiGLU(gate, up) = gate·σ(gate)·up。两次 `pl.load` 各取一个输入，sigmoid 只作用在 gate 上，最后乘 up。

> [examples/beginner/04_activation.py:92-107](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/examples/beginner/04_activation.py#L92-L107) —— GeGLU 把其中的 σ(gate) 换成 GELU 近似，即先 `mul(tile_gate, 1.702)` 再走同一条 sigmoid 链。**两段代码几乎逐行同构**，说明「换一个激活函数」在 Tile 级就是「换一串原语组合」。

现在看这套组合的底层机制——`pl.exp` 为什么既能吃 Tensor 又能吃 Tile：

> [python/pypto/language/op/unified_ops.py:549-555](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/language/op/unified_ops.py#L549-L555) —— `exp` 的统一入口按 `isinstance` 分发：Tensor 走 `_tensor.exp`，Tile 走 `_tile.exp`，其他类型抛 `TypeError`。

```python
def exp(input: T) -> T:
    if isinstance(input, Tensor):
        return _tensor.exp(input)
    if isinstance(input, Tile):
        return _tile.exp(input)
    raise TypeError(f"pl.exp: expected Tensor or Tile, got {type(input).__name__}")
```

这解释了 u1-l4 说过的「`pl.add` 按操作数类型在 Tensor/Tile 两级分发」。`unified_ops.py` 就是那个分发层。

更值得注意的是 `pl.mul` 的分发里藏着一个优化：

> [python/pypto/language/op/unified_ops.py:361-371](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/language/op/unified_ops.py#L361-L371) —— `Tile * Tile` 走 `_tile.mul`，但 `Tile * float` 走的是 `_tile.muls`（注意多了个 s）。标量乘被单独路由到一个广播专用的算子。

对应的底层实现：

> [python/pypto/language/op/tile_ops.py:1060-1071](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/language/op/tile_ops.py#L1060-L1071) —— `muls(lhs, rhs)`：tile 与标量的逐元素乘。同族还有 [adds](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/language/op/tile_ops.py#L1032-L1043) 和 [subs](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/language/op/tile_ops.py#L1046-L1057)。

所以你写 `pl.mul(tile_x, -1.0)`，实际进入 IR 的是 `tile.muls`，而不是先造一个全 -1 的 Tile 再做 tile-tile 乘。这在 IR 层省掉一次广播。

`recip` 还有一个精度开关，本讲综合实践会用到：

> [python/pypto/language/op/tile_ops.py:1175-1186](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/language/op/tile_ops.py#L1175-L1186) —— `recip(tile, high_precision=False)`，docstring 写明 high_precision 选择 PTOAS 的高精度倒数模式，仅 FP16/FP32 可用。

#### 4.2.4 代码实践

**实践目标**：亲眼看一次「算子组合」的数值行为，并区分三类偏差。

**操作步骤**：

1. 运行 `python examples/beginner/04_activation.py`，确认打印 `OK`。
2. 复制为 `/tmp/activation_probe.py`，只保留 `gelu` 算子和它的验证段。
3. 把验证段的期望值从 `x * torch.sigmoid(1.702 * x)` 改成 torch 的精确 GELU：`torch.nn.functional.gelu(x)`，容差保持 `atol=1e-4`。
4. 重新运行，观察 assert 是否触发；把断言临时换成打印 `(out - exact).abs().max().item()`。

**需要观察的现象**：换成精确 GELU 做参照后，最大偏差会明显大于 1e-4（大约在 1e-2 量级）。

**预期结果**：σ(1.702x) 是一个很粗的近似，它的「模型误差」远超容差。但对照**同公式**的 `x * torch.sigmoid(1.702 * x)` 时（即原版验证），偏差在 1e-5 量级，说明**硬件实现本身是准的**。

这一步训练的是本讲最重要的判断力：**先固定数学公式，再谈实现精度**。对照实现错了，再准的算子也会「看起来错了」。

若本机未装好环境，标记为「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：用 `mul` / `add` / `exp` / `recip` 写出 `tanh(u)` 的组合。提示：把 tanh 写成 sigmoid 的仿射变换。

**答案**：

\[ \tanh(u) = \frac{e^{u} - e^{-u}}{e^{u} + e^{-u}} = \frac{2}{1 + e^{-2u}} - 1 = 2\,\sigma(2u) - 1 \]

验证右端：分子分母同乘 \(e^{2u}\) 得 \(\frac{2e^{2u}}{1+e^{2u}} - 1 = \frac{e^{2u}-1}{e^{2u}+1}\)，正是 tanh。对应代码：

```python
e = pl.exp(pl.mul(u, -2.0))     # e^{-2u}
sig = pl.recip(pl.add(e, 1.0))  # σ(2u)
tanh_u = pl.sub(pl.mul(sig, 2.0), 1.0)
```

这个形式还有个好处：`u → +∞` 时 `e^{-2u} → 0`、结果 → 1；`u → -∞` 时 `e^{-2u} → +∞`、结果 → -1。两端都不溢出，数值上是稳定的。（对比直接算 \(\frac{e^u - e^{-u}}{e^u + e^{-u}}\)：大 \(|u|\) 时 \(e^u\) 会溢出。）

**练习 2**：`pl.mul(tile_x, -1.0)` 和 `pl.mul(tile_x, tile_of_minus_one)`（假设后者是一个全 -1 的同形 Tile）在 IR 层有什么区别？

**答案**：前者经 [unified_ops.py:367-368](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/language/op/unified_ops.py#L367-L368) 分发到 `_tile.muls`，生成 `tile.muls` 算子，标量直接内嵌为立即数；后者生成 `tile.mul`，需要先有一个真实的全 -1 Tile（要么 load 进来，要么用 fill 造出来），多占一块片上空间和一条指令。能用标量形式就不要造广播 Tile。

**练习 3**：`fused_add_relu`（[04_activation.py:35-44](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/examples/beginner/04_activation.py#L35-L44)）里用到了 `pl.relu`，为什么 SiLU 不能也来一个 `pl.silu`？

**答案**：`relu` 是硬件级原语（见 [tile_ops.py:1216-1226](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/language/op/tile_ops.py#L1216-L1226)，直接映射到 `tile.relu` 这个注册算子），而 SiLU/GELU 不是。一个函数要不要成为内置算子，取决于它是否对应真实的硬件指令，以及通用性是否值得付出五层维护成本（注册表、推断、绑定、codegen、测试）。SiLU 用四条原语就能拼出来，所以不值得内置。

---

### 4.3 Tile 形状运算与 `pl.Out` 的真相

#### 4.3.1 概念说明

前两个模块的算子都是「输出形状 == 输入形状」。`concat` 打破了这个对称：两个 32×16 的 Tile 拼成一个 32×32 的 Tile。这类**形状在片上发生改变**的算子，是搭建复杂算子的第二类积木（第一类是逐元素组合）。

同一个模块还要拆穿一个容易误解的东西：`pl.Out[pl.Tensor]` 看起来像泛型构造，很多人以为它创建了一个新类型。**它没有。**

#### 4.3.2 核心流程

concat 的数据流：

```text
a (Tensor 32×16)          b (Tensor 32×16)
   │ pl.load [0,0],[32,16]   │ pl.load [0,0],[32,16]
   ▼                          ▼
tile_a (Tile 32×16)       tile_b (Tile 32×16)
   └──────────┬──────────────┘
              ▼
     pl.concat(tile_a, tile_b)  →  Tile 32×32
              │ pl.store [0,0] → c
              ▼
        c (Tensor 32×32)
```

而 `pl.Out` 在运行时做的事只有一件：

```text
解释器遇到注解 pl.Out[pl.Tensor]
   └─ 触发 _DirectionWrapper.__class_getitem__(pl.Tensor)
        └─ 原样返回 pl.Tensor          ← 就这一步，没有任何包装
```

#### 4.3.3 源码精读

先看 concat 算子本体：

> [examples/beginner/06_concat.py:28-35](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/examples/beginner/06_concat.py#L28-L35) —— 两次 load 得到两个 32×16 Tile，`pl.concat` 沿列方向拼成 32×32，一次性 store 到 c。注意 `tile_out` 有显式的类型注解 `pl.Tile[[32, 32], pl.FP32]`。

```python
@pl.jit
def tile_concat_32x32(a: pl.Tensor, b: pl.Tensor, c: pl.Out[pl.Tensor]):
    with pl.at(level=pl.Level.CORE_GROUP):
        tile_a = pl.load(a, [0, 0], [32, 16])
        tile_b = pl.load(b, [0, 0], [32, 16])
        tile_out: pl.Tile[[32, 32], pl.FP32] = pl.concat(tile_a, tile_b)
        pl.store(tile_out, [0, 0], c)
    return c
```

> [python/pypto/language/op/tile_ops.py:601-612](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/language/op/tile_ops.py#L601-L612) —— `concat(src0, src1)` 的 docstring 明确限定"Concatenate two tiles along the **column** dimension"。也就是说 `pl.concat` 只支持列拼接，行拼接需要别的手段（转置后拼再转回，或用 `assemble`）。

宿主侧验证同样遵循「torch 当参考实现」的套路：

> [examples/beginner/06_concat.py:38-49](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/examples/beginner/06_concat.py#L38-L49) —— 参考实现是 `torch.cat([a, b], dim=1)`，输出缓冲 `c` 是 32×32 的零张量。

```python
a = torch.randn(32, 16, dtype=torch.float32)
b = torch.randn(32, 16, dtype=torch.float32)
c = torch.zeros((32, 32), dtype=torch.float32)
tile_concat_32x32(a, b, c, config=cfg)
expected = torch.cat([a, b], dim=1)
assert torch.allclose(c, expected, rtol=1e-5, atol=1e-5)
```

接着拆穿 `pl.Out`。它的全部实现只有 55 行，核心是：

> [python/pypto/language/typing/direction.py:33-52](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/language/typing/direction.py#L33-L52) —— `_DirectionWrapper.__class_getitem__` 原样返回传入的类型；`Out` / `InOut` 是它的两个子类。docstring 写明"At runtime, Out[T] and InOut[T] return T unchanged"。

```python
class _DirectionWrapper:
    def __class_getitem__(cls, item: T) -> T:
        """Enable Wrapper[T] subscript syntax. Returns the item unchanged at runtime."""
        return item


class InOut(_DirectionWrapper): ...
class Out(_DirectionWrapper): ...
```

运行时 `pl.Out[pl.Tensor]` 求值结果就是 `pl.Tensor` 本身。那方向信息存在哪？答案是：**只在 AST 解析阶段起作用**。文件头注释说得很清楚：

> [python/pypto/language/typing/direction.py:10-19](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/language/typing/direction.py#L10-L19) —— "These types are used as **AST-level markers** for parameter direction annotations"。解析器在静态分析函数签名时读出 `Out` 这个标记，写进 IR 的 `param_directions_`（u4-l5 会精读这个字段）；`if TYPE_CHECKING` 分支则把 `Out[T]` 变成 `Annotated[T, "Out"]` 让 mypy 不报错。

这带来两个推论：

- `pl.Out` 是**纯解析期标记**，不产生任何运行时对象和运行时开销。
- 参数方向影响的是编译器怎么处理这个参数（是否需要分配、是否参与返回值），以及编排代码生成怎么别名它——这正是 `pass-submit-awareness` 那套规则的源头，u3-l2 / u4-l5 会展开。

#### 4.3.4 代码实践

**实践目标**：验证 `pl.Out` 是透传标记，并体会 concat 的「只拼列」限制。

**操作步骤**：

1. 在 Python 交互环境（仓库根目录，已 `pip install -e` 后）执行：
   ```python
   import pypto.language as pl
   print(pl.Out[pl.Tensor] is pl.Tensor)
   print(pl.InOut[int])
   ```
2. 运行 `python examples/beginner/06_concat.py`，确认 `OK`。
3. 复制为 `/tmp/concat_probe.py`，把两处 `pl.load` 的 shapes 从 `[32, 16]` 改成 `[16, 32]`，`pl.store` 与输出缓冲 `c` 同步改成 `(32, 64)` 之外的合理值（先想清楚再改）。
4. 思考：改动后这个算子变成了什么语义？

**需要观察的现象**：

- 第 1 步应打印 `True` 和 `<class 'int'>`——`Out[...]` 下标访问就是恒等函数。
- 第 3 步如果直接跑，大概率报形状不匹配的错误。

**预期结果**：

- `pl.Out[X] is X` 为 `True`，证明 `Out` 在运行时是透传。
- 第 3 步把 shapes 改成 `[16, 32]` 后，两个 Tile 变成 16 行 32 列。`pl.concat` 只沿**列**拼接，所以会得到 16×64 的 Tile，而输出张量如果是 32×32 就放不下，store 会失败。正确做法是把 `c` 改成 `(16, 64)`，此时参考实现对应 `torch.cat([a, b], dim=1)` 且 `a`、`b` 也应是 `(16, 32)`。**行方向的拼接不能靠改 shapes 实现**。

若本机未装好环境，标记为「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：`tile_concat_32x32` 里为什么 `tile_out` 可以写类型注解 `pl.Tile[[32, 32], pl.FP32]`，而前面几个模块的算子都没写？

**答案**：这是可选的显式注解。PyPTO 的类型推断本来就能从 `pl.concat` 的两个操作数推出结果形状，所以不写也能编译。写出来的好处是：(a) 让读代码的人一眼看到形状变化，(b) 让推断结果和你的预期在编译期就被校验一遍，形状算错会尽早暴露。形状没变化的算子写不写意义不大，所以前几个模块的示例都省了。

**练习 2**：如果把 `tile_concat_32x32` 的参数 `c` 注解从 `pl.Out[pl.Tensor]` 改成裸的 `pl.Tensor`（去掉 `pl.Out`），会怎样？

**答案**：`c` 的方向会从 `Out` 退化为默认的 `In`。后果是编译器认为 `c` 是只读输入：`pl.store(tile_out, [0, 0], c)` 对一个 In 参数做写操作，会在解析或验证阶段被拒绝（即便侥幸通过，产物里 `c` 也不会出现在返回映射中，宿主侧读不到结果）。方向注解不是文档装饰，它决定了这个参数在 IR 里的 `param_directions_` 取值，进而决定代码生成是否为它生成写回。

**练习 3**：`pl.Out` 与 `pl.InOut` 有什么区别？什么时候必须用 `InOut`？

**答案**：`Out` 是纯写参数——内核不读它的旧值，只往里写，所以宿主侧传入的初始内容无关紧要（示例里全传 `torch.zeros` 正是这个原因）。`InOut` 是读写参数——内核**先读旧值再写新值**，典型场景是原地累加、residual 连接、把上一次迭代的输出当本次输入。如果一个算子逻辑上需要读 `c` 的旧值，就必须声明 `InOut`，声明成 `Out` 会让「读旧值」这步拿到未定义数据。

---

### 4.4 延伸视野：cube 单元与多级内存

#### 4.4.1 概念说明

前面所有算子都跑在**向量单元（vector unit）**上。AI 加速器还有一类**立方单元（cube unit）**专管矩阵乘。cube 用的数据不能直接从全局内存来，必须经过一条多级内存搬运链。这个模块只建立印象，不求深究——完整的内存空间体系属于 u5-l7。

#### 4.4.2 核心流程

矩阵乘的数据要走的完整路径：

```text
GM（全局内存）
  │  pl.load(target_memory=Mat)      → Mat  （L1，矩阵缓冲）
  │  pl.move(target_memory=Left)     → Left （L0A，左操作数）
  │  pl.move(target_memory=Right)    → Right（L0B，右操作数）
  ▼
pl.matmul(Left, Right)               → Acc  （L0C，累加器）
  │  pl.store
  ▼
GM
```

对比一下：向量化算子只需要 `load → 计算 → store` 一步到位；cube 算子中间多了 `Mat → Left/Right` 两跳。**越靠近计算单元，容量越小、速度越快**，这就是为什么分块尺寸选择会直接影响性能。

#### 4.4.3 源码精读

> [examples/beginner/05_matmul.py:29-38](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/examples/beginner/05_matmul.py#L29-L38) —— 一次完整的 64×64 矩阵乘：load 时显式指定 `target_memory=pl.MemorySpace.Mat`，再用两次 `pl.move` 分别送入 Left（L0A）和 Right（L0B），`pl.matmul` 在 L0C 上产出结果，最后 store 回 GM。

```python
@pl.jit
def matmul_64(a: pl.Tensor, b: pl.Tensor, c: pl.Out[pl.Tensor]):
    with pl.at(level=pl.Level.CORE_GROUP):
        tile_a_l1 = pl.load(a, [0, 0], [64, 64], target_memory=pl.MemorySpace.Mat)
        tile_b_l1 = pl.load(b, [0, 0], [64, 64], target_memory=pl.MemorySpace.Mat)
        tile_a_l0a = pl.move(tile_a_l1, target_memory=pl.MemorySpace.Left)
        tile_b_l0b = pl.move(tile_b_l1, target_memory=pl.MemorySpace.Right)
        tile_c_l0c = pl.matmul(tile_a_l0a, tile_b_l0b)
        pl.store(tile_c_l0c, [0, 0], c)
    return c
```

文件头的 docstring 把这条链写成了口诀：

> [examples/beginner/05_matmul.py:17-18](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/examples/beginner/05_matmul.py#L17-L18) —— "Memory hierarchy: GM -> Mat (L1) -> Left/Right (L0A/L0B) -> matmul -> Acc (L0C)"。

两个底层接口：

> [python/pypto/language/op/tile_ops.py:374-380](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/language/op/tile_ops.py#L374-L380) —— `load` 的 `target_memory` 参数：`MemorySpace.Vec` 或 `MemorySpace.Mat`，默认 `None` 表示留给编译器自动放置；但 **MX 布局的张量必须显式指定 `Mat`**。

> [python/pypto/language/op/tile_ops.py:615-639](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/language/op/tile_ops.py#L615-L639) —— `move(tile, target_memory)` 支持的目标空间：`Vec / Mat / Left / Right / LeftScale / RightScale`，还可选传 block 布局与 scatter 布局。

注意验证容差变了：

> [examples/beginner/05_matmul.py:48-50](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/examples/beginner/05_matmul.py#L48-L50) —— matmul 用的是 `rtol=1e-3, atol=1e-3`，比 elementwise 的 `1e-5` 宽松两个数量级。因为矩阵乘有 64 次累加，浮点误差会累积。

#### 4.4.4 代码实践

**实践目标**：观察「删掉显式内存搬运」之后编译器会不会替你补。

**操作步骤**：

1. 运行 `python examples/beginner/05_matmul.py`，确认 `OK`。
2. 复制为 `/tmp/matmul_probe.py`，把两次 `pl.move` 删掉，直接 `pl.matmul(tile_a_l1, tile_b_l1)`。
3. 重新运行。
4. 如果能跑通，再对比两版的耗时（宿主侧用 `time.perf_counter` 粗测即可）。

**需要观察的现象**：两种可能——(a) 编译器自动插入 move（`InferTileMemorySpace` 这个 Pass 就是干这个的，见 pass 文档 17 号）；(b) 校验器直接报「matmul 操作数必须在 L0A/L0B」。

**预期结果**：待本地验证。无论哪种结果都有收获：结果 (a) 说明显式 `move` 是给编译器留的控制权，结果 (b) 说明它是硬性约束。**这正是你需要在本机跑一次才能确定的事情**，也是本实践的价值所在。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `pl.load` 有 `target_memory` 参数，而 `pl.store` 没有？

**答案**：load 的目的地是片上一块具体的存储，所以要指定放到哪一级（Vec/Mat）；store 是把数据写回 GM 的输出张量，目的地只有 GM 一个，没有「放到哪一级」的选择，自然不需要这个参数。

**练习 2**：`Left` 和 `Right` 为什么要分成两个内存空间，而不是共用一个 `L0`？

**答案**：因为矩阵乘的两个操作数在硬件上走的是两条物理通路——左操作数进 L0A、右操作数进 L0B，cube 单元同时从两侧读数才能在一个周期内完成一拍乘累加。分成两个空间让 IR 能精确表达「哪个操作数在哪个物理缓冲」，代码生成也才能发出正确的搬运指令。`move` 的 `blayout` 参数进一步控制块内布局，这是后端布局规则（u6-l3）的输入。

---

## 5. 综合实践

**5.1 任务描述**

实现一个 **tanh 近似的 GELU 算子**：

- 输入 `x`、输出 `output` 均为 `pl.Tensor[[1024, 1024], pl.FP32]`，输出用 `pl.Out` 标注。
- 用 torch 的 GELU tanh 近似做参考实现，最大误差控制在 **1e-4 以内**。

数学公式：

\[ \mathrm{GELU}(x) = 0.5\,x\left(1 + \tanh\left(c\,\left(x + k\,x^{3}\right)\right)\right),\quad c=\sqrt{\tfrac{2}{\pi}}\approx 0.7978845608,\; k=0.044715 \]

**5.2 三个必须先想清楚的设计点**

**设计点一：1024×1024 装不进一个 Tile，必须双层分块循环。**

模块 4.1 只循环了行，因为那里的张量宽度恰好等于 Tile 宽度。这里行列都超出，需要嵌套 `pl.range`。嵌套循环在仓库里是真实可用的写法，见 [examples/intermediate/05_assemble.py:128-129](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/examples/intermediate/05_assemble.py#L128-L129)：

```python
for b in pl.range(4):
    for i in pl.range(8):
        ...
```

**设计点二：没有 `pl.tanh`，要用模块 4.2 的恒等式拆。**

\[ \tanh(u) = 2\,\sigma(2u) - 1,\qquad \sigma(v)=\frac{1}{1+e^{-v}} \]

**设计点三：验证参照必须是同公式，不能是 torch 默认 GELU。**

`torch.nn.functional.gelu(x)` 默认 `approximate='none'`，走精确 erf；而 `approximate='tanh'` 走的就是上面这条公式。用前者做参照，1e-4 的容差会被约 3e-4 的**模型误差**卡死，你会误以为自己写错了——这是模块 4.2.4 已经演练过的陷阱。

**5.3 参考实现**

以下为**示例代码**（不是仓库原有文件），请保存到仓库外的 `/tmp/gelu_tanh.py` 运行，不要写进 `examples/`（项目规则禁止随意新增示例，见 `.claude/rules/testing-and-examples.md`）：

```python
# /tmp/gelu_tanh.py  —— 示例代码，非仓库文件
import math

import pypto.language as pl
import torch
from pypto.runtime import RunConfig

ROWS = COLS = 1024
TILE = 64                      # 1024/64 = 16，共 16x16 = 256 块
C = 0.7978845608               # sqrt(2/pi)
K = 0.044715


@pl.jit
def gelu_tanh(
    x: pl.Tensor[[ROWS, COLS], pl.FP32],
    output: pl.Out[pl.Tensor[[ROWS, COLS], pl.FP32]],
):
    with pl.at(level=pl.Level.CORE_GROUP):
        for i in pl.range(ROWS // TILE):
            for j in pl.range(COLS // TILE):
                off = [i * TILE, j * TILE]
                tile_x = pl.load(x, off, [TILE, TILE])

                # u = C * (x + K * x^3)
                t = pl.mul(tile_x, tile_x)          # x^2
                t = pl.mul(t, tile_x)               # x^3
                t = pl.mul(t, K)                    # K*x^3
                t = pl.add(tile_x, t)               # x + K*x^3
                u = pl.mul(t, C)                    # u

                # tanh(u) = 2*sigmoid(2u) - 1
                t = pl.exp(pl.mul(u, -2.0))         # e^{-2u}
                t = pl.recip(pl.add(t, 1.0))        # sigmoid(2u)
                t = pl.sub(pl.mul(t, 2.0), 1.0)     # tanh(u)

                # 0.5 * x * (1 + tanh(u))
                t = pl.mul(pl.add(t, 1.0), tile_x)
                t = pl.mul(t, 0.5)
                pl.store(t, off, output)
    return output


if __name__ == "__main__":
    torch.manual_seed(0)
    config = RunConfig()

    x = torch.randn(ROWS, COLS, dtype=torch.float32)
    out = torch.zeros_like(x)
    gelu_tanh(x, out, config=config)

    # 正确的参照：同一条 tanh 近似公式
    expected = torch.nn.functional.gelu(x, approximate="tanh")
    max_diff = (out - expected).abs().max().item()
    assert torch.allclose(out, expected, rtol=1e-5, atol=1e-4), (
        f"gelu_tanh failed: max diff = {max_diff:.3e}"
    )
    print(f"OK  vs tanh-approx  max diff = {max_diff:.3e}")

    # 参考：与精确 GELU 的差距是模型误差，不是实现错误
    exact = torch.nn.functional.gelu(x)
    print(f"    tanh-approx vs exact erf  max diff = "
          f"{(expected - exact).abs().max().item():.3e}  (模型误差)")
```

**5.4 逐行讲解关键决策**

| 决策 | 理由 | 依据 |
| --- | --- | --- |
| `TILE = 64` 而不是 128 | 计算链上同时存活的 Tile 有 2~3 个，64×64 FP32 = 16 KB/个，片上压力小得多；128×128 = 64 KB/个，多个中间量叠加后容易超出统一缓冲 | hello world 用 128×128 只有一个中间量，本算子链更长 |
| 中间变量统一重绑为 `t` | Python 变量重绑定是合法的，重绑定后旧 Tile 在 IR 里成为不可达节点，生命周期分析（u5-l7 的 MemoryReuse）能据此复用空间 | `05_assemble.py:132` 在循环里反复重绑 `tile_x` |
| `tile_x` 不重绑、留到最后 | 最后一步 `pl.mul(..., tile_x)` 还要用原始输入，所以它的生命周期必须覆盖整条链 | 与 [04_activation.py:65-71](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/examples/beginner/04_activation.py#L65-L71) 的 `tile_x` 同构 |
| `pl.exp(pl.mul(u, -2.0))` | `Tile * float` 经分发落到 `tile.muls`，标量作为立即数内嵌，不产生广播 Tile | [unified_ops.py:367-368](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/language/op/unified_ops.py#L367-L368) |
| `off = [i * TILE, j * TILE]` 复用 | load 和 store 用**同一个**偏移：读哪块就写哪块，输出张量和输入张量形状相同 | 模块 4.1 的口诀 |
| `approximate="tanh"` 做参照 | 同公式对照才能测出「实现精度」；对照精确版测出的是「模型精度」 | 模块 4.2.4 / 5.2 设计点三 |

**5.5 观察与预期结果**

按顺序观察三件事：

1. **正确性**：第一行打印的 max diff 应远小于 1e-4（参考量级 1e-5~1e-6，因为公式逐项一致，剩下的只有硬件 exp/recip 与 CPU 的浮点差异）。
2. **模型误差**：第二行打印的 tanh 近似 vs 精确 erf 的差距约在 1e-4~1e-3 量级。**这个数字不是你的 bug**，是公式本身的近似误差。
3. **精度兜底**：如果第 1 步的 max diff 落在 1e-4 边缘，把 `pl.recip(t, high_precision=True)` 打开再试（FP32 支持，见 [tile_ops.py:1175-1186](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/language/op/tile_ops.py#L1175-L1186) 的 docstring）。`exp` 没有这个开关。

**进阶实验**：把 `TILE` 改成 128 再跑一次，观察编译是否仍然通过、耗时如何变化。这个实验让你第一次直观感受到「Tile 尺寸是性能调优的第一个旋钮」——为 u7-l3 铺路。

**待本地验证**：以上数值量级（尤其是 max diff 的具体值和 TILE=128 是否编译通过）依赖本机后端与模拟器行为，作者无法在编写讲义时运行确认，请以本地实测为准。

---

## 6. 本讲小结

- **三段式是 Tile 级算子的统一骨架**：`pl.load` 搬进片上 → tile 算子计算 → `pl.store` 写回。张量比 Tile 大时必须分块循环，口诀是 **shapes 恒为 Tile 尺寸、offsets 在源张量坐标系中移动**（[02_elementwise.py:81-95](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/examples/beginner/02_elementwise.py#L81-L95)）。
- **复杂函数靠原语组合，不靠内置算子**：SiLU/GELU/SwiGLU 全部用 `mul`/`add`/`exp`/`recip` 拼出。决定一个函数要不要内置的标准是「是否对应真实硬件指令」，`relu` 是、SiLU 不是。
- **`Tile op float` 会路由到 `tile.muls/adds/subs`**，标量作为立即数内嵌，不产生广播 Tile——能用标量形式就别造全量 Tile（[unified_ops.py:361-371](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/language/op/unified_ops.py#L361-L371)）。
- **`pl.Out` 是纯解析期的方向标记，运行时恒等透传**（`Out[T] is T`），它写入 IR 的参数方向字段，决定编译器如何处理该参数，不产生任何运行时开销（[direction.py:33-52](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/language/typing/direction.py#L33-L52)）。
- **正确性验证的标准姿势**：PyPTO 算子当被测对象、torch 当参考实现，且**必须同公式对照**。要能区分「实现错误」（对照同公式仍超差）和「模型误差」（近似公式与精确版的固有差距）。
- **cube 单元走多级内存链** GM → Mat(L1) → Left/Right(L0A/L0B) → matmul → Acc(L0C)，比向量算子多两跳搬运，这是矩阵乘性能调优的物理根源。

---

## 7. 下一步学习建议

本讲你已经能独立写出并验证一个完整的 Tile 级算子，接下来的学习按两条线推进：

**主线（推荐先走）：DSL 语言基础，单元 2**

- **u2-l1 jit 装饰器与特化缓存**：本讲你多次看到「同一个 `@pl.jit` 函数换 shape 就再编译一次」，下一讲讲清缓存键到底由什么组成、为什么 `TILE = 64` 和 `TILE = 128` 是两次编译。
- **u2-l2 Tensor 与 Tile 类型注解**：本讲综合实践用了 `pl.Tensor[[ROWS, COLS], pl.FP32]` 的显式注解，下一讲系统地讲静态 shape、动态维度（dyndim）和 `Scalar` 注解。
- **u2-l5 标量计算与控制流**：本讲的 `pl.range` 只是拿来就用，下一讲讲清它和内建 `range` 在 IR 层的区别，以及 `if` 分支怎么被追踪。

**辅线（可穿插阅读的源码）**：

- `examples/beginner/03_scalar_ops.py` —— 补上本讲没碰的标量算子。
- `examples/intermediate/05_assemble.py` —— 看 `pl.slice` / `pl.tile.assemble` 怎么在片上原地拼 Tile，比 `pl.concat` 更灵活。
- `python/pypto/language/op/tile_ops.py` 的函数目录 —— 通读一遍函数名，建立「手上有哪些积木」的索引；不用逐个精读，遇到再用。

带着本讲遗留的两个问题去读下一讲会更有收获：**(1) 改 `TILE` 为什么触发重新编译？(2) 1024×1024 的 `ROWS`/`COLS` 是怎么进入特化键的？**
