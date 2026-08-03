# stdlib 数值转换：字符串与整数的互转

## 1. 本讲目标

本讲聚焦 LLVM-libc 中「字符串 ↔ 整数」这一对方向相反、却共享同一套设计哲学的转换。读完本讲，你应当能够：

- 说清 `atoi`/`strtol`/`strtoul` 等入口点为什么都只是「薄壳」，真正的解析算法在哪里。
- 读懂 `__support/str_to_integer.h` 中 `strtointeger` 模板的完整流程，特别是它**如何在溢出发生之前就检测到溢出**。
- 读懂 `__support/integer_to_string.h` 中 `IntegerToString` 类如何把整数反向写成字符串，以及 `radix::Dec/Hex/Oct/Bin` 这套格式导航机制。
- 用一句话概括「公共算法下沉」这一贯穿整个 `__support` 的设计原则，并举出字符串解析与整数格式化各自的下游消费者。

## 2. 前置知识

本讲假设你已建立以下认知（来自前置讲义）：

- **入口点是薄壳**（u1-l5、u2-l1、u5-l1）：公开函数用 `LLVM_LIBC_FUNCTION` 产生 C 符号，自身只做参数校验与边界处理，真正的算法下沉到 `src/__support`。
- **`__support` 是私有标准库**（u4-l1）：它不对应任何公共头文件、不产生公开 C 符号，用 `add_header_library` 声明为**内部头文件库**，由入口点经 CMake 的 `DEPENDS` 引用。C++ 源码里 `#include` 一个 `__support` 头，就必须在 `DEPENDS` 里写上对应的点分目标名。
- **错误处理两层结构**（u4-l3）：内部传错用 `ErrorOr<T>`；但字符串解析走的是另一套结果类型 `StrToNumResult`（见下文），对外写 `libc_errno`。

补充两个 C 标准背景，初学者可能不熟：

- **`strtol` 的语义**：跳过前导空白 → 解析可选正负号 → 按 `base` 解析数字 → 通过 `str_end` 指针告诉调用者「我读到哪儿了」；溢出时返回类型最大/最小值并把 `errno` 置为 `ERANGE`。`base` 取 `0` 表示由字符串前缀自动推断（`0x`→16、`0b`→2、前导 `0`→8，否则 10）。
- **有符号整数的不对称**：例如 8 位有符号数的范围是 \([-128, 127]\)，即 \(|\text{MIN}| = |\text{MAX}| + 1\)。这一点会直接影响下面溢出阈值的设计。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [`src/__support/str_to_integer.h`](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/__support/str_to_integer.h) | **字符串→整数**的公共后端，提供 `strtointeger<T>` 模板，是所有 `strto*`/`ato*` 的实现核心。 |
| [`src/__support/str_to_num_result.h`](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/__support/str_to_num_result.h) | `StrToNumResult<T>` 结果类型，同时携带「值、已解析长度、错误码」。 |
| [`src/__support/integer_to_string.h`](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/__support/integer_to_string.h) | **整数→字符串**的公共工具，提供 `IntegerToString<T>` 类与 `radix::` 格式族。 |
| [`src/__support/ctype_utils.h`](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/__support/ctype_utils.h) | 字符级原子工具：`b36_char_to_int`（字符→数字）、`int_to_b36_char`（数字→字符）、`isspace`、`isalnum` 等，两端共用。 |
| [`src/stdlib/atoi.cpp`](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/stdlib/atoi.cpp) / [`strtol.cpp`](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/stdlib/strtol.cpp) / [`strtoul.cpp`](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/stdlib/strtoul.cpp) | 入口点薄壳：调用 `strtointeger`，设置 `errno` 与 `str_end`。 |
| [`src/__support/CMakeLists.txt`](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/__support/CMakeLists.txt) | 把这两个工具注册为内部头文件库（`add_header_library`）。 |
| [`src/__support/printf_core/int_converter.h`](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/__support/printf_core/int_converter.h) | `IntegerToString` 的典型下游消费者（`printf` 的 `%d/%x/%o/%b`）。 |

## 4. 核心概念与源码讲解

### 4.1 字符串解析：strtointeger 与 strto* 入口点

#### 4.1.1 概念说明

