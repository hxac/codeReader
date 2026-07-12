# 表达式词法分析

## 1. 本讲目标

SDB（简单调试器）很多命令都需要接受「表达式」作为参数，例如 `p $a0 + 4` 打印寄存器加偏移的值、`x 10 *0x80000000` 查看某地址附近的内存。要让这些命令工作，NEMU 必须先把一串人类可读的字符「切成」一个个有意义的片段。这一步就叫**词法分析（lexing）**。

本讲聚焦 `src/monitor/sdb/expr.c` 里的词法分析部分。学完本讲你应该能够：

- 说清楚「词法分析」要解决什么问题，它的输入输出是什么。
- 读懂 `rules` 规则表驱动的设计，并知道如何新增一条词法规则。
- 解释 `init_regex` 为什么要在使用前把正则「预编译」一次。
- 描述 `make_token` 的逐字符扫描循环，并能正确地把识别到的 token 记录进 `tokens[]` 数组。
- 理解 `TK_NOTYPE = 256` 这个看似奇怪的起点背后的设计意图。

本讲只做「切词」，不计算结果。如何由 token 求出表达式的值是下一讲 `u2-l7`（递归下降求值）的内容。

## 2. 前置知识

在进入源码前，先建立几个直觉。

### 2.1 什么是 token

考虑表达式字符串 `"$a0 + 0x10"`。对人来说一眼就能看出它由 5 个有意义的片段组成：

| 片段 | 含义 |
|------|------|
| `$a0` | 寄存器名 |
| ` `（空格） | 无意义，可忽略 |
| `+` | 加号运算符 |
| ` `（空格） | 无意义 |
| `0x10` | 十六进制数字 |

每一个这样的片段就叫一个 **token（记号）**。词法分析的任务，就是从左到右扫描字符串，把它切成一串 token。这是编译器前端「扫描器（scanner）」做的事，也是 `p`/`x` 等调试命令解析参数的第一步。

### 2.2 用正则描述「什么样的串是一个 token」

每种 token 都有一个可描述的字符模式，而**正则表达式**正是描述字符模式的工具。例如：

- 十进制数字：`[0-9]+`
- 十六进制数字：`0x[0-9a-fA-F]+`
- 空白：` +`
- 等号：`==`

NEMU 借用 POSIX 标准库 `<regex.h>` 提供的正则引擎来做匹配，这样我们只需「声明」每种 token 长什么样，不必自己写状态机。

### 2.3 POSIX 正则三件套

理解本讲源码只需三个库函数：

- `regcomp(&re, pattern, flags)`：把字符串形式的正则 `pattern` **编译**成一个内部状态机 `re`（类型 `regex_t`）。编译是较重的操作。
- `regexec(&re, str, nmatch, pmatch, flags)`：用编译好的 `re` 在 `str` 上尝试匹配。匹配成功返回 0，并把匹配区间写进 `pmatch`（类型 `regmatch_t`）。
- `regmatch_t`：`rm_so` 是匹配起点的偏移（start offset），`rm_eo` 是终点的偏移（end offset）。当匹配发生在字符串开头时 `rm_so == 0`，`rm_eo` 正好等于匹配的长度。

### 2.4 与上一讲的衔接

上一讲 `u2-l5` 我们建立了 SDB 的命令框架：`cmd_table` 表驱动分发、`sdb_mainloop` 用 `strtok` 切出命令名。本讲是对「命令的参数」做更精细的解析——`strtok` 只能按空格切，而表达式里 `(a+b)*c` 是不能有空格的，必须靠真正的词法分析器。本讲的产物（`tokens[]` 数组）会直接喂给下一讲 `u2-l7` 的求值器。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [src/monitor/sdb/expr.c](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/monitor/sdb/expr.c) | 本讲主角。词法分析（`rules`/`init_regex`/`make_token`）与求值入口 `expr()` 都在这里，目前大量是留给学生实现的 TODO 脚手架。 |
| [src/monitor/sdb/sdb.h](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/monitor/sdb/sdb.h) | 声明对外接口 `word_t expr(char *e, bool *success)`。 |
| [src/monitor/sdb/sdb.c](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/monitor/sdb/sdb.c) | `init_sdb()` 在这里调用 `init_regex()` 完成规则预编译（第 139 行）。 |
| [include/debug.h](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/include/debug.h) | 定义 `Log`、`panic`、`TODO` 宏；本讲里 `make_token` 用 `Log` 打印每个匹配，未实现处用 `TODO()`。 |
| [include/macro.h](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/include/macro.h) | `ARRLEN` 宏用于由 `rules[]` 反推规则条数 `NR_REGEX`。 |

