# printf_core 架构：解析器、写入器与转换器

## 1. 本讲目标

学完本讲后，你应当能够：

- 说清楚 LLVM-libc 为什么把 `printf` 的实现拆成「解析器（parser）— 写入器（writer）— 转换器（converter）」三段式，以及这种拆分带来了什么好处。
- 认识 `printf_core` 的核心数据结构：`FormatSection`（一段格式说明的内部表示）、`LengthModifier`（长度修饰符）、`TypeDesc`（类型描述），以及一组负数错误码。
- 读懂 `Parser<ArgProvider>` 如何把一段形如 `%ld` 的格式串解析成一个填充好的 `FormatSection`，并从可变参数里取出正确类型的值。
- 读懂 `Writer<write_mode>` 如何用一层缓冲 + 三种「溢出策略」同时服务 `fprintf`（冲刷到流）、`snprintf`（丢弃溢出）、`asprintf`（动态扩容）三个不同的对外接口。
- 看清 `printf_main` 这条主循环如何把三段串起来，并理解 `printf` 与 `scanf` 是「复用同一套架构设计与同一套 `ArgList` 抽象」，而不是共用同一份源码。

## 2. 前置知识

在进入源码之前，先用三段话建立直觉。

**第一，`printf` 的格式串本质上是一串「交替的普通文本与转换说明」。** 例如 `"name=%s, age=%d\n"`，其中 `name=`、`, age=`、`\n` 是原样输出的普通文本，`%s` 和 `%d` 是转换说明（conversion specification）。每个转换说明的语法是：

```
% [索引] 标志 宽度 .精度 长度修饰符 转换名
```

例如 `%-08.3ld` 依次是：标志 `-0`（左对齐、补零）、宽度 `8`、精度 `.3`、长度修饰符 `l`、转换名 `d`。`printf` 的工作就是「把格式串切成一段段」「对每个转换说明从可变参数里取值并格式化」「把结果写到目标（屏幕 / 字符串缓冲 / 文件）」。

**第二，可变参数（`...` 与 `va_list`）是「只能顺序读取、读时必须告知类型」的流。** 你无法回头、也无法在不知道类型的情况下跳过一个参数。这给 `%1$d` 这种「按索引取参数」（POSIX 扩展，称为 index mode）带来了麻烦——后面会看到 `Parser` 是如何用一张「类型描述表」绕开这个限制的。

**第三，承接 u4-l1：`src/__support` 是所有入口点共享的「私有标准库」。** `printf_core` 就住在 `src/__support/printf_core/` 下，它不对应任何公共头文件、不直接产生公开 C 符号，而是以 `add_header_library` / `add_object_library` 声明为内部构建目标，被 `src/stdio/printf.cpp`、`fprintf.cpp`、`sprintf.cpp`、`snprintf.cpp`、`asprintf.cpp`、`vfprintf.cpp` 等入口点经 CMake 的 `DEPENDS` 引用。换句话说，入口点只是「壳」，真正的 `printf` 引擎在 `printf_core` 里。本讲要读的就是这台引擎。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/__support/printf_core/core_structs.h](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/__support/printf_core/core_structs.h) | 核心数据结构：`LengthModifier`、`FormatSection`、`TypeDesc`、错误码。 |
| [src/__support/printf_core/parser.h](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/__support/printf_core/parser.h) | 解析器 `Parser<ArgProvider>`：把格式串逐段解析成 `FormatSection`，并按类型从可变参数取值。 |
| [src/__support/printf_core/writer.h](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/__support/printf_core/writer.h) | 写入器 `Writer<write_mode>` 与三种 `WriteBuffer`：统一缓冲，三种溢出策略。 |
| [src/__support/printf_core/converter.h](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/__support/printf_core/converter.h) | 转换器分派函数 `convert()`：按转换名分派到 `convert_int` / `convert_float` / `convert_string` 等。 |
| [src/__support/printf_core/printf_main.h](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/__support/printf_core/printf_main.h) | 三段式编排：主循环 `get_next_section → convert → write`。 |
| [src/__support/arg_list.h](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/__support/arg_list.h) | `ArgProvider` 抽象（`ArgList` / `MockArgList` / `DummyArgList`），是 `Parser` 模板参数，**`printf` 与 `scanf` 共享**。 |
| [src/__support/printf_core/vfprintf_internal.h](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/__support/printf_core/vfprintf_internal.h) | 示例：`vfprintf` 入口如何把 `FILE*` 接成 `FlushingBuffer` 再驱动 `printf_main`。 |
| [src/stdio/scanf_core/scanf_main.h](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/stdio/scanf_core/scanf_main.h) | 对照：`scanf` 的平行核心，同样三段式但用 `Reader` 取代 `Writer`。 |

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：**核心数据结构 → 解析器 → 写入器 → 转换器与三段式编排**。前三块是零件，第四块把它们装成一台完整引擎，并顺带说清 `printf` 与 `scanf` 的复用关系。

### 4.1 核心数据结构：把一段格式说明装进一个结构体

#### 4.1.1 概念说明

`printf` 解析格式串时，每读到一段转换说明（如 `%08.3ld`），需要把它的「标志 / 宽度 / 精度 / 长度修饰符 / 转换名 / 取到的参数值」一次性记下来，交给下游的转换器使用。如果每次都让转换器自己回头去解析字符串，逻辑会又乱又重复。

