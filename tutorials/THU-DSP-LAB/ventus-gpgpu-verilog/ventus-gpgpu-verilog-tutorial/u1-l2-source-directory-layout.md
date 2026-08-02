# 源码目录结构与模块组织

## 1. 本讲目标

学完本讲后，你应当能够：

- 说出 Ventus GPGPU（Verilog 版）仓库顶层有哪些目录，它们各自承担什么职责。
- 把 `src/` 下的三大基础目录——`define`（配置）、`common_cell`（公共单元库）、`gpgpu_top`（顶层与核心 RTL）——的作用区分清楚。
- 在拿到一个功能需求（例如“我要看取指怎么实现”“我要改 cache 参数”）时，能在目录树中快速定位到对应的源码文件。
- 看懂仿真平台 `testcase/` 的组织方式，知道 `model_list`、`run.f`、`file_list.f` 这几个文件清单是如何把上百个源码文件串起来送进仿真器的。

承接上一讲（u1-l1）建立的顶层鸟瞰，本讲带你“俯瞰”整个仓库的文件组织，为后续深入任何一个模块打好导航基础。

## 2. 前置知识

- **什么是 RTL 源码**：本项目的硬件逻辑用 Verilog / SystemVerilog（`.v` / `.sv`）描述，统称 RTL（Register Transfer Level，寄存器传输级）代码。你可以把它理解成“用代码画的电路图”。
- **什么是宏定义文件**：Verilog 里用 `` `define `` 定义编译期常量（宏），例如 `NUM_THREAD`。项目用一个集中的头文件 `define.v` 统一管理所有可配置参数，所有模块通过 `` `include "define.v" `` 拿到这些常量。
- **什么是文件清单（file list）**：一个芯片项目动辄几百个源码文件，仿真器（如 VCS）需要一个“清单文件”（通常以 `.f` 结尾）把要编译的文件、包含路径、顶层模块列出来。本项目的 `model_list` 和 `run.f` 就是这类清单。
- **目录即模块归属**：硬件项目通常按功能把文件分目录存放，一个目录往往对应一个子系统（如 `l2cache/` 对应 L2 缓存子系统）。记住目录结构 ≈ 记住功能地图。

## 3. 本讲源码地图

| 文件 / 目录 | 作用 | 本讲用来讲解什么 |
| --- | --- | --- |
| `README.md` | 项目说明、综合指标、仿真入口、致谢 | 仓库整体定位与“从哪里开始” |
| `src/define/define.v`、`undefine.v` | 全局配置宏 | `src/define` 目录的职责 |
| `src/common_cell/fifo.v` | 一个同步 FIFO 示例 | `src/common_cell` 公共单元库的复用风格 |
| `src/gpgpu_top/model_list` | 顶层主文件清单（汇总全部 RTL） | 如何把分散的源码组织进一次仿真 |
| `src/gpgpu_top/` 及其子目录 `cta_top/`、`sm/`、`l2cache/`、`axi4_adapter/` | 顶层与四大核心子系统 | `src/gpgpu_top` 的目录划分 |
| `testcase/test_gpgpu_axi_top/common/run.f`、`file_list.f` | 仿真编译清单 | `testcase` 如何引用 RTL 并搭建 testbench |

## 4. 核心概念与源码讲解

本讲按四个最小模块拆分：`src/define`、`src/common_cell`、`src/gpgpu_top`、`testcase`。它们构成一个“从配置到 RTL 再到验证”的完整闭环。

### 4.1 配置中心 src/define

#### 4.1.1 概念说明

`src/define/` 目录只有两个文件：`define.v` 和 `undefine.v`，但它是整个项目的“总开关”。

