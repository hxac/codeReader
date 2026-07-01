# cocotb 测试框架入门

## 1. 本讲目标

学完本讲后，你应当能够：

- 说清楚一个 CoralNPU cocotb 测试台的**标准生命周期**：`init → reset → clock → load_elf → write 输入 → execute_from → wait_for_halted → read 输出`。
- 理解 `CoreMiniAxiInterface` 这个测试接口扮演的两种角色：向内核 `io_axi_slave_*` 端口**注入**命令/数据的「外部主机」，以及响应内核 `io_axi_master_*` 端口**访存**请求的「外部存储服务器」。
- 掌握 `load_elf / lookup_symbol / write / read / execute_from / wait_for_halted` 这一组高层接口各自的职责与底层 AXI 行为。
- 学会使用独立的 `AxiSlave` 类作为「被动从机」，模拟 DUT 主动发起的总线事务（例如访问 DDR），并能区分它与 `CoreMiniAxiInterface` 的适用场景。
- 亲手补全一个最小 cocotb 测试台，把一个加法程序在 RTL 上跑通并验证结果。

## 2. 前置知识

本讲假设你已经读过前置讲义：

- **u2-l2**：知道 CoralNPU 程序是「输入缓冲 + 输出缓冲 + `main()` 计算主体」的三段式结构，缓冲用 `__attribute__((section(".data")))` 钉进 DTCM。
- **u2-l3**：知道 `main` 成功返回后 CRT 会执行 `mpause`，从而拉高 `io_halted`；也知道启动内核的 CSR 序列是「写 PC → 释放时钟门控 → 释放复位」。

此外需要几个通俗概念：

| 术语 | 通俗解释 |
| --- | --- |
| **RTL** | Register Transfer Level，用硬件描述语言（这里是 Chisel 生成的 Verilog）写出的、可被仿真的数字电路模型。 |
| **DUT** | Device Under Test，被测对象，这里就是 CoralNPU 的 `CoreMiniAxi` 顶层模块。 |
| **cocotb** | 一个 Python 仿真协同验证框架。它让 DUT 跑在 Verilator/VCS 里，而你在 Python 里驱动它的输入、观测它的输出，就像在真实 SoC 上跑一个「主机 CPU」。 |
| **AXI manager / subordinate** | AXI 总线里的发起方（manager，旧称 master）与响应方（subordinate，旧称 slave）。CoralNPU 对外同时有两种身份：对命令它是 subordinate（别人给它派活），对访存它是 manager（它去读写外部存储）。 |
| **DPI** | Direct Programming Interface，仿真器里 C/C++ 与 Verilog 互相调用的桥。本讲的「后门加载」就靠它。 |
| **coroutine / `async`/`await`** | Python 的异步协程。cocotb 里每个总线通道、每个监听器都是一个协程，它们并发推进、用 `await` 等待时钟沿或事件。 |

一句话直觉：**真实芯片里有一个主机 CPU 经 AXI 给 CoralNPU 派活；cocotb 让你用 Python 把这个「主机 CPU」脚本化，对着 RTL 重放同样的流程。**

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [doc/tutorials/writing_coralnpu_programs.md](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/doc/tutorials/writing_coralnpu_programs.md) | 官方入门教程，逐步演示如何把一个加法程序配进 cocotb 测试台，是本讲「生命周期」一节的骨架。 |
| [coralnpu_test_utils/core_mini_axi_interface.py](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/coralnpu_test_utils/core_mini_axi_interface.py) | 测试接口库的核心。`CoreMiniAxiInterface` 类封装了驱动 `CoreMiniAxi` DUT 的全部高层方法，是绝大多数 cocotb 测试的基座。 |
| [coralnpu_test_utils/axi_slave.py](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/coralnpu_test_utils/axi_slave.py) | 一个独立的、被动响应的 AXI **从机**模型，用于模拟「DUT 主动访问的外部存储/外设」。 |
| [coralnpu_test_utils/backdoor.py](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/coralnpu_test_utils/backdoor.py) | `load_elf` 的「后门路径」底层，经 DPI `sram_backdoor_load_c` 直接把数据塞进仿真器里的 SRAM。 |
| [tests/cocotb/tutorial/tutorial.py](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/tests/cocotb/tutorial/tutorial.py) | 官方教程配套的测试台骨架，留有 4 个 TODO，正是本讲的实践对象。 |
| [tests/cocotb/tutorial/program.cc](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/tests/cocotb/tutorial/program.cc) | 官方教程配套的程序骨架，同样留有 TODO。 |
| [tests/cocotb/tutorial/BUILD](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/tests/cocotb/tutorial/BUILD) | 把 `.py` 测试台、`.elf` 程序、Verilator 模型粘到一起的 Bazel 目标，定义了 `bazel run //tests/cocotb/tutorial:tutorial`。 |