于是 `core_structs.h` 定义了一个「装下一段格式说明全部信息」的结构体 `FormatSection`，以及配套的 `LengthModifier`（长度修饰符枚举）、`FormatFlags`（标志位）、`TypeDesc`（类型描述）。整套 `printf_core` 的数据流都可以概括成一句话：**`Parser` 生产 `FormatSection`，`Converter` 消费 `FormatSection`**。

#### 4.1.2 核心流程

一段格式串被切成若干 `FormatSection`，分两类：

- **原始段（raw section）**：不含 `%` 的普通文本，`has_conv == false`，只有 `raw_string` 有意义，由 `Writer` 原样输出。
- **转换段（conversion section）**：以 `%` 开头的转换说明，`has_conv == true`，所有字段都有意义，交给 `Converter` 处理。

一个转换段 `FormatSection` 的字段语义如下：

| 字段 | 含义 | 例：`%-08.3ld` |
| --- | --- | --- |
| `has_conv` | 是否为转换段 | `true` |
| `raw_string` | 该段在原格式串里的原文 | `"%-08.3ld"` |
| `flags` | 标志位（`FormatFlags`） | `LEFT_JUSTIFIED \| LEADING_ZEROES` |
| `min_width` | 最小宽度 | `8` |
| `precision` | 精度（`-1` 表示未指定） | `3` |
| `length_modifier` | 长度修饰符 | `LengthModifier::l` |
| `conv_name` | 转换名（单个字符） | `'d'` |
| `conv_val_raw` | 取到的「数值型」参数原始位（`UInt128`） | 该 `long` 的位模式 |
| `conv_val_ptr` | 取到的「指针型」参数（`%p`/`%s`/`%n`） | 指针 |

#### 4.1.3 源码精读

长度修饰符枚举——注意源码注释特意说明：这些枚举值的名字刻意与格式串里的写法一致（`hh`、`h`、`l`、`ll`…），所以命名风格和文件其余部分不同：

