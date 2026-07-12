# 表达式求值（递归下降）

## 1. 本讲目标

承接 u2-l6 的词法分析，本讲回答一个核心问题：**给定一串已经切好的 token，怎样算出它的值？**

学完本讲，你应当能够：

- 用 BNF 文法描述 SDB 支持的表达式，并把它翻译成「递归下降」求值函数 `eval(p, q)`。
- 实现 `check_parentheses`，判断一段 token 是否被一对外层括号整体包围，并校验括号是否匹配。
- 实现「主运算符查找」，在一个 token 区间里找到分治的切割点，并正确区分一元/二元运算符。
- 处理求值的叶子：十进制/十六进制数、寄存器（`$reg`）、内存解引用（`*addr`）、一元负号，以及除零等错误。
- 用 `tools/gen-expr` 生成大量随机表达式，以 gcc 的运算结果为参考实现，对自己的 `expr()` 做差分测试。

## 2. 前置知识

承接 u2-l6，你已经能调用 `make_token(e)` 把用户输入切成一串带类型的 token，存入全局数组 `tokens[]`，数量记在 `nr_token`。本讲不再切词，专注「求值」。

需要几个基础概念：

- **文法（grammar）与递归下降（recursive descent）**：词法分析产出的是「线性」token 序列，但表达式天然有「结构」（谁套在谁里面）。文法描述这种结构；递归下降是一种自顶向下的解析方法——用一组互相递归的函数，每个函数负责文法的一条规则。对表达式可以简化成「一个 `eval` 函数递归调用自身」。
- **运算符优先级（precedence）与结合性（associativity）**：`*` 比 `+` 先算；同优先级的 `-` 左结合，所以 `a - b - c == (a - b) - c`。
- **差分测试（differential testing）**：把你的实现（DUT, Design Under Test）和一个可信参考实现（REF, Reference）喂同样的输入，逐条比对结果。本讲用 gcc 当 REF。
- **分治（divide and conquer）**：把「求一个大表达式」拆成「求左、求右、做一次运算」三个小问题。

几个来自源码的事实（u2-l6 已建立）：

