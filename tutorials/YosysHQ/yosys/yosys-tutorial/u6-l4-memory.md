# memory：存储器推断与映射

## 1. 本讲目标

本讲讲解 Yosys 如何把 Verilog 里的数组式存储器（`reg [7:0] mem [0:255];`）一路翻译成门级网表。学完后你应当能够：

- 说清一条 `reg ... mem [...]` 声明在 RTLIL 里是怎样被表示的（`RTLIL::Memory` 加上若干读/写端口单元）。
- 画出 `memory` 宏命令编排的子 pass 顺序，并解释为什么是这个顺序。
- 说明 `memory_collect` 如何把零散的端口「打包」成一个 `$mem_v2` 单元，`memory_share` 如何合并端口，`memory_narrow` 如何拆分宽端口。
- 解释 `memory_map` 如何把一个多端口存储器拆成「触发器阵列 + 地址译码多路器 + 写多路器」的组合，并能估算它会产生多少个 `$dff`/`$mux`。

本讲承接 [u6-l3 opt：网表优化大流程](u6-l3-opt.md)：`proc` 把 `always` 翻译成门级后，存储器仍以抽象的「数组 + 端口」形式存在，需要 `memory` 这一组 pass 把它「落实」到具体的寄存器与多路器上。

## 2. 前置知识

在进入源码前，先用一段直觉建立心智模型。

**什么是「存储器推断」？** 你在 Verilog 里写 `reg [7:0] mem [0:255];`，这只是声明了一个「256 个字、每字 8 位」的数组。综合工具面前有两条路：

1. **落实为触发器阵列**：把 256 个字各用一个 8 位寄存器存起来，读地址用一棵多路器树选择。这叫 **FF RAM / 逻辑实现**，通用但昂贵（256 个触发器）。
2. **落实为硬件 RAM 原语**：映射到 FPGA 的 Block RAM、分布式 RAM（LUT RAM）或 ASIC 的 SRAM。这需要专门的工艺映射（`memory_libmap`、`memory_bram`）。

Yosys 的 `memory` pass 默认走第 1 条路（落实为触发器与多路器）；第 2 条路由各厂商 `synth_*` 脚本在 `memory -nomap` 之后插入。本讲聚焦第 1 条路，即 `memory_map` 的内部原理。

**两种表示形态**：在 RTLIL 里，一个存储器在「落实」过程中会经历两种形态：

- **解包形态（unpacked）**：一个 `RTLIL::Memory` 声明（描述几何尺寸）+ 若干独立的端口单元（`$memrd`/`$memwr` 各代表一次读/写）。这便于在不同端口之间做合并、共享等优化。
- **打包形态（packed）**：把同一个存储器的所有端口「捆」进一个 `$mem_v2` 单元，端口信息编码进它的参数与端口连线里。

关键术语：**端口（port）** 是一次访问（一个地址、一份数据、一个使能、一个时钟）；**字（word）** 是存储器里的一个存储单元；**地址译码（address decode）** 是判断「某端口访问的地址是否等于某字的地址」。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [passes/memory/memory.cc](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/memory/memory.cc) | `memory` 宏命令，按固定顺序串联各 `memory_*` 子 pass（编排器）。 |
| [passes/memory/memory_collect.cc](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/memory/memory_collect.cc) | `memory_collect`：把解包形态的端口「打包」成单个 `$mem_v2`。 |
| [passes/memory/memory_share.cc](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/memory/memory_share.cc) | `memory_share`：合并可共享的读/写端口（按地址合并、SAT 共享、宽端口合并）。 |
| [passes/memory/memory_narrow.cc](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/memory/memory_narrow.cc) | `memory_narrow`：把宽端口拆回窄端口（独立 pass，不在默认 `memory` 流水线内）。 |
| [passes/memory/memory_map.cc](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/memory/memory_map.cc) | `memory_map`：把 `$mem_v2` 拆成 `$dff` 阵列 + `$mux` 读树 + `$wrmux` 写逻辑。 |
| [kernel/mem.h](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/mem.h) / [kernel/mem.cc](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/mem.cc) | `Mem`/`MemRd`/`MemWr` 辅助类：统一抽象「解包/打包」两种形态，供各 pass 复用。 |
| [kernel/rtlil.h](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/rtlil.h) | `RTLIL::Memory` 结构体定义（存储器的几何声明）。 |
| [passes/proc/proc_memwr.cc](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/proc/proc_memwr.cc) | `proc_memwr`：从 `always` 的写动作生成最初的 `$memwr_v2` 单元（存储器的「诞生地」之一）。 |
| [docs/source/using_yosys/synthesis/memory.rst](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/docs/source/using_yosys/synthesis/memory.rst) | 官方文档对 `memory` 命令的简要说明。 |

## 4. 核心概念与源码讲解

### 4.1 memory 编排：MemoryPass 的子 pass 顺序

