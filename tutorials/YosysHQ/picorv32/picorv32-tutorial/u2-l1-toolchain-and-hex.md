# RISC-V 工具链与从源码到 hex

## 1. 本讲目标

学完本讲后，你应当能够：

1. 说清楚 PicoRV32 为什么需要一套「纯 RV32I[M][C]」交叉工具链，以及 `make build-riscv32i[m][c]-tools` 这四个目标分别装在哪里。
2. 看懂根 `Makefile` 里固件的完整构建链：`.c/.S` → `firmware.elf` → `firmware.bin` → `firmware.hex`，并能解释每一步用到的工具（`gcc`、`objcopy`、`makehex.py`）。
3. 读懂 `firmware/sections.lds` 如何规定程序的入口地址与内存布局，并理解「为什么固件必须从地址 0 开始」。
4. 手工推演 `makehex.py` 如何把一段小端（little-endian）二进制翻成 Verilog `$readmemh` 能读的十六进制文本。

本讲承接 [u1-l2 仓库结构与构建系统](u1-l2-repo-and-build.md)：上一讲我们知道了「`make test` 会用到 `firmware/firmware.hex`」，这一讲就来回答「这个 `.hex` 到底是怎么从源码变出来的」。

## 2. 前置知识

在进入源码前，先用大白话把几个概念讲清楚。

- **交叉工具链（cross toolchain）**：你的开发机通常是 x86-64，但 PicoRV32 是 RISC-V 32 位 CPU。能在 x86 上运行、却生成 RISC-V 机器码的编译器套件，就叫交叉工具链。它的可执行文件通常带一个前缀，例如 `riscv32-unknown-elf-gcc`。
- **RV32I / M / C**：RISC-V 的指令是模块化的。`I` 是基础整数指令集（必备）；`M` 是乘除法扩展（`mul`/`div` 等）；`C` 是压缩指令集（16 位编码）。`RV32IMC` = 32 位 + 乘除 + 压缩，这正是 PicoRV32 的默认配置。
- **`-march` 与 `-mabi`**：`-march=rv32imc` 告诉编译器「可以生成这些指令」；`-mabi=ilp32` 告诉编译器「int、long、指针都是 32 位」。两者必须配套。
- **裸机程序（bare-metal / freestanding）**：PicoRV32 上没有操作系统，也没有标准库。固件用 `-ffreestanding -nostdlib` 编译，自己直接读写内存地址（例如往 `0x10000000` 写一个字节就是往 UART 输出一个字符）。
- **ELF / BIN / HEX 三种格式**：
  - `.elf`：带节区、符号、调试信息的可执行文件，链接器（`ld`/`gcc`）产出。
  - `.bin`：把 `.elf` 里「真正要装进内存的字节」原样抠出来的纯二进制（`objcopy -O binary`）。
  - `.hex`：把 `.bin` 翻译成 Verilog `$readmemh` 能读的文本，每行一个 32 位字，仿真时用它把程序「灌」进测试台的内存模型。

> 一句话串起来：源码 → `gcc` 链接成 `.elf` → `objcopy` 抠成 `.bin` → `makehex.py` 翻成 `.hex` → 仿真器 `$readmemh` 读进内存。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [Makefile](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/Makefile) | 构建「大脑」：定义工具链前缀、四套工具链构建目标，以及固件 `.elf/.bin/.hex` 三步构建规则。 |
| [firmware/sections.lds](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/firmware/sections.lds) | 链接脚本：规定内存区间的起始地址、长度，以及代码/数据如何摆放进内存。 |
| [firmware/makehex.py](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/firmware/makehex.py) | 把 `.bin` 转成 `$readmemh` 文本的小脚本，负责处理小端字节序与定长填充。 |
| [README.md](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/README.md) | 「Building a pure RV32I Toolchain」一节给出手工安装工具链的官方步骤与 Ubuntu 依赖。 |

