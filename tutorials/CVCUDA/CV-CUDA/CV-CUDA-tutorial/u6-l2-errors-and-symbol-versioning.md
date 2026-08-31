# 错误处理与符号版本：Status、Exception 与 ABI 兼容

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 CV-CUDA 中错误的「双轨制」：C 世界用 `NVCVStatus` 返回码 + 线程局部错误消息，C++ 世界用 `nvcv::Exception` 异常。
2. 逐行读懂 `nvcv::ProtectCall` 如何把任何 C++ 异常翻译成 `NVCVStatus` 错误码，并理解标准库异常到错误码的映射表。
3. 跟踪 `cvcudaFlipSubmit` 从非法参数到返回错误码的完整路径（三道防线）。
4. 解释 `CVCUDA_DEFINE_API` 宏如何通过 ELF 符号版本（symver）机制保证「库升级不破坏老程序」的 ABI 兼容。
5. 分别用纯 C 和 C++ 写出符合本仓库契约的错误处理代码。

## 2. 前置知识

### 2.1 C ABI：异常不能穿越的边界

`extern "C"` 函数构成一道 **C ABI（应用二进制接口）边界**。C++ 异常的展开（stack unwinding）依赖编译器与运行时库的内部约定，跨过 `extern "C"` 边界抛出异常是未定义行为——对面的 C 调用者根本不知道如何捕获它。所以任何「C++ 内核 + C 接口」的库都必须在边界处做**翻译**：异常进去，错误码出来。CV-CUDA 的整个 C API（`cvcudaFlipCreate` 等 61 组算子函数）都遵守这条纪律。

### 2.2 errno 模式与线程局部存储（TLS）

返回码只能携带一个整数，详细原因去哪里找？经典做法是 C 标准库的 `errno`：错误发生时把详细信息写进一个**线程局部**变量，调用者随后用配套函数读取。线程局部意味着多线程各自看到各自的错误，互不覆盖。CV-CUDA 的 `nvcvGetLastErrorMessage` 就是这个模式的翻版，底层是 C++11 的 `thread_local` 变量。

### 2.3 ELF 符号版本（symver）

Linux 动态库（ELF 格式）允许给同一个符号名挂多个**版本标签**，例如 `cvcudaFlipCreate@CVCUDA_0.2` 与 `cvcudaFlipCreate@CVCUDA_0.8`。链接时程序会记住自己用的是哪个版本，运行时动态链接器按版本精确匹配。这就是 glibc 能同时服务几十年前编译的老程序的原因。理解 `@@`（默认版本）与 `@`（非默认版本）的区别是本讲第四模块的关键。

### 2.4 与前讲的衔接

u6-l1 讲过每个算子有 `.h`（C 接口）与 `.hpp`（C++ 类）两份等价定义、C ABI 是异常的界碑。本讲把这句话展开成完整的机制剖析：界碑两侧各是什么、翻译如何发生、以及这道界碑本身如何做到十年不裂（符号版本）。

## 3. 本讲源码地图

| 文件 | 职责 |
|------|------|
| [src/nvcv/src/include/nvcv/Status.h](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/include/nvcv/Status.h) | C 侧错误码枚举 `NVCVStatus` 与「最后错误」读写函数声明 |
| [src/nvcv/src/include/nvcv/Status.hpp](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/include/nvcv/Status.hpp) | C++ 侧 `enum class Status`，与 C 枚举数值一一对应 |
| [src/nvcv/src/include/nvcv/Exception.hpp](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/include/nvcv/Exception.hpp) | 公开异常类 `nvcv::Exception`、`SetThreadError` 与 `ProtectCall` |
| [src/nvcv/src/priv/TLS.hpp](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/priv/TLS.hpp) / [TLS.cpp](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/priv/TLS.cpp) | 线程局部存储 `CoreTLS` 的定义与访问入口 |
| [src/nvcv/src/priv/Status.cpp](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/priv/Status.cpp) | 错误写入/读出 TLS 的真正实现，含异常→错误码映射表 |
| [src/cvcuda/OpFlip.cpp](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/OpFlip.cpp) | Flip 的 C API 实现，`CVCUDA_DEFINE_API` + `ProtectCall` 的标准样板 |
| [src/cvcuda/Operator.cpp](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/Operator.cpp) | `nvcvOperatorDestroy` 通用销毁入口 |
| [src/cvcuda/priv/IOperator.hpp](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/IOperator.hpp) / [IOperator.cpp](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/IOperator.cpp) | 句柄校验：`ToDynamicRef` 的 NULL/类型/版本三重检查 |
| [src/cvcuda/priv/SymbolVersioning.hpp](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/SymbolVersioning.hpp) | `CVCUDA_DEFINE_API` 宏的转发定义 |
| [src/nvcv/util/SymbolVersioning.hpp](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/util/SymbolVersioning.hpp) | 符号版本宏的本体，含完整机制注释文档 |
| [src/nvcv/src/include/nvcv/Version.h](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/include/nvcv/Version.h) | `nvcvGetVersion` 运行时版本查询 |

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：错误码双轨制、异常翻译边界、Flip 错误路径实战、符号版本机制。

### 4.1 错误码双轨制：NVCVStatus 与线程局部「最后错误」

#### 4.1.1 概念说明

CV-CUDA 的错误信息分两层：

- **返回码**：一个整数枚举 `NVCVStatus`，随每个 C 函数返回，告诉调用者「成功还是失败、失败的大类」。它必须足够小、足够稳定——因为它是 ABI 的一部分。
- **最后错误（last error）**：一段人类可读的字符串 + 状态码，存在**每个线程自己的**存储槽里。返回码说「参数非法」，最后错误说「参数非法，因为 Input must be cuda-accessible, pitch-linear tensor」。

