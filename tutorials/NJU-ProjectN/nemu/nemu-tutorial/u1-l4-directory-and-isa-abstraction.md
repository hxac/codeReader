# 目录结构与 ISA 抽象层

## 1. 本讲目标

前三讲我们分别建立了对 NEMU 定位（u1-l1）、构建系统（u1-l2）和启动流程（u1-l3）的认知。本讲要回答一个更结构化的问题：**NEMU 如何用「同一套框架源码」去模拟 x86、RISC-V、MIPS、LoongArch 等四种截然不同的指令集？**

学完本讲，你应该能够：

1. 画出 `src/` 与 `include/` 的目录树，并说出每个子目录对应哪个子系统（CPU、内存、设备、ISA……）。
2. 解释 `include/isa.h` 如何用一个 `concat(__GUEST_ISA__, _CPU_state)` 宏，让 `CPU_state` 这个名字在不同 ISA 下指向完全不同的结构体。
3. 说出 `include/common.h` 里 `word_t`、`vaddr_t`、`paddr_t` 三种基本类型的宽度是如何随 `CONFIG_ISA64` 变化的。
4. 描述 `filelist.mk` 机制如何根据 `GUEST_ISA` 把「对应的那个 ISA 目录」编译进来，而把另外三个排除在外。

理解 ISA 抽象层的意义在于：它是 NEMU 整个工程的「骨架关节」。后面所有讲义（CPU 执行、内存、指令实现、差分测试）都会反复出现 `CPU_state`、`word_t`、`isa_exec_once()` 这种「名字不变、实现随 ISA 切换」的符号。看懂本讲，你就能在阅读任何框架代码时，随时把这些符号「代入」成当前 ISA 的具体实现。

---

## 2. 前置知识

阅读本讲前，请确认你已掌握（来自 u1-l1、u1-l2、u1-l3）：

