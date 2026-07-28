# 仓库结构与源码地图

## 1. 本讲目标

学完本讲，你应该能够：

- 说出 tiny-gpu 仓库里每个顶层目录（`src/`、`test/`、`docs/`、`gds/`）的职责。
- 把 `src/` 下的 12 个 SystemVerilog 文件逐一对应到 GPU 架构图中的模块。
- 区分「硬件源码（`.sv`）」和「仿真测试（`.py`）」两类文件，知道它们分别由什么工具处理。
- 看懂 `gpu.sv` 与 `core.sv` 里的两段 `generate` 循环，理解「GPU → 多个 core → 多个 thread」的三层结构是怎么搭起来的。

本讲是整个手册的「地图篇」：我们不深究任何一行电路细节，而是建立一张「文件 ↔ 模块 ↔ 架构图」的对照表，让你在后续每一讲都能随时定位自己在哪。

## 2. 前置知识

承接 [u1-l1 项目概览](u1-l1-project-overview.md)，你已经知道 tiny-gpu 是用 SystemVerilog 写的教学型 GPU。在正式读源码地图前，先建立 5 个最小概念：

1. **硬件描述语言（HDL）**：SystemVerilog/Verilog 不是「运行」的程序，而是用文本描述**电路结构**的语言。`src/` 下的 `.sv` 文件描述的是芯片里真实存在的连线和逻辑门，最终会被综合成硬件（或被仿真器执行）。
2. **模块（module）**：SystemVerilog 的基本积木。一个 `module ... endmodule` 描述一个电路单元，有输入输出端口，像一块带引脚的芯片。
3. **实例化（instantiation）**：一个模块可以在另一个模块内部被「摆放」一次或多次，就像把芯片焊到电路板上。`gpu.sv` 里「摆放」了 `dcr`、`controller`、`core` 等子模块。
4. **`generate` 循环**：SystemVerilog 的语法，用来在编译期**重复生成**多份相同的电路。tiny-gpu 用它复制出多个 core、多个 thread 的硬件资源。
5. **cocotb**：一个让你用 Python 驱动 Verilog 仿真的框架。`test/` 下的 `.py` 文件不是被测对象，而是「测试台」——它们给硬件喂时钟、喂数据，再检查硬件输出。

另外要记住 u1-l1 提到的三层架构分解：**GPU → Core → Thread**。本讲的源码地图完全围绕这个三层结构展开。

## 3. 本讲源码地图

本讲涉及的关键文件如下表，先建立全局印象：

| 文件 | 类型 | 作用 |
| --- | --- | --- |
| `README.md` | 文档 | 项目说明、架构图、ISA、内核示例、仿真方法 |
| `src/gpu.sv` | 硬件源码 | GPU 顶层模块，把 DCR、内存控制器、dispatcher、core 连起来 |
| `src/core.sv` | 硬件源码 | 计算核心，含 1 个 fetcher/decoder/scheduler + 每线程一套 ALU/LSU/registers/PC |
| `Makefile` | 构建 | `sv2v` 转换 + `iverilog` 编译 + `cocotb` 仿真的入口 |
| `test/test_matadd.py` | 仿真测试 | 矩阵加法内核的 cocotb 测试 |
| `docs/images/*.png` | 图片资源 | 架构图（gpu/core/thread）、ISA 图、轨迹样例 |

## 4. 核心概念与源码讲解

### 4.1 src/ 源码模块清单

#### 4.1.1 概念说明

`src/` 目录存放全部硬件源码，一共 **12 个 `.sv` 文件**。每个文件对应一个 `module`，即一块电路。这 12 个模块不是平铺的，而是有明确的层次：顶层 `gpu` 把其他模块「拼装」起来。

对于初学者，最关键的是先记住每个模块的一句话职责：

