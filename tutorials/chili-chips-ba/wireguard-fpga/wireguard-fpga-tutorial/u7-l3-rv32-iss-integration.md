# rv32 RISC-V ISS 与软件集成

## 1. 本讲目标

本讲承接 [u7-l2 VProc 虚拟处理器协同仿真](u7-l2-vproc-cosim.md)。在 u7-l2 中，`VUserMain0` 是一段**手写**的 C++ 测试代码，直接调用 `write`/`read`/`tick` 驱动 `soc_if` 总线。本讲把 `VUserMain0` 升级成另一种角色：**指令集模拟器（ISS）的宿主程序**——它不再手写每一条总线事务，而是创建一个 `rv32` RISC-V ISS 对象，加载一份**真实的 RISC-V 二进制**，让 ISS 去解释执行，由 ISS 在每次访存时回调我们的代码决定该走哪条路。

学完本讲你应当能够：

- 说清「ISS 替代 RTL CPU 为什么能加速仿真」，以及 `rv32` 如何嵌套在 VProc 之上。
- 读懂 `-x`/`-X` 选项如何划定**外部访问区**，把落在区内的访存（如 CSR）送上 HDL 总线、区外的访存（如 IMEM/DMEM）交给 `mem_model` 直接处理。
- 读懂 **timing model** 的 7 类指令周期与 7 个预置核模型，并用 `-V` 选项在 `vusermain.cfg` 中切换。
- 独立配置 `vusermain.cfg` 并跑通一次 `BUILD=ISS` 仿真，观察输出。

## 2. 前置知识

- **指令集模拟器（Instruction Set Simulator, ISS）**：一段用软件解释执行某指令集（ISA）机器码的程序。它不需要真实的 CPU 硬件，只需主机的一个进程。`rv32` 是 Wyvern Semi 开源的 RISC-V（RV32）ISS，本项目以**预编译静态库** `librv32lnx.a` 的形式提供。
- **主机编译 vs. 目标编译**：在 u7-l2 里，VProc 用户代码是**主机原生**编译（host gcc）的；本讲里跑在 ISS 上的 RISC-V 应用则是用 **RISC-V 交叉工具链**编译的（`riscv64-unknown-elf-`）。两份二进制运行在两个完全不同的「机器」上。
- **ELF vs. raw binary**：ISS 既能 `read_elf` 解析带段头的 ELF，也能用 `-B` 直接装载裸二进制（`read_binary`），后者配合 `-L` 指定装载地址。
- **稀疏内存模型 `mem_model`**：在 u7-l1/u7-l2 出现过——一个用 C 实现的、按需分配页面的 64 位地址空间稀疏内存，VProc 用户代码与 HDL 侧的 `mem_model` 组件共享同一空间。本讲里它是「不走总线」时存取 IMEM/DMEM 的后备存储。
- **回调（callback）**：ISS 把「我现在要访存」这件事以函数回调的形式交给宿主代码处理，宿主代码返回该次访问消耗的等待周期数。这是 ISS 与外部世界（HDL 总线 / 内存模型 / 外设模型）唯一的交互接口。
- **周期精确（cycle-accurate）**：RTL 仿真每个时钟沿都算，时序天然精确但慢；ISS 不逐拍推进，靠 **timing model** 给每类指令赋一个近似周期数来恢复「接近真实」的时序，换取速度。

## 3. 本讲源码地图

本讲涉及的源码几乎全部集中在 `4.sim/models/rv32/` 下，核心是 `usercode/` 目录里一组配套的 C++ 文件：

| 文件 | 作用 |
| --- | --- |
| [4.sim/models/rv32/usercode/VUserMain0.cpp](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/4.sim/models/rv32/usercode/VUserMain0.cpp) | node 0 的主入口：创建 ISS、注册回调、装载并运行二进制；定义访存回调 `ext_mem_access` |
| [4.sim/models/rv32/usercode/vuserutils.cpp](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/4.sim/models/rv32/usercode/vuserutils.cpp) | `parseArgs`：从 `vusermain.cfg` 解析 `-x`/`-X`/`-V` 等所有选项；以及寄存器/CSR 转储 |
| [4.sim/models/rv32/usercode/rv32_timing_config.h](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/4.sim/models/rv32/usercode/rv32_timing_config.h) | 7 个核的 timing model 定义与 `update_timing` 方法 |
| [4.sim/models/rv32/usercode/mem_vproc_api.cpp](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/4.sim/models/rv32/usercode/mem_vproc_api.cpp) | 访存分发：按 `access_sim` 在 VProc API（HDL 总线）与 `mem_model` API 之间二选一 |
| [4.sim/models/rv32/usercode/VUserMain0.h](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/4.sim/models/rv32/usercode/VUserMain0.h) | 配置结构 `vusermain_cfg_t` 与外部访问区默认值 |
| [4.sim/models/rv32/usercode/rv32_cache.cpp](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/4.sim/models/rv32/usercode/rv32_cache.cpp) | 指令缓存命中/缺失模型（只建模地址，不缓存数据） |
| [4.sim/vusermain.cfg](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/4.sim/vusermain.cfg) | 运行时配置文件，替代命令行参数 |
| [4.sim/models/rv32/riscvtest/main.s](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/4.sim/models/rv32/riscvtest/main.s) | 自包含的 RISC-V 汇编示例（仓库附预编译 `main.bin`） |
| [4.sim/README.md](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/4.sim/README.md) / [4.sim/models/rv32/README.md](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/4.sim/models/rv32/README.md) | 使用与集成说明 |

