# 模块化格式串：让编译器按需链接 printf 的实现片段

## 1. 本讲目标

学完本讲后，读者应当能够：

- 说出 `printf`/`scanf` 这类「格式串驱动」函数为什么会产生**编译器看不见的死代码**（尤其是浮点转换表、errno 转换）。
- 解释 clang `modular_format` 属性的三个参数（实现函数、实现名、aspect 列表）分别代表什么，以及它如何在调用点被改写。
- 读懂 `LIBC_PRINTF_MODULE` 与 `LIBC_PRINTF_DEFINE_MODULES` 这对宏如何用**弱符号 + 同一头文件二次包含**把一个函数切成「声明（弱引用）/ 定义（实现）」两种形态。
- 描述从用户配置开关 `LIBC_CONF_PRINTF_MODULAR` 到 C++ 编译宏 `LIBC_COPT_PRINTF_MODULAR` 的传递路径。
- 自行讲清「调用者只用 `%d` 时，编译器为何能把浮点转换表排除出最终二进制」这一贯穿全讲的核心问题。

本讲承接 [u7-l2 printf_core 架构](u7-l2-printf-core-architecture.md) 中建立的 parser→converter→writer 三段式模型，回答一个 u7-l2 留下的伏笔：**三段式提供了扩展点，但 converter 里的浮点转换这种「可能永远用不到的大块代码」该如何从二进制里剔除？**

## 2. 前置知识

在进入正题前，先确认你理解下面四个概念。它们在前面讲义里都已建立，这里只做一句话回顾：

- **入口点（entrypoint）**：LLVM-libc 中每个对外公开函数都是一个独立、有名的构建单元，详见 u2-l1。
- **`__support` 私有库**：所有入口点共享的内部工具库，不产生公开 C 符号，靠 CMake 的 `DEPENDS` 引用，详见 u4-l1。
- **`printf_core` 三段式**：`Parser` 产 `FormatSection`、`Converter` 消费 `FormatSection`、`Writer` 负责输出，三者解耦，详见 u7-l2。
- **静态库（archive，`.a`）的惰性链接**：链接器只有在一个 `.o` 能满足某个**未定义符号**时，才会把它从归档里抽出来参与链接。没人引用的 `.o` 会被整段忽略。这是本讲核心机制的地基，请务必记住。

本讲还会用到两个链接器/编译器的底层概念，先做通俗解释：

- **弱符号（weak symbol）**：一种「可以被覆盖、也可以不存在」的符号。对弱符号的引用不会强制把它的定义拉进链接——若没有任何强引用，包含该定义的目标文件（`.o`）不会进入最终二进制。
- **重定位（relocation）**：目标文件里记录「这里需要在链接时填上某符号的地址」的条目。本讲会出现一种特殊的 `BFD_RELOC_NONE`（空重定位）：它不修改任何字节，只是「制造一次对该符号的引用」——引用本身才是目的。

## 3. 本讲源码地图

本讲涉及的关键文件按角色分成五组：

| 文件 | 角色 |
|------|------|
| [docs/dev/modular_format.md](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/docs/dev/modular_format.md) | 官方设计文档，讲清动机与机制 |
| [include/llvm-libc-macros/_LIBC_MODULAR_FORMAT_PRINTF.h](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/include/llvm-libc-macros/_LIBC_MODULAR_FORMAT_PRINTF.h) | 把 clang `modular_format` 属性封装成可移植的公共宏 |
| [include/stdio.yaml](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/include/stdio.yaml) | 在 YAML 规范里给 `asprintf`/`printf` 等函数挂上该属性（由 hdrgen 渲染进公共头） |
| [src/__support/printf_core/printf_config.h](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/__support/printf_core/printf_config.h) | 定义 `LIBC_PRINTF_MODULE` 宏：按是否模块化展开成「内联定义」或「弱声明」 |
| [src/__support/printf_core/converter.h](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/__support/printf_core/converter.h) | 用 `LIBC_PRINTF_MODULE` 把浮点转换 `convert_float` 写成可模块化的函数 |
| [src/__support/printf_core/float_impl.cpp](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/__support/printf_core/float_impl.cpp) | 定义 aspect 符号 `__printf_float`，并触发浮点模块的真正实例化 |
| [src/__support/printf_core/printf_main.h](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/__support/printf_core/printf_main.h) | 引擎入口，默认（非模块化调用）路径会发出空重定位强制拉入全部 aspect |
| [src/stdio/asprintf.cpp](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/stdio/asprintf.cpp) 与 [src/stdio/asprintf_modular.cpp](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/stdio/asprintf_modular.cpp) | 同一个函数的「默认实现」与「模块化实现」两个入口点壳 |
| [config/config.json](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/config/config.json) 与 [src/__support/printf_core/CMakeLists.txt](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/__support/printf_core/CMakeLists.txt)、[src/stdio/CMakeLists.txt](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/stdio/CMakeLists.txt) | 用户配置开关 → 编译宏 → 是否编入模块化 `.cpp` 的传递链 |

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：**死代码问题 → `modular_format` 属性 → 弱符号机制 → 配置开关**。四者环环相扣，建议按顺序读。

