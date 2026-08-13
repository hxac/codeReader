# 归约与广播算子

## 1. 本讲目标

本讲承接 u2-l3「运算符库:数学与张量算子」,把目光从**逐元素算子**(Add/Sqrt/Cast…)转到会**改变张量形状**的两类算子:归约(ReduceSum)与广播(Broadcast)。学完后你应该能够:

- 读懂 `ReduceSum<Pattern>` 与 `Broadcast<Pattern>` 的表达式声明,知道它们在 `Compute()` 里怎么写。
- 说清楚 `Pattern::AR / RA / AB / BA` 各自的维度变换语义,并能为一次归约选出正确配对的广播。
- 理解 transcendental 求值器如何把这两个表达式翻译成 Ascend C 的 `ReduceSum` / `Broadcast` API。
- 看懂 rms_norm 样例里 `ReduceSum → Broadcast` 的级联写法。

## 2. 前置知识

先回顾几条 u2-l1 / u2-l3 已建立、本讲会反复用到的认知:

- ATVOSS 的每个 Op 节点都对应一个 `Evaluator<OpAssign<dst, OpXxx<...>>>` 特化,把表达式翻译成一条 Ascend C 指令,运行时零开销。
- `UnaryOp<T>` 是单输入算子的基类,只吃一个子表达式。
- `Compute()` 里的每个 `=` / 数学函数都在构造**编译期表达式 AST**,不是运行时赋值。
- `TileShape = Shape<HEIGHT, WIDTH>` 决定单核一次 Tile 处理的二维数据块;约定 **axis0 对应行(HEIGHT),axis1 对应列(WIDTH)**。

本讲新增的两类算子与逐元素算子最大的区别是:**会改变张量形状**,因此需要额外的 `Pattern` 参数指明"沿哪个方向变换"。

先用一个直觉建立印象。RMSNorm 的核心是把"每一行各自的均方根"算出来,再除回原张量:

\[
\text{Rms}(\mathbf{x})_{ij} = \frac{x_{ij}}{\sqrt{\dfrac{1}{n}\sum_{k=1}^{n} x_{ik}^{\,2}}}\, g_{ij}
\]

这里 \(\sum_k\) 就是"沿列方向归约,每行压成一个值",随后再用"广播"把这个值复制回整行。归约与广播正是干这两件事的表达式。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [include/utils/patterns.h](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/utils/patterns.h) | 定义 `Pattern` 枚举(AR/RA/AB/BA),以及 CastMode/MemMngPolicy/MemLevel |
| [include/operators/transcendental_expression.h](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/operators/transcendental_expression.h) | 声明 `OpReduceSum`/`OpBroadcast` 节点与 `ReduceSum<>`/`Broadcast<>` 工厂函数 |
| [include/operators/transcendental_evaluator.h](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/operators/transcendental_evaluator.h) | 把归约/广播表达式翻译成 Ascend C `ReduceSum`/`Broadcast` API 的求值器特化 |
| [include/operators/tile_shape.h](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/operators/tile_shape.h) | `GetShape<Operation::Binary>` 取出当前 Tile 的 `{axis0, axis1}` 二维形状 |
| [examples/rms_norm/rms_norm.cpp](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/examples/rms_norm/rms_norm.cpp) | `ReduceSum → Broadcast` 级联的真实样例 |
| [tests/st/test_tile_rms_norm_3.cpp](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/tests/st/test_tile_rms_norm_3.cpp) | 同一逻辑的"线性化"写法,便于对照 |

术语提示:文件名里的 **transcendental(超越)** 在此处只是 ATVOSS 对"归约/广播这类改变形状的算子"的归类名,沿用了 Ascend C 的命名习惯,与数学里的超越函数概念无关。

## 4. 核心概念与源码讲解

### 4.1 ReduceSum / Broadcast 表达式声明

#### 4.1.1 概念说明