## 4. 核心概念与源码讲解

### 4.1 rv32 ISS 集成：在 VProc 上运行真实 RISC-V 二进制

#### 4.1.1 概念说明

回顾 u7-l2：VProc 是一个 HDL 空壳，真正的逻辑由主机侧 C++ 程序经 DPI-C 下发，二者以每次 `write`/`read`/`tick` 为同步点 lock-step 推进。当时 `VUserMain0` 里写的是测试代码本身。

本讲换一种用法：**让 `rv32` ISS 成为「跑在 VProc 上的那个程序」**。嵌套关系是：

```
真实 RISC-V 应用二进制 (main.bin / main.elf)
        ↓ 由 ISS 解释执行
rv32 ISS (主机 C++ 进程, 来自 librv32lnx.a)
        ↓ 经 ext_mem_access 回调
VProc DPI-C (write/read/tick)
        ↓
HDL 测试台 (soc_if 总线 / mem_model)
```

为什么要嵌一层 ISS？**为了仿真加速**。u7-l1 讲过测试台支持三种 `soc_cpu`：picoRV32（RTL，逐拍精确但慢）、IBEX、EDUBOS5。RTL CPU 在 Verilator 里每个时钟沿都要算一堆组合逻辑，长跑很慢；ISS 在主机进程里直接解释指令，快得多。代价是丢掉逐拍精确性——而本项目用 **timing model**（见 4.3）给每类指令赋近似周期数，把时序「补」回来，足以做性能估算与时钟域对齐。

`4.sim/README.md` 把 VProc 上的软件归纳为三种用例（对应其软件分层图）：

1. **native test code**：直接调 VProc/mem_model API（u7-l2 的写法）。
2. **native app**：连同 HAL 一起主机原生编译，HAL 内部被改写为调 VProc API。
3. **RISC-V app**：用 RISC-V 工具链编译，跑在 `rv32` ISS 上——**本讲的用例**。

对前两种，`VUserMain0` 就是应用/测试本体；对第三种，`VUserMain0` 变成 **ISS 集成胶水**，真正的应用是它加载的那份二进制。

#### 4.1.2 核心流程

`rv32/README.md` 给出 `VUserMain0` 的程序流程，可概括为六步：

```
1. 解析 vusermain.cfg → 得到 ISS 配置 cfg 与集成配置 vcfg
2. 创建 rv32 ISS 对象 pCpu = new rv32(...)
3. pre_run_setup：注册访存回调、中断回调、配 timing、按需建 icache
4. 分支：
   - GDB 模式(-g)：可选加载 ELF，进入 rv32gdb_process_gdb 接受远程 gdb
   - 普通模式：read_elf 或 read_binary 加载程序，pCpu->run(cfg) 跑到退出
5. post_run_actions：按需转储寄存器/CSR，若指定了指令条数(-n)则算 MIPS
6. SLEEP_FOREVER：让仿真继续，不立即结束
```

注意一个对迭代效率很重要的点（`4.sim/README.md` 明确说明）：**改 RISC-V 源码或换二进制、改 `vusermain.cfg`，都不需要重建仿真**——除非 HDL 变了。因为「用户应用」是仿真启动后由 ISS 在运行时加载的，并不焊进可执行文件。这与 u1-l4 讲的综合期焊死 `imem.INIT.vh` 形成对照：仿真里 IMEM 由 `mem_model`/ISS 动态填充，迭代极快。

#### 4.1.3 源码精读

主入口 `VUserMain0()` 的整体结构（解析 → 建对象 → 预设 → 跑 → 收尾 → 长睡）：

