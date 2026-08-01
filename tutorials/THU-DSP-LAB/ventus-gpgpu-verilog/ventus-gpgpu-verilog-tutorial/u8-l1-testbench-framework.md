# 仿真测试框架与 testbench

## 1. 本讲目标

学完本讲后，你应当能够：

- 说清 Ventus GPGPU（Verilog 版）AXI 仿真平台的「五大组件」分别是谁、各管什么。
- 看懂一次仿真从上电复位、预加载内存、派发 workgroup、等待执行完成，到最终比对结果打印 `PASSED`/`FAILED` 的完整时序。
- 区分两个容易混淆的职责：**谁把 kernel/数据写进内存**（不是 `host_inter`，而是 `tc.v` 里的 `init_mem`），**谁驱动主机口派发 workgroup**（才是 `host_inter`）。
- 理解 `axi_ram` 这个 AXI4 从端内存模型如何用状态机响应读写，以及它为何能被「正常访存」和「force 预加载」两种方式同时使用。
- 能够独立打开一个测试用例，在源码里定位到「加载程序、发起首个 workgroup、检测退出条件」这三段代码。

本讲是单元 8（工程实践）的第一篇，承接 u1-l4（仿真环境搭建与用例运行）——那里讲了「怎么敲 `make run-vcs-4w8t` 跑起来」，本讲回答「这一声 make 背后，testbench 到底做了什么」。

## 2. 前置知识

在进入源码前，先用三段白话建立直觉。

**什么是 testbench？** 被测芯片 RTL（DUT，Design Under Test）本身是被动的——它需要有时钟、有复位、有人喂给它输入、有地方放它要读写的存储。testbench 就是把这些「外部世界」用 Verilog 搭出来的一个壳：产生时钟复位、扮演主机 CPU、扮演外部 DRAM、并在跑完后检查结果对不对。它只存在于仿真，不会被综合成真实电路。

**AXI4 与 AXI4-Lite 是什么？** 它们是 ARM 定义的片上总线协议。AXI4 是完整版（有 burst 突发、独立 5 通道），AXI4-Lite 是精简版（每笔单拍、无突发）。Ventus 里：主机 CPU 用 **AXI4-Lite** 慢速配置寄存器、派发任务；GPU 对外读写 DRAM 用 **AXI4** 高速搬运数据。u7-l4 已讲过 DUT 侧的转换，本讲关注 testbench 侧怎么「扮演」这两端的主/从。

**`$readmemh`、`force`/`release`、hierarchical 引用是什么？** 这是 Verilog 仿真专用的系统任务与构造：`$readmemh("文件", 数组)` 把一个十六进制文本文件按行读进一个数组；`force 信号=值` 强行覆盖某信号（`release` 解除），常用于绕过正常协议直接「塞」数据；`test_gpu_axi_top.u_host_inter.drv_gpu(...)` 这种带点号的全路径叫 hierarchical（层次化）引用，让一个模块能直接调用另一个模块里定义的 `task`。理解这三个，本讲的代码就懂了一大半。

## 3. 本讲源码地图

本讲围绕 `testcase/test_gpgpu_axi_top/common/` 下的平台文件，以及每个用例私有的 `tc.v`：

| 文件 | 角色 | 关键内容 |
| --- | --- | --- |
| `test_gpu_axi_top.sv` | 仿真顶层 | 例化 DUT、时钟/复位、host_inter、axi_ram、tc；定义波形 dump 与 `PASSED`/`FAILED` 打印 |
| `host_inter.sv` | 主机驱动（AXI4-Lite Master） | 读 metadata、配置 host 寄存器、触发派发、轮询完成 |
| `axi_ram.sv` | 外部存储模型（AXI4 Slave） | reg 数组 + 读写状态机；提供 `display_mem`/`store_mem` 读回结果 |
| `file_list.f` | 文件清单 | 列出 6 个 testbench 文件，其中 `./tc.v` 是用例私有 |
| `<用例>/tc.v` | 测试控制（每用例一份） | 串起整个流程：选数据、预加载内存、启动、等完、比对结果 |

> 提醒：`source_files` 里只列了前四个，但 `file_list.f` 的最后一行 `./tc.v` 把用例控制文件也拉进了编译，而「预加载内存」和「`PASSED`/`FAILED` 判定」恰恰都在 `tc.v` 里。所以本讲第 4.4 节会把 `tc.v` 与 `file_list.f` 一起讲，否则无法回答实践任务里的问题。

## 4. 核心概念与源码讲解

### 4.1 test_gpu_axi_top —— 仿真平台顶层

#### 4.1.1 概念说明

`test_gpu_axi_top` 是整个仿真的最顶层模块。它自身**不做任何运算**，只做三件事：声明连接信号、例化五个子部件、定义两个纯打印 task。可以把它理解成一块「主板」——CPU、内存、GPU 都插在它上面，靠它布线连通。

