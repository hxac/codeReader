# C-model 参考模型（cmod）

## 1. 本讲目标

本讲聚焦 NVDLA 仓库里的 `cmod/` 目录——一份用 SystemC/C++ 写成的「C 参考模型」。读完本讲，你应该能够：

- 说清 `cmod` 是什么：一份与 RTL（`vmod/`）一一对应、但用事务级建模（TLM）描述的软件黄金模型，可被编译成 `libnvdla_cmod.so`。
- 读懂 `NV_NVDLA_core` 如何像 RTL 顶层那样把所有引擎（bdma/cdma/csc/cmac/cacc/sdp/pdp/cdp/…）例化并用 TLM socket 连起来。
- 理解「黄金比对」的思想：同一份激励同时喂给 RTL 与 cmod，cmod 产出期望输出，再与 RTL 结果逐拍/逐事务比对，从而验证 RTL。
- 认识到一个重要事实：cmod 的源码在本仓库是完整的、可构建的，但**消费它的比对框架并未随本仓库开源**——这是阅读 cmod 时必须诚实面对的边界。

本讲承接 u7-l1（trace-player 测试平台）与 u7-l2（CSB 激励与 trace 格式）：那两讲里的测试平台是「直接拿 trace 驱动 RTL DUT」；本讲则介绍「另一条并行的软件模型」，它能在数值层面为 RTL 提供参照。

## 2. 前置知识

本讲会用到几个验证与建模术语，先用通俗语言解释：

- **参考模型 / 黄金模型（reference / golden model）**：用一种「更简单、更可信」的方式（通常是 C/C++）实现同一份规格。验证时，把同一组输入同时送进 RTL 和参考模型，比较两者的输出。若不一致，说明 RTL（或参考模型）有 bug。参考模型之所以「可信」，是因为它用高层算法直写，绕开了时序、握手等容易出错的地方。
- **SystemC**：一个 C++ 库，用 `sc_module`（模块）、`sc_signal`（信号）、`SC_THREAD/SC_METHOD`（并发进程）、`sc_fifo`（FIFO）等类来描述硬件。本质上是「在 C++ 里写硬件并发」，比 RTL 仿真快，又能保留模块/端口/时钟的概念。
- **TLM（Transaction-Level Modeling，事务级建模）**：不再逐周期翻转每一根线，而是把一次「读/写 N 字节、地址 X」打包成一个**事务（transaction）**，通过 `b_transport()` 函数调用传递。一次 `b_transport` 就代表一次完整的存储或寄存器访问，因而比逐拍仿真快几个数量级。
- **`b_transport`**：TLM-2.0 的标准阻塞传输函数。调用它就像「把一个事务交给对方处理，等对方返回结果」。
- **DUT（Design Under Test，被测设计）**：u7-l1 里讲过的 `NV_nvdla` RTL；本讲里 cmod 是它的「软件孪生」。
- **影子/影偶（shadow）配置、done 中断、CSB 配置总线**：这些在前置讲义（u2-l3、u2-l4、u2-l1）已建立，本讲会复用。

一句话定位：**cmod = 用 SystemC/TLM 写的、与 RTL 引擎一一对应的软件模型，是黄金比对的「期望值来源」。**

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 |
|------|------|
| `cmod/README.md` | 一句话说明 cmod 是 OpenDLA 的 C-model，并标注「未包含在初始发布中」。 |
| `cmod/Makefile` | 把整套 C++ 源码编译成 `libnvdla_cmod.so` 的构建脚本，依赖 SystemC。 |
| `cmod/nvdla_core/NV_NVDLA_core.h` / `.cpp` | cmod 的「core」层：声明并例化全部引擎，用 TLM socket 把它们连起来（对应 RTL 的 `NV_NVDLA_partition_*` 连线）。 |
| `cmod/nvdla_top/NV_nvdla.h` / `.cpp` | cmod 的最顶层：包住 `NV_NVDLA_core` 与 CSB/AXI 适配器，对外暴露与 RTL 同名的端口。 |
| `cmod/nvdla_top/NvdlaCsbAdaptor.cpp` | 把外部标准 TLM/AXI 事务翻译成内部 CSB 事务的适配器（cmod 里没有 `apb2csb`，由它替代）。 |
| `cmod/bdma/NV_NVDLA_bdma.h` / `.cpp` | BDMA 引擎的 C 模型，展示「base + reg_model + 手写功能核」的标准引擎结构。 |
| `cmod/bdma/gen/bdma_reg_model.h` | 由 SystemRDL 自动生成的 BDMA 寄存器访问器（与 RTL 的 `NV_NVDLA_CDMA_dual_reg.v` 同源）。 |
| `cmod/include/nvdla_config.h` | cmod 的特性配置头（对应 RTL 的 spec/defs 宏）。 |
| `tools/etc/build.config` | 构建依赖图，登记 `cmod_top` 这个 sandbox 及其依赖。 |

## 4. 核心概念与源码讲解

### 4.1 cmod 参考模型：SystemC/TLM 黄金模型

#### 4.1.1 概念说明

`cmod/` 是 NVDLA 的 **C 参考模型**：用 SystemC/TLM 把整个加速器用软件重新实现一遍。它和 `vmod/`（RTL）描述的是**同一份规格**，只是抽象层级不同：

- RTL（`vmod/`）：逐周期、逐信号、逐触发器，精确到时钟沿，慢但真实。
- cmod（`cmod/`）：事务级，一次 `b_transport` 就是一次完整访问，快但不含精确时序。

为什么需要它？因为验证 RTL 时，光靠 trace-player（u7-l1/u7-l2）「能跑通」还不够——跑通只能证明「没崩」，不能证明「算得对」。要证明算得对，就得有一个可信的「期望值」来源。cmod 正是那个来源：同一组输入下，cmod 用高层算法算出该得什么结果，RTL 也算一遍，两者一比对，差异即 bug。