### 4.1 死代码问题：格式串让编译器「看不见」未用的代码

#### 4.1.1 概念说明

`printf`/`scanf` 的设计哲学是：用一条格式串（如 `"%d %f %s"`）驱动一整套功能。对**调用方**而言这是好事——格式串很紧凑，等价于很多次单独调用，而函数定义只有一份。

但对**实现方**而言，这带来一个隐蔽的体积问题：格式串里某些功能（尤其是**浮点转换**和 **errno 转换**）的实现可能包含很大的数据表（如浮点十进制快速查表）。当一个程序里所有的 `printf` 调用都只用 `%d`、`%s` 这类整数/字符串转换时，这些浮点表就是**完全用不到的死代码**。

关键难点在于：这种死代码**对编译器不可见**。因为格式串是在**运行期**被 `printf_core` 的 Parser 解析的，编译器在编译调用点时，看到的是一次普通的 `printf(...)` 函数调用，它无法断言「这个程序永远不可能走到 `%f` 分支」，于是只能把整个 `printf` 实现（连同浮点表）都链接进二进制。

#### 4.1.2 核心流程

把问题画成对比：

```text
理想世界：  程序只用 %d  →  只链接 %d 的实现     →  二进制小
现实世界：  程序只用 %d  →  编译器看不到 %f 用不到 → 全部实现链接进来 → 二进制大
```

