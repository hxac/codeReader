# 裸机 C 驱动与 `__DATE__`/`__TIME__` 宏

## 1. 本讲目标

本讲进入 fpga_base 的**软件侧**。前面几个单元我们一直在读 VHDL、TCL 脚本和打包元数据，看到的都是「硬件怎么看」。本讲要回答的问题是：**处理器（ARM 核或 MicroBlaze）上的 C 程序，如何通过内存映射 IO 读写 fpga_base 的寄存器，又如何把「软件自身的编译时间」烧进固件寄存器，从而和「固件编译时间」互相对照。**

读完本讲，你应该能够：

- 说清 `Xil_Out32` / `Xil_In32` / `Xil_Out8` / `Xil_In8` 在裸机系统里到底做了什么、与 AXI 事务的对应关系。
- 读懂 `fpga_base.h` 里那段吓人的预处理器三目运算链，明白它如何把 `__DATE__` 字符串 `"Jul 20 2026"` 在**编译期**就翻译成整数 `7`，而不是等到运行期再做字符串解析。
- 看清 `fpga_base_version()` 如何在处理器启动时把软件编译时间写回 `0x18`–`0x28` 五个寄存器，以及这些寄存器为何「可写」——它们在 HDL 里是直通回显接法。
- 自己写一小段 C 代码，调用 `fpga_base_version()` + `fpga_base_print()` 完成一次完整的「上报软件时间 + 打印全部系统信息」流程。

## 2. 前置知识

本讲默认你已经掌握 u2-l3 建立的**寄存器映射**（字节偏移 `0x00` 版本、`0x04`–`0x14` 固件日期、`0x18`–`0x28` 软件日期、`0x40` 项目串、`0x50` 设施串、`0x60` LED、`0x64` DIP 开关），以及 u3 讲过的**固件编译时间机制**（用 FDPE 触发器的 INIT 属性把时间烧进比特流）。这里补充几个本讲用到的软件侧概念。

### 2.1 裸机程序（bare-metal）

在 Zynq SoC 或 MicroBlaze 上，没有 Linux、没有操作系统，CPU 一上电就直接执行你编译出来的 elf 文件。这种「没有 OS 的程序」叫**裸机程序**。它直接访问物理地址、直接读写外设寄存器，没有任何内核驱动层。fpga_base 的 C 驱动就是给这种裸机程序用的（Vitis BSP 里的 standalone 库）。

### 2.2 内存映射 IO（Memory-Mapped IO）

ARM AXI 总线把「访问内存」和「访问外设寄存器」统一成同一套机制：**给每个外设分配一段物理地址空间**。CPU 对这段地址做一次普通的 load/store，总线事务就会被路由到对应外设。比如 fpga_base 被映射到 `0x43C00000`，那么 `*(volatile uint32_t*)0x43C00000 = 0x1234` 就等于「向 fpga_base 的版本寄存器写 0x1234」（实际版本寄存器只读，这里仅示意）。这就是**内存映射 IO**。`Xil_Out32` 等函数就是这套机制的封装。

### 2.3 C 预处理器宏 `__DATE__` 与 `__TIME__`

C 标准规定了几个预定义宏，编译器在**预处理阶段**（比编译更早）就会把它们替换成字符串字面量：

- `__DATE__` → 形如 `"Jul 20 2026"`，格式固定为 `Mmm dd yyyy`，其中 `Mmm` 是三字母月份缩写，`dd` 是日（个位数日前补空格，如 `" 1"`），`yyyy` 是四位年。
- `__TIME__` → 形如 `"14:30:30"`，格式固定为 `HH:MM:SS`，**始终零填充**（如 `"09:05:03"`）。

关键点：这两个宏的值在**编译那一刻**就定死了，写进了编译出来的 elf 文件里。所以它们记录的是「这段 C 代码被编译的时间」，正好可以用来给软件版本打时间戳——这正是 fpga_base 想要的「软件编译时间」。

### 2.4 编译期求值的字符算术