另外会顺带引用 [firmware/start.S](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/firmware/start.S) 与 [testbench.v](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/testbench.v)，用来印证「入口在地址 0」和「`.hex` 最终被 `$readmemh` 吃掉」这两件事。

---

## 4. 核心概念与源码讲解

### 4.1 工具链构建目标

#### 4.1.1 概念说明

PicoRV32 的固件需要一套「针对纯 RV32I[M][C]」的交叉工具链。为什么强调「纯」？因为发行版自带的 RISC-V 工具链（例如 Ubuntu 的 `gcc-riscv64-unknown-elf`）默认库是按 RV32G/RV64G 编译的，里面会用到 PicoRV32 不支持的原子指令（A 扩展）、浮点指令（F/D 扩展）等。为了让库本身也只用基础整数指令，需要用 `--with-arch=rv32i` 重新构建整套工具链（含 newlib C 库）。

项目提供了四个 make 目标，分别对应四种 ISA 配置，装到四个不同目录里，互不干扰。

#### 4.1.2 核心流程

工具链构建的整体流程：

1. `make download-tools`：把 `riscv-gnu-toolchain` 等仓库以裸仓库形式克隆到 `/var/cache/distfiles/`，作为后续克隆的 `--reference` 缓存，加速多次构建。
2. `make build-riscv32XX-tools`：先确认（会清空对应安装目录），再调用 `build-riscv32XX-tools-bh` 后台目标。
3. 后台目标：克隆 `riscv-gnu-toolchain`，checkout 到固定 commit `411d134`，初始化子模块，`configure --with-arch=rv32XX --prefix=/opt/riscv32XX`，最后 `make` 编译安装。

四套目标与安装位置、ISA 的对应关系：

| Make 目标 | 安装目录 | ISA |
| --- | --- | --- |
| `build-riscv32i-tools` | `/opt/riscv32i/` | RV32I |
| `build-riscv32ic-tools` | `/opt/riscv32ic/` | RV32IC |
| `build-riscv32im-tools` | `/opt/riscv32im/` | RV32IM |
| `build-riscv32imc-tools` | `/opt/riscv32imc/` | RV32IMC |

而 `make build-tools` 会一次性把这四套全装上。

#### 4.1.3 源码精读

工具链的固定版本号和安装根目录在 Makefile 顶部就定死了：