文档把这一痛点说得很直白：浮点与 errno 转换「可能涉及巨大的表，且可能是完全死的（wholly dead）。但由于格式串结构，这段代码以一种此前对编译器不可见的方式死去」。见 [docs/dev/modular_format.md:L14-L19](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/docs/dev/modular_format.md#L14-L19)。

要破这个局，必须让「这段代码是否需要」的判定**从运行期上移到编译期**，并让编译器能据此控制链接行为。这正是 `modular_format` 属性要解决的。

#### 4.1.3 源码精读

`printf_core/converter.h` 里用 `LIBC_PRINTF_MODULE` 包裹的 `convert_float` 就是典型的「可能完全死掉」的大块实现——它按 `%f`/`%e`/`%a`/`%g` 分派到四套不同的浮点十进制/十六进制转换算法，每一套都依赖庞大的浮点转字符串表：

[converter.h:L31-L52](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/__support/printf_core/converter.h#L31-L52) —— 用宏把浮点转换写成可模块化的函数，宏在不同模式下展开成完全不同的东西（4.3 节详解）。

注意它被 `#ifndef LIBC_COPT_PRINTF_DISABLE_FLOAT` 包裹——这是「彻底不要浮点」的更粗暴开关；而本讲讲的「按需链接浮点」是更精细的方案，二者正交。

#### 4.1.4 代码实践

1. **实践目标**：建立「浮点转换是一坨可能全死的代码」的直觉。
2. **操作步骤**：在仓库里搜索浮点转换背后的查表实现，定位它依赖的常量表规模。
3. **需要观察的现象**：你会发现浮点十进制转换依赖大整数运算与查表（如 `src/__support/number_writer.h`、`FPUtil` 下的高精度工具），代码量远超 `%d` 的整数转换。
4. **预期结果**：你能用一句话说明「为什么把浮点表无条件链进只打印整数的程序是浪费」。
5. 本步骤为源码阅读型实践，**待本地验证**具体表的大小（不同构建配置下表会被宏裁剪）。

#### 4.1.5 小练习与答案

**练习 1**：为什么传统 `printf` 实现里，即使程序从不打印浮点，浮点表仍会被链接进来？

> **答案**：因为格式串在运行期才被解析，编译器在编译调用点时无法证明「不会用到 `%f`」，只能保守地把整个 `printf` 实现都链入。静态库链接只会剔除「完全没人引用的目标文件」，而浮点表与 `%d` 实现同处一个被引用的函数体内，无法单独剔除。

**练习 2**：「死代码」与「未引用代码」有什么区别？本讲关心的是哪一种？

> **答案**：「未引用代码」是编译器/链接器能直接识别的（没人 call 它）；「死代码」是**逻辑上**永远执行不到、但**语法上**仍被引用的代码（如被同一个函数里的 `switch` 分支覆盖）。本讲关心的是后者——它对工具链不可见，必须借助额外机制才能剔除。

---

### 4.2 modular_format 属性：让编译器改写调用点

#### 4.2.1 概念说明

为了解决 4.1 的痛点，clang 引入了一个新属性 `modular_format(<impl_fn>, <impl_name>, <aspects>...)`。它是已有 `format`（即 `format(printf,...)`）属性的**扩展**：`format` 只负责格式串的编译期语法检查，`modular_format` 在此基础上多了「按需重定向调用 + 按需发出重定位」的能力。

三个参数的含义：

| 参数 | 含义 | asprintf 的取值 |
|------|------|-----------------|
| `impl_fn` | 一个符号，命名「模块化版」的实现函数 | `__asprintf_modular` |
| `impl_name` | 实现的通用名字字符串，用于拼接 aspect 符号 | `"__printf"` |
| `aspects...` | 本实现能处理的 aspect 列表 | `"float"` |

#### 4.2.2 核心流程

当编译器在调用点（例如 `asprintf(ptr, "%d", 5)`）看到带 `modular_format` 属性的函数时，它可以做两件事：

1. **改写调用**：分析**字面量格式串** `"%d"`，算出这次调用实际需要的 aspect 集合。`%d` 不涉及浮点，所以 aspect 集合是空集。于是把这次调用从 `asprintf` **重定向到模块化实现** `__asprintf_modular`。
2. **按需发出重定位**：重定向后，编译器只为「实际需要的 aspect」发出重定位，重定位目标是形如 `<impl_name>_<aspect>` 的符号。`%d` 不需要 float，于是**不发出** `__printf_float` 的重定位。

反过来，如果调用是 `asprintf(ptr, "%f", 3.14)`，编译器算出需要 float aspect，就会发出对 `__printf_float` 的重定位，从而把浮点实现拉进链接。

> 文档原文：当编译器发现某次调用只需要实现的一个固定 aspect 子集时，它「可以把调用重定向到实现函数，并发出一串指向 `<impl_name>_<aspect>` 符号的重定位；这些重定位再把所需 aspect 拉进链接」。见 [modular_format.md:L27-L32](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/docs/dev/modular_format.md#L27-L32)。

注意还有一个「默认入口兜底」语义：当编译器**无法**分析格式串（如格式串不是字面量，或 aspect 集合无法判定）时，调用仍走默认入口（如 `asprintf`）。默认入口也会调用模块化实现，但**无条件地发出所有 aspect 的空重定位**——即把全部 aspect 都拉进来。这与「能分析就只拉需要的」形成对照，4.3 节会用源码印证。

#### 4.2.3 源码精读

属性本身被封装成一个公共宏，避免源码里直接写编译器专属语法：

[_LIBC_MODULAR_FORMAT_PRINTF.h:L12-L16](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/include/llvm-libc-macros/_LIBC_MODULAR_FORMAT_PRINTF.h#L12-L16) —— 把 `format` 与 `modular_format` 两个属性组合，impl_name 固定为 `"__printf"`，aspect 固定为 `"float"`。

```c
#define _LIBC_MODULAR_FORMAT_PRINTF(MODULAR_IMPL_FN, FORMAT_IDX,               \
                                    FIRST_TO_CHECK)                            \
  __attribute__((format(printf, FORMAT_IDX, FIRST_TO_CHECK),                   \
                 modular_format(MODULAR_IMPL_FN, "__printf", "float")))
```

这个宏挂在 YAML 规范里的函数声明上，由 hdrgen 渲染进公共头。以 `asprintf` 为例：

[stdio.yaml:L52-L54](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/include/stdio.yaml#L52-L54) —— `asprintf` 声明带 `_LIBC_MODULAR_FORMAT_PRINTF(__asprintf_modular, 2, 3)`，把模块化实现指向 `__asprintf_modular`。`printf`/`sprintf`/`snprintf`/`vasprintf` 等一族函数都各自挂上对应的 `__*_modular` 符号（见 [stdio.yaml:L305-L307](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/include/stdio.yaml#L305-L307) 等）。

而 `__asprintf_modular` 的实现确实存在，它和默认 `asprintf` 共享同一个 `asprintf.h`：

[asprintf.h:L16-L17](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/stdio/asprintf.h#L16-L17) —— 同时声明 `asprintf` 与 `__asprintf_modular` 两个入口点。

#### 4.2.4 代码实践

1. **实践目标**：在源码里找出「每个默认入口都配一个 `_modular` 兄弟」的成对结构。
2. **操作步骤**：在 `include/stdio.yaml` 里搜索所有 `_LIBC_MODULAR_FORMAT_PRINTF` 出现处，记录每个函数（如 `asprintf`）与它指向的 `__*_modular` 符号；再到 `src/stdio/` 下确认存在对应的 `<func>.cpp` 与 `<func>_modular.cpp`。
3. **需要观察的现象**：默认 `<func>.cpp` 与 `<func>_modular.cpp` 主体几乎相同（都调 `vasprintf_internal`），唯一差别在 `.reloc` 那一行（见 4.3）。
4. **预期结果**：列出一个对照表，证明「属性指定的模块化实现符号」与「实际存在的 `_modular.cpp`」一一对应。
5. 本步骤为源码阅读型实践。

#### 4.2.5 小练习与答案

**练习 1**：`modular_format(MODULAR_IMPL_FN, "__printf", "float")` 里，如果一次调用是 `printf("%f", x)`，编译器会发出对哪个符号的重定位？

> **答案**：会发出对 `__printf_float` 的重定位。符号名由 `<impl_name>_<aspect>` 拼接而来，即 `__printf` + `_` + `float`。这个符号的定义在 4.3 的 `float_impl.cpp` 里。

**练习 2**：为什么 `modular_format` 必须和 `format` 属性一起出现？

> **答案**：`modular_format` 的「分析格式串、算出 aspect 集合」能力建立在 `format` 属性的格式串语义之上；`format` 提供格式串的索引（第几个参数是格式串）与语法检查，`modular_format` 在此基础上扩展出按需链接能力。文档明确指出 `format` 属性「必须也存在（即使是隐式的）」。

---

### 4.3 弱符号机制：LIBC_PRINTF_MODULE 与 LIBC_PRINTF_DEFINE_MODULES

#### 4.3.1 概念说明

4.2 解决了「编译器在调用点发出按需重定向」的问题，但还差一块拼图：**被按需引用的浮点实现，要怎样组织成「不引用就不进链接、一引用才进链接」的形态？**

答案是用**弱符号 + 同一头文件二次包含**。核心是一对宏：`LIBC_PRINTF_MODULE` 负责把一个函数写成「可模块化」的形态；`LIBC_PRINTF_DEFINE_MODULES` 是一个开关，让同一份头文件在「模块化实现文件」里被二次包含时，展开出真正的函数定义。

#### 4.3.2 核心流程

`LIBC_PRINTF_MODULE((签名), { 函数体 })` 在三种配置下展开成三种东西：

| 配置条件 | 展开结果 | 语义 |
|----------|----------|------|
| 未定义 `LIBC_COPT_PRINTF_MODULAR`（默认非模块化构建） | `签名 { 函数体 }`（内联） | 普通的 `LIBC_INLINE` 定义，浮点代码直接内联进调用方，无模块化 |
| 定义了 `LIBC_COPT_PRINTF_MODULAR`，未定义 `LIBC_PRINTF_DEFINE_MODULES`（普通消费方，如 `converter.h` 被默认包含时） | `签名 __attribute__((weak));` | 一个**弱声明**，对它的引用都变成弱引用——不强制拉入实现 |
| 同时定义了两者（在 `float_impl.cpp` 里二次包含 `converter.h`） | `签名 { 函数体 }` | 真正的**定义**，且只有当对应的 aspect 符号被引用时，这个 TU 才会被链接进来 |

关键在于：`converter.h` 这一份头文件被**包含两次**——

- 第一次（默认包含）：展开出弱声明，让 converter 主体的 `convert_float` 调用变成弱引用。
- 第二次（在 `float_impl.cpp` 里 `#define LIBC_PRINTF_DEFINE_MODULES` 后再 `#include "converter.h"`）：展开出真正的定义，并把这个 TU 与 aspect 符号 `__printf_float` 绑在一起。

#### 4.3.3 源码精读

宏的全部逻辑就这几行，是本讲最核心的代码：

[printf_config.h:L56-L74](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/__support/printf_core/printf_config.h#L56-L74) —— 注意三态分支：`LIBC_PRINTF_MODULE_DECL` 在模块化时是 `weak`、否则是 `LIBC_INLINE`；`LIBC_PRINTF_MODULE` 在「非模块化」或「定义了 DEFINE_MODULES」时展开出带函数体的定义，否则只展开出弱声明。

```c
#ifdef LIBC_COPT_PRINTF_MODULAR
#define LIBC_PRINTF_MODULE_DECL __attribute__((weak))
#else
#define LIBC_PRINTF_MODULE_DECL LIBC_INLINE
#endif

// 满足下列任一条件 → 展开"带函数体的定义"：
//   (1) 完全没开模块化；或
//   (2) 开了模块化 且 定义了 LIBC_PRINTF_DEFINE_MODULES（即二次包含场景）
// 否则（开了模块化 但 没定义 DEFINE_MODULES）→ 只展开"弱声明"
#if !defined(LIBC_COPT_PRINTF_MODULAR) || defined(LIBC_PRINTF_DEFINE_MODULES)
#define LIBC_PRINTF_MODULE(SIG, ...) LIBC_PRINTF_MODULE_UNWRAP SIG __VA_ARGS__
#else
#define LIBC_PRINTF_MODULE(SIG, ...)                                           \
  LIBC_PRINTF_MODULE_UNWRAP SIG LIBC_PRINTF_MODULE_DECL;
#endif
```

`converter.h` 用这个宏把 `convert_float` 包起来。默认（非模块化）包含下，它就是一段普通的内联模板函数：

[converter.h:L32-L51](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/__support/printf_core/converter.h#L32-L51) —— 浮点转换的真正算法（按 `%f/%e/%a/%g` 分派）就写在这个宏里。

模板有个特殊难题：`convert_float` 是模板（按 `WriteMode` 参数化），模块化构建下它必须为**所有可能用到的实参**显式实例化，否则弱声明永远解析不到定义。于是 `converter.h` 在「二次包含」时用 `.def` 文件枚举所有 `WriteMode` 并显式实例化：

[converter.h:L54-L60](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/__support/printf_core/converter.h#L54-L60) —— 这段只在定义了 `LIBC_PRINTF_DEFINE_MODULES` 时生效，为每个 `WriteMode` 生成一个 `convert_float<...>` 的显式实例化声明。

而「二次包含 + 显式实例化」的发生地，正是 `float_impl.cpp`，它同时定义了 aspect 符号 `__printf_float`：

[float_impl.cpp:L16-L24](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/__support/printf_core/float_impl.cpp#L16-L24) —— 整个 TU 被 `#ifdef LIBC_COPT_PRINTF_MODULAR` 包住；先 `#define LIBC_PRINTF_DEFINE_MODULES` 再 `#include "converter.h"` 触发浮点模块的真正定义与实例化；最后定义空函数 `__printf_float()` 作为 aspect 符号。注释一语道破：**「Bring this file into the link if `__printf_float` is referenced.」**

```c
#ifdef LIBC_COPT_PRINTF_MODULAR
#define LIBC_PRINTF_DEFINE_MODULES
#include "src/__support/printf_core/converter.h"   // 二次包含 → 真正定义
extern "C" void __printf_float() {}                // aspect 符号：被引用才链入本 TU
#endif
```

> 这就是 aspect 符号的作用：它是一个**链接开关**。`__printf_float` 与浮点实现绑在同一个 TU 里，链接器只有看到对 `__printf_float` 的引用，才会把这个 TU（连同浮点表）抽出来；否则整个 TU 被忽略。

现在回头印证 4.2 提到的「默认入口兜底拉入全部 aspect」。看默认入口 `asprintf.cpp`：

[asprintf.cpp:L29-L34](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/stdio/asprintf.cpp#L29-L34) —— 模块化构建下，默认 `asprintf` 会插入一行内联汇编 `.reloc ., BFD_RELOC_NONE, __printf_float`，发出一个指向 `__printf_float` 的**空重定位**（不改字节，只造引用），从而**无条件**把浮点拉进来。

```c
#ifdef LIBC_COPT_PRINTF_MODULAR
  LIBC_INLINE_ASM(".reloc ., BFD_RELOC_NONE, __printf_float");
  auto ret_val = printf_core::vasprintf_internal<true>(buffer, format, args);
#else
  auto ret_val = printf_core::vasprintf_internal(buffer, format, args);
#endif
```

而模块化入口 `__asprintf_modular` **没有**这行 `.reloc`：

[asprintf_modular.cpp:L29](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/stdio/asprintf_modular.cpp#L29) —— 直接调 `vasprintf_internal<true>`，是否拉入浮点完全交给编译器在调用点发出的重定位决定。

`printf_main.h` 把这种「默认路径强制拉全 aspect」的封装提到引擎层，这样所有 printf 家族都能复用：

[printf_main.h:L45-L53](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/__support/printf_core/printf_main.h#L45-L53) —— `printf_main`（默认）在调 `printf_main_modular` 前先发出 `.reloc ... __printf_float`；`printf_main_modular` 才是真正的 parser→convert 引擎。`vasprintf_internal<use_modular>` 的模板参数 `use_modular` 正是在 `printf_main` 与 `printf_main_modular` 之间二选一，见 [vasprintf_internal.h:L47-L61](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/__support/printf_core/vasprintf_internal.h#L47-L61)。

#### 4.3.4 代码实践

1. **实践目标**：亲手验证「同一份 `converter.h` 在两种包含方式下展开出不同东西」。
2. **操作步骤**：
   - 先读 `converter.h` 顶部 `LIBC_PRINTF_MODULE((...), {...})` 的写法。
   - 再读 `float_impl.cpp`：注意它在 `#include "converter.h"` **之前**先 `#define LIBC_PRINTF_DEFINE_MODULES`。
   - 对照 `printf_config.h` 的三态宏表，在脑中（或在纸上）分别画出「converter.h 被 `asprintf.cpp` 包含」与「被 `float_impl.cpp` 包含」两种情况下 `convert_float` 的展开形态。
3. **需要观察的现象**：前者展开成 `weak` 声明，后者展开成带函数体的定义 + 一组显式模板实例化。
4. **预期结果**：你能解释为什么必须把定义放进一个**独立的 TU**（`float_impl.cpp`）而不是放在被多处包含的头里——因为只有这样，链接器才能以 TU 为粒度决定「链不链」。
5. 本步骤为源码阅读型实践，**待本地验证**（可用 `clang -E` 预处理两处包含对比展开结果）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `__printf_float()` 是一个**空函数体**？它什么都不做，为何有用？

> **答案**：它的作用不是「执行某段代码」，而是**作为一个符号存在于某个 TU 里**。链接器以「是否有对 `__printf_float` 的引用」来判断是否把 `float_impl.cpp` 这个 TU 抽进链接；一旦抽进来，与它同 TU 的浮点 `convert_float` 定义也就一起进入了。函数体空，是因为它的语义是「链接锚点」而非「运行逻辑」。

**练习 2**：模块化构建下，converter 主体里对 `convert_float` 的调用为什么不会因为「找不到定义」而链接失败？

> **答案**：因为该调用是对一个**弱符号**的引用（默认包含下 `LIBC_PRINTF_MODULE` 展开成 `weak` 声明）。弱引用在没有强定义时不会导致链接错误；只有当某处发出对 `__printf_float` 的强引用（默认入口的 `.reloc`，或编译器为 `%f` 调用发出的重定位）时，`float_impl.cpp` 才被拉入，`convert_float` 的定义才出现并满足该弱引用。

---

### 4.4 配置开关：从 LIBC_CONF_PRINTF_MODULAR 到 LIBC_COPT_PRINTF_MODULAR

#### 4.4.1 概念说明

整条机制默认是**关闭**的——因为「让编译器改写调用点」依赖较新的 clang 对 `modular_format` 属性的支持，且会改变链接行为。用户需要显式开启。开关经过两层命名：

- 用户层 `LIBC_CONF_PRINTF_MODULAR`（`CONF`，写在 `config.json` 里）。
- 编译层 `LIBC_COPT_PRINTF_MODULAR`（`COPT`，作为编译宏传给 C++ 源码）。

这两层是 u2-l4 讲过的「`config.json` 选项旋钮」机制的具体实例：`CONF_*` 是对外的配置旋钮，`COPT_*` 是它转化出的内部编译宏。

#### 4.4.2 核心流程

```text
config/config.json: LIBC_CONF_PRINTF_MODULAR = true
        │
        ▼  (CMake 读 config.json，按 u2-l4 的三层覆盖解析)
printf_core/CMakeLists.txt: libc_add_definition(... "LIBC_COPT_PRINTF_MODULAR")
        │
        ▼  (作为 -DLIBC_COPT_PRINTF_MODULAR 传给编译器)
C++ 源码: #ifdef LIBC_COPT_PRINTF_MODULAR  →  启用弱符号/二次包含/_modular.cpp
        │
        ▼
stdio/CMakeLists.txt: list(APPEND <func>_srcs <func>_modular.cpp)  →  把模块化入口编进产物
```

#### 4.4.3 源码精读

开关的「事实来源」是全局配置文件，默认值为 `false`：

[config.json:L63-L66](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/config/config.json#L63-L66) —— 文档说明：「Split printf implementation into modules that can be lazily linked in.」（把 printf 实现拆成可惰性链接的模块）。

`CONF` 到 `COPT` 的翻译发生在 `printf_core` 的 CMake 里：

[printf_core/CMakeLists.txt:L36-L41](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/__support/printf_core/CMakeLists.txt#L36-L41) —— 当 `LIBC_CONF_PRINTF_MODULAR` 为真，调用 `libc_add_definition` 给 `printf_config_copts` 加上 `LIBC_COPT_PRINTF_MODULAR`，最终作为编译选项传播给所有依赖 `printf_core` 的目标。

同一个 `printf_core/CMakeLists.txt` 还把 `float_impl.cpp` 注册进 `printf_main` 这个内部 object library，确保 aspect 符号与浮点实现以一个独立 TU 的形式存在：

[printf_core/CMakeLists.txt:L161-L174](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/__support/printf_core/CMakeLists.txt#L161-L174) —— `float_impl.cpp` 是 `printf_main` 这个 object library 的唯一 SRCS（注意它内部用 `#ifdef LIBC_COPT_PRINTF_MODULAR` 自我空化，非模块化构建时这个 TU 实际为空）。

最后，开启模块化后，每个 printf 家族入口点要**额外**编入对应的 `_modular.cpp`：

[stdio/CMakeLists.txt:L159-L166](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/stdio/CMakeLists.txt#L159-L166) —— `asprintf` 入口的 SRCS 在 `LIBC_CONF_PRINTF_MODULAR` 为真时追加 `asprintf_modular.cpp`。`sprintf`/`snprintf`/`vsprintf`/`vsnprintf`/`vasprintf` 都遵循同一模式。

#### 4.4.4 代码实践

1. **实践目标**：把「一个配置旋钮如何同时影响 C++ 宏与 CMake 源文件列表」看清。
2. **操作步骤**：
   - 在 `config/config.json` 找到 `LIBC_CONF_PRINTF_MODULAR`，记下默认值。
   - 在 `src/__support/printf_core/CMakeLists.txt` 找到它如何变成 `LIBC_COPT_PRINTF_MODULAR`。
   - 在 `src/stdio/CMakeLists.txt` 统计有多少个入口点会因为该开关而追加 `_modular.cpp`。
   - 全局搜索 `#ifdef LIBC_COPT_PRINTF_MODULAR`，看看 C++ 侧有多少处行为分支。
3. **需要观察的现象**：`CONF_*` 只出现在 `config.json` 与 CMake 里；C++ 源码一律用 `COPT_*`。二者通过 `libc_add_definition` 桥接。
4. **预期结果**：画出从 `config.json` 到「产物里多了 `__asprintf_modular` 符号」的完整传递链。
5. 本步骤为源码阅读型实践；如要真机验证，可在 CMake 配置时加 `-DLIBC_CONF_PRINTF_MODULAR=ON`（需较新 clang 支持 `modular_format` 属性，**待本地验证**编译器是否支持）。

#### 4.4.5 小练习与答案

**练习 1**：为什么要把配置拆成 `LIBC_CONF_PRINTF_MODULAR` 和 `LIBC_COPT_PRINTF_MODULAR` 两个名字，而不是只用一个？

> **答案**：这是 u2-l4 讲过的命名约定——`CONF_*` 是面向用户的配置旋钮（写在 `config.json`，经三层覆盖解析），`COPT_*` 是它转化出的内部编译宏（直接进 C++ 的 `#ifdef`）。分开命名让「用户可调的稳定接口」与「内部实现细节」解耦：将来内部改用别的宏名，`CONF_*` 可以不动。

**练习 2**：如果编译器的 clang 版本不支持 `modular_format` 属性，开启这个开关会发生什么？

> **答案**：`_LIBC_MODULAR_FORMAT_PRINTF` 宏会展开成一个编译器不认识的 `__attribute__`，通常导致编译警告（未知属性被忽略）甚至报错；同时 `_modular.cpp` 会被编入但属性不生效，调用点不会被重定向。文档明确这是个「currently gated」的实验特性，需配套的 clang 支持，**待本地验证**具体行为。

---

## 5. 综合实践

把四个模块串起来，回答本讲的核心问题（对应讲义规格里的实践任务）：

> **题目**：用自己的话解释——「当调用者只用 `%d` 时，编译器如何避免把浮点转换表链接进二进制」？并指出 aspect 符号 `__printf_float` 在其中的作用。

请按下面五步完成，每步都给出源码依据：

1. **起点：调用点带属性**。用户写 `printf("%d", 5)`。`printf` 的公共声明（来自 `stdio.yaml`）带 `_LIBC_MODULAR_FORMAT_PRINTF(__printf_modular, 1, 2)`，即 `modular_format` 属性。依据：[stdio.yaml:L305-L307](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/include/stdio.yaml#L305-L307)。

2. **编译器分析格式串**。编译器看到字面量 `"%d"`，算出本次调用只需整数 aspect、**不需要 float aspect**，于是把调用**重定向**到 `__printf_modular`，且**不发出**对 `__printf_float` 的重定位。依据：[modular_format.md:L27-L32](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/docs/dev/modular_format.md#L27-L32)。

3. **默认入口没有被引用**。因为调用被重定向到了 `__printf_modular`，默认入口 `printf`（含 baremetal 的 `printf.cpp`）的那行 `.reloc ., BFD_RELOC_NONE, __printf_float` 所在的 TU 没有任何强引用。依据：[baremetal/printf.cpp:L29-L34](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/stdio/baremetal/printf.cpp#L29-L34)。由于静态库的惰性链接，这个 TU **不会被抽进最终二进制**，于是那行强制拉入浮点的空重定位**不生效**。

4. **aspect 符号无人引用 → 浮点 TU 被剔除**。没有任何地方引用 `__printf_float`，所以 `float_impl.cpp`（与 `__printf_float` 绑定的 TU）也不被链接器抽出。依据：[float_impl.cpp:L16-L24](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/__support/printf_core/float_impl.cpp#L16-L24)。整个浮点 `convert_float` 实现及其背后的浮点表随之缺席。

5. **对照：若调用是 `printf("%f", x)`**。编译器算出需要 float aspect，发出对 `__printf_float` 的重定位 → `float_impl.cpp` 被抽进链接 → 弱引用 `convert_float` 解析到定义 → 浮点表进入二进制。

**aspect 符号 `__printf_float` 的作用总结**：它是一个**链接开关 / 锚点符号**。它本身是个空函数，但与浮点实现同处一个 TU。编译器在调用点发出的「按需重定位」、以及默认入口发出的「强制空重定位」，最终都汇拢到「是否产生对 `__printf_float` 的引用」这一个判定上；链接器据此决定浮点实现进不进二进制。这就是把「格式串死代码」从运行期不可见，变为编译期可分析、链接期可剔除的完整闭环。

> 进阶思考（可选）：如果格式串不是字面量（如 `printf(fmt, ...)`，`fmt` 是运行期变量），编译器无法分析 aspect 集合，会怎么处理？请结合 4.2 的「默认入口兜底」语义预测结果，并到 `asprintf.cpp` 的 `.reloc` 那一行验证你的预测。

## 6. 本讲小结

- `printf`/`scanf` 的格式串驱动设计，会让浮点转换表、errno 转换这类大块代码成为**对编译器不可见的死代码**，无条件链入只打印整数的程序。
- clang `modular_format(impl_fn, impl_name, aspects...)` 属性在 `format` 属性之上扩展，让编译器在**编译期**分析字面量格式串，按需把调用重定向到模块化实现，并只发出实际所需 aspect 的重定位。
- aspect 符号（如 `__printf_float`）是**链接锚点**：它是个空函数，但与浮点实现同处一个 TU；链接器以「是否有对它的引用」决定浮点实现进不进二进制。
- `LIBC_PRINTF_MODULE` 宏三态展开（内联定义 / 弱声明 / 真定义）配合 `LIBC_PRINTF_DEFINE_MODULES` 的「同一头文件二次包含」，把 `convert_float` 切成弱引用与独立 TU 定义两种形态，是弱符号机制的精巧实现。
- 默认入口（如 `asprintf`）用 `.reloc ., BFD_RELOC_NONE, __printf_float` 发出**空重定位**，在编译器无法分析格式串时兜底拉入全部 aspect；模块化入口（如 `__asprintf_modular`）则无此行，完全交给编译器按需决定。
- 整套机制由用户配置 `LIBC_CONF_PRINTF_MODULAR`（`config.json`）开启，经 CMake 的 `libc_add_definition` 翻译成编译宏 `LIBC_COPT_PRINTF_MODULAR`，同时让各入口点追加编入 `_modular.cpp`。

## 7. 下一步学习建议

- **横向对比 scanf**：`scanf` 有一套平行的 `scanf_core`，目前尚未像 printf 这样全面模块化。可阅读 `src/__support/scanf_core/` 思考「若要给 scanf 做同样的模块化，aspect 该怎么划分」。
- **深入浮点转换**：本讲把浮点转换当成「一坨大代码」黑盒处理；若想看清它为何那么大，可进入 `convert_float_decimal` 等的实现，并结合 u6-l2（FPUtil）与 u10-l2（MPFR 正确性验证）理解其精度保证。
- **理解链接语义**：本讲重度依赖「静态库惰性链接」与「弱符号」两个链接器概念。建议结合 u1-l3（构建与链接）与 u1-l4（Overlay/Full 模式中的链接顺序）巩固对 libc 归档链接行为的直觉。
- **尝试一次实验性构建**：在新版 clang 下用 `-DLIBC_CONF_PRINTF_MODULAR=ON` 构建，对比「只用 `%d` 的小程序」在开关开关下的二进制体积差异（**待本地验证**），直观感受模块化的收益。
