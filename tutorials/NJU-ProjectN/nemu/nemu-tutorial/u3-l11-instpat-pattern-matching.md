# INSTPAT 模式匹配译码机制

## 1. 本讲目标

上一讲（u3-l10）我们打开了 `exec_once` 的黑盒，看懂了 `Decode` 工作台、三种 PC、`inst_fetch` 取指，以及遇到非法指令时如何走 `INV` 兜底。那时我们把「怎么认出一条指令」当作已知条件。本讲就来回答这个核心问题：

**给定一条 32 位的指令字，NEMU 怎么判断它是 `addi`、`lbu` 还是 `ebreak`？**

学完本讲你应当能够：

1. 理解 `pattern_decode` 如何把一个人写的模式串（如 `"01??"`、`"??????? ????? ????? 100 ????? 00000 11"`）在编译期「编译」成三个整数 `key`、`mask`、`shift`。
2. 手动对一条已有的 `INSTPAT` 项算出它的 `key/mask/shift`，并解释运行时 `(inst >> shift) & mask == key` 为什么能完成匹配。
3. 掌握 `INSTPAT` / `INSTPAT_START` / `INSTPAT_END` 三个宏如何协作，特别是 GCC「标号作为值（labels as values）」+「计算跳转（computed goto）」这一组合的妙用。
4. 会用 `INSTPAT` 为 riscv32 新增一条指令（以 `addi` 为例）。

## 2. 前置知识

本讲需要你已经理解以下概念（前序讲义已建立）：

- **指令译码（decode）**：CPU 取到一串二进制位后，判断「这是哪条指令、操作数是谁」的过程。u3-l9/u3-l10 已讲过 `exec_once` 单步执行的骨架。
- **位运算**：左移 `<<`、右移 `>>`、按位与 `&`、按位或 `|`。本讲几乎全是位运算。
- **指令编码字段**：以 RISC-V 为例，一条 32 位指令从高到低划分为 `funct7 | rs2 | rs1 | funct3 | rd | opcode` 等字段。识别一条指令，本质上就是检查它的 `opcode`、`funct3`、`funct7` 等若干「固定字段」是否等于预期值，其余字段（寄存器号、立即数）是变量，不用关心。
- **C 预处理器宏**：`do { ... } while (0)` 包装、`__VA_ARGS__` 可变参数、`##` 标记拼接。u1-l4 讲过 `concat` 两层宏的必要性（`##` 会阻止参数展开）。
- **GCC 扩展**：本讲会用到 `goto *ptr;`（按指针跳转）和 `&&label`（取标号地址）两个非标准 C 但 GCC/Clang 支持的扩展。

一个直觉：所谓「匹配一条指令」，就是给指令的若干「固定位」画一个掩码（mask），把不关心的位（don't-care）挖掉，再比较关心位是否等于某个值（key）。本讲的全部分析都是在反复演练这一直觉。

## 3. 本讲源码地图

本讲几乎只围绕一个文件展开，它是 INSTPAT 机制的全部定义所在：

| 文件 | 作用 |
| --- | --- |
| [include/cpu/decode.h](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/include/cpu/decode.h) | INSTPAT 机制的全部核心：`pattern_decode`、`pattern_decode_hex`、`INSTPAT`、`INSTPAT_START/END` 四个宏/函数都在这里。 |
| [src/isa/riscv32/inst.c](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/isa/riscv32/inst.c) | riscv32 的指令实现，是 INSTPAT 的典型「消费者」：定义 `INSTPAT_INST` / `INSTPAT_MATCH`，并列出 `auipc/lbu/sb/ebreak/inv` 五条模式。 |
| [include/macro.h](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/include/macro.h) | 提供 `STRLEN`（编译期字符串长度）、`concat`（标记拼接）、`BITS`、`SEXT` 等被 INSTPAT 链路依赖的宏。 |
| [src/isa/x86/inst.c](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/isa/x86/inst.c) | 对比参照：x86 同样用 INSTPAT 处理定长 opcode 前缀，且展示了「同一文件里多个 INSTPAT 块分属不同函数」的用法。 |

> 提醒：INSTPAT 机制本身（`include/cpu/decode.h`）是 **ISA 无关** 的通用框架；各 ISA 只负责在自己的 `inst.c` 里定义 `INSTPAT_INST(s)`（怎么取指令字）和 `INSTPAT_MATCH(s, ...)`（匹配后执行体长什么样），再列举自己的模式表。这正是 u1-l4 讲过的「一套框架源码适配多 ISA」的又一个例证。

## 4. 核心概念与源码讲解

本讲拆为四个最小模块：`pattern_decode`（二进制编译器）、`pattern_decode_hex`（十六进制编译器）、`INSTPAT` 宏（运行时匹配）、`INSTPAT_START/END`（标号跳转收尾）。它们构成一条完整链路：

```
人写的模式串 "01??"
        │ pattern_decode（编译期，每个 INSTPAT 调用一次）
        ▼
三个整数 key / mask / shift
        │ INSTPAT 宏（运行时）
        ▼
(inst >> shift) & mask == key  ?  匹配 → 执行 INSTPAT_MATCH 体 → goto 跳到 INSTPAT_END
                                  不匹配 → 落到下一条 INSTPAT 继续
```

### 4.1 pattern_decode：把"01??"编译成 key/mask/shift

#### 4.1.1 概念说明

