# Hello World 逐行精读

## 1. 本讲目标

本讲以 PyPTO 官方最简示例 `examples/beginner/01_hello_world.py` 为标本，逐行拆解一个最小可运行算子的每一处语法。读完本讲，你应当能够：

1. 说清 `@pl.jit` 装饰器在一次调用里做了什么：按输入张量的 shape/dtype **特化** → 解析成 IR → **编译** → **缓存** → 上设备执行。
2. 理解 `with pl.at(level=pl.Level.CORE_GROUP)` 作用域的含义：它把一段代码标记为「片上计算区域」，后续会被编译器外提成独立的设备内核。
3. 准确区分 **Tensor**（全局内存中的整块数组）与 **Tile**（片上统一缓冲区里的数据块）两类数据，以及 `pl.load` / `pl.store` 在两者之间搬运数据的三段式编程模型。
4. 明白 `pl.Out[pl.Tensor]` 为什么同时是「参数」和「输出」。
5. 独立完成代码实践：把 Tile 尺寸从 128×128 改成 64×64，用分块循环算完 128×128 的张量加法。

## 2. 前置知识

阅读本讲前，你应当具备（对应前面几讲的内容）：

- **环境已就绪**（u1-l2）：PyPTO 已按开发模式安装，`pypto_core` C++ 扩展可用。示例默认在 `a2a3sim`（Ascend 910B 模拟器）上执行，本机验证需要该工具链可用；没有环境也不影响源码阅读部分。
- **三层架构认知**（u1-l3）：PyPTO 分为 C++ 核心层（IR、Pass、代码生成）、nanobind 绑定层（`pypto_core`）、Python API 层（`python/pypto`，其中 `language` 子包就是 DSL）。本讲读的 `pl.*` 全部来自 Python API 层，但它们最终调用的算子定义在 C++ 的 OpRegistry 里。
- **基础 Python 能力**：装饰器、上下文管理器（`with` 语句）、类型注解（`x: pl.Tensor` 这种写法）。
- **torch 基础**：会创建 `torch.full` / `torch.zeros` 张量，会用 `torch.allclose` 对照结果。

两个术语提前解释：

