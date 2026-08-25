# u15-l1 TIRx 语言参考：解析工具、数据类型与 buffer

## 1. 本讲目标

前面十几个单元里，我们一直在「照抄书中的内核」学习 TIRx：先跑通 hgemm_v1，再沿 GEMM 九步与 FA4 一路读下来。本讲换一个视角——把语言本身当作对象，系统过一遍 **TIRx Language Reference** 的前三部分。读完本讲，你应该能够：

1. 说出 TIRx 四个**解析期工具**（`T.meta_var`、`@T.inline`、`@T.meta_class`、`T.constexpr`）各自的用途与生效时机。
2. 查阅 TIRx 的 **dtype 体系**：标量 dtype、向量 dtype（如 `float32x4`）、dtype 到 CUDA 类型的映射，以及 `dtype` 与 `type` 的区别。
3. 区分 buffer 的**两条声明 API**（`T.alloc_buffer` 与 `T.decl_buffer`）与**五种 scope**（`global` / `shared` / `shared.dyn` / `local` / `tmem`），理解「buffer 是指针之上的元数据」这一核心模型。
4. 拿着语言参考**逐条核对**一个真实内核（hgemm_v1）的每处 buffer 声明是否合法、落在哪层存储上，整理成一份可复用的速查表。

本讲是「手册型」讲义：目标不是学会新内核，而是获得**查证能力**——以后写出或读到任何 TIRx 声明，都能回到语言参考里找到它的准确拼写与语义。

## 2. 前置知识

本讲属于专家层（advanced），默认你已完成单元九（TIRx 编程模型入门），尤其是 u9-l3 的三要素框架。需要的背景概念用三句话回顾：

- **TIRx 是 Python DSL，但被编译的不是 Python 程序。** 内核写在 `@T.prim_func` 函数里，由 TVMScript 解析器翻译成结构化 IR，再经 `tir_pipeline="tirx"` 的 lowering 流水线生成 CUDA。u9-l1 已见过完整回路。
- **三要素（scope/layout/dispatch）。** 每个 tile 操作由「哪些线程执行、数据怎么摆、走哪条硬件路径」刻画（u9-l3）。本讲的 buffer scope 与 layout 正是其中「数据放在哪」一要素的语言层落点。
- **四级存储空间。** GMEM（device 显存）、SMEM（CTA 私有片上）、TMEM（Blackwell 独有的 128 lane × 512 列张量内存）、RF（每线程寄存器），见 u2-l2。本讲的五种 scope 就是这四级在语言里的拼写。

再补充两个本讲新引入的基础区分，后续会反复用到：

| 时间点 | 发生什么 | 谁负责 |
|---|---|---|
| 解析期（parse time） | Python 源码 → TIRx IR | TVMScript 解析器 |
| lowering 期（compile time） | TIRx IR → CUDA 源码 → NVRTC 编译 | TIRx 流水线（`LowerTIRx` 等 pass） |
| 运行期（run time） | 内核在 GPU 上执行，buffer 真正占有存储 | 硬件 |

**解析期工具只作用于第一行**；buffer 的分配发生在 IR 构造/lowering；而「这个 buffer 此刻的值」只在运行期才有意义。把一个语法现象归到哪一行时间轴上，是读懂语言参考的第一步。

## 3. 本讲源码地图

本讲的「源码」是仓库里的语言参考文档本身（Sphinx RST 格式），外加一个被核对的真内核：

| 文件 | 作用 |
|---|---|
| [tirx_guide/language_reference/index.rst](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/tirx_guide/language_reference/index.rst) | 语言参考总入口，声明本参考覆盖五块：解析工具、数据类型与表达式、buffer 与内存、控制流、线程同步（后两块是 u15-l2 的内容） |
| [tirx_guide/language_reference/cuda/parser_utils.rst](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/tirx_guide/language_reference/cuda/parser_utils.rst) | 解析期工具：`T.meta_var`、`@T.inline`、`@T.meta_class`、`T.constexpr` |
| [tirx_guide/language_reference/cuda/data_types.rst](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/tirx_guide/language_reference/cuda/data_types.rst) | dtype 体系、dtype→CUDA 映射、向量 dtype、指针与 `T.reinterpret` |
| [tirx_guide/language_reference/cuda/buffers.rst](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/tirx_guide/language_reference/cuda/buffers.rst) | 本讲最大的一页：两条声明 API、五种 scope、SMEM 静态/动态/池、寄存器、标量、`let`、TMEM、buffer 方法表 |
| [chapter_intro_tirx/index.md](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_intro_tirx/index.md) | hgemm_v1 内核全文，是本讲综合实践的核对对象 |

引用约定：语言参考是 RST 文档，正文讲义引用时给出永久链接与行号；hgemm_v1 的代码引用同样给行号。文中「示例代码」均指标注为示例的片段，书中内核代码则原样摘录。

## 4. 核心概念与源码讲解

### 4.1 解析工具：parse time 的四个帮手

#### 4.1.1 概念说明