| 文件 | 模块 | 一句话职责 |
| --- | --- | --- |
| `src/gpu.sv` | `gpu` | 顶层，把所有部件连成一个完整 GPU |
| `src/dcr.sv` | `dcr` | 设备控制寄存器，保存 `thread_count` |
| `src/dispatch.sv` | `dispatch` | 派发器，把线程切成 block 分给各 core |
| `src/controller.sv` | `controller` | 内存控制器，在多消费者和有限内存通道间仲裁 |
| `src/core.sv` | `core` | 计算核心，一次处理一个 block |
| `src/scheduler.sv` | `scheduler` | 调度器，驱动指令走完七阶段状态机 |
| `src/fetcher.sv` | `fetcher` | 取指单元，从程序内存取指令 |
| `src/decoder.sv` | `decoder` | 译码器，把 16 位指令切成控制信号 |
| `src/alu.sv` | `alu` | 算术逻辑单元，做加减乘除与比较 |
| `src/lsu.sv` | `lsu` | 访存单元，处理 LDR/STR 的异步内存请求 |
| `src/registers.sv` | `registers` | 寄存器堆，每个线程一套 |
| `src/pc.sv` | `pc` | 程序计数器，每个线程一套 |

#### 4.1.2 核心流程

这 12 个模块的「拼装关系」可以用一棵依赖树概括（箭头表示「被谁实例化」）：

```
gpu.sv (顶层)
├── dcr.sv
├── controller.sv  (data 内存控制器)
├── controller.sv  (program 内存控制器)
├── dispatch.sv
└── core.sv × NUM_CORES
    ├── fetcher.sv
    ├── decoder.sv
    ├── scheduler.sv
    ├── alu.sv        × THREADS_PER_BLOCK
    ├── lsu.sv        × THREADS_PER_BLOCK
    ├── registers.sv  × THREADS_PER_BLOCK
    └── pc.sv         × THREADS_PER_BLOCK
```

注意三个要点：

- `controller.sv` 这个模块被**实例化两次**：一次服务 data 内存，一次服务 program 内存（程序内存是只读的）。
- `core.sv` 被 `gpu.sv` 用 `generate` 循环复制 `NUM_CORES` 份。
- `core.sv` 内部又把 `alu/lsu/registers/pc` 用 `generate` 循环复制 `THREADS_PER_BLOCK` 份——这就是「每个线程一套硬件资源」的由来。

#### 4.1.3 源码精读

顶层 `gpu` 模块的开头是参数化定义，它把「几个核、每块多少线程、内存几位」都用 `parameter` 暴露出来：

