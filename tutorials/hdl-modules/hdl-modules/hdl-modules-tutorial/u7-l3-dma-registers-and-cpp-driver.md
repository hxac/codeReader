# DMA 寄存器定义与 C++ 驱动

## 1. 本讲目标

在 u7-l2 里，我们已经看清 `dma_axi_write_simple` 这条「流→DDR」的数据通路，以及 `buffer_start_address` / `buffer_end_address` / `buffer_written_address` / `buffer_read_address` 这四个地址如何构成 FPGA 写、CPU 读的环形缓冲契约。但那四个地址（以及 `enable`、中断状态）到底「在哪里被定义」「硬件怎么认识它们」「软件怎么访问它们」，我们一直黑盒处理。本讲就打开这个黑盒。

学完本讲，你应当能够：

1. 读懂 `regs_dma_axi_write_simple.toml`，说出每个寄存器的访问模式（`r` / `w` / `r_w` / `r_wpulse`）和它里面的位字段。
2. 说清 `hdl-registers` 如何以这份 toml 为「单一信息源」，同时生成 VHDL 寄存器包与 C++ 头/实现，并知道哪些文件是「生成的、不入库」。
3. 读懂 `cpp/` 下手写的 `DmaNoCopy` 驱动，解释它如何用 `buffer_written_address` 与 `buffer_read_address` 两个游标算出「可消费数据量」，以及为什么它能把缓冲指针直接交给用户而不拷贝（zero-copy）。

## 2. 前置知识

本讲是专家层，需要你已掌握下面这些概念（前序讲义已建立）：

- **寄存器访问模式**（u6-l1）：`register_file_pkg` 用 `register_mode_t` 枚举定义了五种模式——`r`（软件读 fabric 值）、`w`（软件写给硬件）、`r_w`（读写回环）、`wpulse`（单拍写脉冲）、`r_wpulse`（读硬件值 + 写清零脉冲）。本讲的 toml 就是这套模式的「声明式写法」。
- **中断寄存器**（u6-l1）：`interrupt_register` 用粘滞 `status` + `mask` + `clear` 聚合成单比特 `interrupt`。本讲会看到 DMA 的多个中断源如何映射成 toml 里的位字段。
- **DMA 的环形缓冲契约**（u7-l2）：四个 buffer 地址、`write_done` / `write_error` / 三个对齐错误中断、以及 `dma_axi_write_simple_axi_lite` 这个套了 AXI-Lite 寄存器文件的顶层。
- **AXI-Lite 寄存器文件**（u5-l4、u6-l1）：CPU 通过 AXI-Lite 单拍事务读写寄存器。

一个需要重新强调的关键认知：**真实工程里的寄存器清单，绝大多数不是手写 VHDL，而是由 `hdl-registers` 从一份 toml 自动生成的**（u6-l1 结尾埋的伏笔）。本讲就是把这条「从 toml 到 VHDL 再到 C++」的生成链完整走一遍。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 | 性质 |
|------|------|------|
| `modules/dma_axi_write_simple/regs_dma_axi_write_simple.toml` | 寄存器清单的**单一信息源**：声明每个寄存器的模式、位字段、描述 | 手写、入库 |
| `tools/build_docs.py` | 调用 `hdl-registers` 的各 generator，从 toml 生成 VHDL 与 C++ 制品 | 手写、入库 |
| `modules/dma_axi_write_simple/src/dma_axi_write_simple_axi_lite.vhd` | DMA 顶层，实例化「生成的」AXI-Lite 寄存器文件 | 手写、入库 |
| `modules/dma_axi_write_simple/cpp/include/dma_axi_write_simple_no_copy.h` | zero-copy C++ 驱动的接口与文档 | 手写、入库 |
| `modules/dma_axi_write_simple/cpp/dma_axi_write_simple_no_copy.cpp` | zero-copy C++ 驱动的实现 | 手写、入库 |

另外有两类「不在仓库里、构建时才生成」的文件，本讲会反复提到它们的名字（见 `dma_axi_write_simple.rst` 的 Hard coded artifacts 清单）：

- VHDL 制品：`dma_axi_write_simple_regs_pkg.vhd`、`dma_axi_write_simple_register_record_pkg.vhd`、`dma_axi_write_simple_register_file_axi_lite.vhd`、`dma_axi_write_simple_register_read_write_pkg.vhd`（仅仿真）。
- C++ 制品：`i_dma_axi_write_simple.h`、`dma_axi_write_simple.h`、`dma_axi_write_simple.cpp`。

注意区分：`cpp/dma_axi_write_simple_no_copy.{h,cpp}` 是**手写**的业务驱动，它 `#include` 了**生成**的 `dma_axi_write_simple.h`。这两层不要混淆。

---

## 4. 核心概念与源码讲解

### 4.1 toml 寄存器清单：用模式与位字段描述硬件接口

#### 4.1.1 概念说明