#### 4.1.1 概念说明

和 `synth`、`opt` 一样，`memory` 是一条**编排型 pass**——它自己不做任何综合算法，只负责按一个「有用的顺序」依次调用一串 `memory_*` 子 pass。这一点从它的构造与帮助文本就能看出来：

[passes/memory/memory.cc:28-29](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/memory/memory.cc#L28-L29) 注册命令名为 `memory`，说明文字是 `translate memories to basic cells`（把存储器翻译为基础单元）。

#### 4.1.2 核心流程

`memory` 的帮助文本（[memory.cc:36-53](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/memory/memory.cc#L36-L53)）列出了它调用的全部子 pass。这与 `execute()` 里的真实调用序列逐字对应（[memory.cc:108-127](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/memory/memory.cc#L108-L127)），可以归纳为下表：

| 顺序 | 子 pass | 作用 | 跳过条件 |
|------|---------|------|----------|
| 1 | `opt_mem` | 针对存储器端口的局部优化 | — |
| 2 | `opt_mem_priority` | 整理写端口的优先级掩码 | — |
| 3 | `opt_mem_feedback` | 处理读写反馈（读改写） | — |
| 4 | `memory_bmux2rom` | 把恒定选择的多路读端口变 ROM | `-norom` |
| 5 | `memory_dff` | 把端口前后的寄存器吸收进端口单元 | `-nordff` / `-memx` |
| 6 | `opt_clean` | 清理悬空线 | — |
| 7 | `memory_share` | 合并可共享的读/写端口 | — |
| 8 | `opt_mem_widen` | 端口宽度整理 | — |
| 9 | `memory_memx` | 把不确定行为显式化为 X | 仅 `-memx` |
| 10 | `opt_clean` | 再次清理 | — |
| 11 | `memory_collect` | 打包成 `$mem_v2` | — |
| 12 | `memory_bram` | 按规则映射 BRAM | 仅 `-bram` |
| 13 | `memory_map` | 落实为触发器与多路器 | `-nomap` |

注意顺序里的一个关键设计：**`memory_share`（第 7 步）在 `memory_collect`（第 11 步）之前**。原因是端口合并优化需要逐个端口地比较地址、时钟、使能，这在「每个端口一个独立单元」的解包形态下最方便；一旦打包进单个 `$mem_v2`，端口信息就被编码进参数数组里，反而不易增删。所以 Yosys 选择「先在解包形态下做完所有端口级优化，再打包」。

`execute()` 里把这些调用串起来的方式非常直白，就是一连串 `Pass::call(design, "子pass名")`（[memory.cc:108-127](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/memory/memory.cc#L108-L127)）——和你在脚本里手写一串 `memory_share;` `memory_collect;` 完全等价，宏命令只是省去你记忆顺序的负担。`-nomap`、`-nordff` 等开关只是跳过对应那一行。

#### 4.1.3 源码精读

命令行选项解析（[memory.cc:70-106](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/memory/memory.cc#L70-L106)）把部分开关「转发」给子 pass：例如 `-nowiden` 被拼进 `memory_share_opts`（第 88-91 行），最终在第 116 行以 `Pass::call(design, "memory_share -nowiden")` 的形式传下去。`-memx` 同时置 `flag_nordff` 与 `flag_memx`（第 83-87 行），因为 memx 流程要求端口不带寄存器。

真正的调用序列在 [memory.cc:108-127](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/memory/memory.cc#L108-L127)，每个 `Pass::call` 就是「按名字查 pass_register 并执行」（参见 u4-l1 讲过的注册机制）。

#### 4.1.4 代码实践

**目标**：直观看到 `memory` 宏命令与手写子 pass 序列等价。

**步骤**：

1. 写一个最小存储器 `mem_demo.v`（256 字 × 8 位，1 写端口 + 1 同步读端口）：

   ```verilog
   module mem_demo(input clk, input we,
                   input [7:0] waddr, input [7:0] wdata,
                   input [7:0] raddr, output reg [7:0] rdata);
       reg [7:0] mem [0:255];
       always @(posedge clk) begin
           if (we) mem[waddr] <= wdata;
           rdata <= mem[raddr];
       end
   endmodule
   ```

2. 准备脚本 `run_mem.ys`：

   ```yosys
   read_verilog mem_demo.v
   proc
   memory -nomap      # 跑完除 memory_map 外的所有子 pass
   write_rtlil mem_after_collect.rtlil
   stat
   ```

3. 运行 `./build/yosys run_mem.ys`，观察日志里依次打印的 `Executing ... pass` 行。

**需要观察的现象**：日志会逐条打印 `opt_mem`、`memory_share`、`memory_collect` 等子 pass 的 header，与你手写这些命令的效果一致。

**预期结果**：第 11 步 `memory_collect` 之后，`mem_after_collect.rtlil` 里出现一个 `$mem_v2` 单元（打包形态）。若把脚本改成完整 `memory`（去掉 `-nomap`），则 `$mem_v2` 消失，换成大量 `$dff`/`$mux`。

> 若本地尚未构建 yosys，运行命令的结果标注为「待本地验证」。构建方式见 [u1-l2](u1-l2-build-and-run.md)。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `memory_share` 必须排在 `memory_collect` 之前，而不能之后？
**答案**：`memory_share` 要增删、合并端口，在「每端口一个单元」的解包形态下最易操作；`memory_collect` 会把所有端口编码进单个 `$mem_v2` 的参数数组，之后合并就得反复拆包/打包，代价高且易错。

**练习 2**：用 `memory -nomap` 综合后，网表里应该留下哪一种存储器单元？
**答案**：留下打包后的 `$mem_v2` 单元（因为跳过了把它落实为触发器的 `memory_map`）。

---

### 4.2 RTLIL::Memory 模型与 Mem 辅助类

#### 4.2.1 概念说明

要读懂 `memory_*` 各 pass，必须先搞清「存储器在 RTLIL 里长什么样」。现代 Yosys 的存储器表示经过一次重要重构：**引入 `kernel/mem.h` 的 `Mem` 辅助类**，把「解包形态」与「打包形态」统一抽象成同一套 C++ 结构，这样无论输入是哪种形态，所有 pass 都能用同一份代码处理。

#### 4.2.2 核心流程

存储器在 RTLIL 里的生命周期是：

```
Verilog: reg [7:0] mem [0:255];
   │  read_verilog + proc
   ▼
解包形态:  RTLIL::Memory (声明: width=8, size=256)
          + $memwr_v2 (写端口单元, 来自 proc_memwr)
          + $memrd/$memrd_v2 (读端口单元)
          + $meminit_v2 (初值单元)
   │  memory_collect  (设 packed=true 并 emit)
   ▼
打包形态:  单个 $mem_v2 单元 (所有端口编码进参数/端口)
   │  memory_map
   ▼
门级:      size 个 $dff (存储字)
          + 每读端口一棵 $mux 树 (地址选择)
          + 每字每写端口一组 $wrmux/$and (写译码)
```

`RTLIL::Memory` 本身非常瘦——它只描述存储器的**几何尺寸**，不包含任何端口或访问逻辑：

[kernel/rtlil.h:2485-2499](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/rtlil.h#L2485-L2499) 定义 `RTLIL::Memory`，核心字段只有三个：`width`（每字位宽）、`start_offset`（最低地址偏移）、`size`（字数）。它继承自 `NamedObject`，因此也有名字与属性（名字即 `memid`，如 `\mem`）。

真正的「端口」是独立的单元。读端口与写端口长什么样，由 `Mem` 辅助类的两个内嵌结构描述：

- `MemRd`（[kernel/mem.h:29-59](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/mem.h#L29-L59)）：一个读端口，含 `addr`（地址）、`data`（读出数据）、`en`（使能）、`clk`/`clk_enable`/`clk_polarity`（时钟）、`arst`/`srst`（复位）、`init_value`/`arst_value`/`srst_value`（初值/复位值）、`wide_log2`（宽端口因子）、以及两个掩码 `transparency_mask`/`collision_x_mask`（描述同址读写时的可见性语义）。
- `MemWr`（[kernel/mem.h:61-81](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/mem.h#L61-L81)）：一个写端口，含 `addr`、`data`、`en`（按位的字节使能）、`clk`/`clk_enable`/`clk_polarity`、`wide_log2`、以及 `priority_mask`（当多个写端口同址同周期写入时，谁优先）。

`Mem` 类（[kernel/mem.h:92-101](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/mem.h#L92-L101)）把上述零散信息聚拢：`memid`、`width`/`start_offset`/`size`、`rd_ports`（`vector<MemRd>`）、`wr_ports`（`vector<MemWr>`）、`inits`（初值列表）、以及两个指针 `mem`（指向 `RTLIL::Memory`，解包时非空）和 `cell`（指向 `$mem_v2` 单元，打包时非空）。布尔位 `packed` 标记当前是哪种形态。

#### 4.2.3 源码精读

**「诞生地」**：存储器写端口最初由 `proc_memwr` 生成。`proc` 把 `always` 块里的 `mem[addr] <= data` 收集成 `RTLIL::MemWriteAction`，再由 [passes/proc/proc_memwr.cc:45-53](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/proc/proc_memwr.cc#L45-L53) 翻译成一个 `$memwr_v2` 单元——设置 `MEMID`、`ABITS`、`WIDTH`、`PORTID`、`PRIORITY_MASK` 参数，并接上 `ADDR`/`DATA`/`EN`/`CLK` 端口。读端口和初值单元则在前端 [frontends/ast/genrtlil.cc:2030](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/frontends/ast/genrtlil.cc#L2030)（`$memrd`）与 [genrtlil.cc:2070](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/frontends/ast/genrtlil.cc#L2070)（`$meminit_v2`）生成。

**统一入口**：所有 `memory_*` pass 都通过 `Mem::get_selected_memories(module)` 拿到当前模块里的存储器列表。[kernel/mem.cc:870-882](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/mem.cc#L870-L882) 的实现同时扫描两个来源——`module->memories`（解包的 `RTLIL::Memory` 声明）与类型为 `$mem`/`$mem_v2` 的单元（打包形态），把它们都归一成 `Mem` 对象。这正是「解包/打包统一」的关键：调用者无需关心输入是哪种形态。

**回写**：修改完 `Mem` 对象后，调 `emit()` 把变更写回网表。[kernel/mem.cc:55-126](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/mem.cc#L55-L126) 显示：若 `packed` 为真，就创建/更新一个 `$mem_v2` 单元（第 126 行 `module->addCell(memid, ID($mem_v2))`），把所有端口「拼接」进它的参数（`RD_CLK_ENABLE`、`WR_CLK_ENABLE`、各种掩码等）与端口（`RD_ADDR`、`WR_DATA` 等）；否则走 [mem.cc:287-305](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/mem.cc#L287-L305) 的解包分支，为每个端口各创建一个 `$memrd_v2`/`$memwr_v2` 单元并新建 `RTLIL::Memory` 声明。`remove()`（[mem.cc:25-53](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/mem.cc#L25-L53)）则把存储器及其所有端口单元从模块里删掉（`memory_map` 落实完毕后调用）。

#### 4.2.4 代码实践

**目标**：用 `write_rtlil` 直接观察「解包形态」与「打包形态」的差异。

**步骤**：

1. 用 4.1.4 的 `mem_demo.v`，准备脚本：

   ```yosys
   read_verilog mem_demo.v
   proc
   write_rtlil mem_unpacked.rtlil   # 解包形态
   memory_collect                    # 仅打包，不落实
   write_rtlil mem_packed.rtlil     # 打包形态
   ```

2. 运行后用文本编辑器或 `grep` 对比两份 `.rtlil`：
   - 在 `mem_unpacked.rtlil` 里找 `memory \mem 8 0 256`（声明：宽 8、偏移 0、256 字），以及若干 `cell $memwr_v2 ...`、`cell $memrd ...`。
   - 在 `mem_packed.rtlil` 里找 `cell $mem_v2 ...`，观察它的参数（`WR_PORTS`、`RD_PORTS`、`ABITS`、`WIDTH` 等）与端口（`WR_ADDR`、`RD_DATA` 等）。

**需要观察的现象**：解包形态下，存储器声明与端口是分散的多行；打包后浓缩成单个 `$mem_v2` 单元，端口数与数据拼接进了它的端口连线。

**预期结果**：`mem_packed.rtlil` 中不再有 `memory \mem ...` 声明和零散 `$memwr_v2`/`$memrd` 单元，取而代之的是一个 `$mem_v2`。

> 具体出现的读端口单元是 `$memrd` 还是 `$memrd_v2` 取决于前端路径，可能为「待本地验证」；但写端口 `$memwr_v2` 与打包单元 `$mem_v2` 是确定的。

#### 4.2.5 小练习与答案

**练习 1**：`RTLIL::Memory` 结构体里有没有「读端口」「写端口」字段？
**答案**：没有。它只有 `width`/`start_offset`/`size` 三个几何字段。端口是独立的单元（`$memrd*`/`$memwr*`），通过 `MEMID` 参数指回所属存储器。

**练习 2**：`Mem` 类的 `mem` 与 `cell` 两个指针分别在什么情况下非空？
**答案**：解包形态下 `mem` 指向 `RTLIL::Memory`（`cell` 为空）；打包形态下 `cell` 指向 `$mem_v2` 单元（`mem` 为空）。`emit()` 会先删掉旧的、再按 `packed` 标志创建对应的那种。

---

### 4.3 collect / share / narrow：端口的打包、合并与拆分

#### 4.3.1 概念说明

这一组 pass 在「端口」这一层做文章：`memory_collect` 负责**打包**（解包 → `$mem_v2`），`memory_share` 负责**合并**（减少端口数），`memory_narrow` 负责**拆分**（把宽端口拆回窄端口）。三者都基于 4.2 的 `Mem` 抽象，因此代码都很短——真正的工作量在 `Mem` 类与各 worker 里。

#### 4.3.2 核心流程

**memory_collect（打包）**：逻辑极简——对每个解包存储器，置 `packed = true` 再 `emit()`，于是 `emit()` 走打包分支生成 `$mem_v2`。

**memory_share（合并）**：三种合并手段，逐步减少端口数：

1. **按地址合并读端口**：若两个读端口的地址高位相同、时钟/使能/复位一致，且低位是「相邻对齐」的常数（如一个读 `addr=00`、一个读 `addr=01`），可合并成一个**宽读端口**（一次读两个字）。
2. **按地址合并写端口**：同理，两个写同址、同时钟的写端口合并成一个宽写端口或一个带复杂使能的端口。
3. **基于 SAT 的写端口共享**：用 SAT 求解器证明两个写端口的使能信号「不可能同时为真」，于是它们可以安全地共享同一个物理端口（用多路器在两份地址/数据间选择）。

**memory_narrow（拆分）**：与合并相反——若某端口是宽端口（`wide_log2 > 0`，一次访问多个字），把它拆成多个一次访问一个字的窄端口。注意：**它不在默认 `memory` 流水线里**（`memory.cc` 调的是 `opt_mem_widen`，方向相反），而是作为独立 pass 提供给特殊流程（如构建非对称存储器时，先把所有端口统一拆成最窄）。

#### 4.3.3 源码精读

**memory_collect** 的全部业务逻辑只有 5 行（[memory_collect.cc:45-50](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/memory/memory_collect.cc#L45-L50)）：遍历 `Mem::get_selected_memories(module)`，对每个尚未打包（`!mem.packed`）的存储器，设 `packed = true` 并 `emit()`。复杂度全藏在 `Mem::emit()` 里（4.2.3 已讲）。

**memory_share** 的 worker 在 `operator()`（[memory_share.cc:474-509](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/memory/memory_share.cc#L474-L509)）里编排：先 `while(consolidate_rd_by_addr(mem))` 与 `while(consolidate_wr_by_addr(mem))` 跑到不动点（每轮可能合并出一批宽端口，又暴露新的可合并机会），再（若未 `-nosat`）调 `consolidate_wr_using_sat`。两种「按地址合并」的核心判定（[memory_share.cc:101-145](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/memory/memory_share.cc#L101-L145) 读端口、[memory_share.cc:222-254](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/memory/memory_share.cc#L222-L254) 写端口）都要求：时钟域相同、地址高位相同、低位为常数；若地址高位「差一位」，则在 `flag_widen` 允许时再多拓宽一位强行合并（[memory_share.cc:133-145](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/memory/memory_share.cc#L133-L145)）。

SAT 共享（[memory_share.cc:297-465](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/memory/memory_share.cc#L297-L465)）的思路很优雅：先用 `QuickConeSat` 把同组写端口的使能（EN）信号的「公共输入锥」编码成 SAT 问题（[memory_share.cc:382-395](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/memory/memory_share.cc#L382-L395)），然后对每对端口提问「是否存在一种输入，使两个端口的 EN 同时为真？」（`qcsat.ez->solve(en1, en2)`，[memory_share.cc:410](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/memory/memory_share.cc#L410)）：
- 若 **可满足**（存在同周期都激活的情况）→ 不能合并；
- 若 **不可满足**（永远不可能同时激活）→ 安全合并，用一个多路器在两份地址/数据间按「本次是谁激活」做选择。

这正是 u10-l1 形式验证讲义里 SAT 工具的典型复用：用可满足性判断来证明「两个事件互斥」。

**memory_narrow** 同样简短（[memory_narrow.cc:48-67](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/memory/memory_narrow.cc#L48-L67)）：扫描各端口，只要有 `wide_log2 > 0` 的宽端口，就调 `mem.narrow()`（由 `Mem` 类负责把宽端口拆成若干等价窄端口）再 `emit()`。

#### 4.3.4 代码实践

**目标**：观察 `memory_share` 如何用一个端口替代两个互斥的写端口。

**步骤**：

1. 写一个含两个「互斥」写端口的存储器（`we_a` 与 `we_b` 由同一个 `sel` 互斥驱动）：

   ```verilog
   module mem_share_demo(input clk, input sel,
                         input [3:0] addr, input [7:0] da, db,
                         input [3:0] raddr, output reg [7:0] rdata);
       reg [7:0] mem [0:15];
       wire we_a = sel ? 1'b0 : 1'b1;      // sel=0 时写 A
       wire we_b = sel ? 1'b1 : 1'b0;      // sel=1 时写 B，二者互斥
       always @(posedge clk) begin
           if (we_a) mem[addr] <= da;
           if (we_b) mem[addr] <= db;
           rdata <= mem[raddr];
       end
   endmodule
   ```

2. 脚本：

   ```yosys
   read_verilog mem_share_demo.v
   proc
   memory_share -nosat      # 先只做按地址合并
   write_rtlil share_step1.rtlil
   memory_share             # 再允许 SAT 合并
   write_rtlil share_step2.rtlil
   stat
   ```

3. 对比两份 `.rtlil` 里 `$memwr_v2`（或打包后的 `$mem_v2` 的 `WR_PORTS` 参数）的端口数。

**需要观察的现象**：SAT 共享开启后，两个互斥写端口被合并为一个，地址与数据前插入一个由 `sel` 控制的多路器。

**预期结果**：`share_step2` 的写端口数比 `share_step1` 少一个，且能看到新增的 `$mux`（在 `addr`/`data` 路径上选择 `da`/`db`）。能否触发 SAT 合并取决于求解器对该输入锥的判断，若未触发则标注「待本地验证」并用 `debug` 命令查看详细日志。

#### 4.3.5 小练习与答案

**练习 1**：`memory_share` 的三种合并里，哪一种依赖 SAT 求解器？如何关闭？
**答案**：写端口的「互斥共享」依赖 SAT（证明两个 EN 永不同时为真）。用 `-nosat` 关闭，仅保留按地址合并与宽端口合并。

**练习 2**：`memory_narrow` 为什么不在默认 `memory` 流水线里？
**答案**：默认流水线的目标是减少端口、便于映射，方向是「合并/拓宽」（`opt_mem_widen`）；`memory_narrow` 是反向的「拆分」，只在构建非对称存储器等需要把端口统一到最窄的 special flow 里手动调用。

---

### 4.4 memory_map：存储器到触发器与多路器的映射

#### 4.4.1 概念说明

`memory_map` 是整条 `memory` 流水线的终点（除非用 `-nomap` 跳过）。它把一个 `$mem_v2`（多端口存储器）落实为「FF RAM / 逻辑实现」：每个存储字用一个 `$dff` 触发器保存，读端口用一棵 `$mux` 二叉树按地址选择，写端口用地址译码 + 写多路器把新数据写进对应字的触发器。

#### 4.4.2 核心流程

设存储器有 `size` 个字、每字 `width` 位。`memory_map` 的 `handle_memory()` 大致分三步：

**第 1 步：为每个字造一个存储触发器。** 地址位数取

\[
\text{abits} = \lceil \log_2(\text{size}) \rceil
\]

对每个字地址 `i`（`0 .. size-1`），生成一个宽度为 `width` 的 `$dff`，时钟取所有写端口共享的 `refclock`。它的 Q 输出代表「这个字当前存的值」，D 输入由第 3 步的写逻辑驱动。例外：若某字被一个「常数地址 + 常数使能」的写端口固定写入（静态字），则不造触发器，直接用常数代替。

**第 2 步：为每个读端口造一棵读多路树。** 读端口要按 `addr` 从 `2^abits` 个字里选一个，用一棵 `abits` 层的 `$mux` 二叉树实现：从读数据输出开始，每一层用地址的一位做选择，把当前信号一分为二。整棵树的叶子连到各字的 Q 输出。一棵完整的读树共有

\[
2^{\text{abits}} - 1
\]

个 `$mux`。

**第 3 步：为每个字、每个写端口造写逻辑。** 对字 `addr` 与写端口 `j`，先用「地址译码」判断 `端口j 的地址 == addr`，再与使能位相与，最后用一个 `$wrmux` 在「旧值」与「写数据」之间选择。多个写端口串成一条写链，末尾接该字触发器的 D 输入。字节使能（byte enable）会让位宽分段，每段一个 `$wrmux`。

**地址译码** 是个小亮点：它把「地址是否等于某常数」实现成一棵**平衡二叉树**——把地址切成两半，递归比较每一半是否相等，再用 `$and` 合并。这比「一位一位比较再全与」更浅，关键中间结果（如「高 4 位相等」）被缓存复用（`decoder_cache`）。

#### 4.4.3 源码精读

入口 `MemoryMapPass::execute`（[memory_map.cc:433-507](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/memory/memory_map.cc#L433-L507)）解析选项后，对每个模块构造 `MemoryMapWorker` 并 `run()`；`run()`（[memory_map.cc:390-394](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/memory/memory_map.cc#L390-L394)）遍历 `Mem::get_selected_memories` 逐个调 `handle_memory`。

**地址译码** 在 [memory_map.cc:85-104](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/memory/memory_map.cc#L85-L104)：`addr_decode(addr_sig, addr_val)` 递归地把地址对半切（`split_at = size/2`），对每一半递归求相等位，再 `$and` 合并；结果缓存在 `decoder_cache`，避免重复造树。单 bit 地址直接用 `module->Eq`。

**存储触发器** 在 [memory_map.cc:205-270](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/memory/memory_map.cc#L205-L270)：循环 `i = 0 .. mem.size-1`，对每个字造 `$dff`（第 237 行 `addCell(ff_id, ID($dff))`），设 `WIDTH = mem.width`、`CLK = refclock`；初值写到输出线的 `init` 属性（[memory_map.cc:261-262](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/memory/memory_map.cc#L261-L262)）。`-formal` 模式下 ROM 用 `$ff`（全局时钟形式验证流，[memory_map.cc:226-232](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/memory/memory_map.cc#L226-L232)）。

**读多路树** 在 [memory_map.cc:288-308](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/memory/memory_map.cc#L288-L308)：外层 `for j = 0 .. abits-wide_log2` 是树的层数，内层对当前每个信号造一个 `$mux`，用 `rd_addr[abits-j-1]` 做选择（第 297 行），把 `A`/`B` 推入下一层信号列表——信号数每层翻倍，最终 `2^abits` 个叶子连到 `data_read[]`（各字的值）。

**写逻辑** 在 [memory_map.cc:328-381](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/memory/memory_map.cc#L328-L381)：对每个字、每个写端口，`addr_decode` 算出「地址命中」信号，若使能不是常数 1 再 `$and` 上使能位（[memory_map.cc:351-364](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/memory/memory_map.cc#L351-L364)），然后用 `$wrmux` 在旧值与写数据间选择（[memory_map.cc:366-376](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/memory/memory_map.cc#L366-L376)）。注意它会把使能位相等的连续位合并成一个更宽的 `$wrmux`（[memory_map.cc:339-347](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/memory/memory_map.cc#L339-L347)），减少单元数。

映射完成后调 `mem.remove()`（[memory_map.cc:387](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/memory/memory_map.cc#L387)）删掉原 `$mem_v2`，网表里只剩 `$dff`/`$mux`/`$and`。

> 提示：在 FPGA/ASIC 流程里，通常不希望大存储器走这条「触发器阵列」的昂贵路线，而是用 `memory -nomap` + `memory_libmap`（见 [docs/source/using_yosys/synthesis/memory.rst:63-84](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/docs/source/using_yosys/synthesis/memory.rst#L63-L84)）把它们映射到硬件 RAM 原语，`memory_map` 只兜底处理无法映射的小存储器。

#### 4.4.4 代码实践

**目标**：观察 `$mem_v2` 如何变成 `$dff`/`$mux`，并验证单元数量与公式吻合。

**步骤**：

1. 仍用 4.1.4 的 `mem_demo.v`（256 字 × 8 位，1 写 + 1 同步读）。脚本：

   ```yosys
   read_verilog mem_demo.v
   proc
   memory_collect
   stat -top mem_demo          # 记下此时 $mem_v2 数
   memory_map
   stat -top mem_demo          # 记下此时 $dff / $mux / $and 数
   ```

2. 对照公式验算：
   - 地址位数 `abits = ⌈log2 256⌉ = 8`。
   - 存储触发器：约 `size = 256` 个 `$dff`（宽度 8）。
   - 读树 `$mux` 数：约 `2^8 − 1 = 255` 个（一个同步读端口；同步读还可能抽出一个输出 `$dff`）。

**需要观察的现象**：`memory_map` 后 `$mem_v2` 消失，出现约 256 个 `$dff` 与一棵约 255 个 `$mux` 的读树，以及写端口的 `$wrmux`/`$and`。

**预期结果**：`stat` 输出里 `$dff` 数与 `size` 接近，`$mux` 数与 `2^abits − 1` 接近；同步读端口的输出寄存器会让 `$dff` 再多 1。具体计数值「待本地验证」，但量级应与公式一致。

> 想看更丰富的存储器模式（字节使能、宽端口、双端口、初值），可直接用仓库自带的 [tests/simple/memory.v](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/tests/simple/memory.v)，里面 `memtest01`～`memtest13` 覆盖了绝大多数常见形态，是本讲最好的练习素材。

#### 4.4.5 小练习与答案

**练习 1**：一个 1024 字、单读端口的 ROM，`memory_map` 后大约会产生多少个 `$mux`？
**答案**：`abits = ⌈log2 1024⌉ = 10`，读树 `$mux` 数约 `2^10 − 1 = 1023` 个；ROM 无写端口，故无 `$wrmux`。

**练习 2**：`memory_map` 里地址译码为什么用「对半切的平衡二叉树」而不是「逐位比较再全与」？
**答案**：平衡二叉树更浅（深度为 `O(log abits)` 而非 `O(abits)`），关键路径更短；且中间结果（如「高半地址相等」）可被多个字复用并缓存进 `decoder_cache`，显著减少单元数。

**练习 3**：为什么大存储器通常要避免走 `memory_map` 的默认路线？
**答案**：默认路线把每个字都落实成一个触发器，N 字存储器要消耗约 N 个 `$dff` 加一棵大读树，面积/功耗代价随容量线性乃至超线性增长；大存储器应映射到硬件 Block RAM/SRAM 原语（`memory -nomap` + `memory_libmap` + `memory_bram`）。

---

## 5. 综合实践

把本讲的知识串起来，跟踪一个真实存储器从 Verilog 到门级的完整变形。

**任务**：用 [tests/simple/memory.v](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/tests/simple/memory.v) 里的 `memtest01`（16 字 × 8 位、1 写 + 1 同步读），编写一个脚本，在 `proc` 之后依次执行：

```yosys
read_verilog tests/simple/memory.v
hierarchy -top memtest01
proc
write_rtlil stage0_proc.rtlil        # ① 解包形态
memory_share                          # ② 端口合并
memory_collect
write_rtlil stage1_collect.rtlil      # ③ 打包形态 $mem_v2
stat
memory_map
write_rtlil stage2_map.rtlil          # ④ 门级 $dff/$mux
stat
```

**要求**：

1. 在 `stage0_proc.rtlil` 中找到 `RTLIL::Memory` 声明（`memory \data 8 0 16`）与 `$memwr_v2` 写端口单元，确认其 `MEMID`/`ADDR`/`DATA`/`EN` 端口。
2. 在 `stage1_collect.rtlil` 中确认 `$mem_v2` 单元出现，读出它的 `WR_PORTS`、`RD_PORTS`、`ABITS`、`WIDTH` 参数。
3. 在 `stage2_map.rtlil` 中确认 `$mem_v2` 消失，统计 `$dff` 与 `$mux` 数量，与公式（`abits=4`，读树约 `2^4−1=15` 个 `$mux`，约 16 个存储 `$dff` 加 1 个同步读输出 `$dff`）对照。
4. 用 `stat` 在三个阶段的输出对比「单元总数」与「单元种类」的变化，写下你的观察。

这个练习覆盖了本讲全部四个最小模块：编排顺序（脚本本身就是 `memory` 宏的等价手写版）、`RTLIL::Memory` 与 `Mem` 模型、`collect`/`share` 的端口变换、以及 `memory_map` 的触发器/多路器落实。

## 6. 本讲小结

- `memory` 是一条**编排型 pass**，自己不做算法，只按固定顺序调用 `opt_mem → memory_dff → memory_share → memory_collect → memory_map` 等子 pass；`memory_share` 必须在 `memory_collect` 之前，因为端口合并在解包形态下最方便。
- 存储器在 RTLIL 里有**两种形态**：解包形态（`RTLIL::Memory` 声明 + 若干 `$memrd`/`$memwr` 端口单元）与打包形态（单个 `$mem_v2`）；`kernel/mem.h` 的 `Mem` 辅助类把两者统一抽象，所有 `memory_*` pass 都经 `Mem::get_selected_memories` 读入、经 `emit()`/`remove()` 回写。
- `RTLIL::Memory` 只描述几何尺寸（`width`/`start_offset`/`size`），不持有端口；端口是独立单元，写端口由 `proc_memwr` 生成 `$memwr_v2`，读端口与初值由前端生成。
- `memory_collect` 把解包形态「打包」成 `$mem_v2`（仅 5 行：置 `packed=true` 再 `emit()`）；`memory_share` 用「按地址合并」与「SAT 互斥证明」减少端口数；`memory_narrow` 反向地把宽端口拆成窄端口（不在默认流水线内）。
- `memory_map` 把 `$mem_v2` 落实为「每字一个 `$dff` + 每读端口一棵 `$mux` 二叉树 + 每字每写端口一组 `$wrmux`/`$and`」；地址译码用平衡二叉树并缓存复用。读树 `$mux` 数约为 `2^abits − 1`。
- 在真实 FPGA/ASIC 流程里，大存储器通常用 `memory -nomap` + `memory_libmap`/`memory_bram` 映射到硬件 RAM 原语，`memory_map` 只兜底处理无法映射的小存储器。

## 7. 下一步学习建议

- **工艺映射视角**：本讲只讲了「落实为触发器」的兜底路线。下一讲 [u6-l5 techmap 与 simplemap](u6-l5-techmap-simplemap.md) 会讲通用的工艺映射机制，之后你可以回头看 [passes/memory/memory_bram.cc](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/memory/memory_bram.cc) 与 [passes/memory/memory_libmap.cc](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/memory/memory_libmap.cc)，理解如何把 `$mem_v2` 映射到具体 Block RAM。
- **厂商流程**：进阶可读 [u8-l2 目标平台综合流程](u8-l2-vendor-synth-flows.md)，看 `synth_xilinx`/`synth_ice40` 如何在通用 `synth` 基础上插入 BRAM/DSP 映射阶段。
- **源码延伸**：想深入 `Mem` 类的端口操作（宽端口拓宽、优先级仿真、透明性仿真），直接读 [kernel/mem.cc](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/mem.cc) 的 `widen_wr_port`、`emulate_priority`、`emulate_transparency` 等方法；`memory_share` 的 SAT 共享则是 u10-l1 SAT 机制的最佳预习材料。
- **更多模式**：[docs/source/using_yosys/synthesis/memory.rst](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/docs/source/using_yosys/synthesis/memory.rst) 列举了 Yosys 支持的几乎所有存储器模式（双端口、读优先、字节使能、非对称等），是设计可综合 RAM 的权威参考。