归约(ReduceSum)与广播(Broadcast)都是**单输入算子**(`UnaryOp`):吃一个子表达式,产出一个结果。它们与 Add/Sqrt 这些逐元素算子的关键差异在于——输出形状 ≠ 输入形状:

- `ReduceSum` 把一个二维块沿某个轴"压扁",元素数变少。
- `Broadcast` 把一个二维块沿某个轴"拉宽",元素数变多。

因此它们的模板第一个参数都是 `Pattern`,用来指明变换方向。注意:`Pattern` 是一个**编译期模板参数**,写死在类型里,这样求值器可以用 `if constexpr` 在编译期分派到不同的 Ascend C 指令,运行时零开销——和 u2-l3 讲的"类型即结构"一脉相承。

#### 4.1.2 核心流程

1. 用户在 `Compute()` 里写 `auto _1 = ReduceSum<Pattern::AR>(in1 * in1);`。
2. 工厂函数把子表达式 `in1 * in1` 包进 `OpReduceSum<Pattern::AR, ...>` 节点,再套上 `Expression` 外壳。
3. 该节点和其他算子节点一样进入编译期 AST,后续被图优化管线(线性化、DAG)处理。
4. 到 Tile 层执行时,匹配到求值器特化,翻译成 Ascend C 的 `ReduceSum` 指令。

#### 4.1.3 源码精读

节点结构 `OpReduceSum`,继承 `UnaryOp`(只有一个子表达式):

