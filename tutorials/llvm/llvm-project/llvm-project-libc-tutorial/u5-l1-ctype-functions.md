# ctype 函数族与 ctype_utils

## 1. 本讲目标

本讲以 `ctype.h` 里的字符判定/转换函数为样本，把前面几讲建立的「入口点 + `__support` 下沉 + CMake 依赖」三件套，落到一个具体、可读、行数极少的函数族上。读完本讲你应当能够：

- 看懂 `isalpha` 这类函数的 `.cpp` 入口点为何如此「薄」，真正的判定逻辑沉到了哪里。
- 解释 `ctype_utils.h` 为什么坚持用「逐字符 case 标签」的 switch/case 写法，并理解「编码无关（encoding independent）」的含义。
- 说出 `isalpha(int c)` 对负值、对超过 `unsigned char` 范围的输入的处理约定，以及它和 `tolower` 在边界处理上的**不对称**。
- 自己为一个 ctype 风格的函数补出实现、CMake 依赖与边界处理。

## 2. 前置知识

本讲假设你已经读过以下讲义：

- **u1-l5 第一个入口点全流程：以 isalpha 为例**：知道入口点的「五件套」（yaml / 内部头 / cpp / CMake / 测试），以及 `LLVM_LIBC_FUNCTION`、`LIBC_NAMESPACE_DECL` 的作用。
- **u2-l2 实现规范与核心宏**：知道 `LLVM_LIBC_FUNCTION(int, isalpha, (int c))` 如何用 asm 别名把内部 C++ 函数映射成公开 C 符号 `isalpha`。
- **u4-l1 __support 总览与设计哲学**：知道 `src/__support` 是所有入口点共享的「私有标准库」，入口点经 CMake 的 `DEPENDS` 引用它，且 `__support` 不产生公开 C 符号。
- **u4-l2 CPP 子集工具库**：知道 `cpp::numeric_limits` 是自带的标准库子集，用来取代魔法数字。

几个名词先统一：

- **入口点（entrypoint）**：对外公开的一个函数或变量，是一个独立的构建单元。
- **判定函数（predicate）**：形如 `isxxx`，输入一个字符，返回非零表示「是」、返回零表示「否」。
- **转换函数（converter）**：形如 `tolower`/`toupper`，输入一个字符，返回转换后的字符（非目标字符则原样返回）。
- **unsigned char 范围**：即 \([0,\, 2^{CHAR\_BIT}-1]\)，在 `CHAR_BIT == 8` 时为 \([0, 255]\)。

## 3. 本讲源码地图

本讲围绕三个最小模块，对应以下源码文件：

| 文件 | 作用 | 本讲角色 |
| --- | --- | --- |
| [src/ctype/isalpha.cpp](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/src/ctype/isalpha.cpp) | `isalpha` 入口点实现 | 最薄入口点的范本 |
| [src/ctype/isalpha.h](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/src/ctype/isalpha.h) | `isalpha` 内部实现头 | 仅一行普通声明 |
| [src/\_\_support/ctype_utils.h](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/src/__support/ctype_utils.h) | 字符判定/转换的公共算法 | 真正的逻辑沉到这里 |
| [src/ctype/CMakeLists.txt](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/src/ctype/CMakeLists.txt) | ctype 入口点 CMake 注册 | 声明 DEPENDS 引用 utils |
| [src/\_\_support/CMakeLists.txt](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/src/__support/CMakeLists.txt) | `__support` 内部目标声明 | `ctype_utils` 头库的诞生地 |
| [src/\_\_support/CPP/limits.h](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/src/__support/CPP/limits.h) | 自带 `numeric_limits` | 提供边界值 `max()` |
| [test/src/ctype/isalpha_test.cpp](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/test/src/ctype/isalpha_test.cpp) | `isalpha` 单元测试 | 验证边界与字符类 |
| [config/linux/x86_64/entrypoints.txt](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/config/linux/x86_64/entrypoints.txt) | 平台入口点名单 | 决定哪些 ctype 函数进产物 |

> 阅读提示：本讲的几条结论对整个 ctype 家族（`isdigit`、`isalnum`、`isspace`、`tolower` 等）都成立，`isalpha` 只是被反复用作例子。

## 4. 核心概念与源码讲解

### 4.1 入口点实现：isalpha 的薄壳与三层分工

#### 4.1.1 概念说明

