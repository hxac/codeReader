# 寄存器实现

## 1. 本讲目标

本讲是 ISA 实现单元（U5）的第二篇，承接 u5-l14 建立的「ISA 抽象与 `CPU_state` 定义」。上一讲我们看清了 NEMU 如何用 `concat(__GUEST_ISA__, _CPU_state)` 拼出当前 ISA 的寄存器结构体；本讲要回答的是：**这套寄存器在代码里到底怎么访问、怎么展示、怎么从名字解析回值、上电时又被初始化成什么样**。

读完本讲，你应当能够：

- 说清 `gpr(idx)` 宏与 `check_reg_idx` 如何协作完成「带越界检查的寄存器访问」，以及 `CONFIG_RT_CHECK`/`CONFIG_RVE` 如何改变其行为。
- 看懂 `regs[]` 寄存器名表与 `reg_name()` 的对应关系，理解 RISC-V「x0 恒为零」在表中的体现。
- 动手实现两个 PA 必做函数：`isa_reg_display()`（打印所有寄存器）与 `isa_reg_str2val()`（名字→值），并把 `isa_reg_display` 接入 SDB 的 `info r` 命令。
- 解释 `restart()` 为何只显式置 `pc` 与 `gpr[0]`，以及内置镜像 `img[]` 那几条指令在自检什么。

## 2. 前置知识

在进入源码前，先用通俗语言铺几个概念。

- **寄存器（register）**：CPU 内部最快的存储单元，相当于 CPU 的「手边抽屉」。RISC-V 有 32 个通用寄存器（General Purpose Register, GPR），记作 `x0`~`x31`，每个宽度与机器字长一致（riscv32 下为 32 位）。
- **x0 恒为零**：RISC-V 硬件规定 `x0` 永远是 0，对它的写操作被丢弃。这给指令集设计带来方便——需要常数 0 时直接用 `x0`，不必单独设一条「产生 0」的指令。在 NEMU 里这个不变量靠「约定 + 显式复位」维护。
- **ABI 名（ABI name）**：程序员写汇编时很少写 `x5`、`x10`，而是写 `t0`、`a0` 这类「角色名」——`t` 系列是临时寄存器，`a` 系列是参数寄存器，`s` 系列是需要被调用方保存的寄存器。`x0` 的 ABI 名就是 `$0`。`regs[]` 表就是把「索引」与「ABI 名」对应起来的查表数据。
- **复位向量（reset vector）**：CPU 上电后取第一条指令的地址。NEMU 中由 `RESET_VECTOR` 宏给出，riscv32 系统模式下默认是 `0x80000000`。
- **`word_t` 与 `FMT_WORD`**：`word_t` 是 NEMU 的「机器字类型」，riscv32 下即 `uint32_t`（见 u5-l14 / u4-l12）；`FMT_WORD` 是与宽度自适应的 `printf` 格式串，riscv32 下展开为 `"0x%08" PRIx32`，用来规范地打印寄存器值。

本讲默认你已经读过 u5-l14（知道 `CPU_state = { word_t gpr[32]; vaddr_t pc; }` 与 `concat` 拼接机制）和 u2-l5（SDB 命令表 `cmd_table` 的表驱动分发）。

## 3. 本讲源码地图

本讲聚焦 riscv32 这一套实现，涉及三个文件：

| 文件 | 作用 |
| --- | --- |
| [src/isa/riscv32/reg.c](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/isa/riscv32/reg.c) | 寄存器名表 `regs[]`、待实现的 `isa_reg_display` / `isa_reg_str2val`。 |
| [src/isa/riscv32/local-include/reg.h](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/isa/riscv32/local-include/reg.h) | `check_reg_idx` 越界检查、`gpr(idx)` 访问宏、`reg_name(idx)` 名字查表。 |
| [src/isa/riscv32/init.c](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/isa/riscv32/init.c) | 内置镜像 `img[]`、`restart()` 上电初始化、`init_isa()` 装配入口。 |

辅助理解的契约与类型定义：

| 文件 | 作用 |
| --- | --- |
| [include/isa.h](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/include/isa.h) | 抽象层声明 `extern CPU_state cpu;`、`isa_reg_display`、`isa_reg_str2val`。 |
| [src/isa/riscv32/include/isa-def.h](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/isa/riscv32/include/isa-def.h) | `CPU_state` 结构体定义（`gpr[32] + pc`）。 |
| [include/common.h](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/include/common.h) | `word_t`、`FMT_WORD` 宽度自适应定义。 |
| [include/memory/paddr.h](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/include/memory/paddr.h) | `RESET_VECTOR` 宏。 |

调用方（实现后会被谁用到）：

| 文件 | 作用 |
| --- | --- |
| [src/cpu/cpu-exec.c](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/cpu/cpu-exec.c) | `assert_fail_msg()` 在崩溃时调用 `isa_reg_display()` 打印现场。 |
| [src/monitor/sdb/expr.c](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/monitor/sdb/expr.c) | 表达式求值（u2-l7）通过 `isa_reg_str2val` 解析 `$reg`。 |