需要特别强调一条**诚实边界**：cmod 的源码在本仓库是完整的，但 README 明确写着它「未包含在初始发布中」，指的是**那个加载 `libnvdla_cmod.so`、同时驱动 RTL 与 cmod 并自动比对的验证框架**没有随本仓库开源。因此本仓库里 cmod 是「可读、可编译、可单独运行」的，但「端到端 RTL↔cmod 自动 diff」需要外部框架。本讲会基于真实源码讲清楚 cmod 本身，对比对流程讲清思想与代码接入点，不虚构仓库里不存在的脚本。

#### 4.1.2 核心流程

cmod 的构建与使用流程：

1. **配置 SystemC 环境**：构建前必须在 `tree.make` 里定义 `SYSTEMC` 指向 SystemC 安装目录。
2. **`tmake` 驱动 `cmod_top` sandbox**：`build.config` 里 `cmod_top` 依赖 `defs`（特性宏）与 `manual`（SystemRDL 寄存器规格），保证 cmod 用到的配置头和寄存器模型先生成。
3. **`make` 编译**：`cmod/Makefile` 把约 80 个 `.cpp`（各引擎 + 自动生成的 reg_model + hls 浮点库 + 适配器）编译、链接成 `libnvdla_cmod.so`。
4. **被外部框架加载**：外部验证框架 dlopen 这个 `.so`，用 `extern "C"` 工厂函数 `NV_nvdlaCon()` 实例化顶层 `NV_nvdla`，把同一份 trace 同时喂给 RTL 与 cmod。
5. **比对**：cmod 产出期望的存储写事务；框架把这些期望值与 RTL 实际写到存储里的数据逐个比对。

#### 4.1.3 源码精读

先看 README 的定位与那条诚实边界：

