# 实现规范与核心宏：命名空间与 LLVM_LIBC_FUNCTION

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清为什么 LLVM-libc 把所有内部实现都关进一个名叫 `LIBC_NAMESPACE_DECL` 的命名空间，以及这个命名空间为什么带有「隐藏可见性（hidden visibility）」。
- 看懂 `LLVM_LIBC_FUNCTION(int, isalpha, (int c))` 这个宏层层展开后，最终生成了哪几行声明，并解释其中的 `asm(...)` 和 `[[gnu::alias(...)]]` 各自的作用。
- 理解「C++ 实现符号」与「公开 C 链接符号」是如何通过汇编别名（asm alias）解耦的，以及这种设计为何能避免与系统 libc 的同名符号冲突。
- 掌握一个入口点（entrypoint）的标准文件结构：实现头文件 `.h` 与实现文件 `.cpp` 各自必须包含哪些要素。

本讲是上一讲《入口点（entrypoint）机制》的延续：上一讲讲了入口点「如何被构建系统管理」，本讲讲入口点的实现代码「必须写成什么样」。

## 2. 前置知识

阅读本讲前，建议你已经具备以下认知（由前面讲义建立）：

- **入口点（entrypoint）**：LLVM-libc 中每个对外公开的函数/全局变量都是一个独立、有名的构建单元（见 u2-l1）。
- **Full / Overlay 构建模式**：同一个函数实现，既可能作为完整 libc 替换品（Full，产出 `libc.a`），也可能只覆盖系统 libc 的少数符号（Overlay，产出 `libllvmlibc.a`，见 u1-l4）。
- **静态库与链接顺序**：静态库（`.a`）里只有被引用到的对象文件才会被链入；同名符号的取舍受链接顺序影响（见 u1-l4）。

此外需要两个 C/C++ 基础概念：

- **名称修饰（name mangling）**：C++ 编译器会把命名空间和函数类型编码进符号名，例如 `__llvm_libc::isalpha` 在目标文件里并不是字面字符串 `isalpha`，而是一串修饰后的名字。而 C 语言的符号名通常就是函数名本身。`extern "C"` 用来关闭修饰，但 LLVM-libc **不**用 `extern "C"`（原因见 4.3）。
- **汇编符号名（assembler name）**：GCC/Clang 允许用 `asm("名字")` 给一个变量或函数指定它在目标文件里真正的符号名，独立于 C++ 修饰后的名字。这是本讲的核心机制。

> 术语提示：本讲多次出现「符号（symbol）」一词，指链接器看到的、目标文件里的命名实体；「可见性（visibility）」指符号能否被动态链接时跨共享库访问，`hidden` 表示仅在当前模块内可见。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [docs/dev/implementation_standard.md](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/docs/dev/implementation_standard.md) | 官方「入口点实现规范」文档，规定 `.h` / `.cpp` 的标准结构 |
| [src/__support/macros/config.h](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/src/__support/macros/config.h) | 定义 `LIBC_NAMESPACE_DECL`（带隐藏可见性的命名空间宏） |
| [src/__support/common.h](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/src/__support/common.h) | 定义 `LLVM_LIBC_FUNCTION` / `LLVM_LIBC_VARIABLE` 宏，是本讲的「主角文件」 |
| [src/ctype/isalpha.h](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/src/ctype/isalpha.h) | 入口点实现头文件的标准范例 |
| [src/ctype/isalpha.cpp](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/src/ctype/isalpha.cpp) | 入口点实现文件的标准范例，使用了 `LLVM_LIBC_FUNCTION` |
| [CMakeLists.txt](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/CMakeLists.txt) | 在配置阶段定义全局宏 `LIBC_NAMESPACE` |
| [docs/dev/code_style.md](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/docs/dev/code_style.md) | `LIBC_NAMESPACE_DECL` 的技术依据与两条 clang-tidy 检查规则 |

## 4. 核心概念与源码讲解

本讲拆为四个最小模块：命名空间约定（4.1）、`LLVM_LIBC_FUNCTION` 宏（4.2）、asm 符号别名（4.3）、文件结构约定（4.4）。其中 4.2 与 4.3 紧密耦合，建议连读。

### 4.1 命名空间约定：把内部实现关进 `LIBC_NAMESPACE_DECL`

#### 4.1.1 概念说明

LLVM-libc 的实现用 C++ 写成，但对外暴露标准 C 接口。一个自然的问题是：实现代码里写的 `isalpha`，到底是「我们这个 libc 的 `isalpha`」，还是「系统 libc 的 `isalpha`」？在 Overlay 模式下，公共头文件 `ctype.h`（来自系统）会声明一个全局的 `isalpha`，如果我们的实现也直接落在全局命名空间里，两者就会撞名、甚至让函数调用解析到错误的实现上。