C 预处理器只懂「常量表达式」。但它可以做字符与整数的运算：`'7' - '0'` 在预处理期就被算成整数 `7`（因为字符常量 `'7'` 的 ASCII 值是 55、`'0'` 是 48）。再用三目运算符 `? :` 串起来，就能在编译期完成一次「字符串 → 整数」的翻译，完全不产生任何运行期代码。本讲最核心的技巧就是这个。

## 3. 本讲源码地图

本讲只精读两个文件，它们是 fpga_base 驱动的全部源码（驱动目录里还有一个 `Makefile` 和两个 `data/` 元数据文件，那些属于 u5-l2 的构建话题，本讲不展开）。

| 文件 | 行数 | 作用 |
| --- | --- | --- |
| [drivers/fpga_base/src/fpga_base.h](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/drivers/fpga_base/src/fpga_base.h) | ~115 | 头文件：寄存器偏移宏、`__DATE__`/`__TIME__` 解析宏、四个函数声明。 |
| [drivers/fpga_base/src/fpga_base.c](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/drivers/fpga_base/src/fpga_base.c) | ~92 | 实现：LED/DIP/版本/打印四个函数，全部基于 `Xil_*` 内存映射 IO。 |

此外，为了说清「软件时间回写」为何可读可写，本讲会引用一处 HDL 顶层连线作为证据：

| 文件 | 作用 |
| --- | --- |
| [hdl/fpga_base_v1_0.vhd](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/hdl/fpga_base_v1_0.vhd) | 第 271–275 行：软件日期寄存器（下标 6–10）的「直通回显」接法，这是它们能被 C 写入并读回的硬件根因。 |

## 4. 核心概念与源码讲解

本讲拆成三个最小模块，按「IO 访问 → 时间解析 → 时间回写」的因果顺序递进。

### 4.1 Xil 内存映射 IO：C 函数如何变成 AXI 事务

#### 4.1.1 概念说明

在裸机系统里，驱动函数要做的事其实极其朴素：**把一个 32 位整数写到某个地址，或从某个地址读回一个 32 位整数**。地址 = 外设基地址 + 寄存器偏移。`Xil_Out32(addr, val)` 写、`Xil_In32(addr)` 读，它们在 standalone BSP 里被实现为一次带 `volatile` 修饰的指针解引用，从而保证编译器不会把它优化掉、也不会乱序。

在 Zynq 上，这次指针访问会被 ARM 的 AXI 主端口发出去，经互联到达 fpga_base 的 AXI 从机（就是 u2-l1/u2-l2 讲的那个五通道从机），触发一次 AXI 写或读事务。换句话说，**一个 C 函数调用，最终变成一次 AXI 总线事务**——这正是 u2 单元讲的那一整套协议存在的意义。

#### 4.1.2 核心流程

```
C: Xil_Out32(base + C_SW_DATE_YEAR_OFS, 2026)
        │
        ├─ 预处理：base+C_SW_DATE_YEAR_OFS = base+0x18 (编译期已知偏移，运行期才知 base)
        │
        └─ 运行期：*(volatile uint32_t*)(base+0x18) = 2026
                │
                └─ ARM 发出 AXI 写事务：AWADDR=base+0x18, WDATA=2026, BRESP=OKAY
                        │
                        └─ fpga_base 的 psi_common_axi_slave_ipif 把它翻译成 o_reg_wdata(6)=2026, o_reg_wr(6)=1
```

注意偏移量（如 `0x18`）是**编译期常量**（由 `#define` 给出），而基地址 `base` 是**运行期变量**（由调用方传入，通常来自 `xparameters.h`）。两者相加才是真正的物理地址。

#### 4.1.3 源码精读

驱动头文件首先包含了 standalone BSP 的 IO 头文件，这是所有 `Xil_*` 函数的来源：

