# Reduce 类原语

## 1. 本讲目标

本讲讲解 tilelang-ascend 上「沿某个轴把一块 UB tile 收缩」的三个 reduce 原语：`T.reduce_sum`、`T.reduce_max`、`T.reduce_min`。它们是 softmax、归一化、FlashAttention 里「求最大值」「求和」等步骤的底层支撑。

学完本讲你应该能够：

- 说清 `dim`、`clear`、`real_shape` 三个参数分别控制什么，以及输出 buffer 的 shape 该怎么写。
- 解释 `clear=True`（先清零再 reduce）与 `clear=False`（在已有结果上做增量 merge）的差别，并能判断什么时候用哪种。
- 理解 `real_shape` 为什么是 2D slice buffer（切出来的不完整子块）的必备参数。
- 跟踪一条 reduce 调用从前端 `T.reduce_*` 到 `tl.ascend_reduce` intrinsic，再到 codegen 与 AscendC 指令（`ReduceSum` / `ReduceMax` / `ReduceMin`）的完整链路。
- 动手写并验证一个带 reduce 的算子，包括 `clear=False` 的增量累加。

## 2. 前置知识

在进入本讲前，请确认你已经掌握以下概念（它们都来自前面的讲义）：

- **UB / fragment 两层抽象**：reduce 主要发生在 UB（Unified Buffer，属 Vector 核的片上缓存）这一层；输入输出 buffer 通常都用 `T.alloc_ub` 申请（参见 u3-l1 内存层级）。
- **scope 决定一切**：`T.copy`、`T.reduce_*` 这些原语最终落到哪条硬件指令，完全由 buffer 的 scope 决定，而不是由函数名决定（参见 u3-l2 数据搬运）。
- **TIR intrinsic 与 lowering**：`T.reduce_*` 在解析期会变成一个 `tir.call_intrin(...)` 调用，它本身不可执行，要由后端 codegen 翻译成真正的 Ascend C 调用（参见 u1-l5 JIT 流程）。
- **block / sub-tile 切分**：reduce 经常在一个被 `cid`/`vid` 切出来的子块上做（参见 u2-l2 kernel launch）。

如果你还不熟悉上面任意一点，建议先回头读对应讲义，否则本讲里「为什么输出是 `[M]` 而不是 `[M,1]`」「为什么要传 `real_shape`」这些细节会难以落地。

一点术语约定：reduce 时，被收缩的那个轴叫 **R 轴（Reduce 轴）**，保留的那些轴叫 **A 轴（Normal 轴）**。例如对 `[M, N]` 做 `dim=-1`，则最后一维 N 是 R 轴、M 是 A 轴；做 `dim=0`，则 M 是 R 轴、N 是 A 轴。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 |
| --- | --- |
| `tilelang/language/reduce_ascend.py` | 前端三件套 `reduce_sum/max/min` 的实现，负责参数解析、`dim` 合法化、输出 shape 校验、`real_shape` 处理，并发射 `tl.ascend_reduce` intrinsic。 |
| `src/op/reduce.cc` | 上游（GPU）通用 `tl.reduce` 算子的实现，给出 reduce 的「概念骨架」：初始值、归约操作、`clear` 与 `duplicate` 逻辑。它是理解 reduce 语义的参考实现。 |
| `src/op/ascend.cc` | 注册 Ascend 专用 builtin `tl.ascend_reduce`（以及 `ascend_block_reduce_*`、`ascend_wholereduce*` 等）。 |
| `src/target/codegen_ascend.cc` | Ascend C codegen：把 `tl.ascend_reduce` 翻译成对模板库 `tl::ascend::reduce_*` 的调用，处理 half 快路径与 narrow（窄列）reduce。 |
| `src/tl_templates/ascend/common.h` | 模板库：`reduce_sum/max/min` 最终调用 AscendC 的 `ReduceSum` / `ReduceMax` / `ReduceMin`（以及 `WholeReduce*` 系列）。 |
| `docs/TileLang-Ascend Programming Guide.md` | 编程手册第 4.1.3.2 节，给出 reduce 的官方语义、图示与 `clear=False` 的 merge 规则。 |
| `examples/reduce/*.py` | 可运行的 reduce 示例，是本讲实践的依据。 |

> ⚠️ 一个容易混淆的点：仓库里有两个名字相近的算子。`src/op/reduce.cc` 注册的是上游通用 **`tl.reduce`**（GPU 路线，基于 `fragment` / warp reduce）；Ascend 的 reduce 走的是另一条专用 builtin **`tl.ascend_reduce`**，定义在 `src/op/ascend.cc`。本讲的三个前端函数 `T.reduce_sum/max/min` 发射的是后者。我们读 `src/op/reduce.cc` 是为了理解 reduce 的通用概念骨架，再落到 Ascend 的实现上。

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：

1. reduce 接口三件套：参数总览。
2. `dim` 合法化与输出 shape 约束（含 `real_shape` 对 slice buffer 的作用）。
3. `clear=False` 的增量 merge 语义。
4. 从 `tl.ascend_reduce` 到 AscendC 指令的 codegen 与模板库。