## 4. 核心概念与源码讲解

本讲按四个最小模块推进：先认识切词的产物 **Token**，再看声明规则的 **rules 表**，接着是预编译的 **init_regex**，最后是真正干活的扫描循环 **make_token**。

### 4.1 词法分析的产物：Token 结构与类型空间

#### 4.1.1 概念说明

词法分析的产出是一串 token。要描述一个 token，需要两样东西：

1. **类型（type）**：它是数字、运算符、寄存器名，还是空格？
2. **字面值（str）**：对数字和寄存器名这类 token，光知道「它是数字」不够，还得把 `0x10` 这个字符串记下来，后面求值时才能转成数值；而对 `+`、`-` 这类运算符，类型本身已经说明了含义，不需要额外存字面值。

NEMU 用一个很小的结构体来表达：

```c
typedef struct token {
  int type;
  char str[32];
} Token;
```

同时用一个静态数组收集所有切出来的 token，并用一个计数器记录数量：

```c
static Token tokens[32] __attribute__((used)) = {};
static int nr_token __attribute__((used)) = 0;
```

`tokens[32]` 表示一条表达式最多切成 32 个 token，`nr_token` 是当前实际切出的个数。`__attribute__((used))` 告诉编译器「别因为我现在没人用就把这两个变量优化掉」——它们是留给学生填写的全局状态，早期版本里确实可能暂时没被引用。

#### 4.1.2 核心流程

把表达式切成 token 后，数据是这样的（以 `"$a0 + 4"` 为例，假设你已实现了相关规则）：

```
tokens[0] = { type=TK_REG,  str="$a0" }
tokens[1] = { type='+' ,    str=""   }   // 运算符不存字面值
tokens[2] = { type=TK_DEC,  str="4"  }
nr_token  = 3
```

注意几个设计要点：

- **空格不进表**：`$a0` 与 `+` 之间的空格被识别为 `TK_NOTYPE`，记录时直接跳过、不占 `tokens` 槽位。
- **类型用 `int`**：单个字符运算符直接拿它的 ASCII 值当类型（`'+'` 即 43），省去为每个运算符起名的麻烦。这也解释了下面 4.1.3 要讲的「为什么 enum 从 256 起」。
- **str 容量 32**：足够装下一个 64 位十六进制数或寄存器名，但若用户输入一个超长数字会有截断风险（见本节练习）。

#### 4.1.3 源码精读