C 标准里「字符串转整数」的函数有一大串：`atoi`、`atol`、`atoll`、`strtol`、`strtoll`、`strtoul`、`strtoull`，再加上 `<inttypes.h>` 的 `strtoimax`/`strtoumax` 和 `<wchar.h>` 的 `wcstol` 一族。它们的**差异仅在返回类型与是否有 `str_end`/`base` 参数**，核心解析逻辑完全相同。

LLVM-libc 的做法是把这份共用逻辑抽成一个模板 `internal::strtointeger<T>`，放进 `__support/str_to_integer.h`：

- 模板参数 `T` 决定返回类型（`long`/`unsigned long`/`intmax_t`…），同一段代码自动适配有符号/无符号、不同位宽。
- 它返回的不是裸整数，而是 `StrToNumResult<T>`，把「值、错误码、已解析长度」三件事打包，让入口点据此设置 `errno` 和 `str_end`。
- 该文件顶部有一处关键警告：**这个接口是与 libc++ 共享的**（`This interface is shared with libc++`）。这意味着修改它要同时照顾两边——这是「公共算法下沉」原则的一个极致体现：下沉到连别的子项目都能复用。

#### 4.1.2 核心流程

`strtointeger` 的执行过程可以拆成「准备 → 解析 → 兜底」三段：

```
1. 校验 base：base 必须是 0 或 [2,36]，否则返回 {0, 0, EINVAL}
2. first_non_whitespace ：跳过前导空白（space/tab/newline/...）
3. get_sign ：解析可选的 '+'/'-'，记录 is_positive
4. 若 base==0：infer_base 由前缀推断
     - "0x"/"0X" → 16
     - "0b"/"0B" → 2
     - 前导 '0'   → 8
     - 其它       → 10
5. 若 base==16 且串以 "0x" 开头：跳过这两个前缀字符（base==2 同理跳 "0b"）
6. 计算溢出阈值 abs_max 与 abs_max_div_by_base（见下方说明）
7. 主循环：逐字符读取，只要 isalnum 且 b36_char_to_int(ch) < base
     result = result * base + digit      ← 带溢出预检查（见下）
8. 收尾：
     - 若曾溢出 → 正数/无符号返回 T::max，负数返回 T::min，error=ERANGE
     - 否则     → 返回 is_positive ? result : -result
```

**溢出检测的数学原理**。最朴素的写法是先算 `result*base+digit` 再比较是否越界，但这会在「比较之前」就已经溢出（未定义行为）。正确做法是**在乘/加之前用阈值预判**。设当前累加值为 \(r\)、基为 \(b\)、当前数字为 \(d\)、该类型允许的最大绝对值为 \(M\)，则发生溢出当且仅当：

\[
r \cdot b + d > M
\]

为避免计算左端时本身溢出，拆成两步检查：

\[
r > \left\lfloor M / b \right\rfloor \quad\Longrightarrow\quad r \cdot b \text{ 必溢出，直接钳位}
\]

否则先算 \(r \leftarrow r \cdot b\)，再检查加法：

\[
r > M - d \quad\Longrightarrow\quad r + d \text{ 必溢出，钳位}
\]

这两步对应源码里的 `abs_max_div_by_base` 与 `abs_max - cur_digit`。

**有符号不对称的处理**。对有符号类型，正方向上限是 `T::max`，负方向下限的绝对值是 `T::max + 1`（因为 \(|T_{\min}| = |T_{\max}|+1\)）。代码用一个无符号中间类型 `ResultType` 来承载，把负向阈值算成：

\[
\text{NEGATIVE\_MAX} = \text{static\_cast<ResultType>}(T\_{::max}) + 1
\]

这样无论正负都在无符号域里统一比较，最后再 `static_cast<T>` 回去，绕开了「对 `INT_MIN` 取负」的经典溢出陷阱。

#### 4.1.3 源码精读

整个后端就是下面这个模板（核心主循环与溢出处理在其中段）：