### 4.1 reduce 接口三件套：参数总览

#### 4.1.1 概念说明

`reduce_sum`、`reduce_max`、`reduce_min` 三个函数的签名完全一致，只是语义不同：

```python
T.reduce_sum(buffer, out, dim=-1, *, clear=True, real_shape=None)
T.reduce_max(buffer, out, dim=-1, *, clear=True, real_shape=None)
T.reduce_min(buffer, out, dim=-1, *, clear=True, real_shape=None)
```

五个要素的含义：

- **buffer**：输入 buffer（或 buffer region），通常是 UB 上的一个 `[M, N]` tile。
- **out**：输出 buffer，存放 reduce 结果。
- **dim**：沿哪一轴收缩（R 轴）。负数表示从最后一维往回数，`-1` 是最后一维。
- **clear**：是否在 reduce 前先初始化 `out`。
- **real_shape**：当 buffer 是「物理上更宽、逻辑上只有一部分有效」的 slice buffer 时，用它描述真正的逻辑有效区域。

注意它是一种 **fast-path（快速路径）原语**：前端只声明「从哪块 buffer reduce 到哪块 out、沿哪个轴」，具体发哪条 AscendC reduce 指令、用哪个 `Pattern`、需不需要临时空间，全交给后端决定。当前主要验收范围是 **2D UB / slice buffer 的 reduce**。

#### 4.1.2 核心流程

三个函数在内部的执行流程是一样的（只是 `reduce_type` 字符串不同）：

```
reduce_sum(buffer, out, dim, clear, real_shape)
   │
   ├─ 1. _parse_reduce_optional_args        # 解析 clear / real_shape（支持关键字和 positional）
   ├─ 2. _legalize_reduce_dim               # 把 dim 归一化成 -1 或 0，并校验是否合法
   └─ 3. reduce(...)                        # 真正发射 intrinsic 的公共函数
         ├─ 校验 real_shape 与输出 shape
         ├─ 算出模板参数 {M, N}
         ├─ 组装模板字符串 "reduce_sum<dtype, {M, N}, dim>"
         └─ tir.call_intrin("tl.ascend_reduce", 模板串, out_ptr, buf_ptr, clear)
```

关键点：前端最终发射的是一个 **TIR intrinsic `tl.ascend_reduce`**，它的第一个参数是一个把「类型 + shape + dim」打包好的字符串（例如 `"reduce_max<float, 4, 8, -1>"`），后端 codegen 会把这个字符串解析出来决定怎么发指令。

#### 4.1.3 源码精读

三个函数本质上是同一个壳，以 `reduce_sum` 为例：

