# 错误处理：error_or 与 errno 机制

## 1. 本讲目标

C 标准库里有一类函数（如 `open`、`dup`、`read`、`malloc`）在失败时不能靠返回值本身说明原因，必须借助一个外部的“错误码”把失败原因告诉调用者。LLVM-libc 在实现这类函数时，需要同时解决两个层面的问题：

- **内部层面**：函数内部、函数与底层 syscall 之间，用什么类型来传递“成功得到一个值”还是“失败得到一个错误码”？
- **公开层面**：C 标准规定失败原因要写进全局/线程局部的 `errno`，而 LLVM-libc 又要同时服务 Full（自带完整 libc）和 Overlay（复用系统 libc）两种构建模式，这个 `errno` 到底从哪里来？

本讲学完后，你应该能够：

1. 理解 `ErrorOr<T>` 这个类型如何作为内部错误传播的统一载体，以及它背后 `cpp::expected` 的实现原理。
2. 掌握 `libc_errno` 抽象如何用一套代码适配“线程局部存储 / 共享存储 / 外部提供 / 直接复用系统 errno”等多种落地方式。
3. 看懂并把“syscall 返回值 → `ErrorOr` → 设置 `libc_errno` → 返回 C 标准约定的失败值”这条端到端调用链写出来，能自己实现一个符合规范的失败可报告函数。

## 2. 前置知识

- **C 的 `errno` 语义**：`errno` 是一个 `int` 型的左值（通常实现成宏或线程局部变量），许多标准函数在失败时会把它设成一个正整数错误码（如 `EBADF`、`ENOMEM`），调用者通过 `#include <errno.h>` 读取它。它**不是函数返回值**，而是一个“副作用”：同一线程里后续的成功调用可能把它重新置 0，所以必须在函数返回失败后立刻读。
- **“判别式联合”（discriminated union）**：一个同时能装“成功值 `T`”或“错误值 `E`”、再用一个布尔标记区分当前是哪一种的数据结构。C++23 的 `std::expected` 就是这种思想的标准化产物。
- **Linux 内核的 syscall 返回约定**：用户态发起系统调用后，内核在成功时返回非负结果，失败时返回一个**负的错误码**（如 `-EBADF`）。glibc/musl 这类传统 libc 会把负值取反再写进 `errno`。LLVM-libc 的 syscall 封装层做的正是这件事，只是它把结果包成了 `ErrorOr`。
- **命名空间与构建目标**：本讲假定你已学过 [u4-l1] 的 `__support` 私有标准库定位、`LIBC_NAMESPACE_DECL` 命名空间，以及“C++ 里 `#include` 一个 `__support` 头，CMake 的 `DEPENDS` 里就要有对应目标”这条一一对应约定（[u4-l1]）。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
| --- | --- |
| `src/__support/error_or.h` | 定义 `ErrorOr<T>` 与 `Error` 两个类型别名，是内部错误类型的唯一入口（极薄，仅做别名）。 |
| `src/__support/CPP/expected.h` | `ErrorOr` 的真正实现：自带的 C++23 风格 `expected`/`unexpected`（[u4-l2] 自包含 CPP 子集的成员）。 |
| `src/__support/libc_errno.h` | 定义 `libc_errno` 抽象：根据 `LIBC_ERRNO_MODE` 在“内部 `Errno` 对象”与“直接宏替换成系统 `errno`”间切换。 |
| `src/errno/libc_errno.cpp` | `Errno` 对象 `operator=`/`operator int()` 的各模式实现，以及全局 `libc_errno` 实例的定义。 |
| `hdr/errno_macros.h` | 代理头：Full 模式从内核头/自带宏取错误码常量，Overlay 模式从系统 `<errno.h>` 取（[u3-l2] 代理头概念）。 |
| `src/__support/OSUtil/linux/syscall_wrappers/dup.h` | 一个真实的返回 `ErrorOr<int>` 的 syscall 封装，展示内核负错误码到 `Error` 的转换。 |
| `src/unistd/linux/dup.cpp` | 一个真实的公开入口点，完整演示 `ErrorOr` → `libc_errno` → 返回 `-1` 的传播。 |

## 4. 核心概念与源码讲解

### 4.1 `error_or` 类型：统一的「值或错误」结果

#### 4.1.1 概念说明

考虑一个返回 `int` 文件描述符的函数 `dup(int fd)`。它成功时返回新描述符（一个非负整数），失败时按 C 标准约定返回 `-1`，并把原因写进 `errno`。问题在于：**底层 syscall 自己已经返回了一个带符号的结果**，成功是值、失败是负错误码，把这两种语义不同的负数混在同一个返回值里，调用方很容易写错判断。