我们希望用一种「人好写、机器好查」的方式描述一条指令的固定位。NEMU 的选择是**模式串**：用 `'0'`/`'1'` 表示必须匹配的位，用 `'?'` 表示不关心（don't-care）的位，空格仅用于人眼分组（编译时忽略）。例如 riscv32 的 `lbu`：

```
"??????? ????? ????? 100 ????? 00000 11"
 ^^^^^^^^^^^^^^^^^^^^     ^^^^^          这些位是寄存器号/立即数，不关心（?）
                  ^^^                ^^^^^^^^^^^^  funct3=100、opcode=0000011，必须匹配
```

但运行时拿一条 32 位指令去做「字符串比对」太慢。`pattern_decode` 的职责是：在**编译期**把这个模式串一次性「编译」成三个整数，让运行时只需做一次「移位 + 与 + 比较」：

- `key`：把所有 `'1'` 在对应位上置 1（`'?'` 当 0 填充）后，再右移对齐到低位；
- `mask`：固定位（`'0'`/`'1'`）置 1、don't-care 位（`'?'`）置 0，同样右移对齐；
- `shift`：模式串**末尾连续 `'?'` 的个数**（即低位有多少位被「砍掉」不参与比较）。

最终运行时判定为：`(指令 >> shift) & mask == key`。

#### 4.1.2 核心流程

`pattern_decode` 自左向右逐字符扫描模式串（最左字符 = 指令最高位 MSB），用「左移 + 或」把每一位累加进 `__key`/`__mask`，并维护「当前末尾连续 `?` 计数」`__shift`。遇到 `'0'`/`'1'` 计数清零，遇到 `'?'` 计数加一。扫描结束后，把 `__key`/`__mask` 再右移 `__shift` 位，把末尾的 don't-care 低位移除。

下面用一个 4 位模式 `"01??"` 把整条流程走一遍（这是示例串，便于理解；真实 riscv32 是 32 位）：

```
扫描 "01??"：
pos0 '0' : __key=(0<<1)|0=0,  __mask=(0<<1)|1=1,  __shift=0   (遇 0/1，计数清零)
pos1 '1' : __key=(0<<1)|1=1,  __mask=(1<<1)|1=3,  __shift=0
pos2 '?' : __key=(1<<1)|0=2,  __mask=(3<<1)|0=6,  __shift=1   (? 计数 +1)
pos3 '?' : __key=(2<<1)|0=4,  __mask=(6<<1)|0=12, __shift=2
finish  : key   = __key   >> 2 = 1   (0b0001)
          mask  = __mask  >> 2 = 3   (0b0011)
          shift = __shift        = 2
```

含义：`"01??"` 只关心最高两位是 `01`，低两位随意。运行时 `(inst >> 2) & 0b11 == 0b01`，即匹配 `0b0100`、`0b0101`、`0b0110`、`0b0111`（十进制 4–7）这四个值。

> 为什么 `__shift` 只数「末尾」的 `?`，而不数中间的 `?`？因为只有末尾的 `?` 对应指令的**低位**，可以通过「整体右移」砍掉；中间的 `?` 没法用一次移位处理，只能靠 `mask` 把它们「挖空」（`mask` 那些位为 0）。riscv32 的 `lbu` 就是典型：它的 `?` 在高位（funct7/rs2/rs1）和中间（rd），但末尾是固定的 opcode，所以 `shift` 往往是 0，全靠 `mask` 挖空。

#### 4.1.3 源码精读