- `define.v`：集中定义所有可配置参数与编码宏。包括规模参数（`NUM_SM` 核数、`NUM_WARP` 每核 warp 数、`NUM_THREAD` 每 warp 线程数）、cache 参数、AXI 位宽、指令编码宏、功能函数 `FN_*`、CSR 地址等。
- `undefine.v`：在某些编译场景下需要先“反定义”（`undef）掉部分宏，避免重复定义或切换配置时使用。

为什么要把所有配置集中到一个文件？因为 GPGPU 的很多结构是参数化生成的（例如“有 32 个 lane 就要生成 32 套寄存器”），一旦改了 `NUM_THREAD`，全项目几十个模块的位宽、深度都会跟着变。集中管理能保证“改一处，处处一致”。

> 提示：关于 `define.v` 里每个参数的精确含义，将在 u1-l3 专门逐组讲解；本讲你只需要知道“它在这里、它是总开关”即可。

#### 4.1.2 核心流程

配置在项目中的流转非常简单：

1. 仿真前：用户根据测试用例需要的 warp/thread 配置，编辑 `src/define/define.v`。
2. 编译时：所有模块通过 `` `include "define.v" `` 把宏引入；仿真器通过 `+incdir+` 指明去哪里找这个头文件（见 4.4.3）。
3. 生成电路时：宏驱动各模块的 `parameter` / `generate` 语句，生成对应规模的硬件。

README 明确提醒：仿真前必须先确认 `NUM_THREAD`：

> 在仿真之前，需要确认 GPGPU 单个 warp 的大小：在 `src/define/define.v` 目录下，修改 `NUM_THREAD`