## 4. 核心概念与源码讲解

### 4.1 cocotb 测试台的标准生命周期

#### 4.1.1 概念说明

CoralNPU 是一颗「run-to-completion」的协处理器：它自己不会主动打印结果，也不会主动索取输入。真实使用场景里，**主机 CPU** 负责把输入数据写进 CoralNPU 的 DTCM、把程序写进 ITCM、然后「敲一下门」（写 CSR）让它跑、最后等它停下来再把结果读回去。

cocotb 测试台的本质，就是用 Python 把这个「主机 CPU」自动化重放一遍，只不过对象是 RTL 而不是真实芯片。因此几乎所有 CoralNPU 测试都遵循**同一个生命周期**：

```
┌─────────┐   ┌────────┐   ┌───────────┐   ┌──────────┐   ┌──────────────┐   ┌──────────────┐   ┌────────┐
│ init()  │──▶│reset() │──▶│ load_elf  │──▶│ write    │──▶│ execute_from │──▶│wait_for_halted│─▶│ read   │
│ 启动监控 │   │ 复位   │   │ 装载程序  │   │ 写输入   │   │  启动内核    │   │  等内核停下  │   │ 读输出 │
└─────────┘   └────────┘   └───────────┘   └──────────┘   └──────────────┘   └──────────────┘   └────────┘
                                │                                              │
                                └──── lookup_symbol 查输入/输出缓冲地址 ───────┘
```

这六步和官方教程 [doc/tutorials/writing_coralnpu_programs.md](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/doc/tutorials/writing_coralnpu_programs.md) 里「Creating the test bench」一节用 `diff` 一步步补全的过程完全对应。

#### 4.1.2 核心流程

把官方教程最终拼出的测试台抽象成伪代码：

```python
@cocotb.test()
async def core_mini_axi_tutorial(dut):
    # ① 搭台：建接口、启动监控协程、复位、起时钟
    core_mini_axi = CoreMiniAxiInterface(dut)
    await core_mini_axi.init()
    await core_mini_axi.reset()
    cocotb.start_soon(core_mini_axi.clock.start())

    # ② 装载程序，拿到入口地址 entry_point
    with open(elf_path, "rb") as f:
        entry_point = await core_mini_axi.load_elf(f)

    # ③ 查输入/输出缓冲地址（symbol），写两组输入到 DTCM
        in1 = core_mini_axi.lookup_symbol(f, "input1_buffer")
        in2 = core_mini_axi.lookup_symbol(f, "input2_buffer")
        out = core_mini_axi.lookup_symbol(f, "output_buffer")
    await core_mini_axi.write(in1, np.arange(8, dtype=np.uint32))
    await core_mini_axi.write(in2, 8994 * np.ones(8, dtype=np.uint32))

    # ④ 启动内核并等它停下
    await core_mini_axi.execute_from(entry_point)
    await core_mini_axi.wait_for_halted()

    # ⑤ 读回输出并打印
    rdata = (await core_mini_axi.read(out, 4 * 8)).view(np.uint32)
    print(f"I got {rdata}")
```

注意一个 CoralNPU 特有的 I/O 约定：程序里**没有 `printf`**。输入输出全靠「主机与内核共享 DTCM」。这也是为什么缓冲必须是全局变量（有固定可查地址），并且我们在测试台里要用 `lookup_symbol` 去问「这个缓冲到底落在哪个地址」——链接脚本决定地址，测试台运行时才知道。

#### 4.1.3 源码精读

官方教程用 `diff` 分四步把测试台补全。这里给出每一步对应文档行号，方便对照：

