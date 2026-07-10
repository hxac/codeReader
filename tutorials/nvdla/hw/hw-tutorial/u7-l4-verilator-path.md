# Verilator 开源仿真路径

## 1. 本讲目标

本讲承接 [u7-l1](u7-l1-traceplayer-testbench.md) 讲解的 trace-player 测试平台。在那里，DUT（`NV_nvdla`）被一套 **SystemVerilog 测试平台**（`tb_top.v`、`csb_master.v`、`axi_slave.v`、`memory.v`）驱动，而运行它依赖 **Synopsys VCS** 这一商用仿真器。本讲要回答的问题是：**如果手里没有 VCS，能不能用开源工具也把同一个 DUT 跑起来？**

答案是 `verif/verilator/` 提供的第二条仿真路径，它把整套「测试平台 + 仿真器」从 SystemVerilog 搬到了 **C++**：用开源的 **Verilator** 把 RTL 编译成 C++ 类，再用一份手写的 C++ 驱动（`nvdla.cpp`）替代原来的 SV 测试平台。

学完本讲，你应当能够：

1. 说清 Verilator 路径相比 VCS 路径在「编译、激励、存储模型、判 PASS/FAIL」四个环节上分别做了什么替代。
2. 读懂 `verif/verilator/Makefile` 与 `verilator.f`，理解 RTL 如何被编译成可执行程序 `VNV_nvdla`。
3. 读懂 `input_txn_to_verilator.pl`，理解同一份人读的 `input.txn` 是如何被翻译成 Verilator 驱动能消费的二进制 `trace.bin`。
4. 读懂 `nvdla.cpp` 的三大组件（`CSBMaster`、`AXIResponder`、`TraceLoader`）与 `main()` 主循环，理解一个 C++ 驱动如何造时钟、发复位、回放 trace、并最终判定 PASS/FAIL。
5. 能够根据手头工具与目标，在 VCS 与 Verilator 两条路径间做合理选择。

## 2. 前置知识

本讲假设你已经掌握以下概念（前序讲义已建立）：

- **DUT 与测试平台**：被测设计 `NV_nvdla`，外加喂激励、接存储、查结果的外围 RTL（见 [u7-l1](u7-l1-traceplayer-testbench.md)）。
- **CSB 配置总线**：CPU 编程 NVDLA 的唯一入口，请求组 `csb2nvdla_*`、响应组 `nvdla2csb_*`（见 [u2-l1](u2-l1-csb-bus-apb2csb.md)）。
- **AXI memif**：DUT 对外的两组 AXI 存储接口——`core2dbb`（接片外主存，地址基 `0x8000_0000`）与 `core2cvsram`（接片上 CVSRAM，地址基 `0x5000_0000`），五通道 AR/R 与 AW/W/B（见 [u4-l1](u4-l1-memif-architecture.md)）。
- **trace 与 sanity**：一段人读的文本 `input.txn`，由若干 `write_reg`/`read_reg`/`load_mem`/`dump_mem`/`wait` 命令组成，构成一次完整的「配置→启动→查结果」流程；`sanity0` 是最小冒烟 trace（见 [u1-l4](u1-l4-first-simulation.md)）。
- **构建沙箱与 outdir**：`tmake` 按 `build.config` 驱动每个 sandbox，把生成的 RTL 放到 `outdir/nv_full/vmod/`（见 [u1-l3](u1-l3-build-system-toolchain.md)）。

本讲还要补充三个 Verilator 特有概念：

| 概念 | 一句话解释 |
|------|-----------|
| **Verilator** | 开源 Verilog/SystemVerilog 「编译器」。它不解释执行 RTL，而是把 RTL 翻译成一个 C++ 类（如 `VNV_nvdla`），再用普通 C++ 编译器编成原生可执行程序，运行速度快但时序精度低。 |
| **C++ 驱动（harness）** | 由于 Verilator 只给你一个「裸的 RTL 类」，时钟翻转、复位、喂激励、接存储这些原本由 SV 测试平台做的事，必须由你用 C++ 自己写。`nvdla.cpp` 就是这份驱动。 |
| **二进制 trace（trace.bin）** | Verilator 没有现成的 `$readmemh` 文件回放器，于是先用 `input_txn_to_verilator.pl` 把 `input.txn` 压成一种紧凑的二进制格式，再由 C++ 驱动逐字节解析回放。 |

> 直觉上：**VCS 路径 = SV 仿真器 + SV 测试平台 + 文本 trace**；**Verilator 路径 = RTL→C++ 编译器 + C++ 驱动 + 二进制 trace**。两者喂的是同一份 `input.txn`、跑的是同一个 `NV_nvdla`，只是「外围」全换了语言与实现。

## 3. 本讲源码地图

本讲只涉及 `verif/verilator/` 一个目录，共 4 个文件：

| 文件 | 行数 | 作用 | 对应 VCS 路径里的什么 |
|------|------|------|----------------------|
| `verif/verilator/Makefile` | 34 | 编译规则：调 Verilator 生成 `.mk`、再调 make 编出 `VNV_nvdla`；另含 trace 转换与 run 规则 | `verif/sim/Makefile`（`make build`/`make run`） |
| `verif/verilator/verilator.f` | 64 | 交给 Verilator 的文件列表：包含路径、库单元、宏、顶层模块 | VCS 的编译文件列表（`TB_VCS_BLD_ARGS`） |
| `verif/verilator/input_txn_to_verilator.pl` | 172 | 把人读的 `input.txn` 翻译成二进制 `trace.bin` | `inp_txn_to_hexdump.pl`（把 txn 转成 `.raw` 供 sequencer 回放） |
| `verif/verilator/nvdla.cpp` | 859 | C++ 驱动：CSBMaster + AXIResponder + TraceLoader + main 主循环 | 整套 SV 测试平台（`tb_top.v`/`csb_master.v`/`axi_slave.v`/`memory.v`） |

此外，本讲还会引用两处「上游」事实：`tools/etc/build.config` 中 verilator 沙箱的依赖声明，以及 `tools/make/tree.make.vm` 中工具变量的定义。

## 4. 核心概念与源码讲解

### 4.1 Verilator 编译：把 RTL 变成可执行程序