`ErrorOr<T>` 的设计意图就是消除这种歧义：它是一个**和类型（sum type）**，任何时刻要么持有成功值 `T`，要么持有错误码 `int`，并由一个标记明确指明当前是哪一种。这样：

- 底层 syscall 封装可以诚实地表达“我成功了（带值）”或“我失败了（带错误码）”，不再用“负数代表错误”这种隐式约定。
- 上层函数拿到 `ErrorOr` 后，用 `if (!ret)` 或 `has_value()` 一眼判断成败，再用 `value()` / `error()` 取对应分量，逻辑清晰且不易出错。

从类型角度，`ErrorOr<T>` 可理解为两种可能性的并集：

\[
\text{ErrorOr}\langle T\rangle \;=\; T \;+\; \text{int}
\]

即“一个 `T` 或一个 `int`”，且带标记区分。

#### 4.1.2 核心流程

`ErrorOr<T>` 在 LLVM-libc 里只是一个**类型别名**，真正的实现是 `cpp::expected<T, int>`。它的判别式联合内部结构可用伪代码描述：

```text
expected<T, E>:
    union { T exp;       # 成功时持有值
            E unexp; }   # 失败时持有错误
    bool is_expected;    # 判别标记：true=成功，false=失败
```

构造与访问流程：

1. 用一个 `T` 构造 → `is_expected = true`，`exp` 就位。
2. 用一个 `unexpected<E>` 构造 → `is_expected = false`，`unexp` 就位。
3. 取值：`has_value()` 读 `is_expected`；成功侧用 `value()` / `operator*` / `operator->`；失败侧用 `error()`。
4. `explicit operator bool()` 直接返回 `is_expected`，便于 `if (ret)` / `if (!ret)` 写法（注意它是 `explicit`，不会发生意外的隐式转换）。

#### 4.1.3 源码精读

先看最薄的入口 `error_or.h`：它只把 `cpp::expected<T, int>` 起了两个好记的名字。