C++ 侧则有一个 `enum class nvcv::Status`，名字去掉 `NVCV_` 前缀，但数值与 C 枚举完全相同——同一套错误码的两种拼写。

#### 4.1.2 核心流程

```text
C 函数失败
   ├── 返回 NVCV_ERROR_XXX            （调用者立刻可判断）
   └── 同时写当前线程的 TLS 槽
            lastErrorStatus  = 错误码
            lastErrorMessage = 详细消息（最长 255 字节 + '\0'）

调用者随后二选一：
   nvcvGetLastErrorMessage(buf, n)   → 读取并【重置】为 SUCCESS（下次再读就是 SUCCESS）
   nvcvPeekAtLastErrorMessage(buf,n) → 只读【不重置】（可反复查看同一错误）
```

读取函数本身也返回 `NVCVStatus`，形成「读取上次错误」也报告状态的递归式设计。

#### 4.1.3 源码精读

**错误码枚举**——全部 12 个值，从 0 开始隐式编号：

- [src/nvcv/src/include/nvcv/Status.h:L48-L62](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/include/nvcv/Status.h#L48-L62)：定义 `NVCV_SUCCESS = 0`、`NVCV_ERROR_NOT_IMPLEMENTED = 1`、`NVCV_ERROR_INVALID_ARGUMENT = 2`……直到 `NVCV_ERROR_UNDERFLOW = 11`。注意枚举没有显式赋值，靠声明顺序编号，所以**新增错误码只能加在末尾**——插队会改变后续所有值，直接破坏 ABI。

- [src/nvcv/src/include/nvcv/Status.h:L71-L71](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/include/nvcv/Status.h#L71-L71)：`NVCV_MAX_STATUS_MESSAGE_LENGTH` 定为 256，是错误消息缓冲的硬上限。

**四个读取/写入函数的契约**：

- [src/nvcv/src/include/nvcv/Status.h:L85-L111](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/include/nvcv/Status.h#L85-L111)：`nvcvGetLastError` 与 `nvcvGetLastErrorMessage`——**读取并重置**当前线程的状态；再次调用返回 `NVCV_SUCCESS`。
- [src/nvcv/src/include/nvcv/Status.h:L113-L138](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/include/nvcv/Status.h#L113-L138)：`nvcvPeekAtLastError` 与 `nvcvPeekAtLastErrorMessage`——只看不重置。
- [src/nvcv/src/include/nvcv/Status.h:L141-L168](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/include/nvcv/Status.h#L141-L168)：`nvcvSetThreadStatus`——给语言绑定（如 Python 扩展）注入状态用的反向入口，接受 printf 风格格式串。

**TLS 存储槽本体**：

- [src/nvcv/src/priv/TLS.hpp:L28-L31](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/priv/TLS.hpp#L28-L31)：`CoreTLS` 结构体的头两个字段就是 `lastErrorStatus` 与 256 字节的 `lastErrorMessage` 数组。整个结构体故意用**平坦定长数组**（注释注明：C API 的字符串返回助手需要稳定存储），后面那一长串 `bufColorSpecName` 等是各类格式名字的复用缓冲，与本讲无关但同属一个 TLS 块。
- [src/nvcv/src/priv/TLS.cpp:L23-L33](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/priv/TLS.cpp#L23-L33)：`thread_local CoreTLS tls` 一个关键词实现「每线程一份」，`GetCoreTLS()` 是唯一入口。

**Get 与 Peek 的差异实现**：

- [src/nvcv/src/priv/Status.cpp:L120-L127](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/priv/Status.cpp#L120-L127)：`GetLastThreadError` 先 Peek 再调用 `SetThreadError(std::exception_ptr{})`——传空指针时 `SetThreadError` 走「置 SUCCESS、写 "success"」分支，等效于重置。
- [src/nvcv/src/priv/Status.cpp:L129-L137](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/priv/Status.cpp#L129-L137)：`PeekAtLastThreadError` 只拷贝消息、返回状态，完全不碰存储。

**C++ 侧的镜像枚举**：

- [src/nvcv/src/include/nvcv/Status.hpp:L42-L56](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/include/nvcv/Status.hpp#L42-L56)：`enum class Status : int8_t`，每个成员直接用 C 枚举赋值（`ERROR_INVALID_ARGUMENT = NVCV_ERROR_INVALID_ARGUMENT`），保证两轨永远同值。

#### 4.1.4 代码实践

**实践目标**：亲手观察「读取即重置」语义。

**操作步骤**（以下为示例代码，需在装有 cvcuda 的环境编译运行，结果待本地验证）：

1. 写一个 C 程序，连续调用两次 `nvcvGetLastErrorMessage`：

```c
/* 示例代码：peek_vs_get.c */
#include <nvcv/Status.h>
#include <stdio.h>

int main(void)
{
    /* 此处应先触发一次错误（见 4.3.4），这里假设 TLS 里已有错误 */
    char buf[NVCV_MAX_STATUS_MESSAGE_LENGTH];

    NVCVStatus s1 = nvcvGetLastErrorMessage(buf, sizeof(buf));
    printf("第 1 次 Get: %d (%s) msg=%s\n", s1, nvcvStatusGetName(s1), buf);

    NVCVStatus s2 = nvcvGetLastErrorMessage(buf, sizeof(buf));
    printf("第 2 次 Get: %d (%s) msg=%s\n", s2, nvcvStatusGetName(s2), buf);
    return 0;
}
```

2. 编译：`gcc peek_vs_get.c -I<安装前缀>/include -L<安装前缀>/lib -lnvcv_types -o peek_vs_get`（链接库名以构建产物为准，待本地确认）。

**需要观察的现象**：第 1 次 Get 返回真实错误码与消息；第 2 次 Get 返回 `NVCV_SUCCESS` 且消息变成 `success`。

**预期结果**：证明 Get 是破坏性读取——所以生产代码要么用 Peek（可重复查询），要么保证错误只处理一次。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `NVCVStatus` 枚举新增错误码只能追加在末尾，不能按语义插入中间？

**答案**：枚举靠声明顺序隐式编号，插入会改变其后所有枚举值对应的整数。已编译的老程序（比如判断 `status == 4` 表示 DEVICE）在换新库后语义会整体错位，属于 ABI 破坏。追加在末尾则老数值不变。

**练习 2**：两个线程同时失败，线程 A 调用 `nvcvGetLastError` 会读到线程 B 的错误吗？

**答案**：不会。存储在 `thread_local CoreTLS` 中，每线程一份（[TLS.cpp:L23-L27](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/priv/TLS.cpp#L23-L27)），线程 A 只能读到本线程最后写入的错误。

### 4.2 异常翻译边界：ProtectCall 与 SetThreadError

#### 4.2.1 概念说明

库内部（priv 层、legacy 内核包装层）全程用 C++ 异常报告错误——构造方便、能携带格式化消息、RAII 自动清理。但异常绝不能穿越 C ABI。`nvcv::ProtectCall` 就是边界上的**单向翻译阀**：把 lambda 里抛出的任何异常，按类型查表翻译成 `NVCVStatus`，写进线程局部存储，再作为返回值交还给 C 调用者。整个 CV-CUDA 的每个 C API 函数体都是同一个模式：

```cpp
CVCUDA_DEFINE_API(0, 2, NVCVStatus, cvcudaFlipSubmit, (参数表))
{
    return nvcv::ProtectCall([&]{ /* 正常逻辑，随便抛异常 */ });
}
```

还有一个反向细节：公开异常类 `nvcv::Exception` 的构造函数**在抛出的同时**就顺手把状态写进了 TLS——这样即使某个上层 C++ 调用者捕获了异常、不让它走到 C 边界，线程错误状态也已经被记录在案。

#### 4.2.2 核心流程

```text
              C++ 异常世界                    C 返回码世界
        ┌──────────────────────────┐   ┌─────────────────────┐
        │  priv 层 throw Exception  │   │                     │
        │           │ 跨越边界      │   │                     │
        │           ▼              │   │                     │
        │  ProtectCall: catch(...) │──►│  写 TLS: 码 + 消息   │
        │  SetThreadError 按类型映射│   │  return NVCVStatus  │
        └──────────────────────────┘   └─────────────────────┘

异常类型 → 错误码映射表：
  nvcv::Exception        → 自带 code()（精确）
  std::invalid_argument  → NVCV_ERROR_INVALID_ARGUMENT
  std::bad_alloc         → NVCV_ERROR_OUT_OF_MEMORY
  其余 std 异常           → NVCV_ERROR_INTERNAL
  未知异常 (...)          → NVCV_ERROR_INTERNAL ("Unexpected error")
```

#### 4.2.3 源码精读

**ProtectCall——整个仓库最常被复用的 14 行**：

- [src/nvcv/src/include/nvcv/Exception.hpp:L243-L256](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/include/nvcv/Exception.hpp#L243-L256)：执行 `fn()`，无异常返回 `NVCV_SUCCESS`；有异常则 `SetThreadError(std::current_exception())` 把异常捕获进 `exception_ptr` 交给映射函数，最后用 `nvcvPeekAtLastError()`（注意是 **Peek** 不是 Get，避免把刚写入的错误读没了）取出翻译结果返回。

**映射表 SetThreadError（公开版）**：

- [src/nvcv/src/include/nvcv/Exception.hpp:L176-L229](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/include/nvcv/Exception.hpp#L176-L229)：先 `rethrow_exception` 恢复原始异常，再按从具体到一般的顺序 `catch`：`nvcv::Exception` 用自带码；`std::invalid_argument` 映射为 INVALID_ARGUMENT；`std::bad_alloc` 映射为 OUT_OF_MEMORY 并附固定消息；domain/length/out_of_range/range/overflow/underflow 全部归入 INTERNAL；兜底 `catch (...)` 归入 INTERNAL + "Unexpected error"。**顺序很重要**：`nvcv::Exception` 必须排在 `std::exception` 系之前，否则会被更宽的 catch 抢先。

**异常构造即记账**：

- [src/nvcv/src/include/nvcv/Exception.hpp:L75-L81](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/include/nvcv/Exception.hpp#L75-L81)：公开构造函数在格式化完消息后立刻调用 `nvcvSetThreadStatus(...)` 写 TLS。抛异常这个动作本身完成记账。
- [src/nvcv/src/include/nvcv/Exception.hpp:L139-L145](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/include/nvcv/Exception.hpp#L139-L145)：带 `InternalCtorTag` 的私有构造函数**不写** TLS，注释写明用途：把 C 状态码反向转成 C++ 异常时使用（避免无意义的重复记账）。本仓库实际翻译主力是 priv 层版本（见下），公开版的 `SetThreadError` 主要服务头文件使用者。

**priv 层的孪生实现（真正写 TLS 的地方）**：

- [src/nvcv/src/priv/Status.cpp:L33-L108](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/priv/Status.cpp#L33-L108)：与公开版结构相同，但直接操作 `CoreTLS`。特别注意 [L51-L55](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/priv/Status.cpp#L51-L55)：若捕到**公开** `nvcv::Exception`，会把状态记为 INTERNAL 并断言失败——库内部实现约定只抛 priv 异常，公开异常出现在这里说明有代码违反了分层纪律。
- [src/nvcv/src/priv/Status.hpp:L38-L51](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/priv/Status.hpp#L38-L51)：priv 版 `ProtectCall`，与公开版逐行同构。

**错误码名字查询**：

- [src/nvcv/src/priv/Status.cpp:L139-L166](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/priv/Status.cpp#L139-L166)：`GetName` 用 `CASE` 宏把枚举值转成字符串，**故意不写 default 分支**——这样编译器在新增枚举值却忘记更新此表时会发出警告，一个小而精的防御式设计。

#### 4.2.4 代码实践

**实践目标**：用 Python 3 分钟验证「异常被翻译成返回码 + 消息可读取」这件事在绑定层同样成立（Python 侧最终呈现为 `RuntimeError`）。

**操作步骤**：

1. 在装有 cvcuda 的 Python 环境中运行（示例代码）：

```python
import cvcuda
import numpy as np

t = cvcuda.Tensor((1, 8, 8, 3), np.uint8, "NHWC")  # 正常 GPU 张量
try:
    out = cvcuda.resize(t, (4, 4), interp=cvcuda.Interp.LINEAR)
    print("成功:", out.shape)
except Exception as e:
    print("异常类型:", type(e).__name__)
    print("消息:", e)
```

2. 把输入张量换成 `interp=cvcuda.Interp.AREA` 之外的非法枚举值、或制造 1x1 输入的 LINEAR resize（回顾 u3-l1：LINEAR 要求源图至少 2x2），观察报错。

**需要观察的现象**：Python 抛出的异常消息开头形如 `INVALID_ARGUMENT: ...`，与 `nvcv::Exception` 的 `what()` 格式（状态名 + 冒号 + 消息，见 [Exception.hpp:L147-L165](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/include/nvcv/Exception.hpp#L147-L165) 的 `doSetMessage`，它先写 `"%s: "` 状态名再拼消息）完全一致——说明 Python 端看到的字符串正是 C++ 异常消息原样透传。

**预期结果**：错误消息以状态名前缀开头，证明一条链：priv 抛 `nvcv::Exception` → ProtectCall 记 TLS → pybind11 捕获 → Python 异常。

#### 4.2.5 小练习与答案

**练习 1**：`ProtectCall` 的 catch 分支为什么用 `nvcvPeekAtLastError()` 而不是 `nvcvGetLastError()` 取返回值？

**答案**：`SetThreadError` 刚把错误写进 TLS，随后用 Peek 只读取出；若用 Get 会先把状态重置成 SUCCESS，第二次读就取不到刚写入的错误了（Get 是破坏性读取，见 4.1.3）。

**练习 2**：一个 priv 层函数抛出 `std::bad_alloc`，C 调用者拿到的返回码是什么？消息是什么？

**答案**：`NVCV_ERROR_OUT_OF_MEMORY`（= 7），消息为固定字符串 "Not enough space for resource allocation"，见 [Exception.hpp:L209-L212](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/include/nvcv/Exception.hpp#L209-L212)。

**练习 3**：库内部为什么禁止抛公开的 `nvcv::Exception`、要求用 priv 版？

**答案**：公开异常的构造函数会调用 `nvcvSetThreadStatus`（它本身是版本化 C API），在翻译流程内部再触发一次 C API 调用与 TLS 写入会造成重复/错乱记账；priv 版异常只格式化消息，记账统一交给边界的 `SetThreadError`。[priv/Status.cpp:L51-L55](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/priv/Status.cpp#L51-L55) 用断言强制这条纪律。

### 4.3 实战错误路径：cvcudaFlipSubmit 的三道防线

#### 4.3.1 概念说明

知道翻译机制后，本模块回答一个具体问题：当 C 调用者犯了一个错（传 NULL 句柄、传不支持的 dtype），错误在库内部走哪条路、最终返回什么码？以 `cvcudaFlipSubmit` 为例，错误要闯过**三道防线**，每道防线抛不同异常、对应不同错误码。读懂这条路径，你就掌握了排查任何 cvcuda C API 报错的地图。

#### 4.3.2 核心流程

```text
cvcudaFlipSubmit(handle, stream, in, out, flipCode)
    │
    ├─ 第一道：句柄校验（priv::ToDynamicRef）
    │    handle == NULL            → ERROR_INVALID_ARGUMENT ("Handle cannot be NULL")
    │    handle 类型不对/已销毁     → ERROR_NOT_COMPATIBLE
    │    句柄 ABI 版本不匹配        → ERROR_NOT_COMPATIBLE (含版本号消息)
    │
    ├─ 第二道：数据视图导出（exportData，u5-l2 讲过）
    │    张量不是 CUDA 可访问的     → ERROR_INVALID_ARGUMENT
    │                                  ("Input must be cuda-accessible, pitch-linear tensor")
    │
    └─ 第三道：legacy 内核参数校验（Flip::infer 返回 ErrorCode）
         dtype 不支持（如 F16）     → ErrorCode::INVALID_DATA_TYPE
         布局不支持                 → ErrorCode::INVALID_DATA_FORMAT
              │
              └─ NVCV_CHECK_THROW → TranslateError
                   INVALID_PARAMETER/FORMAT/SHAPE/TYPE → NVCV_ERROR_INVALID_ARGUMENT
                   其余                              → NVCV_ERROR_INTERNAL
```

#### 4.3.3 源码精读

**C API 标准样板**：

- [src/cvcuda/OpFlip.cpp:L45-L57](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/OpFlip.cpp#L45-L57)：`cvcudaFlipSubmit` 全文。函数体只有两件事：打 NVTX 范围标记（u7-l4 会讲），然后把全部逻辑塞进 `ProtectCall` 的 lambda——lambda 内先 `TensorWrapHandle` 包装输入输出（非持有视图，u6-l1 讲过），再经 `ToDynamicRef<priv::Flip>(handle)` 还原类型并调用。
- [src/cvcuda/OpFlip.cpp:L30-L43](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/OpFlip.cpp#L30-L43)：`cvcudaFlipCreate` 的第一道防线在 C API 层就有一处：`handle` 输出指针为 NULL 直接抛 `ERROR_INVALID_ARGUMENT`，消息 "Pointer to NVCVOperator handle must not be NULL"。

**第一道防线的三重检查**：

- [src/cvcuda/priv/IOperator.hpp:L72-L89](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/IOperator.hpp#L72-L89)：`ToDynamicRef` ——NULL 检查抛 `ERROR_INVALID_ARGUMENT`（"Handle cannot be NULL"）；`dynamic_cast` 失败（句柄不是 Flip、或已被销毁）抛 `ERROR_NOT_COMPATIBLE`（"Handle doesn't correspond to the requested object or was already destroyed."）。
- [src/cvcuda/priv/IOperator.cpp:L19-L40](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/IOperator.cpp#L19-L40)：`ToOperatorPtr` 的版本闸门——把句柄 `reinterpret_cast` 回 `IOperator*` 后比较 `op->version().major()` 与 `CURRENT_VERSION.major()`（定义于 [priv/Version.hpp:L28-L28](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/Version.hpp#L28-L28)），主版本不同抛 `ERROR_NOT_COMPATIBLE` 并在消息里打印两个版本号。这是「符号版本」之外的另一层运行时 ABI 防护（针对 dlopen 直取句柄的场景）。

**第二道防线**：

- [src/cvcuda/priv/OpFlip.cpp:L39-L54](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/OpFlip.cpp#L39-L54)：`exportData<TensorDataStridedCuda>()` 判空即抛 `ERROR_INVALID_ARGUMENT`——CPU 张量、非 pitch-linear 缓冲都会在这里被拦下（机制详见 u5-l2）。

**第三道防线**：

- [src/cvcuda/priv/legacy/flip.cu:L370-L402](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/legacy/flip.cu#L370-L402)：legacy `Flip::infer` 依次校验输入输出 dtype 一致、布局合法（NHWC/HWC/NCHW/CHW）、dtype 属于 8U/16U/32S/32F 白名单，任一不过返回 `ErrorCode::INVALID_DATA_TYPE` 或 `INVALID_DATA_FORMAT`，并用 `LOG_ERROR` 打出详细上下文。
- [src/cvcuda/priv/legacy/CvCudaLegacyHelpers.cpp:L281-L297](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/legacy/CvCudaLegacyHelpers.cpp#L281-L297)：`TranslateError` 把 legacy 错误码翻译成 `NVCVStatus`——四种 INVALID_* 全部映射为 `NVCV_ERROR_INVALID_ARGUMENT`，其余归 `NVCV_ERROR_INTERNAL`。
- [src/nvcv/util/CheckError.hpp:L144-L158](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/util/CheckError.hpp#L144-L158)：`NVCV_CHECK_THROW` 宏——执行语句、检查状态、失败则抛异常，且消息里带 `文件名:行号`（[L117-L121](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/util/CheckError.hpp#L117-L121) 的 `"%s:%d %s"` 格式），把错误定位到源码行。priv 层在 [priv/OpFlip.cpp:L56-L56](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/OpFlip.cpp#L56-L56) 用它包住 `m_legacyOp->infer(...)`。

**错误码契约写在头文件里**：

- [src/cvcuda/include/cvcuda/OpFlip.h:L103-L116](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/include/cvcuda/OpFlip.h#L103-L116)：`@retval` 注释声明 Submit 只可能返回 `NVCV_ERROR_INVALID_ARGUMENT`、`NVCV_ERROR_INTERNAL`、`NVCV_SUCCESS` 三种——这正是上面三道防线产出错误码的并集，文档与实现互相印证。

**通用销毁入口**：

- [src/cvcuda/Operator.cpp:L26-L29](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/Operator.cpp#L26-L29)：`nvcvOperatorDestroy` 同样是 `ProtectCall` 包一句销毁。注意它返回 `void`——`ProtectCall` 的返回值被丢弃，销毁失败只能通过 `nvcvGetLastError` 查询。

#### 4.3.4 代码实践

**实践目标**：用纯 C 程序触发 NULL 句柄错误，打印返回码与消息；再用 C++ 捕获同名异常。这是本讲的核心实践。

**操作步骤**：

1. 编写 C 版（示例代码，需在链接好 libcvcuda 的环境编译运行）：

```c
/* 示例代码：flip_err_c.c —— 故意传 NULL 句柄 */
#include <cvcuda/OpFlip.h>
#include <nvcv/Status.h>
#include <stdio.h>

int main(void)
{
    /* handle=NULL, stream=NULL(默认流), 张量也为 NULL：
       第一道防线会最先拦截 NULL handle */
    NVCVStatus st = cvcudaFlipSubmit(NULL, NULL, NULL, NULL, 1);
    printf("cvcudaFlipSubmit 返回 %d (%s)\n", st, nvcvStatusGetName(st));

    char msg[NVCV_MAX_STATUS_MESSAGE_LENGTH];
    nvcvPeekAtLastErrorMessage(msg, sizeof(msg));
    printf("线程最后错误: %s\n", msg);
    return 0;
}
```

2. 编译运行：`gcc flip_err_c.c -I<前缀>/include -L<前缀>/lib -lcvcuda -o flip_err_c && ./flip_err_c`（还需链接 CUDA runtime，参数待本地确认）。

3. 编写 C++ 版（示例代码）：创建合法算子与张量，但把输入张量换成 **F16** dtype（Flip 的 Limitations 表声明 16bit Float 不支持，见 [OpFlip.h:L70-L74](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/include/cvcuda/OpFlip.h#L70-L74)）：

```cpp
// 示例代码：flip_err_cpp.cpp —— 触发不支持的 dtype
#include <cvcuda/OpFlip.hpp>
#include <nvcv/Exception.hpp>
#include <nvcv/Tensor.hpp>
#include <cstdio>

int main()
{
    try
    {
        cvcuda::Flip op;
        // F16 不在 Flip 支持矩阵内 → 第三道防线
        nvcv::Tensor in({1, 8, 8, 3}, nvcv::DataType_F16, nvcv::TensorLayout("NHWC"));
        nvcv::Tensor out({1, 8, 8, 3}, nvcv::DataType_F16, nvcv::TensorLayout("NHWC"));
        op(nullptr, in, out, 1);   // 传 nullptr 流 = 默认流
        printf("未抛异常?\n");
    }
    catch (const nvcv::Exception &e)
    {
        printf("code = %d, what = %s\n", (int)e.code(), e.what());
    }
    return 0;
}
```

（`nvcv::Tensor` 构造参数以 [Tensor.hpp](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/include/nvcv/Tensor.hpp) 实际签名为准，dtype 创建 API 的准确写法待本地确认。）

**需要观察的现象**：

- C 版：返回码 `2`，名字 `NVCV_ERROR_INVALID_ARGUMENT`，消息含 "Handle cannot be NULL"。
- C++ 版：`what()` 以 `INVALID_ARGUMENT: ` 开头，后面跟 `文件名:行号` 与 "Invalid DataType" 之类的 legacy 消息。

**预期结果**：两侧拿到**同一个错误码**——C 侧是翻译后的返回值，C++ 侧是异常自带的 `code()`；消息内容一致。具体打印文本待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：调用者把 `cvcuda::Flip` 的句柄传给了 `cvcuda::GaussianBlur` 的 Submit，返回什么码？为什么不是 INTERNAL？

**答案**：`NVCV_ERROR_NOT_COMPATIBLE`（= 9）。`ToDynamicRef<priv::GaussianBlur>` 里 `dynamic_cast` 失败（句柄指向的实际是 Flip 对象），走 [IOperator.hpp:L85-L88](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/IOperator.hpp#L85-L88) 分支——这是「调用者用错了对象」而非「库内部坏了」，归为 NOT_COMPATIBLE 更准确。

**练习 2**：`cvcudaFlipSubmit` 把 `in` 传成了 CPU 内存张量，错误在第几道防线、返回码是什么？

**答案**：第二道防线。句柄合法通过第一道后，`exportData<TensorDataStridedCuda>()` 对 CPU 缓冲返回空，[priv/OpFlip.cpp:L42-L47](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/OpFlip.cpp#L42-L47) 抛 `ERROR_INVALID_ARGUMENT`，消息 "Input must be cuda-accessible, pitch-linear tensor"。

**练习 3**：头文件 `@retval` 只声明了两种失败码，但你觉得未来可能出现 `NVCV_ERROR_DEVICE`（比如 CUDA 驱动报错）。这算文档错误吗？

**答案**：不算矛盾，但是契约收紧的体现。当前实现路径产出的错误码确实只有 INVALID_ARGUMENT 与 INTERNAL；若未来引入直通 CUDA 错误的路径，头文件的 `@retval` 列表与 Limitations 契约表需同步更新——这正是 CLAUDE.md 中「公共 API 变更需配套文档」不变量的场景。

### 4.4 符号版本：CVCUDA_DEFINE_API 与跨版本 ABI 兼容

#### 4.4.1 概念说明

C ABI 的最大承诺是**稳定**：三年前链接你库的程序，换新版库依然能跑。但现实是 API 迟早要改——加参数、改类型。直接改签名等于把老程序全部弄坏（链接器找不到旧符号）。CV-CUDA 的解法是 **ELF 符号版本**：

- 每个导出函数的真实符号名带版本后缀，如 `cvcudaFlipCreate_v0_2`；
- 再给它挂一个版本化别名 `cvcudaFlipCreate@@CVCUDA_0.2`（`@@` 表示默认版本）；
- 老程序链接时记住「我要 0.2 版」，新程序默认拿最新版；两者可以**并存于同一个 .so**。

观察一个细节：仓库里不同算子的版本号并不相同——`cvcudaFlipCreate` 是 `0, 2`（[OpFlip.cpp:L30](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/OpFlip.cpp#L30)），`nvcvOperatorDestroy` 是 `0, 3`（[Operator.cpp:L26](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/Operator.cpp#L26)），CLAHE 用 `0, 8`，Posterize 用 `0, 0`。**版本号记录的是该符号的签名最后一次变更时的库版本**——没改过的老函数保留低版本号，改过签名的函数升版本并同时保留旧符号。

#### 4.4.2 核心流程

宏展开链条（以 `CVCUDA_DEFINE_API(0, 2, NVCVStatus, cvcudaFlipCreate, (参数))` 为例）：

```text
CVCUDA_DEFINE_API(0,2,NVCVStatus,cvcudaFlipCreate,(args))          [cvcuda/priv/SymbolVersioning.hpp]
  └─► NVCV_PROJ_DEFINE_API(CVCUDA, 0, 2, ...)                      [nvcv/util/SymbolVersioning.hpp]
        ├─► 真实函数名 = FUNC##_v0_2        → cvcudaFlipCreate_v0_2   （避免符号冲突）
        ├─► symver 别名 = cvcudaFlipCreate@@CVCUDA_0.2              （ELF 版本标签）
        └─► extern "C" + visibility("default")                      （导出为纯 C 可见符号）

两种宏：
  NVCV_PROJ_DEFINE_API      → "@@" 默认版本（新程序链接它）
  NVCV_PROJ_DEFINE_API_OLD  → "@"  非默认版本（仅为兼容保留的旧签名）

gcc ≥ 10：用 __attribute__((__symver__(...)) 
gcc < 10：用内联汇编 .symver 指令
```

运行时按版本取函数：

```c
void *f_old = dlvsym(lib, "nvcvFooCreate", "NVCV_1.0");  // 指定版本
void *f_new = dlsym(lib, "nvcvFooCreate");               // 永远是默认（最新）版本
```

#### 4.4.3 源码精读

**机制文档就在源码注释里**（本仓库最好的 ABI 教材）：

- [src/nvcv/util/SymbolVersioning.hpp:L20-L65](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/util/SymbolVersioning.hpp#L20-L65)：注释完整演示了「v1.0 发布 → 签名变更 → 用 `_OLD` 保留 1.0、新宏定义 1.1 → 用户定义 `NVCV_API_VERSION == 100` 时头文件自动选旧签名并 `.symver` 到 1.0」的全流程，还给出 `dlvsym` 用法。

**宏本体三件套**：

- [src/nvcv/util/SymbolVersioning.hpp:L67-L67](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/util/SymbolVersioning.hpp#L67-L67)：`NVCV_PROJ_FUNCTION_API` 用 `##` 拼接出带版本后缀的真实函数名。
- [src/nvcv/util/SymbolVersioning.hpp:L70-L79](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/util/SymbolVersioning.hpp#L70-L79)：`NVCV_PROJ_SYMVER` 按 gcc 版本二选一：≥10 用 symver 函数属性，否则用 `.symver` 汇编指令；两者都带 `extern "C"` 与 `visibility("default")`。
- [src/nvcv/util/SymbolVersioning.hpp:L86-L92](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/util/SymbolVersioning.hpp#L86-L92)：`_OLD` 用 `"@"`（非默认），现行版用 `"@@"`（默认）——一个字符之差决定链接器选谁。

**cvcuda 侧的转发**：

- [src/cvcuda/priv/SymbolVersioning.hpp:L23-L24](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/SymbolVersioning.hpp#L23-L24)：`CVCUDA_DEFINE_API` / `CVCUDA_DEFINE_OLD_API` 只是给 `NVCV_PROJ_DEFINE_API` 换上 `CVCUDA` 项目前缀——版本节点名因此是 `CVCUDA_0.2` 而非 `NVCV_0.2`。目前仓库尚未用到 `_OLD`（它为未来签名迁移预留）。

**运行时版本查询**：

- [src/nvcv/src/include/nvcv/Version.h:L36-L41](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/include/nvcv/Version.h#L36-L41)：`nvcvGetVersion` 返回**二进制**的版本号，注释明确指出它可能与头文件的 `NVCV_VERSION` 不同——程序可据此在运行时嗅探「头与库不配套」。这与 4.3 节句柄的 `version().major()` 检查构成「符号版本（链接期）+ 句柄版本（运行期）」的双保险。

**同一宏也用于 nvcv 自身**：

- [src/nvcv/src/Status.cpp:L71-L92](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/Status.cpp#L71-L92)：`nvcvSetThreadStatus` 用 `NVCV_DEFINE_API(0, 2, ...)` 定义，内部实现很有趣——它**先抛 priv 异常再让 priv 版 `ProtectCall` 捕获**，借翻译机制写 TLS：绑定层由此获得与 C API 一致的错误注入通道。

#### 4.4.4 代码实践

**实践目标**：亲眼看到 .so 里的版本化符号。

**操作步骤**：

1. 在构建产物目录（u1-l3 的 `build-rel/lib/`）执行（命令可在任何 Linux 环境运行）：

```bash
# 列出 libcvcuda 的动态符号，过滤 Flip 相关
objdump -T build-rel/lib/libcvcuda.so | grep -i flip

# 查看库的版本定义节点
readelf -V build-rel/lib/libcvcuda.so | grep -A3 "Version definition"
```

2. 换一个算子重复，例如 `grep -i clahe`、`grep -i posterize`。

**需要观察的现象**：`objdump -T` 输出中 Flip 函数行的版本列应为 `CVCUDA_0.2`，真实符号名形如 `cvcudaFlipCreate_v0_2`；CLAHE 行应为 `CVCUDA_0.8`；`readelf -V` 能看到名为 `CVCUDA_0.0`、`CVCUDA_0.2`……的版本定义节点列表。

**预期结果**：验证「版本号因符号而异」——不同算子挂不同版本标签，各自记录签名最后变更的时点。具体输出待本地验证（需先完成 u1-l3 的源码构建或已安装 wheel 内的 .so）。

#### 4.4.5 小练习与答案

**练习 1**：为什么真实函数名要带 `_v0_2` 后缀，而不是直接定义 `cvcudaFlipCreate` 再挂版本？

**答案**：同一 .so 里新旧两个版本必须先在**符号名**上不冲突（`_v0_2` 与 `_v0_8` 是两个不同的函数），symver 只是给它们各挂一个同名但版本不同的**别名**供链接器选择。直接定义两个同名函数会在链接期就撞符号。

**练习 2**：`@@` 与 `@` 的区别是什么？如果新旧两个版本都写成 `@@` 会怎样？

**答案**：`@@` 标记默认版本——未指定版本的引用（普通 `dlsym`、未定义宏的编译）都绑定它；`@` 是非默认版本，只有显式要求的引用才绑定。若两个都是默认，链接器无法裁决新程序该绑哪个，行为未定义/报错。

**练习 3**：老程序链接了 `CVCUDA_0.2` 的 `cvcudaFlipCreate`，新版本库删除了 0.2 节点。老程序在运行时会怎样？

**答案**：动态链接器找不到版本 `CVCUDA_0.2` 的定义节点，加载 .so 时直接失败（报 `version 'CVCUDA_0.2' not found` 一类错误），程序起不来。所以删除旧版本节点是重大 ABI 破坏，须配合主版本号升级——这正是「符号只能加不能改、版本节点只能加不能删」纪律的由来。

## 5. 综合实践

**任务：制作你自己的「cvcuda 错误解码器」**，把本讲四个模块串起来。

1. **触发**（用 4.3.4 的两个程序）：分别制造三类错误——NULL 句柄（第一道防线）、CPU 张量输入（第二道防线，可用 C++ 创建 host 张量或直接传包装失败的张量）、F16 dtype（第三道防线）。
2. **记录**：对每个错误，在 C 侧记录 `返回码数值 + nvcvStatusGetName + nvcvPeekAtLastErrorMessage`；在 C++ 侧记录 `e.code() + e.what()`。
3. **对照**：把结果整理成一张表：

| 触发方式 | 防线 | 返回码 | 码值 | 消息开头 |
|---|---|---|---|---|
| handle=NULL | 第一道 | NVCV_ERROR_INVALID_ARGUMENT | 2 | "Handle cannot be NULL" |
| CPU 张量 | 第二道 | NVCV_ERROR_INVALID_ARGUMENT | 2 | "Input must be cuda-accessible..." |
| F16 输入 | 第三道 | NVCV_ERROR_INVALID_ARGUMENT | 2 | "INVALID_ARGUMENT: 文件:行号 Invalid DataType" |
| 错误类型句柄 | 第一道 | NVCV_ERROR_NOT_COMPATIBLE | 9 | "Handle doesn't correspond..." |

（表中后两类待本地验证。）

4. **验证符号版本**：用 4.4.4 的 `objdump`/`readelf` 命令，为表中每个算子补一列「符号版本标签」，并回答：Flip 是 0.2，你触发的其他算子各是什么版本？
5. **思考题**（写进你的笔记）：如果让你给 `cvcudaFlipSubmit` 增加一个 `borderMode` 参数，按本讲的机制应该怎么做才不破坏 ABI？（提示：新签名用 `CVCUDA_DEFINE_API` 升版本号，旧签名改用 `CVCUDA_DEFINE_OLD_API`，头文件按宏切换声明——参考 [util/SymbolVersioning.hpp:L33-L53](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/util/SymbolVersioning.hpp#L33-L53) 的示例。）

## 6. 本讲小结

- **双轨制**：C 世界用 `NVCVStatus` 返回码 + 线程局部「最后错误」（`CoreTLS` 中的状态码与 256 字节消息）；C++ 世界用 `nvcv::Exception`。`nvcv::Status` 与 `NVCVStatus` 数值一一对应，是同一套码的两种拼写。
- **翻译阀**：每个 C API 函数体都是 `return nvcv::ProtectCall([&]{...})`；任何异常经 `SetThreadError` 按类型映射（invalid_argument→INVALID_ARGUMENT、bad_alloc→OUT_OF_MEMORY、其余→INTERNAL），写入 TLS 后以返回码出境。Get 是破坏性读取，Peek 只读。
- **三道防线**：句柄校验（NULL/类型/版本→INVALID_ARGUMENT 或 NOT_COMPATIBLE）、exportData 数据视图校验（→INVALID_ARGUMENT）、legacy 内核参数白名单（经 `NVCV_CHECK_THROW` + `TranslateError`→INVALID_ARGUMENT 或 INTERNAL），错误码契约写在头文件 `@retval` 与 Limitations 表中。
- **符号版本**：`CVCUDA_DEFINE_API(主, 次, ...)` 把函数编译为 `_v主_次` 后缀的真实符号 + `@@CVCUDA_主.次` 的 ELF 版本别名；`_OLD` 宏用单 `@` 保留旧默认版本。版本号因符号而异，记录各签名最后变更时点；`nvcvGetVersion` 与句柄 `version()` 检查提供运行期补充防线。
- **两条纪律**：错误码枚举只能在末尾追加；公开 `nvcv::Exception` 不允许从库内部抛出（priv 层有断言把关），记账统一由边界完成。

## 7. 下一步学习建议

本讲补完了 C API 边界的「错误」与「符号」两块拼图。下一步：

- **u6-l3（分配器）**：继续底层机制——nvcv 的 `Allocator` 抽象与 `Requirements` 协商，理解 `NVCV_ERROR_OUT_OF_MEMORY` 背后的分配路径。
- **错误路径的下游**：读 [src/nvcv/util/CheckError.hpp](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/util/CheckError.hpp) 全文，里面有 `NVCV_CHECK_LOG`、`NVCV_ASSERT` 等完整检查家族，以及 CUDA 错误码到 `NVCVStatus` 的翻译表。
- **Python 侧的对照**：[python/mod_cvcuda/include/nvcv/python/CAPI.hpp:L127-L138](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/include/nvcv/python/CAPI.hpp#L127-L138) 展示了 Python 子模块跨 C 边界时的同款纪律（异常先转 `PyErr` 再过边界），与 `ProtectCall` 异曲同工。
- 若你计划贡献算子（u8-l1），新增 C API 时**必须**用 `CVCUDA_DEFINE_API` 且版本号取当前库版本，绝不能手写 `extern "C"` 裸导出——这是符号版本体系的入场券。