[tilelang/language/reduce_ascend.py:414-437](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/language/reduce_ascend.py#L414-L437) —— `reduce_sum`：先解析可选参数，再把 `dim` 合法化，最后调用公共的 `reduce()`。

真正干活的是公共函数 `reduce()`，它负责算出模板参数并发射 intrinsic：

[tilelang/language/reduce_ascend.py:340-359](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/language/reduce_ascend.py#L340-L359) —— 这段代码把 `reduce_type`（如 `"reduce_max"`）、dtype、`{M, N}`、`dim` 拼成一个模板字符串，连同 `out`、`buffer` 的访问指针和 `clear` 标志，一起打包成一次 `tir.call_intrin("tl.ascend_reduce", ...)` 调用。注意第 352-353 行：仅当物理行宽（`physical_row`）与逻辑宽 `N` 不一致、且都是常量时，才会额外附加一个表示「按物理行宽跨步」的整型参数——这就是 narrow reduce 的开关。

`reduce_type` 字符串里的值（`reduce_sum` / `reduce_max` / `reduce_min`）会被 codegen 用来选模板和指令，我们会在 4.4 节看到。

#### 4.1.4 代码实践

**实践目标**：确认三个函数的签名一致、并理解「reduce_type 字符串」是唯一区别。

**操作步骤**：

1. 打开 `tilelang/language/reduce_ascend.py`，对照 `reduce_sum`、`reduce_max`、`reduce_min` 三个函数体。
2. 注意它们都把一个不同的字符串（`"reduce_sum"` / `"reduce_max"` / `"reduce_min"`）传给 `_reduce_with_clear`。

**需要观察的现象**：三个函数除了这个字符串之外，结构完全一样；真正的「求和 / 求最大 / 求最小」差别不在前端，而在后端 codegen 与模板库。

**预期结果**：你能在源码里指出「前端只负责把意图打包成 intrinsic，语义实现全部后置到 codegen」这一设计。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `reduce_sum` 里的字符串 `"reduce_sum"` 故意改成 `"reduce_avg"`（一个后端不认识的类型），会在哪一步报错？

**参考答案**：前端不会报错（前端不做字符串校验，只打包传递），问题会推迟到后端 codegen 或模板实例化阶段，表现为生成代码里出现 `tl::ascend::reduce_avg<...>` 这样的未知符号，最终在 bisheng 编译 `.so` 时报「未定义符号 / 找不到模板」错误。这正是「语义后置」的副作用：前端错误检测较弱，错误暴露在编译期。

---

### 4.2 dim 合法化与输出 shape 约束

#### 4.2.1 概念说明

reduce 不是「随便给个 dim 都行」。Ascend 的 fast-path reduce 对维度有硬约束，原因在于它最终映射到 AscendC 的 `ReduceSum/Max/Min` 这类向量指令，这些指令只支持特定的 reduce 方向（沿行 AR 或沿列 RA）。因此前端必须先把用户写的 `dim` 归一化、并校验合法性。

支持的 buffer 维度与 `dim` 取值：

| buffer 维度 | 支持的 dim（用户写法） | 归一化后 |
| --- | --- | --- |
| 1D `[N]` | `0` 或 `-1` | `-1` |
| 2D `[M, N]` | `0` / `1` / `-1` / `-2` | `0` 或 `-1` |
| 3D trailing-tile | 仅尾部 tile 轴 `0` / `1` / `-1` / `-2` | `0` 或 `-1` |

注意：归一化后只剩 `-1`（沿最后一维，行 reduce）和 `0`（沿第一维，列 reduce）两种，这正是 AscendC `Reduce*` 指令能直接表达的两个方向。其他方向（比如 3D 中间的轴）不被支持，会直接报错。

输出 `out` 的 shape 有两类合法写法（以 `[M, N]` 输入为例）：

- **压缩形式**：`dim=-1` 输出 `[M]`；`dim=0` 输出 `[N]`。
- **keepdim 形式**：保留被收缩的轴但长度变 1，`dim=-1` 输出 `[M, 1]`；`dim=0` 输出 `[1, N]`。

⚠️ 这里有个常见误区：`keepdim` **不是**「输出可以任意保持成和输入一样的 shape」。只有在被收缩轴本身长度就是 1 的退化情况下，输出数值上才可能和输入同 shape。

#### 4.2.2 核心流程

`dim` 合法化由 `_legalize_reduce_dim` 完成，输出 shape 校验由 `_validate_reduce_out_shape` 完成：

```
用户 dim (如 1 或 -2)
   │
   ├─ _legalize_reduce_dim
   │    ├─ 1D: 只认 0/-1            → 归一化为 -1
   │    ├─ 2D: 认 0/1/-1/-2         → 归一化为 0 或 -1
   │    └─ 3D: 只认尾部 tile 轴      → 归一化为 0 或 -1
   │    （非法 dim → raise ValueError，绝不进后端）
   │
   └─ _validate_reduce_out_shape
        ├─ 算出 reduced_shape（压缩形式）与 keepdim_shape
        ├─ out 必须等于其中之一（slice buffer 还有兼容路径，见下）
        └─ 不匹配 → raise ValueError
```

#### 4.2.3 源码精读

`dim` 归一化逻辑（以 2D 为例）：

[tilelang/language/reduce_ascend.py:239-245](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/language/reduce_ascend.py#L239-L245) —— 对 2D buffer，先把负数 dim 转正（`dim if dim >= 0 else 2 + dim`），再判断它是不是 `0` 或 `1`；如果是 `0` 就归一化成 `0`（列 reduce），否则（即 `1`）归一化成 `-1`（行 reduce）。其它取值直接抛错。

输出 shape 校验逻辑：

[tilelang/language/reduce_ascend.py:149-174](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/language/reduce_ascend.py#L149-L174) —— 这段代码先按 `dim` 算出两种合法输出 shape（`reduced_shape` 压缩形式、`keepdim_shape` 保留轴变 1），然后检查用户给的 `out` 是否等于其中任意一种。第 173 行 `any(_shape_list_equal(out_extent, expected_shape) ...)` 就是这个判断；不满足则在第 179 行抛出带详细诊断信息的 `ValueError`。

约束的权威来源是编程手册：

[docs/TileLang-Ascend Programming Guide.md:684-700](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/docs/TileLang-Ascend%20Programming%20Guide.md#L684-L700) —— 手册明确写出：支持 1D/2D/3D trailing-tile；`dim` 各自的合法取值；`clear` 与 `real_shape` 的含义；以及「非法 dim / 非法 real_shape / 非法 out shape 会在前端直接报错，而不是静默进入后端」。

#### 4.2.4 代码实践

**实践目标**：感受「非法 dim / 非法 out shape 在前端立刻报错」。

**操作步骤**（源码阅读型 + 本地可选运行）：

1. 阅读上面的两段源码，预测下列写法分别会报什么错：
   - 对 `[4, 8]` 的 buffer 传 `dim=2`；
   - `dim=-1` 但把 `out` 申请成 `[4, 8]`（而不是 `[4]` 或 `[4, 1]`）。
2. （可选，需要 NPU）写一个最小 kernel 触发上述两种写法，运行看报错信息是否与预测一致。

**需要观察的现象**：错误在 Python 端（`tilelang.lower` / 首次 `func(...)` 触发 JIT 编译时）就抛出 `ValueError`，且信息里包含逻辑 shape、物理 buffer shape、dim、实际输出 shape 与期望 shape。

**预期结果**：报错信息形如 `Invalid reduce output shape for Ascend fast-path reduce: logical input shape is ..., dim is ..., output shape is ..., expected [M] or [M, 1]`。

**待本地验证**：上述报错文案以源码为准；若没有 NPU，可通过阅读 `_validate_reduce_out_shape` 的 `raise ValueError(...)` 段确认文案。

#### 4.2.5 小练习与答案

**练习 1**：输入是 `[M, N]` 的 UB tile，做 `dim=0` 的 reduce，输出 `out` 可以写成哪几种 shape？

**参考答案**：两种合法形式——压缩形式 `[N]`，或 keepdim 形式 `[1, N]`。其余（如 `[N, 1]`、`[M]`）都不合法，会在前端报错。

**练习 2**：为什么 3D buffer 的 reduce 只支持「尾部 tile 轴」？

**参考答案**：因为 fast-path reduce 最终映射到 AscendC 的 `ReduceSum/Max/Min` 向量指令，这些指令只能表达「沿最内两维的方向」（行 AR / 列 RA）。3D 在这里是「若干个 2D tile 叠起来」的 trailing-tile 视图，可 reduce 的只有最后两个轴（对应 `0/1/-1/-2`），中间轴无法被硬件指令直接表达，所以被禁止。

---

### 4.3 clear=False 的增量 merge 语义

#### 4.3.1 概念说明

`clear` 参数是本讲最容易踩坑、也最实用的一个。它有两种语义：

- **`clear=True`（默认）**：先把 `out` 初始化成 reduce 的「单位元」（sum→0，max→负无穷，min→正无穷），再写入 reduce 结果，相当于**覆盖** `out`。
- **`clear=False`**：不初始化 `out`，先把这次 reduce 的结果算出来（记为 `reduced_result`），再把它与 `out` 里**已有的旧值** `old_out` 做一次 merge，得到新输出 `new_out`。

三种 reduce 的 merge 规则不同：

| reduce 类型 | clear=True 的单位元 | clear=False 的 merge |
| --- | --- | --- |
| `reduce_sum` | 0 | \( \text{new\_out} = \text{old\_out} + \text{reduced\_result} \) |
| `reduce_max` | \(-\infty\) | \( \text{new\_out} = \max(\text{old\_out}, \text{reduced\_result}) \) |
| `reduce_min` | \(+\infty\) | \( \text{new\_out} = \min(\text{old\_out}, \text{reduced\_result}) \) |

什么时候需要 `clear=False`？典型场景是**分块累加 reduce**：一个很大的矩阵被切成多块，每块在 UB 上单独 reduce 出一个部分结果，这些部分结果需要不断「并入」同一个累加器 `out`。第一次用 `clear=True` 建立初值，后续每次用 `clear=False` 把新部分和 merge 进来。

> 数学上可以这么理解：把整块 reduce 看成在所有元素上做一次结合律成立的运算（sum 的加法、max、min 都满足结合律）。分块时，每块的 `reduced_result` 是该块内的部分结果，`clear=False` 的 merge 就是在做块间的结合：
>
> \[ \text{reduce}(X_1 \cup X_2) = \text{merge}\big(\text{reduce}(X_1),\ \text{reduce}(X_2)\big) \]
>
> 对 sum，merge 就是相加；对 max/min，merge 就是取极值。

#### 4.3.2 核心流程

`clear=False` 在不同后端的实现略有差别，但语义统一：

```
clear=False 时的执行流程（以 reduce_sum 为例）
   │
   ├─ 1. 备份 out 里的旧值 old_out（因为 AscendC ReduceSum 内部要用 scratch）
   ├─ 2. 用 clear=True 的方式做一次 reduce，得到 reduced_result（写入 out）
   ├─ 3. merge：out = old_out + reduced_result  （sum）
   │           out = max(old_out, reduced_result)（max）
   │           out = min(old_out, reduced_result)（min）
   └─ done
```

为什么先备份再覆盖？因为 AscendC 的 `ReduceSum` 等指令内部会使用一块临时空间（`sharedTmpBuffer`），直接依赖硬件的 `clear=false` 选项在 slice/real_shape 路径上不够可靠，所以模板库里手动做了「备份 → 强制 clear reduce → 显式 merge」三步。

#### 4.3.3 源码精读

**概念骨架（GPU 路线，但语义同构）**：`src/op/reduce.cc` 给出了 reduce 的通用实现，最能说明 `clear` 与 merge 的设计意图。

初始值（单位元）的定义：

[src/op/reduce.cc:46-61](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/op/reduce.cc#L46-L61) —— `MakeInitValue`：sum → `make_zero`（0），max → `-INFINITY`，min → `+INFINITY`。这就是 `clear=True` 写进 `out` 的初值。

归约操作的定义：

[src/op/reduce.cc:63-83](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/op/reduce.cc#L63-L83) —— `MakeReduce`：sum → `lhs + rhs`，max → `Max(lhs, rhs)`，min → `Min(lhs, rhs)`。

`clear=False` 的「复制 + merge」逻辑（注意 sum 与 abs_sum 一定 `need_duplicate`）：

[src/op/reduce.cc:151-164](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/op/reduce.cc#L151-L164) —— 当 `type == kSum && !clear` 时，`need_duplicate = true`，于是另开一个 `clear_buffer` 做 reduce；下面这段就是把 reduce 结果 merge 回真正的 `dst`：

[src/op/reduce.cc:236-251](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/op/reduce.cc#L236-L251) —— 对 sum，生成 `dst = dst + clear_buffer`，也就是 `new_out = old_out + reduced_result`，与本节公式完全一致。

> 注意：这段是 GPU `tl.reduce` 的实现，Ascend 的 `tl.ascend_reduce` 不走这个 Lower（它走 codegen + 模板库），但**语义规则完全相同**。读它是为了抓住「为什么 sum 要 duplicate、merge 怎么做」的设计源头。

**Ascend 实际实现（模板库）**：在 `common.h` 的 `reduce_sum` 模板里能看到一模一样的「备份 → 强制 clear → 显式 merge」三步：

[src/tl_templates/ascend/common.h:519-539](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/tl_templates/ascend/common.h#L519-L539) —— `clear=false` 分支：先 `GetValue` 把旧 dst 备份到 `dstBackup`，再强制以 `clear=true` 调 `ReduceSum`，最后 `SetValue(reducedValue + dstBackup[i])` 把 merge 结果写回。这就是 Ascend 上 `clear=False` 的真身。

权威语义说明：

[docs/TileLang-Ascend Programming Guide.md:812-840](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/docs/TileLang-Ascend%20Programming%20Guide.md#L812-L840) —— 手册明确：`clear=False` 不改变 reduce 方向、输出 shape 或 `real_shape` 的解释，只在 reduce 结果产生后再与已有 `out` merge 一次；并给出 sum 的数值示例（`old_out=[10,20]`，`reduced_result=[6,15]`，`new_out=[16,35]`）。

#### 4.3.4 代码实践

**实践目标**：验证 `clear=False` 的增量累加语义（reduce_sum）。

**操作步骤**：本实践为「源码阅读 + 数值推演型」，无 NPU 也可完成。

1. 阅读手册第 830-840 行的数值示例。
2. 自己构造一个例子：输入 `[[1,2,3],[4,5,6]]`，先 `reduce_sum(dim=-1)` 得到 `reduced_result`；假设 `old_out=[10,20]`，手算 `new_out`。
3. 对照 [common.h:536-539](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/tl_templates/ascend/common.h#L536-L539) 的 `reducedValue + dstBackup[i]`，确认你的手算与代码逻辑一致。

**需要观察的现象**：`reduced_result = [6, 15]`，`new_out = [10+6, 20+15] = [16, 35]`。

**预期结果**：与手册示例（`[16, 35]`）一致，证明 merge 规则是「相加」而非「覆盖」。

**待本地验证**：若有 NPU，可参考本讲第 5 节综合实践，把两块 reduce 用 `clear=True` + `clear=False` 串起来，对比与一次性 reduce 的结果。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `reduce_sum` 在 `clear=False` 时「必须」做备份 + merge，而 `reduce_max` 看起来可以「直接取 max」？

**参考答案**：核心原因在 [common.h:520-522](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/tl_templates/ascend/common.h#L520-L522) 的注释——`ReduceSum` 内部对 scratch 的使用方式可能干扰 slice/real_shape 路径上对旧 dst 的本地备份，所以 sum 用「标量备份 → 强制 clear → 标量加回」最稳妥。max/min 的 merge 是「取极值」，模板里同样做了显式备份 + 标量比较（`reduce_scalar_max_safe`），并不依赖硬件的 `clear=false`。三者实现策略一致，只是 merge 运算不同。

**练习 2**：如果忘了第一次用 `clear=True`、直接对未初始化的 `out` 用 `clear=False`，会发生什么？

**参考答案**：`out` 里的旧值是未定义的垃圾值，merge 后结果不可预测（sum 会加上垃圾值，max/min 可能被垃圾值污染）。所以正确用法是：**首次建立累加器必须 `clear=True`，后续并入才用 `clear=False`**。

---

### 4.4 从 tl.ascend_reduce 到 AscendC 指令

#### 4.4.1 概念说明

前面三个模块讲的都是「前端怎么声明、怎么校验」。这一模块把镜头拉到后端，看一个 `T.reduce_max(...)` 最终变成了哪条 AscendC 指令。理解这条链路，你才能看懂 `func.get_kernel_source()` 打印出来的设备代码。

完整的 lowering 链路是：

```
T.reduce_max(buf, out, dim=-1)                      # 前端
   └─ tir.call_intrin("tl.ascend_reduce",
        "reduce_max<float, 4, 8, -1>", out, buf, clear)   # TIR intrinsic
        └─ builtin 注册：tl.ascend_reduce (src/op/ascend.cc)
        └─ codegen：ReduceOpCodegen (src/target/codegen_ascend.cc)
             └─ 生成 C++:  tl::ascend::reduce_max<float,4,8,-1>(out, buf, tmp, clear)
                  └─ 模板库 (src/tl_templates/ascend/common.h)
                       └─ AscendC::ReduceMax<float, Pattern::Reduce::AR>(...)
```

最底层的 AscendC 指令有两个模式（由模板里的 `Pattern::Reduce` 决定）：

- **`AR`（A-then-R）**：对应 `dim=-1`，沿行方向 reduce（一行收成一个标量）。
- **`RA`（R-then-A）**：对应 `dim=0`，沿列方向 reduce（一列收成一个标量）。

另外，当输入是「物理上很宽、逻辑上只 reduce 中间一小段列」的 slice buffer 时，普通的 `Reduce*` 指令无法表达（它会把数据当成连续 M×N 块来读），此时 codegen 会改用 **`WholeReduce*`** 系列（带显式跨步 `srcRepStride`），这就是「narrow reduce」路径。

#### 4.4.2 核心流程

codegen 收到一个 `tl.ascend_reduce` 调用后，`ReduceOpCodegen` 的判断顺序：

```
ReduceOpCodegen(op):
   ├─ 解析模板串 → 得到 dtype, M, N, dim
   ├─ 解析尾部可选参数：physical_row（窄列跨步）?、clear
   │
   ├─ if physical_row > 0:                      # narrow reduce 路径
   │     选 reduce_{max,min,sum}_narrow
   │     → AscendC::WholeReduce*(..., srcRepStride = physical_row*elem_bytes/32)
   │
   ├─ elif reduce_sum && dtype==half && clear:  # half 快路径
   │     → reduce_sum_half(...)  → AscendC::WholeReduceSum(...)
   │
   └─ else:                                     # 通用路径
        → tl::ascend::reduce_{sum,max,min}<dtype, M, N, dim>(out, buf, tmp, clear)
             → AscendC::ReduceSum/Max/Min<dtype, AR|RA>(...)
```

#### 4.4.3 源码精读

builtin 注册（声明 `tl.ascend_reduce` 这个 intrinsic 存在）：

[src/op/ascend.cc:1213-1216](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/op/ascend.cc#L1213-L1216) —— 注册 `ascend_reduce`，输入个数为 4，调用效果标记为 `kOpaque`（即不透明、有副作用，不会被当作纯函数优化掉）。

codegen 的分发入口：

[src/target/codegen_ascend.cc:552-553](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/target/codegen_ascend.cc#L552-L553) —— 在 `VisitExpr_` 里识别到 `tl.ascend_reduce` 就转给 `ReduceOpCodegen`。

`ReduceOpCodegen` 的通用路径（非 sum / 非 half / 非 narrow）：

[src/target/codegen_ascend.cc:2133-2145](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/target/codegen_ascend.cc#L2133-L2145) —— 直接把模板名 `op_name`（如 `tl::ascend::reduce_max<float, 4, 8, -1>`）原样打印，后跟 `out, buf` 两个参数和 `clear_str`（`true`/`false`）。这就是最终设备代码里的一行 reduce 调用。

模板库里 `reduce_max` 如何落到 AscendC 指令：

[src/tl_templates/ascend/common.h:555-568](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/tl_templates/ascend/common.h#L555-L568) —— `clear=true` 分支：根据 `dim == -1` 选 `Pattern::Reduce::AR`（行 reduce），否则选 `RA`（列 reduce），调 `AscendC::ReduceMax<T, AR|RA>(dstTensor, srcTensor, sharedTmpBuffer, shape, true)`。这就是 reduce 在硬件上的最终形态。

half 类型的快路径（codegen 端）：

[src/target/codegen_ascend.cc:2094-2117](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/target/codegen_ascend.cc#L2094-L2117) —— 当 `dtype == "half" && clear` 时，reduce_sum 走专用的 `reduce_sum_half`，它直接计算 `mask`、`repeatTime`、`srcRepStride`（按 16 元素一个 C0 块对齐），调用 `WholeReduceSum`。对应模板见 [common.h:455-462](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/tl_templates/ascend/common.h#L455-L462)。

一个真实的 reduce 示例（`dim=-1` 行 reduce，求每行最小值）：

[examples/reduce/example_reduce_min.py:23-30](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/reduce/example_reduce_min.py#L23-L30) —— 经典三段式：`T.copy(A[..., :], a_ub)` 把 GM 数据搬进 UB；`T.reduce_min(a_ub, b_ub, dim=-1)` 沿最后一维 reduce 成 `[sub_block_M]`；`T.copy(b_ub, B[...])` 写回 GM。注意输出 `b_ub` 是压缩形式 `[sub_block_M]`，对应输入 `[sub_block_M, N]` 在 `dim=-1` 下的合法输出。

#### 4.4.4 代码实践

**实践目标**：用 `get_kernel_source()` 直接观察 `T.reduce_max` 最终生成的 Ascend C 代码。

**操作步骤**（源码阅读型，无 NPU 可只做第 1 步）：

1. 阅读 `ReduceOpCodegen`（[codegen_ascend.cc:1962-2146](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/target/codegen_ascend.cc#L1962-L2146)），跟踪一个 `reduce_max<float, 4, 8, -1>` 会走哪条分支、生成什么样的 C++ 代码。
2. （需要 NPU/仿真）参考 [example_reduce_min.py](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/reduce/example_reduce_min.py)，把其中 `T.reduce_min` 换成 `T.reduce_max`，并在编译后调用 `func.get_kernel_source()`，在生成的源码里搜 `reduce_max` 或 `ReduceMax`。

**需要观察的现象**：生成的设备代码里应出现形如 `tl::ascend::reduce_max<float, sub_block_M, N, -1>(b_ub, a_ub, tmp, true);` 的一行（参数顺序与模板定义一致）。

**预期结果**：能在这行 C++ 与 [common.h:560-567](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/tl_templates/ascend/common.h#L560-L567) 的模板实例化之间建立一一对应，确认 `dim=-1` 选中了 `Pattern::Reduce::AR`。

**待本地验证**：具体行号与变量名以本地 `get_kernel_source()` 输出为准。

#### 4.4.5 小练习与答案

**练习 1**：同一个 `reduce_max<float, 4, 8, -1>`，`clear=True` 和 `clear=False` 生成的设备代码行数一样吗？为什么？

**参考答案**：不一样。`clear=True` 只生成一行 `AscendC::ReduceMax<..., AR>(...)` 就返回了（见 [common.h:560-568](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/tl_templates/ascend/common.h#L560-L568) 提前 `return`）；`clear=False` 会额外生成备份循环、reduce 调用、逐元素 merge 循环（见 [common.h:574-594](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/tl_templates/ascend/common.h#L574-L594)）。所以 `clear=False` 的指令更多、更慢，只在需要增量 merge 时才用。

**练习 2**：`AR` 和 `RA` 这两个 Pattern 分别对应 `dim` 取什么值？

**参考答案**：`dim == -1`（沿最后一维、行 reduce）→ `Pattern::Reduce::AR`（A 轴在前、R 轴在后）；`dim == 0`（沿第一维、列 reduce）→ `Pattern::Reduce::RA`（R 轴在前、A 轴在后）。在 [common.h:561-566](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/tl_templates/ascend/common.h#L561-L566) 里由 `if constexpr (dim == -1)` 选择。

---

## 5. 综合实践

把本讲的四个模块串起来：写一个对 `[M, N]` UB tile 做 `dim=-1` 的 `reduce_max` 与 `dim=0` 的 `reduce_sum` 的小算子，并演示 `clear=False` 的增量累加。

下面是**示例代码**（基于 `examples/reduce/example_reduce_min.py` 和 `examples/reduce/example_col_reduce_max_slice_buffer.py` 改写，并非仓库已有文件，需要 NPU/仿真环境运行）：

```python
# 示例代码：综合练习 reduce_max(dim=-1) + reduce_sum(dim=0, clear=False)
import tilelang
from tilelang import language as T
import torch

# dim=-1 的 reduce_max：对 [M,N] 每行求最大值，输出 [M]
@tilelang.jit(out_idx=[1])
def row_reduce_max(M, N, block_M, dtype="float"):
    m_num = M // block_M
    VEC_NUM = 2
    sub_block_M = block_M // VEC_NUM

    @T.prim_func
    def main(A: T.Tensor([M, N], dtype),
             B: T.Tensor([M], dtype)):
        with T.Kernel(m_num, is_npu=True) as (cid, vid):
            a_ub = T.alloc_ub((sub_block_M, N), dtype)
            b_ub = T.alloc_ub((sub_block_M), dtype)   # 压缩形式输出，dim=-1 合法
            row_base = cid * block_M + vid * sub_block_M
            with T.Scope("V"):
                T.copy(A[row_base:row_base+sub_block_M, :], a_ub)
                T.barrier_all()
                T.reduce_max(a_ub, b_ub, dim=-1)        # 第一次：clear=True 建立初值
                T.barrier_all()
                T.copy(b_ub, B[row_base:row_base+sub_block_M])
    return main


# dim=0 的 reduce_sum：对 [M,N] 每列求和，输出 [N]
# 用两块拼接演示 clear=False：把输入沿 M 切成上下两半，分别 reduce 再 merge
@tilelang.jit(out_idx=[1])
def col_reduce_sum_split(M, N, dtype="float"):
    half_M = M // 2

    @T.prim_func
    def main(A: T.Tensor([M, N], dtype),
             B: T.Tensor([N], dtype)):
        with T.Kernel(1, is_npu=True) as (cid, vid):
            top_ub = T.alloc_ub((half_M, N), dtype)
            bot_ub = T.alloc_ub((half_M, N), dtype)
            out_ub = T.alloc_ub((N), dtype)            # 压缩形式输出，dim=0 合法
            with T.Scope("V"):
                T.copy(A[:half_M, :], top_ub)
                T.copy(A[half_M:, :], bot_ub)
                T.barrier_all()
                # 上半块：clear=True，建立初值 out = reduce_sum(top)
                T.reduce_sum(top_ub, out_ub, dim=0, clear=True)
                # 下半块：clear=False，merge 进已有 out
                # new_out = old_out + reduce_sum(bot)
                T.reduce_sum(bot_ub, out_ub, dim=0, clear=False)
                T.barrier_all()
                T.copy(out_ub, B[:])
    return main


if __name__ == "__main__":
    tilelang.cache.clear_cache()
    M, N = 256, 256

    # 1) row reduce max
    f1 = row_reduce_max(M, N, 128)
    a = torch.randn(M, N).npu()
    c1 = f1(a)
    torch.testing.assert_close(c1, torch.max(a, dim=-1).values, rtol=1e-2, atol=1e-2)
    print("reduce_max(dim=-1) Kernel Output Match!")

    # 2) col reduce sum with clear=False merge
    f2 = col_reduce_sum_split(M, N)
    c2 = f2(a)
    torch.testing.assert_close(c2, torch.sum(a, dim=0), rtol=1e-2, atol=1e-2)
    print("reduce_sum(dim=0, clear=False) Kernel Output Match!")
```

**实践要求**：

1. **读懂结构**：标注出每个 `reduce_*` 调用的 `dim`、`out` 的 shape（压缩还是 keepdim）、`clear` 取值，对照 4.2 节确认 shape 合法。
2. **验证 `clear=False` 语义**：`col_reduce_sum_split` 把矩阵沿 M 切两半，上半用 `clear=True`、下半用 `clear=False`。预期结果应等于「一次性对整个 `[M,N]` 做 `dim=0` 的 `reduce_sum`」，这正好验证了 merge 规则 `new_out = old_out + reduced_result`。
3. **（可选）观察生成代码**：在两个 kernel 上分别调用 `func.get_kernel_source()`，对照 4.4 节找到 `tl::ascend::reduce_max` / `reduce_sum` 那一行，确认 `dim=-1` 对应 `AR`、`dim=0` 对应 `RA`。
4. **（可选）改 dim 触发报错**：把 `row_reduce_max` 里的 `dim=-1` 改成 `dim=2`（对 2D buffer 非法），运行确认在前端立刻报 `ValueError`。

**预期结果**：两个 kernel 都打印 `Kernel Output Match!`，证明 reduce 方向、输出 shape 与 `clear=False` merge 语义都正确。

**待本地验证**：上述脚本依赖 NPU（`torch.randn(...).npu()`）与 CANN/bisheng 环境；若无真实 NPU，可用 `target="pto"` 配合 camodel 仿真（参见 u7-l5）跑通，或退化为「源码阅读 + 数值推演」完成 1、2 两步。

## 6. 本讲小结

- `T.reduce_sum/max/min` 是 Ascend 上的 **fast-path reduce 原语**，前端签名一致、只在 `reduce_type` 字符串上区分，语义实现全部后置到 codegen 与模板库。
- `dim` 会被 `_legalize_reduce_dim` 归一化成 `-1`（行 reduce，AR）或 `0`（列 reduce，RA）；非法 dim 与非法输出 shape 都在前端直接报 `ValueError`，绝不静默进后端。
- 输出 `out` 的 shape 有「压缩形式」与「keepdim 形式」两种合法写法；`keepdim` 不是「随便保持输入 shape」。
- `clear=True` 先写单位元再 reduce（覆盖）；`clear=False` 先算 `reduced_result` 再与 `out` 旧值 merge——sum 相加、max 取大、min 取小。分块累加时首次用 `clear=True`、后续用 `clear=False`。
- `real_shape` 用于描述「物理 buffer 更宽、逻辑上只有一部分有效」的 2D slice buffer，此时会走 narrow reduce（`WholeReduce*` 带 `srcRepStride`）路径。
- 完整链路：前端 `T.reduce_*` → `tir.call_intrin("tl.ascend_reduce", ...)` → `ReduceOpCodegen` 生成 `tl::ascend::reduce_*<dtype,M,N,dim>` → 模板库调 `AscendC::ReduceSum/Max/Min<AR|RA>`。

## 7. 下一步学习建议

本讲之后，建议按以下顺序继续：

1. **u3-l5 Element-wise 与 T.Parallel**：reduce 经常和 element-wise 运算组合（如 softmax = `reduce_max` → `exp` → `reduce_sum` → 除法），T.Parallel 负责那些 element-wise 步骤，两者配合才能写出完整算子。
2. **u3-l6 T.Pipelined 软件流水**：当 reduce 在 K 循环里反复执行（如 online softmax 的分块 reduce），用软件流水把 reduce 和数据搬运重叠起来能显著提速；`examples/reduce/example_reduce_min_pipeline.py` 就是一个流水化的 reduce 样例，值得对照阅读。
3. **u6-l6 Tile Op lowering 与 Tail Mask**：当输入维度不是 16/32 对齐时，reduce 的 tail tile 需要靠 `real_shape`（或 Tail Mask pass）告诉硬件「只对有效区域 reduce」，本讲的 `real_shape` 是这条机制的入口。
4. **u7-l1 FlashAttention 实现案例**：FA 是 reduce 的「集大成」用例——`reduce_max` 求 row max、`reduce_sum` 求 softmax 分母，且都在分块循环里用 `clear=False` 做增量 merge。学完本讲再读 FA，会对 reduce 的实战用法有直观体会。