#### 4.1.1 概念说明

Verilator 的工作方式与 VCS 截然不同。VCS 是「先把 RTL 与测试平台一起编进一个仿真镜像 `simv`，运行时由仿真内核逐拍推进时间、调度事件」。Verilator 则是「把 RTL 静态翻译成一个 C++/SystemC 类的源码（`.cpp`/`.h`），再交给普通 C++ 编译器（这里用 clang）编成原生可执行程序」。运行时没有「仿真内核」——时间推进就是你在 C++ 里手动翻转时钟信号、再调用 `dla->eval()`。

因此 Verilator 路径的「编译」分两步：

1. **Verilator 前端**：读入 RTL，产出 `VNV_nvdla.mk` 与一组 `.cpp/.h`（C++ 模型源码）。
2. **C++ 后端**：按 `VNV_nvdla.mk`，用 clang 把这些源码连同 `nvdla.cpp` 一起编成可执行程序 `VNV_nvdla`。

文件名 `VNV_nvdla` 的来历：Verilator 对 `--top-module NV_nvdla` 生成的 C++ 类叫 `VNV_nvdla`，可执行程序也沿用这个名字。它就相当于 VCS 路径里的 `simv`。

#### 4.1.2 核心流程

`verif/verilator/Makefile` 一共定义了四个目标，串起「编译→转换 trace→运行」全过程：

```text
make                              # 默认目标：只编译出 VNV_nvdla
make run TEST=sanity0             # 转 trace.bin + 编译 + 运行
        │
        ├── VNV_nvdla.mk  ←(1) verilator 前端：读 verilator.f，生成 C++ 源与 .mk
        ├── VNV_nvdla     ←(2) make -f VNV_nvdla.mk：clang 编出可执行程序
        └── test/<TEST>/trace.bin ←(3) input_txn_to_verilator.pl：input.txn → trace.bin
                              ↓
            cd test/<TEST> && ../../VNV_nvdla trace.bin   ←(4) 运行
```

注意一个关键依赖关系：Verilator 路径**不自己生成 RTL**，它消费 `tmake` 已经生成好的 `outdir/nv_full/vmod/`。这在 `build.config` 里体现为 verilator 沙箱只声明依赖 `vmod_nvdla_top`（后者又传递依赖所有引擎子模块）。

#### 4.1.3 源码精读

**默认目标**——编译出可执行程序：

