# 第一个入口点全流程：以 isalpha 为例

## 1. 本讲目标

前几讲我们建立了 LLVM-libc 的全局认知：目录怎么分、怎么构建、Overlay 与 Full 有什么区别。但这些都还是「骨架」。本讲要打通「一条完整的血肉」——挑一个最简单的函数 `isalpha`（判断字符是不是字母），把它从**规范 → 实现 → 注册 → 测试**四道工序完整走一遍。

读完本讲，你应该能够：

- 看懂 `isalpha` 这一套「五件套」（`.yaml` / `.h` / `.cpp` / `CMakeLists.txt` / `_test.cpp`）是如何彼此咬合的。
- 初步认识 `LLVM_LIBC_FUNCTION` 宏与 `LIBC_NAMESPACE_DECL` 命名空间的作用（深入版本留给 u2-l2）。
- 理解单元测试是如何引用「内部命名空间下的函数」来做断言的。
- 在心里建立一张「一个函数如何存在于整个体系」的流转图，为后面给任何函数定位打下直觉。

> 本讲是「读」出来的，不是「跑」出来的。你不需要先构建项目就能读懂；但第 4 节和第 5 节会给出「如果你已经按 u1-l3 构建过，可以这样验证」的可选步骤。

## 2. 前置知识

本讲假设你已经读过 u1-l2（目录结构）和 u1-l3（构建入门）。下面几个词会反复出现，先一句话解释：

- **入口点（entrypoint）**：LLVM-libc 把每一个对外公开的函数/变量都当成一个**独立的、可单独构建的单元**。`isalpha` 就是一个入口点。它不是「某个大文件里的一行」，而是「一个有自己的源文件、自己的头文件、自己的 CMake 注册条目的小积木」。
- **公共头文件（public header）**：用户 `#include <ctype.h>` 时看到的那一层声明。
- **实现头（implementation header）**：放在 `src/ctype/isalpha.h`，只给实现和测试用、不对外公开的内部声明。
- **命名空间隔离**：所有内部符号都被关进一个叫 `__llvm_libc` 的 C++ 命名空间里，再通过特殊手段「映射」成对外可见的 C 符号 `isalpha`。
- **YAML 规范**：用机器可读的格式描述「`ctype.h` 里有哪些函数、各自签名是什么」。

如果你对 C 标准库的 `isalpha` 本身不熟，它就是：传入一个 `int`（通常是字符值），返回非零表示「是字母」，返回 0 表示「不是字母」。

## 3. 本讲源码地图

本讲涉及的关键文件，按「流转顺序」排列：

| 文件 | 角色 | 一句话作用 |
| --- | --- | --- |
| `include/ctype.yaml` | 规范 | 用 YAML 描述整个 `ctype.h` 有哪些函数及签名，`isalpha` 在其中占一条。 |
| `src/ctype/isalpha.cpp` | 实现 | `isalpha` 的真正逻辑：边界检查后委托给内部工具。 |
| `src/ctype/isalpha.h` | 实现头 | 在内部命名空间里声明 `int isalpha(int c);`，供实现与测试引用。 |
| `src/ctype/CMakeLists.txt` | 构建 | 用 `add_entrypoint_object(isalpha ...)` 把这一个函数注册成一个可构建单元。 |
| `src/__support/ctype_utils.h` | 内部工具 | 提供 `internal::isalpha(char)` 等公共判定逻辑，是真正「干活」的地方。 |
| `test/src/ctype/isalpha_test.cpp` | 测试 | 对内部命名空间下的 `isalpha` 做断言式单元测试。 |
| `config/linux/x86_64/entrypoints.txt` | 平台裁剪 | 把 `libc.src.ctype.isalpha` 登记进 Linux/x86_64 平台的「事实清单」。 |

记住这张表的大致顺序：**规范（yaml）→ 实现头（.h）→ 实现（.cpp，借助 utils）→ 构建（CMake）→ 平台登记（entrypoints.txt）→ 测试（_test.cpp）**。第 5 节会把它画成一张图。

## 4. 核心概念与源码讲解

### 4.1 YAML 规范：用数据描述一个头文件

#### 4.1.1 概念说明

传统 libc 的 `ctype.h` 是**手写**的：维护者直接在 `.h` 文件里敲出 `int isalpha(int);` 这样的声明。LLVM-libc 不一样——公共头文件是**生成**出来的。维护者只维护一份机器可读的「规范」`include/ctype.yaml`，再由 hdrgen 工具把它翻译成真正的 `.h`。