`hdl-registers` 的核心思想是「**单一信息源（single source of truth）**」：寄存器的名字、地址、位宽、访问模式、位字段、文档描述，只在一份 toml 文件里写一次；然后用代码生成器把这一份声明「投影」成 VHDL 寄存器包、C++ 头文件、甚至 Markdown 文档表。改寄存器只需改 toml，所有下游产物重新生成，永远不会出现「VHDL 改了、C++ 忘了改」的脱节。

toml 用的是最朴素的语法：

- 每个 `[寄存器名]` 是一张表（TOML table），对应一个寄存器。
- `mode = "..."` 声明它的访问模式，取值正是 u6-l1 里 `register_mode_t` 的那五种。
- `description = """..."""` 是多行文档，会原样进入生成的文档与代码注释。
- 寄存器内的位字段写成 `字段名.type = "bit"`（单比特）等子键，每个字段也可带 `description`。

#### 4.1.2 核心流程

先总览 DMA 这份 toml 一共声明了哪些寄存器：

| 寄存器 | 模式 | 含义 | 方向 |
|--------|------|------|------|
| `interrupt_status` | `r_wpulse` | 中断状态（读硬件值；写 1 清零） | 硬件置位、软件读 + 写清零 |
| `interrupt_mask` | `r_w` | 中断使能掩码 | 软件读写 |
| `config` | `r_w` | 配置（含 `enable` 位） | 软件读写 |
| `buffer_start_address` | `w` | 缓冲首字节地址 | 软件写 → 硬件 |
| `buffer_end_address` | `w` | 缓冲尾后一字节地址 | 软件写 → 硬件 |
| `buffer_written_address` | `r` | FPGA 已写到的地址 | 硬件写 → 软件读 |
| `buffer_read_address` | `w` | 软件已消费到的地址 | 软件写 → 硬件 |

注意这七行**完整对应** u7-l2 讲过的环形缓冲契约：硬件推进 `buffer_written_address`，软件推进 `buffer_read_address`，二者在 `[start, end)` 这个环形窗口里你追我赶。toml 只是把这份契约「形式化」：每个寄存器的 `mode` 直接编码了它是「谁写给谁」。

`interrupt_status` 是唯一带位字段的寄存器（其余要么是单个 `bit`，要么是整字地址值）。它的五个位正好是 u7-l2 提到的全部中断源：

| 位字段 | 含义 |
|--------|------|
| `write_done` | 一个 packet（`packet_length_beats` 拍）已写入内存 |
| `write_error` | 内存写返回错误（BRESP） |
| `start_address_unaligned_error` | `buffer_start_address` 未按 packet 长度对齐 |
| `end_address_unaligned_error` | `buffer_end_address` 未对齐 |
| `read_address_unaligned_error` | `buffer_read_address` 未对齐 |

#### 4.1.3 源码精读

先看 `interrupt_status` 的声明，它是 `r_wpulse` 模式的典型：