[core_structs.h:27-44](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/__support/printf_core/core_structs.h#L27-L44) 定义了 `LengthModifier`，其中 `Q`（float128）、`w`/`wf`（C23 位整数 BitInt）由条件编译宏控制，`none` 表示未指定长度修饰符。配套的 `LengthSpec` 在 `lm` 之外多带一个 `bit_width`，专门给 `w32` / `wf64` 这类带位宽的修饰符用：

[core_structs.h:46-49](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/__support/printf_core/core_structs.h#L46-L49)

标志位用 `uint8_t` 位掩码表示，可叠加：

[core_structs.h:57-67](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/__support/printf_core/core_structs.h#L57-L67) —— `-`→`LEFT_JUSTIFIED`、`+`→`FORCE_SIGN`、空格→`SPACE_PREFIX`、`#`→`ALTERNATE_FORM`、`0`→`LEADING_ZEROES`。

`FormatSection` 本体（含一个仅用于测试、release 会被优化掉的 `operator==`）：

[core_structs.h:69-111](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/__support/printf_core/core_structs.h#L69-L111) 值得注意两点：① 参数值统一存进「最大的容器」——整数与浮点都先转成位模式塞进 `conv_val_raw`（`UInt128`，见 [core_structs.h:55](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/__support/printf_core/core_structs.h#L55)），指针塞进 `conv_val_ptr`；② `operator==` 按 `conv_name` 区分到底比指针还是比较数值位。

`TypeDesc` 与 `type_desc_from_type<T>()` 是给 index mode（`%1$d`）用的「类型描述」，它把「大小 + 主类型（整数/浮点/指针/定点）」压成一个紧凑结构，用来在「按索引跳参数」时知道每个参数该按什么类型读：

[core_structs.h:113-148](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/__support/printf_core/core_structs.h#L113-L148)

最后，`printf_core` 的错误处理约定：成功返回 `WRITE_OK = 0`，各类错误是「从 -1001 起互不相同的负数」，刻意避开系统 errno 的取值范围：

[core_structs.h:150-163](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/__support/printf_core/core_structs.h#L150-L163)

#### 4.1.4 代码实践

**实践目标**：用一张表把「格式串写法」和 `LengthModifier` 枚举值一一对应起来，作为后续阅读 `Parser` 的索引。

**操作步骤**：

1. 打开 [core_structs.h:27-44](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/__support/printf_core/core_structs.h#L27-L44)。
2. 对照下表，确认每个枚举值对应的格式串写法与「目标 C 类型」（这点会在 4.2.3 由 `Parser` 验证）：

   | 格式串 | 枚举值 | `Parser` 最终按什么类型取参 |
   | --- | --- | --- |
   | `hh` | `hh` | `int`（`char`/`signed char` 参数在可变参数里提升为 `int`） |
   | `h` | `h` | `int`（`short` 同样提升为 `int`） |
   | `l` | `l` | `long` |
   | `ll` | `ll` | `long long` |
   | `j` | `j` | `intmax_t` |
   | `z` | `z` | `size_t` |
   | `t` | `t` | `ptrdiff_t` |
   | `L` | `L` | 浮点转换里取 `long double` |
   | `Q` | `Q` | `float128`（需 `LIBC_INTERNAL_PRINTF_CONVERT_FLOAT128`） |
   | `w` / `wN` | `w` | 按位宽选 `int`/`long`/`long long`/`intmax_t`（需未定义 `LIBC_COPT_PRINTF_DISABLE_BITINT`） |
   | `wf` / `wfN` | `wf` | 同上 |
   | （无） | `none` | `int`（整数）/ `double`（浮点） |

**需要观察的现象**：`hh`、`h`、`none` 在整数转换下都取 `int`——这印证了 C 标准里「小于 `int` 的整数参数在可变参数列表中会被提升为 `int`」的规则，所以 `Parser` 没必要区分它们。

**预期结果**：你能不看源码说出「`%hd` 和 `%d` 取参类型相同，都是 `int`；`%ld` 取 `long`；`%lld` 取 `long long`」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `FormatSection` 把整型和浮点参数都塞进 `conv_val_raw`（`UInt128`），而不是用 `union` 或继承区分类型？
**答案**：因为转换器在拿到 `FormatSection` 时已经通过 `conv_name` + `length_modifier` 知道该怎么解释这一位模式（比如 `%d` 就按 `uintmax_t` 读、`%f` 就按浮点位读）。用一个「够大的容器装位模式」最简单，免去 `union` 的判别开销，也方便 `operator==` 统一比较。

**练习 2**：`WRITE_OK` 为何定义为 `0`，而错误码全是「-1001 起」的负数？
**答案**：`printf` 系列返回「写入的字符数」（非负）或负数表示出错。引擎内部用 `0` 表示这段写入成功，用一组「远离系统 errno、互不相同」的负数标记不同失败原因；最外层再把负数翻成正的 errno 写回（详见 4.4.3 的 `vfprintf_internal`）。

### 4.2 解析器 Parser：把格式串变成 FormatSection

#### 4.2.1 概念说明

`Parser` 是 `printf_core` 三段式的第一段，职责单一：**从格式串里一段一段地切出 `FormatSection`**。它有两个关键设计。

第一，**模板化「参数来源」**。`Parser` 是 `template <typename ArgProvider>` 的，不直接依赖 `va_list`。任何提供了 `template <class T> T next_var<T>()` 方法的类型都能当 `ArgProvider`。这样同一个 `Parser` 既能接真实的 `internal::ArgList`（包装 `va_list`），也能接测试用的 `MockArgList`，还能接 GPU 用的 `DummyArgList`——这一抽象正是 `printf` 与 `scanf` 能够复用同一套参数机制的根基（见 4.4.1）。

第二，**「边解析边取参」**。`get_next_section()` 在解析到一个转换名时，立刻根据「转换名 + 长度修饰符」查出应该按什么 C 类型去 `ArgProvider` 取值，并把位模式存进 `FormatSection`。也就是说，交给 `Converter` 的 `FormatSection` 已经「自带参数值」，`Converter` 完全不用碰可变参数。

#### 4.2.2 核心流程

`get_next_section()` 的执行过程（伪代码）：

```
若 str[cur_pos] == '%':                       # 转换段
    has_conv = true
    （可选）parse_index   → 处理 %1$d 这种索引
    parse_flags          → 解析 - + 空格 # 0
    解析 width           → 数字、或 '*'（从参数取）
    解析 precision       → '.' 后的数字、或 '*'
    parse_length_modifier → 得到 LengthSpec{lm, bit_width}
    conv_name = str[cur_pos]
    按 (conv_name, lm) 查表 → 决定取参类型 T
    从 ArgProvider 取一个 T，位模式存进 conv_val_raw/conv_val_ptr
否则:                                         # 原始段
    has_conv = false
    一路读到下一个 '%' 或 '\0'
raw_string = 本段原文
返回 FormatSection
```

「按 `(conv_name, lm)` 查表取参」是核心。例如对整数转换（`d/i/u/o/x/X/b/B`），查表规则是：

| `length_modifier` | 取参类型 |
| --- | --- |
| `hh` / `h` / `none` | `int` |
| `l` | `long` |
| `ll` / `L` | `long long` |
| `j` | `intmax_t` |
| `z` | `size_t` |
| `t` | `ptrdiff_t` |

（这与 4.1.4 的表一致，这里由代码本身落实。）

`%ld` 的完整旅程：

1. `parse_length_modifier` 看到 `'l'`，发现下一个字符不是 `'l'`（是 `'d'`），于是返回 `{LengthModifier::l, 0}`，光标前进 1。
2. `conv_name = 'd'`。
3. 进入整数转换分支，`lm == LengthModifier::l` 命中 `long`，调用 `get_next_arg_value<long>()` 从 `ArgProvider` 取一个 `long`。
4. 经 `bit_cast` 把该 `long` 的位模式存进 `conv_val_raw`（一个 `UInt128`）。
5. 返回的 `FormatSection` 随后会被 `convert_int` 按「有符号十进制」解释 `conv_val_raw`。

#### 4.2.3 源码精读

`Parser` 类模板的开头与成员——注意它持有格式串指针 `str`、当前位置 `cur_pos`、以及一个 `ArgProvider args_cur`；index mode 下还额外持有 `args_start`（参数起点，用于回卷）和一张类型描述表 `desc_arr`：

[parser.h:79-113](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/__support/printf_core/parser.h#L79-L113)

取参的宏 `WRITE_ARG_VAL_SIMPLEST`：非 index mode 下就是「取下一个 `arg_type` 参数，位转换后赋给 `dst`」；其中 `int_type_of_v<T>` 对整数就是 `T` 本身，对浮点则映射成其位存储类型：

[parser.h:63-77](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/__support/printf_core/parser.h#L63-L77)

`get_next_section()` 的「标志 / 宽度 / 精度 / 长度修饰符」解析段——注意宽度若来自 `*` 且为负，会自动转成「正宽度 + 左对齐」，精度未出现时是 `-1`（被忽略），出现 `.` 但无数字时隐式为 `0`：

[parser.h:133-174](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/__support/printf_core/parser.h#L133-L174)

接着的「按转换名 + 长度修饰符查表取参」大 `switch`——这里就是 4.1.4 那张表的代码实现。`%ld` 的路径是 `case 'd'` → 内层 `case (LengthModifier::l)` → `WRITE_ARG_VAL_SIMPLEST(..., long, ...)`：

[parser.h:198-213](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/__support/printf_core/parser.h#L198-L213)

`parse_length_modifier` 负责把 `hh/h/l/ll/L/j/z/t/Q/w/wf` 翻译成 `LengthSpec`。对 `'l'` 它会偷看下一字符：若是另一个 `'l'` 就是 `ll`（消费 2 字符），否则是 `l`（消费 1 字符）；对 `'w'` 还会顺带解析可选的位宽数字填进 `bit_width`：

[parser.h:377-433](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/__support/printf_core/parser.h#L377-L433)

取参的底层调用——`get_next_arg_value<T>()` 直接转发给 `ArgProvider::next_var<T>()`，这正是 `Parser` 与具体参数来源解耦的接缝：

[parser.h:436-438](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/__support/printf_core/parser.h#L436-L438)

#### 4.2.4 代码实践（本讲主实践）

**实践目标**：亲手追踪 `%ld` 在 `Parser` 内部的完整转换路径，把 4.2.2 的描述落到具体行号。

**操作步骤**（源码阅读型实践）：

1. 假设格式串为 `"%" "ld"`，调用方传入一个值为 `42L` 的 `long` 参数。
2. 在 [parser.h:122](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/__support/printf_core/parser.h#L122) 处 `str[cur_pos] == '%'` 成立，进入转换段分支，`cur_pos` 前进到指向 `'l'`。
3. 跳过 index/flags/width/precision（都不命中），到达 [parser.h:171](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/__support/printf_core/parser.h#L171) 调用 `parse_length_modifier`。
4. 进入 [parser.h:379-386](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/__support/printf_core/parser.h#L379-L386)：`'l'` 命中，下一字符是 `'d'`（非 `'l'`），返回 `{LengthModifier::l, 0}`，光标前进到 `'d'`。
5. 回到 [parser.h:173-174](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/__support/printf_core/parser.h#L173-L174)：`conv_name = 'd'`，`bit_width = 0`。
6. 进入 [parser.h:198-213](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/__support/printf_core/parser.h#L198-L213) 的 `case 'd'`，内层 `case (LengthModifier::l)` 命中，执行 `WRITE_ARG_VAL_SIMPLEST(section.conv_val_raw, long, conv_index)`。
7. 该宏（[parser.h:75-76](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/__support/printf_core/parser.h#L75-L76)）展开为：`section.conv_val_raw = bit_cast<long>(get_next_arg_value<long>())`，即取出 `42L` 的位模式存进 `UInt128`。

**需要观察的现象**：最终产出的 `FormatSection` 中 `has_conv==true`、`length_modifier==LengthModifier::l`、`conv_name=='d'`、`conv_val_raw` 里是 `42` 的位模式、`raw_string=="%ld"`。

**预期结果**：你能画出 `"%ld"` → `parse_length_modifier` → `case 'd'/case l` → `bit_cast<long>` → `conv_val_raw` 这条调用链，并解释「为什么 `Converter` 后续完全不需要再访问可变参数」。

#### 4.2.5 小练习与答案

**练习 1**：`%lf`（注意是浮点 `f`）和 `%f` 取参类型相同吗？为什么？
**答案**：相同，都取 `double`。在 `case 'f'` 分支里（[parser.h:266-281](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/__support/printf_core/parser.h#L266-L281)），`lm == l` 不命中任何特化 case，落到 `default` 取 `double`。这符合 C 标准：`float` 参数在可变参数里提升为 `double`，`l` 修饰符对 `f` 转换无影响（要 `long double` 得用 `L`）。

**练习 2**：index mode（`%1$d`）下，`Parser` 如何做到「按索引取参数」而不违反「`va_list` 只能顺序读」的限制？
**答案**：它维护一张类型描述表 `desc_arr`（[parser.h:96-100](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/__support/printf_core/parser.h#L96-L100)），每当按顺序读过一次参数就记下其 `TypeDesc`。当请求的索引不是「下一个」时，`args_to_index` 会从起点重新顺序读、按已记录的类型逐个跳过，直到抵达目标索引（[parser.h:494-553](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/__support/printf_core/parser.h#L494-L553)）。最坏情况是 \(O(n^2) \)。

### 4.3 写入器 Writer：一层缓冲与三种溢出策略

#### 4.3.1 概念说明

`Writer` 是三段式的第二段（就「输出方向」而言），职责是**把转换器产生的字节写到一个目标**。但 `printf` 家族有三个性格迥异的对外接口：

- `fprintf`/`printf`：写到文件流，缓冲满了应该「冲刷到流」继续写。
- `snprintf`：写到定长缓冲，缓冲满了应该「丢弃溢出部分」（不能越界写）。
- `asprintf`：写到一块会自动增长的缓冲，缓冲满了应该「回调扩容」。

如果给三者各写一套 `printf` 引擎，代码会大量重复。`Writer` 的做法是：**用一层固定缓冲 + 一个「溢出钩子」抽象出共性，再用三种不同的 `WriteBuffer` 子类提供三种溢出策略**。于是同一套 `convert_*` 代码可以无差别地服务三种目标——这正是模板 `Writer<write_mode>` 的意义。

#### 4.3.2 核心流程

写入的数据流是：

```
convert_*  →  Writer::write(string_view / char / char+长度)
                │
                ├─ 缓冲还装得下？→ inline_memcpy / inline_memset 直接进缓冲（快路径）
                └─ 装不下？      → WriteBuffer::overflow_write(new_str)  ← 由子类决定策略
                                    ├─ DropOverflowBuffer : 只填满剩余空间，丢弃多余
                                    ├─ FlushingBuffer     : 把缓冲冲刷到流，再继续
                                    └─ ResizingBuffer     : 回调扩容缓冲，再继续
```

`Writer` 始终维护一个计数 `chars_written`——注意它统计的是「逻辑上应写入的字符数」，包括因缓冲满而被丢弃的部分。所以 `snprintf(buf, 10, ...)` 即使只写了 9 个字符，返回值仍是「本应写入的总数」，这符合 C 标准对 `snprintf` 返回值的规定。

写入模式 `WriteMode` 由 [write_modes.def](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/__support/printf_core/write_modes.def) 用 X-macro 列出四种：`FILL_BUFF_AND_DROP_OVERFLOW`、`FLUSH_TO_STREAM`、`RESIZE_AND_FILL_BUFF`，外加一个 `RUNTIME_DISPATCH`（用函数指针在运行期分派，避免模板实例化多份代码）。

#### 4.3.3 源码精读

`WriteMode` 枚举由 X-macro `write_modes.def` 生成——这种写法让「模式列表」只在一处定义，同时被枚举定义和后续的模板实例化复用：

[writer.h:24-28](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/__support/printf_core/writer.h#L24-L28)

`WriteBuffer` 基类——持有缓冲指针、长度、当前位置，并把「装不下时怎么办」声明为私有的 `overflow_write`，交由各子类经 `friend class Writer` 暴露：

[writer.h:42-60](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/__support/printf_core/writer.h#L42-L60)

三种溢出策略的子类。`DropOverflowBuffer`（给 `snprintf`）只填满剩余空间、丢弃多余，且永远返回 `WRITE_OK`：

[writer.h:63-80](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/__support/printf_core/writer.h#L63-L80)

`FlushingBuffer`（给 `fprintf`/`printf`）持有一个 `StreamWriter` 函数指针钩子和目标指针，缓冲满时先把已有内容冲刷到流、再写新串：

[writer.h:83-115](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/__support/printf_core/writer.h#L83-L115)

`ResizingBuffer`（给 `asprintf`）持有一个扩容回调，缓冲满时回调负责把缓冲调大：

[writer.h:118-134](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/__support/printf_core/writer.h#L118-L134)

`overflow_write` 按模板参数 `WriteMode` 各特化一份，把调用转发给对应子类——这就是「同一接口、按模式换实现」的分派点：

[writer.h:136-167](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/__support/printf_core/writer.h#L136-L167)

`Writer` 类的三个 `write` 重载，以 `write(string_view)` 为例：快路径用 `inline_memcpy`（承接 u5-l2 的 mem\* 框架）直接拷进缓冲，并用 `LIBC_LIKELY` 标注快路径；慢路径才走 `overflow_write`：

[writer.h:204-213](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/__support/printf_core/writer.h#L204-L213)

填充（padding）的 `pad()`——当要写一长串同一个字符（如宽度补零、空格对齐）时，先用 64 字节的 `mini_buff` 批量 `overflow_write`，避免一字一调：

[writer.h:173-197](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/__support/printf_core/writer.h#L173-L197)

#### 4.3.4 代码实践

**实践目标**：搞清「同一份 `convert_*` 代码如何靠换 `WriteBuffer` 子类来同时服务 `snprintf` 与 `fprintf`」。

**操作步骤**（源码阅读 + 对照型实践）：

1. 看 `snprintf` 的实现思路：它应当构造一个 `DropOverflowBuffer`（定长缓冲），再交给 `printf_main`。在仓库里搜索其用法（`grep` 关键字 `DropOverflowBuffer`），观察它如何传入「用户缓冲 + 容量」。
2. 再看 [vfprintf_internal.h:84-103](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/__support/printf_core/vfprintf_internal.h#L84-L103)：`vfprintf_internal` 构造的是一个 1024 字节栈缓冲 + `FlushingBuffer`，冲刷钩子 `file_write_hook`（[vfprintf_internal.h:65-82](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/__support/printf_core/vfprintf_internal.h#L65-L82)）负责把字节真正写到 `FILE*`。
3. 对比两者：**`convert_int` 等转换函数的代码完全一致，唯一区别是传入的 `WriteBuffer` 子类不同**。

**需要观察的现象**：缓冲未满时，两条路径都走 `Writer::write` 的快路径（直接 `inline_memcpy` 进栈缓冲）；只有缓冲满时，一个丢弃、一个冲刷——分叉点全在 `overflow_write`。

**预期结果**：你能解释「为什么 `printf_core` 只实现一遍格式化逻辑，却能满足 `printf`/`fprintf`/`sprintf`/`snprintf`/`asprintf` 五个接口」——答案就是 `Writer<write_mode>` 把「输出目的地」抽象成了模板参数。

**待本地验证**：上述 `snprintf` 用 `DropOverflowBuffer` 的结论，建议本地 `grep DropOverflowBuffer src/stdio/` 确认具体接入点（本讲未展开其源码）。

#### 4.3.5 小练习与答案

**练习 1**：`snprintf(buf, 5, "%d", 123456)`（缓冲只够 4 字符 + 结尾 `\0`）应返回多少？`Writer` 怎么保证？
**答案**：返回 `6`（本应写入 `"123456"` 共 6 个字符）。`Writer::write` 即使走 `DropOverflowBuffer::fill_remaining_to_buff` 丢弃了溢出部分，也照样在 [writer.h:205](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/__support/printf_core/writer.h#L205) 把 `new_string.size()` 累加进 `chars_written`，所以「逻辑写入数」正确，只是实际缓冲里只装得下前几个字符。

**练习 2**：`RUNTIME_DISPATCH` 模式相比直接用模板特化有什么好处？
**答案**：模板特化会为每个 `WriteMode` 实例化一整套 `convert_*` 函数，二进制体积膨胀；`RUNTIME_DISPATCH` 把 `overflow_write` 收敛成一份用 `if/else` 按 `write_mode_` 字段在运行期分派的代码（[writer.h:136-147](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/__support/printf_core/writer.h#L136-L147)），以「一次间接跳转」换「显著缩小的代码体积」，在体积敏感的目标（如 GPU）上更划算。

### 4.4 转换器 Converter 与三段式编排

#### 4.4.1 概念说明

第三段是转换器，职责是**消费一个 `FormatSection`，把它格式化成字符串并写进 `Writer`**。它由两部分组成：

- 一个总分派函数 `convert(writer, to_conv)`：按 `to_conv.conv_name` 路由到具体的 `convert_int` / `convert_float` / `convert_string` / `convert_char` / `convert_pointer` 等。
- 一组具体转换器（`int_converter.h`、`string_converter.h`、`float_dec_converter.h` 等），每个负责一种类型。

而把「解析—转换—写入」三段真正串成一台引擎的，是 [printf_main.h](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/__support/printf_core/printf_main.h) 里的主循环 `printf_main_modular`：它不停地 `get_next_section()`，转换段调 `convert`、原始段直接 `write`，任一步返回负数即带上错误码退出。

**关于 `printf` 与 `scanf` 的「复用」关系（重要，避免误解）**：`scanf` 并不直接 include `printf_core` 的头文件，而是有一套**平行的** `scanf_core`（位于 `src/stdio/scanf_core/`）。两者复用的是**同一套架构设计**和**同一套 `ArgList` 抽象**，而不是同一份源码：

| 维度 | `printf_core` | `scanf_core` |
| --- | --- | --- |
| 三段式 | Parser → Converter → **Writer**（输出） | Parser → Converter → **Reader**（输入） |
| 核心数据结构 | 自有 `FormatSection`/`LengthModifier`（`printf_core/core_structs.h`） | 自有同名结构（`scanf_core/core_structs.h`，独立定义） |
| 参数来源 | `Parser<internal::ArgList>` | `Parser<internal::ArgList>`（**同一个** `ArgList`） |
| 主循环 | `printf_main_modular`：`convert` / `write` | `scanf_main`：`convert` / `raw_match` |

也就是说，`Parser<ArgProvider>` 这个「模板化参数来源」的设计让 `printf` 与 `scanf` 共用了 `internal::ArgList`（[arg_list.h:28-42](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/__support/arg_list.h#L28-L42)），但解析、转换、输入输出逻辑是各自独立实现的两套平行核心。

#### 4.4.2 核心流程

`printf_main_modular` 的主循环：

```
构造 Parser<internal::ArgList>(format, args)
for section = parser.get_next_section(); 直到 raw_string 为空:
    if section.has_conv:
        result = convert(writer, section)     # 转换段：交给转换器
    else:
        result = writer->write(section.raw_string)   # 原始段：直接写
    if result < 0: return Error(-result)      # 任意段失败即带错误码返回
return writer->get_chars_written()            # 成功：返回写入字符数
```

`convert(writer, to_conv)` 的分派逻辑：

```
if not to_conv.has_conv: return writer->write(raw_string)   # 防御性
switch to_conv.conv_name:
    '%': writer->write("%")
    'c': convert_char
    's': convert_string
    'd','i','u','o','x','X','b','B': convert_int
    'f','F','e','E','a','A','g','G': convert_float
    'm': convert_strerror      # %m：取 errno 转字符串，不消费参数
    'n': convert_write_int     # %n：把已写字符数写回参数指向的 int
    'p': convert_pointer
    default: writer->write(raw_string)   # 未知转换：原样输出
```

#### 4.4.3 源码精读

三段式编排的入口——`printf_main_modular`，注意它把「转换段」与「原始段」分开处理，错误时用 `Error(-result)` 把负数错误码翻成正数交给上层（承接 u4-l3 的 `ErrorOr` 机制）：

[printf_main.h:25-43](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/__support/printf_core/printf_main.h#L25-L43)

`printf_main` 外壳多了一条 `.reloc` 内联汇编——它把符号 `__printf_float` 引用进重定位表，使得「即使本翻译单元没用浮点，链接器也能按需拉入浮点转换模块」。这正是 u7-l3「模块化格式串」的伏笔：

[printf_main.h:45-53](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/__support/printf_core/printf_main.h#L45-L53)

转换器总分派 `convert()`——按 `conv_name` 路由；其中浮点转换被包在 `LIBC_PRINTF_MODULE` 宏里（同样服务于 u7-l3 的按需链接）：

[converter.h:65-135](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/__support/printf_core/converter.h#L65-L135)

`convert_float` 的二级分派（`f`→十进制、`e`→指数、`a`→十六进制、`g`→自动）：

[converter.h:31-52](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/__support/printf_core/converter.h#L31-L52)

以 `convert_int` 为典型转换器：它复用 u7-l1 讲过的 `IntegerToString`（`HexFmt`/`DecFmt`/`OctFmt`/`BinFmt`）把 `to_conv.conv_val_raw`（取为 `uintmax_t`）格式化进栈缓冲，再交给 `Writer` 输出——这正体现了「公共算法下沉到 `__support`」的复用：

[int_converter.h:29-62](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/__support/printf_core/int_converter.h#L29-L62)

`printf` 入口如何接上这台引擎：`vfprintf_internal` 构造 `FlushingBuffer`（栈缓冲 + `file_write_hook` 冲刷到 `FILE*`），构造 `Writer`，加锁后调 `printf_main`，最后再补一次 `flush_to_stream()` 把缓冲里残留的字节冲掉：

[vfprintf_internal.h:84-103](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/__support/printf_core/vfprintf_internal.h#L84-L103)

`scanf` 平行核心的主循环 `scanf_main`——结构与 `printf_main_modular` 几乎对称，只是「写」换成「读匹配」（`raw_match`），成功返回「成功转换与赋值的个数」：

[scanf_main.h:24-45](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/stdio/scanf_core/scanf_main.h#L24-L45)

最后看构建侧：`printf_main` 是一个 `add_object_library`（带一个 `float_impl.cpp` 源文件，专门承载模块化浮点的强符号定义），`DEPENDS` 串起 `.parser`、`.converter`、`.writer`、`.core_structs` 以及共享的 `libc.src.__support.arg_list`——这条 `DEPENDS` 链就是三段式在 CMake 里的投影：

[src/__support/printf_core/CMakeLists.txt:161-174](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/__support/printf_core/CMakeLists.txt#L161-L174)

#### 4.4.4 代码实践

**实践目标**：从入口到核心，追一遍 `printf("%d", 42)` 在源码层面的完整调用链。

**操作步骤**（源码阅读型实践）：

1. 入口 `printf.cpp`（壳）拿到格式串和 `va_list`，构造 `internal::ArgList`（[arg_list.h:28-42](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/__support/arg_list.h#L28-L42)）。
2. 调 `printf_core::printf_main(&writer, "%d", args)`（[printf_main.h:45-53](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/__support/printf_core/printf_main.h#L45-L53)）。
3. 进入主循环（[printf_main.h:31-40](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/__support/printf_core/printf_main.h#L31-L40)）：`parser.get_next_section()` 返回一个 `has_conv==true`、`conv_name=='d'`、`conv_val_raw` 为 `42` 位模式的 `FormatSection`。
4. `convert(writer, section)` 命中 `case 'd'` → `convert_int`（[converter.h:94-102](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/__support/printf_core/converter.h#L94-L102)）。
5. `convert_int` 用 `DecFmt::format_to` 把 `42` 写进栈缓冲，再 `writer->write(string_view)`。
6. `Writer::write`（[writer.h:204-213](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/__support/printf_core/writer.h#L204-L213)）把 `"42"` 拷进 `FlushingBuffer` 的栈缓冲。
7. 主循环结束后，`vfprintf_internal` 调 `wb.flush_to_stream()`（[vfprintf_internal.h:98](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/__support/printf_core/vfprintf_internal.h#L98)）经 `file_write_hook` 把 `"42"` 真正写到 `stdout`。

**需要观察的现象**：解析（取参）、转换（数字转字符串）、写入（缓冲+冲刷）三段职责严格分离，任何一段都不越界碰另一段的内部数据。

**预期结果**：你能画出 `printf → printf_main → Parser::get_next_section → convert → convert_int → Writer::write → FlushingBuffer::flush_to_stream → file_write_hook → fwrite_unlocked` 这条完整链路，并指出三段的边界各在哪几行。

#### 4.4.5 小练习与答案

**练习 1**：`convert()` 里 `default` 分支为什么是 `writer->write(to_conv.raw_string)` 而不是报错？
**答案**：C 标准规定「无效的转换说明行为未定义」，LLVM-libc 选择「尽量宽容」——把整段原文（包括开头的 `%`）原样输出，而不是让整个 `printf` 失败。这样面对陌生扩展说明符时不会崩溃。注意 `Parser` 对完全无法识别的转换名会把 `has_conv` 置为 `false`（[parser.h:319-322](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/__support/printf_core/parser.h#L319-L322)），于是它会被当作原始段直接输出，效果一致。

**练习 2**：`%m` 与 `%n` 是两个特殊转换，它们在「是否消费参数」上有何不同？
**答案**：`%m` **不消费参数**——它直接读当前 `libc_errno` 当作参数（[parser.h:303-308](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/__support/printf_core/parser.h#L303-L308)），交给 `convert_strerror` 转成错误字符串。`%n` **消费一个 `void*` 参数**，但「不产生输出」——它把「目前已写入的字符数」写回该指针指向的 `int`（`convert_write_int`）。`scanf_main` 还专门对 `%n` 做了「不计入成功赋值数」的处理（[scanf_main.h:35-38](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/stdio/scanf_core/scanf_main.h#L35-L38)）。

## 5. 综合实践

**任务**：把本讲四个模块串起来，亲手「模拟」`printf("%6ld", 1234L)` 的完整处理过程，画出数据在各结构之间的流转图。

**要求**：

1. **解析阶段**：参照 4.2，写出 `Parser::get_next_section` 对 `"%6ld"` 的处理。
   - `parse_flags` 得到什么？（提示：无标志）
   - 宽度如何得到 `min_width = 6`？（[parser.h:141-145](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/__support/printf_core/parser.h#L141-L145)）
   - `parse_length_modifier` 返回什么？`conv_name` 是什么？取参类型是什么？
2. **填充 `FormatSection`**：列出该段 `FormatSection` 每个字段的最终值（`has_conv`、`flags`、`min_width`、`precision`、`length_modifier`、`conv_name`、`conv_val_raw`、`raw_string`）。
3. **转换阶段**：参照 4.4，说明 `convert` 如何分派到 `convert_int`，`convert_int` 又如何用 `IntegerToString`（十进制）把 `1234` 转成字符串 `"1234"`。
4. **写入阶段**：参照 4.3，说明因为 `min_width=6 > 4`（数字串长度），`convert_int` 会先用 `Writer::write(' ', 2)`（或在 `LEADING_ZEROES` 下用 `'0'`）补 2 个前导空格得到 `"  1234"`，再写出数字串。指出这发生在 `Writer::write(char, length)`（[writer.h:218-228](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/__support/printf_core/writer.h#L218-L228)）。
5. **对照 `scanf`**：用一句话说明，如果是 `scanf("%6ld", &x)`，`scanf_core` 里会走怎样平行的三段式（Parser → convert → Reader），并指出它复用了 `printf` 的哪一块（答案：`internal::ArgList`）。

**交付物**：一张「`FormatSection` 在四段之间的流转图」+ 一份字段取值表。**待本地验证**：第 4 步「宽度补齐的具体位置（在 `convert_int` 内还是主循环内）」建议本地通读 [int_converter.h](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/__support/printf_core/int_converter.h) 确认补齐逻辑的确切行号。

## 6. 本讲小结

- `printf_core` 把 `printf` 拆成**三段式**：`Parser`（解析格式串、按类型取参）→ `Converter`（按转换名把值格式化成字符串）→ `Writer`（缓冲输出），数据以 `FormatSection` 为载体在三段间流转。
- **核心数据结构** `FormatSection` 把一段格式说明的全部信息（标志/宽度/精度/长度修饰符/转换名/参数位模式）装进一个结构体；`LengthModifier` 枚举与格式串写法一一对应；错误用「-1001 起」的负数表示。
- **`Parser<ArgProvider>`** 是模板，靠 `ArgProvider::next_var<T>()` 与具体参数来源解耦；它「边解析边取参」，对 `%ld` 会查表取 `long`、位转换后存进 `conv_val_raw`，使 `Converter` 完全不必碰可变参数。
- **`Writer<write_mode>`** 用一层固定缓冲 + 三种 `WriteBuffer` 子类（`DropOverflowBuffer`/`FlushingBuffer`/`ResizingBuffer`）统一服务 `snprintf`/`fprintf`/`asprintf`，让同一份 `convert_*` 代码适配三种输出目标。
- **`printf_main_modular`** 是主循环，把三段串成引擎；`printf` 与 `scanf` 复用的是**同一套架构设计与同一个 `internal::ArgList`**，而非同一份源码——`scanf` 有平行的 `scanf_core`。
- 三段式的职责隔离让每段都可独立替换与测试：换 `WriteBuffer` 改输出目标、换 `ArgProvider` 改参数来源、在 `convert` 里加 `case` 加新转换——这些正是后续 u7-l3（模块化格式串）和 u11-l3（贡献新函数）要利用的扩展点。

## 7. 下一步学习建议

- **紧接着读 u7-l3《模块化格式串》**：本讲多次出现的 `LIBC_PRINTF_MODULE` 宏、`printf_main` 里的 `.reloc __printf_float`、以及 `printf_main` 为何是个带 `float_impl.cpp` 的 `add_object_library`，都指向同一个问题——「如何让调用者只用 `%d` 时，链接器不把庞大的浮点转换表拉进二进制」。u7-l3 会从「死代码问题 → 弱符号 → 配置开关」讲透这套机制。
- **回顾 u7-l1《stdlib 数值转换》**：`convert_int` 复用的 `IntegerToString` 就来自那里，建议对照阅读，体会「公共算法下沉到 `__support`」如何同时服务 stdlib 与 printf。
- **想动手扩展的话**：参照 u11-l3《贡献一个完整新函数》，试着在 `convert()` 里加一个自定义转换名（仅作练习），观察「解析—转换—写入」三段需要各自做哪些最小改动。
- **若对输入侧感兴趣**：通读 `src/stdio/scanf_core/`，把它和 `printf_core` 逐文件对照（`parser.h` vs `parser.h`、`writer.h` vs `reader.h`、`printf_main.h` vs `scanf_main.h`），体会同一套架构如何对称地服务「输出」与「输入」。
