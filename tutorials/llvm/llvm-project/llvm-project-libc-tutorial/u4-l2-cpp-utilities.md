# CPP 子集工具库：在无标准库的环境里写 C++

## 1. 本讲目标

在 **u4-l1** 里，我们把 `src/__support` 定性为「所有入口点共享的私有标准库」，并在子模块地图里把 `CPP/` 标成「自包含的 C++ 标准库子集，让 libc 用现代 C++ 却不依赖 libstdc++/libc++」。本讲要回答的核心问题是：

> 这套「自带的 C++ 工具」到底是什么？为什么 LLVM-libc 必须**自己重写一遍** `std::numeric_limits`、`std::span` 这些标准库设施，而不能直接 `#include <limits>`？入口点又该怎么用它们？

学完本讲，你应当能够：

1. 说清「宿主 C++ 运行时不可用」这一约束的来龙去脉，从而理解 `src/__support/CPP/` 存在的根本原因。
2. 看懂 `cpp::numeric_limits` 与 `cpp::span` 这两个代表工具的源码结构，并掌握它们的「自包含」实现手法。
3. 在真实入口点（如 `isalpha`、`string_length`、`inet_ntop`）里识别这些工具的使用方式，并能正确地为新代码补上对应的 CMake `DEPENDS`。

## 2. 前置知识

本讲承接 **u4-l1（`__support` 总览与设计哲学）**，默认你已经理解以下概念，不再展开：

- **入口点（entrypoint）**：每个对外公开函数都是独立、有名的构建单元。
- **`add_header_library`**：`__support` 里纯头文件库的声明规则，只传播 include 路径与编译选项，不编译出 `.o`（详见 u2-l3、u4-l1）。
- **`LIBC_NAMESPACE_DECL`**：带隐藏可见性的命名空间声明，把所有内部符号关进 `__llvm_libc`（详见 u2-l2）。
- **C++ include 与 CMake `DEPENDS` 一一对应**：源码里 include 了哪个 `__support` 头，`DEPENDS` 里就要有对应内部目标（详见 u4-l1）。

一个有用的直觉：标准 C 库要「在裸机 / GPU / Full 构建下替换系统 libc」，就意味着它**自己也不能再去依赖宿主的 C++ 标准库**。`src/__support/CPP/` 就是 libc 给自己造的一套「不依赖任何运行时」的 C++ 零件箱。

## 3. 本讲源码地图

本讲涉及的关键文件及其作用：

| 文件 | 作用 |
|------|------|
| [src/__support/CPP/README.md](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/src/__support/CPP/README.md) | `CPP/` 子目录的设计规则：可包含哪些头、命名空间约定、「精确子集」原则。 |
| [src/__support/CPP/limits.h](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8fe5f7/libc/src/__support/CPP/limits.h) | 自包含的 `numeric_limits`，提供整型的 `min()`/`max()`/`is_signed`/`digits`。 |
| [src/__support/CPP/span.h](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/src/__support/CPP/span.h) | 自包含的 `span`——一段连续内存的「指针+长度」非拥有视图。 |
| [src/__support/CPP/array.h](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/src/__support/CPP/array.h) | 自包含的 `array`（定长数组），`span` 的依赖之一。 |
| [src/__support/CPP/type_traits.h](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/src/__support/CPP/type_traits.h) | 自包含的 `type_traits` 聚合头，聚合了几十个 `is_*`/`remove_*` 类型特征。 |
| [src/__support/CPP/CMakeLists.txt](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/src/__support/CPP/CMakeLists.txt) | `CPP/` 的构建根：把每个工具声明成 `add_header_library` 内部目标。 |
| [src/ctype/isalpha.cpp](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/src/ctype/isalpha.cpp) | 入口点示例：用 `cpp::numeric_limits<unsigned char>::max()` 做边界检查。 |
| [src/string/string_length.h](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/src/string/string_length.h) | 内部示例：用 `cpp::numeric_limits<size_t>::max()` 与 `cpp::is_same_v`。 |
| [src/arpa/inet/inet_ntop.cpp](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/src/arpa/inet/inet_ntop.cpp) | 入口点示例：用 `cpp::span<char>(dst, size)` 把裸缓冲区包成 span。 |

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：**自包含动机**（为什么必须自带）、**常用工具**（limits 与 span 的源码）、**使用方式**（入口点怎么 include 与引用）。

### 4.1 自包含动机：为什么 libc 必须自带一套 C++ 工具

#### 4.1.1 概念说明

LLVM-libc 的实现语言是 C++（C++17），但它对外暴露的是标准 C 接口。这里有一个容易被忽略的张力：**用 C++ 写代码，通常意味着依赖 C++ 标准库（libstdc++ 或 libc++）的运行时**——比如 `std::string` 要链接分配器与异常运行时，`std::numeric_limits` 听起来「只是个编译期常量」，但也写在 `<limits>` 这个标准库里。

但对 LLVM-libc 而言，**依赖宿主 C++ 运行时是不可接受的**，原因有三：