[regs_dma_axi_write_simple.toml:L2-L4](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/dma_axi_write_simple/regs_dma_axi_write_simple.toml#L2-L4) 声明寄存器名与 `mode = "r_wpulse"`。这个模式（承接 u6-l1）意味着：软件**读**它得到硬件当前置位的中断位，软件**写 1** 到某位则把该位清零（写脉冲）。toml 的 `description` 把这套语义讲得很清楚：

[regs_dma_axi_write_simple.toml:L5-L17](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/dma_axi_write_simple/regs_dma_axi_write_simple.toml#L5-L17) 解释「某 bit 触发后一直读为 1，直到被写清；若对应 `interrupt_mask` 位也置 1，则模块的 `interrupt` 信号拉高」。注意它还提示了一条**轮询替代路径**：不用中断工作流时，软件可以直接轮询这个寄存器查事件——这与后面 C++ 驱动的 `check_status()` 直接呼应。

接着是五个位字段，例如 `write_done`：

[regs_dma_axi_write_simple.toml:L19-L24](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/dma_axi_write_simple/regs_dma_axi_write_simple.toml#L19-L24) 用 `write_done.type = "bit"` 声明一个单比特字段，并提示「比较 `buffer_written_address` 与 `buffer_read_address` 可知写了多少字节、写到了哪里」。这条提示正是 4.3 节 zero-copy 驱动的核心算法依据。

再看配置寄存器 `config`，它是 `r_w` 模式、内含一个 `enable` 位：

[regs_dma_axi_write_simple.toml:L59-L75](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/dma_axi_write_simple/regs_dma_axi_write_simple.toml#L59-L75) 规定 `enable` 置位前必须先设好三个 buffer 地址；置位后模块才连续吃流；并且「**不支持 disable**——清零会导致未定义行为」。这条硬约束在 C++ 驱动的 `setup_and_enable()` 里会被一条断言守护。

最后看两个方向相反的地址寄存器，体会 `r` 与 `w` 的区别：

[regs_dma_axi_write_simple.toml:L113-L134](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/dma_axi_write_simple/regs_dma_axi_write_simple.toml#L113-L134) 是 `buffer_written_address`，`mode = "r"`：硬件持续更新它，软件只读。描述里写明了环形缓冲的两条关键不变量——「读地址与该地址相等则缓冲为空」「该寄存器永远回绕到 `start`、永不等于 `end`」（即半开区间，承接 u7-l2 的「written 永不等于 end」）。

[regs_dma_axi_write_simple.toml:L138-L156](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/dma_axi_write_simple/regs_dma_axi_write_simple.toml#L138-L156) 是 `buffer_read_address`，`mode = "w"`：软件持续写它，硬件只读。描述要求「软件消费完 `[read, written)` 的数据后，必须把该寄存器更新为 `buffer_written_address`」，这把「软件释放空间」的协议写死在了寄存器文档里。

> 顺带留意两处都提到 `address_width` 这个 generic：虽然寄存器是 32 位的，但 FPGA 实际只使用低 `address_width` 位，高位读作零。这正是 `dma_axi_write_simple_axi_lite.vhd` 里 `address_width : axi_address_width_t` generic 的用途。

#### 4.1.4 代码实践

**实践目标**：把 toml 当作一份「硬件接口规格书」来读，独立列出全部寄存器与位字段。

**操作步骤**：

1. 打开 `modules/dma_axi_write_simple/regs_dma_axi_write_simple.toml`。
2. 用搜索（或肉眼）数出所有形如 `[xxx]` 的顶层表，共应有 7 个。
3. 对每个寄存器，记下它的 `mode`；对 `interrupt_status`，额外记下它的 5 个 `*.type = "bit"` 子键。
4. 把结果整理成上面 4.1.2、4.1.3 那样的两张表。

**需要观察的现象**：

- 每个寄存器的 `mode` 与「数据流方向」是否一一对应（硬件写→软件读的是 `r`；软件写→硬件读的是 `w`；纯配置是 `r_w`；中断状态是 `r_wpulse`）。
- `description` 里反复出现的对齐要求（按 packet 长度对齐）和 `address_width` 截断提示。

**预期结果**：7 个寄存器，模式分别为 `r_wpulse / r_w / r_w / w / w / r / w`；`interrupt_status` 含 5 个位字段。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `buffer_written_address` 是 `r` 模式，而 `buffer_read_address` 是 `w` 模式？

**参考答案**：`written` 由 FPGA 持续推进（硬件写给总线、软件读），所以软件只读，是 `r`；`read` 由软件持续推进（软件消费数据后写给硬件、硬件据此知道哪些空间可重用），所以软件只写，是 `w`。方向相反，模式相反。

**练习 2**：如果想让某个中断「只在中断工作流里上报、轮询时不可见」，应该动哪个寄存器？为什么 `interrupt_status` 用 `r_wpulse` 而不是 `r_w`？

**参考答案**：动 `interrupt_mask`（`r_w`，清掉对应位即可屏蔽该中断对 `interrupt` 信号的影响）。`interrupt_status` 用 `r_wpulse` 而非 `r_w`，是因为中断状态需要「读出后能用写脉冲清零」——`r_w` 是读写回环（写进去什么读出来什么），无法表达「写 1 清硬件置位」的脉冲语义。

---

### 4.2 hdl-registers 代码生成：从 toml 到 VHDL 与 C++ 制品

#### 4.2.1 概念说明

toml 只是声明，真正让硬件和软件都「认识」这些寄存器的，是 `hdl-registers` 的代码生成器（generator）。每个 generator 负责把同一份 toml 投影成一种目标产物：

- **VHDL 侧**：寄存器包（地址常量、模式常量）、record 包（把寄存器捆成 `regs_up_t` / `regs_down_t` 记录）、AXI-Lite 封装实体（直接挂到 AXI-Lite 总线）、仿真读写包（testbench 用）。
- **C++ 侧**：接口头（纯虚基类）、头文件（常量、掩码、getter/setter 声明）、实现文件。

关键纪律：**生成的制品不入库**。`dma_axi_write_simple.rst` 明确写着「Generated register code artifacts are not checked in to the repository」。推荐用 tsfpga 集成，则每次构建/仿真都自动重新生成、永远最新；若不依赖 tsfpga，文档网站提供可下载的「硬编码制品」快照（不推荐，容易脱节）。

为什么坚持「不入库」？因为寄存器定义会随版本演进，把生成物也提交进 git，会在 bump 版本时制造大量「假冲突」与「手写 vs 生成」脱节。单一信息源 + 自动生成，是这套流程的核心纪律。

#### 4.2.2 核心流程

生成发生在 `tools/build_docs.py` 的 `generate_register_artifacts()` 里。它遍历所有带寄存器的模块，对每个模块的 `register_list` 调用一组 generator：

```
对每个 module：
    register_list = module.registers        # 解析 toml 得到
    若 register_list 为空：跳过
    否则依次调用 7 个 generator，输出到 doc 构建目录：
        VHDL 4 个：RegisterPackage / RecordPackage / AxiLiteWrapper / SimulationReadWrite
        C++  3 个：Interface / Header / Implementation
```

其中 VHDL 的 `AxiLiteWrapper` generator 生成的就是 `dma_axi_write_simple_register_file_axi_lite.vhd`——也就是 u6-l1 讲的那个 `axi_lite_register_file` 的「实例化壳」。它把 toml 声明的寄存器阵列，自动参数化成一个可直接挂 AXI-Lite 总线的 slave。

这条生成链与手写 VHDL 的衔接点，是 `dma_axi_write_simple_axi_lite.vhd`：它 `use work.dma_axi_write_simple_register_record_pkg.all`（生成的 record 包），声明 `regs_up` / `regs_down` 两个记录信号，一边接 DMA core、一边接生成的寄存器文件壳。

#### 4.2.3 源码精读

先看生成入口 `generate_register_artifacts()`：

[build_docs.py:L127-L153](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/tools/build_docs.py#L127-L153) 是 VHDL 四个 generator：`VhdlRegisterPackageGenerator`（地址/模式常量包）、`VhdlRecordPackageGenerator`（`regs_up/down` 记录）、`VhdlAxiLiteWrapperGenerator`（AXI-Lite slave 壳实体）、`VhdlSimulationReadWritePackageGenerator`（仅仿真的读写辅助包）。注意前三个输出到 `vhdl/`（综合+仿真都进），最后一个也是 `vhdl/` 目录但语义上只用于仿真。

[build_docs.py:L155-L165](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/tools/build_docs.py#L155-L165) 是 C++ 三个 generator：`CppInterfaceGenerator`（`i_dma_axi_write_simple.h` 纯虚接口）、`CppHeaderGenerator`（`dma_axi_write_simple.h` 含类与位掩码常量）、`CppImplementationGenerator`（`dma_axi_write_simple.cpp` 实现）。这三个 C++ 制品正是 4.3 节 `DmaNoCopy` 驱动 `#include "dma_axi_write_simple.h"` 的对象。

再看手写 VHDL 如何消费生成的制品：

[dma_axi_write_simple_axi_lite.vhd:L26-L26](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/dma_axi_write_simple/src/dma_axi_write_simple_axi_lite.vhd#L26-L26) `use work.dma_axi_write_simple_register_record_pkg.all`——引用生成的 record 包，从而能使用 `dma_axi_write_simple_regs_up_t` 等类型。

[dma_axi_write_simple_axi_lite.vhd:L58-L59](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/dma_axi_write_simple/src/dma_axi_write_simple_axi_lite.vhd#L58-L59) 声明 `regs_up`（fabric→总线）与 `regs_down`（总线→fabric）两个记录信号，初值取自生成包里的 `_init` 常量。承接 u6-l1 的「`regs_up` / `regs_down` 搬运数据」。

[dma_axi_write_simple_axi_lite.vhd:L91-L101](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/dma_axi_write_simple/src/dma_axi_write_simple_axi_lite.vhd#L91-L101) 实例化生成的 `dma_axi_write_simple_register_file_axi_lite`（即 `VhdlAxiLiteWrapperGenerator` 的产物），把它接到外部 AXI-Lite 总线（`regs_m2s` / `regs_s2m`）和那两个记录信号之间。这一行就是「toml 声明 → 生成的 slave 实体 → 挂上总线」整条链的落点。

最后看 C++ 侧，手写驱动如何引用生成头里的命名空间与掩码：

[dma_axi_write_simple_no_copy.h:L15-L15](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/dma_axi_write_simple/cpp/include/dma_axi_write_simple_no_copy.h#L15-L15) `#include "dma_axi_write_simple.h"`——引入生成的 C++ 头。

[dma_axi_write_simple_no_copy.h:L21-L35](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/dma_axi_write_simple/cpp/include/dma_axi_write_simple_no_copy.h#L21-L35) 引用生成命名空间 `fpga_regs::dma_axi_write_simple::interrupt_status::write_done::mask_shifted` 等位掩码常量，把 `write_done_interrupt_mask`、`any_error_interrupt_mask`、`all_interrupts_mask` 三个语义常量拼出来。这些 `mask_shifted` 常量正是 `CppHeaderGenerator` 从 toml 的位字段自动算出的——toml 里加一个位字段，这里的掩码就自动多一项，无需手改。

#### 4.2.4 代码实践

**实践目标**：把「文档里列的硬编码制品」与「build_docs.py 里的 generator 调用」一一对应，验证两者是同一份 toml 的两种投影。

**操作步骤**：

1. 打开 `modules/dma_axi_write_simple/doc/dma_axi_write_simple.rst`，找到「Hard coded artifacts」小节，它列了 4 个 VHDL 文件 + 3 个 C++ 文件。
2. 打开 `tools/build_docs.py` 的 `generate_register_artifacts()`（L127–L165）。
3. 逐行对照：每个 generator 调用产出的文件名，是否与 rst 清单一一对应。
4. 注意 rst 里特别标注「最后一个 VHDL 文件（`register_read_write_pkg`）只进仿真工程」——对应 generator 名 `VhdlSimulationReadWritePackageGenerator`。

**需要观察的现象**：rst 的 7 个文件名与 build_docs.py 的 7 个 generator 调用应当完全吻合（4 VHDL + 3 C++）。

**预期结果**：能画出「toml → 7 个 generator → 7 个制品文件」的对应表。

#### 4.2.5 小练习与答案

**练习 1**：为什么生成的寄存器制品不提交进 git 仓库？

**参考答案**：为了维护「单一信息源」纪律。寄存器定义（toml）会随版本演进，若把生成物也入库，每次 bump 版本都会产生大量与手写逻辑无关的 diff，且极易出现「改了 toml 忘了重新生成、入库的生成物与 toml 脱节」。用 tsfpga 在构建/仿真时自动生成，能保证生成物永远与 toml 同步。

**练习 2**：`dma_axi_write_simple_axi_lite.vhd` 里实例化的 `dma_axi_write_simple_register_file_axi_lite` 是手写的还是生成的？它内部等价于 u6-l1 的哪个构建块？

**参考答案**：是 `VhdlAxiLiteWrapperGenerator` 生成的。它内部等价于 u6-l1 的 `axi_lite_register_file`——一个根据 `registers` / `default_values` 参数化、把寄存器阵列挂到 AXI-Lite 总线的 slave，只是这里的 `registers` 清单不是手写 VHDL，而是由 toml 经 generator 自动填充。

---

### 4.3 zero-copy C++ 驱动：环形缓冲契约与 written/read 地址

#### 4.3.1 概念说明

`cpp/dma_axi_write_simple_no_copy.{h,cpp}` 是一个**手写**的、架在生成 C++ 寄存器接口之上的业务驱动。它的核心卖点是 **zero-copy（零拷贝）**：当 FPGA 把流数据写进 DDR 环形缓冲后，驱动**不把数据搬到另一个缓冲**再交给用户，而是直接把 FPGA 正在写的那段 DDR 地址（映射成用户空间指针）返回给用户。这非常高效，但代价是用户必须严格遵守「用完前不许让 FPGA 覆写」的契约。

这个契约由**两个软件游标** + **一个硬件寄存器**共同维持：

- `buffer_written_address`（硬件寄存器，`r`）：FPGA 已经写到哪里。
- `m_in_buffer_read_outstanding_address`（软件游标）：驱动已经「交出去」给用户、但用户还没说「用完了」的字节数（相对 `start` 的偏移）。
- `m_in_buffer_read_done_address`（软件游标）：用户已经「用完了」、可以还给 FPGA 重写的字节数（相对 `start` 的偏移），它会被写回 `buffer_read_address` 寄存器。

三者的关系（均为相对 `start` 的偏移，环形于 `[0, buffer_size)`）：

\[ \text{done} \leq \text{outstanding} \leq \text{written} \]

`receive_data` 把 `[outstanding, written)` 里的数据指针交给用户、推进 `outstanding`；`done_with_data` 推进 `done` 并把 `done` 写回 `buffer_read_address` 寄存器。FPGA 只会写 `[written, read=done)` 这段（不会覆写用户还没用完的 `[done, outstanding)`），所以零拷贝是安全的。

#### 4.3.2 核心流程

驱动的典型使用循环（轮询或中断皆可）：

```
构造 DmaNoCopy(reg_base, buffer, buffer_size, assert_handler)
    └─ 把 buffer 起止地址算好存为 m_start_address / m_end_address

setup_and_enable()
    ├─ 断言尚未 enable（防止重复使能）
    ├─ 写 buffer_start_address = m_start_address
    ├─ 写 buffer_end_address   = m_end_address
    ├─ 写 buffer_read_address  = m_start_address   （整块缓冲初始全空）
    └─ 写 config.enable = 1                         （DMA 开始吃流）

循环：
    response = receive_data(min_bytes, max_bytes)
        ├─ check_status()：读 interrupt_status_raw，写回清零，断言无错误位
        ├─ 读 written_address
        ├─ read_address = start + outstanding
        ├─ num_bytes_available = (written - read) % buffer_size   ← 环形可消费量
        ├─ 若 < min_bytes：返回 0
        ├─ 否则取 min(可消费, max_bytes)；若跨过缓冲尾则只读到尾（保证返回连续段）
        └─ 返回 {num_bytes, 指向 buffer[outstanding] 的指针}，并推进 outstanding

    处理 response.data 这段数据（此时 FPGA 不会覆写它）

    done_with_data(num_bytes)
        ├─ 推进 done
        └─ 写 buffer_read_address = start + done   （把这段空间还给 FPGA）
```

最关键的一行算法是 `num_bytes_available` 的计算——它把两个绝对地址之差折叠成环形缓冲里的「可消费字节数」。

#### 4.3.3 源码精读

先看构造与启动。构造函数把用户传入的缓冲指针转成 32 位物理起止地址：

[dma_axi_write_simple_no_copy.cpp:L36-L52](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/dma_axi_write_simple/cpp/dma_axi_write_simple_no_copy.cpp#L36-L52) 把 `buffer` 指针 reinterpret 成 `volatile uint8_t*` 存为 `m_buffer`，并用 `static_cast<uint32_t>` 把起止地址截到 32 位（与 FPGA 寄存器位宽一致，承接 toml 里「只使用 `address_width` 位」的说明）。`volatile` 关键字很关键：它告诉编译器「这段内存会被 FPGA 在总线侧改写」，禁止把读值缓存在寄存器里。

[dma_axi_write_simple_no_copy.cpp:L54-L63](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/dma_axi_write_simple/cpp/dma_axi_write_simple_no_copy.cpp#L54-L63) 是 `setup_and_enable()`：先断言「还没 enable」（守护 toml 里「不支持 disable、不可重复 enable」的约束），再按 toml 描述的顺序写三个 buffer 地址（`read` 初始置为 `start`，即整块缓冲都空），最后置 `config.enable = true`。注意它调用的 `registers.set_buffer_start_address(...)` 等 getter/setter，全部来自生成的 C++ 头——toml 里每个寄存器/字段都自动生成一对 `get_*` / `set_*`。

接着是本讲的核心算法 `receive_data`：

[dma_axi_write_simple_no_copy.cpp:L72-L77](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/dma_axi_write_simple/cpp/dma_axi_write_simple_no_copy.cpp#L72-L77) 读出硬件的 `written_address`，算出软件读游标 `read_address = start + outstanding`，再用一行模运算得到可消费字节数：

\[ \text{num\_bytes\_available} = (\text{written\_address} - \text{read\_address}) \bmod \text{buffer\_size} \]

这是环形缓冲求「前向距离」的标准写法：`written` 与 `read` 都落在同一个 `[start, start+size)` 窗口里，二者的无符号差就是带符号位移，`% size` 把它折叠进 `[0, size)` 作为「从 read 沿写入方向到 written 的字节数」。

> **深入一点（关于 `% size` 的精确性）**：C/C++ 的无符号减法再取模，只有在 `buffer_size` 为 2 的幂时才严格等于数学意义上的环形前向距离（因为此时 \(2^{64} \bmod \text{size} = 0\)，无符号下溢不会污染取模结果）。项目要求 packet 长度必须是 2 的幂（见 `readme.rst`），把缓冲取成「2 的幂个 packet」即可让 `buffer_size` 也是 2 的幂，从而使该模运算精确。这是 zero-copy 驱动一条隐含的使用前提。

[dma_axi_write_simple_no_copy.cpp:L97-L108](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/dma_axi_write_simple/cpp/dma_axi_write_simple_no_copy.cpp#L97-L108) 处理「数据跨过缓冲尾」的回绕情形：若 `written < read`（说明可消费数据绕过了缓冲末尾），驱动**只读到缓冲尾**（`num_bytes_until_end = end - read`），而不是绕回开头继续读。因为 zero-copy 必须返回**连续**的一段内存，绕回开头的那段要等下一次 `receive_data` 才返回。这正是头文件注释里强调的「末尾拐角可能返回少于 `min_num_bytes`」的由来。

[dma_axi_write_simple_no_copy.cpp:L110-L116](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/dma_axi_write_simple/cpp/dma_axi_write_simple_no_copy.cpp#L110-L116) 把 `&m_buffer[outstanding]`（FPGA 实写的那段 DDR）作为 `data` 指针返回——**没有任何拷贝**——并按本次返回的字节数推进 `outstanding`（同样取模回绕）。

再看「用完归还」：

[dma_axi_write_simple_no_copy.cpp:L119-L126](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/dma_axi_write_simple/cpp/dma_axi_write_simple_no_copy.cpp#L119-L126) `done_with_data` 推进 `done` 游标，并把 `start + done` 写回 `buffer_read_address` 寄存器——这就是 toml 里 `buffer_read_address` 描述的「软件消费完后必须更新此寄存器」协议的软件侧落点。一旦写回，FPGA 就知道 `[done, ...)` 这段可以重写了，所以注释反复警告：**调完 `done_with_data` 后，之前 `receive_data` 返回的指针就不再安全**。

最后看状态检查：

[dma_axi_write_simple_no_copy.cpp:L147-L162](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/dma_axi_write_simple/cpp/dma_axi_write_simple_no_copy.cpp#L147-L162) `check_status()` 读 `interrupt_status_raw`（即 toml 的 `interrupt_status`，`r_wpulse`）：若为零则无事发生；否则**立即写回原值清零**（写 1 清中断，正是 `r_wpulse` 的脉冲语义），再用 4.2.3 里拼出的 `any_error_interrupt_mask` 断言「没有任何错误位」，最后返回 `write_done` 是否触发。这条路径同时支持中断工作流与轮询工作流——后者就是直接调用 `receive_data`，让它内部的 `check_status()` 顺带查事件。

#### 4.3.4 代码实践

**实践目标**：在 C++ 头/实现里找到「读写每个 toml 寄存器」的方法，并亲手解释 zero-copy 如何算出可消费数据量。

**操作步骤**：

1. 打开 `cpp/include/dma_axi_write_simple_no_copy.h`，找到 `setup_and_enable()`（[L120](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/dma_axi_write_simple/cpp/include/dma_axi_write_simple_no_copy.h#L120)）、`receive_data()`（[L176](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/dma_axi_write_simple/cpp/include/dma_axi_write_simple_no_copy.h#L176)）、`done_with_data()`（[L187](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/dma_axi_write_simple/cpp/include/dma_axi_write_simple_no_copy.h#L187)）的文档注释。
2. 在 `cpp/dma_axi_write_simple_no_copy.cpp` 里，搜索所有 `registers.set_*` / `registers.get_*` 调用，列出它们对应的 toml 寄存器：
   - `set_buffer_start_address` / `set_buffer_end_address` / `set_buffer_read_address` → `w` 模式三个地址寄存器。
   - `set_config_enable` / `get_config_enable` → `config.enable` 位。
   - `get_buffer_written_address` → `r` 模式的 written 地址。
   - `get_interrupt_status_raw` / `set_interrupt_status_raw` → `r_wpulse` 的中断状态。
3. 用一个具体数值走查 `receive_data` 的可消费量公式：设 `start=0x1000`、`buffer_size=0x1000`（4096，2 的幂）、`outstanding` 此时为 0、FPGA 已写到 `written_address=0x1200`。手算 `(0x1200 - 0x1000) % 0x1000 = 0x200 = 512` 字节，并对照代码确认。
4. 再走一个回绕场景：`outstanding` 偏移到 `0x1F00`（即 `read_address=0x1F00`）、`written_address=0x1100`（FPGA 已绕回开头写了 0x100 字节）。手算 `(0x1100 - 0x1F00) % 0x1000`，确认它等于「读到尾 0x100 字节 + 绕回头 0x100 字节 = 0x200」中的前半段含义，并解释为何代码这次只返回 `end - read = 0x100` 字节。

**需要观察的现象**：

- 每个 `set_*` / `get_*` 都能在 4.1 节的 toml 表里找到同名寄存器，证明「toml 字段 → C++ 方法」的自动生成关系。
- `num_bytes_available` 在非回绕场景给出直观的正向差；在回绕场景给出环形前向距离。
- 回绕场景下 `receive_data` 受 `if (written_address < read_address)` 分支限制，只返回到缓冲尾的连续段。

**预期结果**：能用一句话说出「zero-copy 驱动用 `(written - read) % buffer_size` 算出 FPGA 已写、软件尚未消费的字节数，然后把那段 DDR 地址直接当指针返回；用户用完后调 `done_with_data` 把 `read` 游标写回硬件，释放空间。」数值走查若无法本地运行驱动，明确标注「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `receive_data` 返回的指针指向 `m_buffer`（`volatile uint8_t*`）而不是一块新分配的内存？用户拿到指针后必须遵守什么规则？

**参考答案**：因为 zero-copy 追求效率，不拷贝数据，直接把 FPGA 实写的 DDR 段交给用户。用户必须遵守：在调用 `done_with_data(num_bytes)` 之前用完（或自行拷走）这段数据；`done_with_data` 一旦把对应空间还给 FPGA，该指针就不再安全，可能被 FPGA 覆写。

**练习 2**：`done_with_data` 与 `buffer_read_address` 寄存器是什么关系？为什么它推进的是 `m_in_buffer_read_done_address` 而不是 `m_in_buffer_read_outstanding_address`？

**参考答案**：`done_with_data` 把 `m_in_buffer_read_done_address` 推进后写回 `buffer_read_address` 寄存器，即 toml 里 `buffer_read_address`（`w` 模式）的软件写入侧。它推进 `done` 而非 `outstanding`，是因为 `outstanding` 表示「已交给用户但还没用完」的量（这部分 FPGA 仍不能覆写），只有用户明确说「用完了」才能推进 `done` 并释放给 FPGA。`done ≤ outstanding` 是不变量。

**练习 3**：`check_status()` 里为什么先 `get_interrupt_status_raw()` 紧接着 `set_interrupt_status_raw(同值)`？

**参考答案**：这是 `r_wpulse` 模式的清零协议：读出当前中断状态后，立即把同一组置位位「写 1」回去，从而清掉它们（写脉冲清零）。这样下一次进入 `check_status` 时，只有新发生的中断才会出现，避免同一个中断被重复处理。若不清零，粘滞的中断位会一直为 1。

---

## 5. 综合实践

把本讲三节串起来，完成一次「从 toml 改一个寄存器，到追踪它如何影响 VHDL、C++、驱动行为」的端到端阅读任务。

**任务**：假设要给 DMA 新增一个「统计已写入 packet 总数」的只读寄存器 `num_packets_written`（硬件维护、软件只读）。

1. **改 toml**：在 `regs_dma_axi_write_simple.toml` 末尾仿照 `buffer_written_address` 加一个 `[num_packets_written]` 块，`mode = "r"`，写一段 `description`。说明你会把硬件侧的计数接到哪个 `regs_up` 字段。
2. **追踪 VHDL 生成**：参照 4.2，说出 `VhdlRegisterPackageGenerator` / `VhdlRecordPackageGenerator` / `VhdlAxiLiteWrapperGenerator` 各会为新寄存器多生成什么（地址常量、record 字段、slave 解码分支）。指出 `dma_axi_write_simple_axi_lite.vhd` 的 `regs_up` 记录会自动多出一个字段，DMA core 需要把计数驱动到该字段。
3. **追踪 C++ 生成**：说出 `CppHeaderGenerator` 会为新寄存器生成哪个 getter（形如 `get_num_packets_written()`），并指出它在生成头里的命名空间位置。
4. **扩展驱动**：在 `DmaNoCopy` 类里加一个 `size_t get_num_packets_written()` 方法，内部调用生成的 `registers.get_num_packets_written()`。讨论：这个只读计数器是否需要走 `check_status` / outstanding 机制？（不需要，它与 zero-copy 游标无关，是独立的状态旁路。）

**验收标准**：能画出「toml 新增一行 → 7 个 generator 各自的增量 → 手写 VHDL/C++ 的接入点」这条链；能说清为什么只读状态寄存器（`r`）天然不需要 zero-copy 那套 done/outstanding 游标。本任务为源码阅读型实践，无需综合/运行；若要实际验证生成产物，需本地配好 tsfpga + hdl-registers 并运行 `tools/build_docs.py`，标注「待本地验证」。

## 6. 本讲小结

- `regs_dma_axi_write_simple.toml` 是 DMA 寄存器接口的**单一信息源**：7 个寄存器，模式分别为 `r_wpulse / r_w / r_w / w / w / r / w`，`interrupt_status` 含 5 个位字段——模式直接编码了「谁写给谁」。
- `hdl-registers` 把这份 toml 投影成 4 个 VHDL 制品（寄存器包、record 包、AXI-Lite slave 壳、仿真读写包）和 3 个 C++ 制品（接口头、头、实现）；**生成物不入库**，靠 tsfpga 在构建时自动生成以避免脱节。
- 手写的 `dma_axi_write_simple_axi_lite.vhd` 用 `regs_up` / `regs_down` 两个记录信号，把 DMA core 与生成的 AXI-Lite 寄存器文件壳接到一起——这是「toml 声明 → 生成 slave → 挂上总线」的落点。
- 手写的 `DmaNoCopy` C++ 驱动架在生成的寄存器接口之上，用 `volatile` 指针 + `(written - read) % buffer_size` 公式实现 **zero-copy**：直接把 FPGA 实写的 DDR 段当指针返回。
- zero-copy 的安全性靠两个软件游标维持：`outstanding`（已交用户、未用完）与 `done`（已用完、已写回 `buffer_read_address` 寄存器释放给 FPGA），不变量为 `done ≤ outstanding ≤ written`。
- 中断经 `r_wpulse` 的 `interrupt_status` 上报：`check_status` 读后立即「写 1 清零」，并用生成的位掩码断言无错误位——同一套机制兼容中断与轮询两种工作流。

## 7. 下一步学习建议

- **横向对照另一个带寄存器的模块**：仓库里目前只有 `dma_axi_write_simple` 带 toml 寄存器，但 `hdl-registers` 项目本身有更完整的示例。可以去 [hdl-registers 官网](https://hdl-registers.com) 看 toml 的完整字段类型（`integer`、`enumeration`、`bit_vector` 等），本讲只用到 `bit` 和默认整字。
- **回到验证侧**：本讲的寄存器在仿真里如何被驱动？结合 u8-l1（BFM）与 u8-l2（VUnit 测试台），阅读 `modules/dma_axi_write_simple/test/tb_dma_axi_write_simple.vhd` 与 `sim/dma_axi_write_simple_sim_pkg.vhd`，看 testbench 如何用生成的 `register_read_write_pkg`（`VhdlSimulationReadWritePackageGenerator` 的产物）来读写这些寄存器，而不用手拼 AXI-Lite 事务。
- **深入 zero-copy 的工程化**：本讲的 `DmaNoCopy` 构造函数只适合裸机（物理==虚拟地址）。读头文件注释里提到的「使用操作系统时需另写构造函数」，思考在 Linux 下用 `mmap` 映射 DMA 缓冲时，`register_base_address` 与 `buffer` 分别应传虚拟地址还是物理地址，以及 `volatile` 在用户空间映射的非对齐访问下还成立吗。