- token 类型枚举从 256 起，单字符运算符直接复用 ASCII（`'+'`、`'-'`、`'*'`、`'/'`、`'('`、`')'`），见 [src/monitor/sdb/expr.c:L23-L28](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/monitor/sdb/expr.c#L23-L28)。
- `tokens[i].type` 是类型，`tokens[i].str` 是字面值（如 `"0x10"`、`"$a0"`），见 [src/monitor/sdb/expr.c:L65-L71](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/monitor/sdb/expr.c#L65-L71)。
- `word_t` 在 riscv32 下是 `uint32_t`（[include/common.h:L38](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/include/common.h#L38)），即 32 位无符号整数——这决定了求值的位宽与溢出回绕语义。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| `src/monitor/sdb/expr.c` | 表达式词法分析与求值的唯一实现文件；本讲在其 `expr()` 中插入递归下降求值代码。 |
| `src/monitor/sdb/sdb.h` | 声明 `word_t expr(char *e, bool *success)`，是 SDB 调用求值的公共接口（[L21](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/monitor/sdb/sdb.h#L21)）。 |
| `tools/gen-expr/gen-expr.c` | 随机表达式生成器：生成串 → 用 gcc 编译运行得到参考值 → 输出 `<结果> <表达式>`。 |
| `include/memory/vaddr.h` | 提供 `vaddr_read(addr, len)`，内存解引用 `*addr` 时调用它读取客机内存（[L22](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/include/memory/vaddr.h#L22)）。 |
| `include/isa.h` | 声明 `isa_reg_str2val`，寄存器名查值时调用（[L34](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/include/isa.h#L34)）；其实现详见 u5-l15。 |
| `include/common.h` / `include/macro.h` / `include/debug.h` | `word_t` 类型、`ARRLEN` 宏（[macro.h:L29](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/include/macro.h#L29)）、`TODO()` 宏（[debug.h:L41](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/include/debug.h#L41)，未实现时 panic）。 |

## 4. 核心概念与源码讲解

本讲围绕四个最小模块展开：递归下降 `eval`（4.1，含数字/寄存器/内存解引用等叶子）、`check_parentheses`（4.2）、主运算符查找（4.3）、`gen-expr` 差分测试（4.4）。前三者共同把 token 流变成值，第四者负责验证正确性。

### 4.1 递归下降 eval：从 token 流到值

#### 4.1.1 概念说明

`expr()` 当前的样子非常简洁：先 `make_token` 切词，切成功后留了一个 `TODO()`：

```c
word_t expr(char *e, bool *success) {
  if (!make_token(e)) { *success = false; return 0; }
  /* TODO: Insert codes to evaluate the expression. */
  TODO();
  return 0;
}
```

我们要填的，就是「把 `tokens[0 .. nr_token-1]` 算成一个 `word_t`」这段逻辑。

核心思想是 **分治 + 递归**。一个表达式要么是「单个值」（数字、寄存器），要么可以找到一个「主运算符」把它切成「左操作数 主运算符 右操作数」三段，分别递归求值后再做这一步运算。这就是递归下降——函数 `eval(p, q)` 计算 token 区间 `[p, q]` 的值，并在内部对子区间调用自身。

用 BNF 写出来大致是（`<expr>` 就是「表达式」这条递归规则）：

```
<expr>   ::= <number> | <register>
           | "*" <expr>          （内存解引用）
           | "-" <expr>          （一元负号）
           | "(" <expr> ")"
           | <expr> "+" <expr> | <expr> "-" <expr>
           | <expr> "*" <expr> | <expr> "/" <expr>
```

`eval(p, q)` 的工作，就是在 token 区间 `[p, q]` 上识别出当前命中了 BNF 的哪一条，并按那条规则求值。

#### 4.1.2 核心流程

求值骨架（伪代码）：

```
eval(p, q, success):                       # 求 tokens[p..q] 的值
  若 p > q:        *success = false; 返回 0     # 非法区间（如连续运算符）
  若 p == q:       返回 eval_token(p, success)  # 单 token：数 / 寄存器（见 4.1.3）
  若 check_parentheses(p, q) 为真:              # 整体被一对外层括号包围
                   返回 eval(p+1, q-1, success)
  否则:
      op = find_main_op(p, q)                  # 找主运算符（见 4.3）
      若 op < 0:    *success = false; 返回 0
      若 op == p:                                # 一元运算符（取负 / 解引用）
          v = eval(op+1, q, success)
          返回 apply_unary(tokens[op].type, v, success)
      否则:                                     # 二元运算符
          v1 = eval(p, op-1, success)
          v2 = eval(op+1, q, success)
          返回 apply_binary(tokens[op].type, v1, v2, success)
```

`apply_binary` 用 `switch` 处理 `+ - * /`（可选 `==`），任何错误（典型是除零）都置 `*success = false` 并返回 0。`apply_unary` 处理 `-`（取负）与 `*`（解引用）。

#### 4.1.3 源码精读

**入口与切词衔接**：`expr()` 调用 `make_token`，失败则 `*success=false` 直接返回；成功则进入我们补写的求值段——见 [src/monitor/sdb/expr.c:L115-L125](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/monitor/sdb/expr.c#L115-L125)。

**求值原料**：`make_token` 把结果写入全局 `tokens[32]` 与计数 `nr_token`，见 [src/monitor/sdb/expr.c:L70-L71](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/monitor/sdb/expr.c#L70-L71)。注意数组容量固定 32，过长的表达式会越界——实践中应在 `make_token` 里判 `nr_token >= 32` 并报错。

**对外契约**：`word_t expr(char *e, bool *success)` 声明在 [src/monitor/sdb/sdb.h:L21](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/monitor/sdb/sdb.h#L21)。`success` 是「出参」：成功时设 `true` 并返回真实值，失败时设 `false` 返回 0。调用方（未来的 `p` 命令、监视点）据此判断结果是否可信。

> 示例代码（你需要写入 `expr.c` 的递归骨架，**不是**仓库已有代码）：
> ```c
> static word_t eval(int p, int q, bool *success) {
>   if (p > q) { *success = false; return 0; }           // 非法区间
>   if (p == q) { return eval_token(p, success); }       // 叶子：数 / 寄存器
>   if (check_parentheses(p, q)) { return eval(p + 1, q - 1, success); }
>   int op = find_main_op(p, q);
>   if (op < 0) { *success = false; return 0; }
>   if (op == p) {                                       // 一元：取负 / 解引用
>     word_t v = eval(p + 1, q, success);
>     return apply_unary(tokens[p].type, v, success);
>   }
>   word_t v1 = eval(p, op - 1, success);
>   word_t v2 = eval(op + 1, q, success);
>   return apply_binary(tokens[op].type, v1, v2, success);
> }
> ```
> 然后在 `expr()` 里把 `TODO();` 换成：
> ```c
> *success = true;
> return eval(0, nr_token - 1, success);
> ```

**叶子 `eval_token` 的三种来源**（示例代码）：

> ```c
> static word_t eval_token(int i, bool *success) {
>   switch (tokens[i].type) {
>     case TK_DECIMAL: return strtoul(tokens[i].str, NULL, 10);
>     case TK_HEX:     return strtoul(tokens[i].str, NULL, 16);   // "0x..." 
>     case TK_REG:     return isa_reg_str2val(tokens[i].str, success); // "$a0"
>     default: *success = false; return 0;                        // 既不是数也不是寄存器
>   }
> }
> ```
> - 数字：用 `strtoul` 按十进制或十六进制解析（十六进制串形如 `"0x10"`，基数为 16 时 `strtoul` 会自动识别 `0x` 前缀）。
> - 寄存器：调用 `isa_reg_str2val(name, success)`，把名字（如 `"$a0"` 或 `"a0"`，取决于你的词法规则）转成 `word_t`；接口声明在 [include/isa.h:L34](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/include/isa.h#L34)，实现在 `src/isa/riscv32/reg.c`（属 u5-l15）。

**内存解引用**（一元 `*`，示例代码）：

> ```c
> static word_t apply_unary(int type, word_t v, bool *success) {
>   if (type == '-') return -v;                 // 取负：word_t 是无符号，-v 即按位取反加一
>   if (type == TK_DEREF) return vaddr_read(v, sizeof(word_t)); // 解引用：读 4 字节
>   *success = false; return 0;
> }
> ```
> 解引用通过 [include/memory/vaddr.h:L22](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/include/memory/vaddr.h#L22) 的 `vaddr_read(addr, len)` 读取客机虚拟内存。注意：这里把「一元 `*`」单独记为 `TK_DEREF` 类型（见 4.3.3 关于一元/二元 `*` 的区分）。

#### 4.1.4 代码实践

实践目标：分阶段把 `expr()` 从「只认一个数」扩展到「支持 `+ - * /`、括号、寄存器、十六进制、内存解引用」。

操作步骤：

1. **第一阶段（数字）**：实现最简 `eval`，只让 `p == q` 分支工作——用 `strtoul` 解析十进制数字。把 `expr()` 的 `TODO();` 换成 `*success = true; return eval(0, nr_token - 1, success);`。
2. **第二阶段（二元运算）**：补上 4.2 的 `check_parentheses` 与 4.3 的 `find_main_op`，以及 `apply_binary`（含除零检测 `if (v2 == 0) { *success = false; return 0; }`）。
3. **第三阶段（叶子扩展）**：在 `eval_token` 里加 `TK_HEX`、`TK_REG` 两个分支；在 `apply_unary` 里加取负 `-` 与解引用 `TK_DEREF`。
4. 临时在 `expr.c` 里写一段调试：`bool ok; printf("%u\n", expr("1+2*3", &ok));`，编译运行。

需要观察的现象：
- `expr("1+2*3", &ok)` → `7`（先乘后加）。
- `expr("(1+2)*3", &ok)` → `9`（括号优先）。
- `expr("0x10", &ok)` → `16`。
- `expr("$a0", &ok)` → 当前 `a0` 的值（需 `isa_reg_str2val` 已实现，否则 `ok==false`，待本地验证）。
- `expr("*0xA0000000", &ok)` → 该地址处 4 字节内容（串口/设备区域，待本地验证）。

预期结果：前两式立即正确；后三式依赖词法阶段已加入对应 token 类型与寄存器实现。若 `$a0` 失败，先确认 u2-l6 的词法规则把 `$` 开头的寄存器名识别为 `TK_REG`，并把名字存进了 `tokens[i].str`。

#### 4.1.5 小练习与答案

- **练习**：为什么 `eval` 不直接返回 `int` 而要用 `word_t`？
  **答**：地址、内存值都是 `word_t` 宽（riscv32 下 32 位无符号）。用 `int` 会丢失最高位——`0x80000000` 变成负数，地址计算随之出错。
- **练习**：`expr("1", &ok)` 与 `expr("(1)", &ok)` 最终是否都落到 `p == q` 分支？
  **答**：`"1"` 直接落 `p == q`；`"(1)"` 先命中 `check_parentheses`，递归 `eval(1, 1)` 后才落 `p == q`。结果相同但路径不同——这正是 `check_parentheses` 的作用。

---

### 4.2 check_parentheses：括号匹配与整体包围判定

#### 4.2.1 概念说明

递归下降时，如果一段 token 整体被一对**匹配的**外层括号包围，例如 `( 1 + 2 )`，我们应当剥掉这对括号、对内部递归。但 `( 1 ) + ( 2 )` 的首尾虽然都是括号，却不是「同一对」，不能剥。`check_parentheses(p, q)` 就是判这件事，并顺手校验括号是否配平。

#### 4.2.2 核心流程

```
check_parentheses(p, q):
  若 tokens[p] != '(' 或 tokens[q] != ')': 返回 false      # 首尾非配对括号
  cnt = 0
  对 i 从 p 到 q:
      若 tokens[i] == '(': cnt += 1
      若 tokens[i] == ')':
          cnt -= 1
          若 cnt == 0 且 i != q: 返回 false    # 还没到结尾括号就配平 → 首尾非同一对
          若 cnt < 0:              返回 false  # 右括号多了 → 括号不匹配
  返回 cnt == 0
```

关键点：扫描时维护计数器 `cnt`。若 `cnt` 在到达最后一个 token 之前就归零，说明最左的 `(` 已提前闭合，首尾不是同一对括号。

#### 4.2.3 源码精读

`check_parentheses` 在仓库里尚不存在（属本讲补写），但它服务的对象 `make_token` 已就绪——见 [src/monitor/sdb/expr.c:L73-L112](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/monitor/sdb/expr.c#L73-L112)。注意 token 的类型 `'('` 与 `')'` 直接用 ASCII 字符值（见 u2-l6 的词法规则），所以判定写成 `tokens[i].type == '('` 即可。

> 示例代码（需写入 `expr.c`）：
> ```c
> static bool check_parentheses(int p, int q) {
>   if (tokens[p].type != '(' || tokens[q].type != ')') return false;
>   int cnt = 0;
>   for (int i = p; i <= q; i++) {
>     if (tokens[i].type == '(') cnt++;
>     else if (tokens[i].type == ')') {
>       cnt--;
>       if (cnt == 0 && i != q) return false;   // 首括号提前闭合
>       if (cnt < 0)            return false;   // 右括号多余
>     }
>   }
>   return cnt == 0;
> }
> ```

#### 4.2.4 代码实践

实践目标：用独立小测试验证 `check_parentheses` 的边界。

操作步骤：

1. 把 `check_parentheses` 临时暴露为非 `static`，或写一个 `#ifdef TEST_EXPR` 的 `main`。
2. 手工构造 `tokens` 数组（直接对 `type` 赋值），覆盖四组用例：
   - `( 1 + 2 )` → 期望 `true`
   - `( 1 ) + ( 2 )` → 期望 `false`
   - `( ( 1 ) + 2 )` → 期望 `true`
   - `( 1 + 2 ) * ( 3 )` → 期望 `false`

需要观察的现象 / 预期结果：四组结果依次为 `true / false / true / false`。若无法在 NEMU 内直接驱动，可写一段独立 C 程序包含该函数并 `assert` 这些用例——「待本地验证」。

#### 4.2.5 小练习与答案

- **练习**：`( ( 1 + 2 )`（少一个右括号）调用 `check_parentheses` 会返回什么？表达式合法吗？
  **答**：尾 token 不是 `)`，第一步即返回 `false`；同时 `cnt` 最终为 1，说明括号没配平——表达式不合法。`eval` 入口处宜先对整串扫一遍确保 `cnt == 0`，再做后续解析。
- **练习**：为什么「`cnt` 提前归零」必须特判 `i != q`？
  **答**：合法串 `( 1 )` 在末尾 `)` 处 `cnt` 归零且 `i == q`，是允许的；只有未到末尾就归零才说明首尾非同一对括号。

---

### 4.3 主运算符查找：找到分治切割点

#### 4.3.1 概念说明

剥掉外层括号后，若区间里仍有多个运算符，就要找一个「主运算符」作为分治的根：它的左子树是左操作数，右子树是右操作数。主运算符的判定有三条规则（来自 PA 讲义）：

1. **不在任何括号内**（深度为 0）；
2. 在所有满足第 1 条的运算符中，**优先级最低**；
3. 优先级相同时，取**最右**的一个（保证左结合：`a - b - c` 的根是第二个 `-`）。

优先级表（从低到高）：

| 运算符 | 优先级 | 备注 |
|---|---|---|
| `==`（`TK_EQ`） | 1 | 最低 |
| `+` `-`（二元） | 2 | |
| `*` `/` | 3 | |

一元运算符（取负 `-`、解引用 `*`）**不作为二元主运算符候选**，需在 `eval` 里走 `op == p` 分支特殊处理。

#### 4.3.2 核心流程

```
find_main_op(p, q):
  op = -1；best = +∞；depth = 0
  对 i 从 p 到 q:
      t = tokens[i].type
      若 t == '(': depth += 1; continue
      若 t == ')': depth -= 1; continue
      若 depth != 0: continue                  # 在括号内，跳过
      pr = precedence(t)                        # 非二元运算符返回 -1
      若 pr < 0: continue
      若 该运算符是一元上下文（前驱不是值）: continue   # 跳过一元 - / 一元 *
      若 pr <= best:                            # <= 保证同优先级取最右
          best = pr; op = i
  返回 op
```

**二元/一元的判别**（关键易错点）：`-` 与 `*` 既能二元（减、乘）又能一元（取负、解引用）。判别依据是「前一个 token 是不是值」：

- 前一个 token 是数、寄存器或 `)` → 当前是**二元**运算符；
- 前一个 token 不存在（`i == p`）、是运算符、或是 `(` → 当前是**一元**运算符，不作为二元主运算符候选。

#### 4.3.3 源码精读

`find_main_op` 同样由本讲补写；它消费的 token 类型来自词法阶段，规则表见 [src/monitor/sdb/expr.c:L30-L42](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/monitor/sdb/expr.c#L30-L42)。`==` 的类型 `TK_EQ` 枚举定义在 [src/monitor/sdb/expr.c:L23-L28](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/monitor/sdb/expr.c#L23-L28)。

> 示例代码（需写入 `expr.c`）：
> ```c
> static int precedence(int type) {
>   switch (type) {
>     case TK_EQ:        return 1;
>     case '+': case '-': return 2;   // 二元
>     case '*': case '/': return 3;
>     default:           return -1;   // 非二元运算符
>   }
> }
>
> // 前驱不是「值」时，当前 - 或 * 是一元运算符
> static bool is_unary_context(int i, int p) {
>   if (i == p) return true;                    // 无前驱
>   int pt = tokens[i - 1].type;
>   if (pt == ')') return false;                // 前驱是右括号 → 二元
>   if (pt == TK_DECIMAL || pt == TK_HEX || pt == TK_REG) return false; // 前驱是值 → 二元
>   return true;                                // 前驱是运算符或 '(' → 一元
> }
>
> static int find_main_op(int p, int q) {
>   int op = -1, best = 100, depth = 0;
>   for (int i = p; i <= q; i++) {
>     int t = tokens[i].type;
>     if      (t == '(') { depth++; continue; }
>     else if (t == ')') { depth--; continue; }
>     else if (depth != 0) continue;
>     int pr = precedence(t);
>     if (pr < 0) continue;
>     if (is_unary_context(i, p)) continue;     // 一元 - / 一元 * 不作二元候选
>     if (pr <= best) { best = pr; op = i; }    // <= 取最右
>   }
>   return op;
> }
> ```

> **另一种等价做法**（更干净，推荐）：在 `make_token` 阶段就根据上下文把一元 `-` 标成 `TK_NEG`、一元 `*` 标成 `TK_DEREF`（属 u2-l6 词法范畴）。这样 `find_main_op` 只面对纯二元运算符，无需 `is_unary_context`。无论哪种，最终 `apply_unary` 都用 `TK_DEREF`/`TK_NEG`/`'-'` 来区分取负与解引用。

#### 4.3.4 代码实践

实践目标：验证优先级与结合性正确。

操作步骤：

1. 接入 4.1 的 `eval`、4.2 的 `check_parentheses`，补全 `apply_binary`（`+ - * /`，除零置 `*success = false`）。
2. 用 `expr()` 测试四式：
   - `"1+2*3"` → 期望 `7`（先乘后加）
   - `"2*3+1"` → 期望 `7`
   - `"10-3-2"` → 期望 `5`（左结合）
   - `"(1+2)*3"` → 期望 `9`

需要观察的现象 / 预期结果：四式结果依次 `7 / 7 / 5 / 9`。若 `10-3-2` 得 `1`，说明主运算符取成了最左（结合性反了），把比较方向从 `< best` 改回 `<= best`。

#### 4.3.5 小练习与答案

- **练习**：`a - b - c` 中两个 `-` 同优先级，为何主运算符取第二个？
  **答**：左结合要求 `(a - b) - c`，根是第二个 `-`。`pr <= best` 让后扫到的同优先级运算符覆盖前者，故取最右。
- **练习**：`"-1"`（负一）在 `find_main_op` 里会被当成二元 `-` 吗？
  **答**：不会。`-` 在 `i == p`（无前驱），属一元上下文，被 `is_unary_context` 跳过；`find_main_op` 返回 `-1`，由 `eval` 的 `op < 0` / `op == p` 路径当作取负处理。

---

### 4.4 gen-expr：随机表达式差分测试

#### 4.4.1 概念说明

手写几个用例很难覆盖所有优先级、结合性与嵌套组合。`tools/gen-expr` 的思路是 **差分测试**：随机生成大量合法表达式，用 gcc 当「标准答案」算出结果，再让你的 `expr()` 算一遍，逐条比对。只要有一条不一致，就暴露了求值里的 bug。

它的输出格式是每行 `<结果> <表达式>`，例如 `7 1+2*3`。你拿到的就是「参考答案 + 输入」配对。

#### 4.4.2 核心流程

`gen-expr` 的运行流程：

```
main(loop):
  对 i = 0 .. loop-1:
      gen_rand_expr()                 # 把一个随机表达式写进 buf（当前是 TODO）
      用 code_format 把 buf 包成一段 C 代码：unsigned result = <buf>;
      gcc 编译这段代码 → /tmp/.expr   # 编译失败则跳过该条
      运行 /tmp/.expr，读出 result    # gcc 算出的「标准答案」
      printf("%u %s\n", result, buf)  # 输出「答案 表达式」
```

它把表达式包进 `unsigned result = %s;`，让 **gcc 的编译器替你做参考求值**——这是巧妙的「借力」：不需要自己写一个求值器当 REF，直接用 C 编译器。

#### 4.4.3 源码精读

**缓冲区与代码模板**：`buf` 存随机表达式，`code_buf` 存包好的 C 代码，`code_format` 是模板——见 [tools/gen-expr/gen-expr.c:L24-L32](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/tools/gen-expr/gen-expr.c#L24-L32)。注意模板里用的是 `unsigned result`（32 位无符号），与 riscv32 的 `word_t`（`uint32_t`）位宽一致，所以二者溢出回绕语义相同，差分才成立。

**待实现的随机生成**：`gen_rand_expr()` 当前只是 `buf[0] = '\0'`（生成空串），是 TODO——见 [tools/gen-expr/gen-expr.c:L34-L36](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/tools/gen-expr/gen-expr.c#L34-L36)。空串包成 `unsigned result = ;` 编译必失败，于是 [L56-L57](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/tools/gen-expr/gen-expr.c#L56-L57) 的 `system("gcc ...")` 返回非 0，`continue` 跳过——**所以不实现 `gen_rand_expr`，工具不会有任何输出**。

> 示例代码（需写入 `gen-expr.c` 的 `gen_rand_expr`，递归生成）：
> ```c
> static void gen_rand_expr() {
>   switch (choose(3)) {
>     case 0: sprintf(buf + strlen(buf), "%u", choose(100)); break;   // 随机数
>     case 1: gen_rand_expr(); /* 见下，需拼接时用临时缓冲 */         // ( expr )
>             /* 实际实现：用两个缓冲避免递归覆盖，这里仅示意 */       break;
>     default: gen_rand_expr(); append_op(); gen_rand_expr(); break;  // expr op expr
>   }
> }
> ```
> 实现要点：(1) 用 `choose(n)` 取 `rand() % n`；(2) 递归生成子表达式时要用**临时缓冲**拼接，否则 `buf` 会被子调用覆盖；(3) `append_op` 在 `+ - * /` 与括号里随机选。生成的表达式必须是合法 C 表达式（gcc 能编译），否则该条会被跳过。

**编译运行取答案**：`system("gcc /tmp/.code.c -o /tmp/.expr")` 编译（[L56](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/tools/gen-expr/gen-expr.c#L56)），`popen("/tmp/.expr", "r")` 运行（[L59](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/tools/gen-expr/gen-expr.c#L59)），`fscanf` 读出 `result`（[L63](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/tools/gen-expr/gen-expr.c#L63)），最后 `printf("%u %s\n", result, buf)`（[L66](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/tools/gen-expr/gen-expr.c#L66)）。

**构建方式**：`tools/gen-expr/Makefile` 复用 NEMU 的 `scripts/build.mk`，需要 `NEMU_HOME` 环境变量——见 [tools/gen-expr/Makefile:L16-L18](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/tools/gen-expr/Makefile#L16-L18)。产物是 `build/gen-expr`。

#### 4.4.4 代码实践

实践目标：让 `gen-expr` 产出 100 条「答案 + 表达式」，并用它差分测试你的 `expr()`。

操作步骤：

1. 实现 `gen_rand_expr()`（递归 + 临时缓冲拼接，见上）。
2. 构建：`make -C tools/gen-expr`（确保已 `export NEMU_HOME=<本仓库根目录>`）。
3. 生成用例：`./build/gen-expr 100 > /tmp/input`，检查文件每行形如 `7 1+2*3`。
4. 差分比对：写一个临时驱动读取 `/tmp/input` 每行，对表达式调用 `expr()`，与第一列的 gcc 答案比较，不一致就打印该行。

> 示例代码（临时驱动，可放进一个独立的 `main`，或临时改 `nemu-main.c` 调试）：
> ```c
> char line[65536];
> while (fgets(line, sizeof(line), stdin)) {
>   unsigned ref; char e[65536];
>   if (sscanf(line, "%u %65535[^\n]", &ref, e) != 2) continue;
>   bool ok; word_t got = expr(e, &ok);
>   if (!ok || got != ref) printf("MISMATCH: %s (ref=%u got=%u)\n", e, ref, got);
> }
> ```

需要观察的现象：理想情况下没有任何 `MISMATCH` 输出。出现不一致时，看打印出的表达式，针对其结构（嵌套括号、连续减号、长立即数）回到 `eval`/`find_main_op` 定位 bug。

预期结果：100 条全部通过即说明 `+ - * /` 与括号的求值正确。寄存器、内存解引用、`$reg` 不在 gcc 表达式语法内，`gen-expr` 不会生成它们，需另行手工测试（见 4.1.4）。完整跑通后视环境记为「待本地验证」。

> 注意位宽：若你把 NEMU 切到 riscv64（`word_t` 变 64 位），而 `gen-expr` 仍用 `unsigned`（32 位），二者溢出语义不同，差分会误报。差分测试请保持 riscv32 默认配置。

#### 4.4.5 小练习与答案

- **练习**：为什么 `gen_rand_expr` 递归生成子表达式时必须用临时缓冲？
  **答**：`buf` 是全局单一缓冲；若直接 `gen_rand_expr()` 两次，第二次会覆盖第一次的结果。正确做法是把子表达式生成到临时缓冲，再用 `strcat` 拼回 `buf`。
- **练习**：`gen-expr` 输出的答案与你的 `expr()` 结果都用 `%u`（无符号）。若把比较改成有符号 `%d`，哪些表达式会出现「假不一致」？
  **答**：结果最高位为 1 的值，如 `0 - 1`（riscv32 下）。无符号下是 `4294967295`，有符号下是 `-1`。两边只要统一用无符号比较就不会误报，这正是模板用 `unsigned` 的原因。

---

## 5. 综合实践

把本讲四个模块串起来，完成一个端到端的「表达式求值 + 差分验证 + SDB 接入」小任务：

1. 在 `expr.c` 实现完整的递归下降求值：`eval` + `check_parentheses` + `find_main_op` + `apply_binary`/`apply_unary` + `eval_token`，支持 `+ - * /`、括号、十进制/十六进制数、`$reg` 寄存器、`*addr` 内存解引用与一元负号，除零要安全报错。
2. 在 SDB 新增 `p EXPR` 命令（在 `cmd_table` 加一项，处理函数里调用 `expr(args, &ok)`，失败时打印错误，成功时用 `printf("%u\n", ...)` 打印值）。
3. 实现 `gen_rand_expr()`，构建并跑 `./build/gen-expr 100 > /tmp/input`，用临时驱动做差分，修到无 `MISMATCH`。
4. 在 SDB 里手工验证 `gen-expr` 覆盖不到的部分：`p $a0`、`p *0xA0000000`（串口数据寄存器）、`p ($a0 + 4) * 2`。

完成后，你的 SDB 就具备了一个「不带符号、可读寄存器与内存」的计算器——这正是 u2-l8 监视点（watchpoint）所需的基石：监视点的本质就是「每步重算一个表达式、看值是否变化」。

## 6. 本讲小结

- 表达式求值 = 在 token 数组上做 **分治递归**：`eval(p, q)` 找主运算符切成左右两段，递归求值后再做这一步运算。
- `check_parentheses(p, q)` 用计数器判断一段 token 是否被一对外层括号整体包围，并顺带校验配平。
- 主运算符查找三条规则：深度为 0、优先级最低、同优先级取最右（左结合）；务必用「前驱是否为值」区分一元/二元的 `-` 与 `*`。
- 叶子来自 `strtoul`（数）、`isa_reg_str2val`（寄存器）、`vaddr_read`（解引用）；一元取负直接 `-v`，除零要置 `success=false`。
- `gen-expr` 用 gcc 当参考实现做差分测试：随机生成合法 C 表达式，比对 `unsigned` 结果，能高效暴露优先级与结合性 bug。

## 7. 下一步学习建议

- **横向**：本讲的 `expr()` 将被 u2-l8 监视点直接复用——监视点维护一个表达式，每步执行后重算并与旧值比较。建议先做 u2-l8，体会「求值器作为基础设施」的复用价值。
- **纵向**：进入 U3 的 CPU 执行引擎。`eval` 的递归下降思路与 NEMU「指令译码」在结构上相通——都是「在一串输入上识别结构、分而治之」，可对照 [src/cpu/cpu-exec.c](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/cpu/cpu-exec.c) 的 `exec_once` 阅读。
- **延伸阅读**：`man regex` 回顾词法所用 POSIX 正则；`tools/gen-expr/gen-expr.c` 的差分思路在 u8-l24 的 difftest 机制里会以更大规模再次出现（DUT vs REF 寄存器逐条比对）。