写 TIRx 内核时，你其实同时在写两样东西：一段**会被执行的 Python**（构造期的循环、条件、字符串拼接）和一段**会被翻译成 IR 的内核体**。解析器需要一些「开关」来区分这两种身份。parser_utils 页开宗明义：这几个帮手作用在**解析期**——即「TVMScript 被变成 TIRx」的那一刻——让你把 Python 算好的值内联进 IR、把可复用片段抽成函数、把解析期状态打包成对象（[tirx_guide/language_reference/cuda/parser_utils.rst:L21-L23](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/tirx_guide/language_reference/cuda/parser_utils.rst#L21-L23)）。

四个工具一张表：

| 工具 | 一句话用途 | 生效方式 |
|---|---|---|
| `T.meta_var(x)` | 把 **Python 里算好的值**当编译期 meta 值直接内联进 IR，而不是当脚本变量解析 | 解析期常量折叠 |
| `@T.inline` | 定义「每个调用点都被内联」的函数，生成代码里不出现调用 | 解析期文本级内联 |
| `@T.meta_class` | 把普通 Python 类标记为「实例即解析期 meta 值」，字段可持有 buffer 与标量 | 解析期状态打包 |
| `T.constexpr` | 标记编译期内核参数，由 `@T.jit` 的 `.specialize(...)` 烘焙 | 特化时固定 |

它们共同回答一个问题：**Python 的计算结果如何进入 IR 而不留下一次性的脚本变量**。

#### 4.1.2 核心流程

解析期工具在整个编译回路中的位置：

```
Python 源码
  ├─ 构造期 Python 代码（def hgemm_v1(...) 里的普通 Python）
  └─ @T.prim_func 内核体
        │
        │  TVMScript 解析器（parse time）★ 四个工具在这里起作用
        │    · T.meta_var(x)        → 值内联为编译期 meta
        │    · @T.inline f(...)     → 调用点展开，IR 中无 call
        │    · @T.meta_class        → 字段成为 meta 值
        ▼
TIRx IR（PrimFunc）
        │  TIRx lowering 流水线（u9-l2 讲过，共 19 个 pass，首个是 LowerTIRx）
        ▼
CUDA 源码 → NVRTC → 运行期执行
```

两个容易混淆的点，语言参考都写了：

- **`range(n)` 不是 Python 循环的「展开」标记。** 普通 `range(n)` 解析后仍是一条**串行 TIRx 循环**；要让 lowering 流水线展开它，得写 `T.unroll(n)`（[tirx_guide/language_reference/cuda/parser_utils.rst:L30-L32](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/tirx_guide/language_reference/cuda/parser_utils.rst#L30-L32)）。
- **`@T.inline` 遵循 Python 的词法（LEGB）作用域与晚绑定**，所以参数会遮蔽外层同名变量——它语义上就是 Python 函数，只是身体被搬到调用点（[tirx_guide/language_reference/cuda/parser_utils.rst:L43-L46](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/tirx_guide/language_reference/cuda/parser_utils.rst#L43-L46)）。

#### 4.1.3 源码精读

**(a) `T.meta_var`——内联一个 Python 值。** 文档原文：它告诉解析器把 `x`（一个**在 Python 里算出的值**）当作编译期 *meta* 值直接内联进 IR，而不是解析成脚本变量；这避免了一次性局部变量，并让 Python 值参数化生成的 IR（[tirx_guide/language_reference/cuda/parser_utils.rst:L25-L32](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/tirx_guide/language_reference/cuda/parser_utils.rst#L25-L32)）。文档示例（示例代码，摘自语言参考）：

```python
n = T.meta_var(4)              # 常数 4 被内联为循环 extent
for j in T.unroll(n):          # 标记给 UnrollLoop lowering pass
    acc[0] = acc[0] + A[tx, j]
```

**(b) `@T.inline`——内联函数。** 函数体在解析期于**每个调用点**展开，生成代码里不出现调用（[tirx_guide/language_reference/cuda/parser_utils.rst:L40-L53](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/tirx_guide/language_reference/cuda/parser_utils.rst#L40-L53)）。这解释了 GEMM Step 4 里 `@T.inline` 辅助函数的写法：TMA 配置片段被逐点搬进内核体，IR 层面没有函数调用。

**(c) `@T.meta_class`——解析期状态对象。** 标记后的类，其**实例就是解析器 meta 值**，字段可以持有 buffer 和标量，因此能把一组相关分配与状态捆绑成一个对象在内核体里使用（[tirx_guide/language_reference/cuda/parser_utils.rst:L55-L75](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/tirx_guide/language_reference/cuda/parser_utils.rst#L55-L75)）。文档点明它的典型用途：打包内核的**流水线状态**（barrier、累加器、scratch 视图），免得在内核体里穿针引线一大堆局部变量。FA4 那种「每 stage 一套屏障+邮箱」的结构，正是这类需求的重灾区。

**(d) `T.constexpr`。** 标记编译期内核参数，由 `@T.jit` 的 `.specialize(...)` 烘焙，细节指向 primer 章节（[tirx_guide/language_reference/cuda/parser_utils.rst:L77-L81](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/tirx_guide/language_reference/cuda/parser_utils.rst#L77-L81)）。

**(e) 回到 hgemm_v1 中的真实使用。** 内核里有三处 `T.meta_var`：

- [chapter_intro_tirx/index.md:L133-L134](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_intro_tirx/index.md#L133-L134)：`m_st = T.meta_var(bx * BLK_M)`、`n_st = T.meta_var(by * BLK_N)`——把 Python 常数 `BLK_M`/`BLK_N`（=128）与 IR 表达式 `bx`/`by` 相乘，乘积中的常数部分在解析期定死；
- [chapter_intro_tirx/index.md:L161](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_intro_tirx/index.md#L161)：`m_thr = T.meta_var(m_st + warp_id * 32 + lane_id)`——回写时每线程负责的输出行，同样把 Python 侧常数 `32` 内联。

另有一处「标量语法糖」将在 4.3.3(c) 讲：`phase_mma: T.int32 = 0`（[chapter_intro_tirx/index.md:L135](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_intro_tirx/index.md#L135)）。

#### 4.1.4 代码实践

**实践目标**：确认自己能区分 hgemm_v1 中「解析期内联的 Python 值」与「运行期才确定的 IR 值」。

**操作步骤**（源码阅读型，无需 GPU）：

1. 打开 [chapter_intro_tirx/index.md:L85-L171](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_intro_tirx/index.md#L85-L171) 的 hgemm_v1 全文。
2. 找出全部三处 `T.meta_var`（L133、L134、L161）。
3. 对每一处，把表达式里的操作数分两栏抄写：左栏「Python 构造期已知」（如 `BLK_M`=128、常数 `32`），右栏「IR 表达式」（如 `bx`、`warp_id`、`lane_id`）。
4. 可选（有 tvm 环境时）：把书中内核存成 `hgemm_v1.py`，在文件末尾加 `print(hgemm_v1(128, 128, 64).script())`，运行后观察 `m_st` 一行打印出的常数。注意 TIRx 依赖源码检视，内核**必须写在文件里**，不能放进 `python -c`（u1-l3 的纪律）。

**需要观察的现象**：`script()` 输出中，`T.meta_var` 包装的表达式变成一条普通的 IR 绑定，常数 128 已经「烧」在表达式里，不再引用任何 Python 名字。

**预期结果**：三处表达式均能完成两栏分类；其中 `bx`、`by` 来自 `T.cta_id`（[chapter_intro_tirx/index.md:L104](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_intro_tirx/index.md#L104)），运行期才绑定到 `blockIdx`。步骤 4 的运行结果**待本地验证**（本环境未装 tvm）。

#### 4.1.5 小练习与答案

**练习 1**：`for j in range(n)` 与 `for j in T.unroll(n)` 在 TIRx 里的区别是什么？

**答案**：普通 `range(n)` 解析后仍是一条串行 TIRx 循环；`T.unroll(n)` 把循环标记给 lowering 流水线中的 `UnrollLoop` pass 去展开。两者的区别发生在 lowering 期，而不是解析期。

**练习 2**：`@T.inline` 函数与普通 Python 函数在作用域上有何异同？

**答案**：相同点：都遵循 Python 的词法（LEGB）作用域与晚绑定，参数会遮蔽外层同名变量。不同点：`@T.inline` 的函数体在解析期于每个调用点展开，生成的 IR/代码里没有函数调用；普通 Python 函数调用则只是一段构造期 Python 逻辑，不进入内核体。

**练习 3**：FA4 每个流水线 stage 都持有一组「屏障 + SMEM 邮箱 + 视图」。语言参考中哪个解析工具最适合把这组状态打包？为什么？

**答案**：`@T.meta_class`。它让一个普通 Python 类的实例成为解析期 meta 值，字段可持有 buffer（屏障、邮箱）与标量，从而把相关分配与状态捆绑成一个对象在内核体中使用，避免大量独立局部变量。

### 4.2 数据类型：dtype、向量与指针

#### 4.2.1 概念说明

语言参考对类型的第一个论断是：**每个 TIRx 表达式都携带一个高层「类型 type」，标量与向量类型还包含一个底层「dtype」**（[tirx_guide/language_reference/cuda/data_types.rst:L21-L22](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/tirx_guide/language_reference/cuda/data_types.rst#L21-L22)）。二者的分工：

- **dtype 回答「比特是什么」**：`float32`、`float16`、`bfloat16`、`int32`、`uint8`、`bool`，低精度的 `float8_e4m3fn` / `float4_e2m1fn` 等，以及向量形式如 `float32x4`（[tirx_guide/language_reference/cuda/data_types.rst:L27-L33](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/tirx_guide/language_reference/cuda/data_types.rst#L27-L33)）。每种 dtype 打印成对应的 CUDA 类型。
- **type 回答「这个值是什么形状的量」**：标量/向量是 `PrimType(dtype)`，指针是 `PointerType(PrimType(dtype), scope)`。多数表达式都是 `PrimType`，dtype 与 type 的区别**主要对指针有意义**（[tirx_guide/language_reference/cuda/data_types.rst:L91-L97](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/tirx_guide/language_reference/cuda/data_types.rst#L91-L97)）。

这一节对全书内核的 dtype 选择给出语言层依据：GEMM/FA4 一律「fp16 存储输入输出、fp32 累加」，写出来就是 buffer 声明里的两个 dtype 字符串。

#### 4.2.2 核心流程

一个 dtype 声明从 TIRx 到 CUDA 的旅程：

```
TIRx:  T.alloc_local((1,), "float32x4")     ← 向量 dtype 直接声明 float4 寄存器
          │  lowering（dtype → CUDA 类型映射）
          ▼
CUDA:  float4 v_ptr[1];
       v_ptr[0] = *(float4*)(A_ptr + tx*4);  ← vload(dtype="float32x4") = 一次 16B 访存
```

向量 dtype 的两个要点（[tirx_guide/language_reference/cuda/data_types.rst:L67-L70](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/tirx_guide/language_reference/cuda/data_types.rst#L67-L70)）：

1. buffer 的 dtype 本身可以是向量类型：`T.alloc_local((1,), "float32x4")` 直接声明一个 `float4` 寄存器，按 `v[0]` 索引；
2. 向量 dtype **不绑定 `vload`**——任何 buffer 或标量都能携带它；`float32x4` 的 `vload`/`vstore` 把它当作一次 16 字节访问搬动。

#### 4.2.3 源码精读

**(a) dtype→CUDA 映射表。** 语言参考给出完整对应（[tirx_guide/language_reference/cuda/data_types.rst:L72-L89](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/tirx_guide/language_reference/cuda/data_types.rst#L72-L89)），整理如下：

| TIRx dtype | CUDA 类型 | 出现场景 |
|---|---|---|
| `float32` | `float` | 累加器、TMEM 累加列 |
| `float16` | `half` | GEMM/FA4 的输入输出 |
| `bfloat16` | `nv_bfloat16` | LLM 常用输入 dtype |
| `int32` | `int` | 相位、循环变量 |
| `uint8` | `uchar` | 计数/字节 |
| `bool` | `bool` | 谓词 |
| `float32x4`（向量） | `float4` | 向量化访存 |
| `PointerType` | `T*` | `data` 指针投影 |
| `float8_e4m3fn` / `float4_e2m1fn` 等 | 对应 CUDA 低精度类型 | block-scaled MMA（u7-l2） |

**(b) 文档示例：多 dtype 声明与向量化访存。** 语言参考用一个内核同时声明多种 dtype 的 local/shared buffer，再演示 `float32x4` 的 `vload`/`vstore`（[tirx_guide/language_reference/cuda/data_types.rst:L36-L51](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/tirx_guide/language_reference/cuda/data_types.rst#L36-L51)），其生成 CUDA（[tirx_guide/language_reference/cuda/data_types.rst:L53-L65](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/tirx_guide/language_reference/cuda/data_types.rst#L53-L65)）里可以看到 `half f16_ptr[1]`、`__shared__ alignas(64) half sm_ptr[64]`、`float4 v_ptr[1]` 一一对应。

**(c) 指针（handle）。** 这是最容易踩坑的一节：buffer 值是一个类型为 `BufferType` 的 `Var`，其 `data` 属性投影出一个**不可变的**指针类型 `Expr`，投影结果本身不必是 `Var`（[tirx_guide/language_reference/cuda/data_types.rst:L99-L104](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/tirx_guide/language_reference/cuda/data_types.rst#L99-L104)）。由此得出三条获取/复用指针的规则：

1. `T.alloc_buffer(...)` 分配存储**并**定义其 `data` 指针；
2. `T.decl_buffer(..., data=ptr)` 在一个已有的兼容指针表达式上声明 buffer；
3. 要用**指针表达式**（而非变量）支撑 buffer——例如 `T.ptx.map_shared_rank`（PTX `mapa`，取 cluster 内另一 CTA 的共享地址）返回的是 `uint64` 原始地址——必须先用 `T.reinterpret` 把它转换成带目标元素类型与存储 scope 的指针（[tirx_guide/language_reference/cuda/data_types.rst:L105-L124](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/tirx_guide/language_reference/cuda/data_types.rst#L105-L124)）：

```python
# 示例代码，摘自语言参考：把 mapa 返回的 uint64 转成 shared 指针
from tvm.ir import PointerType, PrimType

ptr = T.reinterpret(
    PointerType(PrimType("uint64"), "shared"),
    T.ptx.map_shared_rank(mbar.ptr_to([0]), 0),
)
remote_mbar = T.decl_buffer([1], "uint64", data=ptr, scope="shared")
```

最后一条纪律：**指针绑定不可重赋值**，换指针值就用新名字（[tirx_guide/language_reference/cuda/data_types.rst:L126-L127](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/tirx_guide/language_reference/cuda/data_types.rst#L126-L127)）。这段 `map_shared_rank` 示例正是 Step 8 跨 CTA 屏障（u13-l2 的 `remote_view`）在语言层的底层机制。

**(d) hgemm_v1 中的 dtype 选择。** 构造函数开头就用 `tvm.DataType` 定下四种类型（[chapter_intro_tirx/index.md:L86-L89](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_intro_tirx/index.md#L86-L89)）：`a_type`/`b_type`/`d_type` 为 `float16`（输入与输出），`acc_type` 为 `float32`（累加）。它们随后出现在参数 buffer（fp16）、TMEM 声明（fp32，L129）、回写寄存器 `Dreg`（fp32）与 `Dreg_f16`（fp16）上——「fp16 存、fp32 累加」这条全书约定，在语言层就是这些 dtype 字符串。

#### 4.2.4 代码实践

**实践目标**：为一个真实内核建立「TIRx dtype → CUDA 类型」的预测能力。

**操作步骤**（推演型，无需 GPU）：

1. 在 hgemm_v1 中枚举所有出现的 dtype：`float16`（a/b/d_type）、`float32`（acc_type 与 tmem）、`uint32`（tmem_addr，L111）、`uint64`（mma_bar，L112）。
2. 查 4.2.3(a) 的映射表（或直接查 [tirx_guide/language_reference/cuda/data_types.rst:L72-L89](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/tirx_guide/language_reference/cuda/data_types.rst#L72-L89)），为每一个写出预期 CUDA 类型。
3. 有 tvm 环境时编译并打印生成 CUDA：`print(ex.mod.imports[0].inspect_source())`（u9-l2 的检视套路），在 CUDA 源里搜索你预测的类型名。

**需要观察的现象**：`inspect_source()` 输出中应出现 `half`（A/B/D 及 Asmem/Bsmem/Dreg_f16）、`float`（Dreg 与 TMEM 相关代码）、`__shared__ ... unsigned int tmem_addr_ptr[1]` 一类声明。注意 `uint32`/`uint64` 未收录于参考的示例映射表片段中，预测时按「`u` 前缀 → `unsigned`」的 CUDA 惯例推断，并以生成代码为准。

**预期结果**：预测表与生成 CUDA 逐条对上；`uint32`→`unsigned int`、`uint64`→`unsigned long long` 的具体拼写**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：`dtype` 和 `.ty`（type）分别描述什么？区别主要在哪里体现？

**答案**：`dtype` 是低层描述——「比特是什么」；`.ty` 是高层类型——标量/向量是 `PrimType(dtype)`，指针是 `PointerType(PrimType(dtype), scope)`。区别主要对**指针**有意义：指针表达式携带元素类型与存储 scope 两项额外信息。

**练习 2**：`T.alloc_local((1,), "float32x4")` 声明了什么？它与 `vload(dtype="float32x4")` 是什么关系？

**答案**：声明了一个向量 dtype 的寄存器 buffer，lowering 成 `float4 v_ptr[1]`，按 `v[0]` 索引。二者相互独立：向量 dtype 不是 `vload` 的专属——任何 buffer 或标量都能携带向量 dtype；`float32x4` 的 `vload`/`vstore` 只是把数据当作一次 16 字节访问搬动。

**练习 3**：为什么 `T.ptx.map_shared_rank` 的返回值不能直接当 `data=` 用，必须先过 `T.reinterpret`？

**答案**：`map_shared_rank`（PTX `mapa`）返回的是 `uint64` 原始地址，缺少元素类型与存储 scope 的类型信息。`T.reinterpret` 用 `PointerType(PrimType("uint64"), "shared")` 把它转换成带完整类型的指针表达式，这样的「兼容指针表达式」才能作为 `T.decl_buffer` 的 `data`。

### 4.3 buffer 与内存作用域：声明 API 与五种 scope

#### 4.3.1 概念说明

buffers 页是语言参考里最长的一页，但核心模型只有一句话——**「基于指针的 buffer 只是指针之上的元数据」**：声明一个 buffer 就是给一个指针配上 shape 与 layout，索引最终解析成一个地址（[tirx_guide/language_reference/cuda/buffers.rst:L98-L101](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/tirx_guide/language_reference/cuda/buffers.rst#L98-L101)）：

\[
\mathrm{addr}(\text{buffer}[\mathrm{coord}]) \;=\; \text{buffer.data} \;+\; \text{elem\_offset} \;+\; \text{layout.apply}(*\mathrm{coord},\ \mathrm{shape}{=}\mathrm{shape})[\text{"m"}]
\]

（`layout.apply` 返回按轴的映射，其 `"m"` 分量是默认轴上的元素偏移。）这个模型解释了 u9-l3 讲过的现象：**同一个逻辑访问，仅因 buffer 元数据不同就会编译成不同的地址算术**。

围绕这个模型有两条声明 API、五种 scope：

- **`T.alloc_buffer(shape, dtype, scope=..., ...)`——分配新存储**，发出 `AllocBuffer` 节点，返回类型为 `BufferType` 的 `Var`。`T.alloc_shared` / `T.alloc_local` 只是 `scope="shared"` / `scope="local"` 的快捷写法（[tirx_guide/language_reference/cuda/buffers.rst:L30-L34](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/tirx_guide/language_reference/cuda/buffers.rst#L30-L34)）。
- **`T.decl_buffer(shape, dtype, data=..., ...)`——声明视图**，不分配；用于别名或重解释存储——pool 的一个子区域、一个 tensor-memory 地址。普通 scope 下 `data=None` 时行为同 `alloc_buffer`；**TMEM 是例外**，改用 `allocated_addr`（[tirx_guide/language_reference/cuda/buffers.rst:L35-L39](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/tirx_guide/language_reference/cuda/buffers.rst#L35-L39)）。

参数 buffer 则在函数签名上绑定：语言参考的写法是 `T.match_buffer`（[tirx_guide/language_reference/cuda/buffers.rst:L21-L24](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/tirx_guide/language_reference/cuda/buffers.rst#L21-L24)）；hgemm_v1 用的是签名注解形式 `A: T.Buffer((M, K), a_type)`（[chapter_intro_tirx/index.md:L96-L100](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_intro_tirx/index.md#L96-L100)），二者绑定的是同一种「参数 buffer」（global scope）。

scope 选择存储空间（[tirx_guide/language_reference/cuda/buffers.rst:L66-L89](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/tirx_guide/language_reference/cuda/buffers.rst#L66-L89)）：

| Scope | 快捷写法 | 存储 | u2-l2 对应 |
|---|---|---|---|
| `"global"` | （默认） | device 全局内存 | GMEM |
| `"shared"` | `T.alloc_shared` | 静态共享内存（`__shared__`） | SMEM |
| `"shared.dyn"` | （池） | 动态共享内存（池化 arena） | SMEM |
| `"local"` | `T.alloc_local` | 每线程寄存器 | RF |
| `"tmem"` | （TMEM 池） | Blackwell tensor memory | TMEM |

#### 4.3.2 核心流程

一个 buffer 声明的「元数据 → 地址算术」链条，以及五种 scope 各自的生命周期管理方式：

```
声明：T.alloc_buffer / T.decl_buffer / T.match_buffer（参数）
        │  元数据 = data 指针 + elem_offset/allocated_addr + layout + scope
        ▼
索引：A[i, j] → addr = data + elem_offset + layout.apply(i, j)["m"]
        │  lowering：按 scope 分派到不同 CUDA 存储
        ├── "global"      → 核核参数指针 A_ptr
        ├── "shared"      → __shared__ T x[N];          （编译期定尺寸）
        ├── "shared.dyn"  → extern __shared__ arena[];  （launch 定尺寸，仅一个）
        ├── "local"       → 每线程数组（静态索引 → SROA 提升为寄存器）
        └── "tmem"        → 无地址！只能经 tcgen05 mma/ld/st/cp 访问
```

生命周期上各 scope 的差异是本节的暗线：

- `global` 参数由调用方传入，内核不管分配；
- `shared`（静态）编译期定尺寸，随内核存在；
- `shared.dyn`（动态）一个内核**只允许一个**动态共享分配（arena），其余都是它的视图；
- `local` 每线程私有，静态索引的数组被标量化成寄存器；
- `tmem` **不是普通 scratch scope**：必须用 warp-uniform 的 `tcgen05.alloc`/`dealloc` 显式申请释放——这是 u7-l3 讲过的 TMEM 生命周期在语言参考侧的对应表述。

#### 4.3.3 源码精读

**(a) 同一访问、四种元数据、四种地址。** 语言参考用 `B[i, j] = A[i, j] + 1` 演示：只改 B 的声明方式（行主序 / 列主序 layout / `elem_offset=64` / 行距 16 的 layout），生成的 CUDA 索引就变成 `i*8+j`、`j*4+i`、`i*8+j+64`、`i*16+j` 四种，而 `A[i, j]` 的装载始终是 `i*8+j`（[tirx_guide/language_reference/cuda/buffers.rst:L103-L125](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/tirx_guide/language_reference/cuda/buffers.rst#L103-L125)）。这就是「buffer 是元数据」最直接的证据。

**(b) 共享内存的两味与池。**

- **静态**：`T.alloc_shared` 编译期定尺寸，lowering 成普通 `__shared__` 数组，配 `cta_sync` 保证全块可见（示例与生成 CUDA 见 [tirx_guide/language_reference/cuda/buffers.rst:L133-L165](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/tirx_guide/language_reference/cuda/buffers.rst#L133-L165)）。
- **动态**：`scope="shared.dyn"` 按 launch 定尺寸（launch 参数 `sharedMemBytes`）。关键约束：**一个内核只能有一个动态共享分配（arena）**，其余 buffer 一律用 `T.decl_buffer(data=arena.data, elem_offset=...)` 做视图；两次独立的 `alloc_buffer(scope="shared.dyn")` 是错误（[tirx_guide/language_reference/cuda/buffers.rst:L167-L200](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/tirx_guide/language_reference/cuda/buffers.rst#L167-L200)）。字节数不需要手设：lowering 时 TVM 给设备内核注 `"tirx.use_dyn_shared_memory"` 标签，host launcher 据此把总字节数作为最后一个 launch 参数传给 `cuLaunchKernelEx`（[tirx_guide/language_reference/cuda/buffers.rst:L202-L220](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/tirx_guide/language_reference/cuda/buffers.rst#L202-L220)）。
- **池语法糖**：`T.SMEMPool` 自动做 arena 记账——bump 分配各 buffer 的 offset，免去手工 `decl` 视图；额外提供 per-buffer `align=`、构造 MMA 兼容 swizzle 布局的 `alloc_tcgen05_mma_AB`、以及**回卷光标复用空间**的 `move_base_to`（[tirx_guide/language_reference/cuda/buffers.rst:L222-L237](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/tirx_guide/language_reference/cuda/buffers.rst#L222-L237)）。TMEM 池就叠在一个 SMEMPool 之上（[tirx_guide/language_reference/cuda/buffers.rst:L239](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/tirx_guide/language_reference/cuda/buffers.rst#L239)）。

hgemm_v1 的 SMEM 段正是这套 API 的标准用法（[chapter_intro_tirx/index.md:L109-L116](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_intro_tirx/index.md#L109-L116)）：先 `pool.alloc` 两个控制对象（`tmem_addr` 槽、`mma_bar` 屏障，后者 `align=8` 满足 mbarrier 的对齐要求），`move_base_to(1024)` 把光标回卷到 1024 字节处——控制对象占据的低地址空间从此让位，操作数 tile `Asmem`/`Bsmem`（各 128×64 fp16、挂 128B swizzle layout）从对齐的 1024 偏移开始摆放，最后 `pool.commit()` 定死池尺寸。

**(c) 寄存器、标量与 let——local scope 的三个层次。**

- **寄存器数组**：`T.alloc_local(shape, dtype)` 每线程私有；静态索引的 local 数组通常被标量化（SROA）进寄存器，动态索引或高寄存器压力下可能落到 local memory（[tirx_guide/language_reference/cuda/buffers.rst:L241-L254](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/tirx_guide/language_reference/cuda/buffers.rst#L241-L254)）。生成代码里 `alignas(64)` 的默认对齐对寄存器驻留数组无性能影响——这是文档明说的已知毛边（[tirx_guide/language_reference/cuda/buffers.rst:L263-L275](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/tirx_guide/language_reference/cuda/buffers.rst#L263-L275)）。
- **标量**：就是**单元素寄存器数组的语法糖**。`phase: T.int32 = 0` 与显式 `T.alloc_local((1,), "int32")` + `[0]` 索引**解析为结构完全相同的 TIRx**（`tvm.ir.assert_structural_equal` 通过，打印机甚至会把显式形式渲染回标量形式）；`T.local_scalar` / `T.shared_scalar` / `T.alloc_scalar` 显式选 scope（[tirx_guide/language_reference/cuda/buffers.rst:L277-L312](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/tirx_guide/language_reference/cuda/buffers.rst#L277-L312)）。为什么不用标量类型的 `Var`？因为可变标量必须由一个能反复 store 的一元素 buffer 支撑，而 `Var` 绑定不可变（[tirx_guide/language_reference/cuda/buffers.rst:L314-L321](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/tirx_guide/language_reference/cuda/buffers.rst#L314-L321)）。hgemm_v1 的 `phase_mma: T.int32 = 0`（[chapter_intro_tirx/index.md:L135](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_intro_tirx/index.md#L135)）即此糖——u11-l3 里翻转它的相位正是对这块单元素寄存器反复 store。
- **`T.let`**：**不可变**绑定（单个 `Bind` 节点，不是 buffer），lowering 成普通 C 标量变量；不可变性让算术分析器把值绑定进分析（常量界、整除/对齐集、范围），这些事实**穿透所有使用点**，供索引简化、边界检查消除与对齐/向量化决策使用——可变标量是一次内存 load，分析器无法假设它不变（[tirx_guide/language_reference/cuda/buffers.rst:L323-L356](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/tirx_guide/language_reference/cuda/buffers.rst#L323-L356)）。经验法则由此可得：**派生常量用 `let`，会变的计数器/相位用标量**。

**(d) Tensor memory：唯一不用指针寻址的 scope。** 语言参考的表述非常硬：TMEM 不是普通 scratch scope——必须用 warp-uniform 的 `T.ptx.tcgen05.alloc` / `tcgen05.dealloc` 显式申请释放，每个张量都是用 `T.decl_buffer(..., scope="tmem", allocated_addr=<列>, layout=<tmem布局>)` 声明的**视图**；`allocated_addr`（列偏移）是**强制的**（tensor-core dispatch 会断言它），所以 `T.alloc_buffer(scope="tmem")`——它不设置该参数——**行不通**；TMEM 不可直接寻址，只能经 `tcgen05` 的 `mma`/`ld`/`st`/`cp` 读写（[tirx_guide/language_reference/cuda/buffers.rst:L358-L369](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/tirx_guide/language_reference/cuda/buffers.rst#L358-L369)）。手工序列（alloc 写回 SMEM 槽 → decl 视图 → 使用 → relinquish + dealloc）见 [tirx_guide/language_reference/cuda/buffers.rst:L371-L386](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/tirx_guide/language_reference/cuda/buffers.rst#L371-L386)；`T.TMEMPool` 把 warp-uniform alloc/dealloc、列 bump 分配与 datapath 布局全部包好（[tirx_guide/language_reference/cuda/buffers.rst:L388-L404](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/tirx_guide/language_reference/cuda/buffers.rst#L388-L404)）。

hgemm_v1 完整走了一遍这条纪律：warp 0 执行 `tcgen05.alloc` 把 TMEM 基址写进 SMEM 槽 `tmem_addr`（[chapter_intro_tirx/index.md:L119-L122](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_intro_tirx/index.md#L119-L122)）；随后用 `T.decl_buffer((128, 512), "float32", scope="tmem", allocated_addr=tmem_addr[0], layout=TileLayout(S[(128,512):(1@TLane,1@TCol)]))` 声明累加器视图（[chapter_intro_tirx/index.md:L128-L131](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_intro_tirx/index.md#L128-L131)）——`allocated_addr` 取自那个槽的运行期值；结尾 `relinquish_alloc_permit` + `dealloc` 释放（[chapter_intro_tirx/index.md:L165-L168](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_intro_tirx/index.md#L165-L168)）。注意 `tmem_addr` 本身是 **SMEM 里的 uint32 数组**，不是 TMEM——它是 alloc 结果的「回信信箱」。

**(e) Buffer 方法：清一色编译期元数据操作。** 语言参考把 buffer 的常用方法列表并强调：buffer 是指针之上的元数据，因此这些方法大多是**编译期**的 reshape/重解释/取指针，各自不发射任何运行期操作（[tirx_guide/language_reference/cuda/buffers.rst:L406-L434](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/tirx_guide/language_reference/cuda/buffers.rst#L406-L434)）：

| 方法 | 是什么 |
|---|---|
| `B.data` | 带类型的基指针表达式；打印为 `B_ptr` |
| `B.ptr_to([i, j])` | 指向某元素的类型化指针（`address_of`）；打印为 `&B_ptr[…]` |
| `B.vload([i], dtype=...)` / `B.vstore([i], v)` | 向量化装载/存储；打印为 `*(float4*)(B_ptr + …)` |
| `B.view(*shape, layout=…)` | 同一块存储按新 shape/layout 重解释（不拷贝） |
| `B.local(*shape, layout=…)` | local buffer 中**当前线程私有**的寄存器切片 |
| `B.permute(*dims)` | 轴置换视图（转置布局） |
| `B.access_ptr(mask, …)` | 掩码访问指针（`tvm_access_ptr`），给 intrinsic 传区域 |

hgemm_v1 用了其中两个：`mma_bar.ptr_to([0])`（把屏障元素地址交给 `mbarrier.init`/`commit`/`try_wait`，L121/L149/L151）与 `Dreg.view(128, BLK_N, layout=TileLayout(S[(128,BLK_N):(1@tid_in_wg,1)]))`——把平的每线程寄存器数组重解释成 128 线程的 warpgroup 集体视图（[chapter_intro_tirx/index.md:L154-L158](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_intro_tirx/index.md#L154-L158)）。`B.local()` 则是 tile 原语内部广泛使用的机制（[tirx_guide/language_reference/cuda/buffers.rst:L471-L487](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/tirx_guide/language_reference/cuda/buffers.rst#L471-L487)）。

#### 4.3.4 代码实践

**实践目标**：把「两条声明 API × 五种 scope」用到 hgemm_v1 的每一个 buffer 上，训练一眼识别声明种类的反射。

**操作步骤**（源码阅读型，无需 GPU）：

1. 通读 [chapter_intro_tirx/index.md:L95-L168](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_intro_tirx/index.md#L95-L168)，找出**所有** buffer 名字：`A`、`B`、`D`、`pool`（派生）、`tmem_addr`、`mma_bar`、`Asmem`、`Bsmem`、`tmem`、`Dreg`、`Dreg_f16`、`Dreg_wg`。
2. 对每个 buffer 回答四问并填表：
   - 用哪条 API 声明（签名注解 / `pool.alloc` / `T.decl_buffer` / `T.alloc_local` / `.view`）？
   - scope 是什么（global / shared.dyn / tmem / local）？
   - 存储是谁分配的（调用方 / SMEMPool arena / tcgen05.alloc / 每线程寄存器）？
   - 生命周期由谁管理（内核外 / pool / 显式 dealloc / 随内核）？
3. 单独回答一道辨析题：**为什么 `tmem` 必须用 `T.decl_buffer(..., allocated_addr=...)` 而不能 `T.alloc_buffer(scope="tmem")`？**依据是 [tirx_guide/language_reference/cuda/buffers.rst:L358-L369](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/tirx_guide/language_reference/cuda/buffers.rst#L358-L369) 的哪一句？

**需要观察的现象**：填写过程中会发现 `tmem_addr` 与 `tmem` 的分离——一个在 SMEM（uint32 槽），一个在 TMEM（fp32 视图）；还会发现 `Dreg_wg` 不是新分配，只是 `Dreg` 的元数据换装。

**预期结果**：12 个名字全部能归入四类（参数 buffer / SMEMPool 内的 shared.dyn 视图 / TMEM 视图 / local 寄存器及其视图）；辨析题答案是「`allocated_addr`（列偏移）强制，tensor-core dispatch 会断言它，而 `alloc_buffer` 不设置该参数」。完整参考答案就是第 5 节综合实践的速查表。

#### 4.3.5 小练习与答案

**练习 1**：一个内核里写两次 `T.alloc_buffer(..., scope="shared.dyn")` 会怎样？正确写法是什么？

**答案**：是错误——一个内核**只允许一个**动态共享内存分配（arena）。正确写法是分配一次 arena，其余 buffer 用 `T.decl_buffer(data=arena.data, elem_offset=..., scope="shared.dyn")` 声明为它的偏移视图；或直接用 `T.SMEMPool` 自动记账。

**练习 2**：`phase: T.int32 = 0`（标量）与 `n: T.let = M * K`（let）都能省掉 `[0]` 式索引，二者的本质区别是什么？各适合放什么？

**答案**：标量是**单元素 local buffer 的语法糖**——解析为与 `alloc_local((1,)) + [0]` 结构相同的 TIRx，可反复 store；`let` 是**不可变 Bind**——lowering 成普通 C 标量变量，无数组无 `[0]`。不可变性让算术分析器把常量界/整除集/范围等事实传播穿透所有使用点，供索引简化与向量化决策。适合的用法：会变的计数器、相位用标量；派生常量用 `let`。

**练习 3**：`B.view(64, 4)` 与 `B.permute(1, 0)` 会搬数据吗？它们靠什么改变行为？

**答案**：都不搬——两者都是纯元数据操作，数据指针不变，只改索引算术。`view` 按新 shape 重解释（`A2[tx, j]` → `A_ptr[tx*4+j]`），`permute` 置换轴得到转置布局（`At[i, j]` → `A_ptr[j*4+i]`）。

## 5. 综合实践

**任务：为 hgemm_v1 制作一份「buffer 声明速查表」，并用语言参考逐条核对合法性。**

这是本讲规格中指定的核心实践。产出物是一张表加一页核对记录，做完后可作为你阅读/编写任何 TIRx 内核的模板。

**步骤 1——制表。** 按 4.3.4 的四问，对 hgemm_v1 的全部 buffer 填写：

| buffer | 声明（行号） | API | scope | 分配者 | 备注 |
|---|---|---|---|---|---|
| `A` / `B` / `D` | L96–L100 | 签名注解 `T.Buffer`（等效 `T.match_buffer`） | global | 调用方（PyTorch 张量） | 参数 buffer，fp16 |
| `tmem_addr` | L111 | `pool.alloc((1,), "uint32")` | shared.dyn | SMEMPool arena | TMEM 基址的「回信槽」 |
| `mma_bar` | L112 | `pool.alloc((1,), "uint64", align=8)` | shared.dyn | SMEMPool arena | mbarrier 需 8 字节对齐 |
| `Asmem` / `Bsmem` | L114–L115 | `pool.alloc(..., layout=mma_shared_layout)` | shared.dyn | SMEMPool arena | 128B swizzle 布局挂在 buffer 上 |
| `tmem` | L128–L131 | `T.decl_buffer(..., scope="tmem", allocated_addr=...)` | tmem | `tcgen05.alloc`（L122） | `allocated_addr` 强制；TLane/TCol 布局 |
| `Dreg` / `Dreg_f16` | L154–L155 | `T.alloc_local((BLK_N,), dtype)` | local | 每线程寄存器 | fp32 累加值 / fp16 回写值 |
| `Dreg_wg` | L156–L157 | `Dreg.view(128, BLK_N, layout=...)` | local（视图） | 不分配（元数据） | `tid_in_wg` 集体视图 |

**步骤 2——逐条核对。** 每行到语言参考里找到支持它的原句并抄行号，核对清单至少包含：

1. TMEM 行：`decl_buffer + allocated_addr` 是否满足「tensor-core dispatch 断言 allocated_addr」的强制要求（buffers.rst L361-L369）；
2. SMEM 行：多个 `shared.dyn` buffer 是否全部经由**同一个** pool（即同一个 arena）——是的，`pool.alloc` 内部就是 decl 视图（buffers.rst L167-L200、L222-L237）；
3. local 行：`Dreg` 被静态索引（`Dreg[:]`、view 后按线程切片），符合「静态索引 → SROA 提升寄存器」的条件（buffers.rst L241-L247）;
4. 布局行：`Asmem` 的 swizzle layout 与 `tmem` 的 TLane/TCol layout 都是「layout 参数附加在 buffer 上」这一机制的实例（u10-l1 三个挂载点）。

**步骤 3——验证（有 Blackwell GPU 时）。** 编译并打印两级代码：

```python
# 示例代码：在 u9-l2 验证回路的基础上增加两级检视
kernel = hgemm_v1(128, 128, 64)
kernel.show()                                   # lowering 前的 tile 级 IR
print(ex.mod.imports[0].inspect_source())       # 生成的 CUDA 源
```

在生成 CUDA 中逐一寻找速查表的落点：`extern __shared__` arena、`tmem_addr_ptr`、操作数 tile 的 swizzle 地址计算、每线程 `Dreg` 数组、`tcgen05.alloc/dealloc` 调用。每个找到的落点在表里打勾。

**预期结果**：速查表 7 行全部有语言参考原句支撑；生成 CUDA 的存储种类（global 参数指针 / 动态共享 arena / 寄存器数组 / tcgen05 指令访问的 TMEM）与表的 scope 列一一对应。步骤 3 的实际输出**待本地验证**（本环境无 tvm 与 Blackwell GPU；无 GPU 时步骤 1–2 的纯核对部分可完整完成）。

**步骤 4（进阶，可选）。** 把同一张表套到 GEMM Step 5（[chapter_gemm_async/index.md](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_async/index.md)）上：新增的 `PIPE_DEPTH` 前导维让 `Asmem`/`Bsmem` 变成多 stage 环，逐 stage 的 `tma_bar` 也各占 arena 一格——观察 pool 记账如何随深度增长，并复核 u12-l2 的 SMEM 成本公式。

## 6. 本讲小结

- **四个解析期工具**：`T.meta_var` 把 Python 值内联为编译期 meta、`@T.inline` 在调用点展开函数、`@T.meta_class` 把 buffer/标量打包成解析期状态对象、`T.constexpr` 由 `.specialize` 烘焙；`range` 是串行循环、`T.unroll` 才交给 UnrollLoop pass。
- **dtype 体系**：dtype 说「比特是什么」（含向量形式 `float32x4`，不绑定 `vload`），type 说「是标量、向量还是指针」；`data` 是不可变的指针投影，`map_shared_rank` 这类原始地址必须经 `T.reinterpret` 转成带 scope 的类型化指针才能当 `data=` 用。
- **buffer 是指针之上的元数据**：`addr = data + elem_offset + layout.apply(...)["m"]`，同一逻辑访问只因元数据不同就编译出不同地址算术；两条声明 API——`alloc_buffer` 分配存储、`decl_buffer` 在已有指针上声明视图。
- **五种 scope 各有生命周期**：global 由调用方传入；静态 shared 编译期定尺寸；动态 shared 只允许一个 arena、其余是 `elem_offset` 视图（SMEMPool 自动记账）；local 是每线程寄存器，标量是其单元素糖、`let` 是不可变绑定；**tmem 不能 alloc_buffer**——必须 `decl_buffer` + 强制的 `allocated_addr`，且只能经 tcgen05 指令访问。
- **buffer 方法是编译期元数据操作**：`data`/`ptr_to`/`vload`/`vstore`/`view`/`permute`/`local`/`access_ptr` 都不发射运行时 op；hgemm_v1 的 `mma_bar.ptr_to` 与 `Dreg.view(tid_in_wg)` 是两个最小实例。
- **查证方法论**：拿到任何 TIRx 声明，先归时间轴（解析期/ lowering 期/运行期），再归 API（alloc/decl/view），最后归 scope 与生命周期——本讲的速查表就是这套三步的固化。

## 7. 下一步学习建议

本讲覆盖了语言参考五块中的前三块。下一讲 **u15-l2（TIRx 语言参考：控制流与线程同步）** 补齐后两块——控制流语法与各级同步原语（warp/warpgroup/CTA/cluster 的同步、`elect`、`fence`），正好把 hgemm_v1 里的 `if` 守卫与 `cta_sync` 纳入同一套查证框架。

更远的两条线：

1. **纵向查证**：读 [tirx_guide/arch/lowering_pipeline.rst](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/tirx_guide/arch/lowering_pipeline.rst)（u15-l3 的主题），看本讲的 buffer 元数据如何被 `FlattenBuffer` 等 pass 展平成地址算术。
2. **横向应用**：带着速查表回到 GEMM Step 7（u13-l1）或 FA4（u14 系列），为其中的每个 buffer 重做一遍四问——那两个内核的 pool 布局与 TMEM 列切分远比 hgemm_v1 复杂，正是检验查证能力的试金石。
