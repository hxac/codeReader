# 集成测试：croc_boot RISC-V 内核

## 1. 本讲目标

本讲承接 [u8-l1 测试体系](u8-l1-test-infrastructure.md)。u8-l1 讲的是「**单元级**等价性测试」——用 `equiv_make`/`equiv_induct` 把 `read_slang` 的产物和一个参考网表逐位比对。这类测试快、确定、适合回归，但它们只检查「输出网表是否等价」，**无法回答一个更尖锐的问题**：sv-elab 综合出来的电路，真的能跑吗？

croc_boot 回答的就是这后一个问题。它把一个真实的 RISC-V 片上系统（croc）整体喂给 `read_slang`，综合后用 CXXRTL 仿真到「CPU 真的启动、真的执行了一段固件」，最后用一个「金丝雀（canary）」内存值证明整条链路无误。

学完本讲你应当：

- 读懂 `tests/croc_boot/` 下 `main.cc`、`run.sh`、`prepare.ys_`、`debugger.tcl_` 四个文件各自的角色，并能讲清从源码到仿真启动的完整数据流。
- 理解「用真实 IP 做端到端启动验证」相比单元等价性测试的意义与代价。
- 了解 CI 中与 croc_boot 并列的「兼容套件（compat-suite）」测试矩阵在做什么，以及它们各自覆盖了 sv-elab 的哪些能力。

## 2. 前置知识

- **CXXRTL**：Yosys 自带的一个后端，它把 RTLIL 网表「编译」成一份可链接的 C++ 仿真模型（`write_cxxrtl` 命令）。你拿到的是一个 C++ 类，调用其 `step()` 方法就推进一个时钟周期，读/写其 `p_端口` 成员就是驱动/观察信号。本讲的 `main.cc` 正是这样一个 CXXRTL testbench。
- **JTAG / remote_bitbang**：JTAG 是硬件调试标准；OpenOCD 是把「调试器软件」和「JTAG 时序」桥接起来的工具。`remote_bitbang` 是 OpenOCD 的一种适配器协议——它不驱动真实硬件引脚，而是通过一个 Unix 域套接字，让另一端的「虚拟 JTAG」（本讲里就是 CXXRTL 仿真模型）按字节协议逐拍拉高/拉低 TCK/TMS/TDI。这样就能用真实 OpenOCD + 真实 GDB 协议去调试一个纯软件仿真出来的 CPU。
- **RISC-V 调试（Debug Module Interface, DMI）**：croc 内核实现了 RISC-V External Debug 规范，OpenOCD 经 JTAG 访问 Debug Module，再由 Debug Module 读写 CPU 寄存器与系统总线（halt、load_image、read_memory 都走这条路）。
- **`$dff` 与 `clk2fflogic`**：`clk2fflogic` 是 Yosys 的 pass，把隐式时钟描述统一拍平成显式 `$dff`；本讲会看到 croc 把所有触发器进一步映射到一个自定义的 `__ff` 单元，以便 `write_cxxrtl` 产出干净的仿真模型。

如果你对 sv-elab 的前端流程还不熟，建议先读 [u2-l1](u2-l1-frontend-registration.md) 与 [u2-l2](u2-l2-slang-driver-pipeline.md)；对 `read_slang` 选项不熟则读 [u2-l3](u2-l3-synthesis-settings.md)。

## 3. 本讲源码地图

本讲聚焦 `tests/croc_boot/` 目录与触发它的 CI 配置：

| 文件 | 作用 |
| --- | --- |
| `tests/croc_boot/prepare.ys_` | Yosys 脚本（`_` 后缀使其不被测试目录扫描当成普通用例）。从 `read_slang` 综合到 `write_cxxrtl` 生成 `croc_soc.cc`，并就地 `g++` 编译出仿真可执行文件 `main`。 |
| `tests/croc_boot/main.cc` | CXXRTL testbench。实例化 `croc_soc` 仿真模型，复位置位，播种 idle 指令，然后化身「虚拟 JTAG」在一个 Unix 套接字上听 OpenOCD 的命令。 |
| `tests/croc_boot/run.sh` | 一键编排：跑 `prepare.ys_` 造 `main` → 后台启动 `main` → 跑 OpenOCD 加载固件并校验金丝雀。 |
| `tests/croc_boot/debugger.tcl_` | OpenOCD 脚本。配置 `remote_bitbang`、建立 RISC-V 调试链、halt → 灌 `helloworld.ihex` → 置 PC → resume → halt → 读金丝雀。 |
| `tests/croc_boot/helloworld.ihex` | Intel HEX 格式的最小 RISC-V 固件，由 OpenOCD 经 DMI 写入仿真模型的指令存储。 |
| `.github/workflows/build-and-test.yaml` | CI 编排。其中 `test-croc-boot` job 下载预打包的 croc 源码、装 OpenOCD、调 `run.sh`；`test-compat` job 则跑兼容套件矩阵。 |