[4.sim/models/rv32/usercode/VUserMain0.cpp:L354-L461](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/4.sim/models/rv32/usercode/VUserMain0.cpp#L354-L461) — node 0 主入口。关键行：`parseArgs(0, NULL, ...)` 因为拿不到 Verilator 命令行，所以传 `0`/`NULL`，函数转而去读 `vusermain.cfg`；`pCpu = new rv32(...)` 创建 ISS；最后 `SLEEP_FOREVER` 让仿真不随程序退出而结束。

`parseArgs` 之所以能拿到配置，是因为 `VUserMain0` 没有命令行参数可拿，于是约定从工作目录的 `vusermain.cfg` 读取一行 `vusermain0 [options]`：

[4.sim/models/rv32/usercode/vuserutils.cpp:L33-L93](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/4.sim/models/rv32/usercode/vuserutils.cpp#L33-L93) — 打开 `CFGFILENAME`（即 `vusermain.cfg`），按节点号拼出 `vusermain0`，找到匹配行后用 `strtok` 切成 argv 数组，再交给 `getopt` 统一解析。

预设阶段注册三类回调并配置 timing/cache：

[4.sim/models/rv32/usercode/VUserMain0.cpp:L61-L115](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/4.sim/models/rv32/usercode/VUserMain0.cpp#L61-L115) — `pre_run_setup`：`register_ext_mem_callback(ext_mem_access)` 把我们的访存回调挂进 ISS（4.2 精读）；`register_int_callback` / `VRegIrq` 挂中断回调（当前测试台未用中断，留作扩展）；按 `vcfg.enable_icache` 决定是否建 `rv32_cache`；最后 `rv32_time_cfg.update_timing(pCpu, vcfg.riscv_core)` 注入选定的 timing model（4.3 精读）。

程序加载有两条路径——ELF 或裸二进制：

[4.sim/models/rv32/usercode/VUserMain0.cpp:L290-L330](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/4.sim/models/rv32/usercode/VUserMain0.cpp#L290-L330) — `read_binary`：按小端每 4 字节拼成 word，用 `pCpu->write_mem(..., MEM_WR_ACCESS_INSTR, ...)` 写入 ISS 内存。注意它走的是 ISS 自身的 `write_mem`（直接写 ISS 内部存储），**不**经 `ext_mem_access` 回调，避免装载阶段触发总线事务。ELF 路径则用库自带的 `pCpu->read_elf`。`-B` 选裸二进制、`-L` 选装载地址。

退出后收尾——转储寄存器与 CSR、按需计算主机侧 IPS：

[4.sim/models/rv32/usercode/VUserMain0.cpp:L122-L157](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/4.sim/models/rv32/usercode/VUserMain0.cpp#L122-L157) — `post_run_actions`：`-R` 转储 x0–x31、`-c` 转储 CSR；若用 `-n` 指定了指令条数，则用 `gettimeofday` 量主机墙上时间算 IPS（注意这是**仿真速度**，不是目标核的 MIPS）。

#### 4.1.4 代码实践

**实践目标**：跑通自包含的 RISC-V 测试，建立「ISS 在 VProc 上跑真二进制」的直觉。

**操作步骤**：

1. 打开 [4.sim/vusermain.cfg](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/4.sim/vusermain.cfg)。默认激活的是第 10 行的 `app_tests` 配置（指向 `../3.build/sw_build/main.bin`，需要先构建软件）。把第 10 行注释掉，启用第 6 行的 basic test（指向自包含的 `riscvtest/main.bin`）：
   ```
   vusermain0 -V PICORV32 -x 0x10000000 -X 0x20000000 -rEHRca -t ./models/rv32/riscvtest/main.bin
   ```
2. 在 `4.sim/` 下运行：
   ```
   make -f MakefileVProc.mk BUILD=ISS run
   ```
   `BUILD=ISS` 会让 Makefile 忽略默认的 `USER_C`，转而编译 `models/rv32/usercode/` 下的 ISS 集成代码并链接 `librv32lnx.a`（见 [4.sim/MakefileVProc.mk:L73-L92](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/4.sim/MakefileVProc.mk#L73-L92)）。

**需要观察的现象**：

- 开头打印 `Wyvern Semiconductors / rv32 RISC-V ISS (on VProc)` 横幅。
- 因 `-r`，逐条打印反汇编（32 位与 16 位压缩指令混排，压缩指令十六进制后带 `'`）。
- 因 `-E`，遇到 `ebreak` 停机；因 `-R`/`-c`，末尾打印 `Register state:` 与 `CSR state:`。
- 最后 `Exited running ./models/rv32/riscvtest/main.bin`。

**预期结果**：参考 [4.sim/README.md:L312-L368](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/4.sim/README.md#L312-L368) 的样例输出——测试把 `0x900dc0de` 写到 `0x10001000` 再读回比较，`a0=0`（pass）、`a7=93`、`ebreak` 退出；CSR 转储里 `minstret = 0xb`（11 条指令）。具体周期数 `mcycle` 随 timing model 变化，**待本地验证**。

> 说明：该实践依赖 Verilator/VProc/riscv 工具链就绪。若环境不具备，转为下面的源码阅读型实践：对照 [4.1.2](#412-核心流程) 的六步流程，在 [VUserMain0.cpp:L354-L461](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/4.sim/models/rv32/usercode/VUserMain0.cpp#L354-L461) 中逐行标注每步对应的代码行。

#### 4.1.5 小练习与答案

**练习 1**：为什么「换一份 RISC-V 二进制」不用重新 `make`，而「改 HDL」必须重新 `make`？

**参考答案**：RISC-V 二进制是仿真启动后由 ISS 在运行时经 `read_elf`/`read_binary` 装载到（由 `mem_model` 提供的）内存里的，不参与 Verilator 编译；HDL 则是 Verilator 编译进可执行文件的一部分，改了必须重编。

**练习 2**：`VUserMain0` 末尾的 `SLEEP_FOREVER` 如果删掉，仿真会怎样？

**参考答案**：`VUserMain0` 一旦返回，node 0 的用户线程就结束了，不再有 `write`/`read`/`tick` 推进该节点的仿真活动；`SLEEP_FOREVER`（本质是 `while(1) VTick(...)`）让节点持续「睡觉」但不退出，从而允许其它节点（如 node 1–4 的以太网 VIP）和测试台继续运行到超时。

---

### 4.2 外部访问区划分：`-x`/`-X` 与 `ext_mem_access` 回调

#### 4.2.1 概念说明

ISS 每执行一条 load/store/取指都要「访存一次」。这次访存该走哪里？本设计给出**二选一**：

- **HDL 总线**：经 VProc 的 `VWriteBE`/`VRead` 打一次真实的 `soc_if` 事务，DUT 的 `soc_fabric` 真的会译码、真的会读写 DMEM/CSR。**慢但真**——能驱动真实外设与数据面。
- **`mem_model` 直接访问**：经 `WriteRamWord`/`ReadRamWord` 等直接读写 C 稀疏内存，**不产生任何总线事务**。**快但假**——只是个后备数组。

由谁决定走哪条？**由地址落在哪个区间决定**。`-x <base>` 与 `-X <top>` 划定半开区间 `[base, top)`，落在区间内的走 HDL，区间外的走 `mem_model`。两个选项的默认值都是 `0xFFFFFFFF`（见 [VUserMain0.h:L59-L65](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/4.sim/models/rv32/usercode/VUserMain0.h#L59-L65)），此时 `base >= top`，区间为空，**所有访存都走 `mem_model`**——即默认「全不走总线」。

为什么这样设计？回到 [u6-l1](u6-l1-sw-arch-boot-memmap.md) 的内存映射：IMEM `0x00000000`、DMEM `0x10000000`、CSR `0x20000000`。

- **IMEM/DMEM 用 `mem_model` 就够了**：它们只是存储，`mem_model` 既快又能与 HDL 侧的 `mem_model` 组件共享（比如 UART 在线烧写 IMEM 写口连着一个 `mem_model`，见 u7-l1）。让它们走总线纯属浪费。
- **CSR 必须走 HDL**：CSR 是 MMIO 外设，是软硬件唯一桥梁（Unit 3）。只有经真实 `soc_if` 事务，才能驱动 `cpu_fifo`、改路由表、触发 FCR 握手——这些是数据面真正在听的信号。

所以典型配置是：**把 CSR 区（`0x20000000` 起）划进外部访问区，IMEM/DMEM 留给 `mem_model`**。这正是本讲实践任务的目标。

> 一个易混点：`ext_mem_access` 里还有两个**特殊地址**先于通用 switch 处理——`sw_irq_addr = 0xafffffff`（触发软件中断）和 `uart0_base_addr = 0x80000000`（ISS 自带的简易 UART 模型，给被测程序的「控制台输出」用）。它们不属于 WG 的 CSR 空间（`0x20000000`），是 ISS 集成自带的「小外设」，别和 WG 硬件 UART 混淆。

#### 4.2.2 核心流程

`ext_mem_access` 是 ISS 注册的访存回调，每次访存都被调用。其流程（源自 [rv32/README.md:L57-L91](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/4.sim/models/rv32/README.md#L57-L91)）：

```
输入: addr, data(in/out), type(读字节/半字/字/取指/写...), time(ISS 累计周期)

— 同步 —
1. access_sim = (base <= addr < top) ? true : false
2. 计算 ISS 自上次调用以来的周期差 cycle_diff
3. 若 access_sim:               VTick(cycle_diff)  // 每次总线访问都同步
   否则(走 mem_model): 仅当是取指且 cycle_diff >= 1000 才 VTick  // 省交互

— 特殊地址 —
4. 若 addr == sw_irq_addr 且写: 更新软中断状态
5. 否则若 addr 落在 ISS UART 区: 交 uart_reg_access 处理

— 通用访存 —
6. switch(type):
     读字节/半字/字 → read_* (按 access_sim 选 VProc 或 mem_model)
     取指           → read_instr + (可选) icache 命中/缺失加等待拍
     写字节/半字/字 → write_*
7. 返回该次访问的等待周期数 (写=0, 读=1, icache miss 另加)
```

第 3 步的「省交互」很关键：当一段代码长时间只在 `mem_model` 里跑（不碰总线），ISS 周期与仿真周期会越拉越远（skew）；只有当 skew 达到 `max_sync_diff = 1000` 拍、且恰好在取指时，才插一次 `VTick` 把仿真追上来。这把「无总线访问期间」的 DPI-C 往返压到最低，是 ISS 比 RTL 快的重要原因之一。

#### 4.2.3 源码精读

回调入口与 `access_sim` 判定——这一行就是「走总线还是走内存模型」的总开关：

[4.sim/models/rv32/usercode/VUserMain0.cpp:L163-L199](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/4.sim/models/rv32/usercode/VUserMain0.cpp#L163-L199) — `ext_mem_access`。`access_sim = addr >= vcfg.ext_access_base_addr && addr < vcfg.ext_access_top_addr`（L175）；随后按 `access_sim` 决定同步策略（L186-L199）。

通用访存的 switch——按 `type` 分派到 `read_byte`/`read_word`/`write_word` 等，每项都把 `access_sim` 透传下去：

[4.sim/models/rv32/usercode/VUserMain0.cpp:L214-L264](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/4.sim/models/rv32/usercode/VUserMain0.cpp#L214-L264) — 注意取指分支（`MEM_RD_ACCESS_INSTR`）里若开了 icache，会调 `icache->rv32_cache_access(addr)`，缺失时叠加 `penalty_slow_mem * line_width/4` 个等待拍。

真正的「二选一」发生在 `mem_vproc_api.cpp`，以 `write_word` / `read_word` 为例：

[4.sim/models/rv32/usercode/mem_vproc_api.cpp:L17-L54](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/4.sim/models/rv32/usercode/mem_vproc_api.cpp#L17-L54) — `access_sim` 为真时 `VWriteBE`/`VRead`（字对齐到 `& ~0x3`，带字节使能），打真实总线事务；为假时 `WriteRamWord`/`ReadRamWord` 走 `mem_model` 直接访问。字节/半字版本还做了相应的移位与字节使能计算。函数原型默认 `access_sim = false`（[mem_vproc_api.h:L41-L47](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/4.sim/models/rv32/usercode/mem_vproc_api.h#L41-L47)），即默认走内存模型。

`-x`/`-X` 在 `parseArgs` 中写入配置结构：

[4.sim/models/rv32/usercode/vuserutils.cpp:L162-L167](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/4.sim/models/rv32/usercode/vuserutils.cpp#L162-L167) — `'x'` 写 `vcfg.ext_access_base_addr`，`'X'` 写 `vcfg.ext_access_top_addr`。这两个字段就喂给上面 L175 的判定。完整选项表（含 `-x`/`-X` 的默认值与含义）见 [4.sim/README.md:L181-L216](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/4.sim/README.md#L181-L216)。

#### 4.2.4 代码实践（本讲主实践）

**实践目标**：亲手用 `-x`/`-X` 改变一次访存的路由，体会「CSR 走 HDL、其余走 mem_model」的分工，并跑通 `BUILD=ISS`。

**操作步骤**：

1. 编辑 [4.sim/vusermain.cfg](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/4.sim/vusermain.cfg)，先确保只有**一行**未注释。先做「配置 A：DMEM 走 HDL」（README 文档化的 basic test）：
   ```
   vusermain0 -x 0x10000000 -X 0x20000000 -rEHRca -t ./models/rv32/riscvtest/main.bin
   ```
   这里 `[0x10000000, 0x20000000)` 覆盖 DMEM 区。`main.bin` 会访问 `0x10001000`（见 [main.s:L23-L28](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/4.sim/models/rv32/riscvtest/main.s#L23-L28)），落在区内 → 走 HDL 总线。
2. 跑：`make -f MakefileVProc.mk BUILD=ISS run`，记录是否正常退出、`Exited running` 是否出现。
3. 改成「配置 B：只把 CSR 区路由到 HDL，DMEM 交给 mem_model」：
   ```
   vusermain0 -x 0x20000000 -X 0x20010000 -rEHRca -t ./models/rv32/riscvtest/main.bin
   ```
   现在外部区是 `[0x20000000, 0x20010000)`（覆盖 WG 的整个 CSR 空间，见 [u3-l1](u3-l1-systemrdl-spec.md)）。`0x10001000` 不再落在区内 → 走 `mem_model`。
4. 再跑一次，对比两次。

**需要观察的现象**：

- 两次测试都应 `pass`（`a0=0`、`a7=93`、`ebreak` 退出），因为无论 DMEM 访问走 HDL DMEM 还是 `mem_model`，都能正确存取 `0x900dc0de`，功能等价。
- 区别在**路径与速度**：配置 A 下 `sw x2,0(x4)` / `lw x5,0(x4)` 针对 `0x10001000` 的访问经 `soc_if` 总线打到真实 HDL DMEM，每拍可见于波形；配置 B 下同一访问由 `mem_model` 直接返回，不产生总线事务、仿真更快。

**预期结果**：两次都 pass；配置 B 因总线事务更少而墙钟更快。具体加速比与波形细节**待本地验证**。

> 若想真正「看见」CSR 总线事务，需要换成会经 HAL 访问 CSR 的程序（如 `../3.build/sw_build/main.bin`），并把外部区设为覆盖 `0x20000000`。仓库默认的 `app_tests` 配置（`-x 0x10000000 -X 0x3FFFFFFF`）正是把 DMEM+CSR 一并送上总线、配合整机固件使用的写法。

#### 4.2.5 小练习与答案

**练习 1**：若把 `-x`/`-X` 都保持默认（`0xFFFFFFFF`），ISS 跑一个会读写 CSR 的程序，会发生什么？

**参考答案**：外部区为空（`base >= top`），所有访存都走 `mem_model`。CSR 写只是改了 C 内存里对应地址的字节，**不会**经 `soc_if` 打到 HDL，DUT 的 CSR 寄存器毫无反应，外设/数据面不会被驱动——程序「自以为」配了寄存器，硬件却没收到。

**练习 2**：`ext_mem_access` 里写访问返回的等待周期是 0、读访问是 1（见 L165-L167），这传达了什么时序假设？

**参考答案**：假设写是「发射即忘」、当拍完成（0 等待），读则需要一拍数据返回延迟（1 等待）。这是对 `soc_if` 这类 vld/rdy 总线的简化建模；真实硬件里未应答的访问会等更久，但 ISS 用这个固定小数把时序近似出来即可。

---

### 4.3 timing model 配置：模拟不同 CPU 核的周期

#### 4.3.1 概念说明

ISS 默认并不周期精确——它一条一条地解释指令，不逐拍推进仿真时钟。但仿真里常常需要「接近真实」的时序：评估一段代码跑了多久、让 ISS 的周期计数与 80 MHz 控制面时钟域对得上、或者对比不同 CPU 核的吞吐。

**timing model** 就是这个「补时序」的机制。它把指令分成 **7 类**，每类赋一个「执行所需周期数」；ISS 每执行一条指令就按其类别累加周期。7 类是：`linear`（普通算术/逻辑）、`jump`（分支跳转）、`load`、`store`、`trap`（异常/中断）、`mul_div`（乘除）、`float`（浮点）。

一段程序在某个核上的总周期数可写成：

\[
T_{\text{cycle}} \;=\; \sum_{k \in \{\text{7 类}\}} n_k \cdot c_k
\]

其中 \(n_k\) 是该类指令的执行条数，\(c_k\) 是该类在这个核模型下的单条周期成本。换一个核模型，就是换一组 \((c_1,\dots,c_7)\)，同一个二进制的 \(T_{\text{cycle}}\) 随之改变。

本项目预置了 **7 个核模型**：`DEFAULT`（抽象基线）、`PICORV32`（本项目真实使用的软核）、`EDUBOS5STG2`/`EDUBOS5STG3`（教学核 2/3 级流水）、`IBEXMULSGL`/`IBEXMULFAST`/`IBEXMULSLOW`（IBEX 配不同乘法器）。通过 `-V <core>` 在 `vusermain.cfg` 里选。

#### 4.3.2 核心流程

1. `parseArgs` 解析 `-V`，把字符串映射成枚举 `risc_v_core_e`，存入 `vcfg.riscv_core`。
2. `pre_run_setup` 创建 `rv32_timing_config` 对象，调 `update_timing(pCpu, vcfg.riscv_core)`。
3. `update_timing` 按枚举选出对应的那组 7 周期常量，逐项调 `iss->update_timing(index, cycles)` 写进 ISS。
4. ISS 在执行期间按这组周期累加 `mcycle`，退出时（`-c`）打印在 CSR 转储里。

`PICORV32` 是本项目真正综合进硬件的软核（见 u2-l1），所以 `-V PICORV32` 让仿真周期最贴近上板实况；`DEFAULT` 是最快但最不准的抽象基线。

#### 4.3.3 源码精读

7 个核的周期常量与 7 类定义全在一个头文件里：

[4.sim/models/rv32/usercode/rv32_timing_config.h:L18-L38](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/4.sim/models/rv32/usercode/rv32_timing_config.h#L18-L38) — `rv32_iss_timing_t` 结构定义 7 类字段；下面 7 个 `const` 常量各是一个核的完整周期表。对比可见：`DEFAULT` 的 `linear=1` 而 `PICORV32` 的 `linear=4`；`IBEXMULSLOW` 的 `mul_div=33`（慢乘法器），`IBEXMULSGL` 只有 `1`。同一段乘法密集代码在这两核下周期差几十倍。

枚举与写入方法：

[4.sim/models/rv32/usercode/rv32_timing_config.h:L43-L85](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/4.sim/models/rv32/usercode/rv32_timing_config.h#L43-L85) — `risc_v_core_e` 枚举列出 7 个核；`update_timing` 用 `switch` 选模型，再用 7 次 `iss->update_timing(TIMING_*, cycles)` 把周期表灌进 ISS。要新增核模型，就在这里加 `case` 与上面的常量。

`-V` 选项的字符串→枚举映射：

[4.sim/models/rv32/usercode/vuserutils.cpp:L195-L216](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/4.sim/models/rv32/usercode/vuserutils.cpp#L195-L216) — `core == "PICORV32"` 等逐串比较，命中则赋对应枚举；都不匹配则报错。所以 `vusermain.cfg` 里 `-V` 的拼写必须与这些字符串完全一致（全大写、无空格）。

#### 4.3.4 代码实践

**实践目标**：用同一个二进制对比两个 timing model 的周期计数，直观感受「换核模型 = 换周期表」。

**操作步骤**：

1. 编辑 [4.sim/vusermain.cfg](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/4.sim/vusermain.cfg)，配置 A 用默认模型：
   ```
   vusermain0 -x 0x10000000 -X 0x20000000 -EHRc -t ./models/rv32/riscvtest/main.bin
   ```
   （不带 `-V` 即 `DEFAULT`，`linear=1`。）
2. 跑 `make -f MakefileVProc.mk BUILD=ISS run`，记下 CSR 转储里的 `mcycle` 值。
3. 改成配置 B，加 `-V PICORV32`：
   ```
   vusermain0 -V PICORV32 -x 0x10000000 -X 0x20000000 -EHRc -t ./models/rv32/riscvtest/main.bin
   ```
4. 再跑，记下 `mcycle`。

**需要观察的现象**：两次的 `minstret`（已执行指令数）应基本相同（同一份二进制、同样的控制流），但 `mcycle` 不同——`PICORV32` 每条普通指令算 4 拍，`DEFAULT` 只算 1 拍，故 `PICORV32` 的 `mcycle` 显著更大。

**预期结果**：`mcycle(PICORV32) > mcycle(DEFAULT)`，差距随 `linear_cycles` 之比近似放大。README 样例（`PICORV32` 下 `minstret=0xb`、`mcycle=0x37`）可作参照；切换到 `DEFAULT` 后 `mcycle` 会明显变小，**待本地验证**确数值。

#### 4.3.5 小练习与答案

**练习 1**：为什么 timing model 用「7 类」而不是「每条指令一个周期数」？

**参考答案**：粒度折中。逐条指令建模既繁琐又依赖完整微架构数据；按 7 个语义大类（线性/跳转/访存/异常/乘除/浮点）建模，既能区分指令的大致代价差异，又只需少量常量，且这些常量可从公开的核手册（如 picoRV32 的周期表）直接查到，便于校准。

**练习 2**：要在 ISS 里支持一个新核（比如某 5 级流水 RV32），需要改哪些地方？

**参考答案**：在 [rv32_timing_config.h](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/4.sim/models/rv32/usercode/rv32_timing_config.h) 加一组 7 周期常量、在 `risc_v_core_e` 枚举加一项、在 `update_timing` 的 `switch` 加一个 `case`；再在 [vuserutils.cpp:L195-L216](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/4.sim/models/rv32/usercode/vuserutils.cpp#L195-L216) 的 `-V` 分支加一条字符串匹配，并在帮助文本（L252 附近）补一行说明。

---

## 5. 综合实践

把本讲三件事——ISS 运行、外部访问区划分、timing model——串成一个小任务：**用 ISS 跑通自包含测试，并量化两项配置的影响**。

1. **基线运行**：按 [4.2.4](#424-代码实践本讲主实践) 配置 A 跑通 `main.bin`，确认输出里出现反汇编、`Register state:`、`CSR state:`、`Exited running`。
2. **路由对照**：切到配置 B（CSR 区送 HDL、DMEM 交 `mem_model`），确认测试仍 pass，记录两次的墙钟耗时，体会「少走总线 → 更快」。
3. **时序对照**：在配置 B 基础上分别用 `-V DEFAULT` 与 `-V PICORV32` 各跑一次，从 CSR 转储摘录 `minstret` 与 `mcycle`，填入下表，验证「`minstret` 几乎不变、`mcycle` 随周期表放大」。

   | 配置 | `-V` | `minstret` | `mcycle` | 是否 pass |
   | --- | --- | --- | --- | --- |
   | 例 | PICORV32 | 0xb | 0x37 | 是 |
   | 你的测量 | DEFAULT | 待本地验证 | 待本地验证 | 待本地验证 |
   | 你的测量 | PICORV32 | 待本地验证 | 待本地验证 | 待本地验证 |

4. **进阶（可选）**：把 `-V` 换成 `IBEXMULSLOW` 再跑一次，观察 `mcycle` 是否因 `main.bin` 几乎不含乘法而几乎不变——据此判断该测试对乘法器时序不敏感。

> 提示：改 `vusermain.cfg` 或换 `-V` 都**不必重建仿真**，直接重跑 `output/Vtb` 即可（若 Makefile 目标是 `run` 会自动复用已编好的可执行文件）。只有改了 HDL 或 `usercode/` 下的 C++ 才需要重新 `make`。

## 6. 本讲小结

- `rv32` 是一个 RISC-V ISS，以预编译库 `librv32lnx.a` 提供；它**嵌套在 VProc 之上**——`VUserMain0` 从「手写测试」变成「ISS 宿主」，加载并运行真实的 RISC-V 二进制，以此替代 RTL CPU 加速仿真。
- ISS 每次访存都回调 `ext_mem_access`；`-x`/`-X` 划定的**外部访问区**决定该次访存走 HDL 总线（`VWriteBE`/`VRead`）还是 `mem_model` 直接访问（`WriteRamWord`/`ReadRamWord`）。典型分工：CSR 走 HDL、IMEM/DMEM 走 `mem_model`。
- 「不走总线」时通过 `max_sync_diff = 1000` 的延迟同步策略压低 DPI-C 往返，这是 ISS 比 RTL 快的关键之一。
- **timing model** 把指令分 7 类、每类赋周期数，预置 7 个核模型；`-V` 在 `vusermain.cfg` 中切换，让 ISS 的 `mcycle` 贴近不同真实核（本项目用 `PICORV32`）。
- 改 RISC-V 二进制或 `vusermain.cfg` 无需重建仿真；改 HDL 或 `usercode/` 的 C++ 才需重编。
- 除 `parseArgs`/`ext_mem_access` 等手写源外，`librv32lnx.a` 与 `include/` 是不可改的预编译产物。

## 7. 下一步学习建议

- 本讲聚焦 node 0（CPU）。下一步建议学习 [u7-l4 以太网 VIP udpIpPg 与 BFM](u7-l4-udpippg-ethernet-vip.md)：node 1–4 的 VProc 驱动 `bfm_ethernet` 生成/接收 UDP-IPv4 包，与本讲的 ISS 共享同一套 VProc/mem_model 机制，但用 `udpIpPg` 的包级 API。
- 想深入端到端验证，可继续读 [u7-l5 mem_model 稀疏内存、PCAP 回放与逐模块测试台](u7-l5-mem-model-pcap-module-tb.md)，看 `mem_model` 如何成为 ISS、以太网 VIP 与 HDL 之间的公共内存「连接器」，以及 PCAP 回放的整包验证方法。
- 若关心被 ISS 跑的那份固件本身，回到 [Unit 6 软件控制面](u6-l1-sw-arch-boot-memmap.md)；若关心仿真如何替换回真实 picoRV32 RTL，复习 [u7-l1 仿真测试台总体架构](u7-l1-sim-testbench-overview.md) 的「可替换 soc_cpu」机制。
- 外部参考：[rv32 ISS 手册](https://github.com/wyvernSemi/riscV/blob/main/iss/doc/iss_manual.pdf)（GDB 远程调试、完整选项语义）与 [VProc 手册](https://github.com/wyvernSemi/vproc/blob/master/doc/VProc.pdf)。