[verif/verilator/Makefile:L12-L12](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/verilator/Makefile#L12-L12) 定义默认产物是 `$(OUTDIR)/$(PROJECT)/verilator/VNV_nvdla`（即 `outdir/nv_full/verilator/VNV_nvdla`）。

[verif/verilator/Makefile:L14-L14](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/verilator/Makefile#L14-L14) 定义 Verilator 参数：`--compiler clang`（指定后端 C++ 编译器为 clang）、`--output-split 250000000`（把生成的巨型 C++ 文件按行数拆分，加速编译）。

**第一步：Verilator 前端**——读 `verilator.f`、生成 `.mk`：

[verif/verilator/Makefile:L20-L21](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/verilator/Makefile#L20-L21) 调用 `$(VERILATOR) --cc --exe -f verilator.f --Mdir ... nvdla.cpp`。其中 `--cc` 生成 C++（而非 SystemC）模型，`--exe` 表示要和用户提供的 `nvdla.cpp` 一起编成可执行程序，`-f verilator.f` 指定文件列表，`--Mdir` 指定输出目录。这里规则依赖 `../../outdir/nv_full/vmod`——即必须先有生成好的 RTL。

> 旁注：`$(VERILATOR)` 与 `$(CLANG)` 这两个工具变量在仓库的 `tools/make/tree.make.vm` 模板里**并未定义**（模板里只定义了 `CPP`/`JAVA`/`PERL`）。也就是说，跑 Verilator 路径需要你自己把 `VERILATOR` 和 `CLANG` 填进 `tree.make` 或环境变量。这是 Verilator 路径相比 VCS 路径「需要更多手工准备」的地方。

**第二步：C++ 后端**——按 `.mk` 编出可执行程序：

[verif/verilator/Makefile:L23-L26](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/verilator/Makefile#L23-L26) 先把 `nvdla.cpp` 拷进输出目录，再 `make -C ... -f VNV_nvdla.mk CC=$(CLANG) CXX=$(CLANG)++ VM_PARALLEL_BUILDS=1`。`VM_PARALLEL_BUILDS=1` 让 Verilator 并行编译拆分出来的多个 `.cpp` 文件，缩短编译时间。

**`verilator.f` 文件列表**——告诉 Verilator 编哪些 RTL、怎么编：

[verif/verilator/verilator.f:L1-L19](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/verilator/verilator.f#L1-L19) 用 `-I` 列出所有引擎子模块、`rams/synth`、`vlibs`、`include` 的头文件搜索路径，全部指向**生成目录** `outdir/nv_full/vmod/...`（再次印证 Verilator 消费的是生成后的 RTL）。

[verif/verilator/verilator.f:L20-L56](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/verilator/verilator.f#L20-L56) 用 `-v` 列出库单元：两个 vlibs 断言/随机原语、`XXIF_libs`（存储接口共享库），以及一大批 `rams/model/RAM*_*.v` 行为 RAM 模型（对应 [u6-l3](u6-l3-ram-models.md) 讲的仿真用 RAM）。

[verif/verilator/verilator.f:L57-L63](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/verilator/verilator.f#L57-L63) 定义关键宏与顶层：

- `-DNO_PLI_OR_EMU -DNO_PLI`：禁用 PLI/仿真专用代码（VCS 路径里那些 `$display`/`$fopen` 等系统任务相关的东西）。
- `-DDESIGNWARE_NOEXIST`：声明 DesignWare IP（如 `DW02_tree` 压缩树）不存在，让 RTL 走行为替代实现。
- `-DSYNTHESIS`：选中 RAM 行为模型的「综合壳」身份（见 [u6-l3](u6-l3-ram-models.md) 的三宏切身份）。
- `--top-module NV_nvdla`：顶层模块。

[verif/verilator/verilator.f:L64-L64](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/verilator/verilator.f#L64-L64) 给出顶层文件 `outdir/nv_full/vmod/nvdla/top/NV_nvdla.v`，Verilator 从这里自顶向下解析整棵例化树。

**构建依赖声明**——为什么 Verilator 沙箱只依赖 `vmod_nvdla_top`：

[tools/etc/build.config:L160-L163](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/tools/etc/build.config#L160-L163) 声明 `verilator` 沙箱只依赖 `vmod_nvdla_top`。因为 `vmod_nvdla_top` 已经在 [build.config:L140-L156](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/tools/etc/build.config#L140-L156) 里传递依赖了全部 16 个引擎子模块，Verilator 只需要这些 RTL「已经生成在 outdir」即可，不必重复声明。

#### 4.1.4 代码实践

**实践目标**：不实际编译（需要安装 Verilator + clang + 完整 outdir，本环境不具备），仅通过阅读 Makefile 与 `verilator.f`，把「RTL 如何变成 `VNV_nvdla`」的两步拆解清楚。

**操作步骤**：

1. 打开 `verif/verilator/Makefile`，找到三个目标 `VNV_nvdla.mk`、`VNV_nvdla`、`run`，分别写出它们的依赖与命令。
2. 打开 `verif/verilator/verilator.f`，统计 `-I` 包含路径数、`-v` 库单元数、`-D` 宏数。
3. 对照 [u6-l3](u6-l3-ram-models.md)，解释为什么这里 `-v` 指向的是 `rams/model/`（行为模型）而不是 `rams/synth/`。

**需要观察的现象**：

- 第 1 步应能看到：先 `$(VERILATOR) ...` 生成 `.mk`，再 `make -f VNV_nvdla.mk ...` 编出可执行程序；`run` 目标先依赖 `trace.bin`（触发 perl 转换）再依赖 `VNV_nvdla`（触发编译），最后 `cd` 进 test 目录执行。
- 第 2 步应得到：19 条 `-I`、约 36 条 `-v`、4 条 `-D`。

**预期结果**：你能用一句话说清——「Verilator 前端把 `verilator.f` 列出的 RTL 翻译成 C++，clang 后端再把它和 `nvdla.cpp` 编成 `VNV_nvdla`，整个过程消费 outdir 里已生成的 RTL，不重新生成」。

> ⚠️ 若要真正编译：需要先 `make`（顶层）生成 `outdir/nv_full/vmod`，并在 `tree.make` 里补上 `VERILATOR` 与 `CLANG` 变量，再 `cd verif/verilator && make`。本环境未安装 Verilator，编译步骤**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `verilator.f` 里的所有路径都指向 `outdir/nv_full/vmod/...` 而不是源码树 `vmod/...`？

> **答案**：因为 NVDLA 的很多 RTL 是由 `defgen`/`eperl` 从模板生成的（见 [u1-l3](u1-l3-build-system-toolchain.md)），生成产物落在 `outdir/nv_full/vmod/`。Verilator 编译的是生成后的最终 RTL，不是带 `#ifdef`/eperl 注释的源模板。

**练习 2**：`-DSYNTHESIS` 与 `-DDESIGNWARE_NOEXIST` 这两个宏在 Verilator 路径里分别起什么作用？

> **答案**：`-DSYNTHESIS` 让 RAM 行为模型切到「综合壳」身份（极简或空实现，避免行为模型里的仿真专用代码干扰 Verilator）；`-DDESIGNWARE_NOEXIST` 声明 DesignWare IP 不存在，迫使 RTL 用行为级替代实现（如把 `DW02_tree` 压缩树展开成普通逻辑），让 Verilator 能完整解析。

### 4.2 trace 转换：把 input.txn 编译成 trace.bin

#### 4.2.1 概念说明

无论哪条仿真路径，输入都是同一份人读的 `input.txn`（见 [u7-l2](u7-l2-csb-sequence-trace.md)）。但 VCS 路径用 `inp_txn_to_hexdump.pl` 把它转成定宽十六进制 `input.txn.raw`，交由 SV sequencer 用 `$readmemh` 回放；Verilator 路径没有 `$readmemh`，于是 `input_txn_to_verilator.pl` 把同一份 `input.txn` 转成一种**二进制**格式 `trace.bin`，再由 C++ 驱动逐字节 `read()` 解析。

关键区别：

- `.raw` 是**文本**（十六进制 ASCII），用 `$readmemh` 一次装入数组。
- `trace.bin` 是**二进制**，每条命令是一个 1 字节操作码后跟若干 32 位小端整数，C++ 驱动按字节流解析。

#### 4.2.2 核心流程

转换器是命令行两参数脚本：第 1 参数是输入目录（含 `input.txn`），第 2 参数是输出文件 `trace.bin`。

```text
input.txn (文本)                     trace.bin (二进制)
─────────────────                   ──────────────────
read_reg 0xffff100b ...        →    [03][addr][mask][exp]   (read_reg, op=3)
write_reg 0xffff100b 0xf0a5..  →    [02][addr][data]        (write_reg, op=2)
load_mem 0x80000000 0x40 f.dat →    [05][addr][len][64 bytes...]
dump_mem 0x80000000 0x40 g.dat →    [04][addr][len][64 bytes...][namelen][fname]
wait                           →    [01]                    (wait, op=1)
(文件末尾)                      →    [FF]                    (结束标志, op=0xFF)
```

每条命令头一个字节是**操作码**，其后跟随的字段数量与长度由操作码决定。注意操作码与文件头部 `%command_hash` 里的十六进制码（00~06）**不一样**——真正写入二进制的是脚本 `pack` 时硬编码的 1/2/3/4/5/0xFF。

`load_mem`/`dump_mem` 还会**就地读取 `.dat` 文件**并把数据原样追加进 `trace.bin`，这样 C++ 驱动只要读一个文件就能拿到「命令 + 数据」全部内容。

#### 4.2.3 源码精读

**文件名与参数约定**：

[verif/verilator/input_txn_to_verilator.pl:L6-L8](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/verilator/input_txn_to_verilator.pl#L6-L8) 定义读寄存器轮询重试次数 `50000`，并把第 1 个命令行参数当作测试目录（`$test_dir`）。

[verif/verilator/input_txn_to_verilator.pl:L22-L23](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/verilator/input_txn_to_verilator.pl#L22-L23) 约定输入 `$test_dir/input.txn`、输出为第 2 参数。

**操作码对照表**（仅作注释参考，与实际 `pack` 码不同）：

[verif/verilator/input_txn_to_verilator.pl:L11-L19](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/verilator/input_txn_to_verilator.pl#L11-L19) 列出 7 类命令的助记符到十六进制串的映射。注意这个 hash 在当前脚本主流程里**未被使用**（它属于一段被注释掉的旧实现），真正写入二进制的是下面各分支里的 `pack` 数字。

**逐行解析与打包**：脚本主体是一个 `while(<$inf>)` 循环，按每行第一个词分发：

[verif/verilator/input_txn_to_verilator.pl:L82-L84](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/verilator/input_txn_to_verilator.pl#L82-L84) `wait` → 写 1 字节 `pack("C", 1)`。

[verif/verilator/input_txn_to_verilator.pl:L85-L92](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/verilator/input_txn_to_verilator.pl#L85-L92) `write_reg` → `pack("CLL", 2, addr, data)`：1 字节操作码 `2`，加两个 32 位小端整数（地址、数据）。注释点明「CSB 高 16 位是杂项、低 16 位是地址」的约定。

[verif/verilator/input_txn_to_verilator.pl:L94-L107](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/verilator/input_txn_to_verilator.pl#L94-L107) `read_reg` → `pack("CLLL", 3, addr, mask, exp_data)`：操作码 `3`，加地址、掩码、期望值三个 32 位整数。注意它把 VCS 路径里的比较模式（EQ/LE/GE，见 [u7-l2](u7-l2-csb-sequence-trace.md)）**简化掉了**，只保留「掩码 + 期望值」，真正的比较由 C++ 驱动按「掩码相等」完成（见 4.3.3）。

[verif/verilator/input_txn_to_verilator.pl:L108-L158](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/verilator/input_txn_to_verilator.pl#L108-L158) `load_mem`/`dump_mem` → 先 `pack("CLL", cmd, addr, offset)` 写头（`load` 操作码 `5`、`dump` 操作码 `4`），随后读 `.dat` 文件，每行 32 字节按字节 `pack("C", ...)` 追加；`dump_mem` 末尾再追加 `pack("L", length) . $mem_in`，把参考文件名也写进去（供 C++ 驱动 dump 后比对/命名）。

[verif/verilator/input_txn_to_verilator.pl:L127-L148](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/verilator/input_txn_to_verilator.pl#L127-L148) `.dat` 解析约定：每行必须能解析成恰好 32 组两位十六进制（即 32 字节），否则报错；读到累计字节数等于 `offset` 即停。

[verif/verilator/input_txn_to_verilator.pl:L166-L166](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/verilator/input_txn_to_verilator.pl#L166-L166) 文件结尾写 1 字节 `0xFF` 作为「结束」标志，C++ 驱动读到它即停止解析。

**Makefile 里的转换规则**：

[verif/verilator/Makefile:L28-L31](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/verilator/Makefile#L28-L31) `trace.bin` 依赖源 trace 目录与脚本，执行 `$(PERL) input_txn_to_verilator.pl $< $@`——把 `verif/traces/traceplayer/<TEST>` 转成 `outdir/nv_full/verilator/test/<TEST>/trace.bin`。

#### 4.2.4 代码实践

**实践目标**：亲手把 `sanity0` 的 `input.txn` 转成 `trace.bin`，用十六进制工具验证转换结果与讲义描述一致。

**操作步骤**：

1. 看 `sanity0` 的 trace 内容（已确认它是 4 行：读默认值→写魔数→读回校验）：

```bash
cat verif/traces/traceplayer/sanity0/input.txn
# 预期：3 条 read_reg/write_reg，外加注释行
```

2. 用脚本转换（`PERL` 通常系统自带）：

```bash
cd verif/verilator
perl input_txn_to_verilator.pl ../../verif/traces/traceplayer/sanity0 /tmp/sanity0.bin
```

3. 用十六进制查看前若干字节：

```bash
xxd /tmp/sanity0.bin | head
```

**需要观察的现象**：

- 第 1 步应看到形如 `read_reg 0xffff100b 0xffffffe0 0x00000000` 的行。
- 第 3 步 `xxd` 输出的第一行，头一个字节应是 `03`（read_reg 操作码），随后 4 字节是小端的 `0xffff100b`（`0b 10 ff ff`），再 4 字节掩码，再 4 字节期望值；中间某处出现 `02`（write_reg）；末尾出现 `ff`。

**预期结果**：`trace.bin` 的字节序列能与「操作码 + 小端 32 位字段」的格式对上，证明转换器工作正常。这一步**可在本环境验证**（仅需 perl）。

> ⚠️ 注意：脚本里 `read_reg_poll_retries = 50000` 与 `sanity0/plusargs.txt` 里的 `+read_reg_poll_retries=10`（VCS 路径用）不同；但实际重试逻辑在 C++ 驱动里硬编码为 10 次（见 4.3.3），perl 里的 50000 仅是一个未被主流程使用的注释值。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `read_reg` 在二进制里只存「地址、掩码、期望值」三段，而 VCS 路径的 `.raw` 还存了比较模式（EQ/LE/GE）？

> **答案**：Verilator 的 C++ 驱动只实现了一种比较——「`(data & mask) == (exp & mask)`」的掩码相等，因此转换时把模式位丢弃。VCS sequencer 支持更丰富的轮询语义（小于等于、大于等于等），所以保留了模式字段。

**练习 2**：`dump_mem` 命令为什么要在数据末尾再追加文件名长度和文件名？

> **答案**：`dump_mem` 的语义是「把 DUT 写到某地址的数据导出，并与参考文件比对」。C++ 驱动需要知道参考文件名才能在比对不通过时记录、或在通过时把结果写出，所以转换器把文件名一并塞进 `trace.bin`，让 C++ 驱动一次性拿到全部信息。

### 4.3 nvdla.cpp 驱动：CSBMaster / AXIResponder / TraceLoader

#### 4.3.1 概念说明

`nvdla.cpp` 是 Verilator 路径的「测试平台」，用 C++ 实现了 VCS 路径里由四五个 SV 文件承担的全部职责。它由三大组件加一个 `main()` 构成：

| 组件 | C++ 类 | 对应 VCS 路径的 | 职责 |
|------|--------|----------------|------|
| CSB 激励源 | `CSBMaster` | `csb_master.v` + `csb_master_seq.v` | 把 trace 里的 `write_reg`/`read_reg` 翻译成对 `csb2nvdla_*` 端口的合法 CSB 握手 |
| AXI 存储模型 | `AXIResponder` | `axi_slave.v` + `memory.v` | 模拟 DBB 与 CVSRAM 两个 AXI slave，提供读写存储与回响应 |
| trace 解释器 | `TraceLoader` | sequencer 的命令回放部分 | 解析 `trace.bin`，把命令分派给 CSBMaster 或 AXIResponder |
| 顶层主循环 | `main()` | `tb_top.v` | 造时钟、发复位、驱动主循环、判定 PASS/FAIL |

直觉上，这三个组件的关系是：`TraceLoader` 是「指挥」，它从 `trace.bin` 读命令；遇到寄存器命令就 enqueue 给 `CSBMaster`、遇到存储命令（load/dump）就自己排队等 `AXIResponder`；`main()` 在每个时钟拍分别让三者各进一步，再翻转时钟。

#### 4.3.2 核心流程

`main()` 的整体节奏（去掉波形与细节）：

```text
1. 实例化 VNV_nvdla、CSBMaster、两个 AXIResponder(dbb, cvsram)、TraceLoader
2. 把 DUT 的两组 AXI 端口指针绑给两个 AXIResponder；设置静态控制管脚
3. trace->load(trace.bin)：解析全部命令，寄存器命令入 CSBMaster 队列，
                                    存储命令入 TraceLoader 自己的队列
4. 复位序列：reset=1 跑 20 拍 → reset=0 跑 20 拍 → reset=1
   再「等缓冲清空」跑 4096 拍
5. 主循环（每拍）：
     csb->eval()        // CSBMaster 推进一步：发请求 / 收响应 / 比对
     若返回 extevent：
        AXIEVENT → trace->axievent()  // 处理 load/dump_mem
        WFI      → 标记 waiting，等 dla_intr 拉高
     axi_dbb->eval(); axi_cvsram->eval()  // AXI 存储模型推进一步
     翻转时钟（core/clk 与 csb/clk 同相），dla->eval()
   循环条件：CSBMaster 还有命令未发完，或 quiesc_timer(=200) 未耗尽
6. 根据 TraceLoader 与 CSBMaster 的 test_passed() 判 PASS/FAIL，决定返回码
```

两个值得注意的简化：

- **单时钟**：`dla_core_clk` 与 `dla_csb_clk` 被**同相翻转**，相当于把 core/falcon 双时钟域（见 [u6-l1](u6-l1-clock-reset-car.md)）合成一个时钟。CSB 跨域 FIFO 仍能工作，只是没有真正的跨域异步效应。
- **激励预装载**：`trace->load()` 在仿真开始前就把**所有**命令解析进内存队列，主循环只是消费队列，不像 SV sequencer 那样边读边发。

#### 4.3.3 源码精读

**CSBMaster——把命令变成 CSB 握手**：

[verif/verilator/nvdla.cpp:L37-L48](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/verilator/nvdla.cpp#L37-L48) 定义 `csb_op` 结构与命令队列 `opq`。每个 op 记录是读还是写、地址、（读的）掩码与期望值、重试次数。

[verif/verilator/nvdla.cpp:L62-L85](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/verilator/nvdla.cpp#L62-L85) `read()`/`write()` 把一条命令包装成 `csb_op` 压入队列。`read` 默认重试 10 次（`op.tries = 10`）——这就是 4.2.4 提到的「实际重试 10 次」来源。

[verif/verilator/nvdla.cpp:L111-L124](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/verilator/nvdla.cpp#L111-L124) 读响应的**掩码相等比对**：`(dla->nvdla2csb_data & op.mask) != (op.data & op.mask)`。不等就把 `reading` 复位、重试数减一；重试耗尽则记 `_test_passed = 0`（CSB 读失败，最终返回码 2）。这正是 4.2 讲的「Verilator 把 EQ/LE/GE 简化成掩码相等」的实现处。

[verif/verilator/nvdla.cpp:L138-L153](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/verilator/nvdla.cpp#L138-L153) 实际驱动端口：写时拉 `csb2nvdla_valid=1`、给 `addr`/`wdat`/`write=1`/`nposted=0`（投递写，见 [u2-l1](u2-l1-csb-bus-apb2csb.md)）；读时给 `addr`、`write=0`，并置 `reading=1` 等下一拍收响应。这些都严格符合 CSB 协议。

**AXIResponder——一个 C++ 类当两个 AXI slave**：

[verif/verilator/nvdla.cpp:L202-L205](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/verilator/nvdla.cpp#L202-L205) 定义 AXI 常量：块大小 `AXI_BLOCK_SIZE=4096`、数据宽度 `AXI_WIDTH=512`、读响应延迟 `AXI_R_LATENCY=32` 拍。

[verif/verilator/nvdla.cpp:L236-L236](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/verilator/nvdla.cpp#L236-L236) 存储体用 `std::map<uint32_t, std::vector<uint8_t>> ram` 实现：以 4KB 块为键、块内按字节寻址，按需 `resize`。这比 VCS 路径里的 `slave_mem.v` 行为数组更省内存（稀疏）。

[verif/verilator/nvdla.cpp:L269-L277](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/verilator/nvdla.cpp#L269-L277) `read`/`write` 字节访问器：先按 `addr / 4096` 找到（或新建）4KB 块，再按 `addr % 4096` 读写一个字节。`load_mem`/`dump_mem` 最终都落到这两个函数。

[verif/verilator/nvdla.cpp:L279-L432](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/verilator/nvdla.cpp#L279-L432) `AXIResponder::eval()` 每拍处理 AXI 五通道：写地址 `AW`、写数据 `W`、写响应 `B`、读地址 `AR`、读数据 `R`。它用多个 `std::queue` 缓冲在途事务，读响应还经 `r0_fifo` 注入 `AXI_R_LATENCY` 拍延迟以模拟真实读延迟。这相当于把 VCS 路径里 `axi_slave.v` 的 7 个 FIFO 解耦逻辑（见 [u7-l1](u7-l1-traceplayer-testbench.md)）用 C++ 重写了一遍。

**TraceLoader——解析 trace.bin 并分派**：

[verif/verilator/nvdla.cpp:L469-L570](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/verilator/nvdla.cpp#L469-L570) `load()` 用 `read(fd, ...)` 逐条读操作码，按 1/2/3/4/5/0xFF 分派：

- [L484-L486](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/verilator/nvdla.cpp#L484-L486) 操作码 `1` = `wait` → 往 CSBMaster 塞一个 `TRACE_WFI` 外部事件。
- [L488-L497](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/verilator/nvdla.cpp#L488-L497) 操作码 `2` = `write_reg` → 读地址、数据，调 `csb->write()`。
- [L498-L509](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/verilator/nvdla.cpp#L498-L509) 操作码 `3` = `read_reg` → 读地址、掩码、期望值，调 `csb->read()`。
- [L510-L538](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/verilator/nvdla.cpp#L510-L538) 操作码 `4` = `dump_mem` → 读地址、长度、参考字节、文件名，入自己的 `opq`，并塞 `TRACE_AXIEVENT`。
- [L539-L559](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/verilator/nvdla.cpp#L539-L559) 操作码 `5` = `load_mem` → 读地址、长度、字节，入 `opq`，塞 `TRACE_AXIEVENT`。
- [L560-L562](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/verilator/nvdla.cpp#L560-L562) 操作码 `0xFF` = 结束。

[verif/verilator/nvdla.cpp:L580-L588](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/verilator/nvdla.cpp#L580-L588) `axievent()` 按地址高位路由：`(addr & 0xF0000000) == 0x50000000` 走 CVSRAM、`== 0x80000000` 走 DBB，否则报错 abort。这与 [u7-l1](u7-l1-traceplayer-testbench.md) 里 DBB=`0x8000_0000`、CVSRAM=`0x5000_0000` 的约定一致。

[verif/verilator/nvdla.cpp:L603-L631](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/verilator/nvdla.cpp#L603-L631) `AXI_DUMPMEM` 处理：逐字节读出 DUT 写到该地址的数据，**与参考字节比对**；任一字节不等就置 `_test_passed = 0`（输出不匹配，最终返回码 1）。这就是 Verilator 路径判结果正确性的核心——`dump_mem` 的参考数据来自转换器塞进 `trace.bin` 的 `.dat` 内容。

**main()——造时钟、发复位、跑主循环、判 PASS/FAIL**：

[verif/verilator/nvdla.cpp:L644-L645](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/verilator/nvdla.cpp#L644-L645) `new VNV_nvdla`——实例化 Verilator 编译出的 DUT 类。

[verif/verilator/nvdla.cpp:L648-L708](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/verilator/nvdla.cpp#L648-L708) 把 DUT 的 `nvdla_core2dbb_*` 与 `nvdla_core2cvsram_*` 两组 AXI 五通道端口指针，分别绑给 `axi_dbb` 与 `axi_cvsram`。`AXIResponder` 直接持有这些指针，每拍读写它们——这是 C++ 驱动与 DUT 交互的方式（操作生成的 C++ 类的公共成员）。

[verif/verilator/nvdla.cpp:L720-L728](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/verilator/nvdla.cpp#L720-L728) 设置静态控制管脚：`global_clk_ovr_on=0`、`tmc2slcg_disable_clock_gating=0`（不强制关 slcg，见 [u6-l1](u6-l1-clock-reset-car.md)）、`test_mode=0`、各 `pwrbus_ram_*_pd=0`（RAM 电源域全开）。

[verif/verilator/nvdla.cpp:L738-L802](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/verilator/nvdla.cpp#L738-L802) 复位序列：`reset=1` 跑 20 拍 → `reset=0` 跑 20 拍（真正生效的复位段）→ `reset=1`；再额外跑 4096 拍「letting buffers clear after reset」让内部缓冲清空。每拍都是「拉高两个时钟→eval→拉低→eval」。

[verif/verilator/nvdla.cpp:L804-L842](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/verilator/nvdla.cpp#L804-L842) 主循环：`while (!csb->done() || quiesc_timer--)`。每拍先 `csb->eval()`；若它返回外部事件，`AXIEVENT` 就 `trace->axievent()` 处理 load/dump，`WFI` 就置 `waiting=1`。`waiting` 时一旦 `dla->dla_intr` 拉高就解除等待（见 [L819-L822](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/verilator/nvdla.cpp#L819-L822)）——这就是 trace 里 `wait` 命令「等一次中断」的语义。然后两个 AXI responder 各 `eval()` 一次，再翻转时钟。`quiesc_timer=200` 保证 CSB 命令发完后还多跑 200 拍，让在途事务收尾。

[verif/verilator/nvdla.cpp:L846-L858](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/verilator/nvdla.cpp#L846-L858) 最终判定：若 `dump_mem` 比对发现不匹配（`trace->test_passed()==0`）返回 `1`；若 CSB 读超时（`csb->test_passed()==0`）返回 `2`；否则打印 `*** PASS` 返回 `0`。返回码非 0 即代表测试失败——这是 [git 提交 7c769aa](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/verilator/nvdla.cpp#L846-L858)「verify output and return with appropriate error codes」加上的。

#### 4.3.4 代码实践

**实践目标**：不运行（需完整编译），通过阅读源码追踪一条 `write_reg` 命令从 `trace.bin` 到 DUT 寄存器端口的完整旅程，画出数据流。

**操作步骤**：

1. 在 `nvdla.cpp` 中定位 `TraceLoader::load()` 的操作码 `2` 分支（[L488-L497](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/verilator/nvdla.cpp#L488-L497)），确认它调用 `csb->write(addr, data)`。
2. 跳到 `CSBMaster::write()`（[L76-L85](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/verilator/nvdla.cpp#L76-L85)），确认它只是把 op 压入 `opq`。
3. 跳到 `CSBMaster::eval()` 的写分支（[L138-L145](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/verilator/nvdla.cpp#L138-L145)），确认它驱动 `csb2nvdla_valid/addr/wdat/write/nposted`。
4. 在 `main()` 主循环（[L810-L825](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/verilator/nvdla.cpp#L810-L825)）确认 `csb->eval()` 每拍被调用，之后翻转时钟 `dla->eval()`。

**需要观察的现象**：一条 `write_reg` 经过「`trace.bin` → `TraceLoader::load` → `CSBMaster::write`（入队）→ 主循环每拍 `CSBMaster::eval`（出队并驱动端口）→ `dla->eval`（DUT 采样）」四级。

**预期结果**：你能画出这条链路图，并指出「入队」与「实际驱动端口」是**异步**的——命令先全部入队，主循环再逐拍消费，这与 SV sequencer「边读边发」不同。

> ⚠️ 实际运行 `VNV_nvdla trace.bin` 需先完成 4.1 的编译，本环境不具备，**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：`main()` 里 `dla_core_clk` 与 `dla_csb_clk` 是同相翻转的，这意味着什么？为什么这样仍然能跑通 CSB 配置？

> **答案**：意味着 Verilator 路径把 core 与 falcon（csb）两个时钟域合并成了单一时钟。CSB 的跨域 FIFO（见 [u2-l2](u2-l2-csb-master-router.md)）在「两时钟完全相同」时退化为同步 FIFO，仍能正确传递请求与响应；只是丢失了真实跨域的异步握手行为，因此 Verilator 路径**不能**用于验证跨时钟域的时序正确性，只能验证功能逻辑。

**练习 2**：测试结果正确性靠什么判定？返回码 0/1/2 分别代表什么？

> **答案**：靠 `dump_mem` 时「DUT 实际写到存储的数据」与「参考 `.dat` 字节」逐字节比对（[L603-L631](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/verilator/nvdla.cpp#L603-L631)）。返回码 `0`=PASS；`1`=dump 比对发现输出不匹配（功能错）；`2`=CSB 读寄存器时掩码比对超时（配置/状态错）。

### 4.4 两条仿真路径对比：VCS vs Verilator

#### 4.4.1 概念说明

把 4.1~4.3 与 [u7-l1](u7-l1-traceplayer-testbench.md)/[u7-l2](u7-l2-csb-sequence-trace.md) 拼起来，就能看清两条路径的全貌。它们**喂同一份 `input.txn`、跑同一个 `NV_nvdla`、用同样的 DBB/CVSRAM 地址约定**，差异全在「外围」：仿真器、测试平台语言、激励格式、存储模型、判 PASS/FAIL 机制。

#### 4.4.2 核心流程对比

| 维度 | VCS 路径（`verif/sim`） | Verilator 路径（`verif/verilator`） |
|------|------------------------|-------------------------------------|
| 仿真器 | Synopsys VCS（商用） | Verilator（开源，RTL→C++） |
| 测试平台语言 | SystemVerilog（`tb_top.v` 等） | C++（`nvdla.cpp`） |
| 产物 | `simv` | `VNV_nvdla` |
| 时钟模型 | 真实 core/falcon 双时钟域 | 单时钟（两 clk 同相翻转） |
| 激励格式 | `input.txn` → `.raw`（文本十六进制，`$readmemh`） | `input.txn` → `trace.bin`（二进制，`read()`） |
| CSB 激励 | `csb_master_seq.v` + `csb_master.v`，支持 EQ/LE/GE 轮询 | `CSBMaster` 类，仅掩码相等、重试 10 次 |
| 存储模型 | `axi_slave.v` + `memory.v`（行为数组 + 带宽节流） | `AXIResponder` 类（`std::map` 稀疏 4KB 块 + 固定延迟） |
| 波形 | FSDB/Verdi（`DUMP=1 DUMPER=VERDI`） | 默认无，可选 VCD（`--trace`，需重编） |
| 判 PASS/FAIL | `checktest_synthtb.pl` 扫日志 `ERROR` + dump 比对 | 进程返回码 0/1/2 |
| 依赖 | 完整 SV TB、VCS license | 需自行备 Verilator + clang，依赖 outdir 已生成 |

#### 4.4.3 各自适用场景

- **VCS 路径**适合：需要精确时序与跨时钟域验证、需要 Verdi 波形深度调试、需要完整 trace-player 特性（多种比较模式、带宽节流逼近真实）、有 VCS license 的团队。
- **Verilator 路径**适合：没有商用仿真器 license、想要快速跑大量功能回归（编译后执行快）、CI 环境（开源、可脚本化）、只需验证「功能对不对」而非精确时序的场景。

> 直觉：Verilator 路径是「轻量、开源、快、但糙」的功能仿真器；VCS 路径是「重型、商用、精确、可调」的时序仿真器。二者互补，不互斥——同一份 trace 可以两条路都跑，功能问题先用 Verilator 快速暴露，时序问题再交给 VCS。

#### 4.4.4 代码实践

**实践目标**：动手对比两条路径在「运行同一个 sanity trace」时的命令差异。

**操作步骤**：

1. VCS 路径（需 VCS，见 [u1-l4](u1-l4-first-simulation.md)）：

```bash
cd verif/sim
make build
make run TESTDIR=../traces/traceplayer/sanity0
# 结果在 verif/sim/sanity0/test.log，由 checktest_synthtb.pl 判定
```

2. Verilator 路径（需 Verilator + clang + 已生成 outdir）：

```bash
cd verif/verilator
make run TEST=sanity0
# 实际执行：perl 转 trace.bin → verilator+clang 编 VNV_nvdla → 运行
# 结果由进程返回码判定：0=PASS, 1=输出不匹配, 2=CSB 读超时
```

**需要观察的现象**：

- VCS 路径产出文本日志 `test.log`，由外部脚本判定 PASSED/FAILED。
- Verilator 路径产出大量 `printf` 日志（`CMD: write_reg ...`、`AXI: ...`）直接打到 stdout，最终一行 `*** PASS` 或 `*** FAIL`，进程返回码即结论。

**预期结果**：你能指出两条路径「输入相同（`sanity0/input.txn`）、判定机制不同（日志扫描 vs 返回码）、外围实现语言不同（SV vs C++）」。

> ⚠️ 两条路径的实际执行都依赖本环境未安装的工具（VCS / Verilator），**待本地验证**。本环境**可验证**的是 4.2.4 的 trace 转换（仅需 perl）。

#### 4.4.5 小练习与答案

**练习 1**：如果只想在 CI 里对每个 trace 做快速功能回归，该选哪条路径？为什么？

> **答案**：Verilator 路径。它是开源的（CI 无需 license）、编译后执行快、进程返回码便于 CI 判定成功/失败。代价是失去精确时序，但功能回归正是其强项。

**练习 2**：同一个 trace 在 Verilator 跑通了，能否保证在 VCS 也一定跑通？为什么？

> **答案**：不能。Verilator 用单时钟、简化了 CSB 轮询语义、存储模型延迟固定，许多与时序、跨时钟域、带宽竞争相关的问题在 Verilator 里暴露不出来。Verilator 通过只能说明「功能逻辑大致正确」，时序与协议细节仍需 VCS 验证。

## 5. 综合实践

**任务**：以 `sanity0` trace 为例，完整追踪一次「读 BDMA 寄存器→写魔数→读回校验」的过程，在两条路径下分别说明每个环节由谁完成，最后用一张大表把两条路径并排对照。

`sanity0/input.txn` 的内容（已确认）是：

```
read_reg 0xffff100b 0xffffffe0 0x00000000   # 读 BDMA.CFG_DST_SURF_0 默认值
write_reg 0xffff100b 0xf0a5a500             # 写魔数 0xf0a5a500
read_reg 0xffff100b 0xffffffe0 0xf0a5a500   # 读回，期望（掩码后）等于魔数
```

请你完成：

1. **Verilator 路径追踪**：说明这条 trace 经 `input_txn_to_verilator.pl` 变成 `trace.bin` 后（操作码序列应是 `03 ... 02 ... 03 ... FF`），由 `TraceLoader::load` 解析、`CSBMaster` 逐条驱动 `csb2nvdla_*`、最后一条 `read_reg` 在 [nvdla.cpp:L111-L124](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/verilator/nvdla.cpp#L111-L124) 做 `(data & 0xffffffe0) == (0xf0a5a500 & 0xffffffe0)` 的掩码比对。
2. **VCS 路径追踪**：说明同一条 trace 经 `inp_txn_to_hexdump.pl` 变成 `.raw`，由 `csb_master_seq.v` 用 `$readmemh` 装入、按比较模式回放（见 [u7-l2](u7-l2-csb-sequence-trace.md)），最终结果由 `checktest_synthtb.pl` 扫描 `test.log` 判定。
3. **动手**：执行 4.2.4 的 perl 转换（本环境可做），用 `xxd` 验证 `trace.bin` 第一个操作码确实是 `03`、末尾是 `ff`。
4. **产出**：写一张表，对「激励生成、CSB 驱动、读校验、判 PASS/FAIL」四个环节，分别列出两条路径的负责组件。

**预期结果**：你能清楚说明——同一个 BDMA 寄存器读写，在两条路径下「逻辑等价、实现不同」，并理解为何 Verilator 适合快速功能冒烟、VCS 适合精确验证。

## 6. 本讲小结

- NVDLA 提供两条等价的仿真路径：`verif/sim`（VCS + SystemVerilog 测试平台）与 `verif/verilator`（Verilator + C++ 驱动），二者喂同一份 `input.txn`、跑同一个 `NV_nvdla`。
- Verilator 路径的编译分两步：Verilator 前端按 `verilator.f` 把 outdir 中已生成的 RTL 翻译成 C++（`VNV_nvdla.mk`），clang 后端再连同 `nvdla.cpp` 编出可执行程序 `VNV_nvdla`。
- `input_txn_to_verilator.pl` 把人读的 `input.txn` 压成二进制 `trace.bin`（操作码 + 小端 32 位字段 + 内嵌 `.dat` 数据 + `0xFF` 结束符），简化掉了 VCS 路径的比较模式。
- `nvdla.cpp` 用三大 C++ 类替代整套 SV 测试平台：`CSBMaster`（CSB 握手 + 掩码相等比对）、`AXIResponder`（DBB/CVSRAM 两个 AXI slave，稀疏 map 存储）、`TraceLoader`（解析 trace.bin 并分派）。
- `main()` 用「单时钟同相翻转」简化了双时钟域，靠 `dump_mem` 逐字节比对判输出正确性，靠 CSB 读掩码比对判配置正确性，返回码 0/1/2 表 PASS/输出错/CSB 错。
- Verilator 路径轻量、开源、快，适合 CI 功能回归；VCS 路径精确、可调，适合时序与协议验证——二者互补。

## 7. 下一步学习建议

- **回到 C-model**：本讲与 [u7-l3](u7-l3-cmodel-reference.md) 的 C-model 都属于「不依赖逐拍时序的快速模型」。建议对比 Verilator（编译后的 RTL，位精确但单时钟）与 C-model（SystemC 事务级，更快但非位精确时序）的异同，理解它们在验证体系里分别扮演的角色。
- **端到端编程**：现在你已掌握「trace 如何驱动 DUT」，可以进入 [u8-l4](u8-l4-end-to-end-integration.md)，学习如何编排一个完整网络层的寄存器序列（BDMA 搬数据→配置卷积→配置 SDP→kick-off→等中断），并尝试自己写一段最小 trace 用 Verilator 跑通。
- **深入存储模型**：若对 `AXIResponder` 的简化存疑，可对照 [u7-l1](u7-l1-traceplayer-testbench.md) 的 `axi_slave.v`/`memory.v`，看 VCS 路径如何用带宽节流与多 outstanding FIFO 做更真实的存储建模。
- **构建系统串联**：结合 [u1-l3](u1-l3-build-system-toolchain.md)，理解为何 Verilator 沙箱在 `build.config` 里只依赖 `vmod_nvdla_top`，以及 `tmake` 如何保证「RTL 先生成、Verilator 后编译」的顺序。