[include/operators/transcendental_expression.h:17-23](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/operators/transcendental_expression.h#L17-L23) — 定义 `OpReduceSum<pattern, T>`,`pattern` 作为模板参数被编码进类型。

工厂函数有两个重载,分别接受 `Expression<T>`(左值)与通用引用 `T&&`(右值/转发),都构造 `Expression<OpReduceSum<pattern, T>>`:

[include/operators/transcendental_expression.h:25-35](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/operators/transcendental_expression.h#L25-L35) — `ReduceSum<Pattern>(...)` 工厂,标注了 `__host_aicore__ constexpr`,说明它在 Host 与 Device 两端、且在编译期都可求值。

`Broadcast` 与 `ReduceSum` 结构完全对称:

[include/operators/transcendental_expression.h:37-43](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/operators/transcendental_expression.h#L37-L43) — `OpBroadcast<pattern, T>`,同样继承 `UnaryOp`。

[include/operators/transcendental_expression.h:45-55](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/operators/transcendental_expression.h#L45-L55) — `Broadcast<Pattern>(...)` 工厂。

一句话总结:ReduceSum 和 Broadcast 在"声明层"非常朴素——只是带一个 `Pattern` 模板参数的 `UnaryOp`,真正的维度变换逻辑全部留到求值器里。

#### 4.1.4 代码实践

**目标**:在 rms_norm 样例里定位归约与广播的声明,并推断它们的表达式类型嵌套。

**步骤**:

1. 打开 `examples/rms_norm/rms_norm.cpp`,找到 `RmsNormCompute::Compute()`。
2. 阅读第 42–44 行三条语句:`_1 = ReduceSum<AR>(in1*in1)`、`_2 = Broadcast<AB>(_1)`、`_3 = in1 / Sqrt(Divs<WIDTH>(_2))`。
3. 在纸上写出 `_1` 的外层类型骨架:`Expression<OpReduceSum<Pattern::AR, Expression<OpMul<...>>>>`(其中 `in1*in1` 展开成 `OpMul` 嵌套两个 `Param`)。
4. 对照工厂函数,确认 `{{lhs.data}}` 这种构造就是把子表达式的 `data` 成员搬进 `UnaryOp`。

**需要观察的现象**:`ReduceSum`/`Broadcast` 返回的是一个 `Expression` 包装的"空对象",它本身不持有任何数据,只把计算结构编码进了类型。

**预期结果**:`_1`、`_2` 都是编译期的表达式节点对象,`sizeof` 几乎为 0(空基类优化),不产生运行时数据搬运。类型推断可静态完成;实际运行 rms_norm 样例的结果为「待本地验证」(需 Ascend 环境)。

#### 4.1.5 小练习与答案

**练习 1**:`ReduceSum<Pattern::AR>(in1 * in1)` 里,`Pattern::AR` 是运行时参数还是编译期参数?为什么这样设计?

**答案**:编译期模板参数。这样求值器能用 `if constexpr` 在编译期分派到对应的 Ascend C Pattern,避免运行时分支,实现零开销。

**练习 2**:为什么 `ReduceSum` 和 `Broadcast` 都只继承 `UnaryOp`,而不是 `BinaryOp`?

**答案**:它们都只有一个输入表达式(被归约/被广播的张量),`Pattern` 是"配置"而非"第二个操作数",所以是单输入算子。

---

### 4.2 Pattern 枚举:AR / RA / AB / BA 的语义

#### 4.2.1 概念说明

`Pattern` 枚举定义在 `patterns.h`,共四个值,分成两组:

[include/utils/patterns.h:14-20](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/utils/patterns.h#L14-L20) — AR/RA 用于归约,AB/BA 用于广播。

理解这四个值的关键,是先固定"二维块的坐标约定":求值器取的是 `Operation::Binary` 形状,即 `OperationShape{axis0, axis1}`,其中 axis0 = TileShape 的第一个维度(行,HEIGHT),axis1 = 第二个维度(列,WIDTH)。所有 AR/RA/AB/BA 都是对这个 `[axis0, axis1]` 二维块操作。

记忆口诀(可由求值器代码直接推出):

- **字母顺序对应轴顺序**:第一个字母讲 axis0,第二个字母讲 axis1。
- **'A'** = 这一维**保持**目标大小;归约里 **'R'** = 这一维被**压成 1**;广播里 **'B'** = 这一维**从 1 扩到**目标大小。

于是得到下表:

| Pattern | 类别 | 作用 | 维度变换 | 与谁互逆 |
|---|---|---|---|---|
| AR | 归约 | 沿 axis1(列)求和,每行压成 1 个值 | `[axis0, axis1] → [axis0, 1]` | AB |
| RA | 归约 | 沿 axis0(行)求和,每列压成 1 个值 | `[axis0, axis1] → [1, axis1]` | BA |
| AB | 广播 | axis1 从 1 扩到 axis1(横向复制) | `[axis0, 1] → [axis0, axis1]` | AR |
| BA | 广播 | axis0 从 1 扩到 axis0(纵向复制) | `[1, axis1] → [axis0, axis1]` | RA |

**AR 与 AB 互逆**(都围绕 `[axis0, 1]` 这个"每行一个值"的形状);**RA 与 BA 互逆**(都围绕 `[1, axis1]` 这个"每列一个值"的形状)。这正是 rms_norm 里成对出现的是 `ReduceSum<AR>` + `Broadcast<AB>` 的原因。

#### 4.2.2 核心流程

给定一个 TileShape = `Shape<H, W>`(即 axis0=H,axis1=W):

1. `ReduceSum<AR>`:对每一行的 W 个元素求和 → 输出 `[H, 1]`(H 个和)。
2. `ReduceSum<RA>`:对每一列的 H 个元素求和 → 输出 `[1, W]`(W 个和)。
3. `Broadcast<AB>`:输入 `[H, 1]`,把每行的 1 个值沿列复制 W 份 → `[H, W]`。
4. `Broadcast<BA>`:输入 `[1, W]`,把那一行的 W 个值沿行复制 H 份 → `[H, W]`。

#### 4.2.3 源码精读

`Pattern` 枚举本体:

[include/utils/patterns.h:14-20](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/utils/patterns.h#L14-L20)

维度变换的"实证"在求值器里——它根据 `Pattern` 构造不同的 `srcShape`/`dstShape` 数组。先看广播分支(归约分支下一节详讲):

[include/operators/transcendental_evaluator.h:24-37](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/operators/transcendental_evaluator.h#L24-L37) — `BroadcastAssign`:`AB` 分支令 `srcShape_ = {axis0, 1}`(每行一个值),调用 `Broadcast<T, 2, 1>`(在第 1 轴广播);`BA` 分支令 `srcShape_ = {1, axis1}`(每列一个值),调用 `Broadcast<T, 2, 0>`(在第 0 轴广播);`dstShape` 恒为 `{axis0, axis1}`。

这段代码正好印证了上表:`AB` 的源形状是 `[axis0, 1]`,`BA` 的源形状是 `[1, axis1]`。

rms_norm 样例里的真实用法(AR 归约每行平方和,AB 再广播回原形状):

[examples/rms_norm/rms_norm.cpp:42-44](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/examples/rms_norm/rms_norm.cpp#L42-L44) — `_1 = ReduceSum<AR>(in1*in1)`(每行平方和,形状 `[H,1]`)、`_2 = Broadcast<AB>(_1)`(广播回 `[H,W]`)。

#### 4.2.4 代码实践

**目标**:完成规格里指定的练习——用 `ReduceSum<Pattern::AR>(in1*in1)` 表达"对 in1 平方后整行求和",并回答"若要改为按列广播回原形状该用哪个 Pattern""AR 与 RA 的区别"。

**步骤**:

1. 假设 in1 的 TileShape = `Shape<4, 8>`(axis0=4 行,axis1=8 列)。
2. 写出 `auto sq = in1 * in1; auto rowSum = Atvoss::ReduceSum<Atvoss::Pattern::AR>(sq);`。
3. 推断 rowSum 的形状:沿 axis1(列)归约 → `[4, 1]`。
4. 想把 rowSum "按列广播回原形状"——即从 `[4,1]` 还原成 `[4,8]`。查表:把 axis1 从 1 扩到 8,应选 `Broadcast<Atvoss::Pattern::AB>`。

**需要观察的现象**:`ReduceSum<AR>` 之后张量"瘦"成 `[4,1]`;只有配合 `Broadcast<AB>` 才能恢复 `[4,8]`。若误用 `Broadcast<BA>`,它期望的源形状是 `[1,8]` 而非 `[4,1]`,维度对不上。

**预期结果**:"整行求和"用 AR;"按列广播回原形状"用 AB(因为源是每行一个值 `[axis0,1]`,要沿列扩)。

**AR 与 RA 的区别**:

- **AR**:沿 axis1(列方向)归约,每行压成一个值,结果 `[axis0, 1]`——即"整行求和"。
- **RA**:沿 axis0(行方向)归约,每列压成一个值,结果 `[1, axis1]`——即"整列求和"。

两者方向不同、产出形状不同,后续配对的广播也不同(AR 配 AB,RA 配 BA)。

#### 4.2.5 小练习与答案

**练习 1**:给定 TileShape = `Shape<2, 6>`,写出 `ReduceSum<RA>` 的输出形状,以及能把它还原成 `[2,6]` 的 Broadcast Pattern。

**答案**:`ReduceSum<RA>` 沿 axis0 归约 → `[1, 6]`;还原需把 axis0 从 1 扩到 2,用 `Broadcast<BA>`。

**练习 2**:为什么说 AR 与 AB"互逆"?用一个具体数值例子说明。

**答案**:对 `[3,4]` 做 AR 得 `[3,1]`(3 个行和);再对 `[3,1]` 做 AB 得 `[3,4]`,每行填满同一个行和。形状上 A→R 再 A→B 回到原状,所以互逆。

**练习 3**:如果把 rms_norm 里的 `Broadcast<AB>` 误写成 `Broadcast<BA>`,会在哪一步出错?

**答案**:`Broadcast<BA>` 期望源形状 `[1, axis1]`,而 `ReduceSum<AR>` 给的是 `[axis0, 1]`,两者不匹配——求值器构造的 `srcShape` 与实际缓冲布局不一致,结果错误(具体报错形态「待本地验证」)。

---

### 4.3 求值器:从表达式到 Ascend C API

#### 4.3.1 概念说明

声明层的 `ReduceSum`/`Broadcast` 只是"占位节点",真正干活的是 Tile 层的求值器特化。和 u2-l3 的数学算子一样,这里也是"表达式声明 ↔ 求值器特化"一一配对:`Evaluator<OpAssign<dst, OpReduceSum<...>>>` 与 `Evaluator<OpAssign<dst, OpBroadcast<...>>>` 两个特化,分别落到 Ascend C 的 `ReduceSum` 与 `Broadcast` API。

这里有一个有意思的**不对称**:归约侧直接复用 Ascend C 自己的 Pattern 枚举(`AscendC::Pattern::Reduce::AR/RA`);广播侧则把 Atvoss 的 AB/BA **拆成"广播轴下标 + srcShape 数组"两个参数**传给 `AscendC::Broadcast`。也就是说,Atvoss 的 `Pattern` 是面向用户的统一抽象,到底层映射时采用了两种不同的编码方式。

#### 4.3.2 核心流程

以 `out = Broadcast<AB>(_1)` 为例,Tile 层执行时:

1. `Evaluator<OpAssign<out, OpBroadcast<AB, _1>>>` 被匹配。
2. 用 `Dtype_t<out>` 取出输出张量的元素类型 Dtype。
3. 调 `GetShape<Operation::Binary>(context.argsTensors)` 取当前 Tile 的 `{axis0, axis1}`。
4. 递归求值左右子树拿到 dst/src 的 UB LocalTensor:`Evaluator<out>{}(...).GetUbTensor()` 与 `Evaluator<_1>{}(...).GetUbTensor()`。
5. 调 `BroadcastAssign<OperationShape, AB, Dtype>(dstTensor, srcTensor, operationShape)`,内部按 AB 选 `Broadcast<T,2,1>` 与 `srcShape={axis0,1}`。

`ReduceSum` 流程同构,只是调 `ReduceSumAssign` → `AscendC::ReduceSum`。

#### 4.3.3 源码精读

归约侧的 Ascend C 映射——Atvoss Pattern::AR/RA 一一对应到 AscendC Pattern:

[include/operators/transcendental_evaluator.h:44-55](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/operators/transcendental_evaluator.h#L44-L55) — `ReduceSumAssign`:`AR` → `AscendC::ReduceSum<T, AscendC::Pattern::Reduce::AR, true>(dst, src, shape, true)`,`RA` → 对应 RA。`shape = {axis0, axis1}` 是源张量的二维形状(末尾两个 `true` 是 CANN SDK 层面的开关,具体语义见 SDK 文档)。

广播侧,AB/BA 被拆成"广播轴 + srcShape":

[include/operators/transcendental_evaluator.h:24-37](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/operators/transcendental_evaluator.h#L24-L37) — `BroadcastAssign`:AB 用 `Broadcast<T,2,1>` + `srcShape={axis0,1}`;BA 用 `Broadcast<T,2,0>` + `srcShape={1,axis1}`;dstShape 恒为 `{axis0,axis1}`。

驱动这两个 Assign 函数的求值器特化(Broadcast 版):

[include/operators/transcendental_evaluator.h:64-77](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/operators/transcendental_evaluator.h#L64-L77) — `Evaluator<OpAssign<T, OpBroadcast<pattern, U>>>`:`operationShape` 来自 `GetShape<Operation::Binary>`,dst/src 张量通过对左右子树递归求值并 `.GetUbTensor()` 取得,最后交给 `BroadcastAssign`。

ReduceSum 版的特化结构相同:

[include/operators/transcendental_evaluator.h:86-99](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/operators/transcendental_evaluator.h#L86-L99) — `Evaluator<OpAssign<T, OpReduceSum<pattern, U>>>`,交给 `ReduceSumAssign`。

补充:取形状的 `GetShape` 与 `Operation::Binary` 的含义——返回 `OperationShape{axis0, axis1}`(即 TileShape 的两个维度):

[include/operators/tile_shape.h:105-125](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/operators/tile_shape.h#L105-L125)

正因为求值器只认二维 `{axis0, axis1}`,目前归约/广播面向的是二维 Tile(与 rms_norm README 中"当前只支持二维输入"一致)。

**延伸(预告 u3-l6)**:含 `ReduceSum` 的表达式在图构建阶段会被特殊对待——reduce 图模块用一组 trait(`ContainsReduceOp` / `IsReduceOp`)挑出归约算子,在其边界插入额外的 `CopyIn`/`Copy`,并把整张图划分成 PreReduce / Reduced 两段。本讲只看"单条 reduce/broadcast 指令如何落到 Ascend C",细节留到 u3-l6。

[include/reduce/graph/helper.h:56-69](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/reduce/graph/helper.h#L56-L69) — 归约算子识别 trait(预告)。

#### 4.3.4 代码实践

**目标**:追踪 `out = Broadcast<AB>(_1)` 这一条表达式在 Tile 层的求值调用链,并对比 `rms_norm.cpp` 的"嵌套写法"与 `test_tile_rms_norm_3.cpp` 的"线性化写法"。

**步骤**:

1. 在 `transcendental_evaluator.h` 第 64–77 行的 Broadcast 求值器特化里,依次列出它做了哪几件事(取 Dtype、取 operationShape、递归求 dst/src、调 `BroadcastAssign`)。
2. 跟进 `BroadcastAssign`(第 24–37 行),确认 AB 分支调用的是 `AscendC::Broadcast<T, 2, 1>`,且 `srcShape = {axis0,1}`。
3. 打开 `tests/st/test_tile_rms_norm_3.cpp` 第 48–51 行,看它的 `Compute` 把 rms_norm 拆成了一条条逗号分隔的 `OpAssign`(`temp = in1*in1, out = ReduceSum<AR>(temp), out = Broadcast<AB>(out), ...`);而 `examples/rms_norm/rms_norm.cpp` 用的是 `_1 = ...; _2 = ...; _3 = ...` 的嵌套 LocalVar 写法。

**需要观察的现象**:两种写法表达的是同一份计算——线性化写法更接近图优化后真正的执行序列(每条 `OpAssign` 对应一次赋值),嵌套写法更接近数学公式。

**预期结果**:两种写法最终都会被线性化 Pass 展开成相同(或等价)的 `OpAssign` 序列,再逐条匹配求值器特化。能否在真机/仿真上跑通为「待本地验证」(需 Ascend 950 + CANN 8.5+ 环境);可用 `bash scripts/build.sh -DSOC=ascend950 rms_norm` 编译。

#### 4.3.5 小练习与答案

**练习 1**:求值器里为什么用 `GetShape<Operation::Binary>` 而不是 `Operation::Unary`?

**答案**:Binary 取的是 `{axis0, axis1}` 两个维度,正是归约/广播需要的二维块形状;Unary 只返回一个总长度 `OperationShape{TotalCnt}`,丢了行列信息,无法表达"沿哪个轴变换"。

**练习 2**:Atvoss 的 `Pattern::AB` 到底层 Ascend C 是怎么映射的?和 `AR` 的映射方式有何不同?

**答案**:`AB` 没有直接对应一个 AscendC Pattern 枚举,而是被拆成"广播轴下标 1"(模板参数)和"`srcShape={axis0,1}`"两个参数传给 `AscendC::Broadcast<T,2,1>`;`AR` 则直接复用 `AscendC::Pattern::Reduce::AR` 枚举传给 `AscendC::ReduceSum`。一个是拆解式映射,一个是枚举式映射。

**练习 3**:`Evaluator<OpAssign<T, OpBroadcast<pattern, U>>>` 里的 `Dtype_t<T>` 取的是谁的类型?为什么用它作为 Ascend C 调用的模板参数?

**答案**:取的是赋值目标 T(左侧出参)的元素类型(`T::Type::PrimType`);因为 Ascend C 的 `Broadcast`/`ReduceSum` 需要知道张量元素类型来选择正确的指令实现,而目标张量的类型即本次运算结果应存的类型。

---

## 5. 综合实践

**任务**:仿照 rms_norm,实现一个"每行平方和再广播回去"的最小算子(先不含 Sqrt/Divs,只练归约 + 广播),并在改写中练习 Pattern 的选择。

参考骨架(**示例代码**,非仓库原有文件,需自行套入 Config 骨架并补 `main()`):

```cpp
// 示例代码:仅演示表达式写法
struct RowSqSumCompute {
    template <template <typename> class Tensor>
    __host_aicore__ constexpr auto Compute() const
    {
        auto in1 = Atvoss::PlaceHolder<1, Tensor<float>, Atvoss::ParamUsage::IN>();
        auto out = Atvoss::PlaceHolder<2, Tensor<float>, Atvoss::ParamUsage::OUT>();
        auto sq = in1 * in1;
        auto rowSum = Atvoss::ReduceSum<Atvoss::Pattern::AR>(sq);   // 每行平方和 -> [H,1]
        return out = Atvoss::Broadcast<Atvoss::Pattern::AB>(rowSum); // 广播回 [H,W]
    }
};
```

要求与思考题:

1. 把 TileShape 设成 `Shape<2, 8>`,手算 `ReduceSum<AR>` 后的形状,再确认 `Broadcast<AB>` 能恢复原状。
2. 若把归约换成"每列平方和"(整列求和),`ReduceSum` 该用哪个 Pattern?对应的广播又该用哪个?
3. 在 `examples/rms_norm/rms_norm.cpp` 基础上,把 `_2 = Broadcast<AB>(_1)` 改成 `_2 = Broadcast<BA>(_1)`,先预测维度是否匹配,再说明会发生什么。
4. 用 `bash scripts/build.sh -DSOC=ascend950 rms_norm` 编译 rms_norm,运行 `output/bin/rms_norm --shape=16,32`,观察是否输出 `Accuracy verification passed`(运行结果「待本地验证」)。

## 6. 本讲小结

- `ReduceSum` 与 `Broadcast` 是带一个 `Pattern` 模板参数的 `UnaryOp`,声明在 `transcendental_expression.h`,产出的是编译期表达式节点,运行时零开销。
- `Pattern` 共四个值:AR/RA 用于归约,AB/BA 用于广播;字母顺序对应 axis0、axis1,'A'=保持目标大小,'R'=压成 1,'B'=从 1 扩开。
- AR 与 AB 互逆(围绕 `[axis0,1]`),RA 与 BA 互逆(围绕 `[1,axis1]`);rms_norm 里成对出现的是 `ReduceSum<AR>` + `Broadcast<AB>`。
- 求值器特化 `Evaluator<OpAssign<dst, OpReduceSum/OpBroadcast<...>>>` 把表达式翻译成 Ascend C 的 `ReduceSum`/`Broadcast` API;归约复用 AscendC Pattern 枚举,广播拆成"广播轴 + srcShape"。
- 求值器只认二维 `{axis0, axis1}`,所以归约/广播当前面向二维 Tile,与 rms_norm"只支持二维输入"一致。
- 含 `ReduceSum` 的表达式在图层会被特殊处理(边界插 CopyIn、划分 PreReduce/Reduced),细节见 u3-l6。

## 7. 下一步学习建议

- 想看归约/广播在端到端算子里如何与 Sqrt/Divs 级联:阅读 `examples/rms_norm/rms_norm.cpp`,并对照 `tests/st/test_tile_rms_norm_3.cpp` 的线性化写法。
- 想理解求值器整体的递归求值模型与 `PipeBarrier` 同步:进入 **u3-l1「求值器系统:从表达式到执行」**。
- 想理解含归约的表达式如何被图层特殊处理(InsertCopyIn、缓冲复用判定):进入 **u3-l6「Reduce 模块:归约图分析」**。
- 想看 RMSNorm 的完整级联与二维 TileShape 协同:进入 **u3-l7「rms_norm 样例:表达式级联」**。