它例化的五个部件是：

1. `gpgpu_axi_top u_dut` —— 被测对象（DUT），即打包成 AXI IP 的整颗 GPU。
2. `gen_clk u_gen_clk` —— 时钟发生器。
3. `gen_rst u_gen_rst` —— 复位发生器。
4. `host_inter u_host_inter` —— 扮演主机 CPU 的驱动器。
5. `axi_ram u_ram` —— 扮演外部 DRAM 的内存模型。
6. `tc u_tc()` —— 测试流程控制器（无端口，靠层次化引用驱动其他模块）。

#### 4.1.2 核心流程

平台启动后的时序如下：

```text
gen_clk  ──> 产生 10ns 周期 clk（100MHz），永久翻转
gen_rst  ──> rst_n 保持 0 共 2 个上升沿，之后拉 1（释放复位）
            └─> 所有 DUT 寄存器在这 2 拍内被复位
host_inter / tc 的 initial 块 ──> 在复位释放后开始驱动
DUT 运行 ──> 经 AXI4 读写 axi_ram（取指 + 访存）
结束 ──> tc 比对结果，调用 PASSED 或 FAILED task 打印 ASCII 横幅
$finish 退出
```

关键点：时钟周期 `PERIOD=10.0`（见 [gen_clk.v:8](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/testcase/test_gpgpu_axi_top/common/gen_clk.v#L8)），所以 `kernel_cycles` 的换算里到处出现「除以 10」（一个周期 = 10ns，见 4.2 节）。

#### 4.1.3 源码精读

**信号声明**分两组：`s_axilite_*` 是连到 `host_inter` 的 AXI4-Lite 口，`m_axi_*` 是连到 `axi_ram` 的 AXI4 口。顶层只是把它们声明成 `wire`/`reg`，再连到对应模块上：[test_gpu_axi_top.sv:6-81](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/testcase/test_gpgpu_axi_top/common/test_gpu_axi_top.sv#L6-L81)（声明两组总线信号）。

**例化 DUT**：把上面两组信号连到 `gpgpu_axi_top` 上——`s_axilite_*` 接它的主机从端，`m_axi_*` 接它的对外主端：[test_gpu_axi_top.sv:83-160](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/testcase/test_gpgpu_axi_top/common/test_gpu_axi_top.sv#L83-L160)（例化 `u_dut`，注意端口按 AXI4 五通道 AW/W/B/AR/R 顺序一一对应）。

**例化时钟与复位**：[test_gpu_axi_top.sv:162-169](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/testcase/test_gpgpu_axi_top/common/test_gpu_axi_top.sv#L162-L169)（`gen_clk` 出 `clk`，`gen_rst` 吃 `clk` 出 `rst_n`）。

**例化主机驱动 `host_inter`**：把 `s_axilite_*` 双向信号连过去，于是 `host_inter` 就成了 AXI4-Lite 总线的主机：[test_gpu_axi_top.sv:171-197](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/testcase/test_gpgpu_axi_top/common/test_gpu_axi_top.sv#L171-L197)。

**例化内存模型 `axi_ram`**：注意它带参数例化 `DATA_WIDTH=64, ADDR_WIDTH=32, ID_WIDTH=4`，复位取反 `~rst_n`（axi_ram 用高有效 `rst`）：[test_gpu_axi_top.sv:199-242](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/testcase/test_gpgpu_axi_top/common/test_gpu_axi_top.sv#L199-L242)。

**例化测试控制器 `tc`**：无端口例化 `tc u_tc();`，它完全靠层次化路径驱动其他模块：[test_gpu_axi_top.sv:244](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/testcase/test_gpgpu_axi_top/common/test_gpu_axi_top.sv#L244)。

**波形 dump**：仿真开始即把整个 `test_gpu_axi_top` 层次（`+all` 含内部、`+mda` 含多维数组/内存）dump 进 `test.fsdb`，供 Verdi 查看：[test_gpu_axi_top.sv:246-249](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/testcase/test_gpgpu_axi_top/common/test_gpu_axi_top.sv#L246-L249)。

**`PASSED`/`FAILED` task**：这两个 task 只是打印一段字符画横幅，本身**不做任何判定**——判定逻辑在 `tc.v` 里（见 4.4 节），判定完才调用它们：[test_gpu_axi_top.sv:251-273](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/testcase/test_gpgpu_axi_top/common/test_gpu_axi_top.sv#L251-L273)。

#### 4.1.4 代码实践

1. **实践目标**：建立平台拓扑直觉。
2. **操作步骤**：打开 [test_gpu_axi_top.sv:83-244](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/testcase/test_gpgpu_axi_top/common/test_gpu_axi_top.sv#L83-L244)，画出五个例化块（u_dut / u_gen_clk / u_gen_rst / u_host_inter / u_ram）及其连线。
3. **需要观察的现象**：`s_axilite_*` 信号同时连到 `u_dut`（作为从端输入）和 `u_host_inter`（作为主端输出）；`m_axi_*` 信号同时连到 `u_dut`（主端输出）和 `u_ram`（从端输入）。
4. **预期结果**：你会得到一张「host_inter ──AXI4-Lite──> DUT ──AXI4──> axi_ram」的链路图，DUT 夹在两种总线之间。
5. 待本地验证：若环境有 Verdi，可在波形里确认 `clk` 每 5ns 翻转一次、`rst_n` 在第 3 个上升沿才变 1。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `axi_ram` 的复位端口接的是 `~rst_n` 而不是 `rst_n`？
**答案**：`gen_rst` 输出的是低有效复位 `rst_n`（复位时为 0），而 `axi_ram` 模块内部用的是高有效 `rst`（见 [axi_ram.sv:265](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/testcase/test_gpgpu_axi_top/common/axi_ram.sv#L265) 的 `if (rst)`），所以顶层取反 `~rst_n` 把极性对齐。

**练习 2**：`PASSED`/`FAILED` task 定义在顶层，但它们怎么知道该打印哪一个？
**答案**：它们自己不知道。它们只是打印工具，被 `tc.v` 的 `print_result` 在比对结果后「按需调用」（见 4.4.3）。真正的判定（`if (&sum_32_pass)`）在 `tc.v` 里。

---

### 4.2 host_inter —— 主机驱动（AXI4-Lite Master）

#### 4.2.1 概念说明

`host_inter` 模拟主机 CPU，是一个 **AXI4-Lite 主端**。它的全部工作可以浓缩成一句话：**「读 metadata 文件 → 把 workgroup 参数逐个写进 DUT 的主机寄存器 → 拉一下 valid 启动 → 等执行完」**。

⚠️ 一个常见误解需要先澄清：**`host_inter` 并不把 kernel 代码或数据写进 DRAM**。它只读 `.metadata` 文件、配置寄存器、启动派发。真正把 kernel/数据预加载进 `axi_ram` 的是 `tc.v` 的 `init_mem`（见 4.4 节）。之所以容易混淆，是因为两者都「读文件」，但读的是不同的文件、写到不同的地方：`host_inter` 读 `.metadata` 写「寄存器」；`init_mem` 读 `.data` 写「内存」。

它对外暴露 5 个 task：`drv_gpu`（配置并启动）、`axilite_write`/`axilite_read`（底层总线读写）、`exe_finish`（等完成并计周期）、`get_result_addr`（解析结果地址）。

#### 4.2.2 核心流程

`drv_gpu` 的执行流程（这是「发起首个 workgroup」的核心）：

```text
1. $readmemh(fn_metadata, metadata)         // 把 .metadata 读进数组
2. 解析 64 位字段（每字段 = {metadata[i+1], metadata[i]}，小端拼接）
   wf_size, wg_size, vgprUsage, sgprUsage, pdsBaseAddr, metaDataBaseAddr ...
3. 逐个 axilite_write(地址, 值) 配置 reg[1]~reg[15]：
   0x04 wg_id | 0x08 num_wf | 0x0c wf_size | 0x10 start_pc(固定 0x8000_0000)
   0x14 vgpr_total | 0x18 sgpr_total | 0x1c lds_total | 0x20 vgpr_per_wf
   0x24 sgpr_per_wf | 0x28 gds_base | 0x2c pds_base | 0x30 csr_knl | 0x34/38/3c kernel_size_3d
4. axilite_write(0x00, 1)                   // 写 reg[0]=1，触发 host_req_valid
5. wait(cta2host_rcvd_ack_o 的下降沿)        // 等 DUT 确认已收到这次派发
6. 记录起始时间 cycle_count[0]
```

`exe_finish` 的流程（这是「检测完成」的核心）：

```text
while(未完成):
    wait(s_axilite_rvalid_o 为低)            // 避免读到上次的残留
    axilite_read(0x44, r_data)               // 回读 reg[17] = host_rsp_valid
    若 r_data != 0  →  完成，跳出
记录结束时间 cycle_count[1]
kernel_cycles = (cycle_count[1] - cycle_count[0]) / 10   // ns 换算成周期
```

#### 4.2.3 源码精读

**寄存器配置 + 触发 valid**：从读 metadata 到写满寄存器、最后写 `reg[0]=1` 的全过程，注意第 160 行的 `axilite_write(32'h0000_0000,32'd1)` 就是「扣下扳机」的一句：[host_inter.sv:104-161](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/testcase/test_gpgpu_axi_top/common/host_inter.sv#L104-L161)（`drv_gpu` 主体：`$readmemh` 读 metadata、按小端解析字段、逐寄存器配置、第 160 行触发 host_req_valid、第 161 行等 DUT 回 ack）。

**`axilite_write`（AXI4-Lite 写）**：用 `fork...join` 并发驱动 AW（写地址）与 W（写数据）两个通道，各自 `wait` 对应的 `ready` 后撤掉 `valid`，体现 AXI「地址与数据通道独立握手」的特点：[host_inter.sv:172-195](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/testcase/test_gpgpu_axi_top/common/host_inter.sv#L172-L195)。

**`axilite_read`（AXI4-Lite 读）**：驱动 AR（读地址）通道、等 `arready`，再等 R（读数据）通道的 `rvalid`，最后采样 `rdata`：[host_inter.sv:197-212](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/testcase/test_gpgpu_axi_top/common/host_inter.sv#L197-L212)。

**`exe_finish`（轮询完成）**：循环回读 `0x44` 直到非零，记录周期数。注意第 223 行 `wait(!s_axilite_rvalid_o)` 是为避免读到上一拍残留的读响应：[host_inter.sv:214-246](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/testcase/test_gpgpu_axi_top/common/host_inter.sv#L214-L246)（轮询 `reg[0x44]`、第 238 行把 ns 差除以 10 得到 `kernel_cycles`）。

> 寄存器映射小结（与 u7-l4 一致）：`reg[0]@0x00`=`host_req_valid`（触发），`reg[2]@0x08`=`num_wf`，`reg[4]@0x10`=`start_pc`，`reg[12]@0x30`=`csr_knl`，`reg[17]@0x44`=`host_rsp_valid`（完成回读）。

#### 4.2.4 代码实践

1. **实践目标**：搞清「一个 workgroup 参数如何从文件流到寄存器」。
2. **操作步骤**：对照 [host_inter.sv:113-160](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/testcase/test_gpgpu_axi_top/common/host_inter.sv#L113-L160)，挑一个字段（如 `wf_size`）追全程：它在 `.metadata` 的第 10/11 字 → 第 118 行 `{metadata[11],metadata[10]}` 拼成 64 位 → 第 134 行 `axilite_write(0x0c, wf_size[31:0])` 写进 `reg[3]`。
3. **需要观察的现象**：`start_pc` 在第 136 行被**硬编码**为 `0x8000_0000`，而不是从 metadata 取（`noused` 读了但没用）。
4. **预期结果**：你能列出一张「metadata 偏移 → 字段名 → 寄存器地址」对照表，并解释为何 `vgpr_size_total = wg_size * vgprUsage`（总量 = 每 warp 用量 × warp 数）。
5. 待本地验证：若跑过仿真，可在 `simv.log` 里看到 `Config finish! time: ... ns`，它对应第 165 行的打印、即 `cycle_count[0]` 采样点。

#### 4.2.5 小练习与答案

**练习 1**：`drv_gpu` 里 `vgpr_size_total` 写的是 `wg_size*vgprUsage`，而 `vgpr_size_per_wf` 写的是 `vgprUsage`。这两个寄存器分别给谁用？
**答案**：`_total` 是整个 workgroup 的 VGPR 总需求，供 CTA 调度器的资源表（allocator）判断「这个 SM 放不放得下」（见 u2-l1）；`_per_wf` 是单 warp 的 VGPR 用量，派发后用于为每个 warp 计算 VGPR 基址偏移（见 u2-l2「逐 warp 递增基址」）。

**练习 2**：`exe_finish` 为什么用「轮询读 `0x44`」而不是等一个中断信号？
**答案**：因为 AXI4-Lite 是「主机主动发起读写」的协议，从端（DUT）不能主动通知主机。DUT 把完成标志写进 `reg[17]`，主机只能反复读它直到变非零——这正是 `while(i==0)` 轮询的原因。

---

### 4.3 axi_ram —— AXI4 内存模型

#### 4.3.1 概念说明

`axi_ram` 是一个行为级（behavioral）的 **AXI4 从端内存模型**，用来替代真实 DRAM。它的核心就是一个大数组 `mem`，外加两套状态机（读、写）把 AXI4 五通道握手翻译成对 `mem` 的读写。

它在仿真里身兼两职，这是理解它的关键：

- **运行期**：作为 DUT 的「远方存储」——DUT 取指、访存的 AXI4 读写都打到它这里，它按状态机正常响应。
- **预加载期**：它的内部信号被 `tc.v` 用 `force` 直接驱动，绕过状态机把 `.data` 内容批量灌进 `mem`（见 4.4）。这两件事之所以能并存，是因为 `force` 只在仿真开始、DUT 还没发起访存时短暂使用，之后 `release` 即恢复正常。

它还提供两个 task 给结果比对用：`display_mem`（打印某地址内容）和 `store_mem`（把一段地址的内容拷到 `mem_tmp_1/2` 数组供逐字比较）。

#### 4.3.2 核心流程

**写状态机**（3 态）：

```text
IDLE  ──awvalid&awready──> 锁存 id/addr/len/size/burst，进 BURST，拉 wready
BURST ──每拍 wvalid&wready──> 按 wstrb 逐字节写 mem，地址步进，count--
        count 归零 ──> 若主机 bready 则发 bvalid 回 IDLE，否则进 RESP
RESP  ──等 bready──> 发 bvalid，回 IDLE
```

**读状态机**（2 态）：

```text
IDLE  ──arvalid&arready──> 锁存 id/addr/len/size/burst，进 BURST
BURST ──每拍──> 从 mem[addr] 读，发 rvalid+rdata，地址步进，count--
        count 归零(rlast) ─> 回 IDLE 并重新拉 arready
```

**地址换算**：AXI 地址是字节地址（32 位），但 `mem` 按 `DATA_WIDTH=64` 位（8 字节）为一个表项。所以有效地址 = 字节地址右移 `log2(8)=3` 位，得到 `mem` 的索引（`VALID_ADDR_WIDTH = ADDR_WIDTH - $clog2(STRB_WIDTH)`）。

#### 4.3.3 源码精读

**存储数组与地址换算**：[axi_ram.sv:136](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/testcase/test_gpgpu_axi_top/common/axi_ram.sv#L136)（`reg [DATA_WIDTH-1:0] mem [...]` 数组，是整个内存模型的核心存储）；[axi_ram.sv:139-142](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/testcase/test_gpgpu_axi_top/common/axi_ram.sv#L139-L142)（把字节地址右移成 `mem` 索引的 `*_addr_valid`）。

**写状态机（组合部分）**：[axi_ram.sv:188-242](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/testcase/test_gpgpu_axi_top/common/axi_ram.sv#L188-L242)（IDLE→BURST→RESP，第 211 行按 `write_burst_reg != 0` 决定是否地址步进）；写时的实际写存储动作在时序块 [axi_ram.sv:259-263](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/testcase/test_gpgpu_axi_top/common/axi_ram.sv#L259-L263)（按 `wstrb` 逐字节写入 `mem`）。

**读状态机**：[axi_ram.sv:291-328](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/testcase/test_gpgpu_axi_top/common/axi_ram.sv#L291-L328)（IDLE→BURST）；读时把 `mem` 内容锁进 `rdata` 在 [axi_ram.sv:345-347](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/testcase/test_gpgpu_axi_top/common/axi_ram.sv#L345-L347)。

**`store_mem` task（结果回读）**：把一段 `mem` 内容拆成 32 位字拷进 `mem_tmp_1`/`mem_tmp_2`，供 `print_result` 逐字与 golden 比较：[axi_ram.sv:383-412](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/testcase/test_gpgpu_axi_top/common/axi_ram.sv#L383-L412)。

#### 4.3.4 代码实践

1. **实践目标**：把 axi_ram 与 D-cache 缺失（u6-l1）串起来。
2. **操作步骤**：沿「D-cache miss → 经 L2、axi4_adapter 发 AXI4 AR → axi_ram 读状态机响应 R → 数据逐级回到 LSU」这条链，在 [axi_ram.sv:291-328](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/testcase/test_gpgpu_axi_top/common/axi_ram.sv#L291-L328) 找到 axi_ram 这一环。
3. **需要观察的现象**：axi_ram 的 `s_axi_rresp` 恒为 `2'b00`（OKAY，见第 155 行）、`s_axi_bresp` 恒为 `2'b00`（第 150 行）——它永远成功，不模拟错误。
4. **预期结果**：理解 axi_ram 只是一个「无错的、按 burst 顺序返回数据」的理想 DRAM，时序上近似零延迟（除握手本身）。
5. 待本地验证：在波形里抓 DUT 的 `m_axi_arvalid_o` 与 axi_ram 的 `s_axi_rvalid`，观察一次 burst 读的握手时序。

#### 4.3.5 小练习与答案

**练习 1**：`mem` 数组的深度是 `2**VALID_ADDR_WIDTH`，其中 `VALID_ADDR_WIDTH = ADDR_WIDTH - $clog2(STRB_WIDTH)`。给定 `ADDR_WIDTH=32, DATA_WIDTH=64`，这个内存有多大？
**答案**：`STRB_WIDTH=64/8=8`，`$clog2(8)=3`，`VALID_ADDR_WIDTH=29`，深度 `2^29` 项，每项 64 位 → `2^29 × 8 B = 4 GB`。当然这只是行为模型的地址空间上限，仿真里实际只用了很少一部分（kernel 与几个 buffer）。

**练习 2**：为什么写状态机里要判断 `write_burst_reg != 2'b00` 才地址步进？
**答案**：`awburst=2'b00` 是 AXI 的 FIXED 突发类型（每次地址不变），只有 INCR（`2'b01`）等类型才需要每次地址递增。axi_ram 据此正确处理不同突发模式。

---

### 4.4 file_list.f 与 tc.v —— 文件组织与测试控制

#### 4.4.1 概念说明

这一节把「平台如何被组织」与「流程如何被驱动」一起讲，因为二者紧密耦合。

**`file_list.f`** 是 testbench 的文件清单，只 6 行。它的精妙在于：5 个平台文件用相对 `common/` 的路径写死，可被所有用例共享；唯独最后一行 `./tc.v` 是「当前用例私有」——每个用例目录（如 `tc_vecadd/`、`tc_gaussian/`）下都有自己的 `tc.v`，编译时由该用例目录发起，`./tc.v` 就指到它。这样「换用例」=「换 `tc.v`」，平台代码零修改。

**`tc.v`** 是测试流程的「总指挥」。它本身没有任何硬件端口（`module tc;`），完全靠**层次化引用**调用 `host_inter` 和 `axi_ram` 里的 task。它的开头先用一堆 `` `define `` 给这些长路径起别名，例如 `` `define drv_gpu u_host_inter.drv_gpu ``，之后代码里写 `` `drv_gpu(...) `` 等于 `test_gpu_axi_top.u_host_inter.drv_gpu(...)`。

`tc.v` 定义了四个关键 task，对应实践任务要找的三个点：

- `init_test_file`：按 `CASE_xWyT` 宏选 `.metadata`/`.data` 文件名。
- `init_mem`：**预加载内存**（实践任务的「AXI RAM 如何被预加载」）。
- `test_main`：串流程，其中调用 `drv_gpu`（**发起首个 workgroup**）。
- `print_result`：读回结果与 golden 比较，调 `PASSED`/`FAILED`（**检测退出条件**）。

#### 4.4.2 核心流程

`tc.v` 顶层 `initial` 的总流程：

```text
repeat(100) @(posedge clk)        // 等 100 拍，让复位彻底稳定
init_test_file()                  // 选数据文件
test_main()                       // 跑测试
repeat(100) @(posedge clk)        // 收尾
$finish()
```

`test_main` 对每个用例文件（`FILE_NUM` 个）循环：

```text
force u_dut.l2_2_mem.m_axi_bvalid_i = 0   // 屏蔽 DUT 的写响应，避免干扰预加载
init_mem(meta, data)                       // ★ 用 force 把 .data 灌进 axi_ram
release u_dut.l2_2_mem.m_axi_bvalid_i     // 解除屏蔽
`drv_gpu(meta, data)                       // ★ 配置寄存器并写 reg[0]=1，发起首个 workgroup
`get_result_addr(meta, data)               // 解析结果缓冲的基址/大小
`exe_finish(meta, data)                    // ★ 轮询 0x44 等执行完成
sum_cycles += kernel_cycles
`print_result()                            // ★ 比对结果，打印 PASSED/FAILED
```

**`init_mem`（预加载）的机制**——这是本节最该记住的一段：

```text
1. $readmemh(fn_data, data)                 // 把 .data 十六进制读进 data[] 数组
   $readmemh(fn_metadata, metadata)         // 读 metadata 拿到 buffer 基址/大小
2. 解析每个 buffer 的 base addr(buf_ba_w) 与 size
3. 对每个 buffer，按「16 字为一组 burst」用 force 直接驱动 axi_ram 的 AXI4 写口：
   force u_ram.s_axi_awvalid = 1
   force u_ram.s_axi_awaddr  = buf_ba_w     // 写地址 = buffer 基址
   force u_ram.s_axi_awlen   = 0x0f (15)    // 16-beat burst
   ... 等 awready，再逐 beat force wdata/wstrb/wlast，把 data[] 依次写进去 ...
4. release 所有 force 信号                   // 恢复 axi_ram 自身驱动
```

所以「AXI RAM 如何被预加载 kernel 与数据」的答案是：**`tc.v` 的 `init_mem` 读 `.data` 文件，用 `force` 直接驱动 `axi_ram` 的 AXI4 写端口，以 burst 方式把内容写进 `mem` 数组；写完 `release`，之后 DUT 的正常访存就能读到这些预置的 kernel 指令和数据。**

#### 4.4.3 源码精读

**`file_list.f` 全文**：[file_list.f:1-6](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/testcase/test_gpgpu_axi_top/common/file_list.f#L1-L6)（注意第 6 行 `./tc.v` 是唯一的用例私有文件）。

**`tc.v` 的层次化别名**：[tc_vecadd/tc.v:2-12](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/testcase/test_gpgpu_axi_top/tc_vecadd/tc.v#L2-L12)（把 `u_host_inter.drv_gpu`、`u_ram.store_mem` 等长路径起短别名，这是 `tc` 能「遥控」其他模块的钥匙）。

**顶层流程**：[tc_vecadd/tc.v:43-51](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/testcase/test_gpgpu_axi_top/tc_vecadd/tc.v#L43-L51)（等 100 拍 → `init_test_file` → `test_main` → 等 100 拍 → `$finish`）。

**`test_main`（串起三件大事）**：[tc_vecadd/tc.v:83-103](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/testcase/test_gpgpu_axi_top/tc_vecadd/tc.v#L83-L103)（第 88 行 `init_mem` 预加载、第 90 行 `drv_gpu` 发起 workgroup、第 94 行 `exe_finish` 等完成、第 97 行 `print_result` 比对）。

**`init_mem` 读文件 + force 预加载**：[tc_vecadd/tc.v:123-124](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/testcase/test_gpgpu_axi_top/tc_vecadd/tc.v#L123-L124)（`$readmemh` 读 `.data` 与 `.metadata`）；核心的 force 驱动 burst 写循环在 [tc_vecadd/tc.v:153-207](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/testcase/test_gpgpu_axi_top/tc_vecadd/tc.v#L153-L207)（逐 buffer、逐 burst 地 force `awvalid/awaddr/awlen` 与 `wdata/wstrb/wlast` 把数据塞进 `u_ram`）。注意第 87 行先 `force ... m_axi_bvalid_i = 0` 把 DUT 的写响应通道静音，防止预加载期间 DUT 的残留事务与 force 冲突。

**`print_result` 比对与 PASSED/FAILED**：先 `store_mem` 把结果从 `axi_ram` 拷到 `mem_tmp_1`，再逐字与 golden 比较：[tc_vecadd/tc.v:240](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/testcase/test_gpgpu_axi_top/tc_vecadd/tc.v#L240)（`store_mem` 回读结果）；比较与判定在 [tc_vecadd/tc.v:242-322](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/testcase/test_gpgpu_axi_top/tc_vecadd/tc.v#L242-L322)（按 `CASE_xWyT` 分支，把每个 32 位结果与期望值比，全对才 `PASSED`，任一错即 `FAILED`）。vecadd 的期望值是浮点 `32'h42000000`（即 32.0）等，见第 244 行。

#### 4.4.4 代码实践（本讲主实践）

> 实践任务原文：在 testbench 中找到 host_inter 加载程序、发起首个 workgroup、以及检测 PASSED/FAILED 退出条件的代码，说明 AXI RAM 如何被预加载 kernel 与数据。

1. **实践目标**：把「加载 → 启动 → 等完 → 判定」四段代码在源码里逐一指认出来，并纠正「host_inter 加载程序」的措辞。
2. **操作步骤**：
   - **加载程序/数据（预加载）**：打开 [tc_vecadd/tc.v:105-209](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/testcase/test_gpgpu_axi_top/tc_vecadd/tc.v#L105-L209)，确认它是 `init_mem`、用 `$readmemh` + `force` 把 `.data` 写进 `u_ram`。这就是真正的「加载」，**它在 `tc.v` 而非 `host_inter`**。
   - **发起首个 workgroup**：`init_mem` 之后，`test_main` 第 90 行调用 `` `drv_gpu ``，进入 [host_inter.sv:104-161](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/testcase/test_gpgpu_axi_top/common/host_inter.sv#L104-L161)，确认第 160 行 `axilite_write(0x00,1)` 是「扣扳机」。
   - **检测完成与 PASSED/FAILED**：等完成在 [host_inter.sv:214-246](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/testcase/test_gpgpu_axi_top/common/host_inter.sv#L214-L246)（`exe_finish` 轮询 `0x44`）；判定在 [tc_vecadd/tc.v:242-322](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/testcase/test_gpgpu_axi_top/tc_vecadd/tc.v#L242-L322)（`print_result` 比较 + 调 `PASSED`/`FAILED`）。
3. **需要观察的现象**：预加载用 `force`/`release`（第 154-205 行），而正常派发用 `axilite_write`（host_inter）——两种完全不同的「写」机制。`force` 绕过协议直接捅 `axi_ram` 内部，`axilite_write` 则严格走 AXI4-Lite 握手。
4. **预期结果**：你能画出 `init_mem($readmemh→force) → drv_gpu(配寄存器→reg[0]=1) → DUT 运行(读写 axi_ram) → exe_finish(轮询0x44) → print_result(store_mem→比较→PASSED/FAILED)` 的完整时序图，并指出「加载」与「启动」是两个不同模块干的。
5. 待本地验证：跑 `make run-vcs-4w8t`，在 `simv.log` 里依次找到 `Begin test`（drv_gpu 第 106 行）、`Config finish!`（第 165 行）、`exe finish!`（exe_finish 第 234 行）、最后的 `PASSED`/`FAILED` 横幅，对照时间戳验证顺序。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `init_mem` 用 `force` 直接驱动 `axi_ram`，而不像 `drv_gpu` 那样走正常的 AXI 写握手？
**答案**：因为预加载发生在 DUT 还没启动、整个 AXI 通路（DUT 主端 → axi_ram 从端）尚未「运转」时；用 `force` 直接驱动从端输入，可以不依赖 DUT、不触发 DUT 的访存逻辑，快速把大量初始数据灌入内存。`release` 后 DUT 才开始正常读写。

**练习 2**：`tc.v` 里第 87 行 `force u_dut.l2_2_mem.m_axi_bvalid_i = 1'd0` 的作用是什么？为什么预加载后要 `release`？
**答案**：`l2_2_mem` 是 DUT 对外 AXI 口的适配实例（见 u7-l4）。预加载期间强制其写响应 `bvalid=0`，是为了屏蔽 DUT 可能发出的残留/无意义写响应，防止它们干扰 `init_mem` 对 `u_ram` 的 force 写时序；预加载完成后 `release`，让 DUT 的真实事务恢复正常握手。

**练习 3**：换一个用例（如 `tc_gaussian`）时，平台文件（`common/` 下的 5 个）需要改吗？
**答案**：不需要。`file_list.f` 里前 5 个文件路径写死、共享；只有 `./tc.v` 随用例目录变化。所以换用例只需进入对应目录、改 `define.v` 的 `NUM_THREAD`、跑对应 `make` 目标即可（详见 u1-l4）。

---

## 5. 综合实践

把本讲知识串起来，做一次「端到端追踪」：

**任务**：以 `tc_vecadd` 的 `4w8t` 配置为例，写一份「仿真生命周期报告」，覆盖以下 7 个时刻，每个时刻给出**对应的源码位置（带行号的永久链接）**和**一句话说明**：

1. 上电复位释放（`gen_rst`）。
2. 内存预加载完成（`init_mem` 的最后一笔 `release`）。
3. workgroup 参数配置完毕（`drv_gpu` 写完 `reg[15]`）。
4. 首个 workgroup 被触发（`drv_gpu` 写 `reg[0]=1`）。
5. DUT 确认收到派发（`cta2host_rcvd_ack_o` 下降沿）。
6. 执行完成被检测到（`exe_finish` 读到 `0x44 != 0`）。
7. 结果判定（`print_result` 调 `PASSED` 或 `FAILED`）。

**进阶**：在报告里画一张时序轴（ns 为单位），标出 `cycle_count[0]`（时刻 4/5 之间）和 `cycle_count[1]`（时刻 6）的位置，并用 `kernel_cycles = (cycle_count[1]-cycle_count[0])/10` 解释这个除以 10 的来源（提示：`gen_clk` 的 `PERIOD`）。

**验证方式**：若本地有 VCS，跑 `make run-vcs-4w8t`，把 `simv.log` 里 `$display` 打印的时间戳填进你的时序轴，核对你的源码定位是否正确。无法运行则标注「待本地验证」。

## 6. 本讲小结

- 仿真顶层 `test_gpu_axi_top` 只做例化与连线，把 DUT、时钟/复位、`host_inter`、`axi_ram`、`tc` 拼成完整平台，自身只额外提供波形 dump 与 `PASSED`/`FAILED` 打印 task。
- `host_inter` 是 AXI4-Lite 主机，读 `.metadata`、配置 host 寄存器、写 `reg[0]=1` 触发首个 workgroup，再轮询 `reg[0x44]` 判断完成——它**不负责**加载 kernel/数据。
- `axi_ram` 是 AXI4 从端内存模型，用 `mem` 数组 + 读写状态机响应 DUT 访存，并提供 `store_mem`/`display_mem` 供结果回读。
- 真正的内存预加载在 `tc.v` 的 `init_mem`：用 `$readmemh` 读 `.data`，再用 `force` 直接驱动 `axi_ram` 的 AXI4 写口以 burst 灌入数据，写完 `release`。
- `PASSED`/`FAILED` 的判定逻辑在 `tc.v` 的 `print_result`：把结果与 golden 逐字比较，全对才 `PASSED`。
- `file_list.f` 让 5 个平台文件跨用例共享，仅 `./tc.v` 随用例替换——这是「换用例零改平台」的关键设计。

## 7. 下一步学习建议

- **横向扩展（其他用例）**：对比 [tc_gaussian/tc.v](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/testcase/test_gpgpu_axi_top/tc_gaussian/tc.v) 与本讲的 `tc_vecadd/tc.v`，观察不同用例的 `print_result` 如何改变 golden 比较逻辑——这是写自定义测试用例的模板。
- **向下游（综合与上板）**：继续学 u8-l3（FPGA 验证与综合流程），看真实硬件（而非 `axi_ram` 模型）如何被驱动，以及 `FPGA_test/driver/naive_driver.c` 用 C 代码（而非 Verilog task）扮演主机。
- **向纵深（指令扩展）**：学 u8-l4（指令集扩展与二次开发），那里会教你如何新增一条指令并为其编写测试用例——届时你需要回到本讲，在 `tc.v` 框架里加一个验证新指令的 case。
- **回顾协议侧**：若对 `host_inter` 写的寄存器如何被 DUT 消费、`axi_ram` 的读写如何对应 TileLink/AXI 转换有疑问，回看 u7-l4（AXI4 适配器与 host 接口）。