`isalpha` 在公共头文件里声明的签名是 `int isalpha(int c);`。它的 C 标准语义是：当 `c` 是一个字母时返回非零，否则返回零。但在 LLVM-libc 里，这个公开符号背后的实现体被刻意写得**极短**——它只做两件事：

1. 把 `int c` 校验到 `unsigned char` 的合法区间；
2. 把校验后的值转成 `char`，交给 `__support` 里的 `internal::isalpha(char)` 做真正的「是不是字母」判定，再把 `bool` 结果转回 `int`。

这就是 **入口点薄壳** 的含义：入口点负责「对外签名 + 边界契约 + 类型适配」，真正的算法下沉到 `__support`。这种分工带来的好处是：同一份 `internal::isalpha` 可以被 `isalpha`、locale 版的 `isalpha_l` 复用，被 `isalnum` 间接复用，还被 `str_to_integer` 等其他模块复用——逻辑只写一遍。

#### 4.1.2 核心流程

`isalpha(int c)` 的执行可以用下面这段伪代码描述：

```
function isalpha(c: int) -> int:
    if c < 0 or c > UCHAR_MAX:        # 边界守卫：负值或越界
        return 0                       # 判定函数统一返回 0
    ch = (char)c                       # 类型适配：int -> char
    return (int)internal::isalpha(ch)  # 委托真正算法，bool -> int
```

对应的「三层」是：

| 层 | 位置 | 职责 |
| --- | --- | --- |
| 公开层 | yaml 生成 / `LLVM_LIBC_FUNCTION` 宏 | 暴露标准 C 符号 `isalpha` |
| 入口点层 | `src/ctype/isalpha.cpp` | 边界守卫 + 类型转换 + 委托 |
| 算法层 | `src/__support/ctype_utils.h` 里的 `internal::isalpha(char)` | 编码无关的逐字符判定 |

#### 4.1.3 源码精读

先看入口点实现本身，全部逻辑只有 5 行：