[src/gpu.sv:10-18](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/gpu.sv#L10-L18) —— 这段定义了 `DATA_MEM_NUM_CHANNELS`、`NUM_CORES`、`THREADS_PER_BLOCK` 等可配置参数，是理解「GPU 能配多大」的入口。

顶层模块在端口之后，依次实例化了 4 类子模块。下面是 DCR（设备控制寄存器）的实例化，你可以看到「父模块把信号连到子模块端口」的写法：

[src/gpu.sv:76-83](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/gpu.sv#L76-L83) —— 这里 `dcr dcr_instance (...)` 把外部的 `device_control_data` 接进去，输出 `thread_count`。

在 `gpu.sv` 里搜索 `controller #(` 会发现它出现两次（[data 内存控制器 L86-L112](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/gpu.sv#L86-L112) 与 [program 内存控制器 L115-L134](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/gpu.sv#L115-L134)），印证了「同一个模块实例化两次」。

#### 4.1.4 代码实践

**实践目标**：亲手确认「12 个模块」的计数，而不是凭记忆。

**操作步骤**：

1. 列出 `src/` 下所有 `.sv` 文件（例如 `ls src/*.sv`）。
2. 数一下数量，应当是 12 个。
3. 在 `src/gpu.sv` 与 `src/core.sv` 里分别搜索形如 `xxx yyy_instance (` 的实例化语句。

**需要观察的现象**：`gpu.sv` 里实例化的子模块名（dcr、controller、dispatch、core）和 `core.sv` 里实例化的子模块名（fetcher、decoder、scheduler、alu、lsu、registers、pc），加起来正好覆盖 12 个文件。

**预期结果**：12 个 `.sv` 文件 = 1 个顶层 `gpu` + 11 个被实例化的子模块，没有任何「孤儿文件」。

**待本地验证**：如果你在仓库里数出的不是 12 个，请回到本讲对照表核查。

#### 4.1.5 小练习与答案

**练习 1**：`controller.sv` 在整个 GPU 里被实例化了几次？分别服务谁？

> **答案**：两次。一次作为 data 内存控制器（连接所有 LSU），一次作为 program 内存控制器（只读，连接所有 fetcher）。

**练习 2**：为什么 `alu.sv` 不在 `gpu.sv` 里直接实例化，而是出现在 `core.sv` 里？

> **答案**：因为 ALU 属于「每个线程一份」的资源，而线程是 core 内部的概念，所以 ALU 由 `core.sv` 在 per-thread 的 `generate` 循环里实例化，顶层 `gpu.sv` 看不到它。

### 4.2 架构图与模块对应关系

#### 4.2.1 概念说明

README 的 Architecture 一节放了两张图并排：`docs/images/gpu.png`（GPU 整体）和 `docs/images/core.png`（Core 内部）。本小节的任务是把这两张图、再加上 `docs/images/thread.png`（线程），与 `src/` 的文件一一对应。

这正是 u1-l1 提到的三层分解：**GPU → Core → Thread**。每一层对应一张图、一组源码文件：

- **GPU 层**：`gpu.png` 对应 `gpu.sv` 及其直接子模块（dcr/dispatch/controller）。
- **Core 层**：`core.png` 对应 `core.sv` 及其单实例子模块（fetcher/decoder/scheduler）。
- **Thread 层**：`thread.png` 对应 core 内部 per-thread 复制的模块（alu/lsu/registers/pc）。

#### 4.2.2 核心流程

理解对应关系的关键，是看懂两段 `generate` 循环如何「展开」架构图：

1. **第一层展开（GPU → Core）**：`gpu.sv` 用一个 `for` 循环生成 `NUM_CORES` 个 `core` 实例，每个 core 拿到自己的 `block_id`、`thread_count`，并各自连一条到程序内存控制器的取指通道。
2. **第二层展开（Core → Thread）**：`core.sv` 内部再用一个 `for` 循环生成 `THREADS_PER_BLOCK` 套 `alu/lsu/registers/pc`，并用 `enable = (i < thread_count)` 做门控——只让真正属于当前 block 的线程硬件工作。

#### 4.2.3 源码精读

第一层 `generate` 循环在 `gpu.sv` 的末尾，它复制出多个 core，并把每个 core 的 LSU 通道汇集成一个扁平数组交给 data 内存控制器：

[src/gpu.sv:154-216](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/gpu.sv#L154-L216) —— `for (i = 0; i < NUM_CORES; i = i + 1)` 这段循环同时做了两件事：把每个 core 的 `THREADS_PER_BLOCK` 个 LSU 信号汇入全局 `lsu_*` 数组，再实例化一个 `core`。

第二层 `generate` 循环在 `core.sv` 内部，它为每个线程实例化一整套执行资源，并用 `enable` 门控：

[src/core.sv:131-211](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/core.sv#L131-L211) —— 注意每个 per-thread 实例都带着 `.enable(i < thread_count)`，例如 [ALU 实例 L136-L146](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/core.sv#L136-L146) 里就有这一行。这就是「只有前 `thread_count` 个线程才真正工作」的硬件实现。

而 core 的「单实例」资源（fetcher/decoder/scheduler）在循环**之外**，全 core 共享：

[src/core.sv:74-129](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/core.sv#L74-L129) —— 这里连续实例化了 `fetcher`、`decoder`、`scheduler` 各一份，体现了「一块 core 只有一条取指/译码/调度流水，但有多套执行单元」的结构。

#### 4.2.4 代码实践

**实践目标**：在架构图上标注出对应的源码文件名。

**操作步骤**：

1. 打开 `docs/images/gpu.png` 和 `docs/images/core.png`（或在 README 的 Architecture 节查看）。
2. 对照本讲 4.1.1 的职责表，在图中每个方框旁写上对应的 `.sv` 文件名。
3. 特别留意：`core.png` 里**只出现一次**的方框（fetcher/decoder/scheduler）对应单实例；**出现多次**的方框（ALU/LSU/registers/PC）对应 per-thread 复制。

**需要观察的现象**：core 图里的 ALU/LSU 等会画成「一排」，正对应 `core.sv` 的 `generate` 循环。

**预期结果**：图上每个方框都能对应到唯一一个 `.sv` 文件，且「画成排」的方框对应 per-thread 模块。

#### 4.2.5 小练习与答案

**练习 1**：架构图里的「Scheduler」应该挂在三层的哪一层？为什么？

> **答案**：Core 层。每个 core 有**一个** scheduler（单实例），它不属于某个具体线程，而是统管整个 core 里所有线程的指令推进，所以在 `core.sv` 的 `generate` 循环之外实例化。

**练习 2**：`docs/images/thread.png` 画的是一个线程的内部结构，它应该包含哪些 `.sv` 模块？

> **答案**：`alu.sv`、`lsu.sv`、`registers.sv`、`pc.sv` 这四个 per-thread 模块，加上它们之间的数据通路（rs/rt → ALU/LSU → 写回寄存器）。

### 4.3 test/ 仿真框架结构

#### 4.3.1 概念说明

`src/` 是「被测对象」（DUT，Device Under Test），`test/` 则是「测试台」。tiny-gpu 用 **cocotb** 框架，让我们用 Python 写测试逻辑去驱动 Verilog 仿真。理解 `test/` 的组织，能帮你分清两类文件的边界：

- `.sv` 文件：用 iverilog 仿真器执行，描述硬件行为。
- `.py` 文件：用 cocotb 驱动，负责产生时钟、喂入程序和数据、检查结果。

`test/` 目录的文件清单：

| 文件 | 作用 |
| --- | --- |
| `test/__init__.py` | 让 `test` 成为可被 cocotb 导入的 Python 包 |
| `test/helpers/setup.py` | 启动序列：时钟、复位、装填内存、写 DCR、拉 start |
| `test/helpers/memory.py` | 用 Python 模拟外部异步 DRAM |
| `test/helpers/logger.py` | 把仿真过程写成可读日志 |
| `test/helpers/format.py` | 把 DUT 内部信号格式化成指令/状态/寄存器轨迹 |
| `test/test_matadd.py` | 矩阵加法内核测试 |
| `test/test_matmul.py` | 矩阵乘法内核测试 |
| `test/logs/.gitkeep` | 占位，仿真日志会输出到此目录 |

#### 4.3.2 核心流程

一个典型测试（如 `test_matadd.py`）的运行流程是：

1. 创建 `program_memory` 和 `data_memory` 两个 Python `Memory` 对象，分别装入指令和数据。
2. 调用 `helpers/setup.py` 的 `setup()`：产生时钟 → 复位 → 把程序/数据写入 DUT 内存接口 → 写 DCR（线程数）→ 拉高 `start`。
3. 主循环里每个时钟周期让 `program_memory.run()` 和 `data_memory.run()` 响应 DUT 的读写请求，直到 `done` 信号拉高。
4. 用 `format.py` 把每个周期的内部信号格式化成轨迹，用 `logger.py` 写到 `test/logs/`。

#### 4.3.3 源码精读

`test_matadd.py` 开头集中体现了「Python 测试台如何引用 helpers」：

[test/test_matadd.py:1-6](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/test/test_matadd.py#L1-L6) —— 这几行 `import` 把 `setup`、`Memory`、`format_cycle`、`logger` 都引进来，说明 helpers 目录是被多个测试共享的通用工具。

紧接着可以看到测试台如何用 Python 对象描述「程序内存」和「数据内存」，注意它们的位宽正好对应 `gpu.sv` 的参数（程序内存 16 位指令、数据内存 8 位数据）：

[test/test_matadd.py:10-33](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/test/test_matadd.py#L10-L33) —— `program` 列表是一串 16 位二进制指令，`data` 列表是矩阵 A 和矩阵 B 的初始值。

#### 4.3.4 代码实践

**实践目标**：确认「测试台与硬件源码是两套独立文件」，且 helpers 被复用。

**操作步骤**：

1. 在 `test/test_matadd.py` 与 `test/test_matmul.py` 里分别搜索 `from .helpers`。
2. 观察它们是否导入了同一批 helpers（setup/memory/format/logger）。

**需要观察的现象**：两个测试文件导入的 helpers 完全一致，只是 `program` 和 `data` 内容不同。

**预期结果**：你会确认 helpers 是「公共测试设施」，而每个 `test_*.py` 只负责提供一个具体内核。

#### 4.3.5 小练习与答案

**练习 1**：cocotb 仿真时，真正「跑」硬件的是哪个工具？Python 在其中扮演什么角色？

> **答案**：硬件由 `iverilog`/`vvp` 仿真器执行（见 Makefile 的 `iverilog -o build/sim.vvp`）；Python（cocotb）通过 VPI 接口驱动仿真器，扮演「测试台」角色——产生时钟、喂入数据、检查输出，它本身不是硬件。

**练习 2**：`test/helpers/` 下的四个文件，哪个最像「外部 DRAM 的替身」？

> **答案**：`memory.py`。它的 `Memory` 类用 Python 字典/数组模拟外部异步内存，按 DUT 给出的地址返回数据，承担了真实硬件里 DRAM 芯片的角色。

### 4.4 docs/ 文档与图片资源

#### 4.4.1 概念说明

`docs/` 目录目前只存放图片资源（`docs/images/`），配合 `README.md` 使用。对学习者而言，这些图是「读源码前的导航」——先看图建立直觉，再回源码印证。`docs/images/` 下的图片：

| 图片 | 出现在 README 的位置 | 对应的源码层次 |
| --- | --- | --- |
| `docs/images/gpu.png` | Architecture / GPU | GPU 顶层（`gpu.sv` 及子模块） |
| `docs/images/core.png` | Architecture（与 gpu.png 并排） | Core 内部（`core.sv`） |
| `docs/images/thread.png` | Execution / Thread | 单个线程（per-thread 模块） |
| `docs/images/isa.png` | ISA | 指令编码格式（`decoder.sv`） |
| `docs/images/trace.png` | Simulation | 仿真日志样例（`format.py` 输出） |

此外仓库根目录还有 `README.md`（主文档）、`Makefile`（构建入口）和 `gds/`（存放 `gpu.gds` 芯片版图文件，属于流片产物，本手册不深入）。

#### 4.4.2 核心流程

阅读 tiny-gpu 的推荐顺序就是「图先行」：

1. 先看 `gpu.png` 了解 GPU 有哪些大部件。
2. 再看 `core.png` 了解一个 core 内部结构。
3. 再看 `thread.png` 了解单线程的数据通路。
4. 遇到指令时对照 `isa.png`。
5. 跑完仿真后对照 `trace.png` 阅读日志。

每一张图都对应本讲建立过的某一层源码，所以读完本讲后，你应当能把图和文件无缝切换。

#### 4.4.3 源码精读

README 在 Architecture 开头并排嵌入了 `gpu.png` 和 `core.png`，这正是三层结构的「上层两张图」：

[README.md:59-62](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/README.md#L59-L62) —— 这段 Markdown 同时引用了 `gpu.png` 和 `core.png`，提示读者「GPU 由多个 Core 组成」。

线程层的图则在 Execution 一节单独出现：

[README.md:218](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/README.md#L218) —— `![Thread](/docs/images/thread.png)` 展示了单个线程的内部结构，对应 `core.sv` 里 per-thread 复制的 ALU/LSU/registers/PC。

#### 4.4.4 代码实践

**实践目标**：建立「图片 ↔ 源码文件」的双向映射。

**操作步骤**：

1. 在仓库里确认 `docs/images/` 下确实存在 5 张 png（`ls docs/images/`）。
2. 在 `README.md` 里搜索每张图片的引用位置（搜索 `docs/images/`）。
3. 给每张图片写一句「它对应哪个 `.sv` 文件或哪层结构」。

**需要观察的现象**：5 张图都能在 README 里找到引用点，且引用点的上下文（小节标题）正好对应它的层次。

**预期结果**：得到一张与 4.4.1 表格一致的映射表。

#### 4.4.5 小练习与答案

**练习 1**：`gds/` 目录下的 `.gds` 文件是什么？为什么本手册不深入它？

> **答案**：`.gds` 是芯片版图（layout）文件，是硬件综合/流片后的物理产物，描述几何图形而非逻辑行为。本手册聚焦「逻辑结构与仿真」，所以不深入版图。

**练习 2**：如果你只想理解「一条指令怎么被译码」，应该先看哪张图、再读哪个源码文件？

> **答案**：先看 `docs/images/isa.png` 了解 16 位指令的位域，再读 `src/decoder.sv` 看这些位域如何被切成控制信号（指令编码与译码的细节在 [u5-l1](u5-l1-isa-encoding.md) 详讲）。

## 5. 综合实践

**任务**：画出一张完整的「模块依赖树」，把 `src/` 下全部 12 个 `.sv` 文件挂到 `gpu.sv` 或 `core.sv` 之下，并为每个模块标注一句话职责。

**要求**：

1. 根节点是 `gpu.sv`。
2. 用缩进或树形符号表示实例化关系。
3. 标注哪些模块是「单实例」（如 dcr、scheduler），哪些是「多实例」（如 core × NUM_CORES、alu × THREADS_PER_BLOCK）。
4. 标注 `controller.sv` 被实例化两次这一特殊情况。

**参考答案（先自己画，再对照）**：

```
gpu.sv  [顶层，单实例]
├── dcr.sv                      [单实例] 设备控制寄存器，存 thread_count
├── controller.sv               [实例 1] data 内存控制器（连所有 LSU）
├── controller.sv               [实例 2] program 内存控制器（只读，连 fetcher）
├── dispatch.sv                 [单实例] 派发器，把线程切成 block 分给 core
└── core.sv                     [× NUM_CORES] 计算核心
    ├── fetcher.sv              [单实例/core] 取指
    ├── decoder.sv             [单实例/core] 译码
    ├── scheduler.sv           [单实例/core] 调度状态机
    ├── alu.sv                 [× THREADS_PER_BLOCK] 算术逻辑单元
    ├── lsu.sv                 [× THREADS_PER_BLOCK] 访存单元
    ├── registers.sv           [× THREADS_PER_BLOCK] 寄存器堆
    └── pc.sv                  [× THREADS_PER_BLOCK] 程序计数器
```

**自检**：树上一共出现 12 个不同的文件名；`controller.sv` 出现两次但只算 1 个文件；`core.sv` 下有 7 个子模块，其中 3 个单实例、4 个 per-thread 多实例。若你的树满足这些，说明你已经掌握了 tiny-gpu 的源码地图。

## 6. 本讲小结

- 仓库分为 `src/`（12 个 `.sv` 硬件源码）、`test/`（cocotb Python 仿真测试）、`docs/images/`（架构图）、`gds/`（版图产物）四部分。
- 源码围绕 **GPU → Core → Thread** 三层结构组织：`gpu.png`/`core.png`/`thread.png` 三张图分别对应这三层。
- `gpu.sv` 用 `generate` 循环复制 `NUM_CORES` 个 core；`core.sv` 再用 `generate` 循环为每个线程复制 `THREADS_PER_BLOCK` 套 ALU/LSU/registers/PC。
- `controller.sv` 是唯一被实例化两次的模块（data + program 内存控制器）。
- `.sv` 文件由 iverilog 仿真器执行，`.py` 文件由 cocotb 驱动——两者是「被测对象」与「测试台」的关系，不要混淆。
- 读源码的正确顺序是「先看图建立直觉，再回源码印证」。

## 7. 下一步学习建议

本讲只建立了「地图」，还没看任何具体的电路行为。接下来：

- 想知道「这些 `.sv` 文件怎么被编译、怎么跑起来」，请学 [u1-l3 构建与仿真工具链](u1-l3-build-and-simulation.md)，亲手跑通 `make test_matadd`。
- 想从「最顶层」开始理解数据如何在模块间流动，请学 [u2-l1 gpu.sv 顶层架构](u2-l1-gpu-top-level.md)。
- 建议的阅读顺序是：先 u1-l3（跑起来）→ 再 u2（顶层）→ 再按单元逐层下探。地图已经在你手里，剩下的就是一层一层往里走。