注意一个关键事实：croc_boot **不在** `tests/CMakeLists.txt` 的 `ALL_TESTS` 列表里，所以 `ctest` 不会跑它（见 [tests/CMakeLists.txt:L26-L90](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/tests/CMakeLists.txt#L26-L90)）。它是一条独立的、更重的 CI 流水线，理由见 4.1。

## 4. 核心概念与源码讲解

### 4.1 croc 启动测试：为什么用一个真实 RISC-V SoC

#### 4.1.1 概念说明

「等价性测试」有一个固有盲区：它需要一个**参考实现**。对 `tests/unit/dff.ys` 这类小用例，参考网表可以手写或用 `read_verilog` 另走一条路得到。但对一个上萬行的真实 SoC，你既无法手写参考网表，也很难找到一个「绝对正确」的对照综合器——否则你就不需要 sv-elab 了。

croc_boot 换了一个思路：**不比网表，比行为**。它的逻辑是——

> 如果 sv-elab 综合出的 croc，在 CXXRTL 里能被真正的 OpenOCD 当成一块真板子调试，能加载固件、能取指执行、最后在内存里留下一个只有「正确执行」才会出现的金丝雀值，那 sv-elab 的翻译在「功能正确性」上就过了关。

这是一种 **differential validation by execution**：把「综合器是否正确」转化为「下游 CPU 是否按预期运行」。它的好处是不需要参考网表，缺点是慢、非确定性来源多（仿真器、OpenOCD 版本、固件），所以只能放在独立 CI job 里，不像单元测试那样每次都跑。

「真实 IP」的价值在于覆盖面：croc 是一个用 SystemVerilog 写的、含接口（interface）、结构体、generate、存储器、断言、多层层次的完整 SoC，远比单元用例构造的人造代码更能暴露 sv-elab 在「真刀真枪的设计」上的缺陷。

#### 4.1.2 核心流程

croc_boot 的端到端流程可以画成下面这条链：

```
croc 源码 (croc.f 列出的 .sv)
        │  read_slang（sv-elab 综合成 RTLIL）
        ▼
RTLIL 网表 croc_soc
        │  prep / async2sync / clk2fflogic / chtype / bwmuxmap
        ▼
write_cxxrtl  →  croc_soc.cc（C++ 仿真模型）
        │  g++ 编译 main.cc + croc_soc.cc
        ▼
可执行文件 main（内含仿真模型 + 虚拟 JTAG 服务器）
        │  后台运行，监听 /tmp/croc-jtag-bitbang.sock
        ▼
OpenOCD（remote_bitbang 客户端）
        │  经 JTAG/DMI halt → load helloworld.ihex → resume
        ▼
CPU 执行固件 → 在 0x03000008 写入 7
        │  OpenOCD halt 后 read_memory 校验
        ▼
金丝雀 == 7 ？  通过 / 失败
```

注意链条里有两类工作：前半段（到 `write_cxxrtl`）是「综合」，由 sv-elab + Yosys 完成；后半段（CXXRTL 仿真 + OpenOCD 调试）是「仿真验证」。本讲关注的是这两段如何被 `run.sh` 串起来，以及 sv-elab 在其中被检验了什么。

#### 4.1.3 源码精读

先看编排脚本 [tests/croc_boot/run.sh:L1-L11](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/tests/croc_boot/run.sh#L1-L11)，它只有三条实质命令：

```bash
rm -rf /tmp/croc-jtag-bitbang.sock
(cd "$TEST_DIR" &&
    yosys -m ../../build/slang.so -s prepare.ys_ &&
    ./main /tmp/croc-jtag-bitbang.sock)
(cd "$TEST_DIR" &&
    openocd -f debugger.tcl_ -d)
```

- `yosys -m ../../build/slang.so -s prepare.ys_`：以插件形式加载 sv-elab（回顾 [u2-l1](u2-l1-frontend-registration.md)：`-m slang.so` 注入 `read_slang` 命令），并执行 `prepare.ys_`。这一步的产物是当前目录下的 `main` 可执行文件（见 4.2.3，`prepare.ys_` 末尾会调 `g++`）。
- `./main /tmp/croc-jtag-bitbang.sock`：启动仿真 + 虚拟 JTAG 服务器。注意它必须**先于** OpenOCD 启动，因为它要在套接字上 `bind`/`listen`。`main` 会把自己 daemon 化（见下文 `fork`），所以这条命令立刻返回，脚本得以继续。
- `openocd -f debugger.tcl_ -d`：连上同一个套接字，驱动调试流程。

`set -ex`（[run.sh:L2](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/tests/croc_boot/run.sh#L2)）意味着任何一条命令失败（包括 OpenOCD 末尾因金丝雀不符而 `shutdown error`）都会让整个脚本非零退出，CI 据此判定红/绿。

再看 testbench [tests/croc_boot/main.cc:L54-L83](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/tests/croc_boot/main.cc#L54-L83) 的「上电复位 + idle 播种」段：

```cpp
cxxrtl_design::p_croc__soc top;
top.p_gclk.set(false);
top.step();
auto step = [&](){
    top.p_gclk.set(true);  top.step();
    top.p_gclk.set(false); top.step();
};
top.p_testmode__i.set(false);
top.p_rst__ni.set(false);
top.p_clk__i.set(false);  step();
top.p_clk__i.set(true);   step();
top.p_rst__ni.set(true);  step();   // 释放复位
top.p_fetch__en__i.set(true); step(); // 允许取指
for (int i = 0; i < 10; i++) { top.p_clk__i.set(false); step(); top.p_clk__i.set(true); step(); }

auto& mem = top.cell_p_i__croc.cell_p_gen__sram__bank_5b_0_5d__2e_i__sram.memory_p_sram;
mem[0x0].set(0x10500073u); // wfi
mem[0x1].set(0xffdff06fu); // jal x0, -4
```

- `top` 就是 `write_cxxrtl` 生成的 `croc_soc` 仿真模型实例（`p_croc__soc` 是转义后的类名，`p_` 前缀、`__` 替换特殊字符，是 CXXRTL 的命名规则）。
- `step()` lambda 模拟一个时钟周期：`gclk` 拉高再拉低，每个边沿调一次 `top.step()` 推进仿真。croc 用单一全局时钟 `gclk`（`prepare.ys_` 里 `add -global_input gclk 1` 引入，见 4.2.3）。
- 复位序列遵循 croc 的上电协议：先拉低 `rst_ni`、跑几拍，再拉高释放，最后置 `fetch_en_i` 允许取指。
- 关键的「idle 播种」：在指令存储第 0、1 字写入两条指令——`wfi`（等待中断）和 `jal x0, -4`（跳回自身，构成死循环）。这样 CPU 复位后不会「乱跑」撞进未初始化的存储，而是安静地停在 `wfi` 循环里，**直到 OpenOCD 通过 JTAG 把它 halt 住**。这是仿真 testbench 配合真实调试器的标准技巧。

随后 [main.cc:L85-L131](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/tests/croc_boot/main.cc#L85-L131) 进入 JTAG bitbang 服务循环，按 OpenOCD 发来的单字节命令驱动虚拟 JTAG 引脚并推进时钟——这部分是把软件仿真「伪装成硬件」的核心，但对 sv-elab 的验证没有直接关系，留作 4.1.4 的阅读练习。

#### 4.1.4 代码实践

**实践目标**：理解 testbench 如何「化身虚拟 JTAG」，并确认金丝雀校验确实依赖 CPU 真正执行。

**操作步骤**：

1. 打开 [main.cc:L85-L131](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/tests/croc_boot/main.cc#L85-L131)，逐个 `case` 画出 OpenOCD 单字节命令到 JTAG 引脚的映射：
   - `'0'..'7'`：把一个 0–7 的数字拆成 3 位，分别驱动 `tck`（bit2）、`tms`（bit1）、`tdi`（bit0）（见 [main.cc:L105-L110](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/tests/croc_boot/main.cc#L105-L110)）。
   - `'R'`：回采一拍 `tdo`（[main.cc:L99-L102](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/tests/croc_boot/main.cc#L99-L102)）。
   - `'r'..'u'`：驱动 `trst_n`/`srst`（[main.cc:L111-L115](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/tests/croc_boot/main.cc#L111-L115)）。
2. 打开 [debugger.tcl_:L16-L19](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/tests/croc_boot/debugger.tcl_#L16-L19)，确认金丝雀判定：`read_memory 0x03000008 32 1` 必须等于 `7`，否则 `shutdown error`。
3. 反汇编 `helloworld.ihex`（用 `riscv64-unknown-elf-objcopy -I ihex -O elf32-littleriscv helloworld.ihex /tmp/hw.elf` 再 `objdump -d`），在 `0x10000000` 附近的代码里找到「向 `0x03000008` 写 7」的若干指令，确认这个值是固件**运行后**才落地的副作用，而不是镜像里预先写好的。

**需要观察的现象**：固件被加载到 `0x10000000`（`reg pc 0x10000000`，见 [debugger.tcl_:L13](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/tests/croc_boot/debugger.tcl_#L13)），CPU 必须从那里取指、执行到写内存指令，金丝雀才会出现。

**预期结果**：若 sv-elab 综合无误，`read_memory` 返回 7；若 sv-elab 在某条指令翻译或某条数据通路上出错，CPU 要么 halt 失败、要么执行轨迹错误，金丝雀不会是 7，OpenOCD 以 `shutdown error` 退出，`run.sh` 因 `set -e` 报红。

> 本地复现需先按 4.3 取得 croc 源码包并装好 OpenOCD；若仅做源码阅读，步骤 1–3 即可完成，标注「待本地验证」的是实际跑 `run.sh` 的部分。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `main` 要先 `fork` 把自己 daemon 化（[main.cc:L34-L40](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/tests/croc_boot/main.cc#L34-L40)）？

**参考答案**：`run.sh` 是串行脚本。`main` 必须常驻以在套接字上服务 OpenOCD 的 JTAG 命令；如果不 daemon 化，`./main ...` 会一直阻塞，`run.sh` 永远走不到 `openocd` 那一行。daemon 化让父进程立刻 `exit(0)`，脚本得以继续启动 OpenOCD，而真正干活的子进程在后台监听套接字。

**练习 2**：如果把 `mem[0x0]` 的 `wfi` 换成一条会改写 `0x03000008` 的指令，金丝雀校验还有意义吗？

**参考答案**：没有。金丝雀的意义在于「只有 OpenOCD 加载的真实固件、经 CPU 真实执行后」才会写出 7。如果复位后 idle 代码自己就把金丝雀写出来了，那无论后续固件是否被正确加载/执行，校验都会误判通过——这正是播种 `wfi`+死循环这种「什么也不做」的 idle 的原因。

---

### 4.2 综合-仿真流程：从 prepare.ys_ 到 CXXRTL 启动

#### 4.2.1 概念说明

`prepare.ys_` 是一条「综合 + 仿真后端」混合脚本。它由两类命令组成：

1. **sv-elab / 综合**：`read_slang` 把 SystemVerilog 翻译成 RTLIL（这是 sv-elab 的本职，也是本测试真正要验证的对象）。
2. **下游 Yosys pass + CXXRTL 后端**：`prep`、`async2sync`、`clk2fflogic`、`chtype`、`bwmuxmap`、`write_cxxrtl`，以及末尾用 `exec` 调外部 `g++`。这些是 Yosys 自带能力，但**它们能成功的前提是 sv-elab 产出了合法且行为正确的网表**。

这里有一个常被初学者忽略的设计点：croc_boot 并不要求 sv-elab 的输出在「网表结构」上与某个参考一致（单元等价性测试才那么做），它只要求输出「能被后续 Yosys 流程消化、并最终仿真出一个能跑的 CPU」。这是一种更宽松、却更贴近真实用户的验收标准。

#### 4.2.2 核心流程

`prepare.ys_` 的命令可以分成五段：

| 段 | 命令 | 作用 |
| --- | --- | --- |
| ① 精化 | `read_slang --top croc_soc -F croc.f -D SYNTHESIS ... --keep-hierarchy` | sv-elab 把 croc 全部源码综合成顶层 `croc_soc` 的 RTLIL。 |
| ② 规整 | `hierarchy -top`、`stat`、`prep -nomem`、`async2sync`、`opt -mux_undef`、`setattr ... keep_hierarchy` | 选顶层、统计、把过程降级、把异步描述归一为同步、保留层次标记。 |
| ③ 时钟统一 | `clk2fflogic`、`chtype -map $ff __ff` | 把所有触发器统一到自定义 `__ff` 单元，方便 CXXRTL 取一个全局采样点。 |
| ④ 黑盒注入 | `read_verilog -icells <<EOF ... module __ff ...` | 用一段内联 Verilog 给 `__ff` 提供「壳子」（内含一个 `$dff`，标 `(* do_flatten *)`）。 |
| ⑤ 仿真导出 | `add -global_input gclk 1`、`flatten`、`bwmuxmap`、`write_cxxrtl -noflatten -g0 croc_soc.cc`、`exec g++ ...` | 注入全局时钟、展平、导出 C++ 模型、就地编译 `main`。 |

其中 **①是 sv-elab 的工作**，②–⑤是 Yosys。但 ① 若出错（比如某个 SV 构造翻译错、或某条诊断该报却没报），后面任何一段都可能失败或产出一个「能编译却跑不对」的模型。

#### 4.2.3 源码精读

**段 ①——`read_slang` 调用**，见 [tests/croc_boot/prepare.ys_:L1-L4](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/tests/croc_boot/prepare.ys_#L1-L4)：

```
read_slang --top croc_soc -F ../third_party/croc/croc.f \
        -D SYNTHESIS -D COMMON_CELLS_ASSERTS_OFF \
        --allow-use-before-declare -D TARGET_SYNTHESIS \
        --keep-hierarchy
```

逐个参数对照 sv-elab 的选项体系（回顾 [u2-l2](u2-l2-slang-driver-pipeline.md)、[u2-l3](u2-l3-synthesis-settings.md)）：

- `--top croc_soc`：指定顶层实例。sv-elab 经 slang driver 取 `topInstances` 后从此处开始精化。
- `-F ../third_party/croc/croc.f`：slang driver 的标准参数，读一个**文件列表**（bender 生成的 flist）。croc 的源码文件众多，靠 `-F` 一次性喂入。
- `-D SYNTHESIS`：定义 `SYNTHESIS` 宏。注意 sv-elab 的 `fixup_options` 默认就会注入 `SYNTHESIS=1`（见 u2-l2）；这里显式再写一次，属于「防御式」写法，确保即便用户加了 `--no-synthesis-define` 也仍置位。`COMMON_CELLS_ASSERTS_OFF`、`TARGET_SYNTHESIS` 是 croc 源码自己约定的宏，用于关闭不可综合的断言代码路径。
- `--allow-use-before-declare`：slang driver 的标准参数，允许「先使用后声明」。croc 这种工业级代码里常见前向引用，开此选项避免误报。
- `--keep-hierarchy`：sv-elab 专属选项，在 [src/slang_frontend.cc:L92-L93](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L92-L93) 注册，帮助信息写明「experimental; may crash」。croc 体积大，保留层次能让综合/调试更可控；这也是对 sv-elab 层次展平/保留这条主路径（见 [u7-l2](u7-l2-hierarchy-and-dissolve.md)）的一次实战检验。

**段 ③④——触发器统一与 `__ff` 壳子**，见 [prepare.ys_:L12-L24](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/tests/croc_boot/prepare.ys_#L12-L24)：

```
clk2fflogic
chtype -map $ff __ff

read_verilog -icells <<EOF
(* do_flatten *)
module __ff (D, Q, gclk);
parameter WIDTH = 1;
input gclk;
input [WIDTH-1:0] D;
output reg [WIDTH-1:0] Q;
$dff #(.CLK_POLARITY(1), .WIDTH(WIDTH)) dff(.D(D), .Q(Q), .CLK(gclk));
endmodule
EOF
```

`clk2fflogic` 把所有抽象时钟/触发器归一成 `$ff`；`chtype -map $ff __ff` 再把 `$ff` 换成用户单元 `__ff`；随后用内联 Verilog 给 `__ff` 一个实现：内部就是一个接在全局时钟 `gclk` 上的 `$dff`，并标 `(* do_flatten *)`。这样 CXXRTL 看到的就是一个「全部以 `gclk` 为采样点、结构扁平」的模型，仿真效率与正确性都更可控。这是 CXXRTL 工作流的常用技巧，不属于 sv-elab，但它是「sv-elab 产物能否继续往下走」的试金石。

**段 ⑤——导出与编译**，见 [prepare.ys_:L26-L33](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/tests/croc_boot/prepare.ys_#L26-L33)：

```
add -global_input gclk 1
hierarchy -top croc_soc
flatten
stat

bwmuxmap
write_cxxrtl -noflatten -g0 croc_soc.cc
exec -- g++ -std=c++14 -o main main.cc -I `yosys-config --datdir`/include/backends/cxxrtl/runtime -Wno-shift-count-overflow -Wno-array-bounds -O1
```

- `add -global_input gclk 1`：给顶层补一个 1 位的全局输入 `gclk`，正是 `main.cc` 里 `top.p_gclk` 驱动的那个时钟。
- `write_cxxrtl -noflatten -g0 croc_soc.cc`：生成 C++ 仿真模型 `croc_soc.cc`（`-g0` 关调试信息、`-noflatten` 保留当前层次）。`main.cc` 第 7 行 `#include "croc_soc.cc"` 直接把它文本包含进来。
- `exec -- g++ ...`：Yosys 的 `exec` 在脚本里调外部命令，用 `g++` 把 `main.cc`（含模型）编成可执行文件 `main`。`-I .../cxxrtl/runtime` 提供 CXXRTL 运行时头；`-Wno-shift-count-overflow -Wno-array-bounds` 压制 croc 生成代码里常见的告警。

`run.sh` 跑完这条脚本，`main` 就躺在 `tests/croc_boot/` 下，随后被 [run.sh:L7-L9](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/tests/croc_boot/run.sh#L7-L9) 启动。

#### 4.2.4 代码实践

**实践目标**：把 `prepare.ys_` 五段命令与「sv-elab 被验证的能力」对上号。

**操作步骤**：

1. 在 [prepare.ys_:L1-L4](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/tests/croc_boot/prepare.ys_#L1-L4) 的 `read_slang` 行上，标注每个参数属于「slang 标准」「sv-elab 专属」还是「设计侧宏」。可对照 [src/slang_frontend.cc:L89-L140](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L89-L140) 的 `addOptions`：凡在该函数里注册的是 sv-elab 专属，其余（`-F`、`-D`、`--top`、`--allow-use-before-declare`）是 slang 标准。
2. 想象把 `--keep-hierarchy` 去掉重跑：参考 [u7-l2](u7-l2-hierarchy-and-dissolve.md)，默认 `hierarchy_mode()` 为 NONE（全展平），写出你预期 `stat`（[prepare.ys_:L6](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/tests/croc_boot/prepare.ys_#L6) 与 [L29](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/tests/croc_boot/prepare.ys_#L29)）输出的模块数量会发生什么变化。
3. 在 `prepare.ys_` 的 `stat` 行（[L6](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/tests/croc_boot/prepare.ys_#L6) 与 [L29](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/tests/croc_boot/prepare.ys_#L29)）前后各看一次单元统计，对比「综合刚结束」与「flatten 之后」的 `$dff`/`__ff`/`$memrd_v2` 等单元数量，体会 ②–⑤ 段对网表的塑形。

**需要观察的现象**：`stat` 会打印模块数、单元数、线网数。开 `--keep-hierarchy` 时顶层下应能看到若干保留的子模块；去掉后应只剩一个扁平的 `croc_soc`。

**预期结果**：两种模式下 `main` 最终都应能生成且能跑通金丝雀（因为 croc 的层次展平不影响功能），但 `stat` 数字差异显著。这一步是「待本地验证」——需要完整 croc 源码与构建好的 `slang.so`。

#### 4.2.5 小练习与答案

**练习 1**：`prepare.ys_` 里 `prep -nomem`（[L7](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/tests/croc_boot/prepare.ys_#L7)）的 `-nomem` 是什么意思？为什么这里要带它？

**参考答案**：`prep` 默认会把 `$mem` 抽象存储器展开/映射成触发器与地址译码（`memory_collect`/`memory_map`）。`-nomem` 让 `prep` **不要**动存储器，保留 `$memrd_v2`/`$memwr_v2`/`$mem_v2` 抽象。croc_boot 这样做是因为 CXXRTL 能直接仿真抽象存储器（读写 `.memory_p_sram` 成员，正是 [main.cc:L81](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/tests/croc_boot/main.cc#L81) 做的），展平成寄存器反而既慢又丢失了「存储器」这个便于调试的对象。这也顺带验证了 sv-elab 的存储器推断（[u7-l1](u7-l1-memory-inference.md)）产出的抽象端口能被下游正确消费。

**练习 2**：为什么 `write_cxxrtl` 之前要 `chtype -map $ff __ff` 再用内联 Verilog 重新定义 `__ff`，而不是直接让 CXXRTL 处理 `$ff`？

**参考答案**：把所有触发器收口到一个带 `(* do_flatten *)`、内部就是 `$dff` 的 `__ff` 单元，相当于给 CXXRTL 一个「统一、扁平、单一时钟 `gclk`」的视图。直接处理 `$ff` 时，抽象时钟与多时钟域会让 CXXRTL 的采样点推导更复杂、更易出错。这是一道「仿真友好性」工程技巧，把 sv-elab 的字级网表改造成仿真器最舒服的形态。

---

### 4.3 兼容套件与 CI 集成：croc_boot 如何被调度

#### 4.3.1 概念说明

croc_boot 不是孤例，它是 sv-elab「真实 IP 验证」策略的一环。CI 里还有一条与之并列、互补的 `test-compat` job，跑的是外部的 **兼容套件（compat-suite）**——仓库 [povik/yosys-slang-compat-suite](https://github.com/povik/yosys-slang-compat-suite)，内含多个开源 RISC-V/SoC IP（black-parrot、bsc-core_tile、cv32e40p、ibex、opentitan、rsd）。它们的差别是：

- **croc_boot**：单一设计，但验证到「能启动、能执行」——**深度**优先。
- **compat-suite**：多个设计，但只验证到「能被 `read_slang` 成功综合、能被 Yosys 后续流程消化」——**广度**优先。

两者一起，构成对 sv-elab「在真实世界能用」的网状保障。理解这条策略，能帮你判断：发现一个 sv-elab 的 bug 时，是该加单元等价性测试、还是该往 compat-suite 里加一个新 IP、还是该像 croc_boot 这样搭一个端到端用例。

#### 4.3.2 核心流程

CI（`.github/workflows/build-and-test.yaml`）把测试拆成三个 job：

```
build           → 造 slang.so + 跑 ctest（单元等价性测试，见 u8-l1）
   ├── test-croc-boot   → 下载 croc 源码包 + 装 OpenOCD + 跑 run.sh  （本讲）
   └── test-compat      → clone compat-suite，逐 IP 跑 <ip>.tcl        （广度）
```

`test-croc-boot` 与 `test-compat` 都 `needs: [build]`，即复用 `build` 产出的 `slang.so` 制品（`upload-artifact`/`download-artifact`），避免重复编译。它们只跑在矩阵的「首尾」Yosys 版本上（`filtered_versions`），而非全部版本，以控制 CI 时长——真实 IP 综合很慢，跑全矩阵代价过高。

#### 4.3.3 源码精读

**`test-croc-boot` job 头与环境变量**，见 [.github/workflows/build-and-test.yaml:L115-L127](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/.github/workflows/build-and-test.yaml#L115-L127)：

```yaml
test-croc-boot:
  runs-on: ubuntu-24.04
  needs: [build, define-matrix]
  strategy:
    matrix:
      yosys_version: ${{ fromJSON(needs.define-matrix.outputs.filtered_versions) }}
    fail-fast: false
  env:
    # bender -d . script flist-plus --relative-path > croc.f
    # /patch absolute paths in croc.f/
    # tar --exclude='.git' ... -cvf ../croc-checkout-250109-846ce38.tar .
    CROC_CHECKOUT: 'croc-checkout-250120-846ce380.tar.gz'
    CROC_CHECKOUT_HASH: '6e7b193fe51c3fe3dad00a675c729b9e12d1fc3c72e8d06c580d92fdeba64403  croc-checkout-250120-846ce380.tar.gz'
```

几个要点：

- `needs: [build, ...]` + `matrix.yosys_version: filtered_versions`：只在首尾 Yosys 版本上跑，复用 build 制品。
- 注释三行是「如何造 croc 源码包」的备忘：用 bender 生成 `croc.f`、把里面的绝对路径改成相对、再打包成 tar。这意味着 croc 源码**不在本仓库**（`tests/third_party/croc/` 只有一个 `.gitkeep`，见 4.3.4），而是一个固定哈希的、托管在 `cutebit.org` 的预打包 tar。
- `CROC_CHECKOUT_HASH` 是 `sha256sum --check` 用的完整校验行，确保下载内容未被篡改/变动。

**取 croc 源码 + 装 OpenOCD + 跑测试**，见 [build-and-test.yaml:L153-L177](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/.github/workflows/build-and-test.yaml#L153-L177)：

```yaml
- name: Cache Croc checkout
  id: cache-croc
  uses: actions/cache@v6
  with:
    path: tests/third_party/croc
    key: ${{ env.CROC_CHECKOUT }}
- if: ${{ steps.cache-yosys.outputs.cache-hit != 'true' }}
  name: Unpack Croc
  run: |
    wget https://cutebit.org/$CROC_CHECKOUT
    echo "$CROC_CHECKOUT_HASH" | sha256sum --check
    tar -xvf $CROC_CHECKOUT -C tests/third_party/croc/
- name: Install OpenOCD
  run: |
    wget https://github.com/xpack-dev-tools/openocd-xpack/releases/download/v0.12.0-4/xpack-openocd-0.12.0-4-linux-x64.tar.gz
    sudo tar xvf xpack-openocd-0.12.0-4-linux-x64.tar.gz -C /opt
    echo "/opt/xpack-openocd-0.12.0-4/bin" >> $GITHUB_PATH
- name: Run tests
  run: |
    tests/croc_boot/run.sh
```

- croc 源码按 `CROC_CHECKOUT` 文件名做缓存键（命中就跳过下载）；解压到 `tests/third_party/croc/`，正是 `prepare.ys_` 里 `-F ../third_party/croc/croc.f` 指向的位置。缓存键是 tar 文件名而非哈希，但下载后立刻 `sha256sum --check` 兜底完整性。
- OpenOCD 用 xpack 预编译版（v0.12.0-4），解压后把 `bin` 加进 `GITHUB_PATH`，使 `run.sh` 里 `openocd` 可直接调用。
- 最后一步就是 4.1.3 讲的 `tests/croc_boot/run.sh`。

**并列的 `test-compat` job**，见 [build-and-test.yaml:L178-L184](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/.github/workflows/build-and-test.yaml#L178-L184) 与 [L208-L221](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/.github/workflows/build-and-test.yaml#L208-L221)：

```yaml
test-compat:
  needs: [build, define-matrix]
  strategy:
    matrix:
      yosys_version: ${{ fromJSON(needs.define-matrix.outputs.filtered_versions) }}
      ip: [black-parrot, bsc-core_tile, cv32e40p, ibex, opentitan, rsd]
...
- name: Pull IP sources
  run: |
    git clone https://github.com/povik/yosys-slang-compat-suite/ compat-suite --depth 1
    cd compat-suite
    git submodule init ${{ matrix.ip }}
    git submodule update --init --recursive --depth 1 ${{ matrix.ip }}
- name: Run script
  run: |
    cd compat-suite
    yosys -m ../build/slang.so ${{ matrix.ip }}.tcl
```

- 每个 IP 是矩阵的一个维度，6 个 IP × 首尾 2 个 Yosys 版本，各自独立跑、`fail-fast: false`（一个 IP 挂不连累其他）。
- 每个 IP 是 compat-suite 仓库的一个 git submodule，按需浅克隆（`--depth 1`）。
- 跑法极简：`yosys -m slang.so <ip>.tcl`。这里的 `.tcl` 通常只做「`read_slang` 整个设计 + 一些基本综合 + 断言无诊断/无崩溃」，**不**做端到端启动——所以它快得多，能一次覆盖 6 个设计。opentitan 因其特殊构建链还多装了 `libxml2-dev` 等（[L214-L217](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/.github/workflows/build-and-test.yaml#L214-L217)）。

#### 4.3.4 代码实践

**实践目标**：确认本仓库里的 croc 是「空占位」，真正内容由 CI 外部注入；并对比 croc_boot 与 compat-suite 两条线的覆盖差异。

**操作步骤**：

1. 列出 `tests/third_party/croc/`，确认它只有 `.gitkeep`（本仓库不跟踪 croc 源码）。这与 `third_party/slang`、`third_party/fmt` 是 git submodule（见仓库根 `.gitmodules`）形成对照——croc 既非 submodule 也非跟踪文件，而是 CI 时下载的 tar。
2. 在 [build-and-test.yaml:L126](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/.github/workflows/build-and-test.yaml#L126) 找到 `CROC_CHECKOUT` 的 tar 文件名，在 [L159-L166](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/.github/workflows/build-and-test.yaml#L159-L166) 找到它被下载、校验、解压到 `tests/third_party/croc/` 的全过程，画出「CI 注入 croc 源码」的链路。
3. 对比 [L175-L177](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/.github/workflows/build-and-test.yaml#L175-L177)（croc_boot 跑 `run.sh`）与 [L218-L221](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/.github/workflows/build-and-test.yaml#L218-L221)（compat 跑 `<ip>.tcl`），用一句话分别概括二者的验证「深度」。

**需要观察的现象**：本地直接 `cd tests/croc_boot && ../run.sh` 会因为 `tests/third_party/croc/` 为空、`croc.f` 不存在而失败——这正是「源码需由 CI 注入」的证据。

**预期结果**：你会得出——croc_boot 验证「1 个设计，综合 + 仿真 + 启动 + 金丝雀」；compat-suite 验证「6 个设计，综合可消化、无崩溃」。前者深，后者广。

> 本实践为源码阅读型，结论可由已有文件直接得出；若要在本地真跑 croc_boot，需自行下载同一 `CROC_CHECKOUT` tar、校验哈希、装 OpenOCD，属于「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `test-croc-boot` 和 `test-compat` 都只在 `filtered_versions`（首尾 Yosys 版本）上跑，而 `build` job 跑 `all_versions`？

**参考答案**：真实 IP 综合很慢，且 sv-elab 的正确性主要由「源码→网表」的翻译逻辑决定，与 Yosys 版本的耦合相对弱。全矩阵跑全部 IP 成本太高、收益低；取首尾两个版本即可覆盖 Yosys API 的「最旧支持版」与「最新版」两个端点，兼顾覆盖与 CI 时长。单元等价性测试（`ctest`）轻量，才在 build job 里跑全矩阵。

**练习 2**：如果我想给 sv-elab 新增对某个 SV 构造的支持，应该用哪种测试来兜底？

**参考答案**：分层兜底——先加一个最小单元等价性测试（[u8-l1](u8-l1-test-infrastructure.md)）锁定该构造的精确行为；若该构造在大型设计里常见，可在 compat-suite 里确认现有 IP 综合不退化；若涉及完整数据通路（如新存储器/时序模式），可考虑仿 croc_boot 搭一个端到端启动用例。三者分别对应「精确点」「广度面」「端到端深度」。

---

## 5. 综合实践

**任务**：把本讲三个模块串起来，画出 croc_boot 的「从一行 git push 到金丝雀 == 7」的完整因果链，并标注每一步若失败分别暴露 sv-elab 的哪类问题。

建议按下列提纲完成（纯源码阅读即可，无需运行）：

1. **触发**：一次 push 进 CI，`build` job 产出 `slang.so` 并上传为制品（参考 [build-and-test.yaml:L94-L111](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/.github/workflows/build-and-test.yaml#L94-L111)）。
2. **取材**：`test-croc-boot` 下载制品、下载并校验 croc tar、装 OpenOCD（[L141-L177](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/.github/workflows/build-and-test.yaml#L141-L177)）。
3. **综合**：`run.sh` 调 `prepare.ys_`，`read_slang` 把 croc 翻成 RTLIL（[prepare.ys_:L1-L4](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/tests/croc_boot/prepare.ys_#L1-L4)）。**这一步失败 → sv-elab 的解析/精化/翻译有 bug，或漏报了诊断。**
4. **后端塑形与导出**：`prep`/`clk2fflogic`/`__ff`/`write_cxxrtl`/`g++`（[prepare.ys_:L5-L33](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/tests/croc_boot/prepare.ys_#L5-L33)）。**这一步失败 → sv-elab 产出的网表不合法，下游 Yosys 无法消化。**
5. **仿真上电**：`main` 实例化模型、复位、播种 idle（[main.cc:L54-L83](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/tests/croc_boot/main.cc#L54-L83)）。
6. **调试器介入**：OpenOCD 经 remote_bitbang halt、灌 `helloworld.ihex`、置 PC、resume（[debugger.tcl_:L11-L15](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/tests/croc_boot/debugger.tcl_#L11-L15)）。**这一步失败 → 可能是 JTAG/DMI 数据通路翻译错（属 sv-elab），也可能是 testbench/OpenOCD 协议问题（非 sv-elab）。**
7. **验收**：CPU 执行固件写出 7，OpenOCD halt 后 `read_memory 0x03000008` 校验（[debugger.tcl_:L15-L19](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/tests/croc_boot/debugger.tcl_#L15-L19)）。**金丝雀不符 → CPU 数据通路/控制通路在某个 SV 构造上被 sv-elab 翻译错，但网表本身「合法可综合」——这正是单元等价性测试最难抓的那类功能性 bug。**

把这张图整理成你的笔记，并对照 [u8-l1](u8-l1-test-infrastructure.md) 的等价性测试，写一段话说明：为什么「金丝雀 == 7」能抓到某些等价性测试抓不到的 bug（提示：等价性测试需要参考网表，而 croc_boot 不需要）。

## 6. 本讲小结

- croc_boot 是一条**端到端功能验证**流水线：用真实 RISC-V SoC（croc）检验 sv-elab 综合出的网表「能不能跑」，而非「等不等于某参考网表」。
- 全链路由 `run.sh` 编排：`prepare.ys_` 做 `read_slang` 综合 + `write_cxxrtl` 导出 + `g++` 编译，产物 `main` 是「CXXRTL 仿真模型 + 虚拟 JTAG 服务器」；随后 OpenOCD 经 remote_bitbang 调试它。
- 验收靠**金丝雀**：固件执行后在 `0x03000008` 写 7，OpenOCD `read_memory` 校验；不符则 `shutdown error`，`set -e` 让 CI 报红。
- `prepare.ys_` 的 `read_slang` 一行集中体现了 sv-elab 的实战用法：`--top`、`-F` 文件列表、`-D SYNTHESIS` 宏、`--allow-use-before-declare`（slang 标准）与 `--keep-hierarchy`（sv-elab 专属、实验性）。
- croc_boot 不在 `ALL_TESTS`/`ctest` 里，而是独立的 `test-croc-boot` CI job，源码由固定哈希的 tar 在 CI 时注入（`tests/third_party/croc/` 平时只有 `.gitkeep`）。
- 与之并列的 `test-compat` job 跑外部兼容套件（6 个开源 IP），走「广度」路线；croc_boot 走「深度」路线，二者互补。

## 7. 下一步学习建议

- 想看「轻量、精确、可回归」的测试范式，回到 [u8-l1 测试体系](u8-l1-test-infrastructure.md)，对照理解单元等价性测试与 croc_boot 的取舍。
- 想了解构建侧（`slang.so` 怎么造出来、为什么有插件/静态库两种产物），接着读 [u8-l3 构建系统深入](u8-l3-build-systems.md)。
- 想动手扩展 sv-elab 的支持面，读 [u8-l4 扩展开发](u8-l4-extending-and-contributing.md)，并可考虑给 compat-suite 贡献一个新 IP 作为广度回归。
- 若你对本讲里 `--keep-hierarchy`、存储器抽象、层次展平的细节感兴趣，分别参看 [u7-l2 层次处理](u7-l2-hierarchy-and-dissolve.md) 与 [u7-l1 存储器推断](u7-l1-memory-inference.md)。
