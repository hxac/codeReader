# WrapperFunctionBuffer 与 C 缓冲类型

## 1. 本讲目标

在上一单元你已经建立了 controller / executor 的二分心智模型，并知道两者之间所有的跨进程调用都被收敛成一种统一签名——wrapper function「字节进、字节出」。但「字节」到底装在什么容器里、跨进程边界时怎么传递、出错时又怎么表达？本讲就来回答这些问题。

读完本讲，你应该能够：

1. 说出 `orc_rt_WrapperFunctionBuffer` 这个 C 结构体为什么有 **small / large / empty / error** 四种内部状态，以及它们各自的判定条件。
2. 读懂 C 端的 `Init / Allocate / FromRange / FromString / FromOutOfBandError / Dispose / Data / Size` 一整套生命周期函数，并解释 `Dispose` 为什么绝不会泄漏也绝不会误释放。
3. 理解「带外错误（out-of-band error）」的编码原理：为什么同一个缓冲能既装结果又装错误，而无需额外标志位。
4. 会用 C++ 的 RAII 封装 `orc_rt::WrapperFunctionBuffer` 的 `copyFrom / allocate / createOutOfBandError / data / size / release` 等接口，并解释它的「仅移动」语义。

---

## 2. 前置知识

本讲依赖你在 u2-l1 已建立的几个概念，这里只做最短的回顾：

- **controller / executor**：控制端链接 LLVM ORC 库（如 LLJIT），负责编译链接；执行端链接 orc-rt，负责执行 JIT 代码。两者可同进程，也可跨进程。
- **wrapper function**：两端之间的统一调用签名。它不关心字节的具体含义，只负责「把一段字节（参数）送过去，把一段字节（结果）拿回来」。本讲讲的正是装载这段字节的**容器**。
- **C ABI 边界**：跨进程（或跨动态库）传递的数据，必须有一个**布局确定、与 C++ 编译器无关**的表示。因此 orc-rt 把这个容器定义成纯 C 结构体，而不是 C++ 类。这一点是本讲全部设计的出发点。

另外补充两个本讲会用到的通用术语：

- **SBO（Small Buffer Optimization，小缓冲优化）**：一种常见技巧——容器内部预留一小块内联数组，数据短就放在内联数组里（免去 `malloc`），数据长才去堆上分配。`std::string` 的 SSO、`llvm::SmallVector` 都属于同一思想。
- **in-band / out-of-band（带内 / 带外）**：「带内」指和正常数据走同一条通道；「带外」指走一条独立的旁路通道。本讲的「带外错误」就是用一条旁路（一个特殊的缓冲状态）来报告错误，而不是把错误塞进正常结果字节里。

---

## 3. 本讲源码地图

本讲只涉及少数几个文件，但它们是整个 RPC 通信层的地基：

| 文件 | 作用 |
|------|------|
| [`include/orc-rt-c/WrapperFunction.h`](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt-c/WrapperFunction.h) | **C ABI 层**：定义缓冲结构体 `orc_rt_WrapperFunctionBuffer`、wrapper function 的统一签名，以及一整套 `static inline` 的生命周期函数。是本讲的主角。 |
| [`include/orc-rt/WrapperFunction.h`](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/WrapperFunction.h) | **C++ 层**：用 RAII 封装 C 结构体的 `orc_rt::WrapperFunctionBuffer`，以及 `WrapperFunction` 的 `call` / `handle` 工具（后者会在 u5-l2 详讲）。 |
| [`test/unit/SPSWrapperFunctionBufferTest.cpp`](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/test/unit/SPSWrapperFunctionBufferTest.cpp) | **测试**：覆盖 empty / small / big 三种缓冲的序列化往返。是验证我们理解的「试金石」。 |
| [`include/orc-rt/SPSWrapperFunctionBuffer.h`](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/SPSWrapperFunctionBuffer.h) | （支撑）为缓冲定义 SPS 序列化 trait，把「长度 + 原始字节」写进 wire 格式。 |
| [`include/orc-rt-c/CoreTypes.h`](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt-c/CoreTypes.h) | （支撑）定义 `orc_rt_SessionRef` 等不透明引用类型，它们出现在 wrapper function 签名里。 |

---

## 4. 核心概念与源码讲解

### 4.1 C 缓冲的三态与生命周期函数

#### 4.1.1 概念说明

