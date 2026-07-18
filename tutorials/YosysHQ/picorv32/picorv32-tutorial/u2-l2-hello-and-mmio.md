# 第一个固件：Hello World 与内存映射 I/O

## 1. 本讲目标

学完本讲，你应当能够：

- 说清楚什么是「内存映射 I/O（Memory-Mapped I/O，MMIO）」，以及为什么 PicoRV32 不需要专门的 `printf` 库就能输出字符。
- 解释 PicoRV32 仿真模型里 `0x10000000` 这个地址的约定：它不是普通内存，而是被测试台「截获」并解释为字符输出的 UART 端口。
- 读懂 `firmware/print.c` 里 `print_chr` / `print_str` / `print_dec` / `print_hex` 四个函数的实现，特别是整型转字符串的两种经典技巧。
- 理解一个裸机 C 函数 `hello()` 是如何从复位向量一步步被调用起来的（承接 u2-l1）。
- 自己动手新增一个 `print_bin()` 函数，并把它接到固件里跑出来。

本讲只涉及固件侧的 C 代码和测试台的输出约定，不进入 CPU 内部（那是第 4、5 单元的事）。我们把 CPU 当成一个「会按地址读写」的黑盒来用。

## 2. 前置知识

### 2.1 内存映射 I/O 是什么

在一台普通 PC 上，打印一个字符通常要调用操作系统提供的函数（如 `printf`），操作系统再去驱动 UART 驱动、USB 驱动……层次很深。

但在一片「裸机（bare-metal）」CPU 上，没有操作系统。CPU 唯一会做的对外操作就是「按地址读写」。于是硬件设计者发明了一个约定：

> 把某些「特殊地址」不接到真正的存储器，而是接到外设（UART、定时器、GPIO……）。CPU 对这些地址做普通的 load/store，硬件（或仿真模型）就把这次读写翻译成对外设的操作。

这就是内存映射 I/O。它的好处是：**CPU 不需要任何新指令**就能控制外设，普通的 `sw`（store word）就是「输出一个字符」。

### 2.2 为什么必须用 `volatile`

考虑这样一行：

```c
*((volatile uint32_t*)OUTPORT) = ch;
```

这里的 `volatile` 关键字告诉编译器：「这次写内存有副作用，不要优化掉、不要合并、不要乱序」。如果去掉 `volatile`，开启 `-Os` 优化的 gcc 很可能会发现「这个地址没人读，写了也白发」，于是把整条 store 删掉——那你的字符就永远不会输出了。**所有 MMIO 访问都必须经过 `volatile` 指针**，这是裸机编程的铁律。

### 2.3 承接 u2-l1

u2-l1 已经讲过：固件由 `firmware/*.c` 和 `firmware/start.S` 经 riscv32 工具链编译成 `firmware.elf` → `firmware.bin` → `firmware.hex`，最终被测试台用 `$readmemh` 读入 128 KB 内存模型。`sections.lds` 把所有代码塞到从地址 0 开始的 96 KB 区间，并把 `start.o` 强制排最前，保证复位向量落在地址 0。本讲就从「地址 0 的第一条指令」继续往下走。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| `firmware/hello.c` | Hello World 入口，只有一行有效代码：`print_str("hello world\n")`。 |
| `firmware/print.c` | 本讲主角。定义输出端口 `OUTPORT=0x10000000`，实现 `print_chr/print_str/print_dec/print_hex`。 |
| `firmware/firmware.h` | 对上面所有函数（以及 irq/sieve/multest/stats）的接口声明，供各 `.c` 文件 `#include`。 |
| `firmware/sections.lds` | 内存布局：ORIGIN=0、LENGTH=96 KB，承接 u2-l1。 |
| `firmware/start.S` | 复位向量 `reset_vec` 与主程序 `start`，在 `ENABLE_HELLO` 下调用 `hello()`。 |
| `testbench.v` | 仿真台：`axi4_memory` 把对 `0x10000000` 的写解释为字符输出——这是 MMIO 约定的「硬件侧」。 |

## 4. 核心概念与源码讲解

### 4.1 内存映射 UART