1. **它要替换 libc 本身**。在 Full 构建下（见 u1-l4），LLVM-libc 用 `-nostdlib` 把系统库完全屏蔽掉，自己当 libc。如果它的实现还去依赖 libstdc++/libc++，就等于「替换 libc 的东西自己又挂回了系统的 C++ 运行时」，循环依赖、且把一整套不需要的运行时拖进了产物。
2. **它要跑在没有 C++ 运行时的目标上**。GPU（AMDGPU/NVPTX）、baremetal、UEFI 这些目标根本没有 libstdc++（见 u11-l2）。
3. **符号洁癖**。LLVM-libc 的内部符号全部锁在带隐藏可见性的 `__llvm_libc` 命名空间里（见 u2-l2），它绝不能让 `std::` 之类的符号泄漏到产物里。

解决办法不是「放弃 C++」，而是「**自己重写一套只够自己用的 C++ 标准库子集**」——这就是 `src/__support/CPP/`。`README.md` 开篇一句话就点明了它的定位：

> This directory contains partial re-implementations of some C++ standard library utilities. They are for use with internal LLVM libc code and tests.（[src/__support/CPP/README.md:1-2](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/src/__support/CPP/README.md#L1-L2)）

关键词是 **partial（部分）** 与 **re-implementations（重新实现）**：它不追求完整复刻 `<limits>`/`<span>`，只实现「libc 内部确实要用到的那个子集」。

#### 4.1.2 核心流程

`CPP/` 维持「自包含」靠三条纪律，全部写在 `README.md` 里：

```
纪律 1：能 include 的头只有三类
        ├─ 本目录（CPP/）里的其它头
        ├─ free-standing C 头（<stddef.h>、<stdint.h>…，注意是 .h 不是 <cstddef>）
        └─ 少数 src/__support/macros 基础宏头

纪律 2：命名空间必须是 LIBC_NAMESPACE::cpp
        （带 __ 前缀的更高层命名空间，避免污染公开符号）

纪律 3：每个 CPP/foo.h 是 std 中 <foo> 的「精确子集」
        行为须与 std::foo 在「已支持的那部分」上完全一致
```

第 1 条纪律的精髓是：**绝不依赖任何会拖入运行时的 C++ 头**。注意它特意区分了 `<stddef.h>`（free-standing C 头，编译器自带、零运行时）与 `<cstddef>`（C++ 头，可能拉入 C++ 运行时细节），要求一律用前者：

> Free-standing C headers are to be included as C headers and not as C++ headers. That is, use `#include <stddef.h>` and not `#include <cstddef>`.（[src/__support/CPP/README.md:12-14](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/src/__support/CPP/README.md#L12-L14)）

第 3 条「精确子集」纪律还附带一个可验证的标准——如果把这些工具的声明换成 `using std::foo;`，libc 代码应该功能不变：

> if each were just declared with `using std::foo;` all the libc code should work the same.（[src/__support/CPP/README.md:24-25](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/src/__support/CPP/README.md#L24-L25)）

这句话很重要：它意味着这套工具不是「另搞一套语义」，而是「标准库的真子集」。你脑子里关于 `std::span`、`std::numeric_limits` 的知识可以**直接**平移到 `cpp::span`、`cpp::numeric_limits` 上，只要确认它在子集内。

#### 4.1.3 源码精读

先验证「纪律 1」确实被遵守。看 `span.h` 顶部 include 了什么：

[src/__support/CPP/span.h:11-18](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/src/__support/CPP/span.h#L11-L18) —— `span.h` 只 include 了 `<stddef.h>`（free-standing C 头，取 `size_t`）、本目录的 `array.h` 与 `limits.h`、`type_traits.h`，以及 `src/__support/macros/` 下的两个基础宏头。**没有任何一个会拖入运行时的 C++ 头**——这正是「自包含」的实证。

再看 `array.h`，它连 `array` 这个最基础的容器都是「自己手写」的：

[src/__support/CPP/array.h:12-15](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/src/__support/CPP/array.h#L12-L15) —— `array.h` 的 include 也只有本目录的 `iterator.h`、`macros/attributes.h`、`macros/config.h` 和 `<stddef.h>`。第 20 行的 `template <class T, size_t N> struct array` 就是一个包裹着 `T Data[N]` 的轻量结构体，零运行时依赖。

接着验证「纪律 2」——命名空间。所有 `CPP/` 工具都包在 `LIBC_NAMESPACE_DECL::cpp` 里：

[src/__support/CPP/limits.h:17-18](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/src/__support/CPP/limits.h#L17-L18) —— `namespace LIBC_NAMESPACE_DECL { namespace cpp {`。注意外层是 `LIBC_NAMESPACE_DECL`（默认 `__llvm_libc`，带隐藏可见性），内层才是 `cpp`。所以使用时写 `cpp::numeric_limits`（在 `LIBC_NAMESPACE_DECL` 内）或全限定 `LIBC_NAMESPACE::cpp::numeric_limits`。

> 小结：`CPP/` 的全部存在理由就是「在不依赖任何 C++ 运行时的前提下，让 libc 享受现代 C++」。它通过「只 include 三类零运行时头 + 锁进 `__llvm_libc::cpp` 命名空间 + 做标准库的精确子集」三条纪律来实现这一点。

#### 4.1.4 代码实践

**实践目标**：亲手验证 `CPP/` 的「自包含」纪律，体会它绝不触碰 C++ 运行时。

**操作步骤**：

1. 打开 [src/__support/CPP/README.md](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/src/__support/CPP/README.md)，定位「Only certain kind of headers can be included」那条规则（约第 7～10 行），记下「允许 include 的三类头」。
2. 任选 `CPP/` 下 3 个头（建议 `limits.h`、`span.h`、`array.h`），逐个检查它们顶部的 `#include`，按「本目录头 / free-standing C 头（`<*.h>`）/ `macros/` 宏头 / 其它」四类归类。
3. 在这 3 个头里搜索是否出现过 `<cstddef>`、`<cstdint>`、`<limits>`、`<type_traits>` 这类**带 `c` 前缀的 C++ 标准头**——按纪律，应当一个都没有（free-standing 头要用 `.h` 形式）。

**需要观察的现象**：

- 每个头的 `#include` 都落在允许的三类里，没有任何带运行时的 C++ 头。
- `<stddef.h>`、`<stdint.h>` 这类以**带 `.h` 的 C 形式**出现，而不是 `<cstddef>`、`<cstdint>`。

**预期结果**：得到一张「头 → include 归类表」，证实 `CPP/` 的每个工具都是「零运行时、零 `std::` 依赖」的。这正是「自包含」二字的落地。

> 这一步纯源码阅读，无需编译即可完成。

#### 4.1.5 小练习与答案

**练习 1**：既然 `cpp::numeric_limits` 和 `std::numeric_limits` 行为一样，为什么不干脆 `#include <limits>` 用标准库的？

> **参考答案**：两个原因。其一是**运行时/产物洁癖**：`<limits>` 是 C++ 标准库头，在 Full 构建下 libc 正用 `-nostdlib` 屏蔽系统库、自己充当 libc，再去 include C++ 标准库就形成了「替换者反过来依赖被替换者」的循环，且会把一整套不需要的运行时拖进产物。其二是**目标覆盖**：libc 要跑在 GPU、baremetal、UEFI 等根本没有 libstdc++/libc++ 的目标上（见 u11-l2）。所以必须自带一份只够自用的子集。

**练习 2**：`README.md` 为什么要求 free-standing 头用 `<stddef.h>` 而不是 `<cstddef>`？

> **参考答案**：两者表面等价，但 `<cstddef>` 是 C++ 头，在某些实现上可能拉入额外的 C++ 运行时细节或产生 `std::` 命名空间符号；而 `<stddef.h>` 是 free-standing C 头，由编译器直接提供、零运行时、零 `std::` 污染。`CPP/` 的全部纪律就是为了「不沾 C++ 运行时」，所以哪怕在这种小处也要坚持用 C 形式的头。

---

### 4.2 常用工具：limits 与 span

#### 4.2.1 概念说明

`CPP/` 下有二十多个头（`array`、`span`、`limits`、`type_traits`、`optional`、`string_view`、`bit`、`atomic`、`expected`、`tuple`、`simd`…），但最常被入口点用到、也最能体现「精确子集」哲学的两个是：

- **`cpp::numeric_limits`**：在编译期给出某整型 `T` 的极值与位数（`min()`/`max()`/`digits`/`is_signed`），取代裸的 `INT_MAX`、`255`、`0xff` 这类「魔法数字」。
- **`cpp::span`**：把「一个裸指针 + 一个长度」包成一个安全的非拥有视图，附带 `size()`、`operator[]`、`subspan()`、`begin()/end()`，取代到处手写 `ptr, len` 二元组。

它们都在 `LIBC_NAMESPACE::cpp` 命名空间下，且都是「标准库的真子集」。理解它们时，请直接套用你对 `std::numeric_limits`/`std::span` 的已有知识，只需确认「想用的那个成员在子集内」。

#### 4.2.2 核心流程

**`numeric_limits` 的实现思路**是用模板特化 + 编译期常量来「推导」整型属性，而不是写死任何具体数值：

```
numeric_limits<T>  继承  numeric_limits_impl<T, is_integral_v<T>>
                                   │
                  ┌────────────────┴────────────────┐
            is_integral=true（有特化）        is_integral=false（空主模板）
            is_signed = T(-1) < T(0)            （如 float → 没有成员，
            digits    = CHAR_BIT*sizeof(T)       浮点走 FPUtil/FPBits，见 u6-l2）
                     - is_signed
            min()/max() 用位运算推导
```

关键点：`is_signed` 用「`T(-1)` 是否小于 0」来判定（无符号时 `-1` 是个大正数，故 `false`）；`digits`（有效位数）用「`CHAR_BIT * sizeof(T) - is_signed`」算出（符号位要扣掉一位）。这套推导**与具体平台无关**，只依赖 `CHAR_BIT`（一字节多少比特）与 `sizeof`，所以同一份代码在任何平台上都对。

`min()`/`max()` 的推导用一个干净的位运算技巧。以 `N = CHAR_BIT * sizeof(T)` 为例：

- 无符号：`max() = ~0`（全 1），`min() = 0`。
- 有符号：`digits = N - 1`，`min() = T(1) << digits`（仅最高位为 1，即最小负数），`max() = ~0 ^ min()`（全 1 与最高位异或，得到最高位为 0、其余为 1，即最大正数）。

  例如 8 位有符号：\(\text{digits}=7\)，\(\text{min}=\texttt{1<<7}=\texttt{0x80}=-128\)，\(\text{max}=\texttt{0xFF}\oplus\texttt{0x80}=\texttt{0x7F}=127\)。

**`span` 的实现思路**更直白：它就是一个「持有 `T*` 指针 + `size_t` 长度」的小类，提供一组只读视图接口。它的构造函数被重载成好几种来源——指针+个数、指针+尾后指针、C 数组、`cpp::array`、另一个 `span`——让你不必关心手头的数据「装在哪种容器里」，都能统一转成 `span`：

```
span 的构造（多种来源 → 统一视图）
  ├─ (T* first, size_t count)
  ├─ (T* first, T* end)            // 长度 = end - first
  ├─ U(&arr)[N]                     // C 数组
  ├─ array<U,N>& arr                // cpp::array
  └─ span<U>& s                     // 另一个 span（支持 const 视图）

只读接口：size() / data() / operator[] / front() / back() / subspan() / begin() / end()
```

#### 4.2.3 源码精读

先看 `numeric_limits`。它的骨架是一个**主模板加偏特化**：

[src/__support/CPP/limits.h:22-51](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/src/__support/CPP/limits.h#L22-L51) —— 第 22 行是空的主模板 `numeric_limits_impl<T, bool is_integral>`；第 24～45 行才是真正干活的偏特化 `numeric_limits_impl<T, true>`（只对整型生效）；第 49～51 行的 `numeric_limits<T>` 公开继承「按 `is_integral_v<T>` 选出的那个特化」。这正是「精确子集」的体现：**只为整型实现**，浮点不在此处管（由 `FPUtil/FPBits` 接手，见 u6-l2），所以非整型的 `numeric_limits` 会落到空主模板——这不是 bug，而是「只实现用得到的部分」。

接着看它如何用纯位运算推导极值：

[src/__support/CPP/limits.h:24-45](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/src/__support/CPP/limits.h#L24-L45) —— 第 25 行 `is_signed = T(-1) < T(0)` 用编译期比较判定符号性；第 27～28 行 `digits = (CHAR_BIT * sizeof(T)) - is_signed`（`bool` 参与算术时 true=1）算出有效位数；第 30～44 行的 `min()`/`max()` 用 `if constexpr` 按符号性分两个分支，全部用位运算推导，**不写死任何 `0xff`/`255`/`65535`**。注意第 12 行 include 的 `hdr/limits_macros.h` 提供了 `CHAR_BIT`，这就是「平台无关」的关键输入。

再看 `span`。先看它的成员类型与「动态长度」常量：

[src/__support/CPP/span.h:42-53](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/src/__support/CPP/span.h#L42-L53) —— 第 42～50 行定义了一组与 `std::span` 对齐的成员类型（`element_type`、`value_type`、`size_type`、`pointer`、`iterator` 等）；第 52～53 行的 `dynamic_extent` 直接复用了刚讲过的 `cpp::numeric_limits<size_type>::max()`——**`CPP/` 工具之间互相引用**，这是「自包含」的另一个侧面：要一个「哨兵最大值」，不用再写 `0xFFFF...`，直接用自家的 `numeric_limits`。

接着看 `span` 的构造函数群与只读接口：

[src/__support/CPP/span.h:55-83](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/src/__support/CPP/span.h#L55-L83) —— 默认构造给空（第 55 行）；`(pointer, count)` 与 `(pointer, end)` 两种指针构造（第 59～63 行）；以及用 `cpp::enable_if_t<is_compatible_v<U>, bool>` 模板约束的 C 数组、`cpp::array`、另一个 `span` 三种容器构造（第 65～77 行）。这些 `enable_if_t`/`is_same_v`/`remove_cv_t`/`is_const_v` 全部来自 [src/__support/CPP/type_traits.h](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/src/__support/CPP/type_traits.h)（它只是一个聚合头，第 12～68 行把几十个 `type_traits/*` 子头全部 include 进来）。

[src/__support/CPP/span.h:87-105](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/src/__support/CPP/span.h#L87-L105) —— 第 87 行 `operator[]`、第 95～96 行 `data()`/`size()`、第 102～105 行 `subspan(offset, count)`：`subspan` 在 `count == dynamic_extent`（即调用者没指定个数）时退化为「取到末尾」（见第 116～122 行的 `count_to_size`），这正是 `std::span::subspan` 的标准语义。

> 小结：`numeric_limits` 用「模板特化 + 位运算推导」给出平台无关的整型极值；`span` 用「指针+长度」封装安全的非拥有视图，并大量复用自家 `type_traits` 与 `numeric_limits`。两者都是标准库的「精确子集」。

#### 4.2.4 代码实践

**实践目标**：在源码层面验证「`numeric_limits` 只为整型实现」「`span` 的成员语义与标准库一致」。

**操作步骤**：

1. 打开 [src/__support/CPP/limits.h:22-45](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/src/__support/CPP/limits.h#L22-L45)，按「`T = unsigned char`（8 位无符号）」手算一遍：`is_signed`、`digits`、`min()`、`max()` 各是多少？再按「`T = int`（假设 32 位）」算一遍，验证 `min()=INT_MIN`、`max()=INT_MAX`。
2. 打开 [src/__support/CPP/span.h:52-53](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/src/__support/CPP/span.h#L52-L53)，确认 `dynamic_extent` 的值就是 `cpp::numeric_limits<size_t>::max()`。
3. 对照 [src/__support/CPP/type_traits.h:12-68](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/src/__support/CPP/type_traits.h#L12-L68)，数一数它聚合了多少个 `type_traits/*.h` 子头，并确认 `span.h` 用到的 `enable_if_t`、`is_same_v`、`remove_cv_t`、`is_const_v` 都在其中。

**需要观察的现象**：

- 手算 `unsigned char`：`is_signed=false`、`digits=8`、`min()=0`、`max()=255`；这正是入口点 `isalpha` 里 `cpp::numeric_limits<unsigned char>::max()` 取到的值。
- `type_traits.h` 不定义任何逻辑，只做聚合——这印证了「精确子集」是靠一个个小组件拼出来的。

**预期结果**：你会在脑中建立一个「`cpp::numeric_limits<T>` 的成员 = 用这几个公式手算出来」的确定性映射，而不再把它当成黑盒。

> 待本地验证：若有构建环境，可写一段「示例代码」打印 `cpp::numeric_limits<unsigned char>::max()` 与 `cpp::numeric_limits<int>::min()`，与手算结果对照。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `numeric_limits` 用 `T(-1) < T(0)` 来判定 `is_signed`，而不是用 `T(-1) < T(1)` 之类？

> **参考答案**：`-1` 在无符号类型里会被解释成该类型的最大值（全 1），是个很大的正数，显然 `> 0`；而在有符号类型里 `-1 < 0` 为真。所以 `T(-1) < T(0)` 这一个编译期表达式就能同时区分两种情况，且不依赖任何写死的阈值，是纯靠语言语义推导的「平台无关」判定。

**练习 2**：`numeric_limits<float>` 在这套实现里能取到 `max()` 吗？为什么？

> **参考答案**：不能。`numeric_limits_impl` 只对 `is_integral_v<T> == true` 做了偏特化（`limits.h` 第 24 行）；`float` 不是整型，会落到第 22 行的**空主模板**，没有 `max()` 成员。这是「精确子集」的刻意取舍：浮点的极值/位表示由 `__support/FPUtil/FPBits`（见 u6-l2）以专门的位域工具负责，`CPP/` 只补足整型这一块。

**练习 3**：`span` 的 `subspan(offset, count)` 在调用者省略 `count` 时如何确定返回的长度？

> **参考答案**：`count` 的默认值是 `dynamic_extent`（即 `cpp::numeric_limits<size_type>::max()`，`span.h` 第 52～53 行与第 103 行）。`count_to_size`（第 116～122 行）检测到 `count == dynamic_extent` 时，就返回 `size() - offset`，即「从 `offset` 取到末尾」。这正是 `std::span::subspan` 的标准语义。

---

### 4.3 使用方式：入口点如何 include 并使用这些工具

#### 4.3.1 概念说明

知道 `CPP/` 有什么、为什么自包含之后，本节回答最后一个问题：**一个入口点要怎么把 `cpp::xxx` 用起来？**

答案分两层，且必须同时满足（这条铁律来自 u4-l1）：

1. **C++ 层**：在 `.cpp`/`.h` 里 `#include "src/__support/CPP/<foo>.h"`，然后写 `cpp::<foo>`（因为都在 `LIBC_NAMESPACE_DECL` 命名空间里，入口点内部直接写 `cpp::` 即可）。
2. **CMake 层**：在该入口点的 `add_entrypoint_object` 的 `DEPENDS` 里加上对应内部目标 `libc.src.__support.CPP.<foo>`，否则编译期找不到头文件。

两层缺一不可——CMake 的 `DEPENDS` 同时承担「构建顺序」与「头文件搜索路径传播」两重职责（详见 u2-l3）。下面用三个真实入口点印证。

#### 4.3.2 核心流程

```
入口点想用 cpp::numeric_limits
   │
   ├─ C++ 层：  #include "src/__support/CPP/limits.h"
   │            ... cpp::numeric_limits<unsigned char>::max() ...
   │
   └─ CMake 层：add_entrypoint_object( ...
                 DEPENDS
                   libc.src.__support.CPP.limits   ← 内部目标名
                   libc.src.ctype_utils )
```

`CPP/` 下每个工具头在 [src/__support/CPP/CMakeLists.txt](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/src/__support/CPP/CMakeLists.txt) 里都被声明成一个 `add_header_library` 内部目标，目标名就是点分路径去掉前缀。`DEPENDS` 里既可以用全限定名 `libc.src.__support.CPP.limits`，也可以用相对名 `.limits`（仅限同一 `CPP/` 目录内的互相引用）。

#### 4.3.3 源码精读

先看最经典的例子——`isalpha` 入口点用 `cpp::numeric_limits` 做边界检查：

[src/ctype/isalpha.cpp:9-22](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/src/ctype/isalpha.cpp#L9-L22) —— 第 11 行 `#include "src/__support/CPP/limits.h"`；第 19 行 `if (c < 0 || c > cpp::numeric_limits<unsigned char>::max()) return 0;`。这里没有写 `255` 或 `0xff`，而是用 `cpp::numeric_limits<unsigned char>::max()`——好处是「类型正确、意图自解释、且当 `char` 不是 8 位时也成立」。注意 `c` 是 `int`，与 `max()` 返回的 `unsigned char`（提升为 `int`）比较，语义清晰。

CMake 侧必须配上对应目标（u4-l1 已引用过这条边，这里聚焦 `CPP.limits`）：

[src/__support/CPP/CMakeLists.txt:48-57](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/src/__support/CPP/CMakeLists.txt#L48-L57) —— `limits` 这个内部目标把 `limits.h` 列为 `HDRS`，并 `DEPENDS` 了 `.type_traits`、`libc.hdr.limits_macros`（提供 `CHAR_BIT`）等。入口点只要在 `DEPENDS` 里写 `libc.src.__support.CPP.limits`，就能拿到 `limits.h` 的搜索路径。

第二个例子——`string_length.h` 同时用了 `numeric_limits` 与 `type_traits` 里的 `is_same_v`：

[src/string/string_length.h:142-144](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/src/string/string_length.h#L142-L144) —— `find_first_character` 的参数 `max_strlen` 默认值用了 `cpp::numeric_limits<size_t>::max()`，表示「不限制最大长度」。再看 [src/string/string_length.h:205-209](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/src/string/string_length.h#L205-L209) —— `if constexpr (cpp::is_same_v<T, char>)` 在编译期按模板参数 `T` 选择不同实现路径。这两处都是「用类型工具写出平台无关、类型安全的代码」的典型。

第三个例子——`inet_ntop` 入口点用 `cpp::span` 把裸缓冲区包成视图：

[src/arpa/inet/inet_ntop.cpp:34-39](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/src/arpa/inet/inet_ntop.cpp#L34-L39) —— `cpp::span<char>(dst, size)` 用「指针+个数」构造，把调用者传入的 `char *dst` 与 `socklen_t size` 这对裸的 `(ptr, len)` 二元组，统一包成一个 `span<char>` 交给内部 `net::ipv4_to_str`/`ipv6_to_str`。这样下游函数就能用 `size()`、`operator[]`、`subspan()` 等安全接口操作缓冲区，而不必到处手传两个参数、还容易写错长度。对应的 `span` 内部目标定义在：

[src/__support/CPP/CMakeLists.txt:65-72](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/src/__support/CPP/CMakeLists.txt#L65-L72) —— `span` 目标 `DEPENDS` 了 `.array` 与 `.type_traits`，因为 `span.h` include 了 `array.h` 与 `type_traits.h`（见 4.1.3）。这条 `DEPENDS` 链是「自包含」在构建层面的延续：`span` 不依赖系统库，只依赖同目录的另两个自家工具。

> 小结：使用 `CPP/` 工具只需两步——C++ 里 `#include` 对应头并写 `cpp::xxx`，CMake 的 `DEPENDS` 里加上 `libc.src.__support.CPP.<foo>`。三个真实入口点（`isalpha`、`string_length`、`inet_ntop`）分别示范了 `numeric_limits`、`is_same_v`、`span` 的典型用法。

#### 4.3.4 代码实践

**实践目标**：亲身体会「自带 `numeric_limits` 带来的安全性与简洁性」。这是本讲的主实践。

**操作步骤**：

1. 在 [src/ctype/isalpha.cpp:18-22](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/src/ctype/isalpha.cpp#L18-L22) 找到对 `cpp::numeric_limits<unsigned char>::max()` 的使用，理解它把输入 `c` 的合法范围限制在 `[0, 255]`。
2. **改写**（示例代码，仅用于对比，不要真的提交）——把那一行边界检查替换成「不依赖 `numeric_limits`」的等价写法。例如：

   ```cpp
   // 示例代码：不依赖 cpp::numeric_limits 的等价判断
   LLVM_LIBC_FUNCTION(int, isalpha, (int c)) {
     if (c < 0 || c > 255)   // 魔法数字 255 硬编码
       return 0;
     return static_cast<int>(internal::isalpha(static_cast<char>(c)));
   }
   ```

3. 对比两种写法，思考：用 `cpp::numeric_limits<unsigned char>::max()` 比直接写 `255` 好在哪里？如果某平台上 `unsigned char` 不是 8 位（即 `CHAR_BIT != 8`），两种写法各自会怎样？

**需要观察的现象**：

- 直观差异：`cpp::numeric_limits<unsigned char>::max()` 把「`unsigned char` 的上界」这个**意图**写进了代码，读者一眼明白；`255` 是个**魔法数字**，读者得自己推断它为什么是 255。
- 类型差异：`numeric_limits<unsigned char>::max()` 返回的是 `unsigned char` 类型（与被比较的对象同源），在与 `int c` 比较时由整型提升规则保证语义正确；写 `255` 是 `int` 字面量，虽然结果相同，但丢失了「这个界限来自 `unsigned char`」的类型信息。
- 平台适应性：`numeric_limits` 的 `max()` 内部用 `(CHAR_BIT * sizeof(T))` 推导（见 4.2），在 `CHAR_BIT != 8` 的平台上会自动给出正确的 `unsigned char` 上界；硬编码 `255` 在那样的平台上就是错的。

**预期结果**：你会得出一个结论——自带 `cpp::numeric_limits` 并非「多此一举」，它让边界检查**意图清晰、类型正确、平台无关**，这正是「在无标准库环境里也要享受现代 C++」的收益。同时这呼应了 4.1 的动机：因为不能 `#include <limits>`，libc 才必须自带一份等价物。

> 待本地验证：这一步是「源码阅读 + 改写对比」型实践，无需编译；若想进一步确认，可在有构建环境时分别编译两种写法并对 `c = -1 / 0 / 255 / 256` 跑 `isalpha` 单元测试，观察输出一致。

#### 4.3.5 小练习与答案

**练习 1**：一个入口点的 `.cpp` 里写了 `cpp::span<char> buf(dst, size);`，但编译时报 `span.h: No such file or directory`。最可能的原因是什么？怎么修？

> **参考答案**：最可能是该入口点的 `add_entrypoint_object` 的 `DEPENDS` 里漏写了 `libc.src.__support.CPP.span`。`DEPENDS` 同时负责把 `__support` 头文件所在目录加入该目标的 include 搜索路径（见 u2-l3、u4-l1）；漏掉它，编译器就找不到 `span.h`。修法是在 `DEPENDS` 里补上 `libc.src.__support.CPP.span`（或同目录内可写 `.span`）。这再次印证「C++ include 与 CMake `DEPENDS` 必须一一对应」。

**练习 2**：`inet_ntop` 为什么要把 `(dst, size)` 包成 `cpp::span<char>` 再传给内部函数，而不是直接传「指针+长度」两个参数？

> **参考答案**：把「指针+长度」绑成一个 `span` 对象，至少有三个好处：(1) 两个本来容易失配的参数（指针和它的长度）被绑在一起，不会出现「指针换了、长度忘改」的悬空错误；(2) 下游函数获得 `size()`、`operator[]`、`subspan()`、`begin()/end()` 等安全、可读的接口，能用范围 `for`、能做切片；(3) 统一了「数据来自 C 数组 / `cpp::array` / 另一个 `span` / 裸指针」的多种来源（见 `span` 的构造函数群），下游不必关心缓冲区「装在哪」。这是用现代 C++ 提升安全性的典型范式。

**练习 3**：`span` 内部目标 `DEPENDS .array` 和 `.type_traits`（[CMakeLists.txt:69-71](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/src/__support/CPP/CMakeLists.txt#L69-L71)）。请说明这两条 `DEPENDS` 与 `span.h` 的 `#include` 之间的对应关系。

> **参考答案**：`span.h` 第 13 行 `#include "array.h"` 对应 `.array`；第 16 行 `#include "type_traits.h"`（用到 `remove_cv_t`/`enable_if_t`/`is_same_v`/`is_const_v`）对应 `.type_traits`。这正是「C++ include 与 CMake `DEPENDS` 一一对应」在 `CPP/` 内部的体现——`CPP/` 工具之间也通过 `DEPENDS` 互相引用，且都用相对名（`.array`/`.type_traits`，因为同处 `CPP/` 目录），保持了「自包含、零系统依赖」的闭环。

---

## 5. 综合实践

把本讲三个最小模块串起来，完成下面这个「`cpp::span` 接线」小任务。

**任务背景**：假设你正在实现一个内部辅助函数 `internal::lowercase_to(span<char> buf)`，它要把 `buf` 里的 ASCII 大写字母原地转成小写。你想用 `cpp::span` 作为参数类型，并复用 `__support/ctype_utils.h` 里已有的字符判定/转换能力（见 u4-l1、u5-l1）。

**要求**：

1. **选型（对应 4.1）**：说明为什么这里用 `cpp::span<char>` 而不是 `std::span`，也不是裸的 `char *buf, size_t len`。结合「自包含动机」给出两点理由。
2. **取能力（对应 4.2）**：写出 `lowercase_to` 的骨架（标注「示例代码」），要求：
   - 参数类型为 `cpp::span<char> buf`；
   - 用 `buf.size()`、`buf[i]`/范围 `for` 遍历；
   - 遍历时用 `cpp::numeric_limits<unsigned char>::max()` 做与 `isalpha` 类似的边界说明（说明为何 `span` 的元素已是 `char`，此处仍可借 `numeric_limits` 表达「合法 ASCII 上界」的意图）。
3. **接线（对应 4.3）**：为这个内部工具所在的内部目标写一份 `add_header_library`（标注「示例代码」），正确列出 `HDRS` 与 `DEPENDS`（至少应包含 `libc.src.__support.CPP.span`、`libc.src.__support.CPP.limits`、`libc.src.__support.ctype_utils`），并逐条说明每条 `DEPENDS` 的来源（对应哪个 `#include` 或哪个被调函数）。

**交付物**：一份「`lowercase_to` 设计说明书」，含：选型理由、骨架代码（标注示例）、CMake `DEPENDS` 清单及逐条溯源。

**示例骨架（供参考，标注为示例代码）**：

```cpp
// 示例代码：仅用于说明用法，非仓库已有文件
#include "src/__support/CPP/limits.h"
#include "src/__support/CPP/span.h"
#include "src/__support/ctype_utils.h"
#include "src/__support/macros/config.h"

namespace LIBC_NAMESPACE_DECL {
namespace internal {

LIBC_INLINE void lowercase_to(cpp::span<char> buf) {
  for (char &c : buf) {
    // 仅处理合法 ASCII 范围；超界字符保持不变（仅作示意）
    if (static_cast<unsigned char>(c) <=
        cpp::numeric_limits<unsigned char>::max()) {
      if (internal::isalpha(c))
        c = internal::tolower(c); // 复用 ctype_utils 的既有能力
    }
  }
}

} // namespace internal
} // namespace LIBC_NAMESPACE_DECL
```

> 这是一个纯源码阅读 + 设计型实践，无需编译。若你有构建环境，可进一步把骨架真的接进去，并参照 u10-l1 的方法为它写一组单元测试。

## 6. 本讲小结

- **`src/__support/CPP/` 是 libc 写给自己用的「C++ 标准库子集」**：因为 libc 要替换系统 libc、要跑在没有 C++ 运行时的目标（GPU/baremetal/UEFI）、且追求符号洁癖，所以**不能依赖 libstdc++/libc++**，只能自带一份「只够自用的部分重实现」。
- **它靠三条纪律维持「自包含」**：只 include 本目录头 / free-standing C 头（用 `<stddef.h>` 而非 `<cstddef>`）/ 少数 `macros/` 宏头；命名空间锁进 `LIBC_NAMESPACE::cpp`；每个 `CPP/foo.h` 是 `std::<foo>` 的「精确子集」。
- **`cpp::numeric_limits` 用模板特化 + 位运算推导极值**：只为整型实现（浮点交给 `FPUtil`），靠 `CHAR_BIT * sizeof(T)` 与 `T(-1) < T(0)` 做到平台无关，取代魔法数字。
- **`cpp::span` 是安全的「指针+长度」非拥有视图**：提供多种来源的构造、`size()`/`operator[]`/`subspan()` 等接口，并复用自家 `numeric_limits`（`dynamic_extent`）与 `type_traits`。
- **入口点使用 `CPP/` 工具只需两步且缺一不可**：C++ 里 `#include "src/__support/CPP/<foo>.h"` 写 `cpp::<foo>`；CMake 的 `DEPENDS` 里加 `libc.src.__support.CPP.<foo>`。三个真实入口点（`isalpha`、`string_length`、`inet_ntop`）分别示范了 `numeric_limits`、`is_same_v`、`span` 的典型用法。

## 7. 下一步学习建议

本讲深入了 `__support` 地图里的 `CPP/` 一格，接下来可以按两条线推进：

1. **继续横向扫 `__support` 的其它常用子模块**：最自然的下一个是 **u4-l3（错误处理：`error_or` 与 `errno`）**——几乎所有「会失败的」入口点（`open`、`malloc`、`strtol`…）都依赖 `error_or.h` 与 `libc_errno.h`，它们与 `CPP/` 一样是扁平铺在 `__support` 根目录的内部工具。
2. **顺着 `numeric_limits` 与 `span` 的使用场景纵向深入**：
   - 想看「整型/字符串互转」如何大量复用 `cpp::numeric_limits`，去看 **u7-l1（stdlib 数值转换）** 的 `str_to_integer.h`/`integer_to_string.h`；
   - 想看「浮点」为何不走 `numeric_limits` 而走专门的位域工具，去看 **u6-l2（FPUtil：浮点位运算）** 的 `FPBits.h`；
   - 想看 `cpp::span` 在格式化核心里如何承载输出缓冲，去看 **u7-l2（printf_core 架构）** 的 `writer.h`。

建议在进入下一讲前，回头确认两件事：你能在 `src/__support/CPP/` 下迅速定位到 `limits.h`/`span.h` 并说出它们的「精确子集」边界；以及你能说出「为什么不能直接 `#include <limits>`」。能答上来，本讲的目标就达成了。