跨进程传递字节，需要一个**布局固定**的容器。orc-rt 选择了纯 C 结构体 `orc_rt_WrapperFunctionBuffer`。它的设计目标有三：

1. **零拷贝友好**：能直接按值传进 wrapper function 的 C 签名里（跨语言、跨进程边界时按值传 struct 是最安全、最可移植的方式）。
2. **短数据免分配**：用 SBO 思路，短数据放内联数组，避免为几个字节就调 `malloc`。
3. **自带错误通道**：同一个容器既能装「正常结果」，又能装「错误」，调用方拿到后第一步就能判断。

它本质上是一个「带带外错误状态的 C 版 SmallVector」——这正是源码注释的原话（见 4.1.3）。

#### 4.1.2 核心流程

缓冲的「状态」完全由两个字段 `Size` 与 `Data` 的取值组合决定。我们可以用一张状态表刻画全部可能：

| 状态 | 判定条件 | 内容存放在哪 | 是否 malloc |
|------|----------|--------------|-------------|
| **empty（空）** | `Size == 0` 且 `Data.ValuePtr == 0` | 无内容 | 否 |
| **small（小）** | `0 < Size ≤ sizeof(char*)` | 内联数组 `Data.Value` 的前 `Size` 字节 | 否 |
| **large（大）** | `Size > sizeof(char*)` | `Data.ValuePtr` 指向的堆内存（前 `Size` 字节） | 是 |
| **error（带外错误）** | `Size == 0` 且 `Data.ValuePtr != 0` | `Data.ValuePtr` 指向一个 `\0` 结尾的错误字符串 | 是 |

> 在 64 位平台上 `sizeof(char*) == 8`，所以「小」的上限是 8 字节。注意 `sizeof(char*)` 是编译期常量，缓冲的 SBO 阈值随平台指针宽度自动伸缩。

围绕这四种状态，C 头文件提供了一组 `static inline` 函数，构成一个微型「生命周期」：

```
       Init ──► empty
        │
        ├──(Allocate / FromRange / FromString)──► small 或 large
        │
        └──(FromOutOfBandError)────────────────► error

       任意状态 ──(Dispose)──► （释放 malloc 内存，结构体可作废）
```

读取则按需分流：`Data()` / `ConstData()` / `Size()` 只对 empty/small/large 合法（对 error 态会 `assert` 失败）；而 `GetOutOfBandError()` 专门用于检测 error 态；`Empty()` 检测是否为 empty。

#### 4.1.3 源码精读

先看结构体本身。它的核心是一个**联合体**，要么是指针、要么是内联字符数组：