[Makefile:2-3](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/Makefile#L2-L3) 锁定 `riscv-gnu-toolchain` 的 git 版本 `411d134`，并把安装根目录设为 `/opt/riscv32`：

```makefile
RISCV_GNU_TOOLCHAIN_GIT_REVISION = 411d134
RISCV_GNU_TOOLCHAIN_INSTALL_PREFIX = /opt/riscv32
```

接下来是最关键的一行——工具链前缀。所有编译命令（`gcc`/`objcopy`/`ld`）都靠它找到正确的可执行文件：

[Makefile:18-19](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/Makefile#L18-L19) 定义工具链前缀与压缩指令开关：

```makefile
TOOLCHAIN_PREFIX = $(RISCV_GNU_TOOLCHAIN_INSTALL_PREFIX)i/bin/riscv32-unknown-elf-
COMPRESSED_ISA = C
```

注意前缀末尾拼了一个字母 `i`：`/opt/riscv32` + `i` + `/bin/riscv32-unknown-elf-` = `/opt/riscv32i/bin/riscv32-unknown-elf-`。也就是说，**默认情况下固件用的是 `riscv32i`（纯 RV32I）那套工具链**。这正是 README 提示的：发行版工具链可这样覆盖使用——`make TOOLCHAIN_PREFIX=riscv64-unknown-elf-`。

四个构建目标是用 GNU make 的 `eval`/`call` 模板批量生成的，避免重复写四遍：

[Makefile:158-161](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/Makefile#L158-L161) 用同一份模板实例化出四套目标：

```makefile
$(eval $(call build_tools_template,riscv32i,rv32i))
$(eval $(call build_tools_template,riscv32ic,rv32ic))
$(eval $(call build_tools_template,riscv32im,rv32im))
$(eval $(call build_tools_template,riscv32imc,rv32imc))
```

模板里真正调 `configure` 的那一行决定了 ISA 与安装目录的对应：

[Makefile:153](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/Makefile#L153) 用 `--with-arch` 指定 ISA，用 `--prefix` 指定安装目录，目录后缀来自去掉名字里的 `riscv32`（例如 `riscv32imc` → `imc`）：

```makefile
mkdir build; cd build; ../configure --with-arch=$(2) --prefix=$(RISCV_GNU_TOOLCHAIN_INSTALL_PREFIX)$(subst riscv32,,$(1)); make
```

于是 `riscv32imc` → `--with-arch=rv32imc --prefix=/opt/riscv32imc`，与上表完全一致。README 的 [Building a pure RV32I Toolchain](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/README.md#L614-L664) 一节也给出了等价的手工命令与所需 Ubuntu 依赖包。

#### 4.1.4 代码实践

**实践目标**：不实际编译工具链（那要几十分钟），而是用 `make -n` 做一次「干跑」，看清每条命令会被展开成什么。

**操作步骤**：

1. 在项目根目录运行 `make -n build-riscv32imc-tools-bh 2>/dev/null | head -40`。
2. 在输出里找到 `configure` 那一行，确认它的 `--with-arch=` 和 `--prefix=` 参数。
3. 再运行 `make -n TOOLCHAIN_PREFIX=riscv64-unknown-elf- firmware/firmware.elf 2>/dev/null | head -20`，观察前缀变化后 `gcc` 的调用方式。

**需要观察的现象**：

- 第 2 步应看到 `--with-arch=rv32imc --prefix=/opt/riscv32imc`。
- 第 3 步应看到调用的是 `riscv64-unknown-elf-gcc`，而非默认的 `/opt/riscv32i/bin/riscv32-unknown-elf-gcc`。

**预期结果**：模板展开后，`configure` 参数与四套目录的对应关系和上表一致。如果机器上没有 make（极少见），可改为直接通读 [Makefile:133-156](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/Makefile#L133-L156) 的模板定义来推断。若无法运行，标注「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：为什么默认 `TOOLCHAIN_PREFIX` 指向 `riscv32i`（纯 I），而固件链接却用 `-march=rv32imc`？二者矛盾吗？

> **答案**：不矛盾。工具链的可执行文件（`gcc`、`as`、`ld`、`objcopy`）本身能处理任何 RISC-V 指令，「纯 RV32I 工具链」约束的是**预编译库**（newlib、libgcc）只含 I 指令。`-march=rv32imc` 只约束**本次编译生成**什么指令。只要源码自己写了 `mul`（属于 M），且链接时显式 `-march` 包含 `m`，就能正常生成 M 指令；链接进来的库仍只是 I 实现。

**练习 2**：`make build-tools` 会在 `/opt/riscv32*` 下创建几个目录？分别叫什么？

> **答案**：四个：`/opt/riscv32i`、`/opt/riscv32ic`、`/opt/riscv32im`、`/opt/riscv32imc`。见 [Makefile:166](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/Makefile#L166)。

---

### 4.2 链接脚本与内存布局

#### 4.2.1 概念说明

链接器（`ld`）需要知道两件事：把各个节（`.text` 代码、`.data` 数据、`.rodata` 只读数据等）按什么顺序排，以及它们最终落在哪些内存地址上。这两件事由**链接脚本**（linker script，`.lds`）描述。

PicoRV32 用的是 [firmware/sections.lds](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/firmware/sections.lds)，它非常短——只有 25 行，把所有节都塞进一个从地址 0 开始的区间。为什么从 0 开始？因为 [u1-l3](u1-l3-run-first-testbench.md) 讲过：复位后 CPU 把 `reg_pc` 设为 `PROGADDR_RESET`，而测试台里 `PROGADDR_RESET=0`。所以第一条指令必须放在地址 0。

#### 4.2.2 核心流程

`sections.lds` 的工作可以概括为两步：

1. **声明内存区**（`MEMORY`）：定义一个名为 `mem` 的区间，起始 `ORIGIN = 0x00000000`，长度 `LENGTH = 0x00018000`（96 KB）。
2. **摆放节区**（`SECTIONS`）：把所有输入节并进一个输出节 `.memory`，起始位置 `.=0x000000`，其中 `start*(.text)` 被显式地放在最前面，保证复位向量 `reset_vec` 优先排布。

96 KB 的固件区 + 预留的 32 KB 栈区 = 测试台 128 KB 内存，这正是脚本注释里写的内存划分。

#### 4.2.3 源码精读

[firmware/sections.lds:10-14](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/firmware/sections.lds#L10-L14) 声明 96 KB 的固件内存区，注释解释了 128 KB 测试台内存的划分：

```
MEMORY {
	/* the memory in the testbench is 128k in size;
	 * set LENGTH=96k and leave at least 32k for stack */
	mem : ORIGIN = 0x00000000, LENGTH = 0x00018000
}
```

`0x00018000` = 98304 字节 = 96 KB。栈放在更高的、固件区之外的地址（[firmware/start.S:381](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/firmware/start.S#L381) 里 `lui sp,(128*1024)>>12` 把栈指针设到 128 KB 处）。

[firmware/sections.lds:16-25](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/firmware/sections.lds#L16-L25) 把所有节收拢进 `.memory`，并让 `start` 文件的代码段排最前：

```
SECTIONS {
	.memory : {
		. = 0x000000;
		start*(.text);
		*(.text);
		*(*);
		end = .;
		. = ALIGN(4);
	} > mem
}
```

几个要点：

- `start*(.text)`：通配 `start*`（即 `start.o`）的 `.text` 段，优先放置。这保证 `start.S` 里的 `reset_vec`（见 [firmware/start.S:41-45](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/firmware/start.S#L41-L45)）落在地址 0，正好是 CPU 复位后取指的位置。
- `*(.text)`：其余所有文件的代码段。
- `*(*)`：兜底，把剩余所有节也塞进来（数据、只读数据等全放一起，这是裸机程序的常见简化）。
- `end = .`：导出一个符号 `end`，标记程序末尾，便于运行时知道堆从哪里开始。
- `> mem`：把整个 `.memory` 输出节放进前面声明的 `mem` 区间。

链接命令本身在 [Makefile:109-113](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/Makefile#L109-L113)，关键参数是 `-T,firmware/sections.lds`（指定链接脚本）和 `-march=rv32imc`（链接时的 ISA 取并集）：

```makefile
firmware/firmware.elf: $(FIRMWARE_OBJS) $(TEST_OBJS) firmware/sections.lds
	$(TOOLCHAIN_PREFIX)gcc -Os -mabi=ilp32 -march=rv32im$(subst C,c,$(COMPRESSED_ISA)) -ffreestanding -nostdlib -o $@ \
		-Wl,--build-id=none,-Bstatic,-T,firmware/sections.lds,-Map,firmware/firmware.map,--strip-debug \
		$(FIRMWARE_OBJS) $(TEST_OBJS) -lgcc
```

注意三个细节：

- `-ffreestanding -nostdlib`：不依赖宿主 OS、不要标准库启动代码。
- `-Wl,...,-Map,firmware/firmware.map,...`：顺便生成一份 `firmware.map` 符号映射表（本讲的实践会用到它）。
- `-lgcc`：最后链接 GCC 自带的辅助库（提供 `__mulsi3` 之类的软实现，当硬件没有 M 扩展时用得上）。

值得对比的是各编译单元的 `-march` 差异：固件 C 文件用 `rv32ic`（[Makefile:118-119](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/Makefile#L118-L119)），`start.S` 用 `rv32imc`（[Makefile:115-116](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/Makefile#L115-L116)），而 `tests/*.S` 用 `rv32im`（[Makefile:121-123](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/Makefile#L121-L123)）。这是因为：固件 C 代码不直接写乘法指令；`start.S` 里有 `hard_mul` 等 `mul` 指令（[firmware/start.S:504-506](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/firmware/start.S#L504-L506)），需要 M；上游 `riscv-tests` 是纯 32 位编码、不需压缩。最终链接用 `rv32imc` 取并集。

#### 4.2.4 代码实践

**实践目标**：亲手把一段汇编链接到 `sections.lds` 规定的地址，并用 `objdump` 验证「地址 0 放的就是第一条指令」。

**操作步骤**（示例代码，需本机有任一 riscv32 工具链）：

1. 新建 `firmware/tiny.S`，内容只是一个死循环：

   ```asm
   // 示例代码：最小的可链接汇编
   .section .text
   .global _start
   _start:
       addi x2, x2, 1      // x2 自增
       j _start            // 死循环
   ```

2. 编译为 `.o`（注意 `-march` 要和你的工具链匹配）：

   ```bash
   riscv32-unknown-elf-gcc -c -mabi=ilp32 -march=rv32imc -o tiny.o tiny.S
   ```

3. 用项目的链接脚本链接（`-T firmware/sections.lds`）：

   ```bash
   riscv32-unknown-elf-gcc -Os -mabi=ilp32 -march=rv32imc -ffreestanding -nostdlib \
       -Wl,-T,firmware/sections.lds,-Map,tiny.map -o tiny.elf tiny.o
   ```

4. 反汇编查看地址布局：

   ```bash
   riscv32-unknown-elf-objdump -d tiny.elf | head
   ```

**需要观察的现象**：`objdump -d` 输出里，`_start` 应出现在地址 `00000000`，第一条指令是 `addi`，第二条是 `j`。

**预期结果**：地址 0 处恰好是 `_start`，印证 `sections.lds` 把 `.text` 从 0 开始摆放。若工具链不可用，本步骤标注「待本地验证」，可改为阅读 `firmware.map`（在正常 `make test` 后生成）对照 `start` 的地址。

#### 4.2.5 小练习与答案

**练习 1**：如果把 `sections.lds` 里的 `. = 0x000000;` 改成 `. = 0x000100;`，固件还能在 PicoRV32 上正常启动吗？为什么？

> **答案**：不能（至少默认配置下不能）。复位后 CPU 从 `PROGADDR_RESET=0` 取指，而第一条指令被链接到了 `0x100`，地址 0 处是空的（`0x00000013` 即 `nop` 的语义，或全零被解码成非法指令），CPU 取不到正确的启动代码。

**练习 2**：`start*(.text)` 这一行的作用是什么？去掉它会有什么后果？

> **答案**：它强制把 `start.o` 的 `.text` 排在输出节最前面，确保 `reset_vec` 落在地址 0。去掉后，链接器按输入文件顺序排布，`reset_vec` 不一定还在地址 0，CPU 复位取到的第一条指令就不确定了。

---

### 4.3 二进制转 hex

#### 4.3.1 概念说明

`.bin` 是一串原始字节，仿真器并不能直接「吃」它。Verilog 提供了系统任务 `$readmemh(filename, array)`：它读一个文本文件，每行解析成一个十六进制数，依次填进数组。所以需要一个翻译脚本，把二进制字节流转成「每行一个 32 位十六进制字」的文本——这就是 [firmware/makehex.py](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/firmware/makehex.py) 的职责。

这里面藏着一个**字节序（endianness）陷阱**：RISC-V 是小端的，`.bin` 里最低字节存在最低地址；而 `$readmemh` 的一行是把整行当成一个数值的最高位写在最左边。所以脚本必须把字节顺序「反转」后再拼接。

#### 4.3.2 核心流程

`makehex.py` 的处理流程：

1. 从命令行读取 `binfile`（输入 `.bin`）和 `nwords`（要输出多少个 32 位字）。
2. 把整个 `.bin` 读进内存，做两个断言：长度必须小于 `4*nwords`（装得下），且必须是 4 的倍数（字对齐）。
3. 循环 `nwords` 次：
   - 对前 `len/4` 个字，取 4 字节，按「高字节在前」打印成 8 位十六进制；
   - 对超出 `.bin` 长度的字，打印 `0`（填充）。

设一个 4 字节字在小端 `.bin` 中按地址递增为 \( b_0, b_1, b_2, b_3 \)（\( b_0 \) 最低位字节），它表示的 32 位数值为：

\[
\text{value} = b_0 + b_1\cdot 2^{8} + b_2\cdot 2^{16} + b_3\cdot 2^{24}
\]

要把它写成 `$readmemh` 期望的十六进制文本（高位在左），就得按 \( b_3\, b_2\, b_1\, b_0 \) 的顺序输出——这正是脚本里 `% (w[3], w[2], w[1], w[0])` 做的事。

#### 4.3.3 源码精读

[Makefile:102-103](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/Makefile#L102-L103) 定义 `.hex` 规则：把 `.bin` 喂给脚本，输出 32768 个字（= 128 KB，正好填满测试台内存），重定向到 `.hex`：

```makefile
firmware/firmware.hex: firmware/firmware.bin firmware/makehex.py
	$(PYTHON) firmware/makehex.py $< 32768 > $@
```

中间一步 `.bin` 由 `objcopy` 从 `.elf` 抠出，剥掉所有 ELF 头和节区信息：

[Makefile:105-107](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/Makefile#L105-L107) 把 ELF 转成纯二进制：

```makefile
firmware/firmware.bin: firmware/firmware.elf
	$(TOOLCHAIN_PREFIX)objcopy -O binary $< $@
	chmod -x $@
```

脚本本体只有十几行。[firmware/makehex.py:12-19](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/firmware/makehex.py#L12-L19) 读参数、读文件、做两个断言：

```python
binfile = argv[1]
nwords = int(argv[2])

with open(binfile, "rb") as f:
    bindata = f.read()

assert len(bindata) < 4*nwords
assert len(bindata) % 4 == 0
```

[firmware/makehex.py:21-26](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/firmware/makehex.py#L21-L26) 循环输出，注意 `% (w[3], w[2], w[1], w[0])` 的反序：

```python
for i in range(nwords):
    if i < len(bindata) // 4:
        w = bindata[4*i : 4*i+4]
        print("%02x%02x%02x%02x" % (w[3], w[2], w[1], w[0]))
    else:
        print("0")
```

**用真实指令走一遍**。`addi x2, x2, 1` 的 32 位编码是 `0x00110113`。在小端 `.bin` 里，它的 4 字节按地址递增为：

| 地址偏移 | 字节 | 含义 |
| --- | --- | --- |
| +0 | `0x13` | 最低位字节 |
| +1 | `0x01` | |
| +2 | `0x11` | |
| +3 | `0x00` | 最高位字节 |

即 `w = (0x13, 0x01, 0x11, 0x00)`。脚本打印 `% (w[3], w[2], w[1], w[0])` = `% (0x00, 0x11, 0x01, 0x13)` → `00110113`，正好还原成 `0x00110113`。`$readmemh` 把这行读成数值 `0x00110113` 写入内存对应字——和 CPU 取指看到的指令完全一致。

最后，`.hex` 是怎么被用的？在测试台里：

[testbench.v:249-254](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/testbench.v#L249-L254) 默认从 `firmware/firmware.hex` 读入，灌进内存模型：

```verilog
reg [1023:0] firmware_file;
initial begin
    if (!$value$plusargs("firmware=%s", firmware_file))
        firmware_file = "firmware/firmware.hex";
    $readmemh(firmware_file, mem.memory);
end
```

可以用 `+firmware=xxx.hex` 在运行时换固件，默认就是 `make` 产出的那份。

#### 4.3.4 代码实践

**实践目标**：用 Python 直接调用 `makehex.py`，手工验证字节序翻转是否正确。

**操作步骤**（本机有 `python3` 即可，不依赖 RISC-V 工具链）：

1. 用 `printf` 造一个 4 字节的二进制文件，内容就是 `addi x2, x2, 1` 的小端字节 `13 01 11 00`：

   ```bash
   printf '\x13\x01\x11\x00' > /tmp/tiny.bin
   ```

2. 用项目脚本转成 hex（要 8 个字，便于看到填充效果）：

   ```bash
   python3 firmware/makehex.py /tmp/tiny.bin 8
   ```

3. 观察第一行与剩余行。

**需要观察的现象**：第一行应为 `00110113`（指令正确还原），其余 7 行应为 `0`（填充）。

**预期结果**：与上文手算完全一致。如果把 `w[3], w[2], w[1], w[0]` 误写成 `w[0], w[1], w[2], w[3]`，第一行会变成 `13011100`，那就是错的字节序——可以临时改一下脚本对比现象（改完记得还原，不要改动源码留痕）。

> 注意：实践时若临时修改了 `makehex.py`，结束后请用 `git checkout firmware/makehex.py` 还原，本讲不允许留下对源码的改动。

#### 4.3.5 小练习与答案

**练习 1**：`makehex.py` 里两个 `assert` 分别防什么错？

> **答案**：`len(bindata) < 4*nwords` 防止固件比目标内存还大（装不下）；`len(bindata) % 4 == 0` 防止 `.bin` 不是 4 字节整数倍（无法按整字输出，说明链接/objcopy 出了问题）。

**练习 2**：Makefile 里固定输出 32768 个字，但 `sections.lds` 的固件区只有 96 KB（24576 字）。多出来的字是什么？会出错吗？

> **答案**：多出来的是 `0` 填充，对应固件区之后的栈区及未用内存。不会出错：`makehex.py` 对超出 `.bin` 长度的部分打印 `0`，`$readmemh` 把这些 0 填进内存高位区域，正好把栈区清零。32768 字 = 128 KB = 测试台内存大小。

---

## 5. 综合实践

把三个模块串起来，完整走一遍「源码 → hex」的链路，这正是本讲规格里要求的核心实践。

**任务**：写一个最小 `.S` 文件（仅含若干 `addi`/`li` 与一个死循环），用 riscv32 工具链按 `firmware/sections.lds` 编译为 `.elf`/`.bin`，再用 `makehex.py` 生成 `.hex`，并用 `readelf`/`objdump` 核对入口地址与节布局。

**建议步骤**：

1. 在 `firmware/` 下新建 `mini.S`（示例代码）：

   ```asm
   // 示例代码：综合实践用最小固件
   .section .text
   .global _start
   _start:
       li x1, 0          // 清零
       li x2, 0
   loop:
       addi x2, x2, 1    // 计数
       addi x1, x1, 2    // 另一个计数
       j loop            // 死循环
   ```

2. 编译、链接（复用项目的链接脚本与参数）：

   ```bash
   TOOLCHAIN=riscv32-unknown-elf-       # 或 riscv64-unknown-elf-
   $TOOLCHAIN/gcc -c -mabi=ilp32 -march=rv32imc -o mini.o mini.S
   $TOOLCHAIN/gcc -Os -mabi=ilp32 -march=rv32imc -ffreestanding -nostdlib \
       -Wl,--build-id=none,-T,firmware/sections.lds,-Map,mini.map -o mini.elf mini.o
   $TOOLCHAIN/objcopy -O binary mini.elf mini.bin
   python3 firmware/makehex.py mini.bin 32768 > mini.hex
   ```

3. 用 `readelf` 核对入口地址：

   ```bash
   $TOOLCHAIN/readelf -h mini.elf | grep Entry
   ```

   入口（Entry point）应为 `0x00000000`（因为 `sections.lds` 从 0 开始，且 `_start` 在最前）。

4. 用 `objdump` 核对节布局和指令：

   ```bash
   $TOOLCHAIN/objdump -d mini.elf
   ```

   `_start` 应在地址 `0x0`，能看到 `li`/`addi`/`j` 的真实编码。

5. 验证 `mini.hex`：第一行应等于 `objdump` 里地址 0 处那条指令的十六进制编码（注意小端翻转）。

**验收标准**：

- `readelf` 显示入口地址 = `0x0`；
- `objdump -d` 第一条指令地址 = `0x0`；
- `mini.hex` 第一行的值 = `objdump` 中第一条指令编码（字节序翻转后）。

如果手头没有 riscv 工具链，可退化为「源码阅读型实践」：阅读正常 `make test` 生成的 `firmware/firmware.map` 与 `firmware/firmware.hex` 头几行，对照 [firmware/start.S:41-45](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/firmware/start.S#L41-L45) 的 `reset_vec`，确认地址 0 处放的是 `waitirq`/`maskirq`/`j start` 这几条指令。这部分结果「待本地验证」。

---

## 6. 本讲小结

- PicoRV32 用四个 make 目标 `build-riscv32i[m][c]-tools` 把「纯 RV32I[M][C]」工具链分别装进 `/opt/riscv32i[m][c]`，固定版本 `411d134`；默认 `TOOLCHAIN_PREFIX` 指向 `riscv32i`。
- 固件构建是清晰的三段链：`.c/.S` 经 `gcc` 链接成 `firmware.elf`，`objcopy -O binary` 抠成 `firmware.bin`，`makehex.py` 翻成 `firmware.hex`。
- `firmware/sections.lds` 把所有节塞进从地址 `0` 开始的 96 KB 区间，并强制 `start.o` 的代码段排最前，保证复位向量落在 CPU 取指的地址 0。
- 不同编译单元的 `-march` 不同（固件 C 用 `rv32ic`、`start.S` 用 `rv32imc`、tests 用 `rv32im`），链接时取并集 `rv32imc`。
- `makehex.py` 的核心是处理小端字节序：把 4 字节按高字节在前拼成一行十六进制，再对超出 `.bin` 的部分用 `0` 填充到 32768 字（128 KB），最终被测试台的 `$readmemh` 读进内存。
- 入口地址、节布局、最终字节编码，三者可以通过 `readelf`/`objdump` 与 `.hex` 交叉验证。

## 7. 下一步学习建议

下一讲 [u2-l2 第一个固件：Hello World 与内存映射 I/O](u2-l2-hello-and-mmio.md) 会让你第一次写出能被 PicoRV32 执行的 C 代码——通过往 `0x10000000` 写字节实现 `print_chr`/`print_str`，把「地址即设备」的内存映射 I/O 概念落到代码上。

巩固本讲内容的延伸阅读：

- 阅读 [firmware/riscv.ld](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/firmware/riscv.ld)，对比它与 `sections.lds` 的差异——它是为「链接 newlib」准备的更完整脚本，入口在 `0x10000`。
- 读完本讲的构建链后，回头重看 [u1-l2](u1-l2-repo-and-build.md) 里 `make test` 的依赖图，确认 `firmware/firmware.hex` 在整张图里的位置。
- 进阶可关注 [u5-l3 原生内存接口与传输状态机](u5-l3-memory-interface.md)：`.hex` 灌进内存后，CPU 如何通过 `mem_valid`/`mem_ready` 握手去取这些指令。
