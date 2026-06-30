# 在 Verilator 仿真器上运行程序

> 本讲承接 [u2-l2](u2-l2-write-compile-program.md)：你已经能用 `coralnpu_v2_binary` 编译出一个落点正确的 `.elf`。这一讲解决下一个问题——**没有真实芯片，怎么把这个 `.elf` 跑起来、并看见它做了什么？**

## 1. 本讲目标

学完本讲，你应该能够：

- 说清楚 CoralNPU 的 **Verilator 仿真器**由哪几层组成、各自负责什么。
- 用 `core_mini_axi_sim` 这个命令行工具，把一个 `.elf` **加载并运行**，并解释从加载到 halt 的完整时序。
- 理解 `CoralNPUSimulator` 这个 C++ 库式封装，以及它和命令行工具共享同一个 Verilator 模型的关系。
- 找到仿真器用来**观测内核状态**（halt / fault / wfi、CSR STATUS、指令轨迹、mailbox）的接口，并说清它们的用法。

## 2. 前置知识

在进入源码前，先用大白话对齐几个概念。

- **RTL 与仿真**：CoralNPU 的硬件用 Chisel / SystemVerilog 写成（合称 RTL）。RTL 本身不能直接「运行程序」。**Verilator** 是一个把 RTL 翻译成 C++ 模型的工具，编译后得到一个可执行的「软件芯片」，让你在普通电脑上跑 RISC-V 程序、观察波形。
- **被测模型（DUT）**：Verilator 从 Chisel 顶层 `CoreMiniAxi` 生成的 C++ 类叫 `VCoreMiniAxi`。它有一堆 `io_*` 端口（时钟、复位、AXI slave/master、`io_halted` 等），仿真器要做的就是「喂端口、读端口」。
- **AXI slave / master**（[u3-l2](u3-l2-axi-integration.md) 会详讲）：从 CoralNPU 的角度看，**slave 端口**是外部主机（比如仿真器）配置它用的；**master 端口**是 CoralNPU 自己主动去访问外部内存用的。仿真器两边的端口都要扮演。
- **加载 ≠ 运行**：把 `.elf` 的代码/数据写进 ITCM/DTCM 只叫「加载」；要让内核真正跑起来，还得按 [u2-l1](u2-l1-toolchain-linker-tcm.md) 提到的启动协议，写 PC、放复位。本讲的核心就是这条「加载→启动→等待 halt」的链路。
- **mpause / halt**：[u2-l1](u2-l1-toolchain-linker-tcm.md) 讲过，CRT 在 `main` 成功返回后会执行 `mpause` 指令，让内核进入 halt（`io_halted` 置位）。仿真器正是靠这个信号判断「程序跑完了」。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [`tests/verilator_sim/BUILD`](https://github.com/google-coral/coralnpu/blob/77bc1ffe06dbf3b7bafc7eab167ead2b42668df9/tests/verilator_sim/BUILD) | 用 `template_rule` 批量定义 8 个仿真器变体（含 `core_mini_axi_sim`） |
| [`tests/verilator_sim/coralnpu/core_mini_axi_sim.cc`](https://github.com/google-coral/coralnpu/blob/77bc1ffe06dbf3b7bafc7eab167ead2b42668df9/tests/verilator_sim/coralnpu/core_mini_axi_sim.cc) | 命令行仿真器入口（SystemC `sc_main`），解析 `--binary` 等参数 |
| [`tests/verilator_sim/coralnpu/core_mini_axi_tb.cc`](https://github.com/google-coral/coralnpu/blob/77bc1ffe06dbf3b7bafc7eab167ead2b42668df9/tests/verilator_sim/coralnpu/core_mini_axi_tb.cc) / [`.h`](https://github.com/google-coral/coralnpu/blob/77bc1ffe06dbf3b7bafc7eab167ead2b42668df9/tests/verilator_sim/coralnpu/core_mini_axi_tb.h) | SystemC 测试台：持有 `VCoreMiniAxi`，负责加载 ELF、驱动启动序列、检测 halt |
| [`hw_sim/coralnpu_simulator.h`](https://github.com/google-coral/coralnpu/blob/77bc1ffe06dbf3b7bafc7eab167ead2b42668df9/hw_sim/coralnpu_simulator.h) | `CoralNPUSimulator` 纯虚抽象基类（库式接口） |
| [`hw_sim/core_mini_axi_simulator.cc`](https://github.com/google-coral/coralnpu/blob/77bc1ffe06dbf3b7bafc7eab167ead2b42668df9/hw_sim/core_mini_axi_simulator.cc) | `CoreMiniAxiSimulator`：用 `CoreMiniAxiWrapper` 实现上述抽象，含 mailbox 回调 |
| [`hw_sim/core_mini_axi_wrapper.h`](https://github.com/google-coral/coralnpu/blob/77bc1ffe06dbf3b7bafc7eab167ead2b42668df9/hw_sim/core_mini_axi_wrapper.h) | 直接驱动 `VCoreMiniAxi` 的封装：时钟 + 4 个 AXI 驱动 + halted/wfi 观测 |
| [`hw_sim/hw_primitives.h`](https://github.com/google-coral/coralnpu/blob/77bc1ffe06dbf3b7bafc7eab167ead2b42668df9/hw_sim/hw_primitives.h) / [`hw_primitives.cc`](https://github.com/google-coral/coralnpu/blob/77bc1ffe06dbf3b7bafc7eab167ead2b42668df9/hw_sim/hw_primitives.cc) | 仿真原语：`Clock`、`AxiSlaveReadDriver`、`AxiMasterWriteDriver` 等 |
| [`hw_sim/mailbox.h`](https://github.com/google-coral/coralnpu/blob/77bc1ffe06dbf3b7bafc7eab167ead2b42668df9/hw_sim/mailbox.h) | `CoralNPUMailbox`：仿真器用来观测内核 master 写出的 16 字节「信箱」 |

---

## 4. 核心概念与源码讲解

### 4.1 仿真器的两条入口与分层架构

#### 4.1.1 概念说明

CoralNPU 的 Verilator 仿真有**两条入口**，最终都驱动同一个 `VCoreMiniAxi` 模型，但包装方式不同：

1. **命令行工具 `core_mini_axi_sim`**（SystemC 体系）：一个可执行文件，接受 `--binary xxx.elf`，内部用 SystemC + TLM（事务级建模）搭一套完整的测试台，带 tohost/semihosting 半主机支持。这是 **README Quick Start** 用的路径，也是回归测试的主力。
2. **C++ 库 `CoralNPUSimulator`**（`hw_sim/` 体系）：一个更轻量的封装，不依赖 SystemC，直接用 C++ 驱动 AXI 信号，对外暴露 `Run / WaitForTermination / ReadMailbox` 等方法。它被编译成 `.so`，供 **npusim / Python**（[u10-l3](u10-l3-npusim-mobilenet.md)）在端到端 ML 推理时调用。

理解这两条线的分工，能让你在面对不同任务时选对工具：**手跑一个程序看结果**用前者，**把仿真器嵌进更大的软件流程**用后者。

#### 4.1.2 核心流程

两条入口的抽象生命周期是一样的：

```text
[宿主机]                          [VCoreMiniAxi 模型]
   │
   │  1. 加载 ELF：把 .text/.data 经 AXI slave 写入 ITCM/DTCM
   │ ──────────────────────────────────────────────────▶
   │  2. 写 PC_START（入口地址）到 CSR
   │ ──────────────────────────────────────────────────▶
   │  3. 解除时钟门控、释放复位（写 RESET_CONTROL）
   │ ──────────────────────────────────────────────────▶  ◀── 内核开始取指执行
   │
   │  4. 轮询 / 等待 io_halted（或 io_fault / io_wfi / tohost）
   │ ◀──────────────────────────────────────────────────
   │  5. 读 STATUS 确认，结束仿真
```

区别只在「谁来扮演外部主机、用什么机制搬运数据」。

#### 4.1.3 源码精读：8 个变体从同一个模板长出来

CoralNPU 有多种内核配置（普通 / verification / highmem / 大 TCM / 带 RVV），每种都要一个仿真器。为了避免重复，`tests/verilator_sim/BUILD` 用了一个 `template_rule` 宏，把 8 个 `cc_binary` 从一份公共配置批量生成：

```python
# tests/verilator_sim/BUILD:219-266
template_rule(
    cc_binary,
    {
        "core_mini_axi_sim":           { "deps": [":core_mini_axi_tb", ] + COMMON_DEPS },
        "core_mini_highmem_axi_sim":   { "deps": [":core_mini_highmem_axi_tb", ] + COMMON_DEPS },
        "rvv_core_mini_axi_sim":       { "deps": [":rvv_core_mini_axi_tb", ] + COMMON_DEPS },
        # ... 共 8 个变体
    },
    srcs = ["coralnpu/core_mini_axi_sim.cc"],   # ← 8 个仿真器共用同一个 main 源文件
)
```

关键点：**8 个仿真器二进制共用同一份 `core_mini_axi_sim.cc` 源码**，差别只在 `deps` 里链接了哪个 testbench 库（`core_mini_axi_tb` / `rvv_core_mini_axi_tb` …），而每个 testbench 库又通过 `VERILATOR_MODEL` 宏（如 `VCoreMiniAxi` / `VRvvCoreMiniAxi`）绑定不同的 Verilator 模型。这就是为什么 README 特别注明「Build the Simulator (non-RVV for shorter build time)」——不带 RVV 的 `core_mini_axi_sim` 编译更快，适合日常跑标量程序。

> 见 [`tests/verilator_sim/BUILD:219-266`](https://github.com/google-coral/coralnpu/blob/77bc1ffe06dbf3b7bafc7eab167ead2b42668df9/tests/verilator_sim/BUILD#L219-L266)（仿真器二进制模板）与 [`tests/verilator_sim/BUILD:110-209`](https://github.com/google-coral/coralnpu/blob/77bc1ffe06dbf3b7bafc7eab167ead2b42668df9/tests/verilator_sim/BUILD#L110-L209)（对应的 testbench 库模板，注意每个变体的 `VERILATOR_MODEL` define）。

#### 4.1.4 代码实践

1. **目标**：确认「同一份 main、不同模型」的生成机制。
2. **步骤**：
   - 打开 [`tests/verilator_sim/BUILD`](https://github.com/google-coral/coralnpu/blob/77bc1ffe06dbf3b7bafc7eab167ead2b42668df9/tests/verilator_sim/BUILD)，找到第二个 `template_rule`（L219 起），数一下它定义了几个 `*_sim` 目标。
   - 对照第一个 `template_rule`（L110 起），看 `core_mini_axi_tb` 和 `rvv_core_mini_axi_tb` 分别把 `VERILATOR_MODEL` 定义成什么。
3. **观察**：8 个 sim 目标；`VCoreMiniAxi` vs `VRvvCoreMiniAxi`。
4. **预期**：你会清楚「带 RVV 的仿真器只是换了一个更大的 Verilator 模型，源码完全一样」。

#### 4.1.5 小练习与答案

- **练习**：为什么 README 推荐先用 `core_mini_axi_sim`（non-RVV）而不是 `rvv_core_mini_axi_sim`？
- **答案**：因为 RVV 向量/矩阵后端是大量 SystemVerilog（[u7](u7-l1-rvv-backend-overview.md)），把它编进 Verilator 模型会显著拖慢编译；只想跑标量程序时，non-RVV 的 `core_mini_axi_sim` 编译更快、足够用。

---

### 4.2 `core_mini_axi_sim`：命令行仿真器与 `--binary` 加载机制

#### 4.2.1 概念说明

这是日常最常用的入口。它是一个 SystemC 程序，`sc_main` 是它的 `main`。你通过命令行参数告诉它「跑哪个 `.elf`、跑多少周期、要不要波形/指令轨迹」。它内部构造一个 `CoreMiniAxi_tb` 测试台，由测试台负责真正加载 ELF、启动内核、判断结束。

#### 4.2.2 核心流程：`run()` 的生命周期

`core_mini_axi_sim.cc` 的 `run()` 函数是一条非常清晰的时序链：

```text
构造 CoreMiniAxi_tb（并注册 halted 回调）
   → sc_start(SC_ZERO_TIME)        # 让 Verilog initial 块先跑
   → LoadElfSync(binary)           # ① 加载 ELF（含写 PC_START）
   → ClockGateSync(false)          # ② 解除时钟门控
   → ResetAsync(false)             # ③ 释放复位 → 内核开始跑
   → 等待 halted_cv 条件变量        # ④ 等 io_halted/io_fault/tohost
   → CheckStatusSync()             # ⑤ 读 STATUS 确认 halted
   → sc_stop()                     # 收尾
```

`LoadElfSync` 的加载机制很值得看：它用 `mmap` 把 `.elf` 映射进内存，校验 ELF 魔数 `0x464c457f`，然后遍历每个 program header，**对每段数据生成「写 → 读 → 比对」三步 AXI 事务**——也就是说它不仅把数据写进 TCM，还会回读校验，确保写入正确。最后它把 ELF 头里的入口地址 `e_entry` 写进 CSR 的 `PC_START` 寄存器。

#### 4.2.3 源码精读

先看命令行参数定义与入口：

```cpp
// tests/verilator_sim/coralnpu/core_mini_axi_sim.cc:36-41
ABSL_FLAG(int, cycles, 100000000, "Simulation cycles");
ABSL_FLAG(bool, trace, false, "Dump VCD trace");
ABSL_FLAG(std::string, binary, "", "Binary to execute");
ABSL_FLAG(bool, debug_axi, false, "Enable AXI traffic debugging");
ABSL_FLAG(bool, instr_trace, false, "Log instructions to console");
ABSL_FLAG(bool, backdoor_load, false, "Enable high-speed backdoor code loading");
```

`--binary` 是必需参数，缺失直接报错退出（[`core_mini_axi_sim.cc:94-97`](https://github.com/google-coral/coralnpu/blob/77bc1ffe06dbf3b7bafc7eab167ead2b42668df9/tests/verilator_sim/coralnpu/core_mini_axi_sim.cc#L94-L97)）。`--instr_trace` 是观测利器，后面会用到。

`run()` 把整条启动链串起来（注意第 ④ 步用条件变量等 halt 回调）：

```cpp
// tests/verilator_sim/coralnpu/core_mini_axi_sim.cc:64-81
std::thread sc_main_thread([&tb]() { tb.start(); });

CHECK_OK(tb.LoadElfSync(binary));     // ① 加载 ELF
CHECK_OK(tb.ClockGateSync(false));    // ② 解除时钟门控
CHECK_OK(tb.ResetAsync(false));       // ③ 释放复位

{
  absl::MutexLock lock_(&halted_mtx);
  halted_cv.Wait(&halted_mtx);        // ④ 等 halted 回调 SignalAll
}

if (!tb.io_fault && !tb.tohost_halt) {
  CHECK_OK(tb.CheckStatusSync());     // ⑤ 读 STATUS 确认
}
sc_stop();
return (!tb.io_fault && !(tb.tohost_halt && tb.tohost_val != 1));
```

加载时写入口地址的关键两行在 `LoadElfAsync` 里，`csr_addr_ + 0x4` 就是 `PC_START`：

```cpp
// tests/verilator_sim/coralnpu/core_mini_axi_tb.cc:358-360
elf_transfers.push_back(utils::Write(
  csr_addr_ + 0x4, reinterpret_cast<uint8_t*>(&entry_point), sizeof(entry_point)));
```

而 `csr_addr_` 默认是 `0x30000`（highmem 变体才是 `0x200000`）：

```cpp
// tests/verilator_sim/coralnpu/core_mini_axi_tb.h:265-269
#ifdef HIGHMEM_RTL
  static constexpr uint32_t csr_addr_ = 0x200000;
#else
  static constexpr uint32_t csr_addr_ = 0x30000;
#endif
```

> CSR 三个寄存器（`0x30000` RESET_CONTROL / `0x30004` PC_START / `0x30008` STATUS）的完整位域语义在 [u3-l5](u3-l5-csr-boot-control.md) 详讲；本讲只需知道「写 `+0x4` 设 PC、写 `+0x0` 控制时钟/复位、读 `+0x8` 看状态」。

`--backdoor_load` 是一个加速开关：打开后，落点在 TCM 内的段不再走 AXI（慢），而是走 [`sram_backdoor`](https://github.com/google-coral/coralnpu/blob/77bc1ffe06dbf3b7bafc7eab167ead2b42668df9/hdl/verilog/sram_backdoor.h) 直接注入 SRAM（快），见 [`core_mini_axi_tb.cc:344-356`](https://github.com/google-coral/coralnpu/blob/77bc1ffe06dbf3b7bafc7eab167ead2b42668df9/tests/verilator_sim/coralnpu/core_mini_axi_tb.cc#L344-L356) 与 [`core_mini_axi_tb.cc:936-938`](https://github.com/google-coral/coralnpu/blob/77bc1ffe06dbf3b7bafc7eab167ead2b42668df9/tests/verilator_sim/coralnpu/core_mini_axi_tb.cc#L936-L938)。

#### 4.2.4 代码实践（本讲主实践的一部分）

1. **目标**：构建 `core_mini_axi_sim` 并理解 `--binary` 必需、`csr_addr_` 取值。
2. **步骤**：
   - 按 README 构建：`bazel build //tests/verilator_sim:core_mini_axi_sim`。
   - 故意不带 `--binary` 运行一次，观察报错。
   - 在 [`core_mini_axi_tb.cc:316-390`](https://github.com/google-coral/coralnpu/blob/77bc1ffe06dbf3b7bafc7eab167ead2b42668df9/tests/verilator_sim/coralnpu/core_mini_axi_tb.cc#L316-L390) 里找到「写 → 读 → Expect」三步事务的生成处，确认加载会回读校验。
3. **观察**：缺 `--binary` 时打印 `--binary is required!` 并返回 -1。
4. **预期**：`--binary` 是唯一必填项；ELF 段被「写后校验」地灌进 TCM，入口地址被写到 `0x30004`。

#### 4.2.5 小练习与答案

- **练习 1**：`run()` 里为什么先 `ClockGateSync(false)` 再 `ResetAsync(false)`，顺序能反过来吗？
- **答案**：测试台注释明确给出顺序契约（[`core_mini_axi_tb.h:203-206`](https://github.com/google-coral/coralnpu/blob/77bc1ffe06dbf3b7bafc7eab167ead2b42668df9/tests/verilator_sim/coralnpu/core_mini_axi_tb.h#L203-L206)）：应先解除时钟门控再释放复位（或先保持复位再门控时钟）。反过来可能导致内核在时钟未稳定时被释放复位，行为不确定。
- **练习 2**：`csr_addr_` 在 `core_mini_highmem_axi_sim` 里是哪个值？
- **答案**：`0x200000`，因为该变体定义了 `HIGHMEM_RTL`。

---

### 4.3 `CoralNPUSimulator`：库式封装与 wrapper/驱动分层

#### 4.3.1 概念说明

第二条入口是 `hw_sim/` 下的 `CoralNPUSimulator`。它是一个**抽象基类**（纯虚接口），定义了「加载、运行、等待、读写 TCM/信箱」这一组高层操作；`CoreMiniAxiSimulator` 是它的具体实现。这层抽象的意义在于：上层（比如 npusim）不需要知道任何 Verilator / AXI 细节，只调 `Run(pc)`、`WaitForTermination()`、`ReadMailbox()` 即可。

它和命令行工具的本质区别是**没有 SystemC**：它直接实例化 `VCoreMiniAxi`，用 `hw_primitives.h` 里的 `Clock` 和 AXI 驱动逐拍（`Step()`）推进仿真。

#### 4.3.2 核心流程：三层调用关系

```text
CoralNPUSimulator (抽象接口, coralnpu_simulator.h)
        │  Create() 返回 CoreMiniAxiSimulator
        ▼
CoreMiniAxiSimulator (core_mini_axi_simulator.cc)
        │  持有 wrapper_，转发 Run/WaitForTermination/Mailbox
        ▼
CoreMiniAxiWrapper (core_mini_axi_wrapper.h)
        │  持有 VCoreMiniAxi + Clock + 4 个 AXI 驱动
        ▼
hw_primitives (Clock / AxiSlaveReadDriver / AxiMasterWriteDriver ...)
        │  每拍 OnFallingEdge/OnRisingEdge 搬运 AXI 信号
        ▼
VCoreMiniAxi (Verilator 生成的 DUT)
```

启动一次程序的流程：`Run(start_addr)` 往 `0x30004` 写入口 PC，再往 `0x30000` 依次写 `1`、`0`（解除门控 + 释放复位），内核开始跑；随后 `WaitForTermination()` 每拍检查 `io_halted || io_wfi`，命中即返回。

#### 4.3.3 源码精读

抽象接口极其简洁，只有 6 个纯虚方法（[`coralnpu_simulator.h:20-38`](https://github.com/google-coral/coralnpu/blob/77bc1ffe06dbf3b7bafc7eab167ead2b42668df9/hw_sim/coralnpu_simulator.h#L20-L38)）：

```cpp
// hw_sim/coralnpu_simulator.h:26-37
virtual void ReadTCM(uint32_t addr, size_t size, char* data) = 0;
virtual const CoralNPUMailbox& ReadMailbox(void) = 0;
virtual void WriteTCM(uint32_t addr, size_t size, const char* data) = 0;
virtual void WriteMailbox(const CoralNPUMailbox& mailbox) = 0;
virtual bool WaitForTermination(int timeout) = 0;   // 等待 halt/wfi
virtual void Run(uint32_t start_addr) = 0;          // 从某 PC 启动
```

实现类的 `Run` 直接对应第 4.2 节那套启动序列，只不过这里用 `WriteWord` 走 AXI slave：

```cpp
// hw_sim/core_mini_axi_simulator.cc:70-74
void CoreMiniAxiSimulator::Run(uint32_t start_addr) {
  wrapper_.WriteWord(0x30004, start_addr);   // PC_START
  wrapper_.WriteWord(0x30000, 1u);           // 解除时钟门控
  wrapper_.WriteWord(0x30000, 0u);           // 释放复位 → 开跑
}
```

`WaitForTermination` 把等待逻辑委托给 wrapper，逐拍检查 halt/wfi 信号：

```cpp
// hw_sim/core_mini_axi_wrapper.h:208-216
bool WaitForTermination(int timeout = 10000) {
  for (int i = 0; i < timeout; i++) {
    if ((*halted_) || (*wfi_)) { return true; }
    Step();                                  // 推进一拍
  }
  return false;                              // 超时未停
}
```

这里的 `halted_` / `wfi_` 是构造时绑定到模型端口的指针（[`core_mini_axi_wrapper.h:115-116`](https://github.com/google-coral/coralnpu/blob/77bc1ffe06dbf3b7bafc7eab167ead2b42668df9/hw_sim/core_mini_axi_wrapper.h#L115-L116)），读 `*halted_` 就是读 `core_.io_halted`。

推进一拍的 `Clock::Step` 在 `hw_primitives.cc`，它把一个时钟周期拆成「拉高 → 通知上升沿观察者 → 拉低 → 通知下降沿观察者」，AXI 驱动就是在这些边沿回调里搬运信号的：

```cpp
// hw_sim/hw_primitives.cc:26-42
void Clock::Step() {
  context_->timeInc(1); (*clock_) = 1; Eval();
  for (auto& observer : observers_) { observer->OnRisingEdge();  Eval(); }
  context_->timeInc(1); (*clock_) = 0; Eval();
  for (auto& observer : observers_) { observer->OnFallingEdge(); Eval(); }
}
```

工厂方法 `Create()` 屏蔽了具体类型，上层只拿抽象指针：

```cpp
// hw_sim/core_mini_axi_simulator.cc:116-119
CoralNPUSimulator* CoralNPUSimulator::Create() {
  return new CoreMiniAxiSimulator();
}
```

> 构造函数（[`core_mini_axi_simulator.cc:22-34`](https://github.com/google-coral/coralnpu/blob/77bc1ffe06dbf3b7bafc7eab167ead2b42668df9/hw_sim/core_mini_axi_simulator.cc#L22-L34)）在 wrapper 上注册了两个回调（`ReadCallback`/`WriteCallback`），这是下一节 mailbox 观测机制的关键。

#### 4.3.4 代码实践（源码阅读型）

1. **目标**：用一个真实示例验证「库式 API 怎么用」。
2. **步骤**：阅读 [`hw_sim/core_mini_axi_simulator_example.cc`](https://github.com/google-coral/coralnpu/blob/77bc1ffe06dbf3b7bafc7eab167ead2b42668df9/hw_sim/core_mini_axi_simulator_example.cc)。它的 `main` 只有四步：`Create()` → `WriteTCM` 逐段加载 ELF → `Run(start_pc)` → `WaitForTermination` → `ReadMailbox`。
3. **观察**：它和命令行工具走的是同一套启动序列，但完全没有 SystemC、没有 `--binary` 参数，全靠 C++ 方法调用。
4. **预期**：你能复述「库式入口把加载/启动/等待拆成三个独立方法，由调用方自行编排」。

#### 4.3.5 小练习与答案

- **练习**：`CoralNPUSimulator::Create()` 返回的是抽象指针还是具体类型？这样做有什么好处？
- **答案**：返回 `CoralNPUSimulator*`（抽象指针），实际对象是 `CoreMiniAxiSimulator`。好处是上层只依赖接口，未来换一种内核实现（比如 RVV 版 `core_mini_axi_simulator_rvv`，见 [`hw_sim/BUILD:78-88`](https://github.com/google-coral/coralnpu/blob/77bc1ffe06dbf3b7bafc7eab167ead2b42668df9/hw_sim/BUILD#L78-L88)）时上层代码不用改。

---

### 4.4 观测内核状态：halt / fault / wfi、STATUS、指令轨迹与 mailbox

#### 4.4.1 概念说明

「跑通了」不等于「看懂了」。CoralNPU 仿真器提供了**四种**观测内核状态的途径，从粗到细：

1. **halt/fault/wfi 信号**：最粗粒度，回答「程序结束了吗？是正常结束还是出错了？」
2. **CSR STATUS 寄存器**（`0x30008`）：硬件层面的状态位，命令行工具用它做最终确认。
3. **指令轨迹**（`--instr_trace` / debug IO 端口）：逐条打印退休指令的 PC、机器码、寄存器写。
4. **mailbox 信箱**：观测内核通过 master 端口「主动写出去」的数据——这是 `CoralNPUSimulator` 路径下看程序输出的主要方式。

#### 4.4.2 核心流程

**halt 检测**（命令行路径）：测试台在每个上升沿 `posedge()` 里检查 `io_halted || io_fault || tohost_halt`，一旦命中（且只命中一次），就调用 `halted_cb`，后者 `SignalAll` 唤醒 `run()` 里等待的条件变量。`io_halted` 正是由 CRT 的 `mpause` 指令（[u2-l1](u2-l1-toolchain-linker-tcm.md)）置位。

**mailbox 机制**（库式路径）：内核经 master 端口往「外部内存」写数据时，仿真器没有真内存，于是用 `WriteCallback` 把写进来的字节（按 strobe 掩码）存进一个 16 字节的 `CoralNPUMailbox`；内核读「外部内存」时，`ReadCallback` 把这个信箱的内容还回去。主机调 `ReadMailbox()` 即可读到内核写出的结果。

#### 4.4.3 源码精读

`posedge()` 里的 halt 检测（注意 `invoked_halted_cb` 保证只触发一次）：

```cpp
// tests/verilator_sim/coralnpu/core_mini_axi_tb.cc:869-880
static bool invoked_halted_cb = false;
if ((io_halted || io_fault || tohost_halt) && !invoked_halted_cb) {
  if (instr_trace_) { tracer_.PrintTrace(); }
  invoked_halted_cb = true;
  if (halted_cb_) { halted_cb_.value()(); }   // → 唤醒 run() 的条件变量
}
```

`CheckStatusAsync` 读 STATUS（`csr_addr_ + 0x8 = 0x30008`），期望低位字节为 1（halted 位）：

```cpp
// tests/verilator_sim/coralnpu/core_mini_axi_tb.cc:437-443
absl::Status CoreMiniAxi_tb::CheckStatusAsync() {
  absl::MutexLock lock(&transfer_queue_mtx_);
  transfer_queue_.push(std::make_unique<TrafficDesc>(utils::merge(
      std::vector<DataTransfer>({utils::Read(csr_addr_ + 0x8, 4),
                                 utils::Expect(DATA(1, 0, 0, 0), 4)}))));
  return absl::OkStatus();
}
```

mailbox 信箱结构只是一个 4×32 位的数组（[`mailbox.h:18-20`](https://github.com/google-coral/coralnpu/blob/77bc1ffe06dbf3b7bafc7eab167ead2b42668df9/hw_sim/mailbox.h#L18-L20)）。当内核 master 写时，`WriteCallback` 按 strobe 把字节拷进信箱：

```cpp
// hw_sim/core_mini_axi_simulator.cc:80-96
AxiWResp CoreMiniAxiSimulator::WriteCallback(const AxiAddr& addr, const AxiWData& data) {
  CoralNPUMailbox& mailbox = wrapper_.mailbox();
  uint8_t* mailbox_data = reinterpret_cast<uint8_t*>(mailbox.message);
  const uint8_t* write_data = reinterpret_cast<const uint8_t*>(&data.write_data_bits_data[0]);
  for (int i = 0; i < 16; i++) {
    if (data.write_data_bits_strb & (1 << i)) { mailbox_data[i] = write_data[i]; }
  }
  /* ... 返回 OK 响应 ... */
}
```

一个最小可运行的例子见 [`mailbox_example.cc`](https://github.com/google-coral/coralnpu/blob/77bc1ffe06dbf3b7bafc7eab167ead2b42668df9/hw_sim/mailbox_example.cc)：内核把 `0xDEADBEEF` 写到外部地址 `0x20000000`（落入信箱 `message[0]`），再读回、写到 `message[1]`，最后 `wfi`。宿主侧 `core_mini_axi_simulator_example.cc` 这样观测：

```cpp
// hw_sim/core_mini_axi_simulator_example.cc:57-68
simulator->Run(start_pc);
if (simulator->WaitForTermination(10000)) {
  std::cout << "Halted" << std::endl;        // wfi/halt 命中
}
CoralNPUMailbox m = simulator->ReadMailbox();
std::cout << "Mailbox value[0]=0x" << std::hex << m.message[0] << std::endl;  // 0xDEADBEEF
std::cout << "Mailbox value[1]=0x" << std::hex << m.message[1] << std::endl;  // 0xDEADBEEF
```

> 指令轨迹（最细粒度）通过内核暴露的 `io_debug_*` 端口实现，`--instr_trace` 打开后由 [`core_mini_axi_tb.cc:445-468`](https://github.com/google-coral/coralnpu/blob/77bc1ffe06dbf3b7bafc7eab167ead2b42668df9/tests/verilator_sim/coralnpu/core_mini_axi_tb.cc#L445-L468) 的 `TraceInstructions()` 逐退休槽打印 PC/指令/idx。这套 debug 端口的字段定义见 [`core_mini_axi_tb.h:65-158`](https://github.com/google-coral/coralnpu/blob/77bc1ffe06dbf3b7bafc7eab167ead2b42668df9/tests/verilator_sim/coralnpu/core_mini_axi_tb.h#L65-L158)。

#### 4.4.4 代码实践

1. **目标**：把「四种观测途径」对号入座到具体接口。
2. **步骤**：
   - 在 [`hw_primitives.cc`](https://github.com/google-coral/coralnpu/blob/77bc1ffe06dbf3b7bafc7eab167ead2b42668df9/hw_sim/hw_primitives.cc) 里确认 `Clock::Step` 推进节拍（观测的基础）；在 [`mailbox.h`](https://github.com/google-coral/coralnpu/blob/77bc1ffe06dbf3b7bafc7eab167ead2b42668df9/hw_sim/mailbox.h) 里确认信箱就是 16 字节。
   - 在 [`core_mini_axi_simulator.cc:80-114`](https://github.com/google-coral/coralnpu/blob/77bc1ffe06dbf3b7bafc7eab167ead2b42668df9/hw_sim/core_mini_axi_simulator.cc#L80-L114) 记录两个回调的签名与用法：`WriteCallback(addr, data)→resp` 用来**捕获**内核写出，`ReadCallback(addr)→data` 用来**回放**信箱内容。
3. **观察**：`WriteCallback` 的 strobe 掩码决定了哪几个字节真正被改写；`ReadCallback` 总是回 `last=1`（单拍事务）。
4. **预期**：你能口述「内核写 0x20000000 → WriteCallback → mailbox.message；宿主 ReadMailbox → 看到 0xDEADBEEF」。

#### 4.4.5 小练习与答案

- **练习 1**：为什么 `mailbox_example.cc` 里内核访问 `0x20000000` 就能被宿主看见，而访问 DTCM（如 `0x10000` 起）却不会进信箱？
- **答案**：`0x20000000` 走的是内核的 **AXI master** 端口（外部内存），仿真器在 master 驱动上注册了 `WriteCallback`/`ReadCallback`，所以会被捕获进信箱；而 DTCM 是内核**内部** SRAM，不经过 master 端口，宿主只能用 `ReadTCM` 主动去读，不会触发信箱回调。
- **练习 2**：命令行工具里对应「读信箱」的等价机制是什么？
- **答案**：命令行工具主要靠 **tohost/半主机** 机制（[`core_mini_axi_tb.cc:470-828`](https://github.com/google-coral/coralnpu/blob/77bc1ffe06dbf3b7bafc7eab167ead2b42668df9/tests/verilator_sim/coralnpu/core_mini_axi_tb.cc#L470-L828) 的 `tohost_reader_thread`，支持 `sys_write` 把 stdout 打到宿主终端）以及 `--instr_trace`，而不是 mailbox。两条入口的 I/O 观测方式不同。

---

## 5. 综合实践

把本讲四条主线串起来，完成一次「编 → 译 → 跑 → 看」的完整闭环：

1. **编**（[u2-l2](u2-l2-write-compile-program.md) 已完成）：确认你已有 `examples/coralnpu_v2_hello_world_add_floats.elf`。
2. **构建仿真器**：

   ```bash
   bazel build //tests/verilator_sim:core_mini_axi_sim
   bazel build //examples:coralnpu_v2_hello_world_add_floats
   ```

3. **跑起来**（参照 [README Quick Start](https://github.com/google-coral/coralnpu/blob/77bc1ffe06dbf3b7bafc7eab167ead2b42668df9/README.md#L31-L45)）：

   ```bash
   bazel-bin/tests/verilator_sim/core_mini_axi_sim \
     --binary bazel-out/k8-fastbuild-ST-dd8dc713f32d/bin/examples/coralnpu_v2_hello_world_add_floats.elf
   ```

   - **观察现象**：进程退出码为 0；日志显示程序加载、运行、halt。结合 [u2-l1](u2-l1-toolchain-linker-tcm.md) 解释：`main` 成功返回后 CRT 执行 `mpause` → `io_halted` 置位 → `run()` 第 ④ 步条件变量被唤醒 → 第 ⑤ 步 `CheckStatusSync` 读到 STATUS halted=1 → 返回成功。
   - **预期结果**：退出码 0；若改为一个会出错的程序，`io_fault` 会触发，退出码非 0。
4. **开指令轨迹再看一次**：加 `--instr_trace`，观察控制台逐条打印的退休指令（PC、机器码），对照你的 C++ 源码理解每条指令的来源。
5. **对照库式入口**：阅读 [`core_mini_axi_simulator_example.cc`](https://github.com/google-coral/coralnpu/blob/77bc1ffe06dbf3b7bafc7eab167ead2b42668df9/hw_sim/core_mini_axi_simulator_example.cc)，确认它用 `Run`/`WaitForTermination`/`ReadMailbox` 三步完成了同样的「跑 + 等停 + 看输出」，从而把两条入口的关系彻底打通。

> 若环境无法实际执行 Bazel，请标注「待本地验证」并只做源码层面的流程梳理——不要假装已经跑过。

## 6. 本讲小结

- CoralNPU 的 Verilator 仿真有**两条入口**：命令行工具 `core_mini_axi_sim`（SystemC 体系，README Quick Start 路径）和 C++ 库 `CoralNPUSimulator`（`hw_sim/`，npusim 调用路径），两者驱动同一个 `VCoreMiniAxi` 模型。
- 8 个仿真器变体由 `BUILD` 的 `template_rule` 从**同一份 `core_mini_axi_sim.cc`** 批量生成，差别仅在链接的 testbench 库与 `VERILATOR_MODEL` 宏。
- 启动链固定为：**加载 ELF（含写 `0x30004` PC_START）→ 解除时钟门控 → 释放复位 → 等待 `io_halted` → 读 `0x30008` STATUS**。
- `CoralNPUSimulator` 是三层封装（抽象接口 → `CoreMiniAxiSimulator` → `CoreMiniAxiWrapper` → `hw_primitives`），把 Verilator/AXI 细节藏在高层方法背后。
- 观测内核状态有四种途径：halt/fault/wfi 信号、CSR STATUS、`--instr_trace` 指令轨迹、以及 mailbox 信箱（`WriteCallback`/`ReadCallback` 捕获内核 master 写出）。
- `main` 成功返回后 CRT 的 `mpause` 触发 `io_halted`，这是仿真器判断「正常跑完」的核心信号。

## 7. 下一步学习建议

- 想用 Python 更系统地写测试台（reset → load_elf → execute_from → wait_for_halted → read）？继续 [u2-l4 cocotb 测试框架入门](u2-l4-cocotb-testbench-intro.md)。
- 想搞清 `0x30000/0x30004/0x30008` 这些 CSR 的位域、以及外部主机启动 CoralNPU 的完整协议？看 [u3-l5 CSR 接口、内存映射与启动控制](u3-l5-csr-boot-control.md)。
- 想了解 Verilator 仿真目标是怎么由 `rules/verilog.bzl` + `.vlt.tpl` 定义与构建的？留到 [u11-l1 Verilator 仿真流程深入](u11-l1-verilator-flow.md)。