[include/orc-rt-c/WrapperFunction.h:27-30](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt-c/WrapperFunction.h#L27-L30) —— 定义 `orc_rt_WrapperFunctionBufferDataUnion`：`ValuePtr`（指针视图）与 `Value`（8 字节内联数组视图）共用同一片内存，这就是 SBO 的物理基础。

接着是结构体本体，源码注释把四种状态讲得非常清楚：

[include/orc-rt-c/WrapperFunction.h:32-52](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt-c/WrapperFunction.h#L32-L52) —— `orc_rt_WrapperFunctionBuffer` 只有两个字段：`Data`（联合体）与 `Size`。注释逐条列出了 small / large / out-of-band error 的判定规则，是本讲最重要的一段说明，务必通读。

`Init` 把缓冲零初始化为 empty 态：

[include/orc-rt-c/WrapperFunction.h:77-81](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt-c/WrapperFunction.h#L77-L81) —— 注意它显式把 `ValuePtr` 置 0。这一步很关键：`Size == 0` 时 `ValuePtr` 必须为 0，否则会被误判成 error 态。

`Allocate` 创建一个**未初始化内容**、指定大小的缓冲，自动落到 small 或 large：

[include/orc-rt-c/WrapperFunction.h:87-96](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt-c/WrapperFunction.h#L87-L96) —— 关键分支：`Size > sizeof(B.Data.Value)` 才 `malloc`，否则复用内联数组。先把 `ValuePtr` 置 0 再判断，保证 `Size==0` 时是 empty 而非 error。

`FromRange` 在 `Allocate` 基础上把源数据 `memcpy` 进来（small 与 large 两条路径都覆盖），并对 `Size==0` 做了保护：

[include/orc-rt-c/WrapperFunction.h:101-114](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt-c/WrapperFunction.h#L101-L114) —— 注意 `else if (Size != 0)`：拷贝空区间时两个分支都不执行，结果就是 empty 态。

`FromString` 复用 `FromRange`，并把字符串长度算成 `strlen + 1`（含 `\0`）：

[include/orc-rt-c/WrapperFunction.h:123-127](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt-c/WrapperFunction.h#L123-L127) —— 注释明确：它拷贝输入字符串，调用方仍需自己释放原 `Source`。

`Dispose` 是「绝不泄漏也绝不误释放」的关键，它的判定条件与「哪些状态做过 malloc」**精确镜像**：

[include/orc-rt-c/WrapperFunction.h:150-154](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt-c/WrapperFunction.h#L150-L154) —— 只在 `Size > sizeof(Value)`（large）**或** `Size == 0 && ValuePtr`（error）时 `free`。empty 与 small 从未 `malloc`，故不 `free`。

读取函数分两类。普通读取对 error 态做了 `assert` 保护：

[include/orc-rt-c/WrapperFunction.h:160-165](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt-c/WrapperFunction.h#L160-L165) —— `Data()` 根据大小返回 `ValuePtr`（large）或 `Value`（small/empty）。

[include/orc-rt-c/WrapperFunction.h:179-184](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt-c/WrapperFunction.h#L179-L184) —— `Size()` 同样 `assert` 非 error 态。这构成一条硬契约：**先判断是否为 error，再取数据/大小**。

最后是空检测与 error 检测这一对互补函数：

[include/orc-rt-c/WrapperFunction.h:190-193](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt-c/WrapperFunction.h#L190-L193) —— `Empty()` 当且仅当 `Size == 0 && ValuePtr == 0`。

[include/orc-rt-c/WrapperFunction.h:202-205](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt-c/WrapperFunction.h#L202-L205) —— `GetOutOfBandError()` 当 `Size == 0` 时返回 `ValuePtr`（即错误字符串），否则返回 0。

#### 4.1.4 代码实践

**实践目标**：在纸上把 C 函数的「输入 → 状态」映射关系走一遍，验证你对四种状态的理解。

**操作步骤**：

1. 打开 [include/orc-rt-c/WrapperFunction.h:87-114](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt-c/WrapperFunction.h#L87-L114)，逐行阅读 `Allocate` 与 `FromRange`。
2. 假设 `sizeof(char*) == 8`，对下列每一次调用，写下调用结束后缓冲的 `Size`、`Data.ValuePtr`（是 0 还是非 0？落在内联数组还是堆？）以及最终状态：

   | 调用 | Size | ValuePtr | 状态 |
   |------|------|----------|------|
   | `Allocate(0)` | ? | ? | ? |
   | `Allocate(4)` | ? | ? | ? |
   | `Allocate(8)` | ? | ? | ? |
   | `Allocate(9)` | ? | ? | ? |
   | `FromRange("foo", 3)` | ? | ? | ? |
   | `FromRange("", 0)` | ? | ? | ? |
   | `FromString("The quick brown fox jumps over the lazy dog")` | ? | ? | ? |

**需要观察的现象 / 预期结果**：`Allocate(8)` 与 `Allocate(9)` 恰好是 small/large 的分水岭——`Allocate(8)` 复用内联数组（`ValuePtr` 仍为 0），`Allocate(9)` 才 `malloc`。`FromRange("", 0)` 应得到 empty 态（两个分支都不执行）。

> 待本地验证：若你在机器上 `printf("%zu", sizeof(char*))` 得到的不是 8（例如 32 位平台为 4），请把阈值与上表对齐重算。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `Allocate` 里要先执行 `B.Data.ValuePtr = 0;`，再去 `if (Size > sizeof(...))` 决定是否 `malloc`？如果去掉这一句，会发生什么？

> **答案**：因为 `Size == 0` 时，缓冲本应是 empty 态（要求 `ValuePtr == 0`）。若不先清零，`ValuePtr` 就是未初始化的垃圾值，`Size == 0 && ValuePtr != 0` 会被误判成 error 态，后续 `Data()`/`Size()` 会触发 `assert`，更糟的是 `Dispose` 会去 `free` 一个野指针。

**练习 2**：一个 `Size == 7` 的缓冲，调用 `Dispose` 时会发生 `free` 吗？为什么？

> **答案**：不会。`Dispose` 的条件是 `Size > sizeof(Value)` 或 `Size == 0 && ValuePtr`。`Size == 7 ≤ 8`，既不满足 large，也不是 error 态（且 small 态 `ValuePtr` 本就是未使用的内联数组），所以不 `free`，结构体可直接作废。

---

### 4.2 带外错误（out-of-band error）编码

#### 4.2.1 概念说明

跨进程调用一定会失败：序列化失败、参数非法、目标资源不存在……调用方拿到结果缓冲时，必须能在**不解析结果字节**的前提下，立刻知道「这是一次成功还是一次失败」。

orc-rt 的做法很巧妙：它不额外加一个 `IsError` 标志位，而是**复用一个本不可能出现的缓冲状态**。回顾 4.1 的状态表，正常的 empty 态要求 `Size == 0 && ValuePtr == 0`；那么 `Size == 0 && ValuePtr != 0` 这一组组合在「正常缓冲」里是**矛盾且不可能**的——于是它被征用为「带外错误」状态，`ValuePtr` 此时指向一段 `\0` 结尾的错误描述字符串。

之所以叫「带外」：正常结果字节走的是「带内」主通道；错误走的是这条由特殊缓冲状态构成的「带外」旁路。两件事复用同一个容器，但走不同判别逻辑。

这种编码带来的直接好处是：通信层（wrapper function 的 C 签名）**完全不需要理解错误格式**，它只搬字节；而判别错误只需一次 `Size == 0 && ValuePtr != 0` 的检查，零额外开销。

#### 4.2.2 核心流程

带外错误在两端的生命周期如下：

```
【执行端 handler 出错】
   make_error / 直接失败
        │ FromOutOfBandError(ErrMsg)
        ▼
   error 态缓冲（Size==0, ValuePtr→字符串）
        │ 经 Return 回调回传
        ▼
【调用端 call 的结果回调】
   第一步：getOutOfBandError() 是否非空？
        ├─ 是 → 包装成 StringError，走错误处理分支（短路，不反序列化）
        └─ 否 → 反序列化结果字节
```

关键点是「短路」：错误一旦在缓冲层被识别，就**不会再尝试反序列化**结果字节——因为错误态下根本没有合法结果字节。这避免了「用错误数据去喂反序列化器」的危险。

#### 4.2.3 源码精读

构造一个 error 态缓冲：

[include/orc-rt-c/WrapperFunction.h:136-144](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt-c/WrapperFunction.h#L136-L144) —— `FromOutOfBandError` 强制 `Size = 0`，并把错误串 `strcpy` 到新 `malloc` 的缓冲里（含 `\0`）。这正是「`Size == 0 && ValuePtr != 0`」的来源。

检测 error 态：

[include/orc-rt-c/WrapperFunction.h:202-205](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt-c/WrapperFunction.h#L202-L205) —— `GetOutOfBandError` 在 `Size == 0` 时返回 `ValuePtr`（错误串），否则返回 0。注意文档说明：缓冲保留字符串所有权，调用方若要长期持有需自行拷贝。

在 C++ 层，`WrapperFunction::handle`（执行端处理入口）拿到参数缓冲后，**第一件事**就是检查带外错误并短路：

[include/orc-rt/WrapperFunction.h:383-384](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/WrapperFunction.h#L383-L384) —— 若 `ArgBytes.getOutOfBandError()` 非空，直接 `Return` 原样回传该错误缓冲，跳过所有反序列化与业务逻辑。这就是「短路」路径。

对称地，`WrapperFunction::call`（调用端）在结果回调里也先查带外错误：

[include/orc-rt/WrapperFunction.h:356-361](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/WrapperFunction.h#L356-L361) —— 若结果是带外错误，把错误串包装成 `make_error<StringError>(ErrMsg)` 交给结果处理器；否则才进入正常反序列化分支。两端都用「先查带外错误」这同一招，形成闭环。

> 关于带外错误与 orc-rt 自身 `Error` 体系的关系：带外错误是**通信层**的错误载体（一段 C 字符串），而 `Error` / `Expected<T>` 是 orc-rt **C++ 内部**的错误类型（u2-l3）。`call` 在边界处用 `make_error<StringError>` 把带外错误串「提升」成内部 `Error`，正是两者衔接的地方。

#### 4.2.4 代码实践

**实践目标**：亲手构造一个带外错误缓冲，并验证它会被正确识别，同时确认它在「数据访问」接口上会触发 `assert`。

**操作步骤**（示例代码，非项目原有代码）：

```cpp
// 示例代码：演示带外错误的构造与判别
#include "orc-rt-c/WrapperFunction.h"
#include <cassert>
#include <cstring>

int main() {
  // 1. 构造一个带外错误缓冲
  orc_rt_WrapperFunctionBuffer B =
      orc_rt_CreateWrapperFunctionBufferFromOutOfBandError("boom: bad args");

  // 2. 它应被识别为 error 态：GetOutOfBandError 返回非空
  const char *Err = orc_rt_WrapperFunctionBufferGetOutOfBandError(&B);
  assert(Err != nullptr);
  assert(strcmp(Err, "boom: bad args") == 0);

  // 3. 它的 Size == 0
  assert(B.Size == 0);
  assert(B.Data.ValuePtr != nullptr);   // Size==0 但 ValuePtr 非 0 → error

  // 4. 释放（会 free 掉那段错误字符串）
  orc_rt_WrapperFunctionBufferDispose(&B);
  return 0;
}
```

**需要观察的现象 / 预期结果**：断言全部通过。若你在第 2 步之后误调用 `orc_rt_WrapperFunctionBufferData(&B)` 或 `orc_rt_WrapperFunctionBufferSize(&B)`，会触发它们内部的 `assert`（"Cannot get data/size for out-of-band error value"）。若用 `WrapperFunction::handle` 包裹一个 error 态缓冲作为 `ArgBytes`，应观察到它**原样回传**、不进入业务 handler。

> 待本地验证：`assert` 在 `NDEBUG`（Release）构建下会被编译掉。若想看到保护效果，请用 Debug 构建（不定义 `NDEBUG`）运行。

#### 4.2.5 小练习与答案

**练习 1**：假如 orc-rt 改为「在结构体里加一个 `bool IsError` 字段」来表达错误，相比现在的带外编码，会有什么缺点？

> **答案**：一是结构体变大、布局变化，破坏 ABI 兼容；二是每次成功调用都要无谓地写/读这个标志位；三是它不再是「复用不可能状态」的零成本哨兵。现在的编码让「成功」路径（empty/small/large）完全不碰错误逻辑，错误逻辑只在 `Size == 0 && ValuePtr != 0` 时触发，开销为零。

**练习 2**：为什么 `GetOutOfBandError` 在文档里强调「缓冲保留字符串所有权」？如果你想在错误回调之外长期保留这条消息，该怎么做？

> **答案**：返回的指针指向缓冲内部 `malloc` 的内存，一旦 `Dispose` 就被释放。要长期保留，应在 `Dispose` 之前用 `strcpy` / `std::string` 把内容拷贝出来。

---

### 4.3 C++ RAII 封装 WrapperFunctionBuffer

#### 4.3.1 概念说明

C 结构体 `orc_rt_WrapperFunctionBuffer` 是 ABI 边界，但直接在 C++ 里手工调用 `Init` / `Dispose` 既啰嗦又容易漏 `Dispose`（尤其在异常或提前 return 时）造成内存泄漏。为此 orc-rt 提供了 C++ 封装类 `orc_rt::WrapperFunctionBuffer`：

- **RAII**：构造即 `Init`，析构即 `Dispose`，资源生命周期与对象绑定。
- **仅移动（move-only）**：拷贝被 `delete`。因为底层 C 结构体可能持有 `malloc` 内存，按值拷贝会导致两个对象指向同一块堆内存，析构时双重释放。移动则把所有权「搬走」，源对象退化为 empty。
- **工厂方法**：`allocate` / `copyFrom` / `createOutOfBandError` 对应 C 端三种构造，返回一个已经拥有资源的对象。
- **`release()`**：在需要把缓冲交还给 C 边界（如调用 `Return` 回调）时，交出所有权、换回原始 C 结构体。

#### 4.3.2 核心流程

封装类的核心是「**始终只让一个对象拥有一份资源**」。三条规则保证了这一点：

```
构造  ──► 持有一份 C 结构体（含可能的 malloc 内存）
拷贝  ──► 编译期禁止（= delete）
移动  ──► swap 搬运：目标拿走资源，源被刷成 empty，全程不拷贝 malloc 内存
析构  ──► Dispose 释放（empty/small 不 free，large/error 才 free）
release ──► swap 出一份 empty，把原始 C 结构体所有权交还调用方（此后本对象不再持有）
```

注意「移动」用的是 `std::swap` 而非拷贝指针。因为 C 结构体是平凡可拷贝的小对象（两个 `size_t` 量级），swap 它的开销可忽略，却天然避免了「两份对象共享一块堆内存」的悬空指针问题。

#### 4.3.3 源码精读

类声明与默认构造（`Init` 到 empty）：

[include/orc-rt/WrapperFunction.h:29-37](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/WrapperFunction.h#L29-L37) —— 默认构造调用 `Init`；另一个 `explicit` 构造直接接管一个已存在的 C 结构体（典型场景：从 wrapper function 的 `ArgBytes` 形参接管）。

拷贝被禁止：

[include/orc-rt/WrapperFunction.h:39-40](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/WrapperFunction.h#L39-L40) —— 拷贝构造与拷贝赋值均 `= delete`。

移动语义（本模块重点）：

[include/orc-rt/WrapperFunction.h:42-52](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/WrapperFunction.h#L42-L52) —— 移动构造先把 `this->B` `Init` 成 empty，再 `swap`：结果是 `this` 拿到对方的资源、对方退化成 empty。移动赋值多一步：先 `Dispose` 释放 `this` 旧资源，再 `Init`、再 `swap`。两种都保证了「搬走而非复制」。

RAII 析构：

[include/orc-rt/WrapperFunction.h:54](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/WrapperFunction.h#L54) —— 析构调用 `Dispose`，large/error 态的 malloc 内存在此释放。

`release()`——把所有权交还 C 边界：

[include/orc-rt/WrapperFunction.h:58-63](https://github.com/llvm/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/WrapperFunction.h#L58-L63) —— 用一个临时 empty 结构体与 `B` swap，返回带走资源的那个。调用后本对象变 empty。这正是把结果交给 `orc_rt_WrapperFunctionReturn` 回调（它按值接收 C 结构体并接管其内存）的标准姿势。

工厂方法三件套：

[include/orc-rt/WrapperFunction.h:80-94](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/WrapperFunction.h#L80-L94) —— `allocate(Size)`（未初始化内容）、`copyFrom(Source, Size)`（按区间拷贝）、`copyFrom(Source)`（拷贝 C 字符串含 `\0`），各自委托对应的 C 构造函数，并把返回的 C 结构体包进 RAII 对象。

错误工厂与错误检测：

[include/orc-rt/WrapperFunction.h:97-106](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/WrapperFunction.h#L97-L106) —— `createOutOfBandError(Msg)` 与 `getOutOfBandError()` 是 C++ 侧访问 4.2 所讲带外错误的入口。

数据访问：

[include/orc-rt/WrapperFunction.h:66-76](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/WrapperFunction.h#L66-L76) —— `data()`（const/非 const 两个重载）、`size()`、`empty()` 全部一行委托给 C 函数，因此同样继承「error 态访问会 `assert`」的契约。

最后看测试如何用这套封装做往返。注意 `SmallBuffer` 用了短串 `"foo"`（走 small），`BigBuffer` 用了长串（走 large），正好覆盖两条路径：

[test/unit/SPSWrapperFunctionBufferTest.cpp:27-42](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/test/unit/SPSWrapperFunctionBufferTest.cpp#L27-L42) —— 三个用例分别对 empty / small / big 调用 `blobSerializationRoundTrip<SPSWrapperFunctionBuffer>`，并用 `memcmp` 比较原始与往返后的缓冲。这正是 4.1 状态表在测试层的印证。

> 顺带一提，SPS 序列化 trait（[SPSWrapperFunctionBuffer.h:37-43](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/SPSWrapperFunctionBuffer.h#L37-L43)）反序列化时用的是 `WrapperFunctionBuffer::allocate(Size)`——它**不关心**数据是 small 还是 large，把分流逻辑完全交给了底层 C 函数。这也解释了为什么 RAII 封装只需暴露 `allocate`，无需暴露 `small`/`large` 的区分接口。

#### 4.3.4 代码实践

**实践目标**：用 C++ 封装走通「拷贝构造缓冲 → 取数据 → 手动 release/dispose」的 small 与 large 两条路径，确认无内存泄漏。

**操作步骤**（示例代码，非项目原有代码）：

```cpp
// 示例代码：用 RAII 封装验证 small 与 large 两条路径的资源管理
#include "orc-rt/WrapperFunction.h"
#include <cassert>
#include <cstring>

using namespace orc_rt;

static void exerciseCopyFrom(const char *Src) {
  // copyFrom 内部会自动选 small 或 large
  WrapperFunctionBuffer B = WrapperFunctionBuffer::copyFrom(Src);
  assert(B.size() == std::strlen(Src) + 1);          // 含 '\0'
  assert(std::memcmp(B.data(), Src, B.size()) == 0);

  // 方式 A：什么都不做，离开作用域时析构自动 Dispose（RAII）
}

int main() {
  // small 路径：3 字节字符串（+ '\0' = 4 ≤ 8）
  exerciseCopyFrom("foo");

  // large 路径：长字符串（+ '\0' > 8）
  exerciseCopyFrom("The quick brown fox jumps over the lazy dog");

  // 方式 B：手动 release，把所有权交给 C 边界，再手工 Dispose
  WrapperFunctionBuffer B =
      WrapperFunctionBuffer::copyFrom("handoff to C boundary");
  orc_rt_WrapperFunctionBuffer Raw = B.release();     // B 退化为 empty
  assert(B.empty());
  assert(Raw.Size > 0);
  orc_rt_WrapperFunctionBufferDispose(&Raw);          // 调用方负责释放
  return 0;
}
```

**需要观察的现象 / 预期结果**：

1. `exerciseCopyFrom` 两次调用——短串走 small、长串走 large——`size()` 与 `memcmp` 断言都应通过，证明两条路径都能正确取到数据。
2. 方式 A 验证 RAII：离开作用域自动释放，无需手写 `Dispose`。
3. 方式 B 验证 `release`：调用后 `B.empty()` 为真，且原数据完整转移到 `Raw`，由你显式 `Dispose`。

> 待本地验证：建议用 AddressSanitizer 构建（`-fsanitize=address`）运行此程序，确认「无 leak」与「无 double-free」。特别地，若你删掉方式 B 末尾的 `Dispose`，ASan 应报一处 large 态的内存泄漏。

#### 4.3.5 小练习与答案

**练习 1**：`WrapperFunctionBuffer` 为什么禁止拷贝、却允许移动？如果允许拷贝会出什么问题？

> **答案**：底层 C 结构体可能持有 `malloc` 内存（large/error 态）。若允许拷贝，两个 C++ 对象会指向同一块堆内存，各自析构都 `Dispose`→`free`，导致双重释放。移动用 `swap` 转移所有权，源对象退化成 empty（无 malloc 内存），始终只有一份所有者。

**练习 2**：阅读移动赋值运算符（[第 47-52 行](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/WrapperFunction.h#L47-L52)），它为什么必须先 `Dispose(&B)` 再 `Init(&B)`，而不能直接 `swap`？

> **答案**：移动赋值前，`this` 可能已经持有一份资源（如 large 态的 malloc 内存）。若直接 `swap`，旧资源会被搬到 `Other` 里，等 `Other` 析构时才释放——这在 `Other` 是临时对象时勉强能用，但语义混乱、且若 `Other` 寿命更长就会延迟释放甚至泄漏。先 `Dispose` 显式释放本对象的旧资源，再 `Init` 重置成 empty，最后 `swap` 接管新资源，语义清晰且无悬空。

**练习 3**：`release()` 返回后，原来的 `WrapperFunctionBuffer` 对象处于什么状态？可以继续安全使用吗？

> **答案**：处于 empty 态（`size()==0`、`empty()==true`）。可以安全使用——你可以再给它赋一个新缓冲，也可以让它自然析构（empty 态 `Dispose` 不 `free` 任何东西）。`release` 的契约是「调用方拿走原始 C 结构体的所有权并负责释放它」。

---

## 5. 综合实践

把三个模块串起来，完成一个「缓冲状态巡检器」小任务：

**任务**：写一个函数 `describe(const orc_rt_WrapperFunctionBuffer &B)`，返回一个字符串描述缓冲当前处于 empty / small / large / error 哪一种状态。然后构造四个分别落入这四种状态的缓冲，依次打印它们的描述，最后全部 `Dispose`。

**要求**：

1. empty 态：用 `orc_rt_WrapperFunctionBufferInit`。
2. small 态：用 `orc_rt_CreateWrapperFunctionBufferFromRange("abcd", 4)`（≤ `sizeof(char*)`）。
3. large 态：用 `orc_rt_CreateWrapperFunctionBufferFromRange` 传入一段长度 > `sizeof(char*)` 的数据。
4. error 态：用 `orc_rt_CreateWrapperFunctionBufferFromOutOfBandError("synthetic error")`。
5. `describe` 内部应优先用 `GetOutOfBandError` 判 error，再用 `Empty` 判 empty，再依据 `Size` 与 `sizeof(B.Data.Value)` 的比较区分 small / large。

**参考实现骨架**（示例代码，非项目原有代码）：

```cpp
// 示例代码：状态巡检器
#include "orc-rt-c/WrapperFunction.h"
#include <cstdio>

const char *describe(const orc_rt_WrapperFunctionBuffer &B) {
  if (orc_rt_WrapperFunctionBufferGetOutOfBandError(&B))
    return "error";                                    // 优先判 error
  if (orc_rt_WrapperFunctionBufferEmpty(&B))
    return "empty";
  return B.Size > sizeof(B.Data.Value) ? "large" : "small";
}

int main() {
  orc_rt_WrapperFunctionBuffer Bs[4];

  // empty
  orc_rt_WrapperFunctionBufferInit(&Bs[0]);
  // small
  Bs[1] = orc_rt_CreateWrapperFunctionBufferFromRange("abcd", 4);
  // large
  const char *Big = "0123456789ABCDEF";                // 16 字节 > 8
  Bs[2] = orc_rt_CreateWrapperFunctionBufferFromRange(Big, 16);
  // error
  Bs[3] = orc_rt_CreateWrapperFunctionBufferFromOutOfBandError("synthetic error");

  for (int i = 0; i < 4; ++i)
    std::printf("Bs[%d] = %s\n", i, describe(Bs[i]));

  for (int i = 0; i < 4; ++i)
    orc_rt_WrapperFunctionBufferDispose(&Bs[i]);       // 仅 large/error 会 free
  return 0;
}
```

**预期结果**：依次输出 `empty / small / large / error`。用 ASan 运行应无泄漏、无 double-free。完成本任务后，你就把本讲的「三态 + 带外错误 + 生命周期」全部跑通了一遍。

---

## 6. 本讲小结

- `orc_rt_WrapperFunctionBuffer` 是跨进程传递字节的 C ABI 容器，由 `Data`（指针/内联数组联合体）与 `Size` 两字段刻画，状态共有 empty / small / large / error 四种。
- 它用 SBO 思想：`Size ≤ sizeof(char*)` 时复用内联数组（免 `malloc`），否则才堆分配。`Allocate` / `FromRange` / `FromString` 的分支逻辑正是这条阈值的体现。
- 带外错误复用了「本不可能」的 `Size == 0 && ValuePtr != 0` 组合，零额外字段、零成功路径开销，使通信层只需搬字节、不解释错误格式。
- `Dispose` 的释放条件 `Size > sizeof(Value) || (Size==0 && ValuePtr)` 与「做过 malloc 的状态」精确镜像，因此 empty/small 不释放、large/error 才释放，杜绝泄漏与误释放。
- 数据访问接口（`Data` / `Size`）对 error 态会 `assert`，硬契约是「先 `GetOutOfBandError` 判错，再取数据」；C++ 层 `call`/`handle` 正是据此短路。
- C++ 封装 `orc_rt::WrapperFunctionBuffer` 提供仅移动的 RAII 所有权，用 `swap` 转移资源避免双重释放，`release()` 用于把所有权交还给 C 边界（如 `Return` 回调）。

---

## 7. 下一步学习建议

本讲只讲了「字节容器」本身，还没有讲「字节怎么被赋予类型含义」。接下来的讲义：

- **u5-l2 Wrapper Function 签名与 call/handle**：讲解 `orc_rt_WrapperFunction` 这个统一 C 签名如何被 `orc_rt::WrapperFunction::call` / `handle` 包装成「序列化 → 调用 → 反序列化」的完整流程，并引入 `AsyncMethod` / `SyncMethod` 适配器。本讲的带外错误短路正是其中第一步。
- **u6-1 Simple Packed Serialization 原理**：讲解容器里那串字节到底是什么格式（SPS wire 格式），届时你会理解 [SPSWrapperFunctionBuffer.h](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/SPSWrapperFunctionBuffer.h) 里 `size + 原始字节` 的写法从何而来。
- 建议同时翻阅 [docs/Design.md](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/docs/Design.md) 的 `WrapperFunction` 小节，把本讲的容器放回「controller↔executor RPC」的大图里再确认一遍。