- **ISA（指令集架构）**：CPU 能理解的机器指令集合与寄存器约定。x86、RISC-V、MIPS、LoongArch 是四种不同的 ISA，它们的寄存器数量、指令编码、内存模型都不同。
- **`CONFIG_*` 配置项**：menuconfig 产生的开关，写成 C 宏进 `include/generated/autoconf.h`，写成 Make 变量进 `include/config/auto.conf`。本讲会频繁出现 `CONFIG_ISA`、`CONFIG_ISA64`、`CONFIG_RVE` 等。
- **`GUEST_ISA` 与 `__GUEST_ISA__`**：前者是 Makefile 变量（值为 `riscv32`/`x86` 等），后者是 C 预处理宏（由 `Makefile` 用 `-D__GUEST_ISA__=$(GUEST_ISA)` 注入，见 [Makefile:52](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/Makefile#L52)）。两者同源，一个给 Make 用、一个给 C 用。
- **客机（guest）与宿主机（host）**：NEMU 模拟的是「客机 CPU/内存」，NEMU 自己运行在「宿主机」上。本讲的「ISA 抽象」就是为客机服务的——框架代码只写「取指—译码—执行」的骨架，具体的指令语义交给被选中的 ISA 目录。

> 术语提示：本讲里的「框架代码（framework）」指与 ISA 无关、所有 ISA 共享的源码（如 `cpu-exec.c`）；「ISA 代码」指某个 ISA 独有的源码（如 `src/isa/riscv32/inst.c`）。ISA 抽象层就是连接两者的「契约」。

---

## 3. 本讲源码地图

本讲围绕「目录划分」与「ISA 抽象契约」展开，涉及的关键文件：

| 文件 | 作用 |
| --- | --- |
| [src/filelist.mk](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/filelist.mk) | 顶层源文件收集：声明 `src/cpu`、`src/monitor`、`src/utils` 等公共目录，并用黑名单排除 AM 模式下的 SDB。 |
| [src/isa/filelist.mk](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/isa/filelist.mk) | ISA 切换的核心：把 `src/isa/$(GUEST_ISA)` 加入编译，并把该目录的 `include` 加入头文件搜索路径。 |
| [include/isa.h](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/include/isa.h) | ISA 抽象契约：用 `concat` 把 `CPU_state`、`ISADecodeInfo` 等名字拼成 ISA 专属类型，并声明 `isa_exec_once`、`isa_mmu_check` 等统一接口。 |
| [include/common.h](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/include/common.h) | 全局类型：定义 `word_t`、`sword_t`、`vaddr_t`、`paddr_t`，宽度随 `CONFIG_ISA64` 切换。 |
| [include/macro.h](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/include/macro.h) | 宏基础设施：`concat`、`MUXDEF`、`BITS`、`SEXT` 等，是 ISA 抽象层的「工具箱」。 |
| [src/isa/riscv32/include/isa-def.h](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/isa/riscv32/include/isa-def.h) | RISC-V 的具体定义：`riscv32_CPU_state`（32 个通用寄存器 + pc）。 |
| [src/isa/x86/include/isa-def.h](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/isa/x86/include/isa-def.h) | x86 的具体定义：`x86_CPU_state`（8 个通用寄存器 + pc，带 union）。 |
| [Makefile](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/Makefile) | 从 `CONFIG_ISA` 提取 `GUEST_ISA`，拼出二进制名 `NAME`，并注入 `-D__GUEST_ISA__`。 |

---

## 4. 核心概念与源码讲解

本讲拆成 4 个最小模块：**目录组织**、**isa.h ISA 接口**、**common.h 基本类型**、**filelist ISA 切换**。前三个讲「静态结构」，最后一个讲「动态选择」，四者合起来就是 NEMU 多 ISA 适配的全貌。

### 4.1 目录组织

#### 4.1.1 概念说明

NEMU 的源码不是一个大目录里堆几百个 `.c` 文件，而是**按子系统分层、按 ISA 分叉**。理解目录结构，等于拿到了一张「NEMU 能力地图」——你之后遇到任何函数，都能凭它所在的目录猜出它的职责。

目录组织遵循两条主线：

1. **纵向分层（框架 vs ISA）**：把「所有 ISA 都一样」的框架代码（CPU 主循环、内存读写、设备调度）放在公共目录；把「每个 ISA 都不同」的代码（寄存器定义、指令译码）放在 `src/isa/<某 ISA>/` 下，四套并存、按需编译。
2. **横向分模块**：每个子系统（CPU、内存、设备、监视器、工具）各占一个目录，职责单一、互不越界。

#### 4.1.2 核心流程

NEMU 的目录树可以概括如下（仅列关键项）：

```text
src/
├── nemu-main.c          # 程序入口 main()
├── filelist.mk          # 顶层：声明公共目录 + 黑名单
├── cpu/                 # CPU 执行框架（与 ISA 无关）
│   ├── cpu-exec.c       #   cpu_exec() 主循环（u3 详讲）
│   └── difftest/        #   差分测试框架（u8 详讲）
├── engine/interpreter/  # 解释器引擎（engine_start、hostcall）
├── monitor/             # 监视器：初始化链 + SDB
│   ├── monitor.c        #   init_monitor（u1-l3 详讲）
│   └── sdb/             #   简单调试器（u2 详讲）
├── memory/              # 内存系统（u4 详讲）
├── device/              # 设备与 I/O（u6 详讲）
│   └── io/              #   mmio / pio / map 框架
├── utils/               # 工具：log、state、disasm、rand
└── isa/                 # ★ ISA 实现：多套并存，按 GUEST_ISA 选用
    ├── filelist.mk      #   选取「当前 ISA」那个目录
    ├── riscv32/         #   RISC-V（riscv64 是它的符号链接！）
    ├── x86/             #   x86
    ├── mips32/          #   MIPS32
    └── loongarch32r/    #   LoongArch32r

include/                 # 公共头文件
├── common.h             #   全局类型（本讲 4.3）
├── isa.h                #   ISA 抽象契约（本讲 4.2）
├── macro.h              #   宏基础设施（concat / MUXDEF / SEXT）
├── cpu/  memory/  device/   # 各子系统的公共头
└── generated/autoconf.h #   menuconfig 产物（CONFIG_* 宏，自动生成，勿手改）
```

特别注意 `src/isa/` 目录：四个 ISA 子目录**同时存在于磁盘上**，但每次编译只会选中其中一个（由 4.4 节的 `filelist.mk` 决定）。还有一个细节值得记住：

> `src/isa/riscv64` 是一个指向 `riscv32` 的**符号链接**（`riscv64 -> riscv32`）。这意味着 RISC-V 32 位和 64 位**共用同一份源码**，二者的差异完全靠 `CONFIG_RV64` 宏在编译期区分（详见 4.2 节的 `MUXDEF`）。

#### 4.1.3 源码精读

目录的「编译边界」不是写在某个地方集中配置，而是**分散在每个目录的 `filelist.mk` 里**。顶层的 [src/filelist.mk](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/filelist.mk) 声明了公共目录：

- [src/filelist.mk:16-19](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/filelist.mk#L16-L19) 把入口文件、`src/cpu`、`src/monitor`、`src/utils` 列为「总是编译」，并把 `src/memory` 标记为「仅 System 模式编译」（`CONFIG_MODE_SYSTEM`），再把 `src/monitor/sdb` 放进「AM 模式黑名单」。
- 注意 `DIRS-$(CONFIG_MODE_SYSTEM)` 这种写法：当 `CONFIG_MODE_SYSTEM=y` 时变量名展开为 `DIRS-y`，于是 `src/memory` 被加入；否则变量名变成一个没人用的 `DIRS-`（空），等于不加入。这是 NEMU 用 Make 变量名做条件选择的惯用手法（u1-l2 已讲过 `SRCS-$(CONFIG_*)` 同款机制）。

每个 ISA 目录内部又是同样的结构。以 RISC-V 为例，[src/isa/riscv32](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/isa/riscv32) 下有 `init.c`（烧录内置镜像 + 初始化 PC）、`inst.c`（指令实现，u5 详讲）、`reg.c`（寄存器读写）、`system/`（中断 `intr.c`、MMU `mmu.c`）、`include/isa-def.h`（CPU 状态定义）、`difftest/`（差分测试的 ISA 适配）。

#### 4.1.4 代码实践

**实践目标**：用「目录映射子系统」的视角，亲手绘制 NEMU 的能力地图。

**操作步骤**：

1. 在项目根目录执行 `ls src/`，对照上面的目录树，给每个子目录写一句话注释。
2. 进入 `src/isa/riscv32/`，执行 `ls`，对照本节列出的文件，猜每个文件属于哪个子系统。
3. 执行 `ls -l src/isa/`，确认 `riscv64 -> riscv32` 是符号链接。

**需要观察的现象**：

- `src/` 下确实有 cpu/monitor/memory/device/utils/isa 六大目录，与 u1-l1 讲的六大能力一一对应。
- `src/isa/` 下四种 ISA 并存。

**预期结果**：你得到一张「目录 → 子系统」对照表。这张表在后续每篇讲义开头都会用到。

> 待本地验证：目录内容随仓库版本可能微调，但「按子系统分层 + 按 ISA 分叉」的总结构是稳定的。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `src/memory` 用 `DIRS-$(CONFIG_MODE_SYSTEM)` 而不是直接 `DIRS-y`？
**答案**：因为存在「非系统模式」（虽然目前默认且只有 System mode），把内存目录的编译与运行模式绑定，可保证换模式时内存代码按需进出。这是一种用 Make 变量名做条件编译的手法，便于将来扩展（如用户态模式不需要 `src/memory`）。

**练习 2**：`src/isa/riscv64` 是符号链接而非独立目录，这样做的好处是什么？
**答案**：RISC-V 32/64 位指令集高度相似（寄存器模型、指令编码大体相同），共用一份源码能避免重复维护；两者的差异（寄存器宽度、地址宽度）通过 `CONFIG_RV64` 宏在编译期用 `MUXDEF` 选择即可（见 4.2 节）。改一处源码，32/64 同时受益。

---

### 4.2 isa.h ISA 接口

#### 4.2.1 概念说明

这是本讲的重头戏。问题是这样提出的：框架代码（比如 `cpu-exec.c`）需要操作「CPU 的寄存器」，于是它声明了一个变量 `CPU_state cpu;`。但是 x86 的 CPU 有 8 个通用寄存器、RISC-V 有 32 个、MIPS 有 32 个但命名和编码都不同——`CPU_state` 这个类型，到底该长什么样？

NEMU 的解法极其优雅：**让 `CPU_state` 在预处理阶段被「替换」成当前 ISA 专属的类型名**。

- 选 x86 时，`CPU_state` → `x86_CPU_state`
- 选 riscv 时，`CPU_state` → `riscv32_CPU_state`（或 `riscv64_CPU_state`）

而 `x86_CPU_state`、`riscv32_CPU_state` 这些具体结构体，分别定义在各自的 `isa-def.h` 里。框架代码只认 `CPU_state` 这个统一名字，**根本不知道也不关心**底层是哪种 ISA。

实现这个魔法只需要两个东西：

1. C 编译宏 `__GUEST_ISA__`（值如 `x86`、`riscv32`），由 Makefile 注入；
2. `concat` 宏，做标识符的「粘贴」。

#### 4.2.2 核心流程

整个拼接流程是一条「从配置到类型」的链：

```text
menuconfig 选 ISA_x86
      │  生成 CONFIG_ISA="x86"
      ▼
Makefile: GUEST_ISA = "x86"
      │  CFLAGS += -D__GUEST_ISA__=x86   (注意没有引号，是个标识符)
      ▼
isa.h: typedef concat(__GUEST_ISA__, _CPU_state) CPU_state;
      │  预处理：concat(x86, _CPU_state)
      ▼
      typedef x86_CPU_state CPU_state;   ← 名字拼出来了！
      │  但 x86_CPU_state 是什么？由 isa-def.h 提供
      ▼
isa-def.h (x86): 真正的结构体定义 { gpr[8]; eax...edi; pc; }
```

关键点：`__GUEST_ISA__` 是一个**标识符**（不是字符串）。`-D__GUEST_ISA__=x86` 等价于 `#define __GUEST_ISA__ x86`，所以 `concat(__GUEST_ISA__, _CPU_state)` 先变成 `concat(x86, _CPU_state)`，再经过 `concat` 的两层展开变成 `x86_CPU_state`。

为什么 `concat` 要用两层宏？因为 C 预处理的一个经典陷阱：

- `#define concat(x, y) x ## y` 是「一层」。当参数本身是另一个宏（如 `__GUEST_ISA__`）时，`##` 会**阻止宏参数的展开**，于是 `concat(__GUEST_ISA__, _CPU_state)` 直接粘成 `__GUEST_ISA___CPU_state`，根本不是我们想要的。
- 解法是再加一层「跳板」宏，让参数在进入 `##` 之前先被完整展开。

这正是 [macro.h](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/include/macro.h) 里 `concat` / `concat_temp` 双层结构的由来：

```c
#define concat_temp(x, y) x ## y   // 真正做粘贴，但 x,y 在此之前已被展开
#define concat(x, y)       concat_temp(x, y)  // 跳板：先展开参数，再交给 concat_temp
```

#### 4.2.3 源码精读

契约本体在 [include/isa.h](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/include/isa.h)，最关键的两行：

- [isa.h:19-25](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/include/isa.h#L19-L25) 先 `#include <isa-def.h>`（哪个 ISA 的 `isa-def.h`？由 4.4 节的头文件搜索路径决定），再用 `concat` 把 `CPU_state` 和 `ISADecodeInfo` 拼出来。注释 `Located at src/isa/$(GUEST_ISA)/include/isa-def.h` 直接点明了这份 `isa-def.h` 来自当前选中的 ISA 目录。

`isa.h` 不止定义类型，它还声明了一整套「ISA 必须实现、框架负责调用」的函数，构成 ISA 抽象契约的完整接口：

- [isa.h:29](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/include/isa.h#L29) `init_isa()`——ISA 初始化（烧录内置镜像、设置初始 PC）。
- [isa.h:31-34](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/include/isa.h#L31-L34) 寄存器接口：`isa_reg_display()`、`isa_reg_str2val()`。
- [isa.h:38](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/include/isa.h#L38) `isa_exec_once()`——**执行一条指令**，CPU 主循环每步调它一次（u3 详讲）。
- [isa.h:41-47](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/include/isa.h#L41-L47) MMU/内存翻译接口：`MMU_DIRECT/MMU_TRANSLATE/MMU_FAIL` 三种模式枚举、`isa_mmu_check`、`isa_mmu_translate`。
- [isa.h:50-52](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/include/isa.h#L50-L52) 中断/异常接口：`isa_raise_intr`、`isa_query_intr`。
- [isa.h:55-56](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/include/isa.h#L55-L56) 差分测试接口。

注意 [isa.h:44-46](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/include/isa.h#L44-L46) 的 `#ifndef isa_mmu_check`：因为 riscv32 和 x86 都在各自的 `isa-def.h` 里把 `isa_mmu_check` 定义成宏（直接返回 `MMU_DIRECT`，表示「不做地址翻译」），所以这里用 `#ifndef` 守卫——若 ISA 用宏提供了，就不重复声明函数；否则声明为函数，留给分页实现（u7 详讲）。

再看两个 ISA 各自的「具体定义」，对比体会差异：

- [riscv32/include/isa-def.h:21-24](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/isa/riscv32/include/isa-def.h#L21-L24)：`riscv32_CPU_state` 是 `{ word_t gpr[32]; vaddr_t pc; }`——32 个等宽通用寄存器 + 一个 pc。这里的类型名 `riscv32_CPU_state` 正是上面 `concat` 拼出来的目标。
- [x86/include/isa-def.h:29-40](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/isa/x86/include/isa-def.h#L29-L40)：`x86_CPU_state` 完全不同——8 个通用寄存器，每个用 union 同时暴露 32/16/8 位视图（`_32`/`_16`/`_8[2]`），还单独列出 `eax...edi` 便于按名字访问。这正是 x86 寄存器编码方案的体现（注释里的 TODO 提示了 PA 学生要重组这个 union）。

类型名拼接还玩了一个更花的把戏：riscv 的结构体标签本身也用了 `MUXDEF` 来在 `riscv32_CPU_state` 和 `riscv64_CPU_state` 之间二选一：

- [riscv32/include/isa-def.h:24](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/isa/riscv32/include/isa-def.h#L24) `} MUXDEF(CONFIG_RV64, riscv64_CPU_state, riscv32_CPU_state);`。当 `CONFIG_RV64` 未定义时 `MUXDEF` 选第二个参数，于是这个 `typedef struct {...} riscv32_CPU_state;` 完成定义；定义 `CONFIG_RV64` 时则标签变成 `riscv64_CPU_state`。而 `isa.h` 里的 `__GUEST_ISA__` 恰好也是 `riscv32` 或 `riscv64`（由 Kconfig 决定），于是 `concat(__GUEST_ISA__, _CPU_state)` 总能匹配上对应的标签——32/64 就这样用「宏拼接 + MUXDEF」优雅地切换了。

#### 4.2.4 代码实践

**实践目标**：亲手验证 `CPU_state` 这个名字在不同 ISA 下被拼成不同的结构体标签。

**操作步骤**（源码阅读型 + 可选编译验证）：

1. 打开 [include/isa.h:24](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/include/isa.h#L24)，假设当前 `__GUEST_ISA__` 为 `riscv32`，在纸上把 `concat(__GUEST_ISA__, _CPU_state)` 逐步展开：
   - `concat(riscv32, _CPU_state)` → `concat_temp(riscv32, _CPU_state)` → `riscv32_CPU_state`。
2. 打开 [riscv32/include/isa-def.h:24](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/isa/riscv32/include/isa-def.h#L24)，确认确实定义了 `riscv32_CPU_state` 这个标签。
3. （可选，待本地验证）写一个最小测试程序 `t.c`：
   ```c
   #include <isa.h>
   int main() { CPU_state c; c.gpr[0] = 0; return 0; }  // 示例代码
   ```
   用 `gcc -Iinclude -D__GUEST_ISA__=riscv32 -Isrc/isa/riscv32/include -include include/generated/autoconf.h -E t.c` 仅做预处理，在输出里 `grep CPU_state`，观察 `CPU_state` 是否被替换为结构体，以及该结构体的字段是不是 `gpr[32]` + `pc`。
4. 把 `-D__GUEST_ISA__=x86 -Isrc/isa/x86/include` 换上，再预处理一次，对比结构体字段是否变成 `gpr[8]` + `eax...edi` + `pc`。

**需要观察的现象**：同一个源文件，仅改 `__GUEST_ISA__`，`CPU_state` 指向的结构体完全不同。

**预期结果**：你亲眼看到「一个名字，多重实现」。这就是 ISA 抽象层的全部秘密。

> 待本地验证：步骤 3、4 需要本机能跑 `gcc -E`，且需要 `autoconf.h`（先 `make menuconfig` 生成）。若环境受限，只做步骤 1、2 的纸面展开也能完整理解原理。

#### 4.2.5 小练习与答案

**练习 1**：如果把 `concat` 写成一层 `#define concat(x, y) x##y`，会发生什么？
**答案**：当 `__GUEST_ISA__` 作为参数传入时，`##` 会阻止它被展开成 `x86`，结果会被粘成 `__GUEST_ISA___CPU_state` 这个不存在的标识符，编译报错。这就是为什么必须用 `concat`/`concat_temp` 两层宏——先在 `concat` 里把参数完整展开，再交给 `concat_temp` 粘贴。

**练习 2**：[isa.h:44](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/include/isa.h#L44) 为什么要用 `#ifndef isa_mmu_check` 包住函数声明？
**答案**：因为部分 ISA（riscv32、x86）在 `isa-def.h` 里把 `isa_mmu_check` 定义成一个**宏**（恒返回 `MMU_DIRECT`，表示不翻译地址）。如果不加 `#ifndef` 守卫，这里又会声明一个同名函数，导致「宏与函数同名」的冲突。`#ifndef` 让「ISA 已用宏实现」时跳过函数声明，未实现时才声明函数供分页 ISA 填充。这是一种「允许 ISA 用宏覆盖接口」的灵活设计。

**练习 3**：框架代码里写 `cpu.pc`，在 riscv 和 x86 下都能编译通过，为什么？
**答案**：因为两个 ISA 的 `CPU_state` 结构体（`riscv32_CPU_state`、`x86_CPU_state`）都含有一个名为 `pc` 的字段（分别为 [riscv32/include/isa-def.h:23](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/isa/riscv32/include/isa-def.h#L23) 与 [x86/include/isa-def.h:39](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/isa/x86/include/isa-def.h#L39)）。ISA 抽象层不仅统一了类型名，还隐式约定了「各 ISA 的状态结构体必须有 `pc` 字段」——这是契约的一部分。

---

### 4.3 common.h 基本类型

#### 4.3.1 概念说明

除了 `CPU_state` 这种「大体量」结构体，NEMU 还有一批**贯穿全代码的基本类型**：表示一个机器字的 `word_t`、表示虚拟地址的 `vaddr_t`、表示物理地址的 `paddr_t`。它们的宽度同样要随 ISA 变化——32 位 ISA 下一个字是 4 字节，64 位 ISA 下是 8 字节。

这些类型定义在 [include/common.h](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/include/common.h)，几乎所有源文件都会包含它。它和 `isa.h` 的区别在于：`isa.h` 描述「ISA 专属结构」，`common.h` 描述「所有 ISA 共享、但宽度随 ISA 调整」的基础类型。

#### 4.3.2 核心流程

`common.h` 用 `MUXDEF`（多路选择宏）做条件选择。`MUXDEF(macro, X, Y)` 的语义是：若布尔宏 `macro` 已定义则取 `X`，否则取 `Y`（[macro.h:49](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/include/macro.h#L49)）。

于是一个 32/64 位自适应的 `word_t` 写出来就是：

```c
typedef MUXDEF(CONFIG_ISA64, uint64_t, uint32_t) word_t;
//  CONFIG_ISA64 已定义 → word_t = uint64_t
//  否则              → word_t = uint32_t
```

三种基本类型的角色：

| 类型 | 含义 | 32 位 ISA | 64 位 ISA |
| --- | --- | --- | --- |
| `word_t` | 一个机器字（通用寄存器宽度） | `uint32_t` | `uint64_t` |
| `sword_t` | 有符号的机器字 | `int32_t` | `int64_t` |
| `vaddr_t` | 虚拟地址 | = `word_t` | = `word_t` |
| `paddr_t` | 物理地址 | `uint32_t` | 通常 `uint32_t`（见下） |

注意 `vaddr_t` 直接等于 `word_t`——因为「虚拟地址宽度 = 机器字宽度」在 NEMU 支持的 ISA 中恒成立。而 `paddr_t` 多了一个 `PMEM64` 的考量：只有当物理内存基址 + 大小超过 4 GiB 时，物理地址才需要 64 位，否则 32 位足够。

数学上，32 位无符号字能表示的最大值是

\[
\texttt{word\_t}_{\max} = 2^{32} - 1 = 4294967295
\]

这就是为什么 `FMT_WORD` 在 32 位下用 `"0x%08" PRIx32`（8 个十六进制位，刚好 \(32/4=8\)）。

#### 4.3.3 源码精读

核心定义集中在 [common.h:38-45](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/include/common.h#L38-L45)：

- [common.h:38-40](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/include/common.h#L38-L40) `word_t`、`sword_t`、`FMT_WORD` 三者都用 `MUXDEF(CONFIG_ISA64, ...)` 在 64/32 位间选择。`FMT_WORD` 是 `printf` 的格式串，配合 `PRIx32`/`PRIx64` 跨平台打印。
- [common.h:42](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/include/common.h#L42) `typedef word_t vaddr_t;`——虚拟地址直接复用机器字宽度。
- [common.h:43-44](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/include/common.h#L43-L44) `paddr_t` 与 `FMT_PADDR` 用 `MUXDEF(PMEM64, ...)`，而 `PMEM64` 由 [common.h:34-36](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/include/common.h#L34-L36) 在 `CONFIG_MBASE + CONFIG_MSIZE > 0x100000000` 时才置 1。

还有两处依赖配置的细节：

- [common.h:24](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/include/common.h#L24) `#include <generated/autoconf.h>` 把所有 `CONFIG_*` 宏引入——这就是为什么上面的 `CONFIG_ISA64`、`CONFIG_MBASE` 能用。没有这行，整个条件选择就失去了依据。
- [common.h:27-32](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/include/common.h#L27-L32) 用 `#ifdef CONFIG_TARGET_AM` 在 AM 模式下改用 `klib.h`（AM 自带的精简库），native 模式下用标准 `assert.h`/`stdlib.h`。这是「同一份 `common.h` 适配两种宿主环境」的体现。

#### 4.3.4 代码实践

**实践目标**：观察 `word_t` 宽度如何随 `CONFIG_ISA64` 变化，并理解它对 `sizeof(CPU_state)` 的影响。

**操作步骤**：

1. 阅读 [common.h:38](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/include/common.h#L38)，写出 `CONFIG_ISA64` 已定义 / 未定义两种情况下 `word_t` 的展开结果。
2. 结合 [riscv32/include/isa-def.h:22](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/isa/riscv32/include/isa-def.h#L22) 的 `word_t gpr[32]`，估算 riscv32（`word_t=uint32_t`）时 `gpr` 数组占多少字节：\(32 \times 4 = 128\) 字节。
3. 假设开启 `CONFIG_RV64`（`word_t=uint64_t`），重算：\(32 \times 8 = 256\) 字节。体会「改一个宏，寄存器堆体积翻倍」。

**需要观察的现象**：`word_t` 是整个系统的「宽度基因」，寄存器、地址、立即数的尺寸都由它派生。

**预期结果**：你理解了为什么差分测试（u8）要按 `word_t` 宽度比对寄存器——宽度错了比对就毫无意义。

> 待本地验证：步骤 2、3 的字节数可用 `printf("%zu", sizeof(((CPU_state*)0)->gpr))` 在真实编译中验证。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `vaddr_t` 直接 `typedef word_t vaddr_t`，而 `paddr_t` 要单独用一个 `PMEM64` 判断？
**答案**：虚拟地址宽度严格等于机器字宽度（这是 ISA 定义），所以直接复用 `word_t`。而物理地址宽度取决于「物理内存有多大」——即便在 64 位 ISA 下，若物理内存不超过 4 GiB，物理地址用 32 位表示就够，能省内存、简化 MMU 逻辑。所以 `paddr_t` 用 `PMEM64`（由 `CONFIG_MBASE+CONFIG_MSIZE` 是否超 4 GiB 触发）独立判断。

**练习 2**：`FMT_WORD` 在 32 位下是 `"0x%08" PRIx32`。这里的 `08` 是什么意思？为什么是 8？
**答案**：`%08x` 表示以十六进制打印、宽度至少 8 位、不足前补 0。因为 32 位 = 4 字节 = \(4 \times 8 = 32\) 个二进制位 = \(32 / 4 = 8\) 个十六进制位，所以正好 8 位。64 位时换成 `"0x%016" PRIx64`（16 个十六进制位）。

---

### 4.4 filelist ISA 切换

#### 4.4.1 概念说明

前面三节解决了「类型怎么随 ISA 变」，本节解决最后一个问题：**编译时，到底把哪个 ISA 目录的 `.c` 文件编进来？** 四个 ISA 目录并存在磁盘上，如果全编进来会互相冲突（四份 `reg.c`、四份 `inst.c` 都定义了同名函数），必须只选一个。

这个选择由 [src/isa/filelist.mk](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/isa/filelist.mk) 完成，机制简洁到只有两行：把 `src/isa/$(GUEST_ISA)` 这个目录加进编译列表，同时把它的 `include` 加进头文件搜索路径。`$(GUEST_ISA)` 是 Make 变量，值来自 `CONFIG_ISA`。

#### 4.4.2 核心流程

「选 ISA」的完整链路横跨 Kconfig、Makefile、filelist、CFLAGS 四层：

```text
Kconfig: choice "Base ISA" → ISA_riscv / ISA_x86 / ...
              │  string CONFIG_ISA = "riscv32" / "x86" / ...
              ▼
Makefile:28  GUEST_ISA = remove_quote(CONFIG_ISA)   = "riscv32"
Makefile:30  NAME = $(GUEST_ISA)-nemu-interpreter   = "riscv32-nemu-interpreter"
Makefile:52  CFLAGS += -D__GUEST_ISA__=riscv32
              ▼
src/isa/filelist.mk:17  DIRS-y += src/isa/riscv32   ← 只编这个目录！
src/isa/filelist.mk:16  INC_PATH += .../src/isa/riscv32/include  ← 只找这份 isa-def.h
              ▼
Makefile:33-34  find ./src -name filelist.mk → 合并所有 DIRS-y → 最终 SRCS
              ▼
build/riscv32-nemu-interpreter   ← 二进制名也带 ISA
```

三件事在这里被一次性串起：

1. **源文件集合**：只有 `src/isa/riscv32/*.c` 被编译，`x86/mips32/loongarch32r` 被排除。
2. **头文件搜索路径**：`#include <isa-def.h>`（在 `isa.h` 里）只会命中 `src/isa/riscv32/include/isa-def.h`。
3. **C 宏**：`-D__GUEST_ISA__=riscv32` 让 `concat` 拼出 `riscv32_*` 系列。

三者同源于 `GUEST_ISA`，所以永远一致——不会出现「编了 riscv32 的源码却把 `__GUEST_ISA__` 定义成 x86」的错位。

#### 4.4.3 源码精读

- [src/isa/filelist.mk:16-17](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/isa/filelist.mk#L16-L17)：两行就是 ISA 切换的全部。`$(GUEST_ISA)` 是从 Makefile 传下来的变量。`INC_PATH` 决定 `#include <isa-def.h>` 找哪个目录，`DIRS-y` 决定编译哪些 `.c`。
- [Makefile:28-30](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/Makefile#L28-L30) 是 `GUEST_ISA` 的源头与 `NAME` 的拼装地。`remove_quote` 去掉 Kconfig 字符串两端的引号（`"riscv32"` → `riscv32`），`NAME = $(GUEST_ISA)-nemu-$(ENGINE)` 决定二进制名（如 `riscv32-nemu-interpreter`）。
- [Makefile:52](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/Makefile#L52) 把 `__GUEST_ISA__` 作为 C 宏注入：`-D__GUEST_ISA__=$(GUEST_ISA)`。注意这里**没有引号**，所以它是个标识符宏（`#define __GUEST_ISA__ riscv32`），正好喂给 `concat`。
- [Makefile:33-34](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/Makefile#L33-L34) `find -L ./src -name "filelist.mk"` 把所有 `filelist.mk`（包括 `src/filelist.mk`、`src/isa/filelist.mk`）都 include 进来，合并各自的 `DIRS-y`。
- [Kconfig:3-23](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/Kconfig#L3-L23) 是 `CONFIG_ISA` 的诞生地：四个互斥的 `ISA_*` 布尔选项组成 `choice`，再由一个 string `CONFIG_ISA` 根据哪个被选中给出 `"x86"`/`"riscv32"`/`"riscv64"`/…。

#### 4.4.4 代码实践（本讲主实践）

**实践目标**：亲手切换 GUEST_ISA（riscv ↔ x86），对比 `autoconf.h`、二进制名、`__GUEST_ISA__` 的连锁变化，把 4.2/4.3/4.4 三节打通。

**操作步骤**：

1. 先以默认（riscv）配置生成 `.config`：执行 `make menuconfig`，确认 *Base ISA* 选 `riscv`，保存退出；再 `make`（或仅 `make` 触发 syncconfig）。
2. 打开 `include/generated/autoconf.h`（menuconfig 自动生成），找到并记录三行：
   - `#define CONFIG_ISA "riscv32"`（或 `CONFIG_ISA_riscv 1`）
   - `CONFIG_ISA64` 是否定义
   - 相关的 `CONFIG_RV64` / `CONFIG_RVE`
3. 记录产物二进制名：`ls build/` 应看到 `riscv32-nemu-interpreter`（由 [Makefile:30](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/Makefile#L30) 决定）。
4. 再次 `make menuconfig`，把 *Base ISA* 改成 `x86`，保存；`make`。
5. 重新打开 `autoconf.h`，对比：`CONFIG_ISA` 变成 `"x86"`，`CONFIG_ISA_riscv` 消失、`CONFIG_ISA_x86` 出现。
6. `ls build/`，确认产物变成 `x86-nemu-interpreter`。
7. （可选）在两种配置下分别执行 `make V=1` 2>&1 | grep __GUEST_ISA__`，观察编译命令里 `-D__GUEST_ISA__=riscv32` 与 `-D__GUEST_ISA__=x86` 的差异。

**需要观察的现象**：

- 改一个菜单选项 → `CONFIG_ISA` 字符串变 → `GUEST_ISA` 变 → 二进制名变、编译的 ISA 目录变、`__GUEST_ISA__` 宏变，四处同步。
- 切到 x86 后，`src/isa/x86/*.c` 被编译，`src/isa/riscv32/*.c` 不再参与；`#include <isa-def.h>` 命中的是 `src/isa/x86/include/isa-def.h`，于是 `CPU_state` 变成 `x86_CPU_state`。

**预期结果**：你用一个完整的「切换—重编—对比」闭环，验证了本讲的中心论点——**`__GUEST_ISA__` 这个宏是驱动 isa.h 类型拼接的唯一开关，而 filelist.mk 是它的物理载体**。

> 待本地验证：本实践依赖本机有 `gcc` 与可运行的 `menuconfig`（需要 ncurses）。若环境不支持交互式 menuconfig，可直接编辑 `.config` 中 `CONFIG_ISA_x86=y` / `CONFIG_ISA_riscv=y` 后 `make`（或用 `make riscv32-defconfig` / 对应 defconfig 目标）。`autoconf.h` 与二进制名的对比在任何能编译的环境下都成立。

#### 4.4.5 小练习与答案

**练习 1**：如果把 `src/isa/filelist.mk` 里的 `$(GUEST_ISA)` 硬编码成 `x86`，但 menuconfig 仍选 riscv，编译会怎样？
**答案**：会出现「源码与宏错位」——编译的是 `src/isa/x86/*.c`（定义 `x86_CPU_state`），但 `__GUEST_ISA__=riscv32` 让 `isa.h` 把 `CPU_state` 拼成 `riscv32_CPU_state`。于是 x86 的源码引用 `x86_CPU_state`，而框架代码用 `CPU_state`（实为 `riscv32_CPU_state`），两者类型不一致，链接期或编译期报错。这正是为什么三处必须同源于一个 `GUEST_ISA`。

**练习 2**：为什么二进制名要带 ISA（`riscv32-nemu-interpreter`）而不是统一叫 `nemu`？
**答案**：因为不同 ISA 的 NEMU 是「不同的可执行文件」，可能需要并存（比如同时用 riscv 版和 x86 版做差分测试，或对比行为）。带 ISA 与引擎的名字让多个产物互不覆盖，[Makefile:30](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/Makefile#L30) 的 `NAME = $(GUEST_ISA)-nemu-$(ENGINE)` 就是为了这种隔离。

---

## 5. 综合实践

把本讲四个模块串成一个任务：**给一个「假想的新 ISA」接上 NEMU 的 ISA 抽象层**（纯源码阅读 + 设计，不真正新增目录）。

任务背景：假设你要让 NEMU 支持一个极简教学 ISA「mini」（只有 4 个通用寄存器 `r0..r3`，32 位定长指令）。请回答：

1. **目录**：应在 `src/isa/` 下新建什么目录？里面至少要有哪些文件？（对照 [src/isa/riscv32](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/isa/riscv32) 的结构回答。）
2. **类型拼接**：在 `src/isa/mini/include/isa-def.h` 里，应定义一个名为 `________` 的结构体标签，才能让 `isa.h` 的 `concat(__GUEST_ISA__, _CPU_state)` 在 `__GUEST_ISA__=mini` 时匹配上？写出该结构体（4 个 `word_t` 寄存器 + `vaddr_t pc`）。
3. **配置**：在 [Kconfig](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/Kconfig) 的 `choice "Base ISA"` 里要加什么？`config ISA` string 要加哪一行 `default "mini" if ISA_mini`？
4. **filelist**：`src/isa/filelist.mk` 需要改吗？为什么？（提示：它用的是 `$(GUEST_ISA)` 变量，不是硬编码的目录名。）
5. **验证**：实现后，`make menuconfig` 选 mini、`make`，预期二进制名是什么？

**参考答案要点**：

1. 新建 `src/isa/mini/`，至少含 `include/isa-def.h`、`init.c`、`inst.c`、`reg.c`、`local-include/reg.h`，可参考 riscv32 目录。
2. 标签必须是 `mini_CPU_state`（因为 `concat(mini, _CPU_state)`）。结构体示例代码：
   ```c
   typedef struct {
     word_t gpr[4];
     vaddr_t pc;
   } mini_CPU_state;
   ```
3. 在 `choice` 里加 `config ISA_mini bool "mini"`；在 `config ISA` string 加 `default "mini" if ISA_mini`。
4. **不需要改** `src/isa/filelist.mk`，因为它用 `$(GUEST_ISA)` 变量，会自动指向 `src/isa/mini`。这正是该抽象的价值——新增 ISA 不必动框架的 filelist。
5. 二进制名为 `mini-nemu-interpreter`（由 [Makefile:30](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/Makefile#L30) 拼出）。

> 这个练习揭示了 NEMU 多 ISA 设计的核心回报：**新增一个 ISA 的成本，被压到了「新建一个目录 + 加一个 Kconfig 选项」，框架代码零改动**。

---

## 6. 本讲小结

- NEMU 源码按「子系统分层（cpu/memory/device/monitor/utils）+ ISA 分叉（src/isa/<四套并存>）」组织，每次编译只选中一个 ISA 目录。
- `include/isa.h` 是 ISA 抽象契约：用 `concat(__GUEST_ISA__, _CPU_state)` 在预处理期把 `CPU_state` 拼成当前 ISA 的专属类型（如 `riscv32_CPU_state`、`x86_CPU_state`），并声明 `isa_exec_once`、`isa_mmu_check`、`isa_raise_intr` 等统一接口。
- `concat` 必须用两层宏（`concat`/`concat_temp`），否则 `##` 会阻止宏参数展开；`MUXDEF` 则用于「宏定义则取 A，否则取 B」的布尔选择。
- `include/common.h` 定义了全局基本类型 `word_t`/`sword_t`/`vaddr_t`/`paddr_t`，宽度由 `MUXDEF(CONFIG_ISA64, ...)` 自适应；它是整个系统的「宽度基因」。
- `src/isa/filelist.mk` 用 `$(GUEST_ISA)` 变量选取 ISA 目录与头文件路径，配合 `Makefile` 的 `-D__GUEST_ISA__=$(GUEST_ISA)` 和二进制名 `$(GUEST_ISA)-nemu-$(ENGINE)`，三处同源、永不错位。
- `src/isa/riscv64` 是指向 `riscv32` 的符号链接，RISC-V 32/64 共用一份源码，靠 `CONFIG_RV64` + `MUXDEF` 在编译期区分。

---

## 7. 下一步学习建议

本讲建立了「框架代码—ISA 抽象契约—ISA 具体实现」的三层结构认知。接下来可以顺着两条线深入：

1. **往下钻 ISA 实现（u2 → u5）**：先学 u2（SDB 监视器），它会在命令里用到 `isa_reg_display()` 这个契约函数；再到 u5（ISA 实现与指令），你会真正打开 `src/isa/riscv32/inst.c`，看 `isa_exec_once()` 如何取指、用 INSTPAT 译码、执行一条指令。
2. **横向看契约的使用方（u3、u4）**：u3（CPU 执行引擎）展示框架代码如何调用 `isa_exec_once()`；u4（内存系统）会用到 `word_t`/`vaddr_t`/`paddr_t` 和 `isa_mmu_check`。

建议在进入下一篇前，先完成第 5 节的「新 ISA 接入」设计练习——它能让你带着「契约双方」的视角去读后续源码，事半功倍。