Token 类型用枚举定义，目前只有两个种子值（[expr.c:L23-L28](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/monitor/sdb/expr.c#L23-L28)）：

```c
enum {
  TK_NOTYPE = 256, TK_EQ,
  /* TODO: Add more token types */
};
```

这里把第一个值显式设成 **256** 是有意为之：运算符 `+ - * / ( )` 这些单字符 token 直接用字符常量（0–255 范围）当 type，而 `TK_NOTYPE`、`TK_EQ`、`TK_DEC` 这类「多个字符才表示得清楚」的 token，类型号必须从 256 起步，才不会和某个 ASCII 字符撞车。`TK_EQ` 没有显式赋值，会自动取 257。学生扩展时新增的 `TK_DEC`/`TK_HEX`/`TK_REG` 等会接着往下排：258、259、260……

> 小知识：`TK_NOTYPE` 专门留给「空格」——它被识别、却不该进入 `tokens`，在 `make_token` 的 switch 里它对应的分支什么也不做。

Token 结构与全局数组定义在 [expr.c:L65-L71](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/monitor/sdb/expr.c#L65-L71)，`str[32]` 这个尺寸直接决定了「一个数字 token 最多能存多少位字符」。

#### 4.1.4 代码实践

**实践目标**：直观感受 `tokens[]` 与 `nr_token` 这对「数组 + 计数器」如何承载切词结果，并体会 `str[32]` 的容量限制。

**操作步骤（源码阅读 + 推理）**：

1. 打开 [expr.c:L65-L71](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/monitor/sdb/expr.c#L65-L71)，确认 `tokens` 最多容纳 32 个 token。
2. 对表达式 `"1 + (2 * 3)"` 手工数一下 token 个数（提示：注意空格不进表），判断是否会超过 32 的上限。
3. 考虑一个边界：若用户输入一个 40 位的十进制数（理论上 128 位整数），它被切成 1 个 token，但字面值要塞进 `str[32]`。讨论：直接用 `strcpy` 会发生什么？应该用什么方式安全地拷贝？

**需要观察的现象 / 预期结果**：

- `"1 + (2 * 3)"` 含数字 3 个、运算符 `+` `*` 2 个、括号 2 个，共 7 个 token，远未超限。
- 对超长数字，`str[32]` 容纳不下 40 个字符 + 结尾 `\0`；应使用带长度限制的 `strncpy(tokens[nr_token].str, substr_start, substr_len)` 并手动补 `\0`，或在拷贝前判断 `substr_len < sizeof(tokens[nr_token].str)`，避免缓冲区溢出。**待本地验证**：你可以在 4.4 节实现完拷贝逻辑后，故意输入超长数字观察是否被正确截断或报错。

#### 4.1.5 小练习与答案

**练习 1**：为什么把 `TK_NOTYPE` 设为 256，而不是 0、1、2 这样的小数字？

**答案**：因为单字符运算符 token 直接用 ASCII 值（0–255）当类型，例如 `'+'` 是 43、`'*'` 是 42。若 `TK_NOTYPE` 取小数字就会与某个运算符冲突。从 256 起步保证所有「多字符 token」的类型号落在 ASCII 区间之外，互不干扰。

**练习 2**：`tokens` 数组大小为 32，如果一条表达式切出了 33 个 token 会怎样？

**答案**：会发生数组越界写。健壮的做法是在写入 `tokens[nr_token]` 前检查 `nr_token < ARRLEN(tokens)`，越界时返回 `false` 表示表达式过长、词法失败（这会让 `expr` 把 `*success` 置 `false`）。

---

### 4.2 rules 规则表：声明式地描述词法

#### 4.2.1 概念说明

如何告诉扫描器「数字长什么样、空格长什么样、等号长什么样」？NEMU 采用了**表驱动（table-driven）**的设计：把每条词法规则写成一行 `{ 正则字符串, token 类型 }`，堆在一张 `rules[]` 表里。扫描时依次用每条规则去试，谁先匹配上就用谁。

这种「数据即逻辑」的好处是：**新增一种 token，只需往表里加一行，扫描循环一行都不用改**。这与上一讲 `cmd_table` 的表驱动思想完全一致。

#### 4.2.2 核心流程

`rules[]` 表的骨架（[expr.c:L30-L42](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/monitor/sdb/expr.c#L30-L42)）：

```c
static struct rule {
  const char *regex;     // 正则字符串
  int token_type;        // 匹配成功时该 token 的类型
} rules[] = {
  {" +", TK_NOTYPE},     // 空格
  {"\\+", '+'},          // 加号（+号在扩展正则里要转义？见下）
  {"==", TK_EQ},         // 等号
};
```

两个关键点要记牢：

1. **规则有先后，先到先得**。`make_token` 会按数组下标从小到大逐条试，**第一条能在当前位置匹配的规则胜出**并立即 `break`。因此顺序很重要：当两条规则存在「前缀冲突」时，更长/更具体的必须排前面。最典型的例子是 `==`（`TK_EQ`）与若日后加入的 `=`（赋值）——`==` 必须排在 `=` 之前，否则 `==` 会被当成两个 `=`。同理，十六进制 `0x[0-9a-fA-F]+` 必须排在十进制 `[0-9]+` 之前，否则 `0x1f` 会被十进制规则先吃掉一个 `0`。

2. **`+` 号为何写成 `\\+`**。C 字符串里 `\\` 先变成一个 `\`，传给正则引擎的是 `\+`；在扩展正则（`REG_EXTENDED`）里 `+` 是「前一元素重复 1 次以上」的元字符，要表示字面的加号必须转义成 `\+`。`*`、`(`、`)` 同理需要转义。

> 关于转义的细节容易绕晕。规则是「先过 C 字符串这一关，再过正则引擎这一关」。想要正则里的 `\+`，C 源码里就得写 `"\\+"`。

#### 4.2.3 源码精读

规则结构体与表的定义见 [expr.c:L30-L42](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/monitor/sdb/expr.c#L30-L42)。表后面紧跟一个由表长反推常量的宏（[expr.c:L44-L46](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/monitor/sdb/expr.c#L44-L46)）：

```c
#define NR_REGEX ARRLEN(rules)
static regex_t re[NR_REGEX] = {};
```

`ARRLEN` 来自 [include/macro.h:L29](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/include/macro.h#L29)，即 `(int)(sizeof(arr)/sizeof(arr[0]))`。这样无论你往 `rules[]` 加多少行，`NR_REGEX` 与 `re[]` 数组都自动跟着变，**不需要手动维护「规则条数」这个魔法数字**。

`re[NR_REGEX]` 是与规则一一对应的「编译后的状态机」数组，初始值 `{}` 全零，等待 `init_regex` 填充（见 4.3）。

#### 4.2.4 代码实践

**实践目标**：动手往 `rules[]` 加几条规则，体会「加一行就能识别新 token」。

**操作步骤**：

1. 先在 4.1.3 提到的枚举里补上新的类型（[expr.c:L23-L28](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/monitor/sdb/expr.c#L23-L28)）：

   ```c
   enum {
     TK_NOTYPE = 256, TK_EQ,
     TK_DEC,            // 十进制数
     TK_HEX,            // 十六进制数 0x...
     TK_REG,            // 寄存器名 $...
   };
   ```

2. 按下面的顺序往 `rules[]` 添加（**顺序很关键**，注意十六进制在十进制之前）：

   ```c
   {" +", TK_NOTYPE},                      // 空格（已有）
   {"\\(", '('},                           // 左括号
   {"\\)", ')'},                           // 右括号
   {"\\+", '+'},                           // 加（已有）
   {"-", '-'},                             // 减
   {"\\*", '*'},                           // 乘（兼作解引用，见 u2-l7）
   {"/", '/'},                             // 除
   {"==", TK_EQ},                          // 等号（已有）
   {"0[xX][0-9a-fA-F]+", TK_HEX},          // 十六进制，必须在十进制之前
   {"[0-9]+", TK_DEC},                     // 十进制
   {"\\$[a-zA-Z0-9]+", TK_REG},            // 寄存器名，如 $a0 $0 $pc
   ```

3. 暂时**不要**改 `make_token` 的 switch（它仍是 `TODO()`），先编译看看 `init_regex` 是否能把你新加的正则全部编译通过。

**需要观察的现象 / 预期结果**：

- 编译应能通过。运行 NEMU 时 `init_sdb()` → `init_regex()` 会预编译所有规则；如果某条正则语法错误，`regcomp` 会返回非 0，`init_regex` 会调用 `panic` 打印 `regex compilation failed` 并终止（见 4.3.3）。若看到该错误，多半是转义层数写错了，按「C 字符串 → 正则」两道关排查。
- 因 switch 仍是 `TODO()`，此刻还无法真正跑通切词，**真正观察 token 输出放在第 5 节综合实践**。

#### 4.2.5 小练习与答案

**练习 1**：如果把 `{"[0-9]+", TK_DEC}` 放在 `{"0[xX][0-9a-fA-F]+", TK_HEX}` **之前**，对输入 `"0x1f"` 会发生什么？

**答案**：十进制规则先匹配，吃掉开头的 `0`，得到一个值为 0 的 `TK_DEC` token；接下来扫描位置落在 `x`，没有规则能匹配 `x`，`make_token` 会打印 `no match at position ...` 并返回 `false`。所以十六进制规则必须排在十进制之前。

**练习 2**：为什么 `+`、`(`、`*` 要写成 `\\+`、`\\(`、`\\*`，而 `-`、`/` 不用转义？

**答案**：在 `REG_EXTENDED` 扩展正则里，`+`、`(`、`)`、`*` 都是元字符（分别表示重复、分组、重复 0 次以上），要匹配字面字符必须用 `\` 转义；而 `-`、`/` 不是元字符，无需转义。再叠上 C 字符串层，`\+` 要写成 `"\\+"`。

---

### 4.3 init_regex：把规则预编译成状态机

#### 4.3.1 概念说明

正则编译（`regcomp`）是把字符串形式的模式翻译成内部状态机的较重操作。而词法规则在程序整个生命周期里是**不变**的，却可能被匹配成千上万次（每输入一个表达式就扫一遍）。合理的做法是：**只编译一次，重复使用**。

`init_regex` 正是干这件事的「一次性预编译器」：它在程序启动早期把 `rules[]` 里每条正则都编译进对应的 `re[i]`，之后 `make_token` 直接拿编译好的 `re[i]` 去匹配，省掉重复编译开销。

#### 4.3.2 核心流程

```text
init_regex():
  对 i = 0 .. NR_REGEX-1:
    regcomp(&re[i], rules[i].regex, REG_EXTENDED)
    若失败 → regerror 取错误信息 → panic 终止
```

用伪代码表示就是「遍历规则表，逐条编译，遇错即停」。注意它用 `REG_EXTENDED` 标志，表示采用**扩展正则语法**（这是上面 4.2 讨论 `+`、`(`、`|` 等元字符转义规则的前提）。

调用时机很关键：`init_regex` 必须在**任何一次** `make_token` 之前完成。NEMU 把它放在 `init_sdb()` 里，而 `init_sdb()` 又在 `init_monitor()` 初始化链中较早执行，保证用户在 SDB 里输入第一个表达式时所有规则早已编译好。

#### 4.3.3 源码精读

预编译函数见 [expr.c:L51-L63](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/monitor/sdb/expr.c#L51-L63)：

```c
void init_regex() {
  int i;
  char error_msg[128];
  int ret;
  for (i = 0; i < NR_REGEX; i ++) {
    ret = regcomp(&re[i], rules[i].regex, REG_EXTENDED);
    if (ret != 0) {
      regerror(ret, &re[i], error_msg, 128);
      panic("regex compilation failed: %s\n%s", error_msg, rules[i].regex);
    }
  }
}
```

要点：

- `regcomp` 成功返回 0，失败返回非 0 的错误码。失败时 `regerror` 把错误码翻译成人话写进 `error_msg`，再交给 `panic`。
- `panic` 定义在 [include/debug.h:L39](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/include/debug.h#L39)，本质是 `Assert(0, ...)`，会打印红色错误信息并终止程序。所以**正则写错会导致 NEMU 启动即崩**，且错误信息里会带上出问题的模式串，方便定位。

调用点在 [src/monitor/sdb/sdb.c:L137-L143](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/monitor/sdb/sdb.c#L137-L143)：

```c
void init_sdb() {
  init_regex();     // 第 139 行：编译正则
  init_wp_pool();   // 初始化监视点池（u2-l8）
}
```

#### 4.3.4 代码实践

**实践目标**：亲手触发一次「正则编译失败」，看清 `init_regex` 的错误诊断输出，体会「编译期就把语法错误挡在门外」的好处。

**操作步骤**：

1. 临时把 `rules[]` 里某条正则改坏，例如把 `{" +", TK_NOTYPE}` 改成 `{"[0-", TK_NOTYPE}`（一个未闭合的字符类）。
2. 重新 `make` 编译并运行 NEMU。
3. 观察终端输出后，**务必把正则改回正确形态**（本讲禁止改源码成品，这里只是为了观察）。

**需要观察的现象 / 预期结果**：

- 程序在 `init_regex` 处 `panic`，红色输出形如 `regex compilation failed: ...`，并附上出问题的模式串 `[0-` 与文件行号。这说明语法错误在「预编译」阶段就被发现，而不会拖到运行匹配时才暴露。

**待本地验证**：不同 glibc 版本的 `regerror` 文案略有差异，但一定会终止程序。

#### 4.3.5 小练习与答案

**练习 1**：如果把 `init_regex()` 的调用从 `init_sdb()` 里删掉，直接运行 SDB 输入 `p 1+2`，会发生什么？

**答案**：`re[]` 全是零初始化的 `regex_t`，从未被 `regcomp` 编译。随后 `make_token` 调 `regexec(&re[i], ...)` 对未编译的状态机匹配，行为未定义，通常会返回错误码或直接崩溃。这正说明「预编译」是必须的前置步骤。

**练习 2**：为什么把编译放进 `init_regex` 一次性完成，而不是在 `make_token` 里每条规则现用现编？

**答案**：因为规则集合在整个运行期不变，重复编译同一批正则是浪费。预编译一次、反复匹配，把昂贵的「翻译」摊到启动时一次性付清，后续每次切词只付出廉价的 `regexec` 匹配成本。

---

### 4.4 make_token：逐字符扫描并记录 token

#### 4.4.1 概念说明

`make_token(char *e)` 是词法分析的核心。它接收一个表达式字符串 `e`，从左到右扫描，在每个位置依次试用所有规则，识别出一个 token 就推进位置、记录下来，直到字符串末尾。若某个位置所有规则都匹配不上，说明出现了非法字符，返回 `false`。

它解决的问题是：把「一维字符串」变成「token 序列」这个后续求值器能消费的结构化输入。

#### 4.4.2 核心流程

`make_token` 的主循环骨架（伪代码）：

```text
make_token(e):
  position = 0
  nr_token = 0
  当 e[position] != '\0':
    matched = false
    对 i = 0 .. NR_REGEX-1:               # 依次试每条规则
      若 regexec(re[i], e+position) 成功 且 匹配起点就在当前位置(rm_so==0):
        matched = true
        substr_len = rm_eo                 # 从开头匹配时，rm_eo 即匹配长度
        position += substr_len             # 推进扫描位置
        根据 rules[i].token_type 记录 token:
          - TK_NOTYPE(空格): 什么都不做（不占 tokens 槽）
          - 数字/寄存器: 把字面子串拷进 tokens[nr_token].str，type 字段赋值，nr_token++
          - 运算符/括号: 只设 type，str 可留空，nr_token++
        break                              # 当前位置只取第一条命中
    若没匹配到任何规则:
      打印 "no match at position ..."
      return false
  return true
```

两个最精妙、也最易看漏的细节：

1. **`rm_so == 0` 锚定当前位置**。`regexec` 默认会在 `e+position` 这个子串里**搜索**任意位置的匹配（不一定从开头）。但词法分析要求「从当前位置开始」匹配，所以必须额外检查 `pmatch.rm_so == 0`，确保命中起点就是子串开头。没有这个判断，扫描器会允许「跳过非法字符」继续匹配，掩盖错误。

2. **`substr_len = pmatch.rm_eo`**。当 `rm_so == 0` 时，匹配区间的终点偏移 `rm_eo` 恰好等于匹配串的长度，因此直接用它作为推进步长。

#### 4.4.3 源码精读

完整函数见 [expr.c:L73-L112](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/monitor/sdb/expr.c#L73-L112)。核心匹配与推进这段（[expr.c:L80-L102](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/monitor/sdb/expr.c#L80-L102)）：

```c
while (e[position] != '\0') {
  for (i = 0; i < NR_REGEX; i ++) {
    if (regexec(&re[i], e + position, 1, &pmatch, 0) == 0 && pmatch.rm_so == 0) {
      char *substr_start = e + position;
      int substr_len = pmatch.rm_eo;
      Log("match rules[%d] = \"%s\" at position %d with len %d: %.*s",
          i, rules[i].regex, position, substr_len, substr_len, substr_start);
      position += substr_len;
      /* TODO: 把识别到的 token 记录进 tokens[] */
      switch (rules[i].token_type) {
        default: TODO();
      }
      break;
    }
  }
  if (i == NR_REGEX) {                     // 所有规则都没命中
    printf("no match at position %d\n%s\n%*.s^\n", position, e, position, "");
    return false;
  }
}
```

逐行解读：

- 第 83 行：`regexec` 第 2 个参数是 `e + position`，即「从当前位置开始的子串」；第 4 个参数 `&pmatch` 接收匹配区间；`== 0` 且 `pmatch.rm_so == 0` 两个条件合起来保证「从当前位置开头的匹配」。
- 第 84–85 行：`substr_start` 指向命中起点，`substr_len = pmatch.rm_eo` 取命中长度。
- 第 87–88 行：现成的 `Log` 调试输出，会把每一个识别到的 token 打印到终端（`Log` → `_Log` → `printf`，见 [include/utils.h:L70-L74](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/include/utils.h#L70-L74)）。**这是后续观察切词结果的关键窗口**。
- 第 90 行：推进扫描位置。
- 第 97–99 行：留空的 switch，目前 `default: TODO()` 会直接 `panic`，需要你按 token 类型补全记录逻辑。
- 第 105–108 行：`if (i == NR_REGEX)`——只有当内层 for 循环**一次都没 break**（即 `i` 走到了 `NR_REGEX`）时才成立，表示当前位置无规则可匹配，是非法字符。`printf` 用 `%*.s^` 在错误位置正下方打印一个 `^` 指示符，非常直观。

入口函数 `expr` 调用 `make_token` 并据此决定成败（[expr.c:L115-L125](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/monitor/sdb/expr.c#L115-L125)）：词法失败就把 `*success` 置 `false` 并返回 0；词法成功则进入（下一讲的）求值阶段。

#### 4.4.4 代码实践

**实践目标**：补全 `make_token` 里 switch 的记录逻辑，让 `tokens[]` 真正装满切好的 token。

**操作步骤**：

1. 在 [expr.c:L97-L99](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/monitor/sdb/expr.c#L97-L99) 的 switch 中替换 `default: TODO();`，参考实现：

   ```c
   switch (rules[i].token_type) {
     case TK_NOTYPE:   /* 空格：跳过，不入表 */
       break;
     case TK_DEC:
     case TK_HEX:
     case TK_REG:
       /* 带字面值的 token：把子串拷进 str，注意限长 */
       tokens[nr_token].type = rules[i].token_type;
       Assert(substr_len < sizeof(tokens[nr_token].str),
              "token string is too long");
       strncpy(tokens[nr_token].str, substr_start, substr_len);
       tokens[nr_token].str[substr_len] = '\0';
       nr_token++;
       break;
     default:          /* 单字符运算符/括号：类型即其 ASCII */
       tokens[nr_token].type = rules[i].token_type;
       nr_token++;
       break;
   }
   ```

   注意：`Assert`、`strncpy` 需要头文件已包含（`debug.h` 经 `isa.h`→`common.h` 链路已可用；`string.h` 在 `common.h` 已包含）。`strncpy` 不保证补 `\0`，所以手动补结尾。

2. 暂时**不要**实现 `expr()` 里的求值（那是 u2-l7）。保留它本来的 `TODO()` 即可——但这样 `expr` 仍会 panic，无法端到端跑通。要观察切词，请用第 5 节综合实践里的临时命令。

**需要观察的现象 / 预期结果**：

- 编译通过后，`make_token` 自身不再 panic。
- 真正观察 token 需要触发 `expr()`，做法见第 5 节综合实践。

#### 4.4.5 小练习与答案

**练习 1**：如果删掉条件里的 `&& pmatch.rm_so == 0`，对输入 `"1 @ 2"`（`@` 是非法字符）会发生什么？

**答案**：`regexec` 会允许「跳过 `@`」后在后面的 `2` 处匹配数字规则，于是 `@` 被悄悄跳过而不报错，最终把非法表达式误判为合法。`rm_so == 0` 强制匹配必须从当前位置开头发生，从而正确地在 `@` 处停下并报 `no match`。

**练习 2**：`if (i == NR_REGEX)` 这个「无匹配」判断为什么成立？把它写成 `if (!matched)` 引入一个布尔变量是否等价？

**答案**：内层 for 循环只有在「没有任何规则命中」时才会自然走完，使 `i` 自增到 `NR_REGEX`；一旦命中就 `break`，`i` 必然小于 `NR_REGEX`。所以 `i == NR_REGEX` 等价于「一整轮都没匹配」。引入 `bool matched = false;` 在命中时置 true、循环后判断 `if (!matched)` 是完全等价、且可读性更好的写法，二者都对。

---

## 5. 综合实践

把本讲的四个模块串起来，完成「表达式切词器」并真正观察它的输出。

### 实践任务

1. **扩展枚举与规则表**（模块 4.1 + 4.2）：按 4.2.4 新增 `TK_DEC`/`TK_HEX`/`TK_REG` 及 `+ - * / ( ) ==` 等规则，注意排列顺序。
2. **补全记录逻辑**（模块 4.4）：按 4.4.4 填好 `make_token` 的 switch。
3. **临时挂一个命令触发切词**：因为目前 `expr()` 还没人调用、`p` 命令也未实现，为了观察切词，临时在 `src/monitor/sdb/sdb.c` 的 `cmd_table` 里加一条 `p` 命令（属于学生练习范畴，本讲仅用于观察词法）：

   ```c
   static int cmd_p(char *args) {
     bool success = true;
     word_t val = expr(args, &success);
     if (!success) { printf("Bad expression\n"); return 0; }
     printf("%u (0x%08x)\n", (unsigned)val, (unsigned)val);  // 值在 u2-l7 后才正确
     return 0;
   }
   /* 在 cmd_table 里加：{ "p", "Evaluate an expression", cmd_p }, */
   ```
   并在 `expr()` 中**临时**把 `TODO();` 改成 `return 0;`（仅为单独验证词法；求值留给 u2-l7）。

4. **编译运行并观察**：`make && ./build/riscv32-nemu-interpreter`（二进制名随 ISA/引擎而变），在 SDB 提示符里输入：

   ```
   (nemu) p 0x10 + 5 * ($a0 + 2)
   ```

### 需要观察的现象

- 由于 `make_token` 里有现成的 `Log`，终端会逐行打印每个识别到的 token，形如：

  ```
  match rules[...] = "0[xX][0-9a-fA-F]+" at position 0 with len 4: 0x10
  match rules[...] = " +" at position 4 with len 1: (空格)
  match rules[...] = "\+" at position 6 with len 1: +
  ...
  ```

- 空格会被识别为 `TK_NOTYPE` 但不进 `tokens`（你可加一句 `Log` 验证 `nr_token`）。
- 输入非法字符（如 `p 1 @ 2`）应看到 `no match at position ...` 与一个 `^` 指向出错位置，命令打印 `Bad expression`。

### 预期结果

- 切词器能正确识别十进制、十六进制、寄存器名、四则运算符与括号；空格被忽略；非法字符被准确定位。
- 打印出的「值」此刻是占位的 0（因为求值未实现），属正常现象——这正是下一讲 `u2-l7` 要补齐的环节。

> 说明：步骤 3、4 是为了「单独验证词法」而引入的临时接线，正式的 `p` 命令与求值逻辑在 `u2-l7` 完成。本讲遵循「不改源码成品」的约定，以上改动属于 PA 学生练习区，请在自己的工作副本上操作。

## 6. 本讲小结

- **Token 是词法分析的产物**：`tokens[32]` + `nr_token` 这对「定长数组 + 计数器」承载切出的 token 序列；`Token.str[32]` 只对数字、寄存器等带字面值的 token 有意义，单字符运算符直接用 ASCII 当类型。
- **类型号从 256 起步**：`TK_NOTYPE = 256` 是为了让多字符 token 不与 0–255 的单字符运算符冲突，这是 enum 设计的关键。
- **rules 表是声明式词法**：每条规则 `{正则, 类型}` 一行，扫描循环零改动；规则有先后、先到先得，前缀冲突时长者/具体者在前（如十六进制先于十进制、`==` 先于 `=`）。
- **init_regex 一次性预编译**：用 `regcomp + REG_EXTENDED` 把不变的正则编译成状态机，避免重复编译；失败即 `panic`，把语法错误挡在启动期。
- **make_token 是核心扫描循环**：`regexec(...) == 0 && pmatch.rm_so == 0` 锚定「从当前位置开头」匹配，`pmatch.rm_eo` 当推进步长；无任何规则命中时按 `if (i == NR_REGEX)` 报错并返回 `false`。
- **本讲只切词、不求值**：词法产物喂给下一讲 `u2-l7` 的递归下降求值器。

## 7. 下一步学习建议

词法分析产出的 `tokens[]` 还只是一串「片段」，要算出 `0x10 + 5 * ($a0 + 2)` 的实际数值，需要：

1. 学习**递归下降求值**：用 BNF 文法定义表达式语法，把「找主运算符 + 递归求左右子表达式」实现成 `eval(p, q)`。这是下一讲 **u2-l7 表达式求值（递归下降）** 的主题。
2. 用 [tools/gen-expr/gen-expr.c](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/tools/gen-expr/gen-expr.c) 生成大量随机表达式，把 gcc 的计算结果当作「标准答案」与你的 `expr()` 做差分对比——这正是本仓库自带的小型差分测试工具，建议在实现完求值后用它压测。
3. （延伸）把 `expr()` 接到 `p`/`x` 命令与 **u2-l8 监视点** 上：监视点要监控的正是「一个表达式的值」随执行是否变化，因此词法 + 求值是监视点机制的底层依赖。

读完本讲后，建议回头再扫一遍 [expr.c](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/monitor/sdb/expr.c) 全文，确认 `rules → init_regex → make_token → tokens → expr` 这条数据流在自己脑中是通畅的，再进入 u2-l7。