#### 4.1.1 概念说明

PicoRV32 的固件要「打印字符」，靠的就是 MMIO。约定很简单：

- 地址 `0x10000000` 被预留为「UART 输出端口」。
- CPU 只要往这个地址写一个字（word），低 8 位就被当作一个 ASCII 字符送出去。
- 这个地址背后没有真正的 RAM，而是一个外设（在仿真里由测试台扮演，在真实 FPGA 里由 `simpleuart` 之类的模块扮演）。

注意：这是一个**写端口**——固件只往里写，从不读它。读 `0x10000000` 在当前测试台里会被当成「越界读」而终止仿真（见 4.1.3）。

#### 4.1.2 核心流程

从 CPU 发起一次字符输出，到屏幕上看到一个字符，经历三步：

1. **C 代码**：`*((volatile uint32_t*)0x10000000) = 'A';`
2. **CPU**：把它编译成一条 `sw`（store word）指令，目标地址 `0x10000000`，通过总线发出一次写事务。
3. **总线/外设**：测试台的 `axi4_memory` 在响应写事务时，发现地址等于 `0x10000000`，不写 RAM，而是 `$write("%c", ...)` 把低字节打印到 stdout。

整个过程 CPU 完全不知道自己在「打印字符」——它只是做了一次普通的 store。这正是 MMIO 的优雅之处。

#### 4.1.3 源码精读

先看固件侧如何定义端口并写一个字符：