为彻底消除这种歧义，规范要求：**所有内部声明与定义都必须包裹在 `LIBC_NAMESPACE_DECL` 命名空间内**。这样实现符号带上了唯一的 C++ 修饰名，与系统 libc 的全局 `isalpha` 物理隔离；内部代码引用时一律写 `LIBC_NAMESPACE::isalpha`，明确指向「自己人」。官方文档原话是：

> All LLVM-libc implementation constructs must be enclosed in the `LIBC_NAMESPACE_DECL` namespace.
>
> 见 [docs/dev/implementation_standard.md:L39-L41](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/docs/dev/implementation_standard.md#L39-L41)

#### 4.1.2 核心流程

`LIBC_NAMESPACE_DECL` 是一个宏，它的值由两层拼出来：

1. **第一层：`LIBC_NAMESPACE` 的值从哪来？** 它不是在头文件里 `#define` 的，而是在 CMake 配置阶段计算后，通过 `add_compile_definitions` 注入为全局编译宏。默认值是 `__llvm_libc`；但当能读到 LLVM 版本号时，会拼上版本后缀（如 `__llvm_libc_19_0_0_git`）。
2. **第二层：`LIBC_NAMESPACE_DECL` 在其基础上加可见性属性。** 在 Clang 下它展开为 `[[gnu::visibility("hidden")]] LIBC_NAMESPACE`；在 GCC 下（暂因告警问题）退化为不带属性的 `LIBC_NAMESPACE`。

用伪代码表示展开关系：

```
LIBC_NAMESPACE             ← CMake 注入，例: __llvm_libc_19_0_0_git
LIBC_NAMESPACE_DECL        ← [[gnu::visibility("hidden")]] LIBC_NAMESPACE
namespace LIBC_NAMESPACE_DECL { ... }   等价于  namespace __llvm_libc_19_0_0_git [[hidden]] { ... }
```

#### 4.1.3 源码精读

先看 `LIBC_NAMESPACE` 是如何被注入的。在仓库根 [CMakeLists.txt:L57-L78](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/CMakeLists.txt#L57-L78)：先设默认命名空间为 `__llvm_libc`，若存在 `LLVM_VERSION_MAJOR` 则拼出版本后缀，最后校验它必须以 `__llvm_libc` 开头，并用 `add_compile_definitions(LIBC_NAMESPACE=...)` 推给所有编译单元。这段代码把「命名空间名」变成了一个构建期可配置项。

再看 `LIBC_NAMESPACE_DECL` 的定义本身，在 [src/__support/macros/config.h:L57-L71](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/src/__support/macros/config.h#L57-L71)。关键几行（Clang 分支）：

```c
#define LIBC_NAMESPACE_DECL [[gnu::visibility("hidden")]] LIBC_NAMESPACE
```

代码上方的注释（同文件 L57-L65）解释了为什么必须是带隐藏可见性的版本：隐藏可见性保证本翻译单元（TU）里的 extern 声明具有确定可见性，**不会生成 GOT 间接跳转（GOT indirection / dynamic relocation）**，这对某些场合的正确性是必要的；而公开 C 符号的可见性由 `LLVM_LIBC_FUNCTION_ATTR` 单独、独立地控制。

还有一个守卫：[src/__support/common.h:L12-L14](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/src/__support/common.h#L12-L14) 在 `LIBC_NAMESPACE` 未定义时直接 `#error`，确保任何使用公共宏的翻译单元都不会「忘记」带上命名空间上下文。

这套约定还被两条 clang-tidy 检查强制执行（见 [docs/dev/code_style.md:L324-L354](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/docs/dev/code_style.md#L324-L354) 的 `implementation-in-namespace` 检查，以及 L356-L392 的 `callee-namespace` 检查）：前者要求顶层声明必须在 `LIBC_NAMESPACE_DECL` 内；后者要求命名空间内的函数调用只能解析到 `LIBC_NAMESPACE` 里的符号，禁止调到全局的 `::strlen` 或系统 `::malloc`。这就从静态分析层面锁死了「误用系统 libc 符号」的可能。

#### 4.1.4 代码实践

1. **实践目标**：确认你机器上某次构建里 `LIBC_NAMESPACE` 的实际取值。
2. **操作步骤**：在一个已配置好的 LLVM-libc 构建目录下（见 u1-l3 的 runtimes 构建），找到任意一个被编译的入口点目标文件对应的编译命令，例如用 `ninja -t commands libc.src.ctype.isalpha.__objects__ 2>/dev/null | head` 或在 `build.ninja` 里搜索 `isalpha.cpp`，观察其中的 `-DLIBC_NAMESPACE=...` 宏定义。
3. **需要观察的现象**：编译命令里应出现形如 `-DLIBC_NAMESPACE=__llvm_libc_19_0_0_git` 的参数（版本号随你的 LLVM 版本变化）。
4. **预期结果**：确认它确实以 `__llvm_libc` 开头，且带有版本后缀。如果只能看到 `-DLIBC_NAMESPACE=__llvm_libc`（无后缀），说明该构建没有读到 `LLVM_VERSION_MAJOR`，符合 [CMakeLists.txt:L59-L62](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/CMakeLists.txt#L59-L62) 的 `if(LLVM_VERSION_MAJOR)` 分支。
5. 若无法本地构建，标注「待本地验证」，并直接以 `__llvm_libc_19_0_0_git` 作为示例值理解后续内容即可。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `LIBC_NAMESPACE` 要带上版本后缀（如 `_19_0_0_git`），而不是固定叫 `__llvm_libc`？

> **参考答案**：带上版本后缀后，不同 LLVM 版本编译出的 libc 在 C++ 修饰名层面互不相同，可以在同一个进程里共存而不会因「同一修饰名、不同实现」而违反 ODR（单一定义规则）或造成符号冲突；同时校验仍要求以 `__llvm_libc` 开头（[CMakeLists.txt:L73-L75](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/CMakeLists.txt#L73-L75)），保留可识别前缀。

**练习 2**：`LIBC_NAMESPACE` 与 `LIBC_NAMESPACE_DECL` 在用途上的区别是什么？

> **参考答案**：`LIBC_NAMESPACE` 仅当作**标识符**用来**访问**已存在的内部符号（如 `LIBC_NAMESPACE::cpp::max`）；而**声明或定义**内部符号时必须用 `LIBC_NAMESPACE_DECL`，因为它额外带上了隐藏可见性属性（见 [docs/dev/code_style.md:L262-L266](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/docs/dev/code_style.md#L262-L266)）。

### 4.2 `LLVM_LIBC_FUNCTION` 宏：变长参数分派

#### 4.2.1 概念说明

入口点的 `.cpp` 文件并不直接写 `int isalpha(int c) { ... }`，而是写：

```cpp
LLVM_LIBC_FUNCTION(int, isalpha, (int c)) {
  // 实现
}
```

`LLVM_LIBC_FUNCTION(type, name, arglist)` 是一个「函数定义生成器」宏。它接受返回类型、函数名、参数列表三件套（也可接受第四个可选参数 `c_alias` 显式指定公开符号名），展开成一组声明加一个函数定义的开头。它的存在让「写出符合规范的入口点」变成一件机械的事，并把「C++ 内部符号 ↔ 公开 C 符号」的映射统一收口在这一处。

#### 4.2.2 核心流程

宏的设计采用「变长参数 + 计数分派」的常见 C 预处理技巧：

```
LLVM_LIBC_FUNCTION(...)                         ← 入口
   ↓ GET_FIFTH 数出参数个数
   ├─ 3 个参数 → LLVM_LIBC_FUNCTION_IMPL_3(type,name,arglist)
   │              ↓ 自动补 c_alias = #name（即字符串化的 name）
   │              → LLVM_LIBC_FUNCTION_IMPL_4(type,name,arglist,"name")
   └─ 4 个参数 → LLVM_LIBC_FUNCTION_IMPL_4(type,name,arglist,c_alias)   ← 真正干活的宏
```

`LLVM_LIBC_FUNCTION_IMPL_4` 再根据构建环境（是否 `LIBC_COPT_PUBLIC_PACKAGING`、是否 MSVC、是否前导下划线平台）选择三种展开形态之一。换句话说，同一个宏在「打包成静态库」与「仅用于测试/内部」时会展开成不同代码。

#### 4.2.3 源码精读

变长分派逻辑在 [src/__support/common.h:L80-L84](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/src/__support/common.h#L80-L84)：

```c
#define LLVM_LIBC_FUNCTION(...)                                                \
  GET_FIFTH(__VA_ARGS__, LLVM_LIBC_FUNCTION_IMPL_4, LLVM_LIBC_FUNCTION_IMPL_3, \
            GET_NOTHING)(__VA_ARGS__)
```

`GET_FIFTH`（[同文件 L48](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/src/__support/common.h#L48)）取出第 5 个实参。传入 3 个参数时，排在第 5 位的是 `LLVM_LIBC_FUNCTION_IMPL_3`；传入 4 个参数时，第 5 位变成 `LLVM_LIBC_FUNCTION_IMPL_4`。于是宏调用被路由到正确的 `IMPL`。

`IMPL_3` 仅仅把 `c_alias` 补成字符串化的名字，再委托给 `IMPL_4`，见 [src/__support/common.h:L77-L78](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/src/__support/common.h#L77-L78)：

```c
#define LLVM_LIBC_FUNCTION_IMPL_3(type, name, arglist)                         \
  LLVM_LIBC_FUNCTION_IMPL_4(type, name, arglist, #name)
```

真正「干活」的是 `IMPL_4`，它有三种形态。其中最常见、也是理解本讲的关键形态（打包构建、非 MSVC、非前导下划线平台）在 [src/__support/common.h:L58-L63](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/src/__support/common.h#L58-L63)：

```c
#define LLVM_LIBC_FUNCTION_IMPL_4(type, name, arglist, c_alias)                \
  LLVM_LIBC_ATTR(name)                                                         \
  LLVM_LIBC_FUNCTION_ATTR decltype(LIBC_NAMESPACE::name)                       \
      __##name##_impl__ asm(c_alias);                                          \
  decltype(LIBC_NAMESPACE::name) name [[gnu::alias(c_alias)]];                 \
  type __##name##_impl__ arglist
```

这一段的细节留到 4.3 逐行拆解。这里只需注意：宏的开头是 `LLVM_LIBC_ATTR(name)`，它是「按函数名注入额外属性」的钩子。其机制在 [src/__support/common.h:L36-L51](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/src/__support/common.h#L36-L51)：若你定义了形如 `LLVM_LIBC_FUNCTION_ATTR_memcpy` 的宏（值需以 `LLVM_LIBC_EMPTY, ` 开头），它就会把对应的属性（如 `[[gnu::weak]]`）插到该函数声明前；未定义时该宏展开为空。这是给厂商/移植者按需调属性的官方扩展点。

> 对比旧文档：官方规范文档 [docs/dev/implementation_standard.md:L63-L75](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/docs/dev/implementation_standard.md#L63-L75) 给出的宏展开是简化版（用 `__##name##_impl__ __asm__(#name)` 与 `[[gnu::alias(#name)]]`）。当前源码 `common.h` 的实际版本新增了可变 `c_alias`、`LLVM_LIBC_ATTR` 钩子、以及前导下划线/MSVC/打包与否的多分支，逻辑更完整但核心「asm 名 + alias」机制一致。**以源码 `common.h` 为准。**

#### 4.2.4 代码实践

跟踪 `LLVM_LIBC_FUNCTION(int, isalpha, (int c))` 的分派过程（纯源码阅读）：

1. **实践目标**：亲手把宏展开一层层还原，确认它最终走到哪个 `IMPL`。
2. **操作步骤**：
   - 代入 `__VA_ARGS__ = int, isalpha, (int c)` 到 [common.h:L82-L84](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/src/__support/common.h#L82-L84) 的 `GET_FIFTH(...)`，数出第 5 个实参是 `LLVM_LIBC_FUNCTION_IMPL_3` 还是 `IMPL_4`。
   - 再到 [common.h:L77-L78](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/src/__support/common.h#L77-L78)，确认 `IMPL_3` 把 `#name` 即 `"isalpha"` 作为 `c_alias` 传给 `IMPL_4`。
3. **需要观察的现象**：三参数写法与四参数写法 `LLVM_LIBC_FUNCTION(int, isalpha, (int c), "isalpha")` 走到完全相同的 `IMPL_4`。
4. **预期结果**：得到 `IMPL_4(int, isalpha, (int c), "isalpha")`，为下一节（4.3）的逐行展开做好准备。

#### 4.2.5 小练习与答案

**练习 1**：为什么要用「`GET_FIFTH` 数参数个数」这种写法，而不是直接定义两个不同名的宏？

> **参考答案**：为了让调用方始终用同一个名字 `LLVM_LIBC_FUNCTION(...)`，由预处理器根据传入参数个数自动选择 3 参或 4 参的实现，降低使用心智负担，同时保留「显式指定 `c_alias`」的逃生舱（例如某些平台需要把公开符号名改成带前缀的形式）。

**练习 2**：`LLVM_LIBC_FUNCTION_ATTR` 宏默认展开成什么？谁会去重定义它？

> **参考答案**：默认为空（见 [common.h:L27-L29](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/src/__support/common.h#L27-L29) 的 `#ifndef LLVM_LIBC_FUNCTION_ATTR` 分支）。由需要给公开符号附加自定义属性的厂商或移植者重定义，例如改变默认可见性。

### 4.3 asm 符号别名：把 C++ 实现映射成公开 C 符号

#### 4.3.1 概念说明

上一节看到 `IMPL_4` 用到了 `asm(c_alias)` 和 `[[gnu::alias(c_alias)]]`。本节回答核心问题：**这两行如何在不使用 `extern "C"`、不污染全局命名空间的前提下，让外部 C 代码能用 `isalpha` 这个名字链接到我们的实现？又如何避免与系统 libc 的 `isalpha` 冲突？**

直觉是这样：实现函数本身留在版本化的 C++ 命名空间里（修饰名独一无二），但用一个 `asm` 别名把它的「汇编符号名」改写成公开的 C 名字 `isalpha`；同时再给 C++ 命名空间里的 `isalpha` 声明一个 `gnu::alias`，让内部代码用 `LIBC_NAMESPACE::isalpha` 调用时也指向同一份代码。于是「一个实现体，两个名字」：内部用 C++ 修饰名（安全、可共存），外部用 C 名字（兼容标准 ABI）。

#### 4.3.2 核心流程

以 `isalpha` 为例，`IMPL_4(int, isalpha, (int c), "isalpha")` 在打包构建（非 MSVC、非前导下划线）下展开为三行声明加函数体：

```c
// 第 1 行：声明「实现体」函数 __isalpha_impl__，并把它的汇编符号名改写为 "isalpha"
decltype(LIBC_NAMESPACE::isalpha) __isalpha_impl__ asm("isalpha");
// 第 2 行：声明命名空间内的 isalpha 是指向符号 "isalpha" 的别名
decltype(LIBC_NAMESPACE::isalpha) isalpha [[gnu::alias("isalpha")]];
// 第 3 行：函数定义的开头（后面接 { ... }）
int __isalpha_impl__(int c)
```

效果示意：

```
                       目标文件里的符号表
   ┌──────────────────────────────────────────────┐
   │  符号 "isalpha"   ──►  __isalpha_impl__ 的机器码  │  ← asm("isalpha") 改写得到
   │  (LIBC_NAMESPACE::isalpha) ─► alias ─► "isalpha" │  ← gnu::alias 指回同一个符号
   └──────────────────────────────────────────────┘
   外部 C 调用 isalpha(...)  ─────────────────►  命中符号 "isalpha"
   内部 C++ 调用 LIBC_NAMESPACE::isalpha(...) ─►  经 alias 也命中符号 "isalpha"
```

关键点：真正的函数体 `__isalpha_impl__` **不存在于**目标文件的符号名 `__isalpha_impl__` 下——它的汇编名被 `asm(...)` 改写成了 `isalpha`。而 C++ 命名空间里的 `isalpha` 反而是一个 alias，指向那个被改写过名字的符号。两者殊途同归，指向同一份机器码。

冲突避免可以分两层理解：

- **C++ 层**：实现体在版本化命名空间 `__llvm_libc_19_0_0_git` 内，修饰名独一无二，与系统 libc 全局 `isalpha`、与其他版本的 libc 都不撞。内部调用一律走 `LIBC_NAMESPACE::isalpha`，并由 `callee-namespace` 检查锁死。
- **链接层**：公开 C 符号 `isalpha` 是一个独立的别名符号。在 Overlay 模式下，靠把 `libllvmlibc.a` 排在系统 libc 之前的链接顺序（见 u1-l4），让链接器优先选取本库的 `isalpha`，其余未覆盖的函数仍回退系统 libc。因为实现体被 `asm` 改名、又被命名空间隔离，所以「覆盖」是干净的符号替换，不会在编译期产生重定义。

> 为什么不用 `extern "C"`？`extern "C"` 会把符号放进全局 C 命名空间并关闭修饰，那样实现体就真的变成了全局 `isalpha`，既破坏了命名空间隔离，也无法支撑「版本后缀共存」。`asm` 别名只改汇编符号名、不动 C++ 语义，是更精准的工具。

#### 4.3.3 源码精读

逐行看 [src/__support/common.h:L58-L63](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/src/__support/common.h#L58-L63) 这段（注释里说本分支不适用于前导下划线平台）：

- 第 1 句 `LLVM_LIBC_ATTR(name)`：按需注入额外属性，默认空。
- 第 2-3 句 `LLVM_LIBC_FUNCTION_ATTR decltype(LIBC_NAMESPACE::name) __##name##_impl__ asm(c_alias);`：声明实现体 `__isalpha_impl__`，类型取自已在前置 `.h` 里声明过的 `LIBC_NAMESPACE::isalpha`（所以这里能用 `decltype`），并用 `asm(c_alias)` 把汇编符号名定为 `isalpha`。`LLVM_LIBC_FUNCTION_ATTR` 给厂商留属性扩展位（默认空，见 [common.h:L27-L29](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/src/__support/common.h#L27-L29)）。
- 第 4 句 `decltype(LIBC_NAMESPACE::name) name [[gnu::alias(c_alias)]];`：声明 C++ 命名空间里的 `isalpha` 是指向符号 `isalpha` 的 alias。
- 第 5 句 `type __##name##_impl__ arglist`：函数定义签名 `int __isalpha_impl__(int c)`，紧接其后的 `{ ... }` 就是实现体。

接着看另外两种形态：

- **前导下划线平台**（macOS、Windows+x86_32，由 [common.h:L22-L25](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/src/__support/common.h#L22-L25) 定义 `LIBC_TARGET_USES_LEADING_UNDERSCORE`），分支在 [common.h:L65-L69](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/src/__support/common.h#L65-L69)。这类平台的 C ABI 符号自带前导下划线，所以不再造 `__name_impl__` + alias，而是直接把命名空间里的 `name` 的汇编名改成 `"_isalpha"`（`asm("_" c_alias)`）。这是同一思想的平台适配。
- **非打包构建**（未定义 `LIBC_COPT_PUBLIC_PACKAGING`，典型是单元测试直接编内部对象），分支在 [common.h:L73-L75](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/src/__support/common.h#L73-L75)，宏退化为最朴素的 `type name arglist`——因为测试只调内部命名空间符号，不需要公开 C 别名。

变量（全局对象）的对应宏是 `LLVM_LIBC_VARIABLE`，机制完全平行（asm 名 + alias），见 [common.h:L88-L107](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/src/__support/common.h#L88-L107)。例如 `LLVM_LIBC_VARIABLE(char **, environ) = nullptr;` 就能为全局变量 `environ` 生成公开 C 符号。

真实调用方就在 [src/ctype/isalpha.cpp:L16-L24](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/src/ctype/isalpha.cpp#L16-L24)：整个文件包在 `namespace LIBC_NAMESPACE_DECL` 内，函数用 `LLVM_LIBC_FUNCTION(int, isalpha, (int c))` 定义，函数体只做边界检查与委托——真正的算法在 `internal::isalpha`（见 u5-l1）。

#### 4.3.4 代码实践

本节给出本讲的主干实践（也是讲义级 `practice_task`）。

1. **实践目标**：亲手画出 `LLVM_LIBC_FUNCTION(int, isalpha, (int c))` 在打包构建下展开后的三行声明 + 函数签名，并据此解释它如何避免与系统 libc 同名符号冲突。
2. **操作步骤**：
   - 打开 [src/__support/common.h:L58-L63](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/src/__support/common.h#L58-L63)，把 `type=int`、`name=isalpha`、`arglist=(int c)`、`c_alias="isalpha"` 代入，逐行抄写出展开结果。
   - 在展开结果上用三种颜色/记号分别标出：① 实现体 `__isalpha_impl__` 及其 `asm("isalpha")`；② C++ 别名 `isalpha` 及其 `[[gnu::alias("isalpha")]]`；③ 函数定义签名。
   - 再打开 [src/ctype/isalpha.cpp:L18-L22](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/src/ctype/isalpha.cpp#L18-L22)，确认第 ③ 部分的函数体正是这里的实现。
3. **需要观察的现象**：目标文件里实际只存在一个名为 `isalpha` 的函数符号；`__isalpha_impl__` 这个名字不会出现在最终符号表中（被 asm 改写）。
4. **预期结果**：你能用一句话说明——「实现体用 `asm` 改名成公开 C 符号、用版本化命名空间隔离 C++ 侧、再用 alias 让内部调用复用同一符号；Overlay 模式靠链接顺序在系统 libc 之上覆盖该符号」。这正是避免冲突的核心。
5. 可选验证（标注「待本地验证」）：构建后用 `nm` 或 `llvm-nm` 查看 `isalpha.cpp` 产生的目标文件，应能看到一个 `T isalpha`（text 段的公开符号），而看不到 `__isalpha_impl__`。

#### 4.3.5 小练习与答案

**练习 1**：如果把 `asm(c_alias)` 这一句删掉（只保留 alias 和函数定义），会发生什么？

> **参考答案**：实现体的汇编符号名会退回到 C++ 修饰名（如 `_ZN15__llvm_libc_..._isalphaE...` 之类），而 `[[gnu::alias("isalpha")]]` 指向的符号 `isalpha` 将没有定义，链接时会出现「未定义符号 `isalpha`」错误。`asm(...)` 正是把实现体重命名到公开符号的关键一步。

**练习 2**：前导下划线平台（macOS）上 `LLVM_LIBC_FUNCTION(int, isalpha, (int c))` 展开成什么？

> **参考答案**：走 [common.h:L65-L69](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/src/__support/common.h#L65-L69) 分支，展开为 `decltype(LIBC_NAMESPACE::isalpha) isalpha asm("_isalpha");` 加 `int isalpha(int c)`，即把命名空间内 `isalpha` 的汇编名直接定为 macOS ABI 所需的 `_isalpha`，不再额外造 `__isalpha_impl__`。

**练习 3**：为什么单元测试目标里这个宏会退化成普通函数？

> **参考答案**：测试目标通常不定义 `LIBC_COPT_PUBLIC_PACKAGING`，走 [common.h:L73-L75](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/src/__support/common.h#L73-L75) 的退化分支，宏变成 `type name arglist`。因为测试只通过内部命名空间 `LIBC_NAMESPACE::isalpha` 直接断言（见 u1-l5、u10-l1），不需要公开 C 别名。

### 4.4 文件结构约定：实现头 `.h` 与实现文件 `.cpp`

#### 4.4.1 概念说明

「规范」不只管宏，还管文件长什么样。每个入口点至少有一个**实现头文件** `<entrypoint名>.h`（如 `src/ctype/isalpha.h`），用来声明内部命名空间下的函数原型；主实现文件 `<entrypoint名>.cpp`（如 `src/ctype/isalpha.cpp`）用 `LLVM_LIBC_FUNCTION` 给出定义。这两类文件各有固定的「骨架」，保证全库风格统一、工具链（如 hdrgen、clang-tidy）能稳定解析。

#### 4.4.2 核心流程

实现头文件骨架：

```
1. 头文件守卫      LLVM_LIBC_SRC_<路径>_<文件名>_H     （与目录路径镜像）
2. 必备 include    src/__support/macros/config.h       （拿到 LIBC_NAMESPACE_DECL）
3. 命名空间        namespace LIBC_NAMESPACE_DECL { ... }
4. 函数原型        int isalpha(int c);                 （普通 C++ 声明，不加宏）
```

实现文件骨架：

```
1. 自身实现头      #include "src/ctype/isalpha.h"
2. 依赖 include    common.h / macros/config.h / 所需 __support 头
3. 命名空间        namespace LIBC_NAMESPACE_DECL { ... }
4. 函数定义        LLVM_LIBC_FUNCTION(int, isalpha, (int c)) { ... }
   全局变量        LLVM_LIBC_VARIABLE(type, name) = ...;   （如有）
```

#### 4.4.3 源码精读

实现头文件范例见 [src/ctype/isalpha.h:L9-L20](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/src/ctype/isalpha.h#L9-L20)：守卫 `LLVM_LIBC_SRC_CTYPE_ISALPHA_H` 与路径 `src/ctype/isalpha.h` 一一对应；先包含 `src/__support/macros/config.h` 以获得 `LIBC_NAMESPACE_DECL`；然后在命名空间内声明普通原型 `int isalpha(int c);`。注意头文件里**不**用 `LLVM_LIBC_FUNCTION`，只是普通声明——因为头文件会被多处包含（含测试），不能在每处都生成 asm 别名。

实现文件范例见 [src/ctype/isalpha.cpp:L9-L24](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/src/ctype/isalpha.cpp#L9-L24)：先包含自身实现头与一组 `__support` 头（`common.h` 提供 `LLVM_LIBC_FUNCTION`、`CPP/limits.h` 提供 `numeric_limits`、`ctype_utils.h` 提供真正的判定逻辑），再在 `LIBC_NAMESPACE_DECL` 内用 `LLVM_LIBC_FUNCTION(int, isalpha, (int c))` 定义函数。

官方规范对这套结构的描述在 [docs/dev/implementation_standard.md:L19-L37](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/docs/dev/implementation_standard.md#L19-L37)（头文件结构）与 [docs/dev/implementation_standard.md:L43-L61](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/docs/dev/implementation_standard.md#L43-L61)（`.cpp` 结构）。变量情形 `LLVM_LIBC_VARIABLE(char **, environ) = nullptr;` 见 [docs/dev/implementation_standard.md:L80-L84](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/docs/dev/implementation_standard.md#L80-L84)。

#### 4.4.4 代码实践

1. **实践目标**：熟悉文件骨架，能凭目录约定默写出一个最小入口点的 `.h` / `.cpp`。
2. **操作步骤**：以 `isdigit` 为假想函数，照 `isalpha` 的骨架，在草稿上写出 `src/ctype/isdigit.h` 与 `src/ctype/isdigit.cpp` 的完整内容（不要改动真实源码，只在草稿/临时文件里写）。注意：头文件守卫应为 `LLVM_LIBC_SRC_CTYPE_ISDIGIT_H`；`.cpp` 必须包含 `common.h` 与 `macros/config.h`。
3. **需要观察的现象**：两个文件的结构与 `isalpha` 完全同构，只是函数名、守卫名、实现内容不同。
4. **预期结果**：你写出的草稿与库内已有的真实 `isdigit` 实现骨架一致（可对照 `src/ctype/isdigit.cpp` 自查）。
5. 这一步为 u11-l3「贡献一个完整新函数」打好基础——那里会要求把 YAML、`.h`、`.cpp`、CMake、entrypoints.txt、测试一次性串起来。

#### 4.4.5 小练习与答案

**练习 1**：为什么实现头文件里用普通声明 `int isalpha(int c);`，而 `.cpp` 里才用 `LLVM_LIBC_FUNCTION`？

> **参考答案**：头文件会被实现文件、测试文件、其他入口点等多处包含；若在头里用宏生成 asm 别名，会在每个包含点都生成同名别名符号，导致重复定义。宏只在唯一的 `.cpp` 定义点使用一次，才能保证「一个实现、一个公开符号」。

**练习 2**：头文件守卫 `LLVM_LIBC_SRC_CTYPE_ISALPHA_H` 与文件路径有什么关系？

> **参考答案**：守卫名就是文件相对路径的「大写 + 下划线」镜像（`src/ctype/isalpha.h` → `LLVM_LIBC_SRC_CTYPE_ISALPHA_H`），并由 clang-tidy 的 `llvm-header-guard` 检查强制保持一致（见 [docs/dev/code_style.md:L296-L299](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/docs/dev/code_style.md#L296-L299)），便于检索与防重复包含。

## 5. 综合实践

把四个最小模块串起来，做一次「宏展开全过程」的端到端追踪：

1. **起点**：阅读 [docs/dev/implementation_standard.md:L43-L75](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/docs/dev/implementation_standard.md#L43-L75)，理解官方对 `.cpp` 结构与宏展开的描述。
2. **追命名空间**：从 [CMakeLists.txt:L57-L78](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/CMakeLists.txt#L57-L78) 找到 `LIBC_NAMESPACE` 的注入点，再到 [config.h:L57-L71](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/src/__support/macros/config.h#L57-L71) 看 `LIBC_NAMESPACE_DECL` 如何给它加上隐藏可见性。
3. **追宏分派**：在 [common.h:L80-L84](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/src/__support/common.h#L80-L84) 处，代入 `LLVM_LIBC_FUNCTION(int, isalpha, (int c))`，确认它被 `GET_FIFTH` 路由到 `IMPL_3`，再在 [common.h:L77-L78](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/src/__support/common.h#L77-L78) 被 `IMPL_3` 补上 `c_alias="isalpha"` 后转入 `IMPL_4`。
4. **追 asm 别名**：在 [common.h:L58-L63](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/src/__support/common.h#L58-L63) 展开三行声明，标注 `asm("isalpha")` 与 `[[gnu::alias("isalpha")]]`，并说明实现体为何只占一个公开符号。
5. **落到真实代码**：打开 [src/ctype/isalpha.cpp:L16-L24](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/src/ctype/isalpha.cpp#L16-L24) 与 [isalpha.h:L9-L20](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/src/ctype/isalpha.h#L9-L20)，确认文件骨架与规范一致。
6. **产出**：画一张包含「CMake → LIBC_NAMESPACE → LIBC_NAMESPACE_DECL → LLVM_LIBC_FUNCTION 分派 → IMPL_4 三行展开 → isalpha.cpp 实现体 → 目标文件符号 `isalpha`」的完整流转图，并在图旁用一句话写出「避免与系统 libc 同名符号冲突」的两层理由（C++ 命名空间隔离 + Overlay 链接顺序覆盖）。

## 6. 本讲小结

- 所有内部实现都必须包在 `LIBC_NAMESPACE_DECL` 内；它是在 `LIBC_NAMESPACE`（CMake 注入、带版本后缀、必须以 `__llvm_libc` 开头）之上加 `[[gnu::visibility("hidden")]]` 得到的。
- 隐藏可见性的作用是避免跨 TU 引用产生 GOT 间接跳转，对部分代码生成的正确性必要；公开 C 符号的可见性另由 `LLVM_LIBC_FUNCTION_ATTR` 独立控制。
- `LLVM_LIBC_FUNCTION(type, name, arglist)` 用「`GET_FIFTH` 数参数」分派到 `IMPL_3`/`IMPL_4`，默认把名字字符串化作为公开符号 `c_alias`。
- 真正干活的是 `IMPL_4`：用 `asm(c_alias)` 把实现体 `__name_impl__` 的汇编符号名改写为公开 C 名，再用 `[[gnu::alias(c_alias)]]` 让 C++ 命名空间内的 `name` 指向同一符号——「一个实现、两个名字」，且不使用 `extern "C"`。
- 这种设计通过「C++ 版本化命名空间隔离 + Overlay 链接顺序覆盖」两层机制，干净地避免与系统 libc 同名符号冲突。
- 入口点遵循固定骨架：实现头 `<name>.h`（守卫镜像路径、含 `config.h`、命名空间内普通声明）+ 实现文件 `<name>.cpp`（用 `LLVM_LIBC_FUNCTION` 定义）；全局变量对应 `LLVM_LIBC_VARIABLE`。

## 7. 下一步学习建议

- 接下来学 **u2-l3《CMake 构建规则详解》**：看 `LLVM_LIBC_FUNCTION` 产出的这些对象文件如何被 `add_entrypoint_object` 注册、再被 `add_entrypoint_library` 聚合成 `libc.a`/`libm.a`。
- 之后学 **u2-l4《平台配置体系》**：理解「实现已就绪」与「是否进入某平台产物」之间由 `entrypoints.txt` 决定的那道关卡。
- 若想看 asm 别名在产物里的真实形态，可在完成 u1-l3 的构建后，对某个入口点目标文件运行 `llvm-nm`，亲眼确认公开符号的存在与命名空间符号的隐藏可见性。