`ErrorOr<T>` 即 `expected<T, int>`，`Error` 即 `unexpected<int>`——见 [src/__support/error_or.h:15-19](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/src/__support/error_or.h#L15-L19)，这段用 `using` 给 `cpp::expected<T,int>` 起了别名 `ErrorOr<T>`、给 `cpp::unexpected<int>` 起了别名 `Error`。文件下方被注释掉的一段（[src/__support/error_or.h:21-36](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/src/__support/error_or.h#L21-L36)）是早期手写的 `ErrorOr` 结构体，后来被替换成基于 `expected` 的实现，可以看出演进痕迹。

真正的实现在 `cpp/expected.h`。先看“装错误”的辅助类型 `unexpected`：

`unexpected<T>` 只是把一个错误值包起来，用于在构造时与“成功值”区分——见 [src/__support/CPP/expected.h:25-33](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/src/__support/CPP/expected.h#L25-L33)。它的构造函数是 `explicit`，并提供 `error()` 取值；最后一行是 CTAD 推导指引（`unexpected(5)` 自动推出 `unexpected<int>`），让调用方能直接写 `return Error(-ret);` 而不必显式写类型。

再看判别式联合本体 `expected`：

`expected<T,E>` 用 `union { T exp; E unexp; }` 加一个 `bool is_expected` 实现“二选一”存储——见 [src/__support/CPP/expected.h:35-45](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/src/__support/CPP/expected.h#L35-L45)。从 `T` 构造时 `is_expected=true`，从 `unexpected<E>` 构造时 `is_expected=false` 并把错误值取出存进 `unexp`。用 `union` 而非两个独立成员，是为了省去另一份 `T`/`E` 的存储（`T` 往往是 `int` 或较大结构体，能省就省）。

访问接口集中在这一段：

`has_value/value/error/operator bool/operator*/operator->` 提供了完整的查询接口——见 [src/__support/CPP/expected.h:47-59](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/src/__support/CPP/expected.h#L47-L59)。注意 `operator bool` 是 `explicit`（第 54 行），避免 `ErrorOr` 在算术表达式里被意外当整数用；`operator->` 让 `ErrorOr<T*>` 或含指针成员的对象能像指针一样访问。

**CMake 提示**：在实现里 `#include "src/__support/error_or.h"` 时，对应 `DEPENDS` 目标是 `libc.src.__support.error_or`（它内部已经带上 `expected`、`common` 等依赖）。

#### 4.1.4 代码实践

**实践目标**：亲手确认 `ErrorOr` 的构造与访问语义，体会“成功带值、失败带错误码”两种状态。

**操作步骤**：

1. 打开 `src/__support/error_or.h` 与 `src/__support/CPP/expected.h` 对照阅读。
2. 写一段**示例代码**（非项目原有代码，仅用于理解）：

   ```cpp
   // 示例代码：演示 ErrorOr 的两种状态
   #include "src/__support/error_or.h"
   using LIBC_NAMESPACE::ErrorOr;
   using LIBC_NAMESPACE::Error;

   ErrorOr<int> do_dup(bool ok) {
     if (ok) return 42;          // 成功：隐式构造 expected(T)
     return Error(9);            // 失败：Error(EBADF) 即 unexpected<int>(9)
   }

   void use() {
     auto r = do_dup(false);
     if (!r) {                   // operator bool == false
       int code = r.error();     // 取错误码
       (void)code;
     }
     auto s = do_dup(true);
     if (s) { int fd = s.value(); (void)fd; }
   }
   ```

3. （可选）把它放进一个测试翻译单元编译。

**需要观察的现象**：成功分支 `r` 为“真”、`value()` 可取；失败分支 `r` 为“假”、`error()` 返回写入的错误码；二者共用同一份存储但不会同时有效。

**预期结果**：`do_dup(false)` 的 `error()` 为 9，`do_dup(true)` 的 `value()` 为 42。若未实际编译，记为**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `expected` 要用 `union` 加一个 `bool` 判别标记，而不是直接存两个成员 `T value; int error;`？

**参考答案**：用 `union` 可节省存储——任意时刻只有一个分量有效，不必同时占用 `T` 与 `int` 两份空间；`bool is_expected` 用极少代价记录“当前生效的是哪一个”，这正是判别式联合的标准做法。

**练习 2**：`operator bool` 被声明为 `explicit`，如果不加 `explicit` 会带来什么隐患？

**参考答案**：不加则 `ErrorOr` 会隐式转成 `bool`，进而参与算术或比较（如 `ErrorOr<int> + 1`），掩盖“这里其实是个值或错误”的语义，容易写出静默错误的代码；`explicit` 强制只在条件判断等显式语境里转换。

**练习 3**：`Error` 为什么用 `unexpected<int>` 而不是直接用 `int` 作为失败参数？

**参考答案**：因为 `expected` 有两个构造函数（接受 `T` 与接受 `unexpected<E>`）。若失败也直接传 `int`，而 `T` 恰好也是 `int`，编译器无法区分这是“成功值”还是“错误码”。`unexpected` 是一个专门“包错误”的类型，构造时不会和成功值重载冲突。

---

### 4.2 `errno` 抽象：`libc_errno` 与 `Errno`

#### 4.2.1 概念说明

`ErrorOr` 解决的是**内部**传错，但 C 标准要求公开函数把失败原因写进 `errno` 这个对外可见的左值。LLVM-libc 的难点在于：`errno` 的“存储位置”在不同部署环境下完全不同。

- **Full 模式**（自带完整 libc，见 [u1-l4]）：LLVM-libc 自己就是 libc，必须自己提供 `errno` 的存储，且按 C 标准应是**每线程一份**（`thread_local`），否则多线程下会互相覆盖。
- **Overlay 模式**（仅覆盖少数函数、其余回退系统 libc，见 [u1-l4]）：程序里真正被读取的 `errno` 是**系统 libc 维护的那个**，LLVM-libc 覆盖的函数也必须写进系统 libc 的 `errno`，否则读出来对不上。
- **特殊目标**：baremetal、UEFI、GPU 可能没有线程局部存储支持，也没有系统 libc，需要别的存储策略，甚至由**嵌入者自己提供**一个返回 `int*` 的函数。

为了用同一份实现代码适配所有情况，LLVM-libc 不让实现代码直接碰 `errno`，而是约定一律写 `libc_errno`。`libc_errno` 这个名字背后到底是什么，由 `LIBC_ERRNO_MODE` 编译期开关决定。这样：

- 实现代码只学一个名字 `libc_errno`，迁移平台时无需改动业务逻辑。
- “errno 从哪来”被集中收敛到一个头文件和一个 `.cpp` 里，可审查、可裁剪。

#### 4.2.2 核心流程

`LIBC_ERRNO_MODE` 有六种取值，决定 `libc_errno` 的落地方式：

| 取值 | 含义 | `libc_errno` 实际是什么 |
| --- | --- | --- |
| `DEFAULT`(0) | 不显式指定，按构建模式自动选 | 见下方自动分派 |
| `UNDEFINED`(1) | 完全不存值 | 写入丢弃、读取恒为 0（裁剪体积用） |
| `THREAD_LOCAL`(2) | 每线程一份 `thread_local int` | 内部 `Errno` 对象，转读写 `thread_errno` |
| `SHARED`(3) | 全局共享一份 `int` | 内部 `Errno` 对象，转读写 `shared_errno`（非线程安全） |
| `EXTERNAL`(4) | 由嵌入者提供 `int *__llvm_libc_errno()` | 内部 `Errno` 对象，转读写该函数返回的地址 |
| `SYSTEM_INLINE`(6) | 直接复用系统 libc 的 `errno` | 宏 `#define libc_errno errno` |

**自动分派**（`DEFAULT` 时）：若处于 Full 构建或非公开打包，选 `THREAD_LOCAL`；否则（典型 Overlay）选 `SYSTEM_INLINE`。流程如下：

```text
未定义 LIBC_ERRNO_MODE 或 == DEFAULT ?
   ├── 是 ──► (LIBC_FULL_BUILD 或 非公开打包) ?
   │             ├── 是 ──► THREAD_LOCAL      # 自带每线程存储
   │             └── 否 ──► SYSTEM_INLINE      # 直接用系统 errno
   └── 否 ──► 采用用户显式指定的模式
```

在 `THREAD_LOCAL`/`SHARED`/`EXTERNAL` 等非系统模式下，`libc_errno` 是一个自定义类型 `Errno` 的全局对象，它重载了 `operator=(int)`（写）和 `operator int()`（读），把读写转发到对应存储；同时对外暴露一个 `extern "C" int *__llvm_libc_errno()`，返回错误码的地址，供需要拿指针的场合使用。在 `SYSTEM_INLINE` 模式下则连这个对象都没有，`libc_errno` 就是个指向系统 `errno` 的宏别名。

#### 4.2.3 源码精读

头文件开头有一段非常重要的**使用规约**注释：内部实现一律用 `libc_errno`、不要直接 `#include <errno.h>`——见 [src/__support/libc_errno.h:12-26](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/src/__support/libc_errno.h#L12-L26)。它还区分了单元/密封测试（用 `libc_errno`）与集成测试（用系统 `errno`）的不同写法。

六种模式的常量定义在这一段：

`LIBC_ERRNO_MODE_*` 六个常量枚举出所有可能的 errno 落地策略——见 [src/__support/libc_errno.h:28-45](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/src/__support/libc_errno.h#L28-L45)。其中 `SYSTEM`(5) 已标注 DEPRECATED，`SYSTEM_INLINE`(6) 是现行的“复用系统 errno”模式。

自动分派与校验逻辑：

`DEFAULT` 自动选模式，并用 `#error` 拒绝非法取值——见 [src/__support/libc_errno.h:47-69](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/src/__support/libc_errno.h#L47-L69)。第 47-54 行做自动分派，第 56-69 行用预处理 `#error` 在编译期拦截不合法的模式值，避免拼错模式名时静默走错分支。

两条分支的代码截然不同。先看 `SYSTEM_INLINE` 分支（直接用系统 errno）：

系统模式下 `libc_errno` 被宏替换成系统的 `errno`，没有任何内部状态——见 [src/__support/libc_errno.h:71-76](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/src/__support/libc_errno.h#L71-L76)。它 `#include <errno.h>` 后直接 `#define libc_errno errno`，因此这种模式下不存在公共 C++ 符号 `LIBC_NAMESPACE::libc_errno`。

再看其余（自带状态）模式：

非系统模式下声明 `Errno` 类型与全局对象，并约定一个 `extern "C"` 取址函数——见 [src/__support/libc_errno.h:79-95](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/src/__support/libc_errno.h#L79-L95)。`struct Errno` 只暴露 `operator=(int)` 和 `operator int()`（第 86-89 行），实现留在 `.cpp`；`extern Errno libc_errno;`（第 91 行）声明全局对象；第 95 行 `using LIBC_NAMESPACE::libc_errno;` 把它引入到全局命名空间，使实现代码里能直接写 `libc_errno = EBADF;`。

各模式的真正实现写在 `libc_errno.cpp`。以 `THREAD_LOCAL` 为例：

线程局部模式下，errno 存在一个 `thread_local int` 里，`Errno` 的读写转发到它——见 [src/errno/libc_errno.cpp:22-31](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/src/errno/libc_errno.cpp#L22-L31)。`LIBC_THREAD_LOCAL int thread_errno;` 是每线程一份的存储；`__llvm_libc_errno()` 返回它的地址；`operator=` 写入、`operator int()` 读取。这正是 C 标准要求的“每线程独立 errno”。

其它模式同理可读：`UNDEFINED` 写入丢弃、读恒为 0（[src/errno/libc_errno.cpp:17-20](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/src/errno/libc_errno.cpp#L17-L20)）；`SHARED` 用一个全局 `int`（[src/errno/libc_errno.cpp:33-42](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/src/errno/libc_errno.cpp#L33-L42)）；`EXTERNAL` 把读写委托给嵌入者提供的 `__llvm_libc_errno()`（[src/errno/libc_errno.cpp:44-47](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/src/errno/libc_errno.cpp#L44-L47)）。最后第 52 行 `Errno libc_errno;` 定义那个被头文件 `extern` 声明的全局对象。

> 关于 `LIBC_THREAD_LOCAL`：它并不总是展开成 `thread_local`。在单线程模式下它被定义为空（见 [src/__support/macros/attributes.h:131-135](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/src/__support/macros/attributes.h#L131-L135)），这样在没有线程局部存储支持的 baremetal 目标上也能编译通过。

`LIBC_THREAD_LOCAL` 在单线程模式下退化为空，保证 baremetal 等无 TLS 目标也能编译——见 [src/__support/macros/attributes.h:131-135](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/src/__support/macros/attributes.h#L131-L135)。

最后看错误码常量（`EBADF`、`ENOMEM` 等）从哪来——代理头 `errno_macros.h`：

错误码常量按构建模式分流：Full/Linux 用内核头加自带宏，Overlay 用系统 `<errno.h>`——见 [hdr/errno_macros.h:12-28](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/hdr/errno_macros.h#L12-L28)。这正是 [u3-l2] 代理头“一条 include、两模式切换来源”的典型实例：Full 模式下从 `<linux/errno.h>` 与自包含的 `error-number-macros.h` 取常量；Overlay 下直接回退到系统 `<errno.h>`。

#### 4.2.4 代码实践

**实践目标**：弄清一次真实的 Full/Linux 构建里 `errno` 到底存在哪里，验证 `libc_errno` 的多模式抽象。

**操作步骤**：

1. 读 `src/__support/libc_errno.h:47-54`，回答：当 `LIBC_ERRNO_MODE` 未定义、且处于 Full 构建（`LIBC_FULL_BUILD` 已定义）时，最终选中的是哪个模式？
2. 据此定位到 `src/errno/libc_errno.cpp:22-31`，确认存储变量是 `thread_errno`，类型是 `LIBC_THREAD_LOCAL int`。
3. 用 `git grep -n "LIBC_THREAD_LOCAL"` 查它在 `src/__support/macros/attributes.h` 的定义，确认在非单线程模式下就是 `thread_local`。
4. （可选）在一个 Full 构建产物里，用 `nm` 查看 `__llvm_libc_errno` 符号是否存在。

**需要观察的现象**：Full 构建应选中 `THREAD_LOCAL`，每个线程的 `errno` 互不影响；Overlay 构建（默认 `SYSTEM_INLINE`）则没有内部存储，`libc_errno` 就是系统 `errno`。

**预期结果**：Full/Linux 下 `errno` 物理存于每线程的 `thread_errno`，经 `Errno::operator=` 写入；Overlay 下 `libc_errno` 是宏、直接指向系统 `errno`。若未实际构建验证，记为**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：为什么 Overlay 模式默认走 `SYSTEM_INLINE`，而 Full 模式默认走 `THREAD_LOCAL`？

**参考答案**：Overlay 模式下程序里被读取的 `errno` 是系统 libc 维护的，LLVM-libc 覆盖的函数也必须写进系统 libc 的 `errno`，所以直接宏替换成系统 `errno`；Full 模式下 LLVM-libc 自己就是 libc，必须自备存储，并按 C 标准提供每线程一份的语义，因此用 `THREAD_LOCAL`。

**练习 2**：`Errno` 只定义了 `operator=(int)` 和 `operator int()`，没有把内部存储暴露成公开成员，这样设计有什么好处？

**参考答案**：存储位置是实现细节（可能是 `thread_local`、可能是共享、可能委托外部函数），把它藏在 `operator=` / `operator int()` 之后，调用方只能用“赋值/读取”这一种统一接口，存储策略可在不同 `LIBC_ERRNO_MODE` 间自由切换而不破坏调用方代码。

**练习 3**：`EXTERNAL` 模式下，`libc_errno = EBADF;` 最终把值写到了哪里？

**参考答案**：写到嵌入者提供的 `int *__llvm_libc_errno()` 返回的地址处（见 [src/errno/libc_errno.cpp:44-47](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/src/errno/libc_errno.cpp#L44-L47)），即由宿主环境决定 errno 的真实存储位置。

---

### 4.3 错误传播模式：syscall → `ErrorOr` → `errno` 的端到端

#### 4.3.1 概念说明

前两节分别讲了内部传错（`ErrorOr`）和对外写错（`libc_errno`）。本节把两者缝合成一条真实链路：**底层 syscall 的带符号返回值 → 经 syscall 封装层转成 `ErrorOr` → 公开入口点据其设置 `libc_errno` 并返回 C 标准约定的失败值**。

这是 LLVM-libc 里所有“失败需报告 errno”的系统类函数（`open`/`dup`/`read`/`write`/`close`…）共同遵循的模式。理解了它，你就能照葫芦画瓢地写出任何这类函数。

#### 4.3.2 核心流程

以 `dup(int fd)`（复制文件描述符）为例，三层调用链如下：

```text
[内核]            syscall(SYS_dup, fd)
                      │  成功: 返回新 fd(>=0)   失败: 返回 -错误码(<0)
                      ▼
[syscall 封装层]  linux_syscalls::dup(fd) -> ErrorOr<int>
                      │  ret<0 ? return Error(-ret) : return ret
                      ▼
[公开入口点]      LLVM_LIBC_FUNCTION(int, dup, (int fd))
                      │  if(!ret){ libc_errno = ret.error(); return -1; }
                      │  return ret.value();
                      ▼
[C 调用者]        int newfd = dup(oldfd);   // 失败得 -1，原因查 errno
```

关键转换点：

1. **负错误码 → 正错误码**：Linux 内核返回 `-EBADF` 这样的负值，封装层用 `Error(-ret)` 取反成正的 `EBADF`，再包进 `ErrorOr` 的失败侧。
2. **`ErrorOr` → `libc_errno`**：入口点用 `if (!ret)` 判失败，把 `ret.error()` 写进 `libc_errno`，再返回 `-1`（C 标准对 `dup` 的失败约定）。
3. **成功直通**：`ret.value()` 取出新描述符直接返回。

#### 4.3.3 源码精读

先看 syscall 封装层（OSUtil 的一部分，[u8-l1] 会详讲 OSUtil）：

`linux_syscalls::dup` 把内核负错误码取反后包成 `Error`，成功则直接返回值——见 [src/__support/OSUtil/linux/syscall_wrappers/dup.h:26-31](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/src/__support/OSUtil/linux/syscall_wrappers/dup.h#L26-L31)。它 `#include "src/__support/error_or.h"`（[第 19 行](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/src/__support/OSUtil/linux/syscall_wrappers/dup.h#L19)）以拿到 `ErrorOr`/`Error`，并对 `SYS_dup` 发起 `syscall_impl`。

再看公开入口点的完整实现，它把上面三层缝合成 6 行：

`dup` 入口点正是端到端错误传播的范本——见 [src/unistd/linux/dup.cpp:18-25](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/src/unistd/linux/dup.cpp#L18-L25)。第 19 行调用 syscall 封装拿到 `ErrorOr<int>`；第 20-23 行在失败时写 `libc_errno` 并返回 `-1`；第 24 行成功时返回 `ret.value()`。注意它 `#include "src/__support/libc_errno.h"`（[第 13 行](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/src/unistd/linux/dup.cpp#L13)），而不是 `<errno.h>`，严格遵循 4.2.3 的使用规约。

> 对照公开签名：入口点实现头 `dup.h` 里只是普通声明 `int dup(int fd);`（[src/unistd/dup.h:17](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/src/unistd/dup.h#L17)），错误码不在签名里体现——`errno` 是 C 标准的“带外”通道，这印证了为什么需要 `libc_errno` 这条独立通路。

这套端到端模式可以总结成一张表，几乎所有系统类入口点都套用它：

| 阶段 | 责任方 | 失败时做什么 | 成功时做什么 |
| --- | --- | --- | --- |
| 内核 syscall | Linux 内核 | 返回 `-errno`（负） | 返回结果（非负） |
| syscall 封装层 | `linux_syscalls::*` | `return Error(-ret);` | `return ret;` |
| 公开入口点 | `LLVM_LIBC_FUNCTION` | `libc_errno = ret.error(); return -1;`（或 NULL/0 视标准而定） | `return ret.value();` |

#### 4.3.4 代码实践（本讲主实践）

**实践目标**：照着 `dup` 的端到端模式，为一个假想的 `my_open` 函数写出返回 `ErrorOr<int>` 的内部函数与设置 `libc_errno` 的公开入口点。

**操作步骤**：

1. 复读 [src/__support/OSUtil/linux/syscall_wrappers/dup.h:26-31](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/src/__support/OSUtil/linux/syscall_wrappers/dup.h#L26-L31) 与 [src/unistd/linux/dup.cpp:18-25](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/src/unistd/linux/dup.cpp#L18-L25) 两个范本。
2. 写出**示例代码**（非项目原有代码，仅用于练习）：

   ```cpp
   // 示例代码：假想的 my_open，演示端到端错误传播
   #include "src/__support/error_or.h"     // ErrorOr / Error
   #include "src/__support/libc_errno.h"   // libc_errno
   #include "src/__support/common.h"       // LLVM_LIBC_FUNCTION
   #include "src/__support/macros/config.h"

   using LIBC_NAMESPACE::ErrorOr;
   using LIBC_NAMESPACE::Error;

   namespace LIBC_NAMESPACE_DECL {
   namespace fake_syscalls {
   // 假装这是底层：成功返回 fd，失败返回 -errno
   LIBC_INLINE ErrorOr<int> my_open(const char *path, int flags) {
     long ret = fake_syscall(path, flags);   // 假设已存在
     if (ret < 0)
       return Error(static_cast<int>(-ret)); // 负错误码 → 正错误码
     return static_cast<int>(ret);
   }
   } // namespace fake_syscalls

   LLVM_LIBC_FUNCTION(int, my_open, (const char *path, int flags)) {
     ErrorOr<int> ret = fake_syscalls::my_open(path, flags);
     if (!ret) {
       libc_errno = ret.error();   // 失败：写 errno，返回 -1
       return -1;
     }
     return ret.value();           // 成功：返回 fd
   }
   } // namespace LIBC_NAMESPACE_DECL
   ```

3. 检查依赖一致性：若把这段真正加进仓库，对应 `CMakeLists.txt` 的 `DEPENDS` 至少要列出 `libc.src.__support.error_or`、`libc.src.__support.libc_errno`、`libc.src.__support.common`、`libc.src.__support.macros.config`（参考真实入口 [src/errno/CMakeLists.txt:10-15](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/src/errno/CMakeLists.txt#L10-L15) 的写法）。

**需要观察的现象**：

- 失败路径：`fake_syscall` 返回负值时，`my_open` 内部把它取反包成 `Error`，公开函数据此把 `libc_errno` 设为该正错误码并返回 `-1`。
- 成功路径：直接返回 `ret.value()`，不碰 `errno`。

**预期结果**：调用者 `my_open("/x", 0)` 在底层失败时得到 `-1`，且 `errno`/`libc_errno` 等于底层返回的负错误码的绝对值（如 `ENOENT`）。由于 `fake_syscall` 是假想的，具体数值**待本地验证**（可替换成真实的 `syscall_impl<int>(SYS_openat, ...)` 来跑通）。

#### 4.3.5 小练习与答案

**练习 1**：syscall 封装层为什么是 `return Error(-ret);` 而不是 `return Error(ret);`？

**参考答案**：Linux 内核失败时返回的是**负**错误码（如 `-ENOENT`），而 `errno` 按标准应是**正**值。封装层用 `-ret` 把负数取反成正错误码，再包进 `Error`，保证 `error()` 取出来的是可直接写进 `errno` 的正值。

**练习 2**：在公开入口点里，为什么是 `if (!ret)` 而不是 `if (ret.error())`？

**参考答案**：`if (!ret)` 用的是 `explicit operator bool`，判断的是“是否成功”（`has_value()`）。错误码本身也可能是 0 或其它值，直接判断 `error()` 既语义不清（0 在某些平台也是合法错误码），也容易把“成功”误判成“失败”。正确做法是用布尔判成败，再用 `error()` 取码。

**练习 3**：如果某个公开函数的失败约定不是返回 `-1` 而是返回 `NULL`（如 `fopen`），这套模式要改哪里？

**参考答案**：只改公开入口点的失败分支返回值（`return nullptr;` 而非 `return -1;`）与返回类型（指针而非 `int`）；内部 `ErrorOr`、`libc_errno = ret.error();` 的写法完全不变。这正体现了“内部传错”与“对外返回约定”两层是解耦的。

---

## 5. 综合实践

把本讲三块知识串起来，完成一次「错误处理全链路追踪 + 自造」任务：

1. **追踪**：从 `src/unistd/linux/dup.cpp` 出发，向上找到公开签名 `src/unistd/dup.h`，向下找到 syscall 封装 `src/__support/OSUtil/linux/syscall_wrappers/dup.h`，再找到 `error_or.h` → `cpp/expected.h` 与 `libc_errno.h` → `libc_errno.cpp`。画出这条完整链路上每个文件扮演的角色（建议画成纵向流程图）。
2. **模式归纳**：在 `src/unistd/linux/` 目录下用 `git grep -n "libc_errno = ret.error()"` 找出至少 3 个遵循同一模式的函数（如 `fchdir`、`dup3`、`fsync`），确认它们的失败分支都是“`libc_errno = ret.error(); return -1;`”这一句的变体，验证这套是通用约定而非 `dup` 独有。
3. **自造**：照 4.3.4 的范本，为假想函数 `my_open` 写出 `ErrorOr` 版内部函数与 `LLVM_LIBC_FUNCTION` 版公开函数，并写出对应 `CMakeLists.txt` 的 `DEPENDS` 列表（至少包含 `libc.src.__support.error_or` 与 `libc.src.__support.libc_errno`）。
4. **反思**：回答一个问题——为什么 LLVM-libc 要把“内部错误类型（`ErrorOr`）”和“对外错误通道（`libc_errno`）”设计成两套独立机制，而不是让函数直接返回 `int` 并直接写 `errno`？（提示：可测试性、平台可移植性、syscall 与公开 API 的解耦。）

完成上述四步后，你应该能独立读懂并实现任何一个“失败需报告 errno”的 LLVM-libc 入口点。

## 6. 本讲小结

- `ErrorOr<T>` 是 LLVM-libc 内部统一的「值或错误」类型，本质是 `cpp::expected<T, int>` 的别名；`Error` 是 `cpp::unexpected<int>` 的别名，用来在构造时与成功值区分。
- `cpp::expected` 用 `union { T; E; }` 加一个 `bool is_expected` 实现判别式联合，提供 `has_value()`/`value()`/`error()`/`operator bool`/`operator*` 等接口，是 C++23 `std::expected` 的自包含子集（[u4-l2]）。
- `libc_errno` 是 `errno` 的抽象别名：内部实现一律用它、不直接碰 `<errno.h>`；它到底是什么由 `LIBC_ERRNO_MODE` 六种取值决定，Full 默认 `THREAD_LOCAL`、Overlay 默认 `SYSTEM_INLINE`。
- 自带状态下 `libc_errno` 是自定义类型 `Errno` 的全局对象，靠重载 `operator=(int)` / `operator int()` 把读写转发到 `thread_local`/共享/外部存储；系统模式下它退化成指向系统 `errno` 的宏。
- 错误码常量（`EBADF` 等）经代理头 `hdr/errno_macros.h` 按构建模式分流：Full/Linux 取内核头与自带宏，Overlay 取系统 `<errno.h>`（[u3-l2]）。
- 端到端模式：内核返回负错误码 → syscall 封装 `Error(-ret)` 包成 `ErrorOr` 失败侧 → 公开入口点 `if(!ret){ libc_errno=ret.error(); return -1; }`，成功则 `return ret.value();`；`dup` 是这一模式的范本。

## 7. 下一步学习建议

- **横向对照更多入口点**：在 `src/unistd/linux/` 下阅读 `getcwd.cpp`、`readlink.cpp` 等返回指针/长度的函数，看失败约定从 `-1` 变成 `NULL`/负值时，端到端模式如何变形（仍遵循同一抽象）。
- **深入 OSUtil**：本讲的 syscall 封装属于 `__support/OSUtil`，[u8-l1（OSUtil 与 Linux 系统调用封装）] 会系统讲解它如何按 OS 与架构分派，与本讲互补。
- **连接启动与线程**：`THREAD_LOCAL` 模式依赖 TLS，而 TLS 的初始化在程序启动阶段完成——学完 [u8-l2（程序启动流程：crt1、do_start 与 TLS）] 后，你会看清“每线程 `errno`”在启动期是如何就位的。
- **回归错误类型本身**：若对 `expected`/`unexpected` 这种和类型感兴趣，可对比 [u4-l2] 里 `cpp::numeric_limits`、`cpp::span` 的设计，体会 `__support/CPP` 这一自包含标准库子集的统一风格。