为什么要这么做？因为「一个函数的签名」在多处都要用：生成公共头、生成文档、做 ABI 校验……与其在五个地方各写一遍、改一处漏四处，不如只写一份 YAML 当「唯一事实来源」。

> YAML 本身**不是实现**，它只描述「这个函数长什么样」，不描述「它怎么算」。怎么算是 `.cpp` 的事。

#### 4.1.2 核心流程

一份 `ctype.yaml` 的骨架是：

```text
header: ctype.h        # 这份规范对应哪个公共头
standards: [stdc]      # 这个头遵循哪些标准
enums: []              # 该头定义的枚举（ctype.h 没有）
objects: []            # 该头定义的全局对象（ctype.h 没有）
functions:             # 该头里的所有函数，逐个列出
  - name: isalpha
    standards: [stdc]
    return_type: int
    arguments:
      - type: int
  ...
```

每个函数都是一个 YAML 列表项，字段含义直观：`name` 是函数名，`return_type` 是返回类型，`arguments` 是参数列表（每个参数给出 `type`）。`standards` 标明它属于哪条标准（`stdc` = C 标准，`posix` = POSIX，`gnu` = GNU 扩展）。

#### 4.1.3 源码精读

看 `isalpha` 在 YAML 里的真实条目：

[include/ctype.yaml:L13-L18](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/include/ctype.yaml#L13-L18) —— `isalpha` 的规范条目：返回 `int`、接受一个 `int` 参数、遵循 `stdc`。

```yaml
  - name: isalpha
    standards:
      - stdc
    return_type: int
    arguments:
      - type: int
```

这份 YAML 会被 hdrgen 工具链消费，最终产出公共头里类似 `int isalpha(int);` 的声明。**hdrgen 的细节是 u3-l1 的主题**，本讲你只要记住：「YAML 是签名的事实来源」即可。

> 顺带留意：同一个 `ctype.yaml` 里还列着 `isalnum`、`isdigit`、`isalpha_l`（带 locale 的版本）等所有 ctype 函数。一个头对应一份 YAML，而不是一个函数一份。

#### 4.1.4 代码实践

1. **实践目标**：建立「YAML 字段 → C 声明」的直觉。
2. **操作步骤**：打开 [include/ctype.yaml](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/include/ctype.yaml#L1-L201)，找到 `isalpha_l` 条目（带 locale 的版本），对比它与 `isalpha` 的 `arguments` 有何不同。
3. **需要观察的现象**：`isalpha_l` 比 `isalpha` 多一个 `type: locale_t` 的参数。
4. **预期结果**：你能在脑海里把它翻译成 `int isalpha_l(int, locale_t);`，从而体会「YAML 列表项的每个字段都直接对应 C 声明的一个零件」。
5. 本步为纯阅读，无需运行命令。

#### 4.1.5 小练习与答案

**练习 1**：YAML 里 `standards` 字段对一个函数意味着什么？为什么需要它？
**答案**：它标明该函数属于哪条标准（如 `stdc`/`posix`/`gnu`）。不同平台、不同构建模式（Full/Overlay）会据此决定要不要暴露这个函数——比如某些 `_l` 后缀的 POSIX 函数只在 Full 模式下构建（见 4.3.3）。

**练习 2**：如果我想给 `ctype.h` 新增一个函数 `isvowel`，YAML 层面要改哪里？
**答案**：在 `functions:` 列表里追加一项，写好 `name`/`return_type`/`arguments`/`standards`。仅此而已——签名侧的改动就一处。（完整新增流程见 u11-l3。）

---

### 4.2 实现与内部头：`isalpha` 真正在做什么

#### 4.2.1 概念说明

规范只说「签名长这样」，真正干活的是 `src/ctype/isalpha.cpp`。但它做得很「薄」：它**只负责把公开入口打磨好**（边界处理、类型转换），然后把核心判定**委托**给一个内部工具 `ctype_utils.h`。

这种「入口薄壳 + 内部工具」的分层是 LLVM-libc 的常见模式：入口点关心「对外契约」，内部工具关心「可复用的纯逻辑」。这样多个入口点（比如 `isalpha` 和未来的兄弟函数）能共享同一份经过验证的判定代码。

本模块还会初步认识两个贯穿全项目的宏：

- `LIBC_NAMESPACE_DECL`：把内部符号关进带「隐藏可见性」的命名空间。
- `LLVM_LIBC_FUNCTION`：把那个隐藏的 C++ 符号「映射」成对外可见的 C 符号 `isalpha`。

#### 4.2.2 核心流程

`isalpha` 入口点的执行逻辑（伪代码）：

```text
入口 isalpha(int c):
    若 c < 0 或 c > UCHAR_MAX:   # 边界外，按 C 标准返回 0
        返回 0
    把 c 转成 char，调用 internal::isalpha(char)
    把 bool 结果转成 int 返回
```

为什么要有边界检查？因为 C 标准规定：`isalpha` 的实参必须是 `unsigned char` 的值或 `EOF`（通常为 -1）。传入负值（非 EOF）或超过 255 的值是**未定义行为**。LLVM-libc 选择了「宽容」处理——越界直接返回 0，而不是访问越界的查表内存，这是更安全的选择。

边界范围记为 \( [0,\ \text{UCHAR\_MAX}] = [0,\ 255] \)。

#### 4.2.3 源码精读

先看实现头 [src/ctype/isalpha.h:L14-L18](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/src/ctype/isalpha.h#L14-L18) —— 在 `LIBC_NAMESPACE_DECL` 命名空间内声明原型，供实现与测试引用。

```cpp
namespace LIBC_NAMESPACE_DECL {
int isalpha(int c);
} // namespace LIBC_NAMESPACE_DECL
```

`LIBC_NAMESPACE_DECL` 是什么？看 [src/__support/macros/config.h:L66](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/src/__support/macros/config.h#L66) —— 在 Clang 下它展开为带隐藏可见性的命名空间：

```cpp
#define LIBC_NAMESPACE_DECL [[gnu::visibility("hidden")]] LIBC_NAMESPACE
```

而 `LIBC_NAMESPACE` 的值由 CMake 在构建时注入，默认是 `__llvm_libc`（见 [CMakeLists.txt:L58-L78](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/CMakeLists.txt#L58-L78)，其中 `add_compile_definitions(LIBC_NAMESPACE=${LIBC_NAMESPACE})` 把它传给编译器）。所以上面的声明实际等价于：

```cpp
namespace __llvm_libc { int isalpha(int c); }
```

并且 `[[gnu::visibility("hidden")]]` 保证这个命名空间里的符号**默认不对外导出**——这就把「内部实现」和「系统 libc 同名符号」隔离开了。

再看实现 [src/ctype/isalpha.cpp:L16-L24](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/src/ctype/isalpha.cpp#L16-L24) —— 完整的入口实现，注意 `LLVM_LIBC_FUNCTION` 宏与边界检查：

```cpp
namespace LIBC_NAMESPACE_DECL {

LLVM_LIBC_FUNCTION(int, isalpha, (int c)) {
  if (c < 0 || c > cpp::numeric_limits<unsigned char>::max())
    return 0;
  return static_cast<int>(internal::isalpha(static_cast<char>(c)));
}

} // namespace LIBC_NAMESPACE_DECL
```

逐行拆解：

- `LLVM_LIBC_FUNCTION(int, isalpha, (int c))`：这是一个宏（**初步认识即可**，深入留给 u2-l2）。它的核心作用是：让下面这个函数体**既存在于隐藏的 `__llvm_libc` 命名空间内**，又**通过 `asm` 别名对外暴露成 C 符号 `isalpha`**。换句话说，它在「C++ 内部名」与「C 公开名」之间架了一座桥。可在 [src/__support/common.h:L80-L84](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/src/__support/common.h#L80-L84) 看到 `LLVM_LIBC_FUNCTION` 宏的总入口，它根据参数个数分派到 `IMPL_3`/`IMPL_4`，后两者最终用 `asm(...)` 产出公开别名（[common.h:L56-L63](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/src/__support/common.h#L56-L63)）。
- `cpp::numeric_limits<unsigned char>::max()`：等于 255，来自自包含的 [src/__support/CPP/limits.h:L38-L44](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/src/__support/CPP/limits.h#L38-L44)。注意它带 `cpp::` 前缀，说明用的是 LLVM-libc **自带的 C++ 工具子集**（u4-l2 主题），而不是宿主 `std::numeric_limits`。
- `internal::isalpha(static_cast<char>(c))`：边界内的值转成 `char` 后，交给内部工具判定。**这就是 `isalpha` 真正调用的辅助函数。**

最后看那个「真正干活」的内部工具 [src/__support/ctype_utils.h:L244-L302](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/src/__support/ctype_utils.h#L244-L302) —— `internal::isalpha(char)` 用一个把 `a`–`z`、`A`–`Z` 全部列出的 `switch` 返回 `true`，`default` 返回 `false`：

```cpp
LIBC_INLINE constexpr bool isalpha(char ch) {
  switch (ch) {
  case 'a': ... case 'z':
  case 'A': ... case 'Z':
    return true;
  default:
    return false;
  }
}
```

注意它返回的是 `bool`，所以入口点要用 `static_cast<int>` 转回 C 期望的 `int`。文件顶部 [ctype_utils.h:L18-L28](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/src/__support/ctype_utils.h#L18-L28) 有一段醒目警告：**不要试图「优化」这些 switch**——这种逐字符列举的形式让编译器生成高效代码，并且**与字符编码无关**（不依赖 ASCII 连续区间，因此也能用于 EBCDIC 等）。这是把判定逻辑下沉到 `ctype_utils` 的一个关键收益。

> 小结：`isalpha` 入口层只做「打磨」（边界 + 类型转换），真正的「是不是字母」由 `internal::isalpha` 决定。这就是本讲实践题要你定位的那个辅助函数。

#### 4.2.4 代码实践

1. **实践目标**：确认入口点与内部工具的委托关系，并理解边界检查的意义。
2. **操作步骤**：
   - 打开 `src/ctype/isalpha.cpp`，确认它 `#include "src/__support/ctype_utils.h"`（[isalpha.cpp:L9-L14](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/src/ctype/isalpha.cpp#L9-L14)）。
   - 在 `ctype_utils.h` 里找到 `internal::isalpha(char)`（L244 起），数一数它列了多少个 case。
   - 想一个问题：如果把入口点里的边界检查 `if (c < 0 || c > ...) return 0;` 删掉，直接 `internal::isalpha(static_cast<char>(c))`，传入 `c = 200`（一个超过 ASCII 的值）会发生什么？
3. **需要观察的现象**：`internal::isalpha` 接受的是 `char`，`static_cast<char>(200)` 的结果与平台 `char` 是否有符号相关。
4. **预期结果**：边界检查不是多余的——它保证只有 \( [0,\ 255] \) 的值进入判定，避免把超大或负的 `int` 截断后误判。**不要真的去改源码**（本讲只读不写），在脑中推演即可。
5. 本步为源码阅读型实践，无需运行。

#### 4.2.5 小练习与答案

**练习 1**：`LLVM_LIBC_FUNCTION(int, isalpha, (int c))` 这一行，和直接写 `int isalpha(int c)` 相比，多了哪两件关键的事？
**答案**：（1）把函数符号留在隐藏的 `__llvm_libc` 命名空间内，避免与系统 libc 同名符号冲突；（2）通过 `asm` 别名对外暴露成标准的 C 符号名 `isalpha`，供用户程序链接。

**练习 2**：为什么入口点不直接把 `switch` 写在自己里面，而要委托给 `ctype_utils.h`？
**答案**：因为 `isalpha`/`isdigit`/`isalnum` 等一族函数会复用这些判定逻辑（例如 `isalnum` 可以由 `isalpha || isdigit` 派生）。下沉到 `ctype_utils` 能让所有入口点共享一份**与编码无关、被编译器优化好**的实现，避免重复。

---

### 4.3 CMake 注册：把一个函数变成可构建单元

#### 4.3.1 概念说明

光有 `.cpp` 还不够——构建系统不知道它的存在。LLVM-libc 用 `add_entrypoint_object` 这个 CMake 自定义规则，把 `isalpha` 登记为一个**独立的构建对象**（entrypoint object）。登记时要告诉构建系统三件事：源文件是谁、头文件是谁、它依赖哪些内部模块。

登记之后，这个对象还不能自动进 `libc.a`——还得由「平台清单」决定它是否被纳入某个平台的产物。这就是 `entrypoints.txt` 的职责。

#### 4.3.2 核心流程

```text
src/ctype/CMakeLists.txt:
    add_entrypoint_object(isalpha, SRCS=..., HDRS=..., DEPENDS=...)
            │
            │  定义了一个可构建对象 libc.src.ctype.isalpha
            ▼
config/linux/x86_64/entrypoints.txt:
    把 libc.src.ctype.isalpha 列进 TARGET_LIBC_ENTRYPOINTS
            │
            │  平台决定「这个对象是否进 Linux/x86_64 的 libc.a」
            ▼
lib/CMakeLists.txt 聚合 → 最终产物
```

关键点：**CMake 注册让函数「可被构建」，平台清单让函数「被纳入某平台」。** 两者分离，正是 LLVM-libc 能在多平台渐进式落地的设计基础（详见 u2-l1、u2-l4）。

#### 4.3.3 源码精读

看 `isalpha` 的注册条目 [src/ctype/CMakeLists.txt:L13-L22](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/src/ctype/CMakeLists.txt#L13-L22)：

```cmake
add_entrypoint_object(
  isalpha
  SRCS
    isalpha.cpp
  HDRS
    isalpha.h
  DEPENDS
    libc.src.__support.CPP.limits
    libc.src.__support.ctype_utils
)
```

字段含义：

- 第一行 `isalpha`：入口点名（也是构建目标名的一部分，最终是 `libc.src.ctype.isalpha`）。
- `SRCS isalpha.cpp`：源文件。
- `HDRS isalpha.h`：实现头。
- `DEPENDS`：**它依赖的内部模块**。注意 `DEPENDS` 里列的不是「随便的依赖」，而是「本入口点 include 了、但不是自己头文件的那些东西」。`isalpha.cpp` include 了 `CPP/limits.h` 和 `ctype_utils.h`，所以这里就列 `libc.src.__support.CPP.limits` 和 `libc.src.__support.ctype_utils`。

> 一个值得留意的细节：对比同文件的 `isalnum`（[CMakeLists.txt:L1-L11](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/src/ctype/CMakeLists.txt#L1-L11)），它的 `DEPENDS` 里**多了** `libc.include.ctype`，而 `isalpha` 没有。这说明 `add_entrypoint_object` 的 `DEPENDS` 只声明「实现真正用到的」依赖，不强求把整份公共头都挂上——`isalpha.cpp` 并没有直接 include 公共 `ctype.h`，所以不列。这种「按需声明」是 LLVM-libc CMake 的惯例。

再看平台登记 [config/linux/x86_64/entrypoints.txt:L12-L14](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/config/linux/x86_64/entrypoints.txt#L12-L14) —— `libc.src.ctype.isalpha` 出现在 `# ctype.h entrypoints` 分组下，说明 Linux/x86_64 平台把它纳入了产物。

最后，注意文件后半段有一个 Overlay 守卫 [src/ctype/CMakeLists.txt:L163-L166](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/src/ctype/CMakeLists.txt#L163-L166)：

```cmake
# Do not build the locale versions in overlay mode.
if(NOT LLVM_LIBC_FULL_BUILD)
  return()
endif()
```

它让所有带 `_l`（locale）后缀的 ctype 函数**只在 Full 模式下构建**，Overlay 模式直接提前 `return()` 跳过。这是 u1-l4 讲过的「Overlay 只能放纯算法函数、不能放依赖私有 ABI 的函数」在源码里的具体体现——locale 函数依赖 `locale_t` 这类平台相关类型，所以被挡在 Overlay 之外。而 `isalpha` 本身在这条 `return()` **之前**注册，两种模式都会构建。

#### 4.3.4 代码实践

1. **实践目标**：验证「CMake 注册 + 平台登记」是两件分开的事。
2. **操作步骤**：
   - 在 `src/ctype/CMakeLists.txt` 中确认 `isalpha` 被 `add_entrypoint_object` 注册（L13）。
   - 在 `config/linux/x86_64/entrypoints.txt` 中确认 `libc.src.ctype.isalpha` 被登记（L14）。
   - 再翻到任意一个**别的**平台目录（如 `config/darwin/` 或 `config/windows/`），用搜索看 `isalpha` 是否也在那里登记。
3. **需要观察的现象**：不同平台的 `entrypoints.txt` 列表可能不同——某些平台可能尚未登记 `isalpha`。
4. **预期结果**：你会直观感受到「同一个入口点在不同平台上的可用性由各自 `entrypoints.txt` 决定」，这正是「retargetable（可重定向）」的体现。
5. 待本地验证（取决于你检出时其他平台目录是否完整）。

#### 4.3.5 小练习与答案

**练习 1**：`add_entrypoint_object` 的 `DEPENDS` 为什么要把 `libc.src.__support.ctype_utils` 列出来？
**答案**：因为 `isalpha.cpp` `#include` 了 `ctype_utils.h` 并调用了 `internal::isalpha`。`DEPENDS` 表达的是「编译/链接本入口点必须先有的内部依赖」，列出它能保证构建顺序，并让头文件搜索路径正确。

**练习 2**：为什么 locale 版的 `isalpha_l` 在 Overlay 模式下不构建？
**答案**：因为 Overlay 模式产出的 `libllvmlibc.a` 只能装「不依赖实现私有 ABI 的纯算法函数」（u1-l4 已讲）。locale 函数依赖 `locale_t` 等平台相关类型与私有布局，放进 Overlay 会破坏 ABI 兼容，所以用 `if(NOT LLVM_LIBC_FULL_BUILD) return()` 提前跳过。

---

### 4.4 单元测试：如何对内部命名空间下的函数做断言

#### 4.4.1 概念说明

LLVM-libc 自带一套与 GoogleTest 风格相似的测试框架（u10-l1 主题），用 `TEST(...)`、`EXPECT_EQ`/`EXPECT_NE` 这样的宏写测试。一个特别之处：测试**直接 include 实现头** `src/ctype/isalpha.h`，然后调用 **`LIBC_NAMESPACE::isalpha(...)`**——也就是内部命名空间下那个隐藏的 C++ 函数，而不是走公开的 C 符号。

这样做的好处是：测试可以在不经过完整 C ABI 链接的情况下，精确、内聚地验证实现逻辑。

#### 4.4.2 核心流程

```text
测试文件 includes:
    src/ctype/isalpha.h      # 拿到 LIBC_NAMESPACE::isalpha 的声明
    test/UnitTest/Test.h     # 拿到 TEST / EXPECT_* 宏
        │
        ▼
写 TEST(LlvmLibcIsAlpha, 名字) { 用 EXPECT_* 断言 LIBC_NAMESPACE::isalpha(...) }
        │
        ▼
test/src/ctype/CMakeLists.txt 用 add_libc_test(isalpha_test, DEPENDS libc.src.ctype.isalpha) 注册
```

关键点：测试对被测入口点显式 `DEPENDS`，确保被测对象先被构建。

#### 4.4.3 源码精读

测试文件的头部 [test/src/ctype/isalpha_test.cpp:L9-L12](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/test/src/ctype/isalpha_test.cpp#L9-L12) —— 注意它 include 的是**实现头**，并引用了 `LIBC_NAMESPACE`：

```cpp
#include "src/__support/CPP/span.h"
#include "src/ctype/isalpha.h"
#include "test/UnitTest/Test.h"
```

第一个手写用例 [isalpha_test.cpp:L33-L42](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/test/src/ctype/isalpha_test.cpp#L33-L42) —— 用若干代表性字符做断言，注意所有调用都带 `LIBC_NAMESPACE::` 前缀：

```cpp
TEST(LlvmLibcIsAlpha, SimpleTest) {
  EXPECT_NE(LIBC_NAMESPACE::isalpha('a'), 0);   // 字母 → 非零
  EXPECT_NE(LIBC_NAMESPACE::isalpha('B'), 0);
  EXPECT_EQ(LIBC_NAMESPACE::isalpha('3'), 0);   // 数字 → 0
  EXPECT_EQ(LIBC_NAMESPACE::isalpha(' '), 0);   // 空格 → 0
  EXPECT_EQ(LIBC_NAMESPACE::isalpha('?'), 0);
  EXPECT_EQ(LIBC_NAMESPACE::isalpha('\0'), 0);
  EXPECT_EQ(LIBC_NAMESPACE::isalpha(-1), 0);    // EOF/负值 → 0
}
```

注意 `EXPECT_NE(..., 0)`（不等于 0）而不是 `EXPECT_TRUE`——因为 C 标准只保证「真返回非零」，并不保证返回 1。这个细节体现了测试对标准语义的尊重。

第二个用例是「全覆盖扫描」[isalpha_test.cpp:L44-L54](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/test/src/ctype/isalpha_test.cpp#L44-L54) —— 遍历 \(-255\) 到 \(254\) 的每个值，借助一个字母表常量 `ALPHA_ARRAY` 和辅助函数 `in_span` 判定「这个值是不是字母」，再和 `isalpha` 的结果比对：

```cpp
TEST(LlvmLibcIsAlpha, DefaultLocale) {
  for (int ch = -255; ch < 255; ++ch) {
    if (in_span(ch, ALPHA_ARRAY))
      EXPECT_NE(LIBC_NAMESPACE::isalpha(ch), 0);
    else
      EXPECT_EQ(LIBC_NAMESPACE::isalpha(ch), 0);
  }
}
```

这个用例直接验证了 4.2 讲的边界行为：负值（非 EOF）也被当作「不是字母」返回 0。

测试本身的注册在 [test/src/ctype/CMakeLists.txt:L13-L21](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/test/src/ctype/CMakeLists.txt#L13-L21) —— 用 `add_libc_test` 把 `isalpha_test.cpp` 注册成测试目标，`DEPENDS` 指向被测对象 `libc.src.ctype.isalpha`：

```cmake
add_libc_test(
  isalpha_test
  SUITE
    libc-ctype-tests
  SRCS
    isalpha_test.cpp
  DEPENDS
    libc.src.ctype.isalpha
)
```

#### 4.4.4 代码实践

1. **实践目标**：亲自跑一次 `isalpha` 的单元测试，验证整条链路（如果你已按 u1-l3 完成构建）。
2. **操作步骤**（**可选，仅当你已构建过**）：
   - 在构建目录里运行整个 ctype 测试套件：`ninja libc-ctype-tests`（先构建）。
   - 运行某个单独的单元测试目标。按 u1-l3 介绍的单测命名约定，目标形如 `libc.test.src.ctype.isalpha_test.__unit__`；确切名称请用 `ninja -t targets all | grep isalpha_test` 在本地确认后再运行。
3. **需要观察的现象**：测试通过；若你故意好奇，可在脑中把 `EXPECT_NE(-1)` 改成 `EXPECT_NE` 一个非法值会怎样——**但不要真改源码**。
4. **预期结果**：`SimpleTest` 与 `DefaultLocale` 两个用例全部 PASS。
5. 如果你尚未构建项目，本步可跳过，改为「阅读 `isalpha_test.cpp` 逐行解释每个断言为何成立」的源码阅读型实践。

#### 4.4.5 小练习与答案

**练习 1**：测试里为什么写 `EXPECT_NE(isalpha('a'), 0)` 而不是 `EXPECT_TRUE(isalpha('a'))`？
**答案**：因为 C 标准只规定「真 = 返回非零」，并未规定具体返回值是 1。用「不等于 0」更贴合标准语义，不会因为实现返回了 8、2 等非零值而误判失败。

**练习 2**：测试文件为什么 include `src/ctype/isalpha.h` 而不是公共的 `<ctype.h>`？
**答案**：因为它要测的是 **LLVM-libc 自己的实现**，且要调用内部命名空间下的 `LIBC_NAMESPACE::isalpha`。实现头声明了该命名空间内的原型；公共头只会暴露 C 符号 `isalpha`，拿不到 `LIBC_NAMESPACE::` 形式的引用。

---

## 5. 综合实践：画出「isalpha 从 YAML 到测试」的流转图

把本讲四个模块串起来，完成下面这个贯穿任务。

### 任务

1. **画一张流转图**（纸上或文本里），把下列文件按它们在「生命周期」中的出场顺序连起来，并标注每个文件扮演的角色：

   ```text
   include/ctype.yaml            ──(签名规范)──▶  hdrgen ──▶ 公共 <ctype.h>（u3-l1 详讲）
        │
        │  （规范只描述签名，与下面的实现是两条相对独立的线）
        ▼
   src/ctype/isalpha.h           ──(内部原型声明)
   src/ctype/isalpha.cpp         ──(入口薄壳: 边界检查 + 委托)
        │  #include
        ▼
   src/__support/ctype_utils.h   ──(internal::isalpha: 真正判定)
        │
        │  被构建系统登记
        ▼
   src/ctype/CMakeLists.txt      ──(add_entrypoint_object 注册)
        │
        ▼
   config/linux/x86_64/entrypoints.txt  ──(平台清单: 纳入 Linux/x86_64 产物)
        │
        │  被测试引用
        ▼
   test/src/ctype/isalpha_test.cpp      ──(对 LIBC_NAMESPACE::isalpha 做断言)
   test/src/ctype/CMakeLists.txt        ──(add_libc_test 注册测试)
   ```

   在每个节点旁用一句话写清它的角色（例如「边界守门员」「事实来源」「可构建单元」等）。

2. **定位 `ctype_utils.h` 并回答**：`isalpha` 实际调用的内部辅助函数全名是什么？它的返回类型是什么？入口点又如何把这个返回值转成 C 期望的 `int`？
   - **参考答案**：全名是 `LIBC_NAMESPACE_DECL::internal::isalpha(char)`，定义在 [ctype_utils.h:L244](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/src/__support/ctype_utils.h#L244)；返回类型是 `bool`；入口点在 [isalpha.cpp:L21](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/src/ctype/isalpha.cpp#L21) 用 `static_cast<int>(...)` 把 `bool` 转成 `int` 返回。

3. **延伸思考**（可选）：用同样的方法，把同目录下的 `isdigit` 也「流转」一遍——它的 `.yaml` 条目、`.cpp`、`CMakeLists` 注册、`_test.cpp` 分别在哪？你会发现四件套结构几乎完全对称，这就是 LLVM-libc 函数族的一致性所在。

## 6. 本讲小结

- LLVM-libc 的每个公开函数都是一个**独立的入口点**，由「五件套」协作：`.yaml`（规范）、`.h`（内部原型）、`.cpp`（实现）、`CMakeLists.txt`（构建注册）、`_test.cpp`（测试）。
- **`include/ctype.yaml` 是签名的事实来源**：`isalpha` 在其中是一条「返回 int、接受 int、遵循 stdc」的列表项，被 hdrgen 消费生成公共头（详见 u3-l1）。
- **入口实现很薄**：`isalpha.cpp` 只做边界检查（把输入限制在 \( [0,\ 255] \)）与类型转换，真正的判定委托给 `ctype_utils.h` 里的 `internal::isalpha(char)`——后者用与编码无关的 `switch` 实现。
- 两个宏贯穿全项目：`LIBC_NAMESPACE_DECL` 把内部符号关进带隐藏可见性的 `__llvm_libc` 命名空间；`LLVM_LIBC_FUNCTION` 再用 `asm` 别名把它映射成公开 C 符号（深入版见 u2-l2）。
- **CMake 注册与平台登记是两件事**：`add_entrypoint_object` 让函数「可被构建」，`config/<os>/<arch>/entrypoints.txt` 决定它「是否进某平台产物」；Overlay 守卫 `if(NOT LLVM_LIBC_FULL_BUILD) return()` 把 locale 版函数挡在 Overlay 之外。
- **测试直接引用内部命名空间**：`isalpha_test.cpp` include 实现头，用 `EXPECT_*` 对 `LIBC_NAMESPACE::isalpha` 断言；`EXPECT_NE(..., 0)` 的写法体现了对「真即非零」标准语义的尊重。

## 7. 下一步学习建议

本讲只是「打通一条链路」，很多概念只是初识。建议接下来：

- 想彻底搞懂 `LLVM_LIBC_FUNCTION` 宏如何用 `asm` 别名做符号映射、`LIBC_NAMESPACE_DECL` 为何要隐藏可见性 → 读 **u2-l2（实现规范与核心宏）**。
- 想理解入口点为何要做成独立粒度、`add_entrypoint_object` 的完整生命周期、以及 `entrypoints.txt` 如何作为平台「事实来源」 → 读 **u2-l1（入口点机制）** 与 **u2-l4（平台配置体系）**。
- 想看 hdrgen 如何把本讲的 YAML 翻译成公共头 → 读 **u3-l1（头文件生成管线）**。
- 想了解 `internal::isalpha` 所在的 `__support` 体系到底沉淀了哪些公共能力 → 读 **u4-l1（`__support` 总览）**。
- 想看测试框架的 `TEST`/`EXPECT_*` 宏到底怎么定义、如何注册 → 读 **u10-l1（单元测试框架）**。

读完这些，你就能从一个 `isalpha` 扩展到「理解任意一个 LLVM-libc 函数的完整生命」。