[src/ctype/isalpha.cpp:16-24](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/src/ctype/isalpha.cpp#L16-L24) —— `isalpha` 入口点：`LLVM_LIBC_FUNCTION` 定义公开符号，函数体先做 `unsigned char` 区间守卫，再把真正的判定委托给 `internal::isalpha`。

```cpp
namespace LIBC_NAMESPACE_DECL {

LLVM_LIBC_FUNCTION(int, isalpha, (int c)) {
  if (c < 0 || c > cpp::numeric_limits<unsigned char>::max())
    return 0;
  return static_cast<int>(internal::isalpha(static_cast<char>(c)));
}

} // namespace LIBC_NAMESPACE_DECL
```

几个要点逐条对应到前面学过的概念：

- `LIBC_NAMESPACE_DECL` 把实现关进带隐藏可见性的 `__llvm_libc` 命名空间（见 u2-l2）。
- `LLVM_LIBC_FUNCTION(int, isalpha, (int c))` 用 asm 别名把内部符号映射成公开 C 符号 `isalpha`（见 u2-l2）。
- `cpp::numeric_limits<unsigned char>::max()` 来自自带 CPP 子集（见 u4-l2），等价于 `255`（当 `CHAR_BIT == 8`），但用工具代替魔法数字。
- `internal::isalpha` 是 `ctype_utils.h` 里的函数，入口点通过 `#include "src/__support/ctype_utils.h"` 拿到它。

入口点的内部实现头极其朴素，只有一行普通声明：

[src/ctype/isalpha.h:14-18](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/src/ctype/isalpha.h#L14-L18) —— 实现头里仅声明 `int isalpha(int c);`，注意它**不带** `LLVM_LIBC_FUNCTION` 宏，宏只写在 `.cpp` 的定义处。

这种「薄壳」并非 `isalpha` 独有，而是整个 ctype 家族的统一写法。把 `isdigit`、`isspace`、`isalnum` 摆在一起对比就能看出模板：

[src/ctype/isdigit.cpp:18-22](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/src/ctype/isdigit.cpp#L18-L22)、[src/ctype/isspace.cpp:18-22](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/src/ctype/isspace.cpp#L18-L22)、[src/ctype/isalnum.cpp:18-22](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/src/ctype/isalnum.cpp#L18-L22) —— 三个判定函数的入口点结构完全一致：同样的守卫、同样的 `static_cast<char>(c)`、只是分别委托给 `internal::isdigit` / `internal::isspace` / `internal::isalnum`。

更有意思的是「组合型」判定，它们不重复实现，而是**把多个 utils 原语拼起来**：

[src/ctype/isxdigit.cpp:18-24](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/src/ctype/isxdigit.cpp#L18-L24) —— `isxdigit`（是否十六进制数字）复用 `internal::isalnum` 与 `internal::b36_char_to_int`，组合出「是字母数字且其 36 进制值 < 16」的判定：

```cpp
const char ch = static_cast<char>(c);
return static_cast<int>(internal::isalnum(ch) &&
                        internal::b36_char_to_int(ch) < 16);
```

[src/ctype/ispunct.cpp:18-23](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/src/ctype/ispunct.cpp#L18-L23) —— `ispunct`（是否标点）同样复用现成原语：「非字母数字 且 是可打印图形字符」：

```cpp
const char ch = static_cast<char>(c);
return static_cast<int>(!internal::isalnum(ch) && internal::isgraph(ch));
```

这正是「公共逻辑下沉到 utils」的最大收益：连新判定都不必从头写，靠组合现成原语即可，杜绝了重复实现带来的不一致风险。

#### 4.1.4 代码实践

**实践目标**：体会「入口点薄壳」与「算法下沉」的分工。

**操作步骤**：

1. 打开 [src/ctype/isalpha.cpp](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/src/ctype/isalpha.cpp)，确认它的函数体里**没有任何**「是不是 a~z、A~Z」的判定逻辑。
2. 打开 [src/\_\_support/ctype_utils.h:244-302](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/src/__support/ctype_utils.h#L244-L302)，确认 `internal::isalpha(char)` 才是真正逐个枚举 `a`~`z`、`A`~`Z` 的地方。
3. 再打开 [src/ctype/isalpha_l.cpp:18-22](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/src/ctype/isalpha_l.cpp#L18-L22)，确认 locale 版 `isalpha_l` **同样**委托给 `internal::isalpha`——同一份算法被两个公开入口点共享。

**需要观察的现象**：三个公开函数（`isalpha`、`isalpha_l`，以及被 `isxdigit` 间接复用）都指向同一个 `internal::isalpha`，说明判定逻辑在源码中**只存在一份**。

**预期结果**：你能用一句话回答「isalpha 的字母判定逻辑写在哪个文件、被几个公开函数共享」。

**待本地验证**：如果想确认编译后确实只生成一份 `internal::isalpha` 的代码，可在已按 u1-l3 构建 `check-libc` 的环境里，对 `libc.a` 执行 `nm libc.a | grep isalpha` 观察符号（具体输出依赖你的构建，故标注待本地验证）。

#### 4.1.5 小练习与答案

**练习 1**：`isxdigit` 没有在 `ctype_utils.h` 里定义一个独立的 `internal::isxdigit`，而是用 `isalnum` + `b36_char_to_int` 组合。这样做的好处是什么？

**参考答案**：避免重复实现字母/数字判定。`b36_char_to_int` 已经把「字符 → 36 进制数值」的映射写成单一事实来源，`isxdigit` 只需复用它并加一个 `< 16` 的阈值，逻辑更短、更不易和 `isalnum` 等产生不一致。

**练习 2**：locale 版 `isalpha_l` 的第二个参数是 `locale_t`，但函数体里完全没用到它。这说明了什么？

**参考答案**：当前 LLVM-libc 的 ctype 判定与 locale 无关（C locale 行为），`locale_t` 参数仅为满足 POSIX 签名而保留；真正的判定仍走 `internal::isalpha`。这也解释了为什么 `_l` 一族函数能和普通版共享同一份 utils。

---

### 4.2 公共 utils：ctype_utils.h 与编码无关的判定

#### 4.2.1 概念说明

`ctype_utils.h` 是 ctype 家族的「算法仓库」。它最显眼的特征是：所有判定/转换函数都用**逐字符列出 case 标签**的 switch/case 写法，例如 `internal::isalpha(char ch)` 把 `a`~`z`、`A`~`Z` 共 52 个字符逐个写成 `case`。文件顶部有一段醒目的警告，明确禁止把这种写法「优化」成 case 区间或位运算查表。理解这段警告，就理解了 LLVM-libc ctype 设计的核心权衡。

#### 4.2.2 核心流程

`internal::isalpha(char ch)` 的判定流程：

```
function internal::isalpha(ch: char) -> bool:
    switch (ch):
        case 'a': case 'b': ... case 'z':   # 显式列出 26 个小写
        case 'A': case 'B': ... case 'Z':   # 显式列出 26 个大写
            return true
        default:
            return false
```

关键设计点（对应文件顶部警告）：

1. **逐字符 case，不用 `case 'a' ... 'z':` 区间**。区间写法假设字符编码里 `a`~`z` 连续，这在 ASCII 成立，但在 EBCDIC 不成立。逐字符 case 让函数与具体编码**解耦**：编译器用目标平台的字符字面量值去匹配，换编码也能正确工作。
2. **相信编译器的优化**。注释引用了 Compiler Explorer 上的对照（`https://godbolt.org/z/qvrebqvvr`），说明这种 switch/case 形式被编译器优化后，**几乎总是**等于或优于手写位运算版本。
3. **`LIBC_INLINE constexpr`**：函数内联且可在编译期求值，让常量参数（如 `isalpha('A')`）被完全折叠。

这三点合起来就是注释里反复强调的「**encoding independent**」——编码无关。

#### 4.2.3 源码精读

先看那段必须读懂的警告：

[src/\_\_support/ctype_utils.h:18-38](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/src/__support/ctype_utils.h#L18-L38) —— 顶部警告：不要「优化」这些函数，switch/case 形式更利于编译器优化且编码无关；并明确禁止用 `case 'a' ... 'z':` 区间，因为它假设字符连续，EBCDIC 下不成立。

`internal::isalpha` 本体就是这段警告的实例：

[src/\_\_support/ctype_utils.h:244-302](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/src/__support/ctype_utils.h#L244-L302) —— `isalpha(char)`：52 个 `case` 标签逐个列出字母，命中返回 `true`，`default` 返回 `false`。没有区间、没有查表、没有位掩码。

```cpp
LIBC_INLINE constexpr bool isalpha(char ch) {
  switch (ch) {
  case 'a':
  // ... 中间省略 b~y ...
  case 'z':
  case 'A':
  // ... 中间省略 B~Y ...
  case 'Z':
    return true;
  default:
    return false;
  }
}
```

文件里同款的函数还有一整套（写法完全一致，只是列出的字符集合不同）：

- [src/\_\_support/ctype_utils.h:40-72](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/src/__support/ctype_utils.h#L40-L72) `islower`：26 个小写字母。
- [src/\_\_support/ctype_utils.h:74-106](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/src/__support/ctype_utils.h#L74-L106) `isupper`：26 个大写字母。
- [src/\_\_support/ctype_utils.h:108-124](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/src/__support/ctype_utils.h#L108-L124) `isdigit`：10 个数字。
- [src/\_\_support/ctype_utils.h:304-372](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/src/__support/ctype_utils.h#L304-L372) `isalnum`：字母 + 数字共 62 个。
- [src/\_\_support/ctype_utils.h:576-588](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/src/__support/ctype_utils.h#L576-L588) `isspace`：空白字符。

转换函数 `tolower`/`toupper` 用的是「配对」式 switch——每个 `case` 直接 `return` 对应的相反大小写字符：

[src/\_\_support/ctype_utils.h:126-183](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/src/__support/ctype_utils.h#L126-L183) —— `tolower(char)`：`case 'A': return 'a';`……逐对映射，`default` 原样返回 `ch`。同样不假设字母连续。

注意 `isgraph` 是文件里少数「尚未编码无关」的例外，文件里也诚实标注了：

[src/\_\_support/ctype_utils.h:591](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/src/__support/ctype_utils.h#L591) —— `isgraph` 用了 `0x20 < ch && ch < 0x7f` 的范围比较，注释写明「not yet encoding independent」，说明编码无关是一个**渐进目标**，并非所有函数都已达标。

文件里还有一个体现「同功能、按需二选一」的例子 `b36_char_to_int`，受配置宏控制：

[src/\_\_support/ctype_utils.h:374-491](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/src/__support/ctype_utils.h#L374-L491) —— 默认版（`#ifndef LIBC_COPT_CTYPE_SMALLER_ASCII`）是逐字符 switch；当定义 `LIBC_COPT_CTYPE_SMALLER_ASCII`（注释称目标已知为 ASCII）时改用 `ch | 32` 的位运算小体积版。这是「可移植优先、体积可配置」的典型取舍。

最后，`ctype_utils` 自身是 `__support` 下的一个**头库（header library）**，由 `add_header_library` 声明，不产生公开符号：

[src/\_\_support/CMakeLists.txt:155-159](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/src/__support/CMakeLists.txt#L155-L159) —— `add_header_library(ctype_utils HDRS ctype_utils.h)`：只列头文件、没有 `SRCS`，是仅供 `#include` 的内部目标，对应 CMake 目标名 `libc.src.__support.ctype_utils`。

入口点正是在 CMake 里用 `DEPENDS` 把它接进来的：

[src/ctype/CMakeLists.txt:13-22](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/src/ctype/CMakeLists.txt#L13-L22) —— `isalpha` 入口点的 `DEPENDS` 里列出 `libc.src.__support.ctype_utils` 和 `libc.src.__support.CPP.limits`，前者提供判定算法，后者提供 `numeric_limits`。

`LIBC_INLINE` 这个修饰符的定义也值得一看，它就是普通的 `inline`，但用宏统一以便跨编译器：

[src/\_\_support/macros/attributes.h:27](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/src/__support/macros/attributes.h#L27) —— `#define LIBC_INLINE inline`。

#### 4.2.4 代码实践

**实践目标**：动手写出 `isalpha` 的等价纯逻辑实现，体会「逐字符 case」的写法。

**操作步骤**：

1. 打开 [src/\_\_support/ctype_utils.h:244-302](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/src/__support/ctype_utils.h#L244-L302)，通读 `internal::isalpha` 的 52 个 case。
2. **不修改任何源码**，仅在本地新建一个临时 `.cpp` 文件，仿照其风格手写一份等价的 `bool my_isalpha(char ch)`（逐字符 case，含 `default: return false;`）。
3. 在 `main` 里循环 `for (char ch = -128; ... ; )` 调用你的 `my_isalpha`，并打印「字母」字符集合。

**示例代码**（注意这是**示例代码**，非项目原有文件）：

```cpp
// 示例代码：仿 ctype_utils 风格的手写 isalpha
bool my_isalpha(char ch) {
  switch (ch) {
  case 'a': case 'b': case 'c': case 'd': case 'e':
  case 'f': case 'g': case 'h': case 'i': case 'j':
  case 'k': case 'l': case 'm': case 'n': case 'o':
  case 'p': case 'q': case 'r': case 's': case 't':
  case 'u': case 'v': case 'w': case 'x': case 'y': case 'z':
  case 'A': case 'B': case 'C': case 'D': case 'E':
  case 'F': case 'G': case 'H': case 'I': case 'J':
  case 'K': case 'L': case 'M': case 'N': case 'O':
  case 'P': case 'Q': case 'R': case 'S': case 'T':
  case 'U': case 'V': case 'W': case 'X': case 'Y': case 'Z':
    return true;
  default:
    return false;
  }
}
```

**需要观察的现象**：用 `clang -O2 -S` 编译这份临时文件，查看生成的汇编——你会看到编译器把 52 个 case 折叠成一个很紧凑的判定（往往是位测试或跳转表），这正是 ctype_utils.h 顶部注释所说的「编译器优化结果等于或优于手写」。

**预期结果**：手写版与 `internal::isalpha` 在 `[0,127]` 输入上行为完全一致。

**待本地验证**：不同 Clang 版本与目标架构生成的汇编形态不同，若要对照注释引用的 Compiler Explorer 例子，请自行在 godbolt 上验证。

#### 4.2.5 小练习与答案

**练习 1**：如果把 `internal::isalpha` 改写成 `case 'A' ... 'Z': return true;`（GCC 扩展区间），在普通 Linux 上能工作吗？为什么项目仍然禁止这种写法？

**参考答案**：在 ASCII 平台（普通 Linux）上能工作，因为 `A`~`Z` 在 ASCII 里连续。但区间写法假设了字符连续，这在 EBCDIC 等编码里不成立，会让 libc 失去「编码无关」的可移植性。项目为了可移植性统一禁止这种写法。

**练习 2**：`isgraph` 当前没有做到编码无关，文件是怎么标注这件事的？这说明「编码无关」是什么性质的目标？

**参考答案**：`isgraph` 上方注释写着「not yet encoding independent」，说明编码无关是一个**渐进的、未完全达成**的目标，已达标的大多数函数用逐字符 case，未达标的诚实标注待改进。

---

### 4.3 边界约定：int 入参与 unsigned char 区间契约

#### 4.3.1 概念说明

C 标准对 `isalpha` 等函数的入参有明确规定：实参应当是 `unsigned char` 能表示的值或 `EOF`，传入其他值是**未定义行为**。但 LLVM-libc 选择了一个更安全的实现：不在未定义行为上「放飞」，而是对越界输入给出确定的返回。

这一节要讲清两件事：

1. 判定函数（`isalpha`/`isdigit`/…）对越界输入**返回 0**。
2. 转换函数（`tolower`/`toupper`）对越界输入**原样返回 `c`**。

二者区间不同、返回不同，是一种**刻意的非对称**，分别对应各自的 C 标准语义。

#### 4.3.2 核心流程

判定函数的边界处理：

```
# 判定函数（is*）
if c < 0 or c > UCHAR_MAX:   # 区间 [0, 255]
    return 0                  # 越界 → 「否」
```

转换函数的边界处理：

```
# 转换函数（to*）
if c < CHAR_MIN or c > CHAR_MAX:   # 区间 [-128, 127]（典型）
    return c                        # 越界 → 原样返回
```

注意两个区间端点的差别：

\[
\text{判定函数合法区间} = [0,\; 2^{CHAR\_BIT}-1]
\]

\[
\text{转换函数处理区间} = [\texttt{numeric\_limits<char>::min()},\; \texttt{numeric\_limits<char>::max()}]
\]

在典型 8 位 `char`、`char` 有符号的平台上，前者是 \([0,255]\)，后者是 \([-128,127]\)。

#### 4.3.3 源码精读

判定函数的守卫（以 `isalpha` 为例）已经在前文出现过，这里聚焦边界语义：

[src/ctype/isalpha.cpp:18-22](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/src/ctype/isalpha.cpp#L18-L22) —— `c < 0 || c > max()` 时返回 `0`。负值（包括代表 `EOF` 的 `-1`）和超过 255 的值都归为「不是字母」。

转换函数 `tolower` 的守卫则不同：

[src/ctype/tolower.cpp:18-24](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/src/ctype/tolower.cpp#L18-L24) —— 用 `numeric_limits<char>::min()/max()` 作端点，且越界时 **`return c`**（原样返回），而不是 `return 0`：

```cpp
LLVM_LIBC_FUNCTION(int, tolower, (int c)) {
  if (c < cpp::numeric_limits<char>::min() ||
      c > cpp::numeric_limits<char>::max()) {
    return c;
  }
  return static_cast<int>(internal::tolower(static_cast<char>(c)));
}
```

这种不对称源于两类函数的标准语义不同：

- `isalpha` 类回答「是不是某类字符」，对非字符输入返回「否」(0) 是自然且无害的。
- `tolower` 类是「转换」，标准规定「非大写字母的输入原样返回」，所以越界时返回 `c` 本身，而不是 0。

边界值来自自带 CPP 子集 `cpp::numeric_limits`，它用位运算推导出极值，取代魔法数字：

[src/\_\_support/CPP/limits.h:22-45](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/src/__support/CPP/limits.h#L22-L45) —— `numeric_limits_impl` 用 `T(-1) < T(0)` 判断是否有符号、用 `CHAR_BIT * sizeof(T)` 算位数，再推导 `min()/max()`。所以 `numeric_limits<unsigned char>::max()` 与 `numeric_limits<char>::min()/max()` 都不需要写死常量。

那么，为什么入口点层要**先把 `c` 校验到 `[0, UCHAR_MAX]` 再调用内部判定**？有两层原因：

1. **类型安全**：`internal::isalpha` 的形参是 `char`。若直接把任意 `int`（如 256、-5）强转 `char`，会得到一个**合法的 char 值**，从而可能错误命中某个 case 标签或错误地返回。先把范围卡死到 `[0, UCHAR_MAX]`，再转 `char`，能保证：值在 `[0,127]` 时正面对应 ASCII；值在 `[128,255]` 时虽然 `char`（有符号时）会变成负数，但它的 case 标签里没有匹配项，必走 `default` 返回假——结果正确。
2. **契约清晰**：`ctype_utils.h` 里的函数只对「一个 `char`」负责，不必再操心 `int` 越界、`EOF` 等问题。边界处理这种「与标准签名耦合」的杂事留在入口点，算法层保持纯粹。

对比看「不依赖 utils」的几个函数，它们的边界/逻辑直接写在入口点里，因为没有可下沉的公共算法：

- [src/ctype/isascii.cpp:16-18](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/src/ctype/isascii.cpp#L16-L18) `isascii`：`(c & (~0x7f)) == 0`，纯位运算判定「是否 7 位 ASCII」，按定义不需要区间守卫。
- [src/ctype/isblank.cpp:16-18](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/src/ctype/isblank.cpp#L16-L18) `isblank`：`c == ' ' || c == '\t'`，两个字符直接比较。
- [src/ctype/iscntrl.cpp:16-19](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/src/ctype/iscntrl.cpp#L16-L19) `iscntrl`：先转 `unsigned` 再比 `ch < 0x20 || ch == 0x7f`。

它们的 CMake 注册里也确实**没有** `ctype_utils` 依赖：

[src/ctype/CMakeLists.txt:24-30](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/src/ctype/CMakeLists.txt#L24-L30) —— `isascii` 块没有 `DEPENDS`，说明它自包含、不引用 `__support`。

最后，这套边界约定被单元测试钉死。`isalpha` 的测试显式覆盖了边界：

[test/src/ctype/isalpha_test.cpp:33-42](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/test/src/ctype/isalpha_test.cpp#L33-L42) —— `SimpleTest` 里 `EXPECT_EQ(isalpha(-1), 0)` 把「负值返回 0」这一边界约定写成断言；

[test/src/ctype/isalpha_test.cpp:44-54](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/test/src/ctype/isalpha_test.cpp#L44-L54) —— `DefaultLocale` 用 `for (int ch = -255; ch < 255; ++ch)` 遍历，验证「字母返回非零、其余返回零」，覆盖了负值区间与超 `[0,127]` 区间。注意断言用 `EXPECT_NE(..., 0)` 表示「真」，尊重「真即非零」的 C 标准语义（见 u1-l5）。

#### 4.3.4 代码实践

**实践目标**：解释「入口点为何先把 `c` 校验到 `[0, UCHAR_MAX]` 再调用内部判定」，并用阅读型实践验证边界处理。

**操作步骤**：

1. 对照 [src/ctype/isalpha.cpp:18-22](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/src/ctype/isalpha.cpp#L18-L22)，假设**删掉**那行 `if (c < 0 || c > ...) return 0;`，在脑海里推演：传入 `c = 256`，强转 `char` 后在常见平台上得到 `0`（即 `'\0'`），会被判定为「不是字母」——这次碰巧正确；但传入 `c = 323`（= `'C' + 256`），强转 `char` 后得到 `'C'`，会被**错误**判定为字母。这说明区间守卫不是多余的。
2. 对照 [src/ctype/tolower.cpp:18-24](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/src/ctype/tolower.cpp#L18-L24)，确认转换函数越界返回 `c` 而非 `0`。
3. 阅读 [test/src/ctype/isalpha_test.cpp:44-54](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/test/src/ctype/isalpha_test.cpp#L44-L54)，确认测试遍历了 `[-255, 255)`，把上面两点的行为都覆盖了。

**需要观察的现象**：区间守卫让「越界 `int` 强转 `char` 后碰巧等于某字母」这条隐患被堵死；测试用大范围循环保证任何越界值都不会被误判。

**预期结果**：你能口头解释「如果没有区间守卫，`isalpha(323)` 会因为 `323 % 256 == 67 == 'C'` 而错误返回真」。

**待本地验证**：若已按 u1-l3 完成构建，可运行单测目标（形如 `ninja libc.test.src.ctype.isalpha_test.__unit__`，确切目标名以你构建目录为准）确认上述断言通过。

#### 4.3.5 小练习与答案

**练习 1**：`isalpha` 的守卫用 `numeric_limits<unsigned char>::max()`，而 `tolower` 用 `numeric_limits<char>::min()/max()`。为什么不统一？

**参考答案**：因为两类函数的语义不同。判定函数关心「这个 `int` 能否安全当作一个字符去查表」，所以用 `unsigned char` 的全范围 `[0,255]`；转换函数关心「这个 `int` 是否还在 `char` 可表示范围内、能安全转换」，所以用 `char` 的范围 `[-128,127]`，且越界时按标准「原样返回」而非返回 0。

**练习 2**：`isascii` 的入口点里没有 `if (c < 0 ...) return 0;` 这样的守卫。它为什么不需要？

**参考答案**：`isascii` 用 `(c & (~0x7f)) == 0` 判定「高 8 位及以上的所有位是否全 0」，这个位运算对任意 `int`（包括负数、超大值）都有确定结果——负数补码高位为 1，必然不等于 0，自动被判为「非 ASCII」。它用位运算自带「天然边界」，所以不需要额外的区间守卫，也不需要下沉到 utils。

---

## 5. 综合实践

把本讲三个最小模块串起来，完成下面这个「阅读 + 推演」小任务（不修改任何项目源码）：

**任务**：为 ctype 家族中**另一个**判定函数（建议选 `isdigit` 或 `isalnum`）写一份「四视图分析」，包含：

1. **入口点视图**：打开它对应的 `src/ctype/<func>.cpp`，抄下函数体，标注「区间守卫」「类型转换」「委托给哪个 `internal::` 函数」三处。
2. **utils 视图**：在 [src/\_\_support/ctype_utils.h](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/src/__support/ctype_utils.h) 里找到它委托的那个 `internal::` 函数，确认是逐字符 case、编码无关。
3. **CMake 视图**：在 [src/ctype/CMakeLists.txt](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/src/ctype/CMakeLists.txt) 里找到它的 `add_entrypoint_object` 块，确认 `DEPENDS` 里是否同时列了 `libc.src.__support.ctype_utils` 与 `libc.src.__support.CPP.limits`。
4. **边界视图**：仿照 4.3.4，假设删掉它的区间守卫，构造一个会**被误判**的具体输入值（提示：找一个落在 `[256, 511]`、且 `v % 256` 恰好是目标字符的整数），写出推演过程。

**验收标准**：你能用一段话说明「这个函数的判定逻辑只存在于 ctype_utils.h 一处，入口点只负责把它接到标准 C 签名上并处理好越界」，并且你的「会被误判的输入值」推演正确。

**进阶（可选）**：对比 [src/ctype/CMakeLists.txt:1-11](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/src/ctype/CMakeLists.txt#L1-L11)（`isalnum`）与 [src/ctype/CMakeLists.txt:13-22](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/src/ctype/CMakeLists.txt#L13-L22)（`isalpha`），你会发现 `isalnum` 的 `DEPENDS` 里多了一项 `libc.include.ctype`，而 `isalpha` 没有。这是一处历史遗留的不一致——试着阅读两者的 `.cpp`，确认这个额外依赖对实现是否真的必要，并形成你自己的判断。

## 6. 本讲小结

- ctype 函数的入口点（如 `isalpha`）是**薄壳**：只做 `unsigned char` 区间守卫、`int↔char` 类型转换，再把真正的判定委托给 `__support/ctype_utils.h` 里的 `internal::` 函数。
- `ctype_utils.h` 用**逐字符 case 标签**的 switch/case 实现 `isalpha`/`isdigit`/`isalnum`/`tolower` 等，刻意不用 case 区间，目的是**编码无关**（ASCII 与 EBCDIC 通用），并相信编译器把它优化得比手写更紧凑。
- 「公共逻辑下沉」还体现在**组合型**判定上：`isxdigit`、`ispunct` 不重复实现，而是复用 `isalnum`、`b36_char_to_int`、`isgraph` 等原语拼出来。
- 边界处理有**刻意的非对称**：判定函数对越界返回 `0`、用 `[0, UCHAR_MAX]` 守卫；转换函数 `tolower`/`toupper` 对越界**原样返回 `c`**、用 `[CHAR_MIN, CHAR_MAX]` 守卫——分别对应各自的 C 标准语义。
- 区间守卫不是多余的：没有它，`int` 越界值强转 `char` 后可能碰巧等于某字母（如 `323 → 'C'`）而误判；守卫让算法层只对「一个 `char`」负责。
- `isascii`/`isblank`/`iscntrl` 等简单函数不依赖 `ctype_utils`，逻辑直接写在入口点，CMake 里也没有对应 `DEPENDS`——是否下沉取决于「有没有可复用的公共算法」。

## 7. 下一步学习建议

- **横向对比**：阅读 [src/string/memory_utils/README.md](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/src/string/memory_utils/README.md) 与 `memcpy` 的实现（u5-l2），体会「字符函数」与「内存函数」在下沉粒度上的差别——前者下沉到逐字符 case，后者下沉到 `block`/`tail`/`loop_and_tail` 等构建块。
- **纵向深入 utils**：`ctype_utils.h` 里的 `b36_char_to_int` / `int_to_b36_char` 还被 `str_to_integer.h` 复用，建议接着读 u7-l1（stdlib 数值转换），看 ctype 的原语如何被字符串解析复用。
- **测试与正确性**：若想亲手补一个 ctype 函数的测试，参考本讲的 [isalpha_test.cpp](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/test/src/ctype/isalpha_test.cpp) 与 u10-l1（单元测试框架）。
- **端到端实战**：当你想真正「贡献一个 ctype 函数」时，回到 u11-l3，按六步清单把 yaml→`.h`→`.cpp`→CMake→`entrypoints.txt`→测试完整走一遍——本讲已经为你准备好了其中实现与依赖两步的全部细节。