## 4. 核心概念与源码讲解

### 4.1 gpr 宏与 check_reg_idx 越界检查

#### 4.1.1 概念说明

寄存器在 `CPU_state` 里就是一个数组 `gpr[32]`。访问它最朴素的方式是 `cpu.gpr[idx]`，但这有两个问题：一是越界（`idx` 算错时可能访问到 `pc` 甚至结构体外）；二是 ISA 配置会改变数组长度（RISC-V E 扩展只有 16 个寄存器）。NEMU 用一个 `gpr(idx)` 宏把「取值」与「检查」绑在一起，让所有访问点都默认安全。

`check_reg_idx` 的职责是：在返回 `idx` 之前，按当前 ISA 配置校验它落在合法范围 `[0, 32)` 或 `[0, 16)` 内。校验本身受 `CONFIG_RT_CHECK` 开关控制——这是运行时检查，会带来微小开销，调试时开启、追求性能时可关闭。

#### 4.1.2 核心流程

```
gpr(idx)
  └─ cpu.gpr[ check_reg_idx(idx) ]
                       │
                       ├─ IFDEF(CONFIG_RT_CHECK, assert(0 <= idx < 上界))
                       └─ return idx            # 无论是否检查，都把 idx 透传回去
```

要点：

- `check_reg_idx` 总是返回 `idx`，断言只是副作用；因此关闭 `CONFIG_RT_CHECK` 时宏退化为 `cpu.gpr[idx]`，零开销。
- 上界用 `MUXDEF(CONFIG_RVE, 16, 32)` 自适应：开启 E 扩展则 16，否则 32。
- `IFDEF` 是 NEMU 的条件编译宏（见 [include/macro.h:71](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/include/macro.h#L71)）：宏已定义时保留代码、未定义时替换为空，详见 u8-l26。

#### 4.1.3 源码精读

`check_reg_idx` 与 `gpr` 定义在 reg.h：

```c
// src/isa/riscv32/local-include/reg.h
static inline int check_reg_idx(int idx) {
  IFDEF(CONFIG_RT_CHECK, assert(idx >= 0 && idx < MUXDEF(CONFIG_RVE, 16, 32)));
  return idx;
}

#define gpr(idx) (cpu.gpr[check_reg_idx(idx)])
```

这两行是 [src/isa/riscv32/local-include/reg.h:21-26](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/isa/riscv32/local-include/reg.h#L21-L26)：`check_reg_idx` 做越界断言并返回 `idx`，`gpr(idx)` 把「检查 + 取值」合并成一次访问。注意 `cpu` 是全局变量（[include/isa.h:32](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/include/isa.h#L32) `extern CPU_state cpu;`），故宏内可直接引用。

`CONFIG_RT_CHECK` 在顶层 Kconfig 中默认开启（[Kconfig:213-215](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/Kconfig#L213-L215)），`CONFIG_RVE` 默认关闭（[src/isa/riscv32/Kconfig:7-9](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/isa/riscv32/Kconfig#L7-L9)）。

#### 4.1.4 代码实践

1. **实践目标**：体会 `CONFIG_RT_CHECK` 的开/关对越界访问的影响。
2. **操作步骤**：
   - 在 `src/isa/riscv32/reg.c` 的 `isa_reg_display`（目前是空函数）里临时加一行 `word_t v = gpr(32);`（越界）。
   - 保持默认配置（`CONFIG_RT_CHECK=y`）编译运行，观察是否触发 `assert`。
   - 再 `make menuconfig` 关闭 `Runtime checking`（`CONFIG_RT_CHECK`），重新编译运行，观察是否还断言。
3. **需要观察的现象**：开启时应在启动或调用处 `assert` 失败并打印寄存器现场（见 4.3）；关闭时越界访问静默通过，可能读到 `pc` 的值。
4. **预期结果**：开启 `RT_CHECK` 时 `idx=32` 不满足 `idx < 32`，断言失败；关闭时 `gpr(32)` 实际读到 `cpu.gpr[32]`，而 `CPU_state` 中 `pc` 紧跟 `gpr[32]` 之后（[isa-def.h:21-24](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/isa/riscv32/include/isa-def.h#L21-L24)），读到的正是 `pc`。验证后记得删掉这行测试代码。
5. **待本地验证**：断言失败的具体栈回溯与 `pc` 的确切读取值依赖你的运行环境。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `check_reg_idx` 写成 `return idx` 而不是 `return idx >= 0 && idx < 32 ? idx : 0` 之类的「容错返回」？

**答案**：容错返回会把「算错索引」的 bug 静默掩盖，让程序带着错误状态继续跑，更难排查。NEMU 选择「失败即停」——越界说明译码逻辑有 bug，应当立刻 `assert` 暴露，而不是猜一个值继续。这与 `assert_fail_msg` 调 `isa_reg_display` 打印现场的理念一致。

**练习 2**：若开启 `CONFIG_RVE`（E 扩展），`gpr(20)` 会发生什么？

**答案**：`MUXDEF(CONFIG_RVE, 16, 32)` 使上界变为 16，`check_reg_idx(20)` 在 `CONFIG_RT_CHECK=y` 时断言失败（`20 < 16` 为假）。E 扩展只保留 `x0`~`x15`，访问 `x20` 本就是非法的。

### 4.2 regs[] 寄存器名表与 reg_name

#### 4.2.1 概念说明

译码时拿到的是寄存器**索引**（如 `rd=10`），但人读汇编时用**ABI 名**（如 `a0`）。`regs[]` 表就是索引→ABI 名的映射，是 `isa_reg_display` 打印、`isa_reg_str2val` 反查的共享数据源。注意它的第 0 项写作 `"$0"`——带美元符号，这是 RISC-V 汇编里 x0 的习惯写法，也是后续 `isa_reg_str2val` 必须处理的一个细节。

#### 4.2.2 核心流程

```
索引 i  ──regs[i]──▶  ABI 名          ("a0", "$0", ...)
索引 i  ──reg_name(i)──▶ regs[check_reg_idx(i)]   # 带越界检查的查表
```

`reg_name(i)` 与直接访问 `regs[i]` 的唯一区别：前者经 `check_reg_idx` 保护，后者裸访问。在可信循环（`for i in [0, 32)`）里二者等价；在来自译码的不可信索引上应使用 `reg_name`。

#### 4.2.3 源码精读

名表定义在 reg.c：

```c
// src/isa/riscv32/reg.c
const char *regs[] = {
  "$0", "ra", "sp", "gp", "tp", "t0", "t1", "t2",
  "s0", "s1", "a0", "a1", "a2", "a3", "a4", "a5",
  "a6", "a7", "s2", "s3", "s4", "s5", "s6", "s7",
  "s8", "s9", "s10", "s11", "t3", "t4", "t5", "t6"
};
```

这是 [src/isa/riscv32/reg.c:19-24](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/isa/riscv32/reg.c#L19-L24)：32 项依次对应 `x0`~`x31`。注意 `regs[0]="$0"`、`regs[10]="a0"`，这两个值在 4.3 的实践中会被用到。

带检查的查表函数在 reg.h：

```c
// src/isa/riscv32/local-include/reg.h
static inline const char* reg_name(int idx) {
  extern const char* regs[];
  return regs[check_reg_idx(idx)];
}
```

这是 [src/isa/riscv32/local-include/reg.h:28-31](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/isa/riscv32/local-include/reg.h#L28-L31)：通过 `extern` 引入 reg.c 的 `regs[]`，再用 `check_reg_idx` 保护后返回名字。`static inline` 让它在头文件中被多处包含而不产生重复定义。

#### 4.2.4 代码实践

1. **实践目标**：建立「索引 ↔ ABI 名」的直觉。
2. **操作步骤**：阅读 `regs[]`，按下表填写索引与 ABI 名对应关系（不运行代码，纯阅读）：

   | 索引 | ABI 名 | 角色 |
   | --- | --- | --- |
   | 0 | ? | 零寄存器 |
   | 1 | ? | 返回地址 |
   | 2 | ? | 栈指针 |
   | 10 | ? | 参数/返回值 0 |
   | 21 | ? | 保存寄存器 11 |

3. **预期结果**：`$0`、`ra`、`sp`、`a0`、`s11`。
4. **延伸**：在 4.3 实现 `isa_reg_display` 后，用 `printf("%s", reg_name(10))` 验证它确实返回 `"a0"`。

#### 4.2.5 小练习与答案

**练习 1**：`regs[0]` 为什么是 `"$0"` 而不是 `"zero"` 或 `"x0"`？这对 `isa_reg_str2val` 意味着什么？

**答案**：NEMU 选用带 `$` 的汇编写法。这意味着 `isa_reg_str2val("$0")` 应当能匹配到 `regs[0]` 并返回 0；而 `isa_reg_str2val("a0")` 匹配 `regs[10]`。两种输入形式（带 `$` 的 `$0` 与不带 `$` 的 `a0`）都能直接用 `strcmp` 与 `regs[]` 比较命中——这正是本讲实践的推荐做法。

**练习 2**：`reg_name` 用 `extern const char* regs[];` 在头文件里声明，而不是 `#include "reg.c"`，为什么？

**答案**：`regs[]` 的定义（含初始化）只能存在于一个翻译单元，否则会重复链接。头文件只放 `extern` 声明表示「这个名字在别处定义」，各包含方共享同一份定义，符合 C 的分离编译模型。

### 4.3 isa_reg_display 与 isa_reg_str2val（待实现）

#### 4.3.1 概念说明

这两个函数是本讲的主战场，也是 PA1 的必做项。它们一正一反：

- `isa_reg_display()`：**值 → 屏幕**。把 `cpu` 里所有寄存器格式化打印出来。它的调用时机很重要——NEMU 在崩溃（`assert_fail_msg`）或差分测试不一致（`dut.c`）时会调它来「拍照存证」，所以越早实现，越早能在出错时看到现场。
- `isa_reg_str2val(name, success)`：**名字 → 值**。给定字符串（如 `"a0"`、`"$0"`），返回对应寄存器的当前值。它是 SDB 表达式求值（u2-l7）解析 `$reg` 的底层支撑——你在 SDB 里输入 `p $a0 + 1` 时，`$a0` 最终就由它转成数值。

二者签名都在抽象层 [include/isa.h:33-34](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/include/isa.h#L33-L34) 声明，是 ISA 无关契约；本讲实现 riscv32 版本，x86/mips32/loongarch32r 各自有一份等价的实现。

#### 4.3.2 核心流程

```
isa_reg_display()
  └─ for i in [0, 32):
       打印 regs[i] 与 gpr(i)        # 用 FMT_WORD 规范格式
     打印 pc

isa_reg_str2val(s, success)
  ├─ 若 s == "pc": *success=true; return cpu.pc        # 可选扩展
  ├─ for i in [0, ARRLEN(regs)):
       若 strcmp(s, regs[i]) == 0: *success=true; return gpr(i)
  └─ 都不匹配: *success=false; return 0
```

`success` 是出参（与 `expr()` 同约定，见 u2-l7）：解析成功置 `true`，失败置 `false`，调用方据此判断返回值是否有效。

#### 4.3.3 源码精读

当前的骨架是两个空函数（[src/isa/riscv32/reg.c:26-31](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/isa/riscv32/reg.c#L26-L31)）：

```c
void isa_reg_display() {
}

word_t isa_reg_str2val(const char *s, bool *success) {
  return 0;
}
```

调用方之一是 `assert_fail_msg`（[src/cpu/cpu-exec.c:94-97](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/cpu/cpu-exec.c#L94-L97)）：

```c
void assert_fail_msg() {
  isa_reg_display();
  statistic();
}
```

它会在 `assert` 失败（如非法指令 `INV`、`check_reg_idx` 越界）时被调用，先打印寄存器现场再打印统计信息。所以只要实现好 `isa_reg_display`，任何崩溃都会自动带上寄存器快照。

打印格式建议用 `FMT_WORD`（[include/common.h:40](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/include/common.h#L40)），riscv32 下展开为 `"0x%08" PRIx32`，保证 8 位十六进制补零；这样换到 riscv64 时格式会自动变成 16 位，无需改实现。

下面是一份**示例代码**（非项目原有，需你填入 reg.c）：

```c
// 示例代码：实现 isa_reg_display 与 isa_reg_str2val
void isa_reg_display() {
  for (int i = 0; i < ARRLEN(regs); i++) {
    printf("%-4s = " FMT_WORD "  ", regs[i], gpr(i));
    if (i % 4 == 3) printf("\n");      // 每行 4 个，便于阅读
  }
  printf("pc   = " FMT_WORD "\n", cpu.pc);
}

word_t isa_reg_str2val(const char *s, bool *success) {
  if (strcmp(s, "pc") == 0) { *success = true; return cpu.pc; }
  for (int i = 0; i < ARRLEN(regs); i++) {
    if (strcmp(s, regs[i]) == 0) { *success = true; return gpr(i); }
  }
  *success = false;
  return 0;
}
```

要点说明：

- `ARRLEN(regs)`（[include/macro.h:29](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/include/macro.h#L29)）自动算出 32，开启 `CONFIG_RVE` 时 `regs[]` 仍含 32 项，但访问 `gpr(i)` 在 `i>=16` 时会被 `check_reg_idx` 拦下——若要严格适配 E 扩展，应把循环上界也换成 `MUXDEF(CONFIG_RVE, 16, 32)`。
- `isa_reg_str2val` 用 `strcmp` 直接比对 `regs[]`：输入 `"a0"` 命中 `regs[10]`、输入 `"$0"` 命中 `regs[0]`，与 4.2 的表完全自洽。
- **与表达式词法的衔接**：若你已在 u2-l6 用 `\$[a-zA-Z0-9]+` 之类的规则识别寄存器 token，需保证存入 `token.str` 的字符串形式与 `regs[]` 项一致。常见做法有两种——(a) 词法层存入含 `$` 的原串（如 `"$a0"`、`"$0"`），并在 `isa_reg_str2val` 里先剥去可选的 `$` 再与「同样剥去 `$` 的 `regs[]` 项」比较；(b) 直接采用本示例的 `strcmp` 方案，并让词法层对 `x0` 存 `"$0"`、对其它寄存器存不带 `$` 的名字。两种皆可，关键是「词法产出的串」与「`isa_reg_str2val` 能识别的串」保持一致。本讲以「能解析 `a0` 与 `$0`」为达标，词法细节留待 u2-l6/u2-l7。

#### 4.3.4 代码实践

1. **实践目标**：实现两个函数，并在 SDB `info r` 中验证。
2. **操作步骤**：
   - 按上面示例代码填入 `src/isa/riscv32/reg.c` 的两个函数体。
   - 在 `src/monitor/sdb/sdb.c` 的 `cmd_table` 中新增 `info` 命令（当前表里只有 `help/c/q`，见 [src/monitor/sdb/sdb.c:61-68](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/monitor/sdb/sdb.c#L61-L68)），其处理函数解析子命令：若为 `r` 则调用 `isa_reg_display()`。**示例代码**如下：
     ```c
     // 示例代码：新增 info 命令（sdb.c）
     static int cmd_info(char *args) {
       char *sub = strtok(args, " ");
       if (sub == NULL) { printf("Usage: info r\n"); return 0; }
       if (strcmp(sub, "r") == 0) isa_reg_display();
       else printf("Unknown subcommand '%s'\n", sub);
       return 0;
     }
     // 并在 cmd_table 里追加：
     // { "info", "Display register status: info r", cmd_info },
     ```
   - `make` 重新编译，运行 `./build/riscv32-nemu-interpreter`，在 SDB 提示符下输入 `info r`。
3. **需要观察的现象**：打印出 32 个通用寄存器与 `pc`，格式整齐。上电后立即 `info r`，`pc` 应为 `0x80000000`（`RESET_VECTOR`），`$0` 为 `0`。
4. **预期结果**：因为 `cpu` 是全局变量、C 自动零初始化，加上 `restart()` 的设置，上电时所有 `gpr` 都是 `0`、`pc` 为 `0x80000000`。运行几步后再 `info r`，可看到 `t0`/`a0` 等被改写（见 4.5）。
5. **待本地验证**：`info` 命令在骨架中尚未提供，需你自行加入 `cmd_table` 并补 `cmd_info` 声明（参考 `cmd_help` 的前向声明写法）。

#### 4.3.5 小练习与答案

**练习 1**：`isa_reg_str2val` 在解析失败时返回 0 并把 `*success` 置 `false`。既然返回值无意义，为什么不在失败时直接 `assert(0)`？

**答案**：因为「名字写错」是用户输入层面的常态（比如 `p $foo` 里 `foo` 不是寄存器），不是程序 bug。`assert(0)` 会让整个模拟器崩溃，体验恶劣；用 `success` 出参把「失败」作为正常结果返回，让上层 `expr()` 优雅地报错（如打印 `invalid expression`）并继续接受下一条命令，才是正确的错误处理姿态。

**练习 2**：`isa_reg_display` 为什么要用 `FMT_WORD` 而不是直接写 `"%08x"`？

**答案**：`FMT_WORD` 随 `CONFIG_ISA64` 自适应——riscv32 下是 `"0x%08x"`，riscv64 下变成 `"0x%016lx"`。直接写 `"%08x"` 在切到 64 位时位数与类型都不对，会打印截断或错的值。用 `FMT_WORD` 让同一份代码在两种宽度下都正确，呼应 u5-l14 的「宽度基因」思想。

**练习 3**：差分测试（u8-l24）在寄存器不一致时也会调 `isa_reg_display`。这对你实现它的格式有什么提示？

**答案**：差分测试需要把 DUT（NEMU）与 REF（如 spike/QEMU）的寄存器逐项对比，所以打印应当「每个寄存器一行或对齐分列、名字与值清晰配对」，便于人眼快速定位哪个寄存器不一致。把它做成稳定、可读的表格格式，能让后续 difftest 排错事半功倍。

### 4.4 restart 初始化与 $0 恒零

#### 4.4.1 概念说明

模拟器「上电」时要给 CPU 一个确定的初始状态，否则寄存器里是随机值，程序行为不可复现。RISC-V 的上电约定有两点最关键：程序计数器指向复位向量；`x0` 恒为 0。`restart()` 就是把这两件事显式做掉的地方。

值得思考的是：`cpu` 是全局变量，C 标准保证全局变量零初始化——也就是说所有 `gpr` 本来就是 0。那么 `restart` 里再写一句 `cpu.gpr[0] = 0;` 是不是多余？不是。它是在「声明不变量」：无论将来谁在何处误写了 `gpr[0]`，上电时都会被重置回 0。这是一种防御式编程，把 RISC-V 的硬件规定用代码固化下来。

#### 4.4.2 核心流程

```
init_isa()
  ├─ memcpy(guest_to_host(RESET_VECTOR), img, sizeof(img))   # 烧录内置镜像
  └─ restart()
       ├─ cpu.pc = RESET_VECTOR        # 复位向量
       └─ cpu.gpr[0] = 0               # x0 恒零（防御式）
```

`init_isa` 由 `init_monitor` 在内存、设备初始化之后调用（[src/monitor/monitor.c:120](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/monitor/monitor.c#L120)），随后 `load_img` 可能用用户镜像覆盖内置镜像（先内后外、后者覆盖，见 u1-l3）。

#### 4.4.3 源码精读

`restart` 与 `init_isa` 在 init.c（[src/isa/riscv32/init.c:29-43](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/isa/riscv32/init.c#L29-L43)）：

```c
static void restart() {
  /* Set the initial program counter. */
  cpu.pc = RESET_VECTOR;

  /* The zero register is always 0. */
  cpu.gpr[0] = 0;
}

void init_isa() {
  /* Load built-in image. */
  memcpy(guest_to_host(RESET_VECTOR), img, sizeof(img));

  /* Initialize this virtual computer system. */
  restart();
}
```

`RESET_VECTOR` 来自 [include/memory/paddr.h:23](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/include/memory/paddr.h#L23)：`#define RESET_VECTOR (PMEM_LEFT + CONFIG_PC_RESET_OFFSET)`，其中 `PMEM_LEFT = CONFIG_MBASE`。riscv32 系统模式下 `CONFIG_MBASE` 默认 `0x80000000`、`CONFIG_PC_RESET_OFFSET` 默认 `0`（[src/memory/Kconfig:3-15](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/memory/Kconfig#L3-L15)），故 `RESET_VECTOR = 0x80000000`。

注意 `restart` 只动了 `pc` 和 `gpr[0]`，其余 31 个通用寄存器依赖全局变量的零初始化——它们此刻都是 0，但程序运行后会被指令改写。

#### 4.4.4 代码实践

1. **实践目标**：理解 `restart` 的最小初始化与「未初始化即随机」的风险。
2. **操作步骤**：阅读 `restart`，回答——为何不像真实硬件那样把所有寄存器也显式置 0？再对比 `CONFIG_MEM_RANDOM`（[src/memory/Kconfig:27-32](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/memory/Kconfig#L27-L32)）把内存随机化的思路：它故意暴露「读未初始化内存」的未定义行为。
3. **预期结果**：寄存器零初始化是 C 语言对全局变量的保证，足够可靠，故无需显式循环清零；而内存的 `MEM_RANDOM` 反其道而行，是**故意**随机化以暴露程序里「读未初始化内存」的 bug。两者目标不同：前者保证可复现的干净起点，后者主动制造噪声来抓 bug。
4. **延伸思考**：若把 `cpu` 改成局部变量（栈上），零初始化保证失效，`restart` 不显式清零就会读到垃圾值——这也是 `cpu` 必须为全局变量的原因之一。

#### 4.4.5 小练习与答案

**练习 1**：`restart` 里 `cpu.gpr[0] = 0;` 注释写「The zero register is always 0」。既然「always」，为什么只在上电时写一次，而不是每条指令执行前都写？

**答案**：每条指令执行前清零 `gpr[0]` 会拖慢模拟。NEMU 的做法是：上电置 0，之后在指令实现里**约定**所有写 `x0` 的操作都被忽略（RISC-V 硬件语义），从而保证它「always 0」。例如 `addi x0, x0, 0` 不应改变 `gpr[0]`——这要靠译码执行体里对 `rd==0` 的特殊处理或写回时跳过，是 u5-l16 的实现细节。本讲的 `gpr[0]=0` 只是起点。

**练习 2**：如果用户镜像把程序入口改到了 `0x80001000`，`restart` 还能用吗？

**答案**：`restart` 把 `pc` 设为 `RESET_VECTOR`（`0x80000000`），与用户镜像的实际入口无关。若程序入口在 `0x80001000`，要么在 `0x80000000` 处放一条跳转到 `0x80001000` 的指令，要么通过 `CONFIG_PC_RESET_OFFSET` 调整复位向量。`restart` 本身只认 `RESET_VECTOR`，是「上电取第一条指令的固定地址」这一硬件行为的直接映射。

### 4.5 built-in 镜像 img 与自检程序

#### 4.5.1 概念说明

`init_isa` 烧录的 `img[]` 是一段「内置测试程序」——即使你不提供任何用户镜像，NEMU 也能跑这段几条指令的小程序验证自身是否正常。它是一条「存 0、读 0、再触发 trap」的自检链：如果 NEMU 的取指、访存、寄存器实现都正确，程序应以 `HIT GOOD TRAP` 结束。

理解这段镜像有两个收益：一是它给了你一个「开箱即跑」的最小用例，可在实现指令前先观察预期行为；二是它揭示了 `ebreak` 作为 `nemu_trap` 的约定——程序的返回值放在 `a0`（即 `x10`）里，0 表示成功。

#### 4.5.2 核心流程

内置镜像 5 个字（[src/isa/riscv32/init.c:21-27](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/isa/riscv32/init.c#L21-L27)），从 `RESET_VECTOR` 起依次执行：

```
地址              机器码          汇编                    语义
0x80000000  0x00000297  auipc t0, 0          t0 = 当前 pc = 0x80000000
0x80000004  0x00028823  sb   x0, 16(t0)      mem[0x80000010] = 0（低字节写入）
0x80000008  0x0102c503  lbu  a0, 16(t0)      a0 = mem[0x80000010] 的低字节 = 0
0x8000000c  0x00100073  ebreak              nemu_trap，返回值 = a0 = 0 → GOOD TRAP
0x80000010  0xdeadbeef  （数据）              被 sb 改写低字节后变为 0xdeadbe00
```

数据流：`auipc` 拿到基址 `t0` → `sb` 向 `t0+16` 写 0 → `lbu` 从 `t0+16` 读回 0 到 `a0` → `ebreak` 以 `a0=0` 触发 trap。`0xdeadbeef` 是占位数据，原本放在 `0x80000010`，被 `sb` 覆盖低字节。

#### 4.5.3 源码精读

镜像定义（[src/isa/riscv32/init.c:21-27](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/isa/riscv32/init.c#L21-L27)）：

```c
static const uint32_t img [] = {
  0x00000297,  // auipc t0,0
  0x00028823,  // sb  zero,16(t0)
  0x0102c503,  // lbu a0,16(t0)
  0x00100073,  // ebreak (used as nemu_trap)
  0xdeadbeef,  // some data
};
```

注释里的 `ebreak (used as nemu_trap)` 点明了 NEMU 的一个约定：`ebreak` 指令被复用为「程序结束」信号（`nemu_trap`），返回值约定放在 `a0`（`x10`，即 `regs[10]`）。`a0=0` 经 `is_exit_status_bad` 判定为成功，打印 `HIT GOOD TRAP`（详见 u7-l23）。这正是 u5-l16 要实现的 `ebreak`/`NEMUTRAP` 语义在本讲的预演。

`init_isa` 用 `memcpy` 把整段 `img` 拷到 `RESET_VECTOR` 起的物理内存（[src/isa/riscv32/init.c:39](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/isa/riscv32/init.c#L39)），`guest_to_host` 把客机物理地址转成宿主机指针（见 u4-l12）。注意拷贝的是全部 5 个字（含 `0xdeadbeef` 数据），所以执行 `sb` 前 `mem[0x80000010]` 是 `0xdeadbeef`。

#### 4.5.4 代码实践

1. **实践目标**：手动追踪镜像执行，验证你对「存 0 读 0」自检的理解。
2. **操作步骤**：
   - 在 4.3 实现 `isa_reg_display` 后，启动 NEMU（不传用户镜像，跑内置镜像）。
   - 在 SDB 中执行 `si 1`（单步一条，若尚未实现 `si` 命令可参考 u2-l5 先加上），再 `info r`，重点看 `t0`（`regs[5]`）与 `pc`。
   - 继续 `si 1`、`si 1`，每次 `info r` 观察 `t0`、`a0`（`regs[10]`）、`pc` 的变化。
3. **预期结果**：
   - 初始：`pc=0x80000000`，所有 `gpr=0`。
   - `si 1` 后（auipc）：`t0=0x80000000`，`pc=0x80000004`。
   - `si 2` 后（sb 已执行）：`t0` 不变，`pc=0x80000008`，内存 `0x80000010` 被改写。
   - `si 3` 后（lbu 已执行）：`a0=0`，`pc=0x8000000c`（指向 ebreak）。
   - 再 `si 1`（执行 ebreak）：触发 `nemu_trap`，因 `a0=0` 应打印 `HIT GOOD TRAP`。
4. **待本地验证**：`si`/`info r` 命令在骨架中尚未实现，需先按 u2-l5 与 4.3 补齐；指令是否被正确执行取决于你是否已实现 `auipc/sb/lbu/ebreak`（u5-l16）。在指令实现前，`si` 会因译码失败而 `invalid_inst`——这本身也验证了 `assert_fail_msg` → `isa_reg_display` 的崩溃现场打印链路。

#### 4.5.5 小练习与答案

**练习 1**：`lbu` 读回的 `a0` 一定是 0 吗？如果 `sb` 把字节写在 `0x80000010`，而 `0xdeadbeef` 原本也在 `0x80000010`，会不会读到 `0xef`？

**答案**：不会读到 `0xef`。`sb x0, 16(t0)` 先把 `0x80000010` 的最低字节写成 0（小端序下 `0xdeadbeef` 的最低字节本是 `0xef`，被覆盖为 `0x00`），所以随后 `lbu` 读到的是 `0x00`。这条自检链正是要验证「写进去什么、读出来就是什么」的访存正确性。

**练习 2**：为什么 `ebreak` 的返回值约定放在 `a0` 而不是某个专用寄存器？

**答案**：`a0`（`x10`）在 RISC-V 调用约定中本就是「函数返回值」寄存器。NEMU 复用这一约定，让「程序结束并报告退出码」与「函数返回」语义对齐——程序可像调用一个函数那样，把退出码放进 `a0` 再 `ebreak`。这样 AM/IOE 等上层抽象能沿用同一套调用约定，降低理解成本（见 u7-l23 的 `is_exit_status_bad`）。

## 5. 综合实践

把本讲四个模块串起来，完成 PA1 寄存器部分的闭环：

**任务**：实现寄存器访问的「展示 + 解析 + 上电自检」完整链路。

1. **实现 `isa_reg_display`**：在 `src/isa/riscv32/reg.c` 中格式化打印 32 个通用寄存器与 `pc`，用 `reg_name(i)` 或 `regs[i]` 取名、`gpr(i)` 取值、`FMT_WORD` 定格式（参考 4.3 示例代码）。
2. **实现 `isa_reg_str2val`**：支持 `"a0"`、`"$0"` 两种形式（直接 `strcmp` 扫描 `regs[]`），可选支持 `"pc"`；失败时通过 `*success=false` 报告。
3. **接入 SDB**：在 `src/monitor/sdb/sdb.c` 的 `cmd_table` 新增 `info` 命令，子命令 `r` 调用 `isa_reg_display()`（参考 4.3.4 示例代码）。
4. **验证自检链**：运行内置镜像，用 `info r` 观察上电现场（`pc=0x80000000`、`$0=0`）；单步若干条后再次 `info r`，确认 `t0`/`a0` 变化符合 4.5 的预测；执行到 `ebreak` 应得到 `HIT GOOD TRAP`。
5. **验证崩溃链路**：故意触发一次非法指令或越界（如临时给 `gpr` 传越界索引，或运行尚未实现的指令），观察 `assert_fail_msg` 是否自动打印寄存器现场——这正是你实现的 `isa_reg_display` 在为你排错。

**验收标准**：`info r` 输出整齐可读；`p $a0`（若已实现表达式寄存器 token，见 u2-l6/u2-l7）能返回与 `info r` 中 `a0` 一致的值；崩溃时自动附带寄存器快照。

## 6. 本讲小结

- `gpr(idx)` 宏 = `cpu.gpr[check_reg_idx(idx)]`，把「越界检查 + 取值」合二为一；`check_reg_idx` 受 `CONFIG_RT_CHECK` 开关控制、上界随 `CONFIG_RVE` 在 16/32 间自适应。
- `regs[]` 是索引→ABI 名的查表数据（`regs[0]="$0"`、`regs[10]="a0"`），`reg_name(i)` 是其带越界检查的访问器，二者是 `display`/`str2val` 的共享数据源。
- `isa_reg_display` 把寄存器格式化打印，被 `assert_fail_msg`（崩溃）与差分测试不一致时调用，是排错的第一现场；建议用 `FMT_WORD` 适配宽度。
- `isa_reg_str2val` 用 `strcmp` 扫描 `regs[]` 把名字解析为值，`success` 出参报告成败，是 SDB 表达式 `$reg` 的底层支撑。
- `restart()` 做最小上电初始化：`cpu.pc = RESET_VECTOR`（riscv32 默认 `0x80000000`）、`cpu.gpr[0] = 0`（防御式固化 x0 恒零）。
- 内置镜像 `img[]` 是「存 0 → 读 0 → ebreak」的自检程序，`ebreak` 复用为 `nemu_trap`、返回值约定在 `a0`，`a0=0` 即 `HIT GOOD TRAP`。

## 7. 下一步学习建议

本讲把「寄存器的访问、展示、解析、初始化」补齐，但刻意回避了指令如何**写回**寄存器——也就是译码执行体。下一步建议进入：

- **u5-l16 RISC-V 指令实现**：精读 `src/isa/riscv32/inst.c`，学习 `isa_exec_once` 取指、`decode_operand` 解码 I/U/S 类型立即数、用 `INSTPAT` 实现 `auipc/lbu/sb/ebreak` 等指令，并验证「写 `x0` 被忽略」这一在 4.4 埋下的约定。
- **复习 u3-l11 INSTPAT 模式匹配**：u5-l16 会大量使用 `INSTPAT`，若对 `pattern_decode`/`key/mask/shift` 已生疏，先回看本讲的越界检查与名表，再复习译码宏。
- **可选：u2-l6/u2-l7 表达式**：若你想让 `p $a0` 真正可用，需回去补齐词法分析的 `TK_REG` 规则与递归下降中对 `isa_reg_str2val` 的调用，把本讲的 `str2val` 与表达式求值闭环。