- 第一步：建台 + `load_elf` 装载程序——见 [writing_coralnpu_programs.md:108-123](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/doc/tutorials/writing_coralnpu_programs.md#L108-L123)。说明：用 bazel runfiles 解析出 `.elf` 路径，`load_elf(f)` 返回入口地址。
- 第二步：`lookup_symbol` 查地址 + `write` 写输入——见 [writing_coralnpu_programs.md:130-153](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/doc/tutorials/writing_coralnpu_programs.md#L130-L153)。说明：`input1_data = np.arange(8)`、`input2_data = 8994 * np.ones(8)`。
- 第三步：`execute_from` + `wait_for_halted`——见 [writing_coralnpu_programs.md:159-185](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/doc/tutorials/writing_coralnpu_programs.md#L159-L185)。
- 第四步：`read` 并打印——见 [writing_coralnpu_programs.md:189-216](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/doc/tutorials/writing_coralnpu_programs.md#L189-L216)。

教程最后给出的运行命令与预期输出——见 [writing_coralnpu_programs.md:218-231](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/doc/tutorials/writing_coralnpu_programs.md#L218-L231)：`bazel run //tests/cocotb/tutorial:tutorial`，应得到 `I got [8994 8995 8996 8997 8998 8999 9000 9001]`（即 `arange(8)` 与 `8994` 逐元素相加）。

而这一切的起点，是仓库里那个留了 4 个 TODO 的真实骨架 [tutorial.py:43-60](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/tests/cocotb/tutorial/tutorial.py#L43-L60)，它已经替你写好了「建台 + 复位 + 起时钟」的前三步，TODO 正好对应后四步。

#### 4.1.4 代码实践

**目标**：在不跑仿真的前提下，先用纯阅读理解「生命周期」的顺序契约。

**步骤**：

1. 打开 [tests/cocotb/tutorial/tutorial.py](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/tests/cocotb/tutorial/tutorial.py)，数一数骨架里 `await` 了哪几个方法、`start_soon` 了什么。
2. 打开 [writing_coralnpu_programs.md](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/doc/tutorials/writing_coralnpu_programs.md) 的四段 `diff`，把每段 diff 对应到生命周期图里的某一步。
3. 思考：为什么 `clock.start()` 用的是 `cocotb.start_soon(...)`（后台协程），而 `reset()` 用的是 `await`（前台等待）？

**需要观察的现象 / 预期结果**：你应当能写出一张「步骤 → 方法 → 前台/后台」的对照表；其中 `clock.start()` 是后台（时钟要一直在跑），`reset()` 是前台（必须等复位完成才能继续）。本步骤为源码阅读型实践，**待本地验证**（若要实际运行，见 4.2.4）。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `await core_mini_axi.wait_for_halted()` 这一行删掉，直接 `read` 输出，会发生什么？

> **答案**：内核可能还没跑完（甚至还没开始执行 `main`），`output_buffer` 里要么是 `load_elf` 时的初值、要么是半成品，读到的结果不可信。`wait_for_halted` 是「同步点」，保证读发生在「内核停下」之后。

**练习 2**：教程里 `input2_data = 8994 * np.ones(8)`，最终输出第一个元素为什么是 8994？

> **答案**：`input1_data = np.arange(8)` 第 0 个元素是 0，`0 + 8994 = 8994`。逐元素相加的结果就是 `[8994, 8995, …, 9001]`。

---

### 4.2 CoreMiniAxiInterface：外部主机视角的 AXI 驱动

#### 4.2.1 概念说明

`CoreMiniAxiInterface` 是本讲的「主角」。它是一个 Python 类，**一头连着 DUT 的端口，一头连着你的测试逻辑**。关键在于它同时扮演两个角色，理解这两个角色就理解了整个类：

| 角色 | 驱动的 DUT 端口 | 含义 | 典型用途 |
| --- | --- | --- | --- |
| **外部主机**（注入方） | `io_axi_slave_*` | DUT 把这个端口暴露给外部，谁连上谁就是它的「老板」。 | `write` 写输入、`read` 读输出、`load_elf_axi` 经总线灌程序、写 CSR 启动内核。 |
| **外部存储服务器**（响应方） | `io_axi_master_*` | DUT 用这个端口主动去读写外部世界。 | 维护一个 4MB 的 `EXTMEM` 模型（`0x20000000`），响应内核发起的访存。 |

> 命名提示：端口叫 `slave`/`master` 是站在 **DUT 视角**——`io_axi_slave_*` 是「DUT 作为 slave」的端口，所以**外部测试代码是 master**，负责驱动它。这一点初学时极易搞反。

它还内置了一个 4MB 的外部存储模型，地址从 `0x20000000` 起，用于模拟 SoC 里 CoralNPU 会去访问的 DDR/外部 RAM。

#### 4.2.2 核心流程

整个类的运行模型可以概括为「**一群后台协程 + 几个高层方法**」：

1. **构造**：`__init__` 把 DUT 所有 AXI 相关端口握成 `ReadyValidInterface` 对象（valid/ready/bits），并初始化一个 4MB 的 numpy 数组当作 `EXTMEM`。
2. **`init()`**：用 `cocotb.start_soon` 启动一组**常驻后台协程**——一个 `_monitor_agent` 在每个时钟沿采样所有通道、把事务塞进各种 FIFO；若干 `*agent` 协程从 FIFO 取事务、按 ready/valid 握手驱动端口。这就是「AXI 引擎」。
3. **高层方法**（你直接 `await` 的那些）：`write / read / load_elf / execute_from / wait_for_halted` 等，它们往 FIFO 里塞请求，或者读 DUT 端口/CSR，背后由引擎完成真实握手。

地址路由的直觉（关键！）：

- 当 `write/read` 的地址落在 `EXTMEM`（`0x20000000`~`0x20400000`）区间，直接读写那个 numpy 数组，**不产生任何 AXI 流量**——因为这块存储本就由测试台（而非 DUT 内部）持有。
- 当地址落在 ITCM/DTCM 等 DUT **内部**存储区间，才会发起真实的 AXI 突发事务（frontdoor），经由 `io_axi_slave_*` 端口把数据真正搬进 DUT。

启动内核的 CSR 序列（与 u2-l3 一致）：

| CSR 地址 | 名字 | 写入值 | 作用 |
| --- | --- | --- | --- |
| `csr_base_addr + 4` = `0x30004` | `PC_START` | 入口地址 | 告诉内核从哪里开始取指 |
| `csr_base_addr` = `0x30000` | `RESET_CONTROL` | `1` | 释放时钟门控 |
| `csr_base_addr` = `0x30000` | `RESET_CONTROL` | `0` | 释放复位，内核起飞 |

#### 4.2.3 源码精读

**构造与默认参数**：[core_mini_axi_interface.py:155-162](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/coralnpu_test_utils/core_mini_axi_interface.py#L155-L162)——说明：默认 `csr_base_addr=0x30000`、`ext_mem_base_addr=0x20000000`、外部存储 4MB；`clock_ns=1.25` 还可被 plusarg `CLOCK_NS` 或环境变量 `COCOTB_CLOCK_NS` 覆盖。

**外部存储模型**：[core_mini_axi_interface.py:199-200](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/coralnpu_test_utils/core_mini_axi_interface.py#L199-L200)——说明：`self.memory_base_addr` 与一个 4MB 的 `np.uint8` 数组 `self.memory`，就是上表里的 `EXTMEM`。

**启动后台引擎**：[core_mini_axi_interface.py:212-220](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/coralnpu_test_utils/core_mini_axi_interface.py#L212-L220)——说明：`init()` 用 `start_soon` 启动 `_monitor_agent`、各 `master_*agent`、`slave_*agent`、`memory_*_agent`，这些协程会跑满整个仿真周期。

**总线监听器**：[core_mini_axi_interface.py:280-351](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/coralnpu_test_utils/core_mini_axi_interface.py#L280-L351)——说明：`_monitor_agent` 每个上升沿采样 `slave_write_resp/read_data`（拿回应）和 `master_read_addr/write_addr/write_data`（捕获内核主动发起的访存），分拣进各 FIFO；对 `X` 态有容错处理。

**复位**：[core_mini_axi_interface.py:456-462](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/coralnpu_test_utils/core_mini_axi_interface.py#L456-L462)——说明：`reset()` 拉低 `io_aresetn`（高电平复位，对应 active-low `aresetn`）再放开，用 `Timer` 而非 `ClockCycles`，因此**不依赖时钟已在跑**。

**写数据（地址路由核心）**：[core_mini_axi_interface.py:717-739](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/coralnpu_test_utils/core_mini_axi_interface.py#L717-L739)——说明：`write()` 把数据切片，若 `_axi_valid_memory_addr`（落在 EXTMEM）则直接写 numpy 数组，否则 `_write_transaction` 走真实 AXI 写。这正是「写 DTCM 会产生 AXI 流量、写 EXTMEM 不会」的实现。

**读数据**：[core_mini_axi_interface.py:797-812](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/coralnpu_test_utils/core_mini_axi_interface.py#L797-L812)——说明：`read()` 同样按地址路由，EXTMEM 直读数组，否则 `_read_transaction` 经 AXI 读回。

**查符号地址**：[core_mini_axi_interface.py:886-892](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/coralnpu_test_utils/core_mini_axi_interface.py#L886-L892)——说明：`lookup_symbol` 用 pyelftools 在 ELF 符号表里找 `st_value`，这就是「缓冲到底落在哪个地址」的答案。

**启动内核**：[core_mini_axi_interface.py:920-928](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/coralnpu_test_utils/core_mini_axi_interface.py#L920-L928)——说明：`execute_from(start_pc)` 依次写 `PC_START=start_pc`、`RESET_CONTROL=1`（释放时钟门控）、`RESET_CONTROL=0`（释放复位），与 u2-l3 的启动链完全一致。

**等内核停下**：[core_mini_axi_interface.py:939-946](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/coralnpu_test_utils/core_mini_axi_interface.py#L939-L946)——说明：`wait_for_halted` 每 1 个时钟周期检查直接暴露的 `io_halted` 端口（即 CRT 里 `mpause` 拉起的信号，见 u2-l3），带超时保护。

#### 4.2.4 代码实践

**目标**：亲手把官方骨架 `tutorial.py` 的 4 个 TODO 补全，跑通加法程序。

**步骤**：

1. 先补 [tests/cocotb/tutorial/program.cc](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/tests/cocotb/tutorial/program.cc) 的 TODO：声明 `input1_buffer[8]`、`input2_buffer[8]`、`output_buffer[8]`（都带 `__attribute__((section(".data")))`），在 `main` 里循环 `output_buffer[i] = input1_buffer[i] + input2_buffer[i];`。
2. 再补 [tutorial.py](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/tests/cocotb/tutorial/tutorial.py) 的 TODO：`load_elf` → `lookup_symbol` 三个缓冲 → `write` 两组输入 → `execute_from` → `wait_for_halted` → `read` 打印。完整代码即 4.1.2 的伪代码。
3. 运行：

   ```bash
   bazel run //tests/cocotb/tutorial:tutorial
   ```

**需要观察的现象 / 预期结果**：终端应打印 `I got [8994 8995 8996 8997 8998 8999 9000 9001]`（与官方教程 [writing_coralnpu_programs.md:226-230](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/doc/tutorials/writing_coralnpu_programs.md#L226-L230) 一致）。构建产物路径可在 `bazel-bin/tests/cocotb/tutorial/` 下找到。本实践依赖 Bazel + Verilator 环境，**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：`write(in1, data)` 里 `in1` 是 DTCM 地址。为什么这次写入会真的在波形上看到 AXI 写事务，而把同样的数据写到 `0x20000000` 就看不到？

> **答案**：见 [core_mini_axi_interface.py:732-737](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/coralnpu_test_utils/core_mini_axi_interface.py#L732-L737)。`0x20000000` 落在 `_axi_valid_memory_addr` 判定的 EXTMEM 区间，直接写 `self.memory` 数组，不产生 AXI 流量；DTCM 地址不在该区间，走 `_write_transaction`，产生真实 AXI 写。

**练习 2**：`execute_from` 为什么要先写 `1` 再写 `0` 到同一个 `RESET_CONTROL` 寄存器？

> **答案**：写 `1` 是「释放时钟门控」（让内核时钟转起来），写 `0` 是「释放复位」（让 PC 真正从 `PC_START` 开始跑）。顺序不能反，否则内核在时钟未就绪时就被解除复位，行为未定义。详见 [core_mini_axi_interface.py:920-928](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/coralnpu_test_utils/core_mini_axi_interface.py#L920-L928)。

---

### 4.3 load_elf 的两条路径：backdoor 与 frontdoor

> 说明：本节仍属于 `core_mini_axi_interface.py` 这个最小模块的深入，但因为它是初学者最容易踩坑、也最能体现「仿真优化」思想的部分，单独成节。

#### 4.3.1 概念说明

`load_elf` 要把 ELF 里每个 `PT_LOAD` 段（代码、只读数据、已初始化数据）搬进 DUT 内存。搬法有两种：

- **frontdoor（前门）**：完全模拟真实主机——经 `io_axi_slave_*` 端口发起 AXI 写突发，一个 beat 一个 beat 把字节搬进 ITCM/DTCM。**最真实，但最慢**：一个大程序要跑成千上万个 AXI 事务。
- **backdoor（后门）**：跳过总线，直接经仿真器的 DPI 函数 `sram_backdoor_load_c` 把数据「瞬移」进 RTL 里的 SRAM 数组。**不真实，但极快**，因为它不消耗仿真周期。

`load_elf` 默认走 backdoor，这是 cocotb 回归测试能快速跑完数千个用例的关键优化。

#### 4.3.2 核心流程

```
load_elf(f) ──▶ 默认 backdoor=True（除非 COCOTB_USE_FRONTDOOR=1）
                     │
        ┌────────────┴────────────┐
        ▼                         ▼
  load_elf_backdoor          load_elf_axi (frontdoor)
  遍历 PT_LOAD 段：           遍历 PT_LOAD 段：
   - 落在 EXTMEM？→写numpy     - 落在 EXTMEM？→写numpy
   - 否则 → DPI backdoor_load  - 否则 → await self.write()（AXI 突发）
  返回 entry_point            返回 entry_point
```

两条路径对 EXTMEM 段的处理一致（都写 numpy 数组 `self.memory`），差别只在「落在 DUT 内部 SRAM 的段」怎么搬：backdoor 用 DPI，frontdoor 用 AXI。

#### 4.3.3 源码精读

**总入口**：[core_mini_axi_interface.py:824-837](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/coralnpu_test_utils/core_mini_axi_interface.py#L824-L837)——说明：`load_elf` 默认 `backdoor=True`，若环境变量 `COCOTB_USE_FRONTDOOR=1` 或显式传 `backdoor=False` 则走 frontdoor。

**后门路径**：[core_mini_axi_interface.py:856-879](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/coralnpu_test_utils/core_mini_axi_interface.py#L856-L879)——说明：`load_elf_backdoor` 遍历段，EXTMEM 段写 numpy，其余调 `backdoor_load`（DPI）。注释明确指出：DPI `sram_backdoor_load_c` **只能到达 DUT 内部由 Chisel 生成的 SRAM，到不了 Python 的 AxiSlave 模型**——所以 EXTMEM 段才要单独用 numpy 处理。

**前门路径**：[core_mini_axi_interface.py:839-854](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/coralnpu_test_utils/core_mini_axi_interface.py#L839-L854)——说明：`load_elf_axi` 对非 EXTMEM 段调 `await self.write()`，即走 4.2 讲过的真实 AXI 写。用于需要专门验证「前门加载」正确性的测试。

**DPI 底层**：[backdoor.py:20-29](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/coralnpu_test_utils/backdoor.py#L20-L29) 与 [backdoor.py:32-55](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/coralnpu_test_utils/backdoor.py#L32-L55)——说明：用 `ctypes.CDLL(None)` 拿到仿真器进程里已链接的 `sram_backdoor_load_c` 符号（要求 Verilator 用 `-rdynamic` 编译），按 `(addr, data_ptr, len)` 调用，直接改写仿真器内存。

#### 4.3.4 代码实践

**目标**：对比两条加载路径在「仿真周期消耗」上的差异。

**步骤**：

1. 先用默认（backdoor）跑一次 4.2.4 的测试，留意日志里 `Backdoor loading N bytes to 0x...` 这类信息（由 [core_mini_axi_interface.py:877](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/coralnpu_test_utils/core_mini_axi_interface.py#L877) 打印）。
2. 设环境变量强制走前门再跑一次：

   ```bash
   COCOTB_USE_FRONTDOOR=1 bazel run //tests/cocotb/tutorial:tutorial
   ```

3. 对比两次的总仿真时间或 `wait_for_halted` 返回的 `cycle_count`（[core_mini_axi_interface.py:939-946](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/coralnpu_test_utils/core_mini_axi_interface.py#L939-L946) 返回内核跑完所用周期数）。

**需要观察的现象 / 预期结果**：两次最终打印的 `I got [...]` 应**完全一致**（加载路径不影响程序正确性）；但前门路径因为多了大量 AXI 写周期，整体仿真墙钟时间明显更长。本实践依赖本地仿真环境，**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `load_elf_backdoor` 对落在 `0x20000000`（EXTMEM）的段不调 DPI，而是写 numpy 数组？

> **答案**：因为 `EXTMEM` 是**测试台侧**用 numpy 维护的存储模型，根本不在 DUT 的 RTL 里；DPI `sram_backdoor_load_c` 只能写 DUT 内部的 Chisel SRAM，物理上到不了那块 numpy 内存。所以必须分流处理。见 [core_mini_axi_interface.py:871-876](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/coralnpu_test_utils/core_mini_axi_interface.py#L871-L876)。

**练习 2**：如果一个测试的目的是「验证 AXI slave 端口能正确接收大批量写」，应该用哪条路径？

> **答案**：用 frontdoor（`COCOTB_USE_FRONTDOOR=1` 或 `load_elf(f, backdoor=False)`）。只有 frontdoor 才会真正经 `io_axi_slave_*` 发起 AXI 写，从而覆盖到 slave 端口的写数据通路。

---

### 4.4 AxiSlave：扮演外部从机的被动 AXI 模型

#### 4.4.1 概念说明

4.2 节的 `CoreMiniAxiInterface` 站在「外部主机」一侧，驱动 DUT 的 `io_axi_slave_*` 端口。但很多测试里，**DUT 自己是 master**——它经 `io_axi_master_*` 主动去访问外部 DDR、外设。这时测试台需要扮演一个**被动从机**，对 DUT 发来的读/写请求作出回应。

`coralnpu_test_utils/axi_slave.py` 里的 `AxiSlave` 类就是这样一个「被动从机」模型。它和 `CoreMiniAxiInterface` 是**镜像角色**：

| | CoreMiniAxiInterface | AxiSlave |
| --- | --- | --- |
| 站位 | 外部主机（主动发起） | 外部从机（被动响应） |
| 驱动的端口 | DUT 的 `io_axi_slave_*`（DUT 视角是 slave） | DUT 的某个 master 端口（DUT 视角是 master） |
| 存储模型 | numpy 数组（`EXTMEM`） | 可选 `dict` 字节级存储 |
| 典型用途 | 给内核灌程序/数据、读结果、写 CSR | 模拟 DDR 控制器/外设，回应内核主动访存 |
| 实际消费者 | 几乎所有 cocotb 测试 | `tests/cocotb/tlul/test_subsystem.py` 等 SoC 子系统测试 |

#### 4.4.2 核心流程

`AxiSlave` 的工作方式是 `start()` 启动 7 个后台协程，分别守住 AXI 的五个通道（AW/W/B/AR/R）外加两个「处理器」：

```
DUT(master) ──AW──▶ _aw_agent ──▶ aw_queue ──┐
DUT(master) ──W───▶ _w_agent  ──▶ w_queue  ──┤  _write_handler: 取 AW+W，写本地存储(若有)，回 B
                                              │                      └─▶ b_queue ──▶ _b_agent ──B──▶ DUT
DUT(master) ──AR──▶ _ar_agent ──▶ ar_queue ─────────▶ _read_handler: 取 AR，读本地存储(若有)，回 R
                                                                      └─▶ r_queue ──▶ _r_agent ──R──▶ DUT
```

两种存储模式：

- `has_memory=True`：维护一个字节级 `dict`（`self.memory`），读返回存过的字节，写按 `strb` 逐字节更新——能正确模拟一块可读写存储。
- `has_memory=False`：读恒返回 `0xDEADBEEF`，写回 `resp=3`（`DECERR`）——用来模拟「这块地址没有存储，访问应报错」。

#### 4.4.3 源码精读

**构造**：[axi_slave.py:19-36](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/coralnpu_test_utils/axi_slave.py#L19-L36)——说明：参数有 `dut/name/clock/reset/log`，以及 `has_memory` 与 `mem_base_addr`；为每个通道建一个 `Queue`。

**启动 7 个协程**：[axi_slave.py:38-45](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/coralnpu_test_utils/axi_slave.py#L38-L45)——说明：`start()` 用 `cocotb.start_soon` 拉起 `_aw_agent/_w_agent/_b_agent/_ar_agent/_r_agent/_write_handler/_read_handler`。

**读处理（带存储）**：[axi_slave.py:47-68](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/coralnpu_test_utils/axi_slave.py#L47-L68)——说明：`_read_handler` 从 `ar_queue` 取请求；若有存储，按总线宽度对齐地址、从 `self.memory` 取字节拼成 `read_data`；否则填 `0xDEADBEEF`；组装成 R 事务（`last=1`）放进 `r_queue`。

**写处理（带存储与报错）**：[axi_slave.py:70-93](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/coralnpu_test_utils/axi_slave.py#L70-L93)——说明：`_write_handler` 取 AW+W；若有存储，按 `strb` 逐字节写入对齐后的地址；否则 `resp=3 (DECERR)` 并打 `error` 日志；回 B 事务。

**真实使用范例**：[tests/cocotb/tlul/test_subsystem.py:517-518](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/tests/cocotb/tlul/test_subsystem.py#L517-L518)——说明：在 SoC 子系统测试里，用 `AxiSlave(..., has_memory=True, mem_base_addr=DDR_MEM_BASE)` 同时模拟 `ddr_ctrl_axi` 与 `ddr_mem_axi` 两个从机，让 DUT 主动发起的 DDR 访问有真实回应。

#### 4.4.4 代码实践

**目标**：通过阅读理解 `AxiSlave` 的两种存储模式，并定位它的真实消费者。

**步骤**：

1. 读 [axi_slave.py:47-93](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/coralnpu_test_utils/axi_slave.py#L47-L93)，对照写出「`has_memory=True` vs `False`」时读/写各返回什么。
2. 打开 [tests/cocotb/tlul/test_subsystem.py](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/tests/cocotb/tlul/test_subsystem.py)，搜索 `AxiSlave(`，确认这些从机挂在 DUT 的哪几个 master 端口上、`mem_base_addr` 各是多少。
3. 思考：为什么这个测试不用 `CoreMiniAxiInterface` 来模拟 DDR？

**需要观察的现象 / 预期结果**：你应当能填出下表，并理解——`CoreMiniAxiInterface` 是「主动派活」的主机模型，无法扮演「被动等 DUT 来访问」的 DDR 从机角色，所以子系统测试需要 `AxiSlave`。

| 模式 | 读返回 | 写返回(resp) |
| --- | --- | --- |
| `has_memory=True` | 本地存储里的真实字节 | `0 (OKAY)` |
| `has_memory=False` | `0xDEADBEEF` | `3 (DECERR)` |

本步骤为源码阅读型实践，结论可直接从源码得出；**待本地验证**（若要在仿真中观察实际报错，需运行对应 SoC 子系统测试）。

#### 4.4.5 小练习与答案

**练习 1**：`AxiSlave` 的存储用的是 Python `dict`（`self.memory = {}`），而 `CoreMiniAxiInterface` 用的是预分配的 numpy 数组。为什么 `AxiSlave` 选 `dict`？

> **答案**：`AxiSlave` 的存储是**稀疏、按需**的——只有被写过的地址才有值，用 `dict` 做「字节地址 → 值」映射（见 [axi_slave.py:56](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/coralnpu_test_utils/axi_slave.py#L56) 的 `self.memory.get(addr+i, 0x00)`），不必预先分配整块内存。`CoreMiniAxiInterface` 的 `EXTMEM` 是固定 4MB 连续区，用 numpy 数组更高效。

**练习 2**：如果 DUT 经 master 端口写了一个 `has_memory=False` 的 `AxiSlave`，DUT 侧会观察到什么？

> **答案**：B 通道回应 `resp=3 (DECERR)`，且测试台日志打印一条 `error`。DUT 的 AXI master 据此可判断该次写访问到了一个「没有存储 backing」的地址。见 [axi_slave.py:84-86](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/coralnpu_test_utils/axi_slave.py#L84-L86)。

---

## 5. 综合实践

把本讲三个最小模块串起来，完成一个**「改算法 + 改测试」的小闭环**：

1. **改程序**：复制 [tests/cocotb/tutorial/program.cc](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/tests/cocotb/tutorial/program.cc)，把 `main` 里的逐元素**加法**改成逐元素**乘法**（`output_buffer[i] = input1_buffer[i] * input2_buffer[i];`）。三个缓冲仍保留 `__attribute__((section(".data")))`。
2. **改测试台**：复制 [tutorial.py](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/tests/cocotb/tutorial/tutorial.py)，按 4.1.2 的生命周期补全，但故意把输入换成方便心算的两组：`input1 = np.arange(8)`、`input2 = 2 * np.ones(8)`。
3. **预测**：先在草稿上手算预期输出。
4. **跑通并验证**：用 `bazel run` 运行（若复制到了新目录，需参照 [tests/cocotb/tutorial/BUILD](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/tests/cocotb/tutorial/BUILD) 里的 `cocotb_test_suite` 写一个新目标），确认打印结果与手算一致。
5. **进阶观察**：在你的测试台里加一行，对 `output_buffer` 地址同时用 `read`（frontdoor）读一次、再用 `CoreMiniAxiInterface` 直接读 `EXTMEM` 区做对照，体会 4.2 讲的「地址路由」——同一个 `read` 方法，落在不同区间走的是完全不同的路径。

> 若没有本地 Bazel/Verilator 环境，第 4 步为**待本地验证**；但第 1~3、5 步（改代码、手算、读源码理解路由）不依赖运行环境，可以独立完成。手算预期：`arange(8) * 2 = [0, 2, 4, 6, 8, 10, 12, 14]`。

## 6. 本讲小结

- CoralNPU 的 cocotb 测试台遵循固定生命周期：**`init → reset → clock → load_elf → lookup_symbol+write 输入 → execute_from → wait_for_halted → read 输出`**，本质是「用 Python 脚本化真实 SoC 里的主机 CPU」。
- `CoreMiniAxiInterface` 一身两角：向 `io_axi_slave_*` 端口注入命令/数据（外部主机），同时用 numpy 数组响应 `io_axi_master_*` 的访存请求（外部 `EXTMEM` 服务器）；地址落在 `0x20000000` 区间直读 numpy、落在 DTCM 等内部区才产生真实 AXI 流量。
- `load_elf` 默认走 **backdoor**（DPI 直写 SRAM，快），可经 `COCOTB_USE_FRONTDOOR=1` 切到 **frontdoor**（真实 AXI 写，慢但更贴近硬件），两条路径对 `EXTMEM` 段都统一写 numpy。
- `execute_from` 的启动序列是「写 `PC_START`(0x30004) → 写 `RESET_CONTROL`(0x30000)=1 释放时钟门控 → 写 `RESET_CONTROL`=0 释放复位」，`wait_for_halted` 直接采样 `io_halted` 端口。
- `AxiSlave` 是镜像角色——被动响应 DUT 主动发起的事务，`has_memory` 决定它是真存储（dict）还是报错从机（读 `0xDEADBEEF`、写 `DECERR`），用于 SoC 子系统测试里模拟 DDR/外设。

## 7. 下一步学习建议

- **横向扩展接口用法**：读 [tests/cocotb/core_mini_axi_debug.py](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/tests/cocotb/core_mini_axi_debug.py)，看 `CoreMiniAxiInterface` 还提供了哪些高级能力（如 `dm_read_reg/dm_write_reg` 等 RISC-V Debug 抽象命令），为 u9-l1（Debug 模块）预热。
- **纵向深入 CSR 与启动**：本讲只用了 `execute_from` 的高层封装，下一阶段可进入 u3-l5（CSR 接口、内存映射与启动控制），从 RTL 侧理解 `RESET_CONTROL/PC_START/STATUS` 这些寄存器的位域。
- **回归测试体系**：本讲只跑了一个 tutorial 用例；想看 cocotb 在工程里如何规模化组织成千上万个 ISA/CSR/RVV 回归，可继续读 u11-l3（cocotb 回归测试体系）与 `rules/coco_tb.bzl`。
- **动手建议**：在进入更深的 RTL 讲义前，强烈建议先按「综合实践」亲手改一次算法并跑通——拥有「我能让这颗 NPU 跑我的代码」的第一手正反馈，会显著降低后续阅读标量核/RVV 后端源码的门槛。