[firmware/print.c:10-15](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/firmware/print.c#L10-L15) 定义了 `OUTPORT` 常量和最基础的 `print_chr`：把字符经 `volatile uint32_t*` 指针写到 `0x10000000`。

```c
#define OUTPORT 0x10000000

void print_chr(char ch)
{
    *((volatile uint32_t*)OUTPORT) = ch;
}
```

`print_chr` 是所有其他打印函数的「原子操作」——无论打印字符串还是数字，最终都归结为「往 `OUTPORT` 写一个个字符」。

再看测试台是如何接住这次写的（这是 MMIO 的「硬件侧」）：

[testbench.v:405-416](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/testbench.v#L405-L416) 在写事务的响应阶段，判断地址：若是 `0x10000000`，则把数据的低 8 位作为一个字符输出。

```verilog
if (latched_waddr == 32'h1000_0000) begin
    if (verbose) begin ... end
    else begin
        $write("%c", latched_wdata[7:0]);   // 非 verbose 模式直接打印字符
        $fflush();
    end
end
```

> 说明：`latched_waddr`/`latched_wdata` 是 AXI 写通道锁存的地址与数据（第 7 单元讲 AXI 时会细讲，这里只需知道「写事务发生时它们就是 CPU 发出的地址和数据」）。`verbose` 为真时会打印成 `OUT: 'A'` 这种带注释的形式；默认（非 verbose）就是原样 `$write` 出来。所以 `make test`（不带 `+verbose`）会把固件打印的内容原样输出到终端。

同一段代码里还有两个相邻的地址约定，构成完整的「PicoRV32 仿真内存映射」：

- `0x20000000`：写魔术数 `123456789` 表示「所有测试通过」。见 [testbench.v:418-420](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/testbench.v#L418-L420)。
- 小于 `128*1024` 的地址：正常读写 RAM（`memory[]`）。越界则 `$finish`，见 [testbench.v:386-393](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/testbench.v#L386-L393) 与 [testbench.v:399-404](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/testbench.v#L399-L404)。

于是 PicoRV32 仿真模型的内存映射可以这样画：

```
0x0000_0000 ┌──────────────────┐  RAM：指令+数据（冯·诺依曼）
            │  firmware.hex     │  由 $readmemh 载入，96 KB 代码 + 32 KB 栈
0x0002_0000 └──────────────────┘
            ⋮
0x1000_0000 ┌──────────────────┐  UART 输出端口（只写，低字节=ASCII）
0x2000_0000 ┌──────────────────┐  测试通过魔术端口（写 123456789）
```

#### 4.1.4 代码实践

**实践目标**：亲手验证「对 `0x10000000` 写 = 打印字符」这条约定，不依赖任何 C 库。

**操作步骤**：

1. 阅读上一讲（u2-l1）的 `testbench_ez.v`，确认你能跑 `make test_ez`。
2. 在 `start.S` 的 `start:` 标号下、`#ifdef ENABLE_HELLO` 之前，临时插三行（**示例代码**，修改后记得还原）：

   ```asm
   lui  a0, 0x10000000>>12   ; a0 = 0x10000000
   addi a1, zero, 'X'        ; a1 = 'X'
   sw   a1, 0(a0)            ; *(0x10000000) = 'X'
   ```

   这正是 `start.S` 末尾打印 `DONE` 的写法（见 [firmware/start.S:481-491](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/firmware/start.S#L481-L491)）。

3. 重新 `make test`（需要 u2-l1 装好的 riscv32 工具链）。

**需要观察的现象**：终端输出的开头会出现字符 `X`。

**预期结果**：因为这条 `sw` 在任何 C 代码之前执行，`X` 会出现在 `hello world` 之前。

> 待本地验证：若你的工具链尚未装好，本步无法执行；可改为阅读 `start.S:481-491`，对照上面的三行手写汇编，确认它们等价。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `print_chr` 里的 `volatile` 去掉，开启 `-Os` 后可能发生什么？

**答案**：gcc 可能判定该 store 无后续读取、属于死存储，从而删除它；于是字符不再输出。这正是 MMIO 必须用 `volatile` 的原因。

**练习 2**：`0x10000000` 这个地址是 RISC-V 规范强制的吗？换成 `0x12340000` 行不行？

**答案**：不是强制约定，是本项目自定义的。理论上换地址可以——但要同时改 `OUTPORT` 和测试台 `axi4_memory` 里 `32'h1000_0000` 的判断，两边保持一致才行。

---

### 4.2 整型转字符串

#### 4.2.1 概念说明

`print_chr` 只能输出单个字符。要打印 `hello world` 这种字符串，就逐字符输出；要打印数字 `42`，就得先把整数 `42` 转换成两个字符 `'4'`、`'2'` 再输出。这一节讲两种把无符号整数转成 ASCII 字符串的经典做法：

- **十进制（`print_dec`）**：用「反复除以 10 取余数」从低位到高位生成数字，再倒着输出。
- **十六进制（`print_hex`）**：从高位到低位，每次取 4 位（一个 nibble），直接查表。

两者都体现了同一条铁律：**ASCII 数字字符 = 数字值 + '0'**（即 `0x30`）。

#### 4.2.2 核心流程

**十进制打印** `print_dec(val)` 的流程：

```
1. 开一个 10 字节缓冲区 buffer（32 位无符号数最多 10 位十进制数字）。
2. 正向循环：反复  digit = val % 10;  val = val / 10;
   把 digit（原始值 0..9，注意不是 ASCII）依次存进 buffer，直到 val 变 0。
   特判：若一开始 val 就是 0，至少存一个 0，保证打印 "0"。
3. 反向循环：指针从尾往头走，每个数字 + '0' 输出。
```

为什么 32 位无符号数最多 10 位十进制数字？因为其最大值为 \(2^{32}-1 = 4294967295\)，即

\[
\lfloor \log_{10}(2^{32}-1) \rfloor + 1 = 10.
\]

所以 `char buffer[10]` 刚好够用。

**十六进制打印** `print_hex(val, digits)` 的流程：

```
对 i 从 (4*digits - 4) 递减到 0，步长 -4：
    nibble = (val >> i) 的低 4 位      // 每次取一个十六进制位
    输出 "0123456789ABCDEF"[nibble]    // 直接用 nibble 索引字符串
```

`digits` 表示要打印几位十六进制；每位 4 bit，所以起始位移是 `4*digits - 4`。从高位往低位走，输出顺序天然正确，不需要反转。

#### 4.2.3 源码精读

先看字符串打印（最简单）：

[firmware/print.c:17-21](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/firmware/print.c#L17-L21) 用一个 `while (*p != 0)` 循环把 C 字符串逐字符写到 `OUTPORT`，直到遇到结尾的 `\0`。

```c
void print_str(const char *p)
{
    while (*p != 0)
        *((volatile uint32_t*)OUTPORT) = *(p++);
}
```

再看十进制打印，这是本讲最有意思的一段：

[firmware/print.c:23-34](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/firmware/print.c#L23-L34) 实现了「取余 + 倒序输出」的整型转字符串技巧。

```c
void print_dec(unsigned int val)
{
    char buffer[10];
    char *p = buffer;
    while (val || p == buffer) {     // ① 生成数字（低位在前）
        *(p++) = val % 10;
        val = val / 10;
    }
    while (p != buffer) {            // ② 反向输出，转成 ASCII
        *((volatile uint32_t*)OUTPORT) = '0' + *(--p);
    }
}
```

两个要点：

1. 循环条件 `val || p == buffer`：`val || p == buffer` 表示「val 非零，或者还一个数字都没存」。后者专门处理 `val == 0` 的情况——此时若没有 `p == buffer`，循环一次都不进，结果什么都不打印；加上之后会存一个 `0`，正确打印出 `"0"`。
2. 第①步存的是**原始数字值**（0..9），不是 ASCII；第②步 `--p` 倒着走并加 `'0'` 转成 ASCII。因为存的时候是低位在前，倒着输出就变成了高位在前，顺序正确。

十六进制打印则换了一种思路，直接从高位生成：

[firmware/print.c:36-40](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/firmware/print.c#L36-L40) 用位移 + 字符串查表实现十六进制输出。

```c
void print_hex(unsigned int val, int digits)
{
    for (int i = (4*digits)-4; i >= 0; i -= 4)
        *((volatile uint32_t*)OUTPORT) = "0123456789ABCDEF"[(val >> i) % 16];
}
```

这里 `(val >> i) % 16` 把想要的那个 nibble 移到最低 4 位并取出来（等价于 `& 0xF`），再用它去索引字符串字面量 `"0123456789ABCDEF"`。因为是从最高 nibble 开始递减，输出顺序天然是从高到低，无需倒序。注意它依赖调用者传入合理的 `digits`（例如 8 表示打印 32 位全 8 位十六进制）。

> 小结：`print_dec` 用「取余 + 倒序」是因为十进制每位宽度不固定（无法简单位移）；`print_hex` 能用「位移 + 查表」是因为每 4 bit 恰好一个十六进制位，对齐得很整齐。

#### 4.2.4 代码实践

**实践目标**：自己写一个 `print_bin(uint32_t val)`，把 32 位整数按二进制打印出来（32 个 `0`/`1`）。

**操作步骤**：

1. 在 `firmware/print.c` 末尾仿照 `print_hex` 增加函数（**示例代码**）：

   ```c
   void print_bin(uint32_t val)
   {
       for (int i = 31; i >= 0; i--)
           *((volatile uint32_t*)OUTPORT) = '0' + ((val >> i) & 1);
   }
   ```

   思路与 `print_hex` 一致：从高位（bit 31）到低位（bit 0），每次取 1 位，加 `'0'` 转成 ASCII。

2. 在 `firmware/firmware.h` 的 `// print.c` 段落里加上声明：

   ```c
   void print_bin(uint32_t val);
   ```

   对应 [firmware/firmware.h:17-21](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/firmware/firmware.h#L17-L21) 这一组声明。

3. 在 `firmware/hello.c` 的 `hello()` 里调用它（见 4.3.3 的调用方式），例如：

   ```c
   print_str("5 = ");
   print_bin(5);
   print_chr('\n');          // 应输出 00000000000000000000000000000101
   ```

4. `make test`。

**需要观察的现象**：`hello world` 之后出现 `5 = ` 紧跟 32 位二进制串。

**预期结果**：`5 = 00000000000000000000000000000101`。因为 `5 = 0b101`，高位全 0。

> 待本地验证：需要 u2-l1 的工具链；若未安装，可对照 `print_hex` 的循环结构手动推演 `(5 >> 31) & 1` … `(5 >> 0) & 1` 的结果，确认只有末三位是 `101`。

#### 4.2.5 小练习与答案

**练习 1**：`print_dec(0)` 会打印什么？为什么不会什么都不打印？

**答案**：打印 `"0"`。因为第一个循环的条件里有 `p == buffer`，即使 `val==0` 也会进入一次循环，存一个 `0`。

**练习 2**：`print_hex` 里把 `(val >> i) % 16` 换成 `(val >> i) & 0xF` 行不行？为什么作者用了 `% 16`？

**答案**：完全等价，`& 0xF` 通常更地道、也更快。`% 16` 只是作者的一种写法，语义相同（因为对 2 的幂，取余等于按位与低位）。

**练习 3**：`print_dec` 为什么不能用 `print_hex` 那种「位移 + 查表」的思路？

**答案**：十进制每一位并不对应固定数量的二进制位（10 不是 2 的幂），无法靠整数位移直接取出「第 k 位十进制数字」；只能靠反复除以 10 取余数。十六进制则每位恰好 4 bit，可以对齐位移。

---

### 4.3 裸机 C 入口

#### 4.3.1 概念说明

我们写的 `hello()` 是一个普通 C 函数。但 CPU 复位后并不会「自动知道」要调用 `hello()`——它只是从复位地址（`PROGADDR_RESET`，仿真里通常是 0）开始机械地取指执行。所以必须有一段启动代码（startup code），把 CPU 从「刚复位的原始状态」引导到「能调用 C 函数的环境」。

在 PicoRV32 固件里，这段引导代码就是汇编文件 `firmware/start.S`。它做三件事：

1. 提供**复位向量** `reset_vec`（放在地址 0）。
2. 初始化运行环境（清零寄存器、设置栈指针 `sp`）。
3. 调用各个 C 函数（`hello`、`sieve`、`multest`、`stats`）。

理解这条链路，才能解释「为什么我改了 `hello.c`，`make test` 就能看到新输出」。

#### 4.3.2 核心流程

从上电到 `hello world` 打印出来，调用链如下：

```
地址 0: reset_vec          (start.S)
   │  waitirq / maskirq / j start
   ▼
start:                     (start.S)
   │  清零 x1..x31
   │  #ifdef ENABLE_HELLO:
   │      sp = 128*1024            ; 设置栈顶
   │      jal ra, hello            ; 调用 C 函数
   ▼
hello():                   (hello.c)
   │  print_str("hello world\n")
   ▼
print_str():               (print.c)
   │  循环 *((volatile*)0x10000000) = *p++
   ▼
testbench axi4_memory:     (testbench.v)
   │  $write("%c", ...)            ; 字符出现在终端
```

两个关键细节：

- **栈指针**：C 函数（尤其是有局部变量、函数调用的）需要栈。`hello()` 本身栈开销很小，但 `print_dec` 有 `char buffer[10]` 局部数组，也依赖栈。所以调用 C 代码前必须设好 `sp`。这里设成 `128*1024`（128 KB 处），即 RAM 的顶端往下长——因为 `sections.lds` 里代码只用了前 96 KB，剩下 32 KB 留给栈，刚好和 [sections.lds:10-14](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/firmware/sections.lds#L10-L14) 注释里的「leave at least 32k for stack」对上。
- **条件编译**：调用 `hello` 这一段被包在 `#ifdef ENABLE_HELLO` 里。`ENABLE_HELLO` 在 `start.S` 顶部默认定义，所以默认会打印 `hello world`；把它注释掉重新编译，就不会打印了。

#### 4.3.3 源码精读

`hello.c` 本身极其简短：

[firmware/hello.c:10-13](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/firmware/hello.c#L10-L13) 整个 `hello` 函数就是把字符串交给 `print_str`。

```c
void hello(void)
{
    print_str("hello world\n");
}
```

它的声明在头文件里：[firmware/firmware.h:23-24](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/firmware/firmware.h#L23-L24)。`hello.c` 第一行 `#include "firmware.h"` 就是为了拿到 `print_str` 的原型。

真正「把 hello 串起来」的是 `start.S`。先看复位向量：

[firmware/start.S:41-45](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/firmware/start.S#L41-L45) 是位于地址 0 的复位向量，注释提醒「no more than 16 bytes here」。

```asm
reset_vec:
    // no more than 16 bytes here !
    picorv32_waitirq_insn(zero)
    picorv32_maskirq_insn(zero, zero)
    j start
```

这里两条 `picorv32_*_insn` 是 PicoRV32 的自定义中断指令（第 6 单元会专门讲，现在只需知道它们用来初始化中断状态），然后 `j start` 跳到主程序。因为 `sections.lds` 把 `start*(.text)` 排在最前，`reset_vec` 就落在地址 0，也就是 CPU 复位后取的第一条指令。

再看主程序里调用 `hello` 的片段：

[firmware/start.S:379-385](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/firmware/start.S#L379-L385) 在清零所有寄存器后，设置栈指针并调用 `hello`。

```asm
#ifdef ENABLE_HELLO
    /* set stack pointer */
    lui sp,(128*1024)>>12

    /* call hello C code */
    jal ra,hello
#endif
```

- `lui sp,(128*1024)>>12`：`lui` 把立即数左移 12 位装入高位，`(128*1024)>>12 == 32`，所以 `sp = 32 << 12 = 0x20000 = 128 KB`，即 RAM 顶端。
- `jal ra,hello`：调用 `hello`，返回地址存入 `ra`。`hello` 返回后继续往下执行后续测试。

而 `ENABLE_HELLO` 的默认定义在文件开头：

[firmware/start.S:8-13](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/firmware/start.S#L8-L13) 集中定义了若干 `ENABLE_*` 宏，控制各功能是否参与编译。

```c
#define ENABLE_QREGS
#define ENABLE_HELLO
#define ENABLE_RVTST
...
```

最后，`hello.o`、`print.o` 是怎么进到 `firmware.elf` 的？看 Makefile：

[Makefile:15](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/Makefile#L15) 把 `hello.o`、`print.o` 列进 `FIRMWARE_OBJS`；[Makefile:109-113](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/Makefile#L109-L113) 把这些 `.o` 与 `start.o`、`tests/*.o` 一起链接成 `firmware.elf`。这就是「改了 `hello.c`，`make` 会重新编译 `hello.o` 并重新链接」的根因——make 的依赖链。

#### 4.3.4 代码实践

**实践目标**：通过条件编译开关，亲手控制 `hello world` 是否打印，验证 `start.S → hello()` 这条调用链。

**操作步骤**：

1. 打开 `firmware/start.S`，找到第 9 行 `#define ENABLE_HELLO`。
2. 把它注释掉：`// #define ENABLE_HELLO`。
3. `make clean && make test`。

**需要观察的现象**：对比开关前后的输出开头。

**预期结果**：注释掉之前，输出第一行是 `hello world`；注释掉之后，`hello world` 消失，直接进入后续测试输出。这证明 `hello()` 确实是被 `start.S` 的 `jal ra,hello` 调用的，而不是「 magically 自动运行」。

> 待本地验证：需要工具链。完成后请**还原**这行 `#define`，避免影响后续讲义的实验。

#### 4.3.5 小练习与答案

**练习 1**：为什么调用 `hello()` 之前必须先 `lui sp,...` 设栈指针？

**答案**：C 函数需要栈来保存返回地址、局部变量（如 `print_dec` 的 `buffer[10]`）。`sp` 没设好就调用 C 函数，栈操作会写到不可预测的地址，通常导致崩溃或乱写内存。

**练习 2**：`reset_vec` 为什么要限定「no more than 16 bytes」？

**答案**：复位向量区通常只预留很小的固定空间（这里是 16 字节 = 4 条 32 位指令）。超过就会和后面的内容（如 `irq_vec`，见 `start.S` 的 `.balign 16`）重叠。所以只能放最精简的几条指令然后 `j start` 跳走。

**练习 3**：如果我在 `hello.c` 里直接写 `printf("hello world\n")`，能跑吗？

**答案**：不能。固件是用 `-nostdlib -ffreestanding` 编译的（见 [Makefile:110-112](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/Makefile#L110-L112)），没有 C 标准库，也就没有 `printf`。这就是为什么项目要自己实现 `print_str/print_dec/print_hex`。

---

## 5. 综合实践

把本讲三个模块串起来，做一个「迷你调试打印」函数。

**任务**：在 `firmware/print.c` / `firmware/hello.c` 里实现并调用一个函数 `print_dbg(const char *name, uint32_t val)`，它的输出格式为：

```
name = <十进制> (0x<8位十六进制>)
```

例如 `print_dbg("answer", 42)` 应输出 `answer = 42 (0x0000002A)`。

**提示**：

1. 在 `print.c` 实现，复用本讲学过的 `print_str`、`print_dec`、`print_hex`：

   ```c
   void print_dbg(const char *name, uint32_t val)
   {
       print_str(name);
       print_str(" = ");
       print_dec(val);
       print_str(" (0x");
       print_hex(val, 8);
       print_chr(')');
       print_chr('\n');
   }
   ```

   （**示例代码**）

2. 在 `firmware.h` 加声明 `void print_dbg(const char *name, uint32_t val);`。
3. 在 `hello()` 里调用 `print_dbg("answer", 42);` 等。
4. `make test` 验证。

**验收标准**：

- 终端能看到形如 `answer = 42 (0x0000002A)` 的行。
- 你能解释每一行 `print_*` 调用对应哪一段源码、最终都归结为「往 `0x10000000` 写字符」。
- 你能指出 `42` 在 `print_dec` 里经历了 `42%10=2`、`4%10=4` 两轮，缓冲区存的是 `[2,4]`，倒序输出 `'4'`、`'2'`。

这个练习同时用到了 MMIO 约定（4.1）、整型转字符串（4.2）和裸机 C 入口（4.3），是把三者打通的最小闭环。

## 6. 本讲小结

- **MMIO**：PicoRV32 把 `0x10000000` 约定为只写的 UART 输出端口；CPU 只需做普通 `sw`，测试台 `axi4_memory` 就把低字节当字符打印。`0x20000000` 是「测试通过」魔术端口。
- **`volatile` 是 MMIO 的命门**：所有对外设地址的访问必须经 `volatile` 指针，否则会被优化掉。
- **整型转字符串两种技巧**：`print_dec` 用「除 10 取余 + 倒序输出」；`print_hex` 用「按 4 bit 位移 + 字符串查表」。两者都遵循「数字 + '0' = ASCII」。
- **裸机入口链路**：`reset_vec(地址0) → start(清零寄存器+设 sp) → jal hello → print_str → 往 0x10000000 写字符`。`hello()` 由 `start.S` 在 `ENABLE_HELLO` 下显式调用。
- **为什么没有 `printf`**：固件用 `-nostdlib -ffreestanding` 编译，无标准库，所以输出函数全部手写，集中在 `print.c`。
- **内存映射全图**：`0x0–0x1FFFF` 为 RAM（代码+栈），`0x10000000` 为 UART，`0x20000000` 为测试通过端口。

## 7. 下一步学习建议

至此，你已经能让 PicoRV32「说话」了——从 C 源码到终端字符的整条链路已经打通。接下来的学习方向：

- **横向扩展（同层固件）**：阅读 `firmware/sieve.c`、`firmware/multest.c`、`firmware/stats.c`，它们都用本讲的 `print_*` 输出结果，是更复杂的裸机 C 例子；`firmware/irq.c` 则展示中断处理，配合 `start.S` 的 `irq_vec` 一起看。
- **向下深入 CPU（第 3 单元起）**：本讲把 CPU 当黑盒，只用了它的「按地址读写」能力。下一单元（u3）将打开 `picorv32.v`，先看它的参数和端口外观，理解「一次 `sw` 到 `0x10000000`」在 CPU 内部是如何变成总线事务的。
- **具体到本讲相关源码**：想了解 `0x10000000` 在真实 FPGA 上如何变成串口字符，可读 `picosoc/simpleuart.v`（第 8 单元 PicoSoC 会用到）；想了解 AXI 写通道的握手时序，可先翻 `testbench.v` 的 `axi4_memory`（第 7 单元细讲）。