[fpga_base.h:22-24](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/drivers/fpga_base/src/fpga_base.h#L22-L24) —— 引入 `xil_io.h`（提供 `Xil_Out32`/`Xil_In32`/`Xil_Out8`/`Xil_In8`）、`stdint.h`（定宽整数 `uint32_t`/`uint8_t`）、`stdio.h`（标准 IO，供后续 `xil_printf`）。

最简单的两个 IO 包装函数展示了写和读的标准写法：

[fpga_base.c:17-25](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/drivers/fpga_base/src/fpga_base.c#L17-L25) —— `fpga_base_set_led` 用 `Xil_Out8` 写 LED 寄存器（`0x60`），`fpga_base_read_dip` 用 `Xil_In32` 读 DIP 开关寄存器（`0x64`）。

这里有一个值得留意的细节：**LED 用 8 位写（`Xil_Out8`），DIP 却用 32 位读（`Xil_In32`）却返回 `uint8_t`**。读 32 位再截断成 8 位是可行的，因为 DIP 开关只占用该寄存器的低 8 位（见 u2-l3 寄存器映射），高 24 位为 0；AXI 从机允许只读有效字节。这是一个**务实但不完全对称**的写法——读侧用了 32 位事务只是顺手，并不代表开关有 32 位。

偏移量本身由宏定义，集中管理，这是「硬件/软件契约」的软件那一半（u2-l3 已强调，改 HDL 寄存器下标必须同步改这里的宏）：

[fpga_base.h:99-100](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/drivers/fpga_base/src/fpga_base.h#L99-L100) —— `C_LED_OFS = 0x60`、`C_DIP_SW_OFS = 0x64`，与 HDL 寄存器下标 24、25 一一对应（下标 ×4 = 偏移）。

#### 4.1.4 代码实践

这是一个**源码阅读型实践**（驱动依赖 Vitis BSP 的 `xil_io.h`，无法在普通 Linux 环境编译，运行结果待本地验证）。

1. 实践目标：弄清 `base_addr` 这个参数从哪里来、`Xil_Out8` 与 `Xil_Out32` 在总线上产生的差异。
2. 操作步骤：
   - 打开 [fpga_base.c:17-20](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/drivers/fpga_base/src/fpga_base.c#L17-L20)，确认 `set_led` 只有一个参数 `base_addr` 和一个 `val`。
   - 回忆 u5-l2 将讲的 `data/fpga_base.tcl`：它会生成 `xparameters.h`，为该 IP 定义 `XPAR_FPGA_BASE_0_BASEADDR`。`base_addr` 的真正来源就是这个由 Vivado/Vitis 根据 Block Design 地址分配自动生成的宏。
3. 需要观察的现象（待本地验证）：在真实 Vitis 工程里，`base_addr` 应等于 `xparameters.h` 中 `XPAR_FPGA_BASE_0_BASEADDR` 的值（例如 `0x43C00000`）。
4. 预期结果：把一个 LED 点亮 → 调用 `fpga_base_set_led(base, 0xFF)`；读到 DIP 状态 → `uint8_t s = fpga_base_read_dip(base);`。
5. 思考题：如果把 `Xil_Out8` 改成 `Xil_Out32` 写 LED，硬件行为会变吗？（提示：LED 寄存器只取低 8 位，写 32 位的高 24 位会被忽略，功能等价，但总线事务的 `WSIZE` 不同。）

#### 4.1.5 小练习与答案

**练习 1**：`fpga_base_read_dip` 返回类型是 `uint8_t`，却调用了 `Xil_In32`。为什么不直接用 `Xil_In8`？
**答案**：两者都能得到正确低字节。用 `Xil_In32` 再截断是作者的选择，读 32 位事务在 Zynq 上和读 8 位一样快（单拍），代码也更短。严格说 `Xil_In8` 更贴合语义、总线 `ARSIZE` 更小，但功能等价。

**练习 2**：驱动里所有函数的第一个参数都叫 `base_addr`，从不出现具体地址。为什么这样设计？
**答案**：为了让同一份驱动能驱动挂在不同地址上的多个 fpga_base 实例。基地址由系统集成（Block Design 地址分配）决定，写在 `xparameters.h` 里，驱动不应硬编码。

---

### 4.2 预处理器日期解析：把字符串编译期翻译成整数

#### 4.2.1 概念说明

这是整个驱动里最巧妙、也最「不好读」的部分。目标很简单：`__DATE__` 是字符串 `"Jul 20 2026"`，但我们最终要往寄存器里写**整数** 2026、7、20。有两种做法：

- **运行期解析**：写一个 `strcmp`/`sscanf` 之类的函数，在 CPU 上跑一遍字符串解析。代价是要引入字符串库、占用代码空间、运行期才出结果。
- **编译期解析**：用预处理器三目运算链，在编译那一刻就把字符串算成整数常量，运行期直接是立即数。

fpga_base 选了第二种。结果是：`DATE_MONTH` 这个宏被用在 `Xil_Out32(..., DATE_MONTH)` 时，等价于 `Xil_Out32(..., 7)`——中间没有任何字符串、没有任何运行期代码。这是嵌入式里省 RAM、省 CPU 的经典技巧。

#### 4.2.2 核心流程

字符串布局先固定下来。`__DATE__` 共 11 个字符（注释里也贴心地标了索引）：

```
"Jul 20 2026"
 0123456789A   <- 注释里的索引（A 表示 10）
```

即：索引 `[0..2]` 是月份缩写、`[4..5]` 是日（个位数时 `[4]` 是空格）、`[7..10]` 是四位年。

解析思路是对每个字段写一个宏，用字符算术把字符转成数字：

- 月份：无法用算术转（`"Jul"` 不是数字），只能逐个比较三字母缩写，用三目链返回 1–12。
- 日/年/时/分：是数字字符，用 `'0'` 做减法还原数值；日和时存在「可能带前导空格」的情况，需先判空格再决定要不要乘 10。

字符转数字的数学本质：ASCII 表里数字字符 `'0'..'9'` 连续排列，所以任意数字字符 `c` 对应的数值就是 \(c - '0'\)。两位十进制数 `"ab"` 的数值是 \((a - '0')\times 10 + (b - '0')\)。

#### 4.2.3 源码精读

**年**：四位数字，每位减 `'0'` 再按位权相加。

[fpga_base.h:57](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/drivers/fpga_base/src/fpga_base.h#L57) —— `DATE_YEAR` 把索引 `[7][8][9][10]` 四个字符按千/百/十/个位加权求和。对 `"Jul 20 2026"`，索引 7='2'、8='0'、9='2'、10='6'，得 \(2\cdot1000+0\cdot100+2\cdot10+6=2026\)。

**月**：一段 12 分支的三目链，逐月匹配三字母缩写。

[fpga_base.h:58-70](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/drivers/fpga_base/src/fpga_base.h#L58-L70) —— `DATE_MONTH` 检查 `__DATE__[0..2]` 三个字符，匹配 `"Jan"`→1、`"Feb"`→2 …… `"Dec"`→12，全不匹配则兜底返回 0。注意六月 `"Jun"` 与七月 `"Jul"` 前两字符都是 `'J','u'`，靠第三个字符 `'n'`/`'l'` 区分；同理 `"Mar"`/`"May"` 都以 `'M','a'` 开头，靠第三字符区分。这是这条链必须写到第三个字符的原因。

**日**：个位数日前是空格，需先判空格。

[fpga_base.h:71](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/drivers/fpga_base/src/fpga_base.h#L71) —— `DATE_DAY` 先看 `[4]` 是否空格：是（如 `"Jul  1 2026"`，个位数日有两个空格）则直接取 `[5]` 的个位；否（如 `"Jul 20 2026"`）则 `([4]-'0')*10 + ([5]-'0')` 得 20。

**时、分**：从 `__TIME__`（`"HH:MM:SS"`）取。

[fpga_base.h:76-77](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/drivers/fpga_base/src/fpga_base.h#L76-L77) —— `TIME_HOUR` 取索引 `[0][1]`、`TIME_MINUTE` 取 `[3][4]`，写法和 `DATE_DAY` 同构（先判空格）。不过 `__TIME__` 按标准**始终零填充**（`"09:05"` 而非 `" 9: 5"`），所以这里的 `[0]==' '` 分支实际永远不会命中，属于作者沿用日/日一致写法的「防御性冗余」，不影响正确性。

#### 4.2.4 代码实践

这是本讲的**核心实践**（属源码阅读型，重点在手工推演而非运行）。

1. 实践目标：手工模拟预处理器，验证 `DATE_MONTH` 如何从 `"Jul 20 2026"` 得到 7。
2. 操作步骤：把 `"Jul 20 2026"` 写下来，标出 `__DATE__[0]`、`[1]`、`[2]` 分别是 `'J'`、`'u'`、`'l'`，然后逐分支过 [fpga_base.h:58-70](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/drivers/fpga_base/src/fpga_base.h#L58-L70) 的三目链。
3. 推演过程：
   - 第 1 分支 `[0]=='J' && [1]=='a' && [2]=='n'`？`'u'!='a'` → 否。
   - 第 2 分支 `[0]=='F'`？否。
   - 第 3、4、5 分支分别要求首字符 `'M'/'A'/'M'` → 否。
   - 第 6 分支 `[0]=='J' && [1]=='u' && [2]=='n'`？前两个满足，但 `'l'!='n'` → 否。
   - 第 7 分支 `[0]=='J' && [1]=='u' && [2]=='l'`？三个全中 → **返回 7**。
4. 预期结果：`DATE_MONTH` 在七月编译时被预处理成整数常量 `7`。同理 `"Jun ..."` 命中第 6 分支得 6，`"Jan ..."` 命中第 1 分支得 1。
5. 进阶观察：整条链在**预处理阶段**就被折叠成一个常数，编译后的 elf 里看不到 `"Jul"` 这个字符串，也看不到任何比较指令——这正是编译期解析的全部价值。运行结果待本地验证（可在 Vitis 里用 `xil_printf("%d", DATE_MONTH)` 反汇编观察是否变成立即数）。

#### 4.2.5 小练习与答案

**练习 1**：`DATE_YEAR` 用了四个字符做加权和。如果某年编译机日期变成 `"Jul 20 2036"`，结果是多少？
**答案**：索引 7='2'、8='0'、9='3'、10='6'，得 \(2000+0+30+6=2036\)。

**练习 2**：为什么 `DATE_MONTH` 必须比较三个字符，而不能只比较前两个？
**答案**：因为存在前两字符相同、第三字符不同的月份对：`"Jun"`/`"Jul"`（都以 `'J','u'` 开头）、`"Mar"`/`"May"`（都以 `'M','a'` 开头）。只比前两字符无法区分，必须看到第三字符。

**练习 3**：`TIME_HOUR` 里 `[0]==' '` 的判断会不会成立？
**答案**：按 C 标准 `__TIME__` 始终零填充（如 `"09:30:00"`），首字符恒为数字，该分支永不命中。这是与 `DATE_DAY` 风格一致的冗余判断，不影响正确性。

---

### 4.3 软件时间回写：让固件「记住」软件是何时编译的

#### 4.3.1 概念说明

u3 单元讲过：fpga_base 用 FDPE 触发器的 INIT 属性，把**固件**的编译时间烧进比特流，软件随时能从 `0x04`–`0x14` 五个只读寄存器读出来。这套机制固件侧已经完备。

但**软件侧**呢？一段 C 程序被编译后，没有任何「INIT」可烧——它只是 elf 里的代码。如果想知道「现在跑的这个 elf 是什么时候编译的」，就得靠 `__DATE__`/`__TIME__`：它们在编译那一刻被固化进 elf。fpga_base 的做法是：**在处理器启动早期，调用 `fpga_base_version()`，把这两个宏解析出的年月日时分，主动写进 `0x18`–`0x28` 这五个寄存器**。之后这五个寄存器就和 `0x04`–`0x14` 一样可读了。

这样设计的好处：上电后读一次 fpga_base，就能同时拿到**固件编译时间**和**软件编译时间**，两者一对照就知道「当前烧的 bitstream 和当前跑的 elf 是不是同一次发布配套的」——这是现场排障的关键信息。`fpga_base_print()` 就是把这两组时间连同版本号、项目名、设施名一起打印出来。

#### 4.3.2 核心流程

```
预处理期：__DATE__/__TIME__ → DATE_YEAR/MONTH/DAY/TIME_HOUR/MINUTE 五个整数常量
                                       │
运行期（上电）：fpga_base_version(base)
                                       │
                  ┌────────────────────┴────────────────────┐
                  ▼                                          ▼
  Xil_Out32(base+0x18, DATE_YEAR)   ...   Xil_Out32(base+0x28, TIME_MINUTE)
                  │
                  └─ AXI 写 → psi_common_axi_slave_ipif → o_reg_wdata(6..10), o_reg_wr(6..10)
                                  │
                                  └─ HDL 直通回显：reg_rdata(6..10) <= reg_wdata(6..10)   ← 这就是可读可写的根因
                                          │
后续任意时刻：fpga_base_print(base) 读 0x18..0x28 → 打印 "SW date/time: ..."
```

#### 4.3.3 源码精读

回写函数把五个时间分量分别写入五个连续寄存器：

[fpga_base.c:27-41](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/drivers/fpga_base/src/fpga_base.c#L27-L41) —— `fpga_base_version()` 依次把 `DATE_YEAR/MONTH/DAY`、`TIME_HOUR/MINUTE` 写入 `0x18/0x1C/0x20/0x24/0x28`。这五次写就是软件侧「上报自身编译时间」的全部动作。

这里有一个**值得诚实指出的小瑕疵**：这个函数用的是字面量 `0x00000018` 等，而**没有**用头文件里已经定义好的 `C_SW_DATE_YEAR_OFS` 等宏（见 [fpga_base.h:84-88](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/drivers/fpga_base/src/fpga_base.h#L84-L88)）。功能上完全等价（值相同），但失去了「改宏即同步」的好处——如果哪天 HDL 调整了软件日期寄存器下标，这里的字面量不会自动跟随。读源码时应留意这种「魔法数字」。

为什么这五个寄存器写下去就能读回来？根因在 HDL 顶层把它们接成「直通回显」：

[hdl/fpga_base_v1_0.vhd:271-275](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/hdl/fpga_base_v1_0.vhd#L271-L275) —— 软件日期寄存器（下标 6–10，即偏移 0x18–0x28）的读数据直接接到写数据上：`reg_rdata(6) <= reg_wdata(6)` …… `reg_rdata(10) <= reg_wdata(10)`。这正是 u2-l3 讲过的「直通回显即读写」——主机写什么，下次读就是什么。相比之下，版本寄存器（下标 0）和固件日期寄存器（下标 1–5）接的是独立信号源，所以只读。读写权限就这样由 HDL 接法天然形成，而非协议层的访问保护。

打印函数则把这些寄存器（含两组时间、版本号、项目/设施串）一并输出：

[fpga_base.c:43-89](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/drivers/fpga_base/src/fpga_base.c#L43-L89) —— `fpga_base_print()` 先打印版本号（`0x00`），再打印固件日期（`0x04`–`0x14`），再打印软件日期（`0x18`–`0x28`）。注意它读软件日期用的是 `C_SW_DATE_*_OFS` 宏（与 `fpga_base_version` 的字面量写法风格不一致，但偏移值一致，结果正确）。

打印项目/设施字符串时，用了一个双重循环按字节倒序还原（`Xil_In8(base + ofs + (word*4) + 3 - byte)`）——这是因为 HDL 把字符串按**大端**打包进 32 位寄存器（第 0 字符放最高字节，见 u2-l3），C 侧读取时必须把每个 4 字节字内倒序才能拼回原串。这一细节也呼应了 u5-l3 将讲的 JTAG 调试脚本里「4-word×4-byte 字节倒序」的同款处理。

#### 4.3.4 代码实践

本实践把本讲主线串起来：写一段调用 `fpga_base_version()` + `fpga_base_print()` 的最小 `main`（属示例代码，依赖 Vitis BSP，运行结果待本地验证）。

1. 实践目标：组装一个完整的「上电→上报软件时间→打印全部系统信息」流程。
2. 操作步骤：在一个 Vitis 裸机 Application 工程的 `main.c` 里写入下面的示例代码，编译后下载到 Zynq/MicroBlaze 运行。

   ```c
   /* 示例代码：调用 fpga_base 驱动的最小 main */
   #include "fpga_base.h"
   #include "xparameters.h"   /* 由 Vitis 根据 Block Design 自动生成 */

   int main(void)
   {
       /* 基地址来自 xparameters.h，名字由 data/fpga_base.tcl 生成（见 u5-l2） */
       uint32_t base = XPAR_FPGA_BASE_0_BASEADDR;

       /* 第 1 步：把软件编译时间写进 0x18-0x28（必须在 print 之前调用） */
       fpga_base_version(base);

       /* 第 2 步：打印版本、固件日期、软件日期、项目名、设施名 */
       fpga_base_print(base);

       return 0;
   }
   ```

3. 需要观察的现象（待本地验证）：串口应输出五行，形如：

   ```
   Version: 0x00000014
   FW date/time: 2026.7.20 09:30
   SW date/time: 2026.7.20 14:05
   Project: MyProject
   Facility: MyLab
   ```

4. 预期结果：`FW date/time` 是比特流综合时刻（来自 FDPE INIT，u3），`SW date/time` 是 elf 编译时刻（来自 `__DATE__`/`__TIME__`）。正常发布流程里两者应来自同一次构建，日期接近；若相差很远，说明 bitstream 与 elf 不配套。
5. 关键检查：若调换 `fpga_base_version` 与 `fpga_base_print` 的顺序，`SW date/time` 会打印出 `0.0.0 00:00`——因为还没写入，读回的是复位默认值（`ResetVal_g` 全 0，见 u2-l2）。这个实验能直观证明「软件时间确实是被 `fpga_base_version` 主动写进去的」。

#### 4.3.5 小练习与答案

**练习 1**：`fpga_base_version()` 用字面量 `0x18` 写，`fpga_base_print()` 用宏 `C_SW_DATE_YEAR_OFS` 读，两者混用。这样做有什么隐患？
**答案**：隐患是「魔法数字」：若 HDL 调整软件日期寄存器下标，并同步改了头文件宏，则 `print` 的读会自动跟随、但 `version` 的写不会，导致写读错位。应统一使用宏。

**练习 2**：为什么 `fpga_base_version()` 必须在 `fpga_base_print()` **之前**调用，否则软件日期打印为 0？
**答案**：软件日期寄存器不像固件日期那样有 INIT 硬连线，它在 HDL 里是「直通回显」——只有被主机写过才有值，否则读回的是复位默认值 0。所以必须先调用 `fpga_base_version` 写入，再读才有意义。

**练习 3**：固件日期（`0x04`–`0x14`）和软件日期（`0x18`–`0x28`）在 HDL 接法上有何根本区别？
**答案**：固件日期寄存器接的是 FDPE 触发器输出的常量（编译期由 INIT 烧定），只读；软件日期寄存器接的是 `reg_wdata`（`reg_rdata <= reg_wdata` 直通回显），可读可写。读写权限由 HDL 接法决定，而非 AXI 协议层。

## 5. 综合实践

把本讲三个模块串成一个完整的「软件侧时间上报与对照」任务。

**任务背景**：你接手了一块现场返修的板子，怀疑上面的 FPGA 比特流和 ARM 裸机程序不是同一次发布。你要用 fpga_base 驱动在 30 秒内判断它们是否配套。

**操作步骤**：

1. 在 Vitis 里新建裸机工程，引入 fpga_base 驱动（驱动如何被编译进 `libxil.a` 见 u5-l2）。
2. 写入第 4.3.4 节的示例 `main`，但增加一行对比逻辑（示例代码）：

   ```c
   /* 示例代码：在 print 之后，额外读出两组年份做对照 */
   uint32_t fw_year = Xil_In32(base + C_FW_DATE_YEAR_OFS);   /* 0x04，固件 */
   uint32_t sw_year = Xil_In32(base + C_SW_DATE_YEAR_OFS);   /* 0x18，软件 */
   xil_printf("Match: %s\r\n", (fw_year == sw_year) ? "yes" : "no");
   ```

3. 编译、下载、看串口。
4. 解读：若 `FW date/time` 与 `SW date/time` 完全一致（同一次 CI 构建），判定配套；若不一致，记录两组时间用于追溯。

**贯穿要点**：

- 第 4.1 模块解释了 `Xil_In32`/`Xil_Out32` 如何变成 AXI 事务——上面每一行读写都依赖它。
- 第 4.2 模块解释了 `__DATE__`/`__TIME__` 如何在编译期变成整数——`SW date/time` 的全部数值来源。
- 第 4.3 模块解释了软件时间为何要先写后读——直通回显接法决定了必须调用 `fpga_base_version`。

**预期结果**（待本地验证）：串口打印版本、固件日期、软件日期、项目名、设施名，外加一行 `Match: yes/no`。你能据此回答「这个 elf 和这个 bitstream 配套吗」。

## 6. 本讲小结

- fpga_base 的 C 驱动极薄：四个函数全部基于 `Xil_Out32`/`Xil_In32`/`Xil_Out8`/`Xil_In8` 内存映射 IO，本质是带 `volatile` 的指针解引用，一次调用对应一次 AXI 事务。
- `__DATE__`/`__TIME__` 是编译期固化的字符串常量；驱动用**预处理器三目运算链 + 字符算术**（`c - '0'`）把它们在编译期翻译成整数，运行期零开销、不占 RAM。
- `DATE_MONTH` 靠逐月比较三字母缩写返回 1–12，必须比较到第三个字符才能区分 `Jun`/`Jul`、`Mar`/`May` 等前缀冲突。
- `fpga_base_version()` 在上电时把软件编译时间写入 `0x18`–`0x28`；这五个寄存器在 HDL 里是 `reg_rdata <= reg_wdata` 的直通回显，故可读可写，而固件日期寄存器只读。
- `fpga_base_print()` 把版本、固件/软件两组日期、项目/设施串一并打印，其中字符串需按字节倒序还原（HDL 大端打包的对称处理）。
- 驱动里存在 `fpga_base_version` 用字面量、`fpga_base_print` 用宏的小不一致——读源码时应留意此类魔法数字。

## 7. 下一步学习建议

- **u5-l2（Vitis 驱动构建与元数据文件）**：本讲的 `XPAR_FPGA_BASE_0_BASEADDR`、`base_addr` 从哪来？答案在 `drivers/fpga_base/data/fpga_base.tcl`（生成 `xparameters.h`）、`data/fpga_base.mdd`（声明 `supported_peripherals`）和 `src/Makefile`（把 `.c` 编进 `libxil.a`）。建议接着读，补全「驱动如何被构建系统集成」这一环。
- **u5-l3（硬件调试 TCL 与 EPICS 集成）**：如果处理器上根本没跑程序、只想在 JTAG 侧读寄存器，或要把这些值接入 EPICS 控制系统，就进入这一讲。它的 `jtag_to_axi_master_cmd.tcl` 和 `FPGA_BASE.template` 与本讲的 C 驱动共用同一份寄存器映射（u2-l3），是同一套契约的三种「消费者」。
- **回头印证 u3**：本讲的「软件编译时间」是 `__DATE__` 路线；u3 讲的「固件编译时间」是 FDPE INIT 路线。两者机制完全不同（一个编译期 C 宏、一个综合后 TCL 写网表），但目标对称——让软硬件都能自我报告编译时间。值得对照重读 u3-l1/u3-l2 加深理解。