详见 [README.md:37-44](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/README.md#L37-L44)（“开始”小节，以 gaussian 用例为例说明仿真入口）。

#### 4.1.3 源码精读

`define.v` 在主文件清单 `model_list` 中被列为第一条源码，可见它是全项目的编译起点之一：

- [src/gpgpu_top/model_list:1](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/model_list#L1)：清单第 1 行即 `../../../src/define/define.v`，说明配置头文件最先被纳入编译。
- [src/gpgpu_top/model_list:186](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/model_list#L186)：清单最后一行是 `undefine.v`，与 `define.v` 首尾呼应，共同构成配置层。

#### 4.1.4 代码实践（源码阅读型）

1. **实践目标**：确认“改配置就是改这一个文件”。
2. **操作步骤**：打开 `src/define/define.v`，搜索 `NUM_THREAD`、`NUM_WARP`、`NUM_SM` 三个宏，记下它们的默认值。
3. **观察现象**：注意这三个宏的值是写死的常量，且 `NUM_THREAD` 同时决定了 lane 数（向量宽度）。
4. **预期结果**：你会看到它们是简单的 `` `define NUM_THREAD 32 `` 形式（具体数值待 u1-l3 确认）。
5. 若你只是阅读、不打算仿真，**不要真的修改并提交**——本讲不动源码。

#### 4.1.5 小练习与答案

- **练习 1**：为什么项目要单独留一个 `undefine.v`？直接都写在 `define.v` 里不行吗？
  - **答案**：`undef` 用于在重复 `include` 或切换配置场景下清除已定义的宏，避免“宏重复定义”警告或旧配置残留。把它独立成文件，便于在需要时按顺序 `include`，保持 `define.v` 本身只负责“定义”。
- **练习 2**：如果我想知道项目支持哪些整数 ALU 操作，应该去 `define.v` 里搜什么关键词？
  - **答案**：搜功能函数宏前缀 `FN_`（如 `FN_ADD`、`FN_SUB`、`FN_SLT`），它们是各执行单元操作类型的统一编码。

### 4.2 公共单元库 src/common_cell

#### 4.2.1 概念说明

`src/common_cell/` 是项目的“标准件库”。里面放的不是某块专属业务逻辑，而是**到处都会复用的通用电路单元**：各种 FIFO、仲裁器、计数器、编码转换等。

把通用单元抽到独立目录有三个好处：

1. **复用**：FIFO、仲裁器几乎每个子系统都要用，集中维护避免重复造轮子。
2. **风格统一**：全项目的握手时序（valid/ready）、复位风格保持一致。
3. **可测试**：通用单元可单独验证，复用时心里有底。

该目录主要包含（按功能分类）：

| 类别 | 代表文件 | 说明 |
| --- | --- | --- |
| FIFO 系列 | `fifo.v`、`stream_fifo.v`、`stream_fifo_useSRAM.v`、`fifo_with_count.v`、`fifo_with_flush.v` 等 | 同步/流式 FIFO，含带计数、带 flush、基于 SRAM 等多种变体 |
| 仲裁器 | `fixed_pri_arb.v`、`round_robin_arb.v` | 固定优先级与轮询仲裁 |
| 组合运算 | `pop_cnt.v`、`find_first.v` | population count（数 1 的个数）、找第一个 1 |
| 编码转换 | `bin2one.v`、`one2bin.v`、`input_reverse.v` | 二进制↔独热码转换等 |
| 存储原语 | `dualportSRAM.v` | 双口 SRAM 行为模型 |

> 这些单元的具体实现与选用技巧将在 u8-l2 详细讲解；本讲重点是“它们住在 `common_cell` 里、是全项目共享的”。

#### 4.2.2 核心流程

公共单元的复用流程：

1. 某子系统（如 LSU、cache）需要一个 FIFO 或仲裁器。
2. 设计者直接例化 `common_cell` 里的对应模块，传入所需 `parameter`（位宽、深度）。
3. 因为这些单元接口统一、行为已知，子系统设计者无需关心其内部实现。

#### 4.2.3 源码精读

以最简单的 [src/common_cell/fifo.v](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/common_cell/fifo.v) 为例，体会公共单元的“参数化 + 通用接口”风格：

- [src/common_cell/fifo.v:12](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/common_cell/fifo.v#L12)：文件头注释写明用途——“Synchronous fifo, fifo's depth can't be zero.”（同步 FIFO，深度不能为 0）。这就是它的使用约束。
- [src/common_cell/fifo.v:16-29](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/common_cell/fifo.v#L16-L29)：模块声明用 `parameter` 暴露 `DATA_WIDTH`（数据位宽）和 `FIFO_DEPTH`（深度），端口为标准的 `w_en_i`/`r_en_i`/`w_data_i`/`r_data_o`/`full_o`/`empty_o`。任何调用者只要传两个参数就能用。
- [src/common_cell/fifo.v:31](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/common_cell/fifo.v#L31)：用 `$clog2(FIFO_DEPTH)` 自动计算地址宽度，并对深度为 1 的边界做了特判——体现了公共单元要照顾各种参数取值的工程严谨性。
- [src/common_cell/fifo.v:94-95](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/common_cell/fifo.v#L94-L95)：`full_o` / `empty_o` 通过读写指针比较生成，是经典的格雷式判满判空逻辑。

该文件在主清单中的位置见 [src/gpgpu_top/model_list:184](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/model_list#L184)。

#### 4.2.4 代码实践（源码阅读型）

1. **实践目标**：感受“同一类单元有多个变体”的工程组织方式。
2. **操作步骤**：在 `src/common_cell/` 目录下，列出所有以 `stream_fifo` 开头的文件，对比它们的文件名后缀（`_useSRAM`、`_with_count`、`_hasflush_true`、`_pipe_true` 等）。
3. **观察现象**：文件名后缀直接反映了该 FIFO 变体的特性（用 SRAM 实现 / 带计数 / 带 flush / 组合直通等）。
4. **预期结果**：你能根据名字猜出每个变体的适用场景，例如需要大深度时选 `stream_fifo_useSRAM`。
5. 各变体的内部差异留待 u8-l2 精读，本讲只做目录层面的归类。

#### 4.2.5 小练习与答案

- **练习 1**：`fixed_pri_arb`（固定优先级）与 `round_robin_arb`（轮询）仲裁器，在什么场景下应分别选用？
  - **答案**：当请求有明显优先级差异（如控制通路优先于数据通路）时用固定优先级，保证高优先级请求不被饿死；当多个对等请求需要公平轮流服务（如多个 SM 访问 L2）时用轮询仲裁，避免某个请求长期得不到授权。
- **练习 2**：`bin2one` 和 `one2bin` 分别做什么？为什么 GPU 里会常用到？
  - **答案**：`bin2one` 把二进制编址译成独热（one-hot）码，`one2bin` 反之。GPU 里大量按 lane（线程）使能、掩码操作都习惯用独热码表示（如活跃掩码每一位代表一个 lane），所以这两种转换非常常用。

### 4.3 顶层与核心 RTL src/gpgpu_top

#### 4.3.1 概念说明

`src/gpgpu_top/` 是项目的主体，几乎所有 RTL 都在这里。它本身又分成若干子目录，每个子目录对应上一讲提到的一个硬件部件：

```
src/gpgpu_top/
├── GPGPU_top.v            ← 顶层模块（聚合所有部件）
├── gpgpu_axi_top.sv       ← 带对外 AXI 接口的顶层封装
├── gpgpu_axi_adpater.v    ← AXI 适配相关
├── axi4lite_2_cta.v       ← AXI4-Lite 主机接口 → CTA 派发
├── sm2cluster_arb.v       ← 各 SM 请求 → cluster 级仲裁
├── cluster_to_l2_arb.v    ← cluster → L2 的请求仲裁
├── l2_distribute.v        ← 把请求分发到各 L2
├── model_list             ← 主文件清单（汇总全部 RTL 路径）
├── cta_top/               ← CTA 调度子系统（基于 MIAOW）
├── sm/                    ← SM 核（流水线 + L1 cache）
├── l2cache/               ← L2 Cache（基于 SiFive block-inclusivecache）
└── axi4_adapter/          ← TileLink → AXI4 协议转换
```

四个子目录的职责一句话总结：

| 子目录 | 功能 |
| --- | --- |
| `cta_top/` | 接收主机下发的 workgroup，查资源表，把 workgroup 派发到某个 SM（CU） |
| `sm/` | SM 核：取指/译码/发射/执行/写回流水线，外加 icache、dcache、共享内存 |
| `l2cache/` | 片上 L2 缓存，含目录、banked store、MSHR 等 |
| `axi4_adapter/` | 把内部 TileLink 风格接口转换成对外的 AXI4 |

`sm/` 是规模最大的部分，它又分两块：

```
src/gpgpu_top/sm/
├── sm_wrapper.v           ← SM 核外壳，对外暴露接口
├── cta2warp.v             ← workgroup → warp 拆分桥梁
├── l1cache_arb.v          ← icache/dcache/shared 三路仲裁
├── pipeline/              ← 主流水线（取指→写回）
└── l1cache/               ← L1 存储子系统
      ├── icache/          ← 指令 cache
      ├── dcache/          ← 数据 cache
      ├── shared_memory/   ← 共享内存（LDS）
      └── common/          ← sram 模板、lru 矩阵等公共件
```

`sm/pipeline/` 下按执行单元和流水级分目录，这是全项目文件最密集的区域：

```
src/gpgpu_top/sm/pipeline/
├── pipe.v                 ← 流水线顶层（串起所有阶段）
├── decodeUnit.v           ← 译码
├── issue.v                ← 发射
├── scoreboard.v           ← 记分板（冒险检测）
├── aluexe.v / writeback.v / branch_back.v / ibuffer2issue.v
├── ibuffer/               ← 指令缓冲
├── operand_collector/     ← 操作数采集 + 标量/向量寄存器堆
├── warp_scheduler/        ← warp 调度与 PC 控制
├── csr/                   ← CSR 文件与执行
├── valu/                  ← 向量整数 ALU
├── vmul/                  ← 向量乘法（阵列乘法器）
├── fpu/                   ← 浮点单元（标量 FPU + 向量 vFPU）
├── sfu_v2/                ← 特殊功能单元（除法/开方等，含 float_div_mvp）
├── lsu/                   ← 访存单元（load/store）
├── simt_stack/            ← SIMT 栈（分支发散与汇合）
└── tensor/                ← 张量核（矩阵乘）
```

其中 `valu/`、`vmul/`、`fpu/`、`sfu_v2/`、`lsu/`、`tensor/` 就是 SM 的各执行单元；`simt_stack/`、`csr/`、`warp_scheduler/`、`operand_collector/`、`ibuffer/` 则是流水线前端的控制与数据通路。这些目录的内部细节将在单元 3~5 逐个展开。

#### 4.3.2 核心流程

`src/gpgpu_top/` 的目录划分本质上对应数据通路：

1. 对外接口层：`gpgpu_axi_top.sv` + `axi4_adapter/` + `axi4lite_2_cta.v` 负责 AXI 对接。
2. 调度层：`cta_top/` 把主机 workgroup 派发给 SM。
3. 计算层：每个 `sm/` 核内部走 `pipeline/` 流水线，访存请求经 `l1cache/` → `l1cache_arb.v` 对外。
4. 互联层：`sm2cluster_arb.v` / `l2_distribute.v` / `cluster_to_l2_arb.v` 把多 SM 请求汇聚到 `l2cache/`。

#### 4.3.3 源码精读

主文件清单 `model_list` 是理解“顶层如何聚合子系统”的钥匙——它按顺序列出了全部要编译的 RTL 文件：

- [src/gpgpu_top/model_list:162](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/model_list#L162)：第 162 行是 `GPGPU_top.v`，即真正的硬件顶层模块；清单里其余文件都是它的子模块。
- 整份 [src/gpgpu_top/model_list](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/model_list) 按子系统分段：开头是 `define`，随后依次是 `cta_top/`、`sm/l1cache/`、`sm/pipeline/`（含 fpu、operand_collector、lsu、simt_stack、valu、tensor、ibuffer、vmul、csr 等）、`sm/` 外壳、顶层互联（`sm2cluster_arb`、`axi4lite_2_cta`、`l2_distribute`、`l2cache/`、`axi4_adapter/`），最后以 `GPGPU_top.v` 收尾。

> 小贴士：`model_list` 里路径都形如 `../../../src/...`，这是相对于**仿真执行目录**（`testcase/.../tc_xxx/`）的路径——从测试用例目录上溯三级正好到仓库根。这一约定在 4.4.3 还会用到。

README 的“致谢”表也间接印证了这套子系统的来源，可与目录一一对应：[README.md:205-213](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/README.md#L205-L213)（CTA scheduler 来自 MIAOW、L2Cache 受 SiFive 启发、FPU/SFU/部分配置分别参考 XiangShan、pulp-platform、rocket-chip）。

#### 4.3.4 代码实践（源码阅读型）

1. **实践目标**：通过 `model_list` 而不是文件浏览器，快速建立“一个子系统包含哪些文件”的认知。
2. **操作步骤**：打开 `src/gpgpu_top/model_list`，只看路径里的目录名，统计 `sm/pipeline/` 下共有多少个执行单元目录（提示：找 `valu/`、`vmul/`、`fpu/`、`sfu_v2/`、`lsu/`、`tensor/` 等）。
3. **观察现象**：你会看到同一目录的文件在清单里集中出现，便于按子系统阅读。
4. **预期结果**：能数出至少 6 个执行单元相关目录，并说出它们各自大致负责什么运算。
5. 这一步只读清单，不打开具体 RTL，目的是训练“看清单定位代码”的能力。

#### 4.3.5 小练习与答案

- **练习 1**：`gpgpu_axi_top.sv` 和 `GPGPU_top.v` 都是“顶层”，它们是什么关系？
  - **答案**：`GPGPU_top.v` 是纯粹的 GPGPU 核心顶层（聚合 CTA、SM、L2 等）；`gpgpu_axi_top.sv` 是在它外面再包一层，加上 `axi4_adapter` 把内部接口转成对外 AXI。需要 AXI 对外接口的仿真/综合用前者，不需要的可以直接用后者。
- **练习 2**：`sm/pipeline/` 下既有 `valu/` 又有 `vmul/`，为什么不合并成一个“运算单元”目录？
  - **答案**：两者实现思路与电路差异很大——`valu` 是按 lane 并行的整数 ALU，`vmul` 是基于阵列乘法器（`array_multiplier`，参考香山）的乘法通路，时序、面积、复用关系都不同。分目录便于独立维护与替换。

### 4.4 仿真用例 testcase

#### 4.4.1 概念说明

`testcase/` 存放所有仿真测试用例和 testbench。它分成两个平行的平台：

- `testcase/test_gpgpu_axi_top/`：**带对外 AXI 接口**的仿真平台（主机经 AXI4-Lite 派发，访存经 AXI4）。这是 README 推荐的主路径。
- `testcase/test_gpgpu_top/`：**不带 AXI 接口**的仿真平台，直接连 cache 接口。README 第 56 行说明：“如果不需要对外的 AXI 接口，则进入 `testcase/test_gpgpu_top/tc_gaussian`，步骤同上”。详见 [README.md:56](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/README.md#L56)。

以 `test_gpgpu_axi_top/` 为例，其结构如下：

```
testcase/test_gpgpu_axi_top/
├── common/                ← 公共 testbench 与编译清单
│   ├── test_gpu_axi_top.sv ← 仿真顶层（例化 GPGPU + host + ram）
│   ├── host_inter.sv       ← 模拟主机，经 AXI4-Lite 派发 workgroup
│   ├── axi_ram.sv          ← 模拟外部 AXI 内存
│   ├── gen_clk.v / gen_rst.v ← 时钟/复位产生
│   ├── run.f               ← VCS 编译选项 + 文件清单入口
│   └── file_list.f         ← testbench 自身的文件清单
├── tc_vecadd/             ← 向量加法用例（含 Makefile、softdata/）
├── tc_matadd/             ← 矩阵加法
├── tc_nn/                 ← 最近邻内插
├── tc_gaussian/           ← 高斯消元（README 示例）
└── tc_bfs/                ← 宽度优先搜索
```

每个 `tc_*` 用例目录里通常有：

- `Makefile`：提供 `make run-vcs-4w4t` 这类目标（`4w4t` 表示 4 warp × 4 thread）。
- `softdata/<warp x thread>/`：预编译好的内核与数据（`.data` 程序数据、`.metadata` 元信息、`.log` 参考输出），按不同的 warp/thread 配置分子目录。
- 一个指向 `common/run.f` 的引用，把 testbench 与 RTL 串起来。

#### 4.4.2 核心流程

一次仿真的启动流程（细节在 u1-l4 展开）：

1. 进入某个 `tc_*` 目录，确认 `define.v` 里的 `NUM_THREAD` 与目标配置一致。
2. 执行 `make run-vcs-4w4t`，Makefile 调用 VCS，用 `common/run.f` 作为编译清单。
3. `run.f` 通过 `-f` 拉入 testbench 清单 `file_list.f` 和 RTL 主清单 `model_list`。
4. 仿真运行，host_inter 加载内核、派发 workgroup，最终打印 `PASSED` 或 `FAILED`。

#### 4.4.3 源码精读

`run.f` 是连接 testbench 与 RTL 的“总线”，逐段看 [testcase/test_gpgpu_axi_top/common/run.f](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/testcase/test_gpgpu_axi_top/common/run.f)：

- [run.f:18](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/testcase/test_gpgpu_axi_top/common/run.f#L18)：`-top test_gpu_axi_top` 指定仿真顶层模块名。
- [run.f:23](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/testcase/test_gpgpu_axi_top/common/run.f#L23)：`-f ../common/file_list.f` 把 testbench 源文件纳入编译。
- [run.f:28](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/testcase/test_gpgpu_axi_top/common/run.f#L28)：`+incdir+../../../src/define/` 告诉仿真器去 `src/define/` 找 `` `include "define.v" `` 的头文件——这正是 4.1 提到的配置头文件被找到的方式。
- [run.f:33](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/testcase/test_gpgpu_axi_top/common/run.f#L33)：`-f ../../../src/gpgpu_top/model_list` 把整个 RTL 主体（4.3 的主清单）拉入仿真。

而 testbench 自身的清单 [testcase/test_gpgpu_axi_top/common/file_list.f](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/testcase/test_gpgpu_axi_top/common/file_list.f) 则只有寥寥几行（见 [file_list.f:1-6](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/testcase/test_gpgpu_axi_top/common/file_list.f#L1-L6)）：`host_inter.sv`、`test_gpu_axi_top.sv`、`gen_rst.v`、`gen_clk.v`、`axi_ram.sv` 以及用例自己的 `tc.v`。可以看出：**RTL 用 `model_list` 汇总，testbench 用 `file_list.f` 汇总，两者再被 `run.f` 统一调度**。

#### 4.4.4 代码实践（源码阅读型）

1. **实践目标**：理解“一个测试用例如何被组织起来”。
2. **操作步骤**：打开 `testcase/test_gpgpu_axi_top/tc_vecadd/Makefile`，找到它调用的仿真命令和引用的 `run.f` 路径；再看 `softdata/` 下有哪些 warp/thread 子目录。
3. **观察现象**：`Makefile` 通过不同目标（如 `run-vcs-4w4t`、`run-vcs-4w8t`）切换不同配置；`softdata/` 里的 `.data`/`.metadata` 文件名与 README 测试表对应。
4. **预期结果**：能说清“Makefile → run.f → file_list.f + model_list”这条引用链。
5. 实际跑仿真的完整步骤留到 u1-l4；本讲只做目录与清单层面的梳理。

#### 4.4.5 小练习与答案

- **练习 1**：`run.f` 里为什么需要 `+incdir+../../../src/define/` 这一行？删掉会怎样？
  - **答案**：因为各 RTL 文件用 `` `include "define.v" `` 引入配置宏，仿真器需要知道去哪个目录找 `define.v`。删掉后编译会报“找不到 define.v”的错误。
- **练习 2**：`test_gpgpu_axi_top` 和 `test_gpgpu_top` 两个平台，本质区别在哪？
  - **答案**：前者把 GPGPU 包成带 AXI 对外接口的形态（用 `gpgpu_axi_top.sv` + `axi4_adapter` + `axi_ram` 模拟外部内存 + `host_inter` 经 AXI4-Lite 派发）；后者跳过 AXI，直接用 cache 接口对外，平台更轻量。仿真目标决定了选哪个。

## 5. 综合实践

把本讲的知识串起来，完成下面这个“画地图”任务（对应本讲的核心实践）：

**任务**：为 `src/gpgpu_top/` 绘制一张目录树状图，并配上功能说明。

1. 用树状图整理 `src/gpgpu_top/` 下的四个子目录（`cta_top/`、`sm/`、`l2cache/`、`axi4_adapter/`），为每个子目录写一句话功能说明（可参考 4.3.1 的表格）。
2. 进一步展开 `sm/pipeline/`，统计并列出其中的**执行单元目录**（如 `valu/`、`vmul/`、`fpu/`、`sfu_v2/`、`lsu/`、`tensor/`），每个写一句话指出它负责的运算类型。
3. 在树状图上用箭头标注一条请求路径：主机请求 → `cta_top/`（派发）→ `sm/`（执行）→ `sm2cluster_arb.v` + `l2_distribute.v`（互联）→ `l2cache/`（L2）→ `axi4_adapter/`（对外）。
4. 最后在 `model_list` 里抽查：你标注的每个子目录，是否都能在清单中找到对应的文件段？这能验证你画的地图与实际编译范围一致。

**验收标准**：图里至少覆盖 4 个子系统目录 + 6 个执行单元目录，且每个标注都能在 `model_list` 或源码中找到出处。这是后续每一讲深入某个子系统前的“随身地图”。

## 6. 本讲小结

- 仓库顶层主要由 `README.md`、`docs/`、`src/`、`testcase/`、`FPGA_test/` 构成；`src/` 是 RTL 主体，`testcase/` 是仿真平台。
- `src/define/`（`define.v` + `undefine.v`）是全项目的配置总开关，`NUM_THREAD`/`NUM_WARP`/`NUM_SM` 等规模参数都集中在这里，仿真前必须确认。
- `src/common_cell/` 是公共单元库（FIFO、仲裁器、popcount 等），全项目复用，风格统一、参数化。
- `src/gpgpu_top/` 是核心 RTL，按 `cta_top/`（调度）、`sm/`（核与流水线）、`l2cache/`（L2）、`axi4_adapter/`（AXI 对接）分目录；`sm/pipeline/` 下按执行单元再细分。
- `src/gpgpu_top/model_list` 是主文件清单，汇总了全部 RTL；`run.f` 通过 `-f` 把它和 testbench 的 `file_list.f` 一起送进仿真器。
- `testcase/` 有带 AXI（`test_gpgpu_axi_top/`）和不带 AXI（`test_gpgpu_top/`）两个平台，每个 `tc_*` 用例含 `Makefile` 与预编译好的 `softdata/`。

## 7. 下一步学习建议

- **下一讲 u1-l3（核心配置参数 define.v）**：本讲只指出了 `define.v` 的位置，下一讲将逐组精读其中的规模参数、cache 参数与编码宏。
- **u1-l4（仿真环境搭建与用例运行）**：把本讲 4.4 看到的 `Makefile` / `run.f` 真正跑起来，亲手得到 `PASSED`/`FAILED`。
- **u1-l5（顶层模块 GPGPU_top 与系统数据流）**：进入 `src/gpgpu_top/GPGPU_top.v`，把本讲的目录地图落实成信号级的数据通路。
- 在进入任何子系统的精读前，建议先把本讲的目录树打印出来贴在手边——后面所有讲义都会频繁引用这里的路径。