[`strtointeger` 模板，字符串→整数的公共后端](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/__support/str_to_integer.h#L112-L194)

关键的内层循环（带溢出预检查）：

```cpp
while (src_cur < src_len && isalnum(src[src_cur])) {
  int cur_digit = b36_char_to_int(src[src_cur]);
  if (cur_digit >= base) break;        // 非法数字，停止
  is_number = true;
  ++src_cur;
  if (result == abs_max) { error_val = ERANGE; continue; }   // 已饱和，只推进指针
  if (result > abs_max_div_by_base) {                          // 乘法会溢出
    result = abs_max; error_val = ERANGE;
  } else {
    result = result * static_cast<ResultType>(base);
  }
  if (result > abs_max - static_cast<ResultType>(cur_digit)) { // 加法会溢出
    result = abs_max; error_val = ERANGE;
  } else {
    result = result + static_cast<ResultType>(cur_digit);
  }
}
```

一个值得注意的细节：**一旦 `result == abs_max`，循环并不 `break`，而是 `continue`**。这是为了继续把 `src_cur` 推到数字串末尾——C 标准要求 `str_end` 指向「整个数字串之后」，即使值已经溢出。

阈值准备与前缀推断的辅助函数：

- [`infer_base：由前缀 0x/0b/前导 0 推断进制`](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/__support/str_to_integer.h#L82-L103)
- [`first_non_whitespace：跳过前导空白`](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/__support/str_to_integer.h#L35-L43)

字符→数字的原子操作来自 `ctype_utils.h`（u5-l1 已介绍过它的「逐字符 switch、编码无关」哲学）：

[`b36_char_to_int：把一个字符映射为 0–35 的数字值`](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/__support/ctype_utils.h#L375-L478)

结果类型 `StrToNumResult<T>` 同时携带值、错误码、已解析长度，并提供 `has_error()` 与隐式转换到 `T`：

[`StrToNumResult 结构](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/__support/str_to_num_result.h#L30-L45)

而入口点薄壳极其简短。`atoi` 标准上等价于 `(int)strtol(str, nullptr, 10)`，源码里正是这么写的——调用 `strtointeger<long>` 再 `static_cast<int>`：

[`atoi.cpp：薄壳，委托给 strtointeger<long>`](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/stdlib/atoi.cpp#L17-L25)

`strtol` 多了两件事：把 `base` 透传、用 `parsed_len` 设置出参 `str_end`（但 `EINVAL` 时不写 `str_end`）：

[`strtol.cpp：透传 base 并据 parsed_len 设置 str_end`](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/stdlib/strtol.cpp#L17-L28)

`strtoul` 与之几乎逐字相同，仅把模板实参换成 `unsigned long`：

[`strtoul.cpp：unsigned long 版本](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/stdlib/strtoul.cpp#L17-L28)

#### 4.1.4 代码实践

**实践目标**：手工把字符串 `"-42"` 喂给 `strtointeger`，验证它能被正确解析。

**操作步骤**（源码阅读型，跟踪调用链）：

1. 打开 `strtointeger` 模板，以 `T = long`、`src = "-42"`、`base = 10` 代入。
2. 逐步写下每个局部变量的值：
   - `first_non_whitespace` 返回 `0`（无前导空白）。
   - `get_sign` 看到 `'-'`，返回 `-1`，`is_positive = false`，`src_cur` 变为 `1`。
   - 进入主循环：`'4' → cur_digit=4`，`'2' → cur_digit=2`，依次累加 `result = 0*10+4 = 4`，再 `4*10+2 = 42`。
   - 循环结束，`is_positive` 为假，返回 `static_cast<long>(-42)`，`parsed_len = 3`，`error = 0`。
3. 对照 [`strtol.cpp`](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/stdlib/strtol.cpp#L17-L28) 确认：`str_end` 会被写成 `str + 3`（指向 `"-42"` 末尾的 `\0`）。

**需要观察的现象**：解析负数时，**取负发生在最后一步**（`is_positive ? result : -result`），中间全程在无符号域里用绝对值累加；这正是前文「有符号不对称」设计的体现。

**预期结果**：返回 `-42`，`errno` 不被设置，`str_end` 指向原串结尾。**待本地验证**：可在已构建的 libc 里运行单测目标 `libc.test.src.stdlib.strtol_test.__unit__`（命名规则见 u1-l3），观察 [`StrtolTest.h` 中 `CleanBaseTenDecode`](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/test/src/stdlib/StrtolTest.h#L40-L55) 是否通过。

#### 4.1.5 小练习与答案

**练习 1**：调用 `strtol("0x1A", nullptr, 0)` 会得到什么？为什么 `src_cur` 中途要 `+2`？

**答案**：得到 `26`。因为 `base==0` 时 `infer_base` 识别出 `0x` 前缀把 `base` 设为 `16`，随后 [`str_to_integer.h#L136-L137`](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/__support/str_to_integer.h#L136-L137) 把 `src_cur` 前进 2 以跳过 `"0x"`，再解析 `"1A"` 为 \(1\times16+10=26\)。

**练习 2**：`strtoul` 和 `strtol` 共用同一个 `strtointeger` 模板，它如何区分有符号/无符号？

**答案**：靠模板实参 `T`。代码用 `cpp::is_unsigned_v<T>` 得到 `IS_UNSIGNED`，据此选择 `NEGATIVE_MAX`（有符号时为 `max+1`，无符号时为 `max`）与溢出时的返回值（无符号永远返回 `max`，见 [`str_to_integer.h#L186-L191`](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/__support/str_to_integer.h#L186-L191)）。

---

### 4.2 整数转字符串：IntegerToString 与 radix 格式族

#### 4.2.1 概念说明

反向转换——把整数写成字符串——同样不止一个调用方：`printf` 的 `%d/%i/%x/%o/%b`、`sprintf`、`strfromd` 内部、断言与错误信息打印（`libc_assert`、`error_to_string`、`signal_to_string`）、时间格式化（`strftime`）等都需要它。于是 LLVM-libc 把它也下沉成一个公共工具 [`IntegerToString<T, Fmt>`](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/__support/integer_to_string.h#L331-L492)。

它的设计有两个亮点：

- **类型参数 `T` + 格式参数 `Fmt`**：`T` 决定被转换的整数类型，`Fmt` 决定如何排版（进制、是否加前缀 `0x`、是否补零到定宽、是否大写、是否强制正号）。
- **栈上缓冲、永不失败**：构造时把结果写进一个**编译期定长**的内部数组 `array`，其大小经过严格估算，保证「任何 `T` 的任何值都能装下」，于是 `view()` 总能成功；另外提供一个可能失败的 `format_to(span, value)`，用于把数据写进调用者提供的外部缓冲。

#### 4.2.2 核心流程

`IntegerToString` 的写入流程用了一个小巧思：**反向写**。因为「取最低位数字」需要不断除以进制基，得到的是从低位到高位的数字，所以用一个 `BackwardStringBufferWriter` 从缓冲**末尾往前**填字符，最后通过 `view()` 返回一段正确顺序的 `string_view`。

```
构造 IntegerToString<T>(value)：
1. 准备 BackwardStringBufferWriter（往缓冲头部方向写）
2. IntegerWriter::write(value, sink)：
   a. 取绝对值 abs(value) —— 用 UNSIGNED_T 承载，绕开 INT_MIN 取负陷阱
   b. 循环：digit = value % BASE; sink.push(digit_char(digit)); value /= BASE
        （十进制走更快的 extract_decimal_digit）
   c. 宽度：若已写字符数 < MIN_DIGITS，补前导 '0'
   d. 符号：十进制下负数 push '-'，或 FORCE_SIGN 时正数 push '+'
   e. 前缀：WithPrefix 时按进制补 "0x"/"0b"/"0"（八进制有去重特判）
3. 记录 written = sink.size()；view() 返回 array 末尾的 written 个字符
```

**缓冲区大小的估算**。对十进制，文件头注释给出了经验公式（`sizeof(T)` 字节数 → 十进制位数上界）：

\[
\text{digits} = \left\lfloor \frac{\text{sizeof}(T) \cdot 5 + 1}{2} \right\rfloor
\]

例如 `sizeof(T)==4`（32 位）得 10（`UINT32_MAX = 4294967295` 正好 10 位），`sizeof(T)==8` 得 20。这个估计比真实上界略大但开销可容忍；其它进制则按「向下取整到最近的 2 的幂进制」来估。最终 `BUFFER_SIZE = max(位数, MIN_DIGITS) + 符号位 + 前缀位`，全部 `constexpr`。

**`INT_MIN` 的处理**。有符号类型是不对称的，直接 `-value` 对 `T::min()` 是未定义行为。代码利用 C++20 起「有符号数保证用二补码表示」的规定，对最小值用 `cpp::bit_cast<UNSIGNED_T>(value)` 直接复用同一组比特——`int8_t(-128)` 的比特 `0b10000000` 重解释成 `uint8_t` 正好是 `128`。

#### 4.2.3 源码精读

格式参数 `Fmt` 是个编译期结构体，把进制、前缀、符号、大小写、最小宽度全部编码进类型，并通过一组嵌套 `using` 提供「导航式」配置：

[`Fmt 结构：用类型把所有排版选项编码为编译期常量`](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/__support/integer_to_string.h#L78-L102)

由此得到一组开箱即用的格式别名，调用方写成 `IntegerToString<uintmax_t, radix::Hex::Uppercase>`：

[`radix 命名空间：Bin/Oct/Dec/Hex 四种预设`](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/__support/integer_to_string.h#L156-L164)

反向写入器把「位置」抽象成一个 `forward` 模板参数（`forward=false` 即从尾向头写），同一份代码同时服务正向与反向两种写入需求：

[`BackwardStringBufferWriter：从缓冲末尾往前填字符`](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/__support/integer_to_string.h#L105-L149)

真正把整数拆成数字并写入的核心逻辑：

[`IntegerWriter::write：取绝对值 → 反向写数字 → 补宽 → 符号 → 前缀`](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/__support/integer_to_string.h#L428-L461)

公开 API 有两面：构造即写入（内部缓冲，永不失败）与静态 `format_to`（外部缓冲，可能失败返回 `nullopt`）：

[`构造函数与 format_to / view`](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/__support/integer_to_string.h#L469-L491)

下游消费者的典型用法可以看 `printf_core/int_converter.h`：它为 `printf` 的四种整数转换各定义一个类型别名，再用 `conv_name`（`'x'/'o'/'b'/'d'`）分派到对应的 `format_to`：

[`int_converter.h：IntegerToString 服务 printf 的 %x/%o/%b/%d`](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/__support/printf_core/int_converter.h#L29-L60)

#### 4.2.4 代码实践

**实践目标**：用 `IntegerToString` 把整数 `-42` 格式化回字符串，并验证结果。

**操作步骤**（源码阅读 + 在测试里验证）：

1. 阅读现有单测 [`integer_to_string_test.cpp`](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/test/src/__support/integer_to_string_test.cpp#L31-L45)，其中 `EXPECT` 宏的用法是：
   ```cpp
   // 示例代码（摘自测试文件的写法，非新代码）
   const IntegerToString<int> buffer(-42);
   EXPECT_EQ(buffer.view(), string_view("-42"));
   ```
2. 在本地（读者自己的副本中）仿照该宏，新增一行对 `-42` 的断言，确认 `view()` 返回 `"-42"`、`size()` 返回 `3`。
3. 再尝试十六进制：`IntegerToString<int, radix::Hex>` 把 `255` 写出应为 `"ff"`；加上 `::WithPrefix::Uppercase` 应为 `"0xFF"`（对照文件头注释的示例表）。

**需要观察的现象**：`view()` 是 `const &` 限定（`&&` 被显式 `delete`），即**不能从临时对象取视图**——因为视图指向对象内部的栈数组，临时对象一销毁视图就悬空。这是该 API 的一个重要安全约束。

**预期结果**：`-42 → "-42"`；`255`（Hex）→ `"ff"`。**待本地验证**：运行单测目标 `libc.test.src.__support.integer_to_string_test.__unit__`。

#### 4.2.5 小练习与答案

**练习 1**：`IntegerToString<int8_t>(-1)` 和 `IntegerToString<int8_t, radix::Hex>(-1)` 分别得到什么？为什么不同？

**答案**：分别是 `"-1"` 和 `"ff"`。十进制 `Fmt` 会处理符号（先取绝对值再补 `'-'`），而十六进制把值**按无符号解释**（`static_cast<UNSIGNED_T>(value)`），`-1` 的二补码 `0xFF` 直接写成 `"ff"`。依据见 [`write`](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/__support/integer_to_string.h#L428-L461) 中 `BASE==10` 与否则的两条分支。

**练习 2**：为什么 `IntegerToString` 要反向（从尾向头）写字符？

**答案**：因为「逐位提取」天然从低位到高位产出数字（`value % BASE` 得到最低位），与人阅读所需的高位在前顺序相反。反向写入器从缓冲末尾往前填，正好把最先产出的低位数字放到最右边，无需额外反转。

---

### 4.3 公共算法下沉：为什么这两个工具住在 __support

#### 4.3.1 概念说明

把 `strtointeger` 和 `IntegerToString` 放进 `__support`，不是随手为之，而是 u4-l1「私有标准库」哲学的直接落地。判断一个工具该不该进 `__support`，看两点：**它是否被多个入口点共享**、**它本身是否不对应任何公共头文件**。这两个工具同时满足：

- `strtointeger` 被 `atoi/atol/atoll/strtol/strtoll/strtoul/strtoull`、`strtoimax/strtoumax`、`wcstol` 一族，甚至 `scanf_core/parser.h`、`time/strftime_core/parser.h` 共用。
- `IntegerToString` 被 `printf_core/int_converter`、`CPP/stringstream`、`libc_assert`、`error_to_string`、`signal_to_string`、`FPUtil/fpbits_str`、`time/strftime` 的数字转换器共用。

它们都通过 `add_header_library` 注册为**内部头文件库**——不产生公开 C 符号、仅供其它目标 `DEPENDS` 引用。

#### 4.3.2 核心流程

「下沉」带来三个好处，构成一条清晰的设计闭环：

```
多个入口点需要同一份逻辑
        │
        ▼
把逻辑抽成 __support 下的模板/类（add_header_library）
        │
        ▼
入口点变成薄壳：只做参数适配 + errno/str_end 设置
        │
        ▼
受益：① 一处实现、处处复用  ② 内部可用现代 C++ ③ 接口可被 libc++ 等外部共享
```

特别地，`str_to_integer.h` / `str_to_num_result.h` 顶部都带 `**** WARNING ****` 注释，声明该接口**与 libc++ 共享**。这意味着这套「下沉」甚至跨越了子项目边界——这是算法下沉原则走到极致的体现。

#### 4.3.3 源码精读

两个工具在 `__support/CMakeLists.txt` 中都以 `add_header_library` 声明，并把各自依赖的其它内部头（如 `ctype_utils`、`CPP/limits`）通过 `DEPENDS` 串起来：

[`str_to_integer 注册为内部头文件库，依赖 ctype_utils 等`](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/__support/CMakeLists.txt#L201-L213)

[`integer_to_string 注册为内部头文件库`](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/__support/CMakeLists.txt#L215-L228)

入口点侧则用 `DEPENDS` 引用它们（u2-l3 介绍的依赖传播机制），例如 `src/stdlib/CMakeLists.txt` 中：

```cmake
# 示例代码（摘自 src/stdlib/CMakeLists.txt）
add_entrypoint_object(
  atoi
  SRCS atoi.cpp
  HDRS atoi.h
  DEPENDS
    libc.src.errno.errno
    libc.src.__support.str_to_integer   # ← 引用下沉后的公共算法
)
```

#### 4.3.4 代码实践

**实践目标**：亲手验证「公共算法下沉」的消费者版图。

**操作步骤**：

1. 在仓库 `libc/` 根下执行（只读检索）：
   ```
   grep -rl "str_to_integer.h" src/ | sort
   grep -rl "integer_to_string.h" src/ | sort
   ```
2. 把命中结果按「入口点目录」归类，你会看到 `str_to_integer.h` 同时出现在 `src/stdlib`、`src/inttypes`、`src/wchar`、`src/stdio/scanf_core`、`src/time/strftime_core`；`integer_to_string.h` 出现在 `src/__support/printf_core`、`src/__support/CPP`、`src/__support/StringUtil`、`src/time/strftime_core` 等处。
3. 任选一处非 stdlib 的消费者（例如 `src/__support/printf_core/int_converter.h`），确认它正是 4.2 里看到的 `IntegerToString` 用法。

**需要观察的现象**：同一个工具被「字符串解析类」「宽字符类」「scanf 解析类」「时间格式化类」等彼此无关的功能复用——这正是把它从任何一个具体入口点里抽出来的理由。

**预期结果**：两个头文件的消费者都跨越多个子目录。**待本地验证**（命令依赖本地源码树）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `strtointeger` 用 `add_header_library` 而不是 `add_entrypoint_object`？

**答案**：因为它不对应任何公共 C 函数、不产生公开符号，只是被多个入口点共享的内部模板。`add_header_library` 声明的是**内部头文件库**，只参与头文件路径与编译选项的传播，正符合「私有标准库」的定位（u4-l1）。

**练习 2**：`str_to_integer.h` 顶部的 `WARNING` 说它「与 libc++ 共享」，这对修改它的人意味着什么？

**答案**：意味着改动必须同时照顾 libc 和 libc++ 两个使用方，且要保证能为 libc++ 的所有目标构建通过；这是一份「公共算法下沉到跨项目复用」的契约，修改门槛因此更高。

---

## 5. 综合实践

**任务**：把字符串 `"-42"` 解析成整数，再用同一套 `__support` 工具把它格式化回字符串，验证往返一致。这个任务串起了本讲的两个方向（解析与格式化），并体现「公共算法下沉」。

**参考思路**（源码阅读型，给出可读的最小调用骨架；下列代码为说明用「示例代码」，非项目原有文件）：

```cpp
// 示例代码：仅供说明调用方式，需在 libc 内部构建环境中编译
#include "src/__support/str_to_integer.h"
#include "src/__support/integer_to_string.h"
using LIBC_NAMESPACE::internal::strtointeger;
using LIBC_NAMESPACE::IntegerToString;

// 1) 字符串 -> 整数
auto res = strtointeger<long>("  -42", 10);   // 注意前导空白，验证 first_non_whitespace
long n = res.value;                            // n == -42
// res.parsed_len == 5（含两个前导空白 + '-' + 两位数字）

// 2) 整数 -> 字符串
IntegerToString<long> buf(n);
// buf.view() == "-42"，buf.size() == 3
```

**完成后请回答**：

1. 把输入改成 `"   -42abc"`，`res.value` 与 `res.parsed_len` 分别是多少？为什么 `abc` 不影响值？（提示：主循环在 `isalnum` 为假或 `cur_digit >= base` 时停止。）
2. 把 `IntegerToString<long>(n)` 换成 `IntegerToString<long, radix::Hex>`，`-42` 会变成什么？为什么不再是 `"-2a"`？（提示：回顾 4.2.5 练习 1。）
3. 这两步分别经过哪些 `__support` 头文件？它们各自的 CMake 目标名是什么？（提示：`libc.src.__support.str_to_integer`、`libc.src.__support.integer_to_string`。）

**预期结果**：第 1 步返回 `-42`、`parsed_len` 指向 `abc` 之前；第 2 步得到 `"-42"`。**待本地验证**（需在 libc 内部环境编译运行）。

## 6. 本讲小结

- `atoi/strtol/strtoul` 等一串入口点都是**薄壳**，真正的字符串→整数逻辑统一在 `__support/str_to_integer.h` 的 `strtointeger<T>` 模板里，靠模板实参区分返回类型与有/无符号。
- `strtointeger` 用**两步阈值预检查**（`abs_max_div_by_base` 与 `abs_max - digit`）在溢出**发生之前**就检测到它，并在无符号域里处理有符号类型的不对称（\(|T_{\min}| = |T_{\max}|+1\)）。
- 结果类型 `StrToNumResult<T>` 同时携带值、错误码、已解析长度，让入口点据此设置 `errno` 与 `str_end`；一旦饱和，循环继续推进指针以保证 `str_end` 指向整段数字末尾。
- 反向的整数→字符串由 `__support/integer_to_string.h` 的 `IntegerToString<T, Fmt>` 承担，用 `radix::Dec/Hex/Oct/Bin` 加嵌套导航（`WithPrefix/WithWidth/Uppercase/WithSign`）表达排版，栈上定长缓冲保证构造永不失败。
- `IntegerToString` 采用**反向写入**适配「从低位到高位」的提取顺序，并用 `bit_cast` 解决 `INT_MIN` 取负的未定义行为。
- 两个工具都以 `add_header_library` 注册为**内部头文件库**，被 stdlib/inttypes/wchar/printf/scanf/strftime 等多处复用——这就是「公共算法下沉」原则的具体落地，`str_to_integer.h` 更是跨项目与 libc++ 共享。

## 7. 下一步学习建议

- **继续向格式化 I/O 深入**：本讲的 `IntegerToString` 是 `printf` 的基础构件，下一讲 [u7-l2 printf_core 架构] 将讲解 `__support/printf_core` 如何把 parser/writer/converter 三段拆开，其中 `int_converter.h` 正是本讲 `IntegerToString` 的直接消费者。
- **阅读更完整的消费者**：挑一个非 stdlib 的下游（如 `src/stdio/scanf_core/parser.h` 对 `strtointeger` 的使用，或 `src/__support/StringUtil/error_to_string.cpp` 对 `IntegerToString` 的使用），体会「薄壳入口点 + __support 算法」如何在不同子系统里反复出现。
- **回顾设计主线**：若想巩固「公共算法下沉」的整体认知，可回到 u4-l1（`__support` 总览）与本讲对照，把 ctype_utils / str_to_integer / integer_to_string 串成「字符级原子 → 方向性转换工具 → 入口点薄壳」的三层图。