[include/cpu/decode.h:30-60](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/include/cpu/decode.h#L30-L60) 定义了 `pattern_decode`。它最巧妙的设计是**展开方式**：

```c
// include/cpu/decode.h:30-60 （关键片段）
__attribute__((always_inline))
static inline void pattern_decode(const char *str, int len,
    uint64_t *key, uint64_t *mask, uint64_t *shift) {
  uint64_t __key = 0, __mask = 0, __shift = 0;
#define macro(i) \
  if ((i) >= len) goto finish; \
  else { \
    char c = str[i]; \
    if (c != ' ') { \
      Assert(c == '0' || c == '1' || c == '?', ...); \
      __key  = (__key  << 1) | (c == '1' ? 1 : 0); \
      __mask = (__mask << 1) | (c == '?' ? 0 : 1); \
      __shift = (c == '?' ? __shift + 1 : 0); \
    } \
  }
#define macro2(i)  macro(i);   macro((i) + 1)
#define macro4(i)  macro2(i);  macro2((i) + 2)
   ...                       // macro8 / macro16 / macro32 / macro64
  macro64(0);                 // 把 0..63 全部展开
  panic("pattern too long");
finish:
  *key = __key >> __shift;
  *mask = __mask >> __shift;
  *shift = __shift;
}
```

逐点解读：

1. **`always_inline` + `static inline`**：`pattern_decode` 不会被编译成独立函数，而是**内联进每一个 `INSTPAT` 调用点**。结合下面的「编译期常量」这一点，意味着整个函数会被常量折叠掉。
2. **`str`、`len` 都是编译期常量**：调用处 `pattern_decode(pattern, STRLEN(pattern), ...)`，`pattern` 是字符串字面量，`STRLEN(pattern)` 即 `sizeof(pattern) - 1`（见 [include/macro.h:26](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/include/macro.h#L26)）。于是 `if ((i) >= len) goto finish;` 在编译期就能判定真伪，`str[i]` 也是常量下标常量字符。
3. **`macro64(0)` 的指数展开**：`macro` 处理一位，`macro2` 处理两位……`macro64` 一口气展开 64 位。`goto finish` 保证一旦 `i >= len`（字符串扫完）立即跳出，所以哪怕模式只有 7 位、32 位，套同一个 `macro64(0)` 也安全。这种「过度展开 + 提前 goto」让编译器能把 `key/mask/shift` 完全折叠成常量。
4. **空格被 `if (c != ' ')` 跳过**：这就是模式串里可以随意加空格分组的原因——`STRLEN(pattern)` 把空格也算进了 `len`，但扫描时跳过它们，所以分组纯为可读性。
5. **`__shift` 的「遇 0/1 清零、遇 ? 累加」语义**：前面流程分析里已逐行验证，它记录的是「以当前位置结尾的连续 `?` 数」。只有扫到字符串末尾时它才等于「末尾连续 `?` 数」。
6. **末尾的 `>> __shift`**：把末尾 don't-care 低位移除，使 `key`/`mask` 紧凑地落在低位，运行时配合 `(inst >> shift)` 使用。

> **为什么要这么折腾，而不是直接用查表/switch？** 因为不同 ISA 的指令布局千差万别（riscv 是定长 32 位、x86 是变长、mips 是定长但字段位置不同），用「模式串」可以用同一套数据格式描述所有 ISA 的指令，且编译期常量折叠后运行时开销只有「移位 + 与 + 比较」三条指令。这是 NEMU「框架通用、性能可接受、对学生好读」三者兼顾的关键设计。

#### 4.1.4 代码实践

**实践目标**：亲手对一条真实的 riscv32 模式算出 `key/mask/shift`，确认算法无误。

**操作步骤**：

1. 打开 [src/isa/riscv32/inst.c:66](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/isa/riscv32/inst.c#L66)，找到 `ebreak` 那一行：

   ```c
   INSTPAT("0000000 00001 00000 000 00000 11100 11", ebreak , N, NEMUTRAP(s->pc, R(10)));
   ```

2. 去掉空格得到 32 位串：`00000000001000000000000001110011`。
3. 这条模式**全是 0/1，没有 `?`**。按算法：`__shift` 全程被清零为 0；`__key` 就是用 0/1 拼出的 32 位整数本身；`__mask` 全 1（共 32 位）。末尾 `>> 0` 不变。
4. 把这个 32 位二进制转成十六进制（每 4 位一组）：
   `0000 0000 0001 0000 0000 0000 0111 0011` = `0x00100073`。

**需要观察的现象 / 预期结果**：

- `key = 0x00100073`，`mask = 0xFFFFFFFF`，`shift = 0`。
- 运行时判定 `(inst >> 0) & 0xFFFFFFFF == 0x00100073`，即**必须精确等于** `0x00100073`。这与「`ebreak` 的标准编码就是 `0x00100073`」完全吻合。
- 你可以在 riscv32 工具链手册或任意 RISC-V 汇编器里查证 `ebreak` 的机器码确实是 `0x00100073`（汇编形如 `00100073`）。**待本地验证**：若有 `riscv32-unknown-elf-objdump`，可写一段含 `ebreak` 的小程序反汇编对照。

> 想再练一个有 `?` 的？取 `lbu`（[src/isa/riscv32/inst.c:63](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/isa/riscv32/inst.c#L63)），按 4.1.2 的步骤逐位扫一遍，应得到 `key=0x4003`、`mask=0x707F`、`shift=0`（因为末尾是固定的 opcode `0000011`，不是 `?`，所以 `shift=0`，靠 `mask` 挖空高位与中间的 `?`）。

#### 4.1.5 小练习与答案

**练习 1**：模式串 `"1????"`（5 位）的 `key/mask/shift` 各是多少？它匹配哪些数值？

**参考答案**：扫描过程——`'1'`：key=1,mask=1,shift=0；随后 4 个 `'?'`：key 依次左移或 0 → 2,4,8,16；mask 左移或 0 → 2,4,8,16；shift 累加到 4。finish：`key=16>>4=1`，`mask=16>>4=1`，`shift=4`。运行时 `(inst>>4)&1==1`，即最高位为 1 的 5 位数，匹配 `10000`–`11111`（16–31）。

**练习 2**：为什么 `__shift` 在遇到 `'0'`/`'1'` 时必须清零，而不是只增不减？试想 `"01?0?"` 若不清零会得到什么错误结果？

**参考答案**：`__shift` 的语义是「末尾连续 `?` 数」，用于决定右移多少位把末尾 don't-care 砍掉。若遇到 `0`/`1` 不清零，`"01?0?"` 末尾只有一个 `?`，但 `__shift` 会变成 2（把中间的 `?` 也算进去），导致 `key/mask` 右移过量、固定位 `0` 被错位移出比较范围，匹配逻辑整体错乱。`mask`（每一位独立标记）负责处理「中间的 `?`」，`__shift`（只看末尾）负责处理「连续末尾的 `?`」，两者分工。

---

### 4.2 pattern_decode_hex：十六进制版编译器

#### 4.2.1 概念说明

`pattern_decode` 处理的是**二进制**模式（每个字符代表 1 位）。但当指令很长（比如某些 ISA 用 64 位编码）或人更习惯读十六进制时，写 32 个 `0/1/?` 容易眼花。`pattern_decode_hex` 是它的**十六进制孪生版本**：每个字符代表 **4 位**，合法字符是 `0-9`、`a-f` 和 `?`。

例如 `"1f?"` 表示 12 位：高 8 位是 `0x1f`（`0001 1111`），低 4 位是 don't-care。

#### 4.2.2 核心流程

逻辑与 `pattern_decode` 完全对称，只是「移位单位」从 1 位变成 4 位：

```
对每个字符 c：
  if c == '?' : __key <<=4 | 0;        __mask <<=4 | 0;       __shift += 4
  else        : __key <<=4 | (c 的数值); __mask <<=4 | 0xF;     __shift  = 0
finish: key = __key >> shift; mask = __mask >> shift; shift = __shift
```

其中 `c` 的数值：`'0'-'9'` → `c - '0'`；`'a'-'f'` → `c - 'a' + 10`。

#### 4.2.3 源码精读

[include/cpu/decode.h:62-86](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/include/cpu/decode.h#L62-L86) 是其实现，结构与 4.1.3 几乎逐行对应：

```c
// include/cpu/decode.h:62-86 （关键片段）
__attribute__((always_inline))
static inline void pattern_decode_hex(const char *str, int len,
    uint64_t *key, uint64_t *mask, uint64_t *shift) {
  uint64_t __key = 0, __mask = 0, __shift = 0;
#define macro(i) \
  if ((i) >= len) goto finish; \
  else { \
    char c = str[i]; \
    if (c != ' ') { \
      Assert((c >= '0' && c <= '9') || (c >= 'a' && c <= 'f') || c == '?', ...); \
      __key  = (__key  << 4) | (c == '?' ? 0 : (c >= '0' && c <= '9') ? c - '0' : c - 'a' + 10); \
      __mask = (__mask << 4) | (c == '?' ? 0 : 0xf); \
      __shift = (c == '?' ? __shift + 4 : 0); \
    } \
  }
  macro16(0);   // 最多 16 个十六进制位 = 64 位
  ...
}
```

注意三处与二进制版的差异：

1. 左移改为 `<< 4`，掩码位改为 `0xf`，`__shift` 每次 `+4`。
2. 合法字符断言放宽为十六进制字符集（注意只接受小写 `a-f`，大写会被 `Assert` 拒绝）。
3. 展开上限用 `macro16(0)`（16×4=64 位），而非二进制版的 `macro64(0)`。

> **关于使用情况**：当前四个 ISA（riscv32/x86/mips32/loongarch32r）的 `inst.c` 都调用 `INSTPAT`，而 `INSTPAT` 内部固定走的是二进制的 `pattern_decode`（见 4.3.3），**`pattern_decode_hex` 目前并没有被任何 ISA 调用**——它是一个「预留工具」。如果你更习惯十六进制，可以自己写一个 `INSTPAT_HEX` 宏把 `pattern_decode(pattern, ...)` 换成 `pattern_decode_hex(pattern, ...)`，框架完全支持。

#### 4.2.4 代码实践

**实践目标**：把 4.1.4 里的 `ebreak` 用十六进制手算一遍，体会两种编译器的等价性。

**操作步骤**：

1. `ebreak` 的 32 位编码 `0x00100073` 写成 8 位十六进制模式（全部固定位、无 `?`）：`"00100073"`。
2. 用 4.2.2 的流程手算：8 个十六进制字符，`__shift` 全程为 0；`__key` 就是 `0x00100073`；`__mask` 是 8 个 `0xf` = `0xFFFFFFFF`。
3. 对照 4.1.4 用二进制算出的结果。

**预期结果**：`key = 0x00100073`，`mask = 0xFFFFFFFF`，`shift = 0`——与二进制版完全一致。这说明两个编译器对同一编码给出相同的 `key/mask/shift`，只是输入格式不同。

#### 4.2.5 小练习与答案

**练习 1**：`pattern_decode_hex` 接受 `"A1"` 吗？为什么？

**参考答案**：不接受。其 `Assert` 只允许 `'0'-'9'`、`'a'-'f'`（小写）和 `'?'`。大写 `'A'` 不在白名单，会触发 `Assert` 失败。这是刻意收紧，避免大小写歧义。

**练习 2**：模式 `"ab?"` 的 `key/mask/shift` 是多少？

**参考答案**：`'a'=10`、`'b'=11`、`'?'`=don't-care。过程：key = `10 → (10<<4)|11 = 0xab → (0xab<<4)|0 = 0xab0`；mask = `0xf → 0xff → 0xff0`；shift 在末尾 `?` 处 `+4` = 4。finish：`key = 0xab0 >> 4 = 0xab`，`mask = 0xff0 >> 4 = 0xff`，`shift = 4`。运行时 `(inst >> 4) & 0xff == 0xab`，匹配高 8 位为 `0xab`、低 4 位任意的 12 位数。

---

### 4.3 INSTPAT 宏：运行时的位掩码匹配

#### 4.3.1 概念说明

`pattern_decode` 给出了「编译一个模式」的能力，但还差三件事把它变成一条可用的「指令识别语句」：

1. 在**哪台「机器」**上取指令字？→ 由各 ISA 用 `INSTPAT_INST(s)` 回答（riscv32 是 `(s)->isa.inst`，x86 是局部变量 `opcode`）。
2. 匹配上之后**执行什么**？→ 由各 ISA 用 `INSTPAT_MATCH(s, ...)` 回答（含解码操作数 + 执行体）。
3. 匹配上之后**如何跳过后续模式**？→ 用 `goto *(__instpat_end)` 直接跳到块尾（见 4.4）。

`INSTPAT(pattern, name, type, ...)` 就是把这三件事粘合起来的一条「指令表项」。`name` 是给人看的指令名（如 `"addi"`），`type` 是操作数类型（如 `I`/`U`/`S`），`...` 是执行体。

#### 4.3.2 核心流程

每次写一条 `INSTPAT(...)`，宏展开后做四步：

```
1. 编译期：pattern_decode(pattern) → key, mask, shift     （常量折叠）
2. 运行时：if ( ((inst >> shift) & mask) == key ) {         （一次移位+与+比较）
3. 运行时：     INSTPAT_MATCH(s, name, type, 执行体);         （解码操作数并执行）
4. 运行时：     goto *(__instpat_end);                       （跳到 INSTPAT_END，避免继续匹配）
            }
```

不匹配则自然落到下一条 `INSTPAT`，重复上述过程。最后一条通常是「全 `?`」的 `inv` 兜底，必然匹配，从而保证总有出口。

#### 4.3.3 源码精读

[include/cpu/decode.h:90-97](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/include/cpu/decode.h#L90-L97) 是 `INSTPAT` 宏本体：

```c
// include/cpu/decode.h:90-97
#define INSTPAT(pattern, ...) do { \
  uint64_t key, mask, shift; \
  pattern_decode(pattern, STRLEN(pattern), &key, &mask, &shift); \
  if ((((uint64_t)INSTPAT_INST(s) >> shift) & mask) == key) { \
    INSTPAT_MATCH(s, ##__VA_ARGS__); \
    goto *(__instpat_end); \
  } \
} while (0)
```

逐点解读：

1. **`pattern_decode(pattern, STRLEN(pattern), ...)`**：`pattern` 是字面量，`STRLEN` 是 `sizeof(pattern)-1`（含空格的长度，空格在 `pattern_decode` 内被跳过）。如前所述，这一步在编译期被折叠为三个常量。
2. **`INSTPAT_INST(s)`**：取「待匹配的指令字」。它由 ISA 定义——riscv32 在 [src/isa/riscv32/inst.c:53](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/isa/riscv32/inst.c#L53) 定义为 `#define INSTPAT_INST(s) ((s)->isa.inst)`；x86 在 [src/isa/x86/inst.c:149](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/isa/x86/inst.c#L149) 定义为 `opcode`（一个字节）。这让同一套 `INSTPAT` 既能匹配 32 位定长指令，也能匹配 x86 的单字节 opcode。
3. **`(((uint64_t)INSTPAT_INST(s) >> shift) & mask) == key`**：运行时唯一的关键判定——移位、掩码、比较。`uint64_t` 强转保证移位语义明确。
4. **`INSTPAT_MATCH(s, ##__VA_ARGS__)`**：匹配成功后执行的「解码 + 执行体」。`##__VA_ARGS__` 是 GNU 扩展，允许可变参数为空时吞掉前面的逗号。它同样由 ISA 定义，见下。
5. **`goto *(__instpat_end)`**：计算跳转，跳到本 `INSTPAT` 块的结尾（`__instpat_end` 是 `INSTPAT_START` 定义的「标号指针」，见 4.4）。这是「匹配即跳出，不再尝试后面的模式」的关键。
6. **`do { ... } while (0)`**：标准宏包装，使 `INSTPAT(...)` 像一条语句，可以安全地跟在 `if`/`else` 后面而不破坏语义。

再看 riscv32 如何定义 `INSTPAT_MATCH`——[src/isa/riscv32/inst.c:54-59](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/isa/riscv32/inst.c#L54-L59)：

```c
// src/isa/riscv32/inst.c:54-59
#define INSTPAT_MATCH(s, name, type, ... /* execute body */ ) { \
  int rd = 0; \
  word_t src1 = 0, src2 = 0, imm = 0; \
  decode_operand(s, &rd, &src1, &src2, &imm, concat(TYPE_, type)); \
  __VA_ARGS__ ; \
}
```

它的固定动作是：声明 `rd/src1/src2/imm` 四个局部变量 → 调用 `decode_operand(s, ..., concat(TYPE_, type))` 按类型把寄存器号和立即数填进去（`concat(TYPE_, I)` → `TYPE_I`，见 [src/isa/riscv32/inst.c:25-48](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/isa/riscv32/inst.c#L25-L48)）→ 执行你写的 `__VA_ARGS__` 体（如 `R(rd) = Mr(src1 + imm, 1)`）。

> 注意 `name` 参数在 riscv32 的 `INSTPAT_MATCH` 里**未被使用**（只是占位），它的价值在于「人眼读模式表时知道这一行是哪条指令」。x86 的 `INSTPAT_MATCH` 多一个 `width` 参数（[src/isa/x86/inst.c:150](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/isa/x86/inst.c#L150)），所以 x86 的 `INSTPAT` 项写成 `INSTPAT(pattern, name, type, width, body)`——这说明 `INSTPAT_MATCH` 是各 ISA 可自由定制的接缝。

最后看 riscv32 的完整模式表——[src/isa/riscv32/inst.c:61-68](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/isa/riscv32/inst.c#L61-L68)：

```c
// src/isa/riscv32/inst.c:61-68
INSTPAT_START();
INSTPAT("??????? ????? ????? ??? ????? 00101 11", auipc, U, R(rd) = s->pc + imm);
INSTPAT("??????? ????? ????? 100 ????? 00000 11", lbu  , I, R(rd) = Mr(src1 + imm, 1));
INSTPAT("??????? ????? ????? 000 ????? 01000 11", sb   , S, Mw(src1 + imm, 1, src2));
INSTPAT("0000000 00001 00000 000 00000 11100 11", ebreak, N, NEMUTRAP(s->pc, R(10)));
INSTPAT("??????? ????? ????? ??? ????? ????? ??", inv  , N, INV(s->pc));   // 兜底，必然匹配
INSTPAT_END();
```

最后一行 `inv` 的模式是「全部 `?`」（末尾连续 `?` 极多，`mask` 全 0），运行时 `(inst >> shift) & 0 == 0` 永远成立，所以它必然匹配，用于捕获所有未被前面规则识别的非法指令并调用 `INV(s->pc)`。**它必须是 `INSTPAT_END()` 前的最后一条。**

#### 4.3.4 代码实践

**实践目标**：用 `INSTPAT` 为 riscv32 新增 `addi` 指令，验证整条链路。

**操作步骤**：

1. 查 RISC-V 手册：`addi rd, rs1, imm` 的编码为 `funct3=000`、`opcode=0010011`，立即数为 12 位符号数（I 型）。模式串应为：
   ```
   "??????? ????? ????? 000 ????? 00100 11"
   ```
   （funct7/rs2/rs1/rd 全是 `?`，funct3=`000`，opcode=`0010011`）
2. 它的操作数类型是 `I`（有 `rs1` 和 12 位立即数），与已有的 `lbu` 同类型，可复用 `decode_operand` 的 `TYPE_I` 分支（[src/isa/riscv32/inst.c:42](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/isa/riscv32/inst.c#L42)）：`src1 = R(rs1)`、`imm = SEXT(BITS(i,31,20),12)`。
3. 在 [src/isa/riscv32/inst.c:62](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/isa/riscv32/inst.c#L62) 的 `INSTPAT` 列表里插一行（放在 `auipc` 后、`inv` 前）：

   ```c
   INSTPAT("??????? ????? ????? 000 ????? 00100 11", addi, I, R(rd) = src1 + imm);
   ```

   语义：`R(rd) = src1 + imm`，即 `rd = rs1 + 符号扩展的12位立即数`。
4. 重新 `make` 编译，跑一段含 `addi` 的程序观察是否正确执行。

**需要观察的现象 / 预期结果**：

- 编译应通过（`addi` 的 `I` 类型已被 `decode_operand` 支持，无需改其它代码）。
- 含 `addi a0, x0, 42`（把 42 写入 `a0`）之类的小程序应得到正确结果。
- **待本地验证**：若你已接入差分测试（见 u8-l24），可让 NEMU（DUT）与 spike/QEMU（REF）对照跑一段含 `addi` 的密集代码，确认寄存器一致。

> 本讲只负责「让指令被识别并执行」，关于 `decode_operand` 如何解码 I/U/S 型立即数、`SEXT` 如何符号扩展、`ebreak` 如何触发 `NEMUTRAP`，是下一讲 u5-l16（RISC-V 指令实现）的主题，届时会把 `add/lw/sw/jal` 等完整指令实现一遍。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `inv` 模式 `"(全是 ?)"` 末尾即使写成比 32 还长的 `?` 串也能正常工作？

**参考答案**：因为 `__shift` 只统计末尾连续 `?`，全 `?` 模式下 `__key`/`__mask` 全程为 0，`key=0`、`mask=0`，无论 `shift` 多大，运行时 `(inst >> shift) & 0 == 0` 恒为真。长度只影响 `shift` 的具体值，不影响「必然匹配」这一结论。

**练习 2**：如果把 `addi` 这一行放在 `inv` **之后**会发生什么？

**参考答案**：`inv` 必然匹配并 `goto *(__instpat_end)` 跳出，导致它之后的 `addi` 永远得不到执行机会——所有指令都会被判为 `INV`（非法）并触发 `invalid_inst`。因此「兜底规则必须放在最后」是 INSTPAT 表的硬性约定。

---

### 4.4 INSTPAT_START/END：用「标号作为值」收尾跳转

#### 4.4.1 概念说明

4.3 里反复出现的 `goto *(__instpat_end)` 是 GCC 的**计算跳转（computed goto）**：跳转目标不是一个写死的标号，而是一个「存放标号地址的变量」。配套的 `&&label` 是 GCC 的**标号作为值（labels as values）**扩展：把一个标号的地址存进一个指针变量。

`INSTPAT_START` / `INSTPAT_END` 用这两个扩展构建一个「块」：开头定义一个指向「块尾标号」的指针 `__instpat_end`，任何一条 `INSTPAT` 匹配成功后都 `goto *(__instpat_end)` 跳到块尾，从而避免继续尝试后面的模式。

#### 4.4.2 核心流程

```
INSTPAT_START()  →  展开为：
   {                                              ← 开一个新作用域 {
     const void * __instpat_end = &&__instpat_end_;   ← 取「块尾标号」地址存入指针

   INSTPAT(...) ← 多条，匹配则 goto *(__instpat_end) 跳出

INSTPAT_END()    →  展开为：
   __instpat_end_: ;                              ← 块尾标号（空语句）
   }                                              ← 关闭作用域 }
```

要点：

- `INSTPAT_START` 开 `{`、`INSTPAT_END` 关 `}`，两者必须成对，把一整张模式表包在一个独立作用域里。
- `__instpat_end` 是这个作用域内的局部指针，存放「块尾」的地址。
- `name` 参数决定块尾标号的名字：`INSTPAT_START(foo)` → 标号 `__instpat_end_foo`；`INSTPAT_START()`（空 name）→ 标号 `__instpat_end_`。

#### 4.4.3 源码精读

[include/cpu/decode.h:99-100](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/include/cpu/decode.h#L99-L100) 是这两个宏的全部：

```c
// include/cpu/decode.h:99-100
#define INSTPAT_START(name) { const void * __instpat_end = &&concat(__instpat_end_, name);
#define INSTPAT_END(name)   concat(__instpat_end_, name): ; }
```

逐点解读：

1. **`&&concat(__instpat_end_, name)`**：`concat(__instpat_end_, name)` 在 `name` 为空时拼接为标记 `__instpat_end_`（见 u1-l4 讲过的 `concat`）；`name=foo` 时为 `__instpat_end_foo`。`&&标号` 取该标号的运行时地址，存入局部指针 `__instpat_end`。
2. **`{ ... }`**：`INSTPAT_START` 以 `{` 结尾、`INSTPAT_END` 以 `}` 结尾，二者之间是一个完整 C 块。这就是为什么 4.3 里 `INSTPAT` 宏用 `goto *(__instpat_end)` 能引用到这个指针——它们处在同一个作用域。
3. **`concat(__instpat_end_, name): ;`**：定义块尾标号（`:` 前是标号名），紧跟一个空语句 `;`。匹配成功的 `INSTPAT` 跳到这里后，继续执行 `INSTPAT_END` 之后的代码（如 riscv32 的 `R(0) = 0;` 复位零寄存器，见 [src/isa/riscv32/inst.c:70](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/isa/riscv32/inst.c#L70)）。
4. **为什么需要 `name` 参数**：C 的标号是**函数作用域**（function scope），同一函数里不能有两个同名标号。riscv32、mips32、loongarch32r 都只有一个 `INSTPAT` 块，用空 name 没问题。但若你想在**同一个函数**里放两个块（比如先匹配一组前缀，再匹配主指令集），就必须给它们不同的 `name`，否则两个 `__instpat_end_` 标号冲突。

> **那 x86 里的两个 `INSTPAT_START()` 为什么不冲突？** 因为它们在**不同函数**里：一个在 `_2byte_esc`（[src/isa/x86/inst.c:181-183](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/isa/x86/inst.c#L181-L183)），一个在 `isa_exec_once`（[src/isa/x86/inst.c:193-217](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/isa/x86/inst.c#L193-L217)）。标号是函数作用域，跨函数天然不冲突。只有「同函数多块」才需要显式 `name`。

#### 4.4.4 代码实践

**实践目标**：用一个最小例子亲手看到「计算跳转」的效果，理解 `goto *ptr` 的威力。

**操作步骤**：

1. 阅读下面这段**示例代码**（不是项目源码，仅为演示标号作为值/计算跳转，可放进一个独立 `t.c` 用 `gcc -O2 t.c` 编译）：

   ```c
   /* 示例代码：演示 INSTPAT_START/END 的核心机制 */
   #include <stdio.h>
   int main(void) {
     int x = 2;                       // 想象成「指令字」
     const void * __instpat_end = &&done;   // 取「块尾标号」地址
     if (x == 1) { printf("one\n");  goto *(__instpat_end); }
     if (x == 2) { printf("two\n");  goto *(__instpat_end); }
     if (x == 3) { printf("three\n"); goto *(__instpat_end); }
     printf("other\n");               // 类似 inv 兜底
   done: ;                            // 块尾标号
     printf("fin\n");
     return 0;
   }
   ```

2. 编译运行：`gcc -O2 t.c && ./a.out`。
3. 把 `x` 分别改成 1、3、9，观察输出。

**需要观察的现象 / 预期结果**：

- `x=2` 时输出 `two` 然后 `fin`——匹配第二条即 `goto *(__instpat_end)` 跳到 `done`，跳过了后面的 `if`。
- `x=9` 时输出 `other` 然后 `fin`——所有固定规则不匹配，落到兜底。
- 这正是 `INSTPAT` 表的执行模型：自上而下逐条尝试，命中即跳到块尾，最后一条兜底。

**待本地验证**：注意必须用 GCC 或 Clang 才能编译（`&&label` 和 `goto *ptr` 是 GNU 扩展，标准 C 不支持）。若编译报错 `taking address of label is not allowed`，说明用了不支持该扩展的编译器，这正是 NEMU 强制要求 GCC/Clang 的原因之一。

#### 4.4.5 小练习与答案

**练习 1**：如果删掉 `goto *(__instpat_end)`（即 `INSTPAT` 宏里去掉那行），`auipc` 之后紧接着的 `lbu` 模式会发生什么？

**参考答案**：删掉跳转后，即使 `auipc` 已匹配并执行了它的执行体，控制流仍会**继续往下**尝试 `lbu`、`sb`……的匹配。虽然多数情况下后面的模式不会再次匹配（因为指令字段不同），但一旦某条指令同时满足多条规则（比如 `inv` 兜底必然匹配），就会被错误地执行两次，行为未定义。`goto *(__instpat_end)` 保证了「匹配即退出」的互斥语义。

**练习 2**：`INSTPAT_START` / `INSTPAT_END` 为什么不用普通 `break` + `switch`，而要用计算跳转？

**参考答案**：`switch` 只能用整数常量做 case 标签，而指令匹配是「位掩码比较」（`(inst>>shift)&mask==key`），不是对单一整数的相等比较，无法直接塞进 `switch`。用 `if` 逐条比较 + 计算跳转是最自然的写法；计算跳转相比「设一个 flag 然后 break 出 `for`」更直接、也更便于编译器把模式表优化成跳表/决策树。这是 GCC 扩展在 NEMU 里少有但关键的使用点。

---

## 5. 综合实践

把本讲四个模块串起来，完成下面这个「从模式串到可执行指令」的完整小任务：

**任务**：为 riscv32 新增 `addi` 指令，并把它当作一个微型工程来走一遍。

1. **写模式串**：根据 RISC-V 手册确定 `addi` 的固定位（funct3=000，opcode=0010011），写出模式串 `"??????? ????? ????? 000 ????? 00100 11"`。
2. **手算 key/mask/shift**：按 4.1.2 的逐位扫描法，算出这条模式的 `key`、`mask`、`shift`。（提示：末尾 opcode 是固定位，所以 `shift` 应为 0；固定字段是 funct3 的 3 位和 opcode 的 7 位，`mask` 就是这 10 个位为 1。）
3. **加表项**：在 [src/isa/riscv32/inst.c](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/isa/riscv32/inst.c) 的 `INSTPAT` 列表里插入 `addi`，类型用 `I`，执行体 `R(rd) = src1 + imm`，务必放在 `inv` 之前。
4. **复述执行链路**：写出一条 `addi a0, a1, 5` 被执行时，从 `isa_exec_once` 取指 → `INSTPAT_START` 进入块 → 模式匹配（编译期常量 + 运行时 `(inst>>shift)&mask==key`）→ `INSTPAT_MATCH` 调 `decode_operand` 填 `src1/imm` → 执行 `R(rd)=src1+imm` → `goto *(__instpat_end)` 跳出 → `R(0)=0` 复位 的完整过程。
5. **编译验证**：`make` 编译；如有差分测试环境，对照 REF 跑一段含 `addi` 的代码。**待本地验证**：若暂无测试环境，至少保证编译通过且 `si` 单步时寄存器按预期变化。

这个任务覆盖了本讲的全部四个最小模块：`pattern_decode`（第 2 步）、`INSTPAT`（第 3 步）、`INSTPAT_START/END`（第 4 步的块与跳转）、以及 `pattern_decode_hex`（第 2 步可选地用十六进制重算对照）。

## 6. 本讲小结

- NEMU 用**模式串**（`0`/`1`/`?`，空格仅分组）描述一条指令的固定位与 don't-care 位，是 ISA 无关的统一描述格式。
- `pattern_decode` 在**编译期**把模式串「编译」成 `key/mask/shift` 三个整数，靠 `macro64(0)` 的指数展开 + 常量折叠，运行时零字符串开销。
- 运行时匹配只需 `(inst >> shift) & mask == key`：`shift` 砍掉末尾连续 don't-care 低位，`mask` 挖空中间/高位的 don't-care，`key` 给出固定位的期望值。
- `INSTPAT` 宏把「编译模式 → 匹配 → 执行 `INSTPAT_MATCH` 体 → `goto` 跳出」粘成一条指令表项；`INSTPAT_INST`/`INSTPAT_MATCH` 是各 ISA 定制的接缝。
- `INSTPAT_START/END` 用 GCC「标号作为值 + 计算跳转」实现「匹配即跳出块尾」的互斥语义，`name` 参数用于同函数多块时区分标号。
- 模式表必须以「全 `?`」的 `inv` 兜底结尾，保证任何指令都有出口；这也是「`addi` 必须放在 `inv` 之前」的原因。
- `pattern_decode_hex` 是二进制版的十六进制孪生，目前未被内置 ISA 调用，是预留工具。

## 7. 下一步学习建议

本讲只解决了「认出一条指令并给它一个执行壳」，执行壳里用到的细节还没展开。建议接下来：

1. **u5-l15 寄存器实现**：本讲执行体里 `R(rd)`、`src1 = R(rs1)` 的 `R(i)` 就是 `gpr(i)`，去 `src/isa/riscv32/reg.c` 看 `gpr` 宏、`check_reg_idx` 越界检查与 `isa_reg_display` 如何实现。
2. **u5-l16 RISC-V 指令实现**：本讲的 `decode_operand` 与 `immI/immU/immS`、`SEXT` 符号扩展、`ebreak→NEMUTRAP` 都在那里系统讲解；建议跟着把 `add/lw/sw/jal/jalr/beq` 全部用 `INSTPAT` 实现一遍，让一个简单 RISC-V 程序跑通到 `HIT GOOD TRAP`。
3. **u5-l17 x86 变长指令对比**：去看 [src/isa/x86/inst.c](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/isa/x86/inst.c) 如何用 `INSTPAT_INST(s) = opcode`（单字节）配合 `goto again` 循环实现「逐字节取指 + 操作数大小前缀 + 两字节转义」，体会定长 ISA 与变长 ISA 在使用同一套 INSTPAT 时的差异。
4. **u8-l26 宏体系与条件编译**：如果你对本讲的 `concat`、`STRLEN`、`##__VA_ARGS__` 等「宏技巧」意犹未尽，那一讲会系统讲解 `MUXDEF/IFDEF/MAP` 等 NEMU 的宏基础设施。