[cmod/README.md:1-3](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/cmod/README.md#L1-L3) —— 标题 `# C-model for OpenDLA`，并注明 `** Not included in the initial release. **`，即消费 cmod 的验证框架未随初始发布开源。

再看构建脚本如何把它编成动态库。`cmod/Makefile` 强制要求 SystemC：

[cmod/Makefile:6-10](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/cmod/Makefile#L6-L10) —— 若 `SYSTEMC` 未定义就报错；产物目标默认是 `libnvdla_cmod.so`。

[cmod/Makefile:19](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/cmod/Makefile#L19) —— 链接 `libsystemc-2.3.0.so`，说明 cmod 是 SystemC-2.3 程序。

`SRCS` 变量是整套源码的清单，本身就是「cmod 与 RTL 引擎对应关系」的最佳索引：

[cmod/Makefile:21-97](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/cmod/Makefile#L21-L97) —— 列出全部源文件，可见每个 RTL 引擎（bdma/cacc/cdma/cdp/cmac/csc/cvif/glb/mcif/pdp/rubik/sdp）都有一个对应的 `NV_NVDLA_<engine>.cpp`，外加 `gen/*_reg_model.cpp`（自动生成的寄存器模型）、`hls/vlibs/*.cpp`（浮点库，与 `vmod/vlibs/HLS_*` 同源）、`hls/sdp/*.cpp`、`hls_wrapper/*.cpp`（HLS 包装器）等。

编译选项里能看到 SystemC 的痕迹：

[cmod/Makefile:153](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/cmod/Makefile#L153) —— `CPPFLAGS` 含 `-DSC_INCLUDE_DYNAMIC_PROCESSES -std=c++11`，前者是 SystemC 动态进程所需，后者是 C++11 标准。

最后看构建依赖图里 cmod 的位置：

[tools/etc/build.config:10-15](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/tools/etc/build.config#L10-L15) —— `cmod_top` 的 sandbox 是 `cmod`，依赖 `defs` 与 `manual`。注意全树里**没有任何 sandbox 依赖 `cmod_top`**（它是叶子）：这从构建角度印证了「cmod 产物在本仓库内不被消费」，消费方在外部。

cmod 的配置头与 RTL 共享同一份 spec 真相：

[cmod/include/nvdla_config.h:11-13](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/cmod/include/nvdla_config.h#L11-L13) —— 用 `NVDLA_CONFIG_SMALL` 等宏选择配置规模；这些宏对应 RTL 侧 `spec/defs` 的特性集（如 MAC 原子尺寸）。

[cmod/include/nvdla_config.h:39-43](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/cmod/include/nvdla_config.h#L39-L43) —— 根据 `NVDLA_CONFIG_LARGE/SMALL` 包含不同的详细配置头，使 cmod 与 RTL 在「规格参数」上保持一致。

#### 4.1.4 代码实践

**实践目标**：用 `build.config` 与 `cmod/Makefile` 这两个真实文件，手工还原「cmod 依赖什么、产出什么」，而不依赖任何虚构脚本。

**操作步骤**：

1. 打开 `tools/etc/build.config`，定位 `cmod_top:` 段，记下它的 `dependencies`（应为 `defs`、`manual`）。
2. 在同一文件里搜索 `cmod_top` 是否出现在任何其它段的 `dependencies:` 列表里（应搜不到，确认它是叶子）。
3. 打开 `cmod/Makefile`，定位 `TARGET`、`SYSTEMC_LIBRARIES`、`SRCS` 三个变量。
4. 在 `SRCS` 里数一下：对应 RTL 引擎的 `NV_NVDLA_<engine>.cpp` 有多少个；`gen/*_reg_model.cpp` 有多少个；`hls/vlibs/*.cpp` 有多少个。

**需要观察的现象**：

- `cmod_top` 是叶子节点 → cmod 的 `.so` 不被本仓库其它构建步骤消费。
- `SRCS` 里引擎 `.cpp` 的名字与 `vmod/nvdla/` 下的子目录名几乎一一对应（bdma/cdma/csc/cmac/cacc/sdp/pdp/cdp/rubik/glb/mcif/cvif/cbuf/csb_master）。
- 注意 cmod 里**没有** `apb2csb`、`car`、`top` 对应的引擎 `.cpp`：`apb2csb` 是外部 SoC 桥（cmod 用 `NvdlaCsbAdaptor` 替代），`car`（时钟/复位）不体现功能行为，`top` 被 `nvdla_top/` 下的几个文件替代。

**预期结果**：你会得到一张「cmod 源码 ↔ vmod 引擎」的对应表，并确认 cmod 是一条独立的、产出 `.so` 的构建支线。

**待本地验证**：若你的环境装了 SystemC 并在 `tree.make` 里设好 `SYSTEMC`，可在 `cmod/` 下尝试 `make`；若未装 SystemC，`make` 会在 [cmod/Makefile:6-8](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/cmod/Makefile#L6-L8) 处报 `SYSTEMC variable must be defined`——这是预期行为，不是错误。

#### 4.1.5 小练习与答案

**练习 1**：为什么 cmod 用 TLM 的 `b_transport` 而不是逐拍信号翻转？

**参考答案**：参考模型的目标是「快速算出正确的期望值」，而非复现精确时序。一次 `b_transport` 直接完成一次完整访问，省去了逐周期握手与流水延迟，仿真速度高几个数量级；时序正确性应由 RTL 仿真本身保证，不归参考模型管。

**练习 2**：`cmod_top` 依赖 `defs` 和 `manual`，这说明了什么？

**参考答案**：说明 cmod 的「特性配置」和「寄存器规格」与 RTL 同源——都来自 `spec/defs`（特性宏）和 `spec/manual`（SystemRDL）。这正是 cmod 能与 RTL 做**位精确比对**的前提：两者用同一份规格真值，配置参数和寄存器字段定义完全一致。

---

### 4.2 nvdla_core：引擎的中央例化与连线

#### 4.2.1 概念说明

`NV_NVDLA_core` 是 cmod 的「中央骨架」，对应 RTL 里把各分区连起来的顶层连线。它的职责只有两件事：

1. **例化**全部引擎（csb_master、bdma、mcif、cvif、glb、cdma、cbuf、csc、cmac_a/b、cacc、sdp、pdp、cdp、rubik 等）。
2. **连线**：用 TLM socket 把这些引擎按数据通路接起来——CSB 配置怎么分发、MCIF/CVIF 怎么接各引擎的 DMA、卷积五级怎么串、done 中断怎么汇到 glb。

这与 RTL 的 `NV_NVDLA_partition_o/c/...` 例化与 wire 连接是同构的，只是把「wire + valid/ready」换成了「TLM socket + b_transport」。

在它之上还有一层 `NV_nvdla`（`cmod/nvdla_top/`）：它包住 `NV_NVDLA_core`，再加上 CSB 适配器和两个 AXI 适配器，对外暴露与 RTL 顶层同名的端口（`nvdla_core2dbb_axi4`、`nvdla_core2cvsram_axi4`、CSB 口、中断）。这样 cmod 就能「插」进和 RTL 一样的测试平台端口位置。

每个引擎的 C 模型遵循统一的「三层」结构：

- **base**（`NV_NVDLA_<engine>_base`，自动生成）：声明端口与 TLM socket。
- **reg_model**（`gen/<engine>_reg_model`，自动生成）：寄存器访问器，解析 CSB 写、维护字段值、抛事件。
- **手写功能核**（如 `BdmaCore`）：真正的算法行为，用 `SC_THREAD/SC_METHOD` + `sc_fifo` 描述流水。

#### 4.2.2 核心流程

引擎类用 C++ 多继承把这三层粘起来。以 BDMA 为例：

```
class NV_NVDLA_bdma:
    public  NV_NVDLA_bdma_base,   // 端口与 socket（自动生成）
    private bdma_reg_model        // 寄存器访问（自动生成）
{
    BdmaCore *bdma_core;          // 手写功能核
    SC_THREAD(OperationEnableTriggerThread);  // 行为线程
    ...
};
```

数据/控制流（以 CSB 配置一次 BDMA 为例）：

1. 外部 CSB 事务到达 `csb2bdma_req` target socket。
2. `bdma_reg_model`（基类）解析地址、把字段写入 `cfg_*` 变量，并在写 `OP_ENABLE`/`LAUNCH` 时抛 `operation_enable_event_` / `launch_grp0_event_`。
3. BDMA 的 `SC_THREAD`（如 `OperationEnableTriggerThread`）被事件唤醒，把配置打包成 `BdmaCoreConfig` 推进 `bdma_core_config_fifo_`。
4. 手写的 `BdmaCore` 从 FIFO 取配置，发起 `bdma2mcif_rd_req` 等读/写 socket 调用（TLM 事务），完成搬运。
5. 完成后经 `bdma2glb_done_intr` 上报中断给 glb。

`NV_NVDLA_core::Initialize()` 里则集中做「例化 + 连线」：先 `new` 出所有引擎，再把它们的 socket 一一 `.bind()` / 调用式连接，复刻 RTL 的拓扑。

#### 4.2.3 源码精读

先看 `NV_NVDLA_core` 的类声明——它继承自 `NV_NVDLA_core_base`，并持有一组引擎指针：

[cmod/nvdla_core/NV_NVDLA_core.h:15-16](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/cmod/nvdla_core/NV_NVDLA_core.h#L15-L16) —— 引入 `systemc.h` 与 `tlm.h`，说明这是 SystemC+TLM 模块。

[cmod/nvdla_core/NV_NVDLA_core.h:56-58](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/cmod/nvdla_core/NV_NVDLA_core.h#L56-L58) —— `class NV_NVDLA_core: public NV_NVDLA_core_base`，提供 `Initialize()` 与构造/析构。

[cmod/nvdla_core/NV_NVDLA_core.h:72-89](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/cmod/nvdla_core/NV_NVDLA_core.h#L72-L89) —— 子单元指针声明，分三类：接口模块（csb_master/bdma/rbk/mcif/cvif/glb）、卷积核心（cdma/cbuf/csc/cmac_a/cmac_b/cacc）、后处理（sdp/pdp/cdp），外加 `core_dummy`。这与 RTL 引擎集合几乎一致。

[cmod/nvdla_core/NV_NVDLA_core.h:99-119](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/cmod/nvdla_core/NV_NVDLA_core.h#L99-L119) —— 对外 AXI TLM socket：MCIF 与 CVIF 各有「写请求/写响应/读请求/读响应」四组 `multi_passthrough_*_socket<..., 512>`（512 位宽，对应 RTL memif 的 512 位原子）。

[cmod/nvdla_core/NV_NVDLA_core.h:136-143](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/cmod/nvdla_core/NV_NVDLA_core.h#L136-L143) —— 中断信号 `bdma2glb_done_intr[2]` 等，每个引擎两路（对应 producer/consumer 两组影偶，与 u2-l4 一致）。

再看 `Initialize()` 如何例化与连线：

[cmod/nvdla_core/NV_NVDLA_core.cpp:101-127](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/cmod/nvdla_core/NV_NVDLA_core.cpp#L101-L127) —— 集中 `new` 出全部引擎；注意 mcif/cvif 的构造带了 `nvdla_id`，且按 `NVDLA_REFERENCE_MODEL_ENABLE` 传不同的 `monitor` 布尔（4.3 节细讲）。

CSB 配置的分发——对应 RTL 里 csb_master 把请求扇出到各引擎（u2-l2）：

[cmod/nvdla_core/NV_NVDLA_core.cpp:311-327](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/cmod/nvdla_core/NV_NVDLA_core.cpp#L311-L327) —— `csb_master->csb2bdma_req(bdma->csb2bdma_req)`、`csb2cdma_req`、…、`csb2glb_req`、`csb2gec_req`，把 csb_master 的请求 socket 逐一接到各引擎；cvif/mcif 的 CSB 口则接到 `core_dummy`。

MCIF 的客户端绑定——对应 RTL 里各引擎 DMA 接到 MCIF（u4-l1/u4-l2）：

[cmod/nvdla_core/NV_NVDLA_core.cpp:346-362](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/cmod/nvdla_core/NV_NVDLA_core.cpp#L346-L362) —— bdma/cdma_dat/cdma_wt/sdp(及 sdp_b/n/e)/pdp/cdp/rbk 的读请求与写请求分别接到 mcif 的对应 socket，复刻 RTL 的存储接口拓扑。

卷积主流水线的串联——对应 RTL 的 `c→m→a→p` 链（u3-l1）：

[cmod/nvdla_core/NV_NVDLA_core.cpp:466-491](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/cmod/nvdla_core/NV_NVDLA_core.cpp#L466-L491) —— CDMA↔CBUF、CDMA↔CSC、CBUF↔CSC、CSC↔CMAC_a/b、CMAC↔CACC、CACC 的 `accu2sc_credit` 反压回 CSC、CACC→SDP、SDP→PDP，与 RTL 的卷积数据通路一一对应。

再看顶层 `NV_nvdla` 如何包住 core 并加适配器：

[cmod/nvdla_top/NV_nvdla.h:25-42](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/cmod/nvdla_top/NV_nvdla.h#L25-L42) —— `class NV_nvdla : public NV_nvdla_base`，私有持有 `nvdla_core`、`csb_adaptor`、`axi_adaptor_mc`、`axi_adaptor_cv`、`nvdla_top_dummy`。

[cmod/nvdla_top/NV_nvdla.h:46](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/cmod/nvdla_top/NV_nvdla.h#L46) —— `extern "C" NV_nvdla * NV_nvdlaCon(...)`，这是外部框架 dlopen `.so` 后用来实例化顶层的工厂函数。

[cmod/nvdla_top/NV_nvdla.cpp:21-37](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/cmod/nvdla_top/NV_nvdla.cpp#L21-L37) —— 构造里 `new` 出 CSB/AXI 适配器与 `NV_NVDLA_core("nvdla_core", 1)`。

[cmod/nvdla_top/NV_nvdla.cpp:40-55](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/cmod/nvdla_top/NV_nvdla.cpp#L40-L55) —— 把外部 CSB/AXI 端口经适配器接到 `nvdla_core`：`axi_adaptor_mc->standard_axi.bind(nvdla_core2dbb_axi4)`、`axi_adaptor_cv->standard_axi.bind(nvdla_core2cvsram_axi4)`，使 cmod 对外暴露与 RTL 同名的 AXI 端口。

CSB 适配器把外部 TLM 事务翻译成内部 CSB 事务（cmod 里替代 `apb2csb`）：

[cmod/nvdla_top/NvdlaCsbAdaptor.cpp:35-62](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/cmod/nvdla_top/NvdlaCsbAdaptor.cpp#L35-L62) —— `csb_nvdla_bus_cb` 把一次 4 字节对齐的 TLM 读写翻译成 `csb2xx_16m_secure_be_lvl_t`：`addr = address>>2`（字地址）、`write = gp.is_write()`、写时填 `wdat` 与 `wrbe=0xFFFFFFFF`、`nposted=0`，再 `b_transport` 给 core；读则从 `csb_read_fifo` 取回 `rdat`。这与 u2-l1 讲的 CSB 请求包格式完全吻合。

最后看一个引擎的「三层」结构与 SystemC 行为线程。BDMA 用多继承把 base 与 reg_model 粘起来：

[cmod/bdma/NV_NVDLA_bdma.h:45-48](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/cmod/bdma/NV_NVDLA_bdma.h#L45-L48) —— `class NV_NVDLA_bdma: public NV_NVDLA_bdma_base, private bdma_reg_model`，并实现 CSB/读响应的 `b_transport`。

[cmod/bdma/NV_NVDLA_bdma.cpp:60-70](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/cmod/bdma/NV_NVDLA_bdma.cpp#L60-L70) —— 注册 SystemC 行为：`SC_THREAD(OperationEnableTriggerThread/LaunchGroup*TriggerThread)` 与 `SC_METHOD(UpdateIdleStatus/UpdateFreeSlotNum/ClearInt*Flag)`，敏感于 `core_is_idle`、`core_notify_get_config`、`bdma2glb_done_intr[0/1]`。这是 cmod 用 SystemC 并发描述引擎行为的标准写法。

[cmod/bdma/NV_NVDLA_bdma.cpp:73-87](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/cmod/bdma/NV_NVDLA_bdma.cpp#L73-L87) —— `UpdateIdleStatus`/`UpdateFreeSlotNum` 把 FIFO 空闲槽数与 idle 状态回写给 `bdma_reg_model`（`BdmaUpdateIdleStatus`/`BdmaUpdateFreeConfigSlotNum`），这正是 u4-l4 讲的 BDMA `free_slot`/`status_idle` 寄存器的来源。

自动生成的 reg_model 持有与 RTL 同源的寄存器字段：

[cmod/bdma/gen/bdma_reg_model.h:25-30](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/cmod/bdma/gen/bdma_reg_model.h#L25-L30) —— `class bdma_reg_model`，含 `operation_enable_event_`、`launch_grp0_event_`、`launch_grp1_event_` 等事件——对应写 `OP_ENABLE`/`LAUNCH0/1` 时抛事件唤醒引擎线程。

[cmod/bdma/gen/bdma_reg_model.h:44-72](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/cmod/bdma/gen/bdma_reg_model.h#L44-L72) —— `CNVDLA_BDMA_REGSET *bdma_register_group` 与一批 `cfg_*`/`status_*` 字段：`cfg_src_addr_low_v32_`、`cfg_dst_addr_low_v32_`、`cfg_line_size_`、`cfg_cmd_src_ram_type_`/`cfg_cmd_dst_ram_type_`、`cfg_src_line_stride_`、`cfg_surf_repeat_number_`、`cfg_op_en_`、`cfg_launch0_grp0_launch_`、`status_free_slot_`、`status_idle_`、`status_grp0_busy_`…这些字段与 u4-l4 讲的 BDMA 寄存器（源/目的地址、line_size、ram_type、stride、OP_ENABLE、LAUNCH、free_slot、group busy）逐一对上，证明 cmod 与 RTL 寄存器同源。

#### 4.2.4 代码实践

**实践目标**：在 `NV_NVDLA_core.cpp::Initialize()` 里跟踪一条「CSB 配置请求从 csb_master 到达 BDMA」的连线，并与 RTL 对照。

**操作步骤**：

1. 打开 [cmod/nvdla_core/NV_NVDLA_core.cpp:311-327](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/cmod/nvdla_core/NV_NVDLA_core.cpp#L311-L327)，找到 `csb_master->csb2bdma_req(bdma->csb2bdma_req)` 这一行。
2. 打开 `cmod/bdma/NV_NVDLA_bdma.h`，确认 `csb2bdma_req_b_transport` 是 BDMA 侧接收 CSB 请求的入口。
3. 打开 [cmod/bdma/gen/bdma_reg_model.h:44-72](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/cmod/bdma/gen/bdma_reg_model.h#L44-L72)，挑出 `cfg_op_en_` 与 `status_free_slot_` 两个字段。
4. 对照 u4-l4 的 BDMA 寄存器表，确认这两个字段对应的寄存器语义。

**需要观察的现象**：

- cmod 里 csb_master → bdma 的连接是一行 `.bind()`，等价于 RTL 里 csb_master 的 `csb2bdma_req_pvld/prdy/pd` 连到 bdma 寄存器文件。
- `cfg_op_en_`/`status_free_slot_` 的名字与 u4-l4 描述完全一致——同一份寄存器规格在 RTL 与 cmod 两处落地。

**预期结果**：你能画出 cmod 里 CSB 请求从 `csb_master` 经 `csb2bdma_req` socket 进入 `bdma_reg_model`、最终唤醒 `BdmaCore` 线程的链路，并确认它与 RTL 的寄存器接口语义一致。

#### 4.2.5 小练习与答案

**练习 1**：cmod 里为什么没有 `apb2csb` 模块？

**参考答案**：`apb2csb` 是把外部 APB 桥接成 CSB 的 SoC 集成桥，属于「外部接口转换」，不是 NVDLA 自身的功能行为。cmod 用 `NvdlaCsbAdaptor` 把外部 TLM/AXI 事务直接译成 CSB 事务（见 [NvdlaCsbAdaptor.cpp:35-62](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/cmod/nvdla_top/NvdlaCsbAdaptor.cpp#L35-L62)），因此不需要单独的 apb2csb 引擎。

**练习 2**：BDMA 引擎类为什么用 `public NV_NVDLA_bdma_base, private bdma_reg_model` 这种多继承？

**参考答案**：`base` 提供端口与 TLM socket（自动生成、对外可见，故 public）；`reg_model` 提供寄存器访问能力，是引擎内部实现细节（故 private 继承）。这样把「接口」「寄存器」「手写行为核」三者解耦：寄存器规格变了只重新生成 `gen/*_reg_model`，端口变了只重新生成 `*_base`，手写的 `BdmaCore` 行为不动。

**练习 3**：`NV_NVDLA_core.cpp` 里 `cmac_a` 和 `cmac_b` 是同一个类 `NV_NVDLA_cmac` 的两个实例，这对应 RTL 的什么设计？

**参考答案**：对应 RTL 里 CMAC 乘加阵列分 a/b 两半的设计（u3-l5）：两半共享广播的特征 dat、各用不同权重 wt，同拍并行算两组输出通道。cmod 用两个 `NV_NVDLA_cmac` 实例复刻这一结构，CSC 分别把 `sc2mac_dat_a/b`、`sc2mac_wt_a/b` 接到两半。

---

### 4.3 黄金比对：reference model 与比对流程

#### 4.3.1 概念说明

「黄金比对」是 cmod 存在的根本理由。其思想可以用一个对等式概括：

\[ \text{一致} \iff \text{RTL 输出}(x) = \text{cmod 输出}(x), \quad \forall x \in \text{激励空间} \]

即对同一份激励 \(x\)，若 RTL 与 cmod 产出完全相同的输出，则认为 RTL 正确（在 cmod 本身可信的前提下）。比对粒度通常是「存储写事务」：每个引擎最终把结果写回 DBB/CVSRAM，cmod 也写一份期望值，框架逐个事务比对其地址与数据。

cmod 提供了两种角色，由编译宏 `NVDLA_REFERENCE_MODEL_ENABLE` 切换：

- **关闭时（默认）**：cmod 是一个**独立的功能模型**，自己跑完整个网络，产出结果——可用于「只跑 cmod、不跑 RTL」的快速功能验证或调试。
- **开启时**：cmod 额外挂上一个 `NvdlaCoreInternalMonitor`（内部监视器），它像「探针」一样并接在所有 DMA socket 与卷积/后处理数据通路上，能观测每一笔事务、每一段数据，从而与 RTL 的对应信号做比对。

这个 `internal_monitor` 就是「黄金探针」的接入点：它把 cmod 内部每一步的中间数据暴露出来，供外部框架与 RTL 的对应点对齐比对。

**再次诚实说明**：`NvdlaCoreInternalMonitor` 的类定义（`NvdlaCoreInternalMonitor.h`）在本仓库**未被 include 进发布**（[NV_NVDLA_core.h:43-46](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/cmod/nvdla_core/NV_NVDLA_core.h#L43-L46) 用 `#ifdef NVDLA_REFERENCE_MODEL_ENABLE` 保护，且该宏默认不定义）；驱动 RTL 与 cmod 并自动 diff 的外部框架也未随仓库开源。所以本讲讲清的是「cmod 为比对预留了哪些接入点」「比对的思想是什么」，而非仓库里现成可跑的比对脚本。

#### 4.3.2 核心流程

典型（外部框架下的）黄金比对流程：

1. **同源配置**：`spec/defs` 与 `spec/manual` 同时喂给 RTL 与 cmod，保证两者配置宏、寄存器字段一致。
2. **同激励驱动**：同一份 trace（u7-l2 讲的 CSB 寄存器写序列）同时送进 RTL testbench 与 cmod（cmod 侧由 `NvdlaCsbAdaptor` 接收）。
3. **探针挂接**：cmod 以 `NVDLA_REFERENCE_MODEL_ENABLE` 编译，`NV_NVDLA_core` 创建 `internal_monitor`，把各引擎的 DMA socket 与数据通路并接到 monitor 上。
4. **期望值采集**：cmod 跑完，monitor 把每笔存储写（地址+数据）作为期望值上报。
5. **逐事务比对**：外部框架把 RTL 实际写到 DBB/CVSRAM 的事务与 cmod 期望值按地址对齐，逐拍/逐事务比较数据。
6. **定位差异**：首个不一致点即线索，结合卷积五级/后处理通路回溯到具体引擎。

为什么 cmod 能做位精确比对？因为两点同源：

- **寄存器同源**：cmod 的 `gen/*_reg_model` 与 RTL 的 `*_CSB_reg.v`/`*_dual_reg.v` 都由 `spec/manual` 的 SystemRDL 经 Ordt 生成（见 u8-l2）。
- **数值同源**：cmod 的 `hls/vlibs/*.cpp`（fp16/fp17/fp32 加减乘、格式互转）与 RTL 的 `vmod/vlibs/HLS_*` 来自同一份 Catapult HLS 源（见 u6-l4），浮点运算语义一致。

#### 4.3.3 源码精读

`NVDLA_REFERENCE_MODEL_ENABLE` 宏保护的 reference-model 接入点：

[cmod/nvdla_core/NV_NVDLA_core.h:42-47](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/cmod/nvdla_core/NV_NVDLA_core.h#L42-L47) —— `#ifdef NVDLA_REFERENCE_MODEL_ENABLE` 下才 include `NvdlaCoreInternalMonitor.h` 与 `nitro_scsv_converter.h`，说明这是「参考模型模式」的专用接入。

[cmod/nvdla_core/NV_NVDLA_core.h:90-94](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/cmod/nvdla_core/NV_NVDLA_core.h#L90-L94) —— 同宏保护下声明 `NvdlaCoreInternalMonitor *internal_monitor`，以及四组 monitor socket（dma_monitor_mc/cv、convolution_core_monitor_initiator、post_processing_monitor_initiator）与对应 credit target socket。

[cmod/nvdla_core/NV_NVDLA_core.h:147-159](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/cmod/nvdla_core/NV_NVDLA_core.h#L147-L159) —— 四组 monitor initiator socket 声明，分别监视 DMA（MC/CV）、卷积核心、后处理通路——这正是「黄金探针」的连接面。

`Initialize()` 里 mcif/cvif 按是否启用参考模型构造：

[cmod/nvdla_core/NV_NVDLA_core.cpp:108-114](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/cmod/nvdla_core/NV_NVDLA_core.cpp#L108-L114) —— 启用参考模型时 `new NV_NVDLA_mcif("mcif", true, nvdla_id)`（第二个参数 `true` 即 monitor 模式），否则传 `false`。

创建 monitor 并把所有 DMA 探针并接上去（这是 cmod 黄金比对的核心接线）：

[cmod/nvdla_core/NV_NVDLA_core.cpp:128-137](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/cmod/nvdla_core/NV_NVDLA_core.cpp#L128-L137) —— `new NvdlaCoreInternalMonitor("internal_monitor")`，并设 `cdma_wt_dma_arbiter_override_enable = true`（参考模型模式下强制 CDMA 权重仲裁源选择，便于确定性比对）。

[cmod/nvdla_core/NV_NVDLA_core.cpp:168-204](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/cmod/nvdla_core/NV_NVDLA_core.cpp#L168-L204) —— 把 CVIF 的 10 路读 DMA（BDMA/SDP/PDP/CDP/RBK/SDP_B/N/E/CDMA_DAT/CDMA_WT）与 5 路写 DMA 的请求/响应 socket 全部并接到 `internal_monitor` 上。注意这段由 eperl 模板生成（`//: for my $index ...`），与 u6-l5 讲的 eperl 生成机制一致。

[cmod/nvdla_core/NV_NVDLA_core.cpp:231-266](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/cmod/nvdla_core/NV_NVDLA_core.cpp#L231-L266) —— 同样把 MCIF 的全部读/写 DMA 并接到 `internal_monitor`。

[cmod/nvdla_core/NV_NVDLA_core.cpp:267-288](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/cmod/nvdla_core/NV_NVDLA_core.cpp#L267-L288) —— 卷积核心与后处理的数据通路（`sc2mac_dat/wt_a/b`、`mac_a/b2accu`、`cacc2sdp`、`sdp2pdp`）也并接到 monitor，并把四组 monitor initiator/credit socket 挂到顶层。这样 monitor 能观测从取数到输出的整条通路。

数值同源的证据——cmod 的浮点库与 RTL 同名同语义：

[cmod/Makefile:78-97](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/cmod/Makefile#L78-L97) —— `hls/vlibs/` 下编译 `fp16_*`、`fp17_*`、`fp32_*` 加减乘与互转，对应 `vmod/vlibs/HLS_fp*`（u6-4）。cmod 用与 RTL 完全相同的浮点语义计算，这是位精确比对的前提。

#### 4.3.4 代码实践

**实践目标**：在源码里定位 cmod 为「黄金比对」预留的全部接入点，画出探针覆盖范围，并诚实标注哪些部分不在本仓库。

**操作步骤**：

1. 在 [cmod/nvdla_core/NV_NVDLA_core.cpp](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/cmod/nvdla_core/NV_NVDLA_core.cpp) 里搜索所有 `#ifdef NVDLA_REFERENCE_MODEL_ENABLE` 块，数出它们分别接了哪几类探针（CVIF DMA、MCIF DMA、卷积核心、后处理）。
2. 在 `cmod/` 全树搜索 `NvdlaCoreInternalMonitor.h` 是否存在（用 `Glob` 搜 `cmod/**/NvdlaCoreInternalMonitor*`）。
3. 在 `verif/` 与 `tools/` 下搜索 `libnvdla_cmod`、`NV_nvdlaCon`、`NVDLA_REFERENCE_MODEL` 是否有引用。

**需要观察的现象**：

- 第 1 步应看到四类探针接入：CVIF 的 10 读 + 5 写、MCIF 的 10 读 + 5 写、卷积核心 4 条线（dat/wt × a/b）、后处理 2 条线（cacc2sdp、sdp2pdp）。
- 第 2 步：`NvdlaCoreInternalMonitor.h` 在本仓库**找不到**（它仅在 `#ifdef` 内被 include，头文件本身未发布）——这是诚实边界。
- 第 3 步：`verif/` 与 `tools/` 下**没有**对 `libnvdla_cmod`/`NV_nvdlaCon` 的引用（只有 `tools/etc/build.config` 登记了 `cmod_top` 这个构建目标）——再次印证消费框架不在本仓库。

**预期结果**：你得到一张「cmod 探针覆盖图」，并明确写出：「接入点在仓库内、monitor 实现与比对框架在仓库外」。

**待本地验证**：第 2、3 步的搜索结果是确定可复现的；若你发现某文件存在或不存在，以你本地 `git ls-files` 的实际输出为准。

#### 4.3.5 小练习与答案

**练习 1**：为什么 cmod 能与 RTL 做**位精确**比对，而不是「大约一致」？

**参考答案**：因为两者在两个关键维度同源——寄存器规格都来自 `spec/manual` 的 SystemRDL（cmod 的 `gen/*_reg_model` 与 RTL 的 `*_CSB_reg.v` 同源），浮点数值都来自同一份 Catapult HLS 源（cmod 的 `hls/vlibs/*.cpp` 与 RTL 的 `vmod/vlibs/HLS_*` 同源）。同源保证了字段定义与运算语义完全一致，因而可比对到每一位。

**练习 2**：`NVDLA_REFERENCE_MODEL_ENABLE` 关闭和开启时，cmod 的行为有何不同？

**参考答案**：关闭时（默认），mcif/cvif 以 `monitor=false` 构造，不创建 `internal_monitor`，cmod 是一个独立的功能模型，自己跑完网络产出结果，可用于纯软件功能验证。开启时，mcif/cvif 以 `monitor=true` 构造，额外创建 `internal_monitor` 并把所有 DMA 与数据通路并接上去，使 cmod 同时充当「黄金探针」，向外部框架暴露每一步中间数据供比对。

**练习 3**：`internal_monitor` 把探针并接在 DMA socket 上而不是引擎内部寄存器上，有什么好处？

**参考答案**：DMA socket 是引擎与存储之间的标准 TLM 接口，粒度统一（每笔事务 = 一次地址+数据访问），且各引擎都有。在socket 层挂探针，既能观测引擎「实际读写存储」的最终行为（这正是要与 RTL 比对的输出），又不需要侵入每个引擎的内部实现，解耦了 monitor 与引擎算法细节。

## 5. 综合实践

把本讲三个模块串起来，完成一个「cmod 全景阅读」任务：

1. **建立对应表**：打开 `cmod/Makefile` 的 `SRCS`（[cmod/Makefile:21-97](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/cmod/Makefile#L21-L97)），为每个 `NV_NVDLA_<engine>.cpp` 找到 `vmod/nvdla/<engine>/` 下的 RTL 对应模块，写成两列对照表。标注 cmod 缺失的引擎（apb2csb/car）及原因。

2. **追踪一条端到端通路**：从 [NV_nvdla.cpp:40-55](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/cmod/nvdla_top/NV_nvdla.cpp#L40-L55) 的 CSB 入口出发，经 [NvdlaCsbAdaptor.cpp:35-62](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/cmod/nvdla_top/NvdlaCsbAdaptor.cpp#L35-L62) 译成 CSB 事务，到 [NV_NVDLA_core.cpp:311-327](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/cmod/nvdla_core/NV_NVDLA_core.cpp#L311-L327) 分发到 BDMA，再到 [NV_NVDLA_core.cpp:346-362](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/cmod/nvdla_core/NV_NVDLA_core.cpp#L346-L362) 的 BDMA→MCIF 读请求，画出这条「配置→搬运」链路图，并在每个节点旁标注对应的 RTL 信号/模块。

3. **标注黄金探针**：在步骤 2 的链路图上，用另一种颜色标出 `NVDLA_REFERENCE_MODEL_ENABLE` 开启时 `internal_monitor` 并接的位置（参考 [NV_NVDLA_core.cpp:168-288](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/cmod/nvdla_core/NV_NVDLA_core.cpp#L168-L288)），并写一句话说明这些探针点为何适合做比对。

4. **诚实记录边界**：在图末尾写明「cmod 源码与构建在本仓库内；`NvdlaCoreInternalMonitor` 实现与 RTL↔cmod 自动比对框架不在本仓库内（见 README）」。

**预期结果**：一张完整的 cmod 数据通路图，含 RTL 对照与黄金探针标注，且边界说明清晰。

## 6. 本讲小结

- `cmod/` 是 NVDLA 的 SystemC/TLM 参考模型，与 `vmod/` RTL 描述同一份规格，但用事务级 `b_transport` 代替逐拍信号，编译产物是 `libnvdla_cmod.so`。
- `NV_NVDLA_core` 是中央骨架：例化全部引擎并用 TLM socket 连线，拓扑与 RTL 的分区连线同构（CSB 分发、MCIF/CVIF 接 DMA、卷积五级串联、done 中断汇 glb）。
- 顶层 `NV_nvdla` 包住 core + CSB/AXI 适配器，对外暴露与 RTL 同名端口，`extern "C" NV_nvdlaCon` 是外部框架实例化的工厂函数。
- 每个引擎遵循「base（端口，自动生成）+ reg_model（寄存器，自动生成）+ 手写功能核」三层结构，行为用 `SC_THREAD/SC_METHOD` + `sc_fifo` 描述。
- 黄金比对由 `NVDLA_REFERENCE_MODEL_ENABLE` 切换：开启时创建 `internal_monitor`，把所有 DMA 与卷积/后处理数据通路并接成探针，供外部框架与 RTL 逐事务比对。
- cmod 能做位精确比对，靠的是与 RTL 同源（寄存器来自 SystemRDL、浮点来自同一 HLS 源）；但消费 cmod 的 monitor 实现与比对框架未随本仓库开源，这是阅读时必须诚实面对的边界。

## 7. 下一步学习建议

- **向回印证**：对照 u8-l2（RDL/Ordt 寄存器生成）看 `spec/manual/test.rdl` 如何同时生成 RTL 的 `NV_NVDLA_GLB_CSB_reg.v` 与 cmod 的 `gen/glb_reg_model.cpp`，体会「单一可信源」如何同时喂养 RTL 与 cmod。
- **向深读引擎**：挑一个引擎（如 `cmod/cdma/NV_NVDLA_cdma.cpp` 与 `cmod/bdma/BdmaCore.cpp`）精读，看 cmod 如何用高层算法实现 u3-l2/u4-l4 描述的取数/搬运行为，并与 RTL 对照差异。
- **向横看数值**：读 `cmod/hls/vlibs/fp17_mul.cpp` 与 `vmod/vlibs/HLS_fp17_mul.v`（u6-l4），确认两者浮点语义一致，理解位精确比对的数值基础。
- **向回看验证**：结合 u7-l1/u7-l2 的 RTL testbench，理解「trace-player 直驱 RTL」与本讲「cmod 并行参照」是互补的两条验证线——前者验时序与可运行性，后者验数值正确性。
- **继续单元 7**：下一篇 u7-l4 讲 Verilator 开源仿真路径，是另一条不依赖 VCS 的 RTL 仿真支线，与本讲的 cmod 一起构成 NVDLA 的「开源验证生态」。