- **JIT（Just-In-Time，即时编译）**：函数不在定义时编译，而在第一次被调用、看到真实参数后再编译。PyPTO 按「shape + dtype」组合编译，同一组合的第二次调用直接命中缓存。
- **特化（specialize）**：把函数签名中抽象的类型注解，替换成调用时具体的 shape/dtype 常量，生成一份「量体裁衣」的代码。例如 128×128 的输入会生成一份带 128 字面量的内核。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| [examples/beginner/01_hello_world.py](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/examples/beginner/01_hello_world.py) | 最简示例：128×128 张量加法 | 逐行精读的主标本 |
| [python/pypto/language/\_\_init\_\_.py](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/language/__init__.py) | DSL 入口 `pypto.language`（习惯别名 `pl`） | `pl.load`/`pl.store`/`pl.add`/`pl.Out` 等`pl.*`名字各自从哪里导出 |
| [python/pypto/jit/decorator.py](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/jit/decorator.py) | `@pl.jit` 装饰器与 `JITFunction` | `__call__` 的特化/缓存/执行流程 |
| [python/pypto/jit/cache.py](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/jit/cache.py) | JIT 缓存键 | 缓存键由哪些成分组成 |
| [python/pypto/language/dsl_api.py](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/language/dsl_api.py) | `pl.at` 等作用域辅助 API | `AtContext` 与 `at()` 的语义 |
| [python/pypto/language/typing/direction.py](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/language/typing/direction.py) | `Out` / `InOut` 方向注解 | `pl.Out[T]` 只是个标记 |
| [python/pypto/language/typing/tensor.py](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/language/typing/tensor.py) / [typing/tile.py](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/language/typing/tile.py) | Tensor / Tile 双身份类型 | 注解模式与运行时模式 |
| [python/pypto/language/op/tile_ops.py](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/language/op/tile_ops.py) | Tile 级算子的 Python 封装 | `load` / `store` 参数含义 |
| [python/pypto/runtime/runner.py](https://github.com/hw-native-sys-pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/runtime/runner.py) | `RunConfig` 与运行入口 | 默认平台、执行链路终点 |

## 4. 核心概念与源码讲解

先看全貌。整个示例的有效代码只有 8 行（去掉版权头和 `main`）：

[examples/beginner/01_hello_world.py:28-35](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/examples/beginner/01_hello_world.py#L28-L35) —— `@pl.jit` 装饰一个三参数函数：`a`、`b` 是输入 Tensor，`c` 用 `pl.Out[pl.Tensor]` 标注为输出；函数体内用一个 `pl.at` 作用域包住「load → add → store」三步。

```python
@pl.jit
def tile_add(a: pl.Tensor, b: pl.Tensor, c: pl.Out[pl.Tensor]):
    with pl.at(level=pl.Level.CORE_GROUP):
        tile_a = pl.load(a, [0, 0], [128, 128])
        tile_b = pl.load(b, [0, 0], [128, 128])
        tile_c = pl.add(tile_a, tile_b)
        pl.store(tile_c, [0, 0], c)
    return c
```

下面按四个最小模块拆开讲。

### 4.1 `@pl.jit`：特化、编译与缓存

#### 4.1.1 概念说明

`@pl.jit` 把一个普通 Python 函数变成 `JITFunction` 对象。它本身**不编译任何东西**——编译发生在第一次调用时。文件自带的模块注释就概括了这条链路：

[examples/beginner/01_hello_world.py:13-17](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/examples/beginner/01_hello_world.py#L13-L17) —— `@pl.jit decorator: function specializes on torch tensor shape/dtype, compiles, caches`（按 torch 张量的 shape/dtype 特化、编译、缓存）。

三个关键词：

- **特化**：从实参 torch 张量提取 `(shape, dtype)` 元信息，把函数签名里的抽象注解落实为具体值；
- **编译**：把特化后的源码解析成 IR，跑 Pass 流水线，生成 PTO 指令产物；
- **缓存**：以「源码哈希 + shape + dtype + 平台 + 策略……」为键缓存编译产物，下次同键调用跳过编译。

这和 torch 的动态图执行完全不同：torch 每次都走 Python 解释器，PyPTO 第一次调用后就变成了「调度一个已编译的二进制内核」。

#### 4.1.2 核心流程

`JITFunction.__call__` 的执行流程（与 decorator.py 模块 docstring 中写明的 7 步一致）：

```text
tile_add(a, b, c, config=RunConfig())
  │
  ├─ 1. 分类参数：张量 vs 标量；从 torch.Tensor 提取 TensorMeta(shape, dtype)
  ├─ 2. 构造 CacheKey（源码哈希 + 每个张量的 shape/dtype + 平台 + 策略 ...）
  ├─ 3. 查 L1 内存缓存 ──命中──→ 取出 CompiledProgram
  │        └──未命中──→ 特化 → pl.parse() 解析成 IR → ir.compile() 编译 → 存入缓存
  ├─ 4. CompiledProgram(*ordered_args) 在设备上执行（c 被原地写回）
  └─ 5. 返回
```

缓存命中与否的判据只看键的成分：**同一个函数、同一组 shape/dtype、同一平台**就是同一份产物。

#### 4.1.3 源码精读

调用入口。`__call__` 只做「解析出编译产物 → 执行」两件事：

[python/pypto/jit/decorator.py:2133-2169](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/jit/decorator.py#L2133-L2169) —— docstring 明确说明：首次调用对给定 shape/dtype 组合做特化、解析、编译并放入 L1 内存缓存，后续同键调用完全跳过编译；`config=RunConfig(...)` 关键字在这里被 JIT 机制消费（其中 `strategy` 等编译侧字段转发给 `ir.compile()`），不会传给被装饰函数本体。注意返回值约定：**原地写回型调用返回 `None`，结果在 `c` 里**。

缓存查找与编译的核心在 `_resolve_compiled`：

[python/pypto/jit/decorator.py:2091-2122](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/jit/decorator.py#L2091-L2122) —— 第 2091 行调用 `make_cache_key(...)` 组装缓存键，键里包含：函数源码哈希 `source_hash`、每个张量参数的 shape/dtype/layout（第 2094-2096 行）、标量取值、平台 `platform`、优化策略 `strategy`；第 2113 行是典型的 L1 缓存模式——`if key not in self._cache` 才触发编译 `self._compile(...)`，否则第 2127 行直接复用。

缓存键的组装规则在 cache.py：

[python/pypto/jit/cache.py:216-231](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/jit/cache.py#L216-L231) —— 遍历参数名，把每个张量的具体 shape 装进 `TensorCacheInfo`。注意第 221-223 行：被声明为动态维的 dim 在键里记成 `None`，这样不同具体长度的动态维**共享**同一个缓存条目（这是 u2-l1 的伏笔）。

[python/pypto/jit/cache.py:114-131](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/jit/cache.py#L114-L131) —— 源码哈希把 PyPTO 版本号混入 SHA-256：升级 PyPTO 会自动作废旧缓存，无需手动清理。

而 `pl.jit` 名字本身从 `pypto.jit` 导出，最终在 language 包聚合：

[python/pypto/language/\_\_init\_\_.py:40-41](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/language/__init__.py#L40-L41) —— `from pypto.jit import JITFunction, jit`，这就是 `@pl.jit` 里 `jit` 的来源（详见 4.4.3 对该文件的完整拆解）。

#### 4.1.4 代码实践：观察「编译一次、命中多次」

1. **实践目标**：亲眼确认同一 shape 的第二次调用命中缓存，不重复编译。
2. **操作步骤**：
   - 复制 `01_hello_world.py` 为 `01_hello_world_twice.py`（放在仓库外或临时目录均可，本实践不改仓库文件）；
   - 在 `if __name__ == "__main__":` 里把 `tile_add(a, b, c, config=RunConfig())` 连续调用两次，第二次前把 `c` 重新置零，并分别用 `time.perf_counter()` 包住两次调用打印耗时。
3. **需要观察的现象**：第一次调用包含编译（解析、跑 Pass、代码生成），耗时长；第二次调用明显更快。
4. **预期结果**：两次断言都通过，打印两次耗时且第二次显著更短。（具体毫秒数依赖机器，待本地验证。）
5. 若想看编译过程到底发生了什么，可把 `RunConfig(dump_passes=True)` 传入，在输出目录观察逐 Pass 的 IR 导出（详细用法在 u3-l5）。

#### 4.1.5 小练习与答案

**练习 1**：同一个 `tile_add`，先用 `(128, 128)` 输入调用，再用 `(64, 64)` 输入调用，会发生几次编译？

**答案**：两次。shape 是缓存键的组成部分，`(128, 128)` 与 `(64, 64)` 生成两个不同的键，各自触发一次编译，产物各占一个缓存条目（参照 [cache.py:216-231](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/jit/cache.py#L216-L231) 中 shape 进键的逻辑）。

**练习 2**：为什么函数源码哈希要混进缓存键？只看 shape/dtype 不够吗？

**答案**：不够。若你修改了内核函数体（比如把 `pl.add` 改成 `pl.mul`）而输入 shape 不变，仅凭 shape/dtype 会错误命中旧产物。`source_hash` 保证源码一变键就变（[cache.py:114-131](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/jit/cache.py#L114-L131)），同时还混入 PyPTO 版本号，升级框架也会自动失效旧缓存。

### 4.2 `pl.at` 作用域与计算层级

#### 4.2.1 概念说明

`with pl.at(level=pl.Level.CORE_GROUP):` 不是普通的 Python 上下文管理器——**解析器（parser）会识别这个语法模式**，把 `with` 块内的语句收集成一个「作用域」，后续由 `OutlineInCoreScopes` 等 Pass 外提成独立的设备内核函数（u5-l4 会读那个 Pass）。

为什么需要它？因为硬件上是「主机 + AI Core 分层」的 MPMD 模型：哪些代码跑在片上（AI Core）、哪些跑在编排层（AICPU），必须显式划界。`pl.at` 就是这个划界语法：**块内的代码声明自己要在某个层级硬件上执行**。

`Level` 是一个从 C++ 绑定层导出的枚举，描述「Linqu 机器模型」中的层级：

[python/pypto/pypto_core/ir.pyi:875-903](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/pypto_core/ir.pyi#L875-L903) —— 从底向上：`AIV`（单向量核）、`AIC`（单立方体核）、`CORE_GROUP`（核组，如 1 个 AIC + 2 个 AIV）、`CHIP`（芯片）、`HOST`（主机）、`CLUSTER_0/1`（集群）。hello world 用的 `CORE_GROUP` 即「一个核组整体执行这段计算」，是最常用的片上算子粒度。

#### 4.2.2 核心流程

```text
with pl.at(level=CORE_GROUP):
    load / add / store
        │  (DSL 解析阶段)
        ▼
ScopeStmt(InCore)          ← 作用域语句节点，包住块内全部语句
        │  (Pass 流水线：OutlineInCoreScopes，第 8 个 Pass)
        ▼
独立 IR Function + 调用点替换   ← 成为可下发生片的内核
```

层级选择决定的是「执行单元的粒度」：`CORE_GROUP` 是单核组内的同步执行；更大的层级（如 `HOST`、`CLUSTER`）则对应编排/多核协同语义。对入门算子而言 `CORE_GROUP` 几乎总是正确选择。

#### 4.2.3 源码精读

[python/pypto/language/dsl_api.py:1187-1206](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/language/dsl_api.py#L1187-L1206) —— `at()` 函数签名与开头的语义说明：`level=CORE_GROUP` 且不带 `optimizations` 时生成 `ScopeStmt(InCore)`；其他 level 生成 Hierarchy 作用域。它只是一个「标记工厂」，真正的结构由解析器在 AST 层面识别。

[python/pypto/language/dsl_api.py:1141-1149](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/language/dsl_api.py#L1141-L1149) —— `AtContext` 类的 docstring 写明：解析器识别 `with pl.at(...)` 模式并创建对应的 `ScopeStmt`。

[python/pypto/language/dsl_api.py:1174-1181](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/language/dsl_api.py#L1174-L1181) —— `__enter__` 的注释很关键：`@pl.jit`/`@pl.program` 路径**解析函数源码而不是执行它**，所以这个上下文管理器在正常流程中根本不会被真正「进入」；返回 `self` 只是为了让脚本被直接执行（例如做 lint）时语法合法。这解释了 DSL 的本质——**你写的是 Python 语法，但语义由解析器重新解释**。

`Level` 在 language 包中的导出：

[python/pypto/language/\_\_init\_\_.py:43-56](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/language/__init__.py#L43-L56) —— 从 `pypto.pypto_core.ir`（C++ 绑定层）导入 `Level`、`Role`、`MemorySpace`、`TensorLayout` 等枚举，`pl.Level.CORE_GROUP` 由此而来。

#### 4.2.4 代码实践：源码阅读型——追踪 `pl.at` 被翻译成什么

1. **实践目标**：在 IR 层面确认 `with pl.at(...)` 变成了作用域语句。
2. **操作步骤**：
   - 阅读示例时问自己：`with` 块里的 4 条语句在 IR 里如何组织？
   - 运行 `python examples/beginner/01_hello_world.py`（需 u1-l2 环境）确认示例可跑通；
   - 进阶（可选）：给 `RunConfig` 加 `dump_passes=True` 重跑，在导出目录最前面的 Pass 里找到包含 `CORE_GROUP` 作用域的 IR 文本；到 `tests/ut/language/test_at_context.py` 中找一个最小断言用例对照阅读。
3. **需要观察的现象**：dump 的早期 Pass IR 中，load/add/store 语句被包在一个 InCore 作用域内；跑过 `outline_incore_scopes` 之后（第 8 个 Pass），这段代码变成独立函数 + 一处调用。
4. **预期结果**：能指出「作用域 → 独立内核函数」的分界发生在哪个 Pass。dump 文件名即 Pass 名（待本地验证）。

#### 4.2.5 小练习与答案

**练习 1**：如果把 `pl.Level.CORE_GROUP` 写成不存在的 `pl.Level.NO_SUCH_LEVEL`，错误发生在什么时候？

**答案**：在 Python 层立即失败——`Level` 是 C++ 绑定导出的枚举（[ir.pyi:875](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/pypto_core/ir.pyi#L875)），属性访问在装饰/解析前的求值阶段就抛 `AttributeError`，不会等到编译期。

**练习 2**：`with pl.at(...)` 块内定义的 Tile 变量（如 `tile_a`）能在块外继续使用吗？

**答案**：不应这样写。Tile 是片上数据，生命周期绑定在作用域内；作用域外提成独立内核后，块内变量属于那个内核的局部世界。数据要跨出作用域，正确方式是把结果 `pl.store` 回 Tensor（全局内存），在块外以 Tensor 形态使用。

### 4.3 `pl.load` / `pl.store` / `pl.add`：三段式与 `pl.Out`

#### 4.3.1 概念说明

PyPTO Tile 级编程的铁律是三段式：

```text
pl.load（全局内存 → 片上 Tile）→ 片上算子计算 → pl.store（Tile → 全局内存）
```

- **`pl.load(tensor, offsets, shapes)`**：从 Tensor 的 `offsets` 坐标处取一块 `shapes` 大小的数据进片上，返回一个 **Tile**。
- **`pl.add(tile_a, tile_b)`**：片上逐元素加，返回新 Tile。`pl.add` 是**统一分发算子**——同样写 `pl.add`，给 Tensor 就生成 Tensor 级 IR，给 Tile 就生成 Tile 级 IR。
- **`pl.store(tile, offsets, tensor)`**：把 Tile 写回 Tensor 的 `offsets` 处，返回该 Tensor。

`c: pl.Out[pl.Tensor]` 则回答「输出从哪走」：`Out` 标记该参数是**输出参数**，设备直接把结果**原地写进**调用者传入的 `c` 张量里——这正是 `__call__` 原地写回型调用返回 `None`、而断言却检查 `c` 的原因。

#### 4.3.2 核心流程

以 hello world 为例的一次数据旅行：

```text
torch 张量 a, b, c（宿主内存）
   │  (JIT 调用时作为参数下发给设备，a/b/c 在设备全局内存 GM 有对应)
   ▼
pl.load(a,[0,0],[128,128]) ──► tile_a（片上 UB，128×128×FP32）
pl.load(b,[0,0],[128,128]) ──► tile_b
   ▼
pl.add(tile_a, tile_b)    ──► tile_c（仍在片上）
   ▼
pl.store(tile_c,[0,0],c)  ──► 写回 GM 中的 c
```

当张量大于一个 Tile 时，就需要分块：Tile 尺寸固定，循环移动 `offsets` 覆盖整张张量。一个 \( M \times N \) 的张量按 \( T_r \times T_c \) 的 Tile 分块，需要的块数为

\[
\left\lceil \frac{M}{T_r} \right\rceil \times \left\lceil \frac{N}{T_c} \right\rceil
\]

`offsets` 永远用**源张量坐标系**，`shapes` 永远是 **Tile 尺寸**——「形状不动、只挪偏移」是分块循环的写法口诀。

#### 4.3.3 源码精读

`load` 的完整签名与语义：

[python/pypto/language/op/tile_ops.py:374-424](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/language/op/tile_ops.py#L374-L424) —— `load(tensor, offsets, shapes, valid_shape=None, target_memory=None, clamp=False)`：把张量数据拷进统一缓冲区（tile）。docstring 特别说明 `offsets`/`shapes` **始终在源张量坐标系**；只读有效范围（valid extent），因此 Tile 可以比源中实际存在的区域大。第 416-424 行是落地：调用 IR 层 `_ir_ops.load(...)` 构造 Call 表达式，包成 `Tile` 返回——每个 DSL 算子最终都变成 IR 的一个 Call 节点。

`store` 的完整签名与语义：

[python/pypto/language/op/tile_ops.py:427-470](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/language/op/tile_ops.py#L427-L470) —— `store(tile, offsets, output_tensor, ...)`：把 Tile 数据拷回张量；返回值是 `output_tensor.__class__(expr=call_expr)`，即**返回的就是被写的张量本身**（携带 store 表达式），所以示例里 `return c` 能把输出张量交回调用链。`atomic=` 参数（如 `AtomicType.Add`）支持多核原子累加，是 split-K 优化的基础（u7-l3）。

`pl.add` 的类型分发：

[python/pypto/language/op/unified_ops.py:312-327](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/language/op/unified_ops.py#L312-L327) —— 同名 `add` 按 `isinstance` 分派：`Tensor + Tensor` 走 `_tensor.add`，`Tile + Tile` 走 `_tile.add`，`Tile + 标量` 走 `_tile.adds`，纯标量走标量表达式。hello world 里两个操作数都是 `pl.load` 的返回值（Tile），所以生成的是 `tile.add`。

`Out` 的真面目：

[python/pypto/language/typing/direction.py:33-52](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/language/typing/direction.py#L33-L52) —— `Out` 只是 AST 层的方向标记：`__class_getitem__` 把 `Out[T]` 原样返回 `T`（运行时无包装），类型检查器视角下它是 `Annotated[T, "Out"]`。解析器读函数签名时看到这个标记，就把参数方向记为输出（In 是默认方向，不需要写）。对应的还有 `InOut`（读写）。

#### 4.3.4 代码实践：读 `chunked_add`，理解「只挪偏移」

1. **实践目标**：在动手改 hello world 之前，先读懂官方已经写好的分块范例。
2. **操作步骤**：打开 [examples/beginner/02_elementwise.py:81-95](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/examples/beginner/02_elementwise.py#L81-L95) 的 `chunked_add`：`512×128` 的张量装不进一个 `128×128` 的 Tile，于是用 `for i in pl.range(ROWS // TILE_ROWS)` 循环 4 次，`pl.load` 的第一个偏移写成 `i * TILE_ROWS`，`shapes` 恒为 `[TILE_ROWS, COLS]`。
3. **需要观察的现象**：对比第 40-46 行的 `tile_add_128`（单块、偏移 `[0,0]`）与 `chunked_add`（多块、偏移随循环变化），唯一区别就是 offsets。
4. **预期结果**：能复述口诀「shapes 是 Tile 尺寸不变，offsets 在源坐标系里移动」；运行整个文件打印 `OK`（待本地验证）。

#### 4.3.5 小练习与答案

**练习 1**：`pl.load(a, [0, 0], [128, 128])` 的三个参数各是什么含义？第三个 `[128, 128]` 是张量 a 的形状吗？

**答案**：参数依次是源张量、各维偏移、要加载的区域形状。第三个参数**不是**张量形状，而是 Tile 尺寸（要搬多大一块）；张量 a 恰好也是 128×128，所以一次装完。若 a 是 512×128，就需要循环 4 次移动偏移（见 `chunked_add`）。

**练习 2**：为什么 `pl.store` 之后示例还 `return c`？直接不返回行不行？

**答案**：`store` 返回的就是被写的输出张量（[tile_ops.py:470](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/language/op/tile_ops.py#L470)），`return c` 让函数在 IR 层把 `c` 声明为返回值，配合 `pl.Out` 方向形成完整的输出约定；结果同时通过原地写回生效。对 hello world 这种原地写回用法，宿主侧的 `c` 已经被更新，是否 `return` 不影响 `torch.allclose(c, ...)` 的判定。

**练习 3**：把 `pl.add(tile_a, tile_b)` 换成 `pl.add(a, b)`（直接用 Tensor）会发生什么？

**答案**：由于统一分发（[unified_ops.py:319-324](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/language/op/unified_ops.py#L319-L324)），这会走 `_tensor.add` 生成 **Tensor 级**算子——不再是片上 Tile 计算，而是留给编译器自动切分降级（u5-l5 的 ConvertTensorToTileOps）。功能等价，但「手动控制片上分块」的性能手段就交还给编译器了。

### 4.4 Tensor 与 Tile：两类数据，一条搬运链

#### 4.4.1 概念说明

| 维度 | Tensor | Tile |
| --- | --- | --- |
| 生存空间 | 设备全局内存（GM），整块大数组 | 片上统一缓冲区（UB），固定尺寸小块 |
| 注解写法 | `pl.Tensor[[64, 128], pl.FP16]` | `pl.Tile[[64, 64], pl.FP32]` |
| 从哪来 | 函数参数 / `pl.create_tensor` 等 | `pl.load(...)` 的返回值 |
| 典型操作 | 传参、被 load 的源、被 store 的目的 | `tile.*` 算子的操作数 |
| 编程视角 | 算法层：想多大就多大 | 硬件层：一块寄存器/缓存资源 |

Tile 是**硬件感知**的：它的形状必须匹配片上缓冲与指令的约束（例如矩阵乘分块尺寸），这正是性能工程师的工作层面。Tensor→Tile 的搬运（load）与反向搬运（store）都是真实的内存移动，是可以优化的开销——所以「减少搬运、提高 Tile 复用」是性能调优的核心思路。

另一个关键设计：`Tensor` 和 `Tile` 类都有**双重身份**。作为类型注解时（`x: pl.Tensor[[128,128], pl.FP32]`），它们是描述签名的「模板」；作为运行时对象时，它们是 IR 表达式的「包装器」（`pl.load` 返回的 Tile 内部包着一个 Call 表达式）。

#### 4.4.2 核心流程

双身份靠元类（metaclass）的下标协议实现：

```text
注解模式：pl.Tensor[[128,128], pl.FP32]
    → TensorMeta.__getitem__(( [128,128], FP32 ))
    → Tensor(shape, dtype, _annotation_only=True)   # 只存元信息，不包表达式

运行时模式：pl.load(a, [0,0], [128,128])
    → 构造 IR Call 表达式
    → Tile(expr=call_expr)                          # 包住表达式
```

而 `pl.*` 命名空间统一由 `python/pypto/language/__init__.py` 聚合导出，该文件的 import 组织直接反映了算子的三层分类（tensor. / tile. / 统一分发）。

#### 4.4.3 源码精读

Tensor 的下标协议：

[python/pypto/language/typing/tensor.py:38-77](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/language/typing/tensor.py#L38-L77) —— `TensorMeta.__getitem__` 支持 `Tensor[[shape], dtype]`、三元素（带 layout 或 memref）、四元素形式，全部走 `_annotation_only=True` 的注解模式。hello world 用的是裸 `pl.Tensor`（不带下标），表示「shape/dtype 由调用时的实参决定」——这正是需要 JIT 特化的原因。

[python/pypto/language/typing/tensor.py:147-159](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/language/typing/tensor.py#L147-L159) —— `Tensor` 类 docstring 直白列出双重用途：1. 函数签名里的类型注解；2. IR 表达式的运行时包装。

Tile 同构：

[python/pypto/language/typing/tile.py:124-145](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/language/typing/tile.py#L124-L145) —— `Tile` 表示统一缓冲区中的一块数据；注解模式 `x: pl.Tile[[64,64], pl.FP32]`，运行时模式 `tile = pl.load(...)` 返回包装 Call 表达式的 Tile。docstring 里的示例就是标准三段式。

`pl` 命名空间的聚合地（本讲第二个指定精读文件）：

[python/pypto/language/\_\_init\_\_.py:10-38](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/language/__init__.py#L10-L38) —— 模块 docstring 给出职能清单：函数装饰器、Tensor/Tile 类型、类型安全算子（tensor.* / tile.* / system.* 与统一算子）、DSL 辅助（range、yield_）、DataType 常量；并直接给出三段式的标准示例。

[python/pypto/language/\_\_init\_\_.py:77-97](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/language/__init__.py#L77-L97) —— 导入四个算子命名空间 `tensor` / `tile` / `system` / `array`（所以可以写 `pl.tile.load`、`pl.tensor.create`），外加跨核流水的一组 system 算子。

[python/pypto/language/\_\_init\_\_.py:99-157](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/language/__init__.py#L99-L157) —— 第 99-103 行的注释是理解统一分发的钥匙：**凡是 Tile 层也定义了的名字，一律从 `unified_ops` 导入**，让 Tensor/Tile 的分发优先；只有 Tile 层没有对应物（或签名无法统一）的名字才从 `tensor_ops`/`tile_ops` 直出。第 104-119 行从 `tensor_ops` 导入 Tensor 专属算子（`full`、`gather` 等），第 120-154 行从 `tile_ops` 导入 `load`、`store` 等 Tile 专属算子。

[python/pypto/language/\_\_init\_\_.py:158-240](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/language/__init__.py#L158-L240) —— 从 `unified_ops` 导入 `add`、`mul`、`matmul`、`exp` 等一大批统一算子（hello world 的 `pl.add` 在 [第 160 行](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/language/__init__.py#L160)）。

[python/pypto/language/\_\_init\_\_.py:245-262](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/language/__init__.py#L245-L262) —— 从 `typing` 导入 `Tensor`、`Tile`、`Scalar`、`Out`、`InOut` 等类型名。

[python/pypto/language/\_\_init\_\_.py:274-296](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/language/__init__.py#L274-L296) —— `FP16`/`FP32`/`BF16` 等 DataType 常量的便捷重导出（`pl.FP32` 即 `DataType.FP32`）。

#### 4.4.4 代码实践：在源码里「点名」

1. **实践目标**：把 hello world 里出现的每个 `pl.*` 名字追溯到它的定义文件。
2. **操作步骤**：对 `jit`、`Tensor`、`Out`、`at`、`Level`、`load`、`store`、`add`、`CORE_GROUP`（Level 成员）逐个在 `python/pypto/language/__init__.py` 中找到导入行，抄一张表（名字 → 来自哪个模块 → 注解/算子/枚举哪一类）。
3. **需要观察的现象**：`load`/`store` 来自 `op.tile_ops`，`add` 来自 `op.unified_ops`，`Tensor`/`Out` 来自 `typing`，`Level` 来自 C++ 绑定 `pypto_core.ir`。
4. **预期结果**：得到一张 9 行的对照表，并能说出「统一算子 vs 专属算子」的导入策略（[language/\_\_init\_\_.py:99-103](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/language/__init__.py#L99-L103) 的注释）。

#### 4.4.5 小练习与答案

**练习 1**：`pl.Tensor`（裸写）和 `pl.Tensor[[128, 128], pl.FP32]`（带下标注解）作为参数注解，对 JIT 行为有什么不同影响？

**答案**：裸 `pl.Tensor` 不携带 shape/dtype 信息，特化完全依赖调用时的实参；带下标的注解把静态 shape/dtype 写进签名，`compile()` 甚至可以不传任何张量、直接按注解特化（见 [decorator.py:2197-2214](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/jit/decorator.py#L2197-L2214) 的说明）。静态注解让签名即契约，裸注解让调用即契约。

**练习 2**：为什么 `pl.load` 的返回值类型是 Tile 而不是 Tensor？

**答案**：load 的语义就是「全局内存 → 片上」的搬运，产物天然是片上数据块（Tile），后续算子直接在片上执行才有性能意义；若返回 Tensor 则意味着数据又落回全局内存，搬运白做。[tile.py:124-135](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/language/typing/tile.py#L124-L135) 的 docstring 示例正好演示了 load 返回 Tile、被 Tile 算子消费、再被 store 送回的闭环。

### 4.5 调用终点：`RunConfig` 与设备执行

#### 4.5.1 概念说明

`main` 里那句 `tile_add(a, b, c, config=RunConfig())` 是整个程序的驱动。`RunConfig` 是一次「编译 + 执行」的配置包：目标平台、设备号、容差、优化策略、诊断开关……默认值 `platform="a2a3sim"` 表示在 910B 模拟器上执行。真正跑通一个 PyPTO 程序 = DSL 内核（前 4 个模块）+ 这份运行配置。

[examples/beginner/01_hello_world.py:38-47](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/examples/beginner/01_hello_world.py#L38-L47) —— `main` 的验证套路：`torch.full` 造输入、`torch.zeros` 造输出、调用内核、`torch.allclose` 对照 torch 参考实现。这也是全项目示例的通用验证范式。

#### 4.5.2 核心流程

```text
tile_add(a, b, c, config=RunConfig())
   │ JITFunction.__call__ 消费 config
   ├─ 编译侧字段（strategy / dump_passes / memory_planner ...）→ ir.compile()
   ├─ 运行侧字段（platform / device_id / DFX ...）→ 设备执行
   ▼
CompiledProgram(*ordered_args, config=run_config)
   → 编排参数打包 → 二进制装配 → 上设备 → c 原地写回
```

#### 4.5.3 源码精读

[python/pypto/runtime/runner.py:330-338](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/runtime/runner.py#L330-L338) —— `RunConfig` 的默认字段：`platform="a2a3sim"`、`rtol=atol=1e-5`、`strategy=OptimizationStrategy.Default`、`backend_type=Ascend910B`。示例里的 `RunConfig()` 全用默认值。

[python/pypto/runtime/runner.py:366-384](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/runtime/runner.py#L366-L384) —— `__post_init__` 的平台校验：合法值仅 `a2a3sim`/`a2a3`（910B 及其模拟器）与 `a5sim`/`a5`（950）；且平台是公开的唯一事实来源，`a5*` 自动同步 `backend_type=Ascend950`——平台（执行）与后端（代码生成）永远指向同一架构。

[python/pypto/runtime/runner.py:628-682](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/runtime/runner.py#L628-L682) —— `run()` 是 `@pl.program` 路径的用户入口：`ir.compile(...)` 编译后 `compiled(*tensors, config=config)` 执行，返回可重复调用的 `CompiledProgram`。`@pl.jit` 路径殊途同归：`JITFunction.__call__` 最终也是拿到 `CompiledProgram` 再调用（[decorator.py:2166-2169](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/jit/decorator.py#L2166-L2169)）。

#### 4.5.4 代码实践：换一个平台跑跑看

1. **实践目标**：体会 `RunConfig` 对编译目标的控制。
2. **操作步骤**：把示例中的 `RunConfig()` 改成 `RunConfig(platform="a5sim")`（临时副本中改），再运行。
3. **需要观察的现象**：编译产物面向 Ascend 950 架构（backend 自动切到 `Ascend950`）；若本机没有 a5 模拟器工具链，会在执行阶段报环境错误——这本身就是一个有效观察。
4. **预期结果**：理解「平台决定后端」的绑定关系；具体能否执行成功取决于本机工具链（待本地验证）。

#### 4.5.5 小练习与答案

**练习 1**：`config=RunConfig()` 这个关键字参数会传进 `tile_add` 函数体吗？

**答案**：不会。`JITFunction.__call__` 在 JIT 层消费 `config`（[decorator.py:2145-2151](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/jit/decorator.py#L2145-L2151) 的 docstring 写明），编译侧字段转发 `ir.compile()`、运行侧字段驱动设备执行，剩余参数才按签名绑定给内核。

**练习 2**：默认 `RunConfig()` 的 `save_kernels=False` 意味着什么？

**答案**：生成的产物放在临时目录并在执行后清理（[runner.py:181-182](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/runtime/runner.py#L181-L182) 的字段说明）。想保留 `.pto` 产物做分析时设 `save_kernels=True`，产物会落到 `build_output/<程序名>_<时间戳>`（u6-l6 会专门读产物）。

## 5. 综合实践

**任务**：把 hello world 的 Tile 尺寸从 128×128 改成 64×64，仍然算完 128×128 的张量加法，并用 `torch.allclose` 验证。

**背景**：128×128 的张量按 64×64 的 Tile 分块需要 \( \lceil 128/64 \rceil \times \lceil 128/64 \rceil = 4 \) 块——一个 2×2 的双重循环。官方在 `02_elementwise.py` 的 `chunked_add` 里示范了单维循环的写法（[examples/beginner/02_elementwise.py:81-95](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/examples/beginner/02_elementwise.py#L81-L95)），本实践是它的二维推广。

**操作步骤**（在仓库外的临时脚本中完成，不修改仓库文件）：

1. 以 `01_hello_world.py` 为底稿，复制出 `hello_blocked.py`。
2. 在文件顶部定义常量：`ROWS = COLS = 128`，`TILE = 64`。
3. 把内核改写成双重分块循环（示例代码，非仓库原有）：

```python
# 示例代码：基于 01_hello_world.py 修改的 64x64 分块版本
@pl.jit
def tile_add_blocked(a: pl.Tensor, b: pl.Tensor, c: pl.Out[pl.Tensor]):
    with pl.at(level=pl.Level.CORE_GROUP):
        for i in pl.range(ROWS // TILE):        # 2 个行块
            for j in pl.range(COLS // TILE):    # 2 个列块
                tile_a = pl.load(a, [i * TILE, j * TILE], [TILE, TILE])
                tile_b = pl.load(b, [i * TILE, j * TILE], [TILE, TILE])
                tile_c = pl.add(tile_a, tile_b)
                pl.store(tile_c, [i * TILE, j * TILE], c)
    return c
```

4. `main` 部分保持 128×128 的输入输出不变，调用 `tile_add_blocked(a, b, c, config=RunConfig())` 后执行断言：

```python
expected = a + b
assert torch.allclose(c, expected, rtol=1e-5, atol=1e-5), (
    f"blocked tile_add failed: max diff = {(c - expected).abs().max().item()}"
)
```

**需要观察的现象**：

- 四次迭代分别处理 `[0,0]`、`[0,64]`、`[64,0]`、`[64,64]` 四个偏移——`shapes` 恒为 `[64, 64]`，只有 offsets 在动；
- `c` 的四个象限分别被各自的 `pl.store` 写入，最终拼成完整结果；
- 运行输出 `OK`（依赖 u1-l2 搭建的模拟器环境，待本地验证）。

**预期结果**：断言通过，说明分块循环正确覆盖了整张张量。完成本实践后，你就掌握了 Tile 编程最核心的「数据大于 Tile 时怎么办」问题的标准解法——它也是后续所有真实算子（matmul 分块、FlashAttention 分块）的基础骨架。

**选做延伸**：把 `TILE` 改成 32（循环变 4×4=16 块）再跑一次，思考块数变多对 load/store 搬运次数的影响——这就是「分块形状」作为性能变量的第一印象（u7-l3 深入）。

## 6. 本讲小结

- `@pl.jit` 把函数变成 `JITFunction`：首次调用按实参的 shape/dtype **特化**、解析成 IR、**编译**、以「源码哈希 + shape/dtype + 平台 + 策略」为键**缓存**；同键调用直接复用产物，结果通过 `pl.Out` 参数原地写回（调用返回 `None`）。
- `with pl.at(level=pl.Level.CORE_GROUP)` 是片上计算的划界语法：解析器把它收集成 `ScopeStmt(InCore)` 作用域，后续 Pass 把它外提成独立设备内核；`Level` 枚举（AIV/AIC/CORE_GROUP/CHIP/HOST/...）描述机器模型的执行层级。
- Tile 编程三段式 `pl.load → 片上算子 → pl.store`：load 从全局内存 Tensor 搬固定尺寸块进片上返回 Tile；store 把 Tile 写回 Tensor 并返回该 Tensor；`pl.add` 等统一算子按操作数类型在 Tensor 级与 Tile 级之间分发。
- Tensor（全局内存、任意大小、算法视角）与 Tile（片上缓冲、固定尺寸、硬件视角）是两类数据；两类同名类均有「注解」与「运行时包装」双重身份，`pl.*` 命名空间由 `language/__init__.py` 按统一分发优先的策略聚合。
- 分块循环口诀：**shapes 是 Tile 尺寸保持不变，offsets 在源张量坐标系中移动**；块数 \( \lceil M/T_r \rceil \cdot \lceil N/T_c \rceil \)。
- `RunConfig` 驱动一次编译与执行：默认平台 `a2a3sim`，平台与代码生成后端自动绑定；`config=` 关键字被 JIT 层消费，不进内核函数体。

## 7. 下一步学习建议

下一讲（u1-l5「动手写第一个自定义算子」）将把本讲的三段式骨架用到真实算子上：参照 `04_activation.py` 实现一个 GELU 近似算子，练习「load → 多个片上算子组合 → store」的算子链写法，并继续用 torch 对照验证。

继续深挖源码的三个方向：

1. **特化与缓存**（u2-l1）：读 `python/pypto/jit/specializer.py`，看 `TensorMeta`、动态维度（`pl.dynamic`）如何让不同具体长度共享缓存条目。
2. **解析过程**（u3-l1）：读 `python/pypto/language/parser/ast_parser.py`，看 `with pl.at(...)` 这个 AST 模式如何被识别成作用域语句——本讲 4.2 的「解析器识别模式」在那里有代码实证。
3. **作用域外提**（u5-l4）：读 `src/ir/transforms/outline_incore_scopes_pass.cpp`，看 InCore 作用域如何变成独立内核函数，理解 `pl.at` 的编译期归宿。
